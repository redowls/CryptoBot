#!/usr/bin/env bash
# Pull latest code, refresh deps if requirements.txt changed, restart service.
#
# Designed to be run by the trader user (no sudo for git/pip; sudo only for
# the systemctl restart at the end). Re-running with no upstream changes is
# a no-op apart from the restart.
#
# Usage (as trader):
#   ./deploy/update.sh
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BRANCH="${BRANCH:-main}"
SERVICE_NAME="cryptobot"

cd "${APP_DIR}"

log() { printf '[update] %s\n' "$*"; }

# Refuse to update with uncommitted local changes — easier to investigate
# than to silently stash them.
if ! git diff --quiet || ! git diff --cached --quiet; then
    log "ERROR: uncommitted changes in ${APP_DIR}; resolve before updating" >&2
    git status --short >&2
    exit 1
fi

log "fetching origin/${BRANCH}"
PREV=$(git rev-parse HEAD)
git fetch --quiet origin "${BRANCH}"
git checkout --quiet "${BRANCH}"
git pull --quiet --ff-only origin "${BRANCH}"
NEW=$(git rev-parse HEAD)

if [[ "${PREV}" == "${NEW}" ]]; then
    log "already at ${NEW:0:8}; restarting anyway to pick up any config edits"
else
    log "updated ${PREV:0:8} -> ${NEW:0:8}"
    if git diff --name-only "${PREV}" "${NEW}" | grep -qx 'requirements.txt'; then
        log "requirements.txt changed; reinstalling deps"
        ./venv/bin/pip install --quiet --upgrade pip
        ./venv/bin/pip install --quiet -r requirements.txt
    fi
    if git diff --name-only "${PREV}" "${NEW}" | grep -qx 'deploy/cryptobot.service'; then
        log "service file changed; reinstalling unit + daemon-reload"
        sudo install -m 644 deploy/cryptobot.service /etc/systemd/system/${SERVICE_NAME}.service
        sudo systemctl daemon-reload
    fi
fi

log "restarting ${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"
sleep 2
sudo systemctl --no-pager --lines=10 status "${SERVICE_NAME}" || true
log "done. tail logs with: journalctl -u ${SERVICE_NAME} -f"
