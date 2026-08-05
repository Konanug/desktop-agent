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

## Still open

- **Gestures are not built.** A gesture trigger is a path from "someone waves
  in the room" to "the agent runs a tool", and the Discord allowlist does not
  cover it at all. It needs its own security design first.
- **The camera is currently aimed at the ceiling and out of focus.** Nothing
  in software fixes that.
- **D-1 matters more now.** The denied-user Discord test is still unrun, and
  the allowlist is now the only thing between a stranger and a view of the
  room.
