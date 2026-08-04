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

## Pending

### SSH key-only authentication
**Not yet applied — and the order is critical.** `~/.ssh/authorized_keys` was **empty (0 bytes)**, so
password auth is currently the *only* way in. Disabling it first would lock the box out completely,
recoverable only with a physical keyboard and monitor.

Correct sequence:
1. Generate a keypair **on the laptop** (`ssh-keygen -t ed25519`) — the private key must never touch the Pi
2. Install the public key into `~/.ssh/authorized_keys` (`600`), `~/.ssh` (`700`)
3. **Verify key login works in a brand-new session, while the current one stays open**
4. Only then add `PasswordAuthentication no` to the `10-` drop-in
5. `sudo sshd -t`, reload, and confirm with `sudo sshd -T | grep passwordauth`

Step 3 is not optional. Keep the working session open as an escape hatch until a *new* session
authenticates by key.

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
