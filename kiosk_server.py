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
import urllib.request
import urllib.parse
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
# [DEPRECATED — now using Google Calendar API directly]
# Credentials stored in /home/r-server/.env on r-server

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
    """Fetch HAPPENING NOW + next 3 upcoming events from Google Calendar API.
    Returns: {"now": [...active events], "next": [...upcoming events], "error": null}
    Uses /home/kiosk/google_token.json (OAuth2 token for aaryantahir8918@gmail.com).
    """
    global _last_calendar_refresh, _cached_calendar
    if time.time() - _last_calendar_refresh < 180:   # cache 3 min
        return
    _last_calendar_refresh = time.time()

    try:
        import urllib.request

        token_path = "/home/kiosk/google_token.json"
        with open(token_path) as f:
            token_data = json.load(f)
        access_token = token_data.get("token")
        if not access_token:
            _cached_calendar = {"now": [], "next": [], "error": "no access token"}
            return

        # Build ISO time range: today 00:00 to 7 days ahead 23:59 in ET (UTC-4)
        now_ts = time.time()
        from datetime import datetime, timezone, timedelta
        et_offset = timedelta(hours=-4)
        et_tz = timezone(et_offset)
        now_dt = datetime.fromtimestamp(now_ts, tz=et_tz)
        start_iso = now_dt.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S%z")
        end_dt = now_dt + timedelta(days=7)
        end_iso = end_dt.replace(hour=23, minute=59, second=59, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S%z")

        # Google Calendar API: list events on primary calendar
        import urllib.parse
        params = urllib.parse.urlencode({
            "timeMin": start_iso,
            "timeMax": end_iso,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "20",
        })
        url = f"https://www.googleapis.com/calendar/v3/calendars/primary/events?{params}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            gdata = json.loads(resp.read())

        now_events = []
        next_events = []

        for ev in gdata.get("items", []):
            summary = ev.get("summary", "—")
            start_info = ev.get("start", {})
            end_info = ev.get("end", {})

            # dateTime = datetime with TZ; date = all-day (whole day)
            start_dt_str = start_info.get("dateTime") or start_info.get("date")
            end_dt_str = end_info.get("dateTime") or end_info.get("date")

            if not start_dt_str:
                continue

            # Parse start/end
            def parse_google_dt(s):
                # "2026-08-19T14:00:00-04:00" or "2026-08-19"
                if "T" in s:
                    try:
                        return datetime.fromisoformat(s.replace("Z", "+00:00"))
                    except Exception:
                        return None
                else:
                    try:
                        return datetime.strptime(s, "%Y-%m-%d")
                    except Exception:
                        return None

            start_dt = parse_google_dt(start_dt_str)
            end_dt = parse_google_dt(end_dt_str) if end_dt_str else None

            if not start_dt:
                continue

            # Compute Unix timestamps for comparison
            start_ts = start_dt.timestamp() if start_dt else None
            end_ts = end_dt.timestamp() if end_dt else start_ts

            # Format display time
            def fmt_time(dt):
                if not dt:
                    return "—"
                if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
                    return dt.strftime("%b %-d")   # all-day: "Aug 19"
                return dt.strftime("%-I:%M%p")    # "3:00pm"

            time_str = fmt_time(start_dt)

            ev_data = {
                "summary": summary,
                "start": time_str,
                "uid": ev.get("id", ""),
                "_start_ts": start_ts,
                "_end_ts": end_ts,
            }

            # HAPPENING NOW: started before/now, ends after now
            if start_ts and start_ts <= now_ts and end_ts and end_ts > now_ts:
                now_events.append(ev_data)
            elif start_ts and start_ts <= now_ts and not end_ts:
                # All-day event started today — treat as happening now
                now_events.append(ev_data)
            else:
                next_events.append(ev_data)

        # Sort both lists by start timestamp
        def get_ts(e):
            return e.get("_start_ts") or 0

        now_events.sort(key=get_ts)
        next_events.sort(key=get_ts)

        # Format "now" events: show with "Since X" if ongoing
        def fmt_now(ev):
            if ev.get("_start_ts"):
                start_dt = datetime.fromtimestamp(ev["_start_ts"], tz=et_tz)
                return f"Since {start_dt.strftime('%-I:%M%p').lower()}"
            return ev.get("start", "—")

        now_formatted = [
            {"summary": ev["summary"], "start": fmt_now(ev), "uid": ev["uid"]}
            for ev in now_events
        ]
        next_formatted = [
            {"summary": ev["summary"], "start": ev["start"], "uid": ev["uid"]}
            for ev in next_events[:3]
        ]

        _cached_calendar = {
            "now": now_formatted,
            "next": next_formatted,
            "error": None,
        }

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
