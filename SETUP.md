# Running the voucher app

Verified on Python 3.11 + MariaDB 10.11: full flow from employee submission
through manager approval, partial payment, PDF, Excel and ZIP export, with
30 automated tests passing.

Upgrading an existing v2 install? Read [UPGRADE.md](UPGRADE.md) instead —
this page assumes a clean database.

## Use Python 3.11 or 3.12

Not 3.13 or 3.14. `requirements.txt` pins `Pillow>=10,<11` and
`pandas>=2.2,<3`, and those releases have no wheels for the newer
interpreters, so `pip install` tries to compile from source and fails.

```
python --version     # expect 3.11.x or 3.12.x
```

## 1. MySQL must be running

Start MySQL or MariaDB and confirm you can log in before going further.

## 2. Create the database

```
mysql -u root -p < schema_fresh.sql
```

**This drops the application tables first.** Only for a new install.

Then create a limited user for the app to run as — it does not need root:

```
mysql -u root -p < deploy/mysql_user.sql    # edit the password inside first
```

## 3. Create `.env`

```
cp .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"
```

Paste that value as `SECRET_KEY` and fill in your database credentials. The
app **refuses to start** without a real key rather than falling back to a
published constant.

For local development over plain HTTP, also set `COOKIE_SECURE=0`. Leave it
at `1` in production.

`.env` is gitignored. Never commit it.

## 4. Virtual environment and dependencies

```
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
```

In VS Code, press `Ctrl+Shift+P` → *Python: Select Interpreter* → the one
inside `.venv` if it is not picked up automatically.

## 5. Create the first manager

```
python init_manager.py
```

It prompts for username, password, full name and a mobile number. Give it a
real mobile — it is where verification codes go, and manager OTP does not
work without one.

To add employees in bulk from a CSV, see `create_credentials.py`. It
generates a strong random password per account, prints them once, and flags
each account so the holder must change it at first login.

## 6. Run it

In VS Code press **F5** and pick a configuration:

| Configuration | What it does |
|---|---|
| **Voucher app** | Runs `app.py` directly, breakpoints active |
| **Voucher app (auto-reload)** | `flask run`, restarts on save |
| **Create first manager account** | `init_manager.py` under the debugger |

Or from a terminal:

```
python app.py
```

Then open <http://127.0.0.1:5000>. It binds to `127.0.0.1`, not all
interfaces — set `BIND_HOST` if you need otherwise during development.

## 7. Run the tests

```
pip install pytest
set TEST_DB_NAME=voucher_test        # Windows
export TEST_DB_NAME=voucher_test     # macOS / Linux
pytest tests/ -v
```

`tests/test_workflow.py` drops and recreates the database named by
`TEST_DB_NAME`. Never point it at `voucher_db`.

## Production

Do not use `python app.py` in production — it is single-threaded and
unencrypted. `deploy/` has working samples:

| File | Purpose |
|---|---|
| `gunicorn.conf.py` | WSGI server config |
| `voucher.service` | systemd unit, restarts on boot and on crash |
| `nginx.conf.sample` | TLS termination and reverse proxy |
| `backup.sh` | Nightly database + uploads backup |
| `mysql_user.sql` | Least-privilege database user |

```
gunicorn -c deploy/gunicorn.conf.py app:app
```

Set up `deploy/backup.sh` on the first day. The bills in `uploads/` are the
documents you will need years later at tax assessment, and they exist in
exactly one place.

## Notes

- **Offline-friendly.** Bootstrap and the icon font are vendored into
  `static/vendor/`, so the app renders correctly on a LAN with no internet
  and does not break if a firewall blocks jsDelivr.
- **`uploads/` and `logs/` are gitignored.** Bills are employee data and do
  not belong in version control.
- **Health check** at `/healthz` returns 200 when the database is reachable,
  503 when it is not. Useful for a monitor or load balancer.
