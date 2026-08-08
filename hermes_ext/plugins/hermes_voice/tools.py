"""The `speak` tool: say a short reply out loud in the room.

WHY A TOOL AND NOT A DELIVERY TARGET
The webhook adapter is fire-and-forget -- the POST returns 202 and the agent's
answer goes wherever `deliver:` names, with no synchronous return and no HTTP
callback. So the voice service cannot simply receive the reply it caused. The
alternative was scraping it out of the gateway journal, which is brittle string
parsing of a line truncated at 200 characters.

A tool at a documented extension point is the honest version of that, and it
DEGRADES WELL: if the model does not call it, the reply still reaches Discord
and only the audio is missing. A design that breaks silently when a model
chooses differently would be worse than one that simply goes quiet.

TRUST BOUNDARY, same as hermes_display. The model chooses TEXT and nothing
else -- no path, no filename, no voice, no device, no volume. The text is
sanitised and length-capped here rather than trusted, because it is about to be
handed to a subprocess pipeline.
"""

from __future__ import annotations

import os
from pathlib import Path

MAX_CHARS = 600


def _runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "hermes-voice"


def _sanitise(text: str) -> str:
    """Printable characters, collapsed whitespace, hard length cap.

    Control characters are stripped because this string crosses a file into
    another process's subprocess pipeline; newlines are collapsed because piper
    treats them as utterance breaks and a reply full of them stutters.
    """
    out = "".join(c for c in str(text or "") if c.isprintable() or c == " ")
    return " ".join(out.split())[:MAX_CHARS]


def speak(args: dict, **_kwargs) -> str:
    """Say something out loud in the room.

    SIGNATURE MATTERS HERE. Hermes calls a handler as `handler(args_dict,
    **kwargs)` -- every other plugin in this repo does `def f(args: dict,
    **kwargs)`. Declaring `def speak(text="")` instead bound the WHOLE
    arguments dict to `text`, so str() of it was written to speak.txt and
    piper read the punctuation out: every reply began with the spoken word
    "text". It looked like a prompt problem and was a calling-convention one.
    """
    clean = _sanitise((args or {}).get("text") if isinstance(args, dict)
                      else args)
    if not clean:
        return "Nothing to say: `text` was empty after sanitising."
    d = _runtime_dir()
    if not d.is_dir():
        return ("The voice service is not running, so nothing can be spoken. "
                "Your reply still reaches the user through the normal channel.")
    try:
        tmp = d / "speak.txt.tmp"
        tmp.write_text(clean)
        os.replace(tmp, d / "speak.txt")
    except OSError as e:
        return f"Could not queue speech: {e}"
    return f"Speaking {len(clean)} characters aloud."
