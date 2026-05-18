from flask import Flask, request, session, redirect, url_for, render_template, flash
from werkzeug.security import generate_password_hash, check_password_hash
import os
import secrets

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(16))

# ---------------------------------------------------------------------------
# Storage backend — swaps between in-memory (local dev) and Firestore (prod)
# ---------------------------------------------------------------------------
DEV_MODE = os.environ.get('DEV_MODE', 'true').lower() == 'true'

if DEV_MODE:
    # Simple in-memory store for local development
    _users = {}  # user_id -> user dict

    def get_user_by_username(username):
        for uid, u in _users.items():
            if u['username'] == username:
                return uid, u
        return None, None

    def get_user_by_email(email):
        for uid, u in _users.items():
            if u['email'] == email:
                return uid, u
        return None, None

    def create_user(username, email, display_name, password):
        uid = secrets.token_hex(8)
        _users[uid] = {
            'username': username,
            'email': email,
            'display_name': display_name,
            'password_hash': generate_password_hash(password, method='pbkdf2:sha256'),
        }
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
            'password_hash': generate_password_hash(password),
            'created_at': firestore.SERVER_TIMESTAMP,
        })
        return ref.id


# ---------------------------------------------------------------------------
# Routes
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
    return redirect(url_for('play'))


if __name__ == '__main__':
    app.run(debug=True)
