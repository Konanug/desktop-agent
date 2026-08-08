# Runbook

Operating Hermes Pi. Written to be usable when something is broken and you do
not remember how any of this works.

**Everything runs as `alanmyin`. Nothing needs root except the two system-level
units** (`hermes-fbcon-detach`, journald config).

---

## Is it healthy?

```bash
systemctl --user is-active hermes-gateway hermes-display
systemctl is-active hermes-fbcon-detach
```

All three should print `active`. Then:

```bash
# What Hermes thinks it is doing
python3 -c "
import json,time; d=json.load(open('/run/user/1000/hermes-display/state.json'))
print(d['activity'], '| heartbeat %.0fs ago' % (time.time()-d['updated_at']))"

# What the panel is showing, and why
journalctl --user -u hermes-display -n 20 --no-pager -o cat | grep -- '->'
```

A heartbeat older than ~15 s means the gateway is wedged even if systemd still
calls it active. That is exactly what the panel's RECONNECTING/OFFLINE states
are reporting.

---

## Reading the panel

| Panel shows | Meaning | Do |
|---|---|---|
| `IDLE` + slow cyan | healthy, waiting | nothing |
| `RECEIVING` / `THINKING` / `TOOL` / `RESPONDING` | working on a message | nothing |
| `RECONNECTING` (amber) | heartbeat 30–90 s stale | wait; if it persists, restart the gateway |
| `OFFLINE` (red) | gateway not running, or heartbeat >90 s | `systemctl --user status hermes-gateway` |
| `AUTH NEEDED` (red) | model auth failed or quota exhausted | re-auth, below |
| `FAILED` (red, still) | restart limit tripped — **needs a human** | `reset-failed`, below |
| `STALLED` (amber) | activity stuck >120 s; a lost `agent:end` | usually self-clears; restart if not |
| `--:--` for the clock | NTP has not synced yet (~34 s after boot) | wait |

`FAILED` is deliberately distinct: systemd has **given up**, and nothing will
restart the gateway until you intervene.

---

## Common tasks

### Restart something
```bash
systemctl --user restart hermes-gateway    # Discord + agent
systemctl --user restart hermes-display    # panel only; Discord unaffected
```
Restarting the display never affects Discord. Restarting the gateway makes the
panel show OFFLINE for a few seconds — that is correct behaviour, not a fault.

### Clear a `FAILED` state
```bash
systemctl --user reset-failed hermes-gateway
systemctl --user start hermes-gateway
```
Then find out *why* before walking away:
```bash
journalctl --user -u hermes-gateway -n 100 --no-pager | grep -iE 'error|traceback|fatal'
```

### Logs
```bash
journalctl --user -u hermes-gateway -f          # live
journalctl --user -u hermes-display -f
hermes logs                                     # Hermes' own view
tail -f ~/.hermes/logs/errors.log
```
Journald is persistent (200 MB cap, 1 month), so logs survive reboots.

### Re-authenticate the model (`AUTH NEEDED`)
ChatGPT OAuth tokens normally refresh themselves. When they do not:
```bash
hermes auth status openai-codex
hermes auth add openai-codex --type oauth --no-browser
```
Device-code flow: it prints a URL and a code to enter on any browser. **No SSH
tunnel required** — that is why the provider was chosen over the Codex
app-server runtime (docs/DECISIONS.md D6).

If it is quota rather than auth, nothing is broken; the Plus window has to
reset. `gpt-5.6-terra` was chosen partly to make that rarer (D7).

### Change the model
```bash
hermes config get model.default
hermes config set model.default openai-codex/gpt-5.6-sol
systemctl --user restart hermes-gateway
```
Do not hardcode a slug from memory — list what the account actually offers:
```bash
cd ~/.hermes/hermes-agent && ./venv/bin/python -c "
import json,sys; sys.path.insert(0,'.')
from hermes_cli.codex_models import get_codex_model_ids
print(get_codex_model_ids(access_token=json.load(open('$HOME/.hermes/auth.json'))
      ['credential_pool']['openai-codex'][0]['access_token']))"
```

### Rotate the Discord bot token
1. Discord Developer Portal → your app → Bot → **Reset Token**
2. `nano ~/.hermes/.env`, replace `DISCORD_BOT_TOKEN=`
3. `systemctl --user restart hermes-gateway`

Do this immediately if the token is ever exposed: **with the terminal toolset
enabled it is equivalent to shell access.**

### Update Hermes
```bash
hermes update
~/projects/hermes-pi/scripts/install-hermes-ext.sh   # re-link hook + plugin
systemctl --user daemon-reload
systemctl --user restart hermes-gateway
hermes doctor
```
Then re-check the things updates can quietly undo:
```bash
systemctl --user show hermes-gateway -p StartLimitBurst   # expect 10
hermes plugins list | grep hermes_display                 # expect enabled
sudo sshd -T | grep -E 'passwordauth|permitrootlogin'     # expect no / no
```

### Change the visual
```bash
cd ~/projects/hermes-pi
$EDITOR tools/render_frames.py          # PACKS at the bottom
python3 tools/render_frames.py --out assets/anim   # ~90 s
python3 tests/test_anim_seam.py         # MUST pass -- see below
systemctl --user restart hermes-display
```

**Every animated term must use INTEGER cycles per loop.** A float multiplier
leaves a remainder at the wrap and the rings visibly snap back a few degrees,
once per loop, forever. `test_anim_seam.py` catches exactly this.

### Watch the camera live in a browser
```bash
cd ~/projects/hermes-pi
python3 -m camera --stream-url          # prints the link, token included
```

Open it on any device on the LAN. **Opening the page is what turns the camera
on** — there is no separate switch. Close the last tab and the sensor sleeps
again 8 s later; the panel's CAM light goes out when it does, because that
light is driven by the kernel's power state and not by anything this code says.

- `/snapshot.jpg?k=...` — one current frame, for `curl` or a CV client
- `/status.json?k=...` — state, sensor power, motion, viewers, fps, `live`

The link stops working if the token file is deleted; a new one is generated on
next use, so re-run `--stream-url` to get the new link.

To turn it off: `HERMES_CAMERA_STREAM=off` in the unit, or
`HERMES_CAMERA_STREAM_BIND=127.0.0.1` to require `ssh -L 8081:127.0.0.1:8081`.
The ordinary kill switches work on it too — it wakes the sensor by the same
path everything else does.

**NO SIGNAL on the page** means frames stopped arriving, not that the page
broke. Check `systemctl --user status hermes-camera` and whether the camera is
muted (`~/.config/hermes-pi/camera.disabled`).

**The stream connects but delivers 0 frames.** Seen once. Looks exactly like a
network fault and is not one — the capture loop is wedged with the sensor open.
Check liveness, and note **which** fields to read:

```bash
curl -s "http://127.0.0.1:8081/status.json?k=$(cat ~/.config/hermes-pi/camera-stream.token)" \
  | python3 -m json.tool | grep -E "loop_idle_s|last_frame_age_s|sensor_error|state"
```

`updated_at` is **not** a heartbeat — it is written by the HTTP thread that
answers the request and stays current while the loop is dead. `loop_idle_s` and
`last_frame_age_s` are written only by the capture loop.

A watchdog now catches this after 15 s and exits so systemd restarts it, so it
should self-heal within ~20 s and leave a `[camera] WATCHDOG:` line in the
journal. If that line repeats, the restart limit (5 in 300 s) will eventually
put the unit in `FAILED` on purpose — that means it needs a human, not another
restart:

```bash
journalctl --user -u hermes-camera | grep WATCHDOG
systemctl --user reset-failed hermes-camera && systemctl --user start hermes-camera
```

### Hand tracking
Set up (idempotent, safe to re-run):
```bash
cd ~/projects/hermes-pi
./scripts/install-cv.sh              # cv-venv + hand_landmarker.task, then verifies
systemctl --user restart hermes-camera
```

It runs **only while someone is watching the stream** and stops when the last
viewer leaves. Landmarks appear on the live view; the reading is at
`/hands.json?k=...` and in `/run/user/1000/hermes-camera/hands.json`.

**No gesture reaches Hermes.** That is deliberate — see `docs/SECURITY.md`.
Gesture *edges* are published for a desktop client to act on; see below.

Turn it off with `HERMES_CAMERA_HANDS=off` in the unit. If it will not start,
the page and `status.json` carry the reason in `hands_error`; the usual causes
are the venv missing (the unit's `ExecStart` points into it) or the model not
downloaded.

The service still runs fine on `/usr/bin/python3 -m camera` without any of
this — hand tracking is the only thing that stops.

### Gesture events (→ the Windows laptop)

Full write-up in `docs/GESTURES.md`. Day to day:

```bash
K=$(cat ~/.config/hermes-pi/camera-stream.token)
curl -sN "http://127.0.0.1:8081/events?k=$K"                    # watch live
curl -s  "http://127.0.0.1:8081/events.json?k=$K" | python3 -m json.tool
journalctl --user -u hermes-camera | grep "gesture seq="        # every edge, ever
```

**Subscribing is what turns the camera on**, exactly like opening the stream
page — a subscriber counts as a viewer and the panel shows `CAM` throughout.

**Nothing fires.** In order:

1. `status.json` → `hands_tracking` true? If not it is a tracking problem, not
   a gesture one (see above).
2. `status.json` → `gestures_enabled` true? `HERMES_CAMERA_GESTURES=off` in the
   unit makes both endpoints 404.
3. `gestures_suppressed` climbing while `gestures_fired` is not? That is the
   rate limit, not detection — 0.8 s per hand, 30/min overall.
4. Open the browser page and watch its **LAST EDGE** line. It shows the same
   feed a client subscribes to, so it settles "is it the Pi or is it my client"
   without running a client.

**It fires once then goes quiet.** Correct. A gesture is latched until it
clears — drop your hand and make it again.

The client is `clients/windows/hermes_gesture.py` (stdlib only; see the README
beside it). Run it with `--dry-run` after any config change.

### Run the tests
```bash
cd ~/projects/hermes-pi
python3 tests/test_states.py         # panel cannot claim Hermes is healthy when it is not
python3 tests/test_anim_seam.py      # animation loops close exactly
python3 tests/test_display_tools.py  # hostile images and URLs are refused
python3 tests/test_camera_tools.py      # a stale frame is never shown as live
python3 tests/test_camera_indicator.py  # unknown camera state fails toward ON
python3 tests/test_usage_parse.py       # session figures never borrow the weekly line
python3 tests/test_stream.py            # the room is not served without a token
python3 tests/test_hands.py             # fingers read the same at every rotation
python3 tests/test_gestures.py          # a held gesture fires once; limits cannot wedge
```

---

## Panel problems

### Blank
```bash
systemctl --user status hermes-display
ls -l /dev/fb0 && id -nG | tr ' ' '\n' | grep -x video   # need the video group
sudo fuser -v /dev/fb0                                   # who else has it?
```

### A blinking cursor or console text on the panel
The framebuffer console has re-attached:
```bash
systemctl status hermes-fbcon-detach
sudo systemctl restart hermes-fbcon-detach
```

### Screen is blank or shows the wrong thing

The panel is an **HDMI** screen as of 2026-08-06 (it was an ILI9486 SPI TFT
before). The SPI diagnostics that used to live here are gone with it —
`tools/bench_spi.py` was deleted with the panel it measured.

```bash
# Is the screen even detected? EDID is 0 bytes on this panel -- that is normal.
for c in /sys/class/drm/card1-HDMI-A-*/; do
  echo "$(basename $c): $(cat $c/status)"; done

# Is the framebuffer the one we think it is?
cat /sys/class/graphics/fb0/name          # expect vc4drmfb
cat /sys/class/graphics/fb0/virtual_size  # expect 800,480
```

`status=disconnected` is **physical**: micro-HDMI not seated (Pi 5 uses micro,
and adapters fail constantly), wrong port (HDMI-A-1 is the one nearest USB-C),
or the panel's own USB power not connected.

**The mode is forced** in `/boot/firmware/cmdline.txt`:
`video=HDMI-A-1:800x480@60D`. It has to be — the panel returns no EDID, so
without it the kernel guesses from a generic fallback list and picked 1024x768,
which overflowed the screen. The `D` suffix forces the connector on regardless
of hotplug detect.

**Do not try to force 480x320.** vc4 rejects it: CVT gives an 11.15 MHz pixel
clock, below HDMI's 25 MHz minimum. The old Waveshare `hdmi_cvt 480 320 60 6`
recipe worked on Pi 4's firmware path and does nothing on a Pi 5.

### The renderer will not start

```
no framebuffer with driver 'vc4drmfb'; found fb0=...
```

`display/panel.py` resolves its framebuffer **by driver name**, not by index,
and refuses rather than painting into whatever sits at `fb0`. That message
means KMS did not come up — check `dtoverlay=vc4-kms-v3d` is in `config.txt`.

Override with `HERMES_PANEL_DRIVER=<name>` or `--fb fbN` if you know better.

### Everything is slow / low frame rate

No longer a bandwidth question — there is no bus. A full frame is a 768,000 B
memcpy. If the animation stutters it is CPU or the loop, not the display.
Baseline is **1.10% of one core, 77 MB RSS**.

`fps` in `assets/anim/*.json` is the playback rate of a **fixed frame count**,
so raising it makes the loop spin faster, not smoother. Smoother needs more
frames rendered (`tools/render_frames.py`), which costs disk.

---

## Audio (ReSpeaker 2-Mic Pi HAT)

WM8960 codec: control on I2C 0x1a, audio on I2S. Enabled by
`dtoverlay=wm8960-soundcard` in `config.txt`.

```bash
cat /proc/asound/cards                    # expect card 2: wm8960soundcard
speaker-test -D plughw:wm8960soundcard -c2 -t sine -f 440 -l 1
arecord -D plughw:wm8960soundcard -f S16_LE -r 16000 -c2 -d 3 /tmp/t.wav
```

**Card present but silent** is almost always the WM8960's default routing: the
DAC is not connected to the output mixer out of the box.

```bash
amixer -c wm8960soundcard sset 'Left Output Mixer PCM'  on
amixer -c wm8960soundcard sset 'Right Output Mixer PCM' on
amixer -c wm8960soundcard sset 'Headphone' 110
amixer -c wm8960soundcard sset 'Speaker'   110
sudo alsactl store                        # survives reboot via alsa-restore
```

That is already applied and stored. Speaker/headphone output has **never been
confirmed by ear** — nothing was connected when it was set up. Both mics were
verified live (peak 328/250 against silence).

`i2cdetect -y 1` shows 0x1a but `i2cget` fails — that is correct and not a
fault. The WM8960's registers are **write-only**.

### The mixer resets itself after a reboot

Symptom: routing switches survive, both volumes are back at driver defaults
(`Headphone 0%`, `Speaker 82%`) — **no sound at all from the 3.5 mm jack**.
Two separate things cause it, and both were observed:

1. **`alsa-restore` loses a race with card registration.** The saved state is
   fine — `sudo alsactl restore wm8960soundcard` applies it perfectly by hand
   — but at boot the unit ran at 4.70 s while the WM8960 codec only probed at
   3.42 s and the ALSA card binds later still. No card, nothing restored, no
   error message.
2. **WirePlumber then claims the card and applies its own volume.** It starts
   at 7.83 s and enumerates ALSA devices *asynchronously afterwards*, so it
   silently overwrote `hermes-audio.service` even though that ran later
   (8.33 s).

Both are fixed by `scripts/install-audio.sh`:

- `hermes-audio.service` applies `scripts/audio-setup.sh` — the settings as
  versioned code rather than machine state under `/var/lib/alsa`.
- `51-hermes-respeaker.conf` **excludes the card from WirePlumber entirely**.
  Ordering the unit later would just be racing a race. Excluding it is also
  the right architecture: this HAT exists for the voice pipeline, which wants
  direct predictable ALSA access to the mics rather than a session manager
  that can resample them or hand them to another client. HDMI audio is still
  managed by WirePlumber.

```bash
amixer -c wm8960soundcard sget Headphone      # expect ~87%, NOT 0%
wpctl status | grep 'Built-in Audio Stereo'   # expect NOTHING
```

Verified unattended across a reboot: 110/110 with the routing on.

### The mixer resets itself after boot

Two separate things fight over this card, and both were observed losing:

1. **`alsa-restore` loses a race with card registration.** The saved state is
   correct — `sudo alsactl restore wm8960soundcard` applies it perfectly by
   hand — but at boot the unit ran at 4.70 s while the WM8960 codec only probed
   at 3.42 s and the ALSA card binds later still. No card, nothing restored,
   no error.
2. **WirePlumber then claims the card and applies its own volume.** It starts
   at 7.83 s and enumerates ALSA devices *asynchronously afterwards*, so it
   overwrote `hermes-audio.service` (8.33 s) even though that ran later.

Symptom of either: routing switches survive, both volumes are back at driver
defaults (`Headphone 0%`, `Speaker 82%`) — **no sound at all from the 3.5 mm
jack**.

Fixed by `scripts/install-audio.sh`, which does two things:

- `hermes-audio.service` applies the mixer from `scripts/audio-setup.sh` —
  settings as versioned code, not machine state under `/var/lib/alsa`.
- `51-hermes-respeaker.conf` **excludes the card from WirePlumber entirely**.
  Ordering the unit later would just be racing a race. Excluding it is also
  the right architecture: this HAT exists for the voice pipeline, which wants
  direct predictable ALSA access to the mics rather than a session manager
  that can resample them or hand them to another client. HDMI audio is still
  managed by WirePlumber.

```bash
amixer -c wm8960soundcard sget Headphone      # expect ~87%, NOT 0%
wpctl status | grep 'Built-in Audio Stereo'   # expect NOTHING
```

Nothing in this project uses the audio yet.

---

## Replacing the panel

Only `display/panel.py` is hardware-specific. For a different SPI TFT:

1. Update `dtoverlay=` in `/boot/firmware/config.txt`, reboot
2. `python3 -c "import sys;sys.path.insert(0,'.');from display.panel import discover;print(discover())"`
3. If it is not 16 bpp, extend `pack_rgb565()`
4. Set `WIDTH`/`HEIGHT` in `tools/render_frames.py` to the new geometry, regenerate
5. Check the pixel aspect: capture `/dev/fb0` and compare a drawn circle's
   width to its height on the glass. `HERMES_PIXEL_ASPECT` in
   `tools/render_frames.py` pre-compensates a panel that scales non-uniformly

Geometry is read from sysfs at runtime, so the renderer adapts to resolution
changes on its own.

---

## Recovering from a power cut

Nothing to do. Lingering is enabled, so both user services start at boot with
no login. Verified: the gateway came up **7 s after boot** unattended.

Afterwards, sanity-check the SD card and power:
```bash
vcgencmd get_throttled     # 0x0 expected; 0x50000 means under-voltage occurred
sudo dmesg | grep -iE 'ext4|I/O error'
```

---

## If you are locked out of SSH

Password auth is **disabled**. The laptop private key is the only way in.

Recovery needs physical access: keyboard and monitor on the Pi (the console is
detached from the small panel, so use HDMI), or pull the SD card and edit
`authorized_keys` from another machine.

**Before it matters:** add a second key from another device, so one lost laptop
is not a lockout.
