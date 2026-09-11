-- ============================================================================
-- Migration v2 -> v3. Run this against your EXISTING voucher_db.
--
-- It only ADDS columns and tables. No existing row is deleted or rewritten,
-- with one deliberate exception noted below (pending OTP codes are closed,
-- because the old plaintext codes cannot be verified against the new hashed
-- column). Run it once.
--
--   mysql -u root -p voucher_db < migration_v3.sql
--
-- TAKE A BACKUP FIRST:
--   mysqldump -u root -p voucher_db > voucher_db_before_v3.sql
--
-- Requires MySQL 8.0.29+ or MariaDB 10.5+ for "ADD COLUMN IF NOT EXISTS".
-- ============================================================================
USE voucher_db;

-- --- 1. Managers can finally receive an OTP -------------------------------
ALTER TABLE managers ADD COLUMN IF NOT EXISTS phone VARCHAR(30) NULL AFTER email;
ALTER TABLE managers ADD COLUMN IF NOT EXISTS must_change_password TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS must_change_password TINYINT(1) NOT NULL DEFAULT 0;

-- --- 2. OTP hardening -----------------------------------------------------
ALTER TABLE otp_codes ADD COLUMN IF NOT EXISTS attempts TINYINT NOT NULL DEFAULT 0;
ALTER TABLE otp_codes ADD COLUMN IF NOT EXISTS consumed_at DATETIME NULL;
-- Codes are stored as a SHA-256 hash from now on, so the column must widen.
ALTER TABLE otp_codes MODIFY COLUMN otp_code VARCHAR(255) NOT NULL;

-- Close every code still pending under the old scheme. They were stored in
-- plaintext and cannot be checked against the new hashes; anyone mid-flow
-- simply requests a fresh code.
UPDATE otp_codes SET consumed_at = NOW() WHERE consumed_at IS NULL;

-- --- 3. Approver as a real foreign key ------------------------------------
-- approved_by (the name string) is kept: it is a snapshot of who approved at
-- the time, which is what the printed voucher should show even if the
-- manager account is later renamed or removed.
ALTER TABLE vouchers ADD COLUMN IF NOT EXISTS approved_by_id INT NULL AFTER approved_by;

-- --- 4. Atomic voucher numbering ------------------------------------------
CREATE TABLE IF NOT EXISTS voucher_counters (
 prefix VARCHAR(20) NOT NULL PRIMARY KEY,
 last_no INT NOT NULL DEFAULT 0
);

-- Seed the counter from the highest number already issued, so numbering
-- continues from where you are rather than restarting at 0001.
INSERT INTO voucher_counters(prefix, last_no)
SELECT 'MGT-V', COALESCE(MAX(CAST(REGEXP_SUBSTR(voucher_no, '[0-9]+$') AS UNSIGNED)), 0)
  FROM vouchers
ON DUPLICATE KEY UPDATE last_no = GREATEST(
  voucher_counters.last_no,
  (SELECT COALESCE(MAX(CAST(REGEXP_SUBSTR(v.voucher_no, '[0-9]+$') AS UNSIGNED)), 0) FROM vouchers v)
);

-- --- 5. Rate limiting -----------------------------------------------------
CREATE TABLE IF NOT EXISTS rate_limits (
 id BIGINT AUTO_INCREMENT PRIMARY KEY,
 bucket VARCHAR(120) NOT NULL,
 attempted_at DATETIME NOT NULL,
 INDEX idx_rate_bucket (bucket, attempted_at)
);

-- --- 6. Audit log ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
 id BIGINT AUTO_INCREMENT PRIMARY KEY,
 actor_type VARCHAR(20) NOT NULL,
 actor_id INT NULL,
 actor_name VARCHAR(150) NULL,
 action VARCHAR(60) NOT NULL,
 entity VARCHAR(40) NOT NULL,
 entity_id INT NULL,
 before_json LONGTEXT NULL,
 after_json LONGTEXT NULL,
 ip VARCHAR(45) NULL,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 INDEX idx_audit_entity (entity, entity_id),
 INDEX idx_audit_created (created_at)
);

-- --- 7. Check your data before adding the amount constraints --------------
-- These will fail if any existing row violates them, which is the point:
-- a zero or negative amount already in the table is a data problem worth
-- seeing. Run the SELECTs first; fix anything they return; then run the
-- ALTERs.
--
--   SELECT id, voucher_no, amount FROM vouchers WHERE amount <= 0;
--   SELECT id, voucher_id, amount FROM payments WHERE amount <= 0;
--
-- ALTER TABLE vouchers ADD CONSTRAINT chk_voucher_amount CHECK (amount > 0);
-- ALTER TABLE payments ADD CONSTRAINT chk_payment_amount CHECK (amount > 0);

-- --- 8. Overpayment check -------------------------------------------------
-- v3 prevents overpayment in application code inside a locking transaction.
-- Any overpayment already recorded predates the fix; this lists them.
--
--   SELECT v.voucher_no, v.amount, SUM(p.amount) paid
--     FROM vouchers v JOIN payments p ON p.voucher_id = v.id
--    GROUP BY v.id HAVING paid > v.amount;

SELECT 'migration v3 complete' AS status;
