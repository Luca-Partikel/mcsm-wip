/* Minecraft Server Manager – Verwaltung der gehosteten Server.
   Kein Framework, kein Bauschritt: eine Datei, die im Browser direkt läuft.
   Die Seite spricht ausschließlich über die API aus hosted/ARCHITEKTUR.md; welche Routen und
   Felder erwartet werden, steht in hosted/spec-admin.md. */
'use strict';

/* ------------------------------------------------------------------ Hilfen */

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
const esc = (s) => String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* Beispielansicht: beim Öffnen der Datei ohne Server (file://) oder mit ?beispiel=1.
   Dann wird nichts gesendet, sondern eine kleine eingebaute Antwort angezeigt. */
const PARAMS = new URLSearchParams(location.search);
const DEMO = location.protocol === 'file:' || PARAMS.get('beispiel') === '1' || PARAMS.get('demo') === '1';

const TOKEN_KEY = 'mcsm_admin_token';
const HOST_FALLBACK = 'arcardia-nexus.de';
const REFRESH_MS = 5000;

const state = {
  token: '',
  me: null,
  view: 'users',
  status: null,
  users: [],
  invites: [],
  passes: [],
  instances: [],
  passLimits: { days: [1, 3650], max_concurrent: [1, 10], ram_total_mb: [1024, 10240] },
  open: new Set(),          // aufgeklappte Benutzerzeilen
  form: { kind: 'local' },
  busy: false,
  lastError: '',
  freeze: 0,                // solange > 0: keine Auffrischung (Rückfrage offen)
};

const nowSec = () => Math.floor(Date.now() / 1000);

const fmtDate = (ts) => ts
  ? new Date(ts * 1000).toLocaleString('de-DE', { dateStyle: 'medium', timeStyle: 'short' })
  : '–';
const fmtDay = (ts) => ts
  ? new Date(ts * 1000).toLocaleDateString('de-DE', { dateStyle: 'long' })
  : '–';

function fmtBytes(n) {
  const v = Number(n) || 0;
  if (v < 1024) return v + ' B';
  if (v < 1048576) return (v / 1024).toFixed(1) + ' KB';
  if (v < 1073741824) return (v / 1048576).toFixed(1) + ' MB';
  return (v / 1073741824).toFixed(v < 10737418240 ? 2 : 1) + ' GB';
}

/* Arbeitsspeicher in MB als handliche Angabe – 8192 wird zu „8 GB“. */
function gb(mb) {
  const v = Number(mb) || 0;
  if (!v) return '0 GB';
  if (v < 1024) return v + ' MB';
  const g = v / 1024;
  return (Math.round(g * 10) / 10).toString().replace('.', ',') + ' GB';
}

const plural = (n, one, many) => (Number(n) === 1 ? one : many);

function fmtUptime(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return d + ' ' + plural(d, 'Tag', 'Tage') + ' ' + h + ' h';
  if (h) return h + ' h ' + m + ' min';
  return m + ' min';
}

function toast(msg, err = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (err ? ' err' : '');
  el.textContent = String(msg || '');
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), err ? 8000 : 4000);
}

/* Text in die Zwischenablage legen – mit Rückfallweg für Browser ohne Freigabe. */
async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) { /* weiter unten versuchen */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', 'readonly');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  } catch (e) {
    return false;
  }
}

/* --------------------------------------------------------------- Rückfrage */

/* Zeigt eine Rückfrage. Ergebnis: null bei Abbruch, sonst der Text des Eingabefeldes
   (leer, wenn keines gewünscht war). Solange die Rückfrage offen ist, wird nicht aufgefrischt. */
function ask({ title, text, ok = 'Ja', danger = false, input = null }) {
  return new Promise((resolve) => {
    state.freeze += 1;
    const host = $('#modalHost');
    const field = input ? `
      <div class="field" style="margin-top:14px">
        <label for="askInput">${esc(input.label || '')}</label>
        <input type="${input.type === 'number' ? 'number' : 'text'}" id="askInput"
               value="${esc(input.value === undefined ? '' : input.value)}"
               placeholder="${esc(input.placeholder || '')}"
               ${input.min !== undefined ? 'min="' + esc(input.min) + '"' : ''}
               ${input.max !== undefined ? 'max="' + esc(input.max) + '"' : ''}>
        ${input.hint ? `<div class="hint">${esc(input.hint)}</div>` : ''}
      </div>` : '';
    host.innerHTML = `
      <div class="modal-back" id="askBack">
        <div class="modal" role="dialog" aria-modal="true">
          <h3>${esc(title)}</h3>
          <div class="small">${text}</div>
          ${field}
          <div class="btn-row">
            <button class="btn" id="askNo">Abbrechen</button>
            <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" id="askYes">${esc(ok)}</button>
          </div>
        </div>
      </div>`;
    const done = (value) => {
      host.innerHTML = '';
      state.freeze = Math.max(0, state.freeze - 1);
      document.removeEventListener('keydown', onKey);
      resolve(value);
    };
    const onKey = (ev) => {
      if (ev.key === 'Escape') done(null);
      if (ev.key === 'Enter' && ev.target.id === 'askInput') $('#askYes').click();
    };
    document.addEventListener('keydown', onKey);
    $('#askNo').onclick = () => done(null);
    $('#askBack').onclick = (ev) => { if (ev.target.id === 'askBack') done(null); };
    $('#askYes').onclick = () => done(input ? String($('#askInput').value || '') : '');
    const box = $('#askInput');
    if (box) box.focus();
    else $('#askYes').focus();
  });
}

/* ------------------------------------------------------------------- API */

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function api(path, { method = 'GET', body } = {}) {
  if (DEMO) return demoApi(path, method, body);
  let res;
  try {
    const headers = { Accept: 'application/json' };
    if (state.token) headers.Authorization = 'Bearer ' + state.token;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    res = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: 'no-store',
      // Wichtig für die Anmeldung über Discord: der Start setzt ein kurzlebiges Cookie, das den
      // Vorgang an diesen Browser bindet. Ohne das Cookie lehnt die Rückleitung ab.
      credentials: 'same-origin',
    });
  } catch (e) {
    throw new ApiError('Der Server ist gerade nicht erreichbar.', 0);
  }
  let data = {};
  try { data = await res.json(); } catch (e) { data = {}; }
  if (!res.ok) {
    const text = (data && typeof data.error === 'string' && data.error)
      ? data.error
      : httpText(res.status);
    throw new ApiError(text, res.status);
  }
  return data || {};
}

/* Auch ohne verständlichen Satz vom Server soll nie eine rohe Fehlernummer stehen. */
function httpText(status) {
  if (status === 401) return 'Bitte neu anmelden.';
  if (status === 403) return 'Dafür fehlen diesem Konto die Rechte.';
  if (status === 404) return 'Das gibt es nicht (mehr).';
  if (status === 409) return 'Das geht gerade nicht.';
  if (status === 429) return 'Zu viele Anfragen in kurzer Zeit – bitte einen Moment warten.';
  if (status >= 500) return 'Auf dem Server ist etwas schiefgegangen.';
  return 'Die Anfrage konnte nicht bearbeitet werden.';
}

/* --------------------------------------------------------- Anmeldezustand */

function readToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; }
}
function writeToken(token) {
  state.token = token || '';
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch (e) { /* ohne Ablage geht es nur bis zum Neuladen */ }
}

/* Die Rückleitung von Discord hängt das Sitzungstoken an: …/admin.html#token=… */
function tokenFromHash() {
  const raw = String(location.hash || '').replace(/^#/, '');
  if (!raw) return '';
  const q = new URLSearchParams(raw);
  const fehler = q.get('fehler') || q.get('error');
  if (fehler) showLoginNote(fehler, true);
  const verknuepft = q.get('verknuepft') === '1';
  if (verknuepft) {
    // Rueckkehr vom Verknuepfen: die Sitzung blieb bestehen, nur das Discord-Konto ist neu dran.
    setTimeout(() => toast('Dein Discord-Konto ist jetzt verknüpft – ab sofort reicht „Mit Discord anmelden“.'), 300);
  }
  const token = q.get('token') || '';
  if (token || fehler || verknuepft) {
    history.replaceState(null, '', location.pathname + location.search);
  }
  return /^[A-Za-z0-9._-]{16,512}$/.test(token) ? token : '';
}

function showLoginNote(text, err = false) {
  const box = $('#loginNote');
  if (!text) { box.innerHTML = ''; return; }
  box.innerHTML = `<div class="note ${err ? 'note-err' : 'note-info'} small">${esc(text)}</div>`;
}

function showLogin(note = '', err = false) {
  $('#shell').classList.add('hidden');
  $('#login').classList.remove('hidden');
  showLoginNote(note, err);
  if (!note) erstzugangPruefen();
}

/* Auf einer frischen Anlage gibt es noch keinen Betreiber. Das erste Konto entsteht nur mit dem
   Code aus ERSTER-ADMIN.txt – auch über Discord. Deshalb hier das Codefeld gleich aufklappen und
   sagen, woher der Code kommt; sonst steht der Betreiber vor einer Tür ohne Klinke. */
async function erstzugangPruefen() {
  let daten;
  try {
    daten = await api('/api/health');
  } catch (e) { return; }
  if (!daten || !daten.erstzugang) return;
  $('#codeBox').classList.remove('hidden');
  showLoginNote('Dieser Server hat noch keinen Betreiber. Bitte den Einladungscode aus der Datei '
    + 'ERSTER-ADMIN.txt eintragen (sie liegt im Datenordner des Root-Servers). Der Code gilt für '
    + 'beide Wege: unten „Anmelden“ oder oben „Mit Discord anmelden“.');
}

function logoutLocal(note = '', err = false) {
  writeToken('');
  state.me = null;
  showLogin(note, err);
}

async function doLogout() {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch (e) { /* egal */ }
  logoutLocal('Du bist abgemeldet.');
}

async function loginDiscord() {
  const btn = $('#btnDiscord');
  btn.disabled = true;
  try {
    const ziel = location.origin + location.pathname;
    // Beim allerersten Konto muss der Code aus ERSTER-ADMIN.txt mit – sonst legt der Server
    // auch über Discord kein Betreiberkonto an (siehe core/oauth.py, konto_fuer).
    const code = String(($('#inviteCode') || {}).value || '').trim();
    const data = await api('/api/auth/discord/start?ziel=' + encodeURIComponent(ziel)
      + (code ? '&code=' + encodeURIComponent(code) : ''));
    const url = String(data.url || '');
    if (!/^https:\/\/(discord\.com|discordapp\.com)\//.test(url)) {
      throw new ApiError('Die Anmeldung über Discord ist auf diesem Server nicht richtig eingerichtet.', 0);
    }
    try { sessionStorage.setItem('mcsm_admin_state', String(data.state || '')); } catch (e) { /* egal */ }
    location.href = url;
  } catch (e) {
    showLoginNote(e.message, true);
    $('#codeBox').classList.remove('hidden');
  } finally {
    btn.disabled = false;
  }
}

/* Discord nachträglich an das angemeldete Konto hängen (kein neuer Login, keine neue Sitzung). */
async function linkDiscord() {
  const btn = $('#btnLink');
  if (!btn) return;
  btn.disabled = true;
  try {
    const ziel = location.origin + location.pathname;
    const data = await api('/api/auth/discord/start?link=1&ziel=' + encodeURIComponent(ziel));
    const url = String(data.url || '');
    if (!/^https:\/\/(discord\.com|discordapp\.com)\//.test(url)) {
      throw new ApiError('Discord ist auf diesem Server nicht richtig eingerichtet.', 0);
    }
    location.href = url;
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function loginCode() {
  const btn = $('#btnCode');
  const code = String($('#inviteCode').value || '').trim();
  const name = String($('#inviteName').value || '').trim();
  if (!code || !name) {
    showLoginNote('Bitte Code und Anzeigename eintragen.', true);
    return;
  }
  btn.disabled = true;
  try {
    const data = await api('/api/auth/invite', { method: 'POST', body: { code, name, note: 'Verwaltung im Browser' } });
    if (!data.token) throw new ApiError('Der Server hat kein Sitzungstoken geschickt.', 0);
    writeToken(String(data.token));
    showLoginNote('');
    await start();
  } catch (e) {
    showLoginNote(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------- Laden (5 s) */

async function start() {
  try {
    const me = await api('/api/me');
    state.me = me;
    renderMe();
    if (String((me.user || {}).role || '') !== 'admin') {
      $('#shell').classList.remove('hidden');
      $('#login').classList.add('hidden');
      $('#tabs').classList.add('hidden');
      $('#globalNote').innerHTML = `<div class="note note-warn"><h3>Keine Betreiberrechte</h3>
        <p class="mb0">Dieses Konto darf die Verwaltung nicht öffnen. Melde dich mit dem
        Betreiberkonto an.</p></div>`;
      return;
    }
    $('#login').classList.add('hidden');
    $('#shell').classList.remove('hidden');
    $('#tabs').classList.remove('hidden');
    if (DEMO) $('#demoStrip').classList.remove('hidden');
    await tick();
  } catch (e) {
    if (e.status === 401 || e.status === 403) logoutLocal(e.message, true);
    else showLogin(e.message, true);
  }
}

async function tick() {
  if (state.busy || state.freeze > 0 || (!state.token && !DEMO)) return;
  if (document.hidden) return;
  state.busy = true;
  try {
    const jobs = [api('/api/admin/status'), api('/api/admin/users')];
    if (state.view === 'invites') jobs.push(api('/api/admin/invites'));
    if (state.view === 'passes') jobs.push(api('/api/admin/passes'));
    /* Die Instanzen braucht fast jede Ansicht – auch die Rückfrage beim Widerruf eines Passes
       nennt die Server, die dann heruntergefahren werden. */
    if (state.view !== 'invites') jobs.push(api('/api/admin/instances'));
    const res = await Promise.all(jobs);
    state.status = res[0] || {};
    state.users = (res[1] || {}).users || [];
    for (const part of res.slice(2)) {
      if (Array.isArray(part.invites)) state.invites = part.invites;
      if (Array.isArray(part.passes)) {
        state.passes = part.passes;
        if (part.limits) state.passLimits = part.limits;
      }
      if (Array.isArray(part.instances)) state.instances = part.instances;
    }
    state.lastError = '';
    $('#globalNote').innerHTML = '';
    render();
  } catch (e) {
    if (e.status === 401) { logoutLocal('Die Sitzung ist abgelaufen – bitte neu anmelden.', true); return; }
    if (e.message !== state.lastError) {
      state.lastError = e.message;
      $('#globalNote').innerHTML = `<div class="note note-err"><h3>Die Daten sind gerade nicht aktuell</h3>
        <p class="mb0">${esc(e.message)} Die Seite versucht es weiter.</p></div>`;
    }
  } finally {
    state.busy = false;
  }
}

/* ----------------------------------------------------------- Zeichnen */

/* Nur neu zeichnen, wenn sich etwas geändert hat – und niemals, während in diesem
   Bereich etwas eingetippt wird. So gehen Eingaben beim Auffrischen nicht verloren. */
function paint(el, html, sig) {
  if (!el) return;
  if (el.dataset.sig === sig) return;
  const focus = document.activeElement;
  if (focus && el.contains(focus) && /^(INPUT|SELECT|TEXTAREA)$/.test(focus.tagName)) return;
  el.dataset.sig = sig;
  el.innerHTML = html;
}

function renderMe() {
  const user = (state.me && state.me.user) || {};
  const name = String(user.discord_name || user.name || '');
  $('#meName').textContent = name || '–';
  $('#meRole').textContent = user.role === 'admin' ? 'Betreiber' : 'Benutzer';
  const link = $('#btnLink');
  if (link) {
    // Wer sich nur mit Einladungscode angemeldet hat, kann Discord hier nachtragen.
    const verknuepft = !!(user.discord_linked || user.discord_name || user.discord_id);
    link.classList.toggle('hidden', verknuepft);
  }
  const av = $('#meAvatar');
  const url = String(user.avatar_url || user.discord_avatar || '');
  if (/^https:\/\/cdn\.discordapp\.com\//.test(url)) {
    av.innerHTML = `<img class="avatar" src="${esc(url)}" alt="" width="34" height="34">`;
    av.style.border = 'none';
  } else {
    av.textContent = (name.trim()[0] || '?').toUpperCase();
  }
}

function render() {
  renderMe();
  if (state.view === 'users') renderUsers();
  if (state.view === 'invites') renderInvites();
  if (state.view === 'passes') renderPasses();
  if (state.view === 'servers') renderServers();
  if (state.view === 'machine') renderMachine();
  renderHeadSub();
}

function renderHeadSub() {
  const st = state.status || {};
  const running = Array.isArray(st.running) ? st.running.filter((r) => r && r.running).length : 0;
  $('#headSub').textContent = `${running} ${plural(running, 'Server läuft', 'Server laufen')} · `
    + `${gb(st.ram_running_mb)} belegt · ${st.disk_free_text || fmtBytes(st.disk_free_bytes)} Platte frei`;
}

const userName = (uid) => {
  const u = state.users.find((x) => x.id === uid);
  return u ? String(u.name || uid) : String(uid || '');
};

const statePill = (inst) => {
  const s = String(inst.state || '');
  const text = String(inst.state_text || s);
  if (inst.running) return `<span class="pill pill-green">läuft</span>`;
  if (s === 'hosted') return `<span class="pill pill-grey">gestoppt</span>`;
  if (s === 'awaiting_pull') return `<span class="pill pill-amber">${esc(text)}</span>`;
  if (s === 'suspended') return `<span class="pill pill-amber">ruht</span>`;
  if (s === 'uploading' || s === 'downloading') return `<span class="pill pill-blue">${esc(text)}</span>`;
  return `<span class="pill pill-grey">${esc(text)}</span>`;
};

const passPill = (p) => {
  if (p.state === 'active') return `<span class="pill pill-green">gültig</span>`;
  if (p.state === 'revoked') return `<span class="pill pill-red">zurückgezogen</span>`;
  return `<span class="pill pill-grey">abgelaufen</span>`;
};

function statTile(k, v, d, cls = '') {
  return `<div class="stat ${cls}"><div class="k">${esc(k)}</div>
    <div class="v">${esc(v)}</div>${d ? `<div class="d">${esc(d)}</div>` : ''}</div>`;
}

/* ------------------------------------------------------------ Benutzer */

function renderUsers() {
  const st = state.status || {};
  const withPass = state.users.filter((u) => ((u.limits || {}).max_concurrent || 0) > 0).length;
  const blocked = state.users.filter((u) => u.blocked).length;
  paint($('#usersKpis'), [
    statTile('Benutzer', String(state.users.length), blocked ? blocked + ' gesperrt' : 'keine Sperren'),
    statTile('Mit gültigem Pass', String(withPass), withPass === state.users.length ? 'alle' : 'von ' + state.users.length),
    statTile('Server insgesamt', String((st.instances !== undefined ? st.instances : state.instances.length))),
    statTile('Belegter Platz', st.size_bytes !== undefined ? fmtBytes(st.size_bytes) : '–', 'auf der Platte'),
  ].join(''), 'kpi:' + state.users.length + ':' + blocked + ':' + withPass + ':' + st.size_bytes + ':' + st.instances);

  const rows = state.users.map((u) => {
    const open = state.open.has(u.id);
    const limits = u.limits || {};
    const discord = u.discord_name
      ? esc(u.discord_name)
      : (u.discord_linked ? '<span class="muted">verknüpft</span>' : '<span class="muted">nicht verknüpft</span>');
    const zustand = u.blocked
      ? `<span class="pill pill-red">gesperrt</span>${u.blocked_reason ? `<div class="muted small">${esc(u.blocked_reason)}</div>` : ''}`
      : `<span class="pill pill-green">aktiv</span>`;
    const head = `
      <tr class="clickable ${open ? 'open' : ''}" data-toggle="${esc(u.id)}">
        <td><span class="caret">›</span></td>
        <td><b>${esc(u.name)}</b>
          <div class="muted small">${esc(limits.max_concurrent ? (u.limits_text || '') : 'kein gültiger Pass')}</div></td>
        <td>${discord}</td>
        <td>${u.role === 'admin' ? '<span class="pill pill-blue">Betreiber</span>' : 'Benutzer'}</td>
        <td class="nowrap">${esc(fmtDate(u.created_at))}</td>
        <td class="nowrap">${esc(u.last_seen ? fmtDate(u.last_seen) : '–')}</td>
        <td>${zustand}</td>
        <td class="nowrap">
          <button class="btn btn-sm ${u.blocked ? '' : 'btn-danger'}" data-block="${esc(u.id)}"
                  data-on="${u.blocked ? '0' : '1'}">${u.blocked ? 'Entsperren' : 'Sperren'}</button>
        </td>
      </tr>`;
    if (!open) return head;
    const own = state.instances.filter((i) => i.owner === u.id);
    const servers = own.length
      ? own.map((i) => `<div class="wrow">
          <div><span class="w-name">${esc(i.name)}</span>
            <div class="w-meta">${esc(i.state_text || i.state)} · ${esc(gb(i.ram_mb))} · ${esc(i.size_text || fmtBytes(i.size_bytes))}</div></div>
          <div>${statePill(i)}</div></div>`).join('')
      : '<div class="muted small">Noch keine Server.</div>';
    const list = u.passes || [];
    const passRows = list.length
      ? list.map((p) => `<div class="wrow">
          <div><span class="w-name">${esc(p.kind_text || p.kind)}</span>
            <div class="w-meta">${esc(p.max_concurrent)} ${plural(p.max_concurrent, 'Server', 'Server')} gleichzeitig,
              zusammen ${esc(gb(p.ram_total_mb))} · ${esc(p.remaining_text || '')}</div></div>
          <div>${passPill(p)}</div></div>`).join('')
      : '<div class="muted small">Kein Pass ausgestellt.</div>';
    return head + `
      <tr class="open"><td class="sub" colspan="8">
        <div class="sub-box">
          <div><h4>Server</h4>${servers}</div>
          <div><h4>Pässe</h4>${passRows}</div>
          <div class="btn-row">
            <button class="btn btn-sm" data-topass="${esc(u.id)}">Pass ausstellen</button>
          </div>
        </div>
      </td></tr>`;
  }).join('');

  paint($('#usersBody'), rows || emptyRow(8, 'Noch keine Benutzer', 'Erzeuge unter „Einladungscodes“ einen Code.'),
    'users:' + JSON.stringify(state.users.map((u) => [u.id, u.name, u.role, u.blocked, u.blocked_reason,
      u.last_seen, u.discord_name, u.limits_text, (u.passes || []).length])) +
    ':' + Array.from(state.open).sort().join(',') +
    ':' + JSON.stringify(state.instances.map((i) => [i.id, i.owner, i.state, i.running, i.size_bytes])));
}

function emptyRow(cols, title, text) {
  return `<tr><td colspan="${cols}"><div class="empty"><div class="big">·</div>
    <div><b>${esc(title)}</b></div><div class="small">${esc(text)}</div></div></td></tr>`;
}

/* ---------------------------------------------------------- Einladungen */

function renderInvites() {
  const rows = state.invites.map((v) => {
    const offen = v.state === 'open';
    return `<tr>
      <td><span class="code-chip">${esc(v.code)}</span></td>
      <td><span class="pill ${offen ? 'pill-green' : 'pill-grey'}">${esc(v.state_text || v.state)}</span></td>
      <td class="nowrap">${esc(v.uses)} von ${esc(v.uses_max)}</td>
      <td class="nowrap">${esc(fmtDate(v.created_at))}</td>
      <td class="nowrap">${v.expires_at ? esc(fmtDate(v.expires_at)) : '<span class="muted">läuft nicht ab</span>'}</td>
      <td>${esc(v.note || '')}</td>
      <td class="nowrap"><div class="btn-row">
        <button class="btn btn-sm" data-copy="${esc(v.code)}">Kopieren</button>
        ${offen ? `<button class="btn btn-sm btn-danger" data-revinvite="${esc(v.code)}">Zurückziehen</button>` : ''}
      </div></td>
    </tr>`;
  }).join('');
  paint($('#invitesBody'), rows || emptyRow(7, 'Noch keine Codes', 'Oben einen Code erzeugen.'),
    'inv:' + JSON.stringify(state.invites));
}

async function createInvite() {
  const btn = $('#btnInvite');
  btn.disabled = true;
  try {
    const data = await api('/api/admin/invites', {
      method: 'POST',
      body: {
        uses_max: Number($('#invUses').value) || 1,
        days: Number($('#invDays').value) || 0,
        note: String($('#invNote').value || ''),
      },
    });
    const code = String((data.invite || {}).code || '');
    $('#inviteFresh').innerHTML = `<div class="note note-ok">
      <h3>Code erzeugt</h3>
      <div class="flex wrap">
        <span class="code-chip">${esc(code)}</span>
        <button class="btn btn-sm" data-copy="${esc(code)}">Kopieren</button>
      </div>
      <p class="small mb0" style="margin-top:8px">Diesen Code weitergeben – damit legt sich das Konto
        beim ersten Start selbst an.</p></div>`;
    $('#invNote').value = '';
    toast('Der Einladungscode ist erzeugt.');
    await tick();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function revokeInvite(code) {
  const okay = await ask({
    title: 'Code zurückziehen?',
    text: `Der Code <span class="mono">${esc(code)}</span> lässt sich danach nicht mehr einlösen.
      Bereits angelegte Konten bleiben bestehen.`,
    ok: 'Zurückziehen', danger: true,
  });
  if (okay === null) return;
  try {
    await api('/api/admin/invites/' + encodeURIComponent(code) + '/revoke', { method: 'POST' });
    toast('Der Code ist zurückgezogen.');
    await tick();
  } catch (e) {
    toast(e.message, true);
  }
}

/* ---------------------------------------------------------------- Pässe */

/* Auch gesperrte Konten stehen zur Wahl – ein Pass für ein gesperrtes Konto ist erlaubt,
   der Zustand steht dann in Klammern dahinter. */
function passUsers() {
  return state.users.slice();
}

function syncUserSelect() {
  const sel = $('#pmUser');
  const sig = 'u:' + state.users.map((u) => u.id + ':' + u.name).join('|');
  if (sel.dataset.sig === sig) return;
  const keep = sel.value;
  sel.dataset.sig = sig;
  sel.innerHTML = '<option value="">– bitte wählen –</option>' + passUsers().map((u) =>
    `<option value="${esc(u.id)}">${esc(u.name)}${u.blocked ? ' (gesperrt)' : ''}</option>`).join('');
  if (keep && state.users.some((u) => u.id === keep)) sel.value = keep;
}

function formValues() {
  const days = Math.min(3650, Math.max(1, Number($('#pmDays').value) || 1));
  const slots = Math.min(10, Math.max(1, Number($('#pmSlots').value) || 1));
  const ramGb = Math.min(10, Math.max(1, Number($('#pmRam').value) || 1));
  return {
    user_id: String($('#pmUser').value || ''),
    kind: state.form.kind,
    days,
    max_concurrent: slots,
    ram_total_mb: ramGb * 1024,
    note: String($('#pmNote').value || ''),
  };
}

function renderPreview() {
  const v = formValues();
  $('#pmRamVal').textContent = (v.ram_total_mb / 1024) + ' GB';
  $$('#pmDaysQuick button').forEach((b) => b.classList.toggle('on', Number(b.dataset.days) === v.days));
  const box = $('#pmPreview');
  if (!v.user_id) {
    box.textContent = 'Bitte oben einen Benutzer auswählen.';
    return;
  }
  const name = userName(v.user_id);
  const ende = fmtDay(nowSec() + v.days * 86400);
  const art = v.kind === 'premium'
    ? 'Die Server dürfen dauerhaft hier liegen bleiben, auch ohne Kopie auf dem PC.'
    : 'Nach Ablauf holt ' + name + ' die Server auf den eigenen PC zurück.';
  box.innerHTML = `<b>${esc(name)}</b> darf ab sofort <b>${esc(v.days)} ${plural(v.days, 'Tag', 'Tage')}</b>
    lang <b>${esc(v.max_concurrent)} ${plural(v.max_concurrent, 'Server', 'Server')}</b> gleichzeitig laufen
    lassen, zusammen höchstens <b>${esc(gb(v.ram_total_mb))}</b> Arbeitsspeicher.
    <div class="muted small" style="margin-top:6px">Der Pass endet am ${esc(ende)}. ${esc(art)}</div>`;
}

async function issuePass() {
  const v = formValues();
  if (!v.user_id) { toast('Bitte zuerst einen Benutzer auswählen.', true); return; }
  const btn = $('#btnIssue');
  btn.disabled = true;
  try {
    await api('/api/admin/passes', { method: 'POST', body: v });
    toast(`Der Pass für ${userName(v.user_id)} ist ausgestellt.`);
    $('#pmNote').value = '';
    await tick();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

function renderPasses() {
  syncUserSelect();
  renderPreview();
  const onlyActive = $('#pmOnlyActive').checked;
  const list = state.passes.filter((p) => !onlyActive || p.state === 'active');
  const rows = list.map((p) => `<tr>
    <td><b>${esc(userName(p.user_id))}</b>${p.note ? `<div class="muted small">${esc(p.note)}</div>` : ''}</td>
    <td>${esc(p.kind_text || p.kind)}</td>
    <td class="nowrap">${esc(p.max_concurrent)} gleichzeitig<div class="muted small">zusammen ${esc(gb(p.ram_total_mb))}</div></td>
    <td class="nowrap">${esc(fmtDate(p.issued_at))}<div class="muted small">${esc(p.days)} ${plural(p.days, 'Tag', 'Tage')}</div></td>
    <td class="nowrap">${esc(p.remaining_text || '')}<div class="muted small">bis ${esc(fmtDate(p.expires_at))}</div></td>
    <td>${passPill(p)}</td>
    <td class="nowrap"><div class="btn-row">
      ${p.state === 'revoked' ? '' : `<button class="btn btn-sm" data-extend="${esc(p.id)}">Verlängern</button>`}
      ${p.state === 'active' ? `<button class="btn btn-sm btn-danger" data-revpass="${esc(p.id)}">Widerrufen</button>` : ''}
    </div></td>
  </tr>`).join('');
  paint($('#passesBody'), rows || emptyRow(7, 'Noch keine Pässe', 'Oben im Formular einen Pass ausstellen.'),
    'pass:' + onlyActive + ':' + JSON.stringify(list) + ':' + state.users.map((u) => u.id + u.name).join(','));
}

async function extendPass(pid) {
  const entry = state.passes.find((p) => p.id === pid);
  if (!entry) return;
  const value = await ask({
    title: 'Pass verlängern',
    text: `Der Pass von <b>${esc(userName(entry.user_id))}</b> (${esc(entry.remaining_text || '')})
      wird um die angegebenen Tage verlängert – bei einem abgelaufenen Pass ab heute.`,
    ok: 'Verlängern',
    input: { label: 'Um wie viele Tage', type: 'number', value: 30, min: 1, max: 3650 },
  });
  if (value === null) return;
  const days = Math.min(3650, Math.max(1, Number(value) || 0));
  if (!days) { toast('Bitte eine Zahl von 1 bis 3650 eintragen.', true); return; }
  try {
    await api('/api/admin/passes/' + encodeURIComponent(pid) + '/extend', { method: 'POST', body: { days } });
    toast(`Der Pass ist um ${days} ${plural(days, 'Tag', 'Tage')} verlängert.`);
    await tick();
  } catch (e) {
    toast(e.message, true);
  }
}

async function revokePass(pid) {
  const entry = state.passes.find((p) => p.id === pid);
  if (!entry) return;
  const name = userName(entry.user_id);
  const running = state.instances.filter((i) => i.owner === entry.user_id && i.running);
  const hint = running.length
    ? `Es ${plural(running.length, 'läuft gerade', 'laufen gerade')}
       <b>${running.map((i) => esc(i.name)).join(', ')}</b>.`
    : 'Gerade läuft kein Server dieses Kontos.';
  const value = await ask({
    title: 'Pass widerrufen?',
    text: `${esc(name)} hat danach keinen gültigen Pass mehr. ${hint}
      <br><br>Laufende Server werden <b>in 10 Minuten</b> heruntergefahren – die Spieler bekommen
      vorher eine Nachricht im Spiel. Hochgeladene Server warten anschließend darauf, auf den
      PC zurückgeholt zu werden; reine Root-Server ruhen.`,
    ok: 'Pass widerrufen', danger: true,
  });
  if (value === null) return;
  try {
    await api('/api/admin/passes/' + encodeURIComponent(pid) + '/revoke', { method: 'POST' });
    toast('Der Pass ist widerrufen. Die Server werden angekündigt gestoppt.');
    await tick();
  } catch (e) {
    toast(e.message, true);
  }
}

/* --------------------------------------------------------------- Server */

function portsText(inst) {
  const live = inst.ports_live && Object.keys(inst.ports_live).length ? inst.ports_live : (inst.ports || {});
  const parts = [];
  if (live.java) parts.push('Java ' + live.java);
  if (live.bedrock) parts.push('Bedrock ' + live.bedrock);
  for (const [k, v] of Object.entries(live)) {
    if (k !== 'java' && k !== 'bedrock' && v) parts.push(k + ' ' + v);
  }
  return parts.length ? parts.join(' · ') : '–';
}

function addressOf(inst) {
  const live = inst.ports_live && Object.keys(inst.ports_live).length ? inst.ports_live : (inst.ports || {});
  const port = Number(live.java || live.bedrock || 0);
  if (inst.address) return String(inst.address);
  const host = inst.subdomain ? String(inst.subdomain) : HOST_FALLBACK;
  const std = inst.type === 'bedrock' ? 19132 : 25565;
  return port && port !== std ? host + ':' + port : host;
}

function ramText(inst) {
  const live = inst.live || null;
  if (inst.running && live) {
    const used = Number(live.memory_mb || 0);
    return (used ? gb(used) + ' von ' : '') + gb(live.ram_mb || inst.ram_mb);
  }
  return gb(inst.ram_mb) + ' eingerichtet';
}

function renderServers() {
  const st = state.status || {};
  const running = state.instances.filter((i) => i.running);
  const ramUsed = running.reduce((a, i) => a + Number((i.live || {}).memory_mb || 0), 0);
  paint($('#serversKpis'), [
    statTile('Laufende Server', String(running.length), running.length ? running.map((i) => i.name).join(', ') : 'gerade nichts an'),
    statTile('Vergebener Speicher', gb(st.ram_running_mb), 'tatsächlich belegt: ' + gb(ramUsed)),
    statTile('Server insgesamt', String(state.instances.length)),
    statTile('Platte frei', st.disk_free_text || fmtBytes(st.disk_free_bytes),
      (st.disk_free_percent !== undefined ? String(st.disk_free_percent).replace('.', ',') + ' %' : ''),
      st.disk_warning ? 'bad' : ''),
  ].join(''), 'skpi:' + running.map((i) => i.id + i.name).join(',') + ':' + st.ram_running_mb + ':'
    + state.instances.length + ':' + st.disk_free_bytes + ':' + ramUsed);

  const sorted = state.instances.slice().sort((a, b) => {
    if (a.running !== b.running) return a.running ? -1 : 1;
    const ua = userName(a.owner), ub = userName(b.owner);
    return ua === ub ? String(a.name).localeCompare(String(b.name), 'de') : ua.localeCompare(ub, 'de');
  });
  const rows = sorted.map((i) => {
    const canStart = i.state === 'hosted' && !i.running;
    const canStop = i.running;
    return `<tr>
      <td><b>${esc(i.name)}</b>
        <div class="muted small">${esc(i.type === 'bedrock' ? 'Bedrock' : 'Java')}${i.flavor ? ' · ' + esc(i.flavor) : ''}${i.version ? ' ' + esc(i.version) : ''}
          ${i.origin === 'premium' ? ' · <span class="pill pill-blue">Premium</span>' : ''}</div></td>
      <td>${esc(userName(i.owner))}</td>
      <td>${statePill(i)}${i.running && i.live && i.live.uptime ? `<div class="muted small">seit ${esc(fmtUptime(i.live.uptime))}</div>` : ''}</td>
      <td class="nowrap">${esc(i.size_text || fmtBytes(i.size_bytes))}</td>
      <td class="nowrap">${esc(ramText(i))}</td>
      <td class="nowrap small">${esc(portsText(i))}</td>
      <td class="nowrap small mono">${esc(addressOf(i))}</td>
      <td class="nowrap"><div class="btn-row">
        ${canStart ? `<button class="btn btn-sm btn-primary" data-startsrv="${esc(i.id)}">Starten</button>` : ''}
        ${canStop ? `<button class="btn btn-sm btn-danger" data-stopsrv="${esc(i.id)}">Stoppen</button>` : ''}
      </div></td>
    </tr>`;
  }).join('');
  paint($('#serversBody'), rows || emptyRow(8, 'Noch keine Server', 'Sobald jemand einen Server anlegt, steht er hier.'),
    'srv:' + JSON.stringify(sorted.map((i) => [i.id, i.name, i.owner, i.state, i.running, i.size_bytes,
      (i.live || {}).memory_mb, (i.live || {}).uptime, i.ports, i.ports_live, i.subdomain, i.address]))
    + ':' + state.users.map((u) => u.id + u.name).join(','));

  const sum = state.instances.reduce((a, i) => a + Number(i.size_bytes || 0), 0);
  $('#serversSum').textContent = `${state.instances.length} ${plural(state.instances.length, 'Server', 'Server')} · `
    + `${fmtBytes(sum)} auf der Platte`;
}

async function startServer(id) {
  const inst = state.instances.find((i) => i.id === id);
  try {
    await api('/api/servers/' + encodeURIComponent(id) + '/start', { method: 'POST' });
    toast(`„${inst ? inst.name : id}“ wird gestartet.`);
    await tick();
  } catch (e) {
    toast(e.message, true);
  }
}

async function stopServer(id) {
  const inst = state.instances.find((i) => i.id === id);
  const value = await ask({
    title: 'Server stoppen?',
    text: `„${esc(inst ? inst.name : id)}“ wird angekündigt heruntergefahren. Spieler auf dem Server
      bekommen vorher eine Nachricht, die Welt wird gespeichert.`,
    ok: 'Stoppen', danger: true,
  });
  if (value === null) return;
  try {
    await api('/api/servers/' + encodeURIComponent(id) + '/stop', { method: 'POST' });
    toast(`„${inst ? inst.name : id}“ wird gestoppt.`);
    await tick();
  } catch (e) {
    toast(e.message, true);
  }
}

/* ------------------------------------------------------------- Maschine */

function renderMachine() {
  const st = state.status || {};
  const diskTotal = Number(st.disk_total_bytes || 0);
  const diskFree = Number(st.disk_free_bytes || 0);
  const percent = st.disk_free_percent !== undefined
    ? Number(st.disk_free_percent)
    : (diskTotal ? diskFree / diskTotal * 100 : 0);
  const tight = st.disk_warning !== undefined ? !!st.disk_warning : (diskTotal > 0 && percent < 15);
  const ramMachine = Number(st.ram_machine_mb || 0);
  const ramAvail = Number(st.ram_available_mb || 0);
  const ramRunning = Number(st.ram_running_mb || 0);
  const load = Array.isArray(st.load) ? st.load : null;
  const runList = (Array.isArray(st.running) ? st.running : []).filter((r) => r && r.running);

  const warn = tight ? `<div class="note note-err">
      <h3>Die Platte wird knapp</h3>
      <p>Von ${esc(fmtBytes(diskTotal || 32212254720))} sind nur noch
        <b>${esc(st.disk_free_text || fmtBytes(diskFree))}</b> frei
        (${esc(String(Math.round(percent * 10) / 10).replace('.', ','))} %).
        Die Platte ist der Engpass dieser Maschine – eine gut erkundete Welt wiegt schnell 2 bis 5 GB,
        dazu kommen Sicherungen und Übertragungen.</p>
      <p class="mb0">Unter 3 GB freiem Platz lehnt der Server jeden Start ab. Bitte alte Übertragungen
        aufräumen, ruhende Server zurückholen lassen oder keine neuen Pässe mehr ausstellen.</p>
    </div>` : '';

  const ramPct = ramMachine ? Math.min(100, Math.round((ramMachine - ramAvail) / ramMachine * 100)) : 0;
  const diskPct = diskTotal ? Math.min(100, Math.round((diskTotal - diskFree) / diskTotal * 100)) : 0;

  const running = runList.length
    ? runList.map((r) => {
      const inst = state.instances.find((i) => i.id === r.id);
      return `<div class="wrow">
        <div><span class="w-name"><span class="dot ${r.ready ? 'dot-on' : 'dot-busy'}"></span>
          ${esc(inst ? inst.name : r.id)}</span>
          <div class="w-meta">${esc(inst ? userName(inst.owner) : '')} ·
            ${esc(gb(r.ram_mb))} eingerichtet${r.memory_mb ? ', ' + esc(gb(r.memory_mb)) + ' belegt' : ''}</div></div>
        <div class="muted small nowrap">${esc(r.uptime ? 'läuft ' + fmtUptime(r.uptime) : (r.ready ? 'bereit' : 'startet'))}</div>
      </div>`;
    }).join('')
    : '<div class="muted small">Gerade läuft kein Server.</div>';

  const candidates = Array.isArray(st.premium_delete_candidates) ? st.premium_delete_candidates : [];
  const extra = candidates.length ? `<div class="note note-warn">
      <h3>Ruhende Premium-Server</h3>
      <p class="mb0">${candidates.map((c) => esc(c.name)).join(', ')} –
        ${plural(candidates.length, 'dieser Server ruht', 'diese Server ruhen')} lange genug ohne
        gültigen Pass, ${plural(candidates.length, 'er darf', 'sie dürfen')} gelöscht werden.
        Bitte vorher mit den Besitzern sprechen.</p></div>` : '';

  const html = warn + `
    <div class="kpis">
      ${statTile('Freier Arbeitsspeicher', gb(ramAvail), 'von ' + gb(ramMachine) + ' insgesamt')}
      ${statTile('An Server vergeben', gb(ramRunning), runList.length + ' ' + plural(runList.length, 'Server läuft', 'Server laufen'))}
      ${statTile('Freie Platte', st.disk_free_text || fmtBytes(diskFree),
    String(Math.round(percent * 10) / 10).replace('.', ',') + ' % von ' + fmtBytes(diskTotal),
    tight ? 'bad' : (percent < 25 ? 'warn' : ''))}
      ${statTile('Last', load ? load.map((x) => String(Math.round(Number(x) * 100) / 100).replace('.', ',')).join(' · ') : '–',
    load ? '1, 5 und 15 Minuten (8 Kerne)' : 'wird nicht gemeldet')}
    </div>

    <div class="grid2">
      <div class="card">
        <div class="card-head"><h3>Arbeitsspeicher</h3>
          <span class="muted small">${esc(ramPct)} % benutzt</span></div>
        <div class="bar ${ramPct > 88 ? 'bad' : (ramPct > 75 ? 'warn' : '')}"><i style="width:${esc(ramPct)}%"></i></div>
        <p class="small muted mb0">Für Server sollten höchstens 10 GB zugleich vergeben werden;
          der Rest bleibt für das System und die Java-Verwaltung.</p>
      </div>
      <div class="card">
        <div class="card-head"><h3>Platte</h3>
          <span class="muted small">${esc(diskPct)} % belegt</span></div>
        <div class="bar ${tight ? 'bad' : (percent < 25 ? 'warn' : '')}"><i style="width:${esc(diskPct)}%"></i></div>
        <p class="small muted mb0">Welten, Sicherungen und Übertragungen zusammen:
          ${esc(st.size_bytes !== undefined ? fmtBytes(st.size_bytes) : '–')}.</p>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h3>Laufende Server</h3></div>
      ${running}
    </div>

    ${extra}

    <div class="card">
      <div class="card-head"><h3>Dienst</h3></div>
      <div class="grid3">
        ${statTile('Fassung', st.version ? String(st.version) : '–')}
        ${statTile('Läuft seit', st.started_at ? fmtUptime((st.time || nowSec()) - st.started_at) : '–',
    st.started_at ? fmtDate(st.started_at) : '')}
        ${statTile('Konten · Server · Pässe',
      [st.users, st.instances, st.passes].map((x) => (x === undefined ? '–' : x)).join(' · '))}
      </div>
      <p class="small muted mb0" style="margin-top:12px">Freie Ports:
        Java ${esc(st.free_ports_java !== undefined ? st.free_ports_java : '–')},
        Bedrock ${esc(st.free_ports_bedrock !== undefined ? st.free_ports_bedrock : '–')}.
        Offene Übertragungen: ${esc(Array.isArray(st.transfers) ? st.transfers.length : 0)}.</p>
    </div>`;

  paint($('#machineBox'), html, 'mach:' + JSON.stringify([st.ram_available_mb, st.ram_machine_mb,
    st.ram_running_mb, st.disk_free_bytes, st.disk_total_bytes, st.load, st.version, st.started_at,
    st.users, st.instances, st.passes, st.size_bytes, candidates.map((c) => c.id),
    runList.map((r) => [r.id, r.ready, r.memory_mb, Math.floor((r.uptime || 0) / 30)]),
    (Array.isArray(st.transfers) ? st.transfers.length : 0)]));
}

/* ------------------------------------------------------------ Ansichten */

function setView(view) {
  state.view = view;
  $$('#tabs .tab').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
  $$('.view').forEach((s) => s.classList.add('hidden'));
  const box = $('#view' + view.charAt(0).toUpperCase() + view.slice(1));
  if (box) box.classList.remove('hidden');
  render();
  tick();
}

/* ------------------------------------------------------------- Bedienung */

function bind() {
  $('#btnDiscord').onclick = loginDiscord;
  $('#btnCode').onclick = loginCode;
  $('#btnCodeToggle').onclick = () => {
    $('#codeBox').classList.toggle('hidden');
    const box = $('#inviteCode');
    if (box && !$('#codeBox').classList.contains('hidden')) box.focus();
  };
  $('#btnLogout').onclick = doLogout;
  $('#tabs').addEventListener('click', (ev) => {
    const tab = ev.target.closest('.tab');
    if (tab) setView(tab.dataset.view);
  });

  $('#btnInvite').onclick = createInvite;
  $('#btnIssue').onclick = issuePass;
  $('#pmOnlyActive').onchange = () => renderPasses();
  $('#pmUser').onchange = renderPreview;
  ['#pmDays', '#pmSlots', '#pmRam', '#pmNote'].forEach((sel) => {
    const el = $(sel);
    el.oninput = renderPreview;
  });
  $('#pmDaysQuick').addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-days]');
    if (!btn) return;
    $('#pmDays').value = btn.dataset.days;
    renderPreview();
  });
  $('#pmKind').addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-kind]');
    if (!btn) return;
    state.form.kind = btn.dataset.kind === 'premium' ? 'premium' : 'local';
    $$('#pmKind .choice').forEach((b) => b.classList.toggle('sel', b === btn));
    renderPreview();
  });

  /* Alles, was in den Listen steckt, läuft über einen gemeinsamen Zuhörer –
     so überleben die Knöpfe jedes Neuzeichnen. */
  document.addEventListener('click', async (ev) => {
    const t = ev.target.closest('[data-copy],[data-revinvite],[data-block],[data-toggle],[data-topass],'
      + '[data-extend],[data-revpass],[data-startsrv],[data-stopsrv]');
    if (!t) return;
    if (t.dataset.copy !== undefined) {
      const ok = await copyText(t.dataset.copy);
      toast(ok ? 'Der Code liegt in der Zwischenablage.' : 'Kopieren hat nicht geklappt – bitte von Hand abschreiben.', !ok);
      return;
    }
    if (t.dataset.toggle !== undefined) {
      const id = t.dataset.toggle;
      if (state.open.has(id)) state.open.delete(id); else state.open.add(id);
      renderUsers();
      return;
    }
    if (t.dataset.topass !== undefined) {
      setView('passes');
      syncUserSelect();
      $('#pmUser').value = t.dataset.topass;
      renderPreview();
      $('#pmUser').focus();
      return;
    }
    if (t.dataset.revinvite !== undefined) { revokeInvite(t.dataset.revinvite); return; }
    if (t.dataset.block !== undefined) { toggleBlock(t.dataset.block, t.dataset.on === '1'); return; }
    if (t.dataset.extend !== undefined) { extendPass(t.dataset.extend); return; }
    if (t.dataset.revpass !== undefined) { revokePass(t.dataset.revpass); return; }
    if (t.dataset.startsrv !== undefined) { startServer(t.dataset.startsrv); return; }
    if (t.dataset.stopsrv !== undefined) { stopServer(t.dataset.stopsrv); return; }
  });
}

async function toggleBlock(uid, block) {
  const user = state.users.find((u) => u.id === uid);
  const name = user ? user.name : uid;
  let reason = '';
  if (block) {
    const running = state.instances.filter((i) => i.owner === uid && i.running);
    const value = await ask({
      title: 'Konto sperren?',
      text: `<b>${esc(name)}</b> kann sich danach nicht mehr anmelden, alle Sitzungen enden.
        ${running.length ? `Laufende Server (<b>${running.map((i) => esc(i.name)).join(', ')}</b>)
          werden angekündigt heruntergefahren.` : 'Gerade läuft kein Server dieses Kontos.'}
        Die Dateien auf der Platte bleiben erhalten.`,
      ok: 'Sperren', danger: true,
      input: { label: 'Grund (wird dem Konto angezeigt)', placeholder: 'z. B. Zahlung offen' },
    });
    if (value === null) return;
    reason = value;
  } else {
    const value = await ask({
      title: 'Konto entsperren?',
      text: `<b>${esc(name)}</b> kann sich danach wieder anmelden und Server starten,
        sofern ein gültiger Pass vorliegt.`,
      ok: 'Entsperren',
    });
    if (value === null) return;
  }
  try {
    await api('/api/admin/users/' + encodeURIComponent(uid) + '/block',
      { method: 'POST', body: { blocked: block, reason } });
    toast(block ? `${name} ist gesperrt.` : `${name} ist wieder freigegeben.`);
    await tick();
  } catch (e) {
    toast(e.message, true);
  }
}

/* --------------------------------------------------- Beispielantwort */

/* Nur für die Ansicht ohne Server (Datei direkt geöffnet): erfundene Daten, damit sich
   Aussehen und Bedienung beurteilen lassen. Es wird nichts gesendet. */
function demoData() {
  const t = nowSec();
  const users = [
    {
      id: 'a1b2c3d4e5f6', name: 'Luca', role: 'admin', created_at: t - 86400 * 120, blocked: false,
      blocked_reason: '', discord_linked: true, discord_name: 'luca', last_seen: t - 240,
      limits: { max_concurrent: 3, ram_total_mb: 10240, premium: true, pass_count: 1 },
      limits_text: '3 gleichzeitig laufende Server, zusammen 10 GB',
      instances: 2, running: 1, size_bytes: 4.1 * 1073741824, sessions: 2,
      passes: [{
        id: 'p1000000aaaa', user_id: 'a1b2c3d4e5f6', kind: 'premium', kind_text: 'Premium', days: 365,
        max_concurrent: 3, ram_total_mb: 10240, ram_total_text: '10 GB', issued_at: t - 86400 * 40,
        expires_at: t + 86400 * 325, revoked_at: 0, note: 'Betreiber', state: 'active',
        remaining_seconds: 86400 * 325, remaining_text: 'läuft in 325 Tagen ab',
      }],
    },
    {
      id: 'b2c3d4e5f607', name: 'Anna', role: 'user', created_at: t - 86400 * 18, blocked: false,
      blocked_reason: '', discord_linked: true, discord_name: 'anna.k', last_seen: t - 3600 * 5,
      limits: { max_concurrent: 2, ram_total_mb: 8192, premium: false, pass_count: 1 },
      limits_text: '2 gleichzeitig laufende Server, zusammen 8 GB',
      instances: 2, running: 1, size_bytes: 3.3 * 1073741824, sessions: 1,
      passes: [{
        id: 'p2000000bbbb', user_id: 'b2c3d4e5f607', kind: 'local', kind_text: 'Lokal', days: 14,
        max_concurrent: 2, ram_total_mb: 8192, ram_total_text: '8 GB', issued_at: t - 86400 * 3,
        expires_at: t + 86400 * 11, revoked_at: 0, note: 'bezahlt bis Ende März', state: 'active',
        remaining_seconds: 86400 * 11, remaining_text: 'läuft in 11 Tagen ab',
      }],
    },
    {
      id: 'c3d4e5f60718', name: 'Tim', role: 'user', created_at: t - 86400 * 60, blocked: true,
      blocked_reason: 'Zahlung offen', discord_linked: false, discord_name: '', last_seen: t - 86400 * 9,
      limits: { max_concurrent: 0, ram_total_mb: 0, premium: false, pass_count: 0 },
      limits_text: 'kein gültiger Pass', instances: 1, running: 0, size_bytes: 1.2 * 1073741824,
      sessions: 0,
      passes: [{
        id: 'p3000000cccc', user_id: 'c3d4e5f60718', kind: 'local', kind_text: 'Lokal', days: 30,
        max_concurrent: 1, ram_total_mb: 4096, ram_total_text: '4 GB', issued_at: t - 86400 * 45,
        expires_at: t - 86400 * 15, revoked_at: 0, note: '', state: 'expired',
        remaining_seconds: 0, remaining_text: 'abgelaufen',
      }],
    },
  ];
  const instances = [
    {
      id: 'i1111111', owner: 'a1b2c3d4e5f6', name: 'Nexus-Welt', type: 'java', flavor: 'paper',
      version: '1.21.1', ram_mb: 4096, ram_text: '4 GB', ports: { java: 25565 },
      ports_live: { java: 25565 }, state: 'hosted', state_text: 'auf dem Root-Server',
      origin: 'premium', running: true, created_at: t - 86400 * 40, updated_at: t - 3600,
      started_at: t - 7200, parked_at: 0, size_bytes: 2.6 * 1073741824, size_text: '2,60 GB',
      subdomain: 'nexus.arcardia-nexus.de',
      live: { id: 'i1111111', running: true, ready: true, stopping: false, pid: 1421, uptime: 7200, ram_mb: 4096, memory_mb: 3180, exit_code: null, error: '', console_next: 812 },
    },
    {
      id: 'i2222222', owner: 'a1b2c3d4e5f6', name: 'Bauwelt', type: 'java', flavor: 'fabric',
      version: '1.20.1', ram_mb: 6144, ram_text: '6 GB', ports: { java: 25566 }, ports_live: {},
      state: 'hosted', state_text: 'auf dem Root-Server', origin: 'premium', running: false,
      created_at: t - 86400 * 30, updated_at: t - 86400, started_at: t - 86400 * 2, parked_at: 0,
      size_bytes: 1.5 * 1073741824, size_text: '1,50 GB', live: null,
    },
    {
      id: 'i3333333', owner: 'b2c3d4e5f607', name: 'Annas Insel', type: 'bedrock', flavor: 'vanilla',
      version: '1.21.30', ram_mb: 2048, ram_text: '2 GB', ports: { bedrock: 19132 },
      ports_live: { bedrock: 19132 }, state: 'hosted', state_text: 'auf dem Root-Server',
      origin: 'local', running: true, created_at: t - 86400 * 12, updated_at: t - 600,
      started_at: t - 1800, parked_at: 0, size_bytes: 1.1 * 1073741824, size_text: '1,10 GB',
      live: { id: 'i3333333', running: true, ready: true, stopping: false, pid: 1533, uptime: 1800, ram_mb: 2048, memory_mb: 1490, exit_code: null, error: '', console_next: 233 },
    },
    {
      id: 'i4444444', owner: 'b2c3d4e5f607', name: 'Abenteuer', type: 'java', flavor: 'paper',
      version: '1.21.1', ram_mb: 4096, ram_text: '4 GB', ports: { java: 25567 }, ports_live: {},
      state: 'awaiting_pull', state_text: 'wartet auf Rückholung', origin: 'local', running: false,
      created_at: t - 86400 * 20, updated_at: t - 86400 * 2, started_at: t - 86400 * 3,
      parked_at: t - 86400 * 2, size_bytes: 2.2 * 1073741824, size_text: '2,20 GB', live: null,
    },
    {
      id: 'i5555555', owner: 'c3d4e5f60718', name: 'Tims Hügel', type: 'java', flavor: 'paper',
      version: '1.20.6', ram_mb: 4096, ram_text: '4 GB', ports: { java: 25568 }, ports_live: {},
      state: 'suspended', state_text: 'ruht', origin: 'premium', running: false,
      created_at: t - 86400 * 55, updated_at: t - 86400 * 15, started_at: t - 86400 * 16,
      parked_at: t - 86400 * 15, size_bytes: 1.2 * 1073741824, size_text: '1,20 GB', live: null,
    },
  ];
  const status = {
    version: '0.9.0', time: t, started_at: t - 86400 * 3 - 5400,
    users: users.length, instances: instances.length, passes: 3,
    running: instances.filter((i) => i.running).map((i) => i.live),
    ram_running_mb: 6144, ram_machine_mb: 13312, ram_available_mb: 5860,
    disk_free_bytes: 4.1 * 1073741824, disk_free_text: '4,10 GB',
    disk_total_bytes: 30 * 1073741824, disk_free_percent: 13.7, disk_warning: true,
    load: [1.42, 1.18, 0.96],
    free_ports_java: 97, free_ports_bedrock: 99,
    premium_delete_candidates: [], transfers: [], size_bytes: 8.6 * 1073741824,
  };
  const invites = [
    {
      code: 'KRF2-TGQN-SPAP', created_by: users[0].id, created_at: t - 86400 * 2, uses_max: 1,
      uses: 0, expires_at: t + 86400 * 5, revoked_at: 0, note: 'für Anna', state: 'open',
      state_text: 'offen',
    },
    {
      code: 'MHT7-XKDR-9PQB', created_by: users[0].id, created_at: t - 86400 * 20, uses_max: 1,
      uses: 1, expires_at: t - 86400 * 13, revoked_at: 0, note: '', state: 'used',
      state_text: 'aufgebraucht',
    },
  ];
  const passes = users.flatMap((u) => u.passes);
  return { users, instances, status, invites, passes };
}

const DEMO_STORE = DEMO ? demoData() : null;

async function demoApi(path, method) {
  await new Promise((r) => setTimeout(r, 60));
  const d = DEMO_STORE;
  if (method && method !== 'GET') {
    throw new ApiError('In der Beispielansicht wird nichts wirklich geändert.', 0);
  }
  if (path.startsWith('/api/me')) {
    return {
      user: Object.assign({}, d.users[0], { discord_name: 'luca' }),
      passes: d.users[0].passes, limits: d.users[0].limits,
      limits_text: d.users[0].limits_text, server_time: nowSec(),
    };
  }
  if (path.startsWith('/api/admin/status')) return d.status;
  if (path.startsWith('/api/admin/users')) return { users: d.users };
  if (path.startsWith('/api/admin/invites')) return { invites: d.invites };
  if (path.startsWith('/api/admin/passes')) {
    return {
      passes: d.passes,
      templates: [],
      limits: { days: [1, 3650], max_concurrent: [1, 10], ram_total_mb: [1024, 10240] },
    };
  }
  if (path.startsWith('/api/admin/instances')) return { instances: d.instances };
  if (method === 'POST') {
    throw new ApiError('In der Beispielansicht wird nichts wirklich geändert.', 0);
  }
  return {};
}

/* ------------------------------------------------------------------ Start */

function boot() {
  bind();
  renderPreview();
  const fresh = tokenFromHash();
  if (fresh) writeToken(fresh);
  else state.token = readToken();
  setInterval(tick, REFRESH_MS);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
  if (DEMO) { state.token = 'beispiel'; start(); return; }
  if (state.token) start();
  else showLogin();
}

boot();
