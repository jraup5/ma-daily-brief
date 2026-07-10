"""
Local Flask web app for John Raup's Daily M&A Brief.

Start with:
    python app.py

Then open:
    http://localhost:5001
"""

import html as _html
import json
import os
import re
import sys
import threading
import traceback

from flask import Flask, jsonify, request
from dotenv import load_dotenv

load_dotenv()

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_PATH  = os.path.join(BASE_DIR, "ma_digest.html")
INSIGHT_FILE    = os.path.join(BASE_DIR, "key_insight.json")

sys.path.insert(0, BASE_DIR)
import ma_news_digest as pipeline

app = Flask(__name__)

_pipeline_running = False
_last_error: str = ""


# ── manual insight helpers ────────────────────────────────────────────────────

def _read_manual_insight() -> str:
    """Return the saved manual insight, delegating to the pipeline helper."""
    return pipeline._read_saved_insight()


def _write_manual_insight(text: str) -> None:
    with open(INSIGHT_FILE, "w", encoding="utf-8") as f:
        json.dump({"manual": text.strip()}, f, ensure_ascii=False)


# ── HTML snippets injected at serve time ─────────────────────────────────────

_REFRESH_UI = """
<style>
  .refresh-form { display:flex; align-items:center; gap:.6rem; }
  .days-select {
    background:var(--bg-2); border:1px solid var(--border);
    color:var(--text-1); border-radius:4px;
    padding:.3rem .5rem; font-size:.78rem; font-family:inherit; cursor:pointer;
  }
  .refresh-btn {
    background:var(--accent); color:#fff; border:none;
    border-radius:4px; padding:.35rem .9rem;
    font-size:.78rem; font-weight:600; cursor:pointer; transition:opacity .15s;
  }
  .refresh-btn:disabled { opacity:.5; cursor:not-allowed; }
  .refresh-btn:hover:not(:disabled) { opacity:.85; }
  #refresh-status { font-size:.72rem; color:var(--text-3); min-width:8rem; }
</style>
<form class="refresh-form" id="refresh-form" onsubmit="return doRefresh(event)">
  <select class="days-select" id="days-select" name="days">
    <option value="1" selected>1 day</option>
    <option value="2">2 days</option>
    <option value="3">3 days</option>
    <option value="5">5 days</option>
    <option value="7">7 days</option>
  </select>
  <button class="refresh-btn" id="refresh-btn" type="submit">Refresh</button>
  <span id="refresh-status"></span>
</form>
<script>
function doRefresh(e) {
  e.preventDefault();
  const btn = document.getElementById('refresh-btn');
  const status = document.getElementById('refresh-status');
  const days = document.getElementById('days-select').value;
  btn.disabled = true;
  btn.textContent = 'Running…';
  status.textContent = 'Fetching & filtering…';
  fetch('/api/refresh?days=' + days, {method:'POST'})
    .then(r => r.json())
    .then(d => {
      if (d.ok) { status.textContent = 'Done — reloading'; window.location.reload(); }
      else { btn.disabled=false; btn.textContent='Refresh'; status.textContent='⚠ '+(d.error||'Unknown error'); status.style.color='#f87171'; }
    })
    .catch(() => { btn.disabled=false; btn.textContent='Refresh'; status.textContent='⚠ Network error'; status.style.color='#f87171'; });
  return false;
}
</script>
"""

_INSIGHT_EDIT_UI = """
<style>
  .ki-controls { position:absolute; top:.7rem; right:1.5rem; display:flex; align-items:center; gap:.45rem; }
  .ki-edit-btn, .ki-reset-btn {
    background:none; border:1px solid transparent; border-radius:3px;
    color:var(--text-3); font-size:.68rem; cursor:pointer; padding:.1rem .35rem;
    transition:color .12s, border-color .12s; font-family:inherit; line-height:1.4;
  }
  .ki-edit-btn:hover { color:var(--accent); border-color:var(--accent); }
  .ki-reset-btn { font-size:.63rem; }
  .ki-reset-btn:hover { color:#f87171; border-color:#f87171; }
  .ki-textarea {
    width:100%; max-width:88ch; font-size:.93rem; line-height:1.5;
    font-family:inherit; color:var(--text-1); background:var(--bg-2);
    border:1px solid var(--accent); border-radius:4px; padding:.45rem .65rem;
    resize:vertical; min-height:3.8rem; margin-top:.2rem;
  }
  .ki-save-btn, .ki-cancel-btn {
    border:none; border-radius:3px; font-size:.72rem; font-weight:600;
    cursor:pointer; padding:.25rem .6rem; font-family:inherit;
  }
  .ki-save-btn { background:var(--accent); color:#fff; }
  .ki-save-btn:hover { opacity:.85; }
  .ki-cancel-btn { background:var(--bg-3); color:var(--text-2); border:1px solid var(--border); }
  .ki-cancel-btn:hover { border-color:var(--text-3); }
  .ki-saving { opacity:.5; pointer-events:none; }
</style>
<script>
(function() {
  const banner = document.querySelector('.key-insight');
  if (!banner) return;

  const txt = banner.querySelector('.ki-txt');
  const hasManual = banner.dataset.manual === '1';

  // Build controls container
  const controls = document.createElement('div');
  controls.className = 'ki-controls';

  const editBtn = document.createElement('button');
  editBtn.className = 'ki-edit-btn';
  editBtn.textContent = '✎ Edit';
  editBtn.title = 'Edit insight';

  const resetBtn = document.createElement('button');
  resetBtn.className = 'ki-reset-btn';
  resetBtn.textContent = '↺ AI';
  resetBtn.title = 'Reset to AI-generated insight';
  resetBtn.style.display = hasManual ? '' : 'none';

  controls.append(editBtn, resetBtn);
  banner.append(controls);

  // ── edit mode ──
  editBtn.addEventListener('click', () => {
    const original = txt.textContent;
    txt.style.display = 'none';
    controls.style.display = 'none';

    const ta = document.createElement('textarea');
    ta.className = 'ki-textarea';
    ta.value = original;

    const saveBtn = document.createElement('button');
    saveBtn.className = 'ki-save-btn';
    saveBtn.textContent = 'Save';

    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'ki-cancel-btn';
    cancelBtn.textContent = 'Cancel';

    const editControls = document.createElement('div');
    editControls.className = 'ki-controls';
    editControls.append(saveBtn, cancelBtn);

    banner.append(ta, editControls);
    ta.focus();
    ta.select();

    cancelBtn.addEventListener('click', () => {
      ta.remove();
      editControls.remove();
      txt.style.display = '';
      controls.style.display = '';
    });

    saveBtn.addEventListener('click', () => {
      const newText = ta.value.trim();
      if (!newText) return;
      saveBtn.classList.add('ki-saving');
      saveBtn.textContent = 'Saving…';
      fetch('/api/insight', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: newText}),
      })
      .then(r => r.json())
      .then(d => { if (d.ok) window.location.reload(); })
      .catch(() => { saveBtn.textContent = 'Error'; saveBtn.classList.remove('ki-saving'); });
    });
  });

  // ── reset to AI ──
  resetBtn.addEventListener('click', () => {
    if (!confirm('Clear your manual insight and revert to the AI-generated one?')) return;
    fetch('/api/insight', {method: 'DELETE'})
      .then(r => r.json())
      .then(d => { if (d.ok) window.location.reload(); });
  });
})();
</script>
"""


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if not os.path.isfile(DASHBOARD_PATH):
        return (
            "<p>No dashboard found. Click Refresh to generate one.</p>"
            "<p><a href='/'>Reload</a></p>",
            404,
        )

    with open(DASHBOARD_PATH, encoding="utf-8") as f:
        page = f.read()

    manual = _read_manual_insight()

    if manual:
        escaped = _html.escape(manual)
        if 'class="key-insight"' in page:
            # Replace baked-in AI text with manual override.
            page = re.sub(
                r'(<span class="ki-txt">)[^<]*(</span>)',
                r'\g<1>' + escaped + r'\g<2>',
                page,
                count=1,
            )
            # Tag the banner so JS knows a manual is active.
            page = page.replace(
                '<div class="key-insight">',
                '<div class="key-insight" data-manual="1">',
                1,
            )
        else:
            # No banner in HTML (e.g. --no-ai run) — inject one.
            inject = (
                '<div class="key-insight" data-manual="1">'
                '<span class="ki-lbl">Key Insight of the Day</span>'
                f'<span class="ki-txt">{escaped}</span>'
                '</div>'
            )
            page = page.replace('</nav>\n\n<div class="stats">', f'</nav>\n\n{inject}\n\n<div class="stats">', 1)

    page = page.replace("</nav>", _REFRESH_UI + "</nav>", 1)
    page = page.replace("</body>", _INSIGHT_EDIT_UI + "</body>", 1)
    return page


@app.route("/api/insight", methods=["POST"])
def api_insight_save():
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Empty text"}), 400
    _write_manual_insight(text)
    return jsonify({"ok": True})


@app.route("/api/insight", methods=["DELETE"])
def api_insight_reset():
    _write_manual_insight("")
    return jsonify({"ok": True})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    global _pipeline_running, _last_error

    if _pipeline_running:
        return jsonify({"ok": False, "error": "Pipeline already running"}), 409

    try:
        days = max(1, min(int(request.args.get("days", 1)), 30))
    except (TypeError, ValueError):
        days = 1

    _pipeline_running = True
    _last_error = ""
    try:
        orig_dir = os.getcwd()
        os.chdir(BASE_DIR)
        try:
            pipeline.run_pipeline(days=days)
        finally:
            os.chdir(orig_dir)
        return jsonify({"ok": True, "days": days})
    except Exception:
        _last_error = traceback.format_exc()
        print(_last_error, file=sys.stderr)
        return jsonify({"ok": False, "error": "Pipeline failed — check server logs"}), 500
    finally:
        _pipeline_running = False


@app.route("/api/status")
def api_status():
    return jsonify({"running": _pipeline_running, "last_error": _last_error})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print(f"\n  M&A Brief running at  http://localhost:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=False)
