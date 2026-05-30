# triv.ai — Project Reference

## What this is
AI-powered multiplayer trivia game. Host creates a room, configures questions via Claude API, players join with a room code and answer live. Built with Flask + Firestore on Google Cloud App Engine.

## Project location
All code lives in: `/Users/sam.xu/Documents/trivia_proj/triv.ai/`
Planning docs (read-only reference): `/Users/sam.xu/Documents/trivia_proj/` (architecture.md, game-design.md, design_doc.pdf)

## Running locally
```bash
cd /Users/sam.xu/Documents/trivia_proj/triv.ai
python3 main.py
```
Opens at http://127.0.0.1:5001 (port 5001 — macOS AirPlay occupies 5000)

## Stack
- **Backend:** Flask (Python), `app.py` is the main file, `main.py` is the entry point
- **Database:** Cloud Firestore (prod) / JSON file on disk (dev mode)
- **AI:** OpenRouter API — question generation + FRQ grading. Model: `google/gemini-2.0-flash-exp:free` (free tier). Key stored in `.env` as `OPENROUTER_API_KEY`.
- **Frontend:** Jinja2 templates, vanilla JS, no framework
- **Hosting:** Google App Engine Standard (Python 3.11)

## Dev mode
`DEV_MODE=true` (default) — uses `users.json` for persistent local user storage and `_rooms` in-memory dict for rooms (rooms reset on restart, users persist).
`DEV_MODE=false` — uses Firestore (requires gcloud auth, not set up yet).

## Current file structure
```
triv.ai/
  app.py               # all routes + storage logic
  main.py              # entry point (port 5001)
  requirements.txt
  app.yaml
  users.json           # local user storage (dev mode, gitignore this)
  templates/
    base.html          # navbar, flash messages, block structure
    landing.html       # marketing landing page (/)
    login.html         # /login
    register.html      # /register
    play.html          # /play — create game or join with code
    lobby.html         # /room/<code>/lobby — waiting room
    setup.html         # /room/<code>/setup — question configuration
  static/
    css/style.css      # all styles (dark navy theme)
```

## Completed so far
1. **Auth** — register, login, logout with persistent local storage (users.json)
2. **Landing page** — marketing page at `/` with hero, how-it-works, features, scroll animations
3. **Play page** — `/play` with create game (hosts) and join game (guests + logged-in)
4. **Room creation** — generates 6-char room code, stored in `_rooms` dict
5. **Lobby** — `/room/<code>/lobby` with live player list (polls every 2.5s), copy code button, host badge, colored avatars, Start Game button
6. **Setup page** — `/room/<code>/setup` with:
   - Create New tab: topic textarea, file upload (.txt/.pdf/.docx/.md/.csv), question type selector (MCQ/Multi-select/FRQ/Mix), difficulty toggle, question count slider
   - Saved Sets tab: empty state (not implemented yet)
7. **Game flow order:** Create Game → Setup → Lobby → Start Game → (game screen, not built yet)

## Current game flow
1. Host logs in → `/play` → Create Game
2. → `/room/<code>/setup` — configure questions (topic, type, difficulty, count, optional file upload)
3. → Click "Generate Questions" — saves settings to `_rooms[code]['settings']`, redirects to lobby (Claude API call is the TODO here)
4. → `/room/<code>/lobby` — host shares code, players join via `/play` join form
5. → Host clicks "Start Game" → sets `room['status'] = 'active'` → TODO: redirect to live game screen

## Routes
| Method | Route | Function | Description |
|--------|-------|----------|-------------|
| GET | `/` | `landing` | Marketing landing page |
| GET | `/play` | `play` | Create/join game |
| GET/POST | `/login` | `login` | Login |
| GET/POST | `/register` | `register` | Register |
| GET | `/logout` | `logout` | Logout |
| POST | `/create` | `create_room` | Creates room, redirects to setup |
| POST | `/join` | `join_room` | Player joins room, redirects to lobby |
| GET | `/room/<code>/lobby` | `lobby` | Waiting room |
| GET | `/room/<code>/setup` | `setup` | Host question configuration |
| POST | `/room/<code>/generate` | `generate_questions` | Saves settings, redirects to lobby |
| POST | `/room/<code>/start` | `start_game` | Sets status active, TODO game screen |
| GET | `/api/room/<code>/players` | `room_players` | JSON endpoint polled by lobby JS |

## Room data structure
```python
_rooms[code] = {
    'code': str,
    'host_id': str,
    'host_name': str,
    'status': 'lobby' | 'active' | 'ended',
    'players': [{'id': str, 'name': str, 'is_host': bool}],
    'settings': {          # set after generate_questions
        'topic': str,
        'question_type': 'mcq' | 'multi' | 'frq' | 'mix',
        'difficulty': 'easy' | 'medium' | 'hard',
        'count': int,
    },
    'questions': [],       # set after Claude API call (not built yet)
}
```

## Remaining steps (in priority order)

### Step 1 — Claude API question generation (NEXT)
- Wire up `/room/<code>/generate` to call Claude API
- Handle file upload: read uploaded file content, append to topic text
- Store generated questions in `_rooms[code]['questions']`
- Redirect to a question preview page so host can review before launching
- Model: `claude-sonnet-4-20250514`
- Prompt structure is defined in `/Users/sam.xu/Documents/trivia_proj/game-design.md`
- Expected JSON response format is also in game-design.md
- Install: `anthropic` already in requirements.txt

### Step 2 — Firestore + gcloud setup
- Install gcloud CLI
- Run `gcloud auth application-default login`
- Set `DEV_MODE=false`
- Move room storage from `_rooms` dict to Firestore (needed for real-time multiplayer)
- Firestore data model is in `/Users/sam.xu/Documents/trivia_proj/architecture.md`

### Step 3 — Live game loop
- Push questions to all clients simultaneously
- Countdown timer per question
- Answer submission route (`POST /answer`)
- After timer or all answers in: reveal correct answer
- Real-time updates via Firestore listeners (preferred) or polling

### Step 4 — Scoring + leaderboard
- Calculate points after each question (base points + speed bonus)
- FRQ grading: second Claude API call using grading prompt in game-design.md
- Update scores in Firestore
- Live leaderboard display (updates after each question)

### Step 5 — End screen
- Final leaderboard
- Game summary
- "Play again" / "Back to home" options

### Step 6 — Saved question sets
- Save generated question sets to Firestore under the host's account
- Load saved sets in the "Saved Sets" tab on setup page

## Design notes
- Color scheme: dark navy to black gradient (`#000814` → `#001d3d`)
- Accent: `#4361ee` (electric blue), `#93c5fd` (light blue for icons/badges)
- Font: Segoe UI system stack
- All SVG icons are hand-written inline (no icon library)
- Project name: **triv.ai** (all lowercase) — play on "trivia" where the "ia" becomes ".ai"
