"""
Insert manager / employee login credentials directly.

Edit the MANAGERS and EMPLOYEES lists below with the accounts you want,
then run:

    python create_credentials.py

Requires the same .env used by app.py (DB_HOST, DB_USER, DB_PASSWORD, DB_NAME).
Passwords are hashed with werkzeug before being stored — never stored in plain text.
Existing usernames are skipped (not overwritten) so it's safe to re-run.
"""

import os
import mysql.connector
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash

load_dotenv()

# ---- 1. Edit these accounts ------------------------------------------------

MANAGERS = [
    # username, password, full_name, email
    {"username": "admin", "password": "ChangeMe@123", "full_name": "MGT Manager", "email": ""},
]

EMPLOYEES = [
    # username, password, full_name, department, phone, email
    {"username": "pratham", "password": "ChangeMe@123", "full_name": "Pratham Kumar",
     "department": "Project Management", "phone": "", "email": ""},
]

# -----------------------------------------------------------------------------

conn = mysql.connector.connect(
    host=os.getenv("DB_HOST", "localhost"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASSWORD", ""),
    database=os.getenv("DB_NAME", "voucher_db"),
)
cur = conn.cursor()

for m in MANAGERS:
    cur.execute("SELECT id FROM managers WHERE username=%s", (m["username"],))
    if cur.fetchone():
        print(f"[skip] manager '{m['username']}' already exists")
        continue
    cur.execute(
        "INSERT INTO managers(username,password_hash,full_name,email) VALUES(%s,%s,%s,%s)",
        (m["username"], generate_password_hash(m["password"]), m["full_name"], m.get("email") or None),
    )
    print(f"[ok] manager '{m['username']}' created")

for e in EMPLOYEES:
    cur.execute("SELECT id FROM employees WHERE username=%s", (e["username"],))
    if cur.fetchone():
        print(f"[skip] employee '{e['username']}' already exists")
        continue
    cur.execute(
        "INSERT INTO employees(username,password_hash,full_name,department,phone,email) VALUES(%s,%s,%s,%s,%s,%s)",
        (e["username"], generate_password_hash(e["password"]), e["full_name"],
         e.get("department") or None, e.get("phone") or None, e.get("email") or None),
    )
    print(f"[ok] employee '{e['username']}' created")

conn.commit()
cur.close()
conn.close()
print("Done.")
