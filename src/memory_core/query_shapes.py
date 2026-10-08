"""Query shapes that change how `memory_recall` searches.

Two cheap, regex-only classifiers (English and Russian), used by the
`directives` and `multi_query` tiers of the recall pipeline:

* an *advice* query asks what to do or what to pick ("how should I...",
  "recommend...", "как лучше..."); conventions and preferences of the
  project answer it even when they share no words with the question;
* an *ordering or comparison* query names several things and asks about
  their order or difference ("did X happen before Y", "X or Y", "X vs Y");
  each side is searched on its own, because one combined query ranks
  records that mention both sides, which rarely exist.
"""

from __future__ import annotations

import re

from memory_core.query_terms import lexical_terms

MAX_SUB_QUERIES = 3
MIN_SUB_QUERY_TERMS = 1

_ADVICE = re.compile(
    r"\b("
    r"recommend(?:ation)?s?|suggest(?:ion)?s?|advi[cs]e|"
    r"should (?:i|we|one|you)|how (?:should|do|can) (?:i|we)|"
    r"what(?:'s| is) the (?:best|right|proper|usual|standard|preferred|recommended)|"
    r"best (?:way|practice|option|choice)|"
    r"which (?:one |\w+ )?(?:should|to) (?:use|pick|choose|prefer)|"
    r"convention|guideline|prefer(?:red|ence)?s?|rule of thumb|"
    r"стоит ли|как (?:лучше|правильно|принято|нужно)|что (?:лучше|выбрать|использовать)|"
    r"посоветуй|порекомендуй|рекоменд\w*|какой (?:выбрать|использовать)|по какому (?:правилу|принципу)|"
    r"конвенци\w*|предпочт\w*"
    r")\b",
    re.IGNORECASE,
)

_ORDERING_CUE = re.compile(
    r"\b("
    r"first|last|earlier|later|before|after|order|sequence|which came|happened first|"
    r"vs\.?|versus|compared? (?:to|with)|difference between|or|"
    r"сначала|раньше|позже|до|после|порядок|последовательност\w*|что было (?:первым|раньше)|"
    r"или|сравни\w*|в отличие от|разниц\w* между"
    r")\b",
    re.IGNORECASE,
)

_SPLIT = re.compile(
    r"\s*(?:,|;|:|\?|"
    r"\b(?:or|vs\.?|versus|before|after|earlier than|later than|compared (?:to|with)|and then|then|"
    r"или|до того как|после того как|до|после|раньше чем|позже чем|а потом|потом|и затем)\b"
    r")\s*",
    re.IGNORECASE,
)

_LEADING_FILLER = re.compile(
    r"^(?:(?:which|what|who|when|did|was|were|is|are|does|do|had|has|how)\s+)+"
    r"|^(?:(?:что|кто|когда|какой|какая|какое|какие|было|был|была|были)\s+)+",
    re.IGNORECASE,
)

_TRAILING_FILLER = re.compile(
    r"\s+(?:happen(?:ed)?|come|came|occur(?:red)?|first|last|earlier|later|произошл[оаи]|случил[оаи]сь|сначала)\s*$",
    re.IGNORECASE,
)


def is_advice_query(query: str) -> bool:
    """True when the query asks for a recommendation, rule or preferred choice."""
    return bool(query) and _ADVICE.search(query) is not None


_FILLER_TERMS = frozenset({
    "happened", "happen", "came", "come", "occurred", "occur", "first", "last", "earlier", "later",
    "раньше", "позже", "сначала", "было", "произошло", "случилось", "первым", "потом",
})


def _informative(part: str) -> bool:
    terms = lexical_terms(part)
    return len(terms) >= MIN_SUB_QUERY_TERMS and any(term.lower() not in _FILLER_TERMS for term in terms)


def sub_queries(query: str) -> list[str]:
    """Split an ordering/comparison question into the things it compares.

    Returns an empty list when the query has no ordering cue or fewer than
    two parts with at least MIN_SUB_QUERY_TERMS lexical terms each; the
    caller then runs the plain pipeline only.
    """
    if not query or _ORDERING_CUE.search(query) is None:
        return []
    parts: list[str] = []
    for raw in _SPLIT.split(query):
        part = _TRAILING_FILLER.sub("", _LEADING_FILLER.sub("", (raw or "").strip())).strip(" .!")
        if _informative(part) and part.lower() not in {p.lower() for p in parts}:
            parts.append(part)
    if len(parts) < 2:
        return []
    return parts[:MAX_SUB_QUERIES]


_USER_TURN = re.compile(r"^\s*(?:\[[^\]]*\]\s*)?(?:user|human|пользователь)\s*:", re.IGNORECASE)


def is_user_turn(content: str) -> bool:
    """True for a conversation record spoken by the user ("[date] user: ...")."""
    return bool(content) and _USER_TURN.match(content) is not None
