#!/bin/bash
# WAL checkpoint maintenance for kissinger-api graph.db
# Runs daily at 02:00 UTC to prevent unbounded WAL growth

set -e
DB_PATH="/home/lobster/.kissinger/graph.db"
LOG_FILE="/home/lobster/lobster-workspace/logs/wal-checkpoint.log"

mkdir -p "$(dirname "$LOG_FILE")"

{
  echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] Starting WAL checkpoint..."
  
  # Check if database exists
  if [[ ! -f "$DB_PATH" ]]; then
    echo "ERROR: Database not found at $DB_PATH"
    exit 1
  fi
  
  # Run passive checkpoint (hourly pattern)
  echo "Running PASSIVE checkpoint..."
  PASSIVE_RESULT=$(sqlite3 "$DB_PATH" ".timeout 30000" "PRAGMA wal_checkpoint(PASSIVE);" 2>&1)
  echo "PASSIVE result: $PASSIVE_RESULT"
  
  # Run truncate checkpoint (daily pattern)
  echo "Running TRUNCATE checkpoint..."
  TRUNCATE_RESULT=$(sqlite3 "$DB_PATH" ".timeout 120000" "PRAGMA wal_checkpoint(TRUNCATE);" 2>&1)
  echo "TRUNCATE result: $TRUNCATE_RESULT"
  
  # Verify integrity
  INTEGRITY=$(sqlite3 "$DB_PATH" ".timeout 30000" "PRAGMA quick_check;" 2>&1)
  echo "Integrity check: $INTEGRITY"
  
  # Log file sizes
  echo "Database sizes:"
  ls -lh "$DB_PATH"* 2>/dev/null | awk '{print $9, $5}'
  
  echo "[$(date -u +'%Y-%m-%d %H:%M:%S')] WAL checkpoint completed successfully"
} >> "$LOG_FILE" 2>&1

