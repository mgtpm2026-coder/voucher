"""End-to-end tests against a real MySQL/MariaDB database.

These cover the bugs that lost money in the previous version: overpayment,
rejecting a paid voucher, editing a paid voucher, missing CSRF, unguarded
status transitions, and receipt access control.

Set TEST_DB_NAME to a scratch database - it is dropped and recreated.

    export TEST_DB_NAME=voucher_test
    pytest tests/ -v

Skipped automatically when no database is reachable.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = os.getenv("TEST_DB_NAME", "voucher_test")


def _bootstrap():
    """Create the scratch schema. Returns False if no database is reachable."""
    import mysql.connector
    root = dict(host=os.getenv("DB_HOST", "localhost"),
                port=int(os.getenv("DB_PORT", "3306")),
                user=os.getenv("DB_USER", "root"),
                password=os.getenv("DB_PASSWORD", ""))
    try:
        conn = mysql.connector.connect(**root)
    except Exception:
        return False
    cur = conn.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
    cur.execute(f"CREATE DATABASE {TEST_DB} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    cur.execute(f"USE {TEST_DB}")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    raw = open(os.path.join(here, "schema_fresh.sql"), encoding="utf-8").read()
    # Drop "--" comment lines first: they can contain semicolons and would
    # otherwise be glued onto the next statement by the split below.
    sql = "\n".join(line for line in raw.splitlines()
                    if not line.lstrip().startswith("--"))
    # Strip the CREATE DATABASE / USE lines; we made our own scratch database.
    sql = re.sub(r"(?im)^\s*(CREATE DATABASE|USE)\b.*?;\s*$", "", sql)
    for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
        cur.execute(stmt)
    conn.commit()
    cur.close()
    conn.close()
    return True


_available = None


def pytest_namespace():  # pragma: no cover
    return {}


@pytest.fixture(scope="session")
def app_module():
    global _available
    os.environ["FLASK_ENV"] = "test"
    os.environ["SECRET_KEY"] = "test-secret-key-not-for-production-use-only"
    os.environ["DB_NAME"] = TEST_DB
    os.environ["COOKIE_SECURE"] = "0"
    os.environ.setdefault("UPLOAD_FOLDER", "/tmp/voucher_test_uploads")
    os.environ.setdefault("LOG_DIR", "/tmp/voucher_test_logs")

    if _available is None:
        _available = _bootstrap()
    if not _available:
        pytest.skip("No MySQL/MariaDB reachable for integration tests")

    import db
    db.reset_pool()
    import app as app_module
    app_module.app.config["TESTING"] = True
    return app_module


@pytest.fixture
def env(app_module):
    """Fresh data for each test: one manager, one employee, empty tables."""
    from werkzeug.security import generate_password_hash
    from db import execute, query

    for table in ("audit_log", "rate_limits", "payments", "vouchers",
                  "otp_codes", "employees", "managers"):
        execute(f"DELETE FROM {table}")
    execute("UPDATE voucher_counters SET last_no=0")

    mgr = execute("""INSERT INTO managers(username,password_hash,full_name,phone,is_active)
                     VALUES('mgr',%s,'Test Manager','9999999999',1)""",
                  (generate_password_hash("Manager@Pass99"),))
    emp = execute("""INSERT INTO employees(username,password_hash,full_name,phone,is_active)
                     VALUES('emp',%s,'Test Employee','8888888888',1)""",
                  (generate_password_hash("Employee@Pass99"),))
    return {"manager_id": mgr, "employee_id": emp, "query": query, "execute": execute}


def client_for(app_module, username, password):
    c = app_module.app.test_client()
    r = c.post("/", data={"username": username, "password": password},
               follow_redirects=False)
    assert r.status_code == 302, f"login failed for {username}"
    return c


@pytest.fixture
def emp_client(app_module, env):
    return client_for(app_module, "emp", "Employee@Pass99")


@pytest.fixture
def mgr_client(app_module, env):
    return client_for(app_module, "mgr", "Manager@Pass99")


def make_voucher(env, amount="1000.00", status="Pending"):
    from vouchers import allocate_voucher_no
    from db import transaction
    with transaction() as cur:
        no = allocate_voucher_no(cur)
        cur.execute("""INSERT INTO vouchers(voucher_no,employee_id,date,purpose,description,
                           amount,status)
                       VALUES(%s,%s,CURDATE(),'Test','Test desc',%s,%s)""",
                    (no, env["employee_id"], amount, status))
        return cur.lastrowid


# ---------------------------------------------------------------------------

class TestVoucherNumbering:
    def test_sequential_and_unique(self, app_module, env):
        ids = [make_voucher(env) for _ in range(5)]
        numbers = [env["query"]("SELECT voucher_no FROM vouchers WHERE id=%s", (i,), True)
                   ["voucher_no"] for i in ids]
        assert numbers == ["MGT-V-0001", "MGT-V-0002", "MGT-V-0003",
                           "MGT-V-0004", "MGT-V-0005"]
        assert len(set(numbers)) == 5

    def test_counter_survives_deletion(self, app_module, env):
        """Deleting the newest voucher must not cause its number to be reused.

        The old implementation read MAX(id) from vouchers, so deleting a row
        handed the same number to the next submission.
        """
        make_voucher(env)
        second = make_voucher(env)
        env["execute"]("DELETE FROM vouchers WHERE id=%s", (second,))
        third = make_voucher(env)
        assert env["query"]("SELECT voucher_no FROM vouchers WHERE id=%s", (third,),
                            True)["voucher_no"] == "MGT-V-0003"


class TestPaymentGuards:
    def test_cannot_overpay(self, app_module, env, mgr_client):
        vid = make_voucher(env, "1000.00", "Approved")
        ok = mgr_client.post(f"/manager/voucher/{vid}/payment",
                             data={"amount": "600", "payment_type": "Cash",
                                   "payment_date": "2026-09-01"})
        assert ok.status_code == 302
        # Second payment would take the total to 1200 against a 1000 claim.
        mgr_client.post(f"/manager/voucher/{vid}/payment",
                        data={"amount": "600", "payment_type": "Cash",
                              "payment_date": "2026-09-01"})
        paid = env["query"]("SELECT COALESCE(SUM(amount),0) p FROM payments WHERE voucher_id=%s",
                            (vid,), True)["p"]
        assert float(paid) == 600.0

    def test_cannot_pay_unapproved(self, app_module, env, mgr_client):
        vid = make_voucher(env, "1000.00", "Pending")
        mgr_client.post(f"/manager/voucher/{vid}/payment",
                        data={"amount": "100", "payment_type": "Cash",
                              "payment_date": "2026-09-01"})
        assert env["query"]("SELECT COUNT(*) c FROM payments WHERE voucher_id=%s",
                            (vid,), True)["c"] == 0

    def test_rejects_zero_and_negative(self, app_module, env, mgr_client):
        vid = make_voucher(env, "1000.00", "Approved")
        for bad in ("0", "-50"):
            mgr_client.post(f"/manager/voucher/{vid}/payment",
                            data={"amount": bad, "payment_type": "Cash",
                                  "payment_date": "2026-09-01"})
        assert env["query"]("SELECT COUNT(*) c FROM payments WHERE voucher_id=%s",
                            (vid,), True)["c"] == 0


class TestStatusGuards:
    def test_cannot_reject_a_paid_voucher(self, app_module, env, mgr_client):
        """The bug that let a settled claim be re-submitted for more money."""
        vid = make_voucher(env, "1000.00", "Approved")
        mgr_client.post(f"/manager/voucher/{vid}/payment",
                        data={"amount": "500", "payment_type": "Cash",
                              "payment_date": "2026-09-01"})
        mgr_client.post(f"/manager/voucher/{vid}/reject", data={"reason": "changed my mind"})
        assert env["query"]("SELECT status FROM vouchers WHERE id=%s", (vid,),
                            True)["status"] == "Approved"

    def test_cannot_approve_twice(self, app_module, env, mgr_client):
        vid = make_voucher(env, "1000.00", "Approved")
        mgr_client.post(f"/manager/voucher/{vid}/approve")
        rows = env["query"]("SELECT COUNT(*) c FROM audit_log WHERE action='voucher_approve' "
                            "AND entity_id=%s", (vid,), True)
        assert rows["c"] == 0

    def test_cannot_edit_a_paid_voucher(self, app_module, env, emp_client, mgr_client):
        vid = make_voucher(env, "1000.00", "Approved")
        mgr_client.post(f"/manager/voucher/{vid}/payment",
                        data={"amount": "500", "payment_type": "Cash",
                              "payment_date": "2026-09-01"})
        env["execute"]("UPDATE vouchers SET status='Rejected' WHERE id=%s", (vid,))
        emp_client.post(f"/voucher/{vid}/edit",
                        data={"amount": "30000", "date": "2026-09-01", "purpose": "Inflated",
                              "description": "should not apply"})
        amount = env["query"]("SELECT amount FROM vouchers WHERE id=%s", (vid,), True)["amount"]
        assert float(amount) == 1000.0


class TestCsrf:
    def test_post_without_token_is_rejected(self, app_module, env):
        """CSRF is disabled in TESTING for the fixtures above; this turns it
        back on and confirms an unprotected POST cannot approve a voucher."""
        vid = make_voucher(env, "1000.00", "Pending")
        app_module.app.config["WTF_CSRF_ENABLED"] = True
        try:
            c = app_module.app.test_client()
            with c.session_transaction() as s:
                s["user_id"] = env["manager_id"]
                s["role"] = "manager"
                s["name"] = "Test Manager"
            c.post(f"/manager/voucher/{vid}/approve")
            assert env["query"]("SELECT status FROM vouchers WHERE id=%s", (vid,),
                                True)["status"] == "Pending"
        finally:
            app_module.app.config["WTF_CSRF_ENABLED"] = False


class TestAccessControl:
    def test_employee_cannot_read_another_employees_voucher(self, app_module, env, emp_client):
        from werkzeug.security import generate_password_hash
        other = env["execute"]("""INSERT INTO employees(username,password_hash,full_name,is_active)
                                  VALUES('other',%s,'Other Person',1)""",
                               (generate_password_hash("Other@Pass99"),))
        from db import transaction
        from vouchers import allocate_voucher_no
        with transaction() as cur:
            no = allocate_voucher_no(cur)
            cur.execute("""INSERT INTO vouchers(voucher_no,employee_id,date,purpose,description,
                               amount,status,receipt)
                           VALUES(%s,%s,CURDATE(),'Theirs','x',100,'Pending','x.png')""",
                        (no, other))
            vid = cur.lastrowid
        assert emp_client.get(f"/voucher/{vid}/pdf").status_code == 403
        assert emp_client.get(f"/voucher/{vid}/receipt").status_code == 403
        assert emp_client.get(f"/voucher/{vid}/history").status_code == 403

    def test_employee_cannot_reach_manager_pages(self, app_module, env, emp_client):
        assert emp_client.get("/manager", follow_redirects=False).status_code == 302
        assert emp_client.get("/manager/export", follow_redirects=False).status_code == 302

    def test_disabled_account_loses_its_session(self, app_module, env, emp_client):
        assert emp_client.get("/employee").status_code == 200
        env["execute"]("UPDATE employees SET is_active=0 WHERE id=%s", (env["employee_id"],))
        # The old code checked is_active only at login, so the session stayed live.
        assert emp_client.get("/employee", follow_redirects=False).status_code == 302

    def test_traversal_path_is_refused(self, app_module, env):
        assert app_module.safe_upload_path("../../etc/passwd") is None
        assert app_module.safe_upload_path("/etc/passwd") is None


class TestRateLimit:
    def test_login_locks_out_after_repeated_failures(self, app_module, env):
        c = app_module.app.test_client()
        for _ in range(6):
            c.post("/", data={"username": "emp", "password": "wrong-password"})
        r = c.post("/", data={"username": "emp", "password": "Employee@Pass99"})
        # Correct password, but the account is throttled.
        assert r.status_code == 429


class TestPasswordPolicy:
    def test_rejects_weak(self):
        from security import password_problem
        assert password_problem("short") is not None
        assert password_problem("ChangeMe@123") is not None       # the old default
        assert password_problem("alllowercase") is not None       # one character class
        assert password_problem("priya-Password1", "priya") is not None  # contains username

    def test_accepts_reasonable(self):
        from security import password_problem
        assert password_problem("Monsoon-Ledger-77", "priya") is None


class TestAuditLog:
    def test_approval_and_payment_are_recorded(self, app_module, env, mgr_client):
        vid = make_voucher(env, "1000.00", "Pending")
        mgr_client.post(f"/manager/voucher/{vid}/approve")
        mgr_client.post(f"/manager/voucher/{vid}/payment",
                        data={"amount": "400", "payment_type": "UPI",
                              "payment_date": "2026-09-01", "reference_no": "REF1"})
        actions = [r["action"] for r in env["query"]("SELECT action FROM audit_log ORDER BY id")]
        assert "voucher_approve" in actions
        assert "payment_record" in actions
