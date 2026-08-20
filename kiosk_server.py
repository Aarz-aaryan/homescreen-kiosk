#!/usr/bin/env python3
"""
homescreen preview server.
Serves /opt/kiosk/index.html + dynamically generated JSON endpoints.

- /monitors.json:   parsed from r-server Uptime Kuma sqlite3 via SSH
- /identity.json:   hostname, uptime, kernel, Tailscale status
- /rserver-stats.json: CPU/MEM/disk/containers/uptime from rs-stats.sh
- /hermes-health.json: tailscale peer counts, r-server reachability
- /weather.json:     Open-Meteo current weather (free, no auth)
- /audio.json:      mpv lofi stream status
- /calendar.json:    Next 3 upcoming events from Nextcloud CalDAV
- /backgrounds/list.json: pre-validated GIF list (magic-byte checked)
- /backgrounds/<name>:  raw GIF bytes (only from validated list)
- /index.html: static

Bind: 127.0.0.1:8088 (kiosk-local only)
"""
import http.server
import json
import subprocess
import os
import re
import time
from pathlib import Path

KIOSK_DIR = Path("/opt/kiosk")
INDEX_HTML = KIOSK_DIR / "index.html"
HOST = "127.0.0.1"
PORT = 8088

# ─── GIF VALIDATION ────────────────────────────────────────────────────────────
GIF_MAGIC = (b"GIF87a", b"GIF89a")

def is_valid_gif(path: Path) -> bool:
    """True if file exists, ends in .gif/.webp, AND starts with a GIF magic header."""
    if not path.is_file():
        return False
    suffix = path.suffix.lower()
    if suffix not in (".gif", ".webp"):
        return False
    try:
        with open(path, "rb") as f:
            header = f.read(6)
        return header.startswith(b"GIF87a") or header.startswith(b"GIF89a")
    except Exception:
        return False

# Pre-validate all GIFs in gifs/ on startup — prevents broken/black frames at runtime
_gifs_dir = KIOSK_DIR / "gifs"
_valid_gifs: list[str] = []

def _refresh_valid_gifs():
    global _valid_gifs
    _valid_gifs = sorted([
        f.name for f in _gifs_dir.iterdir()
        if is_valid_gif(f)
    ])
    print(f"[GIF] {len(_valid_gifs)}/{len(list(_gifs_dir.glob('*')))} validated GIFs")

_refresh_valid_gifs()

# ─── CACHE ────────────────────────────────────────────────────────────────────
_last_monitors_refresh  = 0
_last_identity_refresh  = 0
_last_rserver_refresh  = 0
_last_hermes_refresh   = 0
_last_weather_refresh  = 0
_last_calendar_refresh  = 0

_cached_monitors   = {"monitors": [], "error": "initializing"}
_cached_identity  = {"hostname": "homescreen", "kernel": "linux", "uptime": "loading", "tailscale": "—"}
_cached_rserver   = {"cpu": "—", "mem": "—", "disk": "—", "uptime": "—", "containers": "—"}
_cached_hermes    = {"status": "—", "uptime": "—", "active_crons": "—", "profile": "—"}
_cached_weather   = {"temp": "—", "cond": "—", "wind": "—", "error": None}
_cached_calendar  = {"events": [], "error": None}

# ─── SSH COMMANDS ─────────────────────────────────────────────────────────────
RS_MONITORS_CMD = [
    "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8",
    "-i", "/home/kiosk/.ssh/id_ed25519",
    "r-server@100.84.224.18",
    "sudo docker exec uptime-kuma sqlite3 /app/data/kuma.db \"SELECT name,active FROM monitor;\""
]
RS_STATS_CMD = [
    "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8",
    "-i", "/home/kiosk/.ssh/id_ed25519",
    "r-server@100.84.224.18",
    "/usr/local/bin/rs-stats.sh"
]

# CalDAV: Nextcloud personal calendar via r-server as proxy
# Credentials stored in /home/r-server/.env on r-server
NC_CAL_CMD = [
    "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
    "-i", "/home/kiosk/.ssh/id_ed25519",
    "r-server@100.84.224.18",
    (
        "source /home/r-server/.env 2>/dev/null && "
        "curl -s -u \"${NEXTCLOUD_ADMIN_USER}:${NEXTCLOUD_ADMIN_PASSWORD}\" "
        "-X REPORT \"http://localhost:9080/remote.php/dav/calendars/${NEXTCLOUD_ADMIN_USER}/personal/\" "
        "-H 'Content-Type: application/xml; charset=utf-8' "
        "-H 'Depth: 1' "
        "--data-binary @- << 'CALREQ' "
        "<?xml version=\"1.0\" encoding=\"UTF-8\" ?>"
        "<C:calendar-query xmlns:D=\"DAV:\" xmlns:C=\"urn:ietf:params:xml:ns:caldav\">"
        "  <D:prop><C:calendar-data/></D:prop>"
        "  <C:filter>"
        "    <C:comp-filter name=\"VCALENDAR\">"
        "      <C:comp-filter name=\"VEVENT\"/>"
        "    </C:comp-filter>"
        "  </C:filter>"
        "</C:calendar-query>"
        "CALREQ"
    )
]

# ─── DATA FETCHERS ────────────────────────────────────────────────────────────
def refresh_monitors():
    global _last_monitors_refresh, _cached_monitors
    if time.time() - _last_monitors_refresh < 30:
        return
    _last_monitors_refresh = time.time()
    try:
        out = subprocess.run(RS_MONITORS_CMD, timeout=15, capture_output=True, text=True).stdout.strip()
        monitors = []
        for line in out.splitlines():
            if "|" not in line:
                continue
            parts = line.strip().split("|")
            name = parts[0].strip()
            active = parts[-1].strip() == "1"
            if name:
                monitors.append({"name": name, "active": active})
        _cached_monitors = {"monitors": monitors, "ts": time.time()}
    except subprocess.TimeoutExpired:
        _cached_monitors = {"monitors": [], "error": "r-server timeout"}
    except Exception as e:
        _cached_monitors = {"monitors": [], "error": type(e).__name__}

def refresh_identity():
    global _last_identity_refresh, _cached_identity
    if time.time() - _last_identity_refresh < 60:
        return
    _last_identity_refresh = time.time()
    try:
        hostname = subprocess.run(["hostname"], capture_output=True, text=True, timeout=2).stdout.strip() or "homescreen"
        kernel   = subprocess.run(["uname", "-r"], capture_output=True, text=True, timeout=2).stdout.strip()
        try:
            with open("/proc/uptime") as f:
                secs = float(f.read().split()[0])
            days  = int(secs // 86400)
            hours = int((secs % 86400) // 3600)
            mins  = int((secs % 3600) // 60)
            uptime = f"{days}d {hours}h" if days > 0 else f"{hours}h {mins}m" if hours > 0 else f"{mins}m"
        except Exception:
            uptime = "unknown"
        ts_state = "—"
        try:
            ts_out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=3).stdout
            if ts_out:
                d = json.loads(ts_out)
                ts_state = "online" if d.get("BackendState") == "Running" else d.get("BackendState", "—").lower()
        except Exception:
            pass
        _cached_identity = {"hostname": hostname, "kernel": kernel, "uptime": uptime, "tailscale": ts_state, "ts": time.time()}
    except Exception as e:
        _cached_identity = {**_cached_identity, "error": type(e).__name__}

def refresh_rserver():
    global _last_rserver_refresh, _cached_rserver
    if time.time() - _last_rserver_refresh < 20:
        return
    _last_rserver_refresh = time.time()
    try:
        out = subprocess.run(RS_STATS_CMD, timeout=15, capture_output=True, text=True).stdout.strip()
        parts = out.split("|")
        if len(parts) >= 5:
            _cached_rserver = {
                "cpu": parts[0].strip(), "mem": parts[1].strip(),
                "disk": parts[2].strip(), "containers": parts[3].strip(),
                "uptime": parts[4].strip(), "ts": time.time(),
            }
        else:
            _cached_rserver = {**_cached_rserver, "error": "bad output: " + out[:80]}
    except subprocess.TimeoutExpired:
        _cached_rserver = {**_cached_rserver, "error": "timeout"}
    except Exception as e:
        _cached_rserver = {**_cached_rserver, "error": type(e).__name__}

def refresh_hermes():
    global _last_hermes_refresh, _cached_hermes
    if time.time() - _last_hermes_refresh < 30:
        return
    _last_hermes_refresh = time.time()
    try:
        ts_out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5).stdout
        online = total = 0
        rserver = False
        if ts_out:
            d = json.loads(ts_out)
            for k, p in d.get("Peer", {}).items():
                total += 1
                if p.get("Online"):
                    online += 1
            rserver = any("100.84.224.18" in str(p.get("TailscaleIPs", [])) for p in d.get("Peer", {}).values())
        status = "online" if online >= 2 else "degraded"
        try:
            with open("/proc/uptime") as f:
                secs = float(f.read().split()[0])
            hermes_up = f"{int(secs // 3600)}h {int((secs % 3600) // 60)}m"
        except Exception:
            hermes_up = "unknown"
        _cached_hermes = {
            "status": status, "tailscale_online": f"{online}/{total}",
            "rserver_online": rserver, "homescreen_uptime": hermes_up,
            "ts": time.time(),
        }
    except Exception as e:
        _cached_hermes = {**_cached_hermes, "status": "unknown", "error": type(e).__name__}

def refresh_weather():
    """Open-Meteo free weather API — Philadelphia coordinates."""
    global _last_weather_refresh, _cached_weather
    if time.time() - _last_weather_refresh < 300:   # cache 5 min
        return
    _last_weather_refresh = time.time()
    try:
        url = "https://api.open-meteo.com/v1/forecast?latitude=39.9526&longitude=-75.1652&current_weather=true"
        r = subprocess.run(["curl", "-s", "--max-time", "10", url], capture_output=True, text=True, timeout=12)
        if r.returncode == 0:
            d = json.loads(r.stdout)
            cw = d.get("current_weather", {})
            code = cw.get("weathercode", 0)
            _cached_weather = {
                "temp": str(cw.get("temperature", "—")),
                "cond": _weather_name(code),
                "wind": str(cw.get("windspeed", "—")),
                "error": None, "ts": time.time(),
            }
    except Exception as e:
        _cached_weather = {**_cached_weather, "error": type(e).__name__}

WEATHER_CODES = {
    0: "clear", 1: "mainly_clear", 2: "partly_cloudy", 3: "cloudy",
    45: "fog", 48: "fog",
    51: "drizzle", 53: "drizzle", 55: "drizzle",
    61: "rain", 63: "rain", 65: "rain",
    71: "snow", 73: "snow", 75: "snow",
    80: "showers", 81: "showers", 82: "showers",
    95: "thunder", 96: "thunder", 99: "thunder",
}

def _weather_name(code):
    return WEATHER_CODES.get(code, f"code{code}")

def refresh_calendar():
    """Fetch HAPPENING NOW + next 3 upcoming events from Nextcloud CalDAV personal calendar.
    Returns: {"now": [...active events], "next": [...upcoming events], "error": null}
    """
    global _last_calendar_refresh, _cached_calendar
    if time.time() - _last_calendar_refresh < 180:   # cache 3 min
        return
    _last_calendar_refresh = time.time()
    try:
        out = subprocess.run(
            ["bash", "-c",
             "source /home/r-server/.env 2>/dev/null && "
             "curl -s -u \"${NEXTCLOUD_ADMIN_USER}:${NEXTCLOUD_ADMIN_PASSWORD}\" "
             "-X REPORT \"http://localhost:9080/remote.php/dav/calendars/${NEXTCLOUD_ADMIN_USER}/personal/\" "
             "-H 'Content-Type: application/xml; charset=utf-8' "
             "-H 'Depth: 1' "
             "--data-raw '<?xml version=\"1.0\" encoding=\"UTF-8\" ?>"
             "<C:calendar-query xmlns:D=\"DAV:\" xmlns:C=\"urn:ietf:params:xml:ns:caldav\">"
             "<D:prop><C:calendar-data/></D:prop>"
             "<C:filter>"
             "<C:comp-filter name=\"VCALENDAR\"><C:comp-filter name=\"VEVENT\"/></C:comp-filter>"
             "</C:filter></C:calendar-query>'"],
            timeout=20, capture_output=True, text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        ).stdout

        now_events = []
        next_events = []
        now_ts = time.time()

        # Extract all VCALENDAR blocks
        for block in re.findall(r'BEGIN:VCALENDAR\s+.*?END:VCALENDAR', out, re.DOTALL):
            vevents = re.findall(r'BEGIN:VEVENT\s+.*?END:VEVENT', block, re.DOTALL)
            for ve in vevents:
                summary_m = re.search(r'SUMMARY[;TZ=]*:(.*)', ve)
                dtstart_m = re.search(r'DTSTART(?:[^\n]*):([^\n]+)', ve)
                dtend_m   = re.search(r'DTEND(?:[^\n]*):([^\n]+)', ve)
                uid_m     = re.search(r'UID:(.*)', ve)

                summary = summary_m.group(1).strip() if summary_m else "—"

                # Parse start time
                start_raw = dtstart_m.group(1).strip() if dtstart_m else None
                end_raw   = dtend_m.group(1).strip()   if dtend_m   else None

                start_ts = _parse_ical_date(start_raw) if start_raw else None
                end_ts   = _parse_ical_date(end_raw)   if end_raw   else start_ts

                # Skip events that ended more than 5 min ago
                if end_ts and end_ts < now_ts - 300:
                    continue

                # Format display time
                if start_ts:
                    start_fmt = _format_event_time(start_ts, end_ts, start_raw)
                else:
                    start_fmt = start_raw or "—"

                ev = {
                    "summary": summary,
                    "start":   start_fmt,
                    "uid":     uid_m.group(1).strip() if uid_m else "",
                    "_start":  start_ts,
                    "_end":    end_ts,
                }

                # HAPPENING NOW: started before/now, ends after now
                if start_ts and start_ts <= now_ts and end_ts and end_ts > now_ts:
                    now_events.append(ev)
                elif start_ts and start_ts <= now_ts and not end_ts:
                    # All-day or no end: show if started before now
                    now_events.append(ev)
                else:
                    next_events.append(ev)

        # Sort both lists by start timestamp
        def get_ts(e):
            return e.get("_start") or 0

        now_events.sort(key=get_ts)
        next_events.sort(key=get_ts)

        _cached_calendar = {
            "now":  now_events,
            "next": next_events[:3],
            "error": None,
        }

    except subprocess.TimeoutExpired:
        _cached_calendar = {"now": [], "next": [], "error": "cal timeout"}
    except Exception as e:
        _cached_calendar = {"now": [], "next": [], "error": type(e).__name__}

def _parse_ical_date(s):
    """Parse iCal DATE or DATETIME (with or without TZ) into Unix timestamp."""
    if not s:
        return None
    s = s.strip().replace("\\n", "")
    # DATE: YYYYMMDD
    # DATETIME: YYYYMMDDTHHMMSS or YYYYMMDDTHHMMSSZ
    m = re.match(r'^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2}))?', s)
    if not m:
        return None
    try:
        from datetime import datetime, timezone, timedelta
        naive = datetime(int(m[1]), int(m[2]), int(m[3]),
                        hour=int(m[4] or 0), minute=int(m[5] or 0), second=int(m[6] or 0))
        if s.endswith('Z'):
            naive = naive.replace(tzinfo=timezone.utc)
        # Assume local time if no TZ; use UTC as fallback
        try:
            import calendar
            return calendar.timegm(naive.utctimetuple())
        except Exception:
            return int(naive.timestamp())
    except Exception:
        return None

def _format_event_time(start_ts, end_ts, raw):
    """Human-readable time for an event."""
    from datetime import datetime
    try:
        start_dt = datetime.utcfromtimestamp(start_ts)
        now = datetime.utcnow()
        diff = (start_dt.date() - now.date()).days

        time_str = start_dt.strftime("%-I:%M%p").lower()
        if diff == 0:
            return f"Today {time_str}"
        elif diff == 1:
            return f"Tomorrow {time_str}"
        elif diff == -1:
            return f"Yesterday {time_str}"
        else:
            return start_dt.strftime("%a %b %-d {time_str}").format(time_str=time_str)
    except Exception:
        return raw or "—"

# ─── HTTP HANDLER ─────────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass   # keep stderr quiet

    def _json(self, data, cache=True):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if not cache else "public, max-age=60")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        # ── static pages ──────────────────────────────────────────────────
        if path in ("/", "/index.html"):
            refresh_monitors()
            refresh_identity()
            try:
                body = INDEX_HTML.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self.send_response(500); self.end_headers()
                self.wfile.write(b"index.html missing")
            return

        # ── data endpoints ─────────────────────────────────────────────────
        if path == "/monitors.json":
            refresh_monitors()
            self._json(_cached_monitors)
            return

        if path == "/identity.json":
            refresh_identity()
            self._json(_cached_identity)
            return

        if path == "/rserver-stats.json":
            refresh_rserver()
            self._json(_cached_rserver)
            return

        if path == "/hermes-health.json":
            refresh_hermes()
            self._json(_cached_hermes)
            return

        if path == "/weather.json":
            refresh_weather()
            self._json(_cached_weather)
            return

        if path == "/calendar.json":
            refresh_calendar()
            self._json(_cached_calendar)
            return

        if path == "/audio.json":
            mpv_running = False
            try:
                mpv_running = subprocess.run(
                    ["pgrep", "-f", "mpv.*somafm|mpv.*lofi|mpv.*groovesalad"],
                    capture_output=True
                ).returncode == 0
            except Exception:
                pass
            track = "—"
            try:
                log = Path("/home/kiosk/.mpv.log")
                if log.exists():
                    titles = [l.split("icy-title:", 1)[1].strip()
                              for l in log.read_text(errors="replace").splitlines()
                              if "icy-title:" in l]
                    if titles:
                        track = titles[-1]
            except Exception:
                pass
            self._json({
                "playing": mpv_running, "track": track,
                "station": "SomaFM Groove Salad",
                "url": "https://ice1.somafm.com/groovesalad-128-mp3",
            })
            return

        # ── backgrounds ────────────────────────────────────────────────────
        if path == "/backgrounds/list.json":
            _refresh_valid_gifs()
            self._json({"backgrounds": _valid_gifs})
            return

        if path.startswith("/backgrounds/"):
            fname = path[len("/backgrounds/"):]
            if "/" in fname or ".." in fname:
                self.send_response(400); self.end_headers()
                self.wfile.write(b"bad path"); return
            if fname not in _valid_gifs:
                self.send_response(404); self.end_headers()
                self.wfile.write(b"not found or not a valid GIF"); return
            fpath = _gifs_dir / fname
            try:
                body = fpath.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/gif")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(500); self.end_headers()
                self.wfile.write(str(e).encode())
            return

        # ── 404 ───────────────────────────────────────────────────────────
        self.send_response(404); self.end_headers()
        self.wfile.write(b"not found")


# ─── STARTUP ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    os.makedirs(KIOSK_DIR, exist_ok=True)
    _refresh_valid_gifs()
    print(f"[GIF] Serving {len(_valid_gifs)} validated backgrounds", flush=True)
    refresh_monitors()
    refresh_identity()
    refresh_calendar()   # warm cache on startup
    server = http.server.HTTPServer((HOST, PORT), Handler)
    print(f"kiosk-web listening on http://{HOST}:{PORT}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
