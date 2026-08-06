# CLAUDE.md — Hermes Pi

Context for Claude Code working on this project. Read this first.

---

## What this is

A persistent physical AI assistant on a Raspberry Pi 5. **Hermes Agent**
(Nous Research) is the assistant runtime — we do **not** write an agent. This
repo is everything *around* it: a framebuffer display renderer, the integration
that feeds it real agent state, and the service definitions that keep it alive
unattended.

Reached over **Discord** from any device. Inference runs on the user's
**ChatGPT Plus subscription** — no API key, nothing billed. A 3.5" SPI panel
shows a JARVIS-style visual that tracks what the agent is actually doing.

**Status: built, working, all 9 planned phases complete.** 17 commits.
Everything below is verified on real hardware, not assumed.

---

## The one rule

**The panel never invents state.** Every pixel traces to a real Hermes event or
to something the renderer observed itself. No free-running "thinking"
animation.

When the two disagree, **observation wins**. `state.json` is an assertion by a
process that may be dead or wedged; `systemctl is-active` is a fact. If you
touch `display/states.py`, keep this ordering intact — `tests/test_states.py`
asserts it, including the case where the state file claims "thinking" while the
unit is dead.

---

## Layout

```
~/projects/hermes-pi/
├── camera/               the sensor owner (systemd user service)
│   ├── sensor.py         ★ ONLY picamera2 file. Swap to change cameras.
│   ├── encode.py         JPEG under a byte ceiling + RGB565 panel preview
│   ├── protocol.py       the tmpfs contract shared with the plugin
│   ├── stream.py         live MJPEG for a browser — TRUST BOUNDARY (token)
│   ├── hands.py          MediaPipe hand/finger tracking — OBSERVATION ONLY
│   └── __main__.py       lazy lifecycle, request serving, ring buffer, stream
├── display/              the renderer (systemd user service)
│   ├── panel.py          ★ ONLY hardware-specific file. Swap to change panels.
│   ├── states.py         resolution order; the "never wrong" logic
│   ├── player.py         mmap'd RGB565 pack playback
│   ├── render.py         Pillow chrome (header/label/footer), zone dirty-hashing
│   ├── watcher.py        polls state.json + request.json
│   ├── health.py         systemd + NTP-sync observation
│   └── __main__.py       main loop @ 30 Hz
├── hermes_ext/           installed INTO ~/.hermes via scripts/install-hermes-ext.sh
│   ├── hooks/hermes-display-state/   in-process; publishes agent state
│   ├── plugins/hermes_display/       display_show_image/_text/_clear — TRUST BOUNDARY
│   └── plugins/hermes_camera/        camera_look/_watch — hands the model real pixels
├── tools/
│   ├── render_frames.py  generates the visual → assets/anim/*.pack
│   ├── bench_spi.py      measures REAL SPI throughput
│   ├── claude_usage.py   rolling 5h Claude token usage → usage.json
│   └── camera_probe.py   verifies the camera is LIVE and measures its rate
├── scripts/
│   ├── install-hermes-ext.sh  symlinks hooks + plugins into ~/.hermes
│   └── install-cv.sh          cv-venv (mediapipe) + hand_landmarker.task
├── tests/                8 modules, all runnable as plain python3
├── systemd/              unit + drop-in templates
└── docs/                 ARCHITECTURE, HARDWARE, SECURITY, RUNBOOK, DECISIONS,
                          STATE-CONTRACT, DEFERRED, CAMERA
```

`assets/anim/*.pack` is **gitignored and generated** (~95 MB, 8 packs).
Rebuild: `python3 tools/render_frames.py --out assets/anim` (~2 min).
`fps` lives only in the `.json` sidecars — changing it needs no re-render.

---

## Environment

| | |
|---|---|
| Host | Raspberry Pi 5, Debian 13 trixie, aarch64, 7.9 GB RAM |
| User | `alanmyin` — **everything runs as this user, never root** |
| Panel | ILI9486 SPI TFT, 480×320 RGB565, `/dev/fb0`, **32 MHz** |
| Hermes | v0.20.0 at `~/.hermes/`, `hermes` on PATH |
| Model | `openai-codex/gpt-5.6-terra`; auxiliary → `gpt-5.6-luna` |
| Services | `hermes-gateway`, `hermes-display`, `hermes-camera`, `hermes-usage` (user) · `hermes-fbcon-detach` (system) |
| Runtime state | `/run/user/1000/hermes-display/{state.json,request.json,images/}` |
| Network | LAN only, `192.168.2.56`. Two network-facing sockets: **22** (ssh) and **8081** (camera live view, token-gated). |

The renderer needs **no installed dependencies** — system Pillow 11.1.0 and
numpy 2.2.4 only. Do not add a venv or pip installs without good reason.

---

## Traps that already cost time — do not rediscover these

**1. `sshd` is FIRST-wins; `systemd` drop-ins are LAST-wins.** Opposite rules,
both hit in this project.
- `sshd_config.d/` → our override is `10-` because `50-cloud-init.conf` sets
  `PasswordAuthentication yes` and the *first* value wins.
- `systemd/journald.conf.d/` → ours is `99-` because Raspberry Pi OS ships
  `40-rpi-volatile-storage.conf` and the *last* value wins.

Verify with `sudo sshd -T`, never by reading the file.

**2. fbtft is ROW-granular, not rectangle-granular.** A 240×240 blit transmits
the full 480-wide rows — measured 228.8 KiB, same as 480×240 (ratio 1.00×).
**Width is free; only row count costs.** "Shrink the region to save bandwidth"
is false here. Frame rate = dirty rows × 480 × 2 bytes.

**3. Animation motion must use INTEGER cycles per loop.** Float multipliers
leave a remainder at the wrap and the rings visibly snap back a few degrees,
once per loop, forever. `tests/test_anim_seam.py` catches it.

**4. Timing framebuffer writes measures memcpy, not SPI.** fbtft defers I/O to
a workqueue. Use `tools/bench_spi.py`, which reads the kernel's own
`/sys/class/spi_master/spi0/spi0.0/statistics/bytes_tx`.

**5. Raising the SPI clock alone does nothing.** Pack `fps` in the sidecar JSON
must be raised to match, or nothing asks for the extra capacity.

**6. The Pi has no battery-backed RTC.** For ~34 s after boot the clock is
confidently wrong (it resumes near last shutdown). The header shows `--:--`
until `timedatectl NTPSynchronized` is true. Correlate boot events with
`ps -o lstart`, not log timestamps.

**7. `mmap.close()` raises `BufferError`** if a numpy view is still alive. Drop
the array first.

**8. Hermes' hook `emit()` AWAITS coroutine handlers inline** — a slow hook
stalls the agent pipeline. The hook must stay a tiny non-blocking tmpfs write.

**9. A fixed poll interval cannot express a frame period that is not a multiple
of it.** At `POLL=0.03` and 12 fps, frames displayed for **90/90/90/60 ms**
instead of a steady 83.3 ms — a visible hitch four times a second. The loop now
sleeps to `player.next_due()`, which holds intervals to ±1 ms. Any future frame
rate must keep this; do not go back to a bare `time.sleep(POLL)`.

**10. Asking for more fps than the bus carries makes it JUMPY, not fast.** At
12 fps the demand was 2,672,640 B/s against 2,562,838 available; roughly one
frame in 23 could not be sent and the wall-clock index skipped to catch up.
Stay meaningfully below the ceiling — 9 fps leaves 13.3% headroom. 10 fps fits
arithmetically but leaves only 3.6%, so any slow blit becomes a visible skip.

**11. Zone dirty-hashing stops chrome being BLITTED, not DRAWN.** Every loop
iteration built three PIL images and rasterised text purely to hash the result
and discard it. That was the single largest CPU cost in the process — 6.36% of
a core. Chrome is now rasterised on a 0.5 s tick (`CHROME_PERIOD`), and
immediately on screen change so transitions never wait on a timer: **0.73%**.
If you add anything to the chrome, keep it behind that tick.

**12. `picamera2` has no `__version__`.** `picamera2.__version__` raises
`AttributeError`, which is indistinguishable from a failed import if the
traceback is truncated. The package IS installed. See `docs/CAMERA.md`.

**13. "Is the camera in use?" CANNOT be answered by open file descriptors.**
libcamera never opens the capture node (`/dev/video0`, `rp1-cfe-csi2_ch0`); it
opens `/dev/media0-1`, `/dev/v4l-subdev0-3` and `/dev/video1,4,6,7,20-27`. And
**pipewire and wireplumber hold exactly that same set permanently, from boot,
on a fully idle system** — including `v4l-subdev2`, the imx708 itself. An
fd-based indicator reads "camera in use" 24/7, which is worse than none: it
trains you to ignore it. Use the sensor's runtime power state instead —
`/sys/bus/i2c/devices/*/power/runtime_status` where `name` is `imx708`. It is
`suspended` at rest despite those fds, `active` only while streaming, and lags
~5 s on the way down (`autosuspend_delay_ms`), which is the safe direction.

**14. `StartLimitIntervalSec` / `StartLimitBurst` belong in `[Unit]`, NOT
`[Service]`.** In `[Service]` systemd logs `Unknown key ... ignoring` and the
crash-loop guard silently does not exist. `hermes-display.service` carried that
bug from the start; both units are fixed. Check with
`journalctl --user -u <unit> | grep -i "unknown key"` after any unit edit.

**15. The panel body has exactly ONE owner per frame, and it must be declared.**
`show_image()` blits a picture, then `renderer.draw()` repaints the text body on
top of it, so the image is never seen. `display_show_image` had this bug from
the day it was written. `display/__main__.py` now tracks `body_owned` and skips
the text redraw. If you add another body producer, extend that flag.

**16. picamera2's `"RGB888"` hands you BGR.** The format name describes packed
byte order, which is the reverse of the channel order in the numpy array.
Treating it as RGB swaps red and blue: skin goes blue, warm light goes cold,
and the whole frame reads as a bad exposure. `camera/sensor.py` reverses it.
Verify empirically rather than from docs — compare `capture_array()` against
`capture_image()` (picamera2's own PIL path, known correct): mean abs diff was
**1.43 as-is vs 0.27 channel-reversed**.

**17. A rotated sensor produces PORTRAIT frames; never resize to fixed
dimensions.** The module is mounted 90 degrees off, so corrected frames are
576x1024. Resizing those to a landscape profile (768x432) squashed everything
to a third of its height. All resizes now fit the LONG EDGE and keep aspect
(`encode._fit_long_edge`) — including the ring buffer, which had the same bug
one layer down and made warm contact sheets differ from cold ones. Rotation is
`HERMES_CAMERA_ROTATE` (default 90).

**18. The panel body has rows that only the camera preview paints.** The
preview is 264 rows and fills the body; the animation is 232 at y=28 and the
label strip covers 262..290, so rows **26, 27, 260, 261** belong to nobody.
Leaving an IMAGE screen without wiping the body leaves a bright band of the old
photo above the state label. `_clear_body()` runs on that transition.

**19. Hermes' `task_id` identifies a SESSION, not a turn.** It is documented as
"unique identifier for terminal/browser session isolation" and is stable for a
whole Discord conversation. A per-turn counter keyed on it never resets: the
camera tool refused permanently after N captures and reported its "capture limit
is exhausted" until the gateway restarted. Any rate limit here must be a
SLIDING WINDOW so it cannot wedge — `tests/test_camera_tools.py` pins that.

**20. `claude -p "/usage"` gives the REAL usage figures, non-interactively.**
Session and weekly percentages, straight from the server. Costs **zero tokens**
(measured against a control period), takes 1.7-3.3 s, creates no transcript.
Two traps around it:
- There is no `usage` SUBCOMMAND. Checking `claude --help` for one and
  concluding it cannot be done is wrong -- it is a slash command, and `-p`
  runs slash commands.
- Resolve the binary explicitly. It lives in `~/.local/bin`, which is NOT on a
  systemd user service's PATH, so relying on PATH works from a shell and
  silently returns nothing from the service -- the panel then shows local
  estimates while looking authoritative.

The panel bar draws the server percentage when available, an owner-declared
budget otherwise, and NOTHING if neither. It previously fell back to drawing
how far through the 5-hour window we were, which sat at ~45% while the real
figure was 90% -- honest in the source, misleading on the glass.

**21. `pkill -f "<pattern>"` matches its own shell.** The pattern appears in the
invoking command line, so it kills the caller (exit 144). Anchor it:
`pgrep -f "^/usr/bin/python3 -m camera"`.

**22. "Wait on a Condition, then read the frame" gives up exactly when it
matters.** The obvious MJPEG shape — `if seq == mine: cond.wait(); return
frame` — is wrong twice: `wait()` can return spuriously, and `drop()` advances
the sequence while leaving the frame `None`, which is precisely the state of a
sleeping camera. A viewer connecting then sees "the sequence already moved",
concludes it missed something, and hangs up. **Every stream closed after 0
frames**, and it looked like a network fault. Wait in a `while` keyed on the
sequence, re-deriving the remaining timeout each pass.
`tests/test_stream.py:test_wait_survives_a_dropped_frame` was verified to FAIL
against the original before being kept.

**23. Resizing before the colour/rotation correction saves less than it looks
like it will.** Correcting a 640-long-edge frame instead of a full one is
obviously cheaper, so `Sensor.grab(long_edge=)` does it — but the resize itself
costs the same either way, because its INPUT is full-res regardless. Measured
per stream frame: 18.3 ms → 16.3 ms, an 11% trim, against a predicted 5 ms
saving that would have been 30%. The service total barely moved (32.9% →
32.5%). Profile stages against the real sensor before believing an arithmetic
model of them — the first model here was wrong by more than the saving.

**24. A camera stream's exposure cap wants the OPPOSITE of a still's.**
`FRAME_DURATION_LIMITS` allows 100 ms because a longer exposure halves the
grain for a model reading a photo. On a live view that same cap drops a dim
room to 10 fps and smears every hand movement — the thing gesture work exists
to see. 30 fps is pinned while a viewer is attached, via `set_controls`, not a
reconfigure (which would cost ~300 ms and a re-settle). Measured cost in a dark
room: mean level 94.0 → 65.3, with **gain already at max 16 so it cannot be
bought back**. In normal light neither cap binds.

**25. "Raise the resolution for a wider field of view" is a category error, and
this camera is ALREADY at 100% of its sensor.** MEASURED at four configurations
with `SENSOR_OUTPUT_SIZE` forced: `ScalerCrop` is `(0,0,4608,2592)` at
1024x576, 1536x864 **and** 2048x1152. Resolution buys DETAIL, never more room.
The FoV here is a lens fact — the standard `imx708` (~75° diagonal) is fitted;
widening it is an `imx708_wide`, i.e. a purchase. (Without the forced mode,
libcamera auto-selects 1536x864 and you get **44.4%** of the sensor — that is
the crop trap already documented, and it is the one thing that genuinely does
change FoV.)

**27. `status.json`'s `updated_at` IS NOT A HEARTBEAT, and reading it as one
wasted a real diagnosis.** The document is built on demand, so when it is
fetched over HTTP the timestamp is written by the *HTTP thread*. During an
observed wedge — capture loop stuck, sensor open and powered, zero frames, every
browser connection closing after 5 s with nothing — `updated_at` stayed
perfectly current and I concluded the loop was alive. It was not. Use
`loop_idle_s` and `last_frame_age_s`, which only the capture loop writes.
`camera/__main__.py` now also carries a watchdog: while the sensor is open the
ring pumps regardless of viewers, so a frame drought is unambiguous, and it
exits 70 for systemd rather than hanging silently. Root cause of that wedge is
**still unknown** and it has not reproduced, including under deliberate
browser-like load — the fix makes it loud and self-healing, not impossible.

**26. Feeding unrelated stills to a `RunningMode.VIDEO` tracker measures
nothing.** VIDEO mode carries a track between frames and uses the previous
frame as a prior, so jump-cutting between different photos breaks it. The
finger classifier scored **9/16 and looked broken** measured that way; given a
steady clip per pose it is **16/16**, including at all four rotations and at
the real 540x960 stream geometry. Use `RunningMode.IMAGE` for still fixtures,
or give the tracker continuity.

---

## Conventions

- **Verify against the installed source, not the docs.** `~/.hermes/hermes-agent/`
  is the truth; the published docs were wrong or incomplete more than once.
- **Measure, don't assume.** Every performance number in `docs/HARDWARE.md` came
  from a counter, and two plausible-sounding estimates turned out badly wrong.
- **Never modify Hermes core.** Use its hook and plugin extension points.
  `scripts/install-hermes-ext.sh` symlinks ours in (loader follows symlinks).
- **Secrets never enter this repo.** `~/.hermes/.env`, `~/.hermes/auth.json`.
- **Commit messages explain WHY**, especially when a measurement changed the
  design. Co-author trailer: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
- **Restart limits:** gateway 10-in-120s. Ordinary admin restarts tripped the
  original 5-in-300s. Recover with `systemctl --user reset-failed hermes-gateway`.

---

## Testing

```bash
cd ~/projects/hermes-pi
python3 tests/test_states.py         # panel cannot claim health when there is none
python3 tests/test_anim_seam.py      # animation loops close exactly
python3 tests/test_display_tools.py  # hostile images/URLs refused
python3 tests/test_camera_tools.py      # a stale frame is never shown as live
python3 tests/test_camera_indicator.py  # unknown camera state fails toward ON
python3 tests/test_usage_parse.py       # session figures never borrow the weekly line
python3 tests/test_stream.py            # the room is not served without a token
python3 tests/test_hands.py             # fingers read the same at every rotation
```

`pytest` is NOT installed system-wide — every test module runs standalone via
`__main__` and imports pytest defensively.

---

## Measured performance (do not regress)

```
SPI 32 MHz · 2,562,838 B/s · 0 errors · 0 timeouts
one 480x232 frame costs 246,499 B TRANSMITTED  ->  ceiling 10.37 fps
running at 9 fps = 2,221,200 B/s = 86.7% of bus, 13.3% headroom
display  0.73% CPU · 52 MB RSS      gateway  0.8% CPU · 163 MB RSS
```

**Budget on transmitted bytes, not pixel bytes.** A 480x232 frame is 222,720 B
of pixels but **246,499 B cross the bus** — fbtft's deferred IO is PAGE
granular, so a 232-row write dirties partial pages at both ends and ~25 extra
rows go out. Measured by playing one pack at 5 and 10 fps and solving. Costing
it by pixel count overstates capacity by 11% and was how fps came to be set
above what the hardware could carry.

**"Idle writes zero SPI bytes" is FALSE and has been since animation packs
landed.** It was true in Phase 6, when the body was a static text screen.
`player.due()` now advances every frame in every state including idle, and each
advance blits all 232 rows: **2.2 MB/s sustained, permanently**. Zone
dirty-hashing still gives the *chrome* zones zero cost, which is the part that
remains true. Not a fault — 0 errors, thermals fine — but it is waste, and the
fix (row-span dirty detection in `player.py`) is unimplemented.

The 2.9% CPU figure that used to sit here was also from the text-screen era.
Measured on the animated build, the ORIGINAL code cost 6.36%; it is 0.73% now
because chrome rasterising is throttled (see trap 11).

---

## Current task — camera DONE; gestures deliberately not built

**Hermes can see.** Ask over Discord and it looks through the Camera Module 3
and answers from the actual pixels — no second model, no local captioning.
`camera_look` (one frame) and `camera_watch` (four moments as one contact sheet,
for motion). Measured numbers, context cost and privacy design in
`docs/CAMERA.md`; the tmpfs interface in `docs/CAMERA-CONTRACT.md`.

Key facts a future session should not have to rediscover:

- Cold capture **673 ms**, warm **44 ms**. A `normal` frame is **~17 KB base64**.
  A four-moment contact sheet is ~29 KB — the same order as one frame, which is
  the whole point of packing them into a grid.
- Images are **immutable in conversation history and re-sent every turn**. That,
  not latency, is the binding constraint. Hence small-by-default, a hard byte
  ceiling, and a 3-image cap per turn.
- The sensor is **lazy** (closed at rest, 20 s idle timeout) and **exclusive** —
  exactly one process may hold it, which is why `hermes-camera` exists as a
  third service rather than living in the display or the gateway.
- The panel's CAM light is driven by the **kernel's sensor power state**, not by
  anything the camera service claims, and it **fails toward ON**. See trap 13
  for why the obvious file-descriptor approach cannot work here.

**There is also a live browser view** (`camera/stream.py`, tcp/8081), added as
the first step toward CV/gesture work. `python3 -m camera --stream-url` prints
the link. Structure follows the owner's auto-drone `streaming/mjpeg_server.py`;
its *camera settings* were deliberately not copied (2 ms exposure, gain 8, NR
off, sharpness 2.0 — right for AprilTag detection on a vibrating airframe,
wrong for a room). Measured 14.1 fps, 8.3 KB/frame, 117 KB/s, **32.5% of one
core while watched**, display unaffected at 1.05%. Key properties:

- It runs **inside** `hermes-camera` because the sensor is exclusive — it is a
  third consumer of frames the loop already grabs, not a second owner.
- **Viewers are the on switch.** Opening the page wakes the sensor; closing the
  last tab sleeps it after an 8 s linger (which covers a page reload).
- The **token is on by default** and checked on every endpoint. See
  `docs/SECURITY.md` — this is the second network-facing socket on the box and
  the first that serves a view of the room.

### Hand tracking — BUILT as observation only (2026-08-05)

`camera/hands.py` runs MediaPipe's hand landmarker: 21 points per hand,
handedness, up to two hands, drawn on the live stream as skeleton + bounding
box + `RIGHT PEACE [2]`. Observation is published to `hands.json` and
`/hands.json`.

**It triggers nothing, and that is deliberate.** See below.

**`Do not install mediapipe` is SUPERSEDED — it was right when written.** The
blocker was that no wheel existed. Re-checked 2026-08-05: mediapipe 1.0.0
publishes `py3-none-manylinux_2_28_aarch64` and installs cleanly. It lives in
its own venv (`scripts/install-cv.sh`) created `--system-site-packages`, and
**verified not to shadow the system numpy 2.2.4** — picamera2 breaks against a
different one, at capture time rather than at import. The renderer's
no-dependencies property is untouched; only `hermes-camera` uses the venv, via
`ExecStart=~/.local/share/hermes-pi/cv-venv/bin/python`. **The model is NOT in
the wheel** (zero `.tflite` files), so the installer fetches
`hand_landmarker.task` separately.

Measured: detection **~60 ms and ~60 ms at every input size** (mediapipe
rescales internally), so resolution is nearly free for it but 10 Hz is already
two thirds of a core. It runs on **its own thread** — inline it would stall the
15 fps stream. Results are therefore always slightly stale, and a result older
than `RESULT_MAX_AGE` (0.5 s) **is not drawn**, because a box hovering where a
hand used to be reads as a live track.

### What is NOT built, and why

**Gesture triggers.** A gesture is a path from "someone waves in the room" to
"the agent runs a tool", and the Discord allowlist does not cover that path at
all — anyone physically present becomes an unauthenticated user of a bot with a
shell. It needs its own security design first: an explicit, bounded, visibly
indicated watch mode; a closed vocabulary mapped to a fixed action allowlist;
and preferably a restricted toolset for that lane. See `docs/SECURITY.md`.
`hands.json` says so in the file itself, because that is the file someone will
find first when wiring a gesture to a command.

### Physical

The camera **has been aimed at the room and focused** (was ceiling-facing until
2026-08-05). Colour is now judgeable and looks correct on a real scene.
**The scene may read rotated** — `HERMES_CAMERA_ROTATE` (default 90) is the
knob. It affects human viewing only: hand reading is rotation-invariant and was
validated 16/16 at 0/90/180/270.

---

### Visual direction — settled

The panel shows the original rings-and-core visual. A HUD/multi-pane direction
was explored against https://github.com/purzbeats/interfaces and **deliberately
abandoned**; the repo was reverted to the plain visual. Do not re-attempt it
without being asked. What was learned and is worth keeping:

- That repo CAN run on this Pi (headless Chromium + SwiftShader; node, chromium
  and ffmpeg are all installed). The blocker was macOS-only Chromium flags
  (`--use-angle=metal`, `--disable-software-rasterizer`), not the hardware.
  The old claim here that it was "architecturally incompatible" was wrong.
- Its output is unusable at 480x232 as-is: it BSP-subdivides into ~60px
  fragments and falls back to JPEG capture, which puts noise on the black.
- Third-party HUD elements bake in **fake telemetry** — one rendered
  "46% PWR LVL", which a pre-rendered pack cannot retract. Screen any borrowed
  asset for text before shipping it.

---

## Deferred / open

- **D-1 (docs/DEFERRED.md): the denied-user Discord test is UNRUN.** Needs a
  second Discord account. The allowlist is the only thing between Discord and
  shell access. "The right person got in" is not evidence the wrong person is
  kept out. Do before calling the prototype finished.
- **D-2:** clock wrong ~34 s after boot (handled, documented).
- Voice/camera deliberately deferred; seams documented in `docs/ARCHITECTURE.md`.
  Pi 5 has no analog audio out — a USB/I2S DAC will be needed, and an I2S HAT
  may contend with the SPI panel for GPIO.
- Only one SSH key exists (`alanmyin-laptop`). Losing it means physical
  recovery. Adding a second key is cheap insurance.

---

## Read these before changing things

| Doc | For |
|---|---|
| `docs/ARCHITECTURE.md` | shape, and *why* |
| `docs/HARDWARE.md` | measured facts the design rests on |
| `docs/RUNBOOK.md` | operating and repairing it |
| `docs/DECISIONS.md` | D1–D7, why choices were made |
| `docs/STATE-CONTRACT.md` | the interface between the two processes |
| `docs/SECURITY.md` | threat model; bot token ≈ shell access |
| `docs/DEFERRED.md` | what is knowingly not done |
| `docs/CAMERA.md` | Camera: how it works, measured rates, context cost, privacy |
| `docs/CAMERA-CONTRACT.md` | the tmpfs interface between camera service and plugin |
