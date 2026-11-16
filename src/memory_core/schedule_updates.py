from __future__ import annotations

import re
from dataclasses import dataclass

from memory_core.dedup import value_tokens

_DAY_NAMES = (
    ('monday', 'mondays', 'понедельник', 'понедельника', 'понедельникам', 'понедельники'),
    ('tuesday', 'tuesdays', 'вторник', 'вторника', 'вторникам', 'вторники'),
    ('wednesday', 'wednesdays', 'среда', 'среду', 'среды', 'средам'),
    ('thursday', 'thursdays', 'четверг', 'четверга', 'четвергам', 'четверги'),
    ('friday', 'fridays', 'пятница', 'пятницу', 'пятницы', 'пятницам'),
    ('saturday', 'saturdays', 'суббота', 'субботу', 'субботы', 'субботам'),
    ('sunday', 'sundays', 'воскресенье', 'воскресенья', 'воскресеньям'),
)
_DAYS = {name: index for index, names in enumerate(_DAY_NAMES) for name in names}
_TIME = re.compile(r'(\d{1,2})[:.](\d{2})(am|pm)?$|(?:(\d{1,2})(am|pm))$')
_HOURS_PER_HALF_DAY = 12
_HOURS_PER_DAY = 24
_MINUTES_PER_HOUR = 60
_CORRECTION_WORDS = frozenset({
    'moved', 'rescheduled', 'shifted', 'changed', 'now',
    'перенесли', 'перенесен', 'перенесена', 'перенесено', 'перенесены', 'переносится',
    'сдвинули', 'теперь',
})
_GRAMMAR = frozenset({
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'at', 'on', 'to', 'from',
    'in', 'every', 'each', 'scheduled', 'happens', 'held', 'time',
    'в', 'во', 'на', 'по', 'с', 'со', 'к', 'проходит', 'проводится', 'состоится',
    'был', 'была', 'было', 'будет', 'каждый', 'каждую', 'каждые', 'время',
}) | _CORRECTION_WORDS
_UNASSERTED = frozenset({
    'not', 'no', 'never', 'might', 'may', 'could', 'would', 'should', 'if', 'perhaps', 'maybe',
    'не', 'нет', 'никогда', 'может', 'могли', 'возможно', 'если', 'ли', 'хотели',
})
_STANDUP = frozenset({'standup', 'stand-up', 'стендап', 'стендапа', 'стендапу', 'стендапе'})


@dataclass(frozen=True)
class ScheduleStatement:
    subject: tuple[str, ...]
    days: frozenset[int]
    times: frozenset[int]
    correction: bool
    asserted: bool


def _time(token: str) -> int | None:
    match = _TIME.fullmatch(token)
    if match is None:
        return None
    hour = int(match[1] or match[4])
    minute = int(match[2] or 0)
    meridiem = match[3] or match[5]
    if minute >= _MINUTES_PER_HOUR:
        return None
    if meridiem:
        if not 1 <= hour <= _HOURS_PER_HALF_DAY:
            return None
        hour = hour % _HOURS_PER_HALF_DAY + (_HOURS_PER_HALF_DAY if meridiem == 'pm' else 0)
    if hour >= _HOURS_PER_DAY:
        return None
    return hour * _MINUTES_PER_HOUR + minute


def schedule_statement(text: str) -> ScheduleStatement | None:
    tokens = value_tokens(text)
    days = frozenset(_DAYS[token] for token in tokens if token in _DAYS)
    times = frozenset(value for token in tokens if (value := _time(token)) is not None)
    if not days and not times:
        return None
    subject = ['standup' if token in _STANDUP else token for token in tokens
        if token not in _DAYS and _time(token) is None and token not in _GRAMMAR]
    if 'standup' in subject:
        subject = [token for token in subject if token != 'meeting']
    if not subject:
        return None
    return ScheduleStatement(tuple(sorted(subject)), days, times,
        bool(set(tokens) & _CORRECTION_WORDS), '?' not in text and not set(tokens) & _UNASSERTED)


def schedule_update(a: ScheduleStatement | None, b: ScheduleStatement | None,
                    lexical_update: bool) -> bool:
    if a is None or b is None:
        return lexical_update
    if not a.asserted or not b.asserted or a.subject != b.subject:
        return False
    changed = (bool(a.days and b.days) and a.days != b.days
               or bool(a.times and b.times) and a.times != b.times)
    return bool(changed and (a.correction or b.correction or lexical_update))
