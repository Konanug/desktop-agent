"""Nothing leaves this house without the owner typing a phrase.

The owner's rule: never send an email without direct permission; the
permission must be TYPED, never spoken, and must be a specific phrase with
correct syntax.

Right now no send tool exists and the OAuth scopes are readonly, so sending is
IMPOSSIBLE rather than merely disallowed -- verified against Google, which
refuses with HTTP 403. These tests exist so the rule is already enforced if
that ever changes, instead of being a note somebody has to remember at the
moment they are adding a feature.

The load-bearing test is test_the_voice_lane_cannot_see_a_send_tool. A tool
handler CANNOT tell which platform invoked it -- the registry does not pass the
platform down -- so "refuse if spoken" cannot be implemented as a runtime
check. It has to be structural: the toolset is absent from the voice lane, so
there is nothing to call.

Run:  python3 tests/test_send_consent.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "hermes_ext", "plugins"))

from hermes_google import consent                            # noqa: E402
from hermes_google.consent import ConsentError               # noqa: E402

PHRASE = "opal-lantern-77"


def _with_phrase(text=PHRASE):
    tmp = Path(tempfile.mkdtemp()) / "phrase"
    tmp.write_text(text)
    consent.PHRASE_PATH = tmp
    return tmp


def _raises(fn, *a, **kw) -> str:
    try:
        fn(*a, **kw)
    except ConsentError as e:
        return str(e)
    raise AssertionError("expected a refusal, got none")


def test_correct_phrase_and_recipient_is_accepted():
    _with_phrase()
    consent.check(f"CONFIRM SEND TO alice@example.com {PHRASE}",
                  "alice@example.com")


def test_missing_phrase_file_refuses_everything():
    """FAILS CLOSED. An unconfigured guard must block, not wave through."""
    consent.PHRASE_PATH = Path("/nonexistent/never/phrase")
    msg = _raises(consent.check, f"CONFIRM SEND TO a@b.c {PHRASE}", "a@b.c")
    assert "no send-consent phrase" in msg.lower()


def test_wrong_phrase_refuses():
    _with_phrase()
    _raises(consent.check, "CONFIRM SEND TO a@b.c not-the-phrase", "a@b.c")


def test_bare_phrase_without_the_syntax_refuses():
    """'Specific phrase with correct syntax'. The phrase alone is not consent
    -- a model that has seen it once could otherwise emit it at will."""
    _with_phrase()
    _raises(consent.check, PHRASE, "a@b.c")
    _raises(consent.check, f"please send it, {PHRASE}", "a@b.c")
    _raises(consent.check, f"CONFIRM SEND {PHRASE}", "a@b.c")   # missing TO


def test_consent_is_bound_to_ONE_recipient():
    """THE REPLAY PROPERTY. A bare passphrase, once said, would authorise every
    later send in the conversation -- including to an address the owner never
    approved."""
    _with_phrase()
    msg = _raises(consent.check,
                  f"CONFIRM SEND TO alice@example.com {PHRASE}",
                  "mallory@example.com")
    assert "per recipient" in msg.lower()


def test_empty_and_none_confirmations_refuse():
    _with_phrase()
    for bad in ("", "   ", None):
        _raises(consent.check, bad, "a@b.c")


def test_a_too_short_phrase_is_rejected_as_configuration():
    _with_phrase("abc")
    msg = _raises(consent.check, "CONFIRM SEND TO a@b.c abc", "a@b.c")
    assert "too short" in msg.lower()


def test_check_raises_rather_than_returning_false():
    """A boolean would fail OPEN under the most likely mistake -- a caller that
    forgets to test the return value still must not be able to send."""
    _with_phrase()
    assert consent.check(f"CONFIRM SEND TO a@b.c {PHRASE}", "a@b.c") is None


# -- the structural layer -------------------------------------------------
def test_nothing_in_the_plugin_can_send_today():
    """The strongest guarantee is that the capability does not exist."""
    import hermes_google.tools as t
    src = Path(t.__file__).read_text()
    for forbidden in ("messages().send", "drafts()", "sendAs", "smtp"):
        assert forbidden not in src, f"a send path appeared: {forbidden}"


def test_scopes_are_readonly():
    """Enforced by Google, not by us: a readonly token is refused server-side
    for a write call whatever the agent asks."""
    from hermes_google.google_client import SCOPES
    assert SCOPES and all(s.endswith(".readonly") for s in SCOPES), SCOPES


def test_the_voice_lane_cannot_see_a_send_tool():
    """THE ONE THAT MAKES 'TYPED, NOT SPOKEN' REAL.

    A handler cannot tell voice from typed -- the registry does not pass the
    platform to tools -- so this cannot be a runtime check. It has to be that
    the voice lane's toolset does not contain the send toolset at all, and
    therefore the tool is not in its surface to be called.

    Asserted against the LIVE config, so it fails if someone widens the voice
    lane later without thinking about this.
    """
    import yaml
    cfg_path = Path.home() / ".hermes/config.yaml"
    if not cfg_path.exists():
        return                              # not this machine; nothing to check
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    webhook = (cfg.get("platform_toolsets") or {}).get("webhook")
    if webhook is None:
        return
    assert "hermes_google_send" not in webhook, (
        "the voice lane has been given a send toolset -- spoken requests could "
        "then send email, which the owner has forbidden")


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
