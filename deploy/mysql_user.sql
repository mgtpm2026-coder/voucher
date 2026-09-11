-- Create a least-privilege database user for the application.
-- The app does not need CREATE, DROP or GRANT at runtime; schema changes are
-- applied by you, as root, from schema_fresh.sql or migration_v3.sql.
CREATE USER IF NOT EXISTS 'voucher_app'@'localhost' IDENTIFIED BY 'CHANGE_THIS_PASSWORD';
GRANT SELECT, INSERT, UPDATE, DELETE ON voucher_db.* TO 'voucher_app'@'localhost';
FLUSH PRIVILEGES;
-- Then set DB_USER=voucher_app and DB_PASSWORD=... in .env.
-- Running the app as MySQL root means an SQL injection anywhere reaches
-- every database on the server.
