// State
let rules = { warnings: [], oks: [] };

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  loadStats();
  loadRules();
});

async function loadStats() {
  try {
    const res = await fetch('/api/stats');
    const data = await res.json();
    const badge = document.getElementById('stats-badge');
    badge.textContent = `${data.total} tickets · ${data.by_type['Change Request'] ?? 0} CRs · ${data.by_type['Product Suggestion'] ?? 0} PSs`;
  } catch {
    document.getElementById('stats-badge').textContent = 'Could not load stats';
  }
}

async function loadRules() {
  try {
    const res = await fetch('/api/rules');
    rules = await res.json();
    renderRules();
  } catch (e) {
    console.error('Failed to load rules', e);
  }
}

// ---------------------------------------------------------------------------
// Scoring
// ---------------------------------------------------------------------------
async function scoreRequest() {
  const query = document.getElementById('query-input').value.trim();
  if (!query) {
    showError('Please enter a request description.');
    return;
  }

  setScoring(true);
  hideError();
  hideResults();

  try {
    const res = await fetch('/api/score', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query }),
    });

    const data = await res.json();

    if (!res.ok) {
      showError(data.error || 'An error occurred while scoring.');
      return;
    }

    renderResults(data);
  } catch (e) {
    showError('Network error — is the server running?');
  } finally {
    setScoring(false);
  }
}

function setScoring(loading) {
  const btn = document.getElementById('score-btn');
  const text = document.getElementById('score-btn-text');
  const spinner = document.getElementById('score-btn-spinner');
  btn.disabled = loading;
  text.textContent = loading ? 'Scoring…' : 'Score Request';
  spinner.classList.toggle('hidden', !loading);
}

function showError(msg) {
  const box = document.getElementById('error-box');
  box.textContent = msg;
  box.classList.remove('hidden');
}

function hideError() {
  document.getElementById('error-box').classList.add('hidden');
}

function hideResults() {
  document.getElementById('results').classList.add('hidden');
}

function renderResults(data) {
  const score = Math.max(0, Math.min(100, Math.round(data.score ?? 0)));

  // Score bar
  document.getElementById('score-value').textContent = score;
  const bar = document.getElementById('score-bar');
  bar.style.width = score + '%';
  bar.style.background = scoreColor(score);

  // Explanation
  document.getElementById('result-explanation').textContent = data.explanation ?? '';

  // Recommendation
  const rec = data.recommendation ?? '';
  const badge = document.getElementById('recommendation-badge');
  badge.textContent = rec;
  badge.className = 'type-badge ' + (rec === 'Change Request' ? 'change-request' : 'feature-suggestion');
  document.getElementById('recommendation-reason').textContent = data.recommendation_reason ?? '';

  // Similar tickets
  const ticketList = document.getElementById('similar-tickets');
  ticketList.innerHTML = '';
  const tickets = data.similar_tickets ?? [];

  if (tickets.length === 0) {
    ticketList.innerHTML = '<p style="font-size:13px;color:#94A3B8">No closely matching past tickets found.</p>';
  } else {
    tickets.forEach(t => {
      ticketList.appendChild(buildTicketCard(t));
    });
  }

  // Meta
  const meta = document.getElementById('result-meta');
  meta.textContent = `Searched ${data.tickets_searched ?? 0} similar past tickets`;

  document.getElementById('results').classList.remove('hidden');
  document.getElementById('results').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function scoreColor(score) {
  if (score <= 20) return '#EF4444';
  if (score <= 40) return '#F97316';
  if (score <= 60) return '#EAB308';
  if (score <= 80) return '#22C55E';
  return '#10B981';
}

function buildTicketCard(t) {
  const div = document.createElement('div');
  div.className = 'ticket-item';

  const statusClass = statusBadgeClass(t.status);

  const keyHtml = t.link
    ? `<a class="ticket-key ticket-key-link" href="${escHtml(t.link)}" target="_blank" rel="noopener noreferrer">${escHtml(t.key)}</a>`
    : `<span class="ticket-key">${escHtml(t.key)}</span>`;

  div.innerHTML = `
    <div class="ticket-item-header">
      ${keyHtml}
      <span class="ticket-status ${statusClass}">${escHtml(t.status)}</span>
      <span class="ticket-status" style="background:#F1F5F9;color:#475569">${escHtml(t.type)}</span>
    </div>
    <div class="ticket-summary">${escHtml(t.summary)}</div>
    <div class="ticket-relevance">${escHtml(t.relevance)}</div>
  `;
  return div;
}

function statusBadgeClass(status) {
  if (!status) return 'status-todo';
  const s = status.toLowerCase();
  if (s === 'done') return 'status-done';
  if (s === 'rejected') return 'status-rejected';
  if (['in development', 'solution design', 'discovery', 'gut feel', 'awaiting approval', 'roadmap/planning'].includes(s)) return 'status-active';
  return 'status-todo';
}

function escHtml(str) {
  return (str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ---------------------------------------------------------------------------
// Rules management
// ---------------------------------------------------------------------------
function renderRules() {
  renderRuleList('warnings-list', rules.warnings, 'warning');
  renderRuleList('oks-list', rules.oks, 'ok');
}

function renderRuleList(containerId, items, type) {
  const container = document.getElementById(containerId);
  container.innerHTML = '';
  if (items.length === 0) {
    container.innerHTML = `<p style="font-size:12px;color:#CBD5E1;padding:4px 0">No ${type === 'warning' ? 'warnings' : 'OKs'} defined yet.</p>`;
    return;
  }
  items.forEach((text, idx) => {
    container.appendChild(buildRuleItem(text, type, idx));
  });
}

function buildRuleItem(text, type, idx) {
  const div = document.createElement('div');
  div.className = `rule-item ${type}-item`;
  div.dataset.type = type;
  div.dataset.idx = idx;

  const ta = document.createElement('textarea');
  ta.className = 'rule-text';
  ta.value = text;
  ta.rows = 2;
  ta.addEventListener('input', () => {
    const listKey = type === 'warning' ? 'warnings' : 'oks';
    rules[listKey][idx] = ta.value;
    autoResize(ta);
  });
  // NOTE: autoResize must NOT be called here — the element isn't in the DOM yet,
  // so scrollHeight returns 0 and locks the textarea to height: 0px.
  // The rows="2" attribute handles the initial height; autoResize fires on input.

  const del = document.createElement('button');
  del.className = 'rule-delete';
  del.textContent = '×';
  del.title = 'Remove';
  del.addEventListener('click', () => deleteRule(type, idx));

  div.appendChild(ta);
  div.appendChild(del);
  return div;
}

function autoResize(ta) {
  ta.style.height = 'auto';
  ta.style.height = ta.scrollHeight + 'px';
}

function addRule(type) {
  const listKey = type === 'warning' ? 'warnings' : 'oks';
  rules[listKey].push('');
  renderRules();
  // Focus the new textarea
  const containerId = type === 'warning' ? 'warnings-list' : 'oks-list';
  const items = document.getElementById(containerId).querySelectorAll('textarea');
  if (items.length) items[items.length - 1].focus();
}

function deleteRule(type, idx) {
  const listKey = type === 'warning' ? 'warnings' : 'oks';
  rules[listKey].splice(idx, 1);
  renderRules();
}

function addExampleRule(type, text) {
  const listKey = type === 'warning' ? 'warnings' : 'oks';
  rules[listKey].push(text);
  renderRules();
}

async function saveRules() {
  // Sync textarea values into rules before saving
  syncTextareas();

  const statusEl = document.getElementById('rules-status');
  const saveText = document.getElementById('save-rules-text');
  saveText.textContent = 'Saving…';

  try {
    const res = await fetch('/api/rules', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(rules),
    });

    if (!res.ok) throw new Error('Server error');

    statusEl.textContent = 'Rules saved successfully.';
    statusEl.className = 'rules-status success';
    statusEl.classList.remove('hidden');
    setTimeout(() => statusEl.classList.add('hidden'), 2500);
  } catch {
    statusEl.textContent = 'Failed to save rules.';
    statusEl.className = 'rules-status error';
    statusEl.classList.remove('hidden');
  } finally {
    saveText.textContent = 'Save Rules';
  }
}

function syncTextareas() {
  document.querySelectorAll('#warnings-list .rule-text').forEach((ta, i) => {
    if (rules.warnings[i] !== undefined) rules.warnings[i] = ta.value;
  });
  document.querySelectorAll('#oks-list .rule-text').forEach((ta, i) => {
    if (rules.oks[i] !== undefined) rules.oks[i] = ta.value;
  });
}

// Allow Ctrl+Enter / Cmd+Enter to submit
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    const focused = document.activeElement;
    if (focused && focused.id === 'query-input') {
      scoreRequest();
    }
  }
});
