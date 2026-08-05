# Camera — Raspberry Pi Camera Module 3

Everything here was verified on this Pi on **2026-08-05**. Nothing is assumed
from a datasheet. Re-verify any time with `python3 tools/camera_probe.py`.

Status: **hardware present and working. No integration written yet** — this is
the ground truth to build tomorrow's work on, not a feature.

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

**4. Let AE/AWB settle.** ~1.5 s after `start()` before the first meaningful
capture, or the frame reflects the initial guess rather than the scene.

---

## Deliberately not decided

No integration exists yet. Open questions for tomorrow, none of them answered
here:

- Does a preview replace the animation, or is it a separate screen?
- Who owns the camera — the display service, the gateway, or a Hermes plugin?
  It cannot be two of them at once; the sensor is exclusive.
- Stills-on-demand via a Hermes tool would reuse the `display_show_image`
  trust boundary already built in `hermes_ext/plugins/hermes_display/`. That
  path already converts to RGB565 and validates length, so it is the cheapest
  honest route to "show me what you see".
- Privacy: `docs/SECURITY.md` treats the bot token as shell access. A camera
  raises that materially — anyone who can reach the bot can see the room.
  That belongs in the threat model **before** the feature, not after.
