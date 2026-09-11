# MGT Voucher Management

Expense voucher and reimbursement tracking for Mysuru Green Technologies
Pvt. Ltd. Flask + MySQL.

Employees submit claims with a bill attached; managers approve, reject with a
reason, and record payments (including partial ones) against approved
vouchers. Everything is exportable to Excel, PDF and a ZIP of the whole
filing tree.

- **New install?** Follow [SETUP.md](SETUP.md).
- **Upgrading an existing v2 install?** Read [UPGRADE.md](UPGRADE.md) first —
  there is a database migration and one behaviour change that will affect
  your users on day one.

## What it does

- One login page for employees and managers; the role is detected from the
  account.
- **Employee** — submit a claim with a bill, edit or resubmit while it is
  pending or rejected, see monthly totals of claimed / paid / outstanding,
  download the printed voucher, view payment history.
- **Manager** — filter and page through all vouchers, approve or reject with
  a recorded reason, record multiple partial payments with type, reference
  and proof of payment, manage employee accounts, read the audit log.
- **Exports** — Excel with a `Summary` sheet and a `Voucher Data` sheet,
  colour-coded by status; a ZIP laid out as `Employee / YYYY-MM / Voucher /`
  containing the PDF plus the original bill and payment proofs.
- **Printed voucher** — A4 payment voucher with the amount in words using
  Indian crore/lakh grouping, payment history, and the bill itself attached
  as a second page.

## Security

- CSRF protection on every form.
- Login and OTP throttling, held in the database so the limit survives
  multiple worker processes.
- OTP codes stored as hashes, capped at five attempts, single-use, ten-minute
  expiry, always delivered to the number already on file.
- Session cookies marked HttpOnly / SameSite / Secure, with an idle timeout.
- Uploads capped in size and verified by content, not just by file extension.
- Bills and payment proofs served by record id through an authorisation
  check — never by a path taken from the URL.
- Append-only audit log of every approval, payment and account change.
- The app refuses to start with a placeholder `SECRET_KEY`.

## Layout

| File | Purpose |
|---|---|
| `app.py` | Routes |
| `db.py` | Connection pool, query/execute/transaction helpers |
| `security.py` | Rate limiting, OTP, password policy, audit log |
| `vouchers.py` | Money arithmetic, payment status, voucher numbering |
| `pdfgen.py` | Printed payment voucher |
| `notifications.py` | SMS / WhatsApp / Email |
| `schema_fresh.sql` | Full schema for a new install |
| `migration_v3.sql` | v2 → v3 upgrade for an existing database |
| `tests/` | pytest suite |
| `deploy/` | gunicorn, systemd, nginx and backup samples |

## Tests

```
pip install pytest
export TEST_DB_NAME=voucher_test
pytest tests/ -v
```

`tests/test_money.py` needs no database. `tests/test_workflow.py` creates a
scratch database named by `TEST_DB_NAME` — it is dropped and recreated on
each run, so never point it at `voucher_db`. It skips itself if no database
is reachable.

## Not included

This release fixes defects and hardens what was already here. It does not add
approval thresholds, a separate Finance role, GST fields, cost centres,
accounting export, or advance settlement — those change the data model and
need business decisions first.
