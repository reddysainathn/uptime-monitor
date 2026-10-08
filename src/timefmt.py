"""Timezone helpers. Stdlib only (zoneinfo, Python 3.9+). Python-safe, never raises.

All displayed times go through here so the whole app shares one clock:
a configurable IANA zone (e.g. America/Chicago, DST handled automatically)
or "local" for the computer's own timezone.
"""
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo, available_timezones
    _HAS_TZ = True
except Exception:
    ZoneInfo = None
    _HAS_TZ = False


def load_tz_name(dd=None, root=None):
    """Personal override (data dir) wins, repo config is the default, else local."""
    for p in ([Path(dd) / "config.local.json"] if dd else []) + (
            [Path(root) / "config.json"] if root else []):
        try:
            v = __import__("json").loads(Path(p).read_text()).get("timezone")
            if isinstance(v, str) and v.strip():
                return v.strip()
        except Exception:
            continue
    return "local"


def valid_tz(name):
    """'local'/'' always ok; otherwise must be a real IANA zone."""
    if not name or name == "local":
        return True
    if not _HAS_TZ:
        return False
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


def _tz(name):
    if not name or name == "local" or not _HAS_TZ:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def fmt(ts, pattern, tzname="local"):
    """Format epoch seconds. Falls back to system local on any problem."""
    try:
        return datetime.fromtimestamp(float(ts), _tz(tzname)).strftime(pattern)
    except Exception:
        try:
            return datetime.fromtimestamp(float(ts)).strftime(pattern)
        except Exception:
            return "—"


def iso(ts, tzname="local"):
    """ISO with UTC offset (for CSVs), e.g. 2026-09-18T16:55:00-05:00."""
    try:
        return datetime.fromtimestamp(float(ts), _tz(tzname)).isoformat(timespec="seconds")
    except Exception:
        try:
            return datetime.fromtimestamp(float(ts)).isoformat(timespec="seconds")
        except Exception:
            return ""


def day_floor(ts, tzname="local"):
    """Epoch of local midnight containing ts (day grouping respects the zone)."""
    try:
        return datetime.fromtimestamp(float(ts), _tz(tzname)).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
    except Exception:
        return int(float(ts) // 86400) * 86400


def label(tzname="local"):
    return "computer time" if not tzname or tzname == "local" else tzname


def parse_local(s, tzname="local"):
    """Parse 'YYYY-MM-DD HH:MM' (or date only) in the given zone -> epoch.
    Raises ValueError on bad input."""
    from datetime import datetime as _dt
    txt = (s or "").strip()
    dt = None
    for pat in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M"):
        try:
            dt = _dt.strptime(txt, pat)
            break
        except Exception:
            continue
    if dt is None:
        raise ValueError("use YYYY-MM-DD HH:MM, e.g. 2026-09-18 21:00")
    z = _tz(tzname)
    if z is not None:
        dt = dt.replace(tzinfo=z)
    return dt.timestamp()
