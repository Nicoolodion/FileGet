(() => {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);
  let pendingTaskId = null;
  let knownTasks = {}; // id -> task (latest snapshot from server)
  let wsMessageCount = 0;
  let lastFullRefresh = 0;
  const REFRESH_COALESCE_MS = 8000;   // debounce full re-fetch
  const WS_QUICK_UPDATE_MS = 1500;    // throttle WS-driven per-link updates
  let lastQuickUpdate = 0;
  let pendingQuickUpdate = false;
  let managerPaused = false;

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
        wsMessageCount++;
        if (msg.channel === '_ping') return;
        if (msg.channel === 'manager') {
          if (typeof msg.data.paused === 'boolean') setPausedUi(msg.data.paused);
          return;
        }
        if (msg.channel === 'links') {
          applyLinkPatch(msg.data);
          scheduleQuickUpdate();
          return;
        }
        if (msg.channel === 'tasks') {
          scheduleQuickUpdate();
          return;
        }
      } catch {}
    };
  }
  connectWS();

  function setPausedUi(paused) {
    managerPaused = !!paused;
    const btn = $('#btn-pause');
    if (!btn) return;
    btn.textContent = paused ? 'Resume' : 'Pause';
    btn.dataset.paused = paused ? '1' : '0';
  }

  function scheduleQuickUpdate() {
    if (pendingQuickUpdate) return;
    const now = Date.now();
    const wait = Math.max(0, WS_QUICK_UPDATE_MS - (now - lastQuickUpdate));
    pendingQuickUpdate = true;
    setTimeout(() => {
      pendingQuickUpdate = false;
      lastQuickUpdate = Date.now();
      applyKnownTasksToDom();
    }, wait);
  }

  function applyLinkPatch(patch) {
    if (!patch || patch.id == null) return;
    const id = patch.id;
    // Find the task containing this link
    for (const tid of Object.keys(knownTasks)) {
      const t = knownTasks[tid];
      const idx = (t.links || []).findIndex(l => l.id === id);
      if (idx >= 0) {
        const l = t.links[idx];
        if (patch.status !== undefined) l.status = patch.status;
        if (patch.progress !== undefined) l.progress = patch.progress;
        if (patch.speed !== undefined) l.speed = patch.speed;
        if (patch.debrided_url !== undefined) l.debrided_url = patch.debrided_url;
        if (patch.error !== undefined) l.error = patch.error;
        return;
      }
    }
    // Unknown -> schedule a refresh
    debouncedFullRefresh();
  }

  function debouncedFullRefresh() {
    if (debouncedFullRefresh._t) return;
    debouncedFullRefresh._t = setTimeout(() => {
      debouncedFullRefresh._t = null;
      refreshTasks();
    }, REFRESH_COALESCE_MS);
  }

  function applyKnownTasksToDom() {
    const tasks = Object.values(knownTasks).sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
    $('#tasks-list').innerHTML = tasks.length ? tasks.map(renderTask).join('') : '<div class="muted">No tasks.</div>';
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
    if (!res.ok) throw new Error((await res.text()) || res.statusText);
    if (res.status === 204) return null;
    return res.json();
  }

  function statusBadge(s) {
    return `<span class="status ${s}">${s}</span>`;
  }

  function fmtBytes(n) {
    if (!n) return '0 B';
    const u = ['B', 'KB', 'MB', 'GB'];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(n >= 100 ? 0 : 1)} ${u[i]}`;
  }

  function fmtSpeed(bytesPerSec) {
    if (!bytesPerSec) return '';
    return `${fmtBytes(bytesPerSec)}/s`;
  }

  function renderTask(t) {
    const linksHtml = t.links.map(l => {
      const cls = l.status === 'failed' ? 'err' : (l.debrided_url || l.status === 'done' ? 'ok' : '');
      const showSpeed = l.status === 'downloading' && l.speed ? fmtSpeed(l.speed) : '';
      const inFlight = !['done', 'failed', 'pending'].includes(l.status);
      const canRetry = l.status === 'failed';
      return `<div class="link" data-link-id="${l.id}">
        <div class="url ${cls}" title="${escapeHtml(l.original_url)}">${escapeHtml(l.original_url)}</div>
        ${statusBadge(l.status)}
        <div class="progress"><div style="width:${(l.progress * 100).toFixed(1)}%"></div></div>
        <div class="speed">${showSpeed}</div>
        <div class="link-actions">
          ${inFlight ? `<button class="secondary small link-cancel" data-id="${l.id}">Cancel</button>` : ''}
          ${canRetry ? `<button class="secondary small link-retry" data-id="${l.id}">Retry</button>` : ''}
        </div>
      </div>`;
    }).join('');
    return `<div class="task" data-id="${t.id}">
      <h3>#${t.id} <span>${escapeHtml(t.title || '')}</span> ${statusBadge(t.status)} <button class="secondary small reprocess">Reprocess</button> <button class="secondary small del">Delete</button></h3>
      <div class="meta">${t.media_type} · created ${new Date(t.created_at).toLocaleString()} ${t.finished_at ? '· finished ' + new Date(t.finished_at).toLocaleString() : ''}</div>
      <div class="links">${linksHtml || '<div class="muted">no links</div>'}</div>
      ${t.log ? `<div class="log" data-task-id="${t.id}">${escapeHtml(t.log)}</div>` : ''}
    </div>`;
  }

  function escapeHtml(s) { return (s || '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

  // Preserve scroll position of log panes across re-renders.
  function captureLogScroll() {
    const map = {};
    $$('.log').forEach(el => { map[el.dataset.taskId] = { top: el.scrollTop, bottom: el.scrollHeight, stuck: el.scrollTop + el.clientHeight >= el.scrollHeight - 4 }; });
    return map;
  }
  function restoreLogScroll(map) {
    if (!map) return;
    $$('.log').forEach(el => {
      const m = map[el.dataset.taskId];
      if (!m) return;
      if (m.stuck) el.scrollTop = el.scrollHeight;
      else el.scrollTop = m.top;
    });
  }

  async function refreshTasks() {
    const showFinished = $('#show-finished').checked;
    const tasks = await api('/api/tasks' + (showFinished ? '?include_last_hours=24' : ''));
    const scrollState = captureLogScroll();
    knownTasks = {};
    for (const t of tasks) knownTasks[t.id] = t;
    applyKnownTasksToDom();
    restoreLogScroll(scrollState);
    lastFullRefresh = Date.now();
  }
  $('#refresh-tasks').addEventListener('click', refreshTasks);
  $('#show-finished').addEventListener('change', refreshTasks);
  $('#tasks-list').addEventListener('click', async (e) => {
    if (e.target.classList.contains('del')) {
      const id = e.target.closest('.task').dataset.id;
      await api(`/api/tasks/${id}`, { method: 'DELETE' });
      refreshTasks();
    } else if (e.target.classList.contains('reprocess')) {
      const id = e.target.closest('.task').dataset.id;
      try {
        const r = await api(`/api/tasks/${id}/reprocess`, { method: 'POST' });
        if (!r.ok) alert('Reprocess: ' + (r.reason || 'no result'));
      } catch (err) { alert('Reprocess failed: ' + err.message); }
      debouncedFullRefresh();
    } else if (e.target.classList.contains('link-cancel')) {
      const id = e.target.dataset.id;
      await api(`/api/links/${id}/cancel`, { method: 'POST' });
      debouncedFullRefresh();
    } else if (e.target.classList.contains('link-retry')) {
      const id = e.target.dataset.id;
      await api(`/api/links/${id}/retry`, { method: 'POST' });
      debouncedFullRefresh();
    }
  });
  setInterval(refreshTasks, 15000); // much less aggressive than before
  refreshTasks();

  // Manager pause / resume
  $('#btn-pause').addEventListener('click', async () => {
    const next = managerPaused ? 'resume' : 'pause';
    try {
      await api(`/api/manager/${next}`, { method: 'POST' });
    } catch (e) { alert('Error: ' + e.message); }
  });
  api('/api/manager/status').then(s => setPausedUi(s.paused)).catch(() => {});

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
        return `<li>[${r.id}] ${escapeHtml(link ? link.original_url : '')} - <strong>${label}</strong></li>`;
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
      `<li>#${p.id} <code>${escapeHtml(p.value)}</code> <button class="danger small" data-id="${p.id}">remove</button></li>`
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
    $('#set-discord').value = s.discord_webhook_url || '';
    $('#paths-list').innerHTML = Object.entries(s.media_paths).map(([k, v]) => `<li><strong>${k}:</strong> <code>${escapeHtml(v)}</code></li>`).join('') + `<li><strong>temp:</strong> <code>${escapeHtml(s.temp_path)}</code></li>`;
    const fmt = (n) => (n == null ? '?' : fmtBytes(n));
    const d = s.disk || {};
    const lines = [];
    if (d.temp) lines.push(`<li><strong>temp</strong> <code>${escapeHtml(d.temp.path)}</code> — ${fmt(d.temp.free)} free / ${fmt(d.temp.total)} total</li>`);
    for (const [k, v] of Object.entries(d.media || {})) {
      if (!v) continue;
      lines.push(`<li><strong>${k}</strong> <code>${escapeHtml(v.path)}</code> — ${fmt(v.free)} free / ${fmt(v.total)} total</li>`);
    }
    $('#disk-list').innerHTML = lines.join('') || '<li class="muted">no disk info available</li>';
  }
  $('#set-concurrency-save').addEventListener('click', async () => {
    const v = parseInt($('#set-concurrency').value, 10);
    if (!v) return;
    await api('/api/settings/concurrency', { method: 'POST', body: JSON.stringify(v) });
  });
  $('#set-discord-save').addEventListener('click', async () => {
    const v = $('#set-discord').value.trim();
    await api('/api/settings/discord', { method: 'POST', body: JSON.stringify({ url: v }) });
  });
  $('#set-discord-test').addEventListener('click', async () => {
    const v = $('#set-discord').value.trim();
    if (!v) { alert('Enter a webhook URL first.'); return; }
    await api('/api/settings/discord', { method: 'POST', body: JSON.stringify({ url: v }) });
    // Trigger by force-finishing a tiny dummy? Simpler: just hit a synthetic endpoint via a no-op task.
    // We'll just notify by calling the manager notifier directly through a normal flow: reuse the settings save as confirmation.
    alert('Saved. The next completed/failed task will post to this webhook.');
  });
  setView('tasks');
  refreshSettings();
})();
