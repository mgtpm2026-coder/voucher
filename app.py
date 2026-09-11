
import io, os, re, json, random, shutil, string, tempfile, zipfile
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta
from functools import wraps

import mysql.connector
import pandas as pd
from PIL import Image as PILImage
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

import notifications

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "CHANGE_THIS_SECRET_KEY")
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "pdf", "webp"}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
PROFILE_FOLDER = os.path.join(app.static_folder, "uploads", "profiles")
os.makedirs(PROFILE_FOLDER, exist_ok=True)

def db():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", "root"),
        database=os.getenv("DB_NAME", "voucher_db"),
    )

def query(sql, params=(), one=False):
    conn=db(); cur=conn.cursor(dictionary=True)
    try:
        cur.execute(sql, params)
        rows=cur.fetchone() if one else cur.fetchall()
        return rows
    finally:
        cur.close(); conn.close()

def execute(sql, params=()):
    conn=db(); cur=conn.cursor()
    try:
        cur.execute(sql, params); conn.commit()
        return cur.lastrowid
    finally:
        cur.close(); conn.close()

def money(v):
    try: return Decimal(str(v or 0)).quantize(Decimal("0.01"))
    except: return Decimal("0.00")

def payment_status(total, paid):
    total, paid = money(total), money(paid)
    if paid <= 0: return "Not Paid"
    if paid >= total: return "Fully Paid"
    return "Partially Paid"

def voucher_view(v):
    paid = query("SELECT COALESCE(SUM(amount),0) paid FROM payments WHERE voucher_id=%s", (v["id"],), True)["paid"]
    v["paid_amount"]=money(paid)
    v["balance"]=max(Decimal("0.00"), money(v["amount"])-v["paid_amount"])
    v["payment_status"]=payment_status(v["amount"], v["paid_amount"])
    return v

def allowed_file(name):
    return "." in name and name.rsplit(".",1)[1].lower() in ALLOWED_EXTENSIONS

def employee_folder(name):
    return os.path.join(UPLOAD_FOLDER, "Employees", secure_filename(name) or "Employee")

def voucher_folder(employee, date_value, voucher_no):
    try:
        month = datetime.strptime(str(date_value)[:10], "%Y-%m-%d").strftime("%Y-%m")
    except:
        month = datetime.now().strftime("%Y-%m")
    path=os.path.join(employee_folder(employee), month, secure_filename(voucher_no))
    os.makedirs(path, exist_ok=True)
    return path

def save_upload(file, employee, date_value, voucher_no, prefix="Bill"):
    if not file or not file.filename: return ""
    if not allowed_file(file.filename): raise ValueError("Unsupported file type.")
    ext=file.filename.rsplit(".",1)[1].lower()
    filename=f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}"
    path=os.path.join(voucher_folder(employee,date_value,voucher_no),filename)
    file.save(path)
    return os.path.relpath(path, UPLOAD_FOLDER).replace("\\","/")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")  # allows any TLD length, e.g. name@company.in

def valid_email(email):
    return bool(email) and bool(EMAIL_RE.match(email.strip()))

_ONES = ["","One","Two","Three","Four","Five","Six","Seven","Eight","Nine","Ten",
         "Eleven","Twelve","Thirteen","Fourteen","Fifteen","Sixteen","Seventeen","Eighteen","Nineteen"]
_TENS = ["","","Twenty","Thirty","Forty","Fifty","Sixty","Seventy","Eighty","Ninety"]

def _two_digit_words(n):
    if n < 20: return _ONES[n]
    return _TENS[n//10] + (" "+_ONES[n%10] if n%10 else "")

def _three_digit_words(n):
    if n >= 100:
        return _ONES[n//100] + " Hundred" + (" " + _two_digit_words(n%100) if n%100 else "")
    return _two_digit_words(n)

def number_to_words_inr(amount):
    """Indian numbering system: ... Crore, Lakh, Thousand, Hundred."""
    amount = money(amount)
    rupees, paise = int(amount), int((amount - int(amount)) * 100)
    if rupees == 0:
        words = "Zero"
    else:
        n = rupees
        crore, n = divmod(n, 10000000)
        lakh, n = divmod(n, 100000)
        thousand, n = divmod(n, 1000)
        hundred = n
        parts = []
        if crore: parts.append(_three_digit_words(crore) + " Crore")
        if lakh: parts.append(_three_digit_words(lakh) + " Lakh")
        if thousand: parts.append(_three_digit_words(thousand) + " Thousand")
        if hundred: parts.append(_three_digit_words(hundred))
        words = " ".join(parts)
    result = f"Rupees {words} Only"
    if paise:
        result = f"Rupees {words} and {_two_digit_words(paise)} Paise Only"
    return result

def save_profile_image(file, user_type, user_id):
    if not file or not file.filename:
        return None
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in IMAGE_EXTENSIONS:
        raise ValueError("Profile photo must be a JPG, PNG or WEBP image.")
    filename = f"{user_type}_{user_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.{ext}"
    file.save(os.path.join(PROFILE_FOLDER, filename))
    return f"uploads/profiles/{filename}"

def generate_otp():
    return "".join(random.choices(string.digits, k=6))

def create_otp(user_type, user_id, purpose, payload=None):
    otp_code = generate_otp()
    expires_at = datetime.now() + timedelta(minutes=10)
    execute("""INSERT INTO otp_codes(user_type,user_id,purpose,otp_code,payload,expires_at,verified,created_at)
               VALUES(%s,%s,%s,%s,%s,%s,0,NOW())""",
            (user_type, user_id, purpose, otp_code, json.dumps(payload or {}), expires_at))
    return otp_code

def latest_otp(user_type, user_id, purpose):
    return query("""SELECT * FROM otp_codes WHERE user_type=%s AND user_id=%s AND purpose=%s
                     AND verified=0 ORDER BY id DESC LIMIT 1""", (user_type, user_id, purpose), True)

def verify_otp_code(user_type, user_id, purpose, code):
    row = latest_otp(user_type, user_id, purpose)
    if not row:
        return None, "No pending verification found. Please request a new OTP."
    if datetime.now() > row["expires_at"]:
        return None, "This OTP has expired. Please request a new one."
    if str(row["otp_code"]) != str(code).strip():
        return None, "Incorrect OTP. Please try again."
    execute("UPDATE otp_codes SET verified=1 WHERE id=%s", (row["id"],))
    return json.loads(row["payload"] or "{}"), None

def next_voucher_no():
    row=query("SELECT voucher_no FROM vouchers ORDER BY id DESC LIMIT 1", one=True)
    if not row: return "MGT-V-0001"
    m=re.search(r"(\d+)$", row["voucher_no"])
    n=int(m.group(1))+1 if m else 1
    return f"MGT-V-{n:04d}"

def current_user():
    if session.get("role")=="employee":
        return query("SELECT * FROM employees WHERE id=%s", (session["user_id"],), True)
    if session.get("role")=="manager":
        return query("SELECT * FROM managers WHERE id=%s", (session["user_id"],), True)
    return None

def login_required(role=None):
    def deco(f):
        @wraps(f)
        def wrapper(*a,**kw):
            if not session.get("user_id") or (role and session.get("role")!=role):
                flash("Please log in to continue.", "warning")
                return redirect(url_for("login"))
            return f(*a,**kw)
        return wrapper
    return deco

@app.context_processor
def globals():
    return {"current_user": current_user(), "role": session.get("role")}

@app.route("/", methods=["GET","POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    if request.method=="POST":
        username=request.form.get("username","").strip()
        password=request.form.get("password","")
        if not username or not password:
            flash("Username and password are required.", "danger")
            return render_template("login.html")
        user=query("SELECT * FROM employees WHERE username=%s AND is_active=1", (username,), True)
        role="employee"
        if not user:
            user=query("SELECT * FROM managers WHERE username=%s AND is_active=1", (username,), True)
            role="manager"
        if user and check_password_hash(user["password_hash"], password):
            session.clear(); session["user_id"]=user["id"]; session["role"]=role; session["name"]=user["full_name"]
            flash(f"Welcome, {user['full_name']}!", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "danger")
    return render_template("login.html")

@app.route("/dashboard")
@login_required()
def dashboard():
    return redirect(url_for("employee_dashboard" if session["role"]=="employee" else "manager_dashboard"))

@app.route("/logout")
def logout():
    session.clear(); flash("You have been logged out.", "info"); return redirect(url_for("login"))

# ---------------------------------------------------------------------------
# Profile pages (employee + manager) — photo upload, OTP-gated contact/password edits
# ---------------------------------------------------------------------------

def profile_table(role):
    return "employees" if role == "employee" else "managers"

@app.route("/employee/profile")
@login_required("employee")
def employee_profile():
    user = query("SELECT * FROM employees WHERE id=%s", (session["user_id"],), True)
    return render_template("employee_profile.html", user=user)

@app.route("/manager/profile")
@login_required("manager")
def manager_profile():
    user = query("SELECT * FROM managers WHERE id=%s", (session["user_id"],), True)
    return render_template("manager_profile.html", user=user)

@app.route("/profile/photo", methods=["POST"])
@login_required()
def profile_photo():
    table = profile_table(session["role"])
    try:
        path = save_profile_image(request.files.get("photo"), session["role"], session["user_id"])
        if not path:
            flash("Please choose an image to upload.", "danger")
        else:
            execute(f"UPDATE {table} SET profile_image=%s WHERE id=%s", (path, session["user_id"]))
            flash("Profile photo updated.", "success")
    except ValueError as e:
        flash(str(e), "danger")
    return redirect(url_for("employee_profile" if session["role"]=="employee" else "manager_profile"))

@app.route("/profile/request-otp", methods=["POST"])
@login_required()
def profile_request_otp():
    table = profile_table(session["role"])
    user = query(f"SELECT * FROM {table} WHERE id=%s", (session["user_id"],), True)
    purpose = request.form.get("purpose")  # 'profile_update' or 'password_reset'
    if not user.get("phone") and session["role"] == "employee":
        flash("No mobile number on file — ask your manager to add one before using OTP verification.", "danger")
        return redirect(url_for("employee_profile" if session["role"]=="employee" else "manager_profile"))

    if purpose == "profile_update":
        new_email = request.form.get("new_email","").strip()
        new_phone = request.form.get("new_phone","").strip()
        if new_email and not valid_email(new_email):
            flash("Please enter a valid email address.", "danger")
            return redirect(url_for("employee_profile" if session["role"]=="employee" else "manager_profile"))
        payload = {"email": new_email or user.get("email"), "phone": new_phone or user.get("phone")}
    elif purpose == "password_reset":
        new_password = request.form.get("new_password","")
        if len(new_password) < 6:
            flash("New password must be at least 6 characters.", "danger")
            return redirect(url_for("employee_profile" if session["role"]=="employee" else "manager_profile"))
        payload = {"password_hash": generate_password_hash(new_password)}
    else:
        flash("Invalid request.", "danger")
        return redirect(url_for("employee_profile" if session["role"]=="employee" else "manager_profile"))

    otp_code = create_otp(session["role"], session["user_id"], purpose, payload)
    # OTP always goes to the CURRENTLY registered mobile number, never the new one being requested,
    # so an attacker who only knows a new number can't hijack the account.
    notifications.send_otp(user.get("phone"), otp_code)
    flash(f"An OTP has been sent to the mobile number on file (ending {str(user.get('phone') or '')[-4:]}).", "info")
    return redirect(url_for("employee_profile" if session["role"]=="employee" else "manager_profile") + f"?verify={purpose}")

@app.route("/profile/verify-otp", methods=["POST"])
@login_required()
def profile_verify_otp():
    table = profile_table(session["role"])
    purpose = request.form.get("purpose")
    code = request.form.get("otp","")
    payload, error = verify_otp_code(session["role"], session["user_id"], purpose, code)
    dest = url_for("employee_profile" if session["role"]=="employee" else "manager_profile")
    if error:
        flash(error, "danger")
        return redirect(dest)
    if purpose == "profile_update":
        execute(f"UPDATE {table} SET email=%s, phone=%s WHERE id=%s", (payload.get("email"), payload.get("phone"), session["user_id"]))
        flash("Contact information updated successfully.", "success")
    elif purpose == "password_reset":
        execute(f"UPDATE {table} SET password_hash=%s WHERE id=%s", (payload.get("password_hash"), session["user_id"]))
        flash("Password reset successfully.", "success")
    return redirect(dest)

@app.route("/employee")
@login_required("employee")
def employee_dashboard():
    rows=query("SELECT * FROM vouchers WHERE employee_id=%s ORDER BY date DESC,id DESC",(session["user_id"],))
    rows=[voucher_view(v) for v in rows]
    summary={
        "total": sum((money(v["amount"]) for v in rows),Decimal()),
        "paid": sum((v["paid_amount"] for v in rows),Decimal()),
        "balance": sum((v["balance"] for v in rows),Decimal()),
        "count": len(rows),
    }
    months={}
    for v in rows:
        key=str(v["date"])[:7]
        months.setdefault(key,{"total":Decimal(),"paid":Decimal(),"balance":Decimal(),"vouchers":[]})
        months[key]["total"]+=money(v["amount"]); months[key]["paid"]+=v["paid_amount"]; months[key]["balance"]+=v["balance"]; months[key]["vouchers"].append(v)
    return render_template("employee_dashboard.html", vouchers=rows, summary=summary, months=months)

@app.route("/voucher/new", methods=["GET","POST"])
@login_required("employee")
def new_voucher():
    if request.method=="POST":
        try:
            amount=money(request.form.get("amount"))
            if amount<=0: raise ValueError("Amount must be greater than zero.")
            date_value=request.form.get("date") or datetime.now().strftime("%Y-%m-%d")
            no=next_voucher_no()
            file=request.files.get("receipt")
            path=save_upload(file,session["name"],date_value,no) if file else ""
            employee=session["user_id"]
            execute("""INSERT INTO vouchers(voucher_no,employee_id,payable_to,date,purpose,description,amount,expense_payment_mode,transaction_id,receipt,status,created_at)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'Pending',NOW())""",
                    (no,employee,request.form.get("payable_to"),date_value,request.form.get("purpose"),request.form.get("description"),amount,request.form.get("expense_payment_mode"),request.form.get("transaction_id"),path))
            flash(f"Voucher {no} submitted successfully.", "success")
            return redirect(url_for("employee_dashboard"))
        except (ValueError, InvalidOperation) as e:
            flash(str(e), "danger")
    return render_template("voucher_form.html")

@app.route("/voucher/<int:voucher_id>/edit", methods=["GET","POST"])
@login_required("employee")
def edit_voucher(voucher_id):
    v = query("SELECT * FROM vouchers WHERE id=%s",(voucher_id,),True)
    if not v or v["employee_id"] != session["user_id"]:
        flash("Voucher not found.","danger"); return redirect(url_for("employee_dashboard"))
    if v["status"] not in ("Pending","Rejected"):
        flash("Only pending or rejected vouchers can be edited.","danger"); return redirect(url_for("employee_dashboard"))
    if request.method=="POST":
        try:
            amount=money(request.form.get("amount"))
            if amount<=0: raise ValueError("Amount must be greater than zero.")
            date_value=request.form.get("date") or str(v["date"])[:10]
            file=request.files.get("receipt")
            receipt=v["receipt"]
            if file and file.filename:
                receipt=save_upload(file,session["name"],date_value,v["voucher_no"])
            was_rejected = v["status"]=="Rejected"
            execute("""UPDATE vouchers SET payable_to=%s,date=%s,purpose=%s,description=%s,amount=%s,
                       expense_payment_mode=%s,transaction_id=%s,receipt=%s,updated_at=NOW()
                       {reset_clause} WHERE id=%s""".format(
                       reset_clause=",status='Pending',reject_reason=NULL" if was_rejected else ""),
                    (request.form.get("payable_to"),date_value,request.form.get("purpose"),request.form.get("description"),
                     amount,request.form.get("expense_payment_mode"),request.form.get("transaction_id"),receipt,voucher_id))
            flash(f"Voucher {v['voucher_no']} updated" + (" and resubmitted for approval." if was_rejected else "."), "success")
            return redirect(url_for("employee_dashboard"))
        except (ValueError, InvalidOperation) as e:
            flash(str(e), "danger")
    return render_template("voucher_form.html", voucher=v)

@app.route("/voucher/<int:voucher_id>/pdf")
@login_required()
def voucher_pdf(voucher_id):
    v=query("""SELECT v.*, e.full_name employee_name FROM vouchers v JOIN employees e ON e.id=v.employee_id WHERE v.id=%s""",(voucher_id,),True)
    if not v: flash("Voucher not found.","danger"); return redirect(url_for("dashboard"))
    if session["role"]=="employee" and v["employee_id"]!=session["user_id"]:
        flash("You are not allowed to view this voucher.","danger"); return redirect(url_for("employee_dashboard"))
    buf=build_pdf_buffer(v)
    return send_file(buf,as_attachment=True,download_name=f"{v['voucher_no']}.pdf",mimetype="application/pdf")

@app.route("/manager")
@login_required("manager")
def manager_dashboard():
    status=request.args.get("status","")
    employee_id=request.args.get("employee_id","")
    month=request.args.get("month","")
    sql="SELECT v.*,e.full_name employee_name FROM vouchers v JOIN employees e ON e.id=v.employee_id WHERE 1=1"
    params=[]
    if status: sql+=" AND v.status=%s"; params.append(status)
    if employee_id: sql+=" AND v.employee_id=%s"; params.append(employee_id)
    if month: sql+=" AND DATE_FORMAT(v.date,'%%Y-%%m')=%s"; params.append(month)
    sql+=" ORDER BY v.date DESC,v.id DESC"
    vouchers=[voucher_view(v) for v in query(sql,params)]
    allv=[voucher_view(v) for v in query("SELECT * FROM vouchers")]
    # NOTE: "Total Claimed" here intentionally sums ALL vouchers (any approval status),
    # matching the definition used on the employee dashboard, so the two views agree.
    # Paid/Balance are equivalent whether summed over all vouchers or approved-only,
    # since payments can only ever be recorded against an Approved voucher.
    summary={"claims":sum((money(v["amount"]) for v in allv),Decimal()),
             "paid":sum((v["paid_amount"] for v in allv),Decimal()),
             "balance":sum((v["balance"] for v in allv),Decimal()),
             "pending":sum(1 for v in allv if v["status"]=="Pending"),
             "approved":sum(1 for v in allv if v["status"]=="Approved"),
             "rejected":sum(1 for v in allv if v["status"]=="Rejected")}
    employees=query("SELECT id,full_name FROM employees WHERE is_active=1 ORDER BY full_name")
    return render_template("manager_dashboard.html",vouchers=vouchers,summary=summary,employees=employees,filters={"status":status,"employee_id":employee_id,"month":month})

@app.route("/manager/voucher/<int:voucher_id>/approve",methods=["POST"])
@login_required("manager")
def approve(voucher_id):
    execute("UPDATE vouchers SET status='Approved',approved_at=NOW(),approved_by=%s,reject_reason=NULL WHERE id=%s",(session["name"],voucher_id))
    v=query("SELECT v.*,e.full_name,e.email,e.phone FROM vouchers v JOIN employees e ON e.id=v.employee_id WHERE v.id=%s",(voucher_id,),True)
    if v: notifications.notify_employee(v, f"Voucher {v['voucher_no']} approved",
            f"Hi {v['full_name']}, your voucher {v['voucher_no']} for ₹{money(v['amount']):,.2f} has been approved.")
    flash("Voucher approved successfully.","success"); return redirect(request.referrer or url_for("manager_dashboard"))

@app.route("/manager/voucher/<int:voucher_id>/reject",methods=["POST"])
@login_required("manager")
def reject(voucher_id):
    reason=request.form.get("reason","").strip()
    if not reason: flash("Rejection reason is required.","danger"); return redirect(request.referrer or url_for("manager_dashboard"))
    execute("UPDATE vouchers SET status='Rejected',reject_reason=%s WHERE id=%s",(reason,voucher_id))
    v=query("SELECT v.*,e.full_name,e.email,e.phone FROM vouchers v JOIN employees e ON e.id=v.employee_id WHERE v.id=%s",(voucher_id,),True)
    if v: notifications.notify_employee(v, f"Voucher {v['voucher_no']} rejected",
            f"Hi {v['full_name']}, your voucher {v['voucher_no']} was rejected. Reason: {reason}")
    flash("Voucher rejected with reason recorded.","warning"); return redirect(request.referrer or url_for("manager_dashboard"))

@app.route("/manager/voucher/<int:voucher_id>/payment",methods=["POST"])
@login_required("manager")
def record_payment(voucher_id):
    v=query("SELECT * FROM vouchers WHERE id=%s",(voucher_id,),True)
    if not v: flash("Voucher not found.","danger"); return redirect(url_for("manager_dashboard"))
    if v["status"]!="Approved": flash("Only approved vouchers can receive payments.","danger"); return redirect(url_for("manager_dashboard"))
    amount=money(request.form.get("amount"))
    paid=money(query("SELECT COALESCE(SUM(amount),0) paid FROM payments WHERE voucher_id=%s",(voucher_id,),True)["paid"])
    balance=money(v["amount"])-paid
    if amount<=0: flash("Payment amount must be greater than zero.","danger")
    elif amount>balance: flash(f"Payment cannot exceed the remaining balance of ₹{balance:,.2f}.","danger")
    else:
        path=""
        proof=request.files.get("proof")
        if proof and proof.filename: path=save_upload(proof,v.get("employee_name") or query("SELECT full_name FROM employees WHERE id=%s",(v["employee_id"],),True)["full_name"],v["date"],v["voucher_no"],"Payment")
        execute("""INSERT INTO payments(voucher_id,payment_date,amount,payment_type,reference_no,remarks,proof_path,created_by,created_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,NOW())""",
                (voucher_id,request.form.get("payment_date") or datetime.now().strftime("%Y-%m-%d"),amount,request.form.get("payment_type"),request.form.get("reference_no"),request.form.get("remarks"),path,session["user_id"]))
        flash(f"Payment of ₹{amount:,.2f} recorded successfully.","success")
    return redirect(request.referrer or url_for("manager_dashboard"))

@app.route("/manager/voucher/<int:voucher_id>/history")
@login_required()
def payment_history(voucher_id):
    v=query("""SELECT v.*,e.full_name employee_name FROM vouchers v JOIN employees e ON e.id=v.employee_id WHERE v.id=%s""",(voucher_id,),True)
    if not v: flash("Voucher not found.","danger"); return redirect(url_for("dashboard"))
    if session["role"]=="employee" and v["employee_id"]!=session["user_id"]: flash("Not allowed.","danger"); return redirect(url_for("employee_dashboard"))
    v=voucher_view(v); payments=query("SELECT * FROM payments WHERE voucher_id=%s ORDER BY payment_date DESC,id DESC",(voucher_id,))
    return render_template("payment_history.html",voucher=v,payments=payments)

@app.route("/manager/export")
@login_required("manager")
def export_excel():
    vouchers=query("""SELECT v.*,e.full_name employee_name,e.department FROM vouchers v
                      JOIN employees e ON e.id=v.employee_id ORDER BY v.date,v.id""")
    for v in vouchers: voucher_view(v)
    # Sheet 1: employee + month + payment status summary
    rows=[]
    for v in vouchers:
        rows.append([str(v["date"])[:7],v["employee_name"],v["department"] or "",
                     v["payment_status"],float(money(v["amount"])),
                     float(v["paid_amount"]),float(v["balance"]),1])
    cols=["Month","Employee","Department","Payment Status","Claim Amount","Total Paid","Balance","Voucher Count"]
    raw=pd.DataFrame(rows,columns=cols)
    if raw.empty:
        summary=pd.DataFrame(columns=cols)
    else:
        summary=raw.groupby(["Month","Employee","Department","Payment Status"],as_index=False).agg(
            **{"Claim Amount":("Claim Amount","sum"),"Total Paid":("Total Paid","sum"),
               "Balance":("Balance","sum"),"Voucher Count":("Voucher Count","sum")})
    # Sheet 2: all voucher data; payment history is flattened into one readable cell.
    detail_rows=[]
    for v in vouchers:
        pays=query("""SELECT payment_date,amount,payment_type,reference_no,remarks
                      FROM payments WHERE voucher_id=%s ORDER BY payment_date,id""",(v["id"],))
        payment_details=" | ".join(
            f"{str(p['payment_date'])[:10]}: ₹{money(p['amount']):,.2f} ({p['payment_type']}{', '+p['reference_no'] if p['reference_no'] else ''})"
            for p in pays
        ) or "No payment"
        payment_types=", ".join(sorted(set(p["payment_type"] for p in pays))) or "-"
        detail_rows.append([
            v["voucher_no"],v["employee_name"],v["department"] or "",str(v["date"])[:10],
            v["purpose"],v["description"] or "",float(money(v["amount"])),float(v["paid_amount"]),
            float(v["balance"]),v["payment_status"],v["status"],payment_types,payment_details,
            v["payable_to"] or "",v["expense_payment_mode"] or "",v["transaction_id"] or "",
            v["reject_reason"] or ""
        ])
    dcols=["Voucher No","Employee","Department","Date","Purpose","Description","Claim Amount",
           "Total Paid","Balance","Payment Status","Approval Status","Payment Types","Payment Details",
           "Payable To","Expense Payment Mode","Transaction ID","Reject Reason"]
    detail=pd.DataFrame(detail_rows,columns=dcols)
    out=io.BytesIO()
    with pd.ExcelWriter(out,engine="openpyxl") as writer:
        summary.to_excel(writer,index=False,sheet_name="Summary")
        detail.to_excel(writer,index=False,sheet_name="Voucher Data")
        wb=writer.book
        from openpyxl.styles import PatternFill, Font, Alignment
        fills={"Fully Paid":"C6EFCE","Partially Paid":"FFEB9C","Not Paid":"FFC7CE",
               "Approved":"C6EFCE","Pending":"FFEB9C","Rejected":"FFC7CE"}
        for ws in wb.worksheets:
            ws.freeze_panes="A2"; ws.auto_filter.ref=ws.dimensions
            headers=[c.value for c in ws[1]]
            for cell in ws[1]:
                cell.font=Font(bold=True); cell.alignment=Alignment(horizontal="center")
            for row in ws.iter_rows(min_row=2):
                statuses=[]
                for key in ("Payment Status","Approval Status"):
                    if key in headers: statuses.append(row[headers.index(key)].value)
                status=next((x for x in statuses if x in fills),None)
                if status:
                    fill=PatternFill("solid",fgColor=fills[status])
                    for c in row: c.fill=fill
            for col in ws.columns:
                letter=col[0].column_letter
                ws.column_dimensions[letter].width=min(max(len(str(c.value or "")) for c in col)+2,45)
            # Currency columns
            for key in ("Claim Amount","Total Paid","Balance"):
                if key in headers:
                    idx=headers.index(key)+1
                    for row in ws.iter_rows(min_row=2,min_col=idx,max_col=idx):
                        row[0].number_format='₹#,##0.00'
    out.seek(0)
    return send_file(out,as_attachment=True,download_name=f"MGT_Voucher_Report_{datetime.now():%Y%m%d}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.route("/manager/export-zip")
@login_required("manager")
def export_zip():
    vouchers=query("""SELECT v.*,e.full_name employee_name FROM vouchers v JOIN employees e ON e.id=v.employee_id ORDER BY e.full_name,v.date,v.id""")
    tmp=tempfile.NamedTemporaryFile(delete=False,suffix=".zip"); tmp.close()
    try:
        with zipfile.ZipFile(tmp.name,"w",zipfile.ZIP_DEFLATED) as z:
            for v in vouchers:
                folder=f"{secure_filename(v['employee_name'])}/{str(v['date'])[:7]}/{v['voucher_no']}/"
                pdfbuf=build_pdf_buffer(v)
                z.writestr(folder+f"{v['voucher_no']}.pdf",pdfbuf.getvalue())
                receipt=v.get("receipt")
                if receipt:
                    path=os.path.join(UPLOAD_FOLDER,receipt)
                    if os.path.isfile(path): z.write(path,folder+os.path.basename(path))
                for p in query("SELECT * FROM payments WHERE voucher_id=%s ORDER BY id",(v["id"],)):
                    if p.get("proof_path"):
                        path=os.path.join(UPLOAD_FOLDER,p["proof_path"])
                        if os.path.isfile(path): z.write(path,folder+os.path.basename(path))
        return send_file(tmp.name,as_attachment=True,download_name=f"MGT_Vouchers_{datetime.now():%Y%m%d}.zip",mimetype="application/zip")
    finally:
        pass

def build_pdf_buffer(v):
    v=voucher_view(v)
    employee_name = v.get("employee_name") or query("SELECT full_name FROM employees WHERE id=%s",(v["employee_id"],),True)["full_name"]
    payments=query("SELECT * FROM payments WHERE voucher_id=%s ORDER BY payment_date,id",(v["id"],))
    buf=io.BytesIO(); doc=SimpleDocTemplate(buf,pagesize=A4,leftMargin=36,rightMargin=36,topMargin=30,bottomMargin=36)
    s=getSampleStyleSheet()
    cell=ParagraphStyle("cell",parent=s["Normal"],fontSize=10,leading=14)
    cell_b=ParagraphStyle("cellb",parent=s["Normal"],fontSize=10,leading=14,fontName="Helvetica-Bold")
    label=lambda text,value: Paragraph(f"<b>{text}</b> {value}",cell)

    elems=[]
    banner_path=os.path.join(app.static_folder,"img","mgt-voucher-banner.jpg")
    if os.path.exists(banner_path):
        with PILImage.open(banner_path) as im: iw,ih=im.size
        w=523; h=w*ih/iw
        elems.append(RLImage(banner_path,width=w,height=h))
    else:
        elems.append(Paragraph("MYSURU GREEN TECHNOLOGIES PRIVATE LIMITED",s["Title"]))
    elems.append(Spacer(1,10))

    approved_by = v.get("approved_by") or ("Pending approval" if v["status"]=="Pending" else "-")
    col=[523/3.0]*3
    data=[
        [label("PV NO.:",v["voucher_no"]), label("Employee:",employee_name), label("DATE :",str(v["date"])[:10])],
        [label("Details and Purpose of expenditure:",v["purpose"] or "-"), "", label("AMOUNT IN Rs.:",f"{money(v['amount']):,.2f}")],
        [label("Expenses account :",v["description"] or "-"), "", ""],
        [label("Amount in words :",number_to_words_inr(v["amount"])), "", ""],
        [label("Mode of Payment :",v["expense_payment_mode"] or "-"), "", ""],
        [label("Transaction ID :",v["transaction_id"] or "-"), "", ""],
        [label("Prepared by :",employee_name), "", label("Approved by :",approved_by)],
    ]
    t=Table(data,colWidths=col)
    t.setStyle(TableStyle([
        ("GRID",(0,0),(-1,-1),0.75,colors.black),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("LEFTPADDING",(0,0),(-1,-1),8),("RIGHTPADDING",(0,0),(-1,-1),8),
        ("TOPPADDING",(0,0),(-1,-1),8),("BOTTOMPADDING",(0,0),(-1,-1),8),
        ("SPAN",(0,1),(1,1)),
        ("SPAN",(0,2),(2,2)),
        ("SPAN",(0,3),(2,3)),
        ("SPAN",(0,4),(2,4)),
        ("SPAN",(0,5),(2,5)),
        ("SPAN",(0,6),(1,6)),
    ]))
    elems.append(t)

    if payments:
        elems.append(Spacer(1,16))
        elems.append(Paragraph("Payment History",s["Heading3"]))
        pdta=[["Date","Amount","Type","Reference"]]+[[str(p["payment_date"])[:10],f"Rs. {money(p['amount']):,.2f}",p["payment_type"],p["reference_no"] or "-"] for p in payments]
        pt=Table(pdta,colWidths=[1.1*inch,1.2*inch,1.4*inch,3.2*inch])
        pt.setStyle(TableStyle([("GRID",(0,0),(-1,-1),.4,colors.grey),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#e8f5e9")),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("PADDING",(0,0),(-1,-1),5)]))
        elems.append(pt)
        elems.append(Spacer(1,6))
        elems.append(Paragraph(f"<b>Total Paid:</b> Rs. {v['paid_amount']:,.2f} &nbsp;&nbsp; <b>Balance:</b> Rs. {v['balance']:,.2f} &nbsp;&nbsp; <b>Status:</b> {v['payment_status']}",cell))

    doc.build(elems); buf.seek(0); return buf

@app.route("/manager/employees")
@login_required("manager")
def employees():
    rows=query("SELECT * FROM employees ORDER BY full_name")
    voucher_counts={r["employee_id"]:r["c"] for r in query("SELECT employee_id, COUNT(*) c FROM vouchers GROUP BY employee_id")}
    for e in rows: e["voucher_count"]=voucher_counts.get(e["id"],0)
    return render_template("employees.html",employees=rows)

@app.route("/manager/employees/add",methods=["POST"])
@login_required("manager")
def add_employee():
    email=request.form.get("email","").strip()
    if email and not valid_email(email):
        flash("Please enter a valid email address (e.g. name@company.in).","danger")
        return redirect(url_for("employees"))
    try:
        execute("""INSERT INTO employees(username,password_hash,full_name,email,phone,department,is_active,created_at)
                   VALUES(%s,%s,%s,%s,%s,%s,1,NOW())""",
                (request.form["username"].strip(),generate_password_hash(request.form["password"]),request.form["full_name"].strip(),email or None,request.form.get("phone"),request.form.get("department")))
        flash("Employee created successfully.","success")
    except mysql.connector.IntegrityError: flash("Username already exists.","danger")
    return redirect(url_for("employees"))

@app.route("/manager/employees/<int:employee_id>/edit",methods=["POST"])
@login_required("manager")
def edit_employee(employee_id):
    email=request.form.get("email","").strip()
    if email and not valid_email(email):
        flash("Please enter a valid email address (e.g. name@company.in).","danger")
        return redirect(url_for("employees"))
    execute("""UPDATE employees SET full_name=%s,email=%s,phone=%s,department=%s WHERE id=%s""",
            (request.form["full_name"].strip(),email or None,request.form.get("phone"),request.form.get("department"),employee_id))
    flash("Employee details updated.","success")
    return redirect(url_for("employees"))

@app.route("/manager/employees/<int:employee_id>/delete",methods=["POST"])
@login_required("manager")
def delete_employee(employee_id):
    try:
        execute("DELETE FROM employees WHERE id=%s",(employee_id,))
        flash("Employee deleted.","success")
    except mysql.connector.IntegrityError:
        flash("This employee has voucher history and can't be deleted — disable the account instead.","danger")
    return redirect(url_for("employees"))

@app.route("/manager/employees/<int:employee_id>/toggle",methods=["POST"])
@login_required("manager")
def toggle_employee(employee_id):
    execute("UPDATE employees SET is_active=NOT is_active WHERE id=%s",(employee_id,)); flash("Employee status updated.","success"); return redirect(url_for("employees"))

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")),debug=os.getenv("FLASK_DEBUG","0")=="1")
