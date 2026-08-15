# Security posture

Audited 2026-08-04. This box holds a Discord bot token and ChatGPT OAuth tokens, and Hermes' `terminal`
toolset is enabled — so **anyone who can reach the bot or the shell can run commands as `alanmyin`.**
That single fact drives everything below.

**Exposure:** LAN only (`<pi-lan-ip>/24`, wlan0). No port forwarding, no tunnel. Corroborated by zero
failed SSH auth attempts in 7 days — an internet-facing port 22 essentially never shows that.

---

## Threat model, honestly stated

The realistic adversary is **anything already on the home network**, plus **anyone who obtains the
Discord bot token**. Remote internet attack is out of scope while the box stays un-forwarded.

Two paths to arbitrary code execution as `alanmyin`:

| Path | Control |
|---|---|
| Discord → `terminal` tool | `DISCORD_ALLOWED_USERS` allowlist (single user ID) |
| SSH → shell | SSH authentication |

**The bot token is credential-equivalent to shell access.** If it leaks, rotate it in the Discord
Developer Portal immediately — treat it exactly as you would a leaked private key.

---

## Changes applied

### 1. Root SSH login disabled
`/etc/ssh/sshd_config.d/10-hermes-pi-hardening.conf` → `PermitRootLogin no` (was `without-password`,
i.e. key-based root login was permitted). Administration is `alanmyin` + `sudo`.

**Why the `10-` prefix matters.** `sshd_config` line 12 is `Include /etc/ssh/sshd_config.d/*.conf`,
files are read in lexical order, and **the first obtained value for a keyword wins**. Since
`50-cloud-init.conf` contains `PasswordAuthentication yes`, an override must sort *before* it. A
`99-*.conf` would be **silently ignored** — configured-looking, but changing nothing. Verify any change
with `sudo sshd -T`, never by reading the file.

Config was validated with `sshd -t` *before* reload. Never reload an unvalidated sshd config remotely.

### 2. `rpcbind` disabled
Was `enabled` + `active`, listening on `0.0.0.0:111`, with **no NFS mounts and no NFS in `/etc/fstab`** —
purely unused attack surface, and a well-known DDoS amplification vector.

```
systemctl disable --now rpcbind.socket rpcbind.service
```

**Result: port 22 was then the only network-facing socket.** Everything else (VS Code server, Hermes
internals) binds to loopback only.

Reverse with `sudo systemctl enable --now rpcbind.socket` if NFS is ever needed.

> **This is no longer true, as of 2026-08-05.** The camera's live view
> (`camera/stream.py`, **tcp/8081**) binds to the LAN by default, because the
> point of it is a link that opens on the laptop. It is the second
> network-facing socket on this box and the first one that serves a view of the
> room, so it is called out here rather than left to be discovered in
> `ss -tlnp`.
>
> What stands between it and the LAN:
>
> - **A token, required by default**, compared with `hmac.compare_digest`, on
>   **every** endpoint — a gate on the page but not on `/snapshot.jpg` would be
>   no gate at all, and `tests/test_stream.py` asserts all four.
>   It lives in `~/.config/hermes-pi/camera-stream.token` (0600), beside the
>   kill switch and deliberately **not** under `~/.hermes/`.
> - **`MAX_VIEWERS = 6`**, and viewer slots are released in a `finally:`. A
>   count that only went up would pin the sensor open until the service was
>   restarted — the same shape of bug as trap 19, with a worse consequence.
> - **The kill switches still work.** The stream wakes the sensor through
>   `ensure_awake()`, so `~/.config/hermes-pi/camera.disabled` and
>   `systemctl --user stop hermes-camera` stop it like anything else.
> - **The panel still cannot be made to lie.** The CAM light reads the kernel's
>   runtime-PM state, so a viewer lights it with no cooperation from this code.
>
> What does **not** stand between it and the LAN: a token in a URL is
> bearer-shaped. It sits in browser history and in any screenshot of the
> address bar, and the connection is plain HTTP, so anyone able to observe LAN
> traffic can read both the token and the pixels. It is a lock on the door, not
> a tunnel. If that is not enough for a given moment, the honest options are
> `HERMES_CAMERA_STREAM_BIND=127.0.0.1` plus `ssh -L`, or
> `HERMES_CAMERA_STREAM=off`.
>
> **This raises D-1 again.** The denied-user Discord test is still unrun, and
> the number of paths to a view of this room has gone from one to two.

### 3. Auxiliary providers restricted
`auxiliary.free_only: true`. Logs showed the auxiliary client willing to fall back to a **paid**
OpenRouter model. No OpenRouter key exists so no spend was possible, but the guard is now explicit
rather than incidental.

### 4. Gateway restart limiting
See `systemd/hermes-gateway.service.d/10-restart-limit.conf`. Not confidentiality, but availability:
Hermes' generated unit disables systemd's start rate limiter while setting `Restart=always`.

---

### 5. SSH is key-only
Applied 2026-08-04, in this order — the order is the safety property, not a formality:

1. Keypair generated **on the laptop** (`ssh-keygen -t ed25519`); the private key never touched the Pi
2. Public key installed to `~/.ssh/authorized_keys` (`600`), `~/.ssh` (`700`)
3. **Key login verified in a new session while the old one stayed open**, confirmed in the auth log —
   not inferred from which prompt appeared:
   ```
   Accepted publickey for alanmyin from <pi-lan-ip> ED25519 SHA256:E8othIIqyxCRYT…
   Accepted publickey: 1    Accepted password: 0
   ```
4. Only then `PasswordAuthentication no`, validated with `sshd -t` before reload

Installed key: `SHA256:<redacted>` (`alanmyin-laptop`).

`KbdInteractiveAuthentication no` is set alongside it. That is not redundant: with `UsePAM yes`,
keyboard-interactive can still reach the PAM password stack even when `PasswordAuthentication no` —
leaving the box brute-forceable while *looking* locked down.

Verified by attempting a password-only login rather than trusting the config:
```
$ ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no alanmyin@127.0.0.1
alanmyin@127.0.0.1: Permission denied (publickey).
```

**Losing the laptop private key locks you out.** Recovery needs a physical keyboard and monitor on the
Pi (or pulling the SD card). If that key is ever at risk, add a second key *before* removing the first.

## Pending

Nothing outstanding.

---

## Deliberately not done

| Measure | Why not |
|---|---|
| `fail2ban` | Guards against remote brute force. LAN-only with no forwarding and 0 failed attempts in 7 days — it would be a background service earning nothing. Revisit immediately if the Pi is ever exposed. |
| `ufw` firewall | Only one port listens at all now. A firewall in front of a single intentionally-open port adds moving parts, not security. Revisit if exposed. |
| Sandboxing the `terminal` tool | Deliberate: the assistant is meant to answer "how's the Pi doing?" — check disk, tail logs, restart services. Confining it would remove most of its usefulness. The allowlist is the control. |

---

## Secrets inventory

| Path | Mode | Contents |
|---|---|---|
| `~/.hermes/.env` | `600` | `DISCORD_BOT_TOKEN` |
| `~/.hermes/auth.json` | `600` | ChatGPT OAuth access + refresh tokens |
| `~/.claude/.credentials.json` | `600` | Claude Code credentials |

**None are in this repo.** `.gitignore` covers `*.env`, `auth.json`, `*.token`, `*.key`, `*.pem`.
`backup/` is also ignored — it holds machine-specific config copies including the MAC address.

Hermes redacts secrets in its own logs (`Secret redaction: ENABLED` at gateway startup, covering tool
output, logs, and chat responses). Our `state.json` contract carries **state only, never content** —
see `docs/STATE-CONTRACT.md`.

---

## Routine checks

```bash
sudo sshd -T | grep -E 'passwordauth|permitrootlogin'   # auth posture (never trust the file)
ss -tlnp | grep -v 127.0.0.1                            # anything newly network-facing?
grep DISCORD_ALLOWED_USERS ~/.hermes/.env               # allowlist still just you?
sudo sshd -T | grep -i allowusers                       # unexpected accounts?
vcgencmd get_throttled                                  # 0x0 expected
```

Re-run after every `hermes update` — an update could reintroduce config the drop-ins were written to
neutralise.

---

## The camera (added 2026-08-05)

The threat model above said the Discord bot token is equivalent to shell access
on this Pi. With a camera attached that becomes **shell access and a view of the
room**. This section is the honest version of what that does and does not mean.

### What actually protects the room

1. **The Discord allowlist.** It is the only thing between a stranger and the
   camera. This raises the priority of **D-1** in `docs/DEFERRED.md` — the
   denied-user test is still unrun. "The right person got in" was never evidence
   the wrong person is kept out; now the wrong person can see the room.
2. **The panel indicator**, because it is driven by the kernel's runtime power
   state for the sensor rather than by anything the camera service says. A
   compromised or crashed service cannot switch the light off. It fails toward
   ON: unknown is displayed as possibly-on, never as off.
3. **Physical** — a lens cover, or unplugging the ribbon.

### What does NOT protect the room, and why

`~/.config/hermes-pi/camera.disabled`, `systemctl --user stop hermes-camera`
and `mask` are **conveniences for the owner, not security controls**.

The agent has the `terminal` tool. That is a shell as `alanmyin`. It can delete
the disable file, start the service, or open the sensor itself, because
`alanmyin` is in the `video` group. Moving the camera service to another uid
would not fix this while `alanmyin` keeps `video` — and it must, because
`/dev/fb0` is in `video` too.

So the software switches stop *accidents* and stop the model doing something
casually. They do not stop an attacker who already has the bot token. Anyone
reasoning about this should assume that if the bot is compromised, the camera is
compromised, and rely on the allowlist and the physical controls instead.

### What the design does to limit blast radius

- The sensor is **closed at rest** and opens only for an explicit capture,
  closing again after 20 s. There is no always-on feed to hijack.
- Every capture writes an audit line to the journal (persistent) with the
  model's stated reason. Never pixel data.
- The panel displays the exact frame that was sent, so anyone in the room can
  see what left it.
- `status.json` carries state, never content.
- Captures are swept from tmpfs after 60 s rather than accumulating.
- The camera tools take **no path, filename or URL** from the model — only a
  reason and a detail level.

### `HERMES_CAMERA_ALWAYS_TRACK` — a deliberate widening, off by default

Hand tracking is normally gated on an attached viewer: `apply_tracking()` starts
it when someone subscribes and stops it 8 s after the last one leaves. That gate
is not an implementation detail. **A camera that continuously works out what
people in the room are doing is a materially different thing from one that could
see them**, and tying the second to an attached viewer keeps it bounded by
something the owner can observe.

`HERMES_CAMERA_ALWAYS_TRACK=on` unties it, so a reconnecting laptop acts on the
first gesture instead of waiting for the tracker to spin up. It is off by
default and belongs in a systemd drop-in, not set in passing.

What it costs, stated plainly:

- The sensor is powered continuously (it implies `ALWAYS_ON` — tracking an
  unpowered sensor is not a thing) and ~60 ms of a core goes on detection
  forever, empty room or not.
- The honest answer to "is it watching me" becomes **yes**.

So it does not go quietly. The panel carries a separate, permanent **`WATCH`**
badge for as long as it is set — a second word rather than a shade of the `CAM`
light, because it is a different claim. `CAM` means the sensor is powered.
`WATCH` means the room is being analysed.

**There is no kernel fact underneath this one**, and that is the weak point.
`CAM` reads the sensor's runtime power state, so a crashed or dishonest camera
service cannot switch it off. "Is it running hand detection" is pure software
with nothing comparable to read, so `WATCH` trusts the service's own
`status.json`, exactly as the microphone indicator does. The fail direction
therefore does real work: unreadable renders `WATCH?`, never nothing — though
only while the sensor is powered, since claiming a demonstrably asleep camera is
analysing the room is its own kind of lie, and a badge that is always lit is one
nobody reads. `tests/test_camera_indicator.py` pins all four cases.

### Gestures → the laptop — BUILT 2026-08-06, and it does NOT cross the line

Debounced gesture **edges** are published on `/events` (SSE, tcp/8081, same
token as the pixels) and a Windows client acts on them. `docs/GESTURES.md`.

**Hermes is not on this path and cannot be reached from it.** Nothing here can
run a tool. What was deliberately not built — and still is not — is the path
from "someone waves in the room" to "*the agent* runs a tool".

Properties that keep it on the right side of that line:

- **The Pi publishes; the laptop decides.** The laptop pulls over the LAN. The
  Pi cannot connect to it, address it, or know it exists until it subscribes.
  The gesture→action mapping is a file on the laptop's disk. The worst a
  compromised Pi can do is **lie about what gesture it saw**, and a lie still
  only reaches the fixed set of actions in that config.
- **A subscriber is a viewer.** It holds the sensor open, keeps tracking
  running and lights the panel's `CAM` indicator. There is no way to receive
  gestures from the room without also being counted as watching it.
- **One journal line per edge**, `[camera] gesture seq=…`, and journald here is
  persistent — an audit trail for anything a subscriber does off the back of it.
- **The client's key table is fixed**, and `run` actions are refused unless the
  config sets `allow_run: true`.
- **No replay.** A client gets future events only unless it asks for history
  explicitly, so a laptop waking from sleep cannot fire a burst of actions from
  gestures made an hour ago.

**The residual risk, stated plainly: a camera authenticates nobody.** Anyone
physically present can trigger any binding — a guest, a delivery, a face on a
video call shown on a screen the camera can see. The Discord allowlist covers
none of them. That is survivable here only because the action vocabulary is the
owner's own choice on the owner's own machine, and the defaults are media keys.
It would not be survivable pointed at a shell.

### Voice → Hermes — BUILT 2026-08-07, with the lane narrowed first

"hey jarvis" → transcript → an agent **stripped of `terminal` and
`code_execution`**. Full write-up: `docs/VOICE.md`.

This crosses a line the gesture work deliberately did not, and it was only
crossed after proving the mitigation exists. The narrowing was verified against
the installed source *before* any voice code was written, in both directions —
the control case (explicitly adding `terminal` back) confirms the resolver is
consulted rather than ignored, which is what the ACP counter-example in Hermes'
own docs made worth checking.

- **The listener is LOOPBACK ONLY** (`127.0.0.1:8644`). This box still has two
  network-facing sockets: 22 and 8081. HMAC-SHA256 with a timestamp-bound V2
  signature is defence in depth against something else already on the machine,
  not the thing keeping strangers out — the bind is.
- **Sliding rate limits**: 3 s gap, 6/min, 60/hour. A television talking to
  itself reaches the hourly cap and stops.
- **The panel shows `MIC`**, and `MIC ((` while actually capturing. Unknown
  fails toward ON.
- **No transcript is ever logged or published.** Journald gets length and
  timing; `status.json` carries state, never content.
  `HERMES_VOICE_LOG_TRANSCRIPT=on` breaks that on purpose, to let you read what
  whisper actually heard while adding a fast-lane phrase. It is off by default,
  it logs only utterances that matched nothing, and it should be turned back off
  — journald here is persistent.

**The voice fast lane does not widen this** (`voice/fastlane.py`, added
2026-08-14). Some spoken commands now skip the agent entirely and publish a
named intent to the laptop instead. That is strictly *less* reach, not more:

- The blast radius is a **gesture's**, not the agent's. It puts a NAME on the
  `/intent` stream; the laptop pulls it, looks it up in a config on its own
  disk, and ignores anything it does not know. Nothing said in this room can
  create a binding, and no tool runs on the Pi.
- The vocabulary is **closed and in the source**, and the match is the WHOLE
  utterance — the same rule as the terminal escape hatch, for the same reason
  that a microphone hears whatever the television says. A substring test fires
  on "don't pause the music"; `tests/test_fastlane.py` was verified to fail
  against one.
- Owner-added phrases (`~/.config/hermes-pi/voice-commands.json`) supply a
  *name*, checked against `[A-Za-z0-9_]{1,32}` before it is sent, and cannot
  smuggle punctuation or length past the endpoint.
- It runs **before** the rate limit, deliberately. These commands cost the agent
  nothing, so rationing them against the agent's budget would be arbitrary — and
  the escape hatch must not be refused for being asked too often.

The residual risk is unchanged in kind and smaller in degree: a television that
says "pause the music" as a complete sentence can pause your music.

**The residual risk, plainly: a microphone authenticates nobody.** Anything
audible — a podcast, a guest, a video call on a speaker — can start a
conversation with an agent that has memory, a camera and web access. That is a
much smaller blast radius than a shell, and it is not zero:

- **`web` is an exfiltration path.** It is included because it is what makes
  the assistant useful, and it means text the mic picked up could in principle
  steer a fetch. Remove it from `platform_toolsets.webhook` if that trade is
  not worth it.
- **Prompt fencing is a request, not a mechanism.** The route prompt wraps the
  transcript and says to treat it as data. That is worth doing and it is the
  weakest of the three defences; do not mistake it for a boundary.
- **The mic indicator is weaker evidence than `CAM`.** The camera light reads
  the kernel's sensor power state, so a crashed camera service cannot switch it
  off. There is no equivalent kernel fact for a microphone, so the mic light
  trusts the voice service's own status file.

### Email is READ-ONLY, by the owner's rule

> Never send an email without direct permission, typed and never spoken, using
> a specific phrase with correct syntax.

Enforced today by capability rather than policy: the OAuth scopes are
`.readonly`, Google refuses a send with HTTP 403, and no send path exists in
the plugin. `tests/test_send_consent.py` pins both.

The "typed, never spoken" half **cannot be a runtime check** — a tool handler
cannot tell which platform invoked it, because the registry does not pass the
platform to tools. It is therefore structural: any future send toolset stays
out of `platform_toolsets.webhook`, so the voice lane never has the tool in its
surface. A test asserts that against the live config, so widening the voice
lane later fails loudly. See `docs/GOOGLE.md`.

### Still not built, and why

**Gesture → Hermes.** The above is why. If it is ever built it needs: an
explicit, bounded, visibly indicated watch mode; a closed vocabulary mapped to a
fixed action allowlist; much tighter limits than the laptop lane (≥1.5 s apart,
≤6/min); and a restricted toolset for that lane. Whether
`platform_toolsets.webhook` can actually narrow that lane is **unconfirmed** —
`platform_toolsets.acp` is a documented counter-example that does not narrow
ACP — and must be proven before anything depends on it. A `deliver_only: true`
webhook route is the safest available shape, because it skips the agent
entirely: zero LLM cost and no ability to run a tool at all. Do not add it
casually.
