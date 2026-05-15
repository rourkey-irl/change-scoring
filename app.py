import os
import json
import re
from pathlib import Path
from flask import Flask, request, jsonify, render_template
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
import anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / '.env', override=True)

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


app = Flask(__name__)

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / 'data'
DATA_DIR.mkdir(exist_ok=True)
RULES_FILE = DATA_DIR / 'rules.json'
XML_FILE = BASE_DIR / 'JIRA-PM-Changes-Features.xml'

TICKETS: list[dict] = []

# ---------------------------------------------------------------------------
# XML parsing
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
    ok_lines = [f"  OK: {o}" for o in rules.get('oks', [])]
    rules_text = '\n'.join(warnings_lines + ok_lines) or '  (none defined)'

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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/score', methods=['POST'])
def api_score():
    data = request.get_json(silent=True) or {}
    query = (data.get('query') or '').strip()
    if not query:
        return jsonify({'error': 'Request description is required'}), 400

    rules = load_rules()
    similar = find_similar(query, n=20)

    try:
        result = score_with_claude(query, similar, rules)
    except json.JSONDecodeError as e:
        return jsonify({'error': f'Claude returned invalid JSON: {e}'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    result['tickets_searched'] = len(similar)
    return jsonify(result)


@app.route('/api/rules', methods=['GET'])
def api_get_rules():
    return jsonify(load_rules())


@app.route('/api/rules', methods=['POST'])
def api_save_rules():
    data = request.get_json(silent=True) or {}
    rules = {
        'warnings': [str(w).strip() for w in data.get('warnings', []) if str(w).strip()],
        'oks': [str(o).strip() for o in data.get('oks', []) if str(o).strip()],
    }
    save_rules(rules)
    return jsonify({'ok': True})


@app.route('/api/stats')
def api_stats():
    from collections import Counter
    status_counts = Counter(t['status'] for t in TICKETS)
    type_counts = Counter(t['type'] for t in TICKETS)
    return jsonify({
        'total': len(TICKETS),
        'by_status': dict(status_counts),
        'by_type': dict(type_counts),
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    print(f'Loading Jira tickets from {XML_FILE}...')
    TICKETS = parse_jira_xml(XML_FILE)
    print(f'Loaded {len(TICKETS)} tickets.')
    app.run(debug=True, port=5001)
