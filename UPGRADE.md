# Upgrading an existing install to v3

Read this before you deploy over a live database. Two things will affect
your users on the first day, and one is a hard stop at startup.

## Back up first

```
mysqldump -u root -p voucher_db > voucher_db_before_v3.sql
tar czf uploads_before_v3.tar.gz uploads/
```

Do not skip this. `migration_v3.sql` only adds things, but a backup is what
lets you roll back if something else goes wrong.

## 1. The app will not start without a real SECRET_KEY

Previously, a missing `.env` silently fell back to a constant published in
the source. Now the app exits with an explanatory message instead. Generate
one before deploying:

```
python -c "import secrets; print(secrets.token_hex(32))"
```

Put it in `.env` as `SECRET_KEY=...`. **Everyone is logged out** when the key
changes — existing session cookies stop validating. Deploy at a quiet time
and tell people they will need to sign in again.

## 2. Passwords must now be at least 10 characters

`security.MIN_PASSWORD_LENGTH` is 10, with a complexity rule and a
common-password blocklist. **Existing passwords keep working** — nobody is
locked out. The rule applies when a password is next set or reset.

If you want everyone moved onto a compliant password, flag the accounts:

```sql
UPDATE employees SET must_change_password = 1;
UPDATE managers  SET must_change_password = 1;
```

Each user is then sent to the change-password page at their next login.

## 3. Run the migration

```
mysql -u root -p voucher_db < migration_v3.sql
```

It adds `managers.phone`, `must_change_password` on both account tables, OTP
hardening columns, `vouchers.approved_by_id`, and the `voucher_counters`,
`rate_limits` and `audit_log` tables. No existing row is deleted.

The one deliberate data change: **every pending OTP is closed.** Old codes
were stored in plaintext and cannot be checked against the new hashed column.
Anyone mid-verification simply requests a new code.

The migration seeds `voucher_counters` from the highest voucher number you
have already issued, so numbering continues from where you are.

Two sections at the end of the file are commented out on purpose — the
amount CHECK constraints, and a query listing any voucher already overpaid.
Run the SELECTs, look at what they return, then decide.

## 4. Install the new dependencies

```
pip install -r requirements.txt
```

New: `Flask-WTF` (CSRF) and `gunicorn`. Use **Python 3.11 or 3.12** — the
pinned Pillow and pandas have no wheels for 3.13+.

## 5. Add a mobile number for each manager

Manager OTP could never work before, because the `managers` table had no
phone column. Now it does, but it is empty:

```sql
UPDATE managers SET phone = '9876543210' WHERE username = 'admin';
```

Without it, the manager profile page will say codes cannot be sent — which
is at least honest. The old version claimed an OTP had been sent when nothing
had been.

## 6. Check your `.env` against `.env.example`

New settings, all with working defaults except the first:

| Setting | Notes |
|---|---|
| `SECRET_KEY` | Required. The app will not start without it. |
| `COOKIE_SECURE` | `1` in production. Set `0` only for local plain HTTP. |
| `BIND_HOST` | Now defaults to `127.0.0.1`, not `0.0.0.0`. |
| `TRUST_PROXY` | `1` only behind a reverse proxy you control. |
| `MAX_UPLOAD_MB` | Default 10. |
| `PER_PAGE` | Manager dashboard page size, default 50. |
| `SESSION_HOURS` | Idle timeout, default 8. |
| `DB_POOL_SIZE` | Default 8. |
| `LOG_DIR` | Default `logs/`. |

## Behaviour that changed

| Before | Now |
|---|---|
| Bills were stored but no page could open them | Linked from both dashboards and the payment history, embedded in the PDF |
| Any POST worked with just a session cookie | CSRF token required on every form |
| A paid voucher could be rejected, then edited and resubmitted | Refused while payments exist |
| Payments could exceed the claim under concurrent use | Checked under a row lock |
| Two simultaneous submissions could collide on a number | Allocated atomically from a counter |
| Balance was floored at zero, hiding overpayment | Shown as negative and flagged |
| Disabling an account left the session live | Ends at the next request |
| Unlimited login and OTP guesses | Throttled; OTP capped at 5 attempts |
| "OTP sent" shown even when nothing was sent | Reports the real delivery result |
| `.env` Twilio/SMTP settings were ignored entirely | Read correctly at call time |
| Bootstrap and fonts loaded from a CDN | Vendored into `static/vendor/` |
| Dev server on `0.0.0.0` | gunicorn samples in `deploy/`, binds localhost by default |

## If you need to roll back

```
mysql -u root -p voucher_db < voucher_db_before_v3.sql
```

Then restore the old application files. The v3 columns are additive, so the
old code also runs against a migrated database if you would rather not
restore the dump — except that old code will not understand hashed OTP
values, so OTP flows would need the `otp_codes` table emptied.
