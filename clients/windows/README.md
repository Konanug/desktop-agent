# hermes-gesture — Windows 11 client

Presses keys on this laptop when the Raspberry Pi sees a hand gesture.

**Python 3.8+ and nothing else.** No `pip install`, no admin rights. Input goes
through `SendInput` via `ctypes` — the same API the OS's own accessibility
tooling uses.

Full design, the wire protocol, and the security reasoning: `docs/GESTURES.md`
in this repo.

---

## Setup

**1. Get the token from the Pi**

```bash
ssh alanmyin@192.168.2.56 cat ~/.config/hermes-pi/camera-stream.token
```

**2. Copy the config and edit it**

```powershell
copy gestures.example.json gestures.json
notepad gestures.json
```

Set `url` (`http://192.168.2.56:8081`) and paste the `token`.

**3. Dry run — always do this first**

```powershell
python hermes_gesture.py --config gestures.json --dry-run
```

It connects, prints `connected cursor=… epoch=…`, and from then on prints every
gesture the Pi sees and exactly which key it *would* press. Nothing is pressed.
Wave at the camera and watch.

**4. For real**

```powershell
python hermes_gesture.py --config gestures.json
```

Ctrl-C to stop. Re-run `--dry-run` every time you change bindings.

---

## Gesture and key names

Bindings match `"<HAND> <GESTURE>"` first, then `"<GESTURE>"` alone.

| | |
|---|---|
| Hands | `LEFT` `RIGHT` `?` |
| Gestures | `FIST` `OPEN` `POINT` `PEACE` `THUMB` `PINKY` `CALL` `THREE` `FOUR` `ROCK`, or `"N UP"` for an unnamed finger pattern (e.g. `"2 UP"`) |
| Media keys | `play_pause` `next_track` `prev_track` `stop_media` `volume_up` `volume_down` `volume_mute` |
| Modifiers | `ctrl` `shift` `alt` `win` |
| Navigation | `tab` `esc` `space` `enter` `backspace` `delete` `insert` `home` `end` `pageup` `pagedown` `left` `right` `up` `down` |
| Also | `a`–`z`, `0`–`9`, `f1`–`f24` |

Combine with `+`: `"ctrl+shift+m"`, `"win+d"`, `"alt+tab"`.

The table is fixed and lives in `hermes_gesture.py`. That is deliberate: the
config file is this program's security boundary, and a boundary you can widen
by typing a hex number into JSON is not one.

## Action types

```json
"ctrl+shift+m"                              shorthand for a key action
{"type": "key",  "keys": "volume_up"}
{"type": "text", "text": "hello"}           types into the FOCUSED window
{"type": "log"}                             prints only — audition a gesture
{"type": "run",  "command": ["notepad.exe"]}   needs "allow_run": true
```

Everything is validated at **startup**, so a typo fails before you go and stand
in front of the camera.

---

## Before you bind anything

**A camera authenticates nobody.** Anyone standing in front of the Pi can
trigger every binding in your config — a guest, a delivery, a face on a video
call being shown on a screen the camera can see.

The question for each binding is not "is this useful" but **"am I relaxed about
a stranger doing this?"** Pausing music is fine to hand to the room. Typing into
whatever window happens to be focused is not obviously fine. Running a command
is why `allow_run` has to be set explicitly.

The Pi keeps a persistent journal line for every gesture edge it publishes, so
there is an audit trail:

```bash
journalctl --user -u hermes-camera | grep "gesture seq="
```

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `403 forbidden` | token does not match `~/.config/hermes-pi/camera-stream.token` on the Pi |
| `404 — gestures disabled` | `HERMES_CAMERA_GESTURES=off` in the Pi's unit |
| Connects, never any events | camera muted, hand tracking not installed (`scripts/install-cv.sh`), or nothing detected — open `http://192.168.2.56:8081/?k=TOKEN` and check the LAST EDGE line |
| `SendInput sent 0/2` | a focused elevated window; Windows blocks injection into higher-integrity processes |
| Reconnects in a loop | Pi rebooting, or the camera service crash-looping — check `systemctl --user status hermes-camera` |
| Fires once then goes quiet | the gesture is latched; drop your hand to clear it, that is the design |

**Running it means the camera is on.** Subscribing wakes the Pi's sensor and
holds it awake, and the Pi's panel shows `CAM` for the whole time. Stopping the
client (Ctrl-C) lets it sleep again after ~8 s.
