"""Smoke tests. Stdlib unittest only. Run: python3 -m unittest discover -s tests -v"""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path


class TestDB(unittest.TestCase):
    def test_events_and_bloat_guards(self):
        from src import db as dbmod
        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "t.db")
            conn = dbmod.connect(p)
            dbmod.open_event(conn, "ONLINE", "Home", "start")
            time.sleep(0.05)
            dbmod.open_event(conn, "OFFLINE_ISP", "Home", "fail")
            dbmod.close_open_event(conn)
            ev = dbmod.recent_events(conn, 10)
            self.assertEqual(len(ev), 2)
            # bulk flush + checkpoint + prune must not crash and must cap size
            buf = {int(time.time() // 60) * 60: [58, 2, 0, 0, 60, 1200.0]}
            dbmod.flush_bulk(conn, buf)
            dbmod.checkpoint(conn)
            dbmod.prune(p, conn)
            self.assertLess(dbmod.size_mb(p), 10)
            s = dbmod.stats_since(conn, time.time() - 86400)
            self.assertIn("uptime_pct", s)
            conn.close()

    def test_stats_ignores_wifioff(self):
        from src import db as dbmod
        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "t.db")
            conn = dbmod.connect(p)
            now = time.time()
            conn.execute(
                "INSERT INTO events (start_ts,end_ts,type) VALUES (?,?,?)",
                (now - 3600, now - 3500, "WIFI_OFF"))
            conn.execute(
                "INSERT INTO events (start_ts,end_ts,type) VALUES (?,?,?)",
                (now - 300, now - 240, "OFFLINE_ISP"))
            conn.commit()
            s = dbmod.stats_since(conn, now - 7200)
            self.assertEqual(s["outages"], 1)
            self.assertGreaterEqual(s["down_sec"], 50)
            conn.close()


class TestNetcheck(unittest.TestCase):
    def test_never_raises(self):
        from src import netcheck as nc
        self.assertIn("wifi_on", nc.get_link_info())
        # ping may be None offline — must not raise
        self.assertTrue(nc.ping("127.0.0.1", 0.5) is None or isinstance(nc.ping("127.0.0.1", 0.5), float))
        self.assertTrue(nc.get_gateway() is None or isinstance(nc.get_gateway(), str))

    def test_traffic_and_medium_never_raise(self):
        from src import netcheck as nc
        t = nc.get_traffic()
        self.assertIsInstance(t, dict)
        self.assertEqual(nc.active_medium({"wifi_on": False, "ssid": "", "ethernet_up": False}), "WiFi Off")
        self.assertIn("WiFi", nc.active_medium({"wifi_on": True, "ssid": "Home", "ethernet_up": False}))
        self.assertEqual(nc.active_medium({"wifi_on": False, "ssid": "", "ethernet_up": True}), "Ethernet")

    def test_ip_helpers_offline_safe(self):
        from src import netcheck as nc
        self.assertTrue(nc.get_local_ip() is None or isinstance(nc.get_local_ip(), str))
        self.assertIsInstance(nc.get_dns_servers(), list)
        self.assertIsInstance(nc.get_isp_details("0.0.0.0", timeout=2), dict)

    def test_dashboard_gen_runs_offline(self):
        import tempfile
        from src import db as dbmod
        from src.dashboard_gen import generate
        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "t.db")
            conn = dbmod.connect(p)
            dbmod.open_event(conn, "ONLINE")
            conn.close()
            out = str(Path(td) / "index.html")
            generate(p, out)
            html = Path(out).read_text()
            self.assertIn("Internet Uptime", html)
            self.assertIn("Bookmark", html)
            self.assertIn("Your connection", html)
            self.assertIn("Proof for your provider", html)
            self.assertIn("7-day verdict", html)
            self.assertIn("privBtn", html)
            self.assertIn("togglePriv", html)
            self.assertIn("class=priv", html)
            self.assertIn("pollStatus", html)
            self.assertIn("LIVE ·", html)


class TestReports(unittest.TestCase):
    def test_prune_enforces_all_limits(self):
        import os
        from src.report import prune_reports
        with tempfile.TemporaryDirectory() as td:
            # 25 reports: 10 old (>40d), 15 recent
            for i in range(25):
                p = Path(td) / ("report-2024-01-%02d-7d.csv" % (i + 1))
                p.write_text("x" * 1000)
                age = 50 * 86400 if i < 10 else (5 * 60 if i > 20 else 86400)
                os.utime(p, (time.time() - age, time.time() - age))
            res = prune_reports(td, keep_days=30, keep_count=20, max_mb=5.0)
            remaining = list(Path(td).glob("report-*"))
            self.assertLessEqual(len(remaining), 20)
            self.assertEqual(len(res["deleted"]), 25 - len(remaining))
            # non-report files untouched
            keep = Path(td) / "notes.txt"
            keep.write_text("hi")
            prune_reports(td)
            self.assertTrue(keep.exists())

    def test_prune_size_cap(self):
        from src.report import prune_reports
        with tempfile.TemporaryDirectory() as td:
            for i in range(5):
                (Path(td) / ("report-2026-09-%02d-7d.csv" % (i + 1))).write_text("x" * 2000)
            res = prune_reports(td, keep_days=30, keep_count=20, max_mb=0.005)
            import os
            total = sum(p.stat().st_size for p in Path(td).glob("report-*"))
            self.assertLessEqual(total, 0.005 * 1024 * 1024 + 2500)
            self.assertTrue(res["deleted"])

    def test_config_validation(self):
        from src.monitor import validate_config
        clean, errs = validate_config({"auto_open_after_sec": 45, "isp_details_enabled": False,
                                       "check_interval_sec": 0.1, "hack": 1})
        self.assertEqual(clean, {"auto_open_after_sec": 45, "isp_details_enabled": False})
        self.assertEqual(len(errs), 2)
        clean, errs = validate_config({"report_retain_count": 500})
        self.assertEqual(clean, {})
        self.assertTrue(errs)

    def test_union_never_double_counts(self):
        from src.db import union_seconds
        self.assertEqual(union_seconds([(0, 10), (5, 15), (20, 25)]), 20)
        self.assertEqual(union_seconds([(0, 10), (10, 20)]), 20)
        self.assertEqual(union_seconds([]), 0)

    def test_stats_overlap_safe(self):
        from src import db as dbmod
        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "t.db")
            conn = dbmod.connect(p)
            now = time.time()
            # two overlapping "writers" logged the same outage twice
            conn.execute("INSERT INTO events (start_ts,end_ts,type) VALUES (?,?,?)",
                         (now - 300, now - 200, "OFFLINE_ISP"))
            conn.execute("INSERT INTO events (start_ts,end_ts,type) VALUES (?,?,?)",
                         (now - 290, now - 200, "OFFLINE_ISP"))
            conn.commit()
            s = dbmod.stats_since(conn, now - 7200)
            self.assertEqual(s["down_sec"], 100)  # merged, not 190
            conn.close()

    def test_stats_counts_router_drops_as_outages(self):
        from src import db as dbmod
        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "t.db")
            conn = dbmod.connect(p)
            now = time.time()
            conn.execute("INSERT INTO events (start_ts,end_ts,type) VALUES (?,?,?)",
                         (now - 300, now - 240, "LOCAL_FAIL"))
            conn.execute("INSERT INTO events (start_ts,end_ts,type) VALUES (?,?,?)",
                         (now - 200, now - 100, "OFFLINE_ISP"))
            conn.commit()
            s = dbmod.stats_since(conn, now - 7200)
            self.assertEqual(s["outages"], 2)  # down is down: both count
            self.assertEqual(s["outages_isp"], 1)
            self.assertEqual(s["outages_local"], 1)
            self.assertEqual(s["local_sec"], 60)
            conn.close()

    def test_verdict_branches(self):
        from src import db as dbmod
        from src.dashboard_gen import _verdict
        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "t.db")
            conn = dbmod.connect(p)
            now = time.time()
            conn.execute("INSERT INTO events (start_ts,end_ts,type) VALUES (?,?,?)",
                         (now - 300, now - 240, "LOCAL_FAIL"))
            conn.commit()
            t, _sub, _tone = _verdict(conn, isp_owns_router=True)
            self.assertIn("provider's router", t)
            t2, _, _ = _verdict(conn, isp_owns_router=False)
            self.assertIn("your router", t2)
            conn.close()

    def test_timefmt_central(self):
        from src import timefmt as TF
        # 2026-09-18 12:00 UTC = 07:00 CDT (UTC-5)
        ts = 1789732800.0
        self.assertEqual(TF.fmt(ts, "%H:%M", "America/Chicago"), "07:00")
        self.assertEqual(TF.fmt(ts, "%H:%M", "local"), TF.fmt(ts, "%H:%M", ""))
        self.assertTrue(TF.valid_tz("America/Chicago"))
        self.assertTrue(TF.valid_tz("local"))
        self.assertFalse(TF.valid_tz("Mars/Olympus"))
        self.assertIn("-05:00", TF.iso(ts, "America/Chicago"))
        # day floor: 13:00 and 17:00 CDT are the same Central day
        self.assertEqual(TF.day_floor(ts + 6 * 3600, "America/Chicago"),
                         TF.day_floor(ts + 10 * 3600, "America/Chicago"))

    def test_config_tz_validation(self):
        from src.monitor import validate_config
        clean, errs = validate_config({"timezone": "America/Chicago"})
        self.assertEqual(clean, {"timezone": "America/Chicago"})
        clean, errs = validate_config({"timezone": "Narnia"})
        self.assertEqual(clean, {})
        self.assertTrue(errs)

    def test_usable_addr(self):
        from src.netcheck import usable_addr
        self.assertTrue(usable_addr("192.168.1.5"))
        self.assertTrue(usable_addr("10.0.0.2"))
        self.assertFalse(usable_addr(None))
        self.assertFalse(usable_addr(""))
        self.assertFalse(usable_addr("169.254.10.20"))  # self-assigned = hung DHCP
        self.assertFalse(usable_addr("127.0.0.1"))
        self.assertFalse(usable_addr("0.0.0.0"))
        self.assertFalse(usable_addr("not-an-ip"))

    def test_airport_parsing(self):
        from src.netcheck import _parse_airport
        on, ssid = _parse_airport("     agrCtlRSSI: -60\n     SSID: HomeNet\n     link auth: wpa2\n")
        self.assertEqual((on, ssid), (True, "HomeNet"))
        self.assertEqual(_parse_airport("AirPort: Off\n")[0], False)
        self.assertEqual(_parse_airport("garbage\n"), (None, ""))
        self.assertEqual(_parse_airport("")[0], None)

    def test_classify_never_blames_without_link(self):
        from src.monitor import classify
        up = {"wifi_on": True, "ethernet_up": None}
        down = {"wifi_on": False, "ethernet_up": False}
        unknown = {"wifi_on": None, "ethernet_up": None}
        # internet answering => always UP, whatever the flags say
        for link in (up, down, unknown):
            self.assertEqual(classify(link, False, True), "INET_UP")
        # all dead: known-down => WIFI_OFF; unknown => NO_LINK (never blame)
        self.assertEqual(classify(down, False, False), "WIFI_OFF")
        self.assertEqual(classify(unknown, False, False), "NO_LINK")
        self.assertEqual(classify(unknown, True, False), "NO_LINK")
        # LOCAL_DOWN only on positively-up link with gw + internet dead
        self.assertEqual(classify(up, False, False), "LOCAL_DOWN")
        self.assertEqual(classify({"wifi_on": None, "ethernet_up": True}, False, False), "LOCAL_DOWN")
        # gw fine + internet dead => provider side
        self.assertEqual(classify(up, True, False), "INET_DOWN")

    def test_forget_range(self):
        from src import db as dbmod
        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "t.db")
            conn = dbmod.connect(p)
            base = 2000000000.0
            conn.execute("INSERT INTO events (start_ts,end_ts,type) VALUES (?,?,?)",
                         (base, base + 60, "LOCAL_FAIL"))
            conn.execute("INSERT INTO events (start_ts,end_ts,type) VALUES (?,?,?)",
                         (base + 60, base + 120, "SLEEP"))
            conn.execute("INSERT INTO events (start_ts,end_ts,type) VALUES (?,?,?)",
                         (base + 120, base + 180, "ONLINE"))
            conn.execute("INSERT INTO events (start_ts,end_ts,type,ssid) VALUES (?,?,?,?)",
                         (base + 180, None, "ONLINE", "x"))  # open event: must survive
            conn.commit()
            conn.close()
            res = dbmod.forget_range(p, base, base + 180, "test note")
            self.assertEqual(res, {"deleted": 3, "inserted": 1})
            conn = dbmod.connect(p)
            rows = conn.execute("SELECT type,ssid FROM events ORDER BY start_ts").fetchall()
            kinds = [r[0] for r in rows]
            self.assertEqual(kinds, ["WIFI_OFF", "ONLINE"])
            conn.close()

    def test_forget_range_types_filter(self):
        from src import db as dbmod
        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "t.db")
            conn = dbmod.connect(p)
            base = 2000000000.0
            conn.execute("INSERT INTO events (start_ts,end_ts,type) VALUES (?,?,?)",
                         (base, base + 60, "LOCAL_FAIL"))
            conn.execute("INSERT INTO events (start_ts,end_ts,type) VALUES (?,?,?)",
                         (base + 60, base + 120, "SLEEP"))
            conn.commit()
            conn.close()
            res = dbmod.forget_range(p, base, base + 180, "x", ["LOCAL_FAIL"])
            self.assertEqual(res["deleted"], 1)
            conn = dbmod.connect(p)
            kinds = sorted(r[0] for r in conn.execute("SELECT type FROM events").fetchall())
            self.assertEqual(kinds, ["SLEEP", "WIFI_OFF"])
            conn.close()

    def test_parse_local(self):
        from src.timefmt import parse_local, fmt
        ts = parse_local("2026-09-18 21:00", "America/Chicago")
        self.assertEqual(fmt(ts, "%Y-%m-%d %H:%M", "America/Chicago"), "2026-09-18 21:00")
        with self.assertRaises(ValueError):
            parse_local("not a date", "America/Chicago")

    def test_single_instance_stale_takeover(self):
        import os
        from src.monitor import _single_instance
        import logging
        log = logging.getLogger("test")
        with tempfile.TemporaryDirectory() as td:
            Path(td, "monitor.pid").write_text("99999999")  # dead pid
            self.assertTrue(_single_instance(td, log))
            self.assertEqual(Path(td, "monitor.pid").read_text().strip(), str(os.getpid()))
            Path(td, "monitor.pid").write_text(str(os.getppid()))  # another live process
            self.assertFalse(_single_instance(td, log))  # live (other) => refuse


class TestSpeed(unittest.TestCase):
    def test_local_mirror_end_to_end(self):
        import threading
        import time as _t
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from src import netcheck as _nc
        import src.speedtest as st

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                import re as _re
                try:
                    m = _re.search(r"bytes=(\d+)", self.path)
                    n = int(m.group(1)) if m else 1000
                except Exception:
                    n = 1000
                body = b"x" * min(n, 5000000)
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                # Drain the full body: real speedtest clients stream
                # multi-MB bodies with precise send timing; replying early
                # would reset the client's connection mid-send.
                try:
                    ln = int(self.headers.get("Content-Length", "0") or 0)
                except Exception:
                    ln = 0
                left = min(ln, 100000000)
                while left > 0:
                    buf = self.rfile.read(min(left, 262144))
                    if not buf:
                        break
                    left -= len(buf)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old_dt, old_cap, old_up, old_ping = (
            st.DOWN_TIME_SEC, st.DOWN_CAP_BYTES, st.UP_BYTES, _nc.ping)
        st.DOWN_TIME_SEC, st.DOWN_CAP_BYTES, st.UP_BYTES = 2, 2000000, 100000
        _nc.ping = lambda host, timeout=1.0: 5.0
        try:
            with tempfile.TemporaryDirectory() as td:
                r = st.Runner(td,
                              down_tpls=["http://127.0.0.1:%d/down?bytes={n}" % port],
                              up_url="http://127.0.0.1:%d/up" % port)
                self.assertTrue(r.start())
                self.assertFalse(r.start())  # busy guard
                for _ in range(100):
                    s = r.snapshot()
                    if s["phase"] in ("done", "error"):
                        break
                    _t.sleep(0.2)
                self.assertEqual(s["phase"], "done")
                self.assertGreater(s["result"]["down_mbps"], 0)
                self.assertGreater(s["result"]["up_mbps"], 0)
                self.assertEqual(s["result"]["ping_ms"], 5.0)
                self.assertTrue(Path(td, "speedtest.json").exists())
                self.assertIsNotNone(st.last_result(td))
        finally:
            (st.DOWN_TIME_SEC, st.DOWN_CAP_BYTES, st.UP_BYTES, _nc.ping) = (
                old_dt, old_cap, old_up, old_ping)
            srv.shutdown()

    def test_report_identity_states(self):
        import json as _json
        from src.report import load_identity, identity_box
        with tempfile.TemporaryDirectory() as td:
            enabled, info = load_identity(str(Path(td) / "uptime.db"))
            box = identity_box(enabled, info)
            self.assertIn("not yet collected", box)  # nothing recorded yet: says so
            Path(td, "ipinfo.json").write_text(_json.dumps({
                "public_ip": "9.9.9.9", "local_ip": "192.168.1.5", "gateway": "192.168.1.1",
                "dns": ["1.1.1.1"], "updated_ts": 2000000000,
                "details": {"isp": "Test ISP", "org": "Test Org", "as": "AS123 Test",
                            "country": "Nowhere", "regionName": "", "city": "Testville",
                            "reverse": "x.example"}}))
            enabled, info = load_identity(str(Path(td) / "uptime.db"))
            box = identity_box(enabled, info)
            for needle in ("9.9.9.9", "Test ISP", "AS123 Test", "Testville", "192.168.1.5"):
                self.assertIn(needle, box)
            box_off = identity_box(False, info)
            self.assertIn("turned OFF", box_off)

    def test_summarize_honest_counts(self):
        """1s blips must NOT count as outages; confirmed drops must."""
        from src.report import summarize, letter_kind
        now = 2000000000.0
        since = now - 86400
        rows = [
            (since + 100, since + 101, "LOCAL_FAIL", "", "gw ping lost once"),   # 1s blip
            (since + 200, since + 230, "OFFLINE_ISP", "", "down 30s"),           # confirmed ISP
            (since + 300, since + 310, "LOCAL_FAIL", "", "router down 10s"),     # confirmed local
            (since + 400, since + 4600, "WIFI_OFF", "", "user off"),             # excluded
            (since + 5000, None, "ONLINE", "", "running"),
        ]
        beats = {int(since // 86400) * 86400: {"up": 80000, "down": 30, "loc": 10, "checks": 80040}}
        s = summarize(rows, beats, since, now)
        self.assertEqual(s["isp_conf"], 1)
        self.assertEqual(s["isp_sec"], 30)
        self.assertEqual(s["loc_conf"], 1)
        self.assertEqual(s["loc_sec"], 10)
        self.assertEqual(s["loc_blip"], 1)
        self.assertEqual(s["isp_blip"], 0)
        self.assertEqual(s["checks"], 80040)
        self.assertEqual(letter_kind(s), "isp")
        s2 = summarize([(since + 100, since + 101, "LOCAL_FAIL", "", "x")], {}, since, now)
        self.assertEqual(letter_kind(s2), "blips")
        s3 = summarize([(since + 100, since + 200, "ONLINE", "", "x")], {}, since, now)
        self.assertEqual(letter_kind(s3), "clear")


class TestPaths(unittest.TestCase):
    def test_fresh_install_uses_persistent_dir(self):
        from unittest import mock
        from src import paths as P
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            want = home / "Library" / "Application Support" / "UptimeMonitor"
            with mock.patch.object(Path, "home", return_value=home):
                with mock.patch.object(P.platform, "system", return_value="Darwin"):
                    self.assertEqual(P.data_dir(), want)

    def test_legacy_db_migrates_once_copy_only(self):
        from unittest import mock
        from src import paths as P
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            persist = home / "Library" / "Application Support" / "UptimeMonitor"
            legacy = home / "Library" / "Caches" / "UptimeMonitor"
            legacy.mkdir(parents=True, exist_ok=True)
            from src import db as dbmod
            conn = dbmod.connect(str(legacy / "uptime.db"))
            dbmod.open_event(conn, "ONLINE", "", "seed")
            conn.close()
            (legacy / "traffic.json").write_text("{}")
            with mock.patch.object(Path, "home", return_value=home):
                with mock.patch.object(P.platform, "system", return_value="Darwin"):
                    self.assertEqual(P.data_dir(), persist)
            self.assertTrue((persist / "uptime.db").is_file())
            self.assertTrue((persist / "traffic.json").is_file())
            # copy-only: legacy source is never deleted
            self.assertTrue((legacy / "uptime.db").is_file())
            # second call is a no-op (persistent already has state)
            with mock.patch.object(Path, "home", return_value=home):
                with mock.patch.object(P.platform, "system", return_value="Darwin"):
                    self.assertEqual(P.data_dir(), persist)


class TestOutageStickiness(unittest.TestCase):
    """Downtime must stick: a sustained outage stays red, never flaps.

    Regression tests for: red shown ~1s then green again while still down,
    with the downtime missing from reports.
    """

    def _make_mon(self, td):
        import logging
        from src.monitor import Monitor
        cfg = {
            "db_path": str(Path(td) / "t.db"),
            "check_interval_sec": 1.0, "fail_threshold": 2, "success_threshold": 2,
            "ping_timeout_sec": 0.8, "tcp_timeout_sec": 2.0,
            "ping_targets": ["1.1.1.1", "8.8.8.8", "9.9.9.9"],
            "tcp_check": {"host": "1.1.1.1", "port": 443},
            "sleep_gap_sec": 5.0, "dashboard_port": 8899, "dashboard_regen_sec": 10,
            "auto_open_after_sec": 30, "auto_open_cooldown_sec": 600,
            "auto_open_enabled": False, "isp_details_enabled": False,
        }
        return Monitor(cfg, logging.getLogger("test-sticky"), record=True)

    def _drive(self, mon, link_seq, net_ok_seq, gw_ok=True):
        """Drive mon.sample() with a stubbed network. No real I/O."""
        from unittest import mock
        from src import netcheck as probemod
        n = max(len(link_seq), len(net_ok_seq))
        states, cur = [], {"net_ok": False}
        with mock.patch.object(probemod, "get_gateway", return_value="192.168.1.99"), \
             mock.patch.object(probemod, "get_local_ip", return_value="192.168.1.5"):
            def fake_ping(host, timeout_sec=0.8):
                if host == "192.168.1.99":
                    return 2.0 if gw_ok else None
                return 7.0 if cur["net_ok"] else None

            def fake_tcp(host="1.1.1.1", port=443, timeout=2.0):
                return 7.0 if cur["net_ok"] else None

            with mock.patch.object(probemod, "ping", side_effect=fake_ping), \
                 mock.patch.object(probemod, "tcp_check", side_effect=fake_tcp):
                for i in range(n):
                    mon.link = dict(link_seq[i % len(link_seq)])
                    cur["net_ok"] = net_ok_seq[i % len(net_ok_seq)]
                    mon.last_slow = time.time()  # skip slow path: link set above
                    mon.last_tick = time.time()  # no fake sleep gaps
                    states.append(mon.sample()["state"])
        return states

    def test_sustained_outage_stays_red(self):
        with tempfile.TemporaryDirectory() as td:
            mon = self._make_mon(td)
            try:
                link = {"wifi_on": True, "ssid": "Home", "ethernet_up": None}
                states = self._drive(mon, [link], [False] * 8)
                self.assertEqual(states[0], "ONLINE")  # 1st fail below threshold
                self.assertTrue(all(s == "OFFLINE_ISP" for s in states[1:]),
                                "sustained outage must stay red, got %r" % (states,))
            finally:
                mon.close()

    def test_flaky_link_readout_must_not_clear_outage(self):
        with tempfile.TemporaryDirectory() as td:
            mon = self._make_mon(td)
            try:
                up = {"wifi_on": True, "ssid": "Home", "ethernet_up": None}
                unknown = {"wifi_on": None, "ssid": "", "ethernet_up": None}
                states = self._drive(mon, [up, unknown], [False] * 10)
                self.assertIn("OFFLINE_ISP", states)
                tail = states[4:]
                self.assertTrue(all(s == "OFFLINE_ISP" for s in tail),
                                "flaky link readout must not flap outage, got %r" % (states,))
                kinds = [r[0] for r in mon.conn.execute("SELECT type FROM events ORDER BY id").fetchall()]
                self.assertEqual(kinds, ["ONLINE", "OFFLINE_ISP"],
                                 "outage must be one red event, not fragments: %r" % (kinds,))
                from src import db as dbmod
                s = dbmod.stats_since(mon.conn, time.time() - 3600)
                self.assertEqual(s["outages_isp"], 1)
            finally:
                mon.close()

    def test_confirmed_wifi_off_never_blames_isp(self):
        with tempfile.TemporaryDirectory() as td:
            mon = self._make_mon(td)
            try:
                link = {"wifi_on": False, "ssid": "", "ethernet_up": False}
                states = self._drive(mon, [link], [False] * 6)
                self.assertTrue(all(s == "WIFI_OFF" for s in states), states)
                kinds = [r[0] for r in mon.conn.execute("SELECT type FROM events").fetchall()]
                self.assertNotIn("OFFLINE_ISP", kinds)
            finally:
                mon.close()

    def test_lone_success_does_not_end_outage(self):
        with tempfile.TemporaryDirectory() as td:
            mon = self._make_mon(td)
            try:
                link = {"wifi_on": True, "ssid": "Home", "ethernet_up": None}
                states = self._drive(mon, [link],
                                       [False] * 4 + [True] + [False] * 2 + [True, True])
                self.assertEqual(states[4], "OFFLINE_ISP",
                                 "one success must not end an outage, got %r" % (states,))
                self.assertEqual(states[-1], "ONLINE",
                                 "two consecutive successes must recover, got %r" % (states,))
            finally:
                mon.close()

    def test_single_blip_never_opens_outage(self):
        with tempfile.TemporaryDirectory() as td:
            mon = self._make_mon(td)
            try:
                link = {"wifi_on": True, "ssid": "Home", "ethernet_up": None}
                states = self._drive(mon, [link], [False, True, True, True])
                self.assertTrue(all(s == "ONLINE" for s in states), states)
                kinds = [r[0] for r in mon.conn.execute("SELECT type FROM events").fetchall()]
                self.assertNotIn("OFFLINE_ISP", kinds)
            finally:
                mon.close()

    def test_always_unknown_link_stays_gray_never_red(self):
        # Honesty guard: on systems with no usable link readout, a dead
        # internet must stay gray (NO_LINK), never blame the provider.
        with tempfile.TemporaryDirectory() as td:
            mon = self._make_mon(td)
            try:
                unknown = {"wifi_on": None, "ssid": "", "ethernet_up": None}
                states = self._drive(mon, [unknown], [False] * 6)
                self.assertEqual(states[0], "ONLINE")  # debounce: 1st fail
                self.assertTrue(all(s == "NO_LINK" for s in states[1:]), states)
                kinds = [r[0] for r in mon.conn.execute("SELECT type FROM events").fetchall()]
                self.assertNotIn("OFFLINE_ISP", kinds)
            finally:
                mon.close()

    def test_diagnostic_link_shape(self):
        from src import netcheck as probemod
        link = probemod.get_link_info()
        self.assertIn("wifi_on", link)
        self.assertIn("ethernet_up", link)


if __name__ == "__main__":
    unittest.main()
