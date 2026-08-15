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
**ChatGPT Plus subscription** — no API key, nothing billed. An 800x480 HDMI
panel shows a JARVIS-style visual that tracks what the agent is actually doing.

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
│   ├── stream.py         live MJPEG + /events SSE — TRUST BOUNDARY (token)
│   ├── hands.py          MediaPipe hand/finger tracking — OBSERVATION ONLY
│   ├── custom.py         user-trained gestures: normalise + k-NN that abstains
│   ├── gestures.py       level → debounced EDGE; publishes, never acts
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
│   ├── plugins/hermes_camera/        camera_look/_watch — hands the model real pixels
│   ├── plugins/hermes_voice/         speak — the reply, out loud
│   └── plugins/hermes_google/        gmail/calendar, READ-ONLY scopes
├── tools/
│   ├── render_frames.py  generates the visual → assets/anim/*.pack
│   ├── gesture_train.py  record YOUR hand signs → custom_gestures.json
│   ├── gesture_calibrate.py  live pinch/reach readout for thresholds
│   ├── claude_usage.py   rolling 5h Claude token usage → usage.json
│   └── camera_probe.py   verifies the camera is LIVE and measures its rate
├── scripts/
│   ├── install-hermes-ext.sh  symlinks hooks + plugins into ~/.hermes
│   ├── install-cv.sh          cv-venv (mediapipe) + hand_landmarker.task
│   └── install-gesture-client.ps1  the laptop task, and it STARTS it
├── voice/                the microphone owner (systemd user service)
│   ├── listen.py         wake word + endpointing + faster-whisper (two models)
│   ├── fastlane.py       spoken commands that SKIP the agent -> /intent
│   ├── sink.py           HMAC POST to the narrowed webhook lane; rate limits
│   ├── speak.py          piper TTS out the ReSpeaker
│   └── __main__.py       the loop; STATE, NEVER CONTENT in status.json
├── clients/windows/      hermes_gesture.py — STDLIB ONLY, runs on the laptop
├── tests/                12 modules, all runnable as plain python3
├── systemd/              unit + drop-in templates
└── docs/                 ARCHITECTURE, HARDWARE, SECURITY, RUNBOOK, DECISIONS,
                          STATE-CONTRACT, DEFERRED, CAMERA
```

`assets/anim/*.pack` is **gitignored and generated** (~243 MB, 8 packs — it
was ~95 MB at 480x232; 800x358 is 2.7x the pixels).
Rebuild: `python3 tools/render_frames.py --out assets/anim` (~5 min).
`fps` lives only in the `.json` sidecars — changing it needs no re-render.

---

## Environment

| | |
|---|---|
| Host | Raspberry Pi 5, Debian 13 trixie, aarch64, 7.9 GB RAM |
| User | `alanmyin` — **everything runs as this user, never root** |
| Panel | Waveshare HDMI LCD, **800×480** RGB565, `/dev/fb0` via vc4 KMS fbdev. Mode is FORCED in `cmdline.txt` (`video=HDMI-A-1:800x480@60D`) because the panel returns **zero bytes of EDID** |
| Audio | ReSpeaker 2-Mic Pi HAT (WM8960, I2C 0x1a + I2S). ALSA card `wm8960soundcard`, **excluded from WirePlumber** so nothing fights the mixer. Mixer set by `hermes-audio.service`, verified across a reboot. **`Input Mixer Boost` is the switch that connects the mics at all** — off by default, and with it off the ADC returns flat silence however correct every other control looks. Mics now genuinely live (ambient rms 178 vs 0.98). **Speaker output still never confirmed — nothing plugged in** |
| Hermes | v0.20.0 at `~/.hermes/`, `hermes` on PATH |
| Model | `openai-codex/gpt-5.6-terra`; auxiliary → `gpt-5.6-luna` |
| Services | `hermes-gateway`, `hermes-display`, `hermes-camera`, `hermes-usage`, `hermes-audio`, `hermes-voice`, `hermes-button` (user) · `hermes-fbcon-detach` (system) |
| Runtime state | `/run/user/1000/hermes-display/{state.json,request.json,images/}` |
| Network | LAN only, `<pi-lan-ip>`. Two network-facing sockets: **22** (ssh) and **8081** (camera live view, token-gated). |

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

**2. ⚠ HISTORICAL — the SPI panel was removed 2026-08-06.** Traps 2, 4, 5 and
10 all describe the ILI9486/fbtft/SPI bus, which no longer exists. They are
kept because the *reasoning* recurs and because much of the code still carries
its shape, but do not treat their numbers as current. **There is no bus budget
any more**: the framebuffer is memory a display controller scans out on its
own, so writes cost a memcpy and nothing else. `tools/bench_spi.py` has been
deleted along with the panel it measured.

Original: fbtft was ROW-granular, not rectangle-granular. A 240×240 blit
transmitted the full 480-wide rows — measured 228.8 KiB, same as 480×240 (ratio
1.00×). Width was free; only row count cost.

**3. Animation motion must use INTEGER cycles per loop.** Float multipliers
leave a remainder at the wrap and the rings visibly snap back a few degrees,
once per loop, forever. `tests/test_anim_seam.py` catches it.

**4. HISTORICAL (see 2). Timing framebuffer writes measured memcpy, not SPI.**
fbtft deferred I/O to a workqueue; the old bench read the kernel's own
`bytes_tx` counter. On HDMI a memcpy is now genuinely all there is, so timing
the write IS the honest measurement — the opposite of what this trap warned.

**5. HISTORICAL (see 2). Raising the SPI clock alone did nothing** — pack `fps`
had to rise to match. The surviving half is still true and still bites:
**`fps` is the playback rate of a FIXED frame count**, so raising it shortens
the loop period rather than smoothing motion. Smoother motion means rendering
more frames, which now costs disk rather than bus.

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

**10. HISTORICAL (see 2). Asking for more fps than the bus carried made it
JUMPY, not fast.** At 12 fps the demand was 2,672,640 B/s against 2,562,838
available; roughly one frame in 23 could not be sent and the wall-clock index
skipped to catch up. 9 fps was chosen for 13.3% headroom. **On HDMI there is no
such ceiling** — the packs still run at 9 fps only because that is the rate
their integer cycle counts were composed for.

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

**28. `time.monotonic()`'s ORIGIN IS UNDEFINED, so 0.0 is not a safe "never"
sentinel.** `GestureGate` used `_last_fire = 0.0` and compared `mono -
_last_fire < min_gap`. On this Pi monotonic starts large so it worked; under
test, where time starts at 0, **the very first gesture of every session was
silently swallowed**. Anything that means "has not happened yet" must be `None`
and be tested for, not a number that happens to be far away on one machine.

**29. A debounce built on CONSECUTIVE agreement never commits under flicker.**
The obvious shape — "N frames in a row agree" — is reset by a single
mis-detected frame, and hand detection mis-reads frames routinely (motion blur,
a hand turning). At any flicker rate near N it commits *never*, not late.
`camera/gestures.py` requires a MAJORITY of a sliding window (3 of 5) instead,
which tolerates one bad frame in every three.
`tests/test_gestures.py:test_one_bad_frame_does_not_break_a_held_gesture` is
the pin; it fires 0 events against the consecutive-run version.

**30. `SendInput`'s `INPUT` union is sized by `MOUSEINPUT`, not by the member
you use — and `--dry-run` cannot catch getting it wrong.** The union's size
comes from its LARGEST member (MOUSEINPUT, 32 B on x64), so declaring only
`ki` — which every abbreviated copy of this snippet online does — makes
`sizeof(INPUT)` 32 instead of 40. Windows validates `SendInput`'s third
argument against it and rejects **every** call with `ERROR_INVALID_PARAMETER`
(87), pressing nothing. This SHIPPED, because dry-run never calls SendInput, so
the first real keypress was the first validation. Its symptom was then misread
as UIPI blocking an elevated window, which is a **different** error (5,
`ERROR_ACCESS_DENIED`); the client now names both rather than guessing. Layout
is asserted at import and pinned by `tests/test_gestures.py` — which runs on
the **Pi**, because the client uses explicit-width ctypes types rather than
`ctypes.wintypes` precisely so its structs can be tested off Windows. Both
tests were verified to FAIL against the shipped version.

**31. A SHORT RECORDING SUMMARISED BY ITS PEAK CANNOT TELL A LIVE MIC FROM A
DEAD ONE.** The ReSpeaker's `Input Mixer Boost` comes up OFF, and with it off
the WM8960's ADC returns a flat ~1.0 RMS — digital silence — while every other
capture control reads correct (LINPUT1 routed on, Capture +30 dB unmuted, ADC
unmuted, ALC off). A 3 s `arecord` showed peaks of ~250 and was reported here
as "both mics measured live". It was not: reading the stream frame by frame
showed **all of that energy in the first 80 ms and silence thereafter** — the
stream-start transient, not sound. Measure the level OVER TIME, or a dead
microphone passes. Measured back to back in one room: as-shipped 0.98 rms,
`Input Mixer Boost on` 146, plus a +20 dB LINPUT1 boost 4471 (which then clips
on speech). `scripts/audio-setup.sh` sets it.

**32. `round(numpy.float32)` RETURNS `numpy.float32`, and json.dumps refuses
it.** onnxruntime hands back float32 scores; rounding them for a status file
looks like it produces a Python float and does not. The crash lands in the
status writer, far from the value's origin, and only when that field is
non-zero. `tests/test_hands.py` already pinned this for the camera and the
lesson still had to be relearned — coerce with `float()` at the publishing
boundary, not per field.

**34. `systemctl mask` on a unit stored in `/etc/systemd/system/` DESTROYS IT.**
Masking writes a symlink to `/dev/null` at that exact path, over the real file.
Unmasking then removes the symlink and the unit is simply gone —
`hermes-fbcon-detach` came back only because `systemd/` is committed. Mask
units that ship with the OS; for our own, `systemctl disable` is the reversible
one.

**33. A tool schema wrapped in the OpenAI `{"type":"function","function":{…}}`
envelope REGISTERS FINE AND SILENTLY LOSES ITS PARAMETERS.** Hermes'
`ctx.register_tool` wants `name`/`description`/`parameters` FLAT at the top
level, as every plugin in `hermes_ext/` does. Wrapped, the tool appears in the
surface, the model calls it, and the handler receives `args={}` with only
`task_id`/`session_id`/`user_task` in kwargs — the declared arguments never
arrive. `speak` therefore answered "nothing to say" on every voice turn, which
looks like a handler bug and was misdiagnosed as one twice. **Also: Hermes
calls handlers BOTH ways** — `handler({"text": …})` and `handler({}, text=…)` —
so `hermes_ext/plugins/_argshim.py` reads from both rather than guessing. The
symptom of getting either wrong is silence at the far end of the pipeline, a
long way from the schema.

**35. A CLIENT TIMEOUT EQUAL TO THE SERVER'S WAIT IS A COIN TOSS, AND IT
PRESENTS AS FLAKINESS.** `/snapshot.jpg` deliberately waits `FRAME_WAIT` (5.0 s)
for a fresh frame before refusing, and the test that pins that behaviour called
`urlopen(..., timeout=5)`. Which side fired first was decided purely by
scheduling: on an idle Pi the server won and the test passed, on a loaded
GitHub runner the client won and it failed with `TimeoutError`. It passed here
and failed in CI for three commits, including a **docs-only** one — which is
the tell, because a docs commit cannot break a test. Anything racing a server's
own timeout must DERIVE its timeout from that constant, not restate the number,
or raising one silently puts them back in a tie. Reproduce by running the
module under four spinning cores.

**36. WHISPER HALLUCINATES ON CLIPS UNDER ABOUT A SECOND, so a one-word voice
command cannot be measured — or shipped.** A bare "pause" is ~0.6 s of audio;
`tiny.en` returned "Okay." for "play" and `base.en` returned "toes." for
"pause", spending **9.6 s** to do it against 1.6 s for a normal phrase. The
first benchmark of the fast-lane vocabulary scored **5/11 on both models** and
looked like proof that neither was usable. It was measuring synthetic
single-word clips, which is not the task. Re-run on multi-word phrases padded
the way a real capture is (`PREROLL` ahead, `SILENCE_END` behind), both models
score **15/18** — and, crucially, *identically*, which is the entire argument
for reading with the small one first. Same shape as trap 26: the fixture was
wrong, not the thing being measured.

The corollary is a design rule, not a preference: **every fast-lane phrase is at
least two words**, and `tests/test_fastlane.py` asserts it. Phrase choice is
measured too — "previous track" reads as "Prove this truck" and "skip back" as
"Get back!", while "go back", "last song" and "next song" are 3/3 across voices.

**37. A NAME PUBLISHED WITH NO BINDING AT THE FAR END FAILS COMPLETELY
SILENTLY, and that is the security property working as designed.** The Pi puts a
word on `/intent`; the laptop ignores words it does not know. So `PREV` against a
laptop that binds `PREVIOUS` does nothing, reports nothing, and looks like
success at every layer — `/intent` returns 200 with `subscribers: 1`, the voice
service says "Going back", and no music moves. The first draft of
`voice/fastlane.py` got three of seven names wrong this way.
`tests/test_fastlane.py:test_every_intent_has_a_binding_on_the_laptop` reads
`gestures.example.json` and checks the two sides against each other, because
agreeing once is not the same as staying agreed.

**38. `os.startfile` IS the right API for a URI and still does not get you the
app.** It is `ShellExecute` — it does not route through the browser, and
replacing `webbrowser.open` with it was correct. But it can only reach the
desktop app if something registered a handler for the scheme, and that
registration is not ours: the Microsoft Store build registers differently from
the standalone installer, neither registers before the app has been run once,
and a browser update can claim the association. When it is missing Windows falls
back to the web player — which is indistinguishable from the bug that was just
fixed, and is why "still opens the web" was reported after a correct fix. Launch
the executable with `--uri=` first and keep the scheme as fallback, and **print
which route ran**, because this was diagnosed by guessing twice.

**39. A Windows Scheduled Task with no "Start in" runs from
`C:\Windows\System32`.** A relative `--config` then resolves to a file that is
not there and the client exits within milliseconds — before it has a console, a
log, or any way to say so. That is indistinguishable from "the task never
started", and was diagnosed as exactly that. Set `-WorkingDirectory` *and* have
the program look next to itself; either alone is enough, and the failure is
expensive enough to want both. Related: `LogonType Interactive` is not optional
— a task that runs "whether the user is logged on or not" lives in session 0,
which has no desktop, so every `SendInput` fails while the process stays alive
and connected.

**40. `$PSScriptRoot` CAN BE EMPTY IN A `param()` DEFAULT, and the error names
the wrong thing.** `param([string]$Dir = $PSScriptRoot)` came back empty under
`powershell -ExecutionPolicy Bypass -File .\script.ps1`, and the first cmdlet to
touch it — `Resolve-Path` — failed with *"Cannot bind argument to parameter
'Path' because it is an empty string"*. That names `Path`, a parameter the
script never mentions, and says nothing about which variable was empty. Resolve
in the BODY with a fallback chain (`$PSScriptRoot` → `$MyInvocation.MyCommand.
Path` → `Get-Location`) and check it before use.

**41. `Register-ScheduledTask` NEEDS ELEVATION for the root task folder** —
`Access is denied`, `HRESULT 0x80070005`, from an ordinary PowerShell. So a
Scheduled Task cannot be the only way to autostart the laptop client, and the
installer falls back to a **Startup shortcut**, which needs no admin and still
provides everything `SendInput` requires: logon start, the owner's session, and
a desktop. The only thing lost is restart-on-failure, and the common failure is
the *connection*, which the client already retries forever.

Install **exactly one** of the two and stop any client already running — a
Startup shortcut plus a Scheduled Task has already happened here and shows up on
the Pi as `viewers=2` with every key pressed twice. Match the running client on
its COMMAND LINE, not on `Get-Process pythonw`: the Python in use may be shared,
and on this laptop it is the Hermes agent's own venv, so a blanket kill would
take out something unrelated.

**And if you DO elevate to register a task, the account that installs it is not
the account it must run as.** When UAC elevates a different admin account,
`$env:USERNAME` in that shell is the admin; a task registered for it runs in the
admin's session, so `SendInput` presses keys into a desktop nobody is looking at
while the client connects, receives gestures and reports success. Take the
principal from `Win32_ComputerSystem.UserName` — the console user — and print
both when they differ. `LogonType Interactive` needs no password to register for
another user, since the task only runs while that user is logged on.

**There is no PowerShell on this Pi**, so nothing in
`scripts/install-gesture-client.ps1` can be run before the owner runs it. Four
consequences to keep: every optional refinement (`$trigger.Delay`,
`RestartInterval`/`RestartCount`) is wrapped in `try/catch`, because an
unsupported property must not be why the whole install fails; `$script` was
renamed to `$clientPy`, since `script` is a scope keyword and "legal but
confusing" is not worth it when it cannot be tested; **`$x = if (…) { } <newline>
else { }` is a PARSE ERROR** in an assignment, because the newline ends the
statement — write the `if` as a statement instead; and the script prints the
client's own log on failure rather than naming its path.

**42. `events closed (N events, Ts)` DATES THE DEATH; it does not diagnose the
client.** `sent += 1` runs before the flush, so `0 events` means the first event
*write* raised — the socket was already dead when the gesture arrived, not that
the client mishandled it. Combined with the heartbeat that is the only thing
detecting a silent peer, the arithmetic is: beats at 10 s and 20 s went into a
send buffer and "succeeded"; the write at 28 s got the RST back. So the peer
died between roughly t+10 and t+28 — a WINDOW, not a moment, and never the
timestamp in the log line.

Reading `(0 events, 29s)` as "the client crashed on its first event" is the
tempting inversion and it sends you to read the client. Observed once already,
against a client that was provably fine: running
`clients/windows/hermes_gesture.py --dry-run` ON THE PI, against
`127.0.0.1:8081`, received real gestures and acted on them. That works because
the client is stdlib-only and uses explicit-width ctypes rather than
`ctypes.wintypes` — reproducing on the Pi is a first move, not a last resort.

**49. `throttled` READS 0 IN THE LOW BITS WHILE THE BOARD IS BROWNING OUT
REPEATEDLY.** `throttled=0x50000` is bits 16 and 18 — *under-voltage HAS
occurred*, *throttling HAS occurred*. The low nibble says only whether it is
happening in the instant you looked, and each event lasts 2–6 s, so a spot check
almost always reads clean. **Nine events in one boot, six inside ten minutes**,
found only by `journalctl -k | grep -i undervolt`. Check the sticky bits and the
kernel log, never the live flags.

Measured rail: **min 4.833 V**, trip at ~4.63 V, so ~0.2 V of margin — and 2 Hz
`vcgencmd` sampling cannot see the transients the firmware actually trips on, so
the real minimum is lower. Full numbers in `docs/HARDWARE.md`.

**And check the fan by its TACHOMETER, not its requested state.**
`cooling_device*/cur_state` is what was asked for; `hwmon*/fan1_input` is what is
happening — 5341 RPM here, against a `cur_state` of 2. Same
observation-beats-assertion rule the panel is built on. The fan was fine; the
question "why is it hot" was pointing at the wrong component, and 60.6 °C at
cooling level 2 of 4 is the designed response to trip points of 50/60/68/75 °C.

**47. A TOOL THAT WAITS FOR THE USER MUST NOT BE IN A FIRE-AND-FORGET LANE.**
`clarify` asks a question and blocks on the answer. The webhook platform — which
is the voice lane — provides **no clarify callback at all**, so the question
goes nowhere and the turn sits there. `agent.clarify_timeout` defaults to
**3600 s**, so "sits there" means an hour. Observed as
`⏳ Working — 39 min — iteration 1/500, clarify`, repeatedly.

The give-away is the tool name in the status line, and it is worth reading:
`iteration 1/500` says the agent never got past its FIRST step, which is not
what a slow model looks like. Removed from `platform_toolsets.webhook`;
`agent.clarify_timeout: 240` caps it everywhere else, because an hour is not a
sensible ceiling even on Discord where it CAN be answered.

Same test for anything added to that lane later: if the tool needs a human to
respond before it returns, the voice lane cannot host it.

**48. HERMES SHIPS A SPOTIFY TOOLSET — do not write one.** Seven tools
(`spotify_search`, `_playback`, `_queue`, `_playlists`, `_library`, `_albums`,
`_devices`) at `~/.hermes/hermes-agent/plugins/spotify/`, PKCE OAuth, tokens in
`~/.hermes/auth.json`, refreshed on 401. Off by default so its schemas do not
ride on every call. `hermes tools` enables it, `hermes auth spotify` logs in.
Needs a per-user developer app (Spotify forbids shipping a shared one) —
`HERMES_SPOTIFY_CLIENT_ID` is already in `~/.hermes/.env` here. **Premium is
required for playback control, and an active Spotify Connect device must
exist**, else 403 "no active device".

This is a different thing from the laptop lane and both are worth keeping: the
fast lane pauses music in ~1 s without a model, while the Web API can find a
specific song but costs a full agent turn to do it.

**46. A VENV'S `pythonw.exe` CAN BE A CONSOLE BINARY, AND THAT ONE FACT CAUSED
TWO SEPARATE BUGS OVER TWO EVENINGS.** CONFIRMED on this laptop — the PE
subsystem byte at optional-header offset `0x5C` reads **3** (console), not 2
(GUI), for `…\hermes-agent\venv\Scripts\pythonw.exe`. Consequences, both
measured:

* **It inherits the console of whatever started it and dies with that window.**
  `LastTaskResult` was **3221225786** = `0xC000013A` = `STATUS_CONTROL_C_EXIT`.
  A Task Scheduler-owned process has no business caring about a PowerShell
  window; this one did, because it had a console to inherit. 529 s of healthy
  running ended the instant the window closed, 110 s after the last gesture.
* **Its inherited `stdout` exists but is unwritable**, so `sys.stdout is None`
  is FALSE, the "no console" branch never runs, no log is ever written, and
  every `print()` raises from inside the event loop. That is trap 44's silent
  killer, and it is the same property.

Diagnosis order that worked, after several that did not: `LastTaskResult` is the
one value that says HOW a task's process ended, and `0xC000013A` names the cause
outright. Read it before theorising. The subsystem check is four lines of
PowerShell against the PE header and settles the rest.

`scripts/install-gesture-client.ps1` now requires an interpreter that both RUNS
and is GUI-subsystem, and finds the base interpreter via `pyvenv.cfg`'s `home =`
when the venv's own is console. **This is free because the client is
stdlib-only** — any Python 3.8+ will do, so there is no reason to be attached to
the one on PATH.

**44. NEITHER `Start-Process` NOR `explorer.exe <lnk>` DETACHES A PROCESS FROM
THE CONSOLE THAT STARTED IT.** Both were tried and both were MEASURED dying the
instant the PowerShell window closed — and not failing to work first: the Pi
logged the client connecting and delivering **7 gestures over 29 s**, then the
socket went away. `explorer.exe <file>` looks like it should reparent to the
shell, but an already-running explorer hands the request back and the process
still ends up owned by the console.

`Invoke-CimMethod -ClassName Win32_Process -MethodName Create` builds the
process from the WMI service, so its parent is `WmiPrvSE` and no console owns
it. No elevation needed. A Scheduled Task is better still, because the scheduler
owns it — but that needs admin, which is the whole reason the fallback exists.

Note what is NOT broken here: a Startup shortcut is correct for every future
logon, where the shell is the parent. Only the instance started *by the
installer* was ever at risk, which is why this looked like "autostart doesn't
work" rather than "the launch I did just now doesn't stick".

**A wrong theory this cost, worth recording because the evidence was already in
hand:** the same symptom was first blamed on a broken `pythonw.exe` — a venv
copy that supposedly would not start. A one-line smoke test returned `True`.
Existence-vs-runs is a real trap and the installer now smoke tests candidates
anyway, but it was not this bug, and the owner's measurement said so before the
theory did.

**45. AN AUTOSTART YOU CANNOT REMOVE PLUS ONE YOU ARE ADDING IS TWO.** Removing
a Scheduled Task needs the same elevation registering one does, so a
non-elevated re-install against a leftover task can only make things worse: the
task starts a client at logon, the new shortcut starts a second, the Pi reports
`viewers=2` and every key is pressed twice. The installer used to warn and carry
on — it now refuses and prints the elevated command, because "exactly one
autostart" is the one promise it makes.

**43. `Start-Process` from PowerShell makes the client a CHILD of that window.**
The whole point of the laptop client is outliving the window it was started
from, and starting it any other way tests a launch path that will never be used
again. Launch the Startup shortcut through `explorer.exe` instead: the process
is reparented to the shell, and it is then started exactly as it will be at
every future logon — same shortcut, same launcher.

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
python3 tests/test_gestures.py          # a held gesture fires once; limits cannot wedge
python3 tests/test_voice.py             # no transcript in state; limits cannot wedge
python3 tests/test_fastlane.py          # the television cannot pause your music
```

`pytest` is NOT installed system-wide — every test module runs standalone via
`__main__` and imports pytest defensively.

---

## Measured performance (do not regress)

```
CURRENT — HDMI, 800x480 RGB565, vc4 KMS fbdev
display  1.10% CPU · 77 MB RSS      gateway  0.8% CPU · 163 MB RSS
camera (gesture subscriber attached)  71.9% of one core
```

**There is no display bandwidth budget any more.** A full 800x480 frame is
768,000 B memcpy'd into memory the display controller scans out by itself.
Against DRAM bandwidth that is nothing, and there is no error counter, no
timeout counter, and no transmit ceiling to watch. Display cost went from
0.73%/52 MB to **1.10%/77 MB** for 2.4x the pixels.

**Superseded, for context on why the code looks the way it does:** on SPI at
32 MHz the bus carried 2,562,838 B/s; one 480x232 frame cost 246,499 B
*transmitted* (not the 222,720 B of pixels — fbtft's deferred IO was PAGE
granular, so ~25 extra rows went out), giving a hard 10.37 fps ceiling and a
design that budgeted dirty ROWS. Idle still blitted 2.2 MB/s permanently, which
was documented here as waste with an unimplemented fix (D-3, row-span dirty
detection). **D-3 is now moot** — the thing it was saving no longer costs
anything.

Zone dirty-hashing and the throttled chrome rasterising (trap 11) are still
worth keeping: those were always CPU savings, not bus savings, and CPU is still
real. The 6.36% → 0.73% improvement measured there stands.

---

## Current task — latency; the fast lane is the answer

**The complaint was that it felt sluggish, and the measurement said the model
was six of the ten seconds.** That part is ChatGPT serving time and is not a
setting. So the fix was to stop sending fixed commands to a model at all —
`voice/fastlane.py`, which matches a closed set of phrases and publishes a named
intent straight to the laptop. **"pause the music" went from ~9.5 s to 900 ms.**

Read `docs/VOICE.md` before touching any of it. The load-bearing parts:

- **Two-stage STT.** `tiny.en` reads first (888 ms) and `base.en` only on a
  miss (1748 ms). MEASURED identical on the command vocabulary — 15/18 each —
  and **Hermes only ever sees the `base.en` transcript**, so questions lose no
  accuracy. See trap 36 for why the first benchmark of this said 5/11 and was
  measuring nothing.
- **The match is the WHOLE utterance**, and every phrase is at least two words.
  Both are pinned; the substring version fires on "don't pause the music".
- **A command nobody received is not confirmed.** `/intent` reports
  `subscribers`, and zero means say so rather than say "Playing."
- **The names must match the laptop's bindings** and the failure is silent —
  trap 37.

Also settled this round: `streaming.enabled: true` so Discord shows text as it
arrives, and `cronjob`/`file`/`todo`/`session_search` out of the voice lane.
**Negative results worth not retrying:** `cpu_threads=4` does not help whisper
(1828 ms vs 1870 ms — already saturating), and the Discord gateway reconnects
are not a latency source (5 isolated events across all boots).

### Camera — DONE

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

### Gestures → the Windows laptop — BUILT (2026-08-06)

`camera/gestures.py` turns the LEVEL in `hands.json` into an **edge** and
publishes it on `/events` (SSE, tcp/8081, same token). `clients/windows/
hermes_gesture.py` subscribes and presses keys — **stdlib only, no pip install
on the laptop**. Full write-up: **`docs/GESTURES.md`**.

**Hermes is not on this path and cannot be reached from it**, so the deliberate
non-build below still stands. Load-bearing properties:

- **The laptop PULLS and owns the mapping.** The Pi cannot address it. Worst
  case from a compromised Pi is a *lie about a gesture*, which still only
  reaches the fixed action list in the laptop's own config.
- **A subscriber is a viewer** — wakes the sensor, keeps tracking alive, lights
  `CAM`. No receiving gestures from a room without being counted as watching it.
  This is why tracking stops ~8 s after the laptop client dies, which is working
  as designed and not a bug to fix on the Pi. `HERMES_CAMERA_ALWAYS_TRACK=on`
  unties it and carries a permanent `WATCH` badge as the price — see
  `docs/SECURITY.md`. Default off.
- **Deploy the client with `scripts/install-gesture-client.ps1`.** It sets the
  working directory, uses `LogonType Interactive`, and actually starts the task
   — see traps 38 and 39 for the two ways doing this by hand failed silently.
- **The vocabulary is CLOSED** — FIST OPEN POINT PEACE THUMB CALL ROCK PINCH.
  `classify()` used to name every finger pattern, so a hand in view permanently
  asserted a command and moving it fired a run of them. Anything else is now
  `None`, which the debouncer treats exactly like no hand. THREE/FOUR/PINKY
  were dropped deliberately: they are what a hand passes through while opening.
- **Landmark distances MUST be aspect-corrected.** x and y are normalised by
  width and height separately and this frame is portrait, so raw thumb-to-index
  over hand scale swings **2.6x** across rotations of the same pose (0.549 →
  1.447) and is stable to 1% corrected. PINCH turns on that ratio.
- **Debounce 3-of-5, latch until cleared, sliding limits** (0.8 s per hand,
  30/min global). Per-hand vs global is deliberate: a global min gap would drop
  half of every two-handed gesture.
- **Lag is reported, not argued about**: every event carries `latency_ms`
  (inference+queue) and `dwell_ms` (debounce hold). Measured detection cost —
  10 Hz **70.8%** of a core / 300 ms dwell, 15 Hz **96.2%** / 200 ms.
- **A rate-limited gesture is DROPPED, not deferred**, and no replay on
  reconnect. `age_s` is monotonic-derived, never a cross-machine timestamp.

### What is STILL not built, and why

**Gesture → Hermes.** A gesture reaching *the agent* is a path from "someone
waves in the room" to "a bot with a shell runs a tool", and the Discord
allowlist does not cover it at all — a camera authenticates nobody. It needs its
own security design first: an explicit, bounded, visibly indicated watch mode; a
closed vocabulary mapped to a fixed action allowlist; limits far tighter than
the laptop lane; and preferably a restricted toolset for that lane, which is
**unconfirmed to be possible** for `webhook` (`platform_toolsets.acp` is a
documented counter-example that does not narrow ACP). `deliver_only: true`
skips the agent entirely and is the safest available shape. See
`docs/SECURITY.md`. `hands.json` says so in the file itself, because that is
the file someone will find first.

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
- **Audio hardware is IN and working** (ReSpeaker 2-Mic Pi HAT, 2026-08-06).
  The old note here predicted an I2S HAT might contend with the SPI panel for
  GPIO; it does not, and the question is moot anyway now the SPI panel is gone.
  I2S uses GPIO 18-21, which never overlapped SPI0 (7-11) or the panel's
  control pins (17/24/25). **Speaker output is UNVERIFIED — nothing has been
  plugged into the HAT yet.** Both mics measured live. Nothing in this project
  uses the audio yet; voice is still not built.
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
| `docs/GESTURES.md` | edges, the `/events` wire, and the Windows client |
| `docs/VOICE.md` | wake word, STT, the narrowed webhook lane, the mic light |
| `docs/GOOGLE.md` | Gmail/Calendar, read-only scopes, the typed-consent rule |

**ESCAPE HATCH.** The panel hides the console, so if the network drops there is
no terminal and no SSH. Say **"open terminal"** after the wake word, or hold the
HAT button 3 s. Both are handled LOCALLY (`voice/local.py`, `scripts/button-watch.py`)
and never touch Hermes — the agent is a cloud call and is dead in exactly that
situation. `scripts/console-mode.sh` is the same thing by hand.
