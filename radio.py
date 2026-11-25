#!/usr/bin/env python3
"""Navidrome -> 24/7 random MP3 radio stream.

One ffmpeg encode, one shared ring buffer, N listeners.  Every listener is
attached to the same byte stream (live edge, minus --prebuffer-kb), so the
whole server hears the same song at the same moment, like a real station.

Endpoints:
    /radio.mp3     MP3 stream, Transfer-Encoding: chunked, no Content-Length
    /nowplaying    JSON status
    /              one line of help

Point Etched's Radio block at http://<host>:8080/radio.mp3
"""

import argparse
import hashlib
import json
import logging
import os
import random
import signal
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("radio")

# --------------------------------------------------------------------------
# shared ring buffer
# --------------------------------------------------------------------------


class Ring:
    """Byte buffer with absolute offsets; new readers attach at the live edge."""

    def __init__(self, capacity):
        self._buf = bytearray(capacity)
        self._cap = capacity
        self._lock = threading.Lock()
        self._start = 0
        self._end = 0

    def write(self, data):
        n = len(data)
        if n == 0:
            return
        with self._lock:
            if n >= self._cap:
                data = data[-self._cap:]
                n = len(data)
                self._start = self._end  # everything older is gone
            pos = self._end % self._cap
            first = min(n, self._cap - pos)
            self._buf[pos:pos + first] = data[:first]
            if n > first:
                self._buf[0:n - first] = data[first:]
            self._end += n
            if self._end - self._start > self._cap:
                self._start = self._end - self._cap

    def read(self, pos, maxlen):
        with self._lock:
            if pos < self._start:
                pos = self._start
            avail = self._end - pos
            if avail <= 0:
                return b"", pos
            n = min(avail, maxlen)
            p = pos % self._cap
            first = min(n, self._cap - p)
            data = bytes(self._buf[p:p + first])
            if n > first:
                data += bytes(self._buf[0:n - first])
            return data, pos + n

    @property
    def end(self):
        with self._lock:
            return self._end

    @property
    def start(self):
        with self._lock:
            return self._start


# --------------------------------------------------------------------------
# track sources
# --------------------------------------------------------------------------


class Navidrome:
    """Minimal Subsonic client (token auth)."""

    def __init__(self, base, user, password, music_folder=None, genre=None,
                 size=50, timeout=20):
        self.base = base.rstrip("/")
        self.user = user
        self.password = password
        self.music_folder = music_folder
        self.genre = genre
        self.size = size
        self.timeout = timeout

    def _auth(self):
        salt = os.urandom(8).hex()
        token = hashlib.md5((self.password + salt).encode()).hexdigest()
        return {"u": self.user, "t": token, "s": salt,
                "v": "1.16.1", "c": "tfg-radio", "f": "json"}

    def _url(self, endpoint, **params):
        params.update(self._auth())
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return f"{self.base}/rest/{endpoint}.view?{q}"

    def random_songs(self):
        q = {"size": self.size}
        if self.music_folder:
            q["musicFolderId"] = self.music_folder
        if self.genre:
            q["genre"] = self.genre
        url = self._url("getRandomSongs", **q)
        with urllib.request.urlopen(url, timeout=self.timeout) as resp:
            doc = json.load(resp)
        body = doc.get("subsonic-response", {})
        if body.get("status") != "ok":
            raise RuntimeError(f"getRandomSongs failed: {body.get('error')}")
        songs = (body.get("randomSongs") or {}).get("song") or []
        if isinstance(songs, dict):  # single result
            songs = [songs]
        return songs

    def stream_url(self, song_id):
        return self._url("stream", id=song_id)


class TrackSource:
    """Random, no-immediate-repeat queue."""

    def __init__(self, navidrome=None, directory=None, history=40):
        self.navidrome = navidrome
        self.directory = directory
        self.history = history
        self._recent = []
        self._queue = []
        self._lock = threading.Lock()

    def _remember(self, key):
        self._recent.append(key)
        if len(self._recent) > self.history:
            self._recent.pop(0)

    def _refill(self):
        if self.directory:
            files = []
            for root, _, names in os.walk(self.directory):
                for name in names:
                    if name.lower().endswith((".mp3", ".flac", ".ogg", ".opus",
                                               ".m4a", ".wav")):
                        files.append(os.path.join(root, name))
            if not files:
                raise RuntimeError(f"no audio files under {self.directory}")
            random.shuffle(files)
            items = [{"key": p, "url": p, "title": os.path.basename(p),
                      "artist": ""} for p in files]
        else:
            songs = self.navidrome.random_songs()
            random.shuffle(songs)
            items = [{"key": s["id"], "url": self.navidrome.stream_url(s["id"]),
                      "title": s.get("title", "?"),
                      "artist": s.get("artist", "")} for s in songs]
        fresh = [i for i in items if i["key"] not in self._recent]
        self._queue = fresh or items
        LOG.info("queue refilled with %d track(s)", len(self._queue))

    def next(self):
        with self._lock:
            for _ in range(3):
                if not self._queue:
                    self._refill()
                if not self._queue:
                    raise RuntimeError("track source returned nothing")
                track = self._queue.pop()
                if track["key"] in self._recent and len(self._queue) > 1:
                    continue
                self._remember(track["key"])
                return track
            self._remember(track["key"])
            return track


# --------------------------------------------------------------------------
# producer
# --------------------------------------------------------------------------


class Pacer:
    """Hold the encoder at realtime so the ring stays near the live edge."""

    def __init__(self, bytes_per_second, burst=0.6):
        self.bps = bytes_per_second
        self.burst = burst
        self.t0 = time.monotonic()
        self.written = 0

    def add(self, n):
        self.written += n
        ahead = self.written / self.bps - (time.monotonic() - self.t0)
        if ahead > self.burst:
            time.sleep(ahead - self.burst)


class Producer(threading.Thread):
    def __init__(self, ring, source, ffmpeg, bitrate, sample_rate, channels):
        super().__init__(name="producer", daemon=True)
        self.ring = ring
        self.source = source
        self.ffmpeg = ffmpeg
        self.bitrate = bitrate
        self.sample_rate = sample_rate
        self.channels = channels
        self.pacer = Pacer(bitrate * 1000 / 8)
        self.stop_event = threading.Event()
        self.current = None
        self.failures = 0
        self.ffmpeg_cmd = None
        self.proc = None

    def command(self, url):
        net = ["-timeout", "20000000"] if url.startswith(("http://", "https://")) else []
        return [
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-rw_timeout", "20000000", *net,
            "-i", url,
            "-vn", "-map", "0:a:0",
            "-c:a", "libmp3lame", "-b:a", f"{self.bitrate}k",
            "-ar", str(self.sample_rate), "-ac", str(self.channels),
            "-id3v2_version", "0", "-write_xing", "0",
            "-f", "mp3", "pipe:1",
        ]

    def run(self):
        while not self.stop_event.is_set():
            try:
                track = self.source.next()
            except Exception as exc:  # noqa: BLE001
                LOG.error("track source failed: %s", exc)
                time.sleep(5)
                continue
            self.current = track
            self.ffmpeg_cmd = self.command(track["url"])
            LOG.info("playing %s - %s", track.get("artist") or "?", track["title"])
            produced = self._encode(track)
            if produced == 0:
                self.failures += 1
                LOG.warning("track produced no audio (%d in a row)", self.failures)
                if self.failures >= 3:
                    time.sleep(5)
                    self.failures = 0
            else:
                self.failures = 0

    def _encode(self, track):
        try:
            proc = subprocess.Popen(
                self.ffmpeg_cmd, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0)
        except OSError as exc:
            LOG.error("cannot start ffmpeg: %s", exc)
            return 0
        self.proc = proc
        stderr_tail = []
        drain = threading.Thread(
            target=self._drain, args=(proc.stderr, stderr_tail), daemon=True)
        drain.start()
        produced = 0
        try:
            while not self.stop_event.is_set():
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                self.ring.write(chunk)
                self.pacer.add(len(chunk))
                produced += len(chunk)
        finally:
            if self.stop_event.is_set():
                proc.kill()
            try:
                proc.stdout.close()
            except OSError:
                pass
            proc.wait()
            drain.join(timeout=2)
        if proc.returncode not in (0, -9) and produced < 1024:
            LOG.warning("ffmpeg exit %s for %s: %s", proc.returncode,
                        track["url"], b" ".join(stderr_tail[-4:]).decode(
                            "utf-8", "replace")[:400])
        return produced

    @staticmethod
    def _drain(stream, sink):
        try:
            for line in stream:
                sink.append(line)
                if len(sink) > 40:
                    del sink[0]
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Radio:
    def __init__(self, ring, producer, prebuffer, token=None):
        self.ring = ring
        self.producer = producer
        self.prebuffer = prebuffer
        self.token = token
        self.listeners = 0
        self.skips = 0
        self.started = time.time()
        self._lock = threading.Lock()


def make_handler(radio):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "tfg-radio/1.0"

        def log_message(self, fmt, *args):  # noqa: A003
            LOG.debug("%s %s", self.address_string(), fmt % args)

        def version_string(self):
            return self.server_version

        def _authorized(self, query):
            return not radio.token or query.get("k") == radio.token

        def _status(self):
            p = radio.producer
            return {
                "listeners": radio.listeners,
                "skips": radio.skips,
                "bytes": radio.ring.end,
                "uptime": round(time.time() - radio.started, 1),
                "track": ({"title": p.current["title"],
                           "artist": p.current.get("artist", "")}
                          if p.current else None),
                "stream": "/radio.mp3",
            }

        def _common_headers(self):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")

        def do_HEAD(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if not self._authorized(dict(urllib.parse.parse_qsl(parsed.query))):
                self.send_error(403)
                return
            if path not in ("/radio.mp3", "/nowplaying", "/"):
                self.send_error(404)
                return
            self.send_response(200)
            self._common_headers()
            if path == "/radio.mp3":
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("icy-name", "TFG Radio")
                self.send_header("icy-br", str(radio.producer.bitrate))
            else:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if not self._authorized(dict(urllib.parse.parse_qsl(parsed.query))):
                self.send_error(403)
                return
            if path == "/nowplaying":
                body = json.dumps(self._status(), ensure_ascii=False).encode()
                self.send_response(200)
                self._common_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
            if path == "/":
                body = (b"TFG radio: /radio.mp3 (audio/mpeg, chunked), "
                        b"/nowplaying (json)\n")
                self.send_response(200)
                self._common_headers()
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path != "/radio.mp3":
                self.send_error(404)
                return
            self.stream()

        def stream(self):
            self.connection.settimeout(60)
            self.send_response(200)
            self._common_headers()
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("icy-name", "TFG Radio")
            self.send_header("icy-genre", "Random")
            self.send_header("icy-br", str(radio.producer.bitrate))
            self.end_headers()
            with radio._lock:
                radio.listeners += 1
            cursor = max(radio.ring.start, radio.ring.end - radio.prebuffer)
            LOG.info("listener %s joined at %d (%d live)", self.address_string(),
                     cursor, radio.listeners)
            try:
                while True:
                    start = radio.ring.start
                    if cursor < start:  # listener fell out of the buffer
                        with radio._lock:
                            radio.skips += 1
                        cursor = start
                    data, nxt = radio.ring.read(cursor, 32768)
                    if not data:
                        time.sleep(0.02)
                        continue
                    cursor = nxt
                    self.wfile.write(b"%x\r\n" % len(data))
                    self.wfile.write(data)
                    self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError, TimeoutError,
                    OSError):
                pass
            finally:
                with radio._lock:
                    radio.listeners -= 1
                LOG.info("listener %s left (%d live)", self.address_string(),
                         radio.listeners)

    return Handler


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--navidrome-url", help="e.g. http://127.0.0.1:4533")
    ap.add_argument("--user")
    ap.add_argument("--password", default=os.environ.get("NAVIDROME_PASSWORD"),
                    help="or set NAVIDROME_PASSWORD")
    ap.add_argument("--music-folder", help="Navidrome music folder id")
    ap.add_argument("--genre", help="only random songs of this genre")
    ap.add_argument("--dir", help="play files from a local directory instead")
    ap.add_argument("--listen", default="0.0.0.0:8080")
    ap.add_argument("--bitrate", type=int, default=160, help="kbit/s")
    ap.add_argument("--sample-rate", type=int, default=44100)
    ap.add_argument("--channels", type=int, default=2)
    ap.add_argument("--buffer-mb", type=int, default=16)
    ap.add_argument("--prebuffer-kb", type=int, default=64)
    ap.add_argument("--token", help="require ?k=TOKEN on every request")
    ap.add_argument("--ffmpeg", default=os.environ.get("FFMPEG", "ffmpeg"))
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    if not args.dir and not (args.navidrome_url and args.user and args.password):
        ap.error("need --dir, or --navidrome-url/--user/--password")
    return args


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s")

    navidrome = None
    if not args.dir:
        navidrome = Navidrome(args.navidrome_url, args.user, args.password,
                              music_folder=args.music_folder, genre=args.genre)
    source = TrackSource(navidrome=navidrome, directory=args.dir)
    ring = Ring(args.buffer_mb * 1024 * 1024)
    producer = Producer(ring, source, args.ffmpeg, args.bitrate,
                        args.sample_rate, args.channels)
    radio = Radio(ring, producer, args.prebuffer_kb * 1024, token=args.token)

    host, _, port = args.listen.rpartition(":")
    httpd = ThreadingHTTPServer((host or "0.0.0.0", int(port)), make_handler(radio))
    httpd.daemon_threads = True

    producer.start()
    suffix = f"?k={args.token}" if args.token else ""
    LOG.info("streaming on http://%s:%s/radio.mp3%s (%d kbit/s)", host, port,
             suffix, args.bitrate)
    stop = threading.Event()

    def _on_signal(signum, _frame):
        LOG.info("signal %s, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    while not stop.wait(0.5):
        pass

    producer.stop_event.set()
    if producer.proc is not None:
        try:
            producer.proc.kill()
        except OSError:
            pass
    httpd.shutdown()
    httpd.server_close()
    LOG.info("stopped")


if __name__ == "__main__":
    main()
