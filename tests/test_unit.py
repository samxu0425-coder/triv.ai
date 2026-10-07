"""Unit tests: pure logic in app.py, no HTTP and no real AI calls."""
import io
import json

import pytest
from werkzeug.datastructures import FileStorage

from conftest import FakeAIClient


# ── Speed-based scoring ────────────────────────────────────────────────────

@pytest.mark.parametrize('elapsed, expected', [
    (0, 1000),     # instant answer = max
    (5, 800),
    (10, 600),     # halfway = midpoint
    (15, 400),
    (20, 200),     # at the buzzer = min
])
def test_speed_points_is_linear_from_1000_to_200(trivia, elapsed, expected):
    assert trivia.speed_points(elapsed) == expected


def test_speed_points_never_drops_below_minimum(trivia):
    assert trivia.speed_points(25) == 200
    assert trivia.speed_points(10_000) == 200


def test_speed_points_never_exceeds_maximum(trivia):
    # Clock skew could make elapsed slightly negative
    assert trivia.speed_points(-3) == 1000


def test_speed_points_strictly_decrease_over_time(trivia):
    points = [trivia.speed_points(t) for t in range(0, 21)]
    assert points == sorted(points, reverse=True)
    assert len(set(points)) == len(points)


@pytest.mark.parametrize('elapsed, q_time, expected', [
    (0, 60, 1000),
    (30, 60, 600),     # halfway through a long question = midpoint
    (5, 10, 600),      # halfway through a short question = midpoint
    (10, 10, 200),
    (20, 60, 733),     # 1/3 of the way through a 60s question
])
def test_speed_points_scale_to_the_questions_time_limit(trivia, elapsed, q_time, expected):
    assert trivia.speed_points(elapsed, q_time) == expected


# ── Per-question time limits ───────────────────────────────────────────────

@pytest.mark.parametrize('value, expected', [
    (30, 30), ('45', 45),
    (2, 5), (0, 5), (-10, 5),          # clamped up to the 5s minimum
    (1000, 300), ('301', 300),         # clamped down to the 300s maximum
    (None, 20), ('', 20), ('abc', 20), # garbage falls back to the global default
])
def test_clamp_q_time(trivia, value, expected):
    assert trivia.clamp_q_time(value) == expected


def test_clamp_q_time_custom_fallback(trivia):
    assert trivia.clamp_q_time(None, fallback=42) == 42


def test_question_time_priority_override_then_room_then_global(trivia):
    room = {
        'settings': {'time_per_q': 30},
        'questions': [{'time': 10}, {}, {'time': 999}],
        'current_q': 1,
    }
    assert trivia.question_time(room, 0) == 10      # per-question override
    assert trivia.question_time(room, 1) == 30      # room default
    assert trivia.question_time(room) == 30         # defaults to current question
    assert trivia.question_time(room, 2) == 300     # override is clamped
    assert trivia.question_time(room, 7) == 30      # out of range -> room default
    assert trivia.question_time({'questions': [{}]}, 0) == 20  # nothing set -> global


# ── AI response parsing ────────────────────────────────────────────────────

def test_parse_raw_plain_json(trivia):
    assert trivia._parse_raw('[{"a": 1}]') == [{'a': 1}]


def test_parse_raw_strips_fenced_json_block(trivia):
    raw = '```json\n[{"question": "Q?"}]\n```'
    assert trivia._parse_raw(raw) == [{'question': 'Q?'}]


def test_parse_raw_strips_fence_without_language_and_whitespace(trivia):
    raw = '\n\n  ```\n[1, 2, 3]\n```  \n'
    assert trivia._parse_raw(raw) == [1, 2, 3]


def test_parse_raw_rejects_non_json(trivia):
    with pytest.raises(json.JSONDecodeError):
        trivia._parse_raw('Sure! Here are your questions:')


# ── Room codes ─────────────────────────────────────────────────────────────

def test_room_codes_are_six_unambiguous_characters(trivia):
    for _ in range(500):
        code = trivia.generate_room_code()
        assert len(code) == 6
        assert set(code) <= set('ABCDEFGHJKMNPQRSTUVWXYZ23456789')
        assert not set(code) & set('01OIL'), f'ambiguous char in {code}'


# ── Leaderboard ────────────────────────────────────────────────────────────

def test_scores_list_sorted_high_to_low_with_names(trivia):
    room = {
        'players': [{'id': 'a', 'name': 'Ann'}, {'id': 'b', 'name': 'Bob'}],
        'scores': {'a': 300, 'b': 900, 'gone': 50},
    }
    assert trivia._scores_list(room) == [
        {'id': 'b', 'name': 'Bob', 'score': 900},
        {'id': 'a', 'name': 'Ann', 'score': 300},
        {'id': 'gone', 'name': 'Unknown', 'score': 50},
    ]


# ── Prompt building ────────────────────────────────────────────────────────

def test_build_prompt_includes_settings(trivia):
    prompt = trivia.build_prompt('Roman history', 'mcq', 'hard', 7)
    assert 'Generate 7 trivia questions' in prompt
    assert 'Roman history' in prompt
    assert 'Difficulty: Hard' in prompt
    assert 'Question type: mcq' in prompt


def test_build_prompt_mix_asks_for_all_types(trivia):
    prompt = trivia.build_prompt('x', 'mix', 'easy', 3)
    assert 'a mix of mcq, multi, and frq' in prompt


# ── call_ai fallback chain ─────────────────────────────────────────────────

def test_call_ai_uses_groq_first(trivia, monkeypatch):
    groq = FakeAIClient(content='[{"question": "Q"}]')
    monkeypatch.setattr(trivia, 'groq_client', groq)
    assert trivia.call_ai('t', 'mcq', 'easy', 1) == [{'question': 'Q'}]
    assert groq.calls[0]['model'] == 'llama-3.3-70b-versatile'


def test_call_ai_falls_back_to_openrouter_when_groq_fails(trivia, monkeypatch):
    monkeypatch.setattr(trivia, 'groq_client', FakeAIClient(error=RuntimeError('rate limited')))
    openrouter = FakeAIClient(content='```json\n[{"question": "Fallback"}]\n```')
    monkeypatch.setattr(trivia, 'or_client', openrouter)
    assert trivia.call_ai('t', 'mcq', 'easy', 1) == [{'question': 'Fallback'}]
    assert len(openrouter.calls) == 1


def test_call_ai_raises_when_every_provider_fails(trivia):
    with pytest.raises(RuntimeError, match='blocked'):
        trivia.call_ai('t', 'mcq', 'easy', 1)


# ── FRQ grading ────────────────────────────────────────────────────────────

FRQ_Q = {'question': 'Who wrote Hamlet?', 'answer': 'Shakespeare'}


def test_frq_blank_answer_scores_zero_without_calling_ai(trivia, monkeypatch):
    groq = FakeAIClient(content='{"score": 100}')
    monkeypatch.setattr(trivia, 'groq_client', groq)
    q = {**FRQ_Q, 'rubric': [{'condition': 'Missing name', 'deduction': 50}]}
    result = trivia.grade_frq_answer(q, '   ')
    assert result == {'score_pct': 0, 'triggered': ['Missing name (−50%)']}
    assert groq.calls == []


def test_frq_uses_ai_score_and_triggered_conditions(trivia, monkeypatch):
    groq = FakeAIClient(content='```json\n{"score": 75, "triggered": ["Misspelled"]}\n```')
    monkeypatch.setattr(trivia, 'groq_client', groq)
    q = {**FRQ_Q, 'rubric': [{'condition': 'Misspelled', 'deduction': 25}]}
    assert trivia.grade_frq_answer(q, 'Shakespear') == {'score_pct': 75, 'triggered': ['Misspelled']}
    assert 'subtract 25%: Misspelled' in groq.calls[0]['messages'][0]['content']


@pytest.mark.parametrize('ai_score, expected', [(150, 100), (-20, 0)])
def test_frq_clamps_ai_score_to_0_100(trivia, monkeypatch, ai_score, expected):
    monkeypatch.setattr(trivia, 'groq_client', FakeAIClient(content=f'{{"score": {ai_score}}}'))
    assert trivia.grade_frq_answer(FRQ_Q, 'anything')['score_pct'] == expected


@pytest.mark.parametrize('answer, expected', [
    ('  shakespeare ', 100),   # exact match ignoring case/whitespace
    ('Marlowe', 0),
])
def test_frq_falls_back_to_exact_match_when_ai_fails(trivia, answer, expected):
    # groq_client is blocked by the fixture, so this exercises the fallback path
    assert trivia.grade_frq_answer(FRQ_Q, answer) == {'score_pct': expected, 'triggered': []}


def test_frq_falls_back_when_ai_returns_garbage(trivia, monkeypatch):
    monkeypatch.setattr(trivia, 'groq_client', FakeAIClient(content='I think it is right'))
    assert trivia.grade_frq_answer(FRQ_Q, 'Shakespeare')['score_pct'] == 100


# ── File uploads ───────────────────────────────────────────────────────────

def test_read_uploaded_text_file(trivia):
    f = FileStorage(stream=io.BytesIO('Photosynthesis notes'.encode()), filename='Notes.TXT')
    assert trivia.read_uploaded_file(f) == 'Photosynthesis notes'


def test_read_uploaded_unsupported_file_returns_none(trivia):
    f = FileStorage(stream=io.BytesIO(b'\x00\x01'), filename='image.png')
    assert trivia.read_uploaded_file(f) is None
