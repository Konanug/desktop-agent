# Decisions

Running log of choices made during the build, and why. Newest last.

---

## D1 — Delete `~/.bash_profile` rather than rewrite it

**Phase:** 1
**Date:** 2026-08-03

### Problem

`LCD-show` created a **root-owned** `~/.bash_profile` containing only:

```sh
export FRAMEBUFFER=/dev/fb1
startx  2> /tmp/log_output.txt
```

A bash login shell reads `~/.bash_profile` and then **stops** — it never falls through to `~/.profile`.
Stock Debian puts `~/.local/bin` on `PATH` from `~/.profile:25-27`, and `~/.bashrc` has no `PATH` block
to compensate. So this one file caused three unrelated-looking symptoms at once:

1. `claude` reported "command not found" despite being installed and working
2. `startx` fired and failed on **every SSH login** (`parse_vt_settings: Cannot open /dev/tty0`)
3. `FRAMEBUFFER` pointed at `/dev/fb1`, which does not exist — the panel is `/dev/fb0`

`~/.profile` documents this trap itself, at lines 2-3:
*"This file is not read by bash(1), if ~/.bash_profile or ~/.bash_login exists."*

### Decision

**Delete the file** instead of replacing it with a shim that sources `~/.profile`.

### Why

- It is a **foreign artifact**, not part of the image. `.profile` and `.bashrc` are dated Jun 17
  (image creation); `.bash_profile` is dated Aug 3 19:55 — the same minute LCD-show ran.
- Deleting restores **stock Debian behaviour exactly**: bash finds no `.bash_profile` and no
  `.bash_login`, so it reads `.profile`, which sources `.bashrc` and fixes `PATH`. A shim would
  reimplement what `.profile` already does correctly, and risk double-sourcing.
- Neither line had value: the export names a nonexistent device, and `startx` is precisely the
  autostart we want gone for an appliance.

### Impact beyond the obvious

The Hermes installer places its binary at **`~/.local/bin/hermes`** — the exact directory that was
being dropped from `PATH`. Left unfixed, Phase 2 would have looked like a broken Hermes install. This
is why Phase 1 is a hard prerequisite for Phase 2, not cosmetic cleanup.

### Verification

```
$ bash -l -c 'command -v claude && claude --version'
/home/alanmyin/.local/bin/claude
2.1.221 (Claude Code)
```

`PATH` now leads with `/home/alanmyin/.local/bin`, and a login shell produces no output.

### Rollback

`cp backup/.bash_profile.orig ~/.bash_profile` (byte-identical copy verified before deletion).

---

## D2 — Stop the leftover X session; keep Xorg and LXDE installed

**Phase:** 1
**Date:** 2026-08-03

An Xorg session started by the old `.bash_profile` was still running from boot and held `/dev/fb0`.
Its autostart is now gone, so it was stopped rather than left occupying the panel.

**Packages are deliberately left installed.** Removing the autostart is fully reversible — `startx`
from a real console still brings the desktop up if it is ever needed for diagnosis — whereas
uninstalling would not be. The appliance simply never starts it.

Confirmed after stopping: nothing holds `/dev/fb0`, and an unprivileged process (euid 1000, via the
`video` group) can mmap and write the full 307,200-byte frame. No root is required for the renderer.

Also confirmed: `stride = 960` = `480 × 2` exactly, so there is **no row padding**. The renderer can
treat the framebuffer as one contiguous RGB565 block instead of copying row by row.

---

## D3 — Power supply replaced; under-voltage resolved

**Phase:** 1
**Date:** 2026-08-03

The initial baseline showed `vcgencmd get_throttled = 0x50000` — bit 16 (under-voltage **has occurred**)
and bit 18 (throttling **has occurred**) since boot. Both are sticky since-boot flags, so they cannot be
cleared without a reboot and they persist even when the current state is healthy.

The power supply was replaced. After the resulting reboot: **`throttled = 0x0`** — no under-voltage, no
throttling, no sticky history. Confirmed resolved rather than merely quiet.

This matters for an always-on appliance: under-voltage on a Pi 5 causes silent CPU throttling and, more
insidiously, SD-card corruption over time. Worth re-checking `get_throttled` whenever the panel or any
other GPIO-powered peripheral is added, since those draw from the same rail.

**Bonus validation:** the same reboot proved the Phase 1 appliance behaviour for free — no Xorg running
after boot, nothing holding `/dev/fb0`, `.bash_profile` still absent, `claude` resolving on a login
shell. That was the planned Phase 1 reboot test, obtained without a deliberate one.

---

## D4 — Terminal toolset stays enabled

**Phase:** 2
**Date:** 2026-08-03

Hermes' `terminal` tool executes shell commands as `alanmyin`. **Enabled**, with security resting on:

1. a strict single-user Discord allowlist (`DISCORD_ALLOWED_USERS`), which Hermes enforces before
   dispatch — and which denies everyone by default if unset
2. the Codex sandbox profile left at Hermes' default `:workspace`, never `:danger-no-sandbox`
3. no inbound network ports, so the only reachable path is Discord itself

**Why not disable it:** the assistant is meant to be able to answer "how's the Pi doing?" — check disk,
tail a log, restart a service. Removing terminal access removes most of what makes it useful as a
resident assistant rather than a chatbot.

**Residual risk, stated plainly:** anyone who can message the bot can run commands as this user. The
allowlist is therefore the single most security-critical setting in the system, and bot-token compromise
is equivalent to shell access. A `pre_tool_call` denylist hook was considered and deferred — it guards
against the agent misfiring, not against an intruder, and costs a subprocess per tool call. Revisit if
the agent ever surprises us.

---

## D5 — Visual style: cyan glow on black; SPI overclock to be trialled

**Phase:** 6b / 7
**Date:** 2026-08-03

**Style:** classic JARVIS/arc-reactor cyan (`#00E5FF` core) with volumetric glow falloff to near-black,
concentric rings at 30–60% alpha. High contrast suits both a small panel read from a distance and an
RGB565 display, where subtle low-contrast gradients would band visibly. Glow is baked into the
pre-rendered packs, so it costs build time only — zero runtime cost.

**SPI clock:** trial **32 MHz** (up from the `tft35a` overlay default of 16 MHz), which roughly doubles
throughput — a 260×260 animation region goes from ~12 to ~24 fps. Requires benchmarking at 16 MHz first,
then a 10-minute soak at 32 MHz checking for tearing and `dmesg` SPI errors. `backup/config.txt.orig`
from Phase 0 is the rollback. 48 MHz was considered and declined: ILI9486 clones get progressively less
reliable, and intermittent corruption is exactly the failure a short soak would miss.
