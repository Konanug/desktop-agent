"""Spoken commands that go straight to the laptop, without the agent.

WHY THIS EXISTS
A voice turn was measured at roughly ten seconds: ~2 s of transcription, ~6 s
of three model round trips, ~1-2 s of speech. The middle number is ChatGPT
serving time and is not a setting we can turn. So the only way to make "pause
the music" feel instant is to stop sending it to a model at all -- it has one
possible meaning and there is nothing to reason about.

This is the same trade `local.py` already makes for the escape hatch, with one
difference: those commands must work when the network is gone, these merely
should not WAIT for it.

WHY THIS IS NOT A NEW POWER
It publishes a NAME on the same `/intent` stream that `hermes_laptop` already
uses and that gestures already ride. It cannot open, press or run anything --
the laptop pulls the name, looks it up in a config on its own disk, and ignores
anything it does not recognise. Nothing said in this room can create a binding.
So the blast radius is unchanged from what a hand gesture could already do, and
strictly smaller than the agent's, which has `terminal`.

WHY EXACT-MATCH, AND WHY THESE PHRASES
Same rule as `local.py`: the transcript must be the WHOLE utterance. A substring
test would fire on "don't pause the music" and on whatever the television says.

The phrases are also not arbitrary. MEASURED with faster-whisper across three
synthetic voices, and short commands fail in ways that are worth knowing:

    go back              3/3      previous song   1/3  ("Treat this song")
    last song            3/3      skip back       0/3  ("Get back!")
    next song            3/3      go back a track 1/3  ("Go back a truck")
    play the music       ok       previous track  1/3  ("Prove this truck")

A one-word command is worse still -- a bare "pause" is 0.6 s of audio, and
whisper hallucinates on clips that short ("pause" came back as "toes"). Every
phrase here is at least two words for that reason, not for politeness.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from . import local

# The closed vocabulary. phrase -> (intent name, what to say back).
#
# Several phrases may share an intent; that is how a wording that whisper hears
# reliably gets added without inventing a new action. The NAME is what crosses
# to the laptop, and the laptop decides what it means.
#
# Volume says nothing back on purpose: the change in loudness IS the
# confirmation, and speaking over it to announce it would be absurd.
COMMANDS: dict[str, tuple[str, str]] = {
    "play the music":       ("PLAY",     "Playing."),
    "play the song":        ("PLAY",     "Playing."),
    "resume the music":     ("PLAY",     "Playing."),
    "pause the music":      ("PAUSE",    "Paused."),
    "pause the song":       ("PAUSE",    "Paused."),
    "stop the music":       ("PAUSE",    "Paused."),
    "next song":            ("NEXT",     "Next."),
    "next track":           ("NEXT",     "Next."),
    "skip the song":        ("NEXT",     "Next."),
    "go back":              ("PREV",     "Going back."),
    "last song":            ("PREV",     "Going back."),
    "turn the volume up":   ("VOL_UP",   ""),
    "turn it up":           ("VOL_UP",   ""),
    "turn the volume down": ("VOL_DOWN", ""),
    "turn it down":         ("VOL_DOWN", ""),
    "open spotify":         ("SPOTIFY",  "Opening Spotify."),
}

# Albums and anything else the owner adds, e.g.
#   {"play the album rumours": "ALBUM_RUMOURS",
#    "play the album rumors":  "ALBUM_RUMOURS"}
#
# Two spellings there is not a typo. MEASURED: every voice tested transcribed
# "rumours" as "rumors", because the model is American. Rather than guess at
# spelling, the file takes aliases and `voice/__main__.py` logs the transcript
# of anything it did NOT match -- so the way to add a phrase is to say it once,
# read what was heard, and paste that in.
EXTRA_PATH = Path.home() / ".config/hermes-pi/voice-commands.json"

# Same shape the /intent endpoint enforces. Checked here too, because a name
# rejected at the far end is a command that silently did nothing.
_NAME = re.compile(r"^[A-Za-z0-9_]{1,32}$")

_TOKEN = Path.home() / ".config/hermes-pi/camera-stream.token"


def extra() -> dict[str, tuple[str, str]]:
    """Owner-added phrases. Never raises -- a broken file must not take the
    built-in commands (or the service) down with it."""
    try:
        raw = json.loads(EXTRA_PATH.read_text())
    except (OSError, ValueError):
        return {}
    out: dict[str, tuple[str, str]] = {}
    if not isinstance(raw, dict):
        return {}
    for phrase, name in raw.items():
        name = str(name or "").strip().upper()
        key = local.normalise(phrase)
        if key and _NAME.match(name):
            out[key] = (name, "Right away.")
    return out


def match(text: str) -> tuple[str, str] | None:
    """(intent, reply) if the WHOLE utterance is a fast-lane command."""
    key = local.normalise(text)
    if not key:
        return None
    return extra().get(key) or COMMANDS.get(key)


def send(intent: str, timeout: float = 3.0) -> tuple[bool, str]:
    """Publish the intent. Returns (acted, what to say if not).

    The second element is the important one. `/intent` reports how many
    subscribers received the name, and zero means the laptop is not listening --
    the command went nowhere. Saying "Playing." then would be the panel
    inventing state, in speech instead of pixels, so it is refused here.
    """
    if not _NAME.match(intent):                        # pragma: no cover
        return False, "That command is not something I can send."
    try:
        tok = _TOKEN.read_text().strip()
    except OSError:
        tok = ""
    if not tok:
        return False, "I can't reach the laptop -- the stream token is missing."
    port = os.environ.get("HERMES_CAMERA_STREAM_PORT", "8081")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/intent?k={tok}",
        data=json.dumps({"action": intent}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return False, f"The camera service refused that, error {e.code}."
    except Exception:
        return False, "I can't reach the camera service on this Pi."
    if not doc.get("subscribers"):
        return False, "Your laptop isn't connected, sir."
    return True, ""
