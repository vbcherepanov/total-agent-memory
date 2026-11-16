import json

import pytest

from evaluation.locomo import load_dataset


def sample():
    return {'sample_id': 'conversation-a', 'conversation': {
        'speaker_a': 'A', 'speaker_b': 'B', 'session_1_date_time': '2026-11-15',
        'session_1': [{'dia_id': 'D1:1', 'speaker': 'A', 'text': 'The ledger is in Paris.'}],
    }, 'qa': [{'question': 'Where is the ledger?', 'answer': 'Paris', 'category': 1,
               'evidence': ['D1:1']}]}


def test_load_real_locomo_shape_and_evidence(tmp_path):
    path = tmp_path / 'locomo.json'
    path.write_text(json.dumps([sample()]), encoding="utf-8")
    dataset = load_dataset(path)
    assert len(dataset) == 1
    assert dataset[0].turns[0].dialogue_id == 'D1:1'
    assert dataset[0].questions[0].evidence == ('D1:1',)
    assert dataset[0].turns[0].session_date == '2026-11-15'


@pytest.mark.parametrize('payload', [[], {}, [42], [{'query': 'fake scenario'}]])
def test_rejects_non_locomo_dataset(tmp_path, payload):
    path = tmp_path / 'invalid.json'
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match='LoCoMo'):
        load_dataset(path)


def test_missing_locomo_dataset_is_explicit(tmp_path):
    with pytest.raises(ValueError, match='LoCoMo.*dataset'):
        load_dataset(tmp_path / 'missing.json')


def test_duplicate_dialogue_ids_are_invalid(tmp_path):
    data = sample()
    data['conversation']['session_1'] *= 2
    path = tmp_path / 'duplicate.json'
    path.write_text(json.dumps([data]), encoding="utf-8")
    with pytest.raises(ValueError, match='duplicate'):
        load_dataset(path)


def test_adversarial_and_missing_evidence_are_retained(tmp_path):
    data = sample()
    data['qa'].append({'question': 'What is the password?', 'category': 5, 'evidence': []})
    path = tmp_path / 'locomo.json'
    path.write_text(json.dumps([data]), encoding="utf-8")
    dataset = load_dataset(path)
    assert dataset[0].questions[1].category == 5
    assert dataset[0].questions[1].evidence == ()
