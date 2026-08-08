# Handoff — overnight batch, 2026-08-08

All seven tasks worked. **Five are done and running. Two need you**, and one of
those is a security decision you should look at with fresh eyes.

Commit: `bd7825b`. All 10 test modules pass. Six services active. **No reboot
was done**, as asked.

---

## ⚠ Read this first: the voice lane now has a shell

You asked why voice could not do what Discord could. It was my doing — I
narrowed that lane on purpose and flagged it at the time. You have now asked
twice, so **voice has full tool parity with Discord, including `terminal`**.
Verified: a spoken request ran the terminal tool and answered in 10.8 s.

What that means, stated plainly one more time so it is your decision and not a
thing that drifted:

> **A microphone authenticates nobody.** Anything audible in that room — a
> podcast, a television, a guest, a video call on a speaker — can now reach an
> agent that has a shell as `alanmyin`, plus your camera and (once connected)
> your email.

Still in place: 3 s minimum gap, 6/minute, 60/hour, the `MIC` light, one wake
per utterance, and no transcript ever logged.

**To put it back to the restricted lane** (camera, display, memory, vision, web
— no shell), one command:

```bash
python3 - <<'EOF'
import pathlib, yaml
p = pathlib.Path.home()/".hermes/config.yaml"; c = yaml.safe_load(p.read_text())
c["platform_toolsets"]["webhook"] = c["platform_toolsets"]["_webhook_restricted_backup"]
p.write_text(yaml.safe_dump(c, sort_keys=False))
EOF
systemctl --user restart hermes-gateway
```

The previous list is kept in the config as `_webhook_restricted_backup` for
exactly this.

---

## Needs you — Gmail + Calendar (about 5 minutes)

Everything is built and loaded; only the OAuth consent is missing, and only you
can give it. The tools currently answer *"Google account is not connected yet"*
rather than erroring, so nothing is broken in the meantime.

1. https://console.cloud.google.com/ → new project (any name)
2. **APIs & Services → Library** → enable **Gmail API** *and* **Google Calendar API**
3. **OAuth consent screen** → External → fill the three required fields →
   **Audience → Test users → add your own Gmail address**
   (without this step the login is refused as "app not verified")
4. **Credentials → Create credentials → OAuth client ID → Desktop app** → download JSON
5. Put it on the Pi at `~/.config/hermes-pi/google-client-secret.json`
6. Run it — prints a link, you approve on any device, paste a code back:

```bash
cd ~/projects/hermes-pi && python3 scripts/google_auth.py
systemctl --user restart hermes-gateway
```

Then ask *"how many unread emails do I have?"*

**Scopes are read-only and Google enforces that** — it cannot send, delete or
change anything. Deliberate, given the voice lane above. Details in
`docs/GOOGLE.md`.

---

## Needs you — train your own hand signs

The pipeline is built and wired; it just has no samples yet.

```bash
cd ~/projects/hermes-pi
python3 tools/gesture_train.py --record OK --seconds 8
python3 tools/gesture_train.py --record SPOCK --seconds 8
python3 tools/gesture_train.py --check          # held-out accuracy
systemctl --user restart hermes-camera
```

Hold the pose and **move it around** — nearer, further, rotated. Position,
rotation and scale are removed by construction, so what you are actually
collecting is the range of shapes *you* make when you mean that sign.

`--check` splits results three ways. **WRONG is the number to watch, not
accuracy** — a rejected gesture costs you a repeat, a wrong one runs something
you did not ask for, and anyone in the room can trigger it.

It learns from **landmarks, not images**, and `docs/GESTURES.md` explains why
that is better here rather than a shortcut.

---

## Done and running

| | |
|---|---|
| **Circle aspect** | Captured `/dev/fb0`: rings are perfectly round in the framebuffer, so the *panel* was squashing them (800×480 sent to a 480×320 screen). Pre-compensated; measured 0.885 → **0.965** on the glass. Packs regenerated. |
| **Header overlap** | The same capture showed the clock and date colliding into a smear. They were two guessed offsets that cleared each other at the old resolution. Now stacked from measured glyph heights. |
| **MIC label** | Constant now. The *body* carries "I am hearing you" — the visual switches and the strip reads `LISTENING`, only when Hermes has nothing more important to show. |
| **Voice tool parity** | Above. Verified running `terminal`. |
| **Transcript with replies** | Every voice reply opens with 🎤 "*what it heard*". A bot cannot post *as you*, so this is the fallback you named — and it earns its place: it is how you tell a wrong answer from a misheard question. |
| **Updated + cleaned** | claude 2.1.222 → **2.1.226**. Caches cleared. `tools/bench_spi.py` deleted — it measured a bus that no longer exists, and the runbook still told people to run it. |

---

## Two things I could not test

- **The circle.** I corrected it from a framebuffer capture and arithmetic, and
  I cannot see your screen. If it now looks too *wide*, the panel is not
  480×320 — set `HERMES_PIXEL_ASPECT=1.0` and re-run `tools/render_frames.py`.
- **The speaker.** Still nothing plugged in, so TTS has never been heard.
  `~/.local/share/hermes-pi/voice-venv/bin/python -m voice --say "testing"`.

## Still outstanding from before

- **D-1**: the denied-user Discord test has never been run. It matters more now
  — that allowlist is the only thing between a stranger and the shell, and voice
  has just been given the same reach.
- **Pinch thresholds** are still my provisional numbers, not measured from your
  hand: `python3 tools/gesture_calibrate.py --collect pinch`.
