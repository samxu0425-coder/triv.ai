"""Integration tests: real Flask routes + JSON-file storage, AI and clock faked."""
import io
import json
import re

import pytest

from conftest import FRQ, MCQ, MULTI, join_as_guest, login


def flashes(client):
    with client.session_transaction() as sess:
        return [msg for _, msg in sess.get('_flashes', [])]


def answer(player, code, value):
    return player.post(f'/room/{code}/answer', json={'answer': value})


def state(c, code):
    return c.get(f'/api/room/{code}/state').get_json()


# ═══════════════════════════════════════════════════════════════════════════
# Auth
# ═══════════════════════════════════════════════════════════════════════════

def register(client, username='sam', password='secret1', confirm=None, email=None):
    return client.post('/register', data={
        'username': username, 'email': email or f'{username}@example.com',
        'display_name': username.title(), 'password': password,
        'confirm_password': confirm if confirm is not None else password,
    })


def test_register_logs_user_in_and_persists(client, trivia):
    resp = register(client)
    assert resp.status_code == 302 and resp.headers['Location'].endswith('/play')
    with client.session_transaction() as sess:
        assert sess['username'] == 'sam'
    uid, user = trivia.get_user_by_username('sam')
    assert uid and user['password_hash'] != 'secret1'


@pytest.mark.parametrize('kwargs, error', [
    ({'confirm': 'different'}, 'Passwords do not match'),
    ({'password': 'abc'}, 'Password must be at least 6 characters'),
])
def test_register_rejects_bad_passwords(client, kwargs, error):
    resp = register(client, **kwargs)
    assert resp.status_code == 200
    assert error in resp.get_data(as_text=True)


def test_register_rejects_duplicate_username_and_email(make_client):
    register(make_client())
    assert 'Username already taken' in register(make_client(), email='new@example.com').get_data(as_text=True)
    assert 'Email already registered' in register(make_client(), username='other',
                                                  email='SAM@example.com').get_data(as_text=True)


def test_login_and_logout(make_client):
    register(make_client())
    c = make_client()
    assert 'Invalid username or password' in c.post(
        '/login', data={'username': 'sam', 'password': 'wrong!'}).get_data(as_text=True)

    resp = c.post('/login', data={'username': 'sam', 'password': 'secret1'})
    assert resp.status_code == 302
    with c.session_transaction() as sess:
        assert 'user_id' in sess

    c.get('/logout')
    with c.session_transaction() as sess:
        assert 'user_id' not in sess


# ═══════════════════════════════════════════════════════════════════════════
# Rooms & lobby
# ═══════════════════════════════════════════════════════════════════════════

def test_create_room_requires_login(client):
    resp = client.post('/create')
    assert resp.headers['Location'].endswith('/login')


def test_create_room_makes_host_and_goes_to_setup(room, client):
    assert room['status'] == 'lobby'
    assert room['players'] == [{'id': 'host1', 'name': 'Host', 'is_host': True}]
    assert client.get(f"/room/{room['code']}/setup").status_code == 200


def test_join_room_case_insensitive_and_no_duplicates(room, make_client):
    guest = make_client()
    for _ in range(2):
        resp = guest.post('/join', data={'code': room['code'].lower(), 'display_name': 'Ann'})
        assert resp.headers['Location'].endswith(f"/room/{room['code']}/lobby")
    assert [p['name'] for p in room['players']] == ['Host', 'Ann']


def test_join_unknown_or_started_room_is_rejected(room, make_client):
    guest = make_client()
    guest.post('/join', data={'code': 'ZZZZZZ', 'display_name': 'Ann'})
    assert 'Room not found. Double-check the code.' in flashes(guest)

    room['status'] = 'active'
    guest.post('/join', data={'code': room['code'], 'display_name': 'Ann'})
    assert 'This game has already started.' in flashes(guest)


def test_players_api(room, client):
    data = client.get(f"/api/room/{room['code']}/players").get_json()
    assert data['status'] == 'lobby' and len(data['players']) == 1
    assert client.get('/api/room/NOPE00/players').status_code == 404


def test_only_host_can_open_setup(room, make_client):
    guest = make_client()
    login(guest, 'someone_else', 'Eve')
    resp = guest.get(f"/room/{room['code']}/setup")
    assert resp.headers['Location'].endswith('/lobby')


# ═══════════════════════════════════════════════════════════════════════════
# Question generation
# ═══════════════════════════════════════════════════════════════════════════

def test_generate_stores_questions_and_settings(room, client, trivia, monkeypatch):
    seen = {}

    def fake_call_ai(topic, question_type, difficulty, count):
        seen.update(topic=topic, type=question_type, difficulty=difficulty, count=count)
        return [dict(MCQ)]
    monkeypatch.setattr(trivia, 'call_ai', fake_call_ai)

    resp = client.post(f"/room/{room['code']}/generate", data={
        'topic': 'Geography', 'question_type': 'mcq', 'difficulty': 'easy', 'count': '5',
        'time_per_q': '45'})
    assert resp.headers['Location'].endswith('/lobby')
    assert room['questions'] == [{**MCQ, 'time': 45}]
    assert room['settings']['time_per_q'] == 45
    assert room['questions_saved'] is False
    assert seen == {'topic': 'Geography', 'type': 'mcq', 'difficulty': 'easy', 'count': 5}


@pytest.mark.parametrize('submitted, stored', [('1', 5), ('9999', 300), ('', 20)])
def test_generate_clamps_time_per_question(room, client, trivia, monkeypatch, submitted, stored):
    monkeypatch.setattr(trivia, 'call_ai', lambda *a: [dict(MCQ), dict(FRQ)])
    client.post(f"/room/{room['code']}/generate", data={'topic': 'x', 'time_per_q': submitted})
    assert [q['time'] for q in room['questions']] == [stored, stored]


def test_generate_prepends_uploaded_file_to_topic(room, client, trivia, monkeypatch):
    seen = {}

    def fake_call_ai(topic, *args):
        seen['topic'] = topic
        return [dict(MCQ)]
    monkeypatch.setattr(trivia, 'call_ai', fake_call_ai)
    client.post(f"/room/{room['code']}/generate", data={
        'topic': 'focus on chapter 2',
        'material_file': (io.BytesIO(b'Chapter notes'), 'notes.txt'),
    }, content_type='multipart/form-data')
    assert seen['topic'] == 'Chapter notes\n\nfocus on chapter 2'


def test_generate_requires_topic(room, client):
    resp = client.post(f"/room/{room['code']}/generate", data={'topic': '  '})
    assert resp.headers['Location'].endswith('/setup')
    assert 'Please enter a topic or upload a file.' in flashes(client)


def test_generate_reports_ai_failure(room, client):
    # AI clients are blocked by the fixture, so every provider fails
    resp = client.post(f"/room/{room['code']}/generate", data={'topic': 'Space'})
    assert resp.headers['Location'].endswith('/setup')
    assert 'Failed to generate questions. Please try again.' in flashes(client)
    assert 'questions' not in room


# ═══════════════════════════════════════════════════════════════════════════
# Live game: start, answer, scoring
# ═══════════════════════════════════════════════════════════════════════════

def test_start_requires_questions(room, client):
    resp = client.post(f"/room/{room['code']}/start")
    assert resp.headers['Location'].endswith('/lobby')
    assert room['status'] == 'lobby'


def test_start_initialises_game_state(room, start_game):
    start_game(room, [MCQ, FRQ])
    assert room['status'] == 'active'
    assert room['current_q'] == 0
    assert room['scores'] == {'guest1': 0}   # the host doesn't play
    assert room['answers'] == {'0': {}, '1': {}}
    assert room['grades'] == {'0': {}, '1': {}}


@pytest.mark.parametrize('seconds, points', [(0, 1000), (10, 600), (20, 200)])
def test_mcq_correct_answer_scored_by_speed(room, start_game, clock, seconds, points):
    player = start_game(room, [MCQ])
    clock.advance(seconds)
    assert answer(player, room['code'], 'Paris').get_json() == {'correct': True, 'points': points}
    assert room['scores']['guest1'] == points


@pytest.mark.parametrize('seconds, points', [(0, 1000), (30, 600), (60, 200)])
def test_scoring_uses_the_questions_own_time_limit(room, start_game, clock, seconds, points):
    player = start_game(room, [{**MCQ, 'time': 60}])
    clock.advance(seconds)
    assert answer(player, room['code'], 'Paris').get_json()['points'] == points


def test_scoring_uses_room_default_time_when_question_has_none(room, start_game, clock):
    room['settings'] = {'time_per_q': 40}
    player = start_game(room, [MCQ])
    clock.advance(20)
    assert answer(player, room['code'], 'Paris').get_json()['points'] == 600


def test_mcq_answer_ignores_case_and_whitespace(room, start_game):
    player = start_game(room, [MCQ])
    assert answer(player, room['code'], '  paris ').get_json()['correct'] is True


def test_mcq_wrong_answer_scores_zero(room, start_game):
    player = start_game(room, [MCQ])
    assert answer(player, room['code'], 'Rome').get_json() == {'correct': False, 'points': 0}


def test_multi_requires_exact_set_in_any_order(room, start_game, make_client, clock):
    player = start_game(room, [MULTI])
    assert answer(player, room['code'], ['Yellow', 'Red', 'Blue']).get_json() == {'correct': True, 'points': 1000}

    partial = make_client()
    join_as_guest(partial, 'guest2', 'Partial', room['code'])
    assert answer(partial, room['code'], ['Red', 'Blue']).get_json() == {'correct': False, 'points': 0}


def test_frq_points_are_speed_times_grade(room, start_game, clock, trivia, monkeypatch):
    monkeypatch.setattr(trivia, 'grade_frq_answer',
                        lambda q, a: {'score_pct': 50, 'triggered': ['Incomplete']})
    player = start_game(room, [FRQ])
    clock.advance(5)  # 800 speed points * 50% grade
    assert answer(player, room['code'], 'Shakespear').get_json() == {'submitted': True, 'pending_reveal': True}
    assert room['scores']['guest1'] == 400

    # Grade stays hidden from the player until the answer is revealed
    assert state(player, room['code'])['my_frq_grade'] is None
    room['revealed'] = True
    assert state(player, room['code'])['my_frq_grade'] == {
        'score_pct': 50, 'points': 400, 'triggered': ['Incomplete']}


def test_host_cannot_answer(room, start_game, client):
    start_game(room, [MCQ])
    resp = answer(client, room['code'], 'Paris')
    assert resp.status_code == 403
    assert 'host1' not in room['scores']


def test_leaderboard_excludes_host(room, start_game, client):
    player = start_game(room, [MCQ])
    answer(player, room['code'], 'Rome')
    names = [row['name'] for row in state(client, room['code'])['scores']]
    assert names == ['Player']


def test_paused_time_does_not_count_against_players(room, start_game, client, clock):
    player = start_game(room, [MCQ])
    clock.advance(5)
    client.post(f"/room/{room['code']}/pause")
    clock.advance(60)   # long pause: would be past the 20s deadline if the clock kept running
    assert answer(player, room['code'], 'Paris').get_json() == {'correct': True, 'points': 800}


def test_cannot_answer_twice(room, start_game):
    player = start_game(room, [MCQ])
    answer(player, room['code'], 'Rome')
    resp = answer(player, room['code'], 'Paris')
    assert resp.status_code == 400 and resp.get_json()['error'] == 'Already answered'
    assert room['scores']['guest1'] == 0


def test_answer_validation_errors(room, start_game, make_client):
    player = start_game(room, [MCQ])
    assert answer(make_client(), room['code'], 'Paris').status_code == 401      # not in game
    assert player.post(f"/room/{room['code']}/answer", json={}).status_code == 400
    assert answer(player, 'NOPE00', 'Paris').status_code == 404
    room['status'] = 'ended'
    assert answer(player, room['code'], 'Paris').status_code == 400


def test_cannot_answer_after_reveal(room, start_game, client):
    player = start_game(room, [MCQ])
    client.post(f"/room/{room['code']}/skip")
    assert answer(player, room['code'], 'Paris').status_code == 400


def test_cannot_answer_after_timer_expires_even_if_nobody_polled(room, start_game, trivia, clock):
    # `revealed` is only persisted when someone polls /state; the answer route
    # must still close the question once the clock runs out.
    player = start_game(room, [MCQ])
    clock.advance(trivia.question_time(room, 0) + 1)
    assert room.get('revealed') is not True
    assert answer(player, room['code'], 'Paris').status_code == 400
    assert room['scores'].get('guest1', 0) == 0


def test_rejected_late_answer_is_not_recorded(room, start_game, client):
    player = start_game(room, [MCQ])
    client.post(f"/room/{room['code']}/skip")
    answer(player, room['code'], 'Paris')
    assert 'guest1' not in room['answers'].get('0', {})


def test_can_still_answer_just_before_buzzer(room, start_game, trivia, clock):
    player = start_game(room, [MCQ])
    clock.advance(trivia.question_time(room, 0) - 0.5)
    assert answer(player, room['code'], 'Paris').status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# Live game: state polling, timer, host controls
# ═══════════════════════════════════════════════════════════════════════════

def test_state_hides_answer_until_revealed(room, start_game, client):
    player = start_game(room, [MCQ])
    s = state(player, room['code'])
    assert s['question']['text'] == 'Capital of France?'
    assert s['question']['answer'] is None
    assert s['time_left'] == 20

    assert client.post(f"/room/{room['code']}/skip").get_json() == {'ok': True}
    s = state(player, room['code'])
    assert s['revealed'] and s['question']['answer'] == 'Paris' and s['time_left'] == 0


def test_state_auto_reveals_when_timer_runs_out(room, start_game, clock):
    player = start_game(room, [MCQ])
    clock.advance(12)
    assert state(player, room['code'])['time_left'] == 8
    clock.advance(8)
    s = state(player, room['code'])
    assert s['revealed'] and s['question']['answer'] == 'Paris'


def test_timer_follows_each_questions_time_limit(room, start_game, client, clock):
    room['settings'] = {'time_per_q': 30}
    player = start_game(room, [{**MCQ, 'time': 10}, FRQ])
    code = room['code']

    s = state(player, code)
    assert s['q_time'] == 10 and s['time_left'] == 10
    clock.advance(10)
    assert state(player, code)['revealed'] is True   # short question expires at 10s

    client.post(f'/room/{code}/next')
    s = state(player, code)
    assert s['q_time'] == 30 and s['time_left'] == 30   # falls back to the room default
    clock.advance(25)
    s = state(player, code)
    assert s['time_left'] == 5 and not s['revealed']


def test_state_counts_answers_in(room, start_game):
    player = start_game(room, [MCQ])
    answer(player, room['code'], 'Paris')
    s = state(player, room['code'])
    assert s['answers_in'] == 1 and s['total_players'] == 1 and s['my_answer'] == 'Paris'


def test_pause_freezes_timer_and_resume_continues(room, start_game, client, clock):
    player = start_game(room, [MCQ])
    code = room['code']
    clock.advance(5)
    client.post(f'/room/{code}/pause')
    clock.advance(60)  # a long pause must not run the clock out
    s = state(player, code)
    assert s['paused'] and s['time_left'] == 15 and not s['revealed']

    client.post(f'/room/{code}/resume')
    clock.advance(3)
    s = state(player, code)
    assert not s['paused'] and s['time_left'] == 12


def test_next_question_advances_then_ends(room, start_game, client, clock):
    player = start_game(room, [MCQ, FRQ])
    code = room['code']
    client.post(f'/room/{code}/skip')
    clock.advance(30)

    assert client.post(f'/room/{code}/next').get_json() == {'status': 'active', 'current_q': 1}
    s = state(player, code)
    assert s['current_q'] == 1 and not s['revealed'] and s['time_left'] == 20

    assert client.post(f'/room/{code}/next').get_json() == {'status': 'ended'}
    assert state(player, code)['status'] == 'ended'


def test_end_game_early(room, start_game, client):
    player = start_game(room, [MCQ])
    answer(player, room['code'], 'Paris')
    assert client.post(f"/room/{room['code']}/end").get_json() == {'status': 'ended'}
    s = state(player, room['code'])
    assert s['status'] == 'ended'
    assert s['scores'][0] == {'id': 'guest1', 'name': 'Player', 'score': 1000}


@pytest.mark.parametrize('action', ['skip', 'pause', 'resume', 'next', 'end'])
def test_host_controls_reject_players(room, start_game, action):
    player = start_game(room, [MCQ])
    assert player.post(f"/room/{room['code']}/{action}").status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# Saved sets
# ═══════════════════════════════════════════════════════════════════════════

def save_set(trivia, owner='host1', name='Geo', questions=(MCQ,)):
    return trivia._save_set_to_file(owner, name, {'topic': name}, list(questions))


def test_save_set_from_room_with_default_name(room, client, trivia):
    room['questions'] = [MCQ]
    room['settings'] = {'topic': 'A' * 60}
    data = client.post(f"/room/{room['code']}/save_set", json={}).get_json()
    assert data['ok']
    saved = trivia._get_set(data['id'], 'host1')
    assert saved['name'] == 'A' * 50 + '...'
    assert saved['question_count'] == 1
    assert room['questions_saved'] is True


def test_save_set_requires_login_and_questions(room, client, make_client):
    assert make_client().post(f"/room/{room['code']}/save_set").status_code == 401
    assert client.post(f"/room/{room['code']}/save_set", json={}).status_code == 400


def test_load_set_into_room(room, client, trivia):
    set_id = save_set(trivia, questions=[MCQ, FRQ])
    resp = client.post(f"/room/{room['code']}/load_set/{set_id}")
    assert resp.headers['Location'].endswith('/lobby')
    assert room['questions'] == [MCQ, FRQ]
    assert room['questions_saved'] is True


def test_cannot_load_someone_elses_set(room, client, trivia):
    set_id = save_set(trivia, owner='stranger')
    client.post(f"/room/{room['code']}/load_set/{set_id}")
    assert 'Saved set not found.' in flashes(client)
    assert 'questions' not in room


def test_setup_badges_detect_question_type(room, client, trivia, clock):
    for name, qs in [('All MCQ', [MCQ, MCQ]), ('Mixed Bag', [MCQ, FRQ]), ('Only Multi', [MULTI])]:
        save_set(trivia, name=name, questions=qs)
        clock.advance(1)
    html = client.get(f"/room/{room['code']}/setup").get_data(as_text=True)
    badges = re.findall(r'<span class="set-badge">(MCQ|MULTI|FRQ|MIX)</span>', html)
    # get_user_sets returns newest first
    assert badges == ['MULTI', 'MIX', 'MCQ']


def test_rename_set(client, trivia):
    login(client)
    set_id = save_set(trivia)
    url = f'/api/saved_sets/{set_id}/rename'
    assert client.post(url, json={'name': '  World Capitals  '}).get_json() == {'ok': True}
    assert trivia._get_set(set_id, 'host1')['name'] == 'World Capitals'
    assert client.post(url, json={'name': '   '}).status_code == 400


def test_rename_and_delete_only_your_own_sets(client, trivia):
    login(client, 'intruder', 'Eve')
    set_id = save_set(trivia, owner='host1')
    assert client.post(f'/api/saved_sets/{set_id}/rename', json={'name': 'Mine'}).status_code == 404
    assert client.post(f'/api/saved_sets/{set_id}/delete').status_code == 404
    assert trivia._get_set(set_id, 'host1')['name'] == 'Geo'


def test_delete_set(client, trivia):
    login(client)
    set_id = save_set(trivia)
    assert client.post(f'/api/saved_sets/{set_id}/delete').get_json() == {'ok': True}
    assert trivia._get_set(set_id, 'host1') is None


def test_edit_page_embeds_questions_for_editor(client, trivia):
    login(client)
    set_id = save_set(trivia, questions=[MCQ, MULTI])
    html = client.get(f'/saved_sets/{set_id}/edit').get_data(as_text=True)
    start = html.index('id="questions-data"')
    blob = html[html.index('>', start) + 1:html.index('</script>', start)]
    assert json.loads(blob) == [MCQ, MULTI]


def test_edit_page_rejects_other_users(client, trivia):
    login(client, 'intruder', 'Eve')
    set_id = save_set(trivia)
    assert client.get(f'/saved_sets/{set_id}/edit').headers['Location'].endswith('/play')


def test_update_set_replaces_questions_and_keeps_name_if_blank(client, trivia):
    login(client)
    set_id = save_set(trivia, questions=[MCQ])
    resp = client.post(f'/saved_sets/{set_id}/update', json={'questions': [MCQ, FRQ], 'set_name': ''})
    assert resp.get_json() == {'ok': True}
    saved = trivia._get_set(set_id, 'host1')
    assert saved['name'] == 'Geo' and saved['question_count'] == 2


# ═══════════════════════════════════════════════════════════════════════════
# Manual question builder
# ═══════════════════════════════════════════════════════════════════════════

def test_build_page_renders_for_host(room, client):
    assert client.get(f"/room/{room['code']}/build").status_code == 200


def test_build_loads_questions_into_room(room, client):
    timed = {**FRQ, 'time': 90}
    data = client.post(f"/room/{room['code']}/build",
                       json={'questions': [MCQ, timed], 'time_per_q': 25}).get_json()
    assert data == {'ok': True, 'redirect': f"/room/{room['code']}/lobby"}
    assert room['questions'] == [MCQ, timed]
    assert room['settings']['time_per_q'] == 25
    assert room['questions_saved'] is False


def test_build_rejects_empty_question_list(room, client):
    assert client.post(f"/room/{room['code']}/build", json={'questions': []}).status_code == 400


def test_save_as_set_saves_without_loading_into_room(room, client, trivia):
    resp = client.post(f"/room/{room['code']}/save_as_set",
                       json={'questions': [MCQ], 'set_name': 'Built', 'time_per_q': 35})
    assert resp.get_json()['ok']
    sets = trivia.get_user_sets('host1')
    assert [s['name'] for s in sets] == ['Built']
    assert sets[0]['settings']['topic'] == 'Manual Build'
    assert sets[0]['settings']['time_per_q'] == 35
    assert 'questions' not in room


def test_editor_pages_use_the_set_default_time(room, client, trivia):
    set_id = trivia._save_set_to_file('host1', 'Timed', {'time_per_q': 45}, [MCQ])
    assert "Number('45')" in client.get(f'/saved_sets/{set_id}/edit').get_data(as_text=True)
    assert "Number('20')" in client.get(f"/room/{room['code']}/build").get_data(as_text=True)


@pytest.mark.parametrize('body, error', [
    ({'questions': [MCQ], 'set_name': ' '}, 'Set name is required'),
    ({'questions': [], 'set_name': 'X'}, 'No questions provided'),
])
def test_save_as_set_validation(room, client, body, error):
    resp = client.post(f"/room/{room['code']}/save_as_set", json=body)
    assert resp.status_code == 400 and resp.get_json()['error'] == error
