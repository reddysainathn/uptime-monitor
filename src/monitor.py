"""UptimeMonitor daemon. Stdlib only. Python 3.9+ safe. macOS/Linux/Windows.

Tiny + crash-safe design:
- 1s fast path: ping 3 targets + TCP 443 in parallel (reused pool)
- 5s slow path: wifi/ssid/ethernet (OS calls are expensive, so cached)
- EVENTS written immediately (never lose outage proof)
- HEARTBEATS buffered in memory, flushed 1x/min (60x less disk I/O)
- GAP recovery: if killed/rebooted, next start inserts honest gray GAP, not fake uptime
- Never exits on network error; OS restarts it if killed (launchd/systemd/scheduler)

Run:  python3 -m src.monitor            # foreground (launchd/systemd use this)
       python3 -m src.monitor --once    # single sample, for testing
Bookmark dashboard: http://127.0.0.1:8899
"""
import argparse
import atexit
import json
import logging
import re
import signal
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import db as dbmod
from . import netcheck
from .dashboard_gen import generate as gen_dashboard

ROOT = Path(__file__).resolve().parent.parent
STOP = False


def load_config():
    from .paths import data_dir, resolve_db_path
    dd = data_dir()
    defaults = {
        "check_interval_sec": 1.0, "fail_threshold": 2, "success_threshold": 2,
        "ping_timeout_sec": 0.8, "tcp_timeout_sec": 2.0,
        "ping_targets": ["1.1.1.1", "8.8.8.8", "9.9.9.9"],
        "tcp_check": {"host": "1.1.1.1", "port": 443},
        "sleep_gap_sec": 5.0, "dashboard_port": 8899,
        "dashboard_regen_sec": 10, "auto_open_after_sec": 30,
        "auto_open_cooldown_sec": 600, "auto_open_enabled": True,
        "isp_details_enabled": True, "isp_owns_router": False,
        "timezone": "local",
        "report_retain_days": 30, "report_retain_count": 20, "report_max_mb": 5.0,
        # runtime state lives OUTSIDE the repo (synced folders lock sqlite/logs)
        "db_path": str(dd / "uptime.db"), "log_path": str(dd / "monitor.log"),
        "dashboard_dir": str(dd / "dashboard"), "reports_dir": str(dd / "reports"),
    }
    try:
        cfg = json.loads((ROOT / "config.json").read_text())
    except Exception:
        cfg = {}
    for k, v in cfg.items():
        if k in ("db_path", "log_path", "dashboard_dir", "reports_dir"):
            continue  # paths are auto-probed; override via UPTIME_DB env if needed
        defaults[k] = v
    try:
        # UI-saved safe settings live beside the DB (repo folder may be read-only/synced)
        local = json.loads((Path(defaults["db_path"]).parent / "config.local.json").read_text())
        for k in SAFE_CONFIG:
            if k in local:
                defaults[k] = local[k]
    except Exception:
        pass
    try:
        defaults["db_path"] = resolve_db_path(cfg.get("db_path", ""))
    except Exception:
        pass
    return defaults


def setup_log(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=1)  # cap ~2MB
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log = logging.getLogger("uptime")
    log.setLevel(logging.INFO)
    log.handlers = [h]
    return log


def recover_gap(conn, log):
    """Insert honest GAP if app was dead (kill/reboot) between last write and now."""
    try:
        r1 = conn.execute("SELECT MAX(start_ts), MAX(end_ts) FROM events").fetchone()
        r2 = conn.execute("SELECT MAX(minute_ts) FROM heartbeats").fetchone()
        last = 0.0
        if r1:
            for v in r1:
                if isinstance(v, (int, float)) and v and v > last:
                    last = float(v)
        if r2 and r2[0]:
            last = max(last, float(r2[0]) + 60)  # heartbeat covers that minute
        now = time.time()
        if last and 120 < now - last < 30 * 86400:
            conn.execute("UPDATE events SET end_ts=? WHERE end_ts IS NULL", (last,))
            conn.execute(
                "INSERT INTO events (start_ts,end_ts,type,ssid,detail) VALUES (?,?,?,?,?)",
                (last, now, "GAP", "", "app was not running (reboot/kill)"),
            )
            conn.commit()
            log.info("recovered GAP %.0fs", now - last)
        else:
            conn.execute("UPDATE events SET end_ts=? WHERE end_ts IS NULL", (now,))
            conn.commit()
    except Exception as e:
        log.warning("recover_gap failed: %s", e)


def classify(link, gw_ok, lat_ok):
    """Pure verdict for one sample. Unit-testable. Never blames without proof.

    link: {wifi_on, ethernet_up} with None = unknown.
    Returns one of: WIFI_OFF | NO_LINK | LOCAL_DOWN | INET_DOWN | INET_UP.
    - Known down (wifi off + no ethernet) -> WIFI_OFF.
    - Unknown link + everything unreachable -> NO_LINK ("no usable
      connection"): WiFi-off, asleep-waking and reconnecting all look
      identical here, so we report the symptom, not a guessed cause.
      Both are gray, excluded, never an accusation.
    - LOCAL_DOWN only when the link is positively up AND gateway + internet
      both fail after retries.
    """
    wifi_on = link.get("wifi_on")
    eth_up = link.get("ethernet_up")
    link_up = bool(wifi_on) or bool(eth_up)
    link_down = (wifi_on is False and not eth_up)
    if lat_ok:
        return "INET_UP"
    if link_down:
        return "WIFI_OFF"
    if not link_up:
        return "NO_LINK"  # unknown link: describe, don't diagnose
    if not gw_ok:
        return "LOCAL_DOWN"
    return "INET_DOWN"


class Monitor:
    def __init__(self, cfg, log, record=True):
        self.cfg = cfg
        self.log = log
        self.record = record  # False for --once: probe only, write nothing (no junk)
        self.conn = dbmod.connect(cfg["db_path"])
        self.state = "ONLINE"
        self.fail_n = 0
        self.ok_n = 0
        self.ssid = ""
        self.link = {"wifi_on": None, "ssid": "", "ethernet_up": None}
        self.last_tick = time.time()
        self.last_slow = 0.0
        self.last_regen = 0.0
        self.last_flush = time.time()
        self.buf = {}  # minute_ts -> [up,down,local,wifioff,checks,lat_sum]
        self.notified_outage_id = None
        self.last_open_ts = 0.0
        self.last_prune = time.time()
        self.ipinfo = {}
        self.ip_future = None
        self.last_ip_check = 0.0
        self._ip_recheck = True  # check once shortly after startup, then daily + on reconnect
        self.traffic_base = None  # first counters sample -> session usage
        self.traffic_last = {}
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="probe")
        from .speedtest import Runner as _SpeedRunner
        self.speed = _SpeedRunner(Path(cfg["db_path"]).parent)
        if self.record:
            dbmod.open_event(self.conn, "ONLINE", "", "monitor start")
        self.log.info("monitor start, db=%s", cfg["db_path"])

    def set_state(self, new, detail=""):
        if new == self.state:
            return
        self.log.info("state %s -> %s %s", self.state, new, detail)
        self.state = new
        self.fail_n = 0
        self.ok_n = 0
        if self.record:
            dbmod.open_event(self.conn, new, self.ssid, detail)

    def bump(self, bucket, latency=0):
        if not self.record:
            return
        m = int(time.time() // 60) * 60
        a = self.buf.setdefault(m, [0, 0, 0, 0, 0, 0.0])
        idx = {"up": 0, "down": 1, "local": 2, "wifioff": 3}.get(bucket, 0)
        a[idx] += 1
        a[4] += 1
        a[5] += latency or 0
        if len(self.buf) > 5000:  # hard cap: disk stuck for days -> drop oldest, keep RAM flat
            for k in sorted(self.buf)[:len(self.buf) - 5000]:
                del self.buf[k]

    def flush(self):
        if not self.buf:
            return
        try:
            dbmod.flush_bulk(self.conn, self.buf)
            self.buf.clear()
            self.last_flush = time.time()
        except Exception as e:
            self.log.warning("flush failed: %s", e)

    def sample(self):
        """One 1s sample. All network calls bounded; never raises."""
        try:
            now = time.time()
            # sleep/wake: clock jumped while we were frozen
            if now - self.last_tick > self.cfg["sleep_gap_sec"] + 1.5:
                gap = round(now - self.last_tick)
                if self.record:
                    self.conn.execute(
                        "INSERT INTO events (start_ts,end_ts,type,ssid,detail) VALUES (?,?,?,?,?)",
                        (self.last_tick, now, "SLEEP", self.ssid, "system sleep/wake %ss" % gap),
                    )
                    self.conn.commit()
                    dbmod.open_event(self.conn, self.state, self.ssid, "resume after sleep")
                self.fail_n = self.ok_n = 0
                self.log.info("sleep gap %ss", gap)
            self.last_tick = now

            # slow path every 5s (expensive OS calls). On failure keep the
            # previous reading: a failed query during wake proves nothing.
            if now - self.last_slow > 5:
                self.last_slow = now
                try:
                    fresh = netcheck.get_link_info()
                    if fresh.get("wifi_on") is not None or fresh.get("ethernet_up") is not None:
                        self.link = fresh
                    if fresh.get("ssid"):
                        self.ssid = fresh["ssid"]
                    elif self.link.get("ssid"):
                        self.ssid = self.link["ssid"]
                except Exception:
                    pass
                self._slow_traffic()
                self._slow_ipinfo(now)

            wifi_on = self.link.get("wifi_on")
            eth_up = self.link.get("ethernet_up")
            if wifi_on is False and not eth_up:
                self.set_state("WIFI_OFF", "wifi off, ethernet down (user)")
                self.bump("wifioff")
                self.fail_n = 0
                self.ok_n = 0
                return {"state": self.state, "latency": None}

            gw = netcheck.get_gateway()
            # Honesty rule: ONE lost ping proves nothing (normal on WiFi).
            # Only blame the router after a retry fails AND the internet is
            # also unreachable. If the internet answers, a lost gateway ping
            # is just noise — stay ONLINE.
            gw_ok = True
            if gw:
                gw_ok = (netcheck.ping(gw, 0.6) is not None
                         or netcheck.ping(gw, 0.6) is not None)

            # fast path: internet = ANY target answers
            futs = [self.pool.submit(netcheck.ping, t, self.cfg["ping_timeout_sec"])
                    for t in self.cfg["ping_targets"]]
            tcp = self.cfg.get("tcp_check", {})
            if tcp.get("host"):
                futs.append(self.pool.submit(
                    netcheck.tcp_check, tcp["host"], tcp.get("port", 443),
                    self.cfg.get("tcp_timeout_sec", 2.0)))
            lat = None
            for f in futs:
                try:
                    r = f.result(timeout=self.cfg["ping_timeout_sec"] + 2)
                    if r is not None and (lat is None or r < lat):
                        lat = r
                except Exception:
                    pass

            if lat is not None:
                self.ok_n += 1
                self.fail_n = 0
                if self.state in ("OFFLINE_ISP", "LOCAL_FAIL") and self.ok_n >= self.cfg["success_threshold"]:
                    self.set_state("ONLINE", "recovered")
                    self._ip_recheck = True  # reconnects often bring a new IP — re-check soon
                elif self.state in ("WIFI_OFF", "NO_LINK"):
                    self.set_state("ONLINE", "link back")
                self.bump("up", lat)
            else:
                self.fail_n += 1
                self.ok_n = 0
                detail = "all of %s + tcp failed" % ",".join(self.cfg["ping_targets"])
                v = classify(self.link, gw_ok, False)
                if v in ("LOCAL_DOWN", "INET_DOWN"):
                    # WiFi can look "on" while holding no usable address
                    # (failed DHCP lease, no route). That is a local
                    # no-connection — never a provider outage.
                    try:
                        if not netcheck.usable_addr(netcheck.get_local_ip()):
                            v = "NO_LINK"
                    except Exception:
                        pass
                if v in ("WIFI_OFF", "NO_LINK"):
                    # Gray time, never an accusation. WIFI_OFF = confirmed off;
                    # NO_LINK = nothing usable (off, waking, or reconnecting).
                    self.set_state(v, "wifi off" if v == "WIFI_OFF"
                                   else "no usable link (wifi off, waking, or reconnecting)")
                    self.bump("wifioff")
                    self.fail_n = 0
                elif self.fail_n >= self.cfg["fail_threshold"] and self.state not in ("OFFLINE_ISP", "LOCAL_FAIL"):
                    if v == "LOCAL_DOWN":
                        self.set_state("LOCAL_FAIL",
                                       "link up, but gateway and internet both unreachable")
                    else:
                        self.set_state("OFFLINE_ISP", detail + ("" if gw else " (no gateway found)"))
                if self.state in ("OFFLINE_ISP", "LOCAL_FAIL"):
                    self.bump("down" if self.state == "OFFLINE_ISP" else "local")
                    if self.record and self.cfg.get("auto_open_enabled", True):
                        self.maybe_notify()
                elif self.state != "WIFI_OFF" and self.state != "NO_LINK":
                    self.bump("up")  # below threshold: single lost packets, not an outage
            return {"state": self.state, "latency": lat}
        except Exception as e:
            self.log.warning("sample error: %s", e)
            return {"state": self.state, "latency": None}

    def _slow_traffic(self):
        """Sample interface counters every 5s. One tiny file, overwritten (no bloat)."""
        try:
            cur = netcheck.get_traffic()
            if self.traffic_base is None:
                self.traffic_base = cur
            self.traffic_last = cur
            try:
                medium = netcheck.active_medium(self.link, netcheck.get_default_iface())
            except Exception:
                medium = "Unknown"
            snap = {"ts": time.time(), "medium": medium,
                    "ssid": self.ssid, "base": self.traffic_base, "cur": cur}
            dd = str(Path(self.cfg["db_path"]).parent)
            tmp = str(Path(dd) / "traffic.json.tmp")
            with open(tmp, "w") as f:
                json.dump(snap, f)
            try:
                import os
                os.replace(tmp, str(Path(dd) / "traffic.json"))
            except Exception:
                pass
        except Exception:
            pass

    def _slow_ipinfo(self, now):
        """Public IP daily + right after every reconnect (that's when IPs change).

        Runs in the worker pool so the 1s loop never waits on the internet
        to describe the internet. Disabled entirely by isp_details_enabled=false.
        Details (ISP/ASN) re-fetch only when the IP actually changed — they're
        static per IP, so scheduled re-fetching would be pointless traffic.
        """
        try:
            if not self.cfg.get("isp_details_enabled", True):
                return
            if self.state != "ONLINE":
                return
            if self.ip_future is not None:
                if self.ip_future.done():
                    try:
                        self.ipinfo = self.ip_future.result(timeout=1) or self.ipinfo
                    except Exception:
                        pass
                    self.ip_future = None
                else:
                    return  # previous lookup still running -> skip, never pile up
            due_daily = now - self.last_ip_check >= 86400
            if not (due_daily or self._ip_recheck):
                return
            self.last_ip_check = now
            self._ip_recheck = False
            prev = dict(self.ipinfo)
            self.ip_future = self.pool.submit(self._ipinfo_worker, prev)
        except Exception:
            pass

    def _ipinfo_worker(self, prev):
        """Blocking lookups live here (worker thread, not the 1s loop)."""
        try:
            info = {"updated_ts": time.time()}
            info["local_ip"] = netcheck.get_local_ip() or ""
            info["gateway"] = netcheck.get_gateway() or ""
            info["dns"] = netcheck.get_dns_servers()
            pub = netcheck.get_public_ip()
            if not pub:
                return prev or info  # offline mid-lookup: keep old data, don't blank it
            info["public_ip"] = pub
            old = prev.get("details", {})
            if pub != prev.get("public_ip") or not old:
                # New IP (or never successfully looked up): fetch details once.
                # Details are static per IP — no scheduled re-fetching.
                det = netcheck.get_isp_details(pub)
                if det:
                    info["details"] = det
                    info["details_ts"] = time.time()
                else:
                    info["details"] = old
                    info["details_ts"] = prev.get("details_ts", 0)
            else:
                info["details"] = old
                info["details_ts"] = prev.get("details_ts", 0)
            try:
                dd = str(Path(self.cfg["db_path"]).parent)
                tmp = str(Path(dd) / "ipinfo.json.tmp")
                with open(tmp, "w") as f:
                    json.dump(info, f)
                import os
                os.replace(tmp, str(Path(dd) / "ipinfo.json"))
            except Exception:
                pass
            return info
        except Exception:
            return prev

    def maybe_notify(self):
        """Open the dashboard rarely and only for real provider outages.

        Rules (after the 40-tabs incident):
        - OFFLINE_ISP only. Router-side flaps never pop tabs (you're home —
          the timeline already shows them, and flapping would spam).
        - Max 2 opens per rolling hour + the configured cooldown.
        - Same tab when the browser allows it (new=0).
        """
        try:
            if self.state != "OFFLINE_ISP":
                return
            row = self.conn.execute(
                "SELECT id,start_ts FROM events WHERE end_ts IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return
            oid, start = row[0], row[1]
            if oid == self.notified_outage_id:
                return  # same outage already opened -> stay silent
            now = time.time()
            if now - start < self.cfg["auto_open_after_sec"]:
                return
            if now - self.last_open_ts < self.cfg.get("auto_open_cooldown_sec", 600):
                self.notified_outage_id = oid  # skip quietly, don't queue a late open
                return
            self._open_times = [t for t in getattr(self, "_open_times", []) if now - t < 3600]
            if len(self._open_times) >= 2:
                self.notified_outage_id = oid  # hourly budget spent -> stay silent
                return
            link_ok = (self.link.get("wifi_on") is not False) or self.link.get("ethernet_up")
            if self.link.get("wifi_on") is None and self.link.get("ethernet_up") is None:
                link_ok = True  # unknown OS -> local page still works offline
            if link_ok:
                url = "http://127.0.0.1:%d" % self.cfg["dashboard_port"]
                try:
                    webbrowser.open(url, new=0)  # same tab when possible
                except TypeError:
                    webbrowser.open(url)
                self.log.info("auto-opened dashboard for outage %s", oid)
                self.last_open_ts = now
                self._open_times.append(now)
            self.notified_outage_id = oid
        except Exception as e:
            self.log.warning("notify failed: %s", e)

    def close(self):
        try:
            self.flush()
            dbmod.close_open_event(self.conn)
            self.pool.shutdown(wait=False)
            self.conn.close()
        except Exception:
            pass


# Safe-for-UI settings: (min, max) for numbers, None for booleans.
# Anything NOT listed here can only be changed by editing config.json by hand.
# Dangerous keys (intervals, thresholds, ports, paths) are deliberately excluded:
# a bad threshold would silently break outage detection.
SAFE_CONFIG = {
    "auto_open_enabled": None,
    "auto_open_after_sec": (10, 300),
    "auto_open_cooldown_sec": (60, 3600),
    "isp_details_enabled": None,
    "isp_owns_router": None,
    "timezone": "tz",
    "report_retain_days": (1, 365),
    "report_retain_count": (1, 100),
    "report_max_mb": (1, 100),
    "dashboard_regen_sec": (5, 120),
}


def validate_config(patch):
    """Returns (clean_dict, errors). Never raises."""
    clean, errors = {}, []
    try:
        for k, v in (patch or {}).items():
            if k not in SAFE_CONFIG:
                errors.append("not editable from UI: %s" % k)
                continue
            rule = SAFE_CONFIG[k]
            if rule is None:
                if isinstance(v, bool):
                    clean[k] = v
                else:
                    errors.append("%s must be true/false" % k)
            elif rule == "tz":
                from . import timefmt as _tf
                if isinstance(v, str) and _tf.valid_tz(v.strip() or "local"):
                    clean[k] = v.strip() or "local"
                else:
                    errors.append("unknown timezone — try America/Chicago or blank for computer time")
            else:
                try:
                    num = float(v)
                except Exception:
                    errors.append("%s must be a number" % k)
                    continue
                lo, hi = rule
                if not (lo <= num <= hi):
                    errors.append("%s must be %s..%s" % (k, lo, hi))
                    continue
                clean[k] = int(num) if k != "report_max_mb" else round(num, 1)
    except Exception:
        pass
    return clean, errors


def _rep():
    """Fresh report module (picks up report fixes without restarting)."""
    import importlib
    import src.report as _r
    try:
        importlib.reload(_r)
    except Exception:
        pass
    return _r


class APIHandler(SimpleHTTPRequestHandler):
    """Localhost-only UI backend. Serves dashboard + JSON API for reports/config."""
    mon = None
    log = logging.getLogger("uptime")

    def log_message(self, *a):
        pass  # keep logs clean; app events go to monitor.log

    def _json(self, obj, code=200):
        try:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def _report_name_ok(self, name):
        from .report import REPORT_RE
        return bool(REPORT_RE.match(name or "")) and "/" not in name and name == Path(name).name

    def do_GET(self):
        try:
            u = urlparse(self.path)
            if u.path == "/api/status":
                m = self.mon
                try:
                    dbmb = dbmod.size_mb(m.cfg["db_path"])
                except Exception:
                    dbmb = 0
                try:
                    from .report import list_reports, dir_size_mb
                    reps = list_reports(m.cfg["reports_dir"])
                    rep_mb = dir_size_mb(m.cfg["reports_dir"])
                except Exception:
                    reps, rep_mb = [], 0
                try:
                    logb = Path(m.cfg["log_path"]).stat().st_size if Path(m.cfg["log_path"]).exists() else 0
                except Exception:
                    logb = 0
                try:
                    med = netcheck.active_medium(m.link, netcheck.get_default_iface())
                except Exception:
                    med = "Unknown"
                try:
                    open_ev = dbmod.current_open(m.conn)
                    since = open_ev[1] if open_ev else time.time()
                except Exception:
                    since = time.time()
                return self._json({
                    "state": m.state, "ssid": m.ssid, "since": since,
                    "medium": med,
                    "wifi_on": m.link.get("wifi_on"), "ethernet_up": m.link.get("ethernet_up"),
                    "ipinfo": m.ipinfo, "traffic": {"base": m.traffic_base, "cur": m.traffic_last},
                    "db_mb": round(dbmb, 2), "reports": len(reps), "reports_mb": round(rep_mb, 2),
                    "log_kb": round(logb / 1024, 1),
                })
            if u.path == "/api/reports":
                from .report import list_reports
                return self._json({"reports": list_reports(self.mon.cfg["reports_dir"])})
            if u.path == "/api/config":
                return self._json({k: self.mon.cfg.get(k) for k in SAFE_CONFIG})
            if u.path == "/api/speedtest":
                try:
                    return self._json(self.mon.speed.snapshot())
                except Exception:
                    return self._json({"phase": "unavailable"}, 500)
            if u.path.startswith("/reports/"):
                name = u.path[len("/reports/"):]
                if not self._report_name_ok(name):
                    return self._json({"error": "bad filename"}, 400)
                p = Path(self.mon.cfg["reports_dir"]) / name
                if not p.is_file():
                    return self._json({"error": "not found"}, 404)
                ctype = "text/html" if name.endswith(".html") else "text/csv"
                try:
                    data = p.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception:
                    pass
                return
            return SimpleHTTPRequestHandler.do_GET(self)
        except Exception:
            pass

    def do_POST(self):
        try:
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/api/report":
                try:
                    days = max(1, min(90, int(q.get("days", ["7"])[0])))
                except Exception:
                    days = 7
                m = self.mon
                csv_p, html_p, _summ, pruned = _rep().generate_report(
                    m.cfg["db_path"], m.cfg["reports_dir"], days)
                return self._json({"csv": Path(csv_p).name, "html": Path(html_p).name,
                                   "pruned": pruned["deleted"]})
            if u.path == "/api/speedtest":
                if self.mon.speed.start():
                    return self._json({"started": True})
                return self._json({"error": "a test is already running"}, 409)
            if u.path == "/api/reports/clean":
                m = self.mon
                res = _rep().prune_reports(m.cfg["reports_dir"], m.cfg.get("report_retain_days", 30),
                                           m.cfg.get("report_retain_count", 20),
                                           m.cfg.get("report_max_mb", 5.0))
                return self._json(res)
            if u.path == "/api/config":
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    patch = json.loads(self.rfile.read(max(0, n)).decode() or "{}")
                except Exception:
                    return self._json({"error": "bad JSON"}, 400)
                clean, errors = validate_config(patch)
                m = self.mon
                m.cfg.update(clean)
                try:
                    lp = Path(m.cfg["db_path"]).parent / "config.local.json"
                    disk = {}
                    try:
                        disk = json.loads(lp.read_text())
                    except Exception:
                        pass
                    disk.update(clean)
                    lp.write_text(json.dumps(disk, indent=2))
                except Exception as e:
                    return self._json({"saved": clean, "persist_warning": str(e), "errors": errors})
                return self._json({"saved": clean, "errors": errors})
            return self._json({"error": "unknown endpoint"}, 404)
        except Exception:
            pass

    def do_DELETE(self):
        try:
            u = urlparse(self.path)
            if u.path.startswith("/api/reports/"):
                name = u.path[len("/api/reports/"):]
                if not self._report_name_ok(name):
                    return self._json({"error": "bad filename"}, 400)
                p = Path(self.mon.cfg["reports_dir"]) / name
                try:
                    p.unlink()
                    return self._json({"deleted": name})
                except FileNotFoundError:
                    return self._json({"error": "not found"}, 404)
                except Exception:
                    return self._json({"error": "cannot delete"}, 500)
            return self._json({"error": "unknown endpoint"}, 404)
        except Exception:
            pass


def serve_forever(mon, log):
    d = Path(mon.cfg["dashboard_dir"])
    d.mkdir(parents=True, exist_ok=True)
    APIHandler.mon = mon
    APIHandler.log = log
    try:
        handler = partial(APIHandler, directory=str(d))
        srv = ThreadingHTTPServer(("127.0.0.1", mon.cfg["dashboard_port"]), handler)
        log.info("dashboard http://127.0.0.1:%d (%s)", mon.cfg["dashboard_port"], d)
        srv.serve_forever()
    except Exception as e:
        log.warning("serve failed: %s", e)


def _single_instance(dd, log):
    """Pidfile guard: a second copy exits instead of double-writing the DB
    and double-popping tabs. Stale files (dead pid) are taken over safely."""
    import os
    pf = Path(dd) / "monitor.pid"
    try:
        old = int(pf.read_text().strip())
        os.kill(old, 0)  # alive?
        if old != os.getpid():
            log.info("another monitor (pid %d) is already running — exiting", old)
            return False
    except Exception:
        pass
    try:
        pf.write_text(str(os.getpid()))
    except Exception:
        pass
    return True


def main():
    ap = argparse.ArgumentParser(description="Tiny cross-platform internet uptime monitor")
    ap.add_argument("--once", action="store_true", help="single sample then exit")
    ap.add_argument("--forget", action="store_true",
                    help="reclassify a user-confirmed wifi-off window (needs --from/--to)")
    ap.add_argument("--from", dest="from_", default="",
                    help="'YYYY-MM-DD HH:MM' start of the window (your timezone setting)")
    ap.add_argument("--to", dest="to_", default="", help="same format, end of the window")
    ap.add_argument("--types", default="",
                    help="only touch these event types, e.g. LOCAL_FAIL (default: all)")
    args = ap.parse_args()
    cfg = load_config()
    log = setup_log(cfg["log_path"])
    Path(cfg["dashboard_dir"]).mkdir(parents=True, exist_ok=True)
    Path(cfg["reports_dir"]).mkdir(parents=True, exist_ok=True)
    log.info("data dir: %s", str(Path(cfg['db_path']).parent))

    if args.forget:
        import shutil
        from . import timefmt as _tf
        tzname = cfg.get("timezone", "local") or "local"
        try:
            s_ts = _tf.parse_local(args.from_, tzname)
            e_ts = _tf.parse_local(args.to_, tzname)
        except ValueError as e:
            print("bad date: %s" % e)
            return
        if not (e_ts > s_ts):
            print("end must be after start")
            return
        dd = Path(cfg["db_path"]).parent
        try:  # refuse while the writer runs, unless explicitly forced
            import os as _os
            live = int((dd / "monitor.pid").read_text().strip())
            _os.kill(live, 0)
            print("monitor (pid %d) is still running — stop it first "
                  "(sh scripts/uptime-monitor off)" % live)
            return
        except Exception:
            pass
        bak = dd / ("uptime.db.bak-" + time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(cfg["db_path"], bak)
        olds = sorted(dd.glob("uptime.db.bak-*"))[:-3]  # hard cap: keep 3 backups
        for o in olds:
            try:
                o.unlink()
            except Exception:
                pass
        res = dbmod.forget_range(
            cfg["db_path"], s_ts, e_ts,
            "user-confirmed: wifi off + machine asleep; misdetected states reclassified on request",
            [t.strip() for t in args.types.split(",") if t.strip()] or None)
        print("backup: %s" % bak)
        print("deleted %d events, inserted 1 WIFI_OFF covering %s → %s (%s)" % (
            res["deleted"], args.from_, args.to_, tzname))
        return

    if not args.once and not _single_instance(str(Path(cfg["db_path"]).parent), log):
        print("another monitor is already running (see monitor.pid) — exiting")
        return

    conn0 = dbmod.connect(cfg["db_path"])
    recover_gap(conn0, log)
    dbmod.prune(cfg["db_path"], conn0, log)
    conn0.close()

    mon = Monitor(cfg, log, record=not args.once)

    def _stop(signum, frame):
        global STOP
        STOP = True

    for s in ("SIGTERM", "SIGINT"):
        try:
            signal.signal(getattr(signal, s), _stop)
        except Exception:
            pass
    atexit.register(mon.close)  # flushed on TERM/INT (not on kill -9; GAP covers that)

    if args.once:
        print(json.dumps(mon.sample(), indent=2))
        mon.close()
        return

    t = threading.Thread(target=serve_forever, args=(mon, log), daemon=True)
    t.start()

    while not STOP:
        t0 = time.monotonic()
        mon.sample()
        if time.time() - mon.last_regen > mon.cfg.get("dashboard_regen_sec", 10):
            mon.last_regen = time.time()
            try:
                import importlib
                import src.dashboard_gen as _dg
                try:
                    importlib.reload(_dg)  # template tweaks apply without restarting
                except Exception:
                    pass
                _dg.generate(mon.cfg["db_path"], str(Path(mon.cfg["dashboard_dir"]) / "index.html"),
                             mon.cfg["reports_dir"], mon.cfg["db_path"])
            except Exception as e:
                log.warning("dashboard gen failed: %s", e)
        if time.time() - mon.last_flush > 60:
            mon.flush()
        if time.time() - mon.last_prune > 86400:
            mon.last_prune = time.time()
            try:
                dbmod.prune(mon.cfg["db_path"], mon.conn, log)
                _rep().prune_reports(mon.cfg["reports_dir"], mon.cfg.get("report_retain_days", 30),
                                     mon.cfg.get("report_retain_count", 20),
                                     mon.cfg.get("report_max_mb", 5.0))
            except Exception as e:
                log.warning("daily prune failed: %s", e)
        dt = cfg["check_interval_sec"] - (time.monotonic() - t0)
        if dt > 0:
            time.sleep(dt)
    mon.close()


if __name__ == "__main__":
    main()
