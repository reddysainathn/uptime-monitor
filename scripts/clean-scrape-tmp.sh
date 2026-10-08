#!/bin/sh
# clean-scrape-tmp.sh — GLOBAL safe stale-session cleanup for opencode scratch.
# Canonical location: ~/.config/opencode/scripts/clean-scrape-tmp.sh (works for ALL projects).
# Project copies (if any) must stay byte-identical to this file.
#
#   sh ~/.config/opencode/scripts/clean-scrape-tmp.sh --dry-run            # default: list only
#   sh ~/.config/opencode/scripts/clean-scrape-tmp.sh --apply              # actually delete
#   OPENCODE_SCRATCH_TTL_HOURS=24 sh ... --dry-run                         # custom TTL
#   OPENCODE_SCRATCH_ROOT=/tmp/opencode-scratch sh ... --dry-run           # custom root
#
# SAFETY: deletes ONLY inside $ROOT/sessions/<timestamped-dir>.
# Refuses to run if $ROOT does not contain "opencode-scratch" or lacks the
# `.opencode-scratch-root` marker. NEVER pass / /tmp $HOME ~ as root.
set -eu

ROOT="${OPENCODE_SCRATCH_ROOT:-${TMPDIR:-/tmp}/opencode-scratch}"
TTL_HOURS="${OPENCODE_SCRATCH_TTL_HOURS:-72}"
MODE="dry-run"

usage() {
  echo "usage: clean-scrape-tmp.sh [--dry-run|--apply] [--ttl-hours=N] [--root=DIR]"
  echo "  default TTL: ${TTL_HOURS}h via OPENCODE_SCRATCH_TTL_HOURS"
  exit 0
}

for a in "$@"; do
  case "$a" in
    --apply) MODE="apply" ;;
    --dry-run) MODE="dry-run" ;;
    --ttl-hours=*) TTL_HOURS="${a#--ttl-hours=}" ;;
    --root=*) ROOT="${a#--root=}" ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $a" >&2; usage ;;
  esac
done

case "$TTL_HOURS" in ''|*[!0-9]*) echo "FATAL: TTL must be integer hours, got '$TTL_HOURS'" >&2; exit 2;; esac
case "$ROOT" in *opencode-scratch*) : ;; *) echo "FATAL: refusing — root '$ROOT' lacks 'opencode-scratch'" >&2; exit 2;; esac
case "$ROOT" in "/"|"/tmp"|"$HOME"|"$HOME/"|"~"|"~/"|"") echo "FATAL: refusing dangerous root '$ROOT'" >&2; exit 2;; esac
if [ ! -f "$ROOT/.opencode-scratch-root" ]; then
  echo "FATAL: marker missing: $ROOT/.opencode-scratch-root" >&2
  echo "Create the scratch root first (mkdir -p) or pass --root=<scratch-root>." >&2
  exit 2
fi

SESSIONS="$ROOT/sessions"
if [ ! -d "$SESSIONS" ]; then
  echo "No sessions dir yet: $SESSIONS (nothing to clean)"
  exit 0
fi

NOW=$(date +%s)
DELETED=0
KEPT=0
FREED=0

for d in "$SESSIONS"/*/; do
  [ -d "$d" ] || continue
  # last-used = SESSION.md mtime if present, else dir mtime (portable: stat -f %m macOS, stat -c %Y linux)
  MT=0
  if [ -f "$d/SESSION.md" ]; then
    MT=$(stat -f %m "$d/SESSION.md" 2>/dev/null || stat -c %Y "$d/SESSION.md" 2>/dev/null || echo 0)
  else
    MT=$(stat -f %m "$d" 2>/dev/null || stat -c %Y "$d" 2>/dev/null || echo 0)
  fi
  AGE_H=0
  if [ "$MT" -gt 0 ]; then AGE_H=$(( (NOW - MT) / 3600 )); fi
  SIZE=$(du -sk "$d" 2>/dev/null | cut -f1)
  if [ "$AGE_H" -ge "$TTL_HOURS" ]; then
    if [ "$MODE" = "apply" ]; then
      # Final containment: target must still be inside sessions dir
      case "$d" in "$SESSIONS/"*) rm -rf -- "$d" && echo "DELETED (${AGE_H}h old, ${SIZE}KB): $d" ;; *) echo "SKIP (escapes root): $d" >&2 ;; esac
      DELETED=$((DELETED + 1)); FREED=$((FREED + SIZE))
    else
      echo "STALE   (${AGE_H}h old, ${SIZE}KB): $d"
      DELETED=$((DELETED + 1)); FREED=$((FREED + SIZE))
    fi
  else
    echo "KEEP    (${AGE_H}h old, ${SIZE}KB): $d"
    KEPT=$((KEPT + 1))
  fi
done

if [ "$MODE" = "apply" ]; then
  echo "Done: deleted=$DELETED kept=$KEPT freed=${FREED}KB (TTL=${TTL_HOURS}h)"
else
  echo "Dry-run: stale=$DELETED kept=$KEPT reclaimable=${FREED}KB (TTL=${TTL_HOURS}h). Re-run with --apply to delete."
fi
