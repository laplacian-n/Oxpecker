#!/usr/bin/env bash
# Backs up this project's durable local state — agent/state/ (sessions, findings, evidence,
# technique KB, audit log, API key files) and engagements/ + the legacy engagement/ dir (RoE,
# scope, per-engagement SQLite stores) — to a timestamped tar.gz, then prunes old backups beyond
# a retention count. Everything here lives only on this machine (this project keeps no cloud
# state at all); the point is surviving a disk failure or a bad `rm`, not remote redundancy — if
# you want off-machine redundancy, point BACKUP_DIR at a mounted remote/removable volume, or
# rsync the resulting archives elsewhere yourself.
#
# Usage: ./deploy/backup-localai.sh [backup_dir] [retention_count]
#   backup_dir       default: ~/localai-backups
#   retention_count  default: 14 (keep the newest N archives, delete older ones)
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${1:-$HOME/localai-backups}"
RETENTION="${2:-14}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="$BACKUP_DIR/localai-state-$TIMESTAMP.tar.gz"

mkdir -p "$BACKUP_DIR"

# Only the paths that actually hold durable state — never the .venv, __pycache__, or the static
# frontend, which are all reproducible from the repo itself.
PATHS_TO_BACKUP=()
[ -d "$PROJECT_DIR/agent/state" ] && PATHS_TO_BACKUP+=("agent/state")
[ -d "$PROJECT_DIR/engagements" ] && PATHS_TO_BACKUP+=("engagements")
[ -d "$PROJECT_DIR/engagement" ] && PATHS_TO_BACKUP+=("engagement")

if [ "${#PATHS_TO_BACKUP[@]}" -eq 0 ]; then
    echo "nothing to back up yet (no agent/state, engagements/, or engagement/ dir found)"
    exit 0
fi

tar -czf "$ARCHIVE" -C "$PROJECT_DIR" "${PATHS_TO_BACKUP[@]}"
echo "wrote $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"

# Prune: keep only the newest $RETENTION archives in $BACKUP_DIR.
mapfile -t existing < <(ls -1t "$BACKUP_DIR"/localai-state-*.tar.gz 2>/dev/null)
if [ "${#existing[@]}" -gt "$RETENTION" ]; then
    for old in "${existing[@]:$RETENTION}"; do
        rm -f "$old"
        echo "pruned $old"
    done
fi
