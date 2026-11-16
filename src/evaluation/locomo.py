from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

SESSION_KEY = re.compile(r'session_(\d+)$')
CATEGORIES = frozenset({1, 2, 3, 4, 5})


@dataclass(frozen=True)
class DialogueTurn:
    dialogue_id: str
    speaker: str
    text: str
    session: str
    session_date: str
    caption: str


@dataclass(frozen=True)
class Question:
    text: str
    category: int
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class Conversation:
    sample_id: str
    turns: tuple[DialogueTurn, ...]
    questions: tuple[Question, ...]


def _text(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f'LoCoMo {field} must be text' + ('' if allow_empty else ' and non-empty'))
    return value


def _turns(conversation: dict[str, object]) -> tuple[DialogueTurn, ...]:
    sessions = sorted((key for key in conversation if SESSION_KEY.fullmatch(key)),
                      key=lambda key: int(key.split('_')[1]))
    turns: list[DialogueTurn] = []
    seen: set[str] = set()
    for session in sessions:
        records = conversation[session]
        if not isinstance(records, list):
            raise ValueError(f'LoCoMo {session} must contain a list of turns')
        date = _text(conversation.get(f'{session}_date_time', ''), 'session date', allow_empty=True)
        for record in records:
            if not isinstance(record, dict):
                raise ValueError('LoCoMo turn must be an object')
            identity = _text(record.get('dia_id'), 'dialogue ID')
            if identity in seen:
                raise ValueError(f'LoCoMo duplicate dialogue ID: {identity}')
            seen.add(identity)
            turns.append(DialogueTurn(
                identity, _text(record.get('speaker'), 'speaker'), _text(record.get('text'), 'turn text'),
                session, date, _text(record.get('blip_caption', ''), 'caption', allow_empty=True)))
    if not turns:
        raise ValueError('LoCoMo conversation has no dialogue turns')
    return tuple(turns)


def _questions(records: object) -> tuple[Question, ...]:
    if not isinstance(records, list) or not records:
        raise ValueError('LoCoMo conversation must contain non-empty qa')
    questions: list[Question] = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError('LoCoMo question must be an object')
        category = record.get('category')
        if type(category) is not int or category not in CATEGORIES:
            raise ValueError('LoCoMo question category must be an integer from 1 to 5')
        evidence = record.get('evidence', [])
        if not isinstance(evidence, list):
            raise ValueError('LoCoMo evidence must be a list of dialogue IDs')
        questions.append(Question(_text(record.get('question'), 'question'), category,
                                  tuple(_text(item, 'evidence ID') for item in evidence)))
    return tuple(questions)


def load_dataset(path: str | Path) -> tuple[Conversation, ...]:
    try:
        raw = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError(f'Cannot read LoCoMo dataset {path}: {error}') from error
    if not isinstance(raw, list) or not raw:
        raise ValueError('LoCoMo dataset must be a non-empty list of conversations')
    conversations: list[Conversation] = []
    identities: set[str] = set()
    for index, record in enumerate(raw):
        if not isinstance(record, dict) or not isinstance(record.get('conversation'), dict):
            raise ValueError(f'LoCoMo sample {index + 1} must contain a conversation object')
        identity = _text(record.get('sample_id', f'conversation-{index}'), 'sample ID')
        if identity in identities:
            raise ValueError(f'LoCoMo duplicate sample ID: {identity}')
        identities.add(identity)
        conversations.append(Conversation(identity, _turns(record['conversation']), _questions(record.get('qa'))))
    return tuple(conversations)
