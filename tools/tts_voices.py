#!/usr/bin/env python3
"""Try piper voices out loud and pick one.

    python3 tools/tts_voices.py --list
    python3 tools/tts_voices.py --audition          # play every candidate
    python3 tools/tts_voices.py --use en_GB-alan-medium

Voices are from rhasspy/piper-voices on Hugging Face. Each is an .onnx plus a
.json sidecar; "medium" and "high" are model sizes, and on this Pi high is
still comfortably faster than nothing (~0.7x realtime at medium).

WHY AUDITION RATHER THAN READ DESCRIPTIONS
Voice is not a spec sheet. Two voices described identically as "British male"
sound nothing alike, and the one that reads well in a demo sentence can be the
one that grates on the tenth time it tells you the time. This plays the same
sentence through each, announced, so the comparison is like for like.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
DIR = Path.home() / ".local/share/hermes-pi/models/piper"
VENV_PIPER = Path.home() / ".local/share/hermes-pi/voice-venv/bin/piper"
CARD = os.environ.get("HERMES_VOICE_CARD", "plughw:wm8960soundcard")

# Curated for "posh" first, with two contrasts at the end so the choice is
# informed rather than a default. path = the voice's folder on HF.
VOICES = {
    "en_GB-alan-medium":        ("en/en_GB/alan/medium",        "British male, measured, RP-ish. The classic assistant voice."),
    "en_GB-cori-high":          ("en/en_GB/cori/high",          "British female, HIGH quality model. Warm, very natural."),
    "en_GB-southern_english_female-low": ("en/en_GB/southern_english_female/low", "Southern English female. Crisp RP."),
    "en_GB-jenny_dioco-medium": ("en/en_GB/jenny_dioco/medium", "British female, bright and friendly."),
    "en_GB-northern_english_male-medium": ("en/en_GB/northern_english_male/medium", "Northern English male. Warmer, less formal."),
    "en_GB-alba-medium":        ("en/en_GB/alba/medium",        "Scottish female."),
    "en_US-ryan-high":          ("en/en_US/ryan/high",          "American male, HIGH quality. For contrast."),
    "en_US-lessac-medium":      ("en/en_US/lessac/medium",      "American female. The current default."),
}

SAMPLE = ("Good evening. You have three unread messages, and nothing on your "
          "calendar until Thursday.")


def fetch(name: str) -> Path | None:
    path, _desc = VOICES[name]
    onnx = DIR / f"{name}.onnx"
    if onnx.exists() and (DIR / f"{name}.onnx.json").exists():
        return onnx
    DIR.mkdir(parents=True, exist_ok=True)
    for suffix in ("", ".json"):
        url = f"{BASE}/{path}/{name}.onnx{suffix}"
        dest = DIR / f"{name}.onnx{suffix}"
        try:
            print(f"    fetching {name}.onnx{suffix} ...", end="", flush=True)
            urllib.request.urlretrieve(url, dest)
            print(" ok")
        except Exception as e:
            print(f" FAILED ({e.__class__.__name__})")
            dest.unlink(missing_ok=True)
            return None
    return onnx


def say(model: Path, text: str) -> bool:
    try:
        piper = subprocess.Popen(
            [str(VENV_PIPER), "--model", str(model), "--output_file", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        play = subprocess.Popen(["aplay", "-D", CARD, "-q", "-"],
                                stdin=piper.stdout, stderr=subprocess.DEVNULL)
        piper.stdout.close()
        piper.stdin.write(text.encode())
        piper.stdin.close()
        play.wait()
        return True
    except Exception as e:
        print(f"    playback failed: {e}")
        return False


def audition(only: list[str] | None) -> int:
    names = only or list(VOICES)
    print("Playing each voice. Same sentence every time, announced first.\n")
    for i, name in enumerate(names, 1):
        _p, desc = VOICES[name]
        print(f"[{i}/{len(names)}] {name}\n    {desc}")
        model = fetch(name)
        if model is None:
            continue
        say(model, f"Voice {i}. {name.replace('-', ' ').replace('_', ' ')}.")
        say(model, SAMPLE)
        print()
    print("Pick one with:  python3 tools/tts_voices.py --use <name>")
    return 0


def use(name: str) -> int:
    if name not in VOICES:
        print(f"unknown voice {name!r}. --list to see them.")
        return 2
    model = fetch(name)
    if model is None:
        return 1
    # voice/speak.py reads this path; a symlink means changing voice does not
    # need a code edit or a service file change.
    link = DIR / "current.onnx"
    for p in (link, Path(str(link) + ".json")):
        p.unlink(missing_ok=True)
    link.symlink_to(model.name)
    Path(str(link) + ".json").symlink_to(f"{name}.onnx.json")
    print(f"voice set to {name}")
    print("restart to apply:  systemctl --user restart hermes-voice")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tts_voices")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--audition", action="store_true")
    ap.add_argument("--only", nargs="*", help="audition just these")
    ap.add_argument("--use", metavar="NAME")
    a = ap.parse_args(argv)

    if a.list:
        cur = (DIR / "current.onnx")
        now = os.readlink(cur).replace(".onnx", "") if cur.is_symlink() else "(default)"
        print(f"current: {now}\n")
        for n, (_p, d) in VOICES.items():
            have = "*" if (DIR / f"{n}.onnx").exists() else " "
            print(f" {have} {n:38s} {d}")
        print("\n * = already downloaded")
        return 0
    if a.use:
        return use(a.use)
    if a.audition or a.only:
        return audition(a.only or None)
    ap.print_help()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(0)
