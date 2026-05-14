from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEBUI = ROOT / "src" / "kiwi" / "webui"


def _read(name: str) -> str:
    return (WEBUI / name).read_text(encoding="utf-8")


def test_index_uses_ltr_and_modal_shell() -> None:
    html = _read("index.html")
    assert 'lang="en"' in html
    assert 'dir="ltr"' in html
    assert 'id="modalOverlay"' in html
    assert 'id="modalBody"' in html
    assert 'id="modalCloseBtn"' in html
    assert 'id="page-traffic"' in html
    assert 'id="page-workers"' in html
    assert 'id="toastRoot"' in html
    assert 'id="toggleAutoRefreshBtn"' in html


def test_routes_page_has_search_and_modal_editor() -> None:
    js = _read("app.js")
    assert "routeSearchInput" in js
    assert "routeSearchClearBtn" not in js
    assert "state.routeFilter" in js
    assert 'data-sort-key="remaining"' in js
    assert 'data-sort-key="progress"' in js
    assert "remaining_unsynced" in js
    assert "progress_pct" in js
    assert "routeSort" in js
    assert "data-sort-key" in js
    assert "toggleRouteSort(" in js
    assert "openRouteEditor(" in js
    assert "openModal(" in js
    assert "data-route-name" in js


def test_other_sections_use_modal_for_edit_or_preview() -> None:
    js = _read("app.js")
    assert "openScriptEditorModal(" in js
    assert "Edit Keyword Links" in js
    assert "File Preview:" in js
    assert "Add Admin" in js
    assert "renderTrafficPage()" in js
    assert "/api/traffic/stats" in js
    assert "renderWorkersPage()" in js
    assert "/api/workers/status" in js
    assert "Service Run Traffic History" in js
    assert "All Runs Download" in js
    assert "Current Run Download" in js
    assert "logsLevelFilter" in js
    assert "logsLoggerFilter" in js
    assert "logsMessageFilter" in js
    assert "autoRefreshEnabled" in js
    assert "toggleAutoRefreshBtn" in js
    assert "refreshAutoRefreshUi()" in js


def test_styles_define_compact_rows_and_modal_layout() -> None:
    css = _read("styles.css")
    assert ".actions-cell" in css
    assert ".compact-input" in css
    assert ".icon-only" in css
    assert ".modal-overlay" in css
    assert ".modal-shell" in css
    assert ".modal-grid" in css
    assert ".toast-root" in css
    assert ".toast.error" in css
