# AGENTS.md — Safety rules (auto-loaded by OpenCode)

## Approved workspace
- Approved folder = this repo root only (`UptimeMonitor/`).
- NEVER read, edit, or delete anything outside it without explicit user approval.
- To approve another folder: add an explicit `external_directory` allow in `opencode.json` LAST (last match wins), e.g. `"/Users/you/Approved/**": "allow"`. Until then, outside = blocked.
- Personal dirs (`~/Documents`, `~/Desktop`, `~/Photos`, `~/Library`, iCloud) and corp mounts (`/Volumes/**`) are NEVER auto-touched. Always `ask`.

## Files
- Edits inside repo: allowed to propose, but destructive overwrites still prompt (`edit: ask`).
- NEVER touch secrets, even on read: `~/.ssh/**`, `~/.aws/**`, `~/.gnupg/**`, `~/.pki/**`, `~/.config/gcloud/**`, `~/.azure/**`, `~/.kube/**`, `~/Library/Keychains/**`, `/etc/**`, `*.pem`, `*.key`, `*.p12`, `.env*`, `*secret*`, `*credential*`. Tell the user to handle them manually.
- `snapshot: true` is on — edits can be undone, but don't rely on it for secrets.

## Shell
- Default = `ask`. Prompt-free: `ls`, `pwd`, `git status/diff/log/branch/show` (+ `add`/`commit`/`stash`/`checkout`/`pull`), any `python3 *`, `sh scripts/*`, the `uptime*` aliases, and read-only text utils (`awk`, `sed -n`, `cat`, `head`, `tail`, `wc`, `diff`, `grep`, `sleep`, `uname`, `command`). Everything else still prompts; hard blocks below ignore Allow.
- NEVER run without explicit approval: `sudo/su`, `rm/rmdir/unlink/shred`, `dd/mkfs/fdisk/parted/diskutil erase`, `chmod/chown`, `shutdown/reboot/halt`, `kill/killall`, `curl|sh`, `eval`, `git push --force`, `git reset --hard`, `git clean -fd`, `docker prune/rm`, `kubectl delete`, `terraform destroy`, `launchctl unload`, `systemctl`, `defaults delete/write`, `scp/rsync/ftp`, `curl -F/--data` (uploads), anything touching `~/.ssh` or `~/.aws`.
- The `SafetyGuard` plugin hard-blocks the above even if Allow is clicked. If blocked, explain why and offer a safe alternative.

## Network / uploads
- `share: disabled` — `/share` is off. NEVER upload sessions, reports, or docs to `opncd.ai` or any external host without explicit opt-in. To enable for a trusted project only, set `"share": "manual"` in that project's `opencode.json`.
## Private info (this monitor sees your IPs — keep them out of the LLM)
- NEVER send to the model: public IP, local IP, gateway, SSID, MAC, hostname, username, ISP/ASN/city, or any bytes from the live DB (`~/Library/Caches/UptimeMonitor/`, `~/.local/share/uptime-monitor/`), `reports/`, `*.db`, `*.log`, generated dashboard HTML. Debug with `src/*.py` + `config.json` + synthetic fixtures (RFC-5737 `203.0.113.10`) only.
- Runtime-data reads are permission-denied by design — do not work around them. Redact leftovers as `<redacted>`.
- `webfetch` / `websearch`: always `ask`. Never paste proprietary code, personal docs, or network identifiers into queries or URLs. Prefer local files.
- This project is local-only by design (`127.0.0.1:8899`, no outbound except pings). Keep it that way.

## Scratch tmp (approved external data dir)
- ONLY approved outside-repo write target: `${TMPDIR:-/tmp}/opencode-scratch/sessions/YYYY-MM-DD_HH-MM-SS_<slug>/{scrape,tools-out,external}` + `SESSION.md`. All webfetch/scrape/tool/external bytes go there — NEVER repo root, `/tmp/` root, `~/Documents`, or `/Volumes/**`.
- Refresh `SESSION.md` mtime on reuse (`touch`). Stale >72h (`OPENCODE_SCRATCH_TTL_HOURS`) = clean via GLOBAL `sh ~/.config/opencode/scripts/clean-scrape-tmp.sh --dry-run` then `--apply` (local mirror `sh scripts/clean-scrape-tmp.sh` is byte-identical). NEVER hand-`rm`; script requires `.opencode-scratch-root` marker and refuses outside scratch root.

## If unsure
Stop, explain the risk in one line, and ask. Never guess with deletes, privilege escalation, or uploads.
