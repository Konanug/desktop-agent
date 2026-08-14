# What Hermes can do

Everything below is working now. Split by **how it reaches you**, because the
routes have very different security properties and that matters more than the
feature list.

---

## 1. Your laptop — via the gesture client

**Requires `hermes_gesture.py` running on the laptop.** If it is not, Hermes is
told nobody is listening rather than reporting success into the void.

### How it works, and why it is safe

**The Pi NAMES; the laptop DECIDES.** Hermes cannot press a key or open a URL —
it puts a *word* on a stream. `gestures.json` on the laptop decides what words
mean and ignores ones it does not know. **Nothing Hermes says can create a new
binding.**

So the worst case from a compromised Pi, or from someone talking to Hermes who
should not be, is a word that still only reaches the list you wrote yourself.

### Music (Spotify and anything else)

| Say | Does |
|---|---|
| "play" / "pause" | `play_pause` |
| "next track" / "skip" | `next_track` |
| "previous track" | `prev_track` |
| "volume up" / "down" / "mute" | volume keys |
| "open spotify" | opens the app or web player |

**No Spotify API key, no OAuth, nothing to expire.** These are the system-wide
media keys, which Spotify honours whether or not it has focus — the same keys
your keyboard sends.

### Opening things

`GMAIL` · `YOUTUBE` · `CALENDAR` · `GITHUB` · `SPOTIFY`

Opens in your default browser. `webbrowser.open`, not a shell, so a URL can
never be read as a command line. `http`/`https` only — `file://` and protocol
handlers are refused at startup.

### Windows and screen

`MAXIMIZE` (win+↑) · `MINIMIZE` (win+↓) · `DESKTOP` (win+d) · `LOCK` (win+l) ·
`SCREENSHOT` (win+shift+s) · `NEXT_DESKTOP` (ctrl+win+→)

### Adding your own

Add a line to `gestures.json` on the laptop:

```json
"HERMES DISCORD": { "type": "url", "url": "https://discord.com/app" },
"HERMES CLOSE":   { "type": "key", "keys": "alt+f4" }
```

Then just ask. Delete them all and Hermes can do nothing to the laptop, while
hand gestures keep working — **the two are separate grants** and a `HERMES`
intent deliberately does *not* match a bare gesture binding.

---

## 2. Your laptop — via hand gestures

**No Hermes involved at all.** The camera sees the shape, the Pi publishes an
edge, the laptop acts. Works with the agent offline.

| Gesture | Does |
|---|---|
| ✌️ PEACE | play / pause |
| ✋ OPEN | maximise window |
| ✊ FIST | minimise window |
| 👍 RIGHT THUMB | next track |
| 👍 LEFT THUMB | previous track |
| 🤏 PINCH | unbound (thresholds want calibrating) |

Also available: `POINT`, `CALL`, `ROCK`, plus any signs you train yourself with
`tools/gesture_train.py`.

**A camera authenticates nobody** — anyone in the room can make these. Bind
things you would be relaxed about a stranger doing.

---

## 3. Your Google account — read-only

| | |
|---|---|
| `gmail_unread` | count, senders, subjects — inbox only |
| `gmail_search` | any Gmail query, **including spam and trash** |
| `gmail_read` | the actual text of a message |
| `calendar_agenda` | upcoming events, 1–30 days |

*"How many unread emails", "what's in my spam this week", "read me the one from
the bank", "what's on tomorrow".*

**It cannot send, delete or modify anything** — the OAuth scopes are
`.readonly` and Google refuses a write server-side. If sending is ever added it
needs a typed confirmation phrase naming the recipient, and the voice lane
cannot reach it by construction. See `docs/GOOGLE.md`.

---

## 4. The room — camera and voice

- **"What can you see?"** — it looks through the camera and answers from the
  actual pixels. `camera_look` for one frame, `camera_watch` for four moments
  as a contact sheet when motion matters.
- **The camera is currently ALWAYS ON.** The panel's `CAM` light is
  consequently lit permanently — it reads the kernel's power state, so it
  cannot be on without saying so. Remove
  `~/.config/systemd/user/hermes-camera.service.d/10-always-on.conf` to go back
  to the lazy lifecycle.
- **Speaks its answers** through the ReSpeaker, and shows the full reply in
  Discord with your question quoted back.
- **A live browser view** at `:8081`, token-gated.

---

## 5. The Pi itself

Full `terminal` access, so *"how's the Pi doing"*, *"restart the camera"*,
*"what's using the disk"* all work — by voice as well as Discord.

**This is the widest grant in the system.** Anything audible in that room
reaches a shell as your user. The revert is one line, in `HANDOFF.md`.

---

## 6. Escape hatches — no agent, no internet

For when the network drops and the panel is all the screen shows:

| | |
|---|---|
| Say | "hey jarvis" → **"open terminal"** (a complete sentence, nothing else) |
| Hold | The HAT button, **3 seconds** |

Both handled locally by the voice service and a GPIO watcher. Neither touches
Hermes, because the agent is a cloud call and is dead in exactly that situation.

---

## What it deliberately cannot do

| | |
|---|---|
| Send email | read-only scopes, enforced by Google |
| Open arbitrary things on the laptop | only names you have bound |
| Run commands on the laptop | `run` actions need `allow_run: true` |
| Reach the laptop unprompted | the laptop pulls; the Pi cannot address it |
| Act on gestures without the camera on | a subscriber counts as a viewer, and lights `CAM` |
