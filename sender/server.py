#!/usr/bin/env python3
"""
server.py - low-latency screen streaming to the Device Cast receiver app.

Unlike DLNA, where the TV downloads and buffers a video for seconds, the receiver runs a web page
served from here that gets the stream over WebSocket and keeps only a few hundred milliseconds of
buffer. Without MediaSource it falls back to JPEG frames or MJPEG.

Endpoints:
  GET /hello          discovery (the receiver scans the network for this server)
  GET /receiver.js    receiver code, served from here so it updates without reinstalling the app
  WS  /ws             fragmented MP4 (H.264 + AAC) for MediaSource, one shared ffmpeg
  WS  /wsj            JPEG frames, latest frame only
  WS  /wst            changed screen tiles from the screencap helper, plus the cursor channel
  GET /stream.mjpg    MJPEG fallback
  POST /stats         receiver statistics, logged every ~10 s

Usage: python3 server.py [--width 1280 --height 720 --fps 30 --bitrate 4M --audio BlackHole --tv 192.0.2.10]
"""

import argparse
import base64
import collections
import hashlib
import ipaddress
import json
import os
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request

import deskpad
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RECEIVER_DIR = os.path.join(ROOT, "receiver")
PORT = 8091
APP_ID = "DevCast001.DeviceCast"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_MAX_CLIENT_PAYLOAD = 64 * 1024   # receivers only send short acks, pings and close frames
START_TIMEOUT = 4.0
START_ATTEMPTS = 3
MAX_QUEUE = 10         # 100 ms fragments queued per client; beyond this, skip to the next keyframe
SNDBUF = 256 * 1024    # small send buffer so a slow network cannot hide seconds of delay
SO_NWRITE = 0x1024     # macOS: bytes still in the socket send buffer


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, file=sys.stderr, flush=True)


def printable(text):
    return "".join(ch for ch in text if ch.isprintable())


def avfoundation_devices():
    p = subprocess.run(["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                       capture_output=True, text=True)
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


def resolve_device(spec, devices):
    if spec is None:
        return None
    if spec.isdigit():
        return int(spec)
    for idx, name in devices:
        if spec.lower() in name.lower():
            return idx
    sys.exit(f"Device '{spec}' not found. Available: " + ", ".join(n for _, n in devices))


def input_args(opt):
    if opt.source.startswith("test"):
        return ["-f", "lavfi", "-re", "-i", f"testsrc2=size={opt.width}x{opt.height}:rate={opt.fps}"], False
    inp = f"{opt.screen_index}:{opt.audio_index if opt.audio_index is not None else 'none'}"
    return ["-thread_queue_size", "1024", "-fflags", "nobuffer", "-f", "avfoundation",
            "-capture_cursor", "0" if opt.no_cursor else "1", "-framerate", str(opt.fps),
            "-pixel_format", "nv12", "-i", inp], opt.audio_index is not None


def video_filter(opt):
    return (f"scale={opt.width}:{opt.height}:force_original_aspect_ratio=decrease,"
            f"pad={opt.width}:{opt.height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p")


def fmp4_cmd(opt):
    """ffmpeg command writing fragmented MP4 (H.264 + AAC, 100 ms fragments) to stdout."""
    inp, has_audio = input_args(opt)
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning"] + inp
    if not has_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]
    gop = str(max(1, opt.fps // 2))
    cmd += ["-vf", video_filter(opt), "-r", str(opt.fps)]
    if opt.encoder == "h264_videotoolbox":
        cmd += ["-c:v", "h264_videotoolbox", "-realtime", "1", "-prio_speed", "1", "-profile:v", "main",
                "-b:v", opt.bitrate, "-maxrate", opt.bitrate, "-bufsize", opt.bitrate, "-g", gop, "-bf", "0"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-profile:v", "main",
                "-b:v", opt.bitrate, "-maxrate", opt.bitrate, "-bufsize", opt.bitrate, "-g", gop, "-bf", "0"]
    cmd += ["-c:a", "aac", "-b:a", "96k", "-ar", "48000", "-ac", "2",
            "-f", "mp4", "-movflags", "empty_moov+default_base_moof+omit_tfhd_offset+frag_custom",
            "-frag_duration", "100000", "-min_frag_duration", "100000",
            "-video_track_timescale", "90000", "-flush_packets", "1", "pipe:1"]
    return cmd


def jpeg_cmd(opt):
    """ffmpeg command writing a JPEG sequence to stdout. mpdecimate drops frames identical to the
    previous one (a static screen costs almost nothing), still sending one frame per second."""
    inp, _ = input_args(opt)
    fps = min(opt.fps, 15)
    vf = (video_filter(opt).replace(",format=yuv420p", "") +
          f",fps={fps},mpdecimate=hi=768:lo=320:frac=0.33:max={fps},format=yuvj420p")
    return (["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning"] + inp +
            ["-map", "0:v:0", "-an", "-vf", vf, "-fps_mode", "vfr",
             "-c:v", "mjpeg", "-q:v", str(opt.jpeg_q), "-f", "image2pipe", "-flush_packets", "1", "pipe:1"])


def mjpeg_cmd(opt):
    inp, _ = input_args(opt)
    fps = min(opt.fps, 20)
    return (["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning"] + inp +
            ["-map", "0:v:0", "-an", "-vf", video_filter(opt) + ",format=yuvj420p", "-r", str(fps),
             "-c:v", "mjpeg", "-q:v", "7", "-f", "mpjpeg", "-flush_packets", "1", "pipe:1"])


def read_with_timeout(stream, size, timeout):
    ready, _, _ = select.select([stream], [], [], timeout)
    return stream.read1(size) if ready else b""


def drain_stderr(proc, forward=False):
    """Read a child's stderr in the background, keeping the last 4 KB in proc.err_tail.
    With forward=True every line is also logged (used for screencap summaries)."""
    proc.err_tail = b""

    def run():
        try:
            for line in iter(proc.stderr.readline, b""):
                proc.err_tail = (proc.err_tail + line)[-4096:]
                if forward:
                    log("  " + line.decode("utf-8", "replace").rstrip())
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=run, daemon=True).start()


def stop_proc(proc):
    try:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=3)
    except Exception:  # noqa: BLE001
        proc.kill()
    time.sleep(0.1)
    tail = getattr(proc, "err_tail", b"")
    return "\n".join(l for l in tail.decode("utf-8", "replace").splitlines() if "objc[" not in l)


def iter_boxes(buf, start=0, end=None):
    """Yield (type, payload_start, box_end) for the MP4 boxes in buf[start:end]."""
    end = len(buf) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, = struct.unpack_from(">I", buf, pos)
        btype = buf[pos + 4:pos + 8]
        hdr = 8
        if size == 1:
            size, = struct.unpack_from(">Q", buf, pos + 8)
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            return
        yield btype, pos + hdr, pos + size
        pos += size


def fragment_video_time(moof, timescale=90000):
    """Time (s) of the fragment's first video sample, from the tfdt of track 1."""
    for btype, ps, pe in iter_boxes(moof):
        if btype != b"moof":
            continue
        for t2, s2, e2 in iter_boxes(moof, ps, pe):
            if t2 != b"traf":
                continue
            track_id, base = None, None
            for t3, s3, e3 in iter_boxes(moof, s2, e2):
                if t3 == b"tfhd":
                    track_id = struct.unpack_from(">I", moof, s3 + 4)[0]
                elif t3 == b"tfdt":
                    version = moof[s3]
                    base = struct.unpack_from(">Q" if version == 1 else ">I", moof, s3 + 4)[0]
            if track_id == 1 and base is not None:
                return base / timescale
    return None


def fragment_starts_with_keyframe(moof):
    """True if the first video sample of the moof is a keyframe (a new client can start there)."""
    for btype, ps, pe in iter_boxes(moof):
        if btype != b"moof":
            continue
        for t2, s2, e2 in iter_boxes(moof, ps, pe):
            if t2 != b"traf":
                continue
            default_flags = None
            track_id = None
            for t3, s3, e3 in iter_boxes(moof, s2, e2):
                if t3 == b"tfhd":
                    flags = struct.unpack_from(">I", moof, s3)[0] & 0xFFFFFF
                    track_id = struct.unpack_from(">I", moof, s3 + 4)[0]
                    p = s3 + 8
                    if flags & 0x1:
                        p += 8
                    if flags & 0x2:
                        p += 4
                    if flags & 0x8:
                        p += 4
                    if flags & 0x10:
                        p += 4
                    if flags & 0x20:
                        default_flags = struct.unpack_from(">I", moof, p)[0]
                elif t3 == b"trun" and track_id == 1:
                    flags = struct.unpack_from(">I", moof, s3)[0] & 0xFFFFFF
                    p = s3 + 8  # version/flags + sample_count
                    if flags & 0x1:
                        p += 4
                    if flags & 0x4:
                        first = struct.unpack_from(">I", moof, p)[0]
                    elif flags & 0x400:
                        if flags & 0x100:
                            p += 4
                        if flags & 0x200:
                            p += 4
                        first = struct.unpack_from(">I", moof, p)[0]
                    else:
                        first = default_flags if default_flags is not None else 0
                    return (first & 0x00010000) == 0
    return False


TILE_WINDOW = 30                # safety cap on updates not yet acknowledged by the receiver
TILE_MAX_UNSENT = 512 * 1024    # safety cap on bytes in the local send buffer (sent but unacked included)
TILE_MAX_UNDRAWN = 160 * 1024   # bytes sent but not yet acknowledged as drawn: the real throttle
SCREENCAP = os.path.join(ROOT, "tools.noindex", "screencap")
SCREENCAP_SRC = os.path.join(HERE, "screencap.swift")


AUDIODELAY = os.path.join(ROOT, "tools.noindex", "audiodelay")
AUDIODELAY_SRC = os.path.join(HERE, "audiodelay.swift")


def ensure_helper(binary, source, frameworks):
    """Build a Swift helper if the binary is missing or older than its source. True when ready."""
    try:
        if os.path.exists(binary) and os.path.getmtime(binary) >= os.path.getmtime(source):
            return True
    except OSError:
        return os.path.exists(binary)
    log(f"building {os.path.basename(binary)}...")
    os.makedirs(os.path.dirname(binary), exist_ok=True)
    cmd = ["swiftc", "-O", "-swift-version", "5"]
    for fw in frameworks:
        cmd += ["-framework", fw]
    r = subprocess.run(cmd + ["-o", binary, source], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"{os.path.basename(binary)} build failed: " + r.stderr[-300:])
        return False
    return True


def ensure_screencap():
    return ensure_helper(SCREENCAP, SCREENCAP_SRC,
                         ["AppKit", "ScreenCaptureKit", "CoreImage", "CoreMedia", "CoreVideo"])


DISPLAY_LATENCY = 0.05   # typical TV panel processing, not measurable from here
VIDEO_PIPELINE = 0.20    # capture, H.264 encoding and 100 ms MP4 fragments before the TV buffer


AUDIO_SYNC_STEP = 0.06     # seconds; smaller latency changes do not move the audio delay


class AudioSync:
    """Delays the computer's own audio (speakers or headphones) by the measured latency of the picture
    on the TV, using the audiodelay helper (Core Audio process tap, macOS 14.2+)."""

    def __init__(self, offset):
        self.offset = offset
        self.proc = None
        self.samples = collections.deque(maxlen=9)
        self.current = None
        self.lock = threading.Lock()

    def start(self):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            if not ensure_helper(AUDIODELAY, AUDIODELAY_SRC, ["CoreAudio", "AudioToolbox"]):
                return
            self.current = max(0.0, 0.1 + self.offset)
            self.samples.clear()
            self.proc = subprocess.Popen([AUDIODELAY, "--delay", f"{self.current:.3f}"],
                                         stdin=subprocess.PIPE, stderr=subprocess.PIPE)
            drain_stderr(self.proc, forward=True)

    def observe(self, picture_latency):
        """picture_latency: seconds from capture to the TV screen, as measured for the current mode."""
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                return
            self.samples.append(picture_latency)
            target = max(0.0, sorted(self.samples)[len(self.samples) // 2] + self.offset)
            # Ignore small fluctuations: every change makes the helper glide to the new delay.
            if self.current is None or abs(target - self.current) >= AUDIO_SYNC_STEP:
                self.current = target
                try:
                    self.proc.stdin.write(f"delay {target:.3f}\n".encode())
                    self.proc.stdin.flush()
                except OSError:
                    pass

    def reset(self):
        """Forget the measurements (mode switch: tiles and video have very different latencies)."""
        with self.lock:
            self.samples.clear()

    def stop(self):
        with self.lock:
            if self.proc is None:
                return
            try:
                self.proc.stdin.write(b"quit\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
            self.proc = None


def read_exact(stream, n):
    if n == 0:
        return b""   # tile without data (the "video region" metadata entry)
    data = stream.read(n)
    return data if data and len(data) == n else None


class TileHub:
    """Tiles mode: screencap (ScreenCaptureKit) delivers only the changed regions of the screen at
    native 1920x1080. One client at a time drives the capture (the most recent one)."""

    def __init__(self, opt):
        self.opt = opt
        self.lock = threading.Lock()
        self.proc = None
        self.owner = None
        self.stopping = False
        self.last_error = ""
        self.updates = 0
        self.bytes = 0
        self.full_frames = 0
        # latest cursor shape ("C ...") and position ("c X Y"), with a sequence number for forwarders
        self.cur_cond = threading.Condition()
        self.cur_shape = None
        self.cur_pos = None
        self.cur_seq = 0

    def _cursor_reader(self, fd):
        buf = b""
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("ascii", "replace")
                    with self.cur_cond:
                        if text.startswith("C "):
                            self.cur_shape = text
                        elif text.startswith("c "):
                            self.cur_pos = text
                        self.cur_seq += 1
                        self.cur_cond.notify_all()
        except OSError:
            pass
        finally:
            os.close(fd)

    def _start(self):
        if self.proc and self.proc.poll() is None:
            return True
        args = [SCREENCAP, "--fps", "60", "--quality", "0.62", "--full-quality", "0.5",
                "--width", str(self.opt.width), "--height", str(self.opt.height)]
        if self.opt.source == "test":
            args += ["--test"]
        elif self.opt.source == "test-scroll":
            args += ["--test-scroll"]
        elif self.opt.source == "test-video":
            args += ["--test-video"]
        else:
            args += ["--display", str(self.opt.display_n)]
        r, w = os.pipe()
        args += ["--cursor-fd", str(w)]
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     pass_fds=(w,))
        os.close(w)
        threading.Thread(target=self._cursor_reader, args=(r,), daemon=True).start()
        drain_stderr(self.proc, forward=True)
        return True

    def request(self, cmd):
        """Send screencap a command ("next", "next N", "full" or "probe"). Returns (bytes, tiles) or None."""
        with self.lock:
            self._start()
            p = self.proc
            try:
                p.stdin.write((cmd + "\n").encode())
                p.stdin.flush()
                hdr = read_exact(p.stdout, 16)
                if not hdr:
                    raise OSError("screencap closed")
                n = struct.unpack(">I", hdr[12:16])[0]
                parts = [hdr]
                for _ in range(n):
                    th = read_exact(p.stdout, 12)
                    ln = struct.unpack(">I", th[8:12])[0]
                    body = read_exact(p.stdout, ln)
                    if body is None:
                        raise OSError("truncated screencap reply")
                    parts += [th, body]
                data = b"".join(parts)
            except (OSError, TypeError, struct.error):
                err = stop_proc(p)
                self.last_error = err or "screencap exited"
                self.proc = None
                return None
            if n:
                self.updates += 1
                self.bytes += len(data)
                if cmd == "full":
                    self.full_frames += 1
            return data, n

    def stop_when_idle(self, delay=10):
        """Stop screencap if no receiver reconnects within `delay` seconds (it captures at 60 fps)."""
        def check():
            time.sleep(delay)
            if self.owner is None and self.proc is not None:
                log("no receiver connected: stopping the capture helper")
                self.stop()
                self.stopping = False
        threading.Thread(target=check, daemon=True).start()

    def stop(self):
        self.stopping = True
        with self.lock:
            if self.proc:
                try:
                    self.proc.stdin.write(b"quit\n")
                    self.proc.stdin.flush()
                except OSError:
                    pass
                stop_proc(self.proc)
                self.proc = None


class JpegHub:
    """Minimum-latency mode: one ffmpeg producing JPEGs; each client always gets the latest frame.
    On a slow network intermediate frames are lost, but delay never accumulates (no video player
    buffer on the receiver)."""

    def __init__(self, opt):
        self.opt = opt
        self.cond = threading.Condition()
        self.frame, self.ts, self.seq = None, 0.0, 0
        self.clients = 0
        self.running = False
        self.stopping = False
        self.proc = None
        self.last_error = ""
        self.frames_made = 0
        self.bytes_made = 0

    def subscribe(self):
        with self.cond:
            self.clients += 1
            if not self.running:
                self.running = True
                threading.Thread(target=self._run, daemon=True).start()

    def unsubscribe(self):
        with self.cond:
            self.clients -= 1

    def wait_frame(self, last_seq, timeout=10):
        with self.cond:
            if self.seq == last_seq:
                self.cond.wait(timeout)
            return self.seq, self.ts, self.frame

    def _publish(self, frame):
        with self.cond:
            self.frame, self.ts = frame, time.time()
            self.seq += 1
            self.frames_made += 1
            self.bytes_made += len(frame)
            self.cond.notify_all()

    def _run(self):
        attempt = 0
        while True:
            with self.cond:
                if self.clients <= 0:
                    self.running = False
                    log("no clients: stopping capture")
                    return
            attempt += 1
            cmd = jpeg_cmd(self.opt)
            if self.opt.verbose and attempt == 1:
                log("ffmpeg: " + " ".join(cmd))
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.proc = proc
            drain_stderr(proc)
            first = read_with_timeout(proc.stdout, 262144, START_TIMEOUT)
            if not first:
                self.last_error = stop_proc(proc)
                if attempt < START_ATTEMPTS:
                    log(f"ffmpeg produced no data for {START_TIMEOUT}s: restarting (attempt {attempt + 1})")
                    continue
                log("WARNING: no data from capture (ffmpeg). Screen Recording permission is probably "
                    "missing for the app that started the server.")
                with self.cond:
                    self.seq = -1
                    self.cond.notify_all()
                    self.running = False
                if Handler.httpd is not None:
                    threading.Thread(target=Handler.httpd.shutdown, daemon=True).start()
                return
            log(f"JPEG capture started (after {attempt} attempt(s))")
            attempt = 0
            buf = first
            t_check = time.time()
            while True:
                while True:
                    s = buf.find(b"\xff\xd8")
                    if s < 0:
                        buf = buf[-1:]
                        break
                    e = buf.find(b"\xff\xd9", s + 2)
                    if e < 0:
                        buf = buf[s:]
                        break
                    self._publish(buf[s:e + 2])
                    buf = buf[e + 2:]
                chunk = proc.stdout.read1(262144)
                if not chunk:
                    err = stop_proc(proc)
                    if self.stopping:
                        return
                    log("ffmpeg exited unexpectedly: " + err[-300:])
                    break
                buf += chunk
                if time.time() - t_check > 1:
                    t_check = time.time()
                    with self.cond:
                        if self.clients <= 0:
                            stop_proc(proc)
                            self.running = False
                            log("no clients: stopping capture")
                            return


class Broadcaster:
    """One ffmpeg; distributes the init segment and fragments to every WebSocket client."""

    def __init__(self, opt):
        self.opt = opt
        self.lock = threading.Lock()
        self.clients = {}       # id -> dict(queue=list, cond=Condition, started=bool)
        self.init = None
        self.proc = None
        self.thread = None
        self.running = False
        self.stopping = False   # set during an intentional stop: ffmpeg exiting is not an error then
        self.timeline = collections.deque(maxlen=900)   # (video time, wall clock) for latency estimates
        self.dropped = 0
        self.last_error = ""

    def subscribe(self):
        cid = object()
        with self.lock:
            self.clients[cid] = {"queue": [], "cond": threading.Condition(), "started": False}
            if not self.running:
                self.running = True
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()
        return cid

    def unsubscribe(self, cid):
        # with no clients left, _run stops ffmpeg on its own
        with self.lock:
            self.clients.pop(cid, None)

    def _push(self, cid, data):
        c = self.clients.get(cid)
        if not c:
            return
        with c["cond"]:
            c["queue"].append(data)
            if len(c["queue"]) > MAX_QUEUE:
                # The network cannot keep up: drop the queue instead of accumulating delay and resume
                # at the next keyframe (every half second). The init segment is not resent, because
                # the receiver's player stalls if it gets one mid-stream.
                self.dropped += len(c["queue"])
                c["queue"].clear()
                c["skip"] = True
            c["cond"].notify()

    def _run(self):
        attempt = 0
        while True:
            with self.lock:
                if not self.clients:
                    self.running = False
                    log("no clients: stopping capture")
                    return
            attempt += 1
            cmd = fmp4_cmd(self.opt)
            if self.opt.verbose and attempt == 1:
                log("ffmpeg: " + " ".join(cmd))
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.proc = proc
            drain_stderr(proc)
            first = read_with_timeout(proc.stdout, 65536, START_TIMEOUT)
            if not first:
                err = stop_proc(proc)
                self.last_error = err
                if attempt < START_ATTEMPTS:
                    log(f"ffmpeg produced no data for {START_TIMEOUT}s: restarting (attempt {attempt + 1})")
                    continue
                log("ffmpeg failed to start. " + ("Last lines:\n" + err[-600:] if err.strip() else ""))
                log("WARNING: no data from capture (ffmpeg). Screen Recording permission is probably "
                    "missing for the app that started the server.")
                if Handler.httpd is not None:
                    threading.Thread(target=Handler.httpd.shutdown, daemon=True).start()
                with self.lock:
                    for c in self.clients.values():
                        with c["cond"]:
                            c["queue"].append(None)
                            c["cond"].notify()
                    self.clients.clear()
                    self.running = False
                return
            log(f"capture started (after {attempt} attempt(s))")
            self.timeline.clear()
            attempt = 0
            buf = first
            self.init = None
            pending_moof = None
            t_last_client_check = time.time()
            while True:
                # split complete boxes off the buffer
                while True:
                    if len(buf) < 8:
                        break
                    size, = struct.unpack_from(">I", buf, 0)
                    hdr = 8
                    if size == 1 and len(buf) >= 16:
                        size, = struct.unpack_from(">Q", buf, 8)
                        hdr = 16
                    if size < hdr or len(buf) < size:
                        break
                    box, buf = buf[:size], buf[size:]
                    btype = box[4:8]
                    if btype in (b"ftyp", b"moov"):
                        self.init = (self.init or b"") + box
                        if btype == b"moov":
                            log(f"init segment ready ({len(self.init)} bytes)")
                    elif btype == b"moof":
                        pending_moof = box
                    elif btype == b"mdat" and pending_moof is not None:
                        frag = pending_moof + box
                        key = fragment_starts_with_keyframe(pending_moof)
                        vt = fragment_video_time(pending_moof)
                        if vt is not None:
                            self.timeline.append((vt, time.time()))
                        pending_moof = None
                        with self.lock:
                            for cid, c in list(self.clients.items()):
                                if not c["started"]:
                                    if not (key and self.init):
                                        continue
                                    with c["cond"]:
                                        c["queue"].append(self.init)
                                        c["started"] = True
                                if c.get("skip"):
                                    if not key:
                                        continue
                                    c["skip"] = False
                                self._push(cid, frag)
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    err = stop_proc(proc)
                    if self.stopping:
                        log("capture stopped")
                        self.running = False
                        return
                    log("ffmpeg exited unexpectedly: " + err[-300:])
                    break
                buf += chunk
                if time.time() - t_last_client_check > 1:
                    t_last_client_check = time.time()
                    with self.lock:
                        if not self.clients:
                            stop_proc(proc)
                            self.running = False
                            log("no clients: stopping capture")
                            return

    def next_chunk(self, cid, timeout=15):
        c = self.clients.get(cid)
        if not c:
            return None
        with c["cond"]:
            if not c["queue"]:
                c["cond"].wait(timeout)
            if not c["queue"]:
                return b""      # timeout: the caller sends a ping
            return c["queue"].pop(0)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    opt = None
    bc: Broadcaster = None
    httpd = None
    last_stats = 0.0
    last_hello = None
    jhub = None
    thub = None
    jpeg_samples = collections.deque(maxlen=30)
    audio_sync = None
    allowed = None              # set of allowed client IPs; None accepts everyone
    refused = set()
    refused_lock = threading.Lock()

    def _client_allowed(self):
        """Reply 403 to clients outside the allowlist. Loopback is always allowed."""
        ip = self.client_address[0]
        if Handler.allowed is None or ip in Handler.allowed:
            return True
        try:
            if ipaddress.ip_address(ip).is_loopback:
                return True
        except ValueError:
            pass
        with Handler.refused_lock:
            first = ip not in Handler.refused
            Handler.refused.add(ip)
        if first:
            log(f"refused request from {ip} (not allowed; see --allow)")
        self.close_connection = True
        self._send(403, "text/plain", b"forbidden")
        return False

    def jpeg_report(self):
        sm = sorted(Handler.jpeg_samples)
        hub = self.jhub
        rate = ""
        th = self.thub
        if self.opt.codec == "tiles" and th is not None and th.updates:
            rate = f", {th.updates} updates, avg {th.bytes / th.updates / 1024:.1f} KB"
        elif hub is not None and hub.frames_made:
            rate = f", {hub.frames_made} frames, avg {hub.bytes_made / hub.frames_made / 1024:.0f} KB"
        if not sm:
            return "latency to receiver: measuring" + rate
        med = sm[len(sm) // 2]
        return (f"latency to receiver {med / 1000:.2f} s (min {sm[0] / 1000:.2f}, max {sm[-1] / 1000:.2f}; "
                f"excl. capture and display){rate}")

    def do_POST(self):
        if not self._client_allowed():
            return
        if self.path.split("?")[0] != "/stats":
            self._send(404, "text/plain", b"not found")
            return
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(n, 4096)).decode("utf-8", "replace")
        now = time.time()
        if now - Handler.last_stats > 9:   # one log line every ~10 s
            Handler.last_stats = now
            text, _, tail = body.partition(" |ct=")
            if " |eco=" in text:
                text = text.partition(" |eco=")[0]
                log(f"receiver {self.client_address[0]}: {printable(text)}")
                log("  " + self.jpeg_report())
            else:
                log(f"receiver {self.client_address[0]}: {printable(text)}")
                log("  " + self.pipeline_report(tail, now))
        if " |eco=" in body:
            try:
                eco = float(body.split(" |eco=")[1].split("|")[0])
                fa = float(body.split("|fa=")[1].split("|")[0])
                if eco > 0:
                    Handler.jpeg_samples.append(now * 1000 - fa - eco)
            except (IndexError, ValueError):
                pass
        sync = Handler.audio_sync
        if sync is not None:
            m = re.search(r"video lag ([0-9.]+) s", body)
            if m:
                sync.observe(float(m.group(1)) + VIDEO_PIPELINE + DISPLAY_LATENCY)
            elif " |eco=" in body and Handler.jpeg_samples:
                tiles = sorted(Handler.jpeg_samples)[len(Handler.jpeg_samples) // 2] / 1000
                sync.observe(tiles + DISPLAY_LATENCY)
        self._send(204, "text/plain", b"")

    def pipeline_report(self, ct_text, now):
        """Estimated delay from ffmpeg output to the receiver's screen, and queue state."""
        bc = self.bc
        parts = []
        try:
            ct = float(ct_text)
            best = None
            for vt, wall in list(bc.timeline):
                if vt <= ct:
                    best = (vt, wall)
            if best:
                delay = now - (best[1] + (ct - best[0]))
                parts.append(f"latency {delay:.2f} s (excl. capture and display)")
        except ValueError:
            pass
        with bc.lock:
            queues = [len(c["queue"]) for c in bc.clients.values()]
            socks = [c.get("sock") for c in bc.clients.values() if c.get("sock")]
        unsent = 0
        for sk in socks:
            try:
                unsent += sk.getsockopt(socket.SOL_SOCKET, SO_NWRITE)
            except OSError:
                pass
        parts.append(f"queue {max(queues) if queues else 0} fragments, {unsent // 1024} KB unsent, "
                     f"{bc.dropped} fragments dropped")
        return ", ".join(parts)

    def do_OPTIONS(self):
        if not self._client_allowed():
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._client_allowed():
            return
        path = self.path.split("?")[0]
        if self.opt.verbose and path != "/stream.mjpg":
            log(f"request from {self.client_address[0]}: GET {self.path[:80]}")
        if path == "/hello":
            if self.client_address[0] != Handler.last_hello:
                Handler.last_hello = self.client_address[0]
                log(f"receiver {self.client_address[0]}: discovered this server")
            info = {"app": "devicecast", "name": socket.gethostname().split(".")[0],
                    "width": self.opt.width, "height": self.opt.height, "fps": self.opt.fps,
                    "codec": self.opt.codec}
            self._send(200, "application/json", json.dumps(info).encode())
        elif path in ("/", "/receiver.html", "/receiver.js"):
            name = "receiver.js" if path == "/receiver.js" else "receiver.html"
            try:
                with open(os.path.join(RECEIVER_DIR, name), "rb") as f:
                    body = f.read()
            except OSError:
                body = f"{name} is missing from the receiver folder".encode()
            if name == "receiver.js":
                q = self.path.split("?", 1)[1] if "?" in self.path else ""
                log(f"receiver {self.client_address[0]}: loading receiver ({printable(q.replace('&', ', '))})")
            ctype = "application/javascript; charset=utf-8" if name.endswith(".js") else "text/html; charset=utf-8"
            self._send(200, ctype, body)
        elif path == "/ws":
            self.handle_ws()
        elif path == "/wsj":
            self.handle_wsj()
        elif path == "/wst":
            self.handle_wst()
        elif path == "/stream.mjpg":
            self.handle_mjpeg()
        else:
            self._send(404, "text/plain", b"not found")

    # WebSocket, server side only: handshake, outgoing frames, ping/pong.
    def handle_ws(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._send(400, "text/plain", b"websocket expected")
            return
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        sock = self.connection
        sock.settimeout(None)
        log(f"receiver {self.client_address[0]}: WebSocket open (MediaSource)")
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SNDBUF)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        cid = self.bc.subscribe()
        with self.bc.lock:
            if cid in self.bc.clients:
                self.bc.clients[cid]["sock"] = sock
        closed = threading.Event()
        threading.Thread(target=self._ws_reader, args=(sock, closed), daemon=True).start()
        sent = 0
        try:
            while not closed.is_set():
                chunk = self.bc.next_chunk(cid)
                if chunk is None:
                    break
                if chunk == b"":
                    self._ws_send(sock, 0x9, b"")
                    continue
                self._ws_send(sock, 0x2, chunk)
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            closed.set()
            self.bc.unsubscribe(cid)
            log(f"receiver {self.client_address[0]}: WebSocket closed after {sent / 1e6:.1f} MB")
            self.close_connection = True

    @staticmethod
    def _recv_exact(sock, n):
        data = b""
        while len(data) < n:
            part = sock.recv(n - len(data))
            if not part:
                return b""
            data += part
        return data

    @staticmethod
    def _ws_send(sock, opcode, payload):
        ln = len(payload)
        if ln < 126:
            hdr = bytes([0x80 | opcode, ln])
        elif ln < 65536:
            hdr = bytes([0x80 | opcode, 126]) + struct.pack(">H", ln)
        else:
            hdr = bytes([0x80 | opcode, 127]) + struct.pack(">Q", ln)
        sock.sendall(hdr + payload)

    def _ws_read_frame(self, sock):
        """Read one client frame. Returns (opcode, payload), or None when the connection must close:
        EOF, an unmasked frame (forbidden for clients by RFC 6455) or an oversized payload."""
        hdr = self._recv_exact(sock, 2)
        if not hdr or not hdr[1] & 0x80:
            return None
        opcode = hdr[0] & 0x0F
        ln = hdr[1] & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._recv_exact(sock, 2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._recv_exact(sock, 8))[0]
        if ln > WS_MAX_CLIENT_PAYLOAD:
            return None
        mask = self._recv_exact(sock, 4)
        if len(mask) != 4:
            return None
        payload = self._recv_exact(sock, ln) if ln else b""
        return opcode, bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

    def _ws_reader(self, sock, closed, on_message=None):
        """Read client frames until close: answer pings, pass text/binary payloads to on_message."""
        try:
            while not closed.is_set():
                frame = self._ws_read_frame(sock)
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    self._ws_send(sock, 0xA, payload)
                elif on_message and opcode in (0x1, 0x2):
                    on_message(payload)
        except Exception:  # noqa: BLE001
            pass
        closed.set()

    # Tiles WebSocket: sharp, minimum latency.
    def handle_wst(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._send(400, "text/plain", b"websocket expected")
            return
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        sock = self.connection
        sock.settimeout(None)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 256 * 1024)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        log(f"receiver {self.client_address[0]}: WebSocket open (tiles, 1920x1080)")
        closed = threading.Event()
        window = threading.Condition()
        state = {"inflight": 0, "tvq": 0, "sizes": collections.deque(), "undrawn": 0,
                 "sent_at": collections.deque(), "ack_ms": []}

        def on_ack(payload):
            # "a<N>": one update drawn, N updates still queued on the receiver
            with window:
                state["inflight"] = max(0, state["inflight"] - 1)
                if state["sizes"]:
                    state["undrawn"] -= state["sizes"].popleft()
                if state["sent_at"]:
                    state["ack_ms"].append(1000 * (time.time() - state["sent_at"].popleft()))
                try:
                    state["tvq"] = int(payload[1:] or b"0")
                except ValueError:
                    state["tvq"] = 0
                window.notify()

        def reader():
            self._ws_reader(sock, closed, on_ack)
            with window:
                window.notify()

        threading.Thread(target=reader, daemon=True).start()
        hub = self.thub
        me = object()
        hub.owner = me
        if Handler.audio_sync is not None:
            Handler.audio_sync.start()
        sent = 0
        cmd = "full"
        send_lock = threading.Lock()

        def send(opcode, payload):
            with send_lock:
                self._ws_send(sock, opcode, payload)

        def cursor_forwarder():
            # forward cursor shape and position as soon as they change; the receiver draws it on top
            seq, shape_sent = -1, None
            try:
                while not closed.is_set() and hub.owner is me:
                    with hub.cur_cond:
                        if hub.cur_seq == seq:
                            hub.cur_cond.wait(1)
                        seq, shape, pos = hub.cur_seq, hub.cur_shape, hub.cur_pos
                    if shape and shape is not shape_sent:
                        send(0x1, shape.encode())
                        shape_sent = shape
                    if pos:
                        send(0x1, pos.encode())
            except OSError:
                pass

        threading.Thread(target=cursor_forwarder, daemon=True).start()
        # Level 0-3: how much to reduce moving regions. Rises when the network is saturated,
        # falls when it is idle.
        level, lvl_t, lvl_wait, calm = 0, time.time(), 0.0, 0
        mode, video_since, novideo_since = "tiles", None, None

        def strip_meta(data, n):
            """Remove the "video region" entry (a zero-byte tile, always last) and return it."""
            off, meta = 16, None
            for i in range(n):
                x, y, w, h, ln = struct.unpack_from(">HHHHI", data, off)
                if ln == 0 and i == n - 1:
                    meta = (x, y, w, h)
                    data = data[:off] + data[off + 12:]
                    data = data[:12] + struct.pack(">I", n - 1) + data[16:]
                    return data, n - 1, meta
                off += 12 + ln
            return data, n, None
        prof = {"win": 0.0, "req": 0.0, "send": 0.0, "n": 0, "t": time.time(), "net": 0, "tv": 0, "bytes": 0}
        try:
            while not closed.is_set() and hub.owner is me:
                if mode == "video":
                    # the receiver is playing H.264; only check whether the video is still running
                    time.sleep(0.3)
                    got = hub.request("probe")
                    if got is None:
                        break
                    _, _, meta = strip_meta(*got)
                    if meta:
                        novideo_since = None
                    else:
                        novideo_since = novideo_since or time.time()
                        if time.time() - novideo_since >= 2:
                            log("  video ended: back to tiles")
                            send(0x1, b"mode tiles")
                            Handler.jpeg_samples.clear()
                            if Handler.audio_sync is not None:
                                Handler.audio_sync.reset()
                            mode, cmd, video_since = "tiles", "full", None
                            with window:
                                state.update(inflight=0, undrawn=0, tvq=0)
                                state["sizes"].clear(); state["sent_at"].clear()
                    continue
                a = time.time()
                # Throttle on data still sitting in the local send buffer. This does not depend on
                # receiver acks, which arrive late because the TV batches small packets.
                while not closed.is_set():
                    try:
                        unsent = sock.getsockopt(socket.SOL_SOCKET, SO_NWRITE)
                    except OSError:
                        unsent = 0
                    with window:
                        tv_behind = state["inflight"] > 0 and (state["tvq"] >= 3 or state["undrawn"] > TILE_MAX_UNDRAWN)
                        if state["inflight"] < TILE_WINDOW and unsent <= TILE_MAX_UNSENT and not tv_behind:
                            break
                    if unsent > TILE_MAX_UNSENT or state["undrawn"] > TILE_MAX_UNDRAWN:
                        prof["net"] += 1
                    else:
                        prof["tv"] += 1
                    time.sleep(0.004)
                b = time.time()
                if closed.is_set():
                    break
                lvl_wait += b - a
                if b - lvl_t >= 1.0:
                    frac = lvl_wait / (b - lvl_t)
                    # rise at once when saturated; fall only after 3 consecutive idle seconds
                    calm = calm + 1 if frac < 0.1 else 0
                    new = min(3, level + 1) if frac > 0.35 else max(0, level - 1) if calm >= 3 else level
                    if new < level:
                        calm = 0
                    if not self.opt.auto_quality:
                        new = 0
                    if new != level:
                        log(f"  network {'saturated' if new > level else 'idle'} (waiting {frac:.0%}): level {level} -> {new}")
                        level = new
                    lvl_t, lvl_wait = b, 0.0
                got = hub.request(cmd if cmd == "full" else f"next {level}")
                c = time.time()
                if got is None:
                    log("WARNING: no data from capture (tiles). " +
                        (hub.last_error.strip()[-200:] if hub.last_error else ""))
                    log("Screen Recording permission is probably missing for the app that started the server.")
                    if Handler.httpd is not None:
                        threading.Thread(target=Handler.httpd.shutdown, daemon=True).start()
                    break
                data, n = got
                data, n, meta = strip_meta(data, n)
                if self.opt.auto_video:
                    if meta:
                        video_since = video_since or time.time()
                        if time.time() - video_since >= 1.5:
                            log(f"  video detected, {meta[2]}x{meta[3]} at ({meta[0]},{meta[1]}): switching to H.264")
                            send(0x1, b"mode h264")
                            Handler.jpeg_samples.clear()
                            if Handler.audio_sync is not None:
                                Handler.audio_sync.reset()
                            mode, novideo_since = "video", None
                            continue
                    else:
                        video_since = None
                if n == 0:
                    if cmd == "full":
                        continue
                    send(0x9, b"")
                    continue
                cmd = "next"
                with window:
                    state["inflight"] += 1
                    state["sizes"].append(len(data))
                    state["undrawn"] += len(data)
                    state["sent_at"].append(time.time())
                send(0x2, data)
                sent += len(data)
                d = time.time()
                prof["win"] += b - a; prof["req"] += c - b; prof["send"] += d - c; prof["n"] += 1
                prof["bytes"] += len(data)
                if d - prof["t"] > 10 and prof["n"]:
                    n, el = prof["n"], d - prof["t"]
                    with window:
                        acks, state["ack_ms"] = sorted(state["ack_ms"]), []
                    ack_txt = (f", ack p50 {acks[len(acks) // 2]:.0f} p95 {acks[int(len(acks) * 0.95)]:.0f} ms"
                               if acks else "")
                    log(f"  server: {n / el:.1f} upd/s, {prof['bytes'] * 8 / el / 1e6:.1f} Mbit/s, "
                        f"wait {1000 * prof['win'] / n:.0f} ms/upd (network {prof['net'] * 4} ms, "
                        f"receiver {prof['tv'] * 4} ms), capture+encode {1000 * prof['req'] / n:.0f} ms/upd, "
                        f"send {1000 * prof['send'] / n:.1f} ms/upd{ack_txt}")
                    prof.update(win=0.0, req=0.0, send=0.0, n=0, t=d, net=0, tv=0, bytes=0)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            closed.set()
            log(f"receiver {self.client_address[0]}: WebSocket closed after {sent / 1e6:.1f} MB")
            self.close_connection = True
            if hub.owner is me:
                hub.owner = None
                hub.stop_when_idle()
                if Handler.audio_sync is not None:
                    Handler.audio_sync.stop()

    # JPEG WebSocket: minimum latency.
    def handle_wsj(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._send(400, "text/plain", b"websocket expected")
            return
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        sock = self.connection
        sock.settimeout(None)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 128 * 1024)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        log(f"receiver {self.client_address[0]}: WebSocket open (JPEG, minimum latency)")
        closed = threading.Event()
        threading.Thread(target=self._ws_reader, args=(sock, closed), daemon=True).start()
        hub = self.jhub
        hub.subscribe()
        last, sent = 0, 0
        try:
            while not closed.is_set():
                seq, ts, frame = hub.wait_frame(last)
                if seq == -1:
                    break
                if seq == last or frame is None:
                    self._ws_send(sock, 0x9, b"")
                    continue
                last = seq
                # 8-byte prefix: sender clock in ms, echoed back by the receiver to measure real latency
                self._ws_send(sock, 0x2, struct.pack(">d", ts * 1000) + frame)
                sent += len(frame)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            closed.set()
            hub.unsubscribe()
            log(f"receiver {self.client_address[0]}: WebSocket closed after {sent / 1e6:.1f} MB")
            self.close_connection = True

    def handle_mjpeg(self):
        log(f"receiver {self.client_address[0]}: MJPEG fallback")
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=ffmpeg")
        self._cors()
        self.send_header("Connection", "close")
        self.end_headers()
        proc = subprocess.Popen(mjpeg_cmd(self.opt), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        drain_stderr(proc)
        try:
            first = read_with_timeout(proc.stdout, 65536, START_TIMEOUT)
            if not first:
                log("MJPEG: ffmpeg produced no data")
                return
            self.wfile.write(first)
            while True:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            stop_proc(proc)
            self.close_connection = True


def close_tv_app(tv_ip):
    try:
        req = urllib.request.Request(f"http://{tv_ip}:8001/api/v2/applications/{APP_ID}", method="DELETE")
        with urllib.request.urlopen(req, timeout=5):
            pass
        log("Device Cast app closed on the TV")
    except Exception as e:  # noqa: BLE001
        log(f"(could not close the app on the TV: {e})")


def launch_tv_app(tv_ip):
    """Launch Device Cast on the TV, first with sdb, then with the Smart View REST API.

    In hotel mode the REST launch opens the app but the system suspends it after the first
    request; launched through sdb it runs in the foreground."""
    sdb = os.environ.get("TIZEN_SDB") or os.path.join(ROOT, "tools.noindex", "tizen-studio", "tools", "sdb")
    if os.path.exists(sdb):
        try:
            subprocess.run([sdb, "connect", tv_ip], capture_output=True, timeout=8)
            r = subprocess.run([sdb, "-s", f"{tv_ip}:26101", "shell", "0", "was_execute", APP_ID],
                               capture_output=True, text=True, timeout=12)
            if "launched" in (r.stdout + r.stderr).lower():
                log("Device Cast app launched on the TV (sdb)")
                return True
            log("sdb: " + (r.stdout + r.stderr).strip()[:120])
        except subprocess.TimeoutExpired:
            log("sdb not responding: restarting its server and trying the REST API")
            subprocess.run([sdb, "kill-server"], capture_output=True, timeout=5)
        except Exception as e:  # noqa: BLE001
            log(f"sdb failed: {e}")
    try:
        req = urllib.request.Request(f"http://{tv_ip}:8001/api/v2/applications/{APP_ID}",
                                     data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            log(f"Device Cast app launched on the TV (REST): {r.read().decode('utf-8', 'replace').strip()[:40]}")
            return True
    except socket.timeout:
        log("launch request sent to the TV (slow reply)")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"could not launch the app on the TV ({e}): open it from the Apps menu")
        return False


def local_ip_towards(target="192.0.2.1"):
    """Local address of the interface that routes to target. connect() on a UDP socket only selects
    the route and sends no packets; the default is a documentation address (RFC 5737), so it
    resolves to the default-route interface."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 80))
        return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        s.close()


def ip_arg(value):
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an IP address: {value}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=PORT, help=f"HTTP/WebSocket port (default {PORT})")
    ap.add_argument("--bind", metavar="ADDR",
                    help="listen address (default: the local address on the route to --tv, else 0.0.0.0)")
    ap.add_argument("--allow", metavar="IP", type=ip_arg, action="append", default=[],
                    help="allow this client IP (repeatable). Loopback and --tv are always allowed; "
                         "without --tv or --allow every client is accepted")
    ap.add_argument("--source", choices=["screen", "test", "test-scroll", "test-video"], default="screen",
                    help="synthetic test sources; test-scroll only with --codec tiles")
    ap.add_argument("--screen", help="screen index or name (default: Capture screen 0)")
    ap.add_argument("--audio", help="audio device index or name (e.g. BlackHole); default: silence")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--bitrate", default="4M", help="H.264 bitrate (default 4M)")
    ap.add_argument("--codec", choices=["h264", "jpeg", "tiles"], default="h264",
                    help="h264: smooth video with audio; jpeg: minimum latency, 720p, up to 15 fps; "
                         "tiles: changed regions only, native 1080p, minimum latency")
    ap.add_argument("--jpeg-q", type=int, default=6, help="ffmpeg JPEG quality, 2 (high) to 31 (low)")
    ap.add_argument("--no-auto-quality", dest="auto_quality", action="store_false",
                    help="tiles: do not reduce moving regions when the network is saturated")
    ap.add_argument("--no-auto-video", dest="auto_video", action="store_false",
                    help="tiles: do not switch to H.264 when a video plays on screen")
    ap.add_argument("--no-audio-sync", dest="audio_sync", action="store_false",
                    help="tiles: do not delay the computer's audio to match the picture on the TV")
    ap.add_argument("--audio-offset", type=float, default=0.0, metavar="SECONDS",
                    help="extra audio delay (negative: less), to fine-tune lip sync")
    ap.add_argument("--encoder", choices=["auto", "h264_videotoolbox", "libx264"], default="auto")
    ap.add_argument("--no-cursor", action="store_true", help="do not capture the mouse cursor")
    ap.add_argument("--tv", metavar="IP", help="TV address: launch the Device Cast app on it and allow it as a client")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--stop-deskpad", action="store_true", help="quit DeskPad on exit (used by devicecast.py)")
    opt = ap.parse_args()

    if opt.encoder == "auto":
        enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        opt.encoder = "h264_videotoolbox" if " h264_videotoolbox " in enc else "libx264"
    if opt.source == "screen" and deskpad.wants_extend(opt.screen):
        idx = deskpad.start()
        opt.screen = f"Capture screen {idx}" if idx is not None else None
        opt.stop_deskpad = opt.stop_deskpad or idx is not None
    if opt.source == "screen":
        video, audio = avfoundation_devices()
        screens = [(i, n) for i, n in video if n.lower().startswith("capture screen")]
        if not screens:
            sys.exit("ffmpeg found no 'Capture screen' device.")
        wanted = [i for i, n in screens if opt.screen and (opt.screen == str(i) or opt.screen.lower() in n.lower())]
        if opt.screen and not wanted:
            # e.g. the DeskPad virtual display was chosen in the menu but DeskPad is not running
            log(f"screen '{opt.screen}' not available (DeskPad not running?): using the main screen")
        opt.screen_index = wanted[0] if wanted else screens[0][0]
        opt.screen_name = dict(screens).get(opt.screen_index, "Capture screen 0")
        opt.audio_index = resolve_device(opt.audio, audio)
    else:
        opt.screen_index = opt.audio_index = None

    if opt.codec == "tiles":
        opt.bitrate = "3M"   # H.264 bitrate used while a video plays; fits typical Wi-Fi
        if opt.audio_sync and opt.source == "screen":
            Handler.audio_sync = AudioSync(opt.audio_offset)
    if opt.codec == "tiles" and not ensure_screencap():
        log("tile capture not available: using JPEG mode")
        opt.codec = "jpeg"
    opt.display_n = 0
    if opt.source == "screen" and getattr(opt, "screen_name", None):
        try:
            opt.display_n = int(opt.screen_name.split()[-1])
        except ValueError:
            pass
    if opt.codec == "jpeg" and opt.width > 1280:
        # measured: the TV decodes a 720p JPEG in ~40 ms, a 1080p one in 80 ms to over 1 s
        opt.width, opt.height = 1280, 720
        log("JPEG mode: using 1280x720 (the TV cannot decode 1080p fast enough)")

    if opt.tv or opt.allow:
        allowed = set(opt.allow)
        if opt.tv:
            allowed.add(opt.tv)
            try:
                allowed.add(socket.gethostbyname(opt.tv))
            except OSError:
                pass
        Handler.allowed = allowed
    else:
        log("WARNING: no --tv or --allow given: accepting requests from any host on the network")
    bind = opt.bind or (local_ip_towards(opt.tv) if opt.tv else "0.0.0.0")

    Handler.opt = opt
    Handler.bc = Broadcaster(opt)
    Handler.jhub = JpegHub(opt)
    Handler.thub = TileHub(opt)
    try:
        httpd = ThreadingHTTPServer((bind, opt.port), Handler)
    except OSError as e:
        sys.exit(f"Cannot listen on {bind}:{opt.port}: {e}")
    httpd.daemon_threads = True
    Handler.httpd = httpd
    ip = bind if bind != "0.0.0.0" else local_ip_towards(opt.tv or "192.0.2.1")
    detail = (f"JPEG q{opt.jpeg_q}, max 15 fps" if opt.codec == "jpeg"
              else "tiles, changed regions only" if opt.codec == "tiles"
              else f"{opt.fps} fps, {opt.bitrate}, {opt.encoder}")
    log(f"Serving on http://{ip}:{opt.port}/ ({opt.width}x{opt.height}, {detail})")
    if opt.tv:
        launch_tv_app(opt.tv)
    log("Waiting for the receiver. Press Ctrl+C to stop.")

    def on_signal(*_):
        threading.Thread(target=httpd.shutdown, daemon=True).start()
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    failed = False
    try:
        httpd.serve_forever()
    finally:
        bc = Handler.bc
        failed = bool(bc.last_error) and bc.init is None
        bc.stopping = True
        with bc.lock:
            bc.clients.clear()
        if bc.proc:
            stop_proc(bc.proc)
        jh = Handler.jhub
        jh.stopping = True
        with jh.cond:
            jh.clients = 0
            jh.cond.notify_all()
        if jh.proc:
            stop_proc(jh.proc)
        Handler.thub.stop()
        if Handler.audio_sync is not None:
            Handler.audio_sync.stop()
        failed = failed or (bool(jh.last_error) and jh.frames_made == 0 and bc.init is None)
        if opt.tv:
            close_tv_app(opt.tv)
        if opt.stop_deskpad:
            deskpad.stop()
        log("Server stopped.")
    sys.stderr.flush()
    os._exit(2 if failed else 0)


if __name__ == "__main__":
    main()
