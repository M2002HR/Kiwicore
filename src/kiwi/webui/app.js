const state = {
  user: null,
  currentPage: 'dashboard',
  routeFilter: '',
  routeSort: { key: 'name', dir: 'asc' },
  autoRefreshMs: 5000,
  autoRefreshTimer: null,
  autoRefreshRunning: false,
  autoRefreshEnabled: true,
  dashboard: null,
  routes: [],
  routeMetrics: {},
  traffic: null,
  workers: null,
  scripts: { channel: [], guard: [] },
  sync: null,
  logs: [],
  logFilters: {
    level: '',
    logger: '',
    message: '',
  },
  storage: null,
  health: null,
  settings: null,
  admins: [],
  keywordLinks: [],
};

const menuItems = [
  ['dashboard', 'Dashboard', 'Service overview and stats'],
  ['routes', 'Routes', 'Full route management'],
  ['scripts', 'Scripts', 'Edit channel and guard scripts'],
  ['keywords', 'Keywords', 'Manage keyword links'],
  ['sync', 'Sync Queue', 'Queue and review status'],
  ['logs', 'Logs', 'Live log viewer'],
  ['traffic', 'Traffic', 'Download and upload usage'],
  ['workers', 'Workers', 'Sync workers and host resources'],
  ['storage', 'Storage', 'Stored messages and files'],
  ['system', 'System', 'Health checks and settings'],
  ['admins', 'Admins', 'Admin user management'],
];

const els = {
  app: document.getElementById('app'),
  loginView: document.getElementById('loginView'),
  loginForm: document.getElementById('loginForm'),
  loginUser: document.getElementById('loginUser'),
  loginPass: document.getElementById('loginPass'),
  loginError: document.getElementById('loginError'),
  logoutBtn: document.getElementById('logoutBtn'),
  menu: document.getElementById('menu'),
  meInfo: document.getElementById('meInfo'),
  pageTitle: document.getElementById('pageTitle'),
  pageDesc: document.getElementById('pageDesc'),
  reloadBtn: document.getElementById('reloadBtn'),
  toggleAutoRefreshBtn: document.getElementById('toggleAutoRefreshBtn'),
  stopServiceBtn: document.getElementById('stopServiceBtn'),
  autoRefreshInfo: document.getElementById('autoRefreshInfo'),
  toastRoot: document.getElementById('toastRoot'),
  modalOverlay: document.getElementById('modalOverlay'),
  modalTitle: document.getElementById('modalTitle'),
  modalBody: document.getElementById('modalBody'),
  modalCloseBtn: document.getElementById('modalCloseBtn'),
};

function showFlash(text, isError = false) {
  const root = els.toastRoot;
  if (!root) return;
  const toast = document.createElement('div');
  toast.className = `toast ${isError ? 'error' : 'success'}`;
  toast.textContent = String(text || '');
  root.appendChild(toast);
  requestAnimationFrame(() => {
    toast.classList.add('show');
  });
  const closeDelayMs = 4200;
  const fadeMs = 220;
  setTimeout(() => {
    toast.classList.add('hide');
    setTimeout(() => {
      toast.remove();
    }, fadeMs);
  }, closeDelayMs);
}

function esc(v) {
  return String(v ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    const message = data?.detail || data?.error || `HTTP ${res.status}`;
    throw new Error(message);
  }
  return data;
}

function setAutoRefreshText(text) {
  if (!els.autoRefreshInfo) return;
  els.autoRefreshInfo.textContent = text;
}

function refreshAutoRefreshUi() {
  const sec = Math.floor(state.autoRefreshMs / 1000);
  if (state.autoRefreshRunning && state.autoRefreshEnabled) {
    setAutoRefreshText('Auto: updating...');
  } else if (state.autoRefreshEnabled) {
    setAutoRefreshText(`Auto: ${sec}s`);
  } else {
    setAutoRefreshText(`Auto: paused (${sec}s)`);
  }
  if (!els.toggleAutoRefreshBtn) return;
  if (state.autoRefreshEnabled) {
    els.toggleAutoRefreshBtn.textContent = '⏸';
    els.toggleAutoRefreshBtn.title = 'Pause auto refresh';
    els.toggleAutoRefreshBtn.setAttribute('aria-label', 'Pause auto refresh');
  } else {
    els.toggleAutoRefreshBtn.textContent = '▶';
    els.toggleAutoRefreshBtn.title = 'Resume auto refresh';
    els.toggleAutoRefreshBtn.setAttribute('aria-label', 'Resume auto refresh');
  }
}

function cssEsc(value) {
  const text = String(value ?? '');
  if (window.CSS && typeof window.CSS.escape === 'function') {
    return window.CSS.escape(text);
  }
  return text.replace(/[\\"]/g, '\\$&');
}

function captureFocusedFieldSnapshot() {
  const el = document.activeElement;
  if (!el || !(el instanceof HTMLElement)) return null;
  const tag = String(el.tagName || '').toLowerCase();
  if (!['input', 'textarea', 'select'].includes(tag)) return null;
  const page = el.closest('.page');
  const snap = {
    tag,
    id: String(el.id || ''),
    name: String(el.getAttribute('name') || ''),
    type: String(el.getAttribute('type') || ''),
    placeholder: String(el.getAttribute('placeholder') || ''),
    pageId: String(page?.id || ''),
    inModal: !!el.closest('#modalOverlay'),
    selectionStart: null,
    selectionEnd: null,
  };
  if (tag !== 'select' && 'selectionStart' in el && 'selectionEnd' in el) {
    const start = Number(el.selectionStart);
    const end = Number(el.selectionEnd);
    snap.selectionStart = Number.isFinite(start) ? start : null;
    snap.selectionEnd = Number.isFinite(end) ? end : null;
  }
  return snap;
}

function findFieldFromSnapshot(snap) {
  if (!snap || typeof snap !== 'object') return null;
  const root = snap.inModal
    ? (els.modalBody || els.modalOverlay || document)
    : (document.getElementById(String(snap.pageId || '')) || document);
  const tag = String(snap.tag || '').toLowerCase();
  if (!tag) return null;

  if (snap.id) {
    const byId = root.querySelector(`#${cssEsc(snap.id)}`);
    if (byId) return byId;
  }
  if (snap.name) {
    const byName = root.querySelector(`${tag}[name="${cssEsc(snap.name)}"]`);
    if (byName) return byName;
  }
  if (snap.placeholder && (tag === 'input' || tag === 'textarea')) {
    const byPlaceholder = root.querySelector(`${tag}[placeholder="${cssEsc(snap.placeholder)}"]`);
    if (byPlaceholder) return byPlaceholder;
  }
  if (snap.type && tag === 'input') {
    const byType = root.querySelector(`input[type="${cssEsc(snap.type)}"]`);
    if (byType) return byType;
  }
  return null;
}

function restoreFocusedFieldSnapshot(snap) {
  const el = findFieldFromSnapshot(snap);
  if (!el || !(el instanceof HTMLElement)) return;
  try {
    el.focus({ preventScroll: true });
  } catch (_) {
    el.focus();
  }
  if (
    snap
    && snap.tag !== 'select'
    && 'setSelectionRange' in el
    && Number.isFinite(snap.selectionStart)
    && Number.isFinite(snap.selectionEnd)
  ) {
    try {
      el.setSelectionRange(snap.selectionStart, snap.selectionEnd);
    } catch (_) {
      // no-op
    }
  }
}

async function runAction(fn, fallbackError = 'Unknown error') {
  try {
    await fn();
  } catch (err) {
    const message = (err && err.message) ? err.message : fallbackError;
    showFlash(`Error: ${message}`, true);
  }
}

function iconBtn({ act = '', title = '', icon = '•', extraClass = '', attrs = '' } = {}) {
  const a1 = act ? ` data-act="${esc(act)}"` : '';
  const glyphClass = icon === '+' ? 'icon-glyph plus-glyph' : 'icon-glyph';
  return `<button class="btn icon-only subtle-icon ${esc(extraClass)}" title="${esc(title)}" aria-label="${esc(title)}"${a1} ${attrs}><span class="${glyphClass}">${icon}</span></button>`;
}

function isModalOpen() {
  return !!els.modalOverlay && !els.modalOverlay.classList.contains('hidden');
}

function openModal(title, contentHtml) {
  if (!els.modalOverlay || !els.modalTitle || !els.modalBody) return;
  els.modalTitle.textContent = title || 'Modal';
  els.modalBody.innerHTML = contentHtml || '';
  els.modalOverlay.classList.remove('hidden');
  els.modalOverlay.setAttribute('aria-hidden', 'false');
}

function closeModal() {
  if (!els.modalOverlay || !els.modalBody) return;
  els.modalOverlay.classList.add('hidden');
  els.modalOverlay.setAttribute('aria-hidden', 'true');
  els.modalBody.innerHTML = '';
}

function buildMenu() {
  els.menu.innerHTML = menuItems.map(([id, label]) => `
    <button data-page="${id}">${label}</button>
  `).join('');
  els.menu.querySelectorAll('button').forEach(btn => {
    btn.addEventListener('click', () => {
      navigate(btn.dataset.page || 'dashboard');
    });
  });
}

function setPageMeta(page) {
  const item = menuItems.find(x => x[0] === page) || menuItems[0];
  els.pageTitle.textContent = item[1];
  els.pageDesc.textContent = item[2];
}

function navigate(page) {
  state.currentPage = page;
  setPageMeta(page);
  menuItems.forEach(([id]) => {
    const section = document.getElementById(`page-${id}`);
    if (section) section.classList.toggle('hidden', id !== page);
  });
  els.menu.querySelectorAll('button').forEach(btn => btn.classList.toggle('active', btn.dataset.page === page));
  renderCurrentPage();
}

function card(title, value, sub = '') {
  return `
    <article class="card">
      <h3>${esc(title)}</h3>
      <div class="stat">${esc(value)}</div>
      <div class="muted">${esc(sub)}</div>
    </article>
  `;
}

function formatBytes(value) {
  const n = Math.max(0, Number(value || 0));
  if (n < 1024) return `${n.toFixed(0)} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(2)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatDateTime(value) {
  const text = String(value || '').trim();
  if (!text) return '-';
  const d = new Date(text);
  if (Number.isNaN(d.getTime())) return text;
  return d.toLocaleString();
}

function formatDuration(secValue) {
  const total = Math.max(0, Math.floor(Number(secValue || 0)));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const mins = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (days > 0) return `${days}d ${hours}h ${mins}m`;
  if (hours > 0) return `${hours}h ${mins}m ${secs}s`;
  if (mins > 0) return `${mins}m ${secs}s`;
  return `${secs}s`;
}

function badgeByStatus(status) {
  const s = String(status || '').toLowerCase();
  if (s === 'ok' || s === 'sent' || s === 'active') return `<span class="badge ok">${esc(status)}</span>`;
  if (s === 'warn' || s === 'queued' || s === 'processing') return `<span class="badge warn">${esc(status)}</span>`;
  return `<span class="badge err">${esc(status)}</span>`;
}

async function loadDashboard() {
  const data = await api('/api/dashboard');
  state.dashboard = data.data;
}

async function loadRoutes() {
  const [routesRes, syncRes] = await Promise.all([
    api('/api/routes'),
    api('/api/sync/stats'),
  ]);
  state.routes = routesRes.routes || [];
  state.routeMetrics = (syncRes.stats && syncRes.stats.route_metrics) ? syncRes.stats.route_metrics : {};
}

async function loadScriptsMeta() {
  const [ch, gu] = await Promise.all([
    api('/api/scripts/channel'),
    api('/api/scripts/guard'),
  ]);
  state.scripts.channel = ch.files || [];
  state.scripts.guard = gu.files || [];
}

async function loadKeywords() {
  const data = await api('/api/keyword-links');
  state.keywordLinks = data.links || [];
}

async function loadSync() {
  const [stats, reviews] = await Promise.all([
    api('/api/sync/stats'),
    api('/api/sync/reviews?limit=100&only_open=true'),
  ]);
  state.sync = { stats: stats.stats || {}, reviews: reviews.reviews || [] };
}

async function loadLogs() {
  const params = new URLSearchParams();
  params.set('limit', '250');
  if (state.logFilters.level) params.set('level', state.logFilters.level);
  if (state.logFilters.logger) params.set('logger', state.logFilters.logger);
  if (state.logFilters.message) params.set('message', state.logFilters.message);
  const data = await api(`/api/logs?${params.toString()}`);
  state.logs = data.logs || [];
}

async function loadTraffic() {
  const data = await api('/api/traffic/stats');
  state.traffic = data.traffic || {};
}

async function loadWorkers() {
  const data = await api('/api/workers/status');
  state.workers = data.workers || {};
}

async function loadStorage() {
  const [summary, runs] = await Promise.all([
    api('/api/storage/summary'),
    api('/api/storage/runs?limit=120'),
  ]);
  state.storage = { summary: summary.summary || {}, runs: runs.runs || [] };
}

async function loadSystem() {
  const [health, settings] = await Promise.all([
    api('/api/system/status'),
    api('/api/system/settings'),
  ]);
  state.health = health.data || {};
  state.settings = settings.data || {};
}

async function loadAdmins() {
  const data = await api('/api/admin/users');
  state.admins = data.users || [];
}

function renderDashboardPage() {
  const page = document.getElementById('page-dashboard');
  const d = state.dashboard || {};
  const sync = d.sync || {};
  const routes = d.routes || {};
  const svc = d.service || {};
  page.innerHTML = `
    <div class="grid cols-4">
      ${card('Total Routes', routes.total ?? 0, 'route definitions')}
      ${card('Synced Routes', routes.synced ?? 0, 'status=synced')}
      ${card('Syncing Routes', routes.syncing ?? 0, 'status=syncing')}
      ${card('Queue Depth', sync.queue_depth ?? 0, 'sync queue')}
    </div>
    <div class="grid cols-4" style="margin-top:10px">
      ${card('Open Reviews', sync.open_reviews ?? 0, 'pending/manual checks')}
      ${card('Run Once Calls', svc.run_once_calls ?? 0, 'polling iterations')}
      ${card('Processed Total', svc.processed_messages_total ?? 0, 'successful processing')}
      ${card('Consecutive Errors', svc.consecutive_poll_errors ?? 0, 'continuous failures')}
    </div>
    <div class="card" style="margin-top:10px">
      <h3>Quick Service Status</h3>
      <div class="row">
        ${Object.entries((d.health || {}).checks || {}).map(([k, v]) => `<div>${esc(k)}: ${badgeByStatus(v.status || 'unknown')}</div>`).join('') || '<span class="muted">-</span>'}
      </div>
    </div>
  `;
}

function routeStatusPill(routeStatus) {
  const status = String(routeStatus || '').toLowerCase();
  if (status === 'synced') return '<span class="badge ok">synced</span>';
  if (status === 'syncing') return '<span class="badge warn">syncing</span>';
  if (status === 'deactive') return '<span class="badge err">deactive</span>';
  return '<span class="badge">-</span>';
}

function effectiveRouteStatus(routeStatus, metrics) {
  const normalized = String(routeStatus || 'deactive').toLowerCase();
  if (normalized === 'deactive') return 'deactive';
  const remaining = Number(metrics?.remaining_unsynced ?? 0);
  if (Number.isFinite(remaining) && remaining > 0) return 'syncing';
  return 'synced';
}

function findRouteByName(name) {
  return (state.routes || []).find((item) => String(item?.name || '') === String(name || ''));
}

function routeSortIndicator(key) {
  const activeKey = String(state.routeSort?.key || 'name');
  const dir = String(state.routeSort?.dir || 'asc');
  if (activeKey !== key) return '↕';
  return dir === 'asc' ? '▲' : '▼';
}

function toggleRouteSort(key) {
  const activeKey = String(state.routeSort?.key || 'name');
  const activeDir = String(state.routeSort?.dir || 'asc');
  if (activeKey === key) {
    state.routeSort = { key, dir: activeDir === 'asc' ? 'desc' : 'asc' };
    return;
  }
  state.routeSort = { key, dir: 'asc' };
}

function routeSortValue(route, metrics, key) {
  if (key === 'name') return String(route?.name || '');
  if (key === 'source') return String(route?.source_channel_username || '');
  if (key === 'destination') return String(route?.destination_channel_username || route?.destination_channel_id || '');
  if (key === 'channel_script') return String(route?.channel_script || '');
  if (key === 'guard') return String(route?.gaurd_script || '');
  if (key === 'status') return effectiveRouteStatus(route?.status, metrics);
  if (key === 'remaining') return Number(metrics?.remaining_unsynced ?? 0);
  if (key === 'progress') return Number(metrics?.progress_pct ?? 0);
  if (key === 'actions') return String(route?.name || '');
  return String(route?.name || '');
}

function renderRoutesPage(opts = {}) {
  const prevSearch = document.getElementById('routeSearchInput');
  const prevFocusedSearch = !!prevSearch && document.activeElement === prevSearch;
  const keepSearchFocus = !!opts.keepSearchFocus || prevFocusedSearch;
  let caretStart = Number.isInteger(opts.caretStart) ? Number(opts.caretStart) : null;
  let caretEnd = Number.isInteger(opts.caretEnd) ? Number(opts.caretEnd) : null;
  if (prevFocusedSearch && prevSearch) {
    const ps = Number(prevSearch.selectionStart);
    const pe = Number(prevSearch.selectionEnd);
    if (caretStart == null && Number.isFinite(ps)) caretStart = ps;
    if (caretEnd == null && Number.isFinite(pe)) caretEnd = pe;
  }
  const page = document.getElementById('page-routes');
  const needle = String(state.routeFilter || '').trim().toLowerCase();
  const filtered = (state.routes || []).filter((r) => {
    if (!needle) return true;
    const hay = [
      r?.name,
      r?.source_channel_username,
      r?.destination_channel_username,
      r?.destination_channel_id,
      r?.channel_script,
      r?.gaurd_script,
      r?.status,
    ].map((x) => String(x || '').toLowerCase()).join(' | ');
    return hay.includes(needle);
  });

  const sortKey = String(state.routeSort?.key || 'name');
  const sortDir = String(state.routeSort?.dir || 'asc');
  const sorted = filtered.slice().sort((a, b) => {
    const ma = state.routeMetrics && a?.name ? state.routeMetrics[String(a.name)] : null;
    const mb = state.routeMetrics && b?.name ? state.routeMetrics[String(b.name)] : null;
    const va = routeSortValue(a, ma, sortKey);
    const vb = routeSortValue(b, mb, sortKey);
    let cmp = 0;
    if (typeof va === 'number' && typeof vb === 'number') {
      cmp = va - vb;
    } else {
      cmp = String(va).localeCompare(String(vb), undefined, { sensitivity: 'base', numeric: true });
    }
    if (cmp === 0) {
      cmp = String(a?.name || '').localeCompare(String(b?.name || ''), undefined, { sensitivity: 'base', numeric: true });
    }
    return sortDir === 'asc' ? cmp : -cmp;
  });

  const rows = sorted.map((r) => {
    const routeKey = encodeURIComponent(String(r.name || ''));
    const name = esc(r.name || '-');
    const source = esc(r.source_channel_username || '-');
    const dest = esc(r.destination_channel_username || r.destination_channel_id || '-');
    const cs = esc(r.channel_script || '-');
    const gs = esc(r.gaurd_script || '-');
    const m = state.routeMetrics && r.name ? state.routeMetrics[String(r.name)] : null;
    const routeStatus = effectiveRouteStatus(r.status, m);
    const isDeactive = routeStatus === 'deactive';
    const remaining = Number(m?.remaining_unsynced ?? 0);
    const progressPct = Number(m?.progress_pct ?? 0);
    const remainingLabel = isDeactive ? '-' : String(remaining);
    const progressLabel = isDeactive ? '-' : (Number.isFinite(progressPct) ? `${progressPct.toFixed(1)}%` : '-');
    const progressBadge = isDeactive ? '' : (progressPct >= 95 ? 'ok' : (progressPct >= 60 ? 'warn' : 'err'));
    return `
      <tr>
        <td class="route-col-name" title="${name}">${name}</td>
        <td class="route-col-source" title="${source}">${source}</td>
        <td class="route-col-destination" title="${dest}">${dest}</td>
        <td>${cs}</td>
        <td>${gs}</td>
        <td>${routeStatusPill(routeStatus)}</td>
        <td>${remainingLabel}</td>
        <td><span class="badge ${progressBadge}">${progressLabel}</span></td>
        <td class="actions-cell">
          <div class="icon-actions">
            ${iconBtn({ act: 'edit', title: `Edit ${r.name || ''}`, icon: '✎', attrs: `data-route-name="${routeKey}"` })}
            ${iconBtn({ act: 'toggle', title: routeStatus === 'deactive' ? 'Start route' : 'Stop route', icon: routeStatus === 'deactive' ? '▶' : '⏸', attrs: `data-route-name="${routeKey}" data-status="${routeStatus}"` })}
            ${iconBtn({ act: 'delete', title: `Delete ${r.name || ''}`, icon: '✕', extraClass: 'btn-danger', attrs: `data-route-name="${routeKey}"` })}
          </div>
        </td>
      </tr>
    `;
  }).join('');

  page.innerHTML = `
    <div class="card">
      <div class="row" style="justify-content:space-between">
        <h3>Route Management</h3>
        <div class="row">
          <input id="routeSearchInput" class="compact-input" placeholder="Search routes (name/source/destination/script)..." value="${esc(state.routeFilter)}">
          <button id="routeStartAllBtn" class="btn">Start All</button>
          <button id="routeStopAllBtn" class="btn btn-danger">Stop All</button>
          ${iconBtn({ title: 'Add new route', icon: '+', attrs: 'id="routeAddBtn"' })}
        </div>
      </div>
      <div class="meta-line">Showing ${filtered.length} of ${state.routes.length} routes</div>
      <div class="table-wrap routes-table-wrap">
        <table class="routes-table">
          <thead>
            <tr>
              <th class="route-col-name"><button class="th-sort" data-sort-key="name">Name <span class="sort-arrow">${routeSortIndicator('name')}</span></button></th>
              <th class="route-col-source"><button class="th-sort" data-sort-key="source">Source <span class="sort-arrow">${routeSortIndicator('source')}</span></button></th>
              <th class="route-col-destination"><button class="th-sort" data-sort-key="destination">Destination <span class="sort-arrow">${routeSortIndicator('destination')}</span></button></th>
              <th><button class="th-sort" data-sort-key="channel_script">Channel Script <span class="sort-arrow">${routeSortIndicator('channel_script')}</span></button></th>
              <th><button class="th-sort" data-sort-key="guard">Guard <span class="sort-arrow">${routeSortIndicator('guard')}</span></button></th>
              <th><button class="th-sort" data-sort-key="status">Status <span class="sort-arrow">${routeSortIndicator('status')}</span></button></th>
              <th><button class="th-sort" data-sort-key="remaining">Remaining <span class="sort-arrow">${routeSortIndicator('remaining')}</span></button></th>
              <th><button class="th-sort" data-sort-key="progress">Progress <span class="sort-arrow">${routeSortIndicator('progress')}</span></button></th>
              <th><button class="th-sort" data-sort-key="actions">Actions <span class="sort-arrow">${routeSortIndicator('actions')}</span></button></th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>
  `;

  document.getElementById('routeSearchInput')?.addEventListener('input', (e) => {
    const inputEl = e.target;
    state.routeFilter = String(inputEl?.value || '');
    const start = Number(inputEl?.selectionStart);
    const end = Number(inputEl?.selectionEnd);
    renderRoutesPage({
      keepSearchFocus: true,
      caretStart: Number.isFinite(start) ? start : null,
      caretEnd: Number.isFinite(end) ? end : null,
    });
  });

  page.querySelectorAll('button[data-sort-key]').forEach(btn => {
    btn.addEventListener('click', () => {
      const key = String(btn.dataset.sortKey || '').trim();
      if (!key) return;
      toggleRouteSort(key);
      renderRoutesPage({ keepSearchFocus, caretStart, caretEnd });
    });
  });
  if (keepSearchFocus) {
    const search = document.getElementById('routeSearchInput');
    if (search) {
      search.focus();
      const fallbackPos = String(search.value || '').length;
      const start = caretStart == null ? fallbackPos : Math.max(0, Math.min(fallbackPos, caretStart));
      const end = caretEnd == null ? start : Math.max(start, Math.min(fallbackPos, caretEnd));
      try {
        search.setSelectionRange(start, end);
      } catch (_) {
        // no-op
      }
    }
  }

  page.querySelectorAll('button[data-act]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const act = btn.dataset.act;
      const routeName = decodeURIComponent(String(btn.dataset.routeName || ''));
      const route = findRouteByName(routeName);
      const name = route?.name;
      if (!name) return;
      if (act === 'delete') {
        if (!confirm(`Delete route ${name}?`)) return;
        await runAction(async () => {
          await api(`/api/routes/${encodeURIComponent(name)}`, { method: 'DELETE' });
          showFlash('Route deleted');
          await reloadPageData('routes');
        }, 'Failed to delete route');
        return;
      }
      if (act === 'toggle') {
        const currentStatus = String(btn.dataset.status || 'deactive');
        await runAction(async () => {
          if (currentStatus === 'deactive') {
            await api(`/api/routes/${encodeURIComponent(name)}/sync/start`, { method: 'POST' });
          } else {
            await api(`/api/routes/${encodeURIComponent(name)}/sync/stop`, { method: 'POST' });
          }
          showFlash('Route status updated');
          await reloadPageData('routes');
        }, 'Failed to change route status');
        return;
      }
      if (act === 'edit') {
        openRouteEditor(route);
      }
    });
  });

  document.getElementById('routeAddBtn')?.addEventListener('click', () => openRouteEditor(null));
  document.getElementById('routeStartAllBtn')?.addEventListener('click', async () => {
    await runAction(async () => {
      await api('/api/routes/start-all', { method: 'POST' });
      showFlash('All routes started');
      await reloadPageData('routes');
    }, 'Failed to start all routes');
  });
  document.getElementById('routeStopAllBtn')?.addEventListener('click', async () => {
    await runAction(async () => {
      await api('/api/routes/stop-all', { method: 'POST' });
      showFlash('All routes stopped');
      await reloadPageData('routes');
    }, 'Failed to stop all routes');
  });
}

function openRouteEditor(route) {
  const isEdit = !!route;
  const r = route || {
    name: '', status: 'synced', source_channel_username: '',
    destination_channel_username: '', destination_channel_id: '',
    channel_script: '', gaurd_script: 'default_guard.py', max_message_mb: 15,
    backfill_count: 50, interval_sec: 1, batch_size: 1, retry_attempts: 2,
  };
  const routeMetrics = r.name ? state.routeMetrics[String(r.name)] : null;
  const remaining = Number(routeMetrics?.remaining_unsynced ?? 0);
  const progressPct = Number(routeMetrics?.progress_pct ?? 0);

  openModal(
    isEdit ? 'Edit Route' : 'Add Route',
    `
      <div class="meta-line">Sync progress: ${progressPct.toFixed(1)}% | Remaining unsynced: ${remaining}</div>
      <form id="routeForm" class="modal-grid">
        <label>Route name <input name="name" required value="${esc(r.name || '')}" ${isEdit ? 'readonly' : ''}></label>
        <label>Status <select name="status"><option value="deactive" ${String(r.status || '') === 'deactive' ? 'selected' : ''}>deactive</option><option value="syncing" ${String(r.status || '') === 'syncing' ? 'selected' : ''}>syncing</option><option value="synced" ${String(r.status || '') === 'synced' ? 'selected' : ''}>synced</option></select></label>
        <label>source username <input name="source_channel_username" value="${esc(r.source_channel_username || '')}"></label>
        <label>destination username <input name="destination_channel_username" value="${esc(r.destination_channel_username || '')}"></label>
        <label>destination id <input name="destination_channel_id" value="${esc(r.destination_channel_id || '')}"></label>
        <label>channel script <input name="channel_script" value="${esc(r.channel_script || '')}"></label>
        <label>guard script <input name="gaurd_script" value="${esc(r.gaurd_script || '')}"></label>
        <label>max message MB <input type="number" min="1" name="max_message_mb" value="${esc(r.max_message_mb ?? '')}"></label>
        <label>backfill <input type="number" min="0" name="backfill_count" value="${esc(r.backfill_count ?? 50)}"></label>
        <label>interval sec <input type="number" min="1" name="interval_sec" value="${esc(r.interval_sec ?? 1)}"></label>
        <label>batch size <input type="number" min="1" name="batch_size" value="${esc(r.batch_size ?? 1)}"></label>
        <label>retry attempts <input type="number" min="0" name="retry_attempts" value="${esc(r.retry_attempts ?? 2)}"></label>
      </form>
      <div class="modal-actions">
        ${iconBtn({ title: 'Close', icon: '✕', attrs: 'id="cancelRouteBtn"' })}
        ${iconBtn({ title: 'Save route', icon: '✓', attrs: 'id="saveRouteBtn"' })}
      </div>
    `,
  );

  document.getElementById('cancelRouteBtn')?.addEventListener('click', () => { closeModal(); });
  document.getElementById('saveRouteBtn')?.addEventListener('click', async () => {
    await runAction(async () => {
      const form = document.getElementById('routeForm');
      const fd = new FormData(form);
      const payload = {
        name: String(fd.get('name') || '').trim(),
        status: String(fd.get('status') || 'deactive'),
        source_channel_username: (String(fd.get('source_channel_username') || '').trim() || null),
        destination_channel_username: (String(fd.get('destination_channel_username') || '').trim() || null),
        destination_channel_id: (String(fd.get('destination_channel_id') || '').trim() || null),
        channel_script: (String(fd.get('channel_script') || '').trim() || null),
        gaurd_script: (String(fd.get('gaurd_script') || '').trim() || null),
        max_message_mb: Number(fd.get('max_message_mb') || 0) || null,
        backfill_count: Number(fd.get('backfill_count') || 50),
        interval_sec: Number(fd.get('interval_sec') || 1),
        batch_size: Number(fd.get('batch_size') || 1),
        retry_attempts: Number(fd.get('retry_attempts') || 2),
      };
      if (!payload.name) {
        throw new Error('Route name is required');
      }
      if (!payload.source_channel_username) {
        throw new Error('Source username is required');
      }
      if (!payload.destination_channel_id && !payload.destination_channel_username) {
        throw new Error('At least one of destination username or destination id is required');
      }
      if (isEdit) {
        await api(`/api/routes/${encodeURIComponent(payload.name)}`, { method: 'PUT', body: JSON.stringify(payload) });
        showFlash('Route updated');
      } else {
        await api('/api/routes', { method: 'POST', body: JSON.stringify(payload) });
        showFlash('Route added');
      }
      closeModal();
      await reloadPageData('routes');
    }, 'Failed to save route');
  });
}

function renderScriptsPage() {
  const page = document.getElementById('page-scripts');
  page.innerHTML = `
    <div class="grid cols-2">
      <div class="card">
        <div class="row" style="justify-content:space-between">
          <h3>Channel Scripts</h3>
          ${iconBtn({ title: 'Create channel script', icon: '+', attrs: 'id="newChannelScriptBtn"' })}
        </div>
        <div id="channelScriptsList" class="stack"></div>
      </div>
      <div class="card">
        <div class="row" style="justify-content:space-between">
          <h3>Guard Scripts</h3>
          ${iconBtn({ title: 'Create guard script', icon: '+', attrs: 'id="newGuardScriptBtn"' })}
        </div>
        <div id="guardScriptsList" class="stack"></div>
      </div>
    </div>
  `;

  const chList = document.getElementById('channelScriptsList');
  const guList = document.getElementById('guardScriptsList');

  function renderList(host, items, kind) {
    host.innerHTML = items.map(name => `<button class="btn btn-ghost" data-kind="${kind}" data-name="${esc(name)}">${esc(name)}</button>`).join('');
    host.querySelectorAll('button').forEach(btn => {
      btn.addEventListener('click', async () => {
        await runAction(async () => {
          const k = btn.dataset.kind;
          const n = btn.dataset.name;
          const out = await api(`/api/scripts/${k}/${encodeURIComponent(n)}`);
          openScriptEditorModal({ kind: k, name: n, content: out.content || '' });
        }, 'Failed to load script');
      });
    });
  }

  renderList(chList, state.scripts.channel || [], 'channel');
  renderList(guList, state.scripts.guard || [], 'guard');

  document.getElementById('newChannelScriptBtn')?.addEventListener('click', () => {
    openScriptEditorModal({ kind: 'channel', name: '', content: '#!/usr/bin/env python3\n' });
  });
  document.getElementById('newGuardScriptBtn')?.addEventListener('click', () => {
    openScriptEditorModal({ kind: 'guard', name: '', content: '#!/usr/bin/env python3\n' });
  });
}

function openScriptEditorModal({ kind, name, content }) {
  const safeKind = (kind === 'guard') ? 'guard' : 'channel';
  openModal(
    `${name ? `Edit ${name}` : `New ${safeKind} script`}`,
    `
      <label>File name <input id="scriptNameInput" placeholder="example.py" value="${esc(name || '')}"></label>
      <label>Kind
        <select id="scriptKindInput">
          <option value="channel" ${safeKind === 'channel' ? 'selected' : ''}>channel script</option>
          <option value="guard" ${safeKind === 'guard' ? 'selected' : ''}>guard script</option>
        </select>
      </label>
      <label>Content <textarea id="scriptContent" style="min-height:420px">${esc(content || '')}</textarea></label>
      <div class="modal-actions">
        ${iconBtn({ title: 'Close', icon: '✕', attrs: 'id="scriptCancelBtn"' })}
        ${iconBtn({ title: 'Save script', icon: '✓', attrs: 'id="scriptSaveBtn"' })}
      </div>
    `,
  );

  document.getElementById('scriptCancelBtn')?.addEventListener('click', closeModal);
  document.getElementById('scriptSaveBtn')?.addEventListener('click', async () => {
    await runAction(async () => {
      const finalName = String(document.getElementById('scriptNameInput')?.value || '').trim();
      const finalKind = String(document.getElementById('scriptKindInput')?.value || 'channel');
      const finalContent = String(document.getElementById('scriptContent')?.value || '');
      if (!finalName.endsWith('.py')) {
        throw new Error('File name must end with .py');
      }
      await api(`/api/scripts/${finalKind}/${encodeURIComponent(finalName)}`, { method: 'PUT', body: JSON.stringify({ content: finalContent }) });
      showFlash('Script saved');
      closeModal();
      await loadScriptsMeta();
      renderScriptsPage();
    }, 'Failed to save script');
  });
}

function renderKeywordsPage() {
  const page = document.getElementById('page-keywords');
  const links = state.keywordLinks || [];
  page.innerHTML = `
    <div class="card">
      <div class="row" style="justify-content:space-between">
        <h3>Keyword Links</h3>
        ${iconBtn({ title: 'Edit keyword links', icon: '✎', attrs: 'id="openKeywordsEditorBtn"' })}
      </div>
      <div class="meta-line">Total mappings: ${links.length}</div>
    </div>
  `;

  document.getElementById('openKeywordsEditorBtn')?.addEventListener('click', () => {
    const jsonText = JSON.stringify(links, null, 2);
    openModal(
      'Edit Keyword Links',
      `
        <p class="meta-line">Array of objects with destination/link/keywords</p>
        <textarea id="keywordsEditor" style="min-height:520px">${esc(jsonText)}</textarea>
        <div class="modal-actions">
          ${iconBtn({ title: 'Close', icon: '✕', attrs: 'id="keywordsCancelBtn"' })}
          ${iconBtn({ title: 'Save keyword links', icon: '✓', attrs: 'id="saveKeywordsBtn"' })}
        </div>
      `,
    );

    document.getElementById('keywordsCancelBtn')?.addEventListener('click', closeModal);
    document.getElementById('saveKeywordsBtn')?.addEventListener('click', async () => {
      await runAction(async () => {
        const text = document.getElementById('keywordsEditor')?.value || '[]';
        let parsed;
        try { parsed = JSON.parse(text); } catch (e) {
          throw new Error(`Invalid JSON: ${e}`);
        }
        await api('/api/keyword-links', { method: 'PUT', body: JSON.stringify({ links: parsed }) });
        showFlash('Keyword links saved');
        closeModal();
        await loadKeywords();
        renderKeywordsPage();
      }, 'Failed to save keyword links');
    });
  });
}

function renderSyncPage() {
  const page = document.getElementById('page-sync');
  const stats = state.sync?.stats || {};
  const reviews = state.sync?.reviews || [];
  const statusCounts = Object.entries(stats.status_counts || {}).map(([k, v]) => `<div class="badge">${esc(k)}: ${esc(v)}</div>`).join('');
  const reviewRows = reviews.map(r => `
    <tr>
      <td>${esc(r.id)}</td>
      <td>${esc(r.route_name)}</td>
      <td>${esc(r.reason || '-')}</td>
      <td>${esc(r.last_error || '-')}</td>
      <td>${esc(r.created_at || '-')}</td>
      <td>
        <div class="icon-actions">
          ${iconBtn({ title: 'Retry', icon: '↺', attrs: `data-review="${esc(r.id)}" data-action="retry"` })}
          ${iconBtn({ title: 'Skip', icon: '⏭', extraClass: 'btn-danger', attrs: `data-review="${esc(r.id)}" data-action="skip"` })}
        </div>
      </td>
    </tr>
  `).join('');
  page.innerHTML = `
    <div class="grid cols-4">
      ${card('Queue Depth', stats.queue_depth ?? 0)}
      ${card('Open Reviews', stats.open_reviews ?? 0)}
      ${card('Routes With Stats', Object.keys(stats.routes || {}).length)}
      <article class="card"><h3>Status Counts</h3><div class="row">${statusCounts || '<span class="muted">-</span>'}</div></article>
    </div>
    <div class="card" style="margin-top:10px">
      <h3>Review Queue</h3>
      <div class="table-wrap">
        <table>
          <thead><tr><th>ID</th><th>Route</th><th>Reason</th><th>Error</th><th>Created</th><th>Actions</th></tr></thead>
          <tbody>${reviewRows}</tbody>
        </table>
      </div>
    </div>
  `;
  page.querySelectorAll('button[data-review]').forEach(btn => {
    btn.addEventListener('click', async () => {
      await runAction(async () => {
        const id = btn.dataset.review;
        const action = btn.dataset.action;
        await api(`/api/sync/reviews/${id}/${action}`, { method: 'POST' });
        showFlash('Action completed');
        await reloadPageData('sync');
      }, 'Failed to process review action');
    });
  });
}

function renderLogsPage() {
  const page = document.getElementById('page-logs');
  const rows = (state.logs || []).map(item => {
    return `
      <tr>
        <td>${esc(item.ts || '-')}</td>
        <td>${badgeByStatus(item.level || '-')}</td>
        <td>${esc(item.logger || '-')}</td>
        <td><pre>${esc(item.message || '-')}</pre></td>
      </tr>
    `;
  }).join('');
  page.innerHTML = `
    <div class="row" style="justify-content:space-between;margin-bottom:8px">
      <h3>Recent Logs</h3>
      <div class="icon-actions">${iconBtn({ title: 'Refresh logs', icon: '↻', attrs: 'id="logsRefreshBtn"' })}</div>
    </div>
    <div class="row" style="margin-bottom:8px">
      <select id="logsLevelFilter" class="compact-input" style="width:120px">
        <option value="" ${state.logFilters.level === '' ? 'selected' : ''}>All levels</option>
        <option value="DEBUG" ${state.logFilters.level === 'DEBUG' ? 'selected' : ''}>DEBUG</option>
        <option value="INFO" ${state.logFilters.level === 'INFO' ? 'selected' : ''}>INFO</option>
        <option value="WARNING" ${state.logFilters.level === 'WARNING' ? 'selected' : ''}>WARNING</option>
        <option value="ERROR" ${state.logFilters.level === 'ERROR' ? 'selected' : ''}>ERROR</option>
        <option value="CRITICAL" ${state.logFilters.level === 'CRITICAL' ? 'selected' : ''}>CRITICAL</option>
      </select>
      <input id="logsLoggerFilter" class="compact-input" style="width:220px" placeholder="logger contains..." value="${esc(state.logFilters.logger || '')}">
      <input id="logsMessageFilter" class="compact-input" style="width:320px" placeholder="message contains..." value="${esc(state.logFilters.message || '')}">
      ${iconBtn({ title: 'Apply filters', icon: '✓', attrs: 'id="logsApplyFilterBtn"' })}
      ${iconBtn({ title: 'Clear filters', icon: '⌫', attrs: 'id="logsClearFilterBtn"' })}
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>ts</th><th>level</th><th>logger</th><th>message</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
  document.getElementById('logsRefreshBtn')?.addEventListener('click', async () => {
    await runAction(async () => {
      await reloadPageData('logs');
    }, 'Failed to refresh logs');
  });

  const applyFilters = async () => {
    state.logFilters.level = String(document.getElementById('logsLevelFilter')?.value || '').trim().toUpperCase();
    state.logFilters.logger = String(document.getElementById('logsLoggerFilter')?.value || '').trim();
    state.logFilters.message = String(document.getElementById('logsMessageFilter')?.value || '').trim();
    await runAction(async () => {
      await reloadPageData('logs');
    }, 'Failed to apply log filters');
  };
  document.getElementById('logsApplyFilterBtn')?.addEventListener('click', applyFilters);
  document.getElementById('logsClearFilterBtn')?.addEventListener('click', async () => {
    state.logFilters = { level: '', logger: '', message: '' };
    await runAction(async () => {
      await reloadPageData('logs');
    }, 'Failed to clear log filters');
  });
  document.getElementById('logsLoggerFilter')?.addEventListener('keydown', async (e) => {
    if (e.key === 'Enter') await applyFilters();
  });
  document.getElementById('logsMessageFilter')?.addEventListener('keydown', async (e) => {
    if (e.key === 'Enter') await applyFilters();
  });
  document.getElementById('logsLevelFilter')?.addEventListener('change', async () => {
    await applyFilters();
  });
}

function renderStoragePage() {
  const page = document.getElementById('page-storage');
  const summary = state.storage?.summary || {};
  const runs = state.storage?.runs || [];
  const rows = runs.map(r => `
    <tr>
      <td>${esc(r.updated_at || '-')}</td>
      <td>${esc(r.route || '-')}</td>
      <td>${esc(r.channel_id || '-')}</td>
      <td>${esc(r.run_id || '-')}</td>
      <td>${esc(r.message_id || '-')}</td>
      <td>
        <div class="icon-actions">
          ${iconBtn({ title: 'View payload', icon: '◫', attrs: `data-file="${esc(r.path + '/payload.json')}"` })}
          ${iconBtn({ title: 'View raw update', icon: '≣', attrs: `data-file="${esc(r.path + '/raw_update.json')}"` })}
          ${iconBtn({ title: 'View channel output', icon: '▤', attrs: `data-file="${esc(r.path + '/output/channel_messages.json')}"` })}
        </div>
      </td>
    </tr>
  `).join('');

  page.innerHTML = `
    <div class="grid cols-4">
      ${card('Channels', summary.channels ?? 0)}
      ${card('Runs', summary.runs ?? 0)}
      ${card('Input Files', summary.input_files ?? 0)}
      ${card('Output Files', summary.output_files ?? 0)}
    </div>
    <div class="card" style="margin-top:10px">
      <h3>Recent Runs</h3>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Updated</th><th>Route</th><th>Channel</th><th>Run</th><th>Message</th><th>Files</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>
  `;

  page.querySelectorAll('button[data-file]').forEach(btn => {
    btn.addEventListener('click', async () => {
      await runAction(async () => {
        const p = btn.dataset.file;
        const out = await api(`/api/storage/file?path=${encodeURIComponent(p)}`);
        openModal(
          `File Preview: ${out.file?.path || p}`,
          `
            <div class="meta-line">Size: ${esc(out.file?.size_bytes)} bytes${out.file?.truncated ? ' (truncated preview)' : ''}</div>
            <pre>${esc(out.file?.preview || '-')}</pre>
          `,
        );
      }, 'Failed to load storage file');
    });
  });
}

function renderTrafficPage() {
  const page = document.getElementById('page-traffic');
  const t = state.traffic || {};
  const byRoute = Array.isArray(t.by_route) ? t.by_route.slice() : [];
  const runs = Array.isArray(t.runs) ? t.runs.slice() : [];
  const previousRuns = Array.isArray(t.previous_runs) ? t.previous_runs.slice() : [];
  byRoute.sort((a, b) => {
    const av = Number(a?.total_download_bytes || 0) + Number(a?.total_upload_bytes || 0);
    const bv = Number(b?.total_download_bytes || 0) + Number(b?.total_upload_bytes || 0);
    return bv - av;
  });
  runs.sort((a, b) => Number(b?.started_at_ts || 0) - Number(a?.started_at_ts || 0));
  const rows = byRoute.map((item) => {
    const route = esc(item.route || '-');
    const td = Number(item.total_download_bytes || 0);
    const tu = Number(item.total_upload_bytes || 0);
    const dd = Number(item.today_download_bytes || 0);
    const du = Number(item.today_upload_bytes || 0);
    return `
      <tr>
        <td>${route}</td>
        <td>${formatBytes(td)}</td>
        <td>${formatBytes(tu)}</td>
        <td>${formatBytes(dd)}</td>
        <td>${formatBytes(du)}</td>
        <td>${formatBytes(td + tu)}</td>
      </tr>
    `;
  }).join('');
  const runRows = runs.map((item, idx) => {
    const d = Number(item.total_download_bytes || 0);
    const u = Number(item.total_upload_bytes || 0);
    const isActive = !!item.is_active;
    return `
      <tr>
        <td>${idx + 1}</td>
        <td>${esc(item.run_id || '-')}</td>
        <td>${formatDateTime(item.started_at)}</td>
        <td>${formatDateTime(item.stopped_at)}</td>
        <td>${formatDuration(item.duration_sec || 0)}</td>
        <td>${isActive ? '<span class="badge ok">active</span>' : '<span class="badge">stopped</span>'}</td>
        <td>${formatBytes(d)}</td>
        <td>${formatBytes(u)}</td>
        <td>${formatBytes(d + u)}</td>
        <td>${esc(item.route_count || 0)}</td>
      </tr>
    `;
  }).join('');

  page.innerHTML = `
    <div class="grid cols-4">
      ${card('Current Run Download', formatBytes(t.total_download_bytes || 0), 'this service run')}
      ${card('Current Run Upload', formatBytes(t.total_upload_bytes || 0), 'this service run')}
      ${card(`Today Download (${esc(t.day || '-')})`, formatBytes(t.today_download_bytes || 0), 'current day')}
      ${card(`Today Upload (${esc(t.day || '-')})`, formatBytes(t.today_upload_bytes || 0), 'current day')}
    </div>
    <div class="grid cols-4" style="margin-top:10px">
      ${card('Previous Runs Download', formatBytes(t.previous_total_download_bytes || 0), `${esc(t.previous_runs_count || 0)} runs`)}
      ${card('Previous Runs Upload', formatBytes(t.previous_total_upload_bytes || 0), `${esc(t.previous_runs_count || 0)} runs`)}
      ${card('All Runs Download', formatBytes(t.history_total_download_bytes || 0), `${esc(t.runs_count || 0)} runs`)}
      ${card('All Runs Upload', formatBytes(t.history_total_upload_bytes || 0), `${esc(t.runs_count || 0)} runs`)}
    </div>
    <div class="card" style="margin-top:10px">
      <h3>Service Run Traffic History</h3>
      <div class="meta-line">Previous runs: ${esc(previousRuns.length)} | Current run id: ${esc(t.run_id || '-')}</div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>#</th><th>Run ID</th><th>Started At</th><th>Stopped At</th><th>Duration</th><th>Status</th><th>Download</th><th>Upload</th><th>Total</th><th>Routes</th></tr></thead>
          <tbody>${runRows}</tbody>
        </table>
      </div>
    </div>
    <div class="card" style="margin-top:10px">
      <h3>Per-route Traffic</h3>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Route</th><th>Total Download</th><th>Total Upload</th><th>Today Download</th><th>Today Upload</th><th>Total Traffic</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>
  `;
}

function renderWorkersPage() {
  const page = document.getElementById('page-workers');
  const payload = state.workers || {};
  const workers = payload.workers || {};
  const sync = payload.sync || {};
  const host = payload.host || {};
  const cpu = host.cpu || {};
  const mem = host.memory || {};
  const cgroupMem = host.cgroup_memory || {};
  const disk = host.disk || {};
  const counts = sync.status_counts || {};

  const statusRows = Object.entries(counts).map(([k, v]) => `
    <tr>
      <td>${esc(k)}</td>
      <td>${esc(v)}</td>
    </tr>
  `).join('');

  page.innerHTML = `
    <div class="grid cols-4">
      ${card('Configured Workers', esc(workers.configured_count ?? 0), `backend=${esc(workers.queue_backend || '-')}`)}
      ${card('Estimated Busy Workers', esc(workers.estimated_busy_workers ?? 0), `drain=${workers.drain_task_running ? 'running' : 'idle'}`)}
      ${card('Queue Depth', esc(sync.queue_depth ?? 0), `syncing routes=${esc(sync.syncing_routes ?? 0)}`)}
      ${card('Open Reviews', esc(sync.open_reviews ?? 0), 'ambiguous/manual checks')}
    </div>
    <div class="grid cols-4" style="margin-top:10px">
      ${card('CPU Cores', esc(cpu.cores ?? 0), `load1=${esc(cpu.load_avg?.load1 ?? '-')}`)}
      ${card('CPU Load % (1m/core)', esc(cpu.load_pct_1m_per_core ?? '-'), 'higher means more pressure')}
      ${card('Host RAM Used', formatBytes(mem.used_bytes || 0), `${esc(mem.used_pct ?? '-')}% of ${formatBytes(mem.total_bytes || 0)}`)}
      ${card('Container RAM Used', formatBytes(cgroupMem.current_bytes || 0), cgroupMem.limit_bytes ? `${esc(cgroupMem.used_pct ?? '-')}% of ${formatBytes(cgroupMem.limit_bytes)}` : 'no hard cgroup limit')}
    </div>
    <div class="grid cols-3" style="margin-top:10px">
      ${card('Host Uptime', formatDuration(host.uptime_sec || 0), 'from /proc/uptime')}
      ${card('Disk Used (Storage Dir)', formatBytes(disk.used_bytes || 0), `${esc(disk.used_pct ?? '-')}% of ${formatBytes(disk.total_bytes || 0)}`)}
      ${card('Worker Limits', `route_inflight=${esc(workers.route_max_inflight ?? '-')}`, `lock_ttl=${esc(workers.lock_ttl_sec ?? '-')}s | retry_base=${esc(workers.retry_base_sec ?? '-')}s`)}
    </div>
    <div class="card" style="margin-top:10px">
      <h3>Sync Ledger Status Counts</h3>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Status</th><th>Count</th></tr></thead>
          <tbody>${statusRows}</tbody>
        </table>
      </div>
    </div>
  `;
}

function renderSystemPage() {
  const page = document.getElementById('page-system');
  const health = state.health || {};
  const checks = health.checks || {};
  const settings = state.settings || {};
  const checkCards = Object.entries(checks).map(([k, v]) => `
    <article class="card">
      <h3>${esc(k)}</h3>
      <div>${badgeByStatus(v.status || 'unknown')}</div>
      <pre>${esc(JSON.stringify(v, null, 2))}</pre>
    </article>
  `).join('');

  page.innerHTML = `
    <div class="card">
      <h3>System Health Summary</h3>
      <div class="row">${badgeByStatus(health.summary || 'unknown')}</div>
    </div>
    <div class="grid cols-3" style="margin-top:10px">${checkCards}</div>
    <div class="card" style="margin-top:10px">
      <h3>Settings (sanitized)</h3>
      <pre>${esc(JSON.stringify(settings, null, 2))}</pre>
    </div>
  `;
}

function renderAdminsPage() {
  const page = document.getElementById('page-admins');
  const rows = (state.admins || []).map((u, idx) => `
    <tr>
      <td>${esc(u)}</td>
      <td class="actions-cell">${iconBtn({ title: `Delete admin ${u}`, icon: '✕', extraClass: 'btn-danger', attrs: `data-user-idx="${idx}"` })}</td>
    </tr>
  `).join('');
  page.innerHTML = `
    <div class="card">
      <div class="row" style="justify-content:space-between">
        <h3>Admins</h3>
        ${iconBtn({ title: 'Add admin', icon: '+', attrs: 'id="openAddAdminBtn"' })}
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Username</th><th>Action</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>
  `;

  page.querySelectorAll('button[data-user-idx]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const idx = Number(btn.dataset.userIdx || '-1');
      const user = state.admins[idx];
      if (!user) return;
      if (!confirm(`Delete admin ${user}?`)) return;
      await runAction(async () => {
        await api(`/api/admin/users/${encodeURIComponent(user)}`, { method: 'DELETE' });
        showFlash('Admin deleted');
        await reloadPageData('admins');
      }, 'Failed to delete admin');
    });
  });

  document.getElementById('openAddAdminBtn')?.addEventListener('click', async () => {
    openModal(
      'Add Admin',
      `
        <div class="modal-grid">
          <label>Username <input id="newAdminUser" placeholder="username"></label>
          <label>Password <input id="newAdminPass" placeholder="password" type="password"></label>
        </div>
        <div class="modal-actions">
          ${iconBtn({ title: 'Close', icon: '✕', attrs: 'id="addAdminCancelBtn"' })}
          ${iconBtn({ title: 'Add admin', icon: '✓', attrs: 'id="addAdminBtn"' })}
        </div>
      `,
    );
    document.getElementById('addAdminCancelBtn')?.addEventListener('click', closeModal);
    document.getElementById('addAdminBtn')?.addEventListener('click', async () => {
      await runAction(async () => {
        const u = document.getElementById('newAdminUser')?.value.trim();
        const p = document.getElementById('newAdminPass')?.value;
        if (!u || !p) {
          throw new Error('Username and password are required');
        }
        await api('/api/admin/users', { method: 'POST', body: JSON.stringify({ username: u, password: p }) });
        showFlash('Admin added');
        closeModal();
        await reloadPageData('admins');
      }, 'Failed to add admin');
    });
  });
}

function renderCurrentPage() {
  if (state.currentPage === 'dashboard') return renderDashboardPage();
  if (state.currentPage === 'routes') return renderRoutesPage();
  if (state.currentPage === 'scripts') return renderScriptsPage();
  if (state.currentPage === 'keywords') return renderKeywordsPage();
  if (state.currentPage === 'sync') return renderSyncPage();
  if (state.currentPage === 'logs') return renderLogsPage();
  if (state.currentPage === 'traffic') return renderTrafficPage();
  if (state.currentPage === 'workers') return renderWorkersPage();
  if (state.currentPage === 'storage') return renderStoragePage();
  if (state.currentPage === 'system') return renderSystemPage();
  if (state.currentPage === 'admins') return renderAdminsPage();
}

async function reloadPageData(page = state.currentPage) {
  const focusedFieldSnapshot = captureFocusedFieldSnapshot();
  try {
    if (page === 'dashboard') await loadDashboard();
    if (page === 'routes') await loadRoutes();
    if (page === 'scripts') await loadScriptsMeta();
    if (page === 'keywords') await loadKeywords();
    if (page === 'sync') await loadSync();
    if (page === 'logs') await loadLogs();
    if (page === 'traffic') await loadTraffic();
    if (page === 'workers') await loadWorkers();
    if (page === 'storage') await loadStorage();
    if (page === 'system') await loadSystem();
    if (page === 'admins') await loadAdmins();
    renderCurrentPage();
    restoreFocusedFieldSnapshot(focusedFieldSnapshot);
  } catch (e) {
    showFlash(`Error: ${e.message}`, true);
  }
}

function canAutoRefresh(page = state.currentPage) {
  if (!state.user) return false;
  if (!state.autoRefreshEnabled) return false;
  if (document.hidden) return false;
  if (isModalOpen()) return false;
  if (state.autoRefreshRunning) return false;
  if (page === 'scripts' || page === 'keywords' || page === 'admins') return false;
  return true;
}

function startAutoRefresh() {
  if (state.autoRefreshTimer) clearInterval(state.autoRefreshTimer);
  refreshAutoRefreshUi();
  state.autoRefreshTimer = setInterval(async () => {
    if (!canAutoRefresh()) return;
    state.autoRefreshRunning = true;
    refreshAutoRefreshUi();
    try {
      await reloadPageData(state.currentPage);
    } finally {
      state.autoRefreshRunning = false;
      refreshAutoRefreshUi();
    }
  }, state.autoRefreshMs);
}

async function verifyAuth() {
  try {
    const me = await api('/api/auth/me');
    state.user = me.username;
    return true;
  } catch (_) {
    state.user = null;
    return false;
  }
}

async function bootstrap() {
  buildMenu();

  els.modalCloseBtn?.addEventListener('click', closeModal);
  els.modalOverlay?.addEventListener('click', (e) => {
    if (e.target === els.modalOverlay) closeModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && isModalOpen()) closeModal();
  });

  els.loginForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    els.loginError.textContent = '';
    try {
      const username = els.loginUser.value.trim();
      const password = els.loginPass.value;
      const out = await api('/api/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) });
      state.user = out.username;
      await enterApp();
    } catch (err) {
      els.loginError.textContent = 'Login failed';
    }
  });

  els.logoutBtn.addEventListener('click', async () => {
    await runAction(async () => {
      await api('/api/auth/logout', { method: 'POST' });
      location.reload();
    }, 'Logout failed');
  });

  els.reloadBtn.addEventListener('click', () => {
    runAction(async () => {
      await reloadPageData();
    }, 'Reload failed');
  });
  els.toggleAutoRefreshBtn?.addEventListener('click', () => {
    state.autoRefreshEnabled = !state.autoRefreshEnabled;
    refreshAutoRefreshUi();
    showFlash(state.autoRefreshEnabled ? 'Auto refresh resumed' : 'Auto refresh paused');
  });
  els.stopServiceBtn.addEventListener('click', async () => {
    if (!confirm('Stop Kiwi service?')) return;
    await runAction(async () => {
      await api('/api/actions/stop-service', { method: 'POST' });
      showFlash('Stop request sent');
    }, 'Failed to send stop request');
  });

  const ok = await verifyAuth();
  if (ok) {
    await enterApp();
  } else {
    els.loginView.classList.remove('hidden');
    els.app.classList.add('hidden');
  }
}

async function enterApp() {
  els.loginView.classList.add('hidden');
  els.app.classList.remove('hidden');
  els.meInfo.textContent = `User: ${state.user}`;
  await Promise.all([loadDashboard(), loadRoutes(), loadScriptsMeta(), loadKeywords(), loadSync(), loadLogs(), loadTraffic(), loadWorkers(), loadStorage(), loadSystem(), loadAdmins()]);
  navigate(state.currentPage);
  startAutoRefresh();
  refreshAutoRefreshUi();
}

bootstrap().catch(err => {
  console.error(err);
  showFlash(`Panel bootstrap error: ${err.message}`, true);
});
