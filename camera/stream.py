"""Live MJPEG-over-HTTP view of the camera, for a browser on the LAN.

Structure is lifted from the owner's auto-drone project
(streaming/mjpeg_server.py): a StreamBuffer guarded by a Condition, a
ThreadingHTTPServer, and a `multipart/x-mixed-replace` response that never
ends. That shape is right and there is no reason to invent a different one.

WHAT IS DELIBERATELY DIFFERENT, AND WHY

1. It lives INSIDE the camera service. The sensor is an exclusive kernel
   resource -- one process may hold it. A standalone stream script would
   either take the camera away from Hermes or fail to start, depending only on
   which ran first. So the stream is another consumer of the frames the
   service already grabs, not another owner of the camera.

2. Viewers are COUNTED, and a viewer is a reason to be awake. The service is
   lazy by design (closed at rest, docs/CAMERA.md). Opening the page wakes the
   sensor; closing the last tab lets it fall asleep again. That is the "toggle"
   -- there is no separate on switch to forget about.

3. There is a TOKEN, and the drone has none. A drone stream shows a floor and
   a printed tag for the minutes you are flying. This shows a room,
   continuously, and this project's threat model (docs/SECURITY.md) previously
   had ONE network-facing socket: ssh. Adding an unauthenticated second one
   would be a real reduction in the security of the box, decided by accident.
   The token is stable across restarts so the URL stays bookmarkable.

4. The page refuses to claim it is live when it is not. If frames stop
   arriving it says NO SIGNAL rather than leaving the last one up looking
   current -- the same rule the panel follows.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import protocol

# Bound so a page left open in a dozen tabs cannot spawn unbounded threads.
MAX_VIEWERS = 6

# How long a handler waits for a new frame before giving up on the connection.
# Must be comfortably longer than a cold wake (~434 ms) or opening the page on
# a sleeping camera would disconnect before the first frame ever arrived.
FRAME_WAIT = 5.0

# A frame older than this is not "live". The page says NO SIGNAL instead of
# showing a stale picture that looks current.
STALE_AFTER = 2.0


class StreamBuffer:
    """Latest JPEG plus a sequence number, and the viewer count.

    The sequence number is what makes this correct rather than merely working.
    The drone version waits on the Condition and then reads `self.frame`, which
    means a viewer that was mid-write when a frame arrived silently skips it,
    and a viewer that connects between notifications waits for the NEXT frame
    even though a perfectly good one is already sitting there. Waiting on "seq
    changed from what I last sent" has neither problem.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._frame: bytes | None = None
        self._seq = 0
        self._at = 0.0
        self._viewers = 0
        self._last_viewer_at = 0.0

    # -- producer -------------------------------------------------------
    def push(self, jpeg: bytes, captured_at: float) -> None:
        with self._cond:
            self._frame = jpeg
            self._seq += 1
            self._at = captured_at
            self._cond.notify_all()

    def drop(self) -> None:
        """Sensor closed. Forget the frame so nothing can serve it as live."""
        with self._cond:
            self._frame = None
            self._seq += 1
            self._at = 0.0
            self._cond.notify_all()

    # -- consumers ------------------------------------------------------
    def latest(self) -> tuple[bytes | None, int, float]:
        with self._cond:
            return self._frame, self._seq, self._at

    def wait_for(self, since_seq: int, timeout: float = FRAME_WAIT):
        """Next frame after `since_seq`, or None if none arrives in `timeout`.

        The loop is not decoration. A single `if seq == since: wait()` -- the
        obvious shape, and the one the drone version uses -- is wrong twice
        here. Condition.wait() may return spuriously, and more importantly
        drop() advances the sequence while leaving the frame None, so a viewer
        connecting to a sleeping camera sees "sequence already moved" and gives
        up INSTANTLY instead of waiting for the sensor to wake. Measured: the
        stream closed after 0 frames every time until this became a while.

        Re-deriving the remaining time each pass keeps the total bounded at
        `timeout` no matter how many times it goes round.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._frame is None or self._seq == since_seq:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            return self._frame, self._seq, self._at

    # -- viewer bookkeeping ---------------------------------------------
    # Every acquire is paired with a release in a finally: block. A viewer
    # count that only goes up would pin the sensor open forever, which is the
    # privacy failure this whole service is arranged to avoid.
    def acquire_viewer(self) -> bool:
        with self._cond:
            if self._viewers >= MAX_VIEWERS:
                return False
            self._viewers += 1
            self._last_viewer_at = time.time()
            return True

    def release_viewer(self) -> None:
        with self._cond:
            self._viewers = max(0, self._viewers - 1)
            self._last_viewer_at = time.time()

    @property
    def viewers(self) -> int:
        with self._cond:
            return self._viewers

    @property
    def last_viewer_at(self) -> float:
        with self._cond:
            return self._last_viewer_at


def token_path() -> Path:
    """Beside the kill switch, under the OWNER's config -- not ~/.hermes/.

    Same reasoning as protocol.persistent_disable_path(): anything Hermes
    manages, Hermes can rewrite.
    """
    return Path.home() / ".config" / "hermes-pi" / "camera-stream.token"


def load_or_create_token() -> str:
    """Stable across restarts, so the URL can be bookmarked."""
    p = token_path()
    try:
        tok = p.read_text().strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(16)
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    p.write_text(tok + "\n")
    p.chmod(0o600)
    return tok


def local_ip() -> str:
    """Best-effort LAN address, for printing a URL you can actually click.

    From auto-drone's get_local_ip(). Connecting a UDP socket sends no packet;
    it just asks the kernel which interface would be used, so it works with no
    internet access as long as there is a LAN route.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HERMES CAM</title>
<style>
 :root{--c:#00e6ff;--dim:#0a3a44;--bg:#04080a}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--c);
      font:13px/1.5 ui-monospace,"DejaVu Sans Mono",monospace;
      display:flex;flex-direction:column;align-items:center;gap:10px;padding:14px}
 h1{font-size:13px;letter-spacing:.32em;font-weight:400;margin:0;opacity:.85}
 .wrap{position:relative;border:1px solid var(--dim);padding:6px;
       background:#000;max-width:100%}
 img{display:block;max-width:100%;height:auto;image-rendering:auto}
 .bar{display:flex;gap:14px;flex-wrap:wrap;justify-content:center;
      border:1px solid var(--dim);padding:7px 12px;opacity:.9}
 .bar b{font-weight:400;opacity:.5;letter-spacing:.1em}
 .off{position:absolute;inset:6px;display:none;place-items:center;
      background:#000c;letter-spacing:.3em;color:#ff5252}
 .stale .off{display:grid}
 .stale img{opacity:.25;filter:grayscale(1)}
</style>
<h1>HERMES &middot; CAMERA</h1>
<div class="wrap" id="wrap">
  <img id="v" alt="camera">
  <div class="off">NO SIGNAL</div>
</div>
<div class="bar">
  <span><b>STATE</b> <span id="state">-</span></span>
  <span><b>SENSOR</b> <span id="pw">-</span></span>
  <span><b>MOTION</b> <span id="mo">-</span></span>
  <span><b>VIEWERS</b> <span id="vw">-</span></span>
  <span><b>FPS</b> <span id="fps">-</span></span>
</div>
<div class="bar" id="hb"><span><b>HANDS</b> <span id="hs">-</span></span></div>
<script>
 // Cache-bust once at load; the MJPEG connection itself stays open after that.
 document.getElementById('v').src = 'stream.mjpg' + location.search
                                  + (location.search ? '&' : '?') + 't=' + Date.now();
 async function poll(){
   try{
     const r = await fetch('status.json' + location.search, {cache:'no-store'});
     const s = await r.json();
     state.textContent = s.state;
     pw.textContent = s.sensor_powered === null ? 'unknown'
                    : (s.sensor_powered ? 'POWERED' : 'suspended');
     mo.textContent = (s.motion ?? 0).toFixed(2);
     vw.textContent = s.viewers;
     fps.textContent = (s.stream_fps ?? 0).toFixed(1);
     // Trust the server's own staleness verdict rather than guessing from
     // whether the <img> looks like it updated.
     wrap.classList.toggle('stale', !s.live);
     if (s.hands_error) { hs.textContent = s.hands_error; }
     else if (!s.hands_tracking) { hs.textContent = 'not tracking'; }
   }catch(e){ wrap.classList.add('stale'); }
 }
 async function pollHands(){
   try{
     const r = await fetch('hands.json' + location.search, {cache:'no-store'});
     if(!r.ok){ return; }
     const h = await r.json();
     // Say "stale" rather than showing an old gesture as if it were current.
     hs.textContent = h.stale ? 'stale'
       : (h.hands.length === 0 ? 'none in view'
          : h.hands.map(x => `${x.handedness} ${x.gesture} (${x.fingers_up})`)
                   .join('   ') + `   ${h.detect_ms}ms`);
   }catch(e){}
 }
 poll(); setInterval(poll, 1000);
 pollHands(); setInterval(pollHands, 300);
</script>
"""


def _make_handler(buf: StreamBuffer, token: str, status_fn):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "hermes-cam"
        sys_version = ""

        # Default logging writes a line per request to stderr, which at 1 Hz
        # status polling would bury the capture audit lines in the journal.
        # Connections to the STREAM are logged explicitly instead -- that is
        # the event worth keeping, and journald here is persistent.
        def log_message(self, *a):
            pass

        def _authorised(self, query) -> bool:
            if not token:
                return True
            got = (query.get("k") or [""])[0]
            return hmac.compare_digest(got, token)

        def _send(self, code, ctype, body: bytes, extra=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            # A view of a room has no business in a shared cache, and the
            # status doc must never be served stale.
            self.send_header("Cache-Control", "no-store, private")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def do_GET(self):                                    # noqa: N802
            u = urlparse(self.path)
            q = parse_qs(u.query)
            path = u.path.rstrip("/") or "/"

            if not self._authorised(q):
                # No hint about what is here or what the token looks like.
                self._send(HTTPStatus.FORBIDDEN, "text/plain; charset=utf-8",
                           b"forbidden\n")
                return

            if path == "/":
                self._send(HTTPStatus.OK, "text/html; charset=utf-8",
                           _PAGE.encode())
            elif path == "/status.json":
                self._send(HTTPStatus.OK, "application/json",
                           json.dumps(status_fn()).encode())
            elif path == "/hands.json":
                self._hands()
            elif path == "/snapshot.jpg":
                self._snapshot()
            elif path == "/stream.mjpg":
                self._stream()
            else:
                self._send(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8",
                           b"not found\n")

        # -- endpoints --------------------------------------------------
        def _hands(self):
            """Latest hand observation, for a client that wants to act on it.

            Served from the file rather than from memory so there is exactly
            one representation of this data and no chance of the HTTP view and
            the on-disk view disagreeing. 404 when there is nothing -- an empty
            hands list would say "no hands in view", which is a different claim
            from "not tracking".
            """
            try:
                body = protocol.hands_path().read_bytes()
            except OSError:
                self._send(HTTPStatus.NOT_FOUND, "application/json",
                           b'{"error":"not tracking"}\n')
                return
            self._send(HTTPStatus.OK, "application/json", body)

        def _snapshot(self):
            """One frame. For curl, for a CV client that wants to poll.

            Registers as a viewer for the duration, so hitting this on a
            sleeping camera wakes it rather than returning 503 -- otherwise a
            polling script would only ever get an answer if something else
            happened to be watching at the same time.
            """
            if not buf.acquire_viewer():
                self._send(HTTPStatus.SERVICE_UNAVAILABLE,
                           "text/plain; charset=utf-8", b"too many viewers\n")
                return
            try:
                frame, seq, at = buf.latest()
                if frame is None or time.time() - at > STALE_AFTER:
                    got = buf.wait_for(seq, FRAME_WAIT)
                    if got is None:
                        self._send(HTTPStatus.SERVICE_UNAVAILABLE,
                                   "text/plain; charset=utf-8",
                                   b"no live frame\n")
                        return
                    frame, seq, at = got
                self._send(HTTPStatus.OK, "image/jpeg", frame,
                           extra=(("X-Captured-At", f"{at:.3f}"),))
            finally:
                buf.release_viewer()

        def _stream(self):
            peer = self.client_address[0]
            if not buf.acquire_viewer():
                self._send(HTTPStatus.SERVICE_UNAVAILABLE,
                           "text/plain; charset=utf-8", b"too many viewers\n")
                return
            print(f"[camera] stream opened by {peer} "
                  f"(viewers={buf.viewers})", flush=True)
            frames = 0
            t0 = time.time()
            try:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type",
                                 "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store, private")
                self.send_header("X-Content-Type-Options", "nosniff")
                # No Content-Length is possible on a response that never ends,
                # so HTTP/1.1 requires the connection be marked for close.
                self.send_header("Connection", "close")
                self.end_headers()

                seq = 0
                while True:
                    got = buf.wait_for(seq, FRAME_WAIT)
                    if got is None:
                        # Producer stopped. Ending the response is honest;
                        # holding the socket open would leave the browser
                        # showing the last frame indefinitely.
                        break
                    frame, seq, _at = got
                    self.wfile.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(frame)).encode()
                        + b"\r\n\r\n" + frame + b"\r\n")
                    frames += 1
            except (OSError, ValueError):
                pass            # viewer closed the tab; entirely normal
            finally:
                buf.release_viewer()
                dt = max(1e-6, time.time() - t0)
                print(f"[camera] stream closed for {peer} "
                      f"({frames} frames, {dt:.0f}s, {frames/dt:.1f} fps, "
                      f"viewers={buf.viewers})", flush=True)

    return Handler


class StreamServer:
    def __init__(self, buf: StreamBuffer, status_fn, bind: str, port: int,
                 token: str):
        self.buf = buf
        self.token = token
        self.bind = bind
        self.port = port
        self._httpd = ThreadingHTTPServer(
            (bind, port), _make_handler(buf, token, status_fn))
        self._httpd.daemon_threads = True
        self._httpd.allow_reuse_address = True
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host = local_ip() if self.bind in ("", "0.0.0.0") else self.bind
        q = f"?k={self.token}" if self.token else ""
        return f"http://{host}:{self.port}/{q}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="mjpeg", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass


def start(buf: StreamBuffer, status_fn) -> StreamServer | None:
    """Start the stream server, or return None if it is switched off.

    Binds to the LAN by default because the whole point is a link that opens
    on the laptop. HERMES_CAMERA_STREAM_BIND=127.0.0.1 restricts it to this
    host (reach it over `ssh -L`), and HERMES_CAMERA_STREAM=off disables it.
    """
    if os.environ.get("HERMES_CAMERA_STREAM", "on").lower() in ("off", "0", "no"):
        print("[camera] stream disabled (HERMES_CAMERA_STREAM=off)", flush=True)
        return None
    bind = os.environ.get("HERMES_CAMERA_STREAM_BIND", "0.0.0.0")
    port = int(os.environ.get("HERMES_CAMERA_STREAM_PORT",
                              str(protocol.STREAM_PORT)))
    token = "" if os.environ.get("HERMES_CAMERA_STREAM_TOKEN") == "off" \
        else load_or_create_token()
    try:
        srv = StreamServer(buf, status_fn, bind, port, token)
    except OSError as e:
        # Never fatal: Hermes' ability to see must not depend on a free port.
        print(f"[camera] stream unavailable on {bind}:{port}: {e}", flush=True)
        return None
    srv.start()
    if not token:
        print("[camera] WARNING: stream token disabled -- anyone on the LAN "
              "can watch this room", flush=True)
    print(f"[camera] stream: {srv.url}", flush=True)
    return srv
