"""Bulk-create employee accounts from a CSV file.

Replaces the old version of this script, which shipped a hardcoded
"ChangeMe@123" for every account it created.

Passwords are never written in this file. Each account gets a strong random
password, printed once so you can hand it over, and every account is flagged
must_change_password so the holder replaces it at first login.

Usage:
    python create_credentials.py employees.csv

The CSV needs a header row with these columns (email and phone optional):

    username,full_name,department,phone,email
    priya,Priya Rao,Accounts,9876543210,priya@company.in
    rahul,Rahul N,Site,9876543211,

Existing usernames are skipped, so the script is safe to re-run.
"""

import csv
import os
import secrets
import string
import sys

from dotenv import load_dotenv

load_dotenv()

import mysql.connector  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*-_"


def random_password(length=14):
    """Random password that always satisfies the app's complexity rule."""
    while True:
        pw = "".join(secrets.choice(ALPHABET) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw)):
            return pw


def main(path):
    if not os.path.isfile(path):
        raise SystemExit(f"No such file: {path}")

    conn = mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"), port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "root"), password=os.getenv("DB_PASSWORD", ""),
        database=os.getenv("DB_NAME", "voucher_db"))
    cur = conn.cursor()

    created = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            username = (row.get("username") or "").strip()
            full_name = (row.get("full_name") or "").strip()
            if not username or not full_name:
                print(f"[skip] row missing username or full_name: {row}")
                continue
            cur.execute("SELECT id FROM employees WHERE username=%s", (username,))
            if cur.fetchone():
                print(f"[skip] '{username}' already exists")
                continue
            password = random_password()
            cur.execute("""INSERT INTO employees(username,password_hash,full_name,department,
                               phone,email,must_change_password)
                           VALUES(%s,%s,%s,%s,%s,%s,1)""",
                        (username, generate_password_hash(password), full_name,
                         (row.get("department") or "").strip() or None,
                         (row.get("phone") or "").strip() or None,
                         (row.get("email") or "").strip() or None))
            created.append((username, full_name, password))
            print(f"[ok] '{username}' created")

    conn.commit()
    cur.close()
    conn.close()

    if created:
        print("\n" + "=" * 64)
        print("TEMPORARY PASSWORDS - shown once. Hand these over securely,")
        print("then delete this output. Each user must change it at first login.")
        print("=" * 64)
        for username, full_name, password in created:
            print(f"{username:<20} {full_name:<28} {password}")
        print("=" * 64)
    else:
        print("No new accounts created.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python create_credentials.py <employees.csv>")
    main(sys.argv[1])
