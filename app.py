from flask import Flask, request, session, redirect, url_for, render_template, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from openai import OpenAI
import os
import secrets
import random
import json
import time

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(16))

# Groq client (primary — fast, generous free tier)
groq_client = OpenAI(
    base_url='https://api.groq.com/openai/v1',
    api_key=os.environ.get('grok_api_key'),
)

# OpenRouter client (fallback — 50 free req/day)
or_client = OpenAI(
    base_url='https://openrouter.ai/api/v1',
    api_key=os.environ.get('OPENROUTER_API_KEY'),
)

# ---------------------------------------------------------------------------
# User storage — in-memory (dev) or Firestore (prod)
# ---------------------------------------------------------------------------
DEV_MODE = os.environ.get('DEV_MODE', 'true').lower() == 'true'

if DEV_MODE:
    _USERS_FILE = os.path.join(os.path.dirname(__file__), 'users.json')

    def _load_users():
        if os.path.exists(_USERS_FILE):
            with open(_USERS_FILE, 'r') as f:
                return json.load(f)
        return {}

    def _save_users(users):
        with open(_USERS_FILE, 'w') as f:
            json.dump(users, f, indent=2)

    def get_user_by_username(username):
        users = _load_users()
        for uid, u in users.items():
            if u['username'] == username:
                return uid, u
        return None, None

    def get_user_by_email(email):
        users = _load_users()
        for uid, u in users.items():
            if u['email'] == email:
                return uid, u
        return None, None

    def create_user(username, email, display_name, password):
        users = _load_users()
        uid = secrets.token_hex(8)
        users[uid] = {
            'username': username,
            'email': email,
            'display_name': display_name,
            'password_hash': generate_password_hash(password, method='pbkdf2:sha256'),
        }
        _save_users(users)
        return uid

else:
    from google.cloud import firestore
    db = firestore.Client()

    def get_user_by_username(username):
        docs = db.collection('users').where('username', '==', username).limit(1).get()
        if docs:
            return docs[0].id, docs[0].to_dict()
        return None, None

    def get_user_by_email(email):
        docs = db.collection('users').where('email', '==', email).limit(1).get()
        if docs:
            return docs[0].id, docs[0].to_dict()
        return None, None

    def create_user(username, email, display_name, password):
        ref = db.collection('users').document()
        ref.set({
            'username': username,
            'email': email,
            'display_name': display_name,
            'password_hash': generate_password_hash(password, method='pbkdf2:sha256'),
            'created_at': firestore.SERVER_TIMESTAMP,
        })
        return ref.id

# ---------------------------------------------------------------------------
# Room storage — in-memory (resets on restart, same as _users in dev mode)
# ---------------------------------------------------------------------------
_rooms = {}
QUESTION_TIME = 20   # seconds per question

def generate_room_code():
    # No ambiguous chars (0/O, 1/I/L)
    chars = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
    return ''.join(random.choices(chars, k=6))

# ---------------------------------------------------------------------------
# Routes — pages
# ---------------------------------------------------------------------------

@app.route('/')
def landing():
    return render_template('landing.html')


@app.route('/play')
def play():
    return render_template('play.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if 'user_id' in session:
        return redirect(url_for('play'))

    if request.method == 'POST':
        username = request.form['username'].strip()
        email = request.form['email'].strip().lower()
        display_name = request.form['display_name'].strip()
        password = request.form['password']
        confirm_password = request.form['confirm_password']

        if not all([username, email, display_name, password]):
            flash('All fields are required', 'error')
            return render_template('register.html')

        if password != confirm_password:
            flash('Passwords do not match', 'error')
            return render_template('register.html')

        if len(password) < 6:
            flash('Password must be at least 6 characters', 'error')
            return render_template('register.html')

        if get_user_by_username(username)[0]:
            flash('Username already taken', 'error')
            return render_template('register.html')

        if get_user_by_email(email)[0]:
            flash('Email already registered', 'error')
            return render_template('register.html')

        try:
            uid = create_user(username, email, display_name, password)
            session['user_id'] = uid
            session['username'] = username
            session['display_name'] = display_name
            flash(f'Welcome, {display_name}!', 'success')
            return redirect(url_for('play'))
        except Exception as e:
            app.logger.error(f'Registration error: {e}')
            flash('Registration failed. Please try again.', 'error')
            return render_template('register.html')

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('play'))

    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']

        if not username or not password:
            flash('Username and password are required', 'error')
            return render_template('login.html')

        uid, user = get_user_by_username(username)
        if uid and check_password_hash(user['password_hash'], password):
            session['user_id'] = uid
            session['username'] = user['username']
            session['display_name'] = user['display_name']
            flash(f'Welcome back, {user["display_name"]}!', 'success')
            return redirect(url_for('play'))
        else:
            flash('Invalid username or password', 'error')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out', 'info')
    return redirect(url_for('landing'))


# ---------------------------------------------------------------------------
# Routes — rooms
# ---------------------------------------------------------------------------

@app.route('/create', methods=['POST'])
def create_room():
    if 'user_id' not in session:
        flash('You need to log in to create a game', 'error')
        return redirect(url_for('login'))

    code = generate_room_code()
    while code in _rooms:
        code = generate_room_code()

    _rooms[code] = {
        'code': code,
        'host_id': session['user_id'],
        'host_name': session['display_name'],
        'status': 'lobby',
        'players': [{'id': session['user_id'], 'name': session['display_name'], 'is_host': True}],
    }
    session['room_code'] = code
    session['is_host'] = True
    return redirect(url_for('setup', code=code))


@app.route('/join', methods=['POST'])
def join_room():
    code = request.form.get('code', '').strip().upper()
    display_name = request.form.get('display_name', '').strip()

    if not code or not display_name:
        flash('Room code and display name are required', 'error')
        return redirect(url_for('play'))

    if code not in _rooms:
        flash('Room not found. Double-check the code.', 'error')
        return redirect(url_for('play'))

    room = _rooms[code]
    if room['status'] != 'lobby':
        flash('This game has already started.', 'error')
        return redirect(url_for('play'))

    # Logged-in user ID or generate a guest ID
    player_id = session.get('user_id') or session.setdefault('guest_id', secrets.token_hex(8))

    # Prevent duplicate joins
    if player_id not in [p['id'] for p in room['players']]:
        room['players'].append({'id': player_id, 'name': display_name, 'is_host': False})

    session['room_code'] = code
    session['is_host'] = False
    if 'display_name' not in session:
        session['display_name'] = display_name

    return redirect(url_for('lobby', code=code))


@app.route('/room/<code>/lobby')
def lobby(code):
    if code not in _rooms:
        flash('Room not found.', 'error')
        return redirect(url_for('play'))

    room = _rooms[code]
    is_host = session.get('is_host', False) and session.get('room_code') == code
    return render_template('lobby.html', room=room, is_host=is_host)


@app.route('/api/room/<code>/players')
def room_players(code):
    if code not in _rooms:
        return jsonify({'error': 'Room not found'}), 404
    room = _rooms[code]
    return jsonify({'players': room['players'], 'status': room['status']})


@app.route('/room/<code>/setup')
def setup(code):
    if code not in _rooms:
        flash('Room not found.', 'error')
        return redirect(url_for('play'))

    if session.get('user_id') != _rooms[code]['host_id']:
        flash('Only the host can set up the game.', 'error')
        return redirect(url_for('lobby', code=code))

    return render_template('setup.html', room=_rooms[code])


def read_uploaded_file(file):
    """Extract text from an uploaded file."""
    name = file.filename.lower()
    try:
        if name.endswith(('.txt', '.md', '.csv')):
            return file.read().decode('utf-8', errors='ignore')
        elif name.endswith('.pdf'):
            import PyPDF2
            reader = PyPDF2.PdfReader(file)
            return '\n'.join(
                page.extract_text() for page in reader.pages if page.extract_text()
            )
        elif name.endswith('.docx'):
            import docx
            doc = docx.Document(file)
            return '\n'.join(p.text for p in doc.paragraphs if p.text)
    except Exception as e:
        app.logger.error(f'File read error: {e}')
    return None


def build_prompt(topic, question_type, difficulty, count):
    type_instruction = 'a mix of mcq, multi, and frq' if question_type == 'mix' else question_type
    return f"""Generate {count} trivia questions about the following topic:

{topic}

Difficulty: {difficulty.capitalize()}
Question type: {type_instruction}

Return ONLY a valid JSON array. No markdown, no explanation, no code blocks.
Each object must have exactly these fields:
- "question": string
- "type": "mcq" | "multi" | "frq"
- "choices": list of answer option strings — rules by type:
    mcq:   exactly 4 choices, only 1 is correct
    multi: exactly 4-6 choices, 2 or 3 are correct
    frq:   empty list []
- "answer":
    mcq:   string matching one of the choices exactly
    multi: list of strings, each matching one of the choices exactly
    frq:   string (the expected answer)

Example mcq:  {{"question":"...","type":"mcq","choices":["A","B","C","D"],"answer":"B"}}
Example multi:{{"question":"...","type":"multi","choices":["A","B","C","D"],"answer":["A","C"]}}
Example frq:  {{"question":"...","type":"frq","choices":[],"answer":"Paris"}}
"""


def _parse_raw(raw):
    """Strip markdown fences and parse JSON from a model response."""
    raw = raw.strip()
    if raw.startswith('```'):
        raw = raw.split('\n', 1)[1]
        raw = raw.rsplit('```', 1)[0]
    return json.loads(raw)


def _call_gemini(prompt):
    """Call Gemini via API key (preferred) or ADC as fallback."""
    import urllib.request

    api_key = os.environ.get('GOOGLE_API_KEY')
    if api_key:
        # API key auth — simplest, just append as query param
        url = (f'https://generativelanguage.googleapis.com/v1beta/models/'
               f'gemini-2.0-flash:generateContent?key={api_key}')
        headers = {'Content-Type': 'application/json'}
    else:
        # ADC fallback via gcloud token
        import subprocess
        result = subprocess.run(
            ['gcloud', 'auth', 'application-default', 'print-access-token'],
            capture_output=True, text=True, timeout=10,
        )
        token = result.stdout.strip()
        if not token:
            raise RuntimeError(f'No GOOGLE_API_KEY and gcloud token failed: {result.stderr.strip()}')
        url = ('https://generativelanguage.googleapis.com/v1beta/models/'
               'gemini-2.0-flash:generateContent')
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        }

    payload = json.dumps({
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.7, 'maxOutputTokens': 4096},
    }).encode()
    req = urllib.request.Request(url, data=payload, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return data['candidates'][0]['content']['parts'][0]['text']


def call_ai(topic, question_type, difficulty, count):
    """Generate questions — Gemini ADC first, OpenRouter as fallback."""
    prompt = build_prompt(topic, question_type, difficulty, count)
    last_err = None

    # --- Primary: Groq (fast, generous free tier) ---
    try:
        resp = groq_client.chat.completions.create(
            model='llama-3.3-70b-versatile',
            messages=[{'role': 'user', 'content': prompt}],
            temperature=0.7,
            max_tokens=4096,
        )
        return _parse_raw(resp.choices[0].message.content)
    except Exception as e:
        app.logger.warning(f'Groq failed: {e}')
        last_err = e

    # --- Fallback: OpenRouter free models ---
    for model in [
        'meta-llama/llama-3.3-70b-instruct:free',
        'deepseek/deepseek-v4-flash:free',
        'google/gemma-4-31b-it:free',
    ]:
        try:
            resp = or_client.chat.completions.create(
                model=model,
                messages=[{'role': 'user', 'content': prompt}],
                temperature=0.7,
                max_tokens=4096,
            )
            return _parse_raw(resp.choices[0].message.content)
        except Exception as e:
            app.logger.warning(f'OpenRouter {model} failed: {e}')
            last_err = e

    raise last_err


@app.route('/room/<code>/generate', methods=['POST'])
def generate_questions(code):
    if code not in _rooms:
        flash('Room not found.', 'error')
        return redirect(url_for('play'))

    if session.get('user_id') != _rooms[code]['host_id']:
        flash('Only the host can generate questions.', 'error')
        return redirect(url_for('lobby', code=code))

    topic = request.form.get('topic', '').strip()
    question_type = request.form.get('question_type', 'mcq')
    difficulty    = request.form.get('difficulty', 'medium')
    count         = int(request.form.get('count', 10))

    # Append uploaded file content to topic
    file = request.files.get('material_file')
    if file and file.filename:
        file_text = read_uploaded_file(file)
        if file_text:
            topic = file_text + ('\n\n' + topic if topic else '')

    if not topic:
        flash('Please enter a topic or upload a file.', 'error')
        return redirect(url_for('setup', code=code))

    _rooms[code]['settings'] = {
        'topic': topic,
        'question_type': question_type,
        'difficulty': difficulty,
        'count': count,
    }

    try:
        questions = call_ai(topic, question_type, difficulty, count)
        _rooms[code]['questions'] = questions
        flash(f'{len(questions)} questions generated!', 'success')
    except Exception as e:
        app.logger.error(f'Generation error: {e}')
        flash('Failed to generate questions. Please try again.', 'error')
        return redirect(url_for('setup', code=code))

    return redirect(url_for('lobby', code=code))


@app.route('/room/<code>/start', methods=['POST'])
def start_game(code):
    if code not in _rooms:
        flash('Room not found.', 'error')
        return redirect(url_for('play'))

    if session.get('user_id') != _rooms[code]['host_id']:
        flash('Only the host can start the game.', 'error')
        return redirect(url_for('lobby', code=code))

    room = _rooms[code]
    if not room.get('questions'):
        flash('Generate questions first.', 'error')
        return redirect(url_for('lobby', code=code))

    room['status'] = 'active'
    room['current_q'] = 0
    room['q_start_time'] = time.time()
    room['scores'] = {p['id']: 0 for p in room['players']}
    room['answers'] = [{} for _ in room['questions']]
    room['revealed'] = False

    return redirect(url_for('game', code=code))


# ---------------------------------------------------------------------------
# Routes — live game
# ---------------------------------------------------------------------------

@app.route('/room/<code>/game')
def game(code):
    if code not in _rooms:
        flash('Room not found.', 'error')
        return redirect(url_for('play'))
    room = _rooms[code]
    if room['status'] not in ('active', 'ended'):
        return redirect(url_for('lobby', code=code))
    player_id = session.get('user_id') or session.get('guest_id', '')
    is_host = session.get('is_host', False) and session.get('room_code') == code
    return render_template('game.html', room=room, is_host=is_host, player_id=player_id)


def _scores_list(room):
    players_by_id = {p['id']: p['name'] for p in room['players']}
    scores = room.get('scores', {})
    result = [
        {'id': pid, 'name': players_by_id.get(pid, 'Unknown'), 'score': score}
        for pid, score in scores.items()
    ]
    return sorted(result, key=lambda x: x['score'], reverse=True)


@app.route('/api/room/<code>/state')
def room_state(code):
    if code not in _rooms:
        return jsonify({'error': 'Room not found'}), 404

    room = _rooms[code]
    player_id = session.get('user_id') or session.get('guest_id', '')

    if room['status'] == 'lobby':
        return jsonify({'status': 'lobby'})

    if room['status'] == 'ended':
        return jsonify({'status': 'ended', 'scores': _scores_list(room)})

    current_q = room.get('current_q', 0)
    questions = room.get('questions', [])
    revealed = room.get('revealed', False)

    elapsed = time.time() - room.get('q_start_time', time.time())
    time_left = max(0, QUESTION_TIME - int(elapsed))

    # Auto-reveal when timer hits 0
    if time_left == 0 and not revealed:
        room['revealed'] = True
        revealed = True

    q = questions[current_q] if current_q < len(questions) else None
    my_answer = room['answers'][current_q].get(player_id) if q else None

    response = {
        'status': 'active',
        'current_q': current_q,
        'total_q': len(questions),
        'revealed': revealed,
        'time_left': time_left,
        'my_answer': my_answer,
        'scores': _scores_list(room),
        'answers_in': len(room['answers'][current_q]) if q else 0,
        'total_players': len(room['players']),
    }

    if q:
        response['question'] = {
            'text': q['question'],
            'type': q['type'],
            'choices': q.get('choices', []),
            'answer': q['answer'] if revealed else None,
        }

    return jsonify(response)


@app.route('/room/<code>/answer', methods=['POST'])
def submit_answer(code):
    if code not in _rooms:
        return jsonify({'error': 'Room not found'}), 404

    room = _rooms[code]
    if room['status'] != 'active':
        return jsonify({'error': 'Game not active'}), 400

    player_id = session.get('user_id') or session.get('guest_id', '')
    if not player_id:
        return jsonify({'error': 'Not in game'}), 401

    current_q = room['current_q']
    if room['answers'][current_q].get(player_id) is not None:
        return jsonify({'error': 'Already answered'}), 400

    data = request.get_json(silent=True) or {}
    answer = data.get('answer')
    if answer is None:
        return jsonify({'error': 'No answer provided'}), 400

    q = room['questions'][current_q]
    correct = q['answer']
    elapsed = time.time() - room['q_start_time']
    speed_bonus = max(0, int((QUESTION_TIME - elapsed) / QUESTION_TIME * 500))

    if q['type'] == 'mcq' or q['type'] == 'frq':
        is_correct = str(answer).strip().lower() == str(correct).strip().lower()
    elif q['type'] == 'multi':
        is_correct = set(answer) == set(correct) if isinstance(correct, list) else False
    else:
        is_correct = False

    points = (1000 + speed_bonus) if is_correct else 0
    room['scores'][player_id] = room['scores'].get(player_id, 0) + points
    room['answers'][current_q][player_id] = answer

    return jsonify({'correct': is_correct, 'points': points})


@app.route('/room/<code>/skip', methods=['POST'])
def skip_question(code):
    """Host force-reveals the current question without advancing."""
    if code not in _rooms:
        return jsonify({'error': 'Room not found'}), 404
    if session.get('user_id') != _rooms[code]['host_id']:
        return jsonify({'error': 'Only host can skip'}), 403
    _rooms[code]['revealed'] = True
    return jsonify({'ok': True})


@app.route('/room/<code>/end', methods=['POST'])
def end_game(code):
    """Host ends the game early."""
    if code not in _rooms:
        return jsonify({'error': 'Room not found'}), 404
    if session.get('user_id') != _rooms[code]['host_id']:
        return jsonify({'error': 'Only host can end game'}), 403
    _rooms[code]['status'] = 'ended'
    return jsonify({'status': 'ended'})


@app.route('/room/<code>/next', methods=['POST'])
def next_question(code):
    if code not in _rooms:
        return jsonify({'error': 'Room not found'}), 404

    if session.get('user_id') != _rooms[code]['host_id']:
        return jsonify({'error': 'Only host can advance'}), 403

    room = _rooms[code]
    room['current_q'] += 1

    if room['current_q'] >= len(room['questions']):
        room['status'] = 'ended'
        return jsonify({'status': 'ended'})

    room['q_start_time'] = time.time()
    room['revealed'] = False
    return jsonify({'status': 'active', 'current_q': room['current_q']})


if __name__ == '__main__':
    app.run(debug=True, port=5001)
