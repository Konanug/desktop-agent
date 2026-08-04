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
