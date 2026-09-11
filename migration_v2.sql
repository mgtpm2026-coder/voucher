-- ============================================================================
-- Migration v2 — run this in MySQL Workbench against your EXISTING voucher_db.
-- It only ADDS columns/tables; it does not drop or touch existing rows.
-- Run it ONCE. (Requires MySQL 8.0.29+ for "IF NOT EXISTS" on ADD COLUMN — if
-- your server is older, drop the "IF NOT EXISTS" wording from each line and
-- just run each statement once.)
-- ============================================================================
USE voucher_db;

-- Profile photos for employees & managers
ALTER TABLE employees ADD COLUMN IF NOT EXISTS profile_image VARCHAR(500) NULL AFTER department;
ALTER TABLE managers  ADD COLUMN IF NOT EXISTS profile_image VARCHAR(500) NULL AFTER full_name;

-- OTP verification (used to gate contact-info edits and password resets)
CREATE TABLE IF NOT EXISTS otp_codes (
  id INT AUTO_INCREMENT PRIMARY KEY,
  user_type ENUM('employee','manager') NOT NULL,
  user_id INT NOT NULL,
  purpose ENUM('profile_update','password_reset') NOT NULL,
  otp_code VARCHAR(10) NOT NULL,
  payload TEXT NULL,
  expires_at DATETIME NOT NULL,
  verified TINYINT(1) NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Voucher edit tracking (so employees can resubmit a rejected/pending voucher)
ALTER TABLE vouchers ADD COLUMN IF NOT EXISTS updated_at DATETIME NULL AFTER created_at;

-- Track which manager approved each voucher, for the printed PV
ALTER TABLE vouchers ADD COLUMN IF NOT EXISTS approved_by VARCHAR(150) NULL AFTER approved_at;
