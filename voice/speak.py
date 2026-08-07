"""Saying things out loud, via piper.

The reply reaches Discord through the webhook route's `deliver` setting. That
is the durable copy and it is not this module's problem. This module is the
copy the person in the room actually hears.

WHY A FILE THE AGENT WRITES, RATHER THAN READING THE REPLY OUT OF THE GATEWAY
The webhook adapter is fire-and-forget: the POST returns 202 immediately and
the agent's answer is delivered later, to whatever `deliver:` names. There is
no synchronous return and no HTTP callback, so this service cannot simply
receive the reply. Scraping it out of the journal would work and would be
brittle string parsing of a log line truncated at 200 characters.

Instead the agent is given a `speak` tool (hermes_ext/plugins/hermes_voice)
which drops text into speak.txt, and this watches that file. Same pattern as
hermes_display: an explicit tool at a documented extension point, rather than
reverse-engineering somebody's internals.

It also DEGRADES WELL, which is the real argument. If the agent does not call
speak, the reply still lands in Discord and nothing is lost except the audio.
A design that breaks silently when a model chooses differently would be worse
than one that goes quiet.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import protocol

VOICE_MODEL = Path.home() / ".local/share/hermes-pi/models/piper/en_US-lessac-medium.onnx"

# The piper BINARY FROM THIS VENV, resolved from sys.executable rather than
# found on PATH. Debian ships an unrelated program also called `piper` (a mouse
# configuration GUI), so a bare "piper" on PATH is not merely ambiguous -- it
# is very likely the wrong program, and it fails with "Unknown option --model",
# which reads like a version problem rather than a different application.
PIPER = str(Path(sys.executable).parent / "piper")

# Long replies are for Discord to display, not for a speaker to recite at
# somebody. Truncated rather than refused: hearing the first part is more
# useful than hearing nothing.
MAX_CHARS = 600


class Speaker:
    def __init__(self, card: str = protocol.CARD):
        self.card = card
        self.error: str | None = None
        self.spoke_at = 0.0
        self._proc: subprocess.Popen | None = None

    @property
    def available(self) -> bool:
        if not Path(PIPER).exists() and shutil.which("piper") is None:
            self.error = ("piper-tts not installed in this venv "
                          "(scripts/install-voice.sh)")
            return False
        if not VOICE_MODEL.exists():
            self.error = f"voice model missing at {VOICE_MODEL}"
            return False
        self.error = None
        return True

    @property
    def busy(self) -> bool:
        """Speaking is the one thing here that must not overlap itself."""
        return self._proc is not None and self._proc.poll() is None

    def say(self, text: str) -> bool:
        """Synthesise and play. Non-blocking; returns False if it could not
        start.

        piper writes a WAV to stdout and aplay consumes it, so nothing hits the
        disk and there is no temp file to clean up after a crash.
        """
        text = " ".join(str(text or "").split())[:MAX_CHARS]
        if not text or not self.available:
            return False
        if self.busy:
            return False
        try:
            piper = subprocess.Popen(
                [PIPER, "--model", str(VOICE_MODEL), "--output_file", "-"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL)
            self._proc = subprocess.Popen(
                ["aplay", "-D", self.card, "-q", "-"],
                stdin=piper.stdout, stderr=subprocess.DEVNULL)
            # Hand the write end to piper and let it close; without dropping
            # our reference the pipe never sees EOF and aplay waits forever.
            piper.stdout.close()
            piper.stdin.write(text.encode())
            piper.stdin.close()
            self.spoke_at = time.time()
            return True
        except Exception as e:                                # pragma: no cover
            self.error = f"speak failed: {e}"
            return False

    def stop(self) -> None:
        if self.busy and self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass


def take_pending() -> str | None:
    """Read and REMOVE anything the agent asked to have said.

    Removed on read so a reply is spoken exactly once. A file that survived
    would be re-read on the next tick and recited on a loop, which is the same
    level-versus-edge mistake gestures already made once.
    """
    p = protocol.speak_path()
    try:
        text = p.read_text().strip()
    except OSError:
        return None
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass
    return text or None
