"""The fast lane skips the agent, so its own boundaries have to hold.

Sending a spoken phrase straight to the laptop removes the one component that
was previously reading it and deciding what it meant. What is pinned here is
everything that stopped being someone else's problem the moment that happened:

  * A PHRASE THE ROOM CAN SAY BY ACCIDENT. The match must be the WHOLE
    utterance. "Don't pause the music" contains "pause the music", and a
    substring test -- which is the obvious implementation -- fires on it, on
    "can you play the song later", and on the television.

  * A COMMAND CLAIMED BUT NOT DELIVERED. /intent reports how many subscribers
    received the name. Zero means the laptop is not listening and nothing
    happened; saying "Playing." then is the panel's one rule broken out loud,
    just in speech instead of pixels.

  * A NAME THE FAR END WILL REJECT. /intent enforces [A-Za-z0-9_]{1,32}. A name
    that fails there is a command that silently does nothing, so the same shape
    is enforced before it is sent and on everything the owner's file adds.

  * A BROKEN CONFIG TAKING THE MICROPHONE DOWN. voice-commands.json is hand
    edited. Malformed JSON must cost you your own additions, not the service.

  * ONE-WORD COMMANDS. Not a style question: MEASURED, a bare "pause" is ~0.6 s
    of audio and whisper hallucinates on clips that short -- it came back as
    "toes". Every phrase must be at least two words.

No microphone, no models and no network required.

Run:  python3 tests/test_fastlane.py
"""

import json
import os
import sys
import tempfile
import pathlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice import fastlane                                   # noqa: E402


# -- the room cannot trigger it by talking ---------------------------------

def test_a_command_inside_a_sentence_does_not_fire():
    """The whole utterance, or nothing.

    Each of these CONTAINS an exact command phrase. A substring match -- the
    obvious way to write this -- fires on every one.
    """
    for said in ("dont pause the music",
                 "can you play the song later",
                 "i said next song not that one",
                 "he told me to open spotify yesterday",
                 "why does it always pause the music when i walk away"):
        assert fastlane.match(said) is None, f"fired on {said!r}"


def test_punctuation_and_case_do_not_stop_a_real_command():
    """Whisper capitalises and adds a full stop; that is not a different
    sentence and must not be treated as one."""
    for said in ("Pause the music.", "PAUSE THE MUSIC", "  pause  the music  "):
        hit = fastlane.match(said)
        assert hit is not None, f"missed {said!r}"
        assert hit[0] == "PAUSE"


def test_nothing_matches_empty_or_noise():
    for said in ("", "   ", None, ".", "mm"):
        assert fastlane.match(said) is None


# -- the vocabulary is closed and shaped for the far end -------------------

def test_every_intent_is_a_name_the_endpoint_will_accept():
    """/intent enforces [A-Za-z0-9_]{1,32}. A name it rejects is a command
    that silently does nothing."""
    for phrase, (intent, _reply) in fastlane.COMMANDS.items():
        assert fastlane._NAME.match(intent), f"{phrase!r} -> bad name {intent!r}"


def test_no_command_is_a_single_word():
    """MEASURED: a bare word is ~0.6 s of audio and whisper hallucinates on
    clips that short ("pause" -> "toes"). Two words is a floor, not taste."""
    for phrase in fastlane.COMMANDS:
        assert len(phrase.split()) >= 2, f"{phrase!r} is one word"


def test_phrases_are_already_normalised():
    """A key that normalise() would change can never be matched, because the
    lookup is done on the normalised transcript."""
    from voice import local
    for phrase in fastlane.COMMANDS:
        assert local.normalise(phrase) == phrase, f"{phrase!r} never matches"


# -- it does not claim things that did not happen --------------------------

class _Doc:
    def __init__(self, doc):
        self._b = json.dumps(doc).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _with_response(monkeypatched, doc):
    """Run fastlane.send with /intent answering `doc`."""
    import urllib.request
    real = urllib.request.urlopen
    fastlane._TOKEN = monkeypatched                  # a token that exists
    urllib.request.urlopen = lambda *a, **k: _Doc(doc)
    try:
        return fastlane.send("PLAY")
    finally:
        urllib.request.urlopen = real


def test_no_subscriber_is_reported_not_confirmed():
    """The music did not start. Saying "Playing." would be inventing state."""
    with tempfile.TemporaryDirectory() as d:
        tok = pathlib.Path(d) / "t"
        tok.write_text("abc123")
        ok, why = _with_response(tok, {"seq": 1, "action": "PLAY",
                                       "subscribers": 0})
    assert ok is False, "claimed success with nobody listening"
    assert why, "refused silently -- the owner is told nothing"
    assert "laptop" in why.lower()


def test_a_delivered_intent_reports_success():
    with tempfile.TemporaryDirectory() as d:
        tok = pathlib.Path(d) / "t"
        tok.write_text("abc123")
        ok, why = _with_response(tok, {"seq": 2, "action": "PLAY",
                                       "subscribers": 1})
    assert ok is True and why == ""


def test_a_missing_token_is_reported_not_swallowed():
    real = fastlane._TOKEN
    try:
        fastlane._TOKEN = pathlib.Path("/nonexistent/hermes/token")
        ok, why = fastlane.send("PLAY")
    finally:
        fastlane._TOKEN = real
    assert ok is False and why, "a missing token failed silently"


def test_an_unreachable_service_is_reported_not_swallowed():
    """hermes-camera being down must not look like a command that worked."""
    import urllib.request
    real_open, real_tok = urllib.request.urlopen, fastlane._TOKEN
    with tempfile.TemporaryDirectory() as d:
        tok = pathlib.Path(d) / "t"
        tok.write_text("abc123")
        try:
            fastlane._TOKEN = tok

            def boom(*a, **k):
                raise OSError("connection refused")
            urllib.request.urlopen = boom
            ok, why = fastlane.send("PLAY")
        finally:
            urllib.request.urlopen, fastlane._TOKEN = real_open, real_tok
    assert ok is False and why


# -- the owner's own file cannot break the microphone ----------------------

def test_a_broken_extra_file_costs_only_itself():
    real = fastlane.EXTRA_PATH
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "voice-commands.json"
        try:
            p.write_text("{ this is not json")
            fastlane.EXTRA_PATH = p
            assert fastlane.extra() == {}
            # ... and the built-ins still work.
            assert fastlane.match("pause the music") is not None
        finally:
            fastlane.EXTRA_PATH = real


def test_extra_entries_cannot_smuggle_a_name():
    """The owner's file names an intent; it does not get to invent a shape the
    endpoint will refuse, or one with room for punctuation in it."""
    real = fastlane.EXTRA_PATH
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "voice-commands.json"
        p.write_text(json.dumps({
            "play the album rumours": "ALBUM_RUMOURS",       # good
            "play something bad":     "rm -rf /",            # spaces, slashes
            "play something worse":   "A" * 40,              # too long
            "play something odd":     "ALBUM;DROP",          # punctuation
        }))
        try:
            fastlane.EXTRA_PATH = p
            got = fastlane.extra()
            assert fastlane.match("play the album rumours")[0] == "ALBUM_RUMOURS"
            assert fastlane.match("play something bad") is None
            assert fastlane.match("play something worse") is None
            assert fastlane.match("play something odd") is None
            assert len(got) == 1
        finally:
            fastlane.EXTRA_PATH = real


def test_extra_entries_are_normalised_like_everything_else():
    """A phrase typed with capitals and a question mark must still match what
    whisper produces, or it silently never fires."""
    real = fastlane.EXTRA_PATH
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "voice-commands.json"
        p.write_text(json.dumps({"Play The Album Kind Of Blue!": "ALBUM_KOB"}))
        try:
            fastlane.EXTRA_PATH = p
            assert fastlane.match("play the album kind of blue")[0] == "ALBUM_KOB"
        finally:
            fastlane.EXTRA_PATH = real


# -- the escape hatch keeps priority ---------------------------------------

def test_every_intent_has_a_binding_on_the_laptop():
    """THE FAILURE THIS FILE EXISTS FOR MOST.

    The two sides are deliberately decoupled -- the Pi publishes a name and the
    laptop decides what it means, ignoring anything it does not recognise. That
    is the security property, and it is also a silent failure mode: a name with
    no binding does nothing, reports nothing, and looks exactly like success at
    every point in between.

    Written first as PREV / VOL_UP / VOL_DOWN against a laptop that binds
    PREVIOUS / VOLUME_UP / VOLUME_DOWN, which is three of seven commands doing
    nothing at all. Shipping defaults that do not line up is not something to
    catch by hand twice.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    example = os.path.join(here, os.pardir, "clients", "windows",
                           "gestures.example.json")
    bound = {k.split(" ", 1)[1]
             for k in json.load(open(example))["bindings"]
             if k.startswith("HERMES ")}
    want = {intent for intent, _ in fastlane.COMMANDS.values()}
    missing = want - bound
    assert not missing, (
        f"the voice fast lane publishes {sorted(missing)}, which the shipped "
        f"laptop config does not bind -- those commands would silently do "
        f"nothing. Bound: {sorted(bound)}")


def test_the_fast_lane_does_not_shadow_the_escape_hatch():
    """local.py's phrases must never also be fast-lane phrases: the escape
    hatch has to work with the network gone, and the fast lane needs it."""
    from voice import local
    clash = set(local.COMMANDS) & set(fastlane.COMMANDS)
    assert not clash, f"the escape hatch would be sent to the laptop: {clash}"


def _run() -> int:
    fails = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as e:
            fails += 1
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{'ALL PASS' if not fails else f'{fails} FAILURES'}")
    return fails


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
