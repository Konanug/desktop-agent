# Security posture

Audited 2026-08-04. This box holds a Discord bot token and ChatGPT OAuth tokens, and Hermes' `terminal`
toolset is enabled — so **anyone who can reach the bot or the shell can run commands as `alanmyin`.**
That single fact drives everything below.

**Exposure:** LAN only (`192.168.2.56/24`, wlan0). No port forwarding, no tunnel. Corroborated by zero
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

**Result: port 22 is now the only network-facing socket.** Everything else (VS Code server, Hermes
internals) binds to loopback only.

Reverse with `sudo systemctl enable --now rpcbind.socket` if NFS is ever needed.

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
   Accepted publickey for alanmyin from 192.168.2.89 ED25519 SHA256:E8othIIqyxCRYT…
   Accepted publickey: 1    Accepted password: 0
   ```
4. Only then `PasswordAuthentication no`, validated with `sshd -t` before reload

Installed key: `SHA256:E8othIIqyxCRYTkVkZcuKKugkGsUFCjHSQQLc6CXdE0` (`alanmyin-laptop`).

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

### Not built, and why

**Gesture triggers.** A gesture is a path from "someone waves in the room" to
"the agent runs a tool", and the Discord allowlist does not cover that path at
all — anyone physically present would become an unauthenticated user of a bot
with a shell. If it is ever built it needs: an explicit, bounded, visibly
indicated watch mode; a closed vocabulary mapped to a fixed action allowlist;
and ideally a restricted toolset for that lane. Do not add it casually.
