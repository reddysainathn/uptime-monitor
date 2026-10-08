"""True speed test (~15 seconds). Stdlib only (urllib), no third-party.

Measures, in order: ping/jitter (~1s, parallel pings), download (~7s timed
transfer), upload (~5s). Runs in a background thread; the dashboard polls
for progress and shows results in a popup.

The test host (speed.cloudflare.com) is anycast: Cloudflare routes you to
the nearest server automatically. We read back WHICH server answered
(colo code like DFW) and show it with every result.

Honesty notes (also shown in the UI):
- Single-connection test: reflects one stream, not 10 parallel ones.
- Approximate: real-world conditions vary second to second.
- Data use: ~10 MB typical, up to ~60 MB on very fast lines.
"""
import json
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DOWN_URLS = [
    "https://speed.cloudflare.com/__down?bytes={n}",
    "https://cachefly.cachefly.net/{f}",
]
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
UP_URL = "https://speed.cloudflare.com/__up"
WARMUP_BYTES = 1_000_000
DOWN_TIME_SEC = 6      # timed download window (fast lines hit the cap instead)
DOWN_CAP_BYTES = 50_000_000
UP_BYTES = 6_000_000   # upload body; measured by actual send time
CHUNK = 65536


def mbps(nbytes, seconds):
    if seconds <= 0 or nbytes <= 0:
        return 0.0
    return round(nbytes * 8 / seconds / 1_000_000, 1)


def _get_timed(url, seconds, cap, deadline):
    """Read for up to `seconds` (or `cap` bytes). Returns (bytes, elapsed)."""
    total = 0
    t0 = time.time()
    end = min(t0 + seconds, deadline)
    req = urllib.request.Request(url, headers={"User-Agent": "UptimeMonitor/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        while total < cap and time.time() < end:
            chunk = r.read(CHUNK)
            if not chunk:
                break
            total += len(chunk)
    return total, time.time() - t0


def _colo():
    """Which Cloudflare server answered (e.g. DFW). Empty string on failure."""
    try:
        req = urllib.request.Request(TRACE_URL, headers={"User-Agent": "UptimeMonitor/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            for line in r.read(4096).decode("utf-8", "replace").splitlines():
                if line.startswith("colo="):
                    return line.split("=", 1)[1].strip()[:16]
    except Exception:
        pass
    return ""


def _post(url, payload, deadline):
    req = urllib.request.Request(url, data=payload, method="POST",
                                 headers={"User-Agent": "UptimeMonitor/1.0",
                                          "Content-Type": "application/octet-stream"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read(1024)
    return time.time() - t0


class Runner:
    def __init__(self, datadir, down_tpls=None, up_url=None):
        self.datadir = str(datadir)
        self.down_tpls = down_tpls or list(DOWN_URLS)
        self.up_url = up_url or UP_URL
        self._lock = threading.Lock()
        self._state = {"phase": "idle", "progress": 0}

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def start(self):
        """Returns True if a run started, False if one is already going."""
        with self._lock:
            if self._state.get("phase") == "running":
                return False
            self._state = {"phase": "running", "progress": 1, "step": "ping"}
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        return True

    def _set(self, **kw):
        with self._lock:
            self._state.update(kw)

    def _run(self):
        res = {"when": time.time()}
        try:
            from . import netcheck
            # 1) ping + jitter, in parallel (~1s)
            self._set(step="ping", progress=5)
            with ThreadPoolExecutor(max_workers=4) as pool:
                samples = [f for f in
                           pool.map(lambda _: netcheck.ping("1.1.1.1", 1.0), range(4))
                           if f is not None]
            if not samples:
                raise RuntimeError("no ping replies — are you online?")
            res["ping_ms"] = round(min(samples), 1)
            res["jitter_ms"] = round(
                sum(abs(a - b) for a, b in zip(samples, samples[1:])) / max(1, len(samples) - 1), 1)
            # which server will answer? (anycast routes to the nearest)
            colo = _colo()
            host = "speed.cloudflare.com"
            res["server"] = ("%s · %s" % (colo, host)) if colo else host
            # 2) download: 1 MB warmup, then one timed window (fast lines hit the cap)
            self._set(step="download", progress=15)
            deadline = time.time() + 60
            best = 0.0
            for tpl in self.down_tpls:
                try:
                    try:
                        _get_timed(tpl.format(n=WARMUP_BYTES, f="1mb.test"),
                                   4, WARMUP_BYTES, deadline)
                    except Exception:
                        pass
                    url = tpl.format(n=DOWN_CAP_BYTES, f="10mb.test")
                    got, dt = _get_timed(url, DOWN_TIME_SEC, DOWN_CAP_BYTES, deadline)
                    if got > WARMUP_BYTES:
                        best = max(best, mbps(got, dt))
                    self._set(progress=55)
                    if best > 0:
                        break
                except Exception:
                    continue
            if best <= 0:
                raise RuntimeError("download failed on all mirrors")
            res["down_mbps"] = best
            # 3) upload: fixed body, measured by actual send time
            self._set(step="upload", progress=65)
            try:
                dt = _post(self.up_url, os.urandom(UP_BYTES), time.time() + 20)
                res["up_mbps"] = mbps(UP_BYTES, dt)
            except Exception as e:
                res["up_mbps"] = None
                res["up_note"] = "upload unavailable (%s)" % str(e)[:80]
            self._set(progress=95)
            res["ok"] = True
        except Exception as e:
            res = {"ok": False, "error": str(e)[:200], "when": time.time()}
        try:
            tmp = str(Path(self.datadir) / "speedtest.json.tmp")
            with open(tmp, "w") as f:
                json.dump(res, f)
            os.replace(tmp, str(Path(self.datadir) / "speedtest.json"))
        except Exception:
            pass
        self._set(phase="done" if res.get("ok") else "error",
                  progress=100, result=res if res.get("ok") else None,
                  error=res.get("error"))


def last_result(datadir):
    try:
        d = json.loads((Path(str(datadir)) / "speedtest.json").read_text())
        return d if isinstance(d, dict) and d.get("ok") else None
    except Exception:
        return None
