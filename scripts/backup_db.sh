#!/usr/bin/env bash
# scripts/backup_db.sh — daily SQLite backup using the .backup API (WAL-aware).

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/Monthly-Metrics}"
DB="$APP_DIR/data/metrics.db"
BACKUP_DIR="$APP_DIR/data/backups"
TODAY=$(date +%Y%m%d)
TARGET="$BACKUP_DIR/metrics-$TODAY.db"

mkdir -p "$BACKUP_DIR"
sqlite3 "$DB" ".backup '$TARGET'"

# Retention: 14 daily, preserve 1st-of-month snapshots indefinitely.
find "$BACKUP_DIR" -name 'metrics-????????.db' -type f \
    -mtime +14 ! -name 'metrics-??????01.db' -delete

echo "Backup written: $TARGET"
