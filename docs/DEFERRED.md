# Deferred items

Things consciously postponed, with enough context to pick up cold. Revisit **before calling the first
prototype done** unless noted otherwise.

---

## D-1 — Denied-user test (Discord allowlist)

**Deferred:** 2026-08-04 · **Revisit:** before declaring the prototype complete · **Priority: high**

### What is untested

That a Discord user **not** in `DISCORD_ALLOWED_USERS` gets no response. Requires a second Discord
account or a willing friend, neither available at the time.

### Why it matters more than a normal test

The allowlist is the *only* control between Discord and the `terminal` tool. Anyone who gets past it can
run shell commands as `alanmyin`. Every other security measure on this box assumes the allowlist holds.

### What we do know

Positive path is proven: the session record for the working message carried
`user_id: 1161165901995987084`, matching the single allowlist entry — so the allowlist is genuinely what
admitted that message, not an open door. Hermes' docs also state the gateway denies all users by default
when neither `DISCORD_ALLOWED_USERS` nor `DISCORD_ALLOWED_ROLES` is set, and we set the former
explicitly rather than relying on that default.

**But "the right person got in" is not evidence that "the wrong person is kept out."** Only the negative
test proves that.

### How to run it

1. From a second Discord account in the same server, message in `#general` (free-response is enabled
   there, so no `@mention` needed — this is the widest exposure and therefore the right thing to test).
2. Expect: **no reply**.
3. Confirm on the Pi that it was seen and refused, rather than silently missed:
   ```bash
   grep -iE 'denied|unauthorized|not allowed|allowlist' ~/.hermes/logs/gateway.log | tail
   ```
   A refusal entry is the pass condition. *Silence with no log entry is not a pass* — it could mean the
   message never arrived, which proves nothing.
4. Also worth testing DM from the non-allowlisted account, since DMs skip the mention rule entirely.

---

## D-2 — Clock is wrong for ~34s after every boot

**Deferred:** 2026-08-04 · **Revisit:** Phase 7 (display chrome) · **Priority: medium**

### The finding

This Pi has **no battery-backed RTC**. On power-up the clock resumes from the last-known time, then NTP
corrects it. Measured on the 2026-08-04 boot:

```
14:19:46  kernel boot
14:19:53  hermes-gateway starts  (logs timestamped ~00:37 — the stale clock)
14:20:20  systemd-timesyncd: Initial clock synchronization
```

**~34 seconds of confidently wrong time**, and the offset was ~13.7 hours — it resumed near the previous
shutdown, so it is plausible-looking rather than obviously broken. That is the dangerous kind of wrong.

This also explains an apparent contradiction seen while validating the reboot: `ActiveEnterTimestamp` and
gateway log entries appeared to *precede* the boot. They did not — they were written under the pre-sync
clock. The authoritative check was the process's own `lstart` (kernel-sourced, corrected):
`ps -o lstart -p <pid>`.

### Implication for the display

The idle screen shows time and date. For the first ~34s after every power-on it would display a wrong
time with full confidence — exactly the "fake state" failure the architecture is meant to avoid.

### Proposed handling (Phase 7)

- Check sync status before trusting the clock:
  `timedatectl show -p NTPSynchronized --value` → `yes`/`no`
- While unsynced, render the time dimmed with a small marker (or `--:--`) rather than a wrong time
- Re-check on the existing 30s health-probe tick; no new timer needed
- **Never** stamp anything durable with an unsynced clock

### Also worth remembering

Log timestamps from the first ~34s of any boot are unreliable. When correlating boot-time events, trust
`ps -o lstart` or `journalctl -b` ordering over wall-clock strings.

---

## D-3 — Idle panel saturates the SPI bus (measured 2026-08-05) — **CLOSED, MOOT 2026-08-06**

> The SPI panel was replaced by an 800x480 HDMI screen. There is no bus to
> saturate: the framebuffer is memory the display controller scans out on its
> own, so an idle repaint costs a memcpy and nothing else. The proposed fix
> (row-span dirty detection in `player.py`) was never implemented and no longer
> needs to be. Kept below for the reasoning, which was sound.


**Status: known, unfixed, not urgent.** 0 errors, 0 timeouts, thermals fine — waste, not a fault.

`CLAUDE.md` long claimed "idle panel writes zero SPI bytes". True in Phase 6, when the body was a
static text screen and zone dirty-hashing meant an unchanged panel pushed nothing. Animation packs
made it false and the claim was not updated until now.

`player.due()` returns a new frame every frame period in *every* state, idle included, and each one
is blitted in full. At 9 fps that is **2,221,200 B/s sustained, forever, to display IDLE**.

### Fix, if wanted

Row-span dirty detection in `player.py`: keep the last blitted frame, compare row-wise, blit only the
changed span.

- fbtft is row-granular, so a narrower span is a genuine proportional saving.
- The comparison is ~223 KB of numpy, well under 1 ms, against up to ~110 ms of SPI it can avoid.
- Biggest win on `idle` and `offline`, which are mostly static; near zero on `thinking`, where the
  visual touches most rows anyway. That is the right shape — cost should track how much is moving.

### Caveat that constrains any future frame source

This only pays if frames are *clean*. Any source that puts per-pixel noise on the black background
(JPEG-captured video, for instance) makes every row differ every frame and defeats the optimisation
permanently. Prefer lossless frame sources.

---

## D-4 — Camera: RESOLVED for stills and motion, gestures still deferred (2026-08-05)

Hermes can see. `camera_look` and `camera_watch` hand the model real pixels; the `hermes-camera`
service owns the sensor exclusively; the panel shows a kernel-driven CAM indicator and the frame
that was sent. Measured facts in `docs/CAMERA.md`, protocol in `docs/CAMERA-CONTRACT.md`, threat
model updated in `docs/SECURITY.md`.

**Still deferred: gesture triggers.** A gesture is a path from "someone waves in the room" to "the
agent runs a tool", and the Discord allowlist does not cover it. Needs an explicit bounded watch
mode, a closed gesture vocabulary mapped to a fixed action allowlist, and preferably a restricted
toolset for that lane. Also needs a hand model: **do not install mediapipe** (pip-only, no apt
package, uncertain Python 3.13/aarch64 wheel, PEP 668 environment). `python3-onnxruntime` is in apt
and a small ONNX classifier converted on the laptop is the maintainable route.

**Also open:** a continuous panel preview would be bus-limited (10.37 fps ceiling, display already at
86.7%) and would replace the animation rather than run beside it. Not attempted; the 8-second
still-preview after each capture is cheaper and answers the privacy question better.
