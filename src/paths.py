"""Runtime paths. Code lives in the repo; state lives outside it.

Why: synced/backed-up folders (repos, ~/.local/share on this machine)
reject SQLite journal writes with disk I/O errors. This module PROBES
candidates with a real sqlite round-trip and returns the first that
actually works. Override anytime with:
  config.json {"db_path": ...}  or  env UPTIME_DB=/path/uptime.db

macOS note: persistent state lives in Application Support (survives
reboots and cache cleanups). ~/Library/Caches is only a legacy
fallback: if a DB exists there but not in Application Support, it is
copied over once (never deleted), so clearing Caches no longer wipes
history.
"""
import os
import platform
import sqlite3
from pathlib import Path


def _sqlite_ok(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        p = d / "_probe.db"
        c = sqlite3.connect(str(p), timeout=5)
        c.execute("create table if not exists t(x)")
        c.execute("insert into t values (1)")
        c.commit()
        c.close()
        try:
            p.unlink()
            for suf in ("-journal", "-wal", "-shm"):
                q = Path(str(p) + suf)
                if q.exists():
                    q.unlink()
        except Exception:
            pass
        return True
    except Exception:
        return False


def _has_state(d: Path) -> bool:
    """True if this dir already holds a monitor database."""
    try:
        return (d / "uptime.db").is_file()
    except Exception:
        return False


def _migrate_legacy_to_persistent(src: Path, dst: Path) -> None:
    """Copy legacy Caches state to the persistent dir once.

    Copy-only, never deletes src (user may clear Caches anytime).
    Never raises; partial copies are fine (dashboard regenerates).
    """
    try:
        if _has_state(dst) or not _has_state(src):
            return
        import shutil
        import sqlite3
        # Online-safe DB copy (works even while the old monitor runs).
        copied_db = False
        try:
            s = sqlite3.connect(str(src / "uptime.db"), timeout=5)
            try:
                d = sqlite3.connect(str(dst / "uptime.db"), timeout=5)
                try:
                    s.backup(d)
                    copied_db = True
                finally:
                    try:
                        d.close()
                    except Exception:
                        pass
            finally:
                try:
                    s.close()
                except Exception:
                    pass
        except Exception:
            copied_db = False
        if not copied_db:
            try:
                shutil.copy2(src / "uptime.db", dst / "uptime.db")
            except Exception:
                return
        for name in ("traffic.json", "ipinfo.json", "config.local.json", "speedtest.json"):
            try:
                s = src / name
                t = dst / name
                if s.is_file() and not t.exists():
                    shutil.copy2(s, t)
            except Exception:
                continue
        for sub in ("reports",):
            try:
                sdir, tdir = src / sub, dst / sub
                if not sdir.is_dir():
                    continue
                tdir.mkdir(parents=True, exist_ok=True)
                for f in sdir.iterdir():
                    try:
                        if f.is_file() and not (tdir / f.name).exists():
                            shutil.copy2(f, tdir / f.name)
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception:
        pass


def data_dir() -> Path:
    system = platform.system()
    cands = []
    if system == "Darwin":
        persistent = Path.home() / "Library" / "Application Support" / "UptimeMonitor"
        legacy = Path.home() / "Library" / "Caches" / "UptimeMonitor"
        try:
            if _has_state(legacy) and not _has_state(persistent) and _sqlite_ok(persistent):
                _migrate_legacy_to_persistent(legacy, persistent)
        except Exception:
            pass
        cands.append(persistent)
        cands.append(legacy)
    elif system == "Windows":
        cands.append(Path(os.environ.get("APPDATA", str(Path.home()))) / "UptimeMonitor")
    else:
        cands.append(Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "uptime-monitor")
    cands.append(Path("/tmp") / ("uptime-monitor-%s" % (os.getuid() if hasattr(os, "getuid") else "user")))
    for c in cands:
        try:
            if _sqlite_ok(c):
                return c
        except Exception:
            continue
    fallback = cands[0]
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def resolve_db_path(cfg_db: str = "") -> str:
    """Env override > explicit absolute config path > probed data dir."""
    env = os.environ.get("UPTIME_DB", "").strip()
    if env:
        return env
    if cfg_db and not cfg_db.startswith("data/") and Path(cfg_db).is_absolute():
        return cfg_db
    return str(data_dir() / "uptime.db")
