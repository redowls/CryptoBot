#!/usr/bin/env bash
# Idempotent installer for cryptobot on Ubuntu 24.04.
#
# Assumes Session 3 prerequisites are already done on this VPS:
#   - SQL Server 2025 Express running (mssql-server.service active)
#   - Microsoft ODBC Driver 18 installed (msodbcsql18 package)
#   - Python 3.11+ and git available
#   - UFW configured (port 1433 restricted to home IP, 22 open)
#
# Run as root (or with sudo). Re-running is safe — each step checks for
# existing state before acting.
#
# Usage:
#   sudo REPO_URL=https://github.com/<you>/cryptobot.git ./deploy/install.sh
#   sudo REPO_URL=git@github.com:<you>/cryptobot.git ./deploy/install.sh
set -euo pipefail

# --- config (override via env) ---
TRADER_USER="${TRADER_USER:-trader}"
TRADER_HOME="${TRADER_HOME:-/home/${TRADER_USER}}"
APP_DIR="${APP_DIR:-${TRADER_HOME}/cryptobot}"
REPO_URL="${REPO_URL:?set REPO_URL to the cryptobot git remote (https or ssh)}"
BRANCH="${BRANCH:-main}"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"
SERVICE_NAME="cryptobot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

log() { printf '[install] %s\n' "$*"; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "must run as root (sudo)"
command -v "${PYTHON_BIN}" >/dev/null || die "${PYTHON_BIN} not found; install Python 3.11+ first"
command -v git >/dev/null            || die "git not found; apt install git"
systemctl is-active --quiet mssql-server || log "WARN: mssql-server not active — bot will fail to connect to SQL"

# --- 1. trader user ---
if id "${TRADER_USER}" &>/dev/null; then
    log "user ${TRADER_USER} already exists"
else
    log "creating user ${TRADER_USER}"
    useradd --create-home --shell /bin/bash "${TRADER_USER}"
fi

# --- 2. clone or update repo ---
if [[ -d "${APP_DIR}/.git" ]]; then
    log "repo already cloned at ${APP_DIR}; fetching latest"
    sudo -u "${TRADER_USER}" git -C "${APP_DIR}" fetch --quiet origin
    sudo -u "${TRADER_USER}" git -C "${APP_DIR}" checkout --quiet "${BRANCH}"
    sudo -u "${TRADER_USER}" git -C "${APP_DIR}" pull --quiet --ff-only origin "${BRANCH}"
else
    log "cloning ${REPO_URL} -> ${APP_DIR}"
    sudo -u "${TRADER_USER}" git clone --branch "${BRANCH}" "${REPO_URL}" "${APP_DIR}"
fi

# --- 3. .env file ---
if [[ ! -f "${APP_DIR}/.env" ]]; then
    if [[ -f "${APP_DIR}/.env.example" ]]; then
        log "no .env present; copying .env.example -> .env (FILL IN BEFORE STARTING)"
        sudo -u "${TRADER_USER}" cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
    else
        die ".env.example missing — cannot bootstrap .env"
    fi
fi
chown "${TRADER_USER}:${TRADER_USER}" "${APP_DIR}/.env"
chmod 600 "${APP_DIR}/.env"

# --- 4. venv + deps ---
if [[ ! -d "${APP_DIR}/venv" ]]; then
    log "creating venv with ${PYTHON_BIN}"
    sudo -u "${TRADER_USER}" "${PYTHON_BIN}" -m venv "${APP_DIR}/venv"
fi
log "installing requirements"
sudo -u "${TRADER_USER}" "${APP_DIR}/venv/bin/pip" install --quiet --upgrade pip
sudo -u "${TRADER_USER}" "${APP_DIR}/venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"

# --- 5. log directory ---
sudo -u "${TRADER_USER}" mkdir -p "${APP_DIR}/logs" "${APP_DIR}/backtest/results"

# --- 6. systemd unit ---
log "installing ${SERVICE_FILE}"
install -m 644 "${APP_DIR}/deploy/cryptobot.service" "${SERVICE_FILE}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

# --- 7. final guard: refuse to auto-start with empty .env ---
if ! grep -qE '^ALPACA_API_KEY=.+' "${APP_DIR}/.env"; then
    log "ALPACA_API_KEY empty in .env — NOT starting service yet."
    log "Fill in ${APP_DIR}/.env, then: sudo systemctl start ${SERVICE_NAME}"
    exit 0
fi

log "starting ${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"
sleep 2
systemctl --no-pager --lines=10 status "${SERVICE_NAME}" || true
log "done. tail logs with: journalctl -u ${SERVICE_NAME} -f"
