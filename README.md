# MGT Voucher Management — Fresh Install

This is a clean, new Flask + MySQL voucher system. It intentionally does NOT include the old production database or old uploaded receipts.

Features:
- One login page for employees and managers; role is detected automatically.
- Employee dashboard with monthly grouping, claim/paid/balance totals, PDF download and payment history.
- Manager dashboard with approval/rejection, multiple partial payments, payment type, reference number and payment proof.
- Excel download with exactly two sheets: `Summary` (employee/month/payment status) and `Voucher Data` (all voucher data plus payment details).
- Colored Excel rows by payment/approval status.
- ZIP export: Employee / YYYY-MM / Voucher / PDF + receipts/payment proofs.
- No background image is used.

Fresh install:
1. Create database with `schema_fresh.sql`.
2. Copy `.env.example` to `.env` and enter MySQL credentials.
3. `python -m venv venv`
4. `venv\Scripts\activate`
5. `pip install -r requirements.txt`
6. `python init_manager.py`
7. `python app.py`
8. Open http://127.0.0.1:5000

IMPORTANT: `schema_fresh.sql` drops the four application tables. Use it only for the new empty system requested by the owner. It does not touch other MySQL databases.
