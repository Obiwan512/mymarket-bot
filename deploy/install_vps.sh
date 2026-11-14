#!/usr/bin/env bash
# Ubuntu/Debian VPS setup. Copy the whole bot folder (with your .env) to the server, then:
#   sudo bash deploy/install_vps.sh
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/mymarket-bot

apt-get update -y && apt-get install -y python3 python3-venv
id mymarket >/dev/null 2>&1 || useradd --system --home "$DEST" --shell /usr/sbin/nologin mymarket
mkdir -p "$DEST"
cp -r "$SRC"/. "$DEST"/
rm -rf "$DEST/.venv" "$DEST/.git"
python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install --upgrade pip
"$DEST/.venv/bin/pip" install -r "$DEST/requirements.txt"
chown -R mymarket:mymarket "$DEST"
chmod 600 "$DEST/.env"

echo "--- self test (must show OK for Telegram and mymarket API) ---"
sudo -u mymarket "$DEST/.venv/bin/python" "$DEST/bot.py" --selftest || true

cp "$DEST/deploy/mymarket-bot.service" /etc/systemd/system/mymarket-bot.service
systemctl daemon-reload
systemctl enable --now mymarket-bot
echo "Done. Logs: journalctl -u mymarket-bot -f   |   Edit filters: nano $DEST/watches.json (no restart needed)"
