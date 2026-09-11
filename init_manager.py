"""Create the first manager account interactively.

Safe to re-run: it does nothing if a manager already exists.
"""

import os
from getpass import getpass

from dotenv import load_dotenv

load_dotenv()

import mysql.connector  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from security import password_problem  # noqa: E402

conn = mysql.connector.connect(
    host=os.getenv("DB_HOST", "localhost"), port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "root"), password=os.getenv("DB_PASSWORD", ""),
    database=os.getenv("DB_NAME", "voucher_db"))
cur = conn.cursor()
cur.execute("SELECT COUNT(*) FROM managers")
if cur.fetchone()[0] == 0:
    username = input("Manager username [admin]: ").strip() or "admin"
    password = getpass("Manager password: ")
    problem = password_problem(password, username)
    if problem:
        raise SystemExit(problem)
    if password != getpass("Confirm password: "):
        raise SystemExit("Passwords did not match.")
    name = input("Manager full name [MGT Manager]: ").strip() or "MGT Manager"
    phone = input("Manager mobile (for OTP, optional): ").strip() or None
    email = input("Manager email (optional): ").strip() or None
    cur.execute("""INSERT INTO managers(username,password_hash,full_name,phone,email)
                   VALUES(%s,%s,%s,%s,%s)""",
                (username, generate_password_hash(password), name, phone, email))
    conn.commit()
    print("Manager account created.")
else:
    print("Manager account already exists; nothing changed.")
cur.close()
conn.close()
