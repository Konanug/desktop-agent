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
│   └── plugins/hermes_display/       display_show_image/_text/_clear — TRUST BOUNDARY
├── tools/
│   ├── render_frames.py  generates the visual → assets/anim/*.pack
│   ├── bench_spi.py      measures REAL SPI throughput
│   └── camera_probe.py   verifies the camera is LIVE and measures its rate
├── tests/                3 modules, all runnable as plain python3
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
| Services | `hermes-gateway`, `hermes-display` (user) · `hermes-fbcon-detach` (system) |
| Runtime state | `/run/user/1000/hermes-display/{state.json,request.json,images/}` |
| Network | LAN only, `192.168.2.56`. Port 22 is the ONLY network-facing socket. |

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

## Current task — NEXT: camera

**Camera Module 3 is connected, detected and verified working.** No integration
is written. `docs/CAMERA.md` has the measured ground truth: sensor modes,
30 fps delivered at every capture size, CPU cost, and the gotchas.

The number that constrains the design: the panel ceiling is **10.37 fps** and
the display already uses 86.7% of the bus at 9 fps. A camera preview is
**bus-limited, not camera-limited**, and would REPLACE the animation rather
than run alongside it. Decide that before writing code.

Also unanswered, and listed in `docs/CAMERA.md`: who owns the sensor (it is
exclusive — display service, gateway, or a Hermes plugin, not two), and the
privacy question. `docs/SECURITY.md` already treats the bot token as shell
access; a camera means anyone who reaches the bot can see the room. That
belongs in the threat model BEFORE the feature.

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
| `docs/CAMERA.md` | Camera Module 3: verified facts, measured rates, gotchas |
