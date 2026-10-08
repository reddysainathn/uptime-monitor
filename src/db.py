"""SQLite storage for UptimeMonitor. Stdlib only, crash-safe, anti-bloat.

Why SQLite: built into Python, single file, survives crash, ~3-5 MB/year.
Anti-bloat design (auto-handled, nothing for you to do):
- EVENTS only on change (not every second) -> ~50-200 rows/day even on messy wifi
- HEARTBEATS 1 row/minute, buffered in memory, 1 write/min (60x less I/O)
- DAILY rollup: after 30 days, per-minute rows are merged into 1 row/day
- RETAIN: heartbeats 30d detailed, events 180d, daily forever (tiny)
- SIZE CAP: if file > 10 MB, oldest rows pruned + VACUUM automatically
- WAL checkpoint on every flush so -wal file never grows
"""
import sqlite3
import time
from pathlib import Path

MAX_DB_MB = 10
HEARTBEAT_DETAIL_DAYS = 30
EVENT_RETAIN_DAYS = 180

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts REAL NOT NULL,
    end_ts REAL,
    type TEXT NOT NULL,
    ssid TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_ts);
CREATE TABLE IF NOT EXISTS heartbeats (
    minute_ts INTEGER PRIMARY KEY,
    up_sec INTEGER DEFAULT 0,
    down_sec INTEGER DEFAULT 0,
    local_fail_sec INTEGER DEFAULT 0,
    wifi_off_sec INTEGER DEFAULT 0,
    checks INTEGER DEFAULT 0,
    latency_sum_ms REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS daily (
    day_ts INTEGER PRIMARY KEY,
    up_sec INTEGER DEFAULT 0,
    down_sec INTEGER DEFAULT 0,
    local_fail_sec INTEGER DEFAULT 0,
    wifi_off_sec INTEGER DEFAULT 0,
    outages INTEGER DEFAULT 0
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=5000;")
    except Exception:
        pass
    try:
        conn.execute("PRAGMA synchronous=NORMAL;")
    except Exception:
        pass
    try:
        conn.execute("PRAGMA temp_store=MEMORY;")
    except Exception:
        pass
    try:
        # WAL is fastest, but fails on iCloud/network drives -> fall back to DELETE
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        try:
            conn.execute("PRAGMA journal_mode=DELETE;")
        except Exception:
            pass
    try:
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL;")
    except Exception:
        pass
    conn.executescript(SCHEMA)
    return conn


def checkpoint(conn: sqlite3.Connection):
    """Force WAL -> main db so .db-wal never bloats. Cheap, call after flush."""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    except Exception:
        pass


def size_mb(db_path: str) -> float:
    try:
        p = Path(db_path)
        total = p.stat().st_size if p.exists() else 0
        for suf in ("-wal", "-shm"):
            q = Path(str(p) + suf)
            if q.exists():
                total += q.stat().st_size
        return total / (1024 * 1024)
    except Exception:
        return 0.0


def open_event(conn: sqlite3.Connection, type_: str, ssid: str = "", detail: str = "") -> int:
    """Close any open event, then open a new one. Immediate commit (never lose proof)."""
    now = time.time()
    conn.execute("UPDATE events SET end_ts=? WHERE end_ts IS NULL", (now,))
    cur = conn.execute(
        "INSERT INTO events (start_ts, end_ts, type, ssid, detail) VALUES (?,?,?,?,?)",
        (now, None, type_, ssid[:64], detail[:300]),
    )
    conn.commit()
    return cur.lastrowid


def close_open_event(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE events SET end_ts=? WHERE end_ts IS NULL", (time.time(),))
    conn.commit()
    checkpoint(conn)


def current_open(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT id, start_ts, type, ssid, detail FROM events WHERE end_ts IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()


def add_heartbeat(conn: sqlite3.Connection, minute_ts: int, bucket: str, latency_ms: float = 0):
    """Single-second helper (tests/tools). Daemon uses buffered flush_bulk instead."""
    cols = {"up": "up_sec", "down": "down_sec", "local": "local_fail_sec", "wifioff": "wifi_off_sec"}
    col = cols.get(bucket, "up_sec")
    conn.execute(
        "INSERT INTO heartbeats (minute_ts, {0}, checks, latency_sum_ms) VALUES (?,?,1,?)"
        " ON CONFLICT(minute_ts) DO UPDATE SET {0}={0}+1, checks=checks+1,"
        " latency_sum_ms=latency_sum_ms+excluded.latency_sum_ms".format(col),
        (minute_ts, 1, latency_ms or 0),
    )
    conn.commit()


def flush_bulk(conn: sqlite3.Connection, buf: dict):
    """Write buffered minutes in ONE transaction + checkpoint. buf: {m: [u,d,lo,w,c,ls]}."""
    if not buf:
        return
    for m, (u, d, lo, w, c, ls) in buf.items():
        conn.execute(
            """INSERT INTO heartbeats (minute_ts,up_sec,down_sec,local_fail_sec,
               wifi_off_sec,checks,latency_sum_ms) VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(minute_ts) DO UPDATE SET up_sec=up_sec+?,down_sec=down_sec+?,
               local_fail_sec=local_fail_sec+?,wifi_off_sec=wifi_off_sec+?,
               checks=checks+?,latency_sum_ms=latency_sum_ms+?""",
            (m, u, d, lo, w, c, ls, u, d, lo, w, c, ls),
        )
    conn.commit()
    checkpoint(conn)


def rollup_old_to_daily(conn: sqlite3.Connection):
    """Merge per-minute rows older than 30d into 1 row/day, then delete them."""
    cut = int((time.time() - HEARTBEAT_DETAIL_DAYS * 86400) // 86400) * 86400
    try:
        rows = conn.execute(
            """SELECT (minute_ts - (minute_ts % 86400)) AS day,
               SUM(up_sec),SUM(down_sec),SUM(local_fail_sec),SUM(wifi_off_sec)
               FROM heartbeats WHERE minute_ts < ? GROUP BY day""",
            (cut,),
        ).fetchall()
        for day, u, d, lo, w in rows:
            out = conn.execute(
                "SELECT COUNT(*) FROM events WHERE type='OFFLINE_ISP' AND start_ts>=? AND start_ts<?",
                (day, day + 86400),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO daily (day_ts,up_sec,down_sec,local_fail_sec,wifi_off_sec,outages)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(day_ts) DO UPDATE SET up_sec=up_sec+?,down_sec=down_sec+?,
                   local_fail_sec=local_fail_sec+?,wifi_off_sec=wifi_off_sec+?,outages=outages+?""",
                (day, u or 0, d or 0, lo or 0, w or 0, out,
                 u or 0, d or 0, lo or 0, w or 0, out),
            )
        if rows:
            conn.execute("DELETE FROM heartbeats WHERE minute_ts < ?", (cut,))
            conn.commit()
    except Exception:
        pass


def prune(db_path: str, conn: sqlite3.Connection, log=None):
    """Auto-maintain: rollup + retain + size-cap + vacuum-if-needed. Safe to call daily."""
    try:
        rollup_old_to_daily(conn)
        cut_ev = time.time() - EVENT_RETAIN_DAYS * 86400
        conn.execute("DELETE FROM events WHERE end_ts IS NOT NULL AND end_ts < ?", (cut_ev,))
        conn.commit()
        checkpoint(conn)
        # size cap: if still big, drop oldest heartbeats in chunks (daily already rolled up)
        if size_mb(db_path) > MAX_DB_MB:
            if log:
                log.info("db %.1fMB over cap, pruning", size_mb(db_path))
            conn.execute("DELETE FROM heartbeats WHERE minute_ts IN "
                         "(SELECT minute_ts FROM heartbeats ORDER BY minute_ts ASC LIMIT 5000)")
            conn.execute("DELETE FROM events WHERE id IN "
                         "(SELECT id FROM events WHERE end_ts IS NOT NULL ORDER BY start_ts ASC LIMIT 1000)")
            conn.commit()
            conn.execute("VACUUM;")
            if log:
                log.info("db pruned to %.1fMB", size_mb(db_path))
        else:
            try:
                free = conn.execute("PRAGMA freelist_count;").fetchone()[0]
                if free > 500:
                    conn.execute("PRAGMA incremental_vacuum;")
            except Exception:
                pass
    except Exception as e:
        if log:
            log.warning("prune failed: %s", e)


def forget_range(db_path: str, start_ts: float, end_ts: float, note: str = "",
                 types=None) -> dict:
    """Replace fully-inside events with one WIFI_OFF event.

    For user-confirmed corrections (e.g. "my wifi was off then").
    types: only touch these event types (default: all). Open events (end NULL)
    are never touched. Gray/excluded either way — this fixes labels, not totals.
    Returns {deleted, inserted}.
    """
    conn = connect(db_path)
    try:
        q = ("SELECT COUNT(*) FROM events WHERE start_ts>=? AND start_ts<? "
             "AND end_ts IS NOT NULL AND end_ts<=?")
        d = ("DELETE FROM events WHERE start_ts>=? AND start_ts<? "
             "AND end_ts IS NOT NULL AND end_ts<=?")
        args = [start_ts, end_ts, end_ts]
        if types:
            q += " AND type IN (%s)" % ",".join("?" * len(types))
            d += " AND type IN (%s)" % ",".join("?" * len(types))
            args = args + list(types)
        rows = conn.execute(q, args).fetchone()[0]
        conn.execute(d, args)
        conn.execute(
            "INSERT INTO events (start_ts,end_ts,type,ssid,detail) VALUES (?,?,?,?,?)",
            (start_ts, end_ts, "WIFI_OFF", "",
             note or "reclassified on user request"))
        conn.commit()
        checkpoint(conn)
        return {"deleted": rows, "inserted": 1}
    finally:
        conn.close()


def recent_events(conn: sqlite3.Connection, limit: int = 100):
    return conn.execute(
        "SELECT start_ts, end_ts, type, ssid, detail FROM events ORDER BY start_ts DESC LIMIT ?",
        (limit,),
    ).fetchall()


def union_seconds(intervals) -> float:
    """Total covered time with overlaps merged (never double-counts)."""
    pts = sorted((s, e) for s, e in intervals if e > s)
    total = 0.0
    cur_s = cur_e = None
    for s, e in pts:
        if cur_s is None:
            cur_s, cur_e = s, e
        elif s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total += cur_e - cur_s
            cur_s, cur_e = s, e
    if cur_s is not None:
        total += cur_e - cur_s
    return total


def stats_since(conn: sqlite3.Connection, since_ts: float):
    """Plain numbers for dashboard + ISP. GAP/SLEEP/WIFI_OFF never counted as downtime.

    An OUTAGE is any confirmed internet-down period: provider-side (ISP) plus
    router-side (own box or ISP wall unit). The split by fault is in
    outages_isp / outages_local — never hide one inside a zero total.
    """
    now = time.time()
    rows = conn.execute(
        "SELECT type, start_ts, end_ts FROM events WHERE start_ts >= ? OR end_ts IS NULL OR end_ts >= ?",
        (since_ts, since_ts),
    ).fetchall()
    down = local = wifioff = 0.0
    outages = 0
    n_isp = n_loc = 0
    longest = loc_long = 0.0
    down_iv, loc_iv, off_iv = [], [], []
    for typ, s, e in rows:
        e = e or now
        s = max(s, since_ts)
        d = max(0, e - s)
        if typ == "OFFLINE_ISP":
            down_iv.append((s, e))
            outages += 1
            n_isp += 1
            longest = max(longest, d)
        elif typ == "LOCAL_FAIL":
            loc_iv.append((s, e))
            outages += 1
            n_loc += 1
            loc_long = max(loc_long, d)
        elif typ in ("WIFI_OFF", "NO_LINK"):
            off_iv.append((s, e))
    down, local, wifioff = (union_seconds(v) for v in (down_iv, loc_iv, off_iv))
    total = max(1, now - since_ts - wifioff)
    # four-nines precision (e.g. 99.9935%): with 1s sampling every second counts
    uptime_pct = max(0, 100 * (total - down - local) / total)
    return {
        "down_sec": round(down),
        "local_sec": round(local),
        "wifioff_sec": round(wifioff),
        "outages": outages,
        "outages_isp": n_isp,
        "outages_local": n_loc,
        "longest_sec": round(longest),
        "local_longest_sec": round(loc_long),
        "uptime_pct": round(uptime_pct, 4),
    }
