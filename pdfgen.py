"""Printed payment voucher (PV) generation."""

import io
import os

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (Image as RLImage, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

from vouchers import money_or_zero, number_to_words_inr

CONTENT_WIDTH = 523
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def _escape(value):
    """ReportLab's Paragraph parses its input as markup, so a description
    containing '&' or '<' would previously break PDF generation."""
    return (str(value if value not in (None, "") else "-")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_pdf(voucher, employee_name, payments, static_folder, receipt_abspath=None):
    """Render one voucher. `voucher` must already carry paid_amount/balance."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=36, rightMargin=36,
                            topMargin=30, bottomMargin=36,
                            title=str(voucher["voucher_no"]))
    s = getSampleStyleSheet()
    cell = ParagraphStyle("cell", parent=s["Normal"], fontSize=10, leading=14)
    label = lambda text, value: Paragraph(f"<b>{_escape(text)}</b> {_escape(value)}", cell)

    elems = []
    banner = os.path.join(static_folder, "img", "mgt-voucher-banner.jpg")
    if os.path.exists(banner):
        with PILImage.open(banner) as im:
            iw, ih = im.size
        elems.append(RLImage(banner, width=CONTENT_WIDTH, height=CONTENT_WIDTH * ih / iw))
    else:
        elems.append(Paragraph("MYSURU GREEN TECHNOLOGIES PRIVATE LIMITED", s["Title"]))
    elems.append(Spacer(1, 10))

    approved_by = voucher.get("approved_by") or (
        "Pending approval" if voucher["status"] == "Pending" else "-")
    col = [CONTENT_WIDTH / 3.0] * 3
    data = [
        [label("PV NO.:", voucher["voucher_no"]), label("Employee:", employee_name),
         label("DATE :", str(voucher["date"])[:10])],
        [label("Details and Purpose of expenditure:", voucher["purpose"]), "",
         label("AMOUNT IN Rs.:", f"{money_or_zero(voucher['amount']):,.2f}")],
        [label("Expenses account :", voucher["description"]), "", ""],
        [label("Amount in words :", number_to_words_inr(voucher["amount"])), "", ""],
        [label("Mode of Payment :", voucher["expense_payment_mode"]), "", ""],
        [label("Transaction ID :", voucher["transaction_id"]), "", ""],
        [label("Payable To :", voucher.get("payable_to")), "",
         label("Status :", voucher["status"])],
        [label("Prepared by :", employee_name), "", label("Approved by :", approved_by)],
    ]
    t = Table(data, colWidths=col)
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.75, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("SPAN", (0, 1), (1, 1)),
        ("SPAN", (0, 2), (2, 2)),
        ("SPAN", (0, 3), (2, 3)),
        ("SPAN", (0, 4), (2, 4)),
        ("SPAN", (0, 5), (2, 5)),
        ("SPAN", (0, 6), (1, 6)),
        ("SPAN", (0, 7), (1, 7)),
    ]))
    elems.append(t)

    if voucher["status"] == "Rejected" and voucher.get("reject_reason"):
        elems.append(Spacer(1, 10))
        elems.append(Paragraph(f"<b>Rejected:</b> {_escape(voucher['reject_reason'])}", cell))

    if payments:
        elems.append(Spacer(1, 16))
        elems.append(Paragraph("Payment History", s["Heading3"]))
        rows = [["Date", "Amount", "Type", "Reference"]] + [
            [str(p["payment_date"])[:10], f"Rs. {money_or_zero(p['amount']):,.2f}",
             _escape(p["payment_type"]), _escape(p["reference_no"])]
            for p in payments]
        pt = Table(rows, colWidths=[1.1 * inch, 1.2 * inch, 1.4 * inch, 3.2 * inch])
        pt.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), .4, colors.grey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8f5e9")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("PADDING", (0, 0), (-1, -1), 5),
        ]))
        elems.append(pt)
        elems.append(Spacer(1, 6))
        elems.append(Paragraph(
            f"<b>Total Paid:</b> Rs. {voucher['paid_amount']:,.2f} &nbsp;&nbsp; "
            f"<b>Balance:</b> Rs. {voucher['balance']:,.2f} &nbsp;&nbsp; "
            f"<b>Status:</b> {voucher['payment_status']}", cell))

    # The bill itself now travels with the voucher instead of being an
    # unreachable file on the server.
    if receipt_abspath and os.path.isfile(receipt_abspath):
        ext = os.path.splitext(receipt_abspath)[1].lower()
        if ext in IMAGE_EXTENSIONS:
            try:
                with PILImage.open(receipt_abspath) as im:
                    iw, ih = im.size
                width = min(CONTENT_WIDTH, iw)
                height = width * ih / iw
                max_height = 640
                if height > max_height:
                    height, width = max_height, max_height * iw / ih
                elems.append(Spacer(1, 18))
                elems.append(Paragraph("Bill / Receipt", s["Heading3"]))
                elems.append(RLImage(receipt_abspath, width=width, height=height))
            except Exception:
                # A corrupt upload must not stop the voucher printing.
                elems.append(Paragraph("<i>Receipt attached but could not be rendered.</i>", cell))
        else:
            elems.append(Spacer(1, 14))
            elems.append(Paragraph(
                f"<i>Receipt attached separately: {_escape(os.path.basename(receipt_abspath))}</i>",
                cell))

    doc.build(elems)
    buf.seek(0)
    return buf
