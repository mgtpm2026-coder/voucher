"""Voucher domain rules: money arithmetic, payment status, numbering.

Everything that decides an amount lives here so it can be tested without a
web request, and so there is exactly one definition of "balance".
"""

from decimal import Decimal, InvalidOperation

TWO_PLACES = Decimal("0.01")


def money(value):
    """Parse to a 2-decimal Decimal. Raises on junk rather than hiding it.

    The original version swallowed every exception and returned 0.00, so a
    mistyped amount became a zero-rupee claim with no error shown.
    """
    if value is None or value == "":
        return Decimal("0.00")
    try:
        return Decimal(str(value).replace(",", "").strip()).quantize(TWO_PLACES)
    except (InvalidOperation, ValueError, ArithmeticError):
        raise ValueError("Enter a valid amount, for example 1250.00")


def money_or_zero(value):
    """For display paths where a bad value must not raise."""
    try:
        return money(value)
    except ValueError:
        return Decimal("0.00")


def payment_status(total, paid):
    total, paid = money_or_zero(total), money_or_zero(paid)
    if paid <= 0:
        return "Not Paid"
    if paid >= total:
        return "Fully Paid"
    return "Partially Paid"


def decorate(voucher, paid_amount):
    """Attach paid/balance/status to a voucher row.

    `paid_amount` comes from the caller's JOIN. Nothing here queries the
    database - that is what turned the dashboards into an N+1.

    Balance is NOT floored at zero: an overpayment must be visible, not
    silently displayed as a settled voucher.
    """
    v = voucher
    v["paid_amount"] = money_or_zero(paid_amount)
    v["balance"] = money_or_zero(v["amount"]) - v["paid_amount"]
    v["payment_status"] = payment_status(v["amount"], v["paid_amount"])
    v["overpaid"] = v["balance"] < 0
    return v


# SQL fragment that computes paid-to-date in one pass. Join this instead of
# querying payments once per voucher.
PAID_JOIN = """
  LEFT JOIN (SELECT voucher_id, SUM(amount) paid
               FROM payments GROUP BY voucher_id) pay ON pay.voucher_id = v.id
"""
PAID_COL = "COALESCE(pay.paid, 0) AS paid_to_date"


def allocate_voucher_no(cur, prefix="MGT-V"):
    """Reserve the next voucher number inside the caller's transaction.

    The UPDATE takes a row lock on the counter, so a second submission
    blocks here until the first commits and then reads the incremented
    value. Callers must INSERT the voucher in the same transaction.
    """
    cur.execute("INSERT IGNORE INTO voucher_counters(prefix, last_no) VALUES(%s, 0)", (prefix,))
    cur.execute("UPDATE voucher_counters SET last_no = last_no + 1 WHERE prefix=%s", (prefix,))
    cur.execute("SELECT last_no FROM voucher_counters WHERE prefix=%s", (prefix,))
    row = cur.fetchone()
    number = row["last_no"] if isinstance(row, dict) else row[0]
    return f"{prefix}-{number:04d}"


# ---------------------------------------------------------------------------
# Indian numbering: ... Crore, Lakh, Thousand, Hundred
# ---------------------------------------------------------------------------

_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
         "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen",
         "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two_digit_words(n):
    if n < 20:
        return _ONES[n]
    return _TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else "")


def _three_digit_words(n):
    if n >= 100:
        return _ONES[n // 100] + " Hundred" + (" " + _two_digit_words(n % 100) if n % 100 else "")
    return _two_digit_words(n)


def _group_words(n):
    """Words for a crore/lakh/thousand group, which can exceed 999 crore."""
    if n < 1000:
        return _three_digit_words(n)
    # e.g. 1234 crore -> "One Thousand Two Hundred Thirty Four"
    thousand, rest = divmod(n, 1000)
    out = _three_digit_words(thousand) + " Thousand"
    if rest:
        out += " " + _three_digit_words(rest)
    return out


def number_to_words_inr(amount):
    amount = money_or_zero(amount)
    negative = amount < 0
    amount = abs(amount)
    rupees = int(amount)
    paise = int((amount - rupees) * 100)
    if rupees == 0:
        words = "Zero"
    else:
        n = rupees
        crore, n = divmod(n, 10000000)
        lakh, n = divmod(n, 100000)
        thousand, n = divmod(n, 1000)
        hundred = n
        parts = []
        if crore:
            parts.append(_group_words(crore) + " Crore")
        if lakh:
            parts.append(_three_digit_words(lakh) + " Lakh")
        if thousand:
            parts.append(_three_digit_words(thousand) + " Thousand")
        if hundred:
            parts.append(_three_digit_words(hundred))
        words = " ".join(parts)
    result = f"Rupees {words} Only"
    if paise:
        result = f"Rupees {words} and {_two_digit_words(paise)} Paise Only"
    if negative:
        result = "Minus " + result
    return result
