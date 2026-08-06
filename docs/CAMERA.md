# Camera — Raspberry Pi Camera Module 3

Everything here was verified on this Pi on **2026-08-05**. Nothing is assumed
from a datasheet. Re-verify any time with `python3 tools/camera_probe.py`.

Status: **working and integrated.** Hermes can look through this camera and
answer from the actual pixels. See "Integration" below.

---

## What is actually there

```
sensor    imx708 (Camera Module 3)
path      /base/axi/pcie@1000120000/rp1/i2c@88000/imx708@1a
native    4608x2592, 10-bit RGGB
tooling   rpicam-apps v1.12.0  (rpicam-hello / -still / -vid)
python    python3-picamera2 0.3.36-1  (apt, already installed)
config    camera_auto_detect=1 in /boot/firmware/config.txt
```

Note the binaries are `rpicam-*`, **not** `libcamera-*`. The `libcamera-*`
names do not exist on this image; anything that shells out to them will fail.

Sensor modes reported by `rpicam-hello --list-cameras`:

| mode | max fps | crop |
|---|---|---|
| 1536x864 | 120.13 | (768,432)/3072x1728 |
| 2304x1296 | 56.03 | full |
| 4608x2592 | 14.35 | full |

---

## Measured throughput

`picamera2`, `RGB888`, video configuration, 30 frames each, CPU measured from
`/proc/self/stat`:

| capture size | fps | ms/frame | CPU |
|---|---|---|---|
| 480x272 | 30.4 | 32.9 | ~5% |
| 640x360 | 30.4 | 32.9 | ~6% |
| 1536x864 | 30.4 | 32.9 | ~9% |

**30 fps is the pipeline's delivered rate at every size** — resolution changes
the CPU cost, not the frame rate. The 120 fps in the mode table is a sensor
capability, not what arrives in a numpy array.

### The number that governs any panel preview

The camera is **not** the bottleneck for anything displayed:

```
SPI bus            2,562,838 B/s   (measured)
one 480x232 frame    246,499 B     (measured, transmitted — not 222,720)
                   ------------
panel ceiling          10.37 fps
```

The display currently runs at 9 fps and uses 86.7% of the bus. A camera
preview would be **bus-limited, not camera-limited**, and it cannot run
alongside the animation — it would replace it, not add to it. Budget for that
before designing anything.

---

## Proving the sensor is live

A dark room and a disconnected ribbon both produce a black frame, so "I got an
image back" proves nothing. `tools/camera_probe.py` requires two things only
live silicon produces:

- **spatial noise** — read noise means a frame is never perfectly uniform
- **temporal noise** — two successive frames are never bit-identical

Measured in a dark room at 04:00: spatial `1.865`, temporal `1.476`, against
thresholds of `0.05` and `0.01`. Margins of ~37x and ~147x, so the test is
trustworthy with the lights off.

A gain-response check (raise gain, expect more noise) is reported but is **not**
the pass criterion: in darkness the 1x→16x std ratio was only 1.17x, which is
far too thin to gate on. That was the first version of the test and it would
have produced false failures.

---

## Gotchas found while verifying

**1. `picamera2` has no `__version__`.** `import picamera2; picamera2.__version__`
raises `AttributeError`, which looks exactly like a failed import if the
traceback is truncated. Use `dpkg -l python3-picamera2` for the version.

**2. libcamera logs INFO to stderr on every `configure()`.** Several lines per
call, which buries real output. Set `LIBCAMERA_LOG_LEVELS=*:ERROR`.

**3. Always `close()` a `Picamera2` instance.** Leaving one open holds the
sensor and the next `Picamera2()` fails. The probe closes after every
measurement for this reason.

**4. `"RGB888"` from picamera2 is BGR in the array**, and the sensor here is
mounted 90 degrees off, so corrected frames are PORTRAIT (576x1024). Both are
handled in `camera/sensor.py`. Consequences worth knowing: never resize a frame
to fixed landscape dimensions (it squashes), and the panel preview is a
letterboxed vertical strip because the camera's natural view genuinely is
portrait. `HERMES_CAMERA_ROTATE` changes it if the module is remounted.

**4b. The BGR swap is REAL, and here is the proof, because it will be doubted.**
Same scene, three ways, mean channel values:

```
picamera2 capture_image() (its own PIL path, true RGB)   R 113.5  G 108.1  B 105.4
ours, after frame[..., ::-1]                             R 113.5  G 108.2  B 105.4
raw capture_array(), no swap                             R 105.4  G 108.2  B 113.5
```

Mean absolute difference against truth: **0.512 ours, 10.279 unswapped**.

A project that feeds the array to **cv2** needs NO swap, because cv2.imencode
expects BGR -- that is not a contradiction, it is the same fact seen from the
other side. We use PIL, which expects RGB, so we must swap. Check which library
consumes the array before concluding anything.

Verify colour on a COLOURED scene. A grey desk is invariant under a red/blue
swap, so a neutral test proves nothing -- an early check here "passed" on a
grey scene while the bug was still present.

**5. Wait on `AfState`, do not sleep a guess.** An earlier version slept a flat
1.5 s after `start()` and that number then got quoted as if it were measured.
Polling `capture_metadata()["AfState"]` until `Focused` takes **406 ms**, and
it is correct rather than merely long enough.

---

## Integration — BUILT (2026-08-05)

Hermes can see. Ask it over Discord and it looks through this camera and answers
from the actual pixels. No second model, no local captioning, no describing the
room in words first.

### Why it works on a ChatGPT Plus subscription

Verified against the installed Hermes source, not assumed:

- `openai-codex` reaches `https://chatgpt.com/backend-api/codex` over OAuth.
- `gpt-5.6-terra` reports `modalities.input = ['text','image','pdf']`, so
  `decide_image_input_mode()` resolves to `native` and real pixels are sent.
- `tools/registry.py:_normalize_handler_result` accepts exactly two return
  shapes: a `str`, or `{"_multimodal": True, "content": [...], "text_summary":}`.
  `tools/vision_tools.py:_supports_media_in_tool_results` names `openai-codex`.
- Discord photo attachments already flowed this way, which proved the path.

### Shape

Three processes, none of which may block another:

```
hermes-camera (new)   owns the sensor exclusively; never touches /dev/fb0
  camera/sensor.py      the ONLY picamera2 file -- swap to change cameras
  camera/encode.py      JPEG under a hard ceiling + RGB565 panel preview
  camera/protocol.py    the tmpfs contract (docs/CAMERA-CONTRACT.md)
hermes_ext/plugins/hermes_camera/   camera_look, camera_watch
```

The sensor is **lazy**: closed at rest, opened on demand, closed again after
20 s idle. It is not held open to save latency, because there is barely any
latency to save.

### Measured, on this hardware

| | |
|---|---|
| cold wake (open → focused → first frame) | **434 ms** (AF→Focused 406 ms) |
| cold capture, end to end through the plugin | **673 ms** |
| warm capture | **44 ms** |
| `normal` 768x432 q72 | ~11–13 KB → **~17 KB base64** |
| `fine` 1024x576 q78 | ~20–23 KB → **~30 KB base64** |
| `camera_watch` 2x2 contact sheet | **~29 KB base64** — same order as ONE frame |
| camera service, sensor asleep | **0.25% CPU, 29 MB RSS** |
| display service, unchanged | 0.75% CPU |

The earlier "~1.5 s AE settle" was a fixed `time.sleep()` in the probe, not a
measured requirement. Waiting on `AfState` instead costs 406 ms.

### Field of view — force the sensor mode

A `1024x576` request auto-selects the `1536x864` sensor mode, which is a
`(768,432)/3072x1728` crop — **a 0.67x narrower field of view**. Forcing
`output_size=(2304,1296)` gives `ScalerCrop=(0,0,4608,2592)`, the full frame.
`camera/protocol.py:SENSOR_OUTPUT_SIZE` does this. Without it, "what am I
doing" loses the sides of the room.

### Context cost — the real constraint

An image embeds in **immutable** history and is **re-sent on every later turn**
of that session. It cannot be shrunk or evicted afterwards.

Guards: `normal` by default (~17 KB base64); a hard byte ceiling per profile
enforced by a quality-then-scale loop in `camera/encode.py`; **max 3 image
returns per turn** keyed on `task_id`, because the expensive failure is a model
that keeps looking rather than one large picture; and `camera_watch` packing
four moments into ONE image so motion costs the same as a single frame.

Three `normal` frames ≈ 51 KB of base64 re-uploaded every subsequent turn. The
only reset is a new session.

### Honesty rules, enforced by tests

`tests/test_camera_tools.py` pins these; they are the camera's version of "the
panel never invents state":

- a frame older than `MAX_FRAME_AGE` (2 s) is **refused, never shown**
- an age that cannot be trusted is refused rather than printed — the Pi has no
  RTC and the clock is wrong for ~34 s after boot (trap 6)
- every error says *"You have NOT seen anything. Do not describe the room."*
  A bare error invites answering from priors
- the frame's age and capture time are always in the text beside the image
- `camera_watch` states whether it is showing the seconds **before** the
  question (ring already warm) or **after** (camera was asleep)

### Privacy

**The panel is the tally light, and it is driven by the kernel.**
`display/health.py` reads the sensor's runtime-PM state, so a camera service
that crashes with the sensor open, lies, or is replaced cannot switch the light
off. **The fail direction is inverted** from everything else in this project:
unknown means *assume ON*. `tests/test_camera_indicator.py` pins that.

**The panel also shows the frame that was sent**, for ~8 s. Not "a light is on"
but "here is exactly what left the room".

Kill switches, ascending: `~/.config/hermes-pi/camera.disabled` (outside
`~/.hermes/`, so a config rewrite cannot clobber it) → `systemctl --user stop
hermes-camera` → `mask` → lens cover / unplug the ribbon. **None of the
software ones are enforceable against the agent** — see `docs/SECURITY.md`.

---

## Live stream — BUILT (2026-08-05)

A continuous MJPEG view in a browser, so the camera can be watched and worked
with directly rather than only through Hermes. Structure is taken from the
owner's auto-drone project (`streaming/mjpeg_server.py`): a frame buffer
guarded by a `Condition`, a `ThreadingHTTPServer`, and a
`multipart/x-mixed-replace` response that never ends.

```
python3 -m camera --stream-url        # prints the link, token included
```

| endpoint | for |
|---|---|
| `/` | the page: live view, state, sensor power, motion, viewers, fps |
| `/stream.mjpg` | the MJPEG stream itself |
| `/snapshot.jpg` | one current frame — for `curl`, or a CV client that polls |
| `/status.json` | the service's own status doc, plus `viewers` and `live` |

### It lives INSIDE the camera service, and had to

The sensor is an exclusive kernel resource: one process may hold it. A
standalone stream script would either take the camera away from Hermes or fail
to start, decided by nothing better than which one ran first. So the stream is
a third **consumer** of frames the service already grabs, not a second owner.
`pump_frames()` takes one frame per tick and gives it to whichever of the
stream and the ring is due.

### Viewers are the on switch

The service stays lazy. Opening the page wakes the sensor; closing the last tab
lets it sleep again after `STREAM_LINGER` (8 s, which covers a page reload —
without it every refresh would pay a 434 ms cold wake). There is deliberately
no separate toggle to leave switched on by accident.

Measured, loopback, one viewer:

```
first frame from a sleeping camera   1.55 s   (cold wake included)
delivered rate                      14.1 fps  (STREAM_FPS 15)
frame size                           8.3 KB   at 640 long edge, q70
wire                                 117 KB/s
camera service CPU                   32.5%    of ONE core, only while watched
display service CPU, same window      1.05%   (unaffected)
snapshot, camera already awake        0.7 ms  (served from the buffer)
```

CPU per frame, measured stage by stage, because the first estimate of this was
wrong by more than the thing it was estimating:

```
capture_array (blocks 33 ms wall)     2.0 ms cpu
BGR swap + rot90 + contiguous         7.5 ms
resize to 640 long edge               7.0 ms   <- larger than the JPEG
JPEG q70                              1.8 ms
```

`Sensor.grab(long_edge=...)` resizes **before** correcting colour and rotation,
which trims 18.3 → 16.3 ms. That is an 11% trim, not a fix: the resize costs
the same either way because its *input* is full-res regardless; only the
correction shrinks. Recorded because the obvious reading of these numbers
(“resize first and save 5 ms”) is wrong.

### Exposure: the live view and a still want different things

`FRAME_DURATION_LIMITS` allows up to 100 ms of exposure, which is right for a
still — it halves the grain when a model has to read what is in the frame. It
is wrong for a live view: at 100 ms a dim room runs the stream at 10 fps and
smears every hand movement, which is exactly what gesture work needs to see.
So 30 fps is **pinned while a viewer is attached** and released afterwards, as
a control rather than a reconfiguration (no ~300 ms re-settle).

The cost, measured in a genuinely dark room (lux 5, camera facing an unlit
ceiling at 22:30):

```
stills    cap 100 ms  -> exposure 66.6 ms  gain 16.0 (max)  mean level 94.0
live view pin  33 ms  -> exposure 32.7 ms  gain 16.0 (max)  mean level 65.3
```

About a stop darker, and **gain is already at maximum so it cannot be bought
back**. In normal room light neither cap binds and the two are identical. This
is a deliberate trade of brightness for motion clarity, and it only applies
while someone is watching.

### Colour on the stream path

Same correction as everything else — the stream calls the same
`Sensor.grab()`, so `"RGB888"`-is-BGR and the 90° mount are handled once.
Checked against `capture_image()` (picamera2's own PIL path): mean abs diff
**3.360 as sent vs 3.638 channel-swapped**.

**That margin is thin, and it is thin for a known reason**: the test scene was
a near-neutral dark ceiling (channel means 65/68/65), and a grey scene is
almost invariant under a red/blue swap. It agrees with the strong result
measured earlier on a lit scene (0.512 vs 10.279) and is not independent
evidence. **Colour cannot be properly judged until the camera is aimed at
something with colour in it and focused.**

### Security — this is the second network-facing socket on the box

`docs/SECURITY.md` previously said ssh was the only one. It no longer is, and
that is a real change to the threat model rather than a detail:

- **A token is required by default**, checked with `hmac.compare_digest` on
  every endpoint including `/snapshot.jpg`. It lives in
  `~/.config/hermes-pi/camera-stream.token` (0600) — beside the kill switch,
  under the owner's config and **not** `~/.hermes/`, because anything Hermes
  manages, Hermes can rewrite. Stable across restarts so the URL stays
  bookmarkable.
- `HERMES_CAMERA_STREAM_BIND=127.0.0.1` restricts it to this host; reach it
  with `ssh -L 8081:127.0.0.1:8081`. `HERMES_CAMERA_STREAM=off` disables it.
- `HERMES_CAMERA_STREAM_TOKEN=off` serves the room to anyone on the LAN. It
  logs a warning and takes an explicit setting; it is not the default and not
  the easy path.
- The stream is **not** exempt from the kill switches. It goes through
  `ensure_awake()`, so `~/.config/hermes-pi/camera.disabled` and
  `systemctl --user stop hermes-camera` both stop it.
- The panel's CAM light needs no change: it reads the kernel's runtime-PM
  state, so streaming lights it automatically. Verified — `suspended` →
  `active` while a viewer is attached → `suspended` after.

### Deliberately NOT copied from the drone

Its camera settings are the opposite of what is wanted here, and correctly so
for it: 2 ms exposure, gain 8, autofocus off at a fixed 1 m, noise reduction
off, sharpness 2.0. That freezes airframe vibration for AprilTag detection and
produces a grainy, dark, over-sharpened frame — right for a detector, wrong for
a room a person or a model is looking at. The neutral controls in
`camera/sensor.py` stand.

Its `wait_for` shape was not copied either. Waiting on the Condition and then
reading `self.frame` gives up instantly whenever the sequence has moved on with
no frame present — precisely the state of a sleeping camera — so every
connection here closed after **zero frames** until it became a `while` loop
keyed on the sequence number. `tests/test_stream.py` pins it, and that test was
confirmed to fail against the original.

## Still open

- **Gestures are not built.** A gesture trigger is a path from "someone waves
  in the room" to "the agent runs a tool", and the Discord allowlist does not
  cover it at all. It needs its own security design first.
- **The camera is currently aimed at the ceiling and out of focus.** Nothing
  in software fixes that.
- **D-1 matters more now.** The denied-user Discord test is still unrun, and
  the allowlist is now the only thing between a stranger and a view of the
  room.
