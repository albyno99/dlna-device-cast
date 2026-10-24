#!/usr/bin/env python3
"""
devicecast.py - cast the macOS screen to a TV.

If the TV runs the Device Cast receiver app, control is handed to server.py (low latency).
Otherwise the screen is sent over plain DLNA:
  1. Find the TV with SSDP (UPnP MediaRenderer).
  2. Serve an HTTP stream; when the TV connects, ffmpeg captures the screen (avfoundation)
     and encodes it on the fly to MPEG-TS (H.264 + AAC).
  3. Tell the TV to play that URL via SOAP (AVTransport).

Requires macOS, python3 and ffmpeg (brew install ffmpeg). macOS asks for the Screen Recording
permission on first use.

Examples:
  python3 devicecast.py list                 # list DLNA renderers on the network
  python3 devicecast.py devices              # list capturable screens and audio devices
  python3 devicecast.py cast                 # start casting (uses the only TV found, if just one)
  python3 devicecast.py cast --tv "Living Room"
  python3 devicecast.py cast --audio BlackHole --width 1920 --fps 30 --bitrate 8M
  python3 devicecast.py stop --tv 192.0.2.10

The chosen TV is remembered by UPnP UDN (not by IP) in data/config.json.
"""

import argparse
import atexit
import html
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import deskpad
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SSDP_ADDR = ("239.255.255.250", 1900)
ST_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"
SVC_AVT = "urn:schemas-upnp-org:service:AVTransport:1"
NS = {"d": "urn:schemas-upnp-org:device-1-0"}
STREAM_PATH = "/screen.ts"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(ROOT, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
USER_AGENT = "devicecast/1.0 UPnP/1.0"
MAX_DESCRIPTION_BYTES = 512 * 1024
MIME = "video/mpeg"
# OP=00 (no seeking), CI=0, FLAGS: streaming, no duration (live)
CONTENT_FEATURES = "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"


def log(msg):
    # stderr keeps the stdout of `list --json` pure JSON
    print(time.strftime("[%H:%M:%S] ") + msg, file=sys.stderr, flush=True)


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(**kw):
    cfg = load_config()
    cfg.update(kw)
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError as e:
        log(f"(cannot save {CONFIG_PATH}: {e})")


# Network

def local_ip_towards(target="192.0.2.1"):
    """Local IP used to reach `target`.

    connect() on a UDP socket only selects a route, no packet is sent. The default is a
    documentation address (RFC 5737), which resolves to the default route.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


LOCAL_NET_HINT = ("Local Network permission denied to the app running the cast. System Settings > "
                  "Privacy & Security > Local Network: enable the app (Terminal or Device Cast) and try again.")


def ssdp_discover(timeout=3.0, local_ip=None, unicast_targets=()):
    """Return the LOCATION URLs of the MediaRenderers found.

    Besides multicast, the IPs in `unicast_targets` (e.g. the last TV used) are queried directly,
    for routers that block multicast between Wi-Fi clients.
    """
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {ST_RENDERER}\r\n\r\n"
    ).encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    if local_ip:
        # On macOS, binding to the interface avoids "No route to host" on multicast.
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
            s.bind((local_ip, 0))
        except OSError:
            pass
    s.settimeout(0.5)
    locations = []
    sent = 0
    multicast_ok = True
    try:
        targets = [SSDP_ADDR] + [(ip, 1900) for ip in unicast_targets if ip]
        for i in range(3):
            for t in targets:
                try:
                    s.sendto(msg, t)
                    sent += 1
                except OSError as e:
                    if t == SSDP_ADDR:
                        multicast_ok = False
                        if i == 0:
                            log(f"SSDP multicast failed ({e.strerror}). " + LOCAL_NET_HINT)
                    elif i == 0:
                        log(f"SSDP request to {t[0]} failed: {e}")
            time.sleep(0.1)
        if not multicast_ok and local_ip:
            # Fallback: unicast M-SEARCH to the whole /24 (254 small UDP packets).
            prefix = local_ip.rsplit(".", 1)[0]
            log(f"trying unicast to {prefix}.1-254")
            for host in range(1, 255):
                try:
                    s.sendto(msg, (f"{prefix}.{host}", 1900))
                    sent += 1
                except OSError:
                    pass
        if not sent:
            return []
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = s.recvfrom(65535)
            except socket.timeout:
                continue
            for line in data.decode("utf-8", "replace").split("\r\n"):
                if line.lower().startswith("location:"):
                    loc = line.split(":", 1)[1].strip()
                    if loc in locations:
                        continue
                    # A LOCATION pointing anywhere but the responder would make us fetch from,
                    # and later stream to, a host that never answered the search.
                    u = urllib.parse.urlparse(loc)
                    if u.scheme != "http" or u.hostname != addr[0]:
                        log(f"ignoring SSDP response from {addr[0]} with LOCATION {loc}")
                        continue
                    locations.append(loc)
    finally:
        s.close()
    return locations


def http_get(url, timeout=5, limit=MAX_DESCRIPTION_BYTES):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read(limit + 1)
    if len(body) > limit:
        raise ValueError(f"response larger than {limit} bytes")
    return body


def parse_xml(data):
    """ET.fromstring without DTDs: refuses documents that could carry entity expansion."""
    # Strip NULs so a UTF-16 document cannot hide the markers.
    if re.search(rb"<!(DOCTYPE|ENTITY)", data.replace(b"\x00", b""), re.I):
        raise ValueError("DTD or entity declarations are not accepted")
    return ET.fromstring(data)


def parse_renderer(location):
    """Read the UPnP description; return name, model, IP and AVTransport control URL."""
    try:
        if urllib.parse.urlparse(location).scheme != "http":
            raise ValueError("only http:// locations are accepted")
        root = parse_xml(http_get(location))
    except Exception as e:  # noqa: BLE001
        log(f"  (skipping {location}: {e})")
        return None
    base = root.findtext("d:URLBase", default=None, namespaces=NS) or location
    for dev in root.iter("{urn:schemas-upnp-org:device-1-0}device"):
        if dev.findtext("d:deviceType", default="", namespaces=NS) != ST_RENDERER:
            continue
        control = None
        for svc in dev.iter("{urn:schemas-upnp-org:device-1-0}service"):
            if svc.findtext("d:serviceType", default="", namespaces=NS) == SVC_AVT:
                control = urllib.parse.urljoin(base, svc.findtext("d:controlURL", default="", namespaces=NS))
        if not control:
            continue
        return {
            "udn": dev.findtext("d:UDN", default="", namespaces=NS),
            "name": dev.findtext("d:friendlyName", default="?", namespaces=NS),
            "manufacturer": dev.findtext("d:manufacturer", default="", namespaces=NS),
            "model": dev.findtext("d:modelName", default="", namespaces=NS),
            "ip": urllib.parse.urlparse(location).hostname,
            "location": location,
            "control": control,
        }
    return None


def find_renderers(local_ip, timeout=3.0, location=None):
    """Find renderers. With `location`, skip discovery and read that description directly."""
    found = []
    cfg = load_config()
    locations = [location] if location else ssdp_discover(timeout=timeout, local_ip=local_ip,
                                                           unicast_targets=[cfg.get("tv_ip")])
    if not locations and not location and cfg.get("tv_location"):
        log(f"no SSDP response, trying the last TV used: {cfg['tv_location']}")
        locations = [cfg["tv_location"]]
    for loc in locations:
        r = parse_renderer(loc)
        if r and all(r["control"] != f["control"] for f in found):
            found.append(r)
    return found


def describe(r):
    return f"{r['name']} ({r['model']}, {r['ip']})"


def pick_renderer(renderers, wanted, interactive=False):
    """Pick the TV: --tv (name/IP/model/UDN), then the saved one, then the only one found, then ask."""
    if not renderers:
        sys.exit("No DLNA renderer found. Is the TV on and on the same network as this computer?")
    if wanted:
        w = wanted.lower()
        matches = [r for r in renderers
                   if w in r["name"].lower() or w == r["ip"] or w in r["model"].lower() or w == r["udn"].lower()]
        if not matches:
            sys.exit(f"No TV matches '{wanted}'. Found:\n  " + "\n  ".join(describe(r) for r in renderers))
        return matches[0]
    if len(renderers) == 1:
        return renderers[0]
    saved = load_config().get("tv_udn")
    for r in renderers:
        if saved and r["udn"] == saved:
            log(f"Using the saved TV: {r['name']}")
            return r
    if interactive and sys.stdin.isatty():
        print("Several TVs found:")
        for i, r in enumerate(renderers, 1):
            print(f"  {i}) {describe(r)}")
        while True:
            try:
                n = int(input("Which one? [number] "))
                if 1 <= n <= len(renderers):
                    return renderers[n - 1]
            except (ValueError, EOFError):
                sys.exit("Cancelled.")
    sys.exit("Several TVs found, pick one with --tv NAME or --tv IP:\n  "
             + "\n  ".join(describe(r) for r in renderers))


# SOAP / UPnP

def soap(control_url, action, args=None, timeout=10):
    args = args or {}
    body = "".join(f"<{k}>{html.escape(str(v), quote=False)}</{k}>" for k, v in args.items())
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{SVC_AVT}">{body}</u:{action}>'
        "</s:Body></s:Envelope>"
    ).encode("utf-8")
    req = urllib.request.Request(
        control_url,
        data=envelope,
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{SVC_AVT}#{action}"',
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        m = re.search(r"<errorCode>(\d+)</errorCode>.*?<errorDescription>(.*?)</errorDescription>", text, re.S)
        detail = f"UPnP {m.group(1)}: {m.group(2)}" if m else text[:200]
        raise RuntimeError(f"{action} rejected by the TV ({detail})") from None


def didl_metadata(url, title):
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
        '<item id="0" parentID="-1" restricted="1">'
        f"<dc:title>{html.escape(title)}</dc:title>"
        "<upnp:class>object.item.videoItem</upnp:class>"
        f'<res protocolInfo="http-get:*:{MIME}:{CONTENT_FEATURES}">{html.escape(url)}</res>'
        "</item></DIDL-Lite>"
    )


def transport_state(control_url):
    txt = soap(control_url, "GetTransportInfo", {"InstanceID": 0})
    m = re.search(r"<CurrentTransportState>(.*?)</CurrentTransportState>", txt)
    return m.group(1) if m else "?"


# ffmpeg

def avfoundation_devices():
    """Return (video, audio): lists of (index, name) reported by ffmpeg."""
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, text=True,
    )
    video, audio, section = [], [], None
    for line in p.stderr.splitlines():
        if "AVFoundation video devices" in line:
            section = video
        elif "AVFoundation audio devices" in line:
            section = audio
        else:
            m = re.search(r"\]\s+\[(\d+)\]\s+(.*)$", line)
            if m and section is not None:
                section.append((int(m.group(1)), m.group(2).strip()))
    return video, audio


def resolve_device(spec, devices, kind):
    """`spec` is an index or part of the device name."""
    if spec is None:
        return None
    if spec.isdigit():
        return int(spec)
    for idx, name in devices:
        if spec.lower() in name.lower():
            return idx
    sys.exit(f"{kind.capitalize()} device '{spec}' not found. Run `devices` for the list.")


def has_encoder(name):
    p = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return re.search(rf"\s{re.escape(name)}\s", p.stdout) is not None


def build_ffmpeg_cmd(opt):
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning"]
    audio_from_screen_input = False

    if opt.source == "test":
        cmd += ["-f", "lavfi", "-re", "-i", f"testsrc2=size={opt.width}x{opt.height}:rate={opt.fps}"]
    else:
        inp = f"{opt.screen_index}:{opt.audio_index if opt.audio_index is not None else 'none'}"
        audio_from_screen_input = opt.audio_index is not None
        cmd += ["-thread_queue_size", "1024", "-fflags", "nobuffer", "-f", "avfoundation",
                "-capture_cursor", "0" if opt.no_cursor else "1",
                "-framerate", str(opt.fps), "-pixel_format", "nv12", "-i", inp]

    if not audio_from_screen_input:
        # Silent audio track: some TVs reject streams without audio.
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]

    # Letterbox non-16:9 screens (e.g. 16:10 laptops) to the TV's 16:9.
    vf = (f"scale={opt.width}:{opt.height}:force_original_aspect_ratio=decrease,"
          f"pad={opt.width}:{opt.height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p")
    cmd += ["-vf", vf, "-r", str(opt.fps)]

    low = opt.latency == "low"
    # Low latency: keyframe every half second so the TV starts sooner, no B-frames, small encoder
    # buffer. Normal: 2 s GOP, better quality for the same bitrate.
    gop = str(max(1, opt.fps // 2) if low else opt.fps * 2)
    bufsize = bitrate_scaled(opt.bitrate, 0.5 if low else 1.0)
    if opt.encoder == "h264_videotoolbox":
        cmd += ["-c:v", "h264_videotoolbox", "-realtime", "1", "-prio_speed", "1", "-profile:v", "main",
                "-b:v", opt.bitrate, "-maxrate", opt.bitrate, "-bufsize", bufsize, "-g", gop, "-bf", "0"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-profile:v", "main",
                "-b:v", opt.bitrate, "-maxrate", opt.bitrate, "-bufsize", bufsize, "-g", gop, "-bf", "0"]

    cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
    cmd += ["-f", "mpegts", "-muxdelay", "0", "-muxpreload", "0", "-flush_packets", "1"]
    if low:
        # Constant bitrate with null packets: the TV buffers by bytes, so it fills quickly and
        # starts sooner even when the screen is static.
        cmd += ["-muxrate", bitrate_scaled(opt.bitrate, 1.3, extra=300_000), "-pcr_period", "20"]
    cmd += ["pipe:1"]
    return cmd


def bitrate_scaled(spec, factor, extra=0):
    """'5M' * factor + extra, as a bits/s string for ffmpeg."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmM]?)", spec.strip())
    if not m:
        sys.exit(f"Invalid bitrate: {spec} (e.g. 5M, 800k)")
    mult = {"": 1, "k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6}[m.group(2)]
    return str(int(float(m.group(1)) * mult * factor + extra))


# HTTP server

START_TIMEOUT = 4.0   # seconds ffmpeg gets to produce its first packet
START_ATTEMPTS = 2    # start attempts per TV request


def read_with_timeout(stream, size, timeout):
    """Read up to `size` bytes; return b"" if nothing arrives within `timeout`."""
    import select
    ready, _, _ = select.select([stream], [], [], timeout)
    if not ready:
        return b""
    return stream.read1(size) if hasattr(stream, "read1") else stream.read(size)


def stop_proc(proc):
    """Stop ffmpeg and return its stderr."""
    try:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=3)
    except Exception:  # noqa: BLE001
        proc.kill()
        try:
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            pass
    try:
        return proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
    except Exception:  # noqa: BLE001
        return ""


class StreamState:
    def __init__(self, opt, allowed_clients):
        self.opt = opt
        self.allowed_clients = frozenset(allowed_clients)
        self.lock = threading.Lock()
        self.procs = []           # running ffmpeg processes
        self.first_request = None
        self.bytes_sent = 0
        self.last_stderr = ""

    def kill_all(self):
        with self.lock:
            procs, self.procs = self.procs, []
        for p in procs:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:  # noqa: BLE001
                p.kill()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: StreamState = None  # set by start_server()

    def log_message(self, fmt, *args):  # silence the default access log
        pass

    def _client_allowed(self):
        if self.client_address[0] in self.state.allowed_clients:
            return True
        log(f"refused {self.command} {self.path} from {self.client_address[0]}")
        self.send_error(403)
        return False

    def _headers(self):
        self.send_response(200)
        self.send_header("Content-Type", MIME)
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org", CONTENT_FEATURES)
        self.send_header("Accept-Ranges", "none")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_HEAD(self):
        if not self._client_allowed():
            return
        log(f"TV {self.client_address[0]}: HEAD {self.path}")
        self._headers()

    def do_GET(self):
        if not self._client_allowed():
            return
        st = self.state
        if not self.path.startswith(STREAM_PATH):
            self.send_error(404)
            return
        st.first_request = st.first_request or time.time()
        log(f"TV {self.client_address[0]}: GET {self.path} -> starting ffmpeg")
        self._headers()
        cmd = build_ffmpeg_cmd(st.opt)
        if st.opt.verbose:
            log("ffmpeg: " + " ".join(cmd))
        chunk_size = 188 * 7 if st.opt.latency == "low" else 188 * 64
        got_data = False
        sent = 0
        err = ""
        try:
            # Screen capture on macOS occasionally hangs at startup without an error. Restart
            # ffmpeg if it produces nothing within START_TIMEOUT; the TV waits about 10 s.
            for attempt in range(1, START_ATTEMPTS + 1):
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                with st.lock:
                    st.procs.append(proc)
                start = time.time()
                try:
                    first = read_with_timeout(proc.stdout, chunk_size, START_TIMEOUT)
                    if first:
                        got_data = True
                        log(f"ffmpeg output after {time.time() - start:.1f}s"
                            + (f" (attempt {attempt})" if attempt > 1 else "") + ", streaming")
                        self.wfile.write(first)
                        sent += len(first)
                        while True:
                            chunk = proc.stdout.read(chunk_size)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            sent += len(chunk)
                        break
                    if proc.poll() is None and attempt < START_ATTEMPTS:
                        log(f"no ffmpeg output for {START_TIMEOUT}s, restarting (attempt {attempt + 1})")
                    else:
                        break
                finally:
                    err = stop_proc(proc)
                    with st.lock:
                        if proc in st.procs:
                            st.procs.remove(proc)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            st.last_stderr = err
            log(f"connection closed by the TV after {sent / 1e6:.1f} MB")
            if not got_data:
                log("WARNING: no data from capture (ffmpeg produced no output).")
                if st.opt.source == "screen":
                    log("  The Screen Recording permission is probably missing. System Settings > Privacy &")
                    log("  Security > Screen Recording: enable the app running the cast (Terminal or Device Cast),")
                    log("  then restart it and try again.")
                if err.strip():
                    for line in err.strip().splitlines()[-8:]:
                        log("  ffmpeg: " + line)


def start_server(opt, state, bind_ip):
    Handler.state = state
    try:
        httpd = ThreadingHTTPServer((bind_ip, opt.port), Handler)
    except OSError as e:
        sys.exit(f"Cannot open port {opt.port} on {bind_ip}: {e}. Try --port 8091.")
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


# Commands

def cmd_list(opt):
    ip = local_ip_towards()
    if opt.json:
        rs = find_renderers(ip, timeout=opt.timeout, location=opt.location)
        saved = load_config().get("tv_udn")
        print(json.dumps([dict(r, saved=(r["udn"] == saved)) for r in rs]))
        return
    log(f"Searching for DLNA renderers (local IP {ip})...")
    rs = find_renderers(ip, timeout=opt.timeout, location=opt.location)
    if not rs:
        print("No renderer found.")
        return
    saved = load_config().get("tv_udn")
    for r in rs:
        mark = "  <- saved" if r["udn"] == saved else ""
        print(f"- {r['name']}  [{r['manufacturer']} {r['model']}]  ip={r['ip']}{mark}")
        print(f"    UDN: {r['udn']}")


def cmd_devices(opt):
    video, audio = avfoundation_devices()
    if deskpad.installed():
        # The DeskPad display is replaced by a fixed "extended display" entry, which turns
        # DeskPad on only for the duration of the cast.
        dp = deskpad.display_index()
        if dp:
            video = [(i, n) for i, n in video if n != f"Capture screen {dp[0]}"]
        video = video + [(-1, deskpad.EXTEND_NAME)]
    if opt.json:
        print(json.dumps({"video": [{"index": i, "name": n} for i, n in video],
                          "audio": [{"index": i, "name": n} for i, n in audio]}))
        return
    print("Screens / video (use --screen N or part of the name):")
    for i, n in video:
        print(f"  [{i}] {n}")
    print("Audio (use --audio N or part of the name, e.g. --audio BlackHole):")
    for i, n in audio:
        print(f"  [{i}] {n}")
    print("\nCapturing system audio needs a virtual audio device such as BlackHole:")
    print("  brew install blackhole-2ch   (then create a Multi-Output Device in Audio MIDI Setup)")


def cmd_stop(opt):
    ip = local_ip_towards()
    tv = pick_renderer(find_renderers(ip, timeout=opt.timeout, location=opt.location), opt.tv)
    soap(tv["control"], "Stop", {"InstanceID": 0})
    log(f"Stop sent to {tv['name']}")


TIZEN_APP_ID = "DevCast001.DeviceCast"


def tizen_app_installed(tv_ip):
    """True if the Device Cast app is installed on the TV (Smart View API, port 8001)."""
    try:
        with urllib.request.urlopen(f"http://{tv_ip}:8001/api/v2/applications/{TIZEN_APP_ID}", timeout=3) as r:
            return json.loads(r.read(64 * 1024).decode("utf-8", "replace")).get("id") == TIZEN_APP_ID
    except Exception:  # noqa: BLE001
        return False


def tv_ip_hint(opt):
    if opt.location:
        host = urllib.parse.urlparse(opt.location).hostname
        try:
            socket.create_connection((host, 8001), timeout=1.5).close()
            return host
        except OSError:
            # Stale address (e.g. the TV switched from Wi-Fi to Ethernet): find it again by UDN.
            for r in find_renderers(local_ip_towards(), timeout=3):
                if not opt.tv or opt.tv.lower() in (r["udn"].lower(), r["ip"], r["name"].lower()):
                    log(f"the TV is now at {r['ip']} (was {host})")
                    save_config(tv_ip=r["ip"], tv_location=r["location"])
                    opt.location = r["location"]
                    return r["ip"]
            return host
    if opt.tv and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", opt.tv):
        return opt.tv
    return load_config().get("tv_ip")


def hand_off_to_server(opt, tv_ip):
    """Replace this process with server.py, so Ctrl+C and the app's stop signal reach it."""
    server = os.path.join(HERE, "server.py")
    receiver = os.path.join(ROOT, "receiver", "receiver.js")
    if not os.path.exists(server) or not os.path.exists(receiver):
        log(f"Device Cast mode requested but {server} or {receiver} is missing: using DLNA")
        return False
    # Without the DLNA null-packet padding about 60% of the bitrate is enough, and less data
    # stalls less on a weak TV Wi-Fi link.
    bitrate = bitrate_scaled(opt.bitrate, 0.6)
    args = [sys.executable, server, "--tv", tv_ip, "--width", str(opt.width), "--height", str(opt.height),
            "--fps", str(opt.fps), "--bitrate", bitrate, "--source", opt.source]
    if opt.screen:
        args += ["--screen", opt.screen]
    if opt.audio:
        args += ["--audio", opt.audio]
    if opt.no_cursor:
        args += ["--no-cursor"]
    if opt.encoder != "auto":
        args += ["--encoder", opt.encoder]
    if opt.verbose:
        args += ["-v"]
    if getattr(opt, "stop_deskpad", False):
        args += ["--stop-deskpad"]
    cfg = load_config()
    # Low latency: tiles (changed regions only, native resolution, no audio).
    # Otherwise: H.264 video with audio, about 1 s of delay plus the TV's video pipeline.
    codec = cfg.get("codec") or ("tiles" if opt.latency == "low" else "h264")
    args += ["--codec", codec]
    if codec == "tiles":
        # Quality menu toggles of the menu bar app
        if cfg.get("auto_quality", True) is False:
            args += ["--no-auto-quality"]
        if cfg.get("auto_video", True) is False:
            args += ["--no-auto-video"]
        if cfg.get("audio_sync", True) is False:
            args += ["--no-audio-sync"]
        if cfg.get("audio_offset_ms"):
            args += ["--audio-offset", str(float(cfg["audio_offset_ms"]) / 1000)]
    if codec == "jpeg":
        # ffmpeg JPEG quality (2 best - 31 worst): 9 keeps text-heavy frames around 70-90 KB,
        # which the TV's Wi-Fi transfers quickly.
        args += ["--jpeg-q", str(cfg.get("jpeg_q", 9))]
    log(f"TV has the Device Cast app ({tv_ip}): using low-latency mirroring")
    sys.stderr.flush()
    os.execv(sys.executable, args)
    return True  # not reached


def cmd_cast(opt):
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found: install it with `brew install ffmpeg`.")

    # Extended display: DeskPad runs only for the duration of the cast.
    opt.stop_deskpad = False
    if opt.source == "screen" and deskpad.wants_extend(opt.screen):
        idx = deskpad.start()
        if idx is None:
            opt.screen = None
        else:
            opt.screen = f"Capture screen {idx}"
            opt.stop_deskpad = True
            atexit.register(deskpad.stop)   # DLNA mode; in Device Cast mode server.py closes it

    mode = opt.mode or load_config().get("mode", "auto")
    if mode in ("auto", "tizen"):
        tv_ip = tv_ip_hint(opt)
        if tv_ip and (mode == "tizen" or tizen_app_installed(tv_ip)):
            hand_off_to_server(opt, tv_ip)
        elif mode == "tizen":
            log("tizen mode requested but the Device Cast app was not found on the TV: using DLNA")

    if opt.encoder == "auto":
        opt.encoder = "h264_videotoolbox" if has_encoder("h264_videotoolbox") else "libx264"
    if opt.source == "screen":
        video, audio = avfoundation_devices()
        screens = [(i, n) for i, n in video if n.lower().startswith("capture screen")]
        if not screens:
            sys.exit("ffmpeg reports no 'Capture screen' device. Check `devices`.")
        wanted = [i for i, n in screens if opt.screen and (opt.screen == str(i) or opt.screen.lower() in n.lower())]
        if opt.screen and not wanted:
            # e.g. the DeskPad display was selected in the menu but DeskPad is not running
            log(f"screen '{opt.screen}' not available (DeskPad not running?): using the main screen")
        opt.screen_index = wanted[0] if wanted else screens[0][0]
        opt.audio_index = resolve_device(opt.audio, audio, "audio")
    else:
        opt.screen_index = opt.audio_index = None

    ip = local_ip_towards()
    log(f"Searching for the TV (local IP {ip})...")
    renderers = find_renderers(ip, timeout=opt.timeout, location=opt.location)
    if not renderers:
        # e.g. the TV switched from Wi-Fi to Ethernet and changed address
        log("the TV does not answer at its known address, searching the network..." if opt.location else
            "no TV found on the first attempt, retrying...")
        renderers = find_renderers(ip, timeout=opt.timeout + 2)
    tv = pick_renderer(renderers, opt.tv, interactive=True)
    save_config(tv_udn=tv["udn"], tv_name=tv["name"], tv_ip=tv["ip"], tv_location=tv["location"])
    my_ip = local_ip_towards(tv["ip"]) or ip
    if not my_ip:
        sys.exit(f"No local network route to the TV at {tv['ip']}.")
    url = f"http://{my_ip}:{opt.port}{STREAM_PATH}"
    log(f"TV: {tv['name']} ({tv['model']}, {tv['ip']})")
    log(f"Source: {opt.source}, {opt.width}x{opt.height}@{opt.fps}, {opt.bitrate}, encoder {opt.encoder}, "
        f"latency {opt.latency}, "
        f"audio {'device ' + str(opt.audio_index) if opt.audio_index is not None else 'silent'}")
    log(f"Stream URL: {url}")

    # Only the TV (and this machine) may fetch the screen stream.
    state = StreamState(opt, allowed_clients=(tv["ip"], my_ip, "127.0.0.1", "::1"))
    httpd = start_server(opt, state, my_ip)

    stopping = threading.Event()

    def shutdown(*_):
        if stopping.is_set():
            return
        stopping.set()
        log("Shutting down: sending Stop to the TV and stopping ffmpeg...")
        try:
            soap(tv["control"], "Stop", {"InstanceID": 0}, timeout=5)
        except Exception as e:  # noqa: BLE001
            log(f"  (Stop: {e})")
        state.kill_all()
        httpd.shutdown()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        try:
            soap(tv["control"], "Stop", {"InstanceID": 0}, timeout=5)
        except Exception:  # noqa: BLE001
            pass
        soap(tv["control"], "SetAVTransportURI", {
            "InstanceID": 0,
            "CurrentURI": url,
            "CurrentURIMetaData": didl_metadata(url, opt.title),
        })
        soap(tv["control"], "Play", {"InstanceID": 0, "Speed": "1"})
        play_ts = time.time()
        log("Play sent. Press Ctrl+C to stop casting.")
    except Exception as e:  # noqa: BLE001
        log(f"ERROR: {e}")
        shutdown()
        sys.exit(1)

    last_state = None
    warned = False
    while not stopping.is_set():
        time.sleep(3)
        if stopping.is_set():
            break
        try:
            s = transport_state(tv["control"])
        except Exception as e:  # noqa: BLE001
            s = f"? ({e})"
        if s != last_state:
            log(f"TV state: {s}")
            last_state = s
        if not warned and state.first_request is None and time.time() - play_ts > 15:
            warned = True
            log("The TV has not requested the stream yet. Check that the macOS firewall does not block python3")
            log(f"(System Settings > Network > Firewall) and that the TV can reach {my_ip}:{opt.port}.")
        if s == "STOPPED" and state.first_request and not opt.keep_alive:
            log("TV stopped playback.")
            shutdown()
    log("Cast ended.")


def main():
    ap = argparse.ArgumentParser(description="Cast the screen to a TV (Device Cast receiver app or DLNA).",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--tv", help="TV name (or part of it), IP or UDN")
    common.add_argument("--timeout", type=float, default=3.0, help="SSDP search time in seconds (default 3)")
    common.add_argument("--location", help="URL of the TV's UPnP description; skips discovery (used by the app)")

    p_list = sub.add_parser("list", parents=[common], help="list DLNA renderers on the network")
    p_list.add_argument("--json", action="store_true", help="JSON output (used by the app)")
    p_devices = sub.add_parser("devices", help="list capturable screens and audio devices")
    p_devices.add_argument("--json", action="store_true", help="JSON output (used by the app)")
    sub.add_parser("stop", parents=[common], help="stop playback on the TV")

    p_cast = sub.add_parser("cast", parents=[common], help="start casting the screen")
    p_cast.add_argument("--source", choices=["screen", "test"], default="screen",
                        help="screen: capture the display (default); test: test pattern for diagnostics")
    p_cast.add_argument("--screen", help="index or name of the screen to capture (default: Capture screen 0)")
    p_cast.add_argument("--audio", help="index or name of the audio device (e.g. BlackHole); default: silence")
    p_cast.add_argument("--width", type=int, default=1280)
    p_cast.add_argument("--height", type=int, default=720)
    p_cast.add_argument("--fps", type=int, default=30)
    p_cast.add_argument("--bitrate", default="5M")
    p_cast.add_argument("--encoder", choices=["auto", "h264_videotoolbox", "libx264"], default="auto")
    p_cast.add_argument("--mode", choices=["auto", "dlna", "tizen"], default=None,
                        help="auto (default): Device Cast app on the TV if installed, otherwise DLNA")
    p_cast.add_argument("--latency", choices=["low", "normal"], default="low",
                        help="low (default): frequent keyframes and constant bitrate so the TV starts sooner; "
                             "normal: better quality for the same bitrate")
    p_cast.add_argument("--port", type=int, default=8090)
    p_cast.add_argument("--title", default="Device Cast", help="title shown on the TV (DLNA mode)")
    p_cast.add_argument("--no-cursor", action="store_true", help="hide the mouse pointer")
    p_cast.add_argument("--keep-alive", action="store_true", help="do not exit when the TV stops playback")
    p_cast.add_argument("-v", "--verbose", action="store_true", help="print the ffmpeg command")

    opt = ap.parse_args()
    if opt.cmd is None:
        ap.print_help()
        return
    {"list": cmd_list, "devices": cmd_devices, "stop": cmd_stop, "cast": cmd_cast}[opt.cmd](opt)


if __name__ == "__main__":
    main()
