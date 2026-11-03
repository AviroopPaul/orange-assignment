"""Pure rules for memory: whose words a fact rests on, and which facts matter now."""

import re
from dataclasses import dataclass
from datetime import datetime

from app.domains.memory.models import ALL_SCOPES

_ASKS_TO_FORGET = re.compile(
    r"\b(forget|delete|remove|erase|wipe|stop remembering|(don'?t|do not) (remember|keep))\b",
    re.IGNORECASE,
)


_SUFFIXES = ("ing", "ed", "es", "e", "s")
# Words a fair description adds around what the user said. They prove nothing either way.
_FILLER = frozenset(
    "a an the and or of to in on at for with is are am was be i my me "
    "user customer they them their he she his her".split()
)
_APOSTROPHE = re.compile("['\u2019]")
_YES = frozenset(
    "yes|yeah|yep|sure|ok|okay|yes please|please do|remember|remember that|remember it|"
    "remember this|remember them|remember both|yes remember|yes remember that|yes remember it|"
    "please remember that|please remember it|save|save it|save that|save this|yes save it|"
    "yes save that|please save it|please save that".split("|")
)
_NO = frozenset(
    "no|nope|no thanks|no thank you|not now|skip|skip it|dont|dont remember|dont remember that|"
    "dont remember it|do not remember that|dont save|dont save it|dont save that|"
    "do not save it|do not save that|forget it|forget that".split("|")
)


@dataclass(frozen=True)
class Fact:
    key: str
    value: str
    scope: str
    updated_at: datetime


def _flat(text: str) -> str:
    return " ".join(text.casefold().split())


def is_explicit(value: str, quote: str, user_messages: list[str]) -> bool:
    """The quote is the user's, word for word, and it really says this value.

    A review cannot pass, and neither can a real but unrelated sentence of the user's. A word's
    ending does not matter, so "goes to office" still rests on "I go to office".
    """
    flat_quote = _flat(quote)
    if not flat_quote or not any(flat_quote in _flat(message) for message in user_messages):
        return False
    words = {_stem(word) for word in re.findall(r"\w+", value.casefold()) if word not in _FILLER}
    return bool(words) and words <= {_stem(word) for word in re.findall(r"\w+", flat_quote)}


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 2:
            return word[: -len(suffix)]
    return word


def read_memory_answer(message: str) -> bool | None:
    """A whole-message answer to "remember this?". Anything longer goes to the model."""
    text = " ".join(_APOSTROPHE.sub("", message.casefold()).replace(",", " ").split())
    text = text.strip(" .!?")
    if text in _YES:
        return True
    return False if text in _NO else None


def asks_to_forget(message: str) -> bool:
    return bool(_ASKS_TO_FORGET.search(message))


def _mentions(fact: Fact, message_words: set[str]) -> bool:
    words = re.findall(r"[a-z0-9]+", f"{fact.key} {fact.scope}".casefold())
    return any(word in message_words for word in words if word != ALL_SCOPES)


def rank(facts: list[Fact], service: str | None, message: str, limit: int) -> list[Fact]:
    """The same input always gives the same order, so evals are reproducible offline.

    A fact about another service is left out. With no service known yet, a service's fact counts
    only if the message mentions it.
    """
    message_words = set(re.findall(r"[a-z0-9]+", message.casefold()))
    scored = []
    for fact in facts:
        mentioned = _mentions(fact, message_words)
        if fact.scope == ALL_SCOPES:
            score = 1
        elif fact.scope == service or (service is None and mentioned):
            score = 3
        else:
            continue
        scored.append((-(score + 2 * mentioned), -fact.updated_at.timestamp(), fact.key, fact))
    return [item[-1] for item in sorted(scored, key=lambda item: item[:3])][:limit]
