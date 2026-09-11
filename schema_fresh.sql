
CREATE DATABASE IF NOT EXISTS voucher_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE voucher_db;

-- FRESH INSTALL ONLY: this schema intentionally removes old application data.
SET FOREIGN_KEY_CHECKS=0;
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
 is_active TINYINT(1) NOT NULL DEFAULT 1,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE otp_codes (
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
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at DATETIME NULL,
 CONSTRAINT fk_voucher_employee FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE RESTRICT
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
 CONSTRAINT fk_payment_manager FOREIGN KEY(created_by) REFERENCES managers(id) ON DELETE SET NULL
);

CREATE INDEX idx_vouchers_employee_date ON vouchers(employee_id,date);
CREATE INDEX idx_vouchers_status ON vouchers(status);
CREATE INDEX idx_payments_voucher ON payments(voucher_id);
CREATE INDEX idx_payments_date ON payments(payment_date);
