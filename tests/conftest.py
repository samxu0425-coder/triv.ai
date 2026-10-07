import copy
import os
import sys
from types import SimpleNamespace

import pytest

# Must be set before app is imported: forces JSON-file storage and dummy AI keys
# (load_dotenv won't override these), so tests never touch Firestore or real APIs.
os.environ['DEV_MODE'] = 'true'
os.environ['grok_api_key'] = 'test-key'
os.environ['OPENROUTER_API_KEY'] = 'test-key'

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def fake_ai_response(content):
    """Mimics the shape of an OpenAI-compatible chat completion."""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class FakeAIClient:
    def __init__(self, content=None, error=None):
        self.content, self.error, self.calls = content, error, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return fake_ai_response(self.content)


@pytest.fixture
def trivia(tmp_path, monkeypatch):
    """The app module with isolated storage, empty rooms, and AI clients that fail loudly."""
    monkeypatch.setattr(app_module, '_USERS_FILE', str(tmp_path / 'users.json'))
    monkeypatch.setattr(app_module, '_SETS_FILE', str(tmp_path / 'saved_sets.json'))
    monkeypatch.setattr(app_module, '_rooms', {})
    blocked = FakeAIClient(error=RuntimeError('AI calls are blocked in tests'))
    monkeypatch.setattr(app_module, 'groq_client', blocked)
    monkeypatch.setattr(app_module, 'or_client', blocked)
    app_module.app.config.update(TESTING=True, SECRET_KEY='test-secret')
    return app_module


@pytest.fixture
def clock(trivia, monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(trivia, 'time', c)
    return c


@pytest.fixture
def client(trivia):
    return trivia.app.test_client()


@pytest.fixture
def make_client(trivia):
    """Factory for extra independent clients (separate cookie jars = separate players)."""
    return trivia.app.test_client


def login(client, user_id='host1', name='Host'):
    with client.session_transaction() as sess:
        sess['user_id'] = user_id
        sess['display_name'] = name
        sess['username'] = name.lower()


def join_as_guest(client, guest_id, name, code):
    """Puts a guest player into a room as though they had used the join form."""
    with client.session_transaction() as sess:
        sess['guest_id'] = guest_id
        sess['display_name'] = name
        sess['room_code'] = code
        sess['is_host'] = False


MCQ = {'question': 'Capital of France?', 'type': 'mcq',
       'choices': ['Berlin', 'Paris', 'Rome', 'Madrid'], 'answer': 'Paris'}
MULTI = {'question': 'Primary colors?', 'type': 'multi',
         'choices': ['Red', 'Green', 'Blue', 'Yellow'], 'answer': ['Red', 'Blue', 'Yellow']}
FRQ = {'question': 'Who wrote Hamlet?', 'type': 'frq', 'choices': [], 'answer': 'Shakespeare'}


@pytest.fixture(autouse=True)
def _samples_untouched():
    before = copy.deepcopy((MCQ, MULTI, FRQ))
    yield
    assert (MCQ, MULTI, FRQ) == before, 'a test (or route) mutated a shared sample question; pass a copy'


@pytest.fixture
def room(trivia, client):
    """A lobby room hosted by the logged-in `client`. Returns the room dict."""
    login(client)
    resp = client.post('/create')
    code = resp.headers['Location'].rstrip('/').split('/')[-2]
    return trivia._rooms[code]


@pytest.fixture
def start_game(trivia, client, clock, make_client):
    """Loads questions into `room`, adds a guest player, starts the game. Returns the player client."""
    def _start(room, questions):
        room['questions'] = [dict(q) for q in questions]
        player = make_client()
        join_as_guest(player, 'guest1', 'Player', room['code'])
        room['players'].append({'id': 'guest1', 'name': 'Player', 'is_host': False})
        assert client.post(f"/room/{room['code']}/start").status_code == 302
        return player
    return _start
