r"""Drive Windows 11 from hand gestures seen by the Raspberry Pi.

    python hermes_gesture.py --config gestures.json --dry-run     # ALWAYS FIRST
    python hermes_gesture.py --config gestures.json

Subscribes to the Pi's /events feed and presses keys on this machine. Nothing
else. Requires Python 3.8+ and NOTHING ELSE -- no pip install, no admin. Input
is synthesised through SendInput via ctypes, which is the same API the OS's own
accessibility tooling uses.

WHICH WAY THE CONTROL FLOWS, AND WHY IT MATTERS

This laptop PULLS. The Pi cannot connect to it, cannot address it, and does not
know it exists until it subscribes. Every decision about what a gesture MEANS
is taken here, from a config file on this disk. So the worst a compromised or
malfunctioning Pi can do is lie about what gesture it saw -- and a lie still
only reaches the small, fixed set of actions in that config.

That is the entire reason the mapping is not on the Pi. It would have been less
code there.

WHAT A CAMERA CANNOT DO

A camera authenticates nobody. Anyone standing in that room can make these
gestures, including someone you did not invite, including someone on a video
call being displayed on a screen the camera can see. Choose bindings you would
be relaxed about a stranger triggering. Pausing music is a fine thing to hand
to the room. Anything that types into a focused window, closes an app without
saving, or runs a command is a different proposition, and the config has to opt
into that explicitly -- see `allow_run` below.

REPLAY IS REFUSED, TWICE

  * On connect the client asks for FUTURE events only. It does not ask the Pi
    what it missed. Waking your laptop must not fire a burst of actions from
    gestures made while it was asleep.
  * Every event is checked against `age_s`, which the Pi computes from its own
    monotonic clock and not from a wall clock the two machines would have to
    agree on. The Pi has no battery-backed RTC and is confidently wrong about
    the time for ~34 s after a boot; comparing timestamps across the two
    machines would reject everything during that window and silently accept
    everything if the skew went the other way.

Both bounds are independent of the Pi's own rate limiting, which this client
does not trust, because "the thing that acts" should never rely on "the thing
that reports" for its own safety limits.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

IS_WINDOWS = sys.platform == "win32"

# An event older than this is not acted on. Generous enough to survive a slow
# LAN and a GC pause, far too short to replay anything meaningful.
MAX_AGE = 3.0

# Reconnect backoff. Capped low: the failure this is most likely to see is the
# Pi's camera service restarting (3 s) or the laptop's wifi coming back.
BACKOFF_MIN = 1.0
BACKOFF_MAX = 20.0

# Read timeout on the event socket. Must be longer than the server's heartbeat
# (10 s) or every quiet period would look like a dead connection.
READ_TIMEOUT = 30.0

# Desktop apps a gesture may launch, by NAME, resolved to a URI here.
#
# WHY A TABLE AND NOT A URI IN THE CONFIG. The "url" action deliberately
# refuses anything that is not http(s), because a protocol handler is a far
# larger thing to hand to a room than a web page: handlers are registered by
# whatever software is installed, some take arguments, and "file:" reaches the
# disk. Allowing the config to name a scheme would quietly undo that.
#
# So the config names a KEY and this file owns the VALUE -- the same shape as
# the virtual-key table above and as the closed gesture vocabulary on the Pi.
# Adding an app is a code change, reviewed once, rather than a config change
# anyone can make. Entries must be schemes that take NO arguments, so there is
# nothing for a caller to inject into.
APPS = {
    "spotify": "spotify:",
}

# Spotify CONTENT a binding may open, e.g. spotify:album:4aawyAB9vmqN3uQ7FjRGTy.
#
# This is the one place a config supplies a URI, and it is allowed only because
# the shape is pinned to the point where there is nothing to smuggle: a fixed
# scheme, a fixed set of kinds, and a base62 id of exactly 22 characters. No
# query string, no path, no free text, no arguments. The worst thing a person
# standing in front of the camera can do with it is play music.
#
# Contrast the general case, which stays refused: "open this URI" would reach
# every protocol handler installed on the machine. "Play this Spotify id" does
# not generalise to that, which is why it gets its own action type instead of
# being a relaxation of "url" or a second field on "app".
#
# Get an id from Spotify: right-click a track/album/playlist -> Share ->
# Copy Spotify URI. (Copy Link gives a https://open.spotify.com/<kind>/<id>
# URL; the id is the same 22 characters.)
SPOTIFY_URI = re.compile(
    r"^spotify:(album|playlist|track|artist|show|episode):[0-9A-Za-z]{22}$")

# Where Spotify actually is, tried BEFORE the spotify: scheme.
#
# os.startfile IS the right API -- it is ShellExecute, the same thing a
# double-click does, and it does not go anywhere near the default browser. But
# it can only reach the desktop app if something registered a handler for
# "spotify:", and that registration is not ours and is not reliable: the
# Microsoft Store build registers differently from the standalone installer,
# neither registers until the app has been run once, and a browser update can
# take the association over. When it is missing, Windows falls back to the web
# player -- which is the exact symptom, and no amount of correctness in this
# file fixes it.
#
# Launching the executable with --uri= skips the question entirely. The URI is
# already pinned by SPOTIFY_URI above, and it is passed as an argv element, so
# there is no command line for it to break out of.
SPOTIFY_EXE = (
    r"%APPDATA%\Spotify\Spotify.exe",           # standalone installer
    r"%LOCALAPPDATA%\Microsoft\WindowsApps\Spotify.exe",   # Store alias
    r"%PROGRAMFILES%\Spotify\Spotify.exe",
)


def _spotify_exe() -> str | None:
    """The first Spotify.exe that exists, or None.

    Expanded at call time rather than at import: these are per-user paths and
    the program may be installed while this is running.
    """
    for raw in SPOTIFY_EXE:
        p = os.path.expandvars(raw)
        if "%" not in p and os.path.isfile(p):
            return p
    return None


# -- input synthesis -------------------------------------------------------
# Virtual key codes. Deliberately a FIXED TABLE and not "look up any key the
# config names": the config is the security boundary for this program, and a
# boundary you can extend by typing a hex number in a JSON file is not one.
VK = {
    # media and volume -- the benign default vocabulary
    "play_pause": 0xB3, "next_track": 0xB0, "prev_track": 0xB1,
    "stop_media": 0xB2, "volume_up": 0xAF, "volume_down": 0xAE,
    "volume_mute": 0xAD,
    # modifiers
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    # navigation
    "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "enter": 0x0D, "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    # letters and digits
    **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz"},
    **{c: ord(c) for c in "0123456789"},
    # function keys
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}

# Keys that need KEYEVENTF_EXTENDEDKEY. Getting this wrong is not a crash, it
# is a key that quietly does nothing in some applications and works in others.
EXTENDED = {
    0xB3, 0xB0, 0xB1, 0xB2, 0xAF, 0xAE, 0xAD,          # media/volume
    0x5B, 0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22,          # win, edit, nav
    0x25, 0x26, 0x27, 0x28,                            # arrows
}

_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_KEYEVENTF_EXTENDEDKEY = 0x0001

# Explicit-width types rather than ctypes.wintypes. Identical on Windows, and
# importable everywhere -- which is what lets the struct layout below be tested
# on the Pi, where the bug it encodes was actually found.
_WORD = ctypes.c_uint16
_DWORD = ctypes.c_uint32
_LONG = ctypes.c_int32
# ULONG_PTR: 64-bit on x64, 32-bit on x86.
_ULONG_PTR = (ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8
              else ctypes.c_uint32)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", _WORD), ("wScan", _WORD), ("dwFlags", _DWORD),
                ("time", _DWORD), ("dwExtraInfo", _ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    """DECLARED BUT NEVER USED, AND IT HAS TO BE.

    SendInput validates its third argument against sizeof(INPUT), and INPUT's
    union is sized by its LARGEST member -- which is MOUSEINPUT (32 bytes on
    x64), not KEYBDINPUT (24). Declaring only `ki`, which every abbreviated
    copy of this snippet online does, makes sizeof(INPUT) 32 instead of 40.
    Windows then rejects every call with ERROR_INVALID_PARAMETER (87) and
    presses nothing.

    OBSERVED: this shipped, and dry-run could not catch it because dry-run
    never calls SendInput. Its symptom -- `SendInput sent 0/2 (error 87)` --
    was initially misread here as UIPI blocking injection into an elevated
    window, which is a DIFFERENT error (5, ERROR_ACCESS_DENIED). _send() now
    distinguishes them rather than guessing.
    """
    _fields_ = [("dx", _LONG), ("dy", _LONG), ("mouseData", _DWORD),
                ("dwFlags", _DWORD), ("time", _DWORD),
                ("dwExtraInfo", _ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", _DWORD), ("wParamL", _WORD), ("wParamH", _WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", _DWORD), ("u", _INPUTUNION)]


# The Win32 ABI fixes these. Checked at import so a layout mistake fails here,
# loudly, instead of becoming a key that silently does not get pressed.
EXPECTED_INPUT_SIZE = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
assert ctypes.sizeof(INPUT) == EXPECTED_INPUT_SIZE, (
    f"INPUT is {ctypes.sizeof(INPUT)} bytes, Windows expects "
    f"{EXPECTED_INPUT_SIZE} -- SendInput would reject every call with "
    f"ERROR_INVALID_PARAMETER")


class Keyboard:
    """SendInput through ctypes. Built once; no-ops off Windows."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run or not IS_WINDOWS
        if self.dry_run:
            return
        self._INPUT = INPUT
        self._KEYBDINPUT = KEYBDINPUT
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.SendInput.argtypes = (ctypes.c_uint,
                                           ctypes.POINTER(INPUT),
                                           ctypes.c_int)
        self._user32.SendInput.restype = ctypes.c_uint

    def _send(self, inputs) -> None:
        n = len(inputs)
        arr = (self._INPUT * n)(*inputs)
        sent = self._user32.SendInput(n, arr, ctypes.sizeof(self._INPUT))
        if sent == n:
            return
        err = ctypes.get_last_error()
        # Name the two that actually happen. "Something went wrong" sends you
        # looking in the wrong place -- as it did here once already.
        why = {
            5: "access denied -- the focused window is elevated and Windows "
               "blocks injection into a higher-integrity process (UIPI). Run "
               "the target app unelevated, or this client elevated.",
            87: "invalid parameter -- sizeof(INPUT) is wrong for this "
                "architecture. This is a bug in this file, not your setup.",
        }.get(err, "unexpected")
        print(f"  ! SendInput sent {sent}/{n} (error {err}): {why}", flush=True)

    def _key(self, vk: int, up: bool):
        flags = _KEYEVENTF_KEYUP if up else 0
        if vk in EXTENDED:
            flags |= _KEYEVENTF_EXTENDEDKEY
        return self._INPUT(type=1, ki=self._KEYBDINPUT(
            wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0))

    def combo(self, names: list[str]) -> None:
        """Press modifiers, tap the final key, release in reverse order."""
        vks = [VK[n] for n in names]
        if self.dry_run:
            print(f"  [dry-run] key {'+'.join(names)}", flush=True)
            return
        seq = [self._key(v, False) for v in vks]
        seq += [self._key(v, True) for v in reversed(vks)]
        self._send(seq)

    def text(self, s: str) -> None:
        """Type a literal string as Unicode.

        KEYEVENTF_UNICODE bypasses the keyboard layout entirely, so this types
        the same characters on a US and a UK layout. Surrogate pairs are sent
        as two inputs, which is what the API expects.
        """
        if self.dry_run:
            print(f"  [dry-run] text {s!r}", flush=True)
            return
        seq = []
        raw = s.encode("utf-16-le")
        for i in range(0, len(raw), 2):
            code = int.from_bytes(raw[i:i + 2], "little")
            for up in (False, True):
                flags = _KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if up else 0)
                seq.append(self._INPUT(type=1, ki=self._KEYBDINPUT(
                    wVk=0, wScan=code, dwFlags=flags, time=0, dwExtraInfo=0)))
        if seq:
            self._send(seq)


# -- actions ---------------------------------------------------------------
def parse_combo(spec: str) -> list[str]:
    """'ctrl+shift+m' -> ['ctrl','shift','m']. Raises on anything unknown."""
    names = [p.strip().lower() for p in str(spec).split("+") if p.strip()]
    if not names:
        raise ValueError("empty key spec")
    bad = [n for n in names if n not in VK]
    if bad:
        raise ValueError(f"unknown key(s) {bad} -- see VK in this file for the "
                         f"full list of what is allowed")
    return names


class Dispatcher:
    def __init__(self, config: dict, kb: Keyboard):
        self.kb = kb
        self.allow_run = bool(config.get("allow_run", False))
        self.cooldown = float(config.get("cooldown_s", 1.0))
        self._last: dict[str, float] = {}
        self.bindings = {}
        for key, action in (config.get("bindings") or {}).items():
            self.bindings[key.strip().upper()] = self._validate(key, action)

    def _validate(self, key: str, action) -> dict:
        """Fail at STARTUP, not on the gesture.

        A typo that only surfaces the first time you make the shape is a typo
        you find while waving at a camera wondering whether the camera is
        broken.
        """
        if isinstance(action, str):
            action = {"type": "key", "keys": action}
        kind = action.get("type")
        if kind == "key":
            parse_combo(action["keys"])
        elif kind == "text":
            if not isinstance(action.get("text"), str):
                raise ValueError(f"{key}: 'text' action needs a string 'text'")
        elif kind == "run":
            if not self.allow_run:
                raise ValueError(
                    f"{key}: 'run' actions are refused unless the config sets "
                    f'"allow_run": true. Read the header of this file first -- '
                    f"anyone standing in that room can trigger this.")
            if not isinstance(action.get("command"), list):
                raise ValueError(f"{key}: 'command' must be a LIST of "
                                 f"arguments, never a shell string")
        elif kind == "url":
            u = str(action.get("url") or "")
            if not u.startswith(("http://", "https://")):
                raise ValueError(
                    f"{key}: 'url' must start with http:// or https://. "
                    f"Anything else is a file or a protocol handler, which is "
                    f"a different and much larger thing to hand to a room.")
        elif kind == "app":
            name = str(action.get("app") or "").strip().lower()
            if name not in APPS:
                raise ValueError(
                    f"{key}: unknown app {name!r}. The list is CLOSED and "
                    f"lives in this file, not in your config: "
                    f"{', '.join(sorted(APPS))}. That is the point -- a config "
                    f"cannot name an arbitrary protocol handler, so the worst "
                    f"a gesture can reach is one of these.")
        elif kind == "spotify":
            u = str(action.get("uri") or "").strip()
            if not SPOTIFY_URI.match(u):
                raise ValueError(
                    f"{key}: 'uri' must be exactly spotify:<kind>:<22-char id> "
                    f"-- got {u!r}. Kinds: album, playlist, track, artist, "
                    f"show, episode. In Spotify: right-click -> Share -> "
                    f"Copy Spotify URI.")
            action = dict(action, uri=u)
        elif kind == "log":
            pass
        else:
            raise ValueError(f"{key}: unknown action type {kind!r}")
        return action

    def lookup(self, hand: str, gesture: str):
        """'RIGHT PEACE' beats 'PEACE'. Specific wins.

        HERMES INTENTS DO NOT FALL BACK TO A BARE BINDING, and that is a
        deliberate separation of two different grants. Binding `PEACE` means
        "a hand in front of my camera may do this". If a `HERMES PEACE` intent
        also matched it, then asking Hermes to do something would silently
        inherit every gesture you had ever bound -- so granting a gesture would
        be granting the agent, which is not what anyone means by it.

        With the fallback removed, the two lists are independent: you can bind
        gestures without giving Hermes anything, and vice versa.
        """
        hand = (hand or "").upper()
        if hand == "HERMES":
            key = f"HERMES {gesture}".upper()
            return (key, self.bindings[key]) if key in self.bindings else (None, None)
        for key in (f"{hand} {gesture}".upper(), gesture.upper()):
            if key in self.bindings:
                return key, self.bindings[key]
        return None, None

    def _open(self, uri: str) -> bool:
        """Launch a NON-http URI, preferring the app over the URI scheme.

        os.startfile, not webbrowser: webbrowser hands the string to the
        default BROWSER, which is the wrong program for a custom scheme and may
        drop it silently -- that is exactly why 'open spotify' kept landing on
        the web player. No shell string is built, so there is nothing to quote.

        But os.startfile only reaches the app if "spotify:" is registered, and
        that is not something this program controls (see SPOTIFY_EXE). So a
        spotify: URI tries the executable FIRST and falls back to the scheme.
        Which route ran is printed, because the last round of this was diagnosed
        by guessing and the guess was wrong.
        """
        if self.kb.dry_run:
            print(f"  [dry-run] launch {uri}", flush=True)
            return True
        if uri.startswith("spotify:"):
            exe = _spotify_exe()
            if exe:
                # --uri= even for a bare "spotify:", which just opens the app.
                subprocess.Popen([exe, f"--uri={uri}"],
                                 close_fds=True)
                print(f"  via {exe}", flush=True)
                return True
            print("  Spotify.exe not found -- falling back to the spotify: "
                  "handler, which may open the web player", flush=True)
        if not hasattr(os, "startfile"):
            print("  ! this action needs Windows (no os.startfile)", flush=True)
            return False
        os.startfile(uri)                      # type: ignore[attr-defined]
        print("  via the shell (os.startfile)", flush=True)
        return True

    def fire(self, ev: dict) -> bool:
        key, action = self.lookup(ev.get("hand", "?"), ev.get("gesture", ""))
        if action is None:
            print(f"  {ev['hand']} {ev['gesture']} -- no binding", flush=True)
            return False
        now = time.monotonic()
        # A per-binding cooldown, independent of the Pi's rate limit. The thing
        # that ACTS keeps its own bounds; it does not delegate them upstream.
        if now - self._last.get(key, 0.0) < self.cooldown:
            print(f"  {key} -- cooling down", flush=True)
            return False
        self._last[key] = now
        kind = action["type"]
        print(f"  {key} -> {kind} "
              f"{action.get('keys') or action.get('command') or ''}", flush=True)
        try:
            if kind == "key":
                self.kb.combo(parse_combo(action["keys"]))
            elif kind == "text":
                self.kb.text(action["text"])
            elif kind == "url":
                # webbrowser, not `start` -- it opens the DEFAULT browser
                # without a shell, so a URL can never be interpreted as a
                # command line however it is punctuated.
                import webbrowser
                if self.kb.dry_run:
                    print(f"  [dry-run] open {action['url']}", flush=True)
                else:
                    webbrowser.open(action["url"])
            elif kind == "app":
                if not self._open(APPS[str(action["app"]).strip().lower()]):
                    return False
            elif kind == "spotify":
                # Opens the desktop app AND starts playback of that item.
                # _open prefers Spotify.exe over the spotify: scheme -- see
                # SPOTIFY_EXE for why that is not the same thing.
                if not self._open(action["uri"]):
                    return False
            elif kind == "run":
                if self.kb.dry_run:
                    print(f"  [dry-run] run {action['command']}", flush=True)
                else:
                    subprocess.Popen(action["command"], shell=False)
            # "log" does nothing by design -- it is how you audition a gesture.
        except Exception as e:
            print(f"  ! action failed: {e.__class__.__name__}: {e}", flush=True)
            return False
        return True


# -- the feed --------------------------------------------------------------
def parse_sse(stream, on_event, on_hello=None) -> None:
    """Minimal SSE reader. Blocks until the connection dies.

    Written out rather than pulled from a library because the whole client is
    deliberately dependency-free, and because SSE framing is genuinely this
    small: blank line ends a message, `event:` names it, `data:` accumulates.
    Lines starting with ':' are comments -- the server's heartbeat -- and their
    only job is to make a dead socket raise here instead of hanging forever.
    """
    name, data = "message", []
    for raw in stream:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data:
                payload = "\n".join(data)
                try:
                    doc = json.loads(payload)
                except json.JSONDecodeError:
                    doc = None
                if doc is not None:
                    if name == "gesture":
                        on_event(doc)
                    elif name == "hello" and on_hello:
                        on_hello(doc)
            name, data = "message", []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    raise ConnectionError("event stream ended")


def run(cfg: dict, disp: Dispatcher, once: bool = False) -> int:
    base = cfg["url"].rstrip("/")
    token = cfg.get("token") or ""
    url = base + "/events" + (f"?k={urllib.parse.quote(token)}" if token else "")
    backoff = BACKOFF_MIN
    seen = 0

    def on_hello(doc):
        nonlocal backoff
        backoff = BACKOFF_MIN
        print(f"connected  cursor={doc.get('cursor')} "
              f"epoch={doc.get('epoch')}", flush=True)

    def on_event(ev):
        nonlocal seen
        seen += 1
        age = ev.get("age_s")
        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] #{ev.get('seq')} {ev.get('hand')} "
              f"{ev.get('gesture')} [{ev.get('fingers_up')}] "
              f"age={age}s", flush=True)
        # Age is computed by the Pi from its own monotonic clock, so this
        # comparison never involves the two machines agreeing about the time.
        if age is None or age > MAX_AGE:
            print(f"  refused: {age}s old (limit {MAX_AGE}s)", flush=True)
            return
        disp.fire(ev)

    while True:
        try:
            req = urllib.request.Request(url, headers={"Accept":
                                                       "text/event-stream"})
            with urllib.request.urlopen(req, timeout=READ_TIMEOUT) as r:
                parse_sse(r, on_event, on_hello)
        except KeyboardInterrupt:
            print("\nstopped", flush=True)
            return 0
        except urllib.error.HTTPError as e:
            if e.code == 403:
                print("403 forbidden -- the token in your config does not "
                      "match the Pi's ~/.config/hermes-pi/camera-stream.token",
                      flush=True)
                return 2
            if e.code == 404:
                print("404 -- the Pi has gestures disabled "
                      "(HERMES_CAMERA_GESTURES=off)", flush=True)
                return 2
            print(f"http {e.code}: {e.reason}", flush=True)
        except Exception as e:
            print(f"disconnected ({e.__class__.__name__}: {e})", flush=True)
        if once:
            return 1
        print(f"reconnecting in {backoff:.0f}s "
              f"(no replay -- missed gestures stay missed)", flush=True)
        try:
            time.sleep(backoff)
        except KeyboardInterrupt:
            return 0
        backoff = min(BACKOFF_MAX, backoff * 2)


def _open_log(path: str | None) -> None:
    """Give this program somewhere to speak when it has no console.

    Under pythonw.exe -- which is how it runs unattended, and the whole point
    of running it that way -- sys.stdout is None. CPython's print() then
    SILENTLY DOES NOTHING: not an error, not a warning, just nothing. So every
    diagnostic this client is careful to produce ("403 forbidden", "no
    binding", "action failed") is discarded exactly in the configuration where
    nobody is watching to notice it stopped.

    Default location is %LOCALAPPDATA% rather than beside the script, because
    the script may sit somewhere the user cannot write.
    """
    if path is None:
        # `sys.stdout is not None` IS NOT A RELIABLE TEST FOR "has a console".
        # It is the documented pythonw.exe behaviour and it did not hold here:
        # across six background runs -- several of which provably connected to
        # the Pi and delivered gestures -- no log file was ever created, which
        # can only mean this branch returned early. A stdout that is not None
        # but is not writable either fails BOTH ways: nothing is logged, and
        # every print() raises into a program that has nowhere to report it.
        #
        # So probe it rather than trust it: if writing to stdout does not work,
        # this has no console whatever the object says.
        if _stdout_works():
            return                              # a real console: leave it alone
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "hermes-gesture.log")
    try:
        f = open(path, "a", encoding="utf-8", buffering=1)   # line buffered
    except OSError:
        return                                  # never fail to start over a log
    sys.stdout = sys.stderr = f
    print(f"\n--- started {time.strftime('%Y-%m-%d %H:%M:%S')} "
          f"pid={os.getpid()} ---", flush=True)


def _stdout_works() -> bool:
    """Can we actually print? Not "is there a stdout object".

    Under pythonw.exe sys.stdout is documented to be None, and print() then
    silently does nothing -- which is survivable. What is not survivable is a
    stdout that EXISTS and raises on write: print() then throws OSError from
    wherever it is called, and in a program whose whole job is to sit in a loop
    that kills it with no way to say why.

    Writing nothing and flushing is enough to find out, and costs nothing on a
    real console.
    """
    out = getattr(sys, "stdout", None)
    if out is None:
        return False
    try:
        out.write("")
        out.flush()
        return True
    except Exception:
        return False


def _find_config(name: str) -> str:
    """The config, found from the working directory OR from next to this file.

    A Scheduled Task with no "Start in" runs from C:\\Windows\\System32, so a
    relative --config resolves to a path that does not exist and the program
    exits before it has a console, a log, or any way to say so. That looked
    exactly like "the task never ran", and cost an evening.

    An explicit path is honoured as given; only the default and other relative
    names get the fallback, so this cannot silently load a different file than
    the one that was asked for.
    """
    if os.path.isabs(name):
        return name
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    cwd = os.path.abspath(name)
    if not os.path.isfile(cwd) and os.path.isfile(here):
        return here
    return cwd


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="hermes_gesture",
        description="Act on hand gestures seen by the Hermes Pi camera.")
    ap.add_argument("--config", default="gestures.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what WOULD be pressed and press nothing. "
                         "Run this first, every time you change bindings.")
    ap.add_argument("--once", action="store_true",
                    help="do not reconnect; exit when the stream ends")
    ap.add_argument("--log", default=None, metavar="PATH",
                    help="append output here. Implied when there is no "
                         "console (pythonw.exe), where output otherwise "
                         "vanishes silently.")
    ap.add_argument("--fire", default=None, metavar="NAME",
                    help="run one binding NOW and exit, e.g. "
                         "--fire \"HERMES SPOTIFY\". Never connects to the Pi. "
                         "Use it to tell a broken binding apart from a broken "
                         "connection.")
    args = ap.parse_args(argv)
    _open_log(args.log)

    path = _find_config(args.config)
    try:
        cfg = json.loads(open(path, encoding="utf-8").read())
    except OSError as e:
        print(f"cannot read {path}: {e}", flush=True)
        return 2
    except json.JSONDecodeError as e:
        print(f"{path} is not valid JSON: {e}", flush=True)
        return 2
    if not cfg.get("url"):
        print(f"{path} needs a \"url\", e.g. http://192.168.1.50:8081",
              flush=True)
        return 2

    kb = Keyboard(dry_run=args.dry_run)
    try:
        disp = Dispatcher(cfg, kb)
    except (ValueError, KeyError) as e:
        print(f"config error: {e}", flush=True)
        return 2

    if not IS_WINDOWS and not args.dry_run:
        print(f"not running on Windows ({sys.platform}) -- forcing --dry-run",
              flush=True)

    if args.fire is not None:
        # Deliberately BEFORE any network use: the point of --fire is to prove
        # whether a binding works, without the connection being able to be the
        # reason it did not.
        key = args.fire.strip()
        action = disp.bindings.get(key)
        if action is None:
            print(f"no binding named {key!r}. Known: "
                  f"{', '.join(sorted(disp.bindings))}", flush=True)
            return 2
        print(f"firing {key} ({action['type']})", flush=True)
        hand, _, gesture = key.partition(" ")
        ok = disp.fire({"hand": hand, "gesture": gesture})
        print("fired" if ok else "did NOT fire", flush=True)
        return 0 if ok else 1

    print(f"hermes-gesture  {cfg['url']}  "
          f"{len(disp.bindings)} bindings  cooldown {disp.cooldown}s"
          f"{'  [DRY RUN]' if kb.dry_run else ''}", flush=True)
    for k, a in sorted(disp.bindings.items()):
        print(f"  {k:16s} {a['type']:5s} "
              f"{a.get('keys') or a.get('text') or a.get('command') or ''}",
              flush=True)
    print("Ctrl-C to stop.\n", flush=True)
    return run(cfg, disp, once=args.once)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
