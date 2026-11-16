from __future__ import annotations

import pytest

from memory_core.cross_rerank import order_scoped_value_updates

CORRECTIONS = [
    ("The stand-up is at 10:15 on Thursdays.", "The stand-up moved to 9:30 on Mondays."),
    ("Стендап проходит в 10:15 по четвергам.", "Стендап перенесли на 9:30 по понедельникам."),
    ("Dana's standup meeting is at 10:15 on Thursdays.", "Dana's stand-up meeting is now at 9:30 on Mondays."),
    ("Планёрка команды billing по четвергам в 10:15.", "Планёрка команды billing теперь по понедельникам в 9:30."),
    ("The release meeting is on Thursday at 10:15.", "The release meeting was rescheduled to Monday at 09:30."),
    ("Standup on Thursdays at 10:15", "Stand-up moved from Thursdays at 10:15 to Mondays at 9:30"),
    ("Стендап по четвергам в 10:15", "Стендап перенесли с четверга на понедельник в 09:30"),
    ("Standup at 10:15 on Thursday", "Standup now at 09:30 on Thursday"),
    ("Standup on Thursday at 09:30", "Standup now on Monday at 09:30"),
]

NOT_CORRECTIONS = [
    ("Alice's standup is on Thursdays at 10:15", "Bob's standup moved to Mondays at 9:30"),
    ("Стендап billing по четвергам в 10:15", "Стендап analytics перенесли на понедельник в 9:30"),
    ("The standup is on Thursday at 10:15", "The review moved to Monday at 9:30"),
    ("The standup is on Thursday at 10:15", "The standup might move to Monday at 9:30"),
    ("Стендап по четвергам в 10:15", "Стендап не перенесли на понедельник в 9:30"),
    ("Standup on Thursdays at 10:15", "Standup moved to next Monday at 9:30"),
    ("Standup on Thursdays at 10:15", "Did standup move to Mondays at 9:30?"),
    ("The standup is on Monday at 9:30", "The standup is now on Monday at 09:30"),
    ("Standup on Mondays at 9:30", "Standup on Thursdays at 10:15"),
    ("Standup on Thursdays at 10:15 in London", "Standup moved to Mondays at 9:30 in Berlin"),
]


def order(old, new, *, same_project=True, historical=False, new_first=False):
    records = [dict(content=old, time=1, project='alpha'),
               dict(content=new, time=2, project='alpha' if same_project else 'beta')]
    if new_first:
        records.reverse()
    return [item['content'] for item in order_scoped_value_updates(records,
        lambda item: item['content'], lambda item: (item['time'],), lambda item: item['project'],
        older_first=historical)]


@pytest.mark.parametrize(('old', 'new'), CORRECTIONS)
def test_explicit_schedule_correction_wins(old, new):
    assert order(old, new) == [new, old]


@pytest.mark.parametrize(('old', 'new'), NOT_CORRECTIONS)
def test_unrelated_or_unasserted_schedule_is_not_reordered(old, new):
    assert order(old, new) == [old, new]


@pytest.mark.parametrize(('old', 'new'), CORRECTIONS)
def test_schedule_order_preserves_project_and_history(old, new):
    assert order(old, new, same_project=False) == [old, new]
    assert order(old, new, historical=True, new_first=True) == [old, new]


@pytest.mark.parametrize(('old', 'new'), CORRECTIONS[:2])
def test_cross_encoder_cannot_promote_the_old_schedule(old, new):
    from memory_core.cross_rerank import CrossReranker

    class OldScheduleEncoder:
        def rerank(self, query, documents):
            return [float(document == old) for document in documents]

    encoder = CrossReranker('schedule-test', multilingual=True, window=2, weight=4,
        context_chars=0, loader=lambda _: OldScheduleEncoder())
    assert encoder.wait_ready(5)
    records = [(old, 1), (new, 2)]
    result = encoder.rerank('standup / стендап', records, lambda item: item[0],
        recency_of=lambda item: (item[1],), wait=False)
    assert result == records[::-1]
