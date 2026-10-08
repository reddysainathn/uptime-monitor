# UptimeMonitor — free, tiny, ISP-proof internet monitor

Checks your connection **every 1 second**, tells apart **your WiFi off** vs **router dead** vs **ISP down**, keeps a local dashboard + one-click ISP report. No cloud, no account, no pip packages. Python 3.9–3.13, macOS / Linux / Windows. Tested: macOS ✓ · Windows ✗ (not tested here).

## Quick start

Prerequisites: `python3` 3.9–3.13 (`python3 --version`). No pip packages needed.

```sh
git clone https://github.com/reddysainathn/uptime-monitor.git
cd uptime-monitor
sh scripts/install.sh        # mac/linux: runs tests + enables autostart (survives reboot/sleep)
```

Windows: double-click `scripts\install_windows.bat`.

Then bookmark **http://127.0.0.1:8899** (local page — works even when the internet is down).

Check it's running: `sh scripts/uptime-monitor status`
One sample, writes nothing: `python3 -m src.monitor --once`
Foreground instead of background: `python3 -m src.monitor`

## Service switch (enable / disable as you need)

```sh
sh scripts/uptime-monitor on      # enable background monitoring
sh scripts/uptime-monitor off     # disable — keeps all logged data
sh scripts/uptime-monitor status  # is it running? + dashboard + data dir
sh scripts/uptime-monitor logs    # tail the monitor log
```

## What the colors mean (for you + your ISP)

- **Green Online** — internet worked
- **Red ISP outage** — WiFi/router were ON but 1.1.1.1 + 8.8.8.8 + 9.9.9.9 + HTTPS all failed → **ISP's fault**
- **Orange Router/local** — gateway unreachable → your router/cable, not the ISP
- **Gray WiFi off (you) / Sleep / App was off** — you turned WiFi off, laptop slept, or monitor wasn't running. **Never counted** as ISP downtime, never pops the dashboard.

## Auto-open rule (no tab spam)

- Opens dashboard **once** when an outage passes **30s** and WiFi/Ethernet is on.
- Same outage never re-opens. Flapping shares a **10-min cooldown** (`auto_open_cooldown_sec` in `config.json`).
- The opened tab auto-refreshes every 15s, so it stays live for a 10-min outage with just 1 tab.

## ISP report

```sh
python3 -m src.report --days 7
# -> reports/report-YYYY-MM-DD-7d.csv  (open in Excel)
# -> reports/report-YYYY-MM-DD-7d.html (open, Print → Save as PDF, email it)
```

Send both. Say: *"Red rows = my equipment was on, your network unreachable. Please check the line."*

## Dashboard does everything (no terminal needed)

Open **http://127.0.0.1:8899** — status, timeline, heatmap, plus:

- **Connection card** — active medium (`WiFi (HomeNet)` / `Ethernet` / `WiFi + Ethernet` / `WiFi Off`), data consumed (session + total up/down), public IP, ISP, ASN, city, local IP, gateway, DNS. ISP lookups are cached (IP change / 6h) and can be turned off.
- **ISP reports** — generate 1/7/30-day report, open/download, delete one, or **Clean old reports**. Hard caps auto-enforced: max 20 files, 30 days, 5 MB.
- **Settings** — safe toggles only (auto-open on/off + timing, ISP lookup on/off, dashboard refresh, report retention). Thresholds/ports stay in `config.json` by hand so a misclick can't break detection. Saved beside the DB, survive restarts.
- **Storage line** — live DB size proves the 10 MB cap.

## Where's my data?

Auto-picked at startup (first location where SQLite actually works — synced
folders block it, so the app probes): macOS `~/Library/Caches/UptimeMonitor/`,
Linux `~/.local/share/uptime-monitor/`, else `/tmp`. Override with
`UPTIME_DB=/path/uptime.db`. The repo itself stays clean — only code lives here.
DB is capped at ~10 MB with daily rollup + prune; logs rotate at 1 MB.

## Tiny by design (expert notes)

- **~3–5 MB/year**: events only on change + 1 heartbeat row/min (buffered, 1 write/min) + daily rollup after 30d + auto-prune over 10 MB + WAL checkpoint on flush.
- **~0.3% CPU, ~15 MB RAM**: 1s ping×3 + TCP in a reused 4-thread pool; WiFi/SSID checks throttled to every 5s; dashboard regen every 10s.
- **Crash-safe**: outages commit immediately; buffer flushed on SIGTERM; kill/reboot inserts honest gray `GAP` on next start (never fake uptime).
- **Logs capped**: monitor log rotates at 1 MB in the data dir. DB pruned daily + on start.
- **Safe**: `127.0.0.1` only, no outbound except pings, no uploads, all subprocess calls time-bounded.

## Files

```
config.json            # thresholds, port, cooldown — edit freely
src/paths.py             # auto-picks a working data dir (probes SQLite)
src/db.py                # sqlite + anti-bloat (prune/rollup/checkpoint)
src/netcheck.py        # cross-platform ping/tcp/wifi/gateway (never raises)
src/monitor.py         # 1s daemon + server + auto-open + gap recovery
src/dashboard_gen.py   # static HTML dashboard (offline-friendly)
src/report.py          # CSV + printable ISP letter
scripts/install.sh / install_windows.bat / *.plist / *.service
tests/test_smoke.py    # unittest, stdlib only
```

## Uninstall

macOS: `launchctl unload ~/Library/LaunchAgents/com.uptime.monitor.plist` · Linux: `systemctl --user disable --now uptime-monitor` · Windows: `schtasks /delete /tn UptimeMonitor /f`
