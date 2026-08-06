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
| **Debounce** | A value commits only when it holds **3 of the last 5** observations (~300 ms at 10 Hz). Not a run of consecutive agreements — one mis-detected frame mid-hold would reset that forever, and at any flicker rate near the threshold it would never commit at all. |
| **Latch** | Once committed, a gesture cannot fire again until the slot commits to something else, *including* "no hand". Holding your hand up for a minute is one event, not six hundred. |
| **Sliding limits** | `MIN_GAP` 0.8 s **per hand**, `MAX_PER_MIN` 30 **globally**. |

`MIN_GAP` is per-hand and `MAX_PER_MIN` is global on purpose. A global minimum
gap would silently drop one half of any two-handed gesture, because both hands
commit on the same frame and the second arrives nought seconds after the first.

**A rate-limited gesture is DROPPED, not deferred.** It still latches, so it is
never re-emitted once the limit lifts. Firing an action a second and a half
after the hand that meant it has moved on is worse than not firing it.

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
  "url": "http://192.168.2.56:8081",
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
only — how you audition a gesture), and `run` (**refused unless
`"allow_run": true`**, and `command` must be a list, never a shell string).

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

### Two traps worth keeping

- **`ULONG_PTR` is 64-bit on x64.** Declaring `dwExtraInfo` as `DWORD` — the
  mistake in nearly every copy of this snippet online — misaligns the union and
  `SendInput` returns 0 with no error anyone thinks to check.
- **`KEYEVENTF_EXTENDEDKEY`** is needed for media keys, arrows, Win and the
  edit/nav cluster. Getting it wrong is not a crash; it is a key that silently
  does nothing in some applications and works in others.

If a focused window is elevated, UIPI blocks injection into it. The client says
so rather than failing silently.

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
