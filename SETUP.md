# Running the voucher app in VS Code

Verified working on 11 Sep 2026: Python 3.11 + MariaDB 10.11, full flow from
employee submission through manager approval, partial payment, PDF, Excel and
ZIP export.

## Use Python 3.11 or 3.12

Not 3.13 or 3.14. `requirements.txt` pins `Pillow>=10,<11` and `pandas>=2.2,<3`,
and those releases have no wheels for the newer interpreters — `pip install`
tries to compile from source and fails. The `__pycache__` in the original zip was
built by Python 3.14, so whoever ran it last hit a different interpreter than the
pins allow.

```
python --version     # expect 3.11.x or 3.12.x
```

## 1. MySQL must be running

The app needs a reachable MySQL/MariaDB server before it will start — it opens a
connection on the first page load, not lazily. Start the service (XAMPP, MySQL
Workbench's server, or `net start MySQL80` on Windows) and confirm you can log in.

## 2. Create the database

Run `schema_fresh.sql` once. It creates `voucher_db` and five tables.

```
mysql -u root -p < schema_fresh.sql
```

**This script drops `managers`, `employees`, `vouchers`, `payments` and
`otp_codes` first.** On an existing install run `migration_v2.sql` instead — that
one only adds columns.

## 3. Create `.env`

Copy `.env.example` to `.env` and fill in your MySQL credentials. `.env` is
gitignored and must never be committed.

Generate a real secret key rather than leaving the placeholder — the default in
`app.py` is a published constant, and anyone who knows it can forge a manager
session:

```
python -c "import secrets; print(secrets.token_hex(32))"
```

Paste the output as `SECRET_KEY`.

## 4. Virtual environment and dependencies

In the VS Code terminal, from the project folder:

```
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
```

VS Code should detect `.venv` automatically. If it doesn't, press `Ctrl+Shift+P`
→ *Python: Select Interpreter* → pick the one inside `.venv`.

## 5. Create the first manager

```
python init_manager.py
```

It prompts for a username and password and does nothing if a manager already
exists. There is a *Create first manager account* entry in the Run panel that
does the same thing under the debugger.

Do not use `create_credentials.py` as-is — it has a hardcoded `ChangeMe@123` for
both accounts.

## 6. Run it

Press **F5** and pick a configuration:

| Configuration | What it does |
|---|---|
| **Voucher app** | Runs `app.py` directly, breakpoints active. Matches how the app runs today. |
| **Voucher app (auto-reload)** | `flask run` — restarts on file save. Use this while editing templates. |

Then open <http://127.0.0.1:5000>.

Breakpoints work in both `.py` files and Jinja templates (`"jinja": true` is set
in `launch.json`).

## Notes while you are working in it

- **Uploaded bills cannot be opened from the UI.** Nothing serves the `uploads/`
  folder, so a manager approving a claim cannot see the receipt. To view one
  during development, open the file directly from `uploads/Employees/...` on
  disk.
- **`uploads/` is gitignored.** Receipts are employee data. The original zip had
  a real bill committed under `uploads/Employees/Pratham_P_M/` — it was not
  carried into this repository.
- **The dev server is single-threaded.** It will feel fine for one person and
  serialise badly with several. That is expected; it is not for production use.
