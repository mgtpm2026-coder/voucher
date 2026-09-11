-- ============================================================================
-- MGT Voucher Management - schema v3 (FRESH INSTALL ONLY)
--
-- This drops the application tables and recreates them empty. If you already
-- have live voucher data, DO NOT run this - run migration_v3.sql instead,
-- which only adds what is new.
-- ============================================================================

CREATE DATABASE IF NOT EXISTS voucher_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE voucher_db;

SET FOREIGN_KEY_CHECKS=0;
DROP TABLE IF EXISTS audit_log;
DROP TABLE IF EXISTS rate_limits;
DROP TABLE IF EXISTS voucher_counters;
DROP TABLE IF EXISTS otp_codes;
DROP TABLE IF EXISTS payments;
DROP TABLE IF EXISTS vouchers;
DROP TABLE IF EXISTS employees;
DROP TABLE IF EXISTS managers;
SET FOREIGN_KEY_CHECKS=1;

CREATE TABLE managers (
 id INT AUTO_INCREMENT PRIMARY KEY,
 username VARCHAR(100) NOT NULL UNIQUE,
 password_hash VARCHAR(255) NOT NULL,
 full_name VARCHAR(150) NOT NULL,
 profile_image VARCHAR(500),
 email VARCHAR(150),
 -- v3: managers had no phone column, so manager OTP delivery could never work.
 phone VARCHAR(30),
 must_change_password TINYINT(1) NOT NULL DEFAULT 0,
 is_active TINYINT(1) NOT NULL DEFAULT 1,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE employees (
 id INT AUTO_INCREMENT PRIMARY KEY,
 username VARCHAR(100) NOT NULL UNIQUE,
 password_hash VARCHAR(255) NOT NULL,
 full_name VARCHAR(150) NOT NULL,
 email VARCHAR(150),
 phone VARCHAR(30),
 department VARCHAR(100),
 profile_image VARCHAR(500),
 must_change_password TINYINT(1) NOT NULL DEFAULT 0,
 is_active TINYINT(1) NOT NULL DEFAULT 1,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- v3: otp_code now holds a SHA-256 hash, never the live code. `attempts`
-- caps guessing; `consumed_at` replaces the old `verified` flag so an
-- expired or exhausted code is closed off explicitly.
CREATE TABLE otp_codes (
 id INT AUTO_INCREMENT PRIMARY KEY,
 user_type ENUM('employee','manager') NOT NULL,
 user_id INT NOT NULL,
 purpose ENUM('profile_update','password_reset') NOT NULL,
 otp_code VARCHAR(255) NOT NULL,
 payload TEXT NULL,
 expires_at DATETIME NOT NULL,
 attempts TINYINT NOT NULL DEFAULT 0,
 consumed_at DATETIME NULL,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 INDEX idx_otp_lookup (user_type, user_id, purpose, consumed_at)
);

CREATE TABLE vouchers (
 id INT AUTO_INCREMENT PRIMARY KEY,
 voucher_no VARCHAR(40) NOT NULL UNIQUE,
 employee_id INT NOT NULL,
 payable_to VARCHAR(150),
 date DATE NOT NULL,
 purpose VARCHAR(255) NOT NULL,
 description TEXT,
 amount DECIMAL(12,2) NOT NULL,
 expense_payment_mode VARCHAR(50),
 transaction_id VARCHAR(150),
 receipt VARCHAR(500),
 status ENUM('Pending','Approved','Rejected') NOT NULL DEFAULT 'Pending',
 reject_reason TEXT,
 approved_at DATETIME NULL,
 approved_by VARCHAR(150) NULL,
 approved_by_id INT NULL,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at DATETIME NULL,
 CONSTRAINT fk_voucher_employee FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE RESTRICT,
 CONSTRAINT fk_voucher_approver FOREIGN KEY(approved_by_id) REFERENCES managers(id) ON DELETE SET NULL,
 CONSTRAINT chk_voucher_amount CHECK (amount > 0)
);

CREATE TABLE payments (
 id INT AUTO_INCREMENT PRIMARY KEY,
 voucher_id INT NOT NULL,
 payment_date DATE NOT NULL,
 amount DECIMAL(12,2) NOT NULL,
 payment_type ENUM('Cash','UPI','Bank Transfer','Cheque','Other') NOT NULL,
 reference_no VARCHAR(150),
 remarks TEXT,
 proof_path VARCHAR(500),
 created_by INT NULL,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CONSTRAINT fk_payment_voucher FOREIGN KEY(voucher_id) REFERENCES vouchers(id) ON DELETE CASCADE,
 CONSTRAINT fk_payment_manager FOREIGN KEY(created_by) REFERENCES managers(id) ON DELETE SET NULL,
 CONSTRAINT chk_payment_amount CHECK (amount > 0)
);

-- v3: voucher numbers are allocated by UPDATE-then-read inside the same
-- transaction as the INSERT, so two simultaneous submissions cannot both
-- claim the same number.
CREATE TABLE voucher_counters (
 prefix VARCHAR(20) NOT NULL PRIMARY KEY,
 last_no INT NOT NULL DEFAULT 0
);
INSERT INTO voucher_counters(prefix, last_no) VALUES ('MGT-V', 0);

-- v3: login and OTP throttling. Kept in the database rather than process
-- memory so the limit holds across gunicorn workers.
CREATE TABLE rate_limits (
 id BIGINT AUTO_INCREMENT PRIMARY KEY,
 bucket VARCHAR(120) NOT NULL,
 attempted_at DATETIME NOT NULL,
 INDEX idx_rate_bucket (bucket, attempted_at)
);

-- v3: append-only record of every approval, payment and account change.
CREATE TABLE audit_log (
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

CREATE INDEX idx_vouchers_employee_date ON vouchers(employee_id,date);
CREATE INDEX idx_vouchers_status ON vouchers(status);
CREATE INDEX idx_payments_voucher ON payments(voucher_id);
CREATE INDEX idx_payments_date ON payments(payment_date);
