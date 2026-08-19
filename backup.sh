#!/bin/bash
# Backs up tracker.db with a timestamped filename, and keeps the last 14 backups.

APP_DIR="/home/vgpi/baby-tracker"
BACKUP_DIR="$APP_DIR/backups"
DB_FILE="$APP_DIR/tracker.db"
TIMESTAMP=$(date +%Y-%m-%d_%H-%M)

mkdir -p "$BACKUP_DIR"

if [ -f "$DB_FILE" ]; then
    cp "$DB_FILE" "$BACKUP_DIR/tracker_$TIMESTAMP.db"
    # Keep only the 14 most recent backups
    ls -1t "$BACKUP_DIR"/tracker_*.db | tail -n +15 | xargs -r rm --
fi
