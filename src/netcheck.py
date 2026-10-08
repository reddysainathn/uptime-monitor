"""Cross-platform link checks. Stdlib only. Never crashes, never hangs.

Expert-tiny rules:
- Every subprocess has a hard timeout (never block the 1s loop)
- Slow OS queries (wifi/gateway) are CACHED + throttled by caller (every 5s/60s),
  fast path (ping/tcp) runs every 1s
- Unknown OS/feature -> return None (monitor treats None as 'assume up,
  let ping decide') so ISP proof still works anywhere
"""
import platform
import re
import shutil
import socket
import subprocess
import time
SYSTEM = platform.system()  # Darwin | Linux | Windows
_gateway_cache = {"ip": None, "ts": 0}


def _run(cmd, timeout=3):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception:
        return 1, ""


def ping(host: str, timeout_sec: float = 0.8):
    """Return latency_ms or None. Works on mac/linux/windows, no root needed."""
    try:
        if SYSTEM == "Windows":
            cmd = ["ping", "-n", "1", "-w", str(int(timeout_sec * 1000)), host]
        elif SYSTEM == "Darwin":
            cmd = ["ping", "-c", "1", "-W", str(int(timeout_sec * 1000)), "-t", "2", host]
        else:  # Linux
            cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout_sec + 0.5))), host]
        if not shutil.which("ping"):
            return None
        rc, out = _run(cmd, timeout=timeout_sec + 1.5)
        if rc != 0:
            return None
        m = re.search(r"time[=<]\s*([\d.]+)\s*ms", out)
        return float(m.group(1)) if m else 1.0  # up, latency unknown
    except Exception:
        return None


def tcp_check(host="1.1.1.1", port=443, timeout=2.0):
    """TCP connect latency or None. Catches networks that block ICMP."""
    try:
        t0 = time.monotonic()
        socket.create_connection((host, port), timeout=timeout).close()
        return (time.monotonic() - t0) * 1000
    except Exception:
        return None


def get_gateway():
    """Cached 60s. Returns IP string or None."""
    now = time.monotonic()
    if now - _gateway_cache["ts"] < 60 and _gateway_cache["ip"]:
        return _gateway_cache["ip"]
    ip = None
    try:
        if SYSTEM == "Darwin":
            rc, out = _run(["route", "-n", "get", "default"], timeout=2)
            m = re.search(r"gateway:\s*(\S+)", out)
            ip = m.group(1) if m else None
        elif SYSTEM == "Linux":
            if shutil.which("ip"):
                rc, out = _run(["ip", "route", "show", "default"], timeout=2)
                m = re.search(r"default via (\S+)", out)
                ip = m.group(1) if m else None
            if not ip and shutil.which("route"):
                rc, out = _run(["route", "-n"], timeout=2)
                m = re.search(r"^0\.0\.0\.0\s+(\S+)", out, re.M)
                ip = m.group(1) if m else None
        elif SYSTEM == "Windows":
            rc, out = _run(["route", "print", "0.0.0.0"], timeout=3)
            m = re.search(r"0\.0\.0\.0\s+0\.0\.0\.0\s+(\S+)", out)
            ip = m.group(1) if m else None
    except Exception:
        ip = None
    _gateway_cache.update(ip=ip, ts=now)
    return ip


def _parse_airport(out):
    """Parse `airport -I` output. Returns (wifi_on|None, ssid).

    wifi_on False only on explicit "AirPort: Off". Anything unparseable
    yields None (unknown) — never a confident wrong answer.
    """
    try:
        if re.search(r"^\s*AirPort:\s*Off\s*$", out, re.M):
            return False, ""
        m = re.search(r"^\s*SSID:\s*(.+?)\s*$", out, re.M)
        if m:
            return True, m.group(1)
        if re.search(r"^\s*link auth:\s*\S+", out, re.M):
            return True, ""
    except Exception:
        pass
    return None, ""


def _airport_bin():
    for p in ("/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport",
              "/System/Library/PrivateFrameworks/Apple80211.framework/Resources/airport"):
        try:
            with open(p, "rb"):
                return p
        except Exception:
            continue
    return shutil.which("airport")


def _wifi_device_macos():
    """Device name of the Wi-Fi interface (e.g. en0), or None."""
    try:
        if shutil.which("networksetup"):
            _, out = _run(["networksetup", "-listallhardwareports"], timeout=3)
            m = re.search(r"Hardware Port:\s*Wi-Fi\s*\nDevice:\s*(\S+)", out)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def get_link_info():
    """Return {wifi_on, ssid, ethernet_up}. None = unknown/unsupported (safe)."""
    wifi_on, ssid, eth_up = None, "", None
    try:
        if SYSTEM == "Darwin":
            ap = _airport_bin()
            if ap:
                _, out = _run([ap, "-I"], timeout=3)
                wifi_on, ssid = _parse_airport(out)
            if wifi_on is None:
                # fallback: networksetup against the real device name, not "Wi-Fi"
                dev = _wifi_device_macos()
                if dev and shutil.which("networksetup"):
                    _, out = _run(["networksetup", "-getairportpower", dev], timeout=3)
                    if re.search(r":\s*On\s*$", out, re.M):
                        wifi_on = True
                    elif re.search(r":\s*Off\s*$", out, re.M):
                        wifi_on = False
                    _, out2 = _run(["networksetup", "-getairportnetwork", dev], timeout=3)
                    m = re.search(r"Current Wi-?Fi Network:\s*(.+)", out2)
                    if m:
                        ssid = m.group(1).strip()
                        wifi_on = True
            # ethernet: any en* with active + inet (excluding wifi en0 usually, so check all)
            rc, out = _run(["ifconfig"], timeout=3)
            # crude but tiny: look for an en interface that is up with an IP
            ups = re.findall(r"^(en\d+):.*status: active", out, re.M)
            has_ip = "inet " in out
            if ups and has_ip and wifi_on is False:
                eth_up = True  # wifi off but wired link live
            elif ups and has_ip:
                eth_up = True if len(ups) > 1 else None  # ambiguous, don't claim
        elif SYSTEM == "Linux":
            if shutil.which("nmcli"):
                rc, out = _run(["nmcli", "-t", "-f", "WIFI", "radio"], timeout=3)
                if "enabled" in out.lower():
                    wifi_on = True
                elif "disabled" in out.lower():
                    wifi_on = False
                rc2, out2 = _run(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"], timeout=3)
                m = re.search(r"yes:(.+)", out2)
                if m:
                    ssid = m.group(1).strip()
            elif shutil.which("iwgetid"):
                rc, out = _run(["iwgetid", "-r"], timeout=3)
                if rc == 0 and out.strip():
                    wifi_on, ssid = True, out.strip()
            if shutil.which("ip"):
                rc, out = _run(["ip", "link", "show", "up"], timeout=2)
                # wired usually en*, eth*, enp*
                eth_up = bool(re.search(r"\d+: (en|eth|enp)\S*:.*UP", out))
                if not eth_up:
                    eth_up = None
        elif SYSTEM == "Windows":
            if shutil.which("netsh"):
                rc, out = _run(["netsh", "wlan", "show", "interfaces"], timeout=4)
                m = re.search(r"State\s*:\s*(\S+)", out)
                if m:
                    wifi_on = m.group(1).lower() == "connected"
                    s = re.search(r"SSID\s*:\s*(.+)", out)
                    if s:
                        ssid = s.group(1).strip()
                rc2, out2 = _run(["netsh", "interface", "show", "interface"], timeout=4)
                eth_up = bool(re.search(r"Connected.*(?:Ethernet|Local Area)", out2, re.I)) or None
    except Exception:
        pass
    return {"wifi_on": wifi_on, "ssid": ssid, "ethernet_up": eth_up}


def get_default_iface():
    """Name of the interface carrying internet (e.g. en0), or None. Never raises."""
    try:
        if SYSTEM == "Darwin":
            _, out = _run(["route", "-n", "get", "default"], timeout=2)
            m = re.search(r"interface:\s*(\S+)", out)
            return m.group(1) if m else None
        if SYSTEM == "Linux" and shutil.which("ip"):
            _, out = _run(["ip", "route", "show", "default"], timeout=2)
            m = re.search(r"dev\s+(\S+)", out)
            return m.group(1) if m else None
    except Exception:
        pass
    return None


def get_traffic():
    """Per-interface bytes {iface: [rx, tx]} since boot. Stdlib only. Never raises."""
    res = {}
    try:
        if SYSTEM == "Linux":
            with open("/proc/net/dev") as f:
                for line in f.read().splitlines()[2:]:
                    if ":" not in line:
                        continue
                    name, vals = line.split(":", 1)
                    flds = vals.split()
                    if len(flds) >= 9:
                        res[name.strip()] = [int(flds[0]), int(flds[8])]
        elif SYSTEM == "Darwin":
            _, out = _run(["netstat", "-ib"], timeout=3)
            for line in out.splitlines():
                p = line.split()
                # Name Mtu ... Ipkts Ierrs Ibytes Opkts Oerrs Obytes ...
                if len(p) >= 10 and p[0] not in ("Name", "lo0", "lo*") and "<Link" in line:
                    try:
                        res[p[0]] = [int(p[6]), int(p[9])]
                    except Exception:
                        continue
        elif SYSTEM == "Windows":
            _, out = _run(["netstat", "-e"], timeout=3)
            m = re.search(r"Bytes\s+(\d+)\s+(\d+)", out)
            if m:
                res["all"] = [int(m.group(1)), int(m.group(2))]
    except Exception:
        pass
    return res


def active_medium(link, default_iface=None):
    """Plain-English medium: 'WiFi (SSID)' | 'Ethernet' | 'WiFi + Ethernet' | 'WiFi Off' | 'Unknown'."""
    try:
        wifi = link.get("wifi_on")
        eth = link.get("ethernet_up")
        ssid = (link.get("ssid") or "").strip()
        if wifi is False and not eth:
            return "WiFi Off"
        if wifi and eth:
            return "WiFi + Ethernet"
        if eth:
            return "Ethernet"
        if wifi:
            return "WiFi (%s)" % ssid if ssid else "WiFi"
        # unknown OS flags -> fall back to default interface name
        if default_iface:
            return "Via %s" % default_iface
        return "Unknown"
    except Exception:
        return "Unknown"


def get_local_ip():
    """LAN IP without sending traffic (UDP connect sends nothing). None if offline."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def usable_addr(ip):
    """False for missing, self-assigned (169.254.x) or loopback addresses.

    A WiFi that is "on" but holds one of these cannot carry traffic —
    typically a failed DHCP lease or no route. Pure function, testable.
    """
    try:
        if not ip or not isinstance(ip, str):
            return False
        ip = ip.strip()
        parts = ip.split(".")
        if len(parts) != 4:
            return False
        a, b = int(parts[0]), int(parts[1])
        if a == 127 or (a == 169 and b == 254) or ip == "0.0.0.0":
            return False
        return all(0 <= int(p) <= 255 for p in parts)
    except Exception:
        return False


def get_dns_servers():
    """Resolver IPs. Never raises, returns [] on failure."""
    out = []
    try:
        if SYSTEM == "Darwin" and shutil.which("scutil"):
            _, txt = _run(["scutil", "--dns"], timeout=3)
            for m in re.finditer(r"nameserver\[\d+\]\s*:\s*(\S+)", txt):
                if m.group(1) not in out:
                    out.append(m.group(1))
        elif SYSTEM == "Linux":
            try:
                with open("/etc/resolv.conf") as f:
                    for line in f:
                        m = re.match(r"\s*nameserver\s+(\S+)", line)
                        if m and m.group(1) not in out:
                            out.append(m.group(1))
            except Exception:
                pass
        elif SYSTEM == "Windows" and shutil.which("nslookup"):
            _, txt = _run(["nslookup", "localhost"], timeout=4)
            m = re.search(r"Address:\s*(\S+)", txt)
            if m:
                out.append(m.group(1))
    except Exception:
        pass
    return out[:3]


def _http_json(url, timeout=5):
    """Minimal GET->dict via stdlib. None on any failure (offline-safe)."""
    try:
        import json
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "UptimeMonitor/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            return json.loads(r.read(200000).decode("utf-8", "replace"))
    except Exception:
        return None


def get_public_ip(timeout=5):
    """Public IPv4 as seen by the internet. Tries 3 providers. None if offline."""
    try:
        import urllib.request
        for url in ("https://api.ipify.org", "https://icanhazip.com", "https://ifconfig.me/ip"):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "UptimeMonitor/1.0"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    ip = r.read(64).decode("utf-8", "replace").strip()
                    if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
                        return ip
            except Exception:
                continue
    except Exception:
        pass
    return None


def get_isp_details(ip, timeout=6):
    """ISP/ASN/geo for an IP via free ip-api.com (no key). {} on failure.

    Cached by caller (only call when IP changes or every ~6h: free tier is
    45 req/min, we use ~4/day). Fields: isp, org, as, country, region, city.
    """
    try:
        d = _http_json("http://ip-api.com/json/%s?fields=status,query,isp,org,as,country,regionName,city,reverse,mobile,proxy,hosting" % ip,
                       timeout=timeout)
        if not d or d.get("status") != "success":
            return {}
        return {k: str(d.get(k, ""))[:120] for k in
                ("query", "isp", "org", "as", "country", "regionName", "city", "reverse")}
    except Exception:
        return {}
