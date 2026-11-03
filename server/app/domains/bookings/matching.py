"""Reads a user's whole message as a yes or a no. Anything else is not an answer.

"Yes, but Saturday" is not a yes. It goes to the model as a change. These checks run on the raw
message before the model, so nothing the model writes can count as approval.
"""

import re
from enum import StrEnum


class Decision(StrEnum):
    YES = "yes"
    NO = "no"


_YES = frozenset(
    {
        "yes",
        "y",
        "yep",
        "yeah",
        "yes please",
        "ok",
        "okay",
        "sure",
        "confirm",
        "confirmed",
        "i confirm",
        "approve",
        "approved",
        "i approve",
        "yes confirm",
        "go ahead",
        "yes go ahead",
        "proceed",
        "do it",
    }
)
_NO = frozenset(
    {
        "no",
        "n",
        "nope",
        "no thanks",
        "no thank you",
        "not now",
        "decline",
        "reject",
    }
)
# Words that name an action count only on a card for that action. "Book it" must not approve a
# cancel. A bare "cancel" is not here: on any card, it could mean "close this".
_YES_FOR = {
    "book": frozenset(
        {"book", "book it", "yes book it", "please book", "please book it"},
    ),
    "cancel": frozenset(
        {"cancel it", "yes cancel it", "yes cancel", "please cancel it", "cancel the booking"},
    ),
    "reschedule": frozenset(
        {"move it", "yes move it", "please move it", "reschedule it", "yes reschedule it"},
    ),
}
_NO_FOR = {
    "book": frozenset({"dont book", "dont book it", "do not book", "do not book it"}),
    "cancel": frozenset({"dont cancel", "dont cancel it", "do not cancel", "do not cancel it"})
    | {"keep it", "keep the booking"},
    "reschedule": frozenset({"dont move it", "do not move it", "dont reschedule"})
    | {"do not reschedule", "keep it", "keep the booking"},
}

_APOSTROPHE = re.compile("['\u2019]")
_EDGE_PUNCTUATION = re.compile(r"^[\s.,!?]+|[\s.,!?]+$")
_SPACES = re.compile(r"\s+")
_INNER_COMMA = re.compile(r",\s*")


def read_decision(message: str, action: str | None = None) -> Decision | None:
    """`action` is what the open card is about. With no card, any action's words count."""
    text = _EDGE_PUNCTUATION.sub("", _APOSTROPHE.sub("", message.casefold()))
    text = _SPACES.sub(" ", _INNER_COMMA.sub(" ", text))
    actions = [action] if action else list(_YES_FOR)
    if text in _YES or any(text in _YES_FOR[name] for name in actions):
        return Decision.YES
    if text in _NO or any(text in _NO_FOR[name] for name in actions):
        return Decision.NO
    return None


# A statement that something has happened. "Not booked" and "before it is booked" do not match.
_DONE_WORDS = "booked|confirmed|reserved|scheduled|cancelled|canceled|rescheduled|moved"
_CLAIM = re.compile(
    rf"\b(?:has been|have been|is|are|was|were|now|successfully|you're|you are all)\s+"
    rf"(?:all\s+|now\s+|successfully\s+)?(?:{_DONE_WORDS})\b(?!\s+by\b)"
    rf"|\bI(?:'ve|\s+have)?\s+(?:just\s+|now\s+|successfully\s+)?"
    r"(?:booked|reserved|scheduled|cancelled|canceled|rescheduled|moved)\b"
    r"|\b(?:booking|appointment|reservation)\s+(?:is|was|has been)\s+"
    r"(?:complete|successful|done)\b",
    re.IGNORECASE,
)


# Earlier in the same sentence, these turn a statement into a denial or a condition.
_NOT_A_CLAIM = re.compile(
    r"\b(?:nothing|not|no|never|neither|until|unless|once|before|after|if|when)\b", re.IGNORECASE
)
_SENTENCE_END = re.compile(r"[.!?\n]")


def claimed_actions(text: str) -> set[str]:
    """Which actions this text says have happened. Being booked says nothing about a cancel."""
    found: set[str] = set()
    for match in _CLAIM.finditer(text):
        before = _SENTENCE_END.split(text[: match.start()])[-1]
        if _NOT_A_CLAIM.search(before):
            continue
        said = match.group(0).casefold()
        if "cancel" in said:
            found.add("cancel")
        elif "rescheduled" in said or "moved" in said:
            found.add("reschedule")
        else:
            found.add("book")
    return found


def claims_side_effect(text: str) -> bool:
    return bool(claimed_actions(text))
