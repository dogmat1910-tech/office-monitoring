#!/bin/bash
# Скрипт синхронизирует /opt/office-monitoring/public/{agent,watchdog}.exe
# с последним GitHub release (по тегу). Запускается systemd-таймером каждые 5 минут.
set -euo pipefail

REPO="dogmat1910-tech/office-monitoring"
PUBLIC_DIR="/opt/office-monitoring/public"
LOG_FILE="/var/log/om-update-exes.log"

mkdir -p "$PUBLIC_DIR"
touch "$LOG_FILE"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

LATEST=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" | grep -oP '"tag_name":\s*"\K[^"]+' || true)
if [ -z "$LATEST" ]; then
    log "failed to fetch latest tag from github"
    exit 1
fi

CURRENT=$(cat "$PUBLIC_DIR/VERSION" 2>/dev/null || echo "")
if [ "$LATEST" = "$CURRENT" ]; then
    exit 0
fi

log "syncing $CURRENT -> $LATEST"

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

curl -fsSL "https://github.com/$REPO/releases/download/$LATEST/office-monitoring-agent.exe"    -o "$TMPDIR/agent.exe"
curl -fsSL "https://github.com/$REPO/releases/download/$LATEST/office-monitoring-watchdog.exe" -o "$TMPDIR/watchdog.exe"

# Sanity check — .exe не может быть меньше мегабайта
if [ "$(stat -c%s "$TMPDIR/agent.exe")" -lt 1000000 ]; then
    log "agent.exe suspiciously small, abort"
    exit 1
fi
if [ "$(stat -c%s "$TMPDIR/watchdog.exe")" -lt 1000000 ]; then
    log "watchdog.exe suspiciously small, abort"
    exit 1
fi

# Атомарная подмена. mv в пределах одной FS — атомарен.
mv "$TMPDIR/agent.exe"    "$PUBLIC_DIR/agent.exe"
mv "$TMPDIR/watchdog.exe" "$PUBLIC_DIR/watchdog.exe"
echo -n "$LATEST" > "$PUBLIC_DIR/VERSION"

log "updated to $LATEST (agent=$(stat -c%s "$PUBLIC_DIR/agent.exe") bytes, watchdog=$(stat -c%s "$PUBLIC_DIR/watchdog.exe") bytes)"
