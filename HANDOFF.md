# Handoff

Everything asked for is done. **Nothing needs your input to keep working** —
the items at the bottom are optional or need your hands.

---

## ⚠ One thing to know before you next get stuck

**The way you escaped last time destroyed a unit file.** `systemctl mask` on
`hermes-fbcon-detach` wrote a `/dev/null` symlink *over* the real file at the
same path, so unmasking it deleted the unit. It came back only because
`systemd/` is committed to git.

Don't do it that way again — you now have two proper escape hatches.

## Getting a terminal when you are locked out

The panel owns the whole screen and there is no login prompt behind it. If the
network drops you cannot SSH either. Both routes below are **entirely local** —
no agent, no internet — because that is exactly the situation they exist for:

| | |
|---|---|
| **Say it** | "hey jarvis" → **"open terminal"** — as a complete sentence, nothing else |
| **Hold it** | The button on the ReSpeaker HAT, **3 seconds unbroken** |

Say **"close terminal"** or hold again to bring the panel back.

The spoken phrase is matched by `voice/local.py` *before* the transcript goes
anywhere near Hermes. Routing it through the agent would have been easier and
would have been dead in the only situation that matters, since inference is a
cloud call.

Neither fires by accident: the phrase must be the **whole** utterance ("can you
open terminal for me" does nothing, and a television cannot stumble into it),
and the button needs an unbroken hold so a knock cannot accumulate.

Over SSH: `scripts/console-mode.sh on|off|status`.

## Your changes — reverted

| | |
|---|---|
| `systemd.unit=multi-user.target` in `cmdline.txt` | removed (redundant; it is already the default and it logged an override warning every boot) |
| `quiet splash` missing | restored |
| `hermes-display` disabled | re-enabled |
| `hermes-fbcon-detach` **masked** | unit restored from git, re-enabled |

Boot path is back to stock: `multi-user.target`, lingering on, all seven user
services enabled, `hermes-fbcon-detach` enabled.

## The resolution, settled with arithmetic

Both numbers you have seen are true, which is why it looked inconsistent:

- **The panel is physically 480×320.**
- **The framebuffer is 800×480**, and it has to be.

HDMI cannot carry 480×320. At 60 Hz it needs an ~11.6 MHz pixel clock and the
HDMI minimum is **25 MHz**, so the driver rejects the mode outright. 640×480 is
also short at ~23.2 MHz. **800×480 (~29 MHz) is the smallest mode that can
physically be transmitted**, and it is the only one this panel offers besides
720×480. The panel's own scaler fits it to the 480×320 glass, and the renderer
pre-compensates that non-square scaling so circles come out round.

This is now stated once, canonically, in `docs/HARDWARE.md`. The stale claim
you saw was in `README.md`, which still described the SPI panel removed weeks
ago.

## Voice replies were silent — fixed

`speak` was returning "nothing to say" on every turn. The cause was the tool
**schema**, not the handler, which is why two rounds of fixing the handler did
nothing: Hermes wants `name`/`description`/`parameters` flat, and I had wrapped
them in the OpenAI `{"type":"function", ...}` envelope. Wrapped, the tool
registers, appears, and gets called — the declared arguments are silently
dropped. Verified end to end.

## System health

| | |
|---|---|
| Power / thermal | `throttled=0x0`, 46.6 °C — clean, no undervoltage |
| Disk | 17 GB used of 115 GB (15%) |
| Memory | 2.0 GB used of 7.9 GB |
| SD card | 0 mmc errors |
| Filesystem | 0 real errors (2 journald messages, from unclean power-offs) |
| Services | all 7 user + 1 system active and enabled; **0 failed units** |
| Network | wlan0 `192.168.2.56`, internet OK |
| Sockets | 22 (ssh) and 8081 (camera, token-gated) on the LAN; 8644 loopback only |
| Tests | **11/11 modules pass** |

## Ready to publish

- `README.md` rewritten — it described the old SPI panel, said voice and camera
  were "deferred", and claimed the machine had no GPU
- `LICENSE` added (MIT — say if you want something else)
- **Secret sweep clean**: no token, key or credential in the working tree *or*
  in git history. Every pattern match is a variable name or a doc line.
- `.gitignore` hardened and **verified by planting fake secrets** and checking
  they were ignored, rather than by reading it

47 commits, 103 files, 4 MB of history.

### To create the repo

```bash
gh repo create hermes-pi --private --source=. --remote=origin --push
```

I stopped short of running that — it is outward-facing and irreversible in a
way the rest of this is not. **Start private**: `docs/SECURITY.md` describes
this machine's threat model in detail, and `docs/HARDWARE.md` carries its
hostname and MAC.

---

## Optional, needs your hands

- **Train hand signs** — `python3 tools/gesture_train.py --record OK`
- **Audition voices** — `python3 tools/tts_voices.py --audition` (currently
  `en_GB-southern_english_female-low`)
- **Calibrate pinch** — `python3 tools/gesture_calibrate.py --collect pinch`
- **Test the escape hatch for real** — say "open terminal", and try the button
- **D-1**: the denied-user Discord test has never been run. It matters more now
  that voice has terminal access.
