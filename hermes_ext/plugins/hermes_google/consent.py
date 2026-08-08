"""Typed, per-recipient consent for anything that leaves this house.

THE RULE, as set by the owner:
    Never send an email without direct permission. The permission must be
    TYPED, never spoken, and must be a specific phrase with correct syntax.

Nothing here can currently send anything -- the OAuth scopes are `.readonly`
and Google refuses a send with HTTP 403 (verified, not assumed). This module
exists so that the rule is ALREADY ENFORCED if send capability is ever added,
rather than being a note somebody has to remember at that moment.

THREE LAYERS, WEAKEST LAST

  1. SCOPE. A readonly token cannot send. Enforced by Google, not by us, and
     unaffected by anything the agent decides. Adding send means a new consent
     screen, which is itself a typed act by the owner at a Google login.

  2. STRUCTURE -- this is the one that makes "typed, not spoken" real. A tool
     handler CANNOT tell which platform invoked it: the registry does not pass
     the platform down, so any runtime check of "did this come from voice"
     would be a guess dressed as a control. Instead, a send tool must live in
     its own toolset that is absent from `platform_toolsets.webhook`. The voice
     lane then cannot see the tool at all -- there is nothing to refuse,
     because there is nothing to call. Narrowing was verified to work in both
     directions (docs/SECURITY.md), so this is a mechanism and not a hope.

  3. PHRASE. The exact phrase below, naming the exact recipient. This is the
     layer a person actually experiences, and it is deliberately last: a model
     asked to require a phrase is a request, whereas a toolset it cannot see is
     a boundary.

WHY THE RECIPIENT IS PART OF THE PHRASE
A bare passphrase can be reused. If it appears once in a conversation, every
later send in that conversation is already authorised, including to a different
address the owner never approved. Binding it to the recipient means consent
authorises ONE delivery to ONE person and cannot be carried sideways.
"""

from __future__ import annotations

import hmac
import re
from pathlib import Path

# The owner's phrase. Absent = nothing may be sent, ever. Fails CLOSED.
PHRASE_PATH = Path.home() / ".config" / "hermes-pi" / "send-consent-phrase"

# Required syntax, checked exactly:
#     CONFIRM SEND TO <recipient> <phrase>
SYNTAX = "CONFIRM SEND TO <recipient> <your phrase>"
_PATTERN = re.compile(r"^CONFIRM\s+SEND\s+TO\s+(\S+)\s+(.+)$", re.DOTALL)


class ConsentError(RuntimeError):
    """Message is meant to be shown to the owner, verbatim."""


def _phrase() -> str:
    try:
        p = PHRASE_PATH.read_text().strip()
    except OSError:
        raise ConsentError(
            "No send-consent phrase is configured, so nothing can be sent. "
            f"The owner must create {PHRASE_PATH} containing a phrase only "
            "they know.") from None
    if len(p) < 8:
        raise ConsentError(
            "The configured send-consent phrase is too short to be meaningful "
            "(needs 8+ characters). Nothing sent.")
    return p


def check(confirmation: str, recipient: str) -> None:
    """Raise ConsentError unless `confirmation` authorises THIS recipient.

    Returns None on success and raises otherwise, so a caller that forgets to
    check a return value still cannot send. A boolean would fail open under
    exactly the mistake most likely to be made.
    """
    want = _phrase()
    m = _PATTERN.match((confirmation or "").strip())
    if not m:
        raise ConsentError(
            f"Refused: the confirmation must be typed in exactly this form:\n"
            f"    {SYNTAX}\n"
            f"Nothing was sent. This phrase must be TYPED by the owner -- a "
            f"spoken request cannot authorise sending.")
    said_to, said_phrase = m.group(1).strip(), m.group(2).strip()

    # Constant-time: the phrase is a secret, and a timing-distinguishable
    # comparison would leak it a character at a time to anything that can
    # retry.
    if not hmac.compare_digest(said_phrase, want):
        raise ConsentError(
            "Refused: the confirmation phrase does not match. Nothing sent.")
    if said_to.lower() != (recipient or "").strip().lower():
        raise ConsentError(
            f"Refused: the confirmation authorises sending to {said_to!r}, but "
            f"the message is addressed to {recipient!r}. Consent is per "
            f"recipient and cannot be carried across. Nothing sent.")
