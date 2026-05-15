let resetTargetId = null;

// ---------------------------------------------------------------------------
// Add user
// ---------------------------------------------------------------------------
async function addUser() {
  const name     = document.getElementById('new-name').value.trim();
  const email    = document.getElementById('new-email').value.trim();
  const password = document.getElementById('new-password').value;
  const role     = document.getElementById('new-role').value;

  hideAddMessages();

  const btnText    = document.getElementById('add-btn-text');
  const btnSpinner = document.getElementById('add-btn-spinner');
  btnText.textContent = 'Adding…';
  btnSpinner.classList.remove('hidden');

  try {
    const res  = await fetch('/api/admin/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, email, password, role }),
    });
    const data = await res.json();

    if (!res.ok) {
      showAddError((data.errors || [data.error]).join(' '));
      return;
    }

    showAddSuccess(`User ${email} added successfully.`);
    document.getElementById('new-name').value     = '';
    document.getElementById('new-email').value    = '';
    document.getElementById('new-password').value = '';
    document.getElementById('new-role').value     = 'user';

    // Add new row to table
    addTableRow(data.id, name, email, role);

  } catch {
    showAddError('Network error. Please try again.');
  } finally {
    btnText.textContent = 'Add User';
    btnSpinner.classList.add('hidden');
  }
}

function addTableRow(id, name, email, role) {
  const tbody = document.querySelector('#user-table tbody');
  const tr    = document.createElement('tr');
  tr.id = `user-row-${id}`;
  tr.innerHTML = `
    <td>${esc(name)}</td>
    <td class="td-email">${esc(email)}</td>
    <td><span class="role-badge ${role === 'admin' ? 'role-admin' : 'role-user'}">${esc(role)}</span></td>
    <td><span class="status-pill status-active">active</span></td>
    <td class="td-muted">Never</td>
    <td class="td-actions">
      <button class="action-btn action-warn" onclick="setStatus(${id}, 'suspended')">Suspend</button>
      <button class="action-btn action-neutral" onclick="openResetModal(${id}, '${esc(name)}')">Reset Pwd</button>
      <button class="action-btn action-neutral" onclick="generateResetLink(${id})">Reset Link</button>
      <button class="action-btn action-danger" onclick="removeUser(${id}, '${esc(name)}')">Remove</button>
    </td>
  `;
  tbody.insertBefore(tr, tbody.firstChild);
}

// ---------------------------------------------------------------------------
// Suspend / Activate
// ---------------------------------------------------------------------------
async function setStatus(uid, status) {
  const label = status === 'suspended' ? 'suspend' : 'activate';
  if (!confirm(`Are you sure you want to ${label} this user?`)) return;

  const res = await fetch(`/api/admin/users/${uid}/status`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status }),
  });

  if (!res.ok) {
    const data = await res.json();
    alert(data.error || 'Failed to update status.');
    return;
  }

  const row  = document.getElementById(`user-row-${uid}`);
  const pill = row.querySelector('.status-pill');
  const td   = row.querySelector('.td-actions');

  if (status === 'suspended') {
    pill.textContent = 'suspended';
    pill.className   = 'status-pill status-suspended';
    row.classList.add('row-suspended');
    td.querySelector('.action-warn').outerHTML =
      `<button class="action-btn action-ok" onclick="setStatus(${uid}, 'active')">Activate</button>`;
  } else {
    pill.textContent = 'active';
    pill.className   = 'status-pill status-active';
    row.classList.remove('row-suspended');
    td.querySelector('.action-ok').outerHTML =
      `<button class="action-btn action-warn" onclick="setStatus(${uid}, 'suspended')">Suspend</button>`;
  }
}

// ---------------------------------------------------------------------------
// Remove user
// ---------------------------------------------------------------------------
async function removeUser(uid, name) {
  if (!confirm(`Permanently remove ${name}? This cannot be undone.`)) return;

  const res = await fetch(`/api/admin/users/${uid}`, { method: 'DELETE' });
  if (!res.ok) {
    const data = await res.json();
    alert(data.error || 'Failed to remove user.');
    return;
  }
  document.getElementById(`user-row-${uid}`)?.remove();
}

// ---------------------------------------------------------------------------
// Reset password modal
// ---------------------------------------------------------------------------
function openResetModal(uid, name) {
  resetTargetId = uid;
  document.getElementById('modal-user-name').textContent = name;
  document.getElementById('modal-password').value = '';
  document.getElementById('modal-confirm').value  = '';
  document.getElementById('modal-error').classList.add('hidden');
  document.getElementById('modal-overlay').classList.remove('hidden');
  document.getElementById('reset-modal').classList.remove('hidden');
  document.getElementById('modal-password').focus();
}

function closeModal() {
  document.getElementById('modal-overlay').classList.add('hidden');
  document.getElementById('reset-modal').classList.add('hidden');
  document.getElementById('link-modal').classList.add('hidden');
  resetTargetId = null;
}

async function submitResetPassword() {
  const password = document.getElementById('modal-password').value;
  const confirm  = document.getElementById('modal-confirm').value;
  const errEl    = document.getElementById('modal-error');

  errEl.classList.add('hidden');

  if (password !== confirm) {
    errEl.textContent = 'Passwords do not match.';
    errEl.classList.remove('hidden');
    return;
  }

  const res  = await fetch(`/api/admin/users/${resetTargetId}/reset-password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  });
  const data = await res.json();

  if (!res.ok) {
    errEl.innerHTML = (data.errors || [data.error]).join('<br>');
    errEl.classList.remove('hidden');
    return;
  }

  closeModal();
  alert('Password updated successfully.');
}

// ---------------------------------------------------------------------------
// Generate reset link (admin-side)
// ---------------------------------------------------------------------------
async function generateResetLink(uid) {
  const res  = await fetch(`/api/admin/users/${uid}/reset-link`, { method: 'POST' });
  const data = await res.json();

  if (!res.ok) {
    alert(data.error || 'Failed to generate reset link.');
    return;
  }

  const linkEl = document.getElementById('link-modal-url');
  linkEl.href        = data.reset_link;
  linkEl.textContent = data.reset_link;
  document.getElementById('modal-overlay').classList.remove('hidden');
  document.getElementById('link-modal').classList.remove('hidden');
}

function closeLinkModal() {
  document.getElementById('modal-overlay').classList.add('hidden');
  document.getElementById('link-modal').classList.add('hidden');
}

function copyModalLink(btn) {
  const url = document.getElementById('link-modal-url').href;
  navigator.clipboard.writeText(url).then(() => {
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent = 'Copy link', 2000);
  });
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function showAddError(msg) {
  const el = document.getElementById('add-error');
  el.textContent = msg;
  el.classList.remove('hidden');
}
function showAddSuccess(msg) {
  const el = document.getElementById('add-success');
  el.textContent = msg;
  el.classList.remove('hidden');
  setTimeout(() => el.classList.add('hidden'), 4000);
}
function hideAddMessages() {
  document.getElementById('add-error').classList.add('hidden');
  document.getElementById('add-success').classList.add('hidden');
}
function esc(str) {
  return (str ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
