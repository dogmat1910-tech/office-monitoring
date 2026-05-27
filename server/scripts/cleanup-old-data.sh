#!/bin/bash
# Retention policy: удаляем старые данные, чтобы диск не кончался.
# Запускается cron'ом раз в сутки в 03:00.
set -euo pipefail

DB="/opt/office-monitoring/server/office_monitoring.db"
VENV="/opt/office-monitoring/server/.venv/bin/python"
LOG="/var/log/om-cleanup.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

$VENV -c "
import sqlite3, os
from datetime import datetime, timedelta

conn = sqlite3.connect('$DB', timeout=30)
cur = conn.cursor()

now = datetime.utcnow()

# Retention periods:
policies = {
    # table: (date_column, days_to_keep)
    'screenshot':       ('captured_at',  30),
    'voicesegment':     ('received_at',  60),
    'windowsample':     ('captured_at',  90),
    'idlesample':       ('captured_at',  90),
    'keystrokesample':  ('captured_at',  90),
    'keystroketext':    ('received_at',  90),
    'heartbeat':        ('received_at',  30),
    'audiochunk':       ('received_at',  30),   # raw audio chunks
    'transcript':       ('created_at',   365),
    'analysis':         ('created_at',   365),
    'conversation':     ('started_at',   365),
    'dailyreport':      ('report_date',  365),
    'agentdiagnostics': ('received_at',  30),
    'categoryauditfeedback': ('reviewed_at', 365),
}

total_deleted = 0
for table, (col, days) in policies.items():
    cutoff = (now - timedelta(days=days)).strftime('%Y-%m-%dT00:00:00')
    try:
        cur.execute(f'DELETE FROM {table} WHERE {col} < ?', (cutoff,))
        n = cur.rowcount
        if n > 0:
            print(f'{table}: deleted {n} rows older than {days} days')
            total_deleted += n
    except Exception as e:
        print(f'{table}: error {e}')

conn.commit()

# Скриншоты — также удаляем файлы с диска
screenshots_dir = '/opt/office-monitoring/server/screenshots_data'
voice_dir = '/opt/office-monitoring/server/voice_data'
audio_dir = '/opt/office-monitoring/server/audio_data'

import shutil
from pathlib import Path

for d in [screenshots_dir, voice_dir, audio_dir]:
    p = Path(d)
    if not p.exists():
        continue
    cutoff_days = 30 if 'screenshot' in d else 60 if 'voice' in d else 30
    cutoff_ts = (now - timedelta(days=cutoff_days)).timestamp()
    for f in p.rglob('*'):
        if f.is_file() and f.stat().st_mtime < cutoff_ts:
            f.unlink()
            total_deleted += 1

# VACUUM если удалили много
if total_deleted > 1000:
    print(f'VACUUM (deleted {total_deleted} total)')
    cur.execute('VACUUM')

conn.close()
print(f'cleanup done: {total_deleted} items removed')
"

log "cleanup finished"
