(() => {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);
  let pendingTaskId = null;
  let lastEvents = [];

  function setView(name) {
    $$('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    $$('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
  }
  $$('.tab').forEach(b => b.addEventListener('click', () => setView(b.dataset.tab)));

  function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => { $('#ws-status').textContent = 'live'; $('#ws-status').className = 'badge online'; };
    ws.onclose = () => { $('#ws-status').textContent = 'disconnected'; $('#ws-status').className = 'badge offline'; setTimeout(connectWS, 2000); };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.channel === '_ping') return;
        lastEvents.push(msg);
        if (lastEvents.length > 50) lastEvents.shift();
        if (msg.channel === 'tasks' || msg.channel === 'links') refreshTasks();
      } catch {}
    };
  }
  connectWS();

  async function api(path, opts = {}) {
    const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
    if (!res.ok) throw new Error((await res.text()) || res.statusText);
    if (res.status === 204) return null;
    return res.json();
  }

  function statusBadge(s) {
    return `<span class="status ${s}">${s}</span>`;
  }

  function renderTask(t) {
    const linksHtml = t.links.map(l => {
      const cls = l.status === 'failed' ? 'err' : (l.debrided_url || l.status === 'done' ? 'ok' : '');
      return `<div class="link">
        <div class="url ${cls}" title="${l.original_url}">${l.original_url}</div>
        ${statusBadge(l.status)}
        <div class="progress"><div style="width:${(l.progress * 100).toFixed(1)}%"></div></div>
      </div>`;
    }).join('');
    return `<div class="task" data-id="${t.id}">
      <h3>#${t.id} <span>${t.title || ''}</span> ${statusBadge(t.status)} <button class="secondary small del">Delete</button></h3>
      <div class="meta">${t.media_type} · created ${new Date(t.created_at).toLocaleString()} ${t.finished_at ? '· finished ' + new Date(t.finished_at).toLocaleString() : ''}</div>
      <div class="links">${linksHtml || '<div class="muted">no links</div>'}</div>
      ${t.log ? `<div class="log">${escapeHtml(t.log)}</div>` : ''}
    </div>`;
  }

  function escapeHtml(s) { return (s || '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

  async function refreshTasks() {
    const showFinished = $('#show-finished').checked;
    const tasks = await api('/api/tasks' + (showFinished ? '?include_last_hours=24' : ''));
    $('#tasks-list').innerHTML = tasks.length ? tasks.map(renderTask).join('') : '<div class="muted">No tasks.</div>';
  }
  $('#refresh-tasks').addEventListener('click', refreshTasks);
  $('#show-finished').addEventListener('change', refreshTasks);
  $('#tasks-list').addEventListener('click', async (e) => {
    if (e.target.classList.contains('del')) {
      const id = e.target.closest('.task').dataset.id;
      await api(`/api/tasks/${id}`, { method: 'DELETE' });
      refreshTasks();
    }
  });
  setInterval(refreshTasks, 5000);
  refreshTasks();

  async function uploadDlcIfAny(taskId) {
    const f = $('#new-dlc').files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append('file', f);
    const res = await fetch(`/api/tasks/${taskId}/upload-dlc`, { method: 'POST', body: fd });
    if (!res.ok) throw new Error(await res.text());
  }

  $('#new-create').addEventListener('click', async () => {
    try {
      const payload = {
        media_type: $('#new-type').value,
        title: $('#new-title').value || null,
        raw_input: $('#new-links').value,
      };
      let task = await api('/api/tasks', { method: 'POST', body: JSON.stringify(payload) });
      await uploadDlcIfAny(task.id);
      task = await api(`/api/tasks/${task.id}`);
      const result = await api(`/api/tasks/${task.id}/debrid`, { method: 'POST' });
      const list = $('#new-preview-list');
      list.innerHTML = result.results.map(r => {
        const link = task.links.find(l => l.id === r.id);
        const label = r.ok ? (r.skipped ? 'already debrided' : 'ok') : ('failed: ' + r.error);
        return `<li>[${r.id}] ${link ? link.original_url : ''} - <strong>${label}</strong></li>`;
      }).join('');
      $('#new-preview').classList.remove('hidden');
      pendingTaskId = task.id;
      if (result.all_ok) {
        $('#new-confirm').focus();
      }
    } catch (e) { alert('Error: ' + e.message); }
  });

  $('#new-confirm').addEventListener('click', async () => {
    if (!pendingTaskId) return;
    await api(`/api/tasks/${pendingTaskId}/start`, { method: 'POST' });
    pendingTaskId = null;
    $('#new-preview').classList.add('hidden');
    setView('tasks');
    refreshTasks();
  });
  $('#new-cancel').addEventListener('click', () => {
    pendingTaskId = null;
    $('#new-preview').classList.add('hidden');
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && pendingTaskId && document.activeElement && document.activeElement.tagName !== 'TEXTAREA' && document.activeElement.tagName !== 'INPUT') {
      $('#new-confirm').click();
    }
  });

  async function refreshPasswords() {
    const list = await api('/api/passwords');
    $('#pw-list').innerHTML = list.map(p =>
      `<li>#${p.id} <code>${p.value}</code> <button class="danger small" data-id="${p.id}">remove</button></li>`
    ).join('') || '<li class="muted">No passwords configured.</li>';
    $$('#pw-list .danger').forEach(b => b.addEventListener('click', async () => {
      await api(`/api/passwords/${b.dataset.id}`, { method: 'DELETE' });
      refreshPasswords();
    }));
  }
  $('#pw-add').addEventListener('click', async () => {
    const v = $('#pw-value').value.trim();
    if (!v) return;
    await api('/api/passwords', { method: 'POST', body: JSON.stringify({ value: v }) });
    $('#pw-value').value = '';
    refreshPasswords();
  });
  setView('tasks');
  refreshPasswords();

  async function refreshSettings() {
    const s = await api('/api/settings');
    $('#set-concurrency').value = s.max_concurrent_downloads;
    $('#paths-list').innerHTML = Object.entries(s.media_paths).map(([k, v]) => `<li><strong>${k}:</strong> <code>${v}</code></li>`).join('') + `<li><strong>temp:</strong> <code>${s.temp_path}</code></li>`;
  }
  $('#set-concurrency-save').addEventListener('click', async () => {
    const v = parseInt($('#set-concurrency').value, 10);
    if (!v) return;
    await api('/api/settings/concurrency', { method: 'POST', body: JSON.stringify(v) });
  });
  setView('tasks');
  refreshSettings();
})();
