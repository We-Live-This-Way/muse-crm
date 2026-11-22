#!/bin/bash
# Nightly hot backup of the CRM database (safe while the server runs).
# Usage: ./backup.sh  — keeps 14 days in /var/backups/lead-crm.
# Add to cron:  0 3 * * * /opt/lead-crm/backup.sh
set -euo pipefail
DB="${CRM_DB:-/opt/lead-crm/crm.db}"
DEST_DIR="${BACKUP_DIR:-/var/backups/lead-crm}"
mkdir -p "$DEST_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
sqlite3 "$DB" ".backup '$DEST_DIR/crm-$STAMP.db'"
chmod 600 "$DEST_DIR/crm-$STAMP.db"
find "$DEST_DIR" -name 'crm-*.db' -mtime +14 -delete
echo "Backed up to $DEST_DIR/crm-$STAMP.db"
