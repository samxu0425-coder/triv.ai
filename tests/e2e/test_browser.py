"""End-to-end tests: a real server driven by real browsers (host + player).

Run with:  python -m pytest tests/e2e          (add --headed to watch)
"""
import re
import threading

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from conftest import MCQ, MULTI

pytestmark = pytest.mark.e2e


@pytest.fixture
def live_server(trivia):
    server = make_server('127.0.0.1', 0, trivia.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown()


@pytest.fixture
def new_page(browser):
    """Each page gets its own browser context, i.e. its own cookies / user."""
    contexts = []

    def _new_page():
        ctx = browser.new_context()
        contexts.append(ctx)
        page = ctx.new_page()
        page.set_default_timeout(10_000)
        page.js_errors = []
        page.on('pageerror', lambda err: page.js_errors.append(str(err)))
        return page
    yield _new_page
    for ctx in contexts:
        ctx.close()


def register(page, base, username):
    page.goto(f'{base}/register')
    page.fill('#username', username)
    page.fill('#display_name', username.title())
    page.fill('#email', f'{username}@example.com')
    page.fill('#password', 'secret1')
    page.fill('#confirm_password', 'secret1')
    page.get_by_role('button', name='Create Account').click()
    page.wait_for_url('**/play')


def create_game(page, base):
    page.goto(f'{base}/play')
    page.get_by_role('button', name='Create Game').click()
    page.wait_for_url('**/setup')
    return page.url.split('/')[-2]


def test_host_and_player_play_a_full_game(live_server, new_page, trivia, monkeypatch):
    monkeypatch.setattr(trivia, 'call_ai', lambda *args: [dict(MCQ)])

    # Host: register → create → generate questions → lobby
    host = new_page()
    register(host, live_server, 'quizmaster')
    code = create_game(host, live_server)
    host.locator('textarea[name="topic"]').fill('European capitals')
    host.locator('#time-per-q').fill('45')
    host.locator('#generateBtn').click()
    host.wait_for_url(f'**/room/{code}/lobby')
    assert trivia._rooms[code]['questions'][0]['time'] == 45

    # Player: join as guest with the room code
    player = new_page()
    player.goto(f'{live_server}/play')
    player.fill('#code', code.lower())
    player.fill('#display_name', 'Alice')
    player.get_by_role('button', name='Join Game').click()
    player.wait_for_url(f'**/room/{code}/lobby')
    expect(player.get_by_text('Waiting for host to start')).to_be_visible()

    # Host sees Alice appear via lobby polling, then starts
    expect(host.locator('#player-list')).to_contain_text('Alice')
    host.get_by_role('button', name='Start Game').click()
    host.wait_for_url(f'**/room/{code}/game')

    # Player's lobby notices the game started and follows
    player.wait_for_url(f'**/room/{code}/game')
    expect(player.locator('#questionText')).to_have_text('Capital of France?')
    expect(player.locator('#timer')).to_have_text(re.compile(r'^(4[0-5]|3\d)$'))  # 45s limit, not the 20s default
    expect(host.locator('#mcqChoices .choice-btn').first).to_be_disabled()  # host can't answer

    player.locator('#mcqChoices .choice-btn', has_text='Paris').click()
    expect(player.locator('#answerFeedback')).to_have_text(re.compile(r'Correct! \+\d+ pts'))
    points = int(re.search(r'\+(\d+)', player.locator('#answerFeedback').inner_text()).group(1))
    assert 900 <= points <= 1000  # answered within a couple of seconds

    # Host reveals, then advances past the last question → game over for everyone
    host.locator('#skipBtn').click()
    host.locator('#nextBtn').click()
    for page in (host, player):
        expect(page.locator('#endOverlay')).to_be_visible()
        first_place = page.locator('#endScores .end-score-row').first
        expect(first_place).to_contain_text('Alice')
        expect(first_place).to_contain_text(f'{points} pts')

    assert host.js_errors == [] and player.js_errors == []


def test_wrong_answer_shows_feedback_and_scores_zero(live_server, new_page, trivia, monkeypatch):
    monkeypatch.setattr(trivia, 'call_ai', lambda *args: [dict(MCQ)])
    host = new_page()
    register(host, live_server, 'quizmaster')
    code = create_game(host, live_server)
    host.locator('textarea[name="topic"]').fill('capitals')
    host.locator('#generateBtn').click()
    host.wait_for_url('**/lobby')

    player = new_page()
    player.goto(f'{live_server}/play')
    player.fill('#code', code)
    player.fill('#display_name', 'Bob')
    player.get_by_role('button', name='Join Game').click()
    player.wait_for_url('**/lobby')
    host.get_by_role('button', name='Start Game').click()
    player.wait_for_url('**/game')

    player.locator('#mcqChoices .choice-btn', has_text='Rome').click()
    expect(player.locator('#answerFeedback')).to_have_text('✗ Wrong!')
    expect(player.locator('#mcqChoices .choice-btn').first).to_be_disabled()
    assert set(trivia._rooms[code]['scores'].values()) == {0}


def test_editor_blocks_incomplete_questions_then_saves(live_server, new_page, trivia):
    host = new_page()
    register(host, live_server, 'builder')
    code = create_game(host, live_server)
    host.goto(f'{live_server}/room/{code}/build')

    host.locator('.editor-add-first-btn').click()
    host.fill('#qpText', 'Largest planet?')

    dialogs = []
    host.on('dialog', lambda d: (dialogs.append(d.message), d.accept()))
    with host.expect_event('dialog'):
        host.locator('#editorSaveBtn').click()
    assert len(dialogs) == 1
    assert 'incomplete' in dialogs[0]
    assert 'no correct answer selected' in dialogs[0]
    assert 'needs at least 2 choices' in dialogs[0]
    assert trivia._rooms[code].get('questions') is None

    for i, choice in enumerate(['Mars', 'Jupiter', 'Venus', 'Earth']):
        host.locator('#mcqList .choice-input').nth(i).fill(choice)
    host.locator('#mcqList .ans-dot').nth(1).click()
    expect(host.locator('#mcqList .ans-dot').nth(1)).to_have_text('●')

    # Override this question's time; the chip in the strip shows it
    expect(host.locator('#qpTime')).to_have_value('20')
    host.fill('#qpTime', '60')
    expect(host.locator('.qchip-time')).to_have_text('60s')

    host.locator('#editorSaveBtn').click()
    host.wait_for_url(f'**/room/{code}/lobby')
    assert trivia._rooms[code]['questions'] == [{
        'question': 'Largest planet?', 'type': 'mcq',
        'choices': ['Mars', 'Jupiter', 'Venus', 'Earth'], 'answer': 'Jupiter', 'time': 60}]
    assert trivia._rooms[code]['settings']['time_per_q'] == 20
    assert len(dialogs) == 1
    assert host.js_errors == []


def test_editing_saved_set_shows_existing_answers(live_server, new_page, trivia):
    """Regression: the LABELS temporal-dead-zone crash hid saved answer selections."""
    host = new_page()
    register(host, live_server, 'editor')
    uid, _ = trivia.get_user_by_username('editor')
    set_id = trivia._save_set_to_file(uid, 'Saved', {'time_per_q': 30},
                                      [{**MCQ, 'time': 15}, dict(MULTI)])

    host.goto(f'{live_server}/saved_sets/{set_id}/edit')
    expect(host.locator('#setNameInput')).to_have_value('Saved')
    dots = host.locator('#mcqList .ans-dot')
    expect(dots).to_have_count(4)
    expect(dots.nth(1)).to_have_class(re.compile(r'\bselected\b'))  # Paris
    expect(host.locator('#mcqList .ans-dot.selected')).to_have_count(1)
    expect(host.locator('#qpTime')).to_have_value('15')               # per-question override

    host.locator('#nextBtn').click()
    checks = host.locator('#multiList .ans-check')
    selected = [i for i in range(checks.count()) if 'selected' in checks.nth(i).get_attribute('class')]
    assert selected == [0, 2, 3]  # Red, Blue, Yellow
    expect(host.locator('#qpTime')).to_have_value('30')               # falls back to set default
    assert host.js_errors == []
