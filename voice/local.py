"""Phrases the voice service acts on ITSELF, without the agent or the network.

WHY THESE CANNOT GO THROUGH HERMES
The escape hatch exists for the case where the Pi is off the internet and the
screen shows only the Hermes visual, so there is no terminal and no SSH. In
exactly that situation Hermes is DEAD -- inference is a cloud call. A backdoor
that needs the agent is a backdoor that is missing when it is needed.

Everything these depend on is already local: openWakeWord and faster-whisper
both run from models on disk, and the action is a shell script that touches
only the framebuffer console. No network, no model, no gateway.

WHY EXACT-MATCH AND NOT "CONTAINS"
A substring test would fire on "can you open terminal for me later" and, worse,
on anything the television says. These require the transcript to be the WHOLE
utterance, normalised for case and punctuation. You have to say the phrase and
nothing else, straight after the wake word, which is hard to do by accident and
trivial to do on purpose.

The phrases are also deliberately not things you would say to an assistant in
passing. "Open terminal" as a complete sentence, alone, is not conversation.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "console-mode.sh"

# phrase -> (argument, what to say back)
COMMANDS = {
    "open terminal":    ("on",  "Terminal is on the screen, sir."),
    "exit hermes":      ("on",  "Terminal is on the screen, sir."),
    "console mode":     ("on",  "Terminal is on the screen, sir."),
    "close terminal":   ("off", "Panel restored."),
    "hermes mode":      ("off", "Panel restored."),
    "resume hermes":    ("off", "Panel restored."),
}


def normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse spaces.

    Whisper freely adds a full stop and varies capitalisation, so a raw
    comparison would miss the phrase for reasons that have nothing to do with
    what was said.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(text or "").lower())).strip()


def match(text: str) -> tuple[str, str] | None:
    """(script arg, reply) if the WHOLE utterance is a local command."""
    return COMMANDS.get(normalise(text))


def run(arg: str) -> bool:
    """Fire and forget. Never raises -- a failed escape hatch must not also
    take down the listener that is the only way to retry it."""
    try:
        subprocess.Popen(["bash", str(SCRIPT), arg],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False
