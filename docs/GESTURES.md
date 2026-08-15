# Gestures → the laptop

Built 2026-08-06. Hand gestures seen by the Pi drive keys on a Windows 11
laptop. Hermes is not involved and cannot be reached from this path.

```
   Pi                                              Windows 11
   ──                                              ──────────
   sensor → hands.py → gestures.py → /events  ⟵SSE⟶  hermes_gesture.py
            (level)     (edge)       tcp/8081         → SendInput
                                     token-gated      ↑ gestures.json
                                                        (the mapping)
```

---

## Why the mapping lives on the laptop

**The laptop pulls.** The Pi cannot connect to it, cannot address it, and does
not know it exists until it subscribes. Every decision about what a gesture
*means* is taken on the laptop, from a file on the laptop's disk.

So the worst a compromised or malfunctioning Pi can do is **lie about what
gesture it saw** — and a lie still only reaches the small fixed set of actions
in that config. It cannot invent a new one.

Putting the mapping on the Pi would have been less code. It would also have
made the Pi a thing that reaches into your laptop, which is a different and
much worse machine to own.

---

## The part that was actually missing: edges

`hands.json` publishes a **level** — "PEACE is showing *right now*". At
`HANDS_HZ` that is ten assertions a second for as long as you hold it, so a
client wired straight to it pauses and unpauses your music five times a second.
Polling slower does not fix it; it only makes the number of spurious actions
smaller and adds latency.

An action needs an **edge**: *this gesture just started*, delivered once.
`camera/gestures.py` does that translation, on the Pi, because it is a fact
this process is best placed to know and duplicating it into every client is how
it ends up subtly different in each and right in none.

Three mechanisms, all pinned by `tests/test_gestures.py`:

| | |
|---|---|
| **Closed vocabulary** | Only shapes in a fixed set produce an event. Everything else is `None` — the same thing, to the debouncer, as no hand at all. |
| **Debounce** | A value commits only when it holds **3 of the last 5** observations (~300 ms at 10 Hz). Not a run of consecutive agreements — one mis-detected frame mid-hold would reset that forever, and at any flicker rate near the threshold it would never commit at all. |
| **Latch** | Once committed, a gesture cannot fire again until the slot commits to something else, *including* "no hand". Holding your hand up for a minute is one event, not six hundred. |
| **Sliding limits** | `MIN_GAP` 0.8 s **per hand**, `MAX_PER_MIN` 30 **globally**. |

`MIN_GAP` is per-hand and `MAX_PER_MIN` is global on purpose. A global minimum
gap would silently drop one half of any two-handed gesture, because both hands
commit on the same frame and the second arrives nought seconds after the first.

**A rate-limited gesture is DROPPED, not deferred.** It still latches, so it is
never re-emitted once the limit lifts. Firing an action a second and a half
after the hand that meant it has moved on is worse than not firing it.

### The vocabulary has to be closed, and it was not at first

`classify()` originally fell back to `f"{n} UP"` for any finger pattern it did
not recognise, so **every hand pose resolved to some gesture**. A hand is
always in some shape, so a hand in view was permanently asserting a command,
and simply moving it fired a run of them — opening a fist passes through
`POINT`, `PEACE`, `THREE`, `FOUR` on the way to `OPEN`, and each was a nameable
gesture that a client would act on.

It now returns `None` for anything outside a fixed set, and `None` means to the
debouncer exactly what "no hand" means: nothing to fire, and clear the latch.
Transitional poses fall in the gap and are silent.

```
FIST  OPEN  POINT  PEACE  THUMB  CALL  ROCK  PINCH
```

`THREE` and `FOUR` were **removed on purpose** — they are what a hand passes
through while opening. `PINKY` too: it is what a relaxed hand does.

Narrow further with `HERMES_CAMERA_GESTURE_SET=PINCH,PEACE` in the unit. That
is not the same as binding the others to nothing on the client: gestures
narrowed away here never consume the rate limit.

An unrecognised hand still gets a caption on the live overlay (`3 UP ~`, the
tilde meaning "not a command"), because "why is nothing firing" needs an answer.
Only `gesture` — which may be `null` in `hands.json` — reaches the debouncer.

### PINCH, and why it needs the landmarks

A pinch cannot be read off five extension booleans: the index is curled enough
that the extension test can go either way. It is two ratios instead, both
against the hand's own wrist-to-knuckle length so they hold at any distance
from the camera:

| | |
|---|---|
| `pinch_ratio` | thumb tip → index tip. Small when pinched. |
| `index_reach` | index tip → wrist. Separates a PINCH from a FIST, which *also* brings those two tips together. What differs is whether the index tip is out in front of the hand or folded back against the palm. |

**Distances must be aspect-corrected, and this is not a detail.** MediaPipe
normalises x by frame width and y by frame height separately, and this camera's
frame is portrait. Measured on the twelve real fixtures, thumb-tip to index-tip
over hand scale:

| pose | raw | aspect-corrected |
|---|---|---|
| thumb_up | 0.549 – 1.447 | 0.924 – 0.932 |
| victory | 1.030 – 1.247 | 1.089 – 1.092 |
| pointing | 0.993 – 1.098 | 1.067 – 1.071 |

Raw swings **2.6×** across rotations of the *same pose*. A threshold on a raw
distance is really a threshold on how the hand happens to be turned.
`fingers_extended()` survived without the correction only because it compares
two distances in nearly the same direction, so the distortion cancels.

**The shipped thresholds are provisional** — the fixtures establish where
*not* pinching sits (0.92–1.09) and say nothing about where your pinch lands.

```bash
python3 tools/gesture_calibrate.py                  # live readout of both ratios
python3 tools/gesture_calibrate.py --collect pinch  # sample a held pose for 8s
python3 tools/gesture_calibrate.py --collect fist
```

Then set `HERMES_PINCH_MAX` and `HERMES_PINCH_MIN_REACH` in the unit. If your
pinch and your fist *overlap* on these numbers, no threshold separates them and
the answer is a different discriminator, not a cleverer cutoff.

### Where the lag is

Two independent halves, reported separately on every event because they are
reduced by completely different means:

| field | what it is | knob |
|---|---|---|
| `latency_ms` | capture → decision on the deciding frame: inference + queueing | `HERMES_CAMERA_HANDS_HZ` |
| `dwell_ms` | how long the gesture had to be held before the debounce committed | `WINDOW` / `MAJORITY` |

```bash
journalctl --user -u hermes-camera | grep "gesture seq=" | tail
# [camera] gesture seq=7 RIGHT PEACE [2] score=0.94 latency=48ms dwell=301ms
```

MEASURED cost of the detection rate, gesture subscriber attached:

| | CPU | minimum dwell |
|---|---|---|
| 10 Hz (default) | **70.8%** of one core | 300 ms |
| 15 Hz | **96.2%** of one core | 200 ms |

25.4 percentage points for 100 ms. Not obviously worth it, which is why the
default did not move — try `HERMES_CAMERA_HANDS_HZ=15` if you still want it
after the vocabulary fix.

**Before spending a core, note what the lag probably was.** With the open
vocabulary, moving your hand fired transitional gestures, and each one consumed
the 0.8 s per-hand minimum gap *and* the client's own cooldown. The gesture you
actually meant then arrived inside a window opened by a gesture you did not,
and was silently dropped — indistinguishable from lag, but caused by spurious
events rather than slow ones. Closing the vocabulary removes that source
entirely. Re-measure before tuning anything else.

Also check the client's `cooldown_s`: at 1.5 s, two deliberate gestures in
quick succession will have the second refused.

### Trap 19 wearing new clothes

`hermes_camera`'s per-turn capture cap keyed on `task_id` only ever went up, so
after N captures the camera refused **permanently** and reported its limit as
exhausted until the gateway was restarted. Both bounds here are sliding windows
over wall time and cannot reach a state they never leave.
`test_the_rate_limit_is_a_sliding_window_and_cannot_wedge` fires ten gestures
against a limit of three, then proves the eleventh works a minute later with no
restart and no reset.

---

## The wire

Both endpoints sit behind the same token as the pixels. "What are the people in
this room doing with their hands" is not meaningfully less private than a
picture of them doing it.

### `GET /events?k=TOKEN[&since=N]` — SSE, subscribe to this

```
retry: 2000

event: hello
data: {"cursor": 0, "epoch": 1786039507.04}

event: gesture
id: 7
data: {"seq":7,"at":1786039611.2,"age_s":0.048,"hand":"RIGHT",
       "gesture":"PEACE","fingers_up":2,"score":0.94,
       "bbox":[0.31,0.22,0.58,0.61],"center":[0.44,0.41]}

: beat
```

SSE rather than a websocket because it is one-directional — the room does not
take instructions — and because a client can consume it with nothing but a
socket and `readline`. That is why `hermes_gesture.py` needs no `pip install`.

- **`epoch`** is the service's `started_at`. A restart resets `seq` to 0, which
  would otherwise leave a saved cursor pointing into the future forever.
- **`: beat`** every 10 s. Gestures are rare, so without a heartbeat a laptop
  that vanished without closing the socket would hold a viewer slot — and
  therefore the sensor — until TCP eventually gave up. Writing is the only way
  to discover a dead peer.
- **Default is no replay.** Without `since`, you get future events only. A
  laptop waking up must not act on a burst of gestures made while it was
  asleep. `since=0` opts into the whole ring (64 events) explicitly.

### `GET /events.json?k=TOKEN[&since=N]` — one-shot, for curl

Returns `{"cursor", "epoch", "events": [...]}`. Defaults to the **whole ring**,
unlike `/events`, because the only reason to fetch it one-shot is to look at
what happened.

### `age_s` is monotonic-derived, and that is load-bearing

This Pi has no battery-backed RTC and is confidently wrong about the time for
~34 s after a boot (trap 6). Freshness therefore travels as `age_s`, computed
by the Pi from `time.monotonic()` at the moment of serialisation — **never** as
a timestamp two machines have to agree about. `at` is wall clock and is for
humans only.

A replayed event is genuinely older than a live one and says so.

---

## A subscriber is a viewer

Subscribing to `/events` takes a viewer slot exactly like the MJPEG stream
does. It wakes the sensor, starts hand tracking, lights the panel's CAM
indicator, and lets the camera sleep again when the last subscriber goes.

**There is deliberately no way to receive gestures from a room without also
being counted as watching it.**

### But it does not read pixels — measured

A gesture subscriber is a viewer for every purpose that matters and reads no
frames, so the service does not encode any for it. `StreamBuffer` therefore
carries two counts: `viewers` (the privacy fact, holds the sensor open) and
`pixel_viewers` (the optimisation).

| | one gesture-only subscriber, empty room |
|---|---|
| before the split (encoding 15 fps nobody read) | **77.0%** of one core |
| after | **71.9%** of one core |

**The arithmetic model predicted ~13 points and it was 5.1** — trap 23 in a new
place, and the second time in this project a plausible stage-cost model has
been wrong by more than the saving. The dominant cost is MediaPipe itself
(36 ms a pass on an empty room, 10 Hz), not the encoder. A further lever exists
— feeding the tracker a smaller long edge, since detection is size-insensitive
— and it is **unmeasured**, so it is not claimed here as a saving.

71.9% of one core, on four cores, under `CPUQuota=200%`, only while someone is
subscribed. The display is unaffected.

---

## The Windows client

`clients/windows/hermes_gesture.py`. **Python 3.8+ and nothing else** — no pip
install, no admin. Input is synthesised through `SendInput` via `ctypes`.

```
python hermes_gesture.py --config gestures.json --dry-run     # ALWAYS FIRST
python hermes_gesture.py --config gestures.json
```

`gestures.json` (copy `gestures.example.json`):

```json
{
  "url": "http://192.168.1.50:8081",
  "token": "<from ~/.config/hermes-pi/camera-stream.token on the Pi>",
  "cooldown_s": 1.5,
  "allow_run": false,
  "bindings": {
    "PEACE":       { "type": "key", "keys": "play_pause" },
    "RIGHT THUMB": { "type": "key", "keys": "next_track" },
    "FIST":        { "type": "key", "keys": "volume_mute" },
    "OPEN":        { "type": "log" }
  }
}
```

Keys match `"<HAND> <GESTURE>"` first, then `"<GESTURE>"` alone — specific wins.
Hands are `LEFT` / `RIGHT` / `?`. Gestures are `FIST OPEN POINT PEACE THUMB
PINKY CALL THREE FOUR ROCK`, or `"N UP"` when the finger pattern has no name.

Action types: `key`, `text` (types into the focused window), `log` (prints
only — how you audition a gesture), `app` and `spotify` (below), and `run`
(**refused unless `"allow_run": true`**, and `command` must be a list, never a
shell string).

### Running it unattended

```powershell
powershell -ExecutionPolicy Bypass -File .\install-gesture-client.ps1
```

Installs an autostart, **starts it**, and reports whether exactly one client is
running. That last part is the point: the hand-written version of this lost a
line twice, and the second time it was `Start-ScheduledTask`, so the task
existed and had never run — which looks exactly like the task dying.

**It needs no administrator.** A Scheduled Task is tried first, because it can
restart the client if the process dies. Registering one in the root task folder
wants elevation on many machines — seen here as `Register-ScheduledTask : Access
is denied` (`HRESULT 0x80070005`) from an ordinary PowerShell — so it falls back
to a **Startup shortcut**, which gives the client everything it actually needs:
logon start, the owner's own session, and a desktop for `SendInput`. What is
given up is restart-on-failure, and that matters less than it sounds: losing the
*connection* is the common failure and the client already retries forever with
backoff. Force either with `-Method task` (in an elevated shell) or
`-Method startup`.

**Exactly one of the two is ever installed**, and any client already running is
stopped first. Having a Startup shortcut and a Scheduled Task at the same time
has already happened here: the Pi sees `viewers=2` and every key gets pressed
twice. The stop matches on the command line rather than killing every
`pythonw` — the Python in use may be a shared one, and on this laptop it is the
Hermes agent's own venv.

Two things it gets right that are easy to miss by hand:

- **`-WorkingDirectory`.** A Scheduled Task with no "Start in" runs from
  `C:\Windows\System32`, so a relative `--config` found nothing and the client
  exited within milliseconds — before it had a console or a log to say why.
  `hermes_gesture.py` now also looks next to itself, so either half suffices.
- **`LogonType Interactive`.** `SendInput` needs a desktop. A task set to run
  whether or not the user is logged on runs in session 0, which has none, and
  every keypress fails silently while the process stays alive and connected.

Under `pythonw.exe` there is no console, and CPython's `print()` **silently
does nothing** when `sys.stdout` is `None` — so the client redirects to
`%LOCALAPPDATA%\hermes-gesture.log`. Watch it live:

```powershell
Get-Content "$env:LOCALAPPDATA\hermes-gesture.log" -Tail 30 -Wait
```

Connection durations of exactly 10 s or 20 s in that log are **not** the client
living that long. The server only notices a peer that vanished without closing
its socket when the next heartbeat write fails, so those are detection latency.

### Opening apps, and playing an album

```json
"HERMES SPOTIFY": { "type": "app",     "app": "spotify" },
"HERMES RUMOURS": { "type": "spotify",
                    "uri": "spotify:album:4aawyAB9vmqN3uQ7FjRGTy" }
```

`app` names a **key**, and this program owns the value — the list is closed and
lives in the source, not the config. `spotify` is the one place a config
supplies a URI, allowed only because the shape is pinned to `spotify:<kind>:<22
base62 chars>`: no query string, no path, no arguments, nothing to smuggle. Get
one with right-click → Share → **Copy Spotify URI**.

**It launches `Spotify.exe`, not the `spotify:` scheme.** `os.startfile` is the
correct API — it is `ShellExecute`, and it does not go near the browser — but it
can only reach the desktop app if something registered a handler, and that
registration is not ours: the Microsoft Store build registers differently from
the standalone installer, neither registers before the app has run once, and a
browser update can take the association. When it is missing Windows falls back
to the web player, which is exactly the symptom, and no amount of correctness
here fixes it. So the executable is tried first and the scheme is the fallback.
The log says which route ran.

### Testing one binding without waving at anything

```powershell
python hermes_gesture.py --config gestures.json --fire "HERMES SPOTIFY"
```

Runs that binding immediately and exits, **without connecting to the Pi** — so
it separates "the binding is broken" from "the connection is broken" in one
command instead of a gesture and a guess. If it still opens a browser, the
handler is the problem rather than the code:

```powershell
reg query HKCR\spotify\shell\open\command
```

### Things it refuses to do

- **Everything is validated at startup, not on the gesture.** A typo that only
  surfaces the first time you make the shape is a typo you find while waving at
  a camera wondering whether the camera is broken.
- **The key table is fixed.** The config is this program's security boundary,
  and a boundary you can widen by typing a hex number into a JSON file is not
  one.
- **Its own cooldown**, per binding, independent of the Pi's rate limit. The
  thing that *acts* keeps its own bounds; it does not delegate them upstream to
  the thing that *reports*.
- **Refuses events older than 3 s**, and never asks for replay on reconnect.

### Three traps worth keeping

- **`sizeof(INPUT)` is set by `MOUSEINPUT`, the union's largest member — not by
  the member you use.** Declaring only `ki` gives 32 bytes on x64 where Windows
  expects 40, and it rejects **every** call with `ERROR_INVALID_PARAMETER` (87)
  and presses nothing. **This shipped**, and `--dry-run` could not catch it,
  because dry-run never calls `SendInput` — the first real keypress was the
  first validation. Layout is now asserted at import and pinned by tests that
  were verified to fail against the broken version.
- **`ULONG_PTR` is 64-bit on x64.** Declaring `dwExtraInfo` as `DWORD` — the
  other classic version of the same bug — misaligns every field after it.
- **`KEYEVENTF_EXTENDEDKEY`** is needed for media keys, arrows, Win and the
  edit/nav cluster. Getting it wrong is not a crash; it is a key that silently
  does nothing in some applications and works in others.

The client names error 87 (its own bug) and error 5 (UIPI blocking injection
into a focused elevated window) separately. They look identical from the
outside and I read one as the other once already.

The structs use explicit-width `ctypes` types rather than `ctypes.wintypes`, so
`tests/test_gestures.py` can check their layout **on the Pi** — the machine
that cannot run the client is the one that tests it.

---

## What this does NOT do, and why the line is there

**It does not reach Hermes.** Nothing on this path can run a tool. The
deliberate non-build recorded in `CLAUDE.md` and `docs/SECURITY.md` is the path
from "someone waves in the room" to "the agent runs a tool", and that path is
still not built.

That line is not about danger in general — it is about *authentication*. **A
camera authenticates nobody.** Anyone standing in that room can make these
gestures: a guest, a delivery, a face on a video call displayed on a screen the
camera can see. The Discord allowlist does not cover any of them.

So the question for every binding is not "is this useful" but **"am I relaxed
about a stranger doing this?"** Pausing music is fine to hand to the room.
Typing into a focused window is a different proposition. Running a command is a
different proposition again, which is why the config has to say `allow_run`
out loud.

Every edge gets one journal line on the Pi — `[camera] gesture seq=N HAND
GESTURE [n] score=x` — and journald here is persistent, so there is an audit
trail for anything a subscriber does off the back of it.

### If a Hermes lane is ever built

It needs its own numbers (the plan file reserved ≥1.5 s apart and ≤6/min, far
tighter than these), its own bounded and visibly-indicated watch mode, a closed
vocabulary mapped to a fixed action allowlist, and a restricted toolset for that
lane. Whether `platform_toolsets.webhook` can actually narrow that lane is
**unconfirmed** — `platform_toolsets.acp` is a documented counter-example that
does not narrow ACP — and it must be proven before anything depends on it.

`deliver_only: true` on a webhook route is the interesting shape: it skips the
agent entirely, so a gesture could notify over Discord at zero LLM cost and
with **no ability to run a tool at all**.

---

## Operating it

```bash
# is the feed alive, and what has fired?
K=$(cat ~/.config/hermes-pi/camera-stream.token)
curl -s "http://127.0.0.1:8081/events.json?k=$K" | python3 -m json.tool

# watch edges as they happen
curl -sN "http://127.0.0.1:8081/events?k=$K"

# every edge, historically
journalctl --user -u hermes-camera | grep "gesture seq="

# counters
curl -s "http://127.0.0.1:8081/status.json?k=$K" \
  | python3 -m json.tool | grep -E "gesture|viewers"
```

`HERMES_CAMERA_GESTURES=off` in the unit disables the gate and makes both
endpoints 404. Turning off hand tracking (`HERMES_CAMERA_HANDS=off`) or the
stream (`HERMES_CAMERA_STREAM=off`) also removes this path, since it is built
on both.

**No events firing?** In order: is `hands_tracking` true in `status.json`; is
`gestures_enabled` true; is `gestures_suppressed` climbing (rate limit rather
than detection); does the browser page's LAST EDGE line update when you make a
shape. That page shows the same feed a client subscribes to, so it answers "is
it the Pi or is it my client" without running a client.

---

## Custom gestures — teach it your own signs

```bash
python3 tools/gesture_train.py --record OK --seconds 8
python3 tools/gesture_train.py --record SPOCK --seconds 8
python3 tools/gesture_train.py --check       # honest held-out accuracy
python3 tools/gesture_train.py --list
systemctl --user restart hermes-camera
```

Hold the pose and **move it around** while recording — nearer, further,
rotated, both hands, slightly sloppy. Position, rotation and distance are
removed by construction, so those variations cost nothing; what you are
collecting is the range of shapes *you* make when you mean that sign.

### Why landmarks and not training images

"Give it training images" reads as fine-tuning a vision model on photographs.
That is the wrong tool here and would be worse at the job:

- **MediaPipe has already solved the hard part.** It turns a photograph into 21
  calibrated points, trained on far more hands than anyone can photograph in a
  kitchen. Learning from raw pixels throws that away and starts again from
  lighting, skin tone, sleeve colour and background.
- **Landmarks are invariant to exactly what ruins image models.** Normalised
  into the hand's own frame, the same pose gives the same numbers at any
  distance, any rotation, in any light.
- **It stays inspectable.** A few hundred samples and a k-NN is a JSON file you
  can read, trains in under a second, and can be deleted and redone in a
  minute. A fine-tune is an opaque blob you will not retrain casually.

### What a sample is

21 landmarks, normalised so only SHAPE survives: wrist to the origin, frame
aspect corrected, rotated so wrist→middle-knuckle points up, scaled so that
vector is length 1. What is left is 42 numbers describing a hand shape and
nothing else.

The rotation is done by **projecting onto the hand's own basis**, not by an
angle and a rotation matrix. Building the basis directly is self-evidently
correct; an `atan2` plus a matrix has four sign conventions to get wrong and
looks plausible when it is. The first version here was wrong in exactly that
way — it normalised the middle knuckle to `(-0.75, -0.66)` instead of `(0, 1)`,
and `tests/test_hands.py` now asserts both invariants.

### The classifier can say "I don't know"

k-NN with a distance ceiling, needing 3 of 5 neighbours to agree. Deliberately
the simplest thing that works, for one reason that outranks accuracy: **a
softmax over N classes always names one of them.** This project has been bitten
by that once already — `classify()` used to name every finger pattern, so a
hand in view permanently asserted a command. Anything past `HERMES_CUSTOM_REJECT`
is `None`, and `None` means no gesture.

`--check` reports held-out accuracy split three ways, and **WRONG is the number
that matters** — not accuracy. A rejected gesture costs you a repeat; a wrong
one runs something you did not ask for, and anyone in the room can trigger it.

### Built-ins always win

A learned gesture that happens to resemble `FIST` does **not** shadow it —
bindings that already work would start doing something else with no visible
cause. Learned gestures fill the gap where the finger table says nothing, which
is the space they were recorded in anyway.


---

## Hermes driving the laptop

"Open Gmail" spoken to Hermes, or typed in Discord, reaches the laptop through
**the same wire the gestures use**.

```
Hermes ─► laptop_do("GMAIL") ─► POST /intent ─► /events ─► client ─► browser
          (names it)                            (same SSE stream)   (decides)
```

### The Pi names; the laptop decides

That is the whole safety argument, and it is the same one that put the gesture
mapping on the laptop rather than the Pi. **Hermes cannot open a URL, press a
key, or run anything on another machine.** It can only put a word on a stream.
If the laptop's config has no binding for that word, nothing happens, and
nothing the agent says can create one.

So the worst case from a compromised Pi — or from someone talking to Hermes who
should not be — is the same as the worst case for gestures: a word that still
only reaches the fixed list the owner wrote down themselves.

### Intents do NOT inherit gesture bindings

`HERMES PEACE` does **not** match a bare `PEACE` binding. Two different grants:
binding `PEACE` means *a hand in front of my camera may do this*. If intents
fell back to it, granting a gesture would silently grant the agent everything
you had ever bound to a hand. `tests/test_gestures.py` pins it.

```json
"HERMES GMAIL":    { "type": "url", "url": "https://mail.google.com" },
"HERMES YOUTUBE":  { "type": "url", "url": "https://youtube.com" },
"HERMES DESKTOP":  { "type": "key", "keys": "win+d" },
"HERMES LOCK":     { "type": "key", "keys": "win+l" },
"HERMES SCREENSHOT": { "type": "key", "keys": "win+shift+s" }
```

Delete them all and Hermes can do nothing to the laptop, while gestures keep
working.

### Everything else applies unchanged

An intent is an ordinary event on the existing stream, so it inherits the lot
rather than re-implementing any of it: token-gated, `age_s` freshness, no
replay on reconnect, the client's own per-binding cooldown, and the requirement
that a subscriber be attached at all. `laptop_do` says so when nobody is
listening rather than reporting success into the void.

Names are `[A-Za-z0-9_]{1,32}` — a name that could carry punctuation or spaces
would be a place to smuggle something into whatever the laptop does with it.

### The `url` action

`webbrowser.open`, not `start` — it opens the default browser without a shell,
so a URL can never be read as a command line however it is punctuated. Only
`http://` and `https://` are accepted; `file://` and protocol handlers are a
much larger thing to hand to a room, and are refused at startup.
