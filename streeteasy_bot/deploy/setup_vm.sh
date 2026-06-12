#!/usr/bin/env bash
#
# One-time provisioning for the StreetEasy bot on a fresh Ubuntu VM
# (e.g. Oracle Cloud Always-Free). Run it from inside the streeteasy_bot folder:
#
#   cd ~/NYC-Housing-Bot/streeteasy_bot
#   bash deploy/setup_vm.sh
#
# It installs system + Python dependencies and prepares the virtualenv. It does
# NOT start the service (you must create .env with your Gmail credentials first).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"
echo ">> Project dir: $SCRIPT_DIR"

echo ">> Installing system packages (python3-venv, pip, git)..."
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip git

echo ">> Creating virtualenv (.venv)..."
python3 -m venv .venv

echo ">> Installing Python requirements (HTTP mode only needs the light deps)..."
# Skip the heavy Playwright browser deps; HTTP mode doesn't use a browser.
grep -viE '^(playwright|playwright-stealth)' requirements.txt > /tmp/req-http.txt
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r /tmp/req-http.txt

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo ">> Created .env from template -- EDIT IT with your Gmail app password and recipients:"
  echo "     nano $SCRIPT_DIR/.env"
else
  echo ">> .env already exists, leaving it as-is."
fi

echo
echo ">> Done. Next steps:"
echo "   1) Edit .env  (GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_RECIPIENTS)"
echo "   2) Test email:   ./.venv/bin/python main.py --test-email"
echo "   3) Seed + run:   ./.venv/bin/python main.py --once -v   (then install the service)"
echo "   4) Install 24/7 service (see deploy/DEPLOY_ORACLE.md)."
