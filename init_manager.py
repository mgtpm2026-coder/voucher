
import os
from getpass import getpass
import mysql.connector
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash

load_dotenv()
conn=mysql.connector.connect(host=os.getenv("DB_HOST","localhost"),port=int(os.getenv("DB_PORT","3306")),
 user=os.getenv("DB_USER","root"),password=os.getenv("DB_PASSWORD",""),database=os.getenv("DB_NAME","voucher_db"))
cur=conn.cursor()
cur.execute("SELECT COUNT(*) FROM managers")
if cur.fetchone()[0] == 0:
    username=input("Manager username [admin]: ").strip() or "admin"
    password=getpass("Manager password: ")
    if len(password)<6: raise SystemExit("Password must be at least 6 characters.")
    name=input("Manager full name [MGT Manager]: ").strip() or "MGT Manager"
    cur.execute("INSERT INTO managers(username,password_hash,full_name) VALUES(%s,%s,%s)",(username,generate_password_hash(password),name))
    conn.commit()
    print("Manager account created.")
else:
    print("Manager account already exists; nothing changed.")
cur.close(); conn.close()
