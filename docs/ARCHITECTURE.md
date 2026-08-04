# Architecture

Two supervised processes and a file. That is the whole system.

```
   phone / laptop ──▶ DISCORD (cloud) ──┐   outbound TLS only; no inbound ports
 ════════════════════════════════════════│═══════ Raspberry Pi 5 ═══════════════
                                         ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ hermes-gateway.service            (systemd --user, Restart=always)   │
   │  Discord adapter → allowlist → AIAgent → openai-codex provider ──────┼──▶ ChatGPT
   │                                          (chatgpt.com/backend-api)   │   subscription
   │  ┌────────────────────────┐   ┌───────────────────────────────────┐  │
   │  │ HOOK hermes-display-   │   │ PLUGIN hermes_display             │  │
   │  │ state  (in-process)    │   │  display_show_image / _text /     │  │
   │  │ agent:start/step/end   │   │  _clear   ← the TRUST BOUNDARY    │  │
   │  └────────┬───────────────┘   └───────────────┬───────────────────┘  │
   └───────────┼───────────────────────────────────┼──────────────────────┘
               │ state.json                        │ request.json + images/*.rgb565
               ▼                                   ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ /run/user/1000/hermes-display/     tmpfs, 0700, no SD-card wear      │
   └───────────────────────────┬──────────────────────────────────────────┘
                               │ polled at 30 Hz (a stat is microseconds)
   ┌───────────────────────────▼──────────────────────────────────────────┐
   │ hermes-display.service            (systemd --user, Restart=always)   │
   │  watcher → states → { chrome via Pillow | animation via mmap'd pack }│
   │            ▲                                    │                    │
   │  health probe: systemctl is-active,             ▼                    │
   │                NTP sync            RGB565 memcpy → mmap /dev/fb0     │
   └──────────────────────────────────────┬───────────────────────────────┘
                                          ▼
                        ILI9486 3.5" SPI TFT · 480×320 · 32 MHz
```

---

## The one rule

**The panel never invents state.** Every pixel traces to a real Hermes event or
to something the renderer observed itself. There is no free-running "thinking"
animation.

And when the two disagree, **observation wins**. `state.json` is an assertion by
a process that may be dead or wedged; `systemctl is-active` is a fact. The
resolution order in `display/states.py` puts the unit's real state above
anything the file claims, which is why a stale file can never make the panel
report health. `tests/test_states.py` asserts exactly this.

## Why two processes

A crash in the renderer must not take Discord down, and a broken gateway must
still leave a panel that can *say so*. Hence `Wants=` + `After=`, never
`Requires=` — the display starts even when the gateway is failed, because
displaying OFFLINE is its job in that moment.

Verified in both directions: `kill -9` on the renderer left Discord untouched;
a failed gateway left the renderer running and honest.

## Why a file and not a socket

It is **durable across a restart**. The renderer can die, be restarted by
systemd, and recover full state by reading one file — no reconnect handshake,
no replay buffer, no lost events. A socket would leave it blind until the next
event, which on an idle assistant could be hours.

It also decouples lifetimes completely: Hermes neither knows nor cares whether
a renderer exists, so a display bug cannot slow an agent turn.

Two files, not one: the hook rewrites `state.json` wholesale from memory, so a
second writer would clobber it. Plugin requests go in `request.json`.

## Where untrusted data stops

`display_show_image` fetches a URL that ultimately came from a Discord message.
All the dangerous work — fetching, decoding, resizing — happens **inside
Hermes**, in `hermes_ext/plugins/hermes_display/tools.py`. The renderer
receives only raw RGB565 of exactly the expected length from a directory it
controls. It never parses a container format, never touches the network, never
opens a model-chosen path.

So a malicious image cannot reach the process that owns `/dev/fb0`.

## What the hardware dictates

SPI is the bottleneck, and two measured facts shape the whole display design
(docs/HARDWARE.md):

1. **fbtft is row-granular.** A 240×240 blit transmits the full 480-wide rows —
   the same as 480×240. Width is free; only **row count** costs. So the visual
   is full-width with bounded height, and "shrink it to save bandwidth" is
   simply false here.
2. **Frame rate = bytes per frame.** 232 rows ≈ 217 KiB ≈ 11.6 fps at 32 MHz.

Hence: pre-rendered RGB565 packs (playback is a memcpy, no decode), zone-based
dirty hashing (an idle panel writes *zero* bytes), and animation confined by
height rather than width.

## Deferred, with seams already in place

Voice, camera and sensors are not built. The renderer consumes an *abstract
activity state*, not Discord events — so a future voice service becomes a
second producer writing the same contract, plus new screens and packs. No
redesign. See `docs/DEFERRED.md` and `docs/STATE-CONTRACT.md`.

**Hardware note for voice:** the Pi 5 has no analog audio out. A USB or I2S DAC
will be needed, and an I2S HAT may contend with the SPI panel for GPIO.

---

## Files that matter

| Path | Role |
|---|---|
| `display/panel.py` | **the only hardware-specific file** — swap this to change panels |
| `display/states.py` | resolution order; the "never wrong" logic |
| `display/player.py` | mmap'd pack playback |
| `hermes_ext/hooks/hermes-display-state/` | publishes agent state (in-process, never blocks) |
| `hermes_ext/plugins/hermes_display/tools.py` | the trust boundary |
| `tools/render_frames.py` | generates the visual; integer cycles only |
| `docs/STATE-CONTRACT.md` | the interface between the two processes |
| `docs/HARDWARE.md` | measured facts the design rests on |
| `docs/RUNBOOK.md` | how to operate and repair it |
| `docs/DECISIONS.md` | why things are the way they are |
