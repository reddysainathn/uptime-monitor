#!/bin/sh
# UptimeMonitor shell aliases — bash + zsh. Idempotent: safe to re-run
# anytime; it replaces its own block instead of duplicating it.
#
# Usage:  sh scripts/aliases.sh
# Then:   source ~/.zshrc   (or ~/.bashrc, or just open a new terminal)
#
# Gives you, from any folder:
#   uptime          status (is it running? dashboard + data dir)
#   uptime-on       enable background monitoring
#   uptime-off      disable (keeps all logged data)
#   uptime-status   same as uptime
#   uptime-logs     tail the monitor log
#   uptime-report   build the 7-day ISP report
#   uptime-probe    single live check, prints ONLINE/OFFLINE + latency
#   uptime-open     open the local dashboard in your browser
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
MARK="# >>> UptimeMonitor aliases >>>"
ENDM="# <<< UptimeMonitor aliases <<<"
if [ "$(uname -s)" = "Darwin" ]; then
  OPENER="open http://127.0.0.1:8899"
else
  OPENER="xdg-open http://127.0.0.1:8899"
fi

block() {
  cat <<EOF
$MARK (repo: $REPO — managed by 'sh scripts/aliases.sh', safe to re-run)
alias uptime="sh \"$REPO/scripts/uptime-monitor\" status"
alias uptime-on="sh \"$REPO/scripts/uptime-monitor\" on"
alias uptime-off="sh \"$REPO/scripts/uptime-monitor\" off"
alias uptime-status="sh \"$REPO/scripts/uptime-monitor\" status"
alias uptime-logs="sh \"$REPO/scripts/uptime-monitor\" logs"
alias uptime-report="(cd \"$REPO\" && python3 -m src.report --days 7)"
alias uptime-probe="(cd \"$REPO\" && python3 -m src.monitor --once)"
alias uptime-open="$OPENER"
$ENDM
EOF
}

# Override for testing: UPTIME_RC_FILES="/tmp/fake.bashrc /tmp/fake.zshrc"
RC_FILES="${UPTIME_RC_FILES:-$HOME/.bashrc $HOME/.zshrc}"
# shellcheck disable=SC2086 — intentional word splitting of the file list
for rc in $RC_FILES; do
  [ -e "$rc" ] || touch "$rc"
  tmp="${rc}.uptime-aliases.$$"
  # copy the rc minus any previous alias block, then append a fresh one.
  # prefix match: the MARK line carries a "(repo: …)" suffix, so whole-line
  # equality would miss blocks installed by an earlier run.
  awk -v m="$MARK" -v e="$ENDM" '
    index($0, m) == 1 {skip = 1; next}
    index($0, e) == 1 {skip = 0; next}
    !skip {print}
  ' "$rc" > "$tmp"
  block >> "$tmp"
  mv "$tmp" "$rc"
  echo "aliases installed in $rc"
done
echo "done. Reload with:  source ~/.zshrc   (or ~/.bashrc, or open a new terminal)"
echo "try:  uptime-status"
