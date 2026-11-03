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
        "book",
        "book it",
        "yes book it",
        "yes confirm",
        "please book",
        "please book it",
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
        "dont book",
        "dont book it",
        "do not book",
        "do not book it",
    }
)

_APOSTROPHE = re.compile("['\u2019]")
_EDGE_PUNCTUATION = re.compile(r"^[\s.,!?]+|[\s.,!?]+$")
_SPACES = re.compile(r"\s+")
_INNER_COMMA = re.compile(r",\s*")


def read_decision(message: str) -> Decision | None:
    text = _EDGE_PUNCTUATION.sub("", _APOSTROPHE.sub("", message.casefold()))
    text = _SPACES.sub(" ", _INNER_COMMA.sub(" ", text))
    if text in _YES:
        return Decision.YES
    if text in _NO:
        return Decision.NO
    return None


# A statement that something has happened. "Not booked" and "before it is booked" do not match.
_DONE_WORDS = "booked|confirmed|reserved|scheduled|cancelled|canceled|rescheduled"
_CLAIM = re.compile(
    rf"\b(?:has been|have been|is|are|was|were|now|successfully|you're|you are all)\s+"
    rf"(?:all\s+|now\s+|successfully\s+)?(?:{_DONE_WORDS})\b(?!\s+by\b)"
    rf"|\bI(?:'ve|\s+have)?\s+(?:just\s+|now\s+|successfully\s+)?"
    r"(?:booked|reserved|scheduled|cancelled|canceled|rescheduled)\b"
    r"|\b(?:booking|appointment|reservation)\s+(?:is|was|has been)\s+"
    r"(?:complete|successful|done)\b",
    re.IGNORECASE,
)


# Earlier in the same sentence, these turn a statement into a denial or a condition.
_NOT_A_CLAIM = re.compile(
    r"\b(?:nothing|not|no|never|neither|until|unless|once|before|after|if|when)\b", re.IGNORECASE
)
_SENTENCE_END = re.compile(r"[.!?\n]")


def claims_side_effect(text: str) -> bool:
    for match in _CLAIM.finditer(text):
        before = _SENTENCE_END.split(text[: match.start()])[-1]
        if not _NOT_A_CLAIM.search(before):
            return True
    return False
