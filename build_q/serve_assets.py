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

  /* ── E2E CI/CD ───────────────────────────────────────────────────── */
  .e2e-form { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; align-items: center; }
  .e2e-form input, .e2e-form select {
    background: var(--bg); color: var(--fg); border: 1px solid var(--border);
    border-radius: 4px; padding: 5px 8px; font-family: var(--mono); font-size: 12px;
  }
  .e2e-form input { min-width: 200px; }
  .e2e-form .or { color: var(--muted); font-size: 12px; }
  .e2e-head { font-family: var(--mono); font-size: 12px; color: var(--muted); margin-top: 4px; }
  .e2e-chain { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 10px; }
  .e2e-chain .node {
    padding: 4px 10px; border-radius: 999px; border: 1px solid var(--border);
    font-size: 12px; font-family: var(--mono); background: var(--bg);
  }
  .e2e-chain .arrow { color: var(--muted); }
  .e2e-chain .node.bp { outline: 2px solid currentColor; outline-offset: 1px; }
  .st-ok { color: var(--green); border-color: var(--green) !important; }
  .st-warn, .st-running { color: var(--yellow); border-color: var(--yellow) !important; }
  .st-fail { color: var(--red); border-color: var(--red) !important; }
  .st-skip, .st-pending { color: var(--muted); }
  .st-active { color: var(--accent); border-color: var(--accent) !important; }
  .e2e-stage {
    margin-top: 8px; border: 1px solid var(--border); border-left-width: 3px;
    border-radius: 6px; padding: 8px 12px; background: var(--bg); font-size: 13px;
  }
  .e2e-stage summary { cursor: pointer; font-weight: 600; }
  .e2e-stage summary .sum { font-weight: 400; color: var(--fg); }
  .e2e-stage ul { margin: 6px 0 0 0; padding-left: 18px; font-family: var(--mono); font-size: 12px; }
  .e2e-stage .hint { margin-top: 4px; font-size: 12px; }
  .e2e-stage pre {
    margin: 6px 0 0 0; padding: 6px 8px; background: #010409; border-radius: 4px;
    font-size: 11px; white-space: pre-wrap; max-height: 220px; overflow-y: auto; color: var(--muted);
  }
  .e2e-verdict { margin-top: 10px; font-weight: 600; }
</style>
</head>
<body>
<header>
  <h1>Jev Web Chat</h1>
  <span class="meta" id="meta">connecting…</span>
  <span class="spacer"></span>
  <span class="meta" id="pb-user"></span>
  <button id="btn-pb" title="Login PocketBase (SSO GitHub) — credential E2E diambil dari akun Anda">🔐 Login</button>
  <button id="btn-e2e" title="Analisa CI/CD end-to-end: trigger → pipeline → GitOps → ArgoCD">🔭 E2E</button>
  <button id="btn-export" title="Export chat ke Markdown">📄 Export</button>
  <button id="btn-clear" title="Hapus riwayat chat">🗑 Clear</button>
  <label class="meta" for="provider-select">provider</label>
  <select id="provider-select"></select>
</header>

<main id="chat"></main>

<footer>
  <form id="chat-form">
    <textarea id="input" placeholder="Ketik request natural (ID/EN) — contoh: bootstrap pay-be-topup develop ke gitops cce production-qoin · /e2e REPO REF · /e2e --sim [SKENARIO]" autofocus></textarea>
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
  opts = Object.assign({}, opts || {});
  const headers = Object.assign({'Content-Type': 'application/json'}, opts.headers || {});
  const tok = getToken();
  if (tok) headers['Authorization'] = 'Bearer ' + tok;
  opts.headers = headers;
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({ error: 'invalid JSON' }));
  if (r.status === 401 && data.pb_login) {
    pbLogout();
    const err = new Error(data.error || 'login PocketBase dibutuhkan');
    err.pbLogin = true;
    throw err;
  }
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

// ─── Login PocketBase user (SSO GitHub via Dex) — credential E2E per-user ──
// Alur sama dengan `bq auth login` (realtime @oauth2 + PKCE dari server).
// Token disimpan di browser ini saja; server tidak menyimpan apa pun.
const PB_AUTH_KEY = 'bq_pb_auth';
const PB_COLLECTION = 'devops_users';
let pbUrl = '';

function jwtExp(token) {
  try {
    const p = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    return JSON.parse(atob(p + '='.repeat((4 - p.length % 4) % 4))).exp || 0;
  } catch (_) { return 0; }
}
function pbAuth() {
  try {
    const a = JSON.parse(localStorage.getItem(PB_AUTH_KEY) || 'null');
    return a && a.token && jwtExp(a.token) * 1000 > Date.now() ? a : null;
  } catch (_) { return null; }
}
function pbLogout() {
  try { localStorage.removeItem(PB_AUTH_KEY); } catch (_) {}
  renderPbUser();
}
function renderPbUser() {
  const a = pbAuth();
  $('#pb-user').textContent = a ? '👤 ' + (a.email || a.id) : '';
  $('#btn-pb').textContent = a ? 'Logout' : '🔐 Login';
}

function pbLoginSSO() {
  if (!pbUrl) return Promise.reject(new Error('PocketBase URL tidak diketahui (cek /api/health)'));
  // Popup dibuka sinkron di handler klik agar tidak diblokir browser.
  const popup = window.open('', 'bq-pb-oauth2', 'width=520,height=680');
  return new Promise((resolve, reject) => {
    let es, timer;
    const finish = (err, val) => {
      clearTimeout(timer);
      if (es) es.close();
      if (popup && !popup.closed) popup.close();
      err ? reject(err) : resolve(val);
    };
    timer = setTimeout(() => finish(new Error('timeout login SSO (5 menit)')), 300000);
    fetch(pbUrl + '/api/collections/' + PB_COLLECTION + '/auth-methods')
      .then(r => r.json())
      .then(methods => {
        const providers = ((methods.oauth2 || {}).providers) || [];
        const prov = providers.find(p => p.name === 'oidc') || providers[0];
        if (!prov) throw new Error('provider OAuth2 tidak tersedia');
        const redirect = pbUrl + '/api/oauth2-redirect';
        es = new EventSource(pbUrl + '/api/realtime');
        es.addEventListener('PB_CONNECT', async (ev) => {
          try {
            const clientId = JSON.parse(ev.data).clientId;
            await fetch(pbUrl + '/api/realtime', { method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({ clientId, subscriptions: ['@oauth2'] })});
            const url = new URL(prov.authURL + encodeURIComponent(redirect));
            url.searchParams.set('state', clientId);
            if (popup && !popup.closed) popup.location = url.toString();
            else window.open(url.toString(), 'bq-pb-oauth2');
            es.addEventListener('@oauth2', async (ev2) => {
              try {
                const msg = JSON.parse(ev2.data || '{}');
                if (msg.error) throw new Error('OAuth2 ditolak: ' + msg.error);
                if (msg.state !== clientId) throw new Error('state OAuth2 tidak cocok');
                const r = await fetch(pbUrl + '/api/collections/' + PB_COLLECTION + '/auth-with-oauth2', {
                  method: 'POST', headers: {'Content-Type': 'application/json'},
                  body: JSON.stringify({ provider: prov.name, code: msg.code,
                    codeVerifier: prov.codeVerifier, redirectURL: redirect,
                    createData: { role: 'view' } })});
                const d = await r.json();
                if (!r.ok || !d.token) throw new Error(d.message || ('HTTP ' + r.status));
                const rec = d.record || {};
                const auth = { token: d.token, id: rec.id, email: rec.email || '' };
                localStorage.setItem(PB_AUTH_KEY, JSON.stringify(auth));
                renderPbUser();
                finish(null, auth);
              } catch (e) { finish(e); }
            });
          } catch (e) { finish(e); }
        });
        es.onerror = () => { if (es.readyState === EventSource.CLOSED) finish(new Error('koneksi realtime PocketBase terputus')); };
      })
      .catch(e => finish(e));
  });
}

$('#btn-pb').addEventListener('click', () => {
  if (pbAuth()) return pbLogout();
  pbLoginSSO().catch(e => alert('login gagal: ' + e.message));
});

async function loadHealth() {
  try {
    const h = await api('/api/health');
    pbUrl = h.pb_url || '';
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

// ─── E2E CI/CD: trigger → pipeline → GitOps → ArgoCD (read-only) ─────────
const E2E_STAGES = [
  ['source', 'Source'], ['trigger', 'Trigger'], ['pipeline', 'Pipeline'],
  ['gitops', 'GitOps'], ['argocd', 'ArgoCD'],
];
const E2E_ICON = { ok: '✅', warn: '⚠️', fail: '❌', skip: '⏭️', running: '⏳' };
let e2eScenarios = null;

async function loadE2EScenarios() {
  if (!e2eScenarios) e2eScenarios = (await api('/api/e2e/scenarios')).scenarios;
  return e2eScenarios;
}

function withToken(url) {
  const tok = getToken();
  return tok ? url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(tok) : url;
}

function renderE2EChain(chain, stages, activeKey, bpKey) {
  chain.innerHTML = '';
  E2E_STAGES.forEach(([key, title], i) => {
    if (i) chain.appendChild(el('span', 'arrow', '→'));
    const st = stages[key];
    let cls = 'st-pending', icon = '○';
    if (st) { cls = 'st-' + st.status; icon = E2E_ICON[st.status] || '?'; }
    else if (key === activeKey) { cls = 'st-active'; icon = '⏳'; }
    const node = el('span', 'node ' + cls + (key === bpKey ? ' bp' : ''), `${icon} ${st ? st.title : title}`);
    chain.appendChild(node);
  });
}

function renderE2EStage(s, isBp) {
  const box = el('details', 'e2e-stage st-' + s.status);
  if (isBp || s.status === 'fail' || s.status === 'warn') box.open = true;
  const sum = el('summary');
  sum.appendChild(document.createTextNode(`${E2E_ICON[s.status] || ''} ${s.title}: `));
  sum.appendChild(el('span', 'sum', s.summary || ''));
  box.appendChild(sum);
  if (s.details && s.details.length) {
    const ul = el('ul');
    s.details.forEach(d => ul.appendChild(el('li', '', d)));
    box.appendChild(ul);
  }
  const logTail = s.data && s.data.log_tail;
  if (logTail) box.appendChild(el('pre', '', String(logTail).trimEnd()));
  (s.hints || []).forEach(h => box.appendChild(el('div', 'hint', '💡 ' + h)));
  return box;
}

async function runE2E(parent, req) {
  const label = req.simulate ? `🧪 simulasi ${req.simulate}` : `${req.repo} @ ${req.ref}`;
  parent.appendChild(el('div', 'e2e-head', '🔭 E2E CI/CD — ' + label));
  const chain = el('div', 'e2e-chain');
  parent.appendChild(chain);
  const stages = {};
  renderE2EChain(chain, stages, 'source', null);
  const body = el('div');
  parent.appendChild(body);
  try {
    const auth = pbAuth();
    if (!auth) {
      const err = new Error('Login PocketBase dulu — credential E2E (GITHUB_TOKEN, kubeconfig) diambil dari akun Anda.');
      err.pbLogin = true;
      throw err;
    }
    const spawn = await api('/api/e2e', { method: 'POST', body: JSON.stringify(req),
      headers: { 'X-PB-Token': auth.token } });
    const es = new EventSource(withToken(spawn.stream_url));
    es.addEventListener('stage', (ev) => {
      renderE2EChain(chain, stages, JSON.parse(ev.data).name, null);
      chat.scrollTop = chat.scrollHeight;
    });
    es.addEventListener('report', (ev) => {
      const r = JSON.parse(ev.data);
      r.stages.forEach(s => { stages[s.key] = s; });
      renderE2EChain(chain, stages, null, r.break_point);
      if (r.sha) {
        parent.insertBefore(el('div', 'e2e-head',
          `commit ${r.sha.slice(0, 7)} · ENV=${r.env} · image ${r.image}:${r.expected_tag}`), chain);
      }
      r.stages.forEach(s => body.appendChild(renderE2EStage(s, s.key === r.break_point)));
      const vcls = r.verdict === 'healthy' ? 'st-ok' : (r.verdict === 'broken' ? 'st-fail' : 'st-warn');
      body.appendChild(el('div', 'e2e-verdict ' + vcls, r.verdict_line));
      if (r.simulate) {
        const ok = !(r.sim_problems || []).length;
        body.appendChild(el('div', 'usage', ok ? '🧪 sesuai harapan skenario'
          : '🧪 tidak sesuai skenario: ' + r.sim_problems.join('; ')));
      }
      const copy = el('button', 'secondary', 'Copy laporan');
      copy.style.marginTop = '8px';
      copy.addEventListener('click', async () => {
        await navigator.clipboard.writeText(r.text);
        copy.textContent = '✓ copied';
        setTimeout(() => copy.textContent = 'Copy laporan', 1200);
      });
      body.appendChild(copy);
      pushHistory('bot', { tool: 'e2e', confidence: 1, preview: 'bq --e2e ' +
        (r.simulate ? '--simulate ' + r.simulate : `${r.repo} ${r.ref}`),
        exit: r.exit_code, output: r.text });
      chat.scrollTop = chat.scrollHeight;
    });
    es.addEventListener('error', (ev) => {
      if (ev.data) body.appendChild(el('div', 'st-fail', 'error: ' + JSON.parse(ev.data).error));
    });
    es.addEventListener('done', () => es.close());
    es.onerror = () => es.close();
  } catch (e) {
    body.appendChild(el('div', 'st-fail', 'error: ' + e.message));
    if (e.pbLogin) {
      const login = el('button', '', '🔐 Login PocketBase & ulangi');
      login.style.marginTop = '8px';
      login.addEventListener('click', () => pbLoginSSO().then(() => {
        parent.innerHTML = '';
        runE2E(parent, req);
      }).catch(err => alert('login gagal: ' + err.message)));
      body.appendChild(login);
    }
  }
}

async function openE2EForm() {
  appendMsg('bot', (m) => {
    m.appendChild(el('div', '', '🔭 Analisa CI/CD end-to-end (read-only): trigger → pipeline → GitOps → ArgoCD'));
    const form = el('form', 'e2e-form');
    const repo = el('input'); repo.placeholder = 'repo (mis. pay-be-topup-manager)';
    const ref = el('input'); ref.placeholder = 'ref (develop / v1.2.3)'; ref.style.minWidth = '140px';
    const go = el('button', '', 'Analisa');
    const sim = document.createElement('select');
    sim.appendChild(new Option('— simulasi —', ''));
    const simBtn = el('button', 'secondary', '🧪 Simulasi');
    simBtn.type = 'button';
    [repo, ref, go, el('span', 'or', 'atau'), sim, simBtn].forEach(n => form.appendChild(n));
    m.appendChild(form);
    const out = el('div');
    m.appendChild(out);
    loadE2EScenarios().then(list => list.forEach(sc =>
      sim.appendChild(new Option(`${sc.name} — ${sc.title}`, sc.name)))).catch(() => {});
    form.addEventListener('submit', (ev) => {
      ev.preventDefault();
      if (!repo.value.trim() || !ref.value.trim()) return;
      const res = el('div'); out.appendChild(res);
      runE2E(res, { repo: repo.value.trim(), ref: ref.value.trim() });
    });
    simBtn.addEventListener('click', () => {
      if (!sim.value) return;
      const res = el('div'); out.appendChild(res);
      runE2E(res, { simulate: sim.value });
    });
    setTimeout(() => repo.focus(), 0);
  });
}

// `/e2e REPO REF` · `/e2e --sim [SKENARIO]` → true bila pesan ditangani di sini
async function handleE2ECommand(msg) {
  const parts = msg.split(/\s+/);
  if (parts[0] !== '/e2e') return false;
  if (parts.length === 1) { openE2EForm(); return true; }
  if (parts[1] === '--sim' || parts[1] === '--simulate') {
    if (!parts[2]) {
      appendMsg('bot', (m) => {
        m.appendChild(el('div', '', 'Skenario simulasi — ketik /e2e --sim <nama>:'));
        const ul = el('ul');
        m.appendChild(ul);
        loadE2EScenarios().then(list => list.forEach(sc =>
          ul.appendChild(el('li', '', `${sc.name} — ${sc.title} (titik putus: ${sc.break_point || '-'}, ${sc.verdict})`))))
          .catch(e => ul.appendChild(el('li', 'st-fail', e.message)));
      });
      return true;
    }
    appendMsg('bot', (m) => runE2E(m, { simulate: parts[2] }));
    return true;
  }
  if (parts.length < 3) {
    appendMsg('bot', (m) => m.appendChild(el('div', 'st-fail',
      'usage: /e2e REPO REF  ·  /e2e --sim [SKENARIO]  ·  /e2e (form)')));
    return true;
  }
  appendMsg('bot', (m) => runE2E(m, { repo: parts[1], ref: parts[2] }));
  return true;
}

$('#btn-e2e').addEventListener('click', openE2EForm);

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
  if (await handleE2ECommand(msg)) return;
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

renderPbUser();
loadHealth();
loadCatalog();
</script>
</body>
</html>
"""
