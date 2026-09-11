"""Database access: a shared connection pool plus small query helpers.

The original code opened a brand-new MySQL connection for every single
statement, which meant a manager dashboard listing N vouchers cost roughly
2N connections. Everything here draws from one pool instead, and
``transaction()`` gives multi-statement work a real atomic unit so a
balance check and the insert that depends on it cannot interleave with
another request.
"""

import os
from contextlib import contextmanager

from mysql.connector import pooling

_pool = None


def _config():
    return {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.getenv("DB_USER", "root"),
        "password": os.getenv("DB_PASSWORD", ""),
        "database": os.getenv("DB_NAME", "voucher_db"),
        "charset": "utf8mb4",
        "collation": "utf8mb4_unicode_ci",
        # Keep the app's clock and MySQL's clock identical. Previously the
        # code mixed Python's datetime.now() with MySQL's NOW(); if the two
        # disagreed, OTP expiry windows silently drifted.
        "time_zone": os.getenv("DB_TIME_ZONE", "+05:30"),
        "autocommit": False,
    }


def get_pool():
    global _pool
    if _pool is None:
        size = max(1, min(32, int(os.getenv("DB_POOL_SIZE", "8"))))
        _pool = pooling.MySQLConnectionPool(
            pool_name="voucher_pool", pool_size=size, pool_reset_session=True, **_config()
        )
    return _pool


def reset_pool():
    """Drop the pool so the next call rebuilds it. Used by the tests."""
    global _pool
    _pool = None


@contextmanager
def connection():
    conn = get_pool().get_connection()
    try:
        yield conn
    finally:
        conn.close()  # returns it to the pool rather than closing the socket


@contextmanager
def transaction(dictionary=True):
    """Run several statements as one atomic unit.

    Commits when the block finishes, rolls back on any exception. Use this
    anywhere a decision is made from a read and then written back -
    recording a payment, allocating a voucher number, changing a status.
    """
    with connection() as conn:
        cur = conn.cursor(dictionary=dictionary)
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


def query(sql, params=(), one=False):
    with connection() as conn:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(sql, params)
            return cur.fetchone() if one else cur.fetchall()
        finally:
            cur.close()


def execute(sql, params=()):
    with transaction(dictionary=False) as cur:
        cur.execute(sql, params)
        return cur.lastrowid
