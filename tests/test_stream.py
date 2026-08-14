"""The live stream must not watch the room for free, or forever.

Two failure modes are pinned here because both have already happened in this
project in a different guise:

  * A COUNTER THAT ONLY GOES UP. hermes_camera's per-turn capture limit keyed
    on task_id never reset, and the camera refused permanently (trap 19). The
    viewer count is the same shape of thing, with a worse consequence: it is
    what holds the sensor open, so a leaked increment means a camera that
    watches the room until the service is restarted.

  * A WAIT THAT DOES NOT WAIT. The first version of StreamBuffer.wait_for()
    gave up instantly whenever the sequence had already moved on with no frame
    present -- which is exactly the state a sleeping camera is in -- so every
    connection closed after zero frames. It looked like a network problem.

Plus the token gate, which is the only thing between anyone on the LAN and a
view of this room.

No camera required: the buffer is fed synthetic JPEGs.

Run:  python3 tests/test_stream.py
"""

import io
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                          # noqa: E402
from PIL import Image                                       # noqa: E402

from camera import encode, stream                           # noqa: E402

JPEG = b"\xff\xd8" + b"\x00" * 64 + b"\xff\xd9"


# -- StreamBuffer ---------------------------------------------------------
def test_wait_returns_an_already_present_frame_immediately():
    b = stream.StreamBuffer()
    b.push(JPEG, time.time())
    t0 = time.monotonic()
    got = b.wait_for(0, timeout=2.0)
    assert got is not None and got[0] == JPEG
    assert time.monotonic() - t0 < 0.05, "waited for a frame it already had"


def test_wait_blocks_until_a_frame_arrives():
    b = stream.StreamBuffer()
    threading.Timer(0.2, lambda: b.push(JPEG, time.time())).start()
    t0 = time.monotonic()
    got = b.wait_for(0, timeout=2.0)
    assert got is not None, "gave up before the frame arrived"
    assert 0.15 < time.monotonic() - t0 < 1.0


def test_wait_survives_a_dropped_frame():
    """THE REGRESSION.

    drop() advances the sequence while leaving the frame None -- the state of a
    camera that just went to sleep. A viewer connecting then must WAIT for the
    sensor to wake, not conclude from the moved sequence that it has missed
    something and hang up. This closed every stream after 0 frames.
    """
    b = stream.StreamBuffer()
    b.push(JPEG, time.time())
    b.drop()
    threading.Timer(0.2, lambda: b.push(JPEG, time.time())).start()
    t0 = time.monotonic()
    got = b.wait_for(0, timeout=2.0)
    assert got is not None, "a sleeping camera made the viewer give up instantly"
    assert time.monotonic() - t0 > 0.15


def test_wait_times_out_rather_than_hanging():
    b = stream.StreamBuffer()
    t0 = time.monotonic()
    assert b.wait_for(0, timeout=0.3) is None
    assert time.monotonic() - t0 < 1.0, "timeout was not honoured"


def test_viewer_count_releases():
    """A count that only goes up would pin the sensor open forever."""
    b = stream.StreamBuffer()
    assert b.viewers == 0
    assert b.acquire_viewer()
    assert b.viewers == 1
    b.release_viewer()
    assert b.viewers == 0, "viewer count leaked -- the camera would never sleep"


def test_viewer_count_is_capped():
    b = stream.StreamBuffer()
    got = [b.acquire_viewer() for _ in range(stream.MAX_VIEWERS + 3)]
    assert got[:stream.MAX_VIEWERS] == [True] * stream.MAX_VIEWERS
    assert not any(got[stream.MAX_VIEWERS:]), "no bound on concurrent viewers"
    for _ in range(stream.MAX_VIEWERS):
        b.release_viewer()
    assert b.viewers == 0


def test_release_cannot_go_negative():
    """An unbalanced release must not make the count wrap below zero, which
    would let the cap be bypassed."""
    b = stream.StreamBuffer()
    b.release_viewer()
    b.release_viewer()
    assert b.viewers == 0


# -- encoding -------------------------------------------------------------
def test_stream_jpeg_keeps_portrait_aspect():
    """Trap 17, one layer further out. The sensor is mounted rotated, so frames
    are PORTRAIT; resizing to a landscape target squashes the room flat."""
    frame = np.zeros((1024, 576, 3), dtype=np.uint8)     # h > w, as delivered
    img = Image.open(io.BytesIO(encode.to_stream_jpeg(frame, long_edge=640)))
    assert img.size == (360, 640), f"aspect not preserved: {img.size}"
    assert img.height > img.width, "a portrait frame came back landscape"


def test_stream_jpeg_is_a_real_jpeg():
    frame = (np.random.rand(480, 270, 3) * 255).astype(np.uint8)
    data = encode.to_stream_jpeg(frame)
    assert data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9"


# -- HTTP -----------------------------------------------------------------
class _Server:
    """A real server on a real port, bound to loopback only."""

    def __enter__(self):
        self.buf = stream.StreamBuffer()
        self.srv = stream.StreamServer(
            self.buf, lambda: {"state": "test", "viewers": self.buf.viewers},
            "127.0.0.1", 0, "s3cret")
        self.port = self.srv._httpd.server_address[1]
        self.srv.start()
        self.base = f"http://127.0.0.1:{self.port}"
        return self

    def __exit__(self, *a):
        self.srv.stop()


def _code(url, timeout=None):
    """HTTP status, with a client timeout that CANNOT race the server.

    The snapshot endpoint deliberately waits FRAME_WAIT for a fresh frame
    before refusing. A client timeout equal to that is a coin toss decided by
    scheduling: on an idle Pi the server answered first and the test passed,
    on a loaded CI runner the client gave up first and it failed with
    TimeoutError. It looked like flakiness and was a fixed race.

    Derived from FRAME_WAIT rather than hardcoded, so changing the server's
    wait cannot silently reintroduce it.
    """
    if timeout is None:
        timeout = stream.FRAME_WAIT * 2 + 5
    try:
        return urllib.request.urlopen(url, timeout=timeout).status
    except urllib.error.HTTPError as e:
        return e.code


def test_token_is_required():
    """The only thing between anyone on the LAN and a view of this room."""
    with _Server() as s:
        assert _code(f"{s.base}/status.json") == 403, "served with NO token"
        assert _code(f"{s.base}/status.json?k=wrong") == 403, "wrong token served"
        assert _code(f"{s.base}/status.json?k=s3cret") == 200
        assert _code(f"{s.base}/?k=s3cret") == 200


def test_token_gate_covers_every_endpoint():
    """A gate on the page but not on the pixels would be no gate at all."""
    with _Server() as s:
        for path in ("/", "/status.json", "/snapshot.jpg", "/stream.mjpg",
                     "/nope"):
            assert _code(f"{s.base}{path}") == 403, f"{path} bypassed the token"


def test_unknown_path_is_a_404_not_a_file():
    with _Server() as s:
        assert _code(f"{s.base}/../camera/stream.py?k=s3cret") in (400, 403, 404)


def test_stream_delivers_framed_jpegs():
    with _Server() as s:
        stop = threading.Event()

        def produce():
            while not stop.is_set():
                s.buf.push(JPEG, time.time())
                time.sleep(0.02)
        t = threading.Thread(target=produce, daemon=True)
        t.start()
        try:
            r = urllib.request.urlopen(f"{s.base}/stream.mjpg?k=s3cret",
                                       timeout=5)
            assert "multipart/x-mixed-replace" in r.headers["Content-Type"]
            data = r.read(len(JPEG) * 3 + 400)
            assert data.count(b"--frame") >= 2, "no multipart boundaries"
            assert b"Content-Length: " + str(len(JPEG)).encode() in data
            assert JPEG in data
            r.close()
        finally:
            stop.set()


def test_a_disconnected_viewer_is_released():
    """The whole privacy story: close the tab, the camera goes back to sleep."""
    with _Server() as s:
        stop = threading.Event()
        threading.Thread(
            target=lambda: [s.buf.push(JPEG, time.time()) or time.sleep(0.02)
                            for _ in iter(lambda: not stop.is_set(), False)],
            daemon=True).start()
        try:
            r = urllib.request.urlopen(f"{s.base}/stream.mjpg?k=s3cret",
                                       timeout=5)
            r.read(64)
            assert s.buf.viewers == 1
            r.close()
            deadline = time.time() + 5
            while s.buf.viewers and time.time() < deadline:
                s.buf.push(JPEG, time.time())     # writes fail -> handler exits
                time.sleep(0.05)
            assert s.buf.viewers == 0, \
                "viewer never released -- the sensor would stay open forever"
        finally:
            stop.set()


def test_snapshot_refuses_rather_than_serving_a_stale_frame():
    """Same rule as camera_look: better no picture than an old one presented
    as current."""
    with _Server() as s:
        s.buf.push(JPEG, time.time() - 3600)          # an hour old
        t0 = time.time()
        assert _code(f"{s.base}/snapshot.jpg?k=s3cret") == 503
        assert time.time() - t0 >= stream.FRAME_WAIT - 0.5, \
            "did not actually wait for a fresh frame"


# -- watchdog -------------------------------------------------------------
# A wedged capture loop was OBSERVED once: sensor open and powered, zero frames
# produced, every browser connection closing after 5 s with nothing. It looked
# exactly like a network fault and survived four minutes until a hand restart,
# and has not reproduced since, including under deliberate browser-like load.
# The expensive part of that incident was not the downtime -- it was that
# nothing said anything was wrong, and that status.json's `updated_at` stayed
# current the whole time because the HTTP thread writes it.
class _FakeSensor:
    def __init__(self, is_open=True):
        self.is_open = is_open
        self.last_error = None


def _svc(is_open=True, last_frame_ago=0.0, loop_idle=0.0):
    from camera.__main__ import Service
    s = Service.__new__(Service)             # no camera, no threads, no venv
    s.sensor = _FakeSensor(is_open)
    s.last_frame_at = time.time() - last_frame_ago
    s.loop_tick = time.monotonic() - loop_idle
    return s


def test_watchdog_is_quiet_when_frames_are_flowing():
    assert _svc(is_open=True, last_frame_ago=0.2).stall_reason() is None


def test_watchdog_fires_on_a_frame_drought():
    """THE OBSERVED FAILURE: sensor open, no frames, indefinitely."""
    from camera.__main__ import STALL_TIMEOUT
    why = _svc(is_open=True, last_frame_ago=STALL_TIMEOUT + 5).stall_reason()
    assert why and "no frame" in why, f"drought not detected: {why!r}"


def test_watchdog_fires_when_the_loop_stops_ticking():
    from camera.__main__ import STALL_TIMEOUT
    why = _svc(is_open=True, last_frame_ago=0.1,
               loop_idle=STALL_TIMEOUT + 5).stall_reason()
    assert why and "not ticked" in why, f"dead loop not detected: {why!r}"


def test_watchdog_never_fires_on_a_sleeping_camera():
    """The camera is closed almost all the time by design. Restarting the
    service every 15 s of idle would be far worse than the bug it guards."""
    from camera.__main__ import STALL_TIMEOUT
    s = _svc(is_open=False, last_frame_ago=STALL_TIMEOUT + 600,
             loop_idle=STALL_TIMEOUT + 600)
    assert s.stall_reason() is None, "would restart a healthy idle service"


def test_watchdog_tolerates_a_freshly_opened_camera():
    """A just-opened sensor has produced no frames yet. ensure_awake() stamps
    last_frame_at at the open precisely so a reopen cannot inherit the stale
    timestamp from the previous session and trip this immediately."""
    assert _svc(is_open=True, last_frame_ago=0.0).stall_reason() is None


def _run() -> int:
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as e:
            fails += 1
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{'ALL PASS' if not fails else f'{fails} FAILURES'}")
    return fails


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
