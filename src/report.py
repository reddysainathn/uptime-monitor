"""ISP report exporter. Stdlib only, Python 3.9+.

Full accounting, begin to end:
- Total checks performed (one per second), up/down/router/wifi-off seconds
- Every broken period listed with start, end, duration
- Day-by-day pace table
- Brief blips (<3s, single lost packets) separated from CONFIRMED drops,
  so the report never overclaims.

HARD LIMITS (auto-enforced after every generation + daily + on-demand):
- REPORT_RETAIN_DAYS  = 30, REPORT_RETAIN_COUNT = 20, REPORT_MAX_MB = 5.

Run: python3 -m src.report --days 7
"""
import argparse
import csv
import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from .paths import data_dir
from . import timefmt as TF

ROOT = Path(__file__).resolve().parent.parent
DD = data_dir()

REPORT_RETAIN_DAYS = 30
REPORT_RETAIN_COUNT = 20
REPORT_MAX_MB = 5.0
REPORT_RE = re.compile(r"^report-[\w\-.]+\.(csv|html)$")
CONFIRM_SEC = 3  # drops shorter than this are single-packet blips, not proof

PLAIN = {
    "OFFLINE_ISP": "internet down (provider side)",
    "LOCAL_FAIL": "router did not answer",
    "WIFI_OFF": "you had WiFi off (not counted)",
    "NO_LINK": "no usable connection (not counted)",
    "SLEEP": "computer asleep (not counted)",
    "GAP": "monitor not running (not counted)",
    "ONLINE": "working",
}


def _fmt(sec):
    sec = int(sec or 0)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h >= 48:
        return "%dd %dh" % (h // 24, h % 24)
    return "%dh %02dm %02ds" % (h, m, s) if h else ("%dm %02ds" % (m, s) if m else "%ds" % s)


def load_identity(db_path):
    """(enabled, info). enabled None = unknown. info {} = not collected yet.

    Never claims more than it knows: if the lookup is off or nothing was
    collected (e.g. never online for 10 min), the report says so plainly
    instead of showing blanks as facts.
    """
    try:
        dd = Path(db_path).parent
        info = {}
        try:
            info = json.loads((dd / "ipinfo.json").read_text())
        except Exception:
            pass
        enabled = None
        try:
            cfg = json.loads((ROOT / "config.json").read_text())
            if "isp_details_enabled" in cfg:
                enabled = bool(cfg["isp_details_enabled"])
        except Exception:
            pass
        try:
            local = json.loads((dd / "config.local.json").read_text())
            if "isp_details_enabled" in local:
                enabled = bool(local["isp_details_enabled"])
        except Exception:
            pass
        if enabled is None:
            enabled = True
        return enabled, info if isinstance(info, dict) else {}
    except Exception:
        return None, {}


def load_router_flag(db_path):
    """True if the router/modem is the ISP's own box. Data-dir override wins."""
    try:
        local = json.loads((Path(db_path).parent / "config.local.json").read_text())
        if "isp_owns_router" in local:
            return bool(local["isp_owns_router"])
    except Exception:
        pass
    try:
        cfg = json.loads((ROOT / "config.json").read_text())
        return bool(cfg.get("isp_owns_router", False))
    except Exception:
        return False


def identity_box(enabled, info, tzname="local"):
    """HTML snippet for the report. Always honest about missing data."""
    if not enabled:
        return ("<div class=box><b>Network identity: not recorded</b><br>"
                "<span class=small>ISP lookup is turned OFF in Settings, so no public IP / "
                "provider details were collected. Turn it on if you want them in future reports.</span></div>")
    det = (info or {}).get("details") or {}
    if not info.get("public_ip"):
        return ("<div class=box><b>Network identity: not yet collected</b><br>"
                "<span class=small>The monitor records this automatically after ~10 minutes online. "
                "Regenerate this report later.</span></div>")
    rows = [("Public IP", info.get("public_ip", "—")),
            ("Provider (ISP)", det.get("isp", "—")),
            ("Organisation", det.get("org", "—")),
            ("Network ID (ASN)", det.get("as", "—")),
            ("Area", " ".join(x for x in (det.get("city", ""), det.get("regionName", ""),
                                          det.get("country", "")) if x) or "—"),
            ("Reverse DNS", det.get("reverse", "—")),
            ("Home IP / router / DNS",
             "%s · %s · %s" % (info.get("local_ip", "—"), info.get("gateway", "—"),
                               ", ".join(info.get("dns", []) or ["—"])))]
    try:
        ts = TF.fmt(float(info.get("updated_ts", 0)), "%b %d, %H:%M", tzname)
    except Exception:
        ts = "unknown time"
    lis = "".join("<tr><td>%s</td><td>%s</td></tr>" % r for r in rows)
    return ("<div class=box><b>Network identity (as observed %s)</b>"
            "<table>%s</table>"
            "<span class=small>Provider/location names are as reported by the lookup service, "
            "recorded automatically while monitoring.</span></div>" % (ts, lis))


def dir_size_mb(d):
    try:
        return sum(p.stat().st_size for p in Path(d).glob("report-*") if p.is_file()) / (1024 * 1024)
    except Exception:
        return 0.0


def list_reports(out_dir):
    """Newest first. Only report-* files (never touch anything else)."""
    try:
        files = [p for p in Path(out_dir).glob("report-*") if p.is_file() and REPORT_RE.match(p.name)]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return [{"name": p.name, "bytes": p.stat().st_size,
                 "mtime": p.stat().st_mtime} for p in files]
    except Exception:
        return []


def prune_reports(out_dir, keep_days=REPORT_RETAIN_DAYS,
                  keep_count=REPORT_RETAIN_COUNT, max_mb=REPORT_MAX_MB):
    """Enforce all three hard limits. Returns {deleted: [...], kept: n}."""
    deleted = []
    try:
        out = Path(out_dir)
        if not out.is_dir():
            return {"deleted": deleted, "kept": 0}
        now = time.time()
        reps = list_reports(out)
        for r in reps:  # 1) age limit
            if now - r["mtime"] > keep_days * 86400:
                try:
                    (out / r["name"]).unlink()
                    deleted.append(r["name"])
                except Exception:
                    pass
        reps = [r for r in list_reports(out) if r["name"] not in deleted]
        for r in sorted(reps, key=lambda r: r["mtime"])[:max(0, len(reps) - keep_count)]:  # 2) count
            try:
                (out / r["name"]).unlink()
                deleted.append(r["name"])
            except Exception:
                pass
        while dir_size_mb(out) > max_mb:  # 3) size
            reps = sorted(list_reports(out), key=lambda r: r["mtime"])
            if not reps:
                break
            try:
                (out / reps[0]["name"]).unlink()
                deleted.append(reps[0]["name"])
            except Exception:
                break
        return {"deleted": deleted, "kept": len(list_reports(out))}
    except Exception:
        return {"deleted": deleted, "kept": 0}


def summarize(rows, beats, since, now, tzname="local"):
    """Pure function (unit-testable). rows: events, beats: heartbeat sums.

    Returns dict with check totals, confirmed drops vs brief blips, daily pace.
    A drop only counts as CONFIRMED at >= CONFIRM_SEC; shorter = lost packets.
    """
    dur = lambda s, e: max(0, (e or now) - max(s, since))
    span = lambda s, e: (max(s, since), e or now)
    isp_conf = isp_blip = loc_conf = loc_blip = 0
    isp_iv, loc_iv, off_iv = [], [], []
    isp_long = loc_long = 0.0
    drops = []  # (start, end, kind, secs, confirmed)
    for s, e, t, ssid, det in rows:
        d = dur(s, e)
        if t == "OFFLINE_ISP":
            if d >= CONFIRM_SEC:
                isp_conf += 1
                isp_iv.append(span(s, e))
                isp_long = max(isp_long, d)
                drops.append((s, e or now, "ISP", d, True))
            else:
                isp_blip += 1
                drops.append((s, e or now, "ISP-blip", d, False))
        elif t == "LOCAL_FAIL":
            if d >= CONFIRM_SEC:
                loc_conf += 1
                loc_iv.append(span(s, e))
                loc_long = max(loc_long, d)
                drops.append((s, e or now, "router", d, True))
            else:
                loc_blip += 1
                drops.append((s, e or now, "router-blip", d, False))
        elif t in ("WIFI_OFF", "NO_LINK"):
            off_iv.append(span(s, e))
    drops.sort()
    from .db import union_seconds as _union
    isp_sec, loc_sec, wifioff_sec = _union(isp_iv), _union(loc_iv), _union(off_iv)
    # daily pace
    days = []
    d = TF.day_floor(since, tzname)
    last = TF.day_floor(now, tzname)
    while d <= last:
        nxt = d + 86400
        dd = [x for x in drops if x[0] < nxt and (x[1] or now) > d]
        hb = beats.get(d, {})
        days.append({"day": d,
                     "checks": hb.get("checks", 0),
                     "up": hb.get("up", 0),
                     "down_isp": hb.get("down", 0),
                     "down_loc": hb.get("loc", 0),
                     "drops": sum(1 for x in dd if x[4])})
        d = nxt
    tot_checks = sum(v.get("checks", 0) for v in beats.values())
    tot_up = sum(v.get("up", 0) for v in beats.values())
    return {
        "checks": tot_checks, "up_sec": tot_up,
        "isp_conf": isp_conf, "isp_blip": isp_blip, "isp_sec": round(isp_sec),
        "isp_long": round(isp_long),
        "loc_conf": loc_conf, "loc_blip": loc_blip, "loc_sec": round(loc_sec),
        "loc_long": round(loc_long),
        "wifioff_sec": round(wifioff_sec),
        "drops": drops, "days": days,
    }


def letter_kind(s):
    if s["isp_conf"] > 0:
        return "isp"
    if s["loc_conf"] > 0:
        return "local"
    if s["isp_blip"] + s["loc_blip"] > 0:
        return "blips"
    return "clear"


def generate_report(db_path, out_dir, days=7):
    """Build CSV + HTML pair, then auto-prune. Returns (csv, html, summary, prune_info)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    now = time.time()
    since = now - days * 86400
    TZ = TF.load_tz_name(str(Path(db_path).parent), ROOT)
    tag = TF.fmt(now, "%Y-%m-%d", TZ)

    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        rows = conn.execute(
            "SELECT start_ts,end_ts,type,ssid,detail FROM events "
            "WHERE start_ts>=? OR end_ts IS NULL ORDER BY start_ts LIMIT 10000", (since,)).fetchall()
        beats = {}
        for m, u, dwn, lo, w, c in conn.execute(
                "SELECT minute_ts,up_sec,down_sec,local_fail_sec,wifi_off_sec,checks "
                "FROM heartbeats WHERE minute_ts>=?", (int(since),)).fetchall():
            dk = TF.day_floor(m, TZ)
            prev = beats.get(dk, {"up": 0, "down": 0, "loc": 0, "checks": 0})
            beats[dk] = {"up": prev["up"] + (u or 0), "down": prev["down"] + (dwn or 0),
                         "loc": prev["loc"] + (lo or 0), "checks": prev["checks"] + (c or 0)}
    finally:
        conn.close()

    s = summarize(rows, beats, since, now, TZ)
    kind = letter_kind(s)
    wall = load_router_flag(str(db_path))
    router_word = "provider's router" if wall else "your router"
    window_str = "%s → %s (last %d day%s)" % (
        TF.fmt(since, "%b %d, %H:%M", TZ),
        TF.fmt(now, "%b %d, %H:%M", TZ),
        days, "" if days == 1 else "s")
    enabled, info = load_identity(str(db_path))
    ident = identity_box(enabled, info, TZ)

    csv_p = out / ("report-%s-%dd.csv" % (tag, days))
    with open(csv_p, "w", newline="") as f:
        f.write("# network identity: public_ip=%s | isp=%s | asn=%s | area=%s\n" % (
            info.get("public_ip", "not-recorded") if enabled else "lookup-off",
            (info.get("details") or {}).get("isp", "-") if enabled else "-",
            (info.get("details") or {}).get("as", "-") if enabled else "-",
            " ".join(x for x in ((info.get("details") or {}).get("city", ""),
                                 (info.get("details") or {}).get("country", "")) if x) or "-"))
        f.write("# rows below are connectivity events; lines starting with # are notes, safe to delete\n")
        w = csv.writer(f)
        w.writerow(["start", "end", "duration_sec", "what", "wifi", "detail", "meaning"])
        for st, e, t, ssid, det in rows:
            e = e or now
            w.writerow([TF.iso(st, TZ),
                        TF.iso(e, TZ),
                        int(e - st), t, ssid or "", (det or "")[:200],
                        PLAIN.get(t, t)])

    drows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%d</td></tr>" % (
            TF.fmt(x["day"], "%b %d", TZ),
            ("%d" % x["checks"]) if x["checks"] else "no data",
            _fmt(x["up"]) if x["checks"] else "—",
            _fmt(x["down_isp"]) if x["checks"] else "—",
            _fmt(x["down_loc"]) if x["checks"] else "—", x["drops"])
        for x in s["days"])
    xrows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            TF.fmt(a, "%m-%d %H:%M:%S", TZ),
            TF.fmt(b, "%H:%M:%S", TZ),
            _fmt(c), {"ISP": "internet", "router": "router",
                      "ISP-blip": "momentary", "router-blip": "momentary"}[k],
            "CONFIRMED" if cf else "blip (<3s)")
        for a, b, k, c, cf in s["drops"][-200:])
    if not xrows:
        xrows = "<tr><td colspan=5>No broken periods at all in this window.</td></tr>"

    if kind == "isp":
        letter = ("Dear support team,<br>My connection failed repeatedly while my own equipment "
                  "stayed up. Below: every confirmed internet-side drop with exact start, end and "
                  "duration, plus the raw log attached. My router was reachable throughout the red "
                  "periods, so please investigate your line — not my equipment.")
    elif kind == "local" and wall:
        letter = ("Dear support team,<br>Your router in my home stopped answering repeatedly "
                  "(orange periods below: exact starts, ends and durations). Since the router "
                  "is your equipment, please check or replace the unit. Raw log attached.")
    elif kind == "local":
        letter = ("Note before sending: this report shows <b>no internet-side failures</b>. "
                  "The drops happened between this computer and its own router (orange periods). "
                  "Please restart the router, check WiFi signal and cables first — this is currently "
                  "not evidence against the provider. Keep the monitor running; if red provider drops "
                  "appear, regenerate and send that.")
    elif kind == "blips":
        letter = ("Note: only momentary blips under 3 seconds were recorded — single lost packets, "
                  "which are normal on WiFi and prove nothing either way. Keep the monitor running; "
                  "send a report once confirmed drops appear.")
    else:
        letter = ("Good news: zero broken periods in this window. Attached log proves continuous "
                  "uptime, which is useful if you were billed for an outage-free period.")

    html = """<!DOCTYPE html><html><head><meta charset=utf-8><title>Internet outage report</title>
<style>body{font-family:Arial,sans-serif;max-width:820px;margin:32px auto;padding:0 20px}
table{width:100%%;border-collapse:collapse;font-size:13px}td,th{border:1px solid #ddd;padding:6px;text-align:left}
h1{font-size:24px}.box{background:#f6f6f6;padding:14px;border-radius:8px;margin:10px 0}
.red{border-left:6px solid #dc2626}.orange{border-left:6px solid #f59e0b}
.small{color:#555;font-size:12px}</style></head><body>
<h1>Internet report — last %d days (%s)</h1>
<p class=small>All times %s.</p>
<p>%s</p>
%s
<div class="box red"><b>Internet: %d confirmed outage(s) · %s total broken · longest %s</b><br>
<span class=small>Counts only drops ≥3s while your WiFi/router were up. Shorter blips: %d (listed, not counted).</span></div>
<div class="box orange"><b>Router: %d confirmed drop(s) · %s total · longest %s (%s)</b><br>
<span class=small>%s Blips: %d.</span></div>
<div class=box><b>From begin to end — %s:<br>%s checks (one per second) · %s working · %s confirmed broken (provider) · %s router-side · %s WiFi-off (excluded)</b></div>
<h2>Broken-seconds pace, day by day</h2>
<table><tr><th>Date</th><th>Checks</th><th>Working</th><th>Broken (provider)</th><th>Router-side</th><th>Confirmed drops</th></tr>%s</table>
<h2>Every broken period (exact times)</h2>
<table><tr><th>Start</th><th>End</th><th>Lasted</th><th>Whose fault</th><th>Status</th></tr>%s</table>
<p>Full raw log attached: %s (open in Excel; “meaning” column explains each row in plain words).</p>
<p class=small>How measured: one check per second against 1.1.1.1 + 8.8.8.8 + 9.9.9.9 plus an HTTPS check; "
a drop is CONFIRMED only after repeated failures (≥3s), so lone lost packets never blame anyone. "
Precisions are approximate to ~1 second. WiFi-off, asleep and monitor-off periods are logged separately "
and excluded from every total above. Provider/location names, where shown, are as reported by the lookup service.</p>
<p><button onclick=window.print()>Print / Save as PDF</button></p></body></html>""" % (
        days, tag, TF.label(TZ), letter, ident,
        s["isp_conf"], _fmt(s["isp_sec"]), _fmt(s["isp_long"]), s["isp_blip"],
        s["loc_conf"], _fmt(s["loc_sec"]), _fmt(s["loc_long"]), router_word,
        ("Your provider's router stopped answering — report it, don't replace it."
         if wall else
         "Your own router unreachable. Check router/cables — not the provider's fault."), s["loc_blip"],
        window_str, ("%d" % s["checks"]), _fmt(s["up_sec"]), _fmt(s["isp_sec"]),
        _fmt(s["loc_sec"]), _fmt(s["wifioff_sec"]),
        drows, xrows, csv_p.name)

    html_p = out / ("report-%s-%dd.html" % (tag, days))
    html_p.write_text(html)
    pruned = prune_reports(out)
    return str(csv_p), str(html_p), s, pruned


def main():
    ap = argparse.ArgumentParser(description="Export ISP-proof report")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--db", default=str(DD / "uptime.db"))
    ap.add_argument("--out", default=str(DD / "reports"))
    ap.add_argument("--clean", action="store_true", help="only prune old reports, generate nothing")
    a = ap.parse_args()
    if a.clean:
        print(prune_reports(a.out))
        return
    csv_p, html_p, summ, pruned = generate_report(a.db, a.out, a.days)
    print("wrote %s and %s (pruned %d)" % (csv_p, html_p, len(pruned["deleted"])))
    print("checks=%d up=%s isp_broken=%s(%d) router=%s(%d)" % (
        summ["checks"], _fmt(summ["up_sec"]), _fmt(summ["isp_sec"]),
        summ["isp_conf"], _fmt(summ["loc_sec"]), summ["loc_conf"]))


if __name__ == "__main__":
    main()
