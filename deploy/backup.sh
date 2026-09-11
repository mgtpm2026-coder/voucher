#!/usr/bin/env bash
# Nightly backup of the database and the uploaded bills.
#   sudo crontab -e
#   15 1 * * *  /opt/voucher/deploy/backup.sh >> /var/log/voucher-backup.log 2>&1
#
# Both halves matter: the database without uploads/ loses every bill, and
# uploads/ without the database loses what each bill was for.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/voucher}"
DEST="${BACKUP_DIR:-/var/backups/voucher}"
KEEP_DAYS="${KEEP_DAYS:-30}"
STAMP=$(date +%Y%m%d_%H%M%S)

set -a; . "$APP_DIR/.env"; set +a
mkdir -p "$DEST"

mysqldump --single-transaction --routines --triggers \
  -h "${DB_HOST:-localhost}" -P "${DB_PORT:-3306}" \
  -u "$DB_USER" -p"$DB_PASSWORD" "$DB_NAME" \
  | gzip > "$DEST/db_$STAMP.sql.gz"

tar czf "$DEST/uploads_$STAMP.tar.gz" -C "$APP_DIR" uploads

find "$DEST" -name '*.gz' -mtime +"$KEEP_DAYS" -delete

echo "$(date -Is) backup ok: db_$STAMP.sql.gz ($(du -h "$DEST/db_$STAMP.sql.gz" | cut -f1)), uploads_$STAMP.tar.gz ($(du -h "$DEST/uploads_$STAMP.tar.gz" | cut -f1))"

# Copy $DEST off this machine - another server, or object storage. A backup
# on the same disk as the database does not survive the failure you are
# backing up against.
#
# Test the restore once a quarter, on a spare machine:
#   gunzip -c db_YYYYMMDD_HHMMSS.sql.gz | mysql -u root -p voucher_restore_test
# An untested backup is not a backup.
