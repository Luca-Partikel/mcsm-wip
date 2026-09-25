/* Minecraft Server Manager – Verwaltung der gehosteten Server.
   Kein Framework, kein Bauschritt: eine Datei, die im Browser direkt läuft.
   Die Seite spricht ausschließlich über die API aus hosted/ARCHITEKTUR.md; welche Routen und
   Felder erwartet werden, steht in hosted/spec-admin.md.

   Anmeldung: Es gibt genau einen Weg herein – „Mit Discord anmelden“. Kennt der Server das
   Discord-Konto noch nicht, schickt er einen Merkzettel zurück; dann fragt die Seite einmalig
   den Einladungscode ab (Schritt 2) und richtet damit das Konto ein. */
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
/* So lange merkt sich der Server ein angefangenes Discord-Konto ohne Einladungscode
   (oauth.REGISTRIERUNG_TTL_SECONDS). Hier nur für die Anzeige „gilt noch …“. */
const REG_TTL_SECONDS = 900;
const VIEWS = ['uebersicht', 'users', 'invites', 'passes', 'servers', 'machine'];

const state = {
  token: '',
  me: null,
  view: 'uebersicht',
  status: null,
  users: [],
  invites: [],
  invitesGeladen: false,
  passes: [],
  instances: [],
  passLimits: { days: [1, 3650], max_concurrent: [1, 10], ram_total_mb: [1024, 10240] },
  open: new Set(),          // aufgeklappte Benutzerzeilen
  form: { kind: 'local' },
  filter: { users: 'alle', usersQ: '', servers: 'alle', serversQ: '' },
  reg: { zettel: '', name: '', ende: 0 },
  erstzugang: false,
  discordBereit: true,
  busy: false,
  lastError: '',
  freeze: 0,                // solange > 0: keine Auffrischung (Rückfrage offen)
  srvClose: null,           // schließt das offene Serverfenster
};

const nowSec = () => Math.floor(Date.now() / 1000);

const fmtDate = (ts) => ts
  ? new Date(ts * 1000).toLocaleString('de-DE', { dateStyle: 'medium', timeStyle: 'short' })
  : '–';
const fmtDay = (ts) => ts
  ? new Date(ts * 1000).toLocaleDateString('de-DE', { dateStyle: 'long' })
  : '–';

/* Größen deutsch: Komma statt Punkt, wie im Programm auf dem PC. */
function fmtBytes(n) {
  const v = Number(n) || 0;
  if (v < 1024) return v + ' B';
  if (v < 1048576) return komma((v / 1024).toFixed(1)) + ' KB';
  if (v < 1073741824) return komma((v / 1048576).toFixed(1)) + ' MB';
  return komma((v / 1073741824).toFixed(v < 10737418240 ? 2 : 1)) + ' GB';
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
const komma = (n) => String(n).replace('.', ',');
const clamp = (n, min, max) => Math.max(min, Math.min(max, n));

function fmtUptime(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return d + ' ' + plural(d, 'Tag', 'Tage') + ' ' + h + ' h';
  if (h) return h + ' h ' + m + ' min';
  return m + ' min';
}

/* mm:ss für den Rückwärtszähler im zweiten Anmeldeschritt. */
function fmtMinSek(sec) {
  const s = Math.max(0, Math.floor(sec));
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
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

/* Jede Rückfrage ist ein eigener Kasten im Stil der Seite – nie das graue Fenster des
   Browsers. Ergebnis: null bei Abbruch, sonst ein Objekt mit den Feldwerten (leeres Objekt,
   wenn keine Felder gewünscht waren). Solange die Rückfrage offen ist, wird nicht aufgefrischt. */
function ask({ title, text, ok = 'Ja', danger = false, fields = [] }) {
  return new Promise((resolve) => {
    state.freeze += 1;
    const host = $('#modalHost');
    const felder = fields.map((f, i) => `
      <div class="field">
        <label for="askFeld${i}">${esc(f.label || '')}</label>
        <input type="${f.type === 'number' ? 'number' : 'text'}" id="askFeld${i}" data-f="${esc(f.name)}"
               value="${esc(f.value === undefined ? '' : f.value)}"
               placeholder="${esc(f.placeholder || '')}"
               ${f.min !== undefined ? 'min="' + esc(f.min) + '"' : ''}
               ${f.max !== undefined ? 'max="' + esc(f.max) + '"' : ''}
               ${f.maxlength !== undefined ? 'maxlength="' + esc(f.maxlength) + '"' : ''}>
        ${f.hint ? `<div class="hint">${esc(f.hint)}</div>` : ''}
      </div>`).join('');
    host.innerHTML = `
      <div class="modal-back" id="askBack">
        <div class="modal" role="dialog" aria-modal="true" aria-labelledby="askTitel">
          <h3 id="askTitel">${esc(title)}</h3>
          <div class="small">${text}</div>
          ${felder}
          <div class="btn-row">
            <button class="btn" id="askNo">Abbrechen</button>
            <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" id="askYes">${esc(ok)}</button>
          </div>
        </div>
      </div>`;
    const done = (value) => {
      host.innerHTML = '';
      state.freeze = Math.max(0, state.freeze - 1);
      document.removeEventListener('keydown', onKey, true);
      resolve(value);
    };
    const onKey = (ev) => {
      if (ev.key === 'Escape') { ev.preventDefault(); done(null); return; }
      if (ev.key === 'Enter' && ev.target && ev.target.dataset && ev.target.dataset.f !== undefined) {
        ev.preventDefault();
        $('#askYes').click();
        return;
      }
      if (ev.key === 'Tab') fangeFokus(ev, $('#askBack'));
    };
    document.addEventListener('keydown', onKey, true);
    $('#askNo').onclick = () => done(null);
    $('#askBack').onclick = (ev) => { if (ev.target.id === 'askBack') done(null); };
    $('#askYes').onclick = () => {
      const out = {};
      $$('[data-f]', host).forEach((el) => { out[el.dataset.f] = String(el.value || ''); });
      done(out);
    };
    const erstes = $('[data-f]', host);
    if (erstes) erstes.focus(); else $('#askYes').focus();
  });
}

/* Die Tabulatortaste soll im offenen Kasten bleiben – sonst wandert der Fokus unsichtbar
   in die Seite dahinter. */
function fangeFokus(ev, box) {
  if (!box) return;
  const ziele = $$('button, [href], input, select, textarea', box)
    .filter((el) => !el.disabled && el.offsetParent !== null);
  if (!ziele.length) return;
  const erste = ziele[0], letzte = ziele[ziele.length - 1];
  if (ev.shiftKey && document.activeElement === erste) { ev.preventDefault(); letzte.focus(); }
  else if (!ev.shiftKey && document.activeElement === letzte) { ev.preventDefault(); erste.focus(); }
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

/* Die Rückleitung von Discord hängt ihr Ergebnis an die Raute – sie geht nie an den Server:
     #token=…                       fertig angemeldet
     #registrierung=…&name=…        Discord kennt uns, der Server das Konto noch nicht
     #verknuepft=1                  ein bestehendes Konto hat jetzt Discord dabei
     #fehler=…                      verständlicher Satz vom Server */
function hashLesen() {
  const roh = String(location.hash || '').replace(/^#/, '');
  const out = { token: '', fehler: '', zettel: '', name: '', verknuepft: false };
  if (!roh) return out;
  const q = new URLSearchParams(roh);
  out.fehler = q.get('fehler') || q.get('error') || '';
  out.verknuepft = q.get('verknuepft') === '1';
  out.name = String(q.get('name') || '').slice(0, 64);
  const zettel = String(q.get('registrierung') || '');
  if (/^[A-Za-z0-9._~-]{16,256}$/.test(zettel)) out.zettel = zettel;
  const token = String(q.get('token') || '');
  if (/^[A-Za-z0-9._-]{16,512}$/.test(token)) out.token = token;
  history.replaceState(null, '', location.pathname + location.search);
  return out;
}

function showLoginNote(text, err = false) {
  const box = $('#loginNote');
  if (!text) { box.innerHTML = ''; return; }
  box.innerHTML = `<div class="note ${err ? 'note-err' : 'note-info'} small">${esc(text)}</div>`;
}

function showLogin(note = '', err = false) {
  $('#shell').classList.add('hidden');
  $('#login').classList.remove('hidden');
  $('#regBox').classList.add('hidden');
  $('#loginStep1').classList.remove('hidden');
  showLoginNote(note, err);
  serverPruefen();
}

/* Was kann dieser Server gerade? Ist Discord eingerichtet, und gibt es überhaupt schon einen
   Betreiber? Beides steht in /api/health und entscheidet, was auf der Anmeldeseite steht. */
async function serverPruefen() {
  let daten;
  try {
    daten = await api('/api/health');
  } catch (e) { return; }
  if (!daten || typeof daten !== 'object') return;
  state.erstzugang = !!daten.erstzugang;
  state.discordBereit = daten.discord !== false;
  const hinweis = $('#loginHint');
  if (!state.discordBereit) {
    $('#btnDiscord').disabled = true;
    hinweis.textContent = 'Die Anmeldung über Discord ist auf diesem Server noch nicht '
      + 'eingerichtet. Bitte die Zugangsdaten in daemon.json eintragen und den Dienst neu starten.';
    return;
  }
  $('#btnDiscord').disabled = false;
  if (state.erstzugang) {
    hinweis.textContent = 'Dieser Server hat noch keinen Betreiber. Melde dich mit Discord an – '
      + 'im zweiten Schritt wird dann der Code aus der Datei ERSTER-ADMIN.txt gebraucht.';
    $('#loginTip').textContent = 'Der Code liegt im Datenordner des Root-Servers und ist nur für '
      + 'root und den Dienstbenutzer lesbar.';
  }
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
    const data = await api('/api/auth/discord/start?ziel=' + encodeURIComponent(ziel));
    const url = String(data.url || '');
    if (!/^https:\/\/(discord\.com|discordapp\.com)\//.test(url)) {
      throw new ApiError('Die Anmeldung über Discord ist auf diesem Server nicht richtig eingerichtet.', 0);
    }
    try { sessionStorage.setItem('mcsm_admin_state', String(data.state || '')); } catch (e) { /* egal */ }
    location.href = url;
  } catch (e) {
    showLoginNote(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

/* Discord nachträglich an das angemeldete Konto hängen (kein neuer Login, keine neue Sitzung). */
async function linkDiscord() {
  const btn = $('#btnLink');
  if (btn) btn.disabled = true;
  try {
    const ziel = location.origin + location.pathname;
    const data = await api('/api/auth/discord/start?link=1&ziel=' + encodeURIComponent(ziel));
    const url = String(data.url || '');
    if (!/^https:\/\/(discord\.com|discordapp\.com)\//.test(url)) {
      throw new ApiError('Discord ist auf diesem Server nicht richtig eingerichtet.', 0);
    }
    location.href = url;
    return true;
  } catch (e) {
    toast(e.message, true);
    return false;
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ------------------------------------------------- Schritt 2: Einladungscode */

let regTimer = 0;

function showRegister(zettel, name) {
  state.reg = { zettel, name: String(name || ''), ende: nowSec() + REG_TTL_SECONDS };
  $('#shell').classList.add('hidden');
  $('#login').classList.remove('hidden');
  $('#loginStep1').classList.add('hidden');
  $('#regBox').classList.remove('hidden');
  showLoginNote('');
  $('#regHello').textContent = state.reg.name
    ? 'Hallo ' + state.reg.name + ' – fast geschafft'
    : 'Fast geschafft';
  $('#regLead').textContent = 'Die Anmeldung bei Discord hat geklappt. Bitte einmal deinen '
    + 'Einladungscode eintragen – danach reicht für immer „Mit Discord anmelden“.';
  $('#regCode').value = '';
  regErstzugangPruefen();
  regTicken();
  if (regTimer) clearInterval(regTimer);
  regTimer = setInterval(regTicken, 1000);
  $('#regCode').focus();
}

/* Auf einer frischen Anlage entsteht das allererste Betreiberkonto nur mit dem Code aus
   ERSTER-ADMIN.txt. Das gehört genau hierhin gesagt – sonst steht der Betreiber vor einer
   Tür ohne Klinke. */
async function regErstzugangPruefen() {
  const box = $('#regFirst');
  box.classList.add('hidden');
  let daten;
  try { daten = await api('/api/health'); } catch (e) { return; }
  state.erstzugang = !!(daten && daten.erstzugang);
  if (!state.erstzugang) return;
  box.innerHTML = '<b>Erstes Betreiberkonto</b><br>Dieser Server hat noch keinen Betreiber. '
    + 'Dafür gilt <b>nur</b> der Code aus der Datei <span class="mono">ERSTER-ADMIN.txt</span> – '
    + 'sie liegt im Datenordner des Root-Servers und ist nur für root und den Dienstbenutzer '
    + 'lesbar. Mit diesem Code wird dieses Konto zum Betreiberkonto.';
  box.classList.remove('hidden');
}

function regTicken() {
  const rest = state.reg.ende - nowSec();
  const feld = $('#regTimer');
  if (rest <= 0) {
    if (regTimer) { clearInterval(regTimer); regTimer = 0; }
    feld.textContent = '';
    showLogin('Die Vormerkung ist abgelaufen. Bitte noch einmal auf „Mit Discord anmelden“ klicken – '
      + 'es dauert nur einen Augenblick.', true);
    return;
  }
  feld.textContent = 'Noch ' + fmtMinSek(rest) + ' Minuten Zeit.';
}

function regAbbrechen() {
  if (regTimer) { clearInterval(regTimer); regTimer = 0; }
  state.reg = { zettel: '', name: '', ende: 0 };
  showLogin('Du kannst jederzeit neu beginnen.');
}

/* Tippen soll leicht sein: Kleinbuchstaben werden groß, Striche setzt die Seite. */
function codeFormatieren(roh) {
  const rein = String(roh || '').toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 12);
  return rein.replace(/(.{4})(?=.)/g, '$1-');
}

async function registrieren() {
  const btn = $('#btnReg');
  const code = String($('#regCode').value || '').trim();
  if (!state.reg.zettel) { regAbbrechen(); return; }
  if (code.replace(/[^A-Za-z0-9]/g, '').length !== 12) {
    showLoginNote('Ein Einladungscode hat zwölf Zeichen, zum Beispiel ABCD-EFGH-IJKL.', true);
    $('#regCode').focus();
    return;
  }
  btn.disabled = true;
  try {
    const data = await api('/api/auth/discord/register', {
      method: 'POST',
      body: { zettel: state.reg.zettel, code, note: 'Verwaltung im Browser' },
    });
    if (!data.token) throw new ApiError('Der Server hat kein Sitzungstoken geschickt.', 0);
    if (regTimer) { clearInterval(regTimer); regTimer = 0; }
    writeToken(String(data.token));
    state.reg = { zettel: '', name: '', ende: 0 };
    showLoginNote('');
    $('#regBox').classList.add('hidden');
    $('#loginStep1').classList.remove('hidden');
    await start();
  } catch (e) {
    // Ein abgelaufener oder verbrauchter Merkzettel ist kein Tippfehler – dann zurück an den Anfang.
    if (e.status === 403 || e.status === 410 || /abgelaufen/i.test(e.message)) {
      if (regTimer) { clearInterval(regTimer); regTimer = 0; }
      state.reg = { zettel: '', name: '', ende: 0 };
      showLogin(e.message, true);
      return;
    }
    showLoginNote(e.message, true);
    $('#regCode').focus();
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
    // Die Einladungscodes zeigt auch die Übersicht (Zähler am Reiter).
    if (state.view === 'invites' || state.view === 'uebersicht') jobs.push(api('/api/admin/invites'));
    if (state.view === 'passes') jobs.push(api('/api/admin/passes'));
    /* Die Instanzen braucht fast jede Ansicht – auch die Rückfrage beim Widerruf eines Passes
       nennt die Server, die dann heruntergefahren werden. */
    if (state.view !== 'invites') jobs.push(api('/api/admin/instances'));
    const res = await Promise.all(jobs);
    state.status = res[0] || {};
    state.users = (res[1] || {}).users || [];
    for (const part of res.slice(2)) {
      if (Array.isArray(part.invites)) { state.invites = part.invites; state.invitesGeladen = true; }
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

function setText(sel, text) {
  const el = $(sel);
  if (el) el.textContent = String(text);
}

function renderMe() {
  const user = (state.me && state.me.user) || {};
  const name = String(user.discord_name || user.name || '');
  setText('#meName', name || '–');
  setText('#meRole', user.role === 'admin' ? 'Betreiber' : 'Benutzer');
  const link = $('#btnLink');
  if (link) {
    // Ein Konto ohne Discord kann es hier nachtragen – danach reicht der Discord-Knopf.
    const verknuepft = !!(user.discord_linked || user.discord_name || user.discord_id);
    link.classList.toggle('hidden', verknuepft);
  }
  const av = $('#meAvatar');
  const url = String(user.avatar_url || user.discord_avatar || '');
  if (/^https:\/\/cdn\.discordapp\.com\//.test(url)) {
    av.innerHTML = `<img src="${esc(url)}" alt="" width="32" height="32">`;
  } else {
    av.textContent = (name.trim()[0] || '?').toUpperCase();
  }
}

function render() {
  renderMe();
  renderHeadLive();
  renderCounts();
  if (state.view === 'uebersicht') renderUebersicht();
  if (state.view === 'users') renderUsers();
  if (state.view === 'invites') renderInvites();
  if (state.view === 'passes') renderPasses();
  if (state.view === 'servers') renderServers();
  if (state.view === 'machine') renderMachine();
}

function renderHeadLive() {
  const st = state.status || {};
  const running = Array.isArray(st.running) ? st.running.filter((r) => r && r.running).length : 0;
  const knapp = !!st.disk_warning;
  const platte = st.disk_free_text || fmtBytes(st.disk_free_bytes);
  const html = [
    `<span class="chip"><span class="dot ${running ? 'dot-on' : ''}"></span>${esc(running)} ${plural(running, 'Server läuft', 'Server laufen')}</span>`,
    `<span class="chip">${esc(gb(st.ram_running_mb))} vergeben</span>`,
    `<span class="chip ${knapp ? 'chip-bad' : ''}">${esc(platte)} Platte frei</span>`,
  ].join('');
  paint($('#headLive'), html, 'live:' + running + ':' + st.ram_running_mb + ':' + platte + ':' + knapp);
}

function renderCounts() {
  const st = state.status || {};
  setText('#cntUsers', state.users.length);
  setText('#cntServers', state.instances.length || (st.instances || 0));
  setText('#cntPasses', st.passes !== undefined ? st.passes : state.passes.length);
  const inv = $('#cntInvites');
  if (inv) {
    inv.classList.toggle('hidden', !state.invitesGeladen);
    inv.textContent = String(state.invites.filter((v) => v.state === 'open').length);
  }
}

const userName = (uid) => {
  const u = state.users.find((x) => x.id === uid);
  return u ? String(u.name || uid) : String(uid || '');
};

const statePill = (inst) => {
  const s = String(inst.state || '');
  const text = String(inst.state_text || s);
  if (inst.running) return `<span class="pill pill-green pill-dot"><span class="dot dot-on"></span>läuft</span>`;
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

/* Eine Kachel. `bar` ist optional: {pct, cls}. */
function statTile(k, v, d, cls = '', bar = null) {
  const balken = bar
    ? `<div class="bar bar-sm ${bar.cls || ''}"><i style="width:${clamp(Math.round(bar.pct), 0, 100)}%"></i></div>`
    : '';
  return `<div class="stat ${cls}"><div class="k">${esc(k)}</div>
    <div class="v">${esc(v)}</div>${d ? `<div class="d">${esc(d)}</div>` : ''}${balken}</div>`;
}

function leer(titel, text, ziel = '', knopf = '') {
  return `<div class="empty">
    <div class="empty-ico"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h10"/></svg></div>
    <b>${esc(titel)}</b><div class="small">${esc(text)}</div>
    ${ziel ? `<div class="btn-row" style="justify-content:center;margin-top:14px">
      <button class="btn btn-sm" data-goto="${esc(ziel)}">${esc(knopf)}</button></div>` : ''}
  </div>`;
}

function emptyRow(cols, title, text) {
  return `<tr><td colspan="${cols}">${leer(title, text)}</td></tr>`;
}

/* ---------------------------------------------------------- Übersicht */

function ramGrenzen() {
  const st = state.status || {};
  return {
    machine: Number(st.ram_machine_mb || 0),
    frei: Number(st.ram_available_mb || 0),
    laufend: Number(st.ram_running_mb || 0),
  };
}

function plattenGrenzen() {
  const st = state.status || {};
  const gesamt = Number(st.disk_total_bytes || 0);
  const frei = Number(st.disk_free_bytes || 0);
  const prozentFrei = st.disk_free_percent !== undefined
    ? Number(st.disk_free_percent)
    : (gesamt ? frei / gesamt * 100 : 0);
  return {
    gesamt, frei, prozentFrei,
    knapp: st.disk_warning !== undefined ? !!st.disk_warning : (gesamt > 0 && prozentFrei < 15),
  };
}

/* Alle Pässe, die an den Konten hängen (die Liste /api/admin/passes wird nur im Reiter geladen). */
function allePaesse() {
  return state.users.flatMap((u) => (u.passes || []).map((p) =>
    Object.assign({}, p, { user_id: p.user_id || u.id })));
}

function renderUebersicht() {
  const st = state.status || {};
  const ram = ramGrenzen();
  const platte = plattenGrenzen();
  const laufend = state.instances.filter((i) => i.running);
  const paesse = allePaesse();
  const gueltig = paesse.filter((p) => p.state === 'active');
  const mitPass = state.users.filter((u) => ((u.limits || {}).max_concurrent || 0) > 0).length;
  const ramPct = ram.machine ? (ram.machine - ram.frei) / ram.machine * 100 : 0;
  const belegtPct = platte.gesamt ? (platte.gesamt - platte.frei) / platte.gesamt * 100 : 0;

  paint($('#ovKpis'), [
    statTile('Laufende Server', String(laufend.length),
      'von ' + state.instances.length + ' ' + plural(state.instances.length, 'angelegten', 'angelegten'),
      laufend.length ? 'good' : ''),
    statTile('Vergeben an Server', gb(st.ram_running_mb), 'Arbeitsspeicher aus laufenden Pässen',
      '', { pct: ram.machine ? Number(st.ram_running_mb || 0) / ram.machine * 100 : 0 }),
    statTile('Platte frei', platte.frei ? (st.disk_free_text || fmtBytes(platte.frei)) : '–',
      komma(Math.round(platte.prozentFrei * 10) / 10) + ' % von ' + fmtBytes(platte.gesamt),
      platte.knapp ? 'bad' : (platte.prozentFrei < 25 ? 'warn' : ''),
      { pct: belegtPct, cls: platte.knapp ? 'bad' : (platte.prozentFrei < 25 ? 'warn' : '') }),
    statTile('Konten', String(state.users.length),
      mitPass + ' ' + plural(mitPass, 'mit gültigem Pass', 'mit gültigem Pass')),
    statTile('Gültige Pässe', String(gueltig.length),
      paesse.length ? 'von ' + paesse.length + ' ausgestellten' : 'noch keiner ausgestellt'),
  ].join(''), 'ov:' + [laufend.length, state.instances.length, st.ram_running_mb, platte.frei,
    platte.gesamt, state.users.length, mitPass, gueltig.length, paesse.length].join(':'));

  /* --- Was sollte der Betreiber sich ansehen? --- */
  const punkte = [];
  if (platte.knapp) {
    punkte.push(merk('red', 'Die Platte wird knapp',
      'Nur noch ' + (st.disk_free_text || fmtBytes(platte.frei)) + ' frei. Unter 3 GB lehnt der '
      + 'Server jeden Start ab.', 'machine', 'Zur Maschine'));
  }
  const wartend = state.instances.filter((i) => i.state === 'awaiting_pull');
  if (wartend.length) {
    punkte.push(merk('amber', wartend.length + ' ' + plural(wartend.length, 'Server wartet', 'Server warten') + ' auf Rückholung',
      wartend.map((i) => i.name + ' (' + userName(i.owner) + ')').join(', ') + ' – die Besitzer '
      + 'holen die Dateien auf ihren PC, erst danach wird hier Platz frei.', 'servers', 'Zu den Servern'));
  }
  const ruhend = state.instances.filter((i) => i.state === 'suspended');
  if (ruhend.length) {
    punkte.push(merk('amber', ruhend.length + ' ' + plural(ruhend.length, 'Premium-Server ruht', 'Premium-Server ruhen'),
      ruhend.map((i) => i.name).join(', ') + ' – ohne gültigen Pass bleiben sie liegen, starten '
      + 'aber nicht mehr.', 'servers', 'Zu den Servern'));
  }
  const bald = gueltig.filter((p) => Number(p.expires_at || 0) - nowSec() < 86400 * 3);
  if (bald.length) {
    punkte.push(merk('amber', bald.length + ' ' + plural(bald.length, 'Pass läuft', 'Pässe laufen') + ' bald ab',
      bald.map((p) => userName(p.user_id) + ' (' + (p.remaining_text || '') + ')').join(', ')
      + ' – Verlängern geht mit einem Klick.', 'passes', 'Zu den Pässen'));
  }
  const gesperrt = state.users.filter((u) => u.blocked);
  if (gesperrt.length) {
    punkte.push(merk('blue', gesperrt.length + ' ' + plural(gesperrt.length, 'Konto ist gesperrt', 'Konten sind gesperrt'),
      gesperrt.map((u) => u.name + (u.blocked_reason ? ' – ' + u.blocked_reason : '')).join(', '),
      'users', 'Zu den Konten'));
  }
  const kandidaten = Array.isArray(st.premium_delete_candidates) ? st.premium_delete_candidates : [];
  if (kandidaten.length) {
    punkte.push(merk('amber', 'Ruhende Premium-Server dürfen gelöscht werden',
      kandidaten.map((c) => c.name).join(', ') + ' – bitte vorher mit den Besitzern sprechen.',
      'machine', 'Zur Maschine'));
  }
  const offen = Array.isArray(st.transfers) ? st.transfers : [];
  if (offen.length) {
    punkte.push(merk('blue', offen.length + ' ' + plural(offen.length, 'offene Übertragung', 'offene Übertragungen'),
      'Angefangene Uploads belegen Platz, bis sie fertig sind oder aufgeräumt werden.',
      'machine', 'Zur Maschine'));
  }
  const ohnePass = state.users.filter((u) => !u.blocked && !((u.limits || {}).max_concurrent || 0));
  if (ohnePass.length) {
    punkte.push(merk('blue', ohnePass.length + ' ' + plural(ohnePass.length, 'Konto hat', 'Konten haben') + ' keinen gültigen Pass',
      ohnePass.map((u) => u.name).join(', ') + ' – ohne Pass lässt sich kein Server einschalten.',
      'passes', 'Pass ausstellen'));
  }

  paint($('#ovAufgaben'), punkte.length
    ? `<div class="aufgaben">${punkte.join('')}</div>`
    : leer('Alles ruhig', 'Nichts, was jetzt erledigt werden müsste.'),
    'ovaufg:' + punkte.length + ':' + punkte.join('').length);
  setText('#ovAufgabenSum', punkte.length
    ? punkte.length + ' ' + plural(punkte.length, 'Punkt', 'Punkte')
    : 'nichts offen');

  /* --- Was läuft gerade? --- */
  const runHtml = laufend.length
    ? laufend.map((i) => {
      const live = i.live || {};
      return `<div class="wrow">
        <div><span class="w-name"><span class="dot ${live.ready === false ? 'dot-busy' : 'dot-on'}"></span>${esc(i.name)}</span>
          <div class="w-meta">${esc(userName(i.owner))} · ${esc(gb(live.memory_mb || i.ram_mb))} von ${esc(gb(i.ram_mb))}
            ${live.uptime ? '· läuft ' + esc(fmtUptime(live.uptime)) : ''}</div></div>
        <div class="btn-row">
          <button class="btn btn-sm" data-srv="${esc(i.id)}">Ansehen</button>
          <button class="btn btn-sm btn-danger" data-stopsrv="${esc(i.id)}">Stoppen</button>
        </div></div>`;
    }).join('')
    : leer('Gerade läuft kein Server', 'Sobald jemand einen Server einschaltet, steht er hier.',
      'servers', 'Zu den Servern');
  paint($('#ovRunning'), runHtml, 'ovrun:' + JSON.stringify(laufend.map((i) => [i.id, i.name,
    (i.live || {}).memory_mb, Math.floor(((i.live || {}).uptime || 0) / 30), (i.live || {}).ready])));
  setText('#ovRunSum', laufend.length ? gb(st.ram_running_mb) + ' vergeben' : '');

  /* --- Auslastung --- */
  const last = Array.isArray(st.load) ? st.load : null;
  const lastWert = last ? Number(last[0]) : 0;
  paint($('#ovMeters'), [
    meter('Arbeitsspeicher', Math.round(ramPct) + ' %',
      ramPct, gb(ram.machine - ram.frei) + ' von ' + gb(ram.machine) + ' belegt',
      ramPct > 88 ? 'bad' : (ramPct > 75 ? 'warn' : '')),
    meter('Platte', Math.round(belegtPct) + ' %', belegtPct,
      fmtBytes(platte.gesamt - platte.frei) + ' von ' + fmtBytes(platte.gesamt) + ' belegt',
      platte.knapp ? 'bad' : (platte.prozentFrei < 25 ? 'warn' : '')),
    meter('Last (1 Minute)', last ? komma(Math.round(lastWert * 100) / 100) : '–',
      last ? lastWert / 8 * 100 : 0,
      last ? 'Acht Kerne; ab 8,0 ist die Maschine ausgelastet.' : 'Die Last wird nicht gemeldet.',
      lastWert > 7 ? 'bad' : (lastWert > 5 ? 'warn' : '')),
  ].join(''), 'ovmet:' + [Math.round(ramPct), Math.round(belegtPct), lastWert, ram.machine, platte.gesamt].join(':'));
}

function merk(farbe, titel, text, ziel, knopf) {
  return `<div class="aufgabe aufgabe-${farbe}">
    <div class="aufgabe-text"><b>${esc(titel)}</b><span class="muted">${esc(text)}</span></div>
    ${ziel ? `<button class="btn btn-sm" data-goto="${esc(ziel)}">${esc(knopf)}</button>` : ''}
  </div>`;
}

function meter(name, wert, pct, fuss, cls = '') {
  return `<div class="meter">
    <div class="meter-head"><span class="meter-name">${esc(name)}</span><span class="meter-val">${esc(wert)}</span></div>
    <div class="bar ${cls}"><i style="width:${clamp(Math.round(pct), 0, 100)}%"></i></div>
    <div class="meter-foot">${esc(fuss)}</div>
  </div>`;
}

/* ------------------------------------------------------------ Benutzer */

function passtZuSuche(text, frage) {
  if (!frage) return true;
  return String(text || '').toLowerCase().includes(frage.toLowerCase());
}

function renderUsers() {
  const st = state.status || {};
  const withPass = state.users.filter((u) => ((u.limits || {}).max_concurrent || 0) > 0).length;
  const blocked = state.users.filter((u) => u.blocked).length;
  paint($('#usersKpis'), [
    statTile('Benutzer', String(state.users.length), blocked ? blocked + ' gesperrt' : 'keine Sperren',
      blocked ? 'warn' : ''),
    statTile('Mit gültigem Pass', String(withPass),
      withPass === state.users.length ? 'alle' : 'von ' + state.users.length),
    statTile('Server insgesamt', String(st.instances !== undefined ? st.instances : state.instances.length),
      state.instances.filter((i) => i.running).length + ' laufen gerade'),
    statTile('Belegter Platz', st.size_bytes !== undefined ? fmtBytes(st.size_bytes) : '–', 'auf der Platte'),
  ].join(''), 'kpi:' + [state.users.length, blocked, withPass, st.size_bytes, st.instances,
    state.instances.filter((i) => i.running).length].join(':'));

  const frage = state.filter.usersQ;
  const liste = state.users.filter((u) => {
    if (state.filter.users === 'pass' && !((u.limits || {}).max_concurrent || 0)) return false;
    if (state.filter.users === 'gesperrt' && !u.blocked) return false;
    return passtZuSuche(u.name, frage) || passtZuSuche(u.discord_name, frage);
  });

  const ich = String(((state.me || {}).user || {}).id || '');
  const rows = liste.map((u) => {
    const open = state.open.has(u.id);
    const selbst = u.id === ich;          // am eigenen Konto gibt es kein „Recht abgeben“
    const limits = u.limits || {};
    const discord = u.discord_name
      ? esc(u.discord_name)
      : (u.discord_linked ? '<span class="muted">verknüpft</span>' : '<span class="muted">nicht verknüpft</span>');
    const zustand = u.blocked
      ? `<span class="pill pill-red">gesperrt</span>${u.blocked_reason ? `<div class="t-sub">${esc(u.blocked_reason)}</div>` : ''}`
      : `<span class="pill pill-green">aktiv</span>`;
    const eigene = state.instances.filter((i) => i.owner === u.id);
    const laufend = eigene.filter((i) => i.running).length;
    const head = `
      <tr class="clickable ${open ? 'open' : ''}" data-toggle="${esc(u.id)}">
        <td><span class="caret">›</span></td>
        <td><div class="t-main">${esc(u.name)}</div>
          <div class="t-sub">seit ${esc(fmtDay(u.created_at))}</div></td>
        <td>${discord}</td>
        <td>${u.role === 'admin' ? '<span class="pill pill-blue">Betreiber</span>' : 'Benutzer'}</td>
        <td>${limits.max_concurrent
          ? esc(limits.max_concurrent) + ' gleichzeitig<div class="t-sub">zusammen ' + esc(gb(limits.ram_total_mb)) + '</div>'
          : '<span class="muted">kein gültiger Pass</span>'}</td>
        <td class="nowrap">${esc(eigene.length)}${laufend ? `<div class="t-sub">${esc(laufend)} ${plural(laufend, 'läuft', 'laufen')}</div>` : ''}</td>
        <td class="nowrap">${esc(u.last_seen ? fmtDate(u.last_seen) : '–')}</td>
        <td>${zustand}</td>
        <td class="nowrap"><div class="btn-row">
          <button class="btn btn-sm ${u.blocked ? '' : 'btn-danger'}" data-block="${esc(u.id)}"
                  data-on="${u.blocked ? '0' : '1'}">${u.blocked ? 'Entsperren' : 'Sperren'}</button>
        </div></td>
      </tr>`;
    if (!open) return head;
    const servers = eigene.length
      ? eigene.map((i) => `<div class="wrow">
          <div><span class="w-name">${esc(i.name)}</span>
            <div class="w-meta">${esc(i.state_text || i.state)} · ${esc(gb(i.ram_mb))} · ${esc(i.size_text || fmtBytes(i.size_bytes))}</div></div>
          <div class="btn-row"><span>${statePill(i)}</span>
            <button class="btn btn-sm" data-srv="${esc(i.id)}">Ansehen</button></div></div>`).join('')
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
      <tr class="open"><td class="sub" colspan="9">
        <div class="sub-box">
          <div><h4>Server</h4>${servers}</div>
          <div><h4>Pässe</h4>${passRows}</div>
          <div class="sub-wide"><h4>Was du hier tun kannst</h4>
            <div class="btn-row">
              <button class="btn btn-sm btn-primary" data-topass="${esc(u.id)}">Pass ausstellen</button>
              <button class="btn btn-sm" data-rename="${esc(u.id)}">Umbenennen</button>
              ${selbst ? '' : `<button class="btn btn-sm" data-role="${esc(u.id)}"
                      data-to="${u.role === 'admin' ? 'user' : 'admin'}">${u.role === 'admin' ? 'Betreiberrecht abgeben' : 'Zum Betreiber machen'}</button>`}
            </div>
            <div class="muted small" style="margin-top:8px">Sitzungen: ${esc(u.sessions === undefined ? '–' : u.sessions)} ·
              Belegter Platz: ${esc(fmtBytes(u.size_bytes))}</div>
          </div>
        </div>
      </td></tr>`;
  }).join('');

  paint($('#usersBody'), rows || (state.users.length
    ? emptyRow(9, 'Kein Treffer', 'Zu dieser Suche gibt es kein Konto.')
    : emptyRow(9, 'Noch keine Benutzer', 'Erzeuge unter „Einladungscodes“ einen Code.')),
    'users:' + JSON.stringify(liste.map((u) => [u.id, u.name, u.role, u.blocked, u.blocked_reason,
      u.last_seen, u.discord_name, u.limits_text, u.sessions, u.size_bytes, (u.passes || []).length])) +
    ':' + Array.from(state.open).sort().join(',') +
    ':' + JSON.stringify(state.instances.map((i) => [i.id, i.owner, i.state, i.running, i.size_bytes])));

  setText('#usersSum', liste.length === state.users.length
    ? 'Eine Zeile anklicken, um Server und Pässe zu sehen.'
    : liste.length + ' von ' + state.users.length + ' Konten');
}

/* ---------------------------------------------------------- Einladungen */

function renderInvites() {
  const nurOffen = $('#invOnlyOpen').checked;
  const liste = state.invites.filter((v) => !nurOffen || v.state === 'open');
  const rows = liste.map((v) => {
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
  paint($('#invitesBody'), rows || (state.invites.length
    ? emptyRow(7, 'Kein offener Code', 'Alle Codes wurden schon eingelöst oder zurückgezogen.')
    : emptyRow(7, 'Noch keine Codes', 'Oben einen Code erzeugen.')),
    'inv:' + nurOffen + ':' + JSON.stringify(liste));
  const offen = state.invites.filter((v) => v.state === 'open').length;
  setText('#invitesSum', state.invites.length
    ? state.invites.length + ' ' + plural(state.invites.length, 'Code', 'Codes') + ' · ' + offen + ' noch offen'
    : 'noch keiner ausgestellt');
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
        <span class="code-chip code-big">${esc(code)}</span>
        <button class="btn btn-sm" data-copy="${esc(code)}">Kopieren</button>
      </div>
      <p class="small mb0">Diesen Code weitergeben. Der Gast meldet sich mit Discord an und trägt
        ihn dort einmal ein – danach gehört das Konto ihm.</p></div>`;
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

function syncUserSelect() {
  const sel = $('#pmUser');
  const sig = 'u:' + state.users.map((u) => u.id + ':' + u.name).join('|');
  if (sel.dataset.sig === sig) return;
  const keep = sel.value;
  sel.dataset.sig = sig;
  sel.innerHTML = '<option value="">– bitte wählen –</option>' + state.users.map((u) =>
    `<option value="${esc(u.id)}">${esc(u.name)}${u.blocked ? ' (gesperrt)' : ''}</option>`).join('');
  if (keep && state.users.some((u) => u.id === keep)) sel.value = keep;
}

/* Die Grenzen kommen vom Server (`/api/admin/passes` → `limits`). Steht dort nichts, gelten die
   Werte aus passes.py. So folgt das Formular dem Dienst, statt eigene Zahlen zu erfinden. */
function passGrenzen() {
  const l = state.passLimits || {};
  const paar = (wert, vorgabe) => (Array.isArray(wert) && wert.length === 2
    ? [Number(wert[0]) || vorgabe[0], Number(wert[1]) || vorgabe[1]] : vorgabe);
  const ram = paar(l.ram_total_mb, [1024, 10240]);
  return {
    days: paar(l.days, [1, 3650]),
    slots: paar(l.max_concurrent, [1, 10]),
    ramGb: [Math.max(1, Math.floor(ram[0] / 1024)), Math.max(1, Math.round(ram[1] / 1024))],
  };
}

/* Die Felder tragen dieselben Grenzen wie die Prüfung – einmal je Änderung. */
function syncGrenzen() {
  const g = passGrenzen();
  const setzen = (sel, min, max) => {
    const el = $(sel);
    if (!el) return;
    const sig = min + ':' + max;
    if (el.dataset.sig === sig) return;
    el.dataset.sig = sig;
    el.min = String(min);
    el.max = String(max);
  };
  setzen('#pmDays', g.days[0], g.days[1]);
  setzen('#pmSlots', g.slots[0], g.slots[1]);
  setzen('#pmRam', g.ramGb[0], g.ramGb[1]);
}

function formValues() {
  const g = passGrenzen();
  const days = clamp(Number($('#pmDays').value) || g.days[0], g.days[0], g.days[1]);
  const slots = clamp(Number($('#pmSlots').value) || g.slots[0], g.slots[0], g.slots[1]);
  const ramGb = clamp(Number($('#pmRam').value) || g.ramGb[0], g.ramGb[0], g.ramGb[1]);
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
    box.innerHTML = '<p class="mb0 muted">Bitte links einen Benutzer auswählen. Hier steht dann in '
      + 'einem Satz, was der Pass erlaubt.</p>';
    return;
  }
  const name = userName(v.user_id);
  const ende = fmtDay(nowSec() + v.days * 86400);
  const art = v.kind === 'premium'
    ? 'Die Server dürfen dauerhaft hier liegen bleiben, auch ohne Kopie auf dem PC.'
    : 'Nach Ablauf holt ' + name + ' die Server auf den eigenen PC zurück.';
  const ramProServer = Math.floor(v.ram_total_mb / v.max_concurrent);
  box.innerHTML = `<p><b>${esc(name)}</b> darf ab sofort <b>${esc(v.days)} ${plural(v.days, 'Tag', 'Tage')}</b>
    lang <b>${esc(v.max_concurrent)} ${plural(v.max_concurrent, 'Server', 'Server')}</b> gleichzeitig laufen
    lassen, zusammen höchstens <b>${esc(gb(v.ram_total_mb))}</b> Arbeitsspeicher.</p>
    <div class="kv">
      <div><div class="k">Art</div><div class="v">${v.kind === 'premium' ? 'Premium' : 'Lokal'}</div></div>
      <div><div class="k">Endet am</div><div class="v">${esc(ende)}</div></div>
      <div><div class="k">Je Server im Schnitt</div><div class="v">${esc(gb(ramProServer))}</div></div>
    </div>
    <p class="small muted mb0">${esc(art)} Anlegen darf ${esc(name)} beliebig viele Server –
      begrenzt ist nur das Einschalten.</p>`;
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
  syncGrenzen();
  renderPreview();
  const onlyActive = $('#pmOnlyActive').checked;
  const list = state.passes.filter((p) => !onlyActive || p.state === 'active');
  const rows = list.map((p) => `<tr>
    <td><div class="t-main">${esc(userName(p.user_id))}</div>${p.note ? `<div class="t-sub">${esc(p.note)}</div>` : ''}</td>
    <td>${p.kind === 'premium' ? '<span class="pill pill-blue">Premium</span>' : '<span class="pill pill-grey">Lokal</span>'}</td>
    <td class="nowrap">${esc(p.max_concurrent)} gleichzeitig<div class="t-sub">zusammen ${esc(gb(p.ram_total_mb))}</div></td>
    <td class="nowrap">${esc(fmtDate(p.issued_at))}<div class="t-sub">${esc(p.days)} ${plural(p.days, 'Tag', 'Tage')}</div></td>
    <td class="nowrap">${esc(p.remaining_text || '')}<div class="t-sub">bis ${esc(fmtDate(p.expires_at))}</div></td>
    <td>${passPill(p)}</td>
    <td class="nowrap"><div class="btn-row">
      ${p.state === 'revoked' ? '' : `<button class="btn btn-sm" data-extend="${esc(p.id)}">Verlängern</button>`}
      ${p.state === 'active' ? `<button class="btn btn-sm" data-limits="${esc(p.id)}">Grenzen</button>` : ''}
      ${p.state === 'active' ? `<button class="btn btn-sm btn-danger" data-revpass="${esc(p.id)}">Widerrufen</button>` : ''}
    </div></td>
  </tr>`).join('');
  paint($('#passesBody'), rows || (state.passes.length
    ? emptyRow(7, 'Kein gültiger Pass', 'Entferne den Haken, um auch abgelaufene zu sehen.')
    : emptyRow(7, 'Noch keine Pässe', 'Oben im Formular einen Pass ausstellen.')),
    'pass:' + onlyActive + ':' + JSON.stringify(list) + ':' + state.users.map((u) => u.id + u.name).join(','));
  const gueltig = state.passes.filter((p) => p.state === 'active').length;
  setText('#passesSum', state.passes.length
    ? state.passes.length + ' ' + plural(state.passes.length, 'Pass', 'Pässe') + ' · ' + gueltig + ' gültig'
    : 'noch keiner ausgestellt');
}

async function extendPass(pid) {
  const entry = state.passes.find((p) => p.id === pid);
  if (!entry) return;
  const grenzen = passGrenzen();
  const werte = await ask({
    title: 'Pass verlängern',
    text: `Der Pass von <b>${esc(userName(entry.user_id))}</b> (${esc(entry.remaining_text || '')})
      wird um die angegebenen Tage verlängert – bei einem abgelaufenen Pass ab heute.`,
    ok: 'Verlängern',
    fields: [{ name: 'days', label: 'Um wie viele Tage', type: 'number', value: 30,
      min: grenzen.days[0], max: grenzen.days[1] }],
  });
  if (werte === null) return;
  const days = clamp(Number(werte.days) || 0, 0, grenzen.days[1]);
  if (!days) {
    toast(`Bitte eine Zahl von ${grenzen.days[0]} bis ${grenzen.days[1]} eintragen.`, true);
    return;
  }
  try {
    await api('/api/admin/passes/' + encodeURIComponent(pid) + '/extend', { method: 'POST', body: { days } });
    toast(`Der Pass ist um ${days} ${plural(days, 'Tag', 'Tage')} verlängert.`);
    await tick();
  } catch (e) {
    toast(e.message, true);
  }
}

async function passGrenzenAendern(pid) {
  const entry = state.passes.find((p) => p.id === pid);
  if (!entry) return;
  const g = passGrenzen();
  const werte = await ask({
    title: 'Grenzen ändern',
    text: `Für den Pass von <b>${esc(userName(entry.user_id))}</b>. Laufende Server werden davon
      nicht angefasst – die neue Grenze gilt ab dem nächsten Einschalten.`,
    ok: 'Übernehmen',
    fields: [
      { name: 'slots', label: 'Wie viele Server gleichzeitig', type: 'number',
        value: entry.max_concurrent, min: g.slots[0], max: g.slots[1] },
      { name: 'ram', label: 'Arbeitsspeicher insgesamt (GB)', type: 'number',
        value: Math.round(Number(entry.ram_total_mb || 0) / 1024), min: g.ramGb[0], max: g.ramGb[1],
        hint: 'Gilt für alle gleichzeitig laufenden Server zusammen.' },
    ],
  });
  if (werte === null) return;
  const body = {
    max_concurrent: clamp(Number(werte.slots) || g.slots[0], g.slots[0], g.slots[1]),
    ram_total_mb: clamp(Number(werte.ram) || g.ramGb[0], g.ramGb[0], g.ramGb[1]) * 1024,
  };
  try {
    await api('/api/admin/passes/' + encodeURIComponent(pid) + '/limits', { method: 'POST', body });
    toast('Die Grenzen sind geändert.');
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

function serverArt(i) {
  return (i.type === 'bedrock' ? 'Bedrock' : 'Java')
    + (i.flavor ? ' · ' + i.flavor : '') + (i.version ? ' ' + i.version : '');
}

function renderServers() {
  const st = state.status || {};
  const running = state.instances.filter((i) => i.running);
  const ramUsed = running.reduce((a, i) => a + Number((i.live || {}).memory_mb || 0), 0);
  const platte = plattenGrenzen();
  paint($('#serversKpis'), [
    statTile('Laufende Server', String(running.length),
      running.length ? running.map((i) => i.name).join(', ') : 'gerade nichts an',
      running.length ? 'good' : ''),
    statTile('Vergebener Speicher', gb(st.ram_running_mb), 'tatsächlich belegt: ' + gb(ramUsed)),
    statTile('Server insgesamt', String(state.instances.length), (() => {
      const wartend = state.instances.filter((i) => i.state === 'awaiting_pull').length;
      return wartend ? wartend + ' ' + plural(wartend, 'wartet', 'warten') + ' auf Rückholung'
        : 'keiner wartet auf Rückholung';
    })()),
    statTile('Platte frei', st.disk_free_text || fmtBytes(platte.frei),
      komma(Math.round(platte.prozentFrei * 10) / 10) + ' %',
      platte.knapp ? 'bad' : '', { pct: 100 - platte.prozentFrei, cls: platte.knapp ? 'bad' : '' }),
  ].join(''), 'skpi:' + running.map((i) => i.id + i.name).join(',') + ':' + st.ram_running_mb + ':'
    + state.instances.length + ':' + st.disk_free_bytes + ':' + ramUsed);

  const frage = state.filter.serversQ;
  const gefiltert = state.instances.filter((i) => {
    const f = state.filter.servers;
    if (f === 'laufend' && !i.running) return false;
    if (f === 'gestoppt' && (i.running || i.state !== 'hosted')) return false;
    if (f === 'wartend' && i.state !== 'awaiting_pull' && i.state !== 'suspended') return false;
    return passtZuSuche(i.name, frage) || passtZuSuche(userName(i.owner), frage)
      || passtZuSuche(addressOf(i), frage);
  });
  const sorted = gefiltert.slice().sort((a, b) => {
    if (a.running !== b.running) return a.running ? -1 : 1;
    const ua = userName(a.owner), ub = userName(b.owner);
    return ua === ub ? String(a.name).localeCompare(String(b.name), 'de') : ua.localeCompare(ub, 'de');
  });
  const rows = sorted.map((i) => {
    const canStart = i.state === 'hosted' && !i.running;
    const canStop = i.running;
    return `<tr class="clickable" data-srv="${esc(i.id)}">
      <td><div class="t-main">${esc(i.name)}</div>
        <div class="t-sub">${esc(serverArt(i))}${i.origin === 'premium' ? ' · Premium' : ''}</div></td>
      <td>${esc(userName(i.owner))}</td>
      <td>${statePill(i)}${i.running && i.live && i.live.uptime ? `<div class="t-sub">seit ${esc(fmtUptime(i.live.uptime))}</div>` : ''}</td>
      <td class="nowrap">${esc(i.size_text || fmtBytes(i.size_bytes))}</td>
      <td class="nowrap">${esc(ramText(i))}</td>
      <td class="nowrap small"><span class="mono">${esc(addressOf(i))}</span>
        <div class="t-sub">${esc(portsText(i))}</div></td>
      <td class="nowrap"><div class="btn-row">
        ${canStart ? `<button class="btn btn-sm btn-primary" data-startsrv="${esc(i.id)}">Starten</button>` : ''}
        ${canStop ? `<button class="btn btn-sm btn-danger" data-stopsrv="${esc(i.id)}">Stoppen</button>` : ''}
        <button class="btn btn-sm" data-srv="${esc(i.id)}">Ansehen</button>
      </div></td>
    </tr>`;
  }).join('');
  paint($('#serversBody'), rows || (state.instances.length
    ? emptyRow(7, 'Kein Treffer', 'Zu dieser Auswahl gibt es keinen Server.')
    : emptyRow(7, 'Noch keine Server', 'Sobald jemand einen Server anlegt, steht er hier.')),
    'srv:' + JSON.stringify(sorted.map((i) => [i.id, i.name, i.owner, i.state, i.running, i.size_bytes,
      (i.live || {}).memory_mb, Math.floor(((i.live || {}).uptime || 0) / 30), i.ports, i.ports_live,
      i.subdomain, i.address]))
    + ':' + state.users.map((u) => u.id + u.name).join(','));

  const sum = state.instances.reduce((a, i) => a + Number(i.size_bytes || 0), 0);
  setText('#serversSum', state.instances.length
    ? `${sorted.length} von ${state.instances.length} ${plural(state.instances.length, 'Server', 'Servern')} · `
      + `${fmtBytes(sum)} auf der Platte`
    : 'Noch ist keiner angelegt.');
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
  const drauf = Number((inst || {}).players_online || 0);
  // Mit Spielern auf dem Server lieber eine halbe Minute Vorlauf, sonst reichen zehn Sekunden.
  const vorschlag = drauf > 0 ? 30 : 10;
  const value = await ask({
    title: 'Server stoppen?',
    text: `„${esc(inst ? inst.name : id)}“ wird heruntergefahren. ${drauf > 0
      ? '<b>' + drauf + (drauf === 1 ? ' Spieler ist' : ' Spieler sind') + ' gerade drauf</b> – sie bekommen'
      : 'Spieler auf dem Server bekämen'} einen Countdown im Spiel, danach wird die Welt gespeichert.`,
    ok: 'Stoppen', danger: true,
    fields: [{ name: 'sek', label: 'Vorwarnzeit in Sekunden', type: 'number', value: vorschlag,
               min: 0, max: 900, hint: '0 = sofort und ohne Ansage.' }],
  });
  if (value === null) return;
  let sek = parseInt(value.sek, 10);
  if (!Number.isFinite(sek) || sek < 0) sek = 0;
  sek = Math.min(900, sek);
  try {
    await api('/api/servers/' + encodeURIComponent(id) + '/stop',
              { method: 'POST', body: { announce_seconds: sek } });
    toast(sek > 0
      ? `„${inst ? inst.name : id}“ wird in ${sek} Sekunden gestoppt – die Spieler wurden gewarnt.`
      : `„${inst ? inst.name : id}“ wird sofort gestoppt.`);
    await tick();
  } catch (e) {
    toast(e.message, true);
  }
}

/* --------------------------------------------- Ein Server im Einzelnen */

let srvTimer = 0;

/* Zeigt einen Server mit allen Angaben und – wenn er läuft – den letzten Konsolenzeilen. */
function showServer(id) {
  const inst = state.instances.find((i) => i.id === id);
  if (!inst) return;
  state.freeze += 1;
  const host = $('#modalHost');
  host.innerHTML = `
    <div class="modal-back" id="srvBack">
      <div class="modal modal-lg" role="dialog" aria-modal="true" aria-labelledby="srvTitel">
        <div class="card-head">
          <div>
            <h3 id="srvTitel">${esc(inst.name)}</h3>
            <div class="modal-lead">${esc(serverArt(inst))} · ${esc(userName(inst.owner))}</div>
          </div>
          <div id="srvPill">${statePill(inst)}</div>
        </div>
        <div class="kv">
          <div><div class="k">Adresse</div><div class="v mono">${esc(addressOf(inst))}</div></div>
          <div><div class="k">Ports</div><div class="v">${esc(portsText(inst))}</div></div>
          <div><div class="k">Arbeitsspeicher</div><div class="v">${esc(ramText(inst))}</div></div>
          <div><div class="k">Größe</div><div class="v">${esc(inst.size_text || fmtBytes(inst.size_bytes))}</div></div>
          <div><div class="k">Zustand</div><div class="v">${esc(inst.state_text || inst.state)}</div></div>
          <div><div class="k">Herkunft</div><div class="v">${inst.origin === 'premium' ? 'Premium (bleibt hier)' : 'Vom PC hochgeladen'}</div></div>
          <div><div class="k">Angelegt</div><div class="v">${esc(fmtDate(inst.created_at))}</div></div>
          <div><div class="k">Zuletzt geändert</div><div class="v">${esc(fmtDate(inst.updated_at))}</div></div>
        </div>
        <h4 class="srv-konsole-titel">Konsole</h4>
        <div class="console" id="srvKonsole">Wird geladen …</div>
        <div class="btn-row">
          ${inst.state === 'hosted' && !inst.running
            ? `<button class="btn btn-primary" data-startsrv="${esc(inst.id)}">Starten</button>` : ''}
          ${inst.running ? `<button class="btn btn-danger" data-stopsrv="${esc(inst.id)}">Stoppen</button>` : ''}
          <button class="btn" id="srvZu">Schließen</button>
        </div>
      </div>
    </div>`;
  const zu = () => {
    if (srvTimer) { clearInterval(srvTimer); srvTimer = 0; }
    host.innerHTML = '';
    state.freeze = Math.max(0, state.freeze - 1);
    document.removeEventListener('keydown', onKey, true);
    tick();
  };
  const onKey = (ev) => {
    if (ev.key === 'Escape') { ev.preventDefault(); zu(); return; }
    if (ev.key === 'Tab') fangeFokus(ev, $('#srvBack'));
  };
  document.addEventListener('keydown', onKey, true);
  $('#srvZu').onclick = zu;
  $('#srvBack').onclick = (ev) => { if (ev.target.id === 'srvBack') zu(); };
  $('#srvZu').focus();
  state.srvClose = zu;
  konsoleHolen(id);
  srvTimer = setInterval(() => konsoleHolen(id), 3000);
}

async function konsoleHolen(id) {
  const box = $('#srvKonsole');
  if (!box) return;
  try {
    const data = await api('/api/servers/' + encodeURIComponent(id) + '/console?tail=200');
    const lines = Array.isArray(data.lines) ? data.lines : [];
    if (!lines.length) {
      box.innerHTML = '<span class="muted">Es gibt gerade keine Ausgabe – der Server läuft nicht.</span>';
      return;
    }
    const unten = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
    box.innerHTML = lines.map((z) => {
      const text = String(z);
      const art = /\b(ERROR|SEVERE|FATAL)\b/.test(text) ? ' line-err'
        : (/\bWARN/.test(text) ? ' line-warn' : '');
      return `<span class="line${art}">${esc(text)}</span>`;
    }).join('');
    if (unten) box.scrollTop = box.scrollHeight;
    const pill = $('#srvPill');
    if (pill && data.live) {
      pill.innerHTML = statePill({ running: !!data.live.running, state: 'hosted', state_text: 'gestoppt' });
    }
  } catch (e) {
    box.innerHTML = `<span class="muted">${esc(e.message)}</span>`;
  }
}

/* ------------------------------------------------------------- Maschine */

function renderMachine() {
  const st = state.status || {};
  const ram = ramGrenzen();
  const platte = plattenGrenzen();
  const load = Array.isArray(st.load) ? st.load : null;
  const runList = (Array.isArray(st.running) ? st.running : []).filter((r) => r && r.running);
  const ramPct = ram.machine ? (ram.machine - ram.frei) / ram.machine * 100 : 0;
  const diskPct = platte.gesamt ? (platte.gesamt - platte.frei) / platte.gesamt * 100 : 0;

  const warn = platte.knapp ? `<div class="note note-err">
      <h3>Die Platte wird knapp</h3>
      <p>Von ${esc(fmtBytes(platte.gesamt || 32212254720))} sind nur noch
        <b>${esc(st.disk_free_text || fmtBytes(platte.frei))}</b> frei
        (${esc(komma(Math.round(platte.prozentFrei * 10) / 10))} %).
        Die Platte ist der Engpass dieser Maschine – eine gut erkundete Welt wiegt schnell 2 bis 5 GB,
        dazu kommen Sicherungen und Übertragungen.</p>
      <p class="mb0">Unter 3 GB freiem Platz lehnt der Server jeden Start ab. Bitte alte Übertragungen
        aufräumen, ruhende Server zurückholen lassen oder keine neuen Pässe mehr ausstellen.</p>
    </div>` : '';

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
    : leer('Gerade läuft kein Server', 'Die Maschine hat Pause.');

  const candidates = Array.isArray(st.premium_delete_candidates) ? st.premium_delete_candidates : [];
  const extra = candidates.length ? `<div class="note note-warn">
      <h3>Ruhende Premium-Server</h3>
      <p class="mb0">${candidates.map((c) => esc(c.name)).join(', ')} –
        ${plural(candidates.length, 'dieser Server ruht', 'diese Server ruhen')} lange genug ohne
        gültigen Pass, ${plural(candidates.length, 'er darf', 'sie dürfen')} gelöscht werden.
        Bitte vorher mit den Besitzern sprechen.</p></div>` : '';

  const transfers = Array.isArray(st.transfers) ? st.transfers : [];
  const transferBox = transfers.length
    ? transfers.map((t) => {
      const inst = state.instances.find((i) => i.id === t.instance_id);
      return `<div class="wrow">
        <div><span class="w-name">${esc(inst ? inst.name : (t.instance_id || t.id))}</span>
          <div class="w-meta">${esc(t.kind === 'download' ? 'Rückholung' : 'Hochladen')} ·
            ${esc(t.done_files || 0)} ${plural(t.done_files, 'Datei', 'Dateien')} fertig ·
            ${esc(fmtBytes(t.total_bytes))} insgesamt · begonnen ${esc(fmtDate(t.created_at))}</div></div>
        <div><span class="pill pill-blue">${esc(t.state || 'offen')}</span></div>
      </div>`;
    }).join('')
    : leer('Keine offene Übertragung', 'Hochladen und Zurückholen sind alle abgeschlossen.');

  const ports = Array.isArray(st.ports) ? st.ports : [];
  const routen = st.routes && typeof st.routes === 'object' ? Object.keys(st.routes).length : 0;

  const html = warn + `
    <div class="kpis">
      ${statTile('Freier Arbeitsspeicher', gb(ram.frei), 'von ' + gb(ram.machine) + ' insgesamt', '',
        { pct: ramPct, cls: ramPct > 88 ? 'bad' : (ramPct > 75 ? 'warn' : '') })}
      ${statTile('An Server vergeben', gb(ram.laufend),
        runList.length + ' ' + plural(runList.length, 'Server läuft', 'Server laufen'))}
      ${statTile('Freie Platte', st.disk_free_text || fmtBytes(platte.frei),
        komma(Math.round(platte.prozentFrei * 10) / 10) + ' % von ' + fmtBytes(platte.gesamt),
        platte.knapp ? 'bad' : (platte.prozentFrei < 25 ? 'warn' : ''),
        { pct: diskPct, cls: platte.knapp ? 'bad' : (platte.prozentFrei < 25 ? 'warn' : '') })}
      ${statTile('Last', load ? load.map((x) => komma(Math.round(Number(x) * 100) / 100)).join(' · ') : '–',
        load ? '1, 5 und 15 Minuten (8 Kerne)' : 'wird nicht gemeldet')}
    </div>

    <div class="grid2">
      <div class="card">
        <div class="card-head"><h3>Auslastung</h3></div>
        ${meter('Arbeitsspeicher', Math.round(ramPct) + ' %', ramPct,
          'Für Server sollten höchstens 10 GB zugleich vergeben werden; der Rest bleibt für das '
          + 'System und die Java-Verwaltung.',
          ramPct > 88 ? 'bad' : (ramPct > 75 ? 'warn' : ''))}
        ${meter('Platte', Math.round(diskPct) + ' %', diskPct,
          'Welten, Sicherungen und Übertragungen zusammen: '
          + (st.size_bytes !== undefined ? fmtBytes(st.size_bytes) : '–') + '.',
          platte.knapp ? 'bad' : (platte.prozentFrei < 25 ? 'warn' : ''))}
      </div>
      <div class="card">
        <div class="card-head"><h3>Laufende Server</h3>
          <span class="muted small">${esc(gb(ram.laufend))} vergeben</span></div>
        ${running}
      </div>
    </div>

    ${extra}

    <div class="card">
      <div class="card-head"><h3>Offene Übertragungen</h3>
        <span class="muted small">${esc(transfers.length)} ${plural(transfers.length, 'Vorgang', 'Vorgänge')}</span></div>
      ${transferBox}
    </div>

    <div class="card">
      <div class="card-head"><h3>Dienst</h3>
        <span class="muted small">${esc(st.domain || HOST_FALLBACK)}</span></div>
      <div class="kv">
        <div><div class="k">Fassung</div><div class="v">${esc(st.version || '–')}</div></div>
        <div><div class="k">Läuft seit</div><div class="v">${esc(st.started_at ? fmtUptime((st.time || nowSec()) - st.started_at) : '–')}</div>
          <div class="kv-sub">${esc(st.started_at ? 'gestartet am ' + fmtDate(st.started_at) : '')}</div></div>
        <div><div class="k">Konten · Server · Pässe</div><div class="v">${esc([st.users, st.instances, st.passes]
          .map((x) => (x === undefined ? '–' : x)).join(' · '))}</div></div>
        <div><div class="k">Vergebene Ports</div><div class="v">${esc(ports.length)}</div></div>
        <div><div class="k">Einträge im Verteiler</div><div class="v">${esc(routen)}</div></div>
        <div><div class="k">Anmeldung über Discord</div><div class="v">${st.discord === false
          ? '<span class="pill pill-amber">nicht eingerichtet</span>'
          : '<span class="pill pill-green">eingerichtet</span>'}</div></div>
      </div>
    </div>`;

  paint($('#machineBox'), html, 'mach:' + JSON.stringify([ram.frei, ram.machine, ram.laufend,
    platte.frei, platte.gesamt, st.load, st.version, st.started_at, st.users, st.instances,
    st.passes, st.size_bytes, st.discord, ports.length, routen, candidates.map((c) => c.id),
    runList.map((r) => [r.id, r.ready, r.memory_mb, Math.floor((r.uptime || 0) / 30)]),
    transfers.map((t) => [t.id, t.state, t.done_files])]));
}

/* ------------------------------------------------------------ Ansichten */

function setView(view) {
  if (!VIEWS.includes(view)) view = 'uebersicht';
  state.view = view;
  $$('#tabs .tab').forEach((b) => {
    const an = b.dataset.view === view;
    b.classList.toggle('active', an);
    b.setAttribute('aria-selected', an ? 'true' : 'false');
  });
  $$('.view').forEach((s) => s.classList.add('hidden'));
  const box = $('#view' + view.charAt(0).toUpperCase() + view.slice(1));
  if (box) box.classList.remove('hidden');
  // Ein neuer Reiter fängt oben an – sonst steht man mitten in einer Liste.
  try { window.scrollTo(0, 0); } catch (e) { /* egal */ }
  render();
  tick();
}

/* ------------------------------------------------------------- Bedienung */

function bind() {
  $('#btnDiscord').onclick = loginDiscord;
  $('#btnReg').onclick = registrieren;
  $('#btnRegBack').onclick = regAbbrechen;
  $('#regCode').addEventListener('input', (ev) => {
    const el = ev.target;
    const amEnde = el.selectionStart === el.value.length;
    el.value = codeFormatieren(el.value);
    if (amEnde) el.setSelectionRange(el.value.length, el.value.length);
  });
  $('#regCode').addEventListener('keydown', (ev) => { if (ev.key === 'Enter') registrieren(); });
  $('#btnLogout').onclick = doLogout;
  $('#btnLink').onclick = linkDiscord;
  $('#tabs').addEventListener('click', (ev) => {
    const tab = ev.target.closest('.tab');
    if (tab) setView(tab.dataset.view);
  });

  $('#btnInvite').onclick = createInvite;
  $('#btnIssue').onclick = issuePass;
  $('#invOnlyOpen').onchange = () => renderInvites();
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
    $$('#pmKind .choice').forEach((b) => {
      b.classList.toggle('sel', b === btn);
      b.setAttribute('aria-pressed', b === btn ? 'true' : 'false');
    });
    renderPreview();
  });

  $('#usersSearch').addEventListener('input', (ev) => {
    state.filter.usersQ = String(ev.target.value || '').trim();
    renderUsers();
  });
  $('#serversSearch').addEventListener('input', (ev) => {
    state.filter.serversQ = String(ev.target.value || '').trim();
    renderServers();
  });
  $('#usersFilter').addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-ufilter]');
    if (!btn) return;
    state.filter.users = btn.dataset.ufilter;
    $$('#usersFilter button').forEach((b) => b.classList.toggle('on', b === btn));
    renderUsers();
  });
  $('#serversFilter').addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-sfilter]');
    if (!btn) return;
    state.filter.servers = btn.dataset.sfilter;
    $$('#serversFilter button').forEach((b) => b.classList.toggle('on', b === btn));
    renderServers();
  });

  /* Alles, was in den Listen steckt, läuft über einen gemeinsamen Zuhörer –
     so überleben die Knöpfe jedes Neuzeichnen. */
  document.addEventListener('click', async (ev) => {
    const t = ev.target.closest('[data-copy],[data-revinvite],[data-block],[data-toggle],[data-topass],'
      + '[data-extend],[data-limits],[data-revpass],[data-startsrv],[data-stopsrv],[data-srv],'
      + '[data-goto],[data-rename],[data-role]');
    if (!t) return;
    if (t.dataset.copy !== undefined) {
      const ok = await copyText(t.dataset.copy);
      toast(ok ? 'Der Code liegt in der Zwischenablage.' : 'Kopieren hat nicht geklappt – bitte von Hand abschreiben.', !ok);
      return;
    }
    if (t.dataset.goto !== undefined) { setView(t.dataset.goto); return; }
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
    if (t.dataset.rename !== undefined) { renameUser(t.dataset.rename); return; }
    if (t.dataset.role !== undefined) { changeRole(t.dataset.role, t.dataset.to); return; }
    if (t.dataset.extend !== undefined) { extendPass(t.dataset.extend); return; }
    if (t.dataset.limits !== undefined) { passGrenzenAendern(t.dataset.limits); return; }
    if (t.dataset.revpass !== undefined) { revokePass(t.dataset.revpass); return; }
    if (t.dataset.startsrv !== undefined) { schliesseServerFenster(); startServer(t.dataset.startsrv); return; }
    if (t.dataset.stopsrv !== undefined) { schliesseServerFenster(); stopServer(t.dataset.stopsrv); return; }
    if (t.dataset.srv !== undefined) { showServer(t.dataset.srv); return; }
  });
}

/* Vor einer Rückfrage muss das Serverfenster weg – es teilt sich den Platz mit ihr. */
function schliesseServerFenster() {
  if (typeof state.srvClose === 'function' && $('#srvBack')) {
    const zu = state.srvClose;
    state.srvClose = null;
    zu();
  }
}

async function toggleBlock(uid, block) {
  const user = state.users.find((u) => u.id === uid);
  const name = user ? user.name : uid;
  let reason = '';
  if (block) {
    const running = state.instances.filter((i) => i.owner === uid && i.running);
    const werte = await ask({
      title: 'Konto sperren?',
      text: `<b>${esc(name)}</b> kann sich danach nicht mehr anmelden, alle Sitzungen enden.
        ${running.length ? `Laufende Server (<b>${running.map((i) => esc(i.name)).join(', ')}</b>)
          werden angekündigt heruntergefahren.` : 'Gerade läuft kein Server dieses Kontos.'}
        Die Dateien auf der Platte bleiben erhalten.`,
      ok: 'Sperren', danger: true,
      fields: [{ name: 'grund', label: 'Grund (wird dem Konto angezeigt)', placeholder: 'z. B. Zahlung offen' }],
    });
    if (werte === null) return;
    reason = werte.grund || '';
  } else {
    const werte = await ask({
      title: 'Konto entsperren?',
      text: `<b>${esc(name)}</b> kann sich danach wieder anmelden und Server starten,
        sofern ein gültiger Pass vorliegt.`,
      ok: 'Entsperren',
    });
    if (werte === null) return;
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

async function renameUser(uid) {
  const user = state.users.find((u) => u.id === uid);
  if (!user) return;
  const werte = await ask({
    title: 'Konto umbenennen',
    text: 'So heißt das Konto in der Verwaltung und im Programm auf dem PC. Die Server und Pässe '
      + 'bleiben unverändert.',
    ok: 'Umbenennen',
    fields: [{ name: 'name', label: 'Neuer Name', value: user.name, maxlength: 32,
      hint: '2 bis 32 Zeichen.' }],
  });
  if (werte === null) return;
  const name = String(werte.name || '').trim();
  if (name.length < 2) { toast('Der Name braucht mindestens zwei Zeichen.', true); return; }
  try {
    await api('/api/admin/users/' + encodeURIComponent(uid) + '/rename',
      { method: 'POST', body: { name } });
    toast('Das Konto heißt jetzt ' + name + '.');
    await tick();
  } catch (e) {
    toast(e.message, true);
  }
}

async function changeRole(uid, ziel) {
  const user = state.users.find((u) => u.id === uid);
  if (!user) return;
  const selbst = String(((state.me || {}).user || {}).id || '') === uid;
  if (selbst && ziel !== 'admin') {
    toast('Das eigene Betreiberrecht lässt sich hier nicht abgeben – sonst sperrst du dich aus.', true);
    return;
  }
  const werte = await ask({
    title: ziel === 'admin' ? 'Zum Betreiber machen?' : 'Betreiberrecht abgeben?',
    text: ziel === 'admin'
      ? `<b>${esc(user.name)}</b> darf danach diese Verwaltung öffnen: alle Konten, alle Pässe,
         alle Server. Das lässt sich jederzeit zurücknehmen.`
      : `<b>${esc(user.name)}</b> ist danach ein gewöhnliches Konto und kann die Verwaltung
         nicht mehr öffnen.`,
    ok: ziel === 'admin' ? 'Zum Betreiber machen' : 'Recht abgeben',
    danger: ziel !== 'admin',
  });
  if (werte === null) return;
  try {
    await api('/api/admin/users/' + encodeURIComponent(uid) + '/role',
      { method: 'POST', body: { role: ziel === 'admin' ? 'admin' : 'user' } });
    toast(ziel === 'admin' ? user.name + ' ist jetzt Betreiber.' : user.name + ' ist wieder ein gewöhnliches Konto.');
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
        max_concurrent: 2, ram_total_mb: 8192, ram_total_text: '8 GB', issued_at: t - 86400 * 12,
        expires_at: t + 86400 * 2, revoked_at: 0, note: 'bezahlt bis Ende März', state: 'active',
        remaining_seconds: 86400 * 2, remaining_text: 'läuft in 2 Tagen ab',
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
    {
      id: 'd4e5f6071829', name: 'Mira', role: 'user', created_at: t - 86400 * 4, blocked: false,
      blocked_reason: '', discord_linked: true, discord_name: 'mira', last_seen: t - 86400,
      limits: { max_concurrent: 0, ram_total_mb: 0, premium: false, pass_count: 0 },
      limits_text: 'kein gültiger Pass', instances: 0, running: 0, size_bytes: 0, sessions: 1,
      passes: [],
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
    load: [1.42, 1.18, 0.96], discord: true, domain: 'arcardia-nexus.de',
    ports: [{ owner: 'i1111111', pool: 'java', proto: 'tcp', first: 25565, count: 1 },
            { owner: 'i3333333', pool: 'bedrock', proto: 'udp', first: 19132, count: 1 }],
    routes: { nexus: { port: 25565 }, 'annas-insel': { port: 19132 } },
    premium_delete_candidates: [],
    transfers: [{ id: 'tr100000', kind: 'upload', state: 'offen', instance_id: 'i4444444',
                  user_id: 'b2c3d4e5f607', total_bytes: 2.2 * 1073741824, done_files: 184,
                  created_at: t - 5400, updated_at: t - 300 }],
    size_bytes: 8.6 * 1073741824,
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
  if (path.startsWith('/api/health')) return { ok: true, discord: true, erstzugang: false };
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
  if (path.indexOf('/console') > 0) {
    return {
      next: 812,
      lines: ['[12:02:11] [Server thread/INFO]: Starting minecraft server version 1.21.1',
        '[12:02:14] [Server thread/INFO]: Preparing level "world"',
        '[12:02:31] [Server thread/INFO]: Done (17.402s)! For help, type "help"',
        '[12:41:08] [Server thread/INFO]: Anna joined the game',
        '[13:02:55] [Server thread/WARN]: Can\'t keep up! Is the server overloaded?'],
      live: { running: true, ready: true },
    };
  }
  if (method === 'POST') {
    throw new ApiError('In der Beispielansicht wird nichts wirklich geändert.', 0);
  }
  return {};
}

/* ------------------------------------------------------------------ Start */

function boot() {
  bind();
  renderPreview();
  const aus = hashLesen();
  if (aus.verknuepft) {
    setTimeout(() => toast('Dein Discord-Konto ist jetzt verknüpft – ab sofort reicht „Mit Discord anmelden“.'), 300);
  }
  setInterval(tick, REFRESH_MS);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
  if (DEMO) { state.token = 'beispiel'; start(); return; }
  // Ein Merkzettel hat Vorrang: Er kommt frisch von Discord, ein altes Token wäre nur im Weg.
  if (aus.zettel) { writeToken(''); showRegister(aus.zettel, aus.name); return; }
  if (aus.token) writeToken(aus.token);
  else state.token = readToken();
  if (state.token) start();
  else showLogin(aus.fehler, !!aus.fehler);
}

boot();
