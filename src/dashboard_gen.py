"""Dashboard generator. Stdlib only, Python 3.9+. No JS framework, no CDN.

Reads SQLite + traffic.json + ipinfo.json -> one static dashboard/index.html.
Works offline (localhost keeps working during an outage — that's the point).
Hero status updates live via JS (/api/status every 5s); charts refresh with
the page (scroll position is preserved across auto-refresh).
"""
import json
import sqlite3
import time
from pathlib import Path

from . import db as dbmod
from . import timefmt as TF

ROOT = Path(__file__).resolve().parent.parent

COLORS = {
    "ONLINE": "#16a34a",
    "OFFLINE_ISP": "#dc2626",
    "LOCAL_FAIL": "#f59e0b",
    "WIFI_OFF": "#9ca3af",
    "NO_LINK": "#9ca3af",
    "SLEEP": "#9ca3af",
    "GAP": "#9ca3af",
}
WORDS = {
    "ONLINE": "Online",
    "OFFLINE_ISP": "Internet",
    "LOCAL_FAIL": "Router",
    "WIFI_OFF": "WiFi off",
    "NO_LINK": "No connection",
    "SLEEP": "Asleep",
    "GAP": "Monitor off",
}

HERO_ADVICE = {
    "ONLINE": "Everything is working. Leave this running — it checks every second.",
    "OFFLINE_ISP": "Internet is DOWN on your provider's side (your router is fine). Generate a report below and send it to them.",
    "LOCAL_FAIL": "Your router isn't answering, so your internet is down at home. Usual suspects: brief WiFi interference, the router needing a restart, or too many devices on it.",
    "WIFI_OFF": "WiFi is off (or no cable plugged in). This time is NOT counted against anyone — turn WiFi on to resume checks.",
    "NO_LINK": "No usable connection right now — WiFi may be off, waking, or reconnecting. Turning WiFi off and back on often fixes it (renews the connection). Gray time, NOT counted against anyone.",
    "SLEEP": "Your computer was asleep. Checks resume automatically.",
    "GAP": "The monitor wasn't running during this gap, so that time is marked unknown — never counted as anyone's fault.",
}


def _fmt_dur(sec):
    sec = int(sec or 0)
    if sec < 60:
        return "%ds" % sec
    m, s = divmod(sec, 60)
    if m < 60:
        return "%dm %ds" % (m, s)
    h, m = divmod(m, 60)
    if h < 48:
        return "%dh %dm" % (h, m)
    return "%dd %dh" % (h // 24, h % 24)


def _fmt_bytes(n):
    try:
        n = float(n or 0)
    except Exception:
        return "-"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return ("%.1f %s" % (n, u)) if u != "B" else ("%d B" % n)
        n /= 1024


def _stat_card(conn, title, days, wall=False, tzname="local"):
    now_ = time.time()
    s = dbmod.stats_since(conn, now_ - days * 86400)
    total = s["outages"]
    color = "#16a34a" if total == 0 else "#dc2626"
    if total == 0:
        big, chips = "All clear", "<span class=chip>no drops at all</span>"
    else:
        big = "%d outage%s" % (total, "" if total == 1 else "s")
        parts = []
        if s["outages_isp"]:
            parts.append("<span class='chip red'>%d internet</span>" % s["outages_isp"])
        if s["outages_local"]:
            parts.append("<span class='chip orange'>%d router</span>" % s["outages_local"])
        chips = " ".join(parts)
    if days < 1:
        win = "%s → %s" % (TF.fmt(now_ - days * 86400, "%b %d, %H:%M", tzname),
                           TF.fmt(now_, "%H:%M", tzname))
    else:
        win = "%s → %s" % (TF.fmt(now_ - days * 86400, "%b %d", tzname),
                           TF.fmt(now_, "%b %d", tzname))
    brokend = s["down_sec"] + s["local_sec"]
    return (
        "<div class=card><div class=ctitle>%s · %s</div>"
        "<div class=bignum style='color:%s'>%s</div>"
        "<div class=sub>%s</div>"
        "<div class=sub>down <b style='color:%s'>%s</b> · <b>%s%%</b> up</div></div>"
    ) % (title, win, color, big, chips,
         "#dc2626" if brokend else "#16a34a", _fmt_dur(brokend), s["uptime_pct"])


def _load_json(p):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return {}


def _esc_attr(s):
    """Escape a value for an HTML attribute (the eye-toggle stores the real value here)."""
    try:
        return str(s).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    except Exception:
        return ""


def _router_flag(dd):
    """True if the router/modem is the ISP's own box (e.g. built into the wall).
    Personal setting: data-dir override wins, repo config is the default."""
    try:
        local = _load_json(str(Path(dd) / "config.local.json"))
        if "isp_owns_router" in local:
            return bool(local["isp_owns_router"])
    except Exception:
        pass
    try:
        cfg = _load_json(str(Path(__file__).resolve().parent.parent / "config.json"))
        return bool(cfg.get("isp_owns_router", False))
    except Exception:
        return False


def _verdict(conn, isp_owns_router=False):
    s = dbmod.stats_since(conn, time.time() - 7 * 86400)
    if s["outages"] == 0:
        return ("All clear — no drops in the last 7 days.",
                "If pages still feel slow, it's speed (not drops) — run a speed test while it's bad.",
                "ok")
    if s["outages_isp"] > 0:
        return (
            "%d outage%s · %s total downtime in 7 days — while your WiFi/router stayed on." % (
                s["outages_isp"], "" if s["outages_isp"] == 1 else "s", _fmt_dur(s["down_sec"])),
            "That's a provider problem. Hit “Report: last 7 days” below and email them both files.",
            "bad")
    if isp_owns_router:
        return (
            "No internet-side failures — but your provider's router stopped answering for %s in 7 days." % _fmt_dur(s["local_sec"]),
            "This is still their ticket: generate the 7-day report and send it.",
            "warn")
    return (
        "No internet-side failures — but your router stopped answering for %s in 7 days." % _fmt_dur(s["local_sec"]),
        "Restart your router and check cables before calling the provider.",
        "warn")


def generate(db_path, out_html, reports_dir=None, _db_path_ignored=None):
    dd = str(Path(db_path).parent)
    if reports_dir is None:
        reports_dir = str(Path(dd) / "reports")
    traffic = _load_json(str(Path(dd) / "traffic.json"))
    ipinfo = _load_json(str(Path(dd) / "ipinfo.json"))
    try:
        from .report import list_reports
        reps = list_reports(reports_dir)[:20]
    except Exception:
        reps = []

    conn = sqlite3.connect(db_path, timeout=10)
    try:
        now = time.time()
        cur = conn.execute(
            "SELECT type, start_ts FROM events WHERE end_ts IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        cur_type, cur_since = (cur[0], cur[1]) if cur else ("ONLINE", now)
        color = COLORS.get(cur_type, "#555")
        word = WORDS.get(cur_type, cur_type)
        advice = HERO_ADVICE.get(cur_type, "")
        OWN = _router_flag(dd)
        orange_txt = ("Router — your provider's box (it didn't answer)"
                      if OWN else
                      "Router — your own box (it didn't answer)")
        legend_h = ("<div class=legend><span><i style=background:#16a34a></i>Working</span>"
                    "<span><i style=background:#dc2626></i>Internet — provider's side failed</span>"
                    "<span><i style=background:#f59e0b></i>" + orange_txt + "</span>"
                    "<span><i style=background:#9ca3af></i>WiFi off / asleep / monitor off (nobody's fault)</span>"
                    "</div>")
        if cur_type == "LOCAL_FAIL" and OWN:
            advice = ("Your provider's router isn't answering, so your internet is down at their box. "
                      "Don't try to fix or replace it — report it.")
        v_title, v_sub, v_tone = _verdict(conn, OWN)
        TZ = TF.load_tz_name(dd, ROOT)
        try:
            midnight = TF.day_floor(now, TZ)
            today_days = max((now - midnight) / 86400, 1 / 86400)
        except Exception:
            today_days = 1

        # connection rows
        medium = traffic.get("medium") or "Unknown"
        ssid = traffic.get("ssid") or ""
        base, cur_t = traffic.get("base") or {}, traffic.get("cur") or {}
        rx_sess = tx_sess = rx_tot = tx_tot = 0
        for iface, vals in (cur_t or {}).items():
            try:
                rx, tx = vals[0], vals[1]
                rx_tot += rx
                tx_tot += tx
                b = (base or {}).get(iface)
                if b:
                    rx_sess += max(0, rx - b[0])
                    tx_sess += max(0, tx - b[1])
            except Exception:
                continue
        det = ipinfo.get("details") or {}
        loc = " ".join(x for x in (det.get("city", ""), det.get("regionName", ""), det.get("country", "")) if x)
        crows = [
            ("Using", medium + ((" · network “%s”" % ssid) if ssid and "WiFi" in medium else ""), True),
            ("Downloaded", "%s · %s" % (_fmt_bytes(rx_tot), _fmt_bytes(rx_sess)), False),
            ("Uploaded", "%s · %s" % (_fmt_bytes(tx_tot), _fmt_bytes(tx_sess)), False),
            ("Your public IP", ipinfo.get("public_ip", "—looking up…—"), True),
            ("Provider", det.get("isp", "—looking up…—"), True),
            ("Network ID (ASN)", det.get("as", "—"), True),
            ("Area", loc or "—", True),
            ("Home IP / router / DNS",
             "%s · %s · %s" % (ipinfo.get("local_ip", "—"), ipinfo.get("gateway", "—"),
                               ", ".join(ipinfo.get("dns", []) or ["—"])), True),
        ]

        def _privcell(val, sensitive):
            if sensitive:
                return "<b><span class=priv data-v=\"%s\">%s</span></b>" % (_esc_attr(val), val)
            return "<b>%s</b>" % val

        crows_h = "".join("<div class=krow><span>%s</span>%s</div>" % (k, _privcell(v, s)) for k, v, s in crows)
        try:
            from .speedtest import last_result as _last_sp
            _sp = _last_sp(dd)
            if _sp:
                lastspeed = "Last test %s: ↓%s ↑%s Mbps · ping %s ms" % (
                    TF.fmt(_sp.get("when", 0), "%b %d, %H:%M", TZ),
                    _sp.get("down_mbps", "?"), _sp.get("up_mbps", "?"), _sp.get("ping_ms", "?"))
            else:
                lastspeed = "No speed test yet."
        except Exception:
            lastspeed = ""

        # timeline: last 24h in 10-min blocks (144, roomy). Worst state in the
        # block wins the color, so even a seconds-long outage stays visible.
        pri = {"OFFLINE_ISP": 0, "LOCAL_FAIL": 1, "WIFI_OFF": 2, "NO_LINK": 2, "SLEEP": 2, "GAP": 2}
        blocks, labels = [], []
        NB, STEP = 144, 600
        for i in range(NB):
            t0 = now - (NB - i) * STEP
            grows = conn.execute(
                """SELECT type, SUM(COALESCE(end_ts,?) - MAX(start_ts,?))
                   FROM events WHERE start_ts < ? + ? AND COALESCE(end_ts,?) > ?
                   GROUP BY type""",
                (now, t0, t0, STEP, now, t0)).fetchall()
            dom, best = "ONLINE", 3
            for t, dur in grows:
                p = pri.get(t, 3)
                if p < best:
                    best, dom = p, t
            blocks.append("<i style=background:%s title='%s'></i>" % (
                COLORS.get(dom, "#16a34a"),
                TF.fmt(t0, "%H:%M", TZ) + " " + WORDS.get(dom, dom)))
            hr = TF.fmt(t0, "%H", TZ)
            labels.append("<span>%s</span>" % (
                TF.fmt(t0, "%H:00", TZ)
                if hr.isdigit() and int(hr) % 3 == 0 and i % 18 == 0 else ""))

        # heatmap: outages (provider + router-side) per day, last 30 days (zone days)
        heats = []
        today0 = TF.day_floor(now, TZ)
        for d in range(29, -1, -1):
            day0 = today0 - d * 86400
            n = conn.execute(
                "SELECT COUNT(*) FROM events WHERE type IN ('OFFLINE_ISP','LOCAL_FAIL') "
                "AND start_ts>=? AND start_ts<?",
                (day0, day0 + 86400)).fetchone()[0]
            alpha = min(1.0, 0.15 + n * 0.2) if n else 0
            bg = ("rgba(220,38,38,%.2f)" % alpha) if n else "#eef2f7"
            heats.append(
                "<div class=hcell><i style=background:%s title='%s: %d outage%s'></i>"
                "<span>%s</span></div>" % (
                    bg, TF.fmt(day0, "%b %d", TZ), n,
                    "" if n == 1 else "s", TF.fmt(day0, "%d", TZ)))

        # events: newest first, human words + color dots
        rows = conn.execute(
            "SELECT start_ts,end_ts,type,ssid,detail FROM events "
            "ORDER BY start_ts DESC LIMIT 30").fetchall()
        trs = []
        for s, e, t, ssd, det_ in rows:
            e = e or now
            trs.append(
                "<tr><td><span class=dot style=background:%s></span>%s</td>"
                "<td>%s</td><td>%s</td><td>%s</td></tr>" % (
                    COLORS.get(t, "#555"), WORDS.get(t, t),
                    TF.fmt(s, "%b %d, %H:%M:%S", TZ),
                    _fmt_dur(e - s), ((det_ or "")[:100] or "—")))

        # reports rows
        rtrs = []
        for r in reps:
            rtrs.append(
                "<tr><td>%s</td><td>%s</td><td>%s</td><td class=actions>"
                "<a class=mini href='/reports/%s'>Open</a>"
                "<a class='mini danger' href='#' onclick=\"delRep('%s');return false\">Delete</a>"
                "</td></tr>" % (
                    r["name"],
                    TF.fmt(r["mtime"], "%b %d, %H:%M", TZ),
                    _fmt_bytes(r["bytes"]), r["name"], r["name"]))
        if not rtrs:
            rtrs.append("<tr><td colspan=4 class=empty>Nothing here yet — press a button above after an outage and the files appear here.</td></tr>")

        try:
            db_kb = Path(db_path).stat().st_size / 1024
        except Exception:
            db_kb = 0

        html = """<!DOCTYPE html><html><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>Internet Uptime — is my internet working?</title><style>
:root{--green:#16a34a;--red:#dc2626;--orange:#f59e0b;--gray:#9ca3af;--ink:#111827;--mut:#6b7280;--line:#e5e7eb;--bg:#f8fafc}
*{box-sizing:border-box}body{font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif;max-width:960px;margin:0 auto;padding:20px 16px 60px;color:var(--ink);background:var(--bg);overflow-x:hidden}
.top{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px}
.top h1{font-size:22px;margin:0}.live{font-size:13px;color:var(--mut)}.live b{color:var(--green)}
.hero{margin:14px 0;border-radius:14px;padding:10px 16px;color:#fff;background:%s;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.hero .hlive{font-size:12px;opacity:.9;letter-spacing:.04em;white-space:nowrap}
.hero .hstate{font-size:22px;font-weight:800;white-space:nowrap}
.hero .hfor{font-size:14px;opacity:.92}
.advline{margin:-6px 0 10px;font-size:13.5px;color:#374151;background:#fff;border:1px solid var(--line);border-radius:10px;padding:8px 12px}
.verdictline{margin:0 0 14px;font-size:13.5px;color:#374151;border-left:6px solid var(--gray);background:#fff;border-top:1px solid var(--line);border-right:1px solid var(--line);border-bottom:1px solid var(--line);border-radius:0 10px 10px 0;padding:8px 12px}
.verdictline.ok{border-left-color:var(--green);background:#f0fdf4}
.verdictline.bad{border-left-color:var(--red);background:#fef2f2}
.verdictline.warn{border-left-color:var(--orange);background:#fffbeb}
.verdictline b{color:var(--ink)}
.sec{background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px;margin:14px 0;overflow:hidden}
.sec h2{margin:0 0 2px;font-size:17px}.sec .why{margin:0 0 12px;color:var(--mut);font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}
.card{border:1px solid var(--line);border-radius:12px;padding:14px;background:#fff}
.ctitle{font-size:13px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.bignum{font-size:26px;font-weight:800;margin:2px 0}.statline{display:flex;justify-content:space-between;margin-top:8px;font-size:14px}
.sub{color:var(--mut);font-size:12.5px}
.tl{display:flex;gap:1px;overflow:hidden}.tl i{flex:1 1 0;min-width:0;height:30px;border-radius:2px}
.tlabels{display:flex;gap:1px;margin-top:2px;overflow:hidden}.tlabels span{flex:1 1 0;min-width:0;font-size:10px;color:var(--mut);white-space:nowrap;overflow:hidden}
.legend{display:flex;gap:12px;flex-wrap:wrap;margin-top:10px;font-size:12.5px;color:#374151}
.legend i{width:11px;height:11px;border-radius:3px;display:inline-block;margin-right:4px;vertical-align:baseline}
.hm{display:flex;gap:4px;overflow:hidden}.hcell{flex:1 1 0;min-width:0}.hcell i{display:block;height:36px;border-radius:5px}.hcell span{font-size:9.5px;color:var(--mut)}
.krow{display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-bottom:1px solid #f1f5f9;font-size:14px}
.krow span{color:var(--mut)}.krow b{text-align:right;font-weight:600}
table{width:100%%;border-collapse:collapse;font-size:13px}td,th{border-bottom:1px solid #f1f5f9;padding:7px 6px;text-align:left}
th{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
.dot{width:10px;height:10px;border-radius:50%%;display:inline-block;margin-right:7px}
.chip{display:inline-block;padding:1px 9px;border-radius:999px;font-size:12px;font-weight:700;background:#f1f5f9;color:#475569}
.chip.red{background:#fee2e2;color:#b91c1c}.chip.orange{background:#ffedd5;color:#c2410c}
.btn{display:inline-block;margin:6px 8px 0 0;padding:10px 16px;border-radius:10px;background:#111827;color:#fff;text-decoration:none;border:0;cursor:pointer;font-size:14px;font-weight:600}
.btn2{background:#fff;color:#111827;border:1px solid #cbd5e1}
.mini{display:inline-block;padding:4px 10px;border:1px solid #cbd5e1;border-radius:7px;text-decoration:none;color:#111827;font-size:12.5px;margin-right:6px;background:#fff}
.mini.danger{color:var(--red);border-color:#fecaca}
td.actions{white-space:nowrap}.empty{color:var(--mut);font-style:italic}
details.sec summary{cursor:pointer;font-size:17px;font-weight:700}details.sec summary::-webkit-details-marker{color:var(--mut)}
input{padding:7px;border:1px solid #cbd5e1;border-radius:8px;margin:2px;font-size:14px}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;align-items:center;justify-content:center;z-index:50}
.overlay.open{display:flex}
.modal{background:#fff;border-radius:14px;padding:22px;max-width:420px;width:92%%;box-shadow:0 10px 40px rgba(0,0,0,.25)}
.modal h2{margin:0 0 6px}.bar{height:12px;background:#eef2f7;border-radius:6px;overflow:hidden;margin:10px 0}
.bar i{display:block;height:100%%;background:var(--green);width:0}
.big3{font-size:30px;font-weight:800}
.setrow{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:6px 0;font-size:14px}
.foot{color:var(--mut);font-size:12.5px;margin-top:14px;display:flex;gap:14px;flex-wrap:wrap}
.refreshbtn{display:inline-flex;align-items:center;gap:6px;margin-left:8px;padding:7px 16px;border-radius:999px;border:1.5px solid #16a34a;background:#f0fdf4;color:#15803d;font-size:13.5px;font-weight:700;cursor:pointer;white-space:nowrap}
.refreshbtn:hover{background:#16a34a;color:#fff}
.refreshbtn:disabled{opacity:.65;cursor:wait}
@media print{.btn,.btn2,.mini,details.settings,.noprint{display:none}body{background:#fff}}
</style></head><body>
<div class=top><h1>🏠 Internet Uptime</h1>
<div class=live><b id=livedot>● LIVE · %s</b> · updated %s · charts refresh every few seconds <button class=refreshbtn id=refreshBtn onclick="manualRefresh()" title="Refresh status now">⟳ Refresh now</button></div></div>

<div class=hero id=hero style='background:%s'><span class=hlive id=herolive>● LIVE</span><span class=hstate id=heroState>%s</span><span class=hfor id=heroFor>for %s · %s</span></div>
<div class=advline id=heroAdv>%s</div>

<div class="verdictline %s">📋 <b>7-day verdict:</b> %s — %s</div>

<details class=sec><summary>What counts as bad? (rules of thumb)</summary>
<p class=why>Your WiFi hop (computer → router): <b>zero multi-second stalls per day</b> is normal for healthy gear. A couple of 1-second blips a day is just WiFi noise — not proof of anything. Daily stalls of seconds, or any 10-minute outage, means faulty equipment or heavy interference.</p>
<p class=why>Internet line: providers typically promise <b>99.9%% uptime on their own network</b> — that allows ~43 minutes down per month, and it covers their line, never your WiFi. So: red internet drops totaling tens of minutes a month = complain with a report. A few lost seconds = normal everywhere.</p></details>

<div class=sec><h2>1 · How bad is it?</h2><p class=why>Outage = your internet was down. Internet (red) = provider's side failed. Router (orange) = the box at home didn't answer.</p>
<div class=grid>%s%s%s</div></div>

<div class=sec><h2>2 · When did it break?</h2><p class=why>Last 24 hours, each bar = 10 minutes (a seconds-long outage still lights its bar). Hover any bar for the exact time.</p>
<div class=tl>%s</div><div class=tlabels>%s</div>
%s

<div class=sec><h2>3 · Which days are worst?</h2><p class=why>Last 30 days. Darker red = more outages that day. Gray = clean.</p>
<div class=hm>%s</div></div>

<div class=sec><h2>4 · Your connection <button class=mini id=privBtn onclick="togglePriv()" title="Hide sensitive details">👁</button></h2><p class=why>What you're on right now and how much data has flowed through it. Left figure = since computer started · right = since monitor started.</p>
%s
<p class=noprint><button class=btn onclick="openSpeed()">Speed test</button> <span class=sub id=lastspeed>%s</span></p></div>

<div class=sec><h2>5 · Proof for your provider</h2><p class=why>One press builds the files. Email both to your provider — the letter explains itself.</p>
<p class=noprint><button class=btn onclick="mkRep(1)">Report: last day</button><button class=btn onclick="mkRep(7)">Report: last 7 days</button><button class=btn onclick="mkRep(30)">Report: last 30 days</button><button class="btn btn2" onclick="cleanReps()">Clean old reports</button></p>
<table><tr><th>File</th><th>Made</th><th>Size</th><th>Open / delete</th></tr>%s</table></div>

<details class=sec><summary>6 · What exactly happened (log)</summary>
<p class=why>Newest first. Gray rows are not anyone's fault and are never counted against your provider.</p>
<table><tr><th>What</th><th>Start</th><th>Lasted</th><th>Note</th></tr>%s</table></details>

<details class="sec settings"><summary>7 · Settings</summary><div id=setbox>loading…</div></details>

<div class=foot><span>Database %s KB (auto-capped at 10 MB)</span>
<span>All times %s</span>
<span><a href=# onclick="window.print();return false">Save page as PDF</a></span>
<span>Bookmark: <b>http://127.0.0.1:8899</b></span></div>

<script>
try{var sy=sessionStorage.getItem('sy');if(sy)window.scrollTo(0,+sy);}catch(e){}
window.addEventListener('beforeunload',function(){try{sessionStorage.setItem('sy',window.scrollY);}catch(e){}});
function heroColor(s){return s==='ONLINE'?'#16a34a':s==='OFFLINE_ISP'?'#dc2626':s==='LOCAL_FAIL'?'#f59e0b':'#6b7280';}
function heroWord(s){return s==='ONLINE'?'✓ Working':s==='OFFLINE_ISP'?'✗ Internet down':s==='LOCAL_FAIL'?'⚠ Router down':s==='WIFI_OFF'?'⏸ WiFi off':s==='NO_LINK'?'⏸ No connection':'…';}
function pollStatus(){fetch('/api/status').then(r=>r.json()).then(s=>{
 var h=document.getElementById('hero');if(!h)return;
 h.style.background=heroColor(s.state);
 document.getElementById('heroState').textContent=heroWord(s.state);
 var d=Math.max(0,Date.now()/1000-(s.since||Date.now()/1000));
 document.getElementById('heroFor').textContent='for '+fmtDur(d)+' · '+s.medium;
 document.getElementById('livedot').textContent='● LIVE · '+s.state;
}).catch(function(){});}setInterval(pollStatus,5000);
function manualRefresh(){location.reload();}
function fmtDur(s){s=Math.floor(s);if(s<60)return s+'s';var m=Math.floor(s/60);if(m<60)return m+'m '+(s%%60)+'s';var hh=Math.floor(m/60);return hh+'h '+(m%%60)+'m';}
function mkRep(d){fetch('/api/report?days='+d,{method:'POST'}).then(r=>r.json()).then(()=>location.reload());}
function togglePriv(){var h=true;try{h=localStorage.getItem('privHidden')!=='1';}catch(e){}privHide(h);}
function privHide(hide){try{localStorage.setItem('privHidden',hide?'1':'0');}catch(e){}
 document.querySelectorAll('.priv').forEach(function(el){el.textContent=hide?'••••••':el.getAttribute('data-v');});
 var b=document.getElementById('privBtn');if(b){b.textContent=hide?'🙈':'👁';b.title=hide?'Show sensitive details':'Hide sensitive details';}}
try{if(localStorage.getItem('privHidden')==='1')privHide(true);}catch(e){}
function cleanReps(){if(confirm('Delete old reports (keeps newest within limits)?'))fetch('/api/reports/clean',{method:'POST'}).then(r=>r.json()).then(()=>location.reload());}
function delRep(n){if(confirm('Delete '+n+'?'))fetch('/api/reports/'+n,{method:'DELETE'}).then(()=>location.reload());}
fetch('/api/config').then(r=>r.json()).then(c=>{
 let h='<div class=setrow><label><input type=checkbox id=s_open '+(c.auto_open_enabled?'checked':'')+'> Pop open this page when an outage passes</label>'
 +'<label><input id=s_after size=3 value="'+c.auto_open_after_sec+'"> seconds</label>'
 +'<label>don’t pop again for <input id=s_cool size=4 value="'+c.auto_open_cooldown_sec+'"> seconds</label></div>'
 +'<div class=setrow><label><input type=checkbox id=s_isp '+(c.isp_details_enabled?'checked':'')+'> Look up provider name &amp; network ID (small extra web request)</label>'
 +'<label><input type=checkbox id=s_own '+(c.isp_owns_router?'checked':'')+'> My router is from my provider</label></div>'
 +'<div class=setrow><label>refresh charts every <input id=s_reg size=3 value="'+c.dashboard_regen_sec+'"> seconds</label>'
 +'<label>timezone <input id=s_tz size=16 value="'+(c.timezone||'local')+'" placeholder="America/Chicago"></label></div>'
 +'<div class=setrow><span>Reports: keep at most</span><label><input id=s_rc size=3 value="'+c.report_retain_count+'"> files,</label>'
 +'<label><input id=s_rd size=3 value="'+c.report_retain_days+'"> days,</label>'
 +'<label><input id=s_rm size=3 value="'+c.report_max_mb+'"> MB</label>'
 +'<button class=btn onclick="saveSet()">Save</button> <span id=smsg></span></div>'
 +'<p class=sub>Speed of checks and detection strictness stay in config.json on purpose — one wrong number there would silently break detection.</p>';
 document.getElementById('setbox').innerHTML=h;});
function saveSet(){let p={auto_open_enabled:document.getElementById('s_open').checked,
 auto_open_after_sec:+document.getElementById('s_after').value,
 auto_open_cooldown_sec:+document.getElementById('s_cool').value,
 isp_details_enabled:document.getElementById('s_isp').checked,
 isp_owns_router:document.getElementById('s_own').checked,
 dashboard_regen_sec:+document.getElementById('s_reg').value,
 timezone:document.getElementById('s_tz').value||'local',
 report_retain_count:+document.getElementById('s_rc').value,
 report_retain_days:+document.getElementById('s_rd').value,
 report_max_mb:+document.getElementById('s_rm').value};
 fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)})
 .then(r=>r.json()).then(j=>{document.getElementById('smsg').textContent=j.errors&&j.errors.length?('Please check: '+j.errors.join('; ')):'Saved.';});}
setTimeout(function(){location.reload();},60000);
var _spT=null;
function openSpeed(){document.getElementById('spOverlay').classList.add('open');spPoll();}
function closeSpeed(){document.getElementById('spOverlay').classList.remove('open');if(_spT){clearInterval(_spT);_spT=null;}}
function startSpeed(){fetch('/api/speedtest',{method:'POST'}).then(r=>r.json()).then(j=>{
 if(j.error){document.getElementById('spMsg').textContent=j.error;return;}
 document.getElementById('spMsg').textContent='';spPoll();});}
function spPoll(){fetch('/api/speedtest').then(r=>r.json()).then(s=>{spShow(s);
 if(_spT)clearInterval(_spT);
 if(s.phase==='running')_spT=setInterval(function(){fetch('/api/speedtest').then(r=>r.json()).then(spShow);},1000);});}
function spShow(s){var bar=document.getElementById('spBar');if(bar)bar.style.width=(s.progress||0)+'%%';
 var st=document.getElementById('spStep');
 if(s.phase==='running'){st.textContent='Testing '+s.step+'… '+s.progress+'%%';}
 else if(s.phase==='done'&&s.result){var r=s.result;
  st.innerHTML='Done. <span class=big3>↓ '+r.down_mbps+'</span> Mbps down · <b>↑ '+(r.up_mbps==null?'n/a':r.up_mbps)+'</b> Mbps up<br>ping '+r.ping_ms+' ms · jitter '+r.jitter_ms+' ms · via '+r.server;}
 else if(s.phase==='error'){st.textContent='Failed: '+(s.error||'unknown');}
 else{st.textContent='Press Start. About 15 seconds, ~10 MB of data.';}}
</script>
<div class=overlay id=spOverlay><div class=modal><h2>Speed test</h2>
<div class=sub>True measurement: ping, then a real download and upload — about 15 seconds. Single connection, approximate. Uses ~10 MB (up to ~60 MB on very fast lines).</div>
<div class=bar><i id=spBar></i></div>
<div id=spStep>Press Start.</div>
<p><button class=btn onclick="startSpeed()">Start</button><button class="btn btn2" onclick="closeSpeed()">Close</button></p>
<p class=sub id=spMsg></p></div></div>
</body></html>""" % (
            color, cur_type, TF.fmt(now, "%H:%M:%S", TZ),
            color, word, _fmt_dur(now - cur_since), medium, advice,
            v_tone, v_title, v_sub,
            _stat_card(conn, "Today", today_days, OWN, TZ),
            _stat_card(conn, "Last 7 days", 7, OWN, TZ),
            _stat_card(conn, "Last 30 days", 30, OWN, TZ),
            "".join(blocks), "".join(labels), legend_h, "".join(heats),
            crows_h, lastspeed, "".join(rtrs), "".join(trs),
            ("%.0f" % db_kb), TF.label(TZ))
        with open(out_html, "w") as f:
            f.write(html)
    finally:
        conn.close()
