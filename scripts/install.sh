#!/bin/sh
# UptimeMonitor installer — macOS + Linux. Stdlib only, no pip.
# Safe: only writes inside this repo + one autostart file. Never sudo unless you ask.
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$(command -v python3)"
echo "repo: $REPO"
echo "python: $PY ($($PY --version 2>&1))"
"$PY" -m unittest discover -s tests -v
"$PY" -c "from src.paths import data_dir; print('data dir:', data_dir())"
OS="$(uname -s)"
if [ "$OS" = "Darwin" ]; then
  DEST="$HOME/Library/LaunchAgents/com.uptime.monitor.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  sed -e "s#__REPO__#$REPO#g" -e "s#__PY__#$PY#g" "$REPO/scripts/com.uptime.monitor.plist" > "$DEST"
  launchctl unload "$DEST" 2>/dev/null || true
  launchctl load "$DEST"
  echo "macOS autostart installed. Dashboard: http://127.0.0.1:8899"
  echo "logs: tail -f $REPO/data/monitor.log"
elif [ "$OS" = "Linux" ]; then
  DEST="$HOME/.config/systemd/user/uptime-monitor.service"
  mkdir -p "$HOME/.config/systemd/user"
  sed -e "s#__REPO__#$REPO#g" -e "s#__PY__#$PY#g" "$REPO/scripts/uptime-monitor.service" > "$DEST"
  systemctl --user daemon-reload
  systemctl --user enable --now uptime-monitor.service
  echo "linux autostart installed. Dashboard: http://127.0.0.1:8899"
else
  echo "Unknown OS. Just run: $PY -m src.monitor"
fi
echo "Bookmark this: http://127.0.0.1:8899"
echo "Manual run: $PY -m src.monitor"
echo "ISP report: $PY -m src.report --days 7"
