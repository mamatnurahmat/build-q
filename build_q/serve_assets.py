"""Static assets untuk `bq --serve` — inline HTML + CSS + JS vanilla.

Dibuat jadi 1 string biar zero file deps / build step / template engine.
"""

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jev Web Chat — bq --serve</title>
<style>
  :root {
    --bg: #0d1117; --fg: #e6edf3; --muted: #7d8590;
    --border: #30363d; --accent: #58a6ff; --card: #161b22;
    --green: #3fb950; --yellow: #d29922; --red: #f85149;
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--fg); font-family: system-ui, -apple-system, sans-serif; }
  body { display: flex; flex-direction: column; height: 100vh; }

  header {
    display: flex; align-items: center; gap: 12px;
    padding: 10px 16px; border-bottom: 1px solid var(--border);
    background: var(--card);
  }
  header h1 { font-size: 15px; margin: 0; font-weight: 600; color: var(--accent); }
  header .meta { font-size: 12px; color: var(--muted); font-family: var(--mono); }
  header select {
    background: var(--bg); color: var(--fg); border: 1px solid var(--border);
    padding: 4px 8px; border-radius: 6px; font-size: 12px;
  }
  header .spacer { flex: 1; }
  header button {
    background: var(--bg); color: var(--fg); border: 1px solid var(--border);
    padding: 4px 8px; border-radius: 6px; font-size: 11px; cursor: pointer;
  }
  header button:hover { border-color: var(--accent); }

  #chat {
    flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 14px;
  }
  .msg { max-width: 90%; }
  .msg.user { align-self: flex-end; background: #1f6feb; color: white; padding: 8px 12px; border-radius: 12px 12px 2px 12px; }
  .msg.bot  { align-self: flex-start; background: var(--card); padding: 10px 14px; border-radius: 12px 12px 12px 2px; border: 1px solid var(--border); }

  .top3 { display: flex; flex-direction: column; gap: 6px; margin-top: 6px; }
  .top3 .row { display: flex; align-items: center; gap: 10px; padding: 6px 10px; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; cursor: default; }
  .top3 .row.selected { border-color: var(--accent); }
  .top3 .rank { color: var(--muted); font-family: var(--mono); font-size: 12px; min-width: 20px; }
  .top3 .name { font-family: var(--mono); font-size: 13px; min-width: 180px; }
  .top3 .bar  { flex: 1; height: 8px; background: var(--border); border-radius: 4px; overflow: hidden; }
  .top3 .bar > span { display: block; height: 100%; background: var(--accent); }
  .top3 .pct  { color: var(--muted); font-family: var(--mono); font-size: 12px; min-width: 46px; text-align: right; }

  .cmdpreview {
    margin-top: 10px; padding: 10px 12px; background: var(--bg);
    border: 1px solid var(--border); border-radius: 6px;
    font-family: var(--mono); font-size: 13px; white-space: pre-wrap; word-break: break-all;
  }
  .cmdpreview.risky { border-color: var(--red); }

  .params { margin-top: 10px; display: flex; flex-direction: column; gap: 6px; }
  .params .field { display: flex; align-items: center; gap: 8px; font-size: 13px; }
  .params .field label { min-width: 110px; font-family: var(--mono); color: var(--muted); font-size: 12px; }
  .params .field input, .params .field select {
    flex: 1; background: var(--bg); color: var(--fg); border: 1px solid var(--border);
    border-radius: 4px; padding: 4px 8px; font-family: var(--mono); font-size: 12px;
  }

  .actions { margin-top: 10px; display: flex; gap: 8px; }
  button {
    background: var(--accent); color: #0d1117; border: none; padding: 7px 14px;
    border-radius: 6px; font-weight: 600; cursor: pointer; font-size: 13px;
  }
  button.secondary { background: var(--card); color: var(--fg); border: 1px solid var(--border); }
  button.danger { background: var(--red); color: white; }
  button:disabled { opacity: 0.4; cursor: not-allowed; }

  .tag { display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 11px; font-family: var(--mono); margin-left: 6px; }
  .tag.risky { background: var(--red); color: white; }
  .tag.safe { background: var(--green); color: white; }

  .output {
    margin-top: 10px; padding: 10px 12px; background: #010409; border: 1px solid var(--border);
    border-radius: 6px; font-family: var(--mono); font-size: 12px; white-space: pre-wrap;
    max-height: 400px; overflow-y: auto;
  }
  .output .exit0 { color: var(--green); }
  .output .exit-nz { color: var(--red); }

  footer {
    border-top: 1px solid var(--border); padding: 10px 16px; background: var(--card);
  }
  footer form { display: flex; gap: 8px; }
  footer textarea {
    flex: 1; background: var(--bg); color: var(--fg); border: 1px solid var(--border);
    border-radius: 6px; padding: 8px 12px; font-family: inherit; font-size: 14px; resize: none; min-height: 40px;
  }
  footer button { align-self: flex-end; }

  .usage { color: var(--muted); font-size: 11px; margin-top: 6px; font-family: var(--mono); }
</style>
</head>
<body>
<header>
  <h1>Jev Web Chat</h1>
  <span class="meta" id="meta">connecting…</span>
  <span class="spacer"></span>
  <button id="btn-export" title="Export chat ke Markdown">📄 Export</button>
  <button id="btn-clear" title="Hapus riwayat chat">🗑 Clear</button>
  <label class="meta" for="provider-select">provider</label>
  <select id="provider-select"></select>
</header>

<main id="chat"></main>

<footer>
  <form id="chat-form">
    <textarea id="input" placeholder="Ketik request natural (ID/EN) — contoh: bootstrap pay-be-topup develop ke gitops cce production-qoin" autofocus></textarea>
    <button type="submit" id="send-btn">Kirim</button>
  </form>
</footer>

<script>
const $ = (s) => document.querySelector(s);
const chat = $('#chat');
const providerSelect = $('#provider-select');

// ─── Token auth (M3) ─────────────────────────────────────────────────────
// Ambil token dari URL `?token=XXX` → simpan ke localStorage.
(function initToken() {
  const url = new URL(window.location.href);
  const t = url.searchParams.get('token');
  if (t) {
    localStorage.setItem('bq_serve_token', t);
    url.searchParams.delete('token');
    window.history.replaceState({}, '', url.toString());
  }
})();
function getToken() { return localStorage.getItem('bq_serve_token') || ''; }

async function api(path, opts) {
  const headers = {'Content-Type': 'application/json'};
  const tok = getToken();
  if (tok) headers['Authorization'] = 'Bearer ' + tok;
  const r = await fetch(path, Object.assign({ headers }, opts || {}));
  const data = await r.json().catch(() => ({ error: 'invalid JSON' }));
  if (r.status === 401) {
    const inp = prompt('Server butuh Bearer token. Masukkan token:');
    if (inp) { localStorage.setItem('bq_serve_token', inp); return api(path, opts); }
    throw new Error('unauthorized');
  }
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

// Chat history untuk export (in-memory + session only)
const chatHistory = [];
function pushHistory(role, obj) { chatHistory.push({ role, ...obj, ts: new Date().toISOString() }); }

function el(tag, cls, content) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (content !== undefined) n.textContent = content;
  return n;
}

function appendMsg(role, builder) {
  const m = el('div', 'msg ' + role);
  builder(m);
  chat.appendChild(m);
  chat.scrollTop = chat.scrollHeight;
  return m;
}

function fmtPct(x) { return (x * 100).toFixed(1) + '%'; }

async function loadHealth() {
  try {
    const h = await api('/api/health');
    $('#meta').textContent = `v${h.version} · ${h.provider.name || '?'} · ${h.provider.model || '?'}`;
  } catch (e) {
    $('#meta').textContent = 'health error';
  }
}

async function loadCatalog() {
  try {
    const c = await api('/api/catalog');
    providerSelect.innerHTML = '';
    Object.values(c.providers).forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.name;
      opt.textContent = p.name + (p.is_default ? ' ★' : '');
      if (p.is_default) opt.selected = true;
      providerSelect.appendChild(opt);
    });
  } catch (e) { /* ignore */ }
}

providerSelect.addEventListener('change', async () => {
  try {
    await api('/api/provider', { method: 'POST', body: JSON.stringify({ name: providerSelect.value }) });
    loadHealth();
  } catch (e) { alert('switch provider gagal: ' + e.message); }
});

function renderBotResponse(parent, data) {
  // Tool header
  const h = el('div');
  if (data.tool === 'none' || !data.tool) {
    h.innerHTML = '<em>Tidak ada tool yang cocok. Coba lebih spesifik.</em>';
    parent.appendChild(h);
    return;
  }
  h.innerHTML = `→ <strong>${data.tool}</strong> <span class="meta">confidence ${fmtPct(data.confidence || 0)}</span>` +
                (data.risky ? '<span class="tag risky">RISKY</span>' : '<span class="tag safe">safe</span>');
  parent.appendChild(h);

  // Top-3
  if (data.top3 && data.top3.length) {
    const top = el('div', 'top3');
    data.top3.forEach((t, i) => {
      const row = el('div', 'row' + (t.name === data.tool ? ' selected' : ''));
      row.appendChild(el('span', 'rank', '#' + (i + 1)));
      row.appendChild(el('span', 'name', t.name));
      const bar = el('div', 'bar');
      const inner = el('span'); inner.style.width = (t.prob * 100).toFixed(1) + '%';
      bar.appendChild(inner); row.appendChild(bar);
      row.appendChild(el('span', 'pct', fmtPct(t.prob)));
      top.appendChild(row);
    });
    parent.appendChild(top);
  }

  // Param form
  const schema = data.params_schema || {};
  const params = Object.assign({}, data.params || {});
  const paramDiv = el('div', 'params');
  Object.keys(schema).forEach(pname => {
    const spec = schema[pname] || {};
    const field = el('div', 'field');
    field.appendChild(el('label', '', pname));
    let input;
    if (spec.type === 'choice' && spec.options) {
      input = document.createElement('select');
      spec.options.forEach(opt => {
        const o = document.createElement('option');
        o.value = o.textContent = opt;
        if (params[pname] === opt) o.selected = true;
        input.appendChild(o);
      });
    } else {
      input = document.createElement('input');
      input.type = 'text';
      input.value = (params[pname] && String(params[pname]).startsWith('<')) ? '' : (params[pname] || '');
      input.placeholder = spec.hint || '';
    }
    input.addEventListener('input', refreshPreview);
    input.addEventListener('change', refreshPreview);
    input.dataset.pname = pname;
    field.appendChild(input);
    paramDiv.appendChild(field);
  });
  parent.appendChild(paramDiv);

  // Preview + actions
  const preview = el('div', 'cmdpreview' + (data.risky ? ' risky' : ''));
  preview.textContent = data.preview || '(preview tidak tersedia)';
  parent.appendChild(preview);

  const actions = el('div', 'actions');
  const runBtn = el('button', data.risky ? 'danger' : '', data.risky ? 'Jalankan (RISKY)' : 'Jalankan');
  const copyBtn = el('button', 'secondary', 'Copy');
  actions.appendChild(runBtn); actions.appendChild(copyBtn);
  parent.appendChild(actions);

  // Usage
  const u = data.usage || {};
  if (u.input_tokens || u.output_tokens || u.cost) {
    const parts = [];
    if (u.input_tokens)  parts.push(`in ${u.input_tokens}`);
    if (u.output_tokens) parts.push(`out ${u.output_tokens}`);
    if (u.cost)          parts.push(`$${Number(u.cost).toFixed(6)}`);
    parent.appendChild(el('div', 'usage', 'tokens: ' + parts.join(' · ')));
  }

  function readParams() {
    const out = {};
    parent.querySelectorAll('[data-pname]').forEach(i => { out[i.dataset.pname] = i.value; });
    return out;
  }

  function refreshPreview() {
    const p = readParams();
    let tmpl = (data._template || data.preview || '');
    // ambil template dari schema call awal: data.preview sudah di-render,
    // tapi untuk live update kita re-render pakai simple format.
    // Simpler: minta server render — tapi hemat round-trip, do client-side format.
    if (data._template) {
      try {
        preview.textContent = data._template.replace(/\{(\w+)\}/g, (_, k) => p[k] || `<${k}>`);
      } catch (e) {}
    }
  }

  copyBtn.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(preview.textContent);
      copyBtn.textContent = '✓ copied';
      setTimeout(() => copyBtn.textContent = 'Copy', 1200);
    } catch (e) { alert('copy fail: ' + e.message); }
  });

  runBtn.addEventListener('click', async () => {
    const cmd = preview.textContent;
    if (!cmd || cmd.includes('<preview')) return alert('Preview belum valid');
    runBtn.disabled = true;
    runBtn.textContent = 'Running…';
    const out = el('div', 'output');
    const streamBody = el('div');
    out.appendChild(streamBody);
    parent.appendChild(out);
    try {
      // M2: pakai async + SSE stream
      const spawn = await api('/api/execute-stream', { method: 'POST',
        body: JSON.stringify({ cmd, risky: data.risky, confirm: true })});
      const header = el('div', '', `job ${spawn.job_id}  ·  streaming…`);
      out.insertBefore(header, streamBody);

      // Note: EventSource bawaan browser belum support custom headers,
      // jadi token auth lewat query-string `?token=` kalau diperlukan.
      let streamUrl = spawn.stream_url;
      const tok = getToken();
      if (tok) streamUrl += (streamUrl.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(tok);
      const es = new EventSource(streamUrl);
      const collected = [];
      es.onmessage = (ev) => {
        try {
          const d = JSON.parse(ev.data);
          if (d.line !== undefined) {
            collected.push(d.line);
            streamBody.appendChild(document.createTextNode(d.line + '\n'));
            out.scrollTop = out.scrollHeight;
          }
        } catch (_) { /* ignore malformed */ }
      };
      es.addEventListener('done', (ev) => {
        try {
          const d = JSON.parse(ev.data);
          const cls = d.exit === 0 ? 'exit0' : 'exit-nz';
          const line = el('div', cls, `✓ exit ${d.exit}  ·  ${d.elapsed_s}s`);
          out.appendChild(line);
          runBtn.textContent = `done (exit ${d.exit})`;
          pushHistory('bot', {
            tool: data.tool, exit: d.exit, elapsed_s: d.elapsed_s,
            preview: cmd, output: collected.join('\n'),
          });
          // Rerun button
          const rerun = el('button', 'secondary', '↻ Rerun');
          rerun.style.marginTop = '8px';
          rerun.addEventListener('click', () => runBtn.click());
          out.appendChild(rerun);
        } catch (_) {}
        es.close();
      });
      es.onerror = () => {
        runBtn.disabled = false; runBtn.textContent = 'Retry';
        es.close();
      };
    } catch (e) {
      alert('execute error: ' + e.message);
      runBtn.disabled = false; runBtn.textContent = 'Retry';
    }
  });
}

// ─── Toolbar: Export markdown, Clear chat (M4) ───────────────────────────
$('#btn-export').addEventListener('click', () => {
  if (!chatHistory.length) return alert('Chat kosong');
  const lines = [
    '# Jev Web Chat — Export',
    '',
    `**Exported**: ${new Date().toISOString()}`,
    `**Messages**: ${chatHistory.length}`,
    '',
    '---',
    '',
  ];
  chatHistory.forEach(m => {
    if (m.role === 'user') {
      lines.push(`### 👤 user _(${m.ts})_`);
      lines.push('');
      lines.push('> ' + (m.text || '').split('\n').join('\n> '));
      lines.push('');
    } else {
      lines.push(`### 🤖 jev _(${m.ts})_`);
      lines.push('');
      if (m.tool) {
        const risky = m.risky ? ' 🔴 RISKY' : ' 🟢 safe';
        lines.push(`**Tool**: \`${m.tool}\` · confidence ${(m.confidence*100).toFixed(1)}%${risky}`);
        lines.push('');
      }
      if (m.preview) {
        lines.push('```bash');
        lines.push(m.preview);
        lines.push('```');
        lines.push('');
      }
      if (m.exit !== undefined) {
        lines.push(`**Exit**: ${m.exit} · ${m.elapsed_s || '?'}s`);
        lines.push('');
      }
      if (m.output) {
        lines.push('<details><summary>Output</summary>');
        lines.push('');
        lines.push('```');
        lines.push(m.output);
        lines.push('```');
        lines.push('');
        lines.push('</details>');
        lines.push('');
      }
    }
  });
  const blob = new Blob([lines.join('\n')], { type: 'text/markdown' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `jev-chat-${new Date().toISOString().slice(0,19).replace(/[:T]/g, '-')}.md`;
  a.click();
});

$('#btn-clear').addEventListener('click', () => {
  if (!chatHistory.length) return;
  if (!confirm('Hapus semua riwayat chat?')) return;
  chatHistory.length = 0;
  chat.innerHTML = '';
});

$('#chat-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const input = $('#input');
  const msg = input.value.trim();
  if (!msg) return;
  pushHistory('user', { text: msg });
  appendMsg('user', (m) => { m.textContent = msg; });
  input.value = '';
  const bot = appendMsg('bot', (m) => { m.innerHTML = '<em>Jev memutuskan…</em>'; });
  try {
    const data = await api('/api/decide', { method: 'POST',
      body: JSON.stringify({ user_msg: msg, provider: providerSelect.value })});
    // Simpan template agar bisa re-render saat edit param
    const schema = data.params_schema || {};
    const toolsCache = window.__toolsCache = window.__toolsCache || null;
    bot.innerHTML = '';
    // Fetch template string sekali dari catalog (cached)
    if (data.tool && data.tool !== 'none') {
      if (!window.__toolsCache) {
        try { window.__toolsCache = (await api('/api/catalog')).tools; } catch (e) {}
      }
      const tmpl = (window.__toolsCache && window.__toolsCache[data.tool]) ?
        window.__toolsCache[data.tool].template : null;
      data._template = tmpl;
    }
    pushHistory('bot', {
      tool: data.tool, confidence: data.confidence,
      risky: data.risky, preview: data.preview,
    });
    renderBotResponse(bot, data);
  } catch (e) {
    bot.innerHTML = '<em style="color:var(--red)">error: ' + e.message + '</em>';
  }
});

// Enter untuk kirim (shift+enter newline)
$('#input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    $('#chat-form').dispatchEvent(new Event('submit'));
  }
});

loadHealth();
loadCatalog();
</script>
</body>
</html>
"""
