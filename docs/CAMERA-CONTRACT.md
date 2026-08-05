# Camera contract

The interface between `hermes-camera` (owns the sensor) and the `hermes_camera`
Hermes plugin (asks for frames). Companion to `docs/STATE-CONTRACT.md`.

Two processes that must not import each other agree on a directory of small
files under `/run/user/1000/hermes-camera/`, mode 0700, on tmpfs.

Authority: `camera/protocol.py`. The plugin deliberately duplicates the
constants because it runs inside the Hermes process, whose import path is not
ours to extend. If they ever disagree, this document decides.

---

## Layout

```
status.json          what the service is doing. STATE, NEVER CONTENT.
requests/<id>.json   written by the plugin, atomic replace
captures/<id>.jpg    the image
captures/<id>.json   metadata — written LAST
DISABLED             transient mute (tmpfs; gone at reboot)
```

## Rules

**1. The metadata file is the completion signal.** The `.jpg` is written first
and the `.json` second. A reader that finds only the `.jpg` must wait, never
read — it may be half a frame.

**2. Requests are a directory, not a file.** Parallel tool calls are real. A
single `request.json` would have two writers racing, which is the same problem
that forced `state.json` and `request.json` apart on the display side. One file
per request, served oldest-first, removes the race rather than managing it.

**3. `status.json` carries state, never content.** Service state, heartbeat,
sensor power, motion scalar. Nothing derived from what the camera can see. Same
rule as `state.json`, for the same reason: this file is world-readable to
anything running as this user, and it is written constantly.

**4. Captures expire.** The service sweeps `captures/` after 60 s. A frame is
worthless once the turn has moved on, and leaving decoded images of the room on
tmpfs is a liability, not a cache.

**5. Both clocks are recorded.** `captured_at` (wall) and `captured_monotonic`.
The Pi has no battery-backed RTC and its wall clock is confidently wrong for
~34 s after boot, so a consumer computing an AGE must be able to notice the
wall clock having moved in a way monotonic did not.

---

## Messages

### request — plugin → service

```json
{"id": "3f9a…", "mode": "look" | "watch",
 "profile": "normal" | "fine",
 "reason": "counting fingers", "created_at": 1785954981.8}
```

`reason` is model-generated. The service sanitises it again before it reaches
the journal — never trust the far side of a contract to have done it.

### capture metadata — service → plugin

```json
{"id": "3f9a…", "kind": "look", "profile": "normal",
 "captured_at": 1785954981.9, "captured_monotonic": 91234.5,
 "w": 768, "h": 432, "bytes": 12916, "quality": 72, "motion": 0.71}
```

`kind: "watch"` adds `frames`, `span_s`, and **`backward`** — whether the
contact sheet covers the seconds *before* the request (the ring was already
warm) or *after* it (the camera was asleep). The plugin turns that flag into an
explicit sentence for the model. It is not cosmetic: it is the difference
between showing the moment being asked about and showing a different one.

### failure

```json
{"id": "3f9a…", "error": "camera unavailable"}
```

No image file is written. The plugin turns this into a **text** result that
states plainly that nothing was seen.

---

## Side channel: the panel preview

On every successful capture the service also writes
`/run/user/1000/hermes-display/images/camera-preview.rgb565` — exactly
`480 × 264 × 2 = 253440` bytes — into the *display's* spool, and the plugin
publishes a normal `request.json` pointing at it.

This deliberately reuses the display's existing trust boundary unchanged: the
renderer still accepts only a file of exactly the expected length from a
directory it controls, and still never decodes a container format.

Consequence: **`request.json` now has two writers**, `hermes_display` and
`hermes_camera`. Both run in-process in the gateway and both write whole
documents by atomic replace, so the last writer wins — which is the correct
semantics for "what should the panel show right now". Noted in
`docs/STATE-CONTRACT.md`.

---

## Sensor power is not part of this contract

The panel's camera indicator does **not** read `status.json`. It reads the
kernel's runtime-PM state for the sensor directly
(`/sys/bus/i2c/devices/*/power/runtime_status` where `name` is `imx708`).

That is deliberate. A privacy indicator driven by the thing it is supervising
is worth very little: if the camera service crashes with the sensor open, is
replaced, or simply lies, the light must still be right. `status.json` reports
`sensor_powered` too, but only so a disagreement is visible — the kernel wins.

Open file descriptors do **not** work for this and must not be reattempted:
libcamera never opens the capture node, and pipewire/wireplumber permanently
hold every node it does open, including the imx708 subdev, from boot on an idle
system. See `tools/camera_probe.py` for the measurement.
