const state = {
  user: null,
  currentPage: 'dashboard',
  routeFilter: '',
  routeSort: { key: 'name', dir: 'asc' },
  tablePrefs: {},
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
  monitor: { messages: [], summary: {}, latestSeq: 0 },
  monitorFilters: {
    route: '',
    status: '',
    search: '',
    activeOnly: true,
  },
  monitorEventSeq: 0,
  monitorEventSeen: new Set(),
  monitorLifecycle: null,
  realtime: {
    channels: {},
    pageReloadDebounceTimer: null,
    wsLastCloseAt: 0,
    wsLastCloseText: '',
    config: null,
    configFetchedAt: 0,
  },
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
  ui: {
    lastInteractionAt: 0,
  },
};

const menuItems = [
  ['dashboard', 'Dashboard', 'Service overview and stats'],
  ['routes', 'Routes', 'Full route management'],
  ['scripts', 'Scripts', 'Edit channel and guard scripts'],
  ['keywords', 'Keywords', 'Manage keyword links'],
  ['sync', 'Sync Queue', 'Queue and review status'],
  ['monitor', 'Message Monitor', 'Per-message pipeline and progress'],
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

const DEFAULT_PAGE_SIZE = 25;
const PAGE_SIZE_OPTIONS = [10, 25, 50, 100, 200];
const PAGE_SCOPES = {
  dashboard: ['dashboard_cards'],
  routes: ['routes_table'],
  scripts: ['scripts_meta'],
  keywords: ['keywords_table'],
  sync: ['sync_reviews_table'],
  monitor: ['monitor_messages_table'],
  logs: ['logs_table'],
  traffic: ['traffic_runs_table', 'traffic_routes_table'],
  workers: ['workers_status_table'],
  storage: ['storage_runs_table'],
  system: ['system_checks_table'],
  admins: ['admins_table'],
};
const WS_FAILURE_SUSPEND_AFTER = 6;
const WS_SUSPEND_MS = 120000;

function parseNumberLike(value) {
  const text = String(value || '').trim().toLowerCase();
  if (!text) return null;
  const compact = text.replaceAll(',', '');
  const pct = compact.match(/^(-?\d+(?:\.\d+)?)\s*%$/);
  if (pct) return Number(pct[1]);
  const bytes = compact.match(/^(-?\d+(?:\.\d+)?)\s*(b|kb|mb|gb|tb)$/i);
  if (bytes) {
    const n = Number(bytes[1]);
    const unit = String(bytes[2] || '').toLowerCase();
    const mul = unit === 'tb' ? 1024 ** 4 : unit === 'gb' ? 1024 ** 3 : unit === 'mb' ? 1024 ** 2 : unit === 'kb' ? 1024 : 1;
    if (Number.isFinite(n)) return n * mul;
  }
  const plain = compact.match(/^-?\d+(?:\.\d+)?$/);
  if (plain) return Number(compact);
  return null;
}

function parseSortToken(value) {
  const text = String(value ?? '').trim();
  const num = parseNumberLike(text);
  if (Number.isFinite(num)) return { kind: 'num', value: num };
  return { kind: 'str', value: text.toLocaleLowerCase() };
}

function getTablePref(tableId, colCount) {
  const key = String(tableId || '').trim();
  if (!key) {
    return {
      sortCol: null,
      sortDir: 'asc',
      page: 1,
      pageSize: DEFAULT_PAGE_SIZE,
      colOrder: Array.from({ length: colCount }, (_, i) => i),
    };
  }
  const raw = state.tablePrefs[key];
  const defOrder = Array.from({ length: colCount }, (_, i) => i);
  const pref = {
    sortCol: null,
    sortDir: 'asc',
    page: 1,
    pageSize: DEFAULT_PAGE_SIZE,
    colOrder: defOrder,
    ...(raw && typeof raw === 'object' ? raw : {}),
  };
  pref.sortDir = pref.sortDir === 'desc' ? 'desc' : 'asc';
  pref.pageSize = PAGE_SIZE_OPTIONS.includes(Number(pref.pageSize)) ? Number(pref.pageSize) : DEFAULT_PAGE_SIZE;
  pref.page = Math.max(1, Number(pref.page) || 1);
  const used = new Set();
  const fixedOrder = [];
  for (const v of Array.isArray(pref.colOrder) ? pref.colOrder : []) {
    const idx = Number(v);
    if (!Number.isInteger(idx) || idx < 0 || idx >= colCount || used.has(idx)) continue;
    used.add(idx);
    fixedOrder.push(idx);
  }
  for (let i = 0; i < colCount; i += 1) {
    if (!used.has(i)) fixedOrder.push(i);
  }
  pref.colOrder = fixedOrder;
  if (!Number.isInteger(pref.sortCol) || pref.sortCol < 0 || pref.sortCol >= colCount) {
    pref.sortCol = null;
  }
  state.tablePrefs[key] = pref;
  return pref;
}

function saveTablePref(tableId, pref) {
  const key = String(tableId || '').trim();
  if (!key) return;
  state.tablePrefs[key] = {
    sortCol: pref.sortCol,
    sortDir: pref.sortDir,
    page: pref.page,
    pageSize: pref.pageSize,
    colOrder: Array.isArray(pref.colOrder) ? pref.colOrder.slice() : [],
  };
}

function moveArrayItem(items, fromIndex, toIndex) {
  const out = items.slice();
  if (fromIndex < 0 || fromIndex >= out.length || toIndex < 0 || toIndex >= out.length) return out;
  const [picked] = out.splice(fromIndex, 1);
  out.splice(toIndex, 0, picked);
  return out;
}

function enhanceTable(table, { pageId, index }) {
  if (!table || !(table instanceof HTMLTableElement)) return;
  const tbody = table.tBodies && table.tBodies[0];
  const thead = table.tHead;
  if (!tbody || !thead || !thead.rows || !thead.rows.length) return;
  const headerRow = thead.rows[0];
  const headerCells = Array.from(headerRow.cells);
  const bodyRows = Array.from(tbody.rows);
  const colCount = headerCells.length;
  if (colCount <= 0) return;

  const tableId = String(table.dataset.tableId || `${pageId}_table_${index + 1}`).trim();
  table.dataset.tableId = tableId;
  const pref = getTablePref(tableId, colCount);
  const rowCells = bodyRows.map((row) => Array.from(row.cells));

  const sortableCols = new Set();
  headerCells.forEach((th, originalCol) => {
    th.classList.add('dt-th');
    th.draggable = true;
    if (th.dataset.noSort === 'true') return;
    if (th.querySelector('input, select, textarea')) return;
    if (th.querySelector('button.th-sort')) return;
    sortableCols.add(originalCol);
  });

  const wrap = table.closest('.table-wrap');
  let pager = null;
  if (wrap) {
    const existingPager = wrap.parentElement?.querySelector(`.table-pager[data-table-id="${cssEsc(tableId)}"]`);
    if (existingPager) existingPager.remove();
    pager = document.createElement('div');
    pager.className = 'table-pager';
    pager.dataset.tableId = tableId;
    pager.innerHTML = `
      <div class="table-pager-left">
        <label>Rows
          <select class="table-page-size">
            ${PAGE_SIZE_OPTIONS.map((n) => `<option value="${n}" ${Number(pref.pageSize) === n ? 'selected' : ''}>${n}</option>`).join('')}
          </select>
        </label>
      </div>
      <div class="table-pager-right">
        <button type="button" class="btn table-page-prev">Prev</button>
        <span class="table-page-info muted">Page 1 / 1</span>
        <button type="button" class="btn table-page-next">Next</button>
      </div>
    `;
    wrap.insertAdjacentElement('afterend', pager);
  }

  const render = () => {
    const order = pref.colOrder.slice();
    headerRow.append(...order.map((i) => headerCells[i]));
    for (let r = 0; r < bodyRows.length; r += 1) {
      const row = bodyRows[r];
      const cells = rowCells[r] || [];
      row.append(...order.map((i) => cells[i]).filter(Boolean));
    }

    const sortedRows = bodyRows.slice();
    if (Number.isInteger(pref.sortCol) && sortableCols.has(pref.sortCol)) {
      const sortCol = Number(pref.sortCol);
      sortedRows.sort((a, b) => {
        const ia = bodyRows.indexOf(a);
        const ib = bodyRows.indexOf(b);
        const ca = rowCells[ia]?.[sortCol];
        const cb = rowCells[ib]?.[sortCol];
        const va = parseSortToken(ca?.textContent || '');
        const vb = parseSortToken(cb?.textContent || '');
        let cmp = 0;
        if (va.kind === 'num' && vb.kind === 'num') {
          cmp = Number(va.value) - Number(vb.value);
        } else {
          cmp = String(va.value).localeCompare(String(vb.value), undefined, { sensitivity: 'base', numeric: true });
        }
        if (cmp === 0) {
          cmp = ia - ib;
        }
        return pref.sortDir === 'desc' ? -cmp : cmp;
      });
    }
    for (const row of sortedRows) tbody.appendChild(row);

    const totalRows = sortedRows.length;
    const pageSize = Math.max(1, Number(pref.pageSize) || DEFAULT_PAGE_SIZE);
    const totalPages = Math.max(1, Math.ceil(totalRows / pageSize));
    pref.page = Math.min(totalPages, Math.max(1, Number(pref.page) || 1));
    const start = (pref.page - 1) * pageSize;
    const end = start + pageSize;
    sortedRows.forEach((row, idx) => {
      row.classList.toggle('dt-row-hidden', idx < start || idx >= end);
    });

    if (pager) {
      const info = pager.querySelector('.table-page-info');
      const prev = pager.querySelector('.table-page-prev');
      const next = pager.querySelector('.table-page-next');
      const sizeSelect = pager.querySelector('.table-page-size');
      if (info) info.textContent = `Page ${pref.page} / ${totalPages} (${totalRows} rows)`;
      if (prev) prev.disabled = pref.page <= 1;
      if (next) next.disabled = pref.page >= totalPages;
      if (sizeSelect && Number(sizeSelect.value) !== pageSize) sizeSelect.value = String(pageSize);
    }

    headerCells.forEach((th, originalCol) => {
      th.classList.toggle('dt-sortable', sortableCols.has(originalCol));
      th.classList.toggle('dt-sorted', Number(pref.sortCol) === originalCol && sortableCols.has(originalCol));
      th.classList.remove('dt-sort-asc', 'dt-sort-desc');
      if (Number(pref.sortCol) === originalCol && sortableCols.has(originalCol)) {
        th.classList.add(pref.sortDir === 'desc' ? 'dt-sort-desc' : 'dt-sort-asc');
      }
    });
    saveTablePref(tableId, pref);
  };

  headerCells.forEach((th) => {
    th.addEventListener('dragstart', (evt) => {
      const displayIndex = Array.from(headerRow.cells).indexOf(th);
      evt.dataTransfer?.setData('text/plain', String(displayIndex));
      evt.dataTransfer.effectAllowed = 'move';
      th.classList.add('dt-dragging');
    });
    th.addEventListener('dragend', () => {
      th.classList.remove('dt-dragging');
      headerCells.forEach((cell) => cell.classList.remove('dt-drop-target'));
    });
    th.addEventListener('dragover', (evt) => {
      evt.preventDefault();
      const target = evt.currentTarget;
      if (!(target instanceof HTMLTableCellElement)) return;
      headerCells.forEach((cell) => cell.classList.remove('dt-drop-target'));
      target.classList.add('dt-drop-target');
    });
    th.addEventListener('dragleave', () => {
      th.classList.remove('dt-drop-target');
    });
    th.addEventListener('drop', (evt) => {
      evt.preventDefault();
      headerCells.forEach((cell) => cell.classList.remove('dt-drop-target'));
      const fromDisplay = Number(evt.dataTransfer?.getData('text/plain'));
      const toDisplay = Array.from(headerRow.cells).indexOf(th);
      if (!Number.isInteger(fromDisplay) || fromDisplay < 0 || toDisplay < 0 || fromDisplay === toDisplay) return;
      pref.colOrder = moveArrayItem(pref.colOrder, fromDisplay, toDisplay);
      render();
    });
  });

  headerCells.forEach((th, originalCol) => {
    if (!sortableCols.has(originalCol)) return;
    th.addEventListener('click', (evt) => {
      if (evt.target instanceof HTMLElement && evt.target.closest('button, a, input, select, textarea')) return;
      if (pref.sortCol === originalCol) {
        pref.sortDir = pref.sortDir === 'asc' ? 'desc' : 'asc';
      } else {
        pref.sortCol = originalCol;
        pref.sortDir = 'asc';
      }
      pref.page = 1;
      render();
    });
  });

  if (pager) {
    pager.querySelector('.table-page-prev')?.addEventListener('click', () => {
      pref.page = Math.max(1, Number(pref.page || 1) - 1);
      render();
    });
    pager.querySelector('.table-page-next')?.addEventListener('click', () => {
      pref.page = Math.max(1, Number(pref.page || 1) + 1);
      render();
    });
    pager.querySelector('.table-page-size')?.addEventListener('change', (evt) => {
      const value = Number(evt.target?.value || DEFAULT_PAGE_SIZE);
      pref.pageSize = PAGE_SIZE_OPTIONS.includes(value) ? value : DEFAULT_PAGE_SIZE;
      pref.page = 1;
      render();
    });
  }

  render();
}

function enhanceTablesForPage(page) {
  const pageEl = document.getElementById(`page-${page}`);
  if (!pageEl) return;
  const tables = Array.from(pageEl.querySelectorAll('.table-wrap table'));
  tables.forEach((table, idx) => enhanceTable(table, { pageId: page, index: idx }));
}

function collectScopesFromPage(page) {
  const out = new Set();
  const pageEl = document.getElementById(`page-${page}`);
  if (pageEl) {
    pageEl.querySelectorAll('[data-ws-scope]').forEach((node) => {
      const raw = String(node.getAttribute('data-ws-scope') || '');
      raw.split(',').map((x) => x.trim()).filter(Boolean).forEach((scope) => out.add(scope));
    });
  }
  const defaults = PAGE_SCOPES[page] || [];
  defaults.forEach((scope) => out.add(scope));
  return Array.from(out);
}

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
  const realtime = state.realtime || {};
  const channels = Object.values(realtime.channels || {});
  const anyConnected = channels.some((ch) => !!ch?.wsConnected);
  const anyConnecting = channels.some((ch) => !!ch?.wsConnecting);
  const anyConfigured = channels.some((ch) => !!ch?.wsConfigured);
  const now = Date.now();
  const anySuspended = channels.some((ch) => Number(ch?.wsSuspendedUntil || 0) > now);
  if (!state.autoRefreshEnabled) {
    setAutoRefreshText('Realtime: paused');
  } else if (anyConnected) {
    setAutoRefreshText('Realtime: live');
  } else if (anyConnecting) {
    setAutoRefreshText('Realtime: connecting...');
  } else if (anySuspended) {
    setAutoRefreshText('Realtime: polling active');
  } else if (anyConfigured) {
    setAutoRefreshText('Realtime: reconnecting (polling active)...');
  } else {
    setAutoRefreshText('Realtime: disconnected');
  }
  if (!els.toggleAutoRefreshBtn) return;
  if (state.autoRefreshEnabled) {
    els.toggleAutoRefreshBtn.textContent = '⏸';
    els.toggleAutoRefreshBtn.title = 'Pause realtime updates';
    els.toggleAutoRefreshBtn.setAttribute('aria-label', 'Pause realtime updates');
  } else {
    els.toggleAutoRefreshBtn.textContent = '▶';
    els.toggleAutoRefreshBtn.title = 'Resume realtime updates';
    els.toggleAutoRefreshBtn.setAttribute('aria-label', 'Resume realtime updates');
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
    value: null,
    checked: null,
  };
  if (tag === 'input' || tag === 'textarea' || tag === 'select') {
    snap.value = typeof el.value === 'string' ? el.value : null;
  }
  if (tag === 'input' && ('checked' in el)) {
    snap.checked = !!el.checked;
  }
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
  if (snap && snap.tag === 'select' && typeof snap.value === 'string' && 'value' in el) {
    try { el.value = snap.value; } catch (_) {}
  }
  if (snap && (snap.tag === 'input' || snap.tag === 'textarea') && typeof snap.value === 'string' && 'value' in el) {
    try { el.value = snap.value; } catch (_) {}
  }
  if (snap && snap.tag === 'input' && typeof snap.checked === 'boolean' && 'checked' in el) {
    try { el.checked = snap.checked; } catch (_) {}
  }
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

function captureScrollSnapshot(page = state.currentPage) {
  const pageEl = document.getElementById(`page-${String(page || '')}`);
  const wraps = pageEl ? Array.from(pageEl.querySelectorAll('.table-wrap')) : [];
  return {
    winX: Number(window.scrollX || 0),
    winY: Number(window.scrollY || 0),
    pageTop: Number(pageEl?.scrollTop || 0),
    pageLeft: Number(pageEl?.scrollLeft || 0),
    wraps: wraps.map((el, idx) => ({
      idx,
      top: Number(el.scrollTop || 0),
      left: Number(el.scrollLeft || 0),
    })),
  };
}

function restoreScrollSnapshot(snapshot, page = state.currentPage) {
  if (!snapshot || typeof snapshot !== 'object') return;
  const pageEl = document.getElementById(`page-${String(page || '')}`);
  const wraps = pageEl ? Array.from(pageEl.querySelectorAll('.table-wrap')) : [];
  requestAnimationFrame(() => {
    try {
      window.scrollTo(Number(snapshot.winX || 0), Number(snapshot.winY || 0));
    } catch (_) {
      // no-op
    }
    if (pageEl) {
      pageEl.scrollTop = Number(snapshot.pageTop || 0);
      pageEl.scrollLeft = Number(snapshot.pageLeft || 0);
    }
    const savedWraps = Array.isArray(snapshot.wraps) ? snapshot.wraps : [];
    for (const saved of savedWraps) {
      const idx = Number(saved?.idx ?? -1);
      if (!Number.isInteger(idx) || idx < 0 || idx >= wraps.length) continue;
      const el = wraps[idx];
      el.scrollTop = Number(saved?.top || 0);
      el.scrollLeft = Number(saved?.left || 0);
    }
  });
}

function isFieldEditingActive() {
  const el = document.activeElement;
  if (!el || !(el instanceof HTMLElement)) return false;
  const tag = String(el.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return true;
  if (el.isContentEditable) return true;
  return false;
}

function markUserInteraction() {
  state.ui.lastInteractionAt = Date.now();
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
  state.monitorLifecycle = null;
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

function formatPercent(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '-';
  return `${n.toFixed(1)}%`;
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
  state.keywordLinks = normalizeKeywordLinksForUi(data.links || []);
}

function normalizeKeywordTerm(item, fallbackPriority = 0) {
  if (typeof item === 'string') {
    const text = String(item || '').trim();
    if (!text) return null;
    return { keyword: text, priority: Number(fallbackPriority) || 0 };
  }
  if (!item || typeof item !== 'object') return null;
  const text = String(item.keyword || item.text || item.term || '').trim();
  if (!text) return null;
  const prRaw = item.priority;
  const priority = Number.isFinite(Number(prRaw)) ? Number(prRaw) : (Number(fallbackPriority) || 0);
  return { keyword: text, priority: Math.trunc(priority) };
}

function normalizeKeywordEntry(item) {
  if (!item || typeof item !== 'object') return null;
  const destination = String(item.destination || '').trim();
  const link = String(item.link || '').trim();
  if (!destination) return null;
  const entryPriority = Number.isFinite(Number(item.priority)) ? Number(item.priority) : 0;
  const rawKeywords = Array.isArray(item.keywords) ? item.keywords : [];
  const terms = [];
  const seen = new Set();
  for (const raw of rawKeywords) {
    const parsed = normalizeKeywordTerm(raw, entryPriority);
    if (!parsed) continue;
    const dedupeKey = parsed.keyword.toLocaleLowerCase();
    if (seen.has(dedupeKey)) continue;
    seen.add(dedupeKey);
    terms.push(parsed);
  }
  if (!terms.length) return null;
  terms.sort((a, b) => {
    const byPr = Number(b.priority || 0) - Number(a.priority || 0);
    if (byPr !== 0) return byPr;
    return String(b.keyword || '').length - String(a.keyword || '').length;
  });
  return { destination, link, keywords: terms };
}

function normalizeKeywordLinksForUi(items) {
  if (!Array.isArray(items)) return [];
  const out = [];
  for (const item of items) {
    const parsed = normalizeKeywordEntry(item);
    if (parsed) out.push(parsed);
  }
  out.sort((a, b) => String(a.destination || '').localeCompare(String(b.destination || ''), undefined, { sensitivity: 'base' }));
  return out;
}

function serializeKeywordLinksForApi(items) {
  return normalizeKeywordLinksForUi(items).map((entry) => ({
    destination: String(entry.destination || '').trim(),
    link: String(entry.link || '').trim(),
    keywords: (entry.keywords || []).map((kw) => ({
      keyword: String(kw.keyword || '').trim(),
      priority: Math.trunc(Number(kw.priority || 0)),
    })),
  }));
}

async function loadSync() {
  const [stats, reviews] = await Promise.all([
    api('/api/sync/stats'),
    api('/api/sync/reviews?limit=100&only_open=true'),
  ]);
  state.sync = { stats: stats.stats || {}, reviews: reviews.reviews || [] };
}

async function loadMonitor() {
  const params = new URLSearchParams();
  params.set('limit', '500');
  if (state.monitorFilters.route) params.set('route', state.monitorFilters.route);
  if (state.monitorFilters.status) params.set('status', state.monitorFilters.status);
  if (state.monitorFilters.search) params.set('search', state.monitorFilters.search);
  if (state.monitorFilters.activeOnly) params.set('active_only', 'true');
  const data = await api(`/api/monitor/messages?${params.toString()}`);
  state.monitor = {
    messages: data.messages || [],
    summary: data.summary || {},
    latestSeq: Number(data.latest_seq || 0),
  };
  state.monitorEventSeq = Math.max(Number(state.monitorEventSeq || 0), Number(data.latest_seq || 0));
}

function notificationTextFromMonitorEvent(evt) {
  const route = String(evt.route_name || '-');
  const msgId = String(evt.message_id || '-');
  const status = String(evt.status || '').toLowerCase();
  const stage = String(evt.stage || '-');
  const err = String(evt.error || '').trim();
  const details = String(evt.details || '').trim();
  if (status === 'sent') {
    return `✅ ${route} | msg ${msgId} sent`;
  }
  if (status === 'blocked') {
    return `⛔ ${route} | msg ${msgId} blocked${err ? ` | ${err}` : ''}`;
  }
  if (status === 'failed') {
    return `❌ ${route} | msg ${msgId} failed${err ? ` | ${err}` : ''}`;
  }
  if (status === 'ambiguous') {
    return `⚠️ ${route} | msg ${msgId} needs review${err ? ` | ${err}` : ''}`;
  }
  if (evt.event_type === 'stage' && (stage === 'dispatch' || stage === 'guard' || stage === 'channel_script')) {
    return `ℹ️ ${route} | msg ${msgId} ${stage}${details ? ` | ${details}` : ''}`;
  }
  return '';
}

async function pollMonitorEvents() {
  const params = new URLSearchParams();
  params.set('after_seq', String(Math.max(0, Number(state.monitorEventSeq || 0))));
  params.set('limit', '300');
  const data = await api(`/api/monitor/events?${params.toString()}`);
  const events = Array.isArray(data.events) ? data.events : [];
  state.monitorEventSeq = Math.max(Number(state.monitorEventSeq || 0), Number(data.latest_seq || 0));
  applyMonitorEvents(events);
}

function applyMonitorEventToTrackedMessage(evt) {
  if (!evt || typeof evt !== "object") return;
  const key = String(evt.dedupe_key || "").trim();
  if (!key) return;
  const item = getMonitorMessageByDedupeKey(key);
  if (!item) return;

  const status = String(evt.status || item.status || "").trim().toLowerCase() || "processing";
  const stage = String(evt.stage || item.current_stage || "").trim().toLowerCase() || "processing";
  const ts = String(evt.ts || "").trim();
  const progressRaw = Number(evt.progress_pct);
  const progress = Number.isFinite(progressRaw) ? Math.max(0, Math.min(100, progressRaw)) : Number(item.progress_pct || 0);

  item.status = status;
  item.current_stage = stage;
  item.updated_at = ts || item.updated_at;
  item.progress_pct = Number.isFinite(progress) ? progress : Number(item.progress_pct || 0);
  if (Number.isFinite(Number(evt.attempt_count))) item.attempt_count = Math.max(0, Number(evt.attempt_count));
  if (Number.isFinite(Number(evt.download_bytes))) item.download_bytes = Math.max(0, Number(evt.download_bytes));
  if (Number.isFinite(Number(evt.upload_bytes))) item.upload_bytes = Math.max(0, Number(evt.upload_bytes));
  const errorText = String(evt.error || "").trim();
  if (errorText) item.last_error = errorText;
  if (evt.is_terminal) item.completed_at = ts || item.completed_at || new Date().toISOString();

  const details = String(evt.details || "").trim() || errorText;
  const history = Array.isArray(item.stage_history) ? item.stage_history.slice() : [];
  history.push({
    ts: ts || new Date().toISOString(),
    stage,
    status,
    progress_pct: Math.round((Number(item.progress_pct || 0) || 0) * 10) / 10,
    details: details || null,
  });
  if (history.length > 120) {
    item.stage_history = history.slice(-120);
  } else {
    item.stage_history = history;
  }
}

function applyMonitorEvents(events) {
  for (const evt of (Array.isArray(events) ? events : [])) {
    const seq = Number(evt.seq || 0);
    if (!Number.isFinite(seq) || seq <= 0) continue;
    if (state.monitorEventSeen.has(seq)) continue;
    state.monitorEventSeen.add(seq);
    applyMonitorEventToTrackedMessage(evt);
    const text = notificationTextFromMonitorEvent(evt);
    if (!text) continue;
    const status = String(evt.status || '').toLowerCase();
    const isErr = status === 'failed' || status === 'blocked' || status === 'ambiguous';
    showFlash(text, isErr);
  }
  if (state.monitorLifecycle && isModalOpen()) {
    renderMonitorLifecycleModalContent();
  }
  if (state.monitorEventSeen.size > 5000) {
    const keep = Array.from(state.monitorEventSeen).sort((a, b) => b - a).slice(0, 2500);
    state.monitorEventSeen = new Set(keep);
  }
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
      ${card('Waiting Routes', routes.sync_waiting ?? 0, 'status=sync_waiting')}
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
  if (status === 'sync_waiting') return '<span class="badge warn">sync_waiting</span>';
  if (status === 'syncing') return '<span class="badge warn">syncing</span>';
  if (status === 'deactive') return '<span class="badge err">deactive</span>';
  return '<span class="badge">-</span>';
}

function effectiveRouteStatus(routeStatus, metrics, runtimeStatus) {
  const runtime = String(runtimeStatus || '').toLowerCase();
  if (runtime === 'deactive' || runtime === 'syncing' || runtime === 'sync_waiting' || runtime === 'synced') {
    return runtime;
  }
  const normalized = String(routeStatus || 'deactive').toLowerCase();
  if (normalized === 'deactive' || normalized === 'syncing' || normalized === 'synced') {
    return normalized;
  }
  // Fallback for legacy/malformed statuses: infer from metrics.
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
  if (key === 'source') return String(route?.source_channel_username || route?.source_channel_id || '');
  if (key === 'source_topic_id') return Number(route?.source_topic_id ?? 0);
  if (key === 'destination') return String(route?.destination_channel_username || route?.destination_channel_id || '');
  if (key === 'channel_script') return String(route?.channel_script || '');
  if (key === 'guard') return String(route?.gaurd_script || '');
  if (key === 'status') return effectiveRouteStatus(route?.status, metrics, route?.runtime_status);
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
      r?.source_channel_id,
      r?.source_topic_id,
      r?.destination_channel_username,
      r?.destination_channel_id,
      r?.channel_script,
      r?.gaurd_script,
      r?.status,
      r?.runtime_status,
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
    const source = esc(r.source_channel_username || r.source_channel_id || '-');
    const sourceTopicId = Number(r?.source_topic_id ?? 0);
    const sourceTopicText = sourceTopicId > 0 ? String(sourceTopicId) : '-';
    const dest = esc(r.destination_channel_username || r.destination_channel_id || '-');
    const cs = esc(r.channel_script || '-');
    const gs = esc(r.gaurd_script || '-');
    const m = state.routeMetrics && r.name ? state.routeMetrics[String(r.name)] : null;
    const routeStatus = effectiveRouteStatus(r.status, m, r.runtime_status);
    const isDeactive = routeStatus === 'deactive';
    const waitRemainingSec = Number(r?.wait_remaining_sec ?? 0);
    const canSendOneUnsynced = routeStatus === 'deactive' || routeStatus === 'sync_waiting';
    const remaining = Number(m?.remaining_unsynced ?? 0);
    const progressPct = Number(m?.progress_pct ?? 0);
    const remainingLabel = isDeactive ? '-' : String(remaining);
    const progressLabel = isDeactive ? '-' : (Number.isFinite(progressPct) ? `${progressPct.toFixed(1)}%` : '-');
    const progressBadge = isDeactive ? '' : (progressPct >= 95 ? 'ok' : (progressPct >= 60 ? 'warn' : 'err'));
    const statusDetail = routeStatus === 'sync_waiting' && waitRemainingSec > 0
      ? `<div class="muted">next in ${esc(formatDuration(waitRemainingSec))}</div>`
      : '';
    return `
      <tr>
        <td class="route-col-name" title="${name}">${name}</td>
        <td class="route-col-source" title="${source}">${source}</td>
        <td title="${esc(sourceTopicText)}">${esc(sourceTopicText)}</td>
        <td class="route-col-destination" title="${dest}">${dest}</td>
        <td>${cs}</td>
        <td>${gs}</td>
        <td>${routeStatusPill(routeStatus)}${statusDetail}</td>
        <td>${remainingLabel}</td>
        <td><span class="badge ${progressBadge}">${progressLabel}</span></td>
        <td class="actions-cell">
          <div class="icon-actions">
            ${iconBtn({ act: 'edit', title: `Edit ${r.name || ''}`, icon: '✎', attrs: `data-route-name="${routeKey}"` })}
            ${iconBtn({ act: 'toggle', title: routeStatus === 'deactive' ? 'Start route' : 'Stop route', icon: routeStatus === 'deactive' ? '▶' : '⏸', attrs: `data-route-name="${routeKey}" data-status="${routeStatus}"` })}
            ${iconBtn({ act: 'sync-one', title: canSendOneUnsynced ? 'Send one unsynced now' : 'Available on deactive/sync_waiting routes', icon: '⇢', attrs: `data-route-name="${routeKey}"${canSendOneUnsynced ? '' : ' disabled aria-disabled="true"'}` })}
            ${iconBtn({ act: 'force-sync', title: 'Force sync', icon: '↻', attrs: `data-route-name="${routeKey}"` })}
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
        <table class="routes-table" data-table-id="routes-main" data-ws-scope="routes_table">
          <thead>
            <tr>
              <th class="route-col-name"><button class="th-sort" data-sort-key="name">Name <span class="sort-arrow">${routeSortIndicator('name')}</span></button></th>
              <th class="route-col-source"><button class="th-sort" data-sort-key="source">Source <span class="sort-arrow">${routeSortIndicator('source')}</span></button></th>
              <th><button class="th-sort" data-sort-key="source_topic_id">Source Topic ID <span class="sort-arrow">${routeSortIndicator('source_topic_id')}</span></button></th>
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
      if (act === 'force-sync') {
        if (!confirm(`Force sync route ${name}?\nThis clears checkpoint and queued sync data for this route.`)) return;
        await runAction(async () => {
          const out = await api(`/api/routes/${encodeURIComponent(name)}/sync/force`, { method: 'POST' });
          const removed = Number(out?.reset?.deleted_ledger_rows || 0);
          showFlash(`Force sync started${removed > 0 ? ` (${removed} records reset)` : ''}`);
          await reloadPageData('routes');
        }, 'Failed to force sync route');
        return;
      }
      if (act === 'sync-one') {
        await runAction(async () => {
          const out = await api(`/api/routes/${encodeURIComponent(name)}/sync/send-one`, { method: 'POST' });
          const result = out?.result || {};
          const processed = Number(result.processed || 0);
          const note = String(result.note || '');
          if (processed > 0) {
            showFlash('One unsynced message sent');
          } else if (note) {
            showFlash(`No message sent (${note})`);
          } else {
            showFlash('No message sent');
          }
          await reloadPageData('routes');
        }, 'Failed to send one unsynced message');
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
    name: '', status: 'synced', source_channel_username: '', source_channel_id: '',
    source_topic_id: null,
    destination_channel_username: '', destination_channel_id: '',
    channel_script: '', gaurd_script: 'default_guard.py', max_message_mb: 60,
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
        <label>source id <input name="source_channel_id" value="${esc(r.source_channel_id || '')}" placeholder="-1001234567890"></label>
        <label>source topic id <input type="number" min="1" name="source_topic_id" value="${esc(r.source_topic_id ?? '')}" placeholder="e.g. 1556041"></label>
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
        source_channel_id: (String(fd.get('source_channel_id') || '').trim() || null),
        source_topic_id: (() => {
          const raw = String(fd.get('source_topic_id') || '').trim();
          if (!raw) return null;
          const n = Number(raw);
          if (!Number.isFinite(n) || n <= 0) return null;
          return Math.trunc(n);
        })(),
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
      if (!payload.source_channel_username && !payload.source_channel_id) {
        throw new Error('At least one of source username or source id is required');
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
  const links = normalizeKeywordLinksForUi(state.keywordLinks || []);
  state.keywordLinks = links;
  const totalKeywords = links.reduce((acc, item) => acc + (Array.isArray(item.keywords) ? item.keywords.length : 0), 0);
  const rows = links.map((entry, idx) => {
    const keywords = Array.isArray(entry.keywords) ? entry.keywords : [];
    const topPriority = keywords.length ? Math.max(...keywords.map((x) => Number(x.priority || 0))) : 0;
    const chips = keywords.slice(0, 5).map((kw) => (
      `<span class="badge">${esc(kw.keyword)} <small>#${esc(kw.priority)}</small></span>`
    )).join('');
    const more = keywords.length > 5 ? `<span class="muted">+${keywords.length - 5} more</span>` : '';
    return `
      <tr>
        <td>${esc(entry.destination || '-')}</td>
        <td>${entry.link ? `<a href="${esc(entry.link)}" target="_blank" rel="noreferrer">${esc(entry.link)}</a>` : '<span class="muted">auto</span>'}</td>
        <td>${keywords.length}</td>
        <td><span class="badge">${esc(topPriority)}</span></td>
        <td><div class="keyword-chip-wrap">${chips}${more}</div></td>
        <td class="actions-cell">
          <div class="icon-actions">
            ${iconBtn({ title: 'Edit mapping', icon: '✎', attrs: `data-kword-act="edit" data-kword-idx="${idx}"` })}
            ${iconBtn({ title: 'Delete mapping', icon: '✕', extraClass: 'btn-danger', attrs: `data-kword-act="delete" data-kword-idx="${idx}"` })}
          </div>
        </td>
      </tr>
    `;
  }).join('');
  page.innerHTML = `
    <div class="card">
      <div class="row" style="justify-content:space-between">
        <h3>Keyword Links</h3>
        <div class="row">
          ${iconBtn({ title: 'Add keyword mapping', icon: '+', attrs: 'id="addKeywordMapBtn"' })}
          ${iconBtn({ title: 'Save all changes', icon: '✓', attrs: 'id="saveKeywordMapBtn"' })}
        </div>
      </div>
      <div class="meta-line">Mappings: ${links.length} | Keywords: ${totalKeywords}</div>
      <div class="table-wrap" style="margin-top:8px">
        <table data-table-id="keywords-main" data-ws-scope="keywords_table">
          <thead>
            <tr>
              <th>Destination</th>
              <th>Link</th>
              <th>Keywords</th>
              <th>Top Priority</th>
              <th>Preview</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>${rows || '<tr><td colspan="6"><span class="muted">No keyword mapping yet</span></td></tr>'}</tbody>
        </table>
      </div>
    </div>
  `;

  document.getElementById('addKeywordMapBtn')?.addEventListener('click', () => openKeywordMappingModal(null));
  document.getElementById('saveKeywordMapBtn')?.addEventListener('click', async () => {
    await runAction(async () => {
      const payload = serializeKeywordLinksForApi(state.keywordLinks || []);
      await api('/api/keyword-links', { method: 'PUT', body: JSON.stringify({ links: payload }) });
      showFlash('Keyword links saved');
      await loadKeywords();
      renderKeywordsPage();
    }, 'Failed to save keyword links');
  });

  page.querySelectorAll('button[data-kword-act]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const idx = Number(btn.dataset.kwordIdx);
      if (!Number.isInteger(idx) || idx < 0 || idx >= links.length) return;
      const act = String(btn.dataset.kwordAct || '');
      if (act === 'edit') {
        openKeywordMappingModal(idx);
        return;
      }
      if (act === 'delete') {
        if (!confirm(`Delete keyword mapping for ${links[idx].destination}?`)) return;
        state.keywordLinks = links.filter((_, i) => i !== idx);
        renderKeywordsPage();
      }
    });
  });
}

function openKeywordMappingModal(editIndex) {
  const links = normalizeKeywordLinksForUi(state.keywordLinks || []);
  const isEdit = Number.isInteger(editIndex) && editIndex >= 0 && editIndex < links.length;
  const current = isEdit ? links[editIndex] : { destination: '', link: '', keywords: [{ keyword: '', priority: 100 }] };
  const keywordRows = (current.keywords || []).map((kw, idx) => `
    <div class="keyword-row" data-keyword-row="${idx}">
      <input data-kword-field="keyword" placeholder="keyword" value="${esc(kw.keyword || '')}">
      <input data-kword-field="priority" type="number" step="1" placeholder="priority" value="${esc(kw.priority ?? 0)}">
      ${iconBtn({ title: 'Remove keyword', icon: '✕', extraClass: 'btn-danger', attrs: 'data-kword-row-act="remove" type="button"' })}
    </div>
  `).join('');

  openModal(
    isEdit ? `Edit Keywords: ${current.destination}` : 'Add Keyword Mapping',
    `
      <form id="keywordMapForm" class="stack">
        <div class="modal-grid">
          <label>Destination <input id="keywordDestinationInput" required value="${esc(current.destination || '')}" placeholder="@channel"></label>
          <label>Link (optional) <input id="keywordLinkInput" value="${esc(current.link || '')}" placeholder="https://ble.ir/channel"></label>
        </div>
        <div class="row" style="justify-content:space-between">
          <h4 style="margin:0">Keywords</h4>
          ${iconBtn({ title: 'Add keyword', icon: '+', attrs: 'id="addKeywordRowBtn" type="button"' })}
        </div>
        <div class="meta-line">Higher priority wins when keywords overlap. Example: \"لیگ قهرمانان اروپا\" > \"لیگ قهرمانان\"</div>
        <div id="keywordRowsWrap" class="stack">${keywordRows || `
          <div class="keyword-row" data-keyword-row="0">
            <input data-kword-field="keyword" placeholder="keyword" value="">
            <input data-kword-field="priority" type="number" step="1" placeholder="priority" value="100">
            ${iconBtn({ title: 'Remove keyword', icon: '✕', extraClass: 'btn-danger', attrs: 'data-kword-row-act="remove" type="button"' })}
          </div>
        `}</div>
      </form>
      <div class="modal-actions">
        ${iconBtn({ title: 'Close', icon: '✕', attrs: 'id="keywordMapCancelBtn"' })}
        ${iconBtn({ title: 'Save mapping', icon: '✓', attrs: 'id="keywordMapSaveBtn"' })}
      </div>
    `,
  );

  const rowsWrap = document.getElementById('keywordRowsWrap');
  const buildKeywordRow = (keyword = '', priority = 100) => `
    <div class="keyword-row">
      <input data-kword-field="keyword" placeholder="keyword" value="${esc(keyword)}">
      <input data-kword-field="priority" type="number" step="1" placeholder="priority" value="${esc(priority)}">
      ${iconBtn({ title: 'Remove keyword', icon: '✕', extraClass: 'btn-danger', attrs: 'data-kword-row-act="remove" type="button"' })}
    </div>
  `;
  const bindRemoveButtons = () => {
    rowsWrap?.querySelectorAll('button[data-kword-row-act="remove"]').forEach((btn) => {
      btn.onclick = () => {
        const row = btn.closest('.keyword-row');
        row?.remove();
      };
    });
  };
  bindRemoveButtons();

  document.getElementById('addKeywordRowBtn')?.addEventListener('click', (e) => {
    e.preventDefault();
    if (!rowsWrap) return;
    rowsWrap.insertAdjacentHTML('beforeend', buildKeywordRow('', 100));
    bindRemoveButtons();
  });

  document.getElementById('keywordMapCancelBtn')?.addEventListener('click', closeModal);
  document.getElementById('keywordMapSaveBtn')?.addEventListener('click', () => {
    const destination = String(document.getElementById('keywordDestinationInput')?.value || '').trim();
    const link = String(document.getElementById('keywordLinkInput')?.value || '').trim();
    if (!destination) {
      showFlash('Destination is required', true);
      return;
    }
    const keywordRowsEls = Array.from(rowsWrap?.querySelectorAll('.keyword-row') || []);
    const keywords = [];
    const seen = new Set();
    for (const row of keywordRowsEls) {
      const keyword = String(row.querySelector('input[data-kword-field="keyword"]')?.value || '').trim();
      const rawPriority = Number(row.querySelector('input[data-kword-field="priority"]')?.value || 0);
      const priority = Number.isFinite(rawPriority) ? Math.trunc(rawPriority) : 0;
      if (!keyword) continue;
      const dedupeKey = keyword.toLocaleLowerCase();
      if (seen.has(dedupeKey)) continue;
      seen.add(dedupeKey);
      keywords.push({ keyword, priority });
    }
    if (!keywords.length) {
      showFlash('At least one keyword is required', true);
      return;
    }

    const normalized = normalizeKeywordEntry({ destination, link, keywords });
    if (!normalized) {
      showFlash('Invalid keyword mapping', true);
      return;
    }
    const next = links.slice();
    if (isEdit) {
      next[editIndex] = normalized;
    } else {
      next.push(normalized);
    }
    state.keywordLinks = normalizeKeywordLinksForUi(next);
    closeModal();
    renderKeywordsPage();
  });
}

function monitorStatusBadge(status) {
  const s = String(status || '').toLowerCase();
  if (s === 'sent') return '<span class="badge ok">sent</span>';
  if (s === 'blocked') return '<span class="badge err">blocked</span>';
  if (s === 'failed') return '<span class="badge err">failed</span>';
  if (s === 'ambiguous') return '<span class="badge warn">ambiguous</span>';
  if (s === 'processing') return '<span class="badge warn">processing</span>';
  if (s === 'queued') return '<span class="badge">queued</span>';
  return `<span class="badge">${esc(status || '-')}</span>`;
}

function renderProgressBar(value) {
  const pct = Math.max(0, Math.min(100, Number(value || 0)));
  return `
    <div class="monitor-progress" title="${pct.toFixed(1)}%">
      <div class="monitor-progress-fill" style="width:${pct.toFixed(1)}%"></div>
    </div>
    <div class="muted">${pct.toFixed(1)}%</div>
  `;
}

function buildSourceMessageLink(item) {
  const messageId = Number(item?.message_id || 0);
  if (!Number.isFinite(messageId) || messageId <= 0) return null;
  const usernameRaw = String(item?.source_channel_username || '').trim();
  if (usernameRaw.startsWith('@') && usernameRaw.length > 1) {
    return `https://t.me/${encodeURIComponent(usernameRaw.slice(1))}/${Math.trunc(messageId)}`;
  }
  const sourceId = String(item?.source_channel_id || '').trim();
  if (sourceId.startsWith('-100')) {
    const tail = sourceId.slice(4);
    if (/^\d+$/.test(tail)) {
      return `https://t.me/c/${tail}/${Math.trunc(messageId)}`;
    }
  }
  return null;
}

function monitorStatusClass(status) {
  const s = String(status || "").toLowerCase();
  if (s === "sent") return "ok";
  if (s === "processing" || s === "queued") return "warn";
  if (s === "failed" || s === "blocked" || s === "ambiguous") return "err";
  return "";
}

function formatMonitorStageName(stage) {
  const raw = String(stage || "").trim();
  if (!raw) return "-";
  return raw.replaceAll("_", " ");
}

function parseIsoTs(value) {
  const text = String(value || "").trim();
  if (!text) return Number.NaN;
  const out = Date.parse(text);
  return Number.isFinite(out) ? out : Number.NaN;
}

function normalizeLifecycleHistory(item) {
  const raw = Array.isArray(item?.stage_history) ? item.stage_history : [];
  const normalized = raw
    .map((step) => ({
      ts: String(step?.ts || "").trim(),
      stage: String(step?.stage || "").trim().toLowerCase() || "processing",
      status: String(step?.status || item?.status || "processing").trim().toLowerCase() || "processing",
      progress_pct: Number(step?.progress_pct ?? item?.progress_pct ?? 0),
      details: step?.details != null ? String(step.details) : "",
    }))
    .filter((step) => step.stage);
  normalized.sort((a, b) => {
    const ta = parseIsoTs(a.ts);
    const tb = parseIsoTs(b.ts);
    if (Number.isFinite(ta) && Number.isFinite(tb) && ta !== tb) return ta - tb;
    return String(a.ts).localeCompare(String(b.ts));
  });
  return normalized;
}

function buildLifecyclePipelineStates(history, fallbackStatus = "") {
  const base = ["queued", "resolve", "download", "guard", "channel_script", "dispatch", "status"];
  const seen = new Set(base);
  const ordered = base.slice();
  for (const h of history) {
    const stage = String(h?.stage || "").trim().toLowerCase();
    if (!stage || seen.has(stage)) continue;
    seen.add(stage);
    ordered.push(stage);
  }
  const latestIndex = history.length ? ordered.indexOf(String(history[history.length - 1]?.stage || "").toLowerCase()) : -1;
  const hasFailure = history.some((step) => {
    const status = String(step?.status || "").toLowerCase();
    return status === "failed" || status === "blocked" || status === "ambiguous";
  });
  return ordered.map((stageName, idx) => {
    const stages = history.filter((h) => String(h?.stage || "").toLowerCase() === stageName);
    const last = stages.length ? stages[stages.length - 1] : null;
    const status = String(last?.status || fallbackStatus || "").toLowerCase();
    const isFailure = status === "failed" || status === "blocked" || status === "ambiguous";
    let stateKind = "pending";
    if (stages.length > 0) stateKind = "done";
    if (idx === latestIndex && stages.length > 0) stateKind = "active";
    if (isFailure) stateKind = "failed";
    if (hasFailure && stageName === "status" && stages.length > 0 && isFailure) stateKind = "failed";
    return {
      stage: stageName,
      state: stateKind,
      attempts: stages.length,
      status,
      details: String(last?.details || "").trim(),
    };
  });
}

function lifecycleSummaryFromHistory(history) {
  let failedCount = 0;
  let ambiguousCount = 0;
  let blockedCount = 0;
  for (const step of history) {
    const status = String(step?.status || "").toLowerCase();
    if (status === "failed") failedCount += 1;
    if (status === "ambiguous") ambiguousCount += 1;
    if (status === "blocked") blockedCount += 1;
  }
  return { failedCount, ambiguousCount, blockedCount };
}

function getMonitorMessageByDedupeKey(dedupeKey) {
  const key = String(dedupeKey || "").trim();
  if (!key) return null;
  const rows = Array.isArray(state.monitor?.messages) ? state.monitor.messages : [];
  return rows.find((item) => String(item?.dedupe_key || "").trim() === key) || null;
}

function resolveLifecycleMessage() {
  const lifecycle = state.monitorLifecycle;
  if (!lifecycle || !lifecycle.dedupeKey) return null;
  const fromMonitor = getMonitorMessageByDedupeKey(lifecycle.dedupeKey);
  if (fromMonitor) return fromMonitor;
  return lifecycle.fallbackMessage || null;
}

function renderMonitorLifecycleModalContent() {
  const root = document.getElementById("monitorLifecycleRoot");
  if (!root) return;
  const lifecycle = state.monitorLifecycle;
  const message = resolveLifecycleMessage();
  if (!lifecycle || !message) {
    root.innerHTML = '<div class="muted">No lifecycle data available for this message.</div>';
    return;
  }

  const status = String(message.status || "").toLowerCase();
  const progress = Math.max(0, Math.min(100, Number(message.progress_pct || 0)));
  const attempts = Math.max(0, Number(message.attempt_count || 0));
  const history = normalizeLifecycleHistory(message);
  const pipeline = buildLifecyclePipelineStates(history, status);
  const retryInfo = lifecycleSummaryFromHistory(history);
  const stageCards = pipeline.map((node) => {
    const cls = node.state === "active" ? "active" : node.state === "done" ? "done" : node.state === "failed" ? "failed" : "";
    const attemptsLabel = node.attempts > 1 ? `<span class="muted">x${node.attempts}</span>` : "";
    return `
      <div class="lifecycle-stage ${cls}">
        <span class="dot"></span>
        <div class="stage-name">${esc(formatMonitorStageName(node.stage))}</div>
        <div class="stage-meta">
          ${node.status ? `<span class="badge ${monitorStatusClass(node.status)}">${esc(node.status)}</span>` : '<span class="badge">-</span>'}
          ${attemptsLabel}
        </div>
      </div>
    `;
  }).join("");

  const steps = history.map((step, idx) => {
    const statusClass = monitorStatusClass(step.status);
    const isLast = idx === history.length - 1;
    const details = String(step.details || "").trim();
    const progressText = Number.isFinite(Number(step.progress_pct)) ? `${Number(step.progress_pct).toFixed(1)}%` : "-";
    return `
      <div class="lifecycle-event ${isLast ? "is-last" : ""}">
        <div class="lifecycle-event-node ${statusClass}"></div>
        <div class="lifecycle-event-body">
          <div class="lifecycle-event-head">
            <strong>${esc(formatMonitorStageName(step.stage))}</strong>
            <span class="badge ${statusClass}">${esc(step.status || "-")}</span>
            <span class="muted">${esc(formatDateTime(step.ts))}</span>
          </div>
          <div class="lifecycle-event-meta">
            <span>progress: <strong>${esc(progressText)}</strong></span>
          </div>
          ${details ? `<div class="lifecycle-event-details">${esc(details)}</div>` : ""}
        </div>
      </div>
    `;
  }).join("");

  const sourceLink = buildSourceMessageLink(message);
  const sourceLinkHtml = sourceLink
    ? `<a href="${esc(sourceLink)}" target="_blank" rel="noreferrer">Open source message</a>`
    : '<span class="muted">Source link not available</span>';
  const downloadBytes = Number(message.download_bytes || 0);
  const uploadBytes = Number(message.upload_bytes || 0);

  root.innerHTML = `
    <section class="lifecycle-shell">
      <div class="lifecycle-top">
        <div class="lifecycle-id-block">
          <div><strong>${esc(message.route_name || "-")}</strong> · msg ${esc(message.message_id || "-")}</div>
          <div class="muted">${esc(message.dedupe_key || "-")}</div>
          <div class="muted">${sourceLinkHtml}</div>
        </div>
        <div class="lifecycle-status-block">
          <div>${monitorStatusBadge(message.status)}</div>
          <div class="muted">attempts: ${esc(attempts)}</div>
        </div>
      </div>

      <div class="lifecycle-kpis">
        <div class="lifecycle-kpi"><span>Progress</span><strong>${esc(progress.toFixed(1))}%</strong></div>
        <div class="lifecycle-kpi"><span>Failed</span><strong>${esc(retryInfo.failedCount)}</strong></div>
        <div class="lifecycle-kpi"><span>Ambiguous</span><strong>${esc(retryInfo.ambiguousCount)}</strong></div>
        <div class="lifecycle-kpi"><span>Blocked</span><strong>${esc(retryInfo.blockedCount)}</strong></div>
        <div class="lifecycle-kpi"><span>Download</span><strong>${esc(formatBytes(downloadBytes))}</strong></div>
        <div class="lifecycle-kpi"><span>Upload</span><strong>${esc(formatBytes(uploadBytes))}</strong></div>
      </div>

      <div class="monitor-progress lifecycle-progress" title="${progress.toFixed(1)}%">
        <div class="monitor-progress-fill" style="width:${progress.toFixed(1)}%"></div>
      </div>

      <div class="lifecycle-pipeline">${stageCards || '<span class="muted">No stage data</span>'}</div>

      <div class="lifecycle-events-wrap">
        <h4>Live Lifecycle Timeline</h4>
        <div class="lifecycle-events">${steps || '<div class="muted">No timeline events yet.</div>'}</div>
      </div>
    </section>
  `;
}

function openMonitorLifecycleModal(item) {
  const dedupeKey = String(item?.dedupe_key || "").trim();
  if (!dedupeKey) return;
  state.monitorLifecycle = {
    dedupeKey,
    fallbackMessage: item ? JSON.parse(JSON.stringify(item)) : null,
  };
  openModal("Message Lifecycle", '<div id="monitorLifecycleRoot"></div>');
  renderMonitorLifecycleModalContent();
}

function renderMonitorPage() {
  const page = document.getElementById('page-monitor');
  const monitor = state.monitor || {};
  const summary = monitor.summary || {};
  const rowsDataRaw = Array.isArray(monitor.messages) ? monitor.messages : [];
  const monitorOrder = { processing: 0, failed: 1, ambiguous: 2, queued: 3, blocked: 4, sent: 5 };
  const rowsData = rowsDataRaw.slice().sort((a, b) => {
    const sa = String(a?.status || '').toLowerCase();
    const sb = String(b?.status || '').toLowerCase();
    const pa = Number.isFinite(monitorOrder[sa]) ? monitorOrder[sa] : 99;
    const pb = Number.isFinite(monitorOrder[sb]) ? monitorOrder[sb] : 99;
    if (pa !== pb) return pa - pb;
    const ua = String(a?.updated_at || '');
    const ub = String(b?.updated_at || '');
    return ub.localeCompare(ua);
  });
  const statusCounts = summary.status_counts || {};
  const routeOptions = Array.from(new Set(rowsData.map((m) => String(m.route_name || '').trim()).filter(Boolean))).sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));
  const rowsByKey = new Map(rowsData.map((item) => [String(item?.dedupe_key || "").trim(), item]));
  const rows = rowsData.map((item) => {
    const history = Array.isArray(item.stage_history) ? item.stage_history : [];
    const timeline = history.slice(-5).map((h) => `<span class="badge">${esc(h.stage)}:${esc(h.status)}</span>`).join('');
    const textPreview = String(item.text_preview || '').trim();
    const textCell = textPreview ? `<div class="monitor-preview">${esc(textPreview)}</div>` : '<span class="muted">-</span>';
    const downloadBytes = Number(item.download_bytes || 0);
    const uploadBytes = Number(item.upload_bytes || 0);
    const totalBytes = downloadBytes + uploadBytes;
    const sourceLink = buildSourceMessageLink(item);
    const sourceLinkHtml = sourceLink
      ? `<a href="${esc(sourceLink)}" target="_blank" rel="noreferrer">open source</a>`
      : '<span class="muted">-</span>';
    return `
      <tr>
        <td class="monitor-col-message">
          <div><strong>${esc(item.route_name || '-')}</strong></div>
          <div class="muted">msg=${esc(item.message_id || '-')} | ${esc(item.source_channel_username || item.source_channel_id || '-')}</div>
          <div class="muted">src: ${sourceLinkHtml}</div>
          <div class="muted">${esc(item.dedupe_key || '-')}</div>
        </td>
        <td>${monitorStatusBadge(item.status)}</td>
        <td>${esc(item.current_stage || '-')}</td>
        <td>${renderProgressBar(item.progress_pct)}</td>
        <td>${esc(item.attempt_count || 0)}</td>
        <td>
          <div class="muted">↓ ${formatBytes(downloadBytes)}</div>
          <div class="muted">↑ ${formatBytes(uploadBytes)}</div>
          <div><strong>${formatBytes(totalBytes)}</strong></div>
        </td>
        <td>${timeline || '<span class="muted">-</span>'}</td>
        <td>${esc(item.last_error || '-')}</td>
        <td>
          <div class="muted">${formatDateTime(item.first_seen_at)}</div>
          <div class="muted">${formatDateTime(item.updated_at)}</div>
          <div class="muted">${formatDateTime(item.completed_at)}</div>
        </td>
        <td>${textCell}</td>
        <td class="actions-cell">
          <div class="icon-actions">
            ${iconBtn({ title: "View lifecycle", icon: "👁", attrs: `data-monitor-view="${esc(item.dedupe_key || "")}"` })}
          </div>
        </td>
      </tr>
    `;
  }).join('');

  page.innerHTML = `
    <div class="grid cols-4">
      ${card('Tracked Messages', summary.total ?? 0, 'monitor memory')}
      ${card('Queued', statusCounts.queued ?? 0, 'waiting in queue')}
      ${card('Processing', statusCounts.processing ?? 0, 'in worker pipeline')}
      ${card('Failed/Review', (Number(statusCounts.failed || 0) + Number(statusCounts.ambiguous || 0)), 'needs retry/review')}
    </div>
    <div class="grid cols-4" style="margin-top:10px">
      ${card('Sent', statusCounts.sent ?? 0, 'delivered')}
      ${card('Blocked', statusCounts.blocked ?? 0, 'policy/routing blocked')}
      ${card('Download Traffic', formatBytes(summary.total_download_bytes || 0), 'tracked per message')}
      ${card('Upload Traffic', formatBytes(summary.total_upload_bytes || 0), 'tracked per message')}
    </div>
    <div class="card" style="margin-top:10px">
      <div class="row" style="justify-content:space-between">
        <h3>Per-message Pipeline Monitor</h3>
        <div class="row">
          <button id="monitorReloadBtn" class="btn">Reload</button>
        </div>
      </div>
      <div class="row" style="margin-top:6px">
        <select id="monitorRouteFilter" class="compact-input" style="width:220px">
          <option value="">All routes</option>
          ${routeOptions.map((name) => `<option value="${esc(name)}" ${state.monitorFilters.route === name ? 'selected' : ''}>${esc(name)}</option>`).join('')}
        </select>
        <select id="monitorStatusFilter" class="compact-input" style="width:180px">
          <option value="" ${state.monitorFilters.status === '' ? 'selected' : ''}>All statuses</option>
          <option value="queued" ${state.monitorFilters.status === 'queued' ? 'selected' : ''}>queued</option>
          <option value="processing" ${state.monitorFilters.status === 'processing' ? 'selected' : ''}>processing</option>
          <option value="failed" ${state.monitorFilters.status === 'failed' ? 'selected' : ''}>failed</option>
          <option value="ambiguous" ${state.monitorFilters.status === 'ambiguous' ? 'selected' : ''}>ambiguous</option>
          <option value="blocked" ${state.monitorFilters.status === 'blocked' ? 'selected' : ''}>blocked</option>
          <option value="sent" ${state.monitorFilters.status === 'sent' ? 'selected' : ''}>sent</option>
        </select>
        <label class="monitor-active-flag"><input id="monitorActiveOnly" type="checkbox" ${state.monitorFilters.activeOnly ? 'checked' : ''}> active only</label>
        <input id="monitorSearchInput" class="compact-input" style="width:340px" placeholder="search dedupe/route/message/error/text..." value="${esc(state.monitorFilters.search || '')}">
        <button id="monitorApplyBtn" class="btn">Apply</button>
      </div>
      <div class="table-wrap" style="margin-top:8px">
        <table class="monitor-table" data-table-id="monitor-main" data-ws-scope="monitor_messages_table">
          <thead>
            <tr>
              <th class="monitor-col-message">Message</th>
              <th>Status</th>
              <th>Stage</th>
              <th>Progress</th>
              <th>Attempts</th>
              <th>Traffic</th>
              <th>Timeline</th>
              <th>Error</th>
              <th>Timestamps</th>
              <th>Preview</th>
              <th data-no-sort="true">Actions</th>
            </tr>
          </thead>
          <tbody>${rows || '<tr><td colspan="11"><span class="muted">No tracked messages yet</span></td></tr>'}</tbody>
        </table>
      </div>
    </div>
  `;

  const applyFilters = async () => {
    state.monitorFilters.route = String(document.getElementById('monitorRouteFilter')?.value || '').trim();
    state.monitorFilters.status = String(document.getElementById('monitorStatusFilter')?.value || '').trim();
    state.monitorFilters.search = String(document.getElementById('monitorSearchInput')?.value || '').trim();
    state.monitorFilters.activeOnly = !!document.getElementById('monitorActiveOnly')?.checked;
    await runAction(async () => {
      await reloadPageData('monitor');
    }, 'Failed to load monitor data');
  };
  document.getElementById('monitorReloadBtn')?.addEventListener('click', applyFilters);
  document.getElementById('monitorApplyBtn')?.addEventListener('click', applyFilters);
  document.getElementById('monitorRouteFilter')?.addEventListener('change', applyFilters);
  document.getElementById('monitorStatusFilter')?.addEventListener('change', applyFilters);
  document.getElementById('monitorActiveOnly')?.addEventListener('change', applyFilters);
  document.getElementById('monitorSearchInput')?.addEventListener('keydown', async (e) => {
    if (e.key === 'Enter') await applyFilters();
  });
  page.querySelectorAll("button[data-monitor-view]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const key = String(btn.getAttribute("data-monitor-view") || "").trim();
      if (!key) return;
      const item = rowsByKey.get(key);
      if (!item) return;
      openMonitorLifecycleModal(item);
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
        <table data-table-id="sync-reviews" data-ws-scope="sync_reviews_table">
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
      <table data-table-id="logs-main" data-ws-scope="logs_table">
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
        <table data-table-id="storage-runs" data-ws-scope="storage_runs_table">
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
        <table data-table-id="traffic-runs" data-ws-scope="traffic_runs_table">
          <thead><tr><th>#</th><th>Run ID</th><th>Started At</th><th>Stopped At</th><th>Duration</th><th>Status</th><th>Download</th><th>Upload</th><th>Total</th><th>Routes</th></tr></thead>
          <tbody>${runRows}</tbody>
        </table>
      </div>
    </div>
    <div class="card" style="margin-top:10px">
      <h3>Per-route Traffic</h3>
      <div class="table-wrap">
        <table data-table-id="traffic-routes" data-ws-scope="traffic_routes_table">
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
        <table data-table-id="workers-status" data-ws-scope="workers_status_table">
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
        <table data-table-id="admins-main" data-ws-scope="admins_table">
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
  if (state.currentPage === 'dashboard') renderDashboardPage();
  else if (state.currentPage === 'routes') renderRoutesPage();
  else if (state.currentPage === 'scripts') renderScriptsPage();
  else if (state.currentPage === 'keywords') renderKeywordsPage();
  else if (state.currentPage === 'sync') renderSyncPage();
  else if (state.currentPage === 'monitor') renderMonitorPage();
  else if (state.currentPage === 'logs') renderLogsPage();
  else if (state.currentPage === 'traffic') renderTrafficPage();
  else if (state.currentPage === 'workers') renderWorkersPage();
  else if (state.currentPage === 'storage') renderStoragePage();
  else if (state.currentPage === 'system') renderSystemPage();
  else if (state.currentPage === 'admins') renderAdminsPage();
  enhanceTablesForPage(state.currentPage);
  refreshRealtimeSubscriptions();
}

async function reloadPageData(page = state.currentPage) {
  const focusedFieldSnapshot = captureFocusedFieldSnapshot();
  const scrollSnapshot = captureScrollSnapshot(page);
  try {
    if (page === 'dashboard') await loadDashboard();
    if (page === 'routes') await loadRoutes();
    if (page === 'scripts') await loadScriptsMeta();
    if (page === 'keywords') await loadKeywords();
    if (page === 'sync') await loadSync();
    if (page === 'monitor') await loadMonitor();
    if (page === 'logs') await loadLogs();
    if (page === 'traffic') await loadTraffic();
    if (page === 'workers') await loadWorkers();
    if (page === 'storage') await loadStorage();
    if (page === 'system') await loadSystem();
    if (page === 'admins') await loadAdmins();
    renderCurrentPage();
    restoreFocusedFieldSnapshot(focusedFieldSnapshot);
    restoreScrollSnapshot(scrollSnapshot, page);
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
  if (isFieldEditingActive()) return false;
  if (Date.now() - Number(state.ui.lastInteractionAt || 0) < 2000) return false;
  if (page === 'scripts' || page === 'keywords' || page === 'admins') return false;
  return true;
}

function ensureRealtimeChannel(scope) {
  const key = String(scope || '').trim();
  if (!key) return null;
  const existing = state.realtime.channels[key];
  if (existing) return existing;
  const created = {
    ws: null,
    wsConnected: false,
    wsConnecting: false,
    wsConfigured: false,
    wsConnectStartedAt: 0,
    reconnectAttempt: 0,
    wsReconnectTimer: null,
    wsLastCloseAt: 0,
    wsLastCloseText: '',
    wsFailureStreak: 0,
    wsSuspendedUntil: 0,
    reloadDebounceTimer: null,
  };
  state.realtime.channels[key] = created;
  return created;
}

function forEachRealtimeChannel(cb) {
  Object.entries(state.realtime.channels || {}).forEach(([scope, channel]) => {
    cb(String(scope), channel);
  });
}

function clearRealtimeReconnectTimer(scope) {
  const ch = ensureRealtimeChannel(scope);
  if (!ch) return;
  if (ch.wsReconnectTimer) {
    clearTimeout(ch.wsReconnectTimer);
    ch.wsReconnectTimer = null;
  }
}

function closeScopeSocket(scope, reason = 'client_close') {
  const key = String(scope || '').trim();
  const ch = ensureRealtimeChannel(key);
  if (!ch) return;
  clearRealtimeReconnectTimer(key);
  if (ch.reloadDebounceTimer) {
    clearTimeout(ch.reloadDebounceTimer);
    ch.reloadDebounceTimer = null;
  }
  const ws = ch.ws;
  ch.ws = null;
  ch.wsConnected = false;
  ch.wsConnecting = false;
  ch.wsConfigured = false;
  ch.reconnectAttempt = 0;
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    try {
      ws.close(1000, reason);
    } catch (_) {
      // no-op
    }
  }
}

function closeRealtimeSocket() {
  forEachRealtimeChannel((scope) => {
    closeScopeSocket(scope, 'client_close');
  });
}

function scheduleRealtimePageReload() {
  if (!canAutoRefresh(state.currentPage)) return;
  if (state.realtime.pageReloadDebounceTimer) return;
  state.realtime.pageReloadDebounceTimer = setTimeout(async () => {
    state.realtime.pageReloadDebounceTimer = null;
    if (!canAutoRefresh(state.currentPage)) return;
    state.autoRefreshRunning = true;
    refreshAutoRefreshUi();
    try {
      await reloadPageData(state.currentPage);
    } finally {
      state.autoRefreshRunning = false;
      refreshAutoRefreshUi();
    }
  }, 220);
}

function scheduleScopePageReload(scope) {
  const key = String(scope || '').trim();
  const scopes = collectScopesFromPage(state.currentPage);
  if (!scopes.includes(key)) return;
  const ch = ensureRealtimeChannel(key);
  if (!ch || ch.reloadDebounceTimer) return;
  ch.reloadDebounceTimer = setTimeout(async () => {
    ch.reloadDebounceTimer = null;
    await scheduleRealtimePageReload();
  }, 180);
}

function scheduleRealtimeReconnect(scope) {
  const key = String(scope || '').trim();
  if (!key || !state.user || !state.autoRefreshEnabled) return;
  const ch = ensureRealtimeChannel(key);
  if (!ch || ch.wsReconnectTimer) return;
  const attempt = Number(ch.reconnectAttempt || 0) + 1;
  ch.reconnectAttempt = attempt;
  const now = Date.now();
  const suspendedUntil = Number(ch.wsSuspendedUntil || 0);
  const suspendedDelay = suspendedUntil > now ? (suspendedUntil - now) : 0;
  const baseDelay = 1500;
  const maxDelay = 30000;
  const retryDelay = Math.min(maxDelay, Math.round(baseDelay * (2 ** Math.min(6, attempt - 1))));
  const delay = Math.max(suspendedDelay, retryDelay);
  ch.wsReconnectTimer = setTimeout(() => {
    ch.wsReconnectTimer = null;
    connectScopeSocket(key);
  }, delay);
}

function handleRealtimeMessage(msg, scope) {
  if (!msg || typeof msg !== 'object') return;
  const type = String(msg.type || '').trim().toLowerCase();
  if (type === 'hello') {
    const latestSeq = Number(msg.latest_seq || 0);
    if (Number.isFinite(latestSeq) && latestSeq > 0) {
      state.monitorEventSeq = Math.max(Number(state.monitorEventSeq || 0), latestSeq);
    }
    scheduleScopePageReload(scope);
    return;
  }
  if (type === 'monitor_events') {
    const latestSeq = Number(msg.latest_seq || 0);
    if (Number.isFinite(latestSeq) && latestSeq > 0) {
      state.monitorEventSeq = Math.max(Number(state.monitorEventSeq || 0), latestSeq);
    }
    applyMonitorEvents(Array.isArray(msg.events) ? msg.events : []);
    scheduleScopePageReload(scope);
    return;
  }
  if (type === 'refresh_hint') {
    const scopes = Array.isArray(msg.scopes) ? msg.scopes.map((x) => String(x || '').trim()) : [];
    if (!scopes.length || scopes.includes(scope)) {
      scheduleScopePageReload(scope);
    }
  }
}

async function getRealtimeConfig(forceRefresh = false) {
  const now = Date.now();
  const maxAgeMs = 4 * 60 * 1000;
  if (!forceRefresh && state.realtime.config && (now - Number(state.realtime.configFetchedAt || 0) < maxAgeMs)) {
    return state.realtime.config;
  }
  const config = await api('/api/realtime/config');
  state.realtime.config = config;
  state.realtime.configFetchedAt = now;
  return config;
}

function buildScopedWsCandidates(config, scope) {
  const base = buildRealtimeWsCandidates(config);
  const out = [];
  for (const item of base) {
    try {
      const parsed = new URL(item);
      parsed.searchParams.set('scope', String(scope || '').trim());
      out.push(parsed.toString());
    } catch (_) {
      // no-op
    }
  }
  return out;
}

async function connectScopeSocket(scope, forceConfigRefresh = false) {
  const key = String(scope || '').trim();
  if (!key || !state.user || !state.autoRefreshEnabled) return;
  const ch = ensureRealtimeChannel(key);
  if (!ch || ch.wsConnected || ch.wsConnecting) return;
  const now = Date.now();
  if (!forceConfigRefresh && Number(ch.wsSuspendedUntil || 0) > now) {
    scheduleRealtimeReconnect(key);
    refreshAutoRefreshUi();
    return;
  }
  ch.wsConnecting = true;
  refreshAutoRefreshUi();
  let config;
  try {
    config = await getRealtimeConfig(forceConfigRefresh);
  } catch (_) {
    ch.wsConnecting = false;
    ch.wsConfigured = false;
    refreshAutoRefreshUi();
    scheduleRealtimeReconnect(key);
    return;
  }

  const enabled = !!config?.enabled;
  const candidates = buildScopedWsCandidates(config, key);
  if (!enabled || !candidates.length) {
    ch.wsConnecting = false;
    ch.wsConfigured = false;
    refreshAutoRefreshUi();
    if (location.protocol === 'https:') {
      const now = Date.now();
      const text = `No secure websocket endpoint available for scope=${key}`;
      if (now - Number(ch.wsLastCloseAt || 0) > 20000 || ch.wsLastCloseText !== text) {
        ch.wsLastCloseAt = now;
        ch.wsLastCloseText = text;
        showFlash(text, true);
      }
    }
    scheduleRealtimeReconnect(key);
    return;
  }

  const idx = Math.max(0, Number(ch.reconnectAttempt || 0)) % candidates.length;
  const wsUrl = String(candidates[idx] || candidates[0]);
  ch.wsConfigured = true;
  let ws;
  try {
    ws = new WebSocket(wsUrl);
  } catch (_) {
    ch.wsConnecting = false;
    ch.wsConnected = false;
    refreshAutoRefreshUi();
    scheduleRealtimeReconnect(key);
    return;
  }
  ch.ws = ws;
  ch.wsConnectStartedAt = Date.now();

  ws.onopen = () => {
    ch.wsConnected = true;
    ch.wsConnecting = false;
    ch.wsConnectStartedAt = 0;
    ch.reconnectAttempt = 0;
    ch.wsFailureStreak = 0;
    ch.wsSuspendedUntil = 0;
    refreshAutoRefreshUi();
  };
  ws.onmessage = (evt) => {
    try {
      const msg = JSON.parse(String(evt.data || '{}'));
      handleRealtimeMessage(msg, key);
    } catch (_) {
      // no-op
    }
  };
  ws.onerror = () => {
    // close handler handles reconnect
  };
  ws.onclose = (evt) => {
    const wasConnected = !!ch.wsConnected;
    const code = Number(evt?.code || 0);
    const reason = String(evt?.reason || '').trim();
    const openedAt = Number(ch.wsConnectStartedAt || 0);
    const lifetimeMs = openedAt > 0 ? Math.max(0, Date.now() - openedAt) : 0;
    ch.ws = null;
    ch.wsConnected = false;
    ch.wsConnecting = false;
    ch.wsConnectStartedAt = 0;
    refreshAutoRefreshUi();
    if (!state.user || !state.autoRefreshEnabled) return;
    const now = Date.now();
    const closeText = `${key}: code=${code}${reason ? ` reason=${reason}` : ''}`;
    const shouldNotify = (wasConnected || code === 1008) && (now - Number(ch.wsLastCloseAt || 0) > 7000 || ch.wsLastCloseText !== closeText);
    if (shouldNotify) {
      ch.wsLastCloseAt = now;
      ch.wsLastCloseText = closeText;
      showFlash(`Realtime disconnected (${closeText})`, true);
    }
    if (code === 1008) {
      state.realtime.config = null;
      state.realtime.configFetchedAt = 0;
    }
    if (!wasConnected && (code === 1006 || code === 0) && lifetimeMs < 4500) {
      ch.wsFailureStreak = Number(ch.wsFailureStreak || 0) + 1;
    } else if (wasConnected) {
      ch.wsFailureStreak = 0;
    }
    if (Number(ch.wsFailureStreak || 0) >= WS_FAILURE_SUSPEND_AFTER) {
      ch.wsFailureStreak = 0;
      ch.wsSuspendedUntil = now + WS_SUSPEND_MS;
      ch.wsConfigured = false;
      refreshAutoRefreshUi();
    }
    scheduleRealtimeReconnect(key);
  };
}

function refreshRealtimeSubscriptions() {
  const disabledPages = new Set(['scripts', 'keywords', 'admins']);
  if (!state.user || !state.autoRefreshEnabled || disabledPages.has(String(state.currentPage || ''))) {
    closeRealtimeSocket();
    return;
  }
  const desiredScopes = collectScopesFromPage(state.currentPage);
  const desiredSet = new Set(desiredScopes);
  forEachRealtimeChannel((scope) => {
    if (!desiredSet.has(scope)) {
      closeScopeSocket(scope, 'scope_changed');
      delete state.realtime.channels[scope];
    }
  });
  desiredScopes.forEach((scope) => {
    ensureRealtimeChannel(scope);
    connectScopeSocket(scope);
  });
  refreshAutoRefreshUi();
}

function buildRealtimeWsCandidates(config) {
  const out = [];
  const securePage = location.protocol === 'https:';
  const add = (value) => {
    const text = String(value || '').trim();
    if (!text) return;
    if (!/^wss?:\/\//i.test(text)) return;
    if (securePage && text.startsWith('ws://')) return;
    if (!out.includes(text)) out.push(text);
  };
  add(config?.ws_url);
  const apiCandidates = Array.isArray(config?.ws_candidates) ? config.ws_candidates : [];
  for (const c of apiCandidates) add(c);

  const port = Number(config?.ws_port || 0);
  const path = String(config?.ws_path || '/ws').trim() || '/ws';
  const ticket = String(config?.ticket || '').trim();
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const querySep = path.includes('?') ? '&' : '?';
  const suffix = ticket ? `${querySep}ticket=${encodeURIComponent(ticket)}` : '';

  if (Number.isFinite(port) && port > 0) {
    try {
      const withPort = new URL(path + suffix, `${protocol}//${location.host}`);
      withPort.port = String(Math.trunc(port));
      add(String(withPort.toString()));
    } catch (_) {
      // no-op
    }
  }
  return out;
}

function startAutoRefresh() {
  if (state.autoRefreshTimer) clearInterval(state.autoRefreshTimer);
  state.autoRefreshTimer = null;
  closeRealtimeSocket();
  forEachRealtimeChannel((scope) => {
    clearRealtimeReconnectTimer(scope);
  });
  refreshAutoRefreshUi();
  if (!state.autoRefreshEnabled) return;
  refreshRealtimeSubscriptions();
  state.autoRefreshTimer = setInterval(async () => {
    if (!canAutoRefresh(state.currentPage)) return;
    const channels = Object.values(state.realtime.channels || {});
    const anyConnected = channels.some((ch) => !!ch?.wsConnected);
    if (anyConnected) return;
    state.autoRefreshRunning = true;
    refreshAutoRefreshUi();
    try {
      await reloadPageData(state.currentPage);
    } finally {
      state.autoRefreshRunning = false;
      refreshAutoRefreshUi();
    }
  }, Math.max(1500, Number(state.autoRefreshMs || 5000)));
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
  markUserInteraction();

  els.modalCloseBtn?.addEventListener('click', closeModal);
  els.modalOverlay?.addEventListener('click', (e) => {
    if (e.target === els.modalOverlay) closeModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && isModalOpen()) closeModal();
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    if (!state.user || !state.autoRefreshEnabled) return;
    refreshRealtimeSubscriptions();
  });
  window.addEventListener('wheel', markUserInteraction, { passive: true });
  window.addEventListener('touchstart', markUserInteraction, { passive: true });
  document.addEventListener('pointerdown', markUserInteraction, true);
  document.addEventListener('input', markUserInteraction, true);
  document.addEventListener('change', markUserInteraction, true);
  document.addEventListener('keydown', markUserInteraction, true);
  window.addEventListener('beforeunload', () => {
    closeRealtimeSocket();
    forEachRealtimeChannel((scope) => {
      clearRealtimeReconnectTimer(scope);
    });
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
    if (state.autoRefreshEnabled) {
      startAutoRefresh();
    } else {
      closeRealtimeSocket();
      forEachRealtimeChannel((scope) => {
        clearRealtimeReconnectTimer(scope);
      });
    }
    refreshAutoRefreshUi();
    showFlash(state.autoRefreshEnabled ? 'Realtime updates resumed' : 'Realtime updates paused');
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
  await Promise.all([loadDashboard(), loadRoutes(), loadScriptsMeta(), loadKeywords(), loadSync(), loadMonitor(), loadLogs(), loadTraffic(), loadWorkers(), loadStorage(), loadSystem(), loadAdmins()]);
  navigate(state.currentPage);
  startAutoRefresh();
  refreshAutoRefreshUi();
}

bootstrap().catch(err => {
  console.error(err);
  showFlash(`Panel bootstrap error: ${err.message}`, true);
});
