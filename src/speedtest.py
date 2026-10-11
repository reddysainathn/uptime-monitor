"""Accurate speed test (~18 seconds). Stdlib only (urllib + http.client), no third-party.

Measures, in order: ping/jitter (~2s), download (~8s, multi-connection),
upload (~6s, multi-connection, adaptive size). Runs in a background thread;
the dashboard polls for progress and shows results in a popup.

The test host (speed.cloudflare.com) is anycast: Cloudflare routes you to
the nearest server automatically. We read back WHICH server answered
(colo code like DFW) and show it with every result.

Accuracy design (why this beats a naive single-fetch test):
- Multi-connection (4x down / 3x up): a single TCP stream is capped by
  round-trip latency and congestion control, so it under-reports fast lines
  by 2-5x. Parallel streams saturate the link like speedtest.net/fast.com.
- Steady-state, not slow-start: TCP ramps up over the first ~1s. We discard
  the first 1.5s and measure sustained throughput, excluding DNS+TLS
  handshake (timing starts at first byte).
- Adaptive upload size: a fixed 6 MB body takes 10s on slow lines but 0.1s
  on fast ones (handshake dominates -> garbage). We probe first, then size
  the real test for ~5s on YOUR line.
- Median ping over 8 samples + mean-absolute-deviation jitter, monotonic
  clocks throughout, proxy bypass (env proxies would skew results).

Honesty notes (also shown in the UI):
- Approximate: real-world conditions vary second to second.
- Data use: ~30 MB typical, up to ~180 MB on very fast lines.
"""
import http.client
import json
import os
import threading
import time
import urllib.parse
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
DOWN_TIME_SEC = 8      # timed download window per round
DOWN_CAP_BYTES = 120_000_000  # TOTAL across all connections (data-use cap)
DOWN_CONNS = 4         # parallel download streams
DOWN_DISCARD_SEC = 1.5  # ignore slow-start ramp at the beginning
UP_BYTES = 12_000_000  # fallback TOTAL upload bytes if the probe fails
UP_CONNS = 3           # parallel upload streams
UP_TARGET_SEC = 5.0    # size the upload body for ~this many seconds
UP_MIN_TOTAL = 2_000_000
UP_MAX_TOTAL = 60_000_000
UP_PROBE_BYTES = 512_000
UP_PROBE_BIG = 4_000_000
CHUNK = 262144         # 256 KiB: fewer syscalls, saturates fast links
PING_SAMPLES = 8
PING_TIMEOUT = 1.0

# Reused random block for uploads (generated once, sent repeatedly).
# Random (incompressible) so no middlebox can cheat by compressing it.
_UPLOAD_BLOCK = None
_UPLOAD_BLOCK_LOCK = threading.Lock()


def _upload_block():
    global _UPLOAD_BLOCK
    if _UPLOAD_BLOCK is None:
        with _UPLOAD_BLOCK_LOCK:
            if _UPLOAD_BLOCK is None:
                try:
                    _UPLOAD_BLOCK = os.urandom(CHUNK)
                except Exception:
                    _UPLOAD_BLOCK = bytes((i % 251 for i in range(CHUNK)))
    return _UPLOAD_BLOCK


def mbps(nbytes, seconds):
    if seconds <= 0 or nbytes <= 0:
        return 0.0
    return round(nbytes * 8 / seconds / 1_000_000, 1)


def _median(vals):
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


def _opener():
    """urlopen opener that ignores env proxies (they would skew the test)."""
    try:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    except Exception:
        return None


_OPENER = None


def _urlopen(req, timeout=30):
    global _OPENER
    try:
        if _OPENER is None:
            _OPENER = _opener()
        if _OPENER is not None:
            return _OPENER.open(req, timeout=timeout)
    except Exception:
        pass
    return urllib.request.urlopen(req, timeout=timeout)


def _get_timed(url, seconds, cap, deadline):
    """Read for up to `seconds` (or `cap` bytes). Returns (bytes, elapsed).

    Kept for warmup + backward compatibility. Timing starts at first byte
    so DNS/TLS handshake is excluded. `deadline` may be a time.time() or
    time.monotonic() timestamp — we honor whichever runs out first by
    checking both.
    """
    total = 0
    t0 = time.monotonic()
    try:
        dl_m = float(deadline)
    except Exception:
        dl_m = t0 + seconds
    # Deadline may be wall-clock (time.time, ~1.7e9) or monotonic (small).
    # Checking the wrong clock either expires instantly or never fires,
    # so detect the domain by magnitude and check only that clock.
    use_wall = dl_m > 1e9
    end_m = t0 + seconds
    end_w = time.time() + seconds
    req = urllib.request.Request(url, headers={
        "User-Agent": "UptimeMonitor/1.0",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
    })
    first = None
    with _urlopen(req, timeout=30) as r:
        while total < cap:
            if use_wall:
                if time.time() >= min(dl_m, end_w):
                    break
            elif time.monotonic() >= min(dl_m, end_m):
                break
            chunk = r.read(CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if first is None:
                first = time.monotonic()
    end = time.monotonic()
    elapsed = (end - first) if first is not None else (end - t0)
    return total, max(elapsed, 1e-6)


def _colo():
    """Which Cloudflare server answered (e.g. DFW). Empty string on failure."""
    try:
        req = urllib.request.Request(TRACE_URL, headers={"User-Agent": "UptimeMonitor/1.0"})
        with _urlopen(req, timeout=8) as r:
            for line in r.read(4096).decode("utf-8", "replace").splitlines():
                if line.startswith("colo="):
                    return line.split("=", 1)[1].strip()[:16]
    except Exception:
        pass
    return ""


def _ping_stats(host="1.1.1.1", samples=PING_SAMPLES, timeout=PING_TIMEOUT):
    """Median ping + mean-absolute-deviation jitter over parallel samples.

    Returns (ping_ms, jitter_ms) or (None, None) if too few replies.
    Median (not min) reflects typical latency; MAD is order-independent,
    unlike consecutive differences on unordered parallel results.
    """
    try:
        from . import netcheck
    except Exception:
        import netcheck  # type: ignore
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = [pool.submit(netcheck.ping, host, timeout) for _ in range(samples)]
            vals = []
            for f in futs:
                try:
                    v = f.result(timeout=timeout + 2)
                except Exception:
                    v = None
                if v is not None:
                    try:
                        vals.append(float(v))
                    except Exception:
                        pass
    except Exception:
        return None, None
    if len(vals) < max(3, samples // 3):
        return None, None
    med = _median(vals)
    jit = sum(abs(v - med) for v in vals) / len(vals)
    return round(med, 1), round(jit, 1)


def _cache_buster(url):
    try:
        sep = "&" if "?" in url else "?"
        return "%s%sr=%d" % (url, sep, int(time.monotonic() * 1000) % 1000000)
    except Exception:
        return url


def _down_urls_for(tpl, per_conn_cap):
    """One URL per connection for template `tpl`, with cache-busting."""
    urls = []
    try:
        per_conn_cap = max(int(per_conn_cap), 256_000)
    except Exception:
        per_conn_cap = WARMUP_BYTES
    # Cachefly serves fixed files; pick one big enough for the cap.
    if "cachefly" in (tpl or ""):
        if per_conn_cap <= 1_500_000:
            fname = "1mb.test"
        elif per_conn_cap <= 12_000_000:
            fname = "10mb.test"
        else:
            fname = "100mb.test"
    else:
        fname = "10mb.test"
    for _ in range(DOWN_CONNS):
        try:
            u = tpl.format(n=per_conn_cap, f=fname)
        except Exception:
            u = tpl
        urls.append(_cache_buster(u))
    return urls


def _download_one(url, seconds, cap, deadline_m):
    """Single-stream timed fetch. Returns dict with steady-state breakdown."""
    out = {"bytes": 0, "t_first": None, "t_last": None, "snaps": [], "error": None}
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "UptimeMonitor/1.0",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
            "Connection": "close",
        })
        end = min(time.monotonic() + seconds, deadline_m)
        with _urlopen(req, timeout=30) as r:
            while out["bytes"] < cap and time.monotonic() < end:
                try:
                    chunk = r.read(CHUNK)
                except Exception as e:
                    # Timeout mid-stream with partial data is still usable.
                    if out["bytes"] > 0:
                        break
                    out["error"] = str(e)[:100]
                    break
                if not chunk:
                    break
                out["bytes"] += len(chunk)
                now = time.monotonic()
                if out["t_first"] is None:
                    out["t_first"] = now
                out["t_last"] = now
                out["snaps"].append((now, out["bytes"]))
    except Exception as e:
        if out["bytes"] <= 0:
            out["error"] = str(e)[:100]
    return out


def _bytes_at(snaps, t):
    """Interpolated byte count at time t from (t, bytes) snapshots."""
    if not snaps:
        return 0
    if t <= snaps[0][0]:
        return 0
    prev_t, prev_b = snaps[0]
    for st, sb in snaps[1:]:
        if st >= t:
            dt = st - prev_t
            if dt <= 0:
                return sb
            frac = (t - prev_t) / dt
            return int(prev_b + (sb - prev_b) * frac)
        prev_t, prev_b = st, sb
    return snaps[-1][1]


def _download_parallel(urls, seconds, per_conn_cap, deadline_m, progress_cb=None):
    """Run DOWN_CONNS streams; return (steady_mbps, overall_mbps, total_bytes).

    Steady-state discards the first DOWN_DISCARD_SEC after the first byte
    (TCP slow-start), which is the dominant bias on fast lines.
    """
    t_start = time.monotonic()
    results = []
    try:
        with ThreadPoolExecutor(max_workers=max(1, len(urls))) as pool:
            futs = [pool.submit(_download_one, u, seconds, per_conn_cap, deadline_m)
                    for u in urls]
            # Live progress while the workers run.
            n = len(futs)
            done = [False] * n
            while True:
                all_done = True
                for i, f in enumerate(futs):
                    if not done[i] and f.done():
                        done[i] = True
                    if not done[i]:
                        all_done = False
                if progress_cb is not None:
                    try:
                        el = time.monotonic() - t_start
                        progress_cb(min(0.99, el / max(seconds, 0.1)))
                    except Exception:
                        pass
                if all_done:
                    break
                time.sleep(0.15)
            for f in futs:
                try:
                    results.append(f.result(timeout=1))
                except Exception:
                    results.append({"bytes": 0, "t_first": None, "t_last": None,
                                    "snaps": [], "error": "join"})
    except Exception:
        pass
    total = sum((r.get("bytes") or 0) for r in results)
    firsts = [r["t_first"] for r in results if r.get("t_first") is not None]
    lasts = [r["t_last"] for r in results if r.get("t_last") is not None]
    if not firsts or not lasts or total <= 0:
        return 0.0, 0.0, total
    t_first = min(firsts)
    t_last = max(lasts)
    overall_dt = t_last - t_first
    # No 50ms floor here: a transfer that finishes in 3ms still proves at
    # least total*8/0.003 throughput (a valid lower bound, seen on loopback).
    # Only a zero/negative dt is unmeasurable.
    overall = mbps(total, max(overall_dt, 1e-6))
    # Steady-state: bytes after the slow-start discard window.
    cutoff = t_first + DOWN_DISCARD_SEC
    steady_wall = t_last - cutoff
    if steady_wall >= 1.0:
        steady_bytes = 0
        for r in results:
            snaps = r.get("snaps") or []
            if not snaps:
                continue
            steady_bytes += max(0, (r.get("bytes") or 0) - _bytes_at(snaps, cutoff))
        steady = mbps(steady_bytes, steady_wall) if steady_bytes > 0 else 0.0
        # Sanity: steady should not wildly exceed overall (reordering/noise
        # guard). If it does, trust overall.
        if steady > 0 and overall > 0 and steady > overall * 3:
            return overall, overall, total
        if steady > 0:
            return steady, overall, total
    return overall, overall, total


def _post(url, payload, deadline):
    """Single POST; returns send+response seconds. Kept for probe/compat."""
    req = urllib.request.Request(url, data=payload, method="POST",
                                 headers={"User-Agent": "UptimeMonitor/1.0",
                                          "Content-Type": "application/octet-stream"})
    t0 = time.monotonic()
    with _urlopen(req, timeout=30) as r:
        r.read(1024)
    return time.monotonic() - t0


def _upload_one_precise(url, nbytes, deadline_m):
    """POST nbytes via http.client; time ONLY the body send.

    Connect/TLS handshake happens before the timer; response wait happens
    after it. Returns (nbytes, send_seconds, t0, t1). Raises on failure.
    """
    parts = urllib.parse.urlparse(url)
    host = parts.hostname or ""
    if not host:
        raise RuntimeError("bad upload url")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    if parts.scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=15)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=15)
    try:
        conn.connect()  # handshake — deliberately NOT timed
        conn.putrequest("POST", path, skip_host=False, skip_accept_encoding=True)
        conn.putheader("User-Agent", "UptimeMonitor/1.0")
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(int(nbytes)))
        conn.putheader("Connection", "close")
        conn.putheader("Cache-Control", "no-cache")
        conn.endheaders()
        block = _upload_block()
        blen = len(block)
        remaining = int(nbytes)
        t0 = time.monotonic()
        while remaining > 0:
            if time.monotonic() > deadline_m:
                raise TimeoutError("upload deadline exceeded")
            n = blen if remaining >= blen else remaining
            conn.send(block[:n])
            remaining -= n
        t1 = time.monotonic()
        resp = conn.getresponse()
        try:
            resp.read(4096)
        except Exception:
            pass
        if resp.status not in (200, 201, 204):
            raise RuntimeError("upload http %s" % resp.status)
        return int(nbytes), max(t1 - t0, 1e-6), t0, t1
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _upload_parallel(url, total_bytes, n_conns, deadline_m, progress_cb=None):
    """Parallel uploads; throughput = total bytes / send-wall seconds."""
    try:
        total_bytes = int(total_bytes)
    except Exception:
        return 0.0
    n_conns = max(1, int(n_conns))
    per = total_bytes // n_conns
    rem = total_bytes - per * n_conns
    sizes = [per + (1 if i < rem else 0) for i in range(n_conns)]
    sizes = [s for s in sizes if s > 0]
    if not sizes:
        return 0.0
    t_start = time.monotonic()
    results = []
    try:
        with ThreadPoolExecutor(max_workers=len(sizes)) as pool:
            futs = [pool.submit(_upload_one_precise, _cache_buster(url), s, deadline_m)
                    for s in sizes]
            while True:
                if all(f.done() for f in futs):
                    break
                if progress_cb is not None:
                    try:
                        # Progress by elapsed vs expected; workers report at end.
                        progress_cb(None)
                    except Exception:
                        pass
                if time.monotonic() > deadline_m:
                    break
                time.sleep(0.15)
            for f in futs:
                try:
                    results.append(f.result(timeout=1))
                except Exception:
                    pass
    except Exception:
        pass
    if not results:
        return 0.0
    # Require a quorum: one stalled stream must not nuke the test, but a
    # lone survivor on a 3-stream test is not trustworthy either.
    if len(results) < max(1, (len(sizes) + 1) // 2):
        return 0.0
    sent = sum(r[0] for r in results)
    t0 = min(r[2] for r in results)
    t1 = max(r[3] for r in results)
    wall = t1 - t0
    _ = t_start
    if wall <= 0.05 or sent <= 0:
        return 0.0
    return mbps(sent, wall)


def _pick_upload_total(up_url, deadline_m):
    """Probe the line, then size the upload body for ~UP_TARGET_SEC seconds."""
    def probe(n):
        try:
            got, dt, _, _ = _upload_one_precise(_cache_buster(up_url), n, deadline_m)[0:4]
            # _upload_one_precise returns (bytes, send_dt, t0, t1)
            send_dt = dt
            if send_dt <= 0 or got <= 0:
                return 0.0
            return got / send_dt  # bytes/sec
        except Exception:
            return 0.0
    try:
        rate = probe(min(UP_PROBE_BYTES, UP_MAX_TOTAL))
        if rate <= 0:
            return UP_BYTES
        if rate * 0.5 < UP_PROBE_BIG:
            # Fast line: small probe was handshake-dominated; re-probe bigger.
            dt_small = min(UP_PROBE_BYTES, UP_MAX_TOTAL) / rate
            if dt_small < 0.5:
                big_rate = probe(min(UP_PROBE_BIG, UP_MAX_TOTAL))
                if big_rate > 0:
                    rate = big_rate
        target = rate * UP_TARGET_SEC
        total = int(min(max(target, UP_MIN_TOTAL), UP_MAX_TOTAL))
        # Round to whole per-connection chunks for even splitting.
        total = max(total - (total % UP_CONNS), UP_CONNS * 256_000)
        return total
    except Exception:
        return UP_BYTES


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
            # 1) ping + jitter: median over parallel samples (~2s)
            self._set(step="ping", progress=5)
            ping_ms, jitter_ms = _ping_stats()
            if ping_ms is None:
                raise RuntimeError("no ping replies — are you online?")
            res["ping_ms"] = ping_ms
            res["jitter_ms"] = jitter_ms
            # which server will answer? (anycast routes to the nearest)
            colo = _colo()
            host = "speed.cloudflare.com"
            res["server"] = ("%s · %s" % (colo, host)) if colo else host
            # 2) download: warmup, then parallel timed streams (steady-state)
            self._set(step="download", progress=15)
            deadline_m = time.monotonic() + 90
            try:
                per_conn_cap = max(256_000, int(DOWN_CAP_BYTES) // max(1, DOWN_CONNS))
            except Exception:
                per_conn_cap = 30_000_000
            best = 0.0
            best_overall = 0.0
            for tpl in self.down_tpls:
                try:
                    try:
                        _get_timed(tpl.format(n=WARMUP_BYTES, f="1mb.test"),
                                   4, WARMUP_BYTES, deadline_m)
                    except Exception:
                        pass

                    def _prog(frac, _tpl=tpl):
                        try:
                            # download spans progress 15 -> 70
                            self._set(progress=int(15 + 55 * max(0.0, min(1.0, frac))))
                        except Exception:
                            pass

                    urls = _down_urls_for(tpl, per_conn_cap)
                    steady, overall, got = _download_parallel(
                        urls, DOWN_TIME_SEC, per_conn_cap, deadline_m,
                        progress_cb=_prog)
                    self._set(progress=70)
                    if got > WARMUP_BYTES and (steady > 0 or overall > 0):
                        cand = steady if steady > 0 else overall
                        best_overall = max(best_overall, overall)
                        best = max(best, cand)
                        res["down_mbps"] = best
                        res["down_mbps_overall"] = round(best_overall, 1)
                        break
                except Exception:
                    continue
            if best <= 0:
                raise RuntimeError("download failed on all mirrors")
            res["down_mbps"] = best
            # 3) upload: probe-sized parallel POSTs, send-time only (~6s)
            self._set(step="upload", progress=75)
            try:
                total = _pick_upload_total(self.up_url, time.monotonic() + 30)
                up_deadline = time.monotonic() + 30

                def _uprog(_frac):
                    try:
                        el = time.monotonic()
                        _uprog.t0 = getattr(_uprog, "t0", el)
                        frac = min(0.99, (el - _uprog.t0) / max(UP_TARGET_SEC, 0.1))
                        self._set(progress=int(75 + 20 * frac))
                    except Exception:
                        pass

                up = _upload_parallel(self.up_url, total, UP_CONNS, up_deadline,
                                      progress_cb=_uprog)
                if up > 0:
                    res["up_mbps"] = up
                else:
                    # Fallback: single precise POST (old behavior, but send-timed).
                    try:
                        n = min(UP_BYTES, UP_MAX_TOTAL)
                        got_b, dt, _, _ = _upload_one_precise(
                            _cache_buster(self.up_url), n, time.monotonic() + 20)
                        res["up_mbps"] = mbps(got_b, dt) if dt > 0 else None
                        if not res.get("up_mbps"):
                            raise RuntimeError("upload measured 0")
                    except Exception as e2:
                        res["up_mbps"] = None
                        res["up_note"] = "upload unavailable (%s)" % str(e2)[:80]
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
