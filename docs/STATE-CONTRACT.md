# State contract

The single interface between Hermes and the display renderer.

**Producer:** the gateway hook running in-process inside Hermes (`hermes_ext/hooks/hermes-display-state/`).
**Consumer:** the display renderer (`display/`).
**Location:** `$XDG_RUNTIME_DIR/hermes-display/state.json` = `/run/user/1000/hermes-display/state.json`.

Chosen over `/run/hermes-display/` (as originally sketched) because `/run` needs root or a systemd
`RuntimeDirectory=` to create, which would have coupled the hook's ability to write to the *display*
service's lifecycle. `XDG_RUNTIME_DIR` is already tmpfs, mode `0700`, owned by uid 1000, and present for
any systemd user service — so the hook creates it itself with no privileges and no cross-service
dependency.

Both sides are versioned by `schema`. A renderer that sees an unknown major version renders a degraded
"unknown state" screen rather than guessing.

---

## Why a file and not a socket

A file is **durable across a renderer restart**. The renderer can crash, be restarted by systemd, and
immediately recover the correct state by reading one file — no reconnect handshake, no replay buffer,
no lost events. A socket stream would leave the renderer blind until the next event happened to fire,
which on an idle assistant could be hours.

It also decouples lifetimes completely: Hermes neither knows nor cares whether a renderer exists, so a
display failure cannot block or slow an agent turn.

On tmpfs there is no SD-card wear, and writes are atomic via `tmp + rename(2)` — a reader never observes
a partially written file.

---

## Schema

```jsonc
{
  "schema": 1,                       // int. Bump major on any breaking change.
  "updated_at": 1785812345.678,      // float, UNIX epoch seconds. Written on EVERY update.
                                     //   Doubles as the heartbeat — see "Liveness" below.
  "pid": 2262,                       // int. Hermes gateway PID; lets the renderer detect a restart.
  "started_at": 1785810000.0,        // float. Gateway process start; renderer shows uptime.

  "activity": "thinking",            // enum, REQUIRED. See "Activity values".
  "activity_since": 1785812345.1,    // float. When the current activity began (for dwell/max-age).

  "tool": "terminal",                // string|null. Set only when activity == "tool_use".
  "iteration": 3,                    // int|null. Agent loop iteration, from agent:step.

  "model_state": "ok",               // enum: "ok" | "error" | "unknown"
  "model_detail": null,              // string|null. SHORT reason, e.g. "auth_expired".
                                     //   Never a raw exception or anything user-supplied.

  "link": {                          // Discord connectivity, as far as Hermes can tell.
    "platform": "discord",
    "last_event_at": 1785812340.0    // float|null. Last inbound gateway event of any kind.
  },

  "display": {                       // Set by the display_show_* plugin tools (Phase 8).
    "mode": "idle",                  // enum: "idle" | "image" | "text"
    "image": null,                   // string|null. Basename within images/. NEVER a path or URL.
    "text": null,                    // string|null. Short caption, <=120 chars, pre-sanitised.
    "expires_at": null               // float|null. Epoch seconds; renderer reverts at this time.
  }
}
```

### Activity values

| Value | Written on | Renderer state |
|---|---|---|
| `starting` | hook load, before gateway ready | `STARTUP` |
| `idle` | `gateway:startup`, and after `agent:end` settles | `IDLE` |
| `receiving` | `agent:start` | `RECEIVING` |
| `thinking` | first `agent:step` with no tool | `THINKING` |
| `tool_use` | `agent:step` carrying `tool_names` | `TOOL_USE` |
| `responding` | `agent:end` | `RESPONDING` |

The renderer derives `RECONNECTING`, `HERMES_OFFLINE`, `AUTH_ERROR`, and `FAILED` **itself** — Hermes
cannot report that it is down. Those are never written by the hook.

---

## Liveness — the rule that keeps the panel honest

The renderer trusts this file for *what Hermes is doing*, never for *whether Hermes is alive*.

Two independent signals, and **observation beats assertion**:

| Signal | Source | Answers |
|---|---|---|
| `updated_at` age | this file | is state fresh? |
| `systemctl --user is-active hermes-gateway` | systemd | is the process actually running? |

Resolution order, evaluated top-down:

1. unit `failed` → `FAILED` *(regardless of file contents)*
2. unit not `active` → `HERMES_OFFLINE`
3. `now - updated_at > T2` (default 90 s) → `HERMES_OFFLINE`
4. `now - updated_at > T1` (default 30 s) → `RECONNECTING`
5. `model_state == "error"` → `AUTH_ERROR`
6. `activity != "idle"` and `now - activity_since > 120 s` → `STALLED`
7. otherwise → the state mapped from `activity`

Step 3 is why `updated_at` must be rewritten on a timer even when nothing changes: a gateway that is
alive but wedged looks identical to a healthy idle one unless the heartbeat stops. The hook therefore
refreshes the file periodically, not only on events.

Steps 1–2 are what make a stale file harmless. If Hermes is gone, no contents of this file can make the
panel claim otherwise.

---

## Privacy rules — non-negotiable

This file holds **state, never content**.

Forbidden: message bodies, agent responses, usernames, display names, user IDs, channel names, tool
arguments, file paths from tool calls, error tracebacks, tokens.

Permitted: enum values, timestamps, integer counters, the tool *name*, and a short pre-sanitised caption
the plugin tool itself generated.

Rationale: this file is world-readable to anything running as this user and is trivially the sort of
thing that ends up pasted into a bug report. Hermes redacts its own logs; we avoid having anything worth
redacting. `model_detail` is a fixed vocabulary (`auth_expired`, `quota_exhausted`, `provider_error`,
`unknown`) rather than free text, so a provider error message can never leak through it.

---

## Write discipline (producer)

- **Atomic:** write `state.json.tmp.<pid>` in the same directory, `os.replace()` onto `state.json`.
  Same filesystem, so the rename is atomic and a reader never sees a torn file.
- **Never raise:** every handler body is wrapped. A display bug must not break Discord. Hermes catches
  hook exceptions, but we do not rely on that as the only guard.
- **Never block:** no network, no locks, no fsync. A write is a few hundred bytes to tmpfs.
- **Full-document writes only:** read-modify-write of the previous document in memory, then replace
  wholesale. No partial updates, so a reader always gets a self-consistent snapshot.

## Read discipline (consumer)

- Tolerate absence: before Hermes' first write the file does not exist → `STARTUP`.
- Tolerate garbage: any parse failure, missing key, or unknown `schema` major → degraded state, logged
  once, never a crash loop.
- Watch with inotify on the **directory** (not the file) — `rename` replaces the inode, so a watch on
  the file descriptor stops firing after the first update. Poll at 1 s as a fallback.
- Re-read on every `IN_MOVED_TO`, and additionally on the health-probe tick so a missed inotify event
  self-heals within 30 s.
