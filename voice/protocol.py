"""Constants and paths for the voice service.

Mirrors camera/protocol.py in spirit: the numbers that shape behaviour live in
one place with the measurement that justifies them, so changing one is a
decision rather than an accident.

STATE, NEVER CONTENT -- the same rule as status.json and hands.json. This
service's status file says whether it is listening and how long inference took.
It never carries a transcript. What was SAID is content, and content about a
microphone in a room is the most sensitive thing this project handles.
"""

from __future__ import annotations

import os
from pathlib import Path

SCHEMA = 1

# The HAT. plughw: rather than hw: so ALSA does the format conversion; the
# WM8960 is happy at 16 kHz but plug costs nothing and removes a class of
# "Invalid argument" failures on a rate it does not natively want.
CARD = os.environ.get("HERMES_VOICE_CARD", "plughw:wm8960soundcard")
RATE = 16000            # what both openWakeWord and Whisper expect
CHANNELS = 1

# openWakeWord's native frame. 1280 samples = 80 ms at 16 kHz; it is not a
# tunable -- the model's melspectrogram front end is built around it.
FRAME = 1280

# MEASURED on this Pi: 6.24 ms cpu per 80 ms frame = 7.8% of one core,
# continuously, 219 MB RSS. That is the always-on cost of the whole feature
# and the reason a wake word is used at all rather than running STT on
# everything.
WAKE_MODEL = os.environ.get("HERMES_VOICE_WAKE", "hey_jarvis")
WAKE_THRESHOLD = float(os.environ.get("HERMES_VOICE_WAKE_THRESHOLD", "0.5"))

# STT. MEASURED transcribing 6 s of audio, int8 on CPU:
#     tiny.en   1.68 s  = 3.6x realtime
#     base.en   2.40 s  = 2.5x realtime      <- chosen
#     small.en  7.08 s  = 0.8x realtime      <- SLOWER THAN REALTIME, unusable
# base.en is the accuracy/latency knee. Anything larger cannot keep up with a
# person talking, which is disqualifying regardless of how well it reads.
STT_MODEL = os.environ.get("HERMES_VOICE_STT", "base.en")

# The model that reads FIRST, only to see whether this is a fixed command.
#
# MEASURED across three voices on realistic phrases, padded as a real capture
# is: tiny.en and base.en both score 15/18 exact on the command vocabulary, at
# 888 ms against 1748 ms. Identical where it is used, half the price. It never
# reaches the agent -- a phrase that is not a command is re-read with STT_MODEL
# before anything is sent -- so question accuracy is untouched.
#
# "off" restores the single-model behaviour.
STT_FAST_MODEL = os.environ.get("HERMES_VOICE_STT_FAST", "tiny.en")

# HOW MANY CORES WHISPER MAY USE, AND WHY IT IS NOT "ALL OF THEM".
#
# ctranslate2 defaults to one thread per core (4 here). This service runs under
# `CPUQuota=150%`, so those 4 threads ask for 400% of a budget that is 150%.
# The kernel grants 150 ms of each 100 ms period and then FREEZES THE WHOLE
# CGROUP for the remainder, repeatedly, while the threadpool's barrier spins
# through what budget there is.
#
# This is not a theory -- it is on the cgroup's own counter. Measured on the
# running service: `nr_throttled 1331` periods and `throttled_usec 111515149`
# (111.5 s of frozen time) in 8 minutes of uptime.
#
# MEASURED, base.en on a 2.53 s clip, inside a 150% scope with the camera live:
#     cpu_threads=0 (4)   4033 / 4184 / 4177 ms
#     cpu_threads=2       2947 / 2962 / 2943 ms      <- chosen, 30% faster
#     cpu_threads=1       3355 / 3332 / 3263 ms
# Two threads is the quota (1.5 cores) rounded to something a scheduler can
# actually run. NOTE this does not contradict the older "cpu_threads=4 does not
# help" note in CLAUDE.md: that was measured WITHOUT the quota binding, where
# the count genuinely did not matter. Under a quota it does.
STT_THREADS = int(os.environ.get("HERMES_VOICE_STT_THREADS", "2"))

# A CEILING ON DECODING, because there was not one and it cost two minutes.
#
# OBSERVED 2026-08-15: a 2.0 s utterance took 11421 ms in tiny.en and then
# 116012 ms in base.en -- against measured norms of 888 ms and 1748 ms. For all
# of that time the microphone was open and the panel was showing its listening
# animation, so from the room it was indistinguishable from a service that had
# locked on and would never stop. Nothing anywhere bounded it.
#
# Generous on purpose: this is a backstop against a runaway, not a latency
# target. A turn that legitimately needs longer than this has already failed the
# person waiting for it.
STT_BUDGET = float(os.environ.get("HERMES_VOICE_STT_BUDGET", "20.0"))

# Past this, a turn is SAID OUT LOUD in the log with its numbers. It is not an
# error -- the turn still completes -- it is the difference between "the box
# felt slow once" and a line naming which stage took the time.
SLOW_TURN = float(os.environ.get("HERMES_VOICE_SLOW_TURN", "8.0"))

# Log the transcript of anything the fast lane did NOT match.
#
# Off by default. journald here is persistent and a permanent record of
# everything said near this microphone is not a thing to create by accident.
# It is worth having temporarily: adding a phrase to the fast lane means
# knowing what whisper actually heard, and every voice tested renders "rumours"
# as "rumors", which is not something to guess at.
LOG_TRANSCRIPT = os.environ.get(
    "HERMES_VOICE_LOG_TRANSCRIPT", "off").lower() in ("on", "1", "yes", "true")

# Utterance capture. A person pausing mid-sentence must not end the turn, and a
# person who has finished must not wait around.
MAX_UTTERANCE = 10.0    # hard ceiling; a stuck endpointer cannot record forever
# Trailing silence that ends the turn.
#
# Went 0.8 -> 1.3 to stop it cutting people off mid-sentence, then back to 0.7
# once HYSTERESIS made that the wrong lever. The two are doing different jobs
# and conflating them cost a second of dead air on every single turn: the
# continue-threshold is what keeps a sentence alive through its dips, so this
# only has to be long enough to distinguish "finished" from "drawing breath".
# With the loose continue threshold, 0.7 s of genuine silence means finished.
SILENCE_END = float(os.environ.get("HERMES_VOICE_SILENCE_END", "0.7"))
MIN_UTTERANCE = 0.4     # shorter than this is a cough, not a request

# If NOTHING is said in this long after a wake, give up immediately.
#
# Raised 2.0 -> 4.5. Two seconds is how long it takes to gather a thought, so
# it was abandoning the turn while the person was still deciding what to ask --
# reported as "it responds to hey jarvis then goes back to idle immediately",
# which is exactly what that looks like from outside. The cost of being
# generous is bounded: nothing was said, so nothing is transcribed or sent, and
# MAX_UTTERANCE still caps the whole capture.
#
# This is the difference between a false wake costing 2 s and costing the full
# ceiling. The first version only ended a capture once speech had been heard
# ("quiet_for >= SILENCE_END and spoke_for > 0"), so a wake word that fired on
# a television with nobody in the room recorded until MAX_UTTERANCE -- the
# cheapest case was accidentally the most expensive. Reported as the mic being
# "left on way after I intended", and that was exactly right.
LEAD_SILENCE = float(os.environ.get("HERMES_VOICE_LEAD", "4.5"))
PREROLL = 0.5           # audio kept from BEFORE the wake word fired, so the
                        # first syllable after it is never clipped

# Anyone audible can trigger this, so the same sliding-window discipline as
# gestures applies -- and tighter, because this reaches an agent rather than a
# media key. Both windows slide and therefore cannot wedge (trap 19).
MIN_GAP = 3.0           # seconds between accepted utterances
MAX_PER_MIN = 6
MAX_PER_HOUR = 60

# Where the agent is reached. LOOPBACK: the only caller is this service, on
# this box, so there is no reason to expose another network-facing socket.
WEBHOOK_URL = os.environ.get(
    "HERMES_VOICE_WEBHOOK", "http://127.0.0.1:8644/webhooks/voice")


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "hermes-voice"


def ensure_dirs() -> Path:
    d = runtime_dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def status_path() -> Path:
    return runtime_dir() / "status.json"


def speak_path() -> Path:
    """Text the agent wants said out loud, dropped here by the hermes_voice
    plugin. A file rather than a socket for the same reason the camera uses
    files: no handshake, no reconnect, no second failure mode."""
    return runtime_dir() / "speak.txt"


def secret_path() -> Path:
    """The webhook HMAC secret. Under the OWNER's config, not ~/.hermes/ --
    anything Hermes manages, Hermes can rewrite."""
    return Path.home() / ".config" / "hermes-pi" / "voice-webhook.secret"


def disabled_path() -> Path:
    """Durable owner kill switch for the microphone. Same placement and same
    honest caveat as the camera's: not enforceable against an agent that has a
    shell, so it is a convenience for the owner, not a security control."""
    return Path.home() / ".config" / "hermes-pi" / "voice.disabled"
