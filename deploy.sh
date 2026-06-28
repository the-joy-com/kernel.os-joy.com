#!/usr/bin/env bash
set -euo pipefail

# Pull the latest main, bring the venv in line with uv.lock, make sure the systemd unit is current, and restart the service. 
# Run from the repo on the server: `./deploy.sh`.
# The shell's deploy ends in `rsync dist/ → docroot` because nothing runs — it's static files. 
# The kernel is a long-running process, so the payload step here is a service restart instead: same rhythm, different last move.
export UV_PROJECT_ENVIRONMENT=venv

# The server user owns this clone — its home holds the venv and uv, and the systemd unit runs as it. 
# Asked once, then remembered in a gitignored file so every later deploy is non-interactive.
USER_FILE=".deploy-user"
if [[ -f "$USER_FILE" ]]; then
  SERVER_USER="$(cat "$USER_FILE")"
else
  read -rp "server user (owns this clone; e.g. $(whoami)): " SERVER_USER
  [[ -n "$SERVER_USER" ]] || { echo "no user given, aborting" >&2; exit 1; }
  printf '%s\n' "$SERVER_USER" > "$USER_FILE"
fi

git pull --ff-only
uv sync --frozen          # install exactly what uv.lock pins; never resolve anew

# Render the unit with the server user filled in, and install it only when it actually changed — so daemon-reload + enable aren't paid on every deploy.
UNIT=/etc/systemd/system/kernel-os-joy.service
RENDERED="$(mktemp)"
trap 'rm -f "$RENDERED"' EXIT
sed "s/__SERVER_USER__/$SERVER_USER/g" deploy/kernel-os-joy.service > "$RENDERED"
if ! sudo cmp -s "$RENDERED" "$UNIT" 2>/dev/null; then
  sudo cp "$RENDERED" "$UNIT"
  sudo systemctl daemon-reload
  sudo systemctl enable kernel-os-joy   # idempotent; first run wires boot persistence
fi

sudo systemctl restart kernel-os-joy
echo "deployed → https://kernel.os-joy.com"
