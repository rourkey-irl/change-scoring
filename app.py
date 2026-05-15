import os
import json
import re
import sqlite3
import secrets
import hashlib
import shutil
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import bcrypt
from dotenv import load_dotenv
from flask import (Flask, request, jsonify, render_template,
                   session, redirect, url_for, abort, flash)
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
import anthropic

load_dotenv(Path(__file__).parent / '.env', override=True)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / 'data'          # app files: XML, seed rules
DATA_DIR.mkdir(exist_ok=True)

# PERSIST_DIR: writable storage that survives redeploys.
# Locally defaults to DATA_DIR. On Railway, point to a mounted volume via
# the PERSIST_DIR environment variable (e.g. /persist).
PERSIST_DIR = Path(os.environ.get('PERSIST_DIR', str(DATA_DIR)))
PERSIST_DIR.mkdir(parents=True, exist_ok=True)

XML_FILE   = DATA_DIR   / 'JIRA-PM-Changes-Features.xml'
RULES_FILE = PERSIST_DIR / 'rules.json'
DB_FILE    = PERSIST_DIR / 'users.db'

TICKETS: list[dict] = []


# ---------------------------------------------------------------------------
# HTML stripper (Jira XML)
# ---------------------------------------------------------------------------

class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reset()
        self._parts = []

    def handle_data(self, d):
        self._parts.append(d)

    def get_data(self):
        return ' '.join(self._parts)


def strip_html(html_text):
    if not html_text:
        return ''
    s = _HTMLStripper()
    try:
        s.feed(html_text)
        return re.sub(r'\s+', ' ', s.get_data()).strip()
    except Exception:
        return html_text


# ---------------------------------------------------------------------------
# Database — users & reset tokens
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT    NOT NULL,
                email        TEXT    NOT NULL UNIQUE,
                password_hash TEXT   NOT NULL,
                role         TEXT    NOT NULL DEFAULT 'user',
                status       TEXT    NOT NULL DEFAULT 'active',
                created_at   TEXT    NOT NULL,
                last_login   TEXT
            );

            CREATE TABLE IF NOT EXISTS reset_tokens (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                token_hash TEXT    NOT NULL UNIQUE,
                expires_at TEXT    NOT NULL,
                used       INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
        ''')


def user_count():
    with get_db() as conn:
        return conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]


def get_user_by_email(email: str):
    with get_db() as conn:
        return conn.execute(
            'SELECT * FROM users WHERE email = ?', (email.lower().strip(),)
        ).fetchone()


def get_user_by_id(user_id: int):
    with get_db() as conn:
        return conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()


def get_all_users():
    with get_db() as conn:
        return conn.execute(
            'SELECT id, name, email, role, status, created_at, last_login '
            'FROM users ORDER BY created_at DESC'
        ).fetchall()


def create_user(name: str, email: str, password: str, role: str = 'user') -> int:
    pw_hash = hash_password(password)
    now = utcnow()
    with get_db() as conn:
        cur = conn.execute(
            'INSERT INTO users (name, email, password_hash, role, status, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (name.strip(), email.lower().strip(), pw_hash, role, 'active', now)
        )
        return cur.lastrowid


def update_user_status(user_id: int, status: str):
    with get_db() as conn:
        conn.execute('UPDATE users SET status = ? WHERE id = ?', (status, user_id))


def update_user_password(user_id: int, password: str):
    pw_hash = hash_password(password)
    with get_db() as conn:
        conn.execute('UPDATE users SET password_hash = ? WHERE id = ?', (pw_hash, user_id))


def delete_user(user_id: int):
    with get_db() as conn:
        conn.execute('DELETE FROM users WHERE id = ?', (user_id,))


def record_login(user_id: int):
    with get_db() as conn:
        conn.execute('UPDATE users SET last_login = ? WHERE id = ?', (utcnow(), user_id))


# ---------------------------------------------------------------------------
# Password utilities
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """bcrypt hash with cost factor 12."""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt(rounds=12)).decode('utf-8')


def verify_password(password: str, pw_hash: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), pw_hash.encode('utf-8'))


PASSWORD_POLICY = [
    (r'.{10,}',          'At least 10 characters'),
    (r'[A-Z]',           'At least one uppercase letter'),
    (r'[a-z]',           'At least one lowercase letter'),
    (r'\d',              'At least one number'),
    (r'[^A-Za-z0-9]',   'At least one special character (e.g. !@#$%)'),
]


def validate_password(password: str) -> list[str]:
    """Returns a list of unmet policy requirements (empty = valid)."""
    return [msg for pattern, msg in PASSWORD_POLICY if not re.search(pattern, password)]


# ---------------------------------------------------------------------------
# Reset-token utilities
# ---------------------------------------------------------------------------

RESET_TOKEN_TTL = timedelta(hours=1)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_reset_token(user_id: int) -> str:
    """Generate a cryptographically secure token, store its hash, return raw token."""
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    expires_at = (datetime.now(timezone.utc) + RESET_TOKEN_TTL).isoformat()
    with get_db() as conn:
        # Invalidate any existing unused tokens for this user
        conn.execute(
            'UPDATE reset_tokens SET used = 1 WHERE user_id = ? AND used = 0',
            (user_id,)
        )
        conn.execute(
            'INSERT INTO reset_tokens (user_id, token_hash, expires_at) VALUES (?, ?, ?)',
            (user_id, token_hash, expires_at)
        )
    return raw


def consume_reset_token(raw: str):
    """Validate token; returns user_id if valid & unused, else None. Marks as used."""
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM reset_tokens WHERE token_hash = ? AND used = 0',
            (token_hash,)
        ).fetchone()
        if not row:
            return None
        expires = datetime.fromisoformat(row['expires_at'])
        if datetime.now(timezone.utc) > expires:
            return None
        conn.execute('UPDATE reset_tokens SET used = 1 WHERE id = ?', (row['id'],))
        return row['user_id']


# ---------------------------------------------------------------------------
# Auth decorators
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login', next=request.path))
        if session.get('role') != 'admin':
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# XML parsing (Jira)
# ---------------------------------------------------------------------------

def _custom_field(item, name_substring):
    for cf in item.findall('.//customfield'):
        name_el = cf.find('customfieldname')
        if name_el is not None and name_substring.lower() in (name_el.text or '').lower():
            val_el = cf.find('.//customfieldvalue')
            if val_el is not None and val_el.text:
                return strip_html(val_el.text)
    return ''


def parse_jira_xml(xml_path: Path) -> list[dict]:
    tree = ET.parse(str(xml_path))
    root = tree.getroot()
    tickets = []

    for item in root.findall('.//item'):
        def text(tag):
            el = item.find(tag)
            return (el.text or '').strip() if el is not None else ''

        comments = []
        for c in item.findall('.//comment'):
            stripped = strip_html(c.text or '')
            if stripped:
                comments.append(stripped[:400])

        ticket = {
            'key': text('key'),
            'summary': text('summary'),
            'description': strip_html(text('description'))[:600],
            'type': text('type'),
            'status': text('status'),
            'resolution': text('resolution'),
            'link': text('link'),
            'comments': comments[:3],
            'customer_reason': _custom_field(item, 'Why has the customer')[:300],
        }
        tickets.append(ticket)

    return tickets


# ---------------------------------------------------------------------------
# Rules persistence
# ---------------------------------------------------------------------------

def load_rules() -> dict:
    if RULES_FILE.exists():
        return json.loads(RULES_FILE.read_text())
    return {'warnings': [], 'oks': []}


def save_rules(rules: dict) -> None:
    RULES_FILE.write_text(json.dumps(rules, indent=2))


# ---------------------------------------------------------------------------
# Relevance scoring (keyword overlap)
# ---------------------------------------------------------------------------

STOPWORDS = {
    'the', 'and', 'for', 'that', 'this', 'with', 'from', 'have', 'they',
    'will', 'are', 'not', 'can', 'has', 'was', 'been', 'also', 'its',
    'our', 'their', 'which', 'when', 'all', 'use', 'any', 'but', 'per',
    'how', 'what', 'into', 'out', 'more', 'some', 'than', 'then', 'there',
}


def tokenize(text: str) -> list[str]:
    return [w for w in re.findall(r'\b[a-z]{3,}\b', text.lower()) if w not in STOPWORDS]


def build_ticket_text(ticket: dict) -> str:
    return ' '.join(filter(None, [
        ticket['summary'],
        ticket['description'],
        ticket['customer_reason'],
        ' '.join(ticket['comments']),
    ]))


def find_similar(query: str, n: int = 20) -> list[dict]:
    query_tokens = set(tokenize(query))
    if not query_tokens:
        return []

    scored = []
    for t in TICKETS:
        ticket_tokens = set(tokenize(build_ticket_text(t)))
        overlap = len(query_tokens & ticket_tokens)
        if overlap > 0:
            scored.append((overlap, t))

    scored.sort(key=lambda x: -x[0])
    return [t for _, t in scored[:n]]


# ---------------------------------------------------------------------------
# Claude scoring
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a customer support analyst for a B2B e-commerce platform company called Experlogix.
You evaluate whether new change requests or feature suggestions are suitable, based on the company's history of past Jira tickets.

Definitions:
- Change Request: customer-specific customisation or one-off product extension. These are chargeable.
- Product Suggestion: an idea suitable for the general product roadmap (not billed to one customer).

Status meanings in the history:
- DONE: completed and delivered — strong positive signal
- Rejected: declined — strong negative signal
- Gut Feel / Discovery / Solution Design / In Development / Awaiting Approval: actively being pursued — positive signal
- To Do / ROADMAP/PLANNING: queued — neutral/positive signal"""


def format_ticket(t: dict) -> str:
    lines = [f"[{t['key']}] {t['type']} | Status: {t['status']} | {t['summary']}"]
    if t['description']:
        lines.append(f"  Desc: {t['description'][:250]}")
    if t['customer_reason']:
        lines.append(f"  Reason: {t['customer_reason']}")
    if t['comments']:
        lines.append(f"  Comments: {' | '.join(t['comments'][:2])[:250]}")
    return '\n'.join(lines)


def score_with_claude(query: str, similar: list[dict], rules: dict) -> dict:
    warnings_lines = [f"  WARNING: {w}" for w in rules.get('warnings', [])]
    ok_lines       = [f"  OK: {o}"      for o in rules.get('oks', [])]
    rules_text     = '\n'.join(warnings_lines + ok_lines) or '  (none defined)'

    tickets_text = '\n\n'.join(format_ticket(t) for t in similar) if similar else '(no similar tickets found)'

    user_prompt = f"""Score the following new request against the company's Jira history.

NEW REQUEST:
{query}

POLICY RULES (apply these with high weight):
{rules_text}

SIMILAR PAST TICKETS FROM JIRA HISTORY ({len(similar)} found):
{tickets_text}

Respond with a JSON object — no markdown, no code fences, just raw JSON:
{{
  "score": <integer 0-100>,
  "explanation": "<1-2 sentences summarising why, referencing patterns or specific tickets>",
  "similar_tickets": [
    {{"key": "<key>", "summary": "<summary>", "status": "<status>", "type": "<type>", "relevance": "<one sentence>"}}
  ],
  "recommendation": "<Change Request|Feature Suggestion>",
  "recommendation_reason": "<1 sentence>"
}}

Scoring guide:
- 0-20: strongly rejected in history or matches a WARNING rule
- 21-40: mostly rejected / unlikely to be entertained
- 41-60: mixed history
- 61-80: generally accepted / similar to DONE or active tickets
- 81-100: highly suitable, strong precedent and/or matches OK rules

Include up to 3 of the most relevant past tickets in similar_tickets."""

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{'role': 'user', 'content': user_prompt}],
    )

    raw = msg.content[0].text.strip()
    return json.loads(raw)


# ===========================================================================
# Routes
# ===========================================================================

# ---------------------------------------------------------------------------
# First-run setup (only active when no users exist)
# ---------------------------------------------------------------------------

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    if user_count() > 0:
        return redirect(url_for('login'))

    errors = []
    if request.method == 'POST':
        name     = request.form.get('name', '').strip()
        email    = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        confirm  = request.form.get('confirm', '')

        if not name:
            errors.append('Name is required.')
        if not email or '@' not in email:
            errors.append('A valid email is required.')
        if password != confirm:
            errors.append('Passwords do not match.')
        pw_errors = validate_password(password)
        errors.extend(pw_errors)

        if not errors:
            create_user(name, email, password, role='admin')
            flash('Admin account created. Please log in.', 'success')
            return redirect(url_for('login'))

    return render_template('setup.html', errors=errors)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route('/login', methods=['GET', 'POST'])
def login():
    if user_count() == 0:
        return redirect(url_for('setup'))
    if 'user_id' in session:
        return redirect(url_for('index'))

    error = None
    if request.method == 'POST':
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user     = get_user_by_email(email)

        if not user or not verify_password(password, user['password_hash']):
            error = 'Invalid email or password.'
        elif user['status'] == 'suspended':
            error = 'Your account has been suspended. Please contact an administrator.'
        else:
            session.permanent = True
            session['user_id'] = user['id']
            session['name']    = user['name']
            session['email']   = user['email']
            session['role']    = user['role']
            record_login(user['id'])
            next_url = request.args.get('next') or url_for('index')
            return redirect(next_url)

    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    reset_link = None
    message    = None
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        user  = get_user_by_email(email)
        # Always show a message (don't reveal whether email exists)
        message = 'If that email is registered, a reset link has been generated below.'
        if user and user['status'] == 'active':
            raw_token = create_reset_token(user['id'])
            reset_link = url_for('reset_password', token=raw_token, _external=True)

    return render_template('forgot_password.html', message=message, reset_link=reset_link)


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    errors  = []
    success = False

    user_id = consume_reset_token(token) if request.method == 'GET' else None

    # On GET, pre-validate token (don't consume yet — we re-check on POST)
    if request.method == 'GET':
        # Re-fetch without consuming
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with get_db() as conn:
            row = conn.execute(
                'SELECT * FROM reset_tokens WHERE token_hash = ? AND used = 0',
                (token_hash,)
            ).fetchone()
        valid = row and datetime.fromisoformat(row['expires_at']) > datetime.now(timezone.utc)
        if not valid:
            return render_template('reset_password.html', token=token,
                                   errors=['This reset link is invalid or has expired.'],
                                   success=False, expired=True)
        return render_template('reset_password.html', token=token,
                               errors=[], success=False, expired=False)

    # POST — consume the token and set new password
    password = request.form.get('password', '')
    confirm  = request.form.get('confirm', '')

    if password != confirm:
        errors.append('Passwords do not match.')
    errors.extend(validate_password(password))

    if not errors:
        uid = consume_reset_token(token)
        if uid is None:
            errors.append('This reset link is invalid or has already been used.')
        else:
            update_user_password(uid, password)
            success = True

    return render_template('reset_password.html', token=token,
                           errors=errors, success=success, expired=False)


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

@app.route('/admin')
@admin_required
def admin():
    users = [dict(u) for u in get_all_users()]
    return render_template('admin.html', users=users,
                           current_user_id=session['user_id'],
                           active_page='users')


@app.route('/admin/rules')
@admin_required
def admin_rules_page():
    return render_template('admin_rules.html', active_page='rules')


@app.route('/api/admin/users', methods=['POST'])
@admin_required
def api_admin_create_user():
    data     = request.get_json(silent=True) or {}
    name     = (data.get('name') or '').strip()
    email    = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    role     = data.get('role', 'user')

    errors = []
    if not name:
        errors.append('Name is required.')
    if not email or '@' not in email:
        errors.append('A valid email is required.')
    if get_user_by_email(email):
        errors.append('A user with that email already exists.')
    if role not in ('admin', 'user'):
        errors.append('Role must be admin or user.')
    errors.extend(validate_password(password))

    if errors:
        return jsonify({'errors': errors}), 400

    uid = create_user(name, email, password, role)
    return jsonify({'id': uid, 'message': f'User {email} created.'})


@app.route('/api/admin/users/<int:uid>/status', methods=['POST'])
@admin_required
def api_admin_set_status(uid):
    if uid == session['user_id']:
        return jsonify({'error': 'You cannot change your own status.'}), 400
    data   = request.get_json(silent=True) or {}
    status = data.get('status')
    if status not in ('active', 'suspended'):
        return jsonify({'error': 'Status must be active or suspended.'}), 400
    if not get_user_by_id(uid):
        return jsonify({'error': 'User not found.'}), 404
    update_user_status(uid, status)
    return jsonify({'ok': True})


@app.route('/api/admin/users/<int:uid>/reset-password', methods=['POST'])
@admin_required
def api_admin_reset_password(uid):
    data     = request.get_json(silent=True) or {}
    password = data.get('password') or ''
    errors   = validate_password(password)
    if errors:
        return jsonify({'errors': errors}), 400
    if not get_user_by_id(uid):
        return jsonify({'error': 'User not found.'}), 404
    update_user_password(uid, password)
    return jsonify({'ok': True})


@app.route('/api/admin/users/<int:uid>', methods=['DELETE'])
@admin_required
def api_admin_delete_user(uid):
    if uid == session['user_id']:
        return jsonify({'error': 'You cannot delete your own account.'}), 400
    if not get_user_by_id(uid):
        return jsonify({'error': 'User not found.'}), 404
    delete_user(uid)
    return jsonify({'ok': True})


@app.route('/api/admin/users/<int:uid>/reset-link', methods=['POST'])
@admin_required
def api_admin_generate_reset_link(uid):
    user = get_user_by_id(uid)
    if not user:
        return jsonify({'error': 'User not found.'}), 404
    raw_token  = create_reset_token(uid)
    reset_link = url_for('reset_password', token=raw_token, _external=True)
    return jsonify({'reset_link': reset_link})


# ---------------------------------------------------------------------------
# Main app routes (all login-protected)
# ---------------------------------------------------------------------------

@app.route('/')
@login_required
def index():
    return render_template('index.html', user_name=session.get('name'),
                           user_role=session.get('role'))


@app.route('/api/score', methods=['POST'])
@login_required
def api_score():
    data  = request.get_json(silent=True) or {}
    query = (data.get('query') or '').strip()
    if not query:
        return jsonify({'error': 'Request description is required'}), 400

    rules   = load_rules()
    similar = find_similar(query, n=20)

    try:
        result = score_with_claude(query, similar, rules)
    except json.JSONDecodeError as e:
        return jsonify({'error': f'Claude returned invalid JSON: {e}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    result['tickets_searched'] = len(similar)

    # Enrich similar_tickets with Jira links looked up from the ticket index
    ticket_links = {t['key']: t['link'] for t in TICKETS if t.get('link')}
    for t in result.get('similar_tickets', []):
        t['link'] = ticket_links.get(t.get('key'), '')

    return jsonify(result)


@app.route('/api/rules', methods=['GET'])
@login_required
def api_get_rules():
    return jsonify(load_rules())


@app.route('/api/rules', methods=['POST'])
@admin_required
def api_save_rules():
    data  = request.get_json(silent=True) or {}
    rules = {
        'warnings': [str(w).strip() for w in data.get('warnings', []) if str(w).strip()],
        'oks':      [str(o).strip() for o in data.get('oks', [])      if str(o).strip()],
    }
    save_rules(rules)
    return jsonify({'ok': True})


@app.route('/api/stats')
@login_required
def api_stats():
    from collections import Counter
    status_counts = Counter(t['status'] for t in TICKETS)
    type_counts   = Counter(t['type']   for t in TICKETS)
    return jsonify({
        'total':     len(TICKETS),
        'by_status': dict(status_counts),
        'by_type':   dict(type_counts),
    })


# ---------------------------------------------------------------------------
# Startup — runs under both `python app.py` and gunicorn
# ---------------------------------------------------------------------------

def _startup():
    # Seed rules.json onto the persistent volume if it's the first deploy
    seed = DATA_DIR / 'rules.json'
    if not RULES_FILE.exists() and seed.exists() and RULES_FILE != seed:
        shutil.copy(seed, RULES_FILE)
        print(f'Seeded rules.json to {RULES_FILE}')

    init_db()

    global TICKETS
    print(f'Loading Jira tickets from {XML_FILE}...')
    TICKETS = parse_jira_xml(XML_FILE)
    print(f'Loaded {len(TICKETS)} tickets.')

    if user_count() == 0:
        print('No users found — visit /setup to create the first admin account.')


_startup()


# ---------------------------------------------------------------------------
# Entry point (local dev only)
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app.run(debug=True, port=5001)
