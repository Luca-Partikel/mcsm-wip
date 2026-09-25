/* Minecraft Server Manager – Oberflächenlogik (kein Framework, keine Abhängigkeiten) */
'use strict';

const TOKEN = new URLSearchParams(location.search).get('t') || '';
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const EULA_URL = 'https://aka.ms/MinecraftEULA';
const PRIVACY_URL = 'https://go.microsoft.com/fwlink/?LinkId=521839';
const MS_LINK_URL = 'https://www.microsoft.com/link';

const state = {
  servers: [],
  system: {},
  view: 'welcome',          // welcome | wizard | install | server | help | xbox
  help: 'bedrock',
  activeId: null,
  tab: 'overview',
  wizard: null,
  install: null,            // { serverId, jobId, job, after }
  xbox: null,               // Assistent für den Xbox-Freunde-Modus
  files: { path: '', showAll: false, open: null, mode: 'form', props: null, content: '', dirty: false },
  players: { data: null, running: null },   // Spielerverwaltung: zuletzt geladenes Bild + Serverzustand dazu
  versions: { java: null, bedrock: null },
  consoleNext: 0,
  publicIp: null,
  publicIpAt: 0,            // Zeitpunkt der letzten Router-Abfrage (0 = noch nie)
  publicIpBusy: false,
  cloudCardSig: '',
  // Cloud (Root-Server): Konto, Pässe, entfernte Server, laufende Übertragung
  cloud: {
    data: null, error: '', loading: true, busy: '', hideBanner: false,
    job: null,                        // { id, label, serverId, job }
    rate: null,                       // { at, bytes, bps } – Geschwindigkeit der Übertragung
    open: null,                       // geöffneter entfernter Server (Konsole/Dateien)
    log: { next: 0, lines: [], running: false },
    files: { path: '', entries: null, open: null, text: '', error: '', dirty: false },
    login: { pending: false, error: '', url: '' },
    cmd: '',                          // Befehlszeile der entfernten Konsole (überlebt das Neuzeichnen)
    askingInvite: false,              // der Dialog für den Einladungscode ist offen
    sig: '',                          // Fingerabdruck des zuletzt Gezeichneten (siehe cloudSig)
  },
  timers: { status: null, console: null, job: null, xbox: null, xboxJob: null,
            cloud: null, cloudJob: null, cloudLog: null, cloudLogin: null, cloudTick: null },
  xboxSig: '',
};

/* ------------------------------------------------------------------ API */

async function api(path, { method = 'GET', body } = {}) {
  const res = await fetch('/api/' + path, {
    method,
    headers: { 'X-Token': TOKEN, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch { /* keine JSON-Antwort */ }
  if (!res.ok) throw new Error(data.error || ('Fehler ' + res.status));
  return data;
}

function toast(msg, err = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (err ? ' err' : '');
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), err ? 7000 : 4000);
}

/* ------------------------------------------------------------------ Dialoge
   Eigene Dialoge statt confirm(), prompt() und alert() des Browsers: Die zeigen im Fenster
   „127.0.0.1:12345 enthält …“, lassen sich nicht gestalten und halten das ganze Programm an.
   Hier: abgedunkelter Hintergrund, Karte in der Mitte, Escape und Klick daneben schließen,
   Tab bleibt im Dialog, Enter bestätigt. Rückgabe ist immer ein Promise. */

const MODALS = [];                       // offene Dialoge, der letzte ist der oberste
const modalOpen = () => MODALS.length > 0;
const FOCUSABLE = 'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), '
  + 'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
let modalNr = 0;

function dialogFields(fields) {
  return fields.map((f) => {
    const id = esc(f.id);
    if (f.type === 'textarea') {
      return `<div class="field"><label for="${id}">${esc(f.label || '')}</label>
        <textarea id="${id}" rows="${f.rows || 4}" placeholder="${esc(f.placeholder || '')}">${esc(f.value || '')}</textarea>
        ${f.hint ? `<div class="hint">${esc(f.hint)}</div>` : ''}</div>`;
    }
    return `<div class="field"><label for="${id}">${esc(f.label || '')}</label>
      <input type="text" id="${id}" value="${esc(f.value || '')}" placeholder="${esc(f.placeholder || '')}"
        ${f.maxlength ? `maxlength="${Number(f.maxlength)}"` : ''}
        ${f.mono ? 'class="mono-in" autocapitalize="characters" spellcheck="false"' : ''}
        ${f.aria ? `aria-label="${esc(f.aria)}"` : ''}>
      ${f.hint ? `<div class="hint">${esc(f.hint)}</div>` : ''}</div>`;
  }).join('');
}

/* opts: title, sub, icon, tone ('danger'|'warn'|'ok'), text, html, body() (statt text/html),
         fields[], validate(values), onSubmit(values, ctx), bind(ctx), confirmText, cancelText
         (null = Knopf weglassen), wide, locked (kein Escape/Klick daneben). */
function dialog(opts = {}) {
  return new Promise((resolve) => {
    const nr = ++modalNr;
    const fields = opts.fields || [];
    const prev = document.activeElement;
    const back = document.createElement('div');
    back.className = 'modal-back';
    back.innerHTML = `
      <div class="modal${opts.wide ? ' modal-wide' : ''}" role="dialog" aria-modal="true"
           aria-labelledby="mdl${nr}t" tabindex="-1">
        <div class="modal-head">
          ${opts.icon ? `<div class="modal-ico${opts.tone ? ' ico-' + opts.tone : ''}" aria-hidden="true">${esc(opts.icon)}</div>` : ''}
          <div class="modal-titles">
            <h2 class="modal-title" id="mdl${nr}t">${esc(opts.title || '')}</h2>
            ${opts.sub ? `<div class="modal-sub">${esc(opts.sub)}</div>` : ''}
          </div>
          <button type="button" class="modal-x" data-mx="cancel" aria-label="Dialog schließen">✕</button>
        </div>
        <div class="modal-body" id="mdl${nr}b"></div>
        <div class="modal-err" role="alert" hidden></div>
        <div class="modal-foot">
          ${opts.cancelText === null ? '' : `<button type="button" class="btn" data-mx="cancel">${esc(opts.cancelText || 'Abbrechen')}</button>`}
          ${opts.confirmText === null ? '' : `<button type="button" class="btn ${opts.tone === 'danger' ? 'btn-red' : 'btn-primary'}" data-mx="ok">${esc(opts.confirmText || 'Bestätigen')}</button>`}
        </div>
      </div>`;
    const card = $('.modal', back);
    const bodyBox = $('.modal-body', back);
    const errBox = $('.modal-err', back);
    const entry = { card, back };
    let done = false;

    const finish = (value) => {
      if (done) return;
      done = true;
      const i = MODALS.indexOf(entry);
      if (i >= 0) MODALS.splice(i, 1);
      document.removeEventListener('keydown', onKey, true);
      back.remove();
      if (!MODALS.length) document.body.classList.remove('modal-on');
      try { if (prev && prev.focus) prev.focus(); } catch { /* Element gibt es nicht mehr */ }
      resolve(value);
    };
    const setError = (msg) => { errBox.hidden = !msg; errBox.textContent = msg || ''; };
    const setBusy = (on) => {
      card.classList.toggle('is-busy', !!on);
      $$('[data-mx]', back).forEach((b) => { b.disabled = !!on; });
    };
    const values = () => {
      const out = {};
      fields.forEach((f) => { const el = $('#' + f.id, back); if (el) out[f.id] = el.value; });
      return out;
    };
    const submit = async () => {
      setError('');
      const v = values();
      if (opts.validate) { const bad = opts.validate(v); if (bad) { setError(bad); return; } }
      if (opts.onSubmit) {
        setBusy(true);
        try {
          const r = await opts.onSubmit(v, ctx);
          setBusy(false);
          if (r === false) return;                     // der Dialog bleibt bewusst offen
        } catch (e) { setBusy(false); setError(e && e.message ? e.message : String(e)); return; }
      }
      finish(fields.length ? v : true);
    };
    const bodyHtml = () => {
      if (opts.body) return opts.body(ctx);
      const text = (opts.text || '').split('\n\n').filter(Boolean)
        .map((p) => `<p>${esc(p)}</p>`).join('');
      return text + (opts.html || '') + (fields.length ? dialogFields(fields) : '');
    };
    const draw = (focusIt) => {
      const wasId = document.activeElement && document.activeElement.id;
      bodyBox.innerHTML = bodyHtml();
      if (opts.bind) opts.bind(ctx);
      if (!focusIt) {
        const back2 = wasId ? $('#' + CSS.escape(wasId), card) : null;
        if (back2) { try { back2.focus(); } catch { /* egal */ } }
        return;
      }
      const first = $('.modal-body input, .modal-body select, .modal-body textarea', card)
        || $('[data-mx="ok"]', card) || card;
      try { first.focus(); if (first.select) first.select(); } catch { /* egal */ }
    };
    const ctx = { root: card, body: bodyBox, setError, setBusy, submit, redraw: () => draw(false),
                  close: (v) => finish(v === undefined ? null : v) };

    const onKey = (e) => {
      if (MODALS[MODALS.length - 1] !== entry) return;
      if (e.key === 'Escape') {
        if (opts.locked) return;
        e.preventDefault(); e.stopPropagation(); finish(null); return;
      }
      if (!card.contains(document.activeElement)) return;
      if (e.key === 'Tab') {
        const list = $$(FOCUSABLE, card).filter((el) => el.offsetWidth || el.offsetHeight);
        if (!list.length) return;
        const first = list[0], last = list[list.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
        return;
      }
      if (e.key === 'Enter') {
        const t = e.target;
        if (t && (t.tagName === 'TEXTAREA' || t.tagName === 'BUTTON' || t.tagName === 'A')) return;
        if (opts.confirmText === null) return;
        e.preventDefault();
        submit();
      }
    };

    back.onclick = (e) => { if (e.target === back && !opts.locked) finish(null); };
    $$('[data-mx]', back).forEach((b) => {
      b.onclick = () => (b.dataset.mx === 'ok' ? submit() : finish(null));
    });
    document.addEventListener('keydown', onKey, true);
    document.body.appendChild(back);
    document.body.classList.add('modal-on');
    MODALS.push(entry);
    draw(true);
  });
}

/** Ja/Nein-Dialog. Gefährliches („danger“) bekommt einen roten Knopf. */
async function askConfirm(opts) {
  return (await dialog({ icon: opts.tone === 'danger' ? '⚠' : '❓', confirmText: 'Bestätigen', ...opts })) === true;
}
/** Eingabe-Dialog (Ersatz für prompt). Rückgabe: Text oder null bei Abbruch. */
async function askInput(opts) {
  const f = { id: opts.id || 'mdlInput', label: opts.label || '', value: opts.value || '',
              placeholder: opts.placeholder || '', hint: opts.hint || '', mono: !!opts.mono,
              maxlength: opts.maxlength, aria: opts.aria };
  const r = await dialog({ icon: '✏', confirmText: 'Übernehmen', ...opts, fields: [f] });
  return r ? String(r[f.id] ?? '').trim() : null;
}
/** Reiner Hinweis (Ersatz für alert). */
function askNote(opts) {
  return dialog({ icon: 'ℹ', cancelText: null, confirmText: 'Verstanden', ...opts });
}

async function refresh() {
  const data = await api('bootstrap');
  state.servers = data.servers;
  state.system = data.system;
  renderSidebar();
  patchStatus();
}

const server = () => state.servers.find((s) => s.id === state.activeId) || null;

/* ------------------------------------------------------------------ Hilfen */

const fmtUptime = (s) => {
  if (!s) return '–';
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h} h ${m} min` : m ? `${m} min ${sec} s` : `${sec} s`;
};
const fmtBytes = (n) => {
  if (n === null || n === undefined) return '';
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
  return (n / 1073741824).toFixed(2) + ' GB';
};
const fmtDate = (ts) => ts ? new Date(ts * 1000).toLocaleString('de-DE', { dateStyle: 'medium', timeStyle: 'short' }) : '';
const fmtCount = (n) => n >= 1e6 ? (n / 1e6).toFixed(1).replace('.0', '') + ' Mio.' : n >= 1e3 ? Math.round(n / 1e3) + ' Tsd.' : String(n || 0);
/* Vier Server-Arten: Bedrock (BDS) · Java nur (Paper) · Java + Crossplay (Paper + Geyser) · Modpack (Fabric/NeoForge/Forge/Quilt). */
const LOADER_NAMES = { fabric: 'Fabric', quilt: 'Quilt', neoforge: 'NeoForge', forge: 'Forge', paper: 'Paper' };
const isModpack = (s) => s.type === 'java' && !!s.flavor && s.flavor !== 'paper';
const bedrockPlayers = (s) => s.type === 'bedrock' || (s.type === 'java' && !isModpack(s) && !!s.geyser);   // kommen Konsolen/Handys drauf?
/* Spielerverwaltung: Bedrock (allowlist.json) und Java mit Paper (whitelist.json) – Modpacks haben keine solche Liste. */
const playersApply = (s) => s.type === 'bedrock' || (s.type === 'java' && !isModpack(s));
const typeLabel = (s) => s.type === 'bedrock' ? 'Bedrock Dedicated Server'
  : isModpack(s) ? `Modpack · ${LOADER_NAMES[s.flavor] || s.flavor}`
  : s.geyser ? 'Java + Crossplay (Paper + Geyser)' : 'Java Edition (Paper, nur Java)';
const typeTag = (s) => s.type === 'bedrock' ? 'BE' : isModpack(s) ? 'MP' : 'JE';
const gb = (mb) => (mb / 1024).toFixed(mb % 1024 ? 1 : 0) + ' GB';
const GM = { survival: 'Überleben', creative: 'Kreativ', adventure: 'Abenteuer', spectator: 'Zuschauer' };
const DF = { peaceful: 'Friedlich', easy: 'Leicht', normal: 'Normal', hard: 'Schwer' };
const statusBig = (s) => s.running ? 'Online' : (s.installing ? 'Wird eingerichtet' : (s.installed ? 'Offline' : 'Nicht installiert'));
const statusSub = (s) => s.running ? 'Läuft seit ' + fmtUptime(s.uptime) : (s.installed ? 'Gestoppt – oben rechts starten' : 'Einrichtung noch nicht abgeschlossen');

/* Ports kommen vom Backend (manager.port_list); Bereiche (from≠to) sind Freigabe-Bereiche, keine Adressen. */
function portsOf(s) {
  if (Array.isArray(s.ports) && s.ports.length) {
    return s.ports.map((p) => ({ ...p, range: p.from !== p.to, port: p.from === p.to ? p.from : `${p.from}–${p.to}` }));
  }
  if (s.type === 'bedrock') return [{ label: 'Bedrock (Konsole, Handy, Windows-App)', port: s.port, from: s.port, to: s.port, proto: 'TCP', range: false }];
  const list = [{ label: 'Java Edition', port: s.port, from: s.port, to: s.port, proto: 'TCP', range: false }];
  if (s.geyser) list.push({ label: 'Bedrock (Konsole, Handy, Windows-App)', port: s.bedrock_port, from: s.bedrock_port, to: s.bedrock_port, proto: 'UDP', range: false });
  return list;
}
const addrPorts = (s) => portsOf(s).filter((p) => !p.range);
const publicAddr = (s) => s.public_address || state.publicIp || '';

/* Platzhalter, solange keine Adresse bekannt ist – nach einem gescheiterten Versuch mit Hinweis statt „wird ermittelt“. */
const pubPlaceholder = () => state.publicIpAt && !state.publicIp ? 'unbekannt – unter „Verbinden“ ermitteln' : '… wird ermittelt';

/* Erklärungszeile unter der Adresse im Dashboard – je nachdem, woher die Adresse stammt. */
function pubNote(s) {
  const settings = '<a href="#" data-tab-go="settings">Einstellungen</a>';
  const src = s.public_address ? `Feste Adresse aus den ${settings}.`
    : state.publicIp ? `Öffentliche IP von der FritzBox ermittelt – für eine feste MyFRITZ!-Adresse: ${settings}.`
    : state.publicIpAt ? `Die FritzBox hat keine öffentliche IPv4 gemeldet – MyFRITZ!-Adresse in den ${settings} eintragen.`
    : `Öffentliche IP wird von der FritzBox abgefragt – für eine feste MyFRITZ!-Adresse: ${settings}.`;
  return `${src} Portfreigabe nötig: <a href="#" data-tab-go="connect">Verbinden</a>.`;
}

/* Alle Adresszeilen (Dashboard und Verbinden) mit der aktuellen öffentlichen Adresse füllen. */
function patchPublicAddr() {
  const s = server();
  if (!s) return;
  $$('[data-pub-port]').forEach((el) => {
    el.textContent = publicAddr(s) ? `${publicAddr(s)} : ${el.dataset.pubPort}` : (el.dataset.pubEmpty || pubPlaceholder());
  });
  $$('[data-pub-note]').forEach((el) => {
    el.innerHTML = pubNote(s);
    $$('[data-tab-go]', el).forEach((a) => a.onclick = (e) => { e.preventDefault(); state.tab = a.dataset.tabGo; render(); });
  });
}

/* Öffentliche IP vom Router holen (UPnP, kein Fremddienst) und im Dashboard eintragen.
   Bei Misserfolg höchstens einmal pro Minute erneut fragen – die FritzBox kann später erreichbar sein. */
async function ensurePublicIp() {
  if (state.publicIp || state.publicIpBusy) return;
  if (Date.now() - state.publicIpAt < 60000) { patchPublicAddr(); return; }
  state.publicIpBusy = true;
  try { state.publicIp = (await api('publicip?auto=1')).ip; }
  catch { /* Router antwortet nicht */ }
  finally { state.publicIpAt = Date.now(); state.publicIpBusy = false; }
  patchPublicAddr();
}
const rangeNote = (s) => portsOf(s).filter((p) => p.range).map((p) => `<div class="muted small">Zusätzlich nutzt der Server <b>${p.proto} ${p.from}–${p.to}</b> (${esc(p.label)}) – wichtig für Firewall und Portfreigabe, nicht zum Eintippen.</div>`).join('');

/* ------------------------------------------------------------------ Seitenleiste */

function renderSidebar() {
  const list = $('#serverList');
  if (!state.servers.length) {
    list.innerHTML = '<div class="srv-empty">Noch kein Server – lege oben einen an.</div>';
  } else {
    list.innerHTML = state.servers.map((s) => `
      <a class="srv-item ${state.view === 'server' && s.id === state.activeId ? 'active' : ''}" data-id="${s.id}">
        <span class="srv-main">
          <span class="dot ${s.running ? 'dot-on' : (s.installed ? '' : 'dot-busy')}"></span>
          <span class="srv-name">${esc(s.name)}</span>
        </span>
        <span class="muted small">${typeTag(s)}</span>
      </a>`).join('');
  }
  $$('.srv-item', list).forEach((el) => el.onclick = () => openServer(el.dataset.id));
  $$('.nav-item[data-help]').forEach((el) => el.classList.toggle('active', state.view === 'help' && el.dataset.help === state.help));
  renderCloudNav();
  $('#sysInfo').textContent = `${state.system.hostname || ''} · ${state.system.local_ip || ''}`;
  renderUpdateBox();
}

/* ------------------------------------------------------------------ Updates (GitHub Releases) */

function renderUpdateBox() {
  const box = $('#updateBox');
  if (!box) return;
  const u = state.update || (state.system && state.system.update) || {};
  const v = state.system.version || u.current || '';
  if (u.available) {
    box.innerHTML = `<button class="btn btn-sm btn-block btn-primary" id="btnUpdate">⬆ Update auf v${esc(u.latest)} verfügbar</button>`;
  } else {
    box.innerHTML = `<div class="muted small">Version ${esc(v)}${u.enabled === false ? '' : ` · <a href="#" id="lnkCheckUpdate">${u.checking ? 'prüft …' : 'Nach Updates suchen'}</a>`}</div>`;
  }
  const b = $('#btnUpdate'); if (b) b.onclick = () => { state.view = 'update'; render(); };
  const l = $('#lnkCheckUpdate'); if (l) l.onclick = (e) => { e.preventDefault(); checkUpdate(true); };
}

async function checkUpdate(force) {
  try {
    state.update = { ...(state.update || {}), checking: true }; renderUpdateBox();
    const u = await api('update' + (force ? '?check=1' : ''));
    state.update = u; renderUpdateBox();
    if (force) {
      if (u.available) toast(`Update auf v${u.latest} verfügbar.`);
      else if (u.enabled === false) toast(u.error || 'Updates sind hier deaktiviert.');
      else if (u.error) toast('Prüfung fehlgeschlagen: ' + u.error, true);
      else toast(`Du hast die neueste Version (v${u.current}).`);
    }
  } catch (e) { state.update = { error: e.message }; renderUpdateBox(); if (force) toast(e.message, true); }
}

function renderUpdate() {
  const u = state.update || {};
  const running = state.servers.some((s) => s.running);
  return `
  <div class="head"><div><h1>Update auf Version ${esc(u.latest)}</h1>
    <div class="head-sub">Installiert: v${esc(u.current)}${u.published ? ' · veröffentlicht ' + esc(String(u.published).slice(0, 10)) : ''}</div></div>
    <button class="btn btn-sm" id="updCancel">Später</button></div>
  <div class="page">
    <div class="card"><h3 style="margin-top:0">Was ist neu</h3><pre class="notes">${esc(u.notes || 'Keine Beschreibung hinterlegt.')}</pre>
      ${u.html_url ? `<p class="small mb0"><a href="${esc(u.html_url)}" target="_blank" rel="noopener noreferrer">Release auf GitHub ansehen</a></p>` : ''}</div>
    <div class="note note-info">Das Update lädt das neue Programmpaket, sichert die aktuelle Version unter <code>data\\backup</code>,
      ersetzt die Programmdateien und startet den Manager neu. <b>Welten, Einstellungen und Server bleiben unverändert.</b></div>
    ${running ? '<div class="note note-warn">Bitte zuerst alle Server stoppen – beim Update wird der Manager neu gestartet.</div>' : ''}
    <div class="btn-row"><button class="btn btn-primary" id="updApply" ${running ? 'disabled' : ''}>⬆ Jetzt aktualisieren</button></div>
  </div>`;
}

function bindUpdate() {
  $('#updCancel').onclick = () => { state.view = state.servers.length ? 'server' : 'welcome'; if (!state.activeId && state.servers[0]) state.activeId = state.servers[0].id; render(); };
  const b = $('#updApply');
  if (b) b.onclick = async () => {
    b.disabled = true;
    try {
      const d = await api('update/apply', { method: 'POST' });
      state.install = { serverId: 'update', jobId: d.job_id, job: null, after: 'restart' };
      state.view = 'install'; render(); pollJob();
    } catch (e) { toast(e.message, true); b.disabled = false; }
  };
}

function pmHtml(d, removed = false) {
  const ok = d.results.filter((r) => r.ok).length, total = d.results.length;
  const errors = [...new Set(d.results.filter((r) => !r.ok).map((r) => r.error))];
  let html = d.ok
    ? `<div class="note note-ok" style="margin:0">${removed ? 'Alle Freigaben entfernt.' : `Alle ${total} Freigaben aktiv${d.external_ip ? ' – öffentliche IP <b>' + esc(d.external_ip) + '</b>' : ''}.`}</div>`
    : `<div class="note note-err" style="margin:0"><b>${ok} von ${total} ${removed ? 'entfernt' : 'angelegt'}.</b> ${errors.map((e) => esc(e)).join('<br>')}</div>`;
  html += `<div class="muted small" style="margin-top:6px">${d.results.map((r) => `${r.ok ? '✓' : '✗'} ${r.proto} ${r.port}`).join(' · ')}</div>`;
  if (d.hint) html += `<div class="note note-warn" style="margin:10px 0 0"><b>Kein öffentliches IPv4:</b> ${esc(d.hint)}</div>`;
  return html;
}

/* ------------------------------------------------------------------ Ansichten */

function render() {
  const view = $('#view');
  stopConsolePolling();
  clearInterval(state.timers.xbox);
  if (state.view !== 'cloud') stopCloudPolling();
  switch (state.view) {
    case 'wizard': view.innerHTML = renderWizard(); bindWizard(); break;
    case 'install': view.innerHTML = renderInstall(); break;
    case 'server': view.innerHTML = renderServer(); bindServer(); break;
    case 'help': view.innerHTML = renderHelp(); break;
    case 'xbox': view.innerHTML = renderXboxWizard(); bindXboxWizard(); break;
    case 'update': view.innerHTML = renderUpdate(); bindUpdate(); break;
    case 'cloud': view.innerHTML = renderCloud(); bindCloud(); break;
    default: view.innerHTML = renderWelcome(); bindWelcome();
  }
  renderSidebar();
  $('#main').scrollTop = 0;
}

function renderWelcome() {
  return `
  <div class="page">
    <div class="empty">
      <img src="logo.svg" class="logo-big" alt="">
      <h1>Willkommen beim Server Manager</h1>
      <p class="lead">Richte in wenigen Minuten einen Minecraft-Server ein – für Xbox, PlayStation,
         Switch, Handy und PC, nur für Java-Spieler oder als Modpack von Modrinth. Alles Nötige wird automatisch geladen.</p>
      <div class="spacer"></div>
      <button class="btn btn-primary" id="welcomeNew">+ Ersten Server anlegen</button>
    </div>
    <div class="grid3">
      <div class="card"><div class="pill pill-green">1</div><h3>Typ &amp; Version wählen</h3>
        <p class="muted small">Bedrock für Konsolen, Java (nur Java oder mit Crossplay) oder ein Modpack – die Versionen kommen direkt von Mojang, PaperMC und Modrinth.</p></div>
      <div class="card"><div class="pill pill-green">2</div><h3>Einstellen</h3>
        <p class="muted small">Name, Spieleranzahl, RAM, Port, Spielmodus – alles in einem Formular, jederzeit änderbar.</p></div>
      <div class="card"><div class="pill pill-green">3</div><h3>Verbinden</h3>
        <p class="muted small">IP und Port werden angezeigt, dazu der Xbox-Freunde-Modus und Anleitungen für FritzBox und Firewall.</p></div>
    </div>
  </div>`;
}
function bindWelcome() { $('#welcomeNew').onclick = startWizard; }

/* ------------------------------------------------------------------ Assistent: Neuer Server */

function startWizard() {
  state.wizard = {
    // kind = gewählte Kachel: bedrock | java (nur Java) | crossplay (Paper + Geyser) | modpack (Modrinth)
    step: 0, kind: 'bedrock', type: 'bedrock', flavor: 'paper', modpack: null, version: '', versionMode: 'latest',
    name: 'Mein Server', motd: 'Willkommen auf meinem Server', port: 19132, bedrock_port: 19132,
    max_players: 10, ram_mb: 4096, gamemode: 'survival', difficulty: 'easy', view_distance: 10,
    level_seed: '', public_address: '', online_mode: true, allow_cheats: false, pvp: true, geyser: true, hardcore: false, eula_accepted: false,
    mp: { query: '', results: null, total: 0, busy: false, error: '', project: null, versions: null, versionsError: '' },
  };
  state.view = 'wizard';
  render();
  loadVersions('bedrock');
}

/* Die Kachel im ersten Schritt legt Typ, Crossplay und Modpack-Modus zusammen fest. */
function applyWizardKind(w, kind) {
  w.kind = kind;
  w.type = kind === 'bedrock' ? 'bedrock' : 'java';
  w.geyser = kind === 'crossplay';
  w.flavor = 'paper';
  w.modpack = null;
  w.port = kind === 'bedrock' ? 19132 : 25565;
  w.ram_mb = kind === 'modpack' ? 6144 : 4096;
}

/* Wizard-Daten für POST /api/servers – ohne den Suchzustand des Modpack-Schritts. */
function wizardPayload(w) {
  const { mp, kind, ...body } = w;
  return body;
}

async function loadVersions(kind) {
  // Fehler werden nicht als Ergebnis gemerkt – jeder weitere Aufruf versucht es erneut.
  if (state.versions[kind] && !state.versions[kind].error) return;
  state.versions[kind] = null;
  try {
    const data = await api('versions?type=' + kind);
    state.versions[kind] = kind === 'java' ? data.versions : data.bedrock;
  } catch (e) {
    state.versions[kind] = { error: e.message };
  }
  if (state.view === 'wizard' && state.wizard && state.wizard.step === 1) render();
}

const stepNames = (w) => ['Server-Typ', w.kind === 'modpack' ? 'Modpack wählen' : 'Version', 'Einstellungen', 'Fertigstellen'];

function stepBar(names, step) {
  return `<div class="steplist" style="display:flex;gap:6px;margin-top:0">${names.map((n, i) => `
    <div class="steprow ${i < step ? 'done' : i === step ? 'active' : ''}" style="flex:1">
      <span class="mark">${i < step ? '✓' : i + 1}</span> ${n}</div>`).join('')}</div>`;
}

function renderWizard() {
  const w = state.wizard;
  let body = '';
  if (w.step === 0) body = wizardStepType(w);
  else if (w.step === 1) body = wizardStepVersion(w);
  else if (w.step === 2) body = wizardStepSettings(w);
  else body = wizardStepFinish(w);

  const names = stepNames(w);
  return `
  <div class="head"><div><h1>Neuer Server</h1><div class="head-sub">Schritt ${w.step + 1} von 4 · ${names[w.step]}</div></div>
    <button class="btn btn-sm" id="wzCancel">Abbrechen</button></div>
  <div class="page">
    ${stepBar(names, w.step)}
    ${body}
    <div class="wizard-nav">
      <button class="btn" id="wzBack" ${w.step === 0 ? 'disabled' : ''}>← Zurück</button>
      ${w.step < 3
        ? '<button class="btn btn-primary" id="wzNext">Weiter →</button>'
        : '<button class="btn btn-primary" id="wzInstall">Server jetzt einrichten</button>'}
    </div>
  </div>`;
}

function wizardStepType(w) {
  return `
  <p class="lead">Wer soll auf den Server kommen? Das entscheidet über den Server-Typ.</p>
  <div class="grid2">
    <button class="choice ${w.kind === 'bedrock' ? 'sel' : ''}" data-kind="bedrock">
      <span class="pill pill-green">Empfohlen für Konsolen</span>
      <h3>Bedrock Dedicated Server</h3>
      <p>Offizieller Server von Mojang. Für <b>Xbox, PS4/PS5, Switch, Handy</b> und die Windows-App.
         Keine Java-Spieler.</p>
    </button>
    <button class="choice ${w.kind === 'java' ? 'sel' : ''}" data-kind="java">
      <span class="pill pill-grey">Nur PC</span>
      <h3>Java Edition (nur Java-Spieler)</h3>
      <p>Paper-Server ohne Crossplay – für Runden, in denen <b>alle die PC-Java-Version</b> haben.
         Plugins möglich, keine Konsolen.</p>
    </button>
    <button class="choice ${w.kind === 'crossplay' ? 'sel' : ''}" data-kind="crossplay">
      <span class="pill pill-blue">Crossplay</span>
      <h3>Java + Crossplay (Paper + Geyser)</h3>
      <p>Paper-Server mit Geyser &amp; Floodgate. <b>Java-Spieler und Konsolen</b> spielen gemeinsam.
         Braucht etwas mehr RAM.</p>
    </button>
    <button class="choice ${w.kind === 'modpack' ? 'sel' : ''}" data-kind="modpack">
      <span class="pill pill-amber">Mods</span>
      <h3>Modpack (Modrinth)</h3>
      <p>Fertiges Modpack von <b>modrinth.com</b> mit Fabric, NeoForge, Forge oder Quilt. Nur Java-Spieler
         mit <b>demselben Modpack</b>. Braucht viel RAM (ab 4–6 GB).</p>
    </button>
  </div>
  <div class="note note-info">Unsicher? Unter <b>Hilfe → Bedrock &amp; Crossplay</b> in der Seitenleiste sind die vier Arten erklärt.
    Der Typ lässt sich später nicht ändern, alle anderen Einstellungen schon.</div>`;
}

/* ---------- Schritt „Modpack wählen“ (Modrinth-Suche → Pack-Version) */

function wizardStepModpack(w) {
  const mp = w.mp;
  const sel = w.modpack;
  let results = '';
  if (mp.busy) results = '<div class="steprow active" style="padding:10px"><span class="mark"></span> Modrinth wird durchsucht …</div>';
  else if (mp.error) results = `<div class="note note-err" style="margin:8px 0">${esc(mp.error)}</div>`;
  else if (Array.isArray(mp.results)) {
    results = mp.results.length ? mp.results.map((h) => `
      <div class="mp-row ${mp.project && mp.project.project_id === h.project_id ? 'active' : ''}" data-mp-project="${esc(h.project_id)}">
        ${h.icon_url ? `<img class="mp-icon" src="${esc(h.icon_url)}" alt="" loading="lazy">` : '<div class="mp-icon mp-icon-empty">🧩</div>'}
        <div class="mp-body">
          <div class="mp-title">${esc(h.title)}</div>
          <div class="mp-desc">${esc(h.description)}</div>
          <div class="mp-meta">⬇ ${fmtCount(h.downloads)} · ${h.loaders.map((l) => LOADER_NAMES[l] || l).join(', ') || 'Loader ?'} · MC ${esc(h.versions.slice(-3).join(', ') || '?')}</div>
        </div>
      </div>`).join('') : '<div class="muted small" style="padding:10px">Nichts gefunden – anderen Suchbegriff probieren.</div>';
    if (mp.total > mp.results.length) results += `<div class="muted small" style="padding:8px 10px">${mp.total} Treffer – die beliebtesten ${mp.results.length} werden gezeigt. Suchbegriff eingrenzen, um andere zu finden.</div>`;
  } else results = '<div class="muted small" style="padding:10px">Tippe einen Namen ein oder lass das Feld leer und drücke Enter für die beliebtesten Packs.</div>';

  let versions = '';
  if (mp.project) {
    const vs = mp.versions;
    if (!vs && !mp.versionsError) versions = '<div class="steprow active" style="padding:10px"><span class="mark"></span> Versionen werden geladen …</div>';
    else if (mp.versionsError) versions = `<div class="note note-err" style="margin:8px 0">${esc(mp.versionsError)}</div>`;
    else if (!vs.length) versions = '<div class="note note-warn" style="margin:8px 0">Dieses Pack hat keine Server-Version im .mrpack-Format – bitte ein anderes wählen.</div>';
    else versions = vs.map((v) => `
      <div class="mp-row mp-version ${sel && sel.version_id === v.id ? 'active' : ''}" data-mp-version="${esc(v.id)}">
        <div class="mp-body">
          <div class="mp-title">${esc(v.name)} ${v.type !== 'release' ? `<span class="pill pill-grey">${esc(v.type)}</span>` : ''}</div>
          <div class="mp-meta">Minecraft <b>${esc(v.mc_version || '?')}</b> · ${v.loaders.map((l) => LOADER_NAMES[l] || l).join(', ')} · ${fmtBytes(v.size)} · ${esc(v.date)}</div>
        </div>
      </div>`).join('');
  }

  return `
  <p class="lead">Suche ein Modpack auf Modrinth und wähle die Pack-Version. Der Manager installiert Loader und Mods automatisch.</p>
  <div class="grid2" style="margin-top:0">
    <div>
      <div class="field"><label for="wzMpQuery">Modpack suchen</label>
        <div class="flex"><input type="text" id="wzMpQuery" placeholder="z. B. Cobblemon, Create, Better MC …" value="${esc(mp.query)}" autocomplete="off">
          <button class="btn btn-sm" type="button" id="wzMpSearch">Suchen</button></div>
        <div class="hint">Nur Packs, die auf einem Server laufen; sortiert nach Downloads.</div></div>
      <div class="mp-list" id="wzMpResults">${results}</div>
    </div>
    <div>
      <div class="field"><label>Pack-Version${mp.project ? ` · ${esc(mp.project.title)}` : ''}</label>
        <div class="mp-list" id="wzMpVersions">${mp.project ? versions : '<div class="muted small" style="padding:10px">Zuerst links ein Modpack wählen.</div>'}</div></div>
      ${sel ? `<div class="note note-ok" style="margin:10px 0 0"><b>Gewählt:</b> ${esc(sel.title)} – ${esc(sel.version_name)}<br>
        <span class="small">Minecraft ${esc(sel.mc_version)} · ${LOADER_NAMES[sel.loader] || esc(sel.loader)} · ${sel.url ? `<a href="${esc(sel.url)}" target="_blank" rel="noopener noreferrer">auf Modrinth ansehen</a>` : ''}</span></div>` : ''}
    </div>
  </div>
  <div class="note note-warn"><b>Gut zu wissen:</b> Modpacks brauchen viel Arbeitsspeicher – empfohlen sind <b>6 GB</b>, mindestens 4 GB (nächster Schritt).
    <b>Bedrock-Crossplay (Geyser)</b> und Bukkit-Plugins wie MCSMCompanion gibt es für Modpacks nicht – Fabric/NeoForge laden keine Bukkit-Plugins.
    Mitspieler brauchen dasselbe Modpack in ihrem Launcher (z.&nbsp;B. Modrinth App).</div>`;
}

async function modpackSearch(w) {
  const mp = w.mp;
  mp.busy = true; mp.error = ''; render();
  try {
    const d = await api('modpacks/search?q=' + encodeURIComponent(mp.query) + '&page=0');
    mp.results = d.hits; mp.total = d.total;
  } catch (e) { mp.error = e.message; mp.results = null; }
  mp.busy = false;
  if (state.view === 'wizard' && state.wizard === w && w.step === 1) render();
}

async function modpackVersions(w, project) {
  const mp = w.mp;
  mp.project = project; mp.versions = null; mp.versionsError = ''; w.modpack = null; render();
  try {
    mp.versions = await api(`modpacks/${encodeURIComponent(project.project_id)}/versions`);
  } catch (e) { mp.versionsError = e.message; mp.versions = []; }
  if (state.view === 'wizard' && state.wizard === w && w.step === 1 && mp.project === project) render();
}

function wizardStepVersion(w) {
  if (w.kind === 'modpack') return wizardStepModpack(w);
  const v = state.versions[w.type];
  if (!v) return '<div class="card"><div class="steprow active"><span class="mark"></span> Versionen werden geladen …</div></div>';
  if (v.error) return `<div class="note note-err"><b>Versionen konnten nicht geladen werden.</b><p>${esc(v.error)}</p>
     <p class="mb0">Bitte Internetverbindung prüfen und <a href="#" id="wzRetry">erneut versuchen</a>.</p></div>`;

  if (w.type === 'bedrock') {
    if (!w.version) w.version = v.latest;
    return `
    <p class="lead">Die Bedrock-Version sollte zur Version deiner Spieler passen. Konsolen aktualisieren
       automatisch – <b>„Aktuell“</b> ist deshalb fast immer richtig.</p>
    <div class="grid2">
      <button class="choice ${w.versionMode === 'latest' ? 'sel' : ''}" data-vmode="latest">
        <span class="pill pill-green">Empfohlen</span><h3>Aktuell · ${esc(v.latest || '?')}</h3>
        <p>Die Version, die Xbox, PlayStation und Handy gerade nutzen.</p></button>
      <button class="choice ${w.versionMode === 'preview' ? 'sel' : ''}" data-vmode="preview">
        <span class="pill pill-grey">Beta</span><h3>Preview · ${esc(v.preview || '?')}</h3>
        <p>Nur für Spieler mit der Preview-/Beta-App. Kann instabil sein.</p></button>
    </div>
    <div class="choice ${w.versionMode === 'custom' ? 'sel' : ''}" data-vmode="custom" role="button" tabindex="0">
      <h3>Andere Version</h3><p>Ältere Version manuell eingeben, z.&nbsp;B. <code>1.21.44.01</code>.
      Sie muss exakt so heißen wie bei Mojang veröffentlicht.</p>
      <div class="field" style="margin:12px 0 0">
        <input type="text" id="wzCustomVersion" placeholder="1.21.44.01" value="${esc(w.versionMode === 'custom' ? w.version : '')}"></div>
    </div>`;
  }

  const list = v;
  if (!w.version) w.version = list[0] || '';
  return `
  <p class="lead">Java-Spieler müssen die gleiche Version wie der Server haben.${w.geyser ? ' Bedrock-Spieler (über Crossplay) kommen mit jeder aktuellen Version.' : ''}</p>
  <div class="field"><label>Minecraft-Version (Paper)</label>
    <select id="wzJavaVersion">${list.map((x) => `<option ${x === w.version ? 'selected' : ''}>${esc(x)}</option>`).join('')}</select>
    <div class="hint">Die passende Java-Laufzeit (z.&nbsp;B. Java 25 für 26.x, Java 21 für 1.21) wird automatisch mitgeladen – nichts muss installiert werden.</div></div>
  ${w.geyser ? `<div class="note note-info mb0">Für Crossplay wird die <b>neueste Version</b> empfohlen: Geyser unterstützt
    immer die aktuellste Java-Version am besten.</div>`
    : '<div class="note note-info mb0">Für einen reinen Java-Server ist die <b>neueste Version</b> meist richtig – Spieler mit älterem Launcher wählen dort einfach dieselbe Version.</div>'}`;
}

function fieldInput(id, label, value, { type = 'text', hint = '', min, max, step, placeholder = '' } = {}) {
  const attrs = [min !== undefined ? `min="${min}"` : '', max !== undefined ? `max="${max}"` : '', step ? `step="${step}"` : ''].join(' ');
  return `<div class="field"><label for="${id}">${label}</label>
    <input type="${type}" id="${id}" value="${esc(value)}" placeholder="${esc(placeholder)}" ${attrs}>
    ${hint ? `<div class="hint">${hint}</div>` : ''}</div>`;
}
function fieldSelect(id, label, value, options, hint = '') {
  return `<div class="field"><label for="${id}">${label}</label>
    <select id="${id}">${options.map(([v, t]) => `<option value="${v}" ${v === value ? 'selected' : ''}>${t}</option>`).join('')}</select>
    ${hint ? `<div class="hint">${hint}</div>` : ''}</div>`;
}
function fieldCheck(id, title, desc, checked) {
  return `<label class="check"><input type="checkbox" id="${id}" ${checked ? 'checked' : ''}>
    <span><div class="t">${title}</div><div class="d">${desc}</div></span></label>`;
}

function settingsForm(s, prefix) {
  const java = s.type === 'java';
  const modpack = isModpack(s);
  const crossplay = java && !modpack && s.geyser !== false;
  const ramHint = modpack ? 'Modpacks brauchen viel RAM: 6 GB empfohlen, mindestens 4 GB. Nicht mehr als die Hälfte deines PC-RAMs.'
    : '2–4 GB reichen für kleine Runden. Für Crossplay mindestens 3 GB. Nicht mehr als die Hälfte deines PC-RAMs.';
  return `
  <div class="grid2" style="margin-top:0">
    <div>
      ${fieldInput(prefix + 'name', 'Name des Servers', s.name, { hint: 'Nur für dich in dieser Liste.' })}
      ${fieldInput(prefix + 'motd', 'MOTD – Text in der Serverliste', s.motd, { hint: 'Das sehen Spieler, wenn sie den Server in ihrer Liste haben.' })}
      ${fieldInput(prefix + 'public_address', 'Öffentliche Adresse / MyFRITZ! (optional)', s.public_address || '',
        { placeholder: 'z. B. abc123.myfritz.net', hint: 'Wird im Dashboard als Adresse für Freunde angezeigt. Leer = öffentliche IP automatisch von der FritzBox ermitteln.' })}
      ${fieldInput(prefix + 'port', java ? 'Port für Java (TCP)' : 'Port (TCP, Verbindungsaufbau)', s.port,
        { type: 'number', min: 1024, max: 65535, hint: java ? 'Standard: 25565' : 'Standard: 19132 – Konsolen erwarten diesen Port.' })}
      ${crossplay ? fieldInput(prefix + 'bedrock_port', 'Port für Bedrock / Konsolen (UDP)', s.bedrock_port,
        { type: 'number', min: 1024, max: 65535, hint: 'Standard: 19132' }) : ''}
      ${fieldInput(prefix + 'max_players', 'Maximale Spieler', s.max_players, { type: 'number', min: 1, max: 200,
        hint: java ? '' : 'Bestimmt auch die Größe des UDP-Bereichs für Spieldaten – nach einer Änderung Firewall-Regel und FritzBox-Freigabe neu anlegen (Tab „Verbinden“).' })}
      ${java ? '' : `<div class="field"><label for="${prefix}public_ip">Öffentliche IPv4 für Freunde aus dem Internet (optional)</label>
        <div class="flex"><input type="text" id="${prefix}public_ip" value="${esc(s.public_ip || '')}" placeholder="leer = beim Start automatisch vom Router (UPnP)">
          <button class="btn btn-sm" type="button" id="${prefix}pubip">IP ermitteln</button></div>
        <div class="hint">Bedrock (NetherNet) bietet Spielern nur die Adressen an, die es kennt – ohne öffentliche IPv4 erreichen Freunde aus dem Internet den Server trotz Portfreigabe nicht.
          Leer lassen, wenn deine FritzBox die IP per UPnP meldet; sonst hier eintragen (ändert sich die IP, neu eintragen). Bei DS-Lite gibt es keine eigene IPv4.</div></div>`}
      ${java ? `<div class="field"><label for="${prefix}ram_mb">Arbeitsspeicher: <b id="${prefix}ramLabel">${gb(s.ram_mb)}</b></label>
        <input type="range" id="${prefix}ram_mb" min="${modpack ? 4096 : 1024}" max="${modpack ? 32768 : 16384}" step="512" value="${s.ram_mb}">
        <div class="hint">${ramHint}</div></div>` : ''}
    </div>
    <div>
      ${fieldSelect(prefix + 'gamemode', 'Spielmodus', s.gamemode, [['survival', 'Überleben'], ['creative', 'Kreativ'], ['adventure', 'Abenteuer']])}
      ${fieldSelect(prefix + 'difficulty', 'Schwierigkeit', s.difficulty, [['peaceful', 'Friedlich'], ['easy', 'Leicht'], ['normal', 'Normal'], ['hard', 'Schwer']])}
      <div class="field"><label for="${prefix}view_distance">Sichtweite: <b id="${prefix}vdLabel">${s.view_distance} Chunks</b></label>
        <input type="range" id="${prefix}view_distance" min="4" max="32" value="${s.view_distance}">
        <div class="hint">Höher = schöner, aber mehr Last. 8–12 ist ein guter Wert.</div></div>
      ${fieldInput(prefix + 'level_seed', 'Welt-Seed (optional)', s.level_seed, { placeholder: 'leer = zufällig', hint: 'Nur für neue Welten wirksam.' })}
      ${bedrockPlayers(s) ? fieldCheck(prefix + 'online_mode', 'Xbox-Konto erforderlich (online-mode)',
        'Empfohlen. Spieler werden bei Xbox Live geprüft – so kann sich niemand als jemand anderes ausgeben.', s.online_mode)
        : fieldCheck(prefix + 'online_mode', 'Konto-Prüfung (online-mode)',
        'Empfohlen. Java-Spieler werden bei Mojang geprüft – nur mit gekauftem Spiel und echtem Namen.', s.online_mode)}
      ${fieldCheck(prefix + 'allow_cheats', java ? 'Befehlsblöcke erlauben' : 'Cheats erlauben',
        java ? 'Aktiviert Befehlsblöcke auf dem Server.' : 'Spieler mit Operator-Rechten dürfen Befehle wie /gamemode nutzen.', s.allow_cheats)}
      ${java ? fieldCheck(prefix + 'pvp', 'PvP', 'Spieler können sich gegenseitig verletzen.', s.pvp) : ''}
      ${java && !modpack ? fieldCheck(prefix + 'geyser', 'Bedrock-Crossplay (Geyser + Floodgate)',
        'Konsolen und Handys können beitreten.' + (prefix === 'st_' ? ' Nach dem Einschalten unten „Neu installieren“ wählen – erst dann werden Geyser und Floodgate geladen.' : ''), s.geyser) : ''}
      ${modpack ? '<div class="note note-info" style="margin:0 0 10px"><b>Kein Crossplay bei Modpacks:</b> Geyser/Floodgate und Bukkit-Plugins laufen nicht auf Fabric, NeoForge, Forge oder Quilt.</div>' : ''}
      ${java && !modpack ? fieldCheck(prefix + 'hardcore', 'MCSM-Hardcore (1 Leben, Grab &amp; Totem)',
        'Kein Vanilla-Hardcore: Wer stirbt, wird sofort Zuschauer an seinem Grab. Ein Mitspieler belebt ihn wieder, indem er ein Totem der Unsterblichkeit am Grab rechtsklickt oder darauf fallen lässt. Im Spiel schalten OPs mit <code>/hardcore on</code> / <code>off</code>.', !!s.hardcore) : ''}
    </div>
  </div>`;
}

function readSettingsForm(prefix, base) {
  const get = (k) => $('#' + prefix + k);
  const out = { ...base };
  for (const k of ['name', 'motd', 'level_seed', 'gamemode', 'difficulty', 'public_ip', 'public_address']) if (get(k)) out[k] = get(k).value;
  for (const k of ['port', 'bedrock_port', 'max_players', 'ram_mb', 'view_distance']) if (get(k)) out[k] = Number(get(k).value);
  for (const k of ['online_mode', 'allow_cheats', 'pvp', 'geyser', 'hardcore']) if (get(k)) out[k] = get(k).checked;
  return out;
}

function bindSettingsForm(prefix) {
  const ram = $('#' + prefix + 'ram_mb');
  if (ram) ram.oninput = () => $('#' + prefix + 'ramLabel').textContent = gb(Number(ram.value));
  const vd = $('#' + prefix + 'view_distance');
  if (vd) vd.oninput = () => $('#' + prefix + 'vdLabel').textContent = vd.value + ' Chunks';
  const pb = $('#' + prefix + 'pubip');
  if (pb) pb.onclick = async () => {
    pb.disabled = true;
    try { const d = await api('publicip'); $('#' + prefix + 'public_ip').value = d.ip; state.publicIp = d.ip; }
    catch (e) { toast(e.message, true); } finally { pb.disabled = false; }
  };
}

function wizardStepSettings(w) {
  return `<p class="lead">Alles hier lässt sich später unter <b>Einstellungen</b> wieder ändern.</p>${settingsForm(w, 'wz_')}`;
}

function wizardStepFinish(w) {
  const mp = w.modpack;
  const rows = [
    ['Typ', typeLabel(w)],
    ...(mp ? [['Modpack', `${mp.title} – ${mp.version_name}`], ['Loader', `${LOADER_NAMES[mp.loader] || mp.loader} · Minecraft ${mp.mc_version}`]] : [['Version', w.version]]),
    ['Name', w.name], ['MOTD', w.motd],
    ['Port', w.type === 'bedrock' ? `${w.port} (TCP) + UDP-Bereich für Spieldaten` : `${w.port} (TCP)` + (w.geyser ? ` · Bedrock ${w.bedrock_port} (UDP)` : '')],
    ['Spieler', w.max_players], ...(w.type === 'java' ? [['RAM', gb(w.ram_mb)]] : []),
    ['Modus', `${GM[w.gamemode] || w.gamemode} · ${DF[w.difficulty] || w.difficulty}`],
  ];
  return `
  <div class="card"><h3 style="margin-top:0">Zusammenfassung</h3>
    <table class="table">${rows.map(([k, v]) => `<tr><td class="muted" style="width:160px">${k}</td><td><b>${esc(v)}</b></td></tr>`).join('')}</table></div>
  <div class="spacer"></div>
  <div class="note note-warn">
    <b>Minecraft-Nutzungsbedingungen</b>
    <p>Der Server wird direkt von ${mp ? 'Mojang, den Loader-Projekten und Modrinth' : 'Mojang bzw. PaperMC'} heruntergeladen. Dafür musst du die
       <a href="${EULA_URL}" target="_blank" rel="noopener noreferrer">Minecraft-EULA</a> und die
       <a href="${PRIVACY_URL}" target="_blank" rel="noopener noreferrer">Datenschutzbestimmungen von Microsoft</a> akzeptieren.</p>
    ${fieldCheck('wz_eula', 'Ich akzeptiere die Minecraft-EULA', 'Ohne diese Zustimmung darf der Server nicht betrieben werden.', w.eula_accepted)}
  </div>
  <div class="note note-info mb0">${mp ? 'Modpack, Mod-Loader und alle Mods werden geladen – je nach Pack <b>einige hundert MB</b>, das dauert ein paar Minuten.'
    : 'Es werden je nach Typ <b>40–150 MB</b> heruntergeladen.'} Der Server startet danach
     noch nicht automatisch – du startest ihn selbst auf der Übersichtsseite.</div>`;
}

function bindWizard() {
  const w = state.wizard;
  $('#wzCancel').onclick = () => { state.view = state.servers.length ? 'server' : 'welcome'; if (!state.activeId && state.servers[0]) state.activeId = state.servers[0].id; render(); };
  $('#wzBack').onclick = () => { collectWizard(); w.step = Math.max(0, w.step - 1); render(); };
  const next = $('#wzNext');
  if (next) next.onclick = () => {
    if (!collectWizard()) return;
    if (w.step === 0) {
      // Ein bereits gewähltes Modpack behält seine Minecraft-Version – sonst blockiert „Weiter“ nach „Zurück“.
      w.version = (w.kind === 'modpack' && w.modpack) ? w.modpack.mc_version : ''; w.versionMode = 'latest';
      if (w.kind === 'modpack') { if (w.mp.results === null && !w.mp.busy) modpackSearch(w); }
      else loadVersions(w.type);
    }
    if (w.step === 1 && w.kind === 'modpack' && !w.modpack) { toast('Bitte ein Modpack und eine Pack-Version wählen.', true); return; }
    if (w.step === 1 && !w.version) { toast('Bitte eine Version wählen.', true); return; }
    w.step++; render();
  };
  const install = $('#wzInstall');
  if (install) install.onclick = async () => {
    w.eula_accepted = $('#wz_eula').checked;
    if (!w.eula_accepted) { toast('Bitte zuerst die Minecraft-EULA bestätigen.', true); return; }
    install.disabled = true;
    try {
      const data = await api('servers', { method: 'POST', body: wizardPayload(w) });
      state.install = { serverId: data.server.id, jobId: data.job_id, job: null, after: 'overview' };
      state.activeId = data.server.id;
      state.view = 'install';
      await refresh();
      render();
      pollJob();
    } catch (e) { toast(e.message, true); install.disabled = false; }
  };

  $$('[data-kind]').forEach((el) => el.onclick = () => { if (el.dataset.kind !== w.kind) applyWizardKind(w, el.dataset.kind); render(); });

  // Modpack-Schritt: Suche auf Enter/Klick (Modrinth erlaubt 300 Anfragen/Minute – keine Suche bei jedem Tastendruck).
  const mq = $('#wzMpQuery');
  if (mq) {
    mq.oninput = () => { w.mp.query = mq.value; };
    mq.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); w.mp.query = mq.value.trim(); modpackSearch(w); } };
    $('#wzMpSearch').onclick = () => { w.mp.query = mq.value.trim(); modpackSearch(w); };
    if (document.activeElement !== mq && !w.mp.project) { mq.focus(); mq.setSelectionRange(mq.value.length, mq.value.length); }
  }
  $$('[data-mp-project]').forEach((el) => el.onclick = () => {
    const hit = (w.mp.results || []).find((h) => h.project_id === el.dataset.mpProject);
    if (hit) modpackVersions(w, hit);
  });
  $$('[data-mp-version]').forEach((el) => el.onclick = () => {
    const v = (w.mp.versions || []).find((x) => x.id === el.dataset.mpVersion), p = w.mp.project;
    if (!v || !p) return;
    w.modpack = { project_id: p.project_id, title: p.title, version_id: v.id, version_name: v.name, mc_version: v.mc_version,
      loader: v.loaders[0], loader_version: '', icon_url: p.icon_url || '', url: p.url || '' };
    w.flavor = v.loaders[0]; w.version = v.mc_version; w.geyser = false;
    render();
  });
  $$('[data-vmode]').forEach((el) => el.onclick = (ev) => {
    if (ev.target.tagName === 'INPUT') return;
    w.versionMode = el.dataset.vmode;
    const v = state.versions.bedrock || {};
    w.version = w.versionMode === 'latest' ? v.latest : w.versionMode === 'preview' ? v.preview : ($('#wzCustomVersion')?.value || '');
    render();
    if (w.versionMode === 'custom') $('#wzCustomVersion')?.focus();
  });
  const custom = $('#wzCustomVersion');
  if (custom) custom.oninput = () => { w.versionMode = 'custom'; w.version = custom.value.trim(); $$('[data-vmode]').forEach((el) => el.classList.toggle('sel', el.dataset.vmode === 'custom')); };
  const jv = $('#wzJavaVersion');
  if (jv) jv.onchange = () => w.version = jv.value;
  const retry = $('#wzRetry');
  if (retry) retry.onclick = (e) => { e.preventDefault(); state.versions[w.type] = null; render(); loadVersions(w.type); };
  if (w.step === 2) bindSettingsForm('wz_');
}

function collectWizard() {
  const w = state.wizard;
  if (w.step === 2) {
    Object.assign(w, readSettingsForm('wz_', w));
    if (w.kind === 'modpack') w.geyser = false;
    if (!w.name.trim()) { toast('Bitte einen Namen eingeben.', true); return false; }
    if (w.type === 'java' && w.geyser && w.port === w.bedrock_port) { toast('Java-Port und Bedrock-Port müssen sich unterscheiden.', true); return false; }
  }
  if (w.step === 1 && w.kind === 'modpack') { const mq = $('#wzMpQuery'); if (mq) w.mp.query = mq.value; }
  if (w.step === 1 && w.type === 'bedrock' && w.versionMode === 'custom') w.version = ($('#wzCustomVersion')?.value || '').trim();
  return true;
}

/* ------------------------------------------------------------------ Installation / Vorgänge */

function renderInstall() {
  const inst = state.install, job = inst.job, s = state.servers.find((x) => x.id === inst.serverId);
  const steps = job ? job.steps.map((st) => `<div class="steprow ${st.state}"><span class="mark">${st.state === 'done' ? '✓' : st.state === 'failed' ? '!' : ''}</span>${esc(st.text)}</div>`).join('') : '';
  const done = job && job.status === 'done', failed = job && job.status === 'error';
  const starting = inst.after === 'console', updating = inst.after === 'restart';
  return `
  <div class="head"><div><h1>${updating ? 'Update wird installiert' : esc(s ? s.name : 'Server') + (starting ? ' wird gestartet' : ' wird eingerichtet')}</h1><div class="head-sub">${s ? typeLabel(s) + ' · ' + esc(s.version) : ''}</div></div></div>
  <div class="page">
    <div class="card">
      <div class="bar"><i style="width:${job ? job.pct : 0}%"></i></div>
      <div class="muted small" id="jobDetail">${esc(job ? job.detail : 'Wird gestartet …')}</div>
      <div class="steplist">${steps}</div>
      ${failed ? `<div class="note note-err"><b>${starting ? 'Der Start' : 'Die Einrichtung'} ist fehlgeschlagen.</b><p>${esc(job.error)}</p>
          <p class="mb0">Du kannst es erneut versuchen – bereits geladene Dateien werden wiederverwendet.</p></div>` : ''}
      ${done ? `<div class="note note-ok"><b>Fertig!</b> ${updating ? 'Der Manager startet jetzt neu – das neue Fenster öffnet sich in wenigen Sekunden. Dieses Fenster kann geschlossen werden.' : starting ? 'Der Server läuft – die Konsole öffnet sich gleich.' : 'Der Server ist eingerichtet und kann gestartet werden.'}</div>` : ''}
      <div class="btn-row" style="margin-top:14px">
        ${updating ? '' : (done || failed ? '<button class="btn btn-primary" onclick="openServer(state.install.serverId)">Zum Server →</button>' : '<span class="muted small">Bitte warten – das Fenster kann offen bleiben.</span>')}
      </div>
    </div>
  </div>`;
}

function pollJob() {
  clearInterval(state.timers.job);
  state.timers.job = setInterval(async () => {
    if (state.view !== 'install' || !state.install) { clearInterval(state.timers.job); return; }
    try {
      const job = await api('job/' + state.install.jobId);
      state.install.job = job;
      render();
      if (job.status !== 'running') {
        clearInterval(state.timers.job);
        if (state.install.after === 'restart') {
          if (job.status === 'done') { clearInterval(state.timers.status); setTimeout(() => { try { window.close(); } catch { /* egal */ } }, 8000); }
          else toast('Update fehlgeschlagen: ' + job.error, true);
          return;
        }
        await refresh();
        const ok = job.status === 'done';
        toast(ok ? (state.install.after === 'console' ? 'Server gestartet.' : 'Server erfolgreich eingerichtet.') : 'Vorgang fehlgeschlagen.', !ok);
        if (ok && state.install.after === 'console') openServer(state.install.serverId, 'console');
      }
    } catch (e) { clearInterval(state.timers.job); toast(e.message, true); }
  }, 800);
}

/* ------------------------------------------------------------------ Server-Ansicht */

function openServer(id, tab = 'overview') {
  if (id !== state.activeId) {
    // Der Dateien-Tab darf nicht mit dem Ordner des vorherigen Servers weitermachen.
    Object.assign(state.files, { path: '', open: null, dirty: false, props: null, content: '' });
  }
  state.activeId = id;
  state.view = 'server';
  state.tab = tab;
  state.consoleNext = 0;
  render();
}
window.openServer = openServer;

function renderServer() {
  const s = server();
  if (!s) return renderWelcome();
  if (state.tab === 'players' && !playersApply(s)) state.tab = 'overview';   // Modpack-Server haben den Tab nicht
  const tabs = [['overview', 'Übersicht'], ['connect', 'Verbinden'],
    ...(playersApply(s) ? [['players', 'Spieler']] : []),
    ['console', 'Konsole'], ['files', 'Dateien'], ['settings', 'Einstellungen']];
  let body = '';
  if (state.tab === 'overview') body = tabOverview(s);
  else if (state.tab === 'connect') body = tabConnect(s);
  else if (state.tab === 'players') body = tabPlayers(s);
  else if (state.tab === 'console') body = tabConsole(s);
  else if (state.tab === 'files') body = tabFiles(s);
  else body = tabSettings(s);
  return `
  <div class="head">
    <div><h1>${esc(s.name)}</h1>
      <div class="head-sub"><span class="dot ${s.running ? 'dot-on' : ''}" data-status="dot"></span>
        <span data-status="text">${statusSub(s)}</span>
        · ${typeLabel(s)} · ${esc(s.version)}</div></div>
    <div class="btn-row">
      <button class="btn btn-primary" id="btnStart" ${s.running || !s.installed || s.installing || s.cloud_locked ? 'disabled' : ''}
        ${s.cloud_locked ? 'title="Dieser Server liegt gerade auf dem Root-Server – zuerst zurückholen."' : ''}>▶ Starten</button>
      <button class="btn btn-danger" id="btnStop" ${!s.running ? 'disabled' : ''}>■ Stoppen</button>
    </div>
  </div>
  <div class="tabs">${tabs.map(([k, t]) => `<div class="tab ${state.tab === k ? 'active' : ''}" data-tab="${k}">${t}</div>`).join('')}</div>
  <div class="page wide">${body}</div>`;
}

/* ---------- Übersicht (Dashboard) */

function tabOverview(s) {
  const ip = state.system.local_ip || '…';
  const ports = portsOf(s);
  return `
  <div class="dash">
    <div class="hero">
      <div>
        <div class="h-status"><span class="h-dot ${s.running ? 'on' : ''}" data-status="hdot"></span>
          <div><div class="h-big" data-status="big">${statusBig(s)}</div>
          <div class="h-sub" data-status="text">${statusSub(s)}</div></div></div>
        <div class="spacer"></div>
        <div class="muted small">${typeLabel(s)} · Version <b>${esc(s.version)}</b> · angelegt ${esc(s.created || '')}</div>
        <div class="btn-row" style="margin-top:14px">
          <button class="btn btn-sm" data-tab-go="console">🖥 Konsole</button>
          <button class="btn btn-sm" data-tab-go="connect">🔗 Verbinden</button>
          <button class="btn btn-sm" data-tab-go="files">📂 Dateien</button>
          <button class="btn btn-sm" id="btnFolder">📁 Ordner im Explorer</button>
        </div>
      </div>
      <div class="h-addr">
        ${addrPorts(s).map((p) => `<div class="addr addr-pub"><div><div class="a-k">Für Freunde · ${p.label}</div>
          <div class="a-v" data-pub-port="${p.port}">${publicAddr(s) ? esc(publicAddr(s)) + ' : ' + p.port : pubPlaceholder()}</div></div><span class="pill pill-green">${p.proto}</span></div>`).join('')}
        <div class="muted small" data-pub-note>${pubNote(s)}</div>
        <div class="lan-line muted small">Im Heimnetz: ${addrPorts(s).map((p) => `<code>${esc(ip)}:${p.port}</code>`).join(' · ')}</div>
      </div>
    </div>

    <div class="kpis">
      <div class="stat"><div class="k">Spieler</div><div class="v">max. ${s.max_players}</div></div>
      <div class="stat"><div class="k">${s.type === 'java' ? 'Arbeitsspeicher' : 'Sichtweite'}</div><div class="v">${s.type === 'java' ? gb(s.ram_mb) : s.view_distance + ' Chunks'}</div></div>
      <div class="stat"><div class="k">Spielmodus</div><div class="v sm">${GM[s.gamemode] || s.gamemode} · ${DF[s.difficulty] || s.difficulty}</div></div>
      <div class="stat"><div class="k">${bedrockPlayers(s) ? 'Xbox-Konto nötig' : 'Konto-Prüfung'}</div><div class="v sm">${s.online_mode ? 'Ja (empfohlen)' : 'Nein'}</div></div>
    </div>

    ${cloudServerCard(s)}

    <div class="grid2" style="margin:0">
      ${bedrockPlayers(s) ? `<div class="card" id="xboxCard">${xboxCard(s)}</div>` : `<div class="card">${isModpack(s) ? modpackCard(s) : javaOnlyCard(s)}</div>`}
      <div class="card">
        <div class="card-head"><h3>🌍 Welten &amp; Backups</h3>
          <button class="btn btn-sm" id="btnBackup" ${s.running ? 'disabled title="Server zuerst stoppen"' : ''}>🗜️ Backup erstellen</button></div>
        <div id="worldsBox" class="muted small">Wird geladen …</div>
      </div>
    </div>

    ${s.companion && s.companion.applies ? `<div class="card" id="companionCard">${companionCard(s)}</div>` : ''}

    <div class="grid2" style="margin:0">
      <div class="card">
        <h3 style="margin-top:0">So geht es weiter</h3>
        <ul class="checklist">
          <li><span class="ck ${s.installed ? 'on' : ''}">${s.installed ? '✓' : ''}</span><span><b>Server eingerichtet</b></span></li>
          <li><span class="ck ${s.running ? 'on' : ''}">${s.running ? '✓' : ''}</span><span><b>Server starten</b> – oben rechts. Der erste Start erzeugt die Welt (${isModpack(s) ? '1–5 Minuten bei Modpacks' : '20–90 s'}).</span></li>
          ${bedrockPlayers(s) ? `<li><span class="ck"></span><span><b>Im Heimnetz beitreten:</b> Handy/Windows-App → <b>Server → Server hinzufügen</b> mit
              <code>${esc(ip)}</code> und Port <code>${s.type === 'java' && s.geyser ? s.bedrock_port : s.port}</code>${s.type === 'java' ? `, Java-Spieler <code>${esc(ip)}:${s.port}</code>` : ''}.</span></li>
          <li><span class="ck ${s.xbox && s.xbox.state === 'online' ? 'on' : ''}">${s.xbox && s.xbox.state === 'online' ? '✓' : ''}</span><span><b>Xbox / PS5 / Switch:</b> am einfachsten über den <b>Xbox-Freunde-Modus</b> links – oder über den DNS-Weg (Hilfe).</span></li>`
            : `<li><span class="ck"></span><span><b>Im Heimnetz beitreten:</b> Java-Launcher → <b>Mehrspieler → Server hinzufügen</b> mit <code>${esc(ip)}:${s.port}</code>.</span></li>
          ${isModpack(s) ? '<li><span class="ck"></span><span><b>Mitspieler brauchen dasselbe Modpack</b> in ihrem Launcher (Modrinth App, Prism o. ä.) – gleiche Pack-Version wie der Server.</span></li>' : ''}`}
          <li><span class="ck"></span><span><b>Freunde von außerhalb:</b> Port in FritzBox und Windows-Firewall freigeben – siehe <a href="#" data-tab-go="connect">Verbinden</a>.</span></li>
        </ul>
        ${bedrockPlayers(s) ? `<div class="note note-warn" style="margin:14px 0 0"><b>Alle Bedrock-Spieler brauchen ein Microsoft-/Xbox-Konto</b> im Spiel –
          sonst „Verbindung zur Welt nicht möglich“. <a href="#" data-gohelp="xbox">Hilfe → Xbox-Konto verbinden</a>.</div>` : ''}
      </div>
      <div class="card">
        <div class="card-head"><h3>🖥 Konsole – letzte Zeilen</h3><button class="btn btn-sm" data-tab-go="console">Öffnen</button></div>
        <div class="minilog" id="miniLog">${s.running ? 'Wird geladen …' : 'Server ist gestoppt.'}</div>
      </div>
    </div>
  </div>`;
}

/* Karte für Modpack-Server: Pack, Version, Loader, Link – anstelle des Xbox-Freunde-Modus (kein Bedrock). */
function modpackCard(s) {
  const mp = s.modpack || {};
  return `<div class="card-head"><h3>🧩 Modpack</h3><span class="pill pill-amber">${esc(LOADER_NAMES[s.flavor] || s.flavor)}</span></div>
    <div class="mp-row" style="cursor:default;padding:0">
      ${mp.icon_url ? `<img class="mp-icon" src="${esc(mp.icon_url)}" alt="">` : '<div class="mp-icon mp-icon-empty">🧩</div>'}
      <div class="mp-body"><div class="mp-title">${esc(mp.title || 'Modpack')}</div>
        <div class="mp-meta">Version <b>${esc(mp.version_name || '?')}</b> · Minecraft ${esc(mp.mc_version || s.version)} · ${esc(LOADER_NAMES[s.flavor] || s.flavor)} ${esc(mp.loader_version || '')}</div></div>
    </div>
    <p class="small" style="margin-top:10px">Mitspieler installieren <b>dasselbe Modpack in derselben Version</b> in ihrem Launcher (z.&nbsp;B. Modrinth App)
       und verbinden sich dann ganz normal über Mehrspieler. Bedrock/Konsolen können nicht beitreten.</p>
    <div class="btn-row">${mp.url ? `<a class="btn btn-sm" href="${esc(mp.url)}" target="_blank" rel="noopener noreferrer">Auf Modrinth ansehen</a>` : ''}
      <button class="btn btn-sm" data-tab-go="settings">Pack-Version ändern</button>
      <button class="btn btn-sm" data-tab-go="files">Mods &amp; Konfiguration</button></div>`;
}

/* Karte für reine Java-Server (Paper ohne Geyser). */
function javaOnlyCard(s) {
  return `<div class="card-head"><h3>☕ Java Edition</h3><span class="pill pill-grey">nur Java</span></div>
    <p class="small">Dieser Server ist ein Paper-Server <b>ohne Crossplay</b> – nur Spieler mit der PC-Java-Version (${esc(s.version)}) kommen drauf.
       Plugins gehören in den Ordner <code>plugins</code> (Tab „Dateien“).</p>
    <p class="small mb0">Sollen später auch Konsolen und Handys mitspielen: unter <a href="#" data-tab-go="settings">Einstellungen</a>
       „Bedrock-Crossplay“ einschalten und <b>Neu installieren</b> – Geyser und Floodgate werden dann nachgeladen.</p>`;
}

/* Karte des Begleit-Plugins MCSMCompanion (nur Paper): Status aus plugins/MCSMCompanion/status.json. */
function companionLive(s) {
  const c = s.companion || {};
  return s.running && c.age !== null && c.age !== undefined && c.age < 180;   // status.json ist frisch
}
function companionHardcoreOn(s) {
  const st = (s.companion || {}).status || {};
  return companionLive(s) ? !!(st.hardcore || {}).enabled : !!s.hardcore;
}

function companionCard(s) {
  const c = s.companion || {};
  const st = c.status || {};
  const hc = st.hardcore || {};
  const live = companionLive(s);
  const hcOn = companionHardcoreOn(s);
  const dead = hc.dead || [];
  const sus = c.suspicious || [];
  const pill = !c.available ? '<span class="pill pill-grey">Datei fehlt</span>'
    : live ? `<span class="pill pill-green">aktiv · v${esc(st.plugin_version || '?')}</span>`
    : c.installed ? '<span class="pill pill-grey">bereit</span>' : '<span class="pill pill-blue">wird beim Start eingerichtet</span>';
  return `<div class="card-head"><h3>🧩 Begleit-Plugin MCSMCompanion</h3>${pill}</div>
    <div class="stats" style="margin:8px 0">
      <div class="stat"><div class="k">MCSM-Hardcore</div><div class="v sm">${hcOn ? '🔥 an' : 'aus'}</div></div>
      ${live ? `<div class="stat"><div class="k">Spieler online</div><div class="v sm">${Number(st.online || 0)} / ${Number(st.max_players || s.max_players)}</div></div>` : ''}
    </div>
    ${hcOn ? `<div class="note note-warn" style="margin:0 0 10px"><b>Hardcore aktiv:</b> Wer stirbt, wird Zuschauer an seinem Grab („R.I.P Name“ mit Datum) und meldet sich im Chat ab.
       Ein Mitspieler belebt ihn mit einem <b>Totem der Unsterblichkeit</b> am Grab wieder – er spawnt dann dort in Überleben.
       ${dead.length ? `<br>Gerade tot: <b>${esc(dead.join(', '))}</b>` : ''}</div>` : ''}
    ${sus.length ? `<div class="note note-err" style="margin:0 0 10px"><b>⚠️ Verdächtige Plugins:</b> ${esc(sus.join(', '))} – ${esc(c.hint || '')}</div>` : ''}
    <div class="btn-row">
      <button class="btn btn-sm" id="btnHardcore">${hcOn ? 'Hardcore ausschalten' : '🔥 Hardcore einschalten'}</button>
      ${s.running ? '<span class="muted small">Gilt sofort – im Spiel auch per <code>/hardcore on|off</code>.</span>' : ''}
    </div>`;
}

function bindCompanionCard(s) {
  const card = $('#companionCard'); if (!card) return;
  const btn = $('#btnHardcore'); if (btn) btn.onclick = async () => {
    const on = companionHardcoreOn(s);
    if (on && !(await askConfirm({
      tone: 'danger', title: 'Hardcore ausschalten?', confirmText: 'Hardcore ausschalten',
      text: 'Alle Spieler, die im Hardcore-Modus gestorben sind, kommen sofort wieder in den '
        + 'Überlebensmodus und können weiterspielen. Das lässt sich nicht einzeln zurücknehmen.',
    }))) return;
    btn.disabled = true;
    try { await api(`servers/${s.id}/hardcore`, { method: 'POST', body: { enabled: !on } }); toast(on ? 'Hardcore ausgeschaltet.' : 'Hardcore eingeschaltet.'); await refresh(); render(); }
    catch (e) { toast(e.message, true); btn.disabled = false; }
  };
  $$('[data-tab-go]', card).forEach((el) => el.onclick = (e) => { e.preventDefault(); state.tab = el.dataset.tabGo; render(); });
}

function xboxCard(s) {
  const x = s.xbox || {};
  const head = (pillClass, pillText) => `<div class="card-head"><h3>🎮 Xbox-Freunde-Modus</h3><span class="pill ${pillClass}">${pillText}</span></div>`;
  if (!x.enabled) {
    return `${head('pill-grey', 'Aus')}
      <p class="small">Dein Server erscheint bei allen Freunden eines <b>Bot-Kontos</b> in der Freundesliste – Xbox, PS5 und Switch
         treten dann einfach über <b>Freunde → Beitreten</b> bei, ganz ohne DNS-Umstellung.</p>
      <div class="btn-row"><button class="btn btn-primary btn-sm" id="btnXboxSetup">Jetzt einrichten</button>
        <a class="btn btn-sm" href="#" data-gohelp="friends">Wie funktioniert das?</a></div>`;
  }
  const addr = `${esc(x.address)}:${x.port}`;
  let body = '';
  if (x.state === 'login') {
    body = `<div class="note note-warn" style="margin:8px 0">
      <b>Anmeldung nötig.</b> Öffne <a href="${MS_LINK_URL}" target="_blank" rel="noopener noreferrer">microsoft.com/link</a>
      und gib diesen Code ein – mit dem <b>Bot-Konto</b>, nicht mit deinem Spielkonto:
      <div class="code-big">${esc(x.code)}</div>
      <div class="btn-row"><a class="btn btn-primary btn-sm" href="${MS_LINK_URL}" target="_blank" rel="noopener noreferrer">microsoft.com/link öffnen</a>
        <button class="btn btn-sm" id="btnXboxCopy">Code kopieren</button></div></div>`;
  } else if (x.state === 'online') {
    body = `<div class="note note-ok" style="margin:8px 0"><b>Online${x.gamertag ? ' als ' + esc(x.gamertag) : ''}.</b>
      Freunde dieses Kontos sehen jetzt „${esc(x.host_name)}“ unter <b>Freunde</b> und können beitreten.
      Neue Mitspieler müssen dem Bot-Konto auf Xbox Live folgen – es folgt automatisch zurück.</div>`;
  } else if (x.state === 'starting') {
    body = `<div class="muted small" style="margin:8px 0">Bot startet … ${x.token_cached ? 'Anmeldung ist gespeichert.' : 'Gleich erscheint ein Anmelde-Code.'}</div>`;
  } else {
    body = `<div class="muted small" style="margin:8px 0">Bot ist gestoppt. ${x.token_cached ? 'Die Anmeldung ist gespeichert – Start genügt.' : 'Beim Start wird ein Anmelde-Code angezeigt.'}
      ${!s.running ? '<br>Er startet automatisch zusammen mit dem Server.' : ''}</div>`;
  }
  if (x.error && x.state !== 'online') body += `<div class="small" style="color:var(--red);margin:4px 0 8px">${esc(x.error)}</div>`;
  const pill = x.state === 'online' ? ['pill-green', 'Online'] : x.state === 'login' ? ['pill-blue', 'Anmelden'] : x.state === 'starting' ? ['pill-grey', 'Startet'] : ['pill-grey', 'Gestoppt'];
  return `${head(pill[0], pill[1])}
    ${body}
    <div class="muted small">Freunde werden auf <code>${addr}</code> geleitet · Anzeigename „${esc(x.host_name)}“</div>
    <div class="btn-row" style="margin-top:10px">
      ${x.running ? '<button class="btn btn-sm" id="btnXboxStop">■ Bot stoppen</button>' : '<button class="btn btn-primary btn-sm" id="btnXboxStart">▶ Bot starten</button>'}
      <button class="btn btn-sm" id="btnXboxSetup">⚙ Ändern</button>
      <button class="btn btn-sm" id="btnXboxReset" title="Anmeldung verwerfen und neu anmelden">Neu anmelden</button>
      <button class="btn btn-sm btn-danger" id="btnXboxDisable">Aus</button>
    </div>`;
}

function bindXboxCard(s) {
  const setup = $('#btnXboxSetup'); if (setup) setup.onclick = () => startXboxWizard(s);
  const start = $('#btnXboxStart'); if (start) start.onclick = () => api(`servers/${s.id}/xbox/start`, { method: 'POST' }).then(() => { toast('Bot wird gestartet …'); return refresh(); }).catch((e) => toast(e.message, true));
  const stop = $('#btnXboxStop'); if (stop) stop.onclick = () => api(`servers/${s.id}/xbox/stop`, { method: 'POST' }).then(() => refresh()).catch((e) => toast(e.message, true));
  const reset = $('#btnXboxReset'); if (reset) reset.onclick = async () => {
    if (!(await askConfirm({
      tone: 'danger', title: 'Gespeicherte Anmeldung verwerfen?', confirmText: 'Verwerfen',
      text: 'Die Verbindung zum Xbox-Konto wird gelöscht. Beim nächsten Start zeigt der Manager '
        + 'einen neuen Code, den du auf microsoft.com/link eingeben musst.',
    }))) return;
    try { await api(`servers/${s.id}/xbox/reset`, { method: 'POST' }); toast('Anmeldung verworfen.'); await refresh(); } catch (e) { toast(e.message, true); }
  };
  const disable = $('#btnXboxDisable'); if (disable) disable.onclick = async () => {
    if (!(await askConfirm({
      tone: 'danger', title: 'Xbox-Freunde-Modus ausschalten?', confirmText: 'Ausschalten',
      text: 'Der Server verschwindet aus der Freundesliste auf Xbox, PlayStation und Switch. '
        + 'Deine Freunde kommen dann nur noch über die Adresse auf den Server.',
    }))) return;
    try { await api(`servers/${s.id}/xbox/disable`, { method: 'POST' }); await refresh(); render(); } catch (e) { toast(e.message, true); }
  };
  const copy = $('#btnXboxCopy'); if (copy) copy.onclick = () => navigator.clipboard?.writeText(s.xbox.code).then(() => toast('Code kopiert.'));
  $$('[data-gohelp]', $('#xboxCard')).forEach((el) => el.onclick = (e) => { e.preventDefault(); showHelp(el.dataset.gohelp); });
}

async function loadOverviewExtras(s) {
  try {
    const d = await api(`servers/${s.id}/worlds`);
    const box = $('#worldsBox');
    if (!box) return;
    const worlds = d.worlds.map((w) => `<div class="wrow"><div><div class="w-name">🌍 ${esc(w.name)}</div><div class="w-meta">${fmtBytes(w.size)} · geändert ${fmtDate(w.mtime)}</div></div>
      <button class="btn btn-sm" data-open-path="${esc(w.path)}">Ordner</button></div>`).join('');
    const backups = d.backups.slice(0, 3).map((b) => `<div class="wrow"><div><div class="w-name">🗜️ ${esc(b.name)}</div><div class="w-meta">${fmtBytes(b.size)} · ${fmtDate(b.mtime)}</div></div>
      <button class="btn btn-sm" data-open-path="backups/${esc(b.name)}">Anzeigen</button></div>`).join('');
    box.className = '';
    box.innerHTML = (worlds || '<div class="muted small">Noch keine Welt – sie entsteht beim ersten Start.</div>')
      + (backups ? `<div class="muted small" style="margin-top:10px">Letzte Backups</div>${backups}` : '')
      + (d.backups.length > 3 ? `<div class="muted small">… und ${d.backups.length - 3} weitere im Ordner <code>backups</code></div>` : '');
    $$('[data-open-path]', box).forEach((el) => el.onclick = () => api(`servers/${s.id}/folder`, { method: 'POST', body: { path: el.dataset.openPath } }).catch((e) => toast(e.message, true)));
  } catch (e) { const box = $('#worldsBox'); if (box) box.textContent = e.message; }
  loadMiniLog(s);
}

async function loadMiniLog(s) {
  const box = $('#miniLog');
  if (!box) return;
  try {
    const d = await api(`servers/${s.id}/console?tail=10`);
    if (d.lines.length) box.innerHTML = d.lines.map((l) => `<div class="${classify(l)}">${esc(l)}</div>`).join('');
    else box.textContent = s.running ? 'Noch keine Ausgabe.' : 'Server ist gestoppt.';
  } catch { /* egal */ }
}

/* ---------- Verbinden */

function tabConnect(s) {
  const ip = state.system.local_ip || '…';
  const ports = portsOf(s);
  const pub = publicAddr(s);
  return `
  <div class="grid2" style="margin-top:0">
  <div>
  <h2 style="margin-top:0">1 · Im selben WLAN / Heimnetz</h2>
  <p>Keine Freigaben nötig. Diese Adresse im Spiel eintragen:</p>
  ${addrPorts(s).map((p) => `<div class="addr"><div><div class="a-k">${p.label}</div><div class="a-v">${esc(ip)} : ${p.port}</div></div><span class="pill pill-grey">${p.proto}</span></div>`).join('')}
  ${rangeNote(s)}

  <h2>2 · Über das Internet</h2>
  <p>Freunde außerhalb deines Netzes brauchen deine <b>öffentliche IP</b> (oder deine MyFRITZ!-Adresse) plus den Port.
     Damit das klappt, sind die Schritte 3 und 4 nötig.</p>
  ${addrPorts(s).map((p) => `<div class="addr"><div><div class="a-k">Öffentliche Adresse · ${p.label}</div>
    <div class="a-v" data-pub-port="${p.port}" data-pub-empty="– noch nicht ermittelt –">${pub ? esc(pub) + ' : ' + p.port : '– noch nicht ermittelt –'}</div></div><span class="pill pill-green">${p.proto}</span></div>`).join('')}
  <div class="btn-row"><button class="btn btn-sm" id="btnPublicIp">Öffentliche IP ermitteln</button></div>
  <p class="muted small">Die Abfrage fragt zuerst deine FritzBox (UPnP), sonst einmalig den Dienst <code>api.ipify.org</code> – nur wenn du den Knopf drückst.</p>
  ${s.type === 'bedrock' ? `<div class="note note-info"><b>Bedrock (NetherNet) muss seine öffentliche IPv4 kennen.</b> Der Server bietet Spielern nur Adressen an, die er kennt –
     die Portfreigabe allein reicht nicht. Der Manager fragt die IP bei jedem Start automatisch von der FritzBox ab (UPnP) und trägt sie ein; klappt das nicht
     (Konsole meldet es), die IP unter <a href="#" data-tab-go="settings">Einstellungen</a> eintragen. Bei DS-Lite (keine eigene IPv4) ist ein Beitritt aus dem Internet nicht möglich.</div>` : ''}

  ${bedrockPlayers(s) ? `<h2>5 · Xbox, PlayStation und Switch</h2>
  <p>Konsolen haben keinen „Server hinzufügen“-Knopf. Drei Wege:</p>
  <ul>
    <li><b>Xbox-Freunde-Modus (empfohlen):</b> Ein Bot-Konto zeigt den Server in der Freundesliste – Einrichtung auf der <a href="#" data-tab-go="overview">Übersicht</a>.</li>
    <li><b>Über einen Freund:</b> Jemand tritt per Handy/Windows-App bei, die Konsole folgt über die Freundesliste.</li>
    <li><b>Per DNS-Trick (BedrockConnect):</b> Auf der Konsole den DNS ändern, dann lässt sich die Adresse eingeben.</li>
  </ul>
  <p>Schritt für Schritt: <a href="#" data-gohelp="friends">Hilfe → Xbox-Freunde-Modus</a> · <a href="#" data-gohelp="console">Hilfe → Xbox &amp; PS5 verbinden</a> ·
     Anmeldeprobleme: <a href="#" data-gohelp="xbox">Hilfe → Xbox-Konto verbinden</a></p>`
    : `<h2>5 · Nur Java-Spieler</h2>
  <p>${isModpack(s) ? 'Ein Modpack-Server ist nur für die Java Edition mit demselben Modpack erreichbar – Konsolen und Handys (Bedrock) können nicht beitreten.'
    : 'Dieser Server läuft ohne Crossplay – nur die Java Edition (PC) kann beitreten. Konsolen und Handys brauchen „Bedrock-Crossplay“ in den Einstellungen (danach „Neu installieren“).'}
     Spieler tragen die Adresse im Launcher unter <b>Mehrspieler → Server hinzufügen</b> ein.</p>`}
  </div>
  <div>
  <h2 style="margin-top:0">3 · Windows-Firewall öffnen</h2>
  <p>Windows blockt eingehende Verbindungen standardmäßig. Der Knopf legt die passende Regel an –
     Windows fragt dabei einmal nach Administratorrechten.</p>
  <div class="btn-row"><button class="btn" id="btnFirewall">🛡️ Windows-Firewall freigeben</button></div>
  <details style="margin-top:10px"><summary class="muted small" style="cursor:pointer">Manuell per Eingabeaufforderung (als Administrator)</summary>
    ${s.firewall_cmds.map((c) => `<p><code style="white-space:normal">${esc(c)}</code></p>`).join('')}</details>

  <h2>4 · Port in der FritzBox freigeben</h2>
  <div class="card" style="margin-bottom:14px">
    <div class="card-head"><h3>Automatisch per UPnP</h3><span class="pill pill-green">empfohlen</span></div>
    <p class="small">Wenn in der FritzBox für diesen PC <b>„Selbstständige Portfreigaben für dieses Gerät erlauben“</b> aktiv ist
       (<b>Heimnetz → Netzwerk → Netzwerkverbindungen → diesen PC bearbeiten</b>), legt der Manager alle nötigen Freigaben selbst an.
       Sie erscheinen dann in der FritzBox unter <b>Internet → Freigaben</b> mit der Bezeichnung „MCSM …“.</p>
    <div class="btn-row"><button class="btn btn-primary btn-sm" id="btnPortmap">🔓 Portfreigabe jetzt anlegen</button>
      <button class="btn btn-sm" id="btnPortmapRemove">Freigaben entfernen</button></div>
    <div id="pmResult" style="margin-top:10px"></div>
  </div>
  <p class="muted small">Oder von Hand:</p>
  <ol class="steps">
    <li><code>http://fritz.box</code> öffnen → <b>Internet → Freigaben → Portfreigaben</b></li>
    <li><b>Gerät für Freigaben hinzufügen</b> → als Gerät <b>diesen PC</b> (${esc(state.system.hostname || '')}, ${esc(ip)}) wählen</li>
    <li><b>Neue Freigabe → Portfreigabe → Anwendung: Andere Anwendung</b></li>
    ${ports.map((p) => `<li>Bezeichnung <code>Minecraft ${p.proto} ${p.port}</code> · Protokoll <b>${p.proto}</b> · Port an Gerät <b>${p.from}</b> · bis Port <b>${p.to}</b> · extern gewünscht <b>${p.from}</b>${p.range ? ` (bis <b>${p.to}</b>)` : ''} <span class="muted small">– ${esc(p.label)}</span></li>`).join('')}
    <li><b>OK → Übernehmen</b>. Danach unter <b>Internet → MyFRITZ!-Konto</b> eine feste Adresse einrichten, damit sich die IP nicht ändert.</li>
  </ol>
  <p>Ausführlich mit allen Fallstricken (feste IP, DS-Lite): <a href="#" data-gohelp="fritzbox">Hilfe → FritzBox-Portfreigabe</a></p>
  </div>
  </div>`;
}

/* ---------- Spieler (Freigabeliste, Sperrliste, wer gerade online ist) */

const BAN_DURATIONS = [['', 'dauerhaft'], ['1h', '1 Stunde'], ['6h', '6 Stunden'], ['1d', '1 Tag'], ['7d', '7 Tage'], ['30d', '30 Tage']];
/* Minecraft schreibt Zeiten als „2026-09-30 02:00:35 +0200“ – für die Anzeige reicht Tag und Uhrzeit. */
const fmtBanTime = (t) => {
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})/.exec(t || '');
  return m ? `${m[3]}.${m[2]}.${m[1]} um ${m[4]}:${m[5]} Uhr` : t;
};

function tabPlayers(s) {
  const be = s.type === 'bedrock';
  return `
  <div class="note note-info" style="margin-top:0">
    <b>${be ? 'Erlaubnisliste' : 'Freigabeliste'}:</b> Ist sie eingeschaltet, kommen nur Spieler auf den Server, die darauf stehen –
    alle anderen werden beim Beitreten abgewiesen. ${be
      ? 'Der Bedrock-Server führt keine eigene Sperrliste: Wer nicht mehr mitspielen soll, wird aus der Erlaubnisliste genommen.'
      : 'Sperren geht auch bei gestopptem Server – der Manager trägt sie dann direkt in die Serverdateien ein.'}
    Läuft der Server, wirkt jede Änderung sofort.
  </div>
  <div id="playersBox"><div class="card"><div class="muted small">Wird geladen …</div></div></div>`;
}

const plRow = (name, meta, buttons) => `<div class="wrow"><div><div class="w-name">${esc(name)}</div>${
  meta ? `<div class="w-meta">${meta}</div>` : ''}</div><div class="btn-row">${buttons}</div></div>`;
const plHint = (text) => `<div class="muted small" style="padding:10px 0">${text}</div>`;

/* Karte 1: Freigabeliste (Java: whitelist.json · Bedrock: allowlist.json) */
function plAllowCard(s, d) {
  const be = d.kind === 'bedrock';
  const label = be ? 'Erlaubnisliste' : 'Freigabeliste';
  const rows = d.allowed.map((p) => plRow(p.name, '',
    `<button class="btn btn-sm" data-pl="whitelist_remove" data-name="${esc(p.name)}">Entfernen</button>`)).join('');
  return `<div class="card">
    <div class="card-head"><h3>✅ ${label}</h3>
      <span class="pill ${d.enabled ? 'pill-green' : 'pill-grey'}">${d.enabled ? 'eingeschaltet' : 'ausgeschaltet'}</span></div>
    ${d.enabled && !d.allowed.length
      ? '<div class="note note-warn" style="margin:0 0 10px"><b>Die Liste ist eingeschaltet, aber leer</b> – so kommt niemand auf den Server. Unten den ersten Namen eintragen.</div>'
      : `<p class="small">${d.enabled
        ? `Es kommen nur diese ${d.allowed.length === 1 ? 'eine Person' : d.allowed.length + ' Spieler'} auf den Server.`
        : 'Zurzeit darf jeder mitspielen, der die Adresse kennt.'}</p>`}
    <div class="btn-row" style="margin-bottom:6px">
      <button class="btn btn-sm" data-pl="${d.enabled ? 'whitelist_off' : 'whitelist_on'}">${d.enabled ? 'Liste ausschalten' : 'Liste einschalten'}</button>
    </div>
    <div class="pl-list">${rows || plHint('Noch niemand eingetragen – unten den ersten Namen hinzufügen.')}</div>
    <div class="flex" style="margin-top:12px">
      <input type="text" id="plAddName" autocomplete="off" placeholder="${be ? 'Gamertag, z. B. Anna Meier' : 'Spielername, z. B. Anna_2011'}">
      <button class="btn btn-sm" id="plAdd">+ Hinzufügen</button></div>
    ${d.running ? '' : `<div class="muted small" style="margin-top:8px">Der Server ist gestoppt – der Manager schreibt den Namen direkt in die Datei${
      be ? '.' : ' und holt die Spieler-Nummer (UUID) bei Mojang.'}</div>`}
  </div>`;
}

/* Karte 2: gesperrte Spieler (banned-players.json) – Bedrock kennt keine solche Liste */
function plBanCard(s, d) {
  if (!d.bans) {
    return `<div class="card">
      <div class="card-head"><h3>⛔ Gesperrte Spieler</h3><span class="pill pill-grey">bei Bedrock nicht nötig</span></div>
      <p class="small mb0">Der Bedrock-Server führt keine Sperrliste. Wer nicht mehr mitspielen soll: die Erlaubnisliste
         einschalten und den Namen dort entfernen – dann kommt er nicht mehr herein.</p></div>`;
  }
  const rows = d.banned.map((p) => plRow(p.name, [
    p.reason ? 'Grund: ' + esc(p.reason) : '',
    p.source ? 'gesperrt von ' + esc(p.source) : '',
    /* Abgelaufene Sperren räumt der Server erst beim nächsten Start weg – bis dahin stehen sie
       noch in der Datei, gelten aber nicht mehr. */
    p.expires && p.expires !== 'forever'
      ? (p.expired ? 'abgelaufen am ' + esc(fmtBanTime(p.expires)) : 'läuft ab am ' + esc(fmtBanTime(p.expires)))
      : 'dauerhaft',
  ].filter(Boolean).join(' · '), `<button class="btn btn-sm" data-pl="unban" data-name="${esc(p.name)}">Entsperren</button>`)).join('');
  const ips = d.banned_ips || [];
  const active = d.banned.filter((p) => !p.expired).length;
  return `<div class="card">
    <div class="card-head"><h3>⛔ Gesperrte Spieler</h3>
      <span class="pill ${active ? 'pill-amber' : 'pill-grey'}">${active}</span></div>
    <div class="pl-list">${rows || plHint('Niemand ist gesperrt.')}</div>
    ${ips.length ? `<div class="muted small" style="margin-top:8px">Zusätzlich ${ips.length === 1
      ? 'ist eine IP-Adresse gesperrt' : 'sind ' + ips.length + ' IP-Adressen gesperrt'} (banned-ips.json):
      ${ips.map((e) => esc(e.ip)).join(', ')}. „Entsperren“ nimmt eine mitgesetzte IP-Sperre mit heraus;
      einzeln geht es im Spiel mit <code>/pardon-ip</code>.</div>` : ''}
    <div class="pl-add">
      <input type="text" id="plBanName" autocomplete="off" placeholder="Spielername">
      <input type="text" id="plBanReason" autocomplete="off" placeholder="Grund – bekommt der Spieler beim Beitreten zu sehen">
      <div class="flex">
        <select id="plBanDur" ${d.running ? 'disabled' : ''}>${BAN_DURATIONS.map(([k, t]) => `<option value="${k}">${t}</option>`).join('')}</select>
        <button class="btn btn-sm btn-danger" id="plBan">Sperren</button>
      </div>
    </div>
    <div class="muted small" style="margin-top:8px">${d.running
      ? 'Solange der Server läuft, sperrt er selbst – dauerhaft. Für eine Sperre auf Zeit den Server stoppen.'
      : 'Der Server ist gestoppt – der Manager trägt die Sperre direkt ein, sie gilt ab dem nächsten Start.'}</div>
  </div>`;
}

/* Karte 3: wer gerade spielt – aus dem Begleit-Plugin oder über den Konsolenbefehl „list“ */
function plOnlineCard(s, d) {
  const rows = d.online.map((n) => plRow(n, '',
    `<button class="btn btn-sm" data-pl="kick" data-name="${esc(n)}">Hinauswerfen</button>`
    + (d.bans ? `<button class="btn btn-sm btn-danger" data-pl="ban" data-name="${esc(n)}">Sperren</button>` : ''))).join('');
  const body = !d.running ? plHint('Der Server ist gestoppt – es ist niemand online.')
    : !d.online_known ? plHint('Der Server hat gerade keine Spielerliste geschickt. Mit „Aktualisieren“ noch einmal fragen.')
    : (rows || plHint('Gerade spielt niemand.'));
  const pill = !d.running ? ['pill-grey', 'Server aus']
    : !d.online_known ? ['pill-grey', 'unbekannt']
    : [d.online.length ? 'pill-green' : 'pill-grey', `${d.online.length} von ${s.max_players}`];
  return `<div class="card" style="margin-top:14px">
    <div class="card-head"><h3>🟢 Gerade online</h3><span class="pill ${pill[0]}">${pill[1]}</span></div>
    <div class="pl-list">${body}</div>
    <div class="btn-row" style="margin-top:10px"><button class="btn btn-sm" id="plReload">⟳ Aktualisieren</button>
      ${d.running && d.bans ? '<span class="muted small">„Sperren“ wirft den Spieler hinaus und lässt ihn nicht mehr herein.</span>' : ''}</div>
  </div>`;
}

function renderPlayersBox(s, d) {
  state.players.data = d;
  state.players.running = !!d.running;
  const box = $('#playersBox');
  if (!box) return;
  /* Beschädigte Serverdateien sähen sonst wie leere Listen aus – der Manager schreibt sie dann nicht. */
  const broken = d.broken || [];
  const brokenNote = broken.length ? `<div class="note note-warn" style="margin:0 0 14px">
    <b>${broken.map(esc).join(' und ')} ${broken.length === 1 ? 'lässt' : 'lassen'} sich nicht lesen</b> –
    die Datei ist beschädigt oder nur halb geschrieben (das passiert nach einem harten Serverende).
    Die Liste darunter ist deshalb unvollständig, und Änderungen daran lehnt der Manager ab.
    Den Server einmal starten und wieder stoppen, dann schreibt er die Datei neu.</div>` : '';
  box.innerHTML = `${brokenNote}<div class="grid2" style="margin:0">${plAllowCard(s, d)}${plBanCard(s, d)}</div>${plOnlineCard(s, d)}`;

  $$('[data-pl]', box).forEach((el) => el.onclick = () => playerAction(s, el.dataset.pl, { name: el.dataset.name || '' }));
  const add = $('#plAddName', box);
  const doAdd = () => playerAction(s, 'whitelist_add', { name: add.value });
  $('#plAdd', box).onclick = doAdd;
  add.onkeydown = (e) => { if (e.key === 'Enter') doAdd(); };
  const ban = $('#plBan', box);
  if (ban) {
    const doBan = () => playerAction(s, 'ban', { name: $('#plBanName', box).value, reason: $('#plBanReason', box).value, duration: $('#plBanDur', box).value });
    ban.onclick = doBan;
    $('#plBanName', box).onkeydown = (e) => { if (e.key === 'Enter') doBan(); };
  }
  $('#plReload', box).onclick = () => loadPlayers(s);
}

async function loadPlayers(s) {
  const box = $('#playersBox');
  if (!box) return;
  box.innerHTML = '<div class="card"><div class="muted small">Wird geladen …</div></div>';
  try { renderPlayersBox(s, await api(`servers/${s.id}/players`)); }
  catch (e) { box.innerHTML = `<div class="note note-err" style="margin-top:0">${esc(e.message)}</div>`; }
}

async function playerAction(s, action, body) {
  const box = $('#playersBox');
  if (!box) return;
  if (action === 'ban' && !(await askConfirm({
    tone: 'danger', title: `„${(body.name || '').trim()}“ sperren?`, confirmText: 'Sperren',
    text: 'Der Spieler kommt nicht mehr auf den Server und wird sofort hinausgeworfen, '
      + 'falls er gerade online ist. Aufheben kannst du die Sperre jederzeit wieder.',
  }))) return;
  $$('button', box).forEach((el) => el.disabled = true);       // kein zweiter Klick, solange der Server antwortet
  try {
    const d = await api(`servers/${s.id}/players`, { method: 'POST', body: { action, ...body } });
    if (d.message) toast(d.message, !!d.warn);
    renderPlayersBox(s, d);
  } catch (e) {
    toast(e.message, true);
    $$('button', box).forEach((el) => el.disabled = false);
  }
}

/* ---------- Konsole */

function tabConsole(s) {
  return `
  <div class="console" id="console"></div>
  <div class="cmd-row">
    <input type="text" id="cmdInput" placeholder="${s.running ? 'Befehl eingeben, z. B. list  oder  say Hallo' : 'Server starten, um Befehle zu senden'}" ${s.running ? '' : 'disabled'}>
    <button class="btn" id="cmdSend" ${s.running ? '' : 'disabled'}>Senden</button>
  </div>
  <p class="muted small">Nützlich: <code>list</code> (wer ist online) · <code>op Spielername</code> (Rechte geben) ·
     <code>say Text</code> · ${s.type === 'bedrock' ? '<code>allowlist add Name</code> (die Welt speichert automatisch)' : '<code>save-all</code> · <code>whitelist add Name</code>'}</p>`;
}

/* ---------- Dateien */

const KIND_ICON = { world: '🌍', plugins: '🧩', plugin: '🧩', config: '⚙️', backup: '🗜️', log: '📜', folder: '📁', other: '📄' };

function tabFiles(s) {
  return `
  <div class="files">
    <aside class="files-side">
      <div class="files-head">
        <div class="crumbs" id="fCrumbs"></div>
        <label class="switch"><input type="checkbox" id="fShowAll" ${state.files.showAll ? 'checked' : ''}> Alle Dateien anzeigen (auch Technik)</label>
      </div>
      <div class="files-list" id="fList"><div class="muted small" style="padding:10px">Wird geladen …</div></div>
      <div class="files-foot">
        <button class="btn btn-sm" id="fExplorer">📁 Im Explorer</button>
        <button class="btn btn-sm" id="fBackup" ${s.running ? 'disabled title="Server zuerst stoppen"' : ''}>🗜️ Welten sichern</button>
        <button class="btn btn-sm" id="fReload">⟳</button>
      </div>
    </aside>
    <section class="files-main" id="fMain">
      <div class="files-empty"><div><div style="font-size:34px">📂</div><p>Links eine Datei wählen.<br><span class="small">Standardmäßig siehst du nur Welten, Einstellungen, Plugins, Logs und Backups.
      <br><code>server.properties</code> lässt sich als Formular mit Erklärungen bearbeiten.</span></p></div></div>
    </section>
  </div>`;
}

const isPropsPath = (p) => /(^|\/)server\.properties$/.test(p || '');

async function loadDir(s, path) {
  const f = state.files;
  f.path = path;
  const list = $('#fList'), crumbs = $('#fCrumbs');
  if (!list) return;
  // Brotkrumen vor der Anfrage – so gibt es auch bei einem Fehler immer den Weg zurück zum Server-Ordner.
  const parts = path ? path.split('/') : [];
  if (crumbs) {
    crumbs.innerHTML = `<a data-crumb="">Server</a>` + parts.map((p, i) => `<span class="sep">›</span><a data-crumb="${esc(parts.slice(0, i + 1).join('/'))}">${esc(p)}</a>`).join('');
    $$('[data-crumb]', crumbs).forEach((el) => el.onclick = () => loadDir(s, el.dataset.crumb));
  }
  try {
    const d = await api(`servers/${s.id}/files?path=${encodeURIComponent(path)}`);
    const entries = d.entries.filter((e) => f.showAll || !e.hidden);
    if (path) entries.unshift({ name: '..', path: path.split('/').slice(0, -1).join('/'), is_dir: true, kind: 'up' });
    list.innerHTML = entries.length ? entries.map((e) => `
      <div class="frow ${e.hidden ? 'dim' : ''} ${f.open === e.path ? 'active' : ''}" data-path="${esc(e.path)}" data-dir="${e.is_dir ? 1 : 0}" data-editable="${e.editable ? 1 : 0}">
        <span class="f-ico">${e.kind === 'up' ? '↩' : (KIND_ICON[e.kind] || '📄')}</span>
        <span class="f-name" title="${esc(e.name)}">${esc(e.name)}</span>
        <span class="f-meta">${e.is_dir ? '' : fmtBytes(e.size)}</span>
      </div>`).join('') : '<div class="muted small" style="padding:10px">Leerer Ordner.</div>';
    $$('.frow', list).forEach((el) => el.onclick = () => {
      if (el.dataset.dir === '1') loadDir(s, el.dataset.path);
      else openFile(s, el.dataset.path, el.dataset.editable === '1');
    });
  } catch (e) {
    if (path) { toast(e.message, true); return loadDir(s, ''); }   // Ordner weg (gelöscht / anderer Server) → zurück zur Wurzel
    list.innerHTML = `<div class="note note-err" style="margin:8px">${esc(e.message)}</div>`;
  }
}

async function openFile(s, path, editable) {
  const f = state.files;
  if (f.dirty && !(await askConfirm({
    tone: 'danger', title: 'Ungespeicherte Änderungen verwerfen?', confirmText: 'Verwerfen',
    text: 'In der geöffneten Datei stehen Änderungen, die noch nicht gespeichert sind. '
      + 'Beim Wechsel gehen sie verloren.',
  }))) return;
  f.open = path; f.dirty = false;
  $$('.frow').forEach((el) => el.classList.toggle('active', el.dataset.path === path));
  const main = $('#fMain');
  const name = path.split('/').pop();
  if (!editable) {
    main.innerHTML = `<div class="files-empty"><div><div style="font-size:34px">🔒</div><p><b>${esc(name)}</b><br><span class="small">Diese Datei ist keine Textdatei oder zu groß für den Editor.</span></p>
      <button class="btn btn-sm" id="fShowInExplorer">Im Explorer anzeigen</button></div></div>`;
    $('#fShowInExplorer').onclick = () => api(`servers/${s.id}/folder`, { method: 'POST', body: { path } }).catch((e) => toast(e.message, true));
    return;
  }
  main.innerHTML = '<div class="files-empty muted">Wird geladen …</div>';
  try {
    const isProps = name === 'server.properties';
    if (isProps && f.mode === 'form') {
      f.props = await api(`servers/${s.id}/props`);
      renderPropsForm(s, path);
    } else {
      const d = await api(`servers/${s.id}/file?path=${encodeURIComponent(path)}`);
      f.content = d.content;
      renderRawEditor(s, path, d, isProps);
    }
  } catch (e) { main.innerHTML = `<div class="note note-err" style="margin:16px">${esc(e.message)}</div>`; }
}

function editorHead(s, path, meta, isProps, extra = '') {
  const f = state.files;
  const locked = isProps && s.running;      // wie die Einstellungen: nur bei gestopptem Server
  return `<div class="editor-head">
    <div><div class="e-title">${esc(path)}</div><div class="e-meta">${meta}${locked ? ' · <b>Server läuft – zum Ändern zuerst stoppen</b>' : ''}</div></div>
    <div class="btn-row">
      ${isProps ? `<span class="seg"><button class="${f.mode === 'form' ? 'on' : ''}" data-mode="form">Formular</button><button class="${f.mode === 'raw' ? 'on' : ''}" data-mode="raw">Textdatei</button></span>` : ''}
      ${extra}
      <button class="btn btn-primary btn-sm" id="fSave" ${locked ? 'disabled title="Server zuerst stoppen"' : ''}>💾 Speichern</button>
    </div></div>`;
}

function bindEditorHead(s, path, isProps) {
  $$('[data-mode]').forEach((el) => el.onclick = () => { state.files.mode = el.dataset.mode; state.files.dirty = false; openFile(s, path, true); });
  if (isProps) { /* Hinweis: Änderungen wirken nach Neustart */ }
}

function propControl(fd) {
  const id = 'p_' + fd.key.replace(/[^a-z0-9]/gi, '_');
  if (fd.managed) return `<input type="text" id="${id}" value="${esc(fd.value)}" disabled title="Wird vom Manager gesetzt">`;
  if (fd.type === 'bool') return `<select id="${id}" data-key="${esc(fd.key)}"><option value="true" ${fd.value === 'true' ? 'selected' : ''}>Ja</option><option value="false" ${fd.value !== 'true' ? 'selected' : ''}>Nein</option></select>`;
  if (fd.type === 'enum') {
    const opts = fd.options.includes(fd.value) ? fd.options : [fd.value, ...fd.options];
    return `<select id="${id}" data-key="${esc(fd.key)}">${opts.map((o) => `<option value="${esc(o)}" ${o === fd.value ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select>`;
  }
  if (fd.type === 'int') return `<input type="number" id="${id}" data-key="${esc(fd.key)}" value="${esc(fd.value)}" ${fd.min !== undefined ? `min="${fd.min}"` : ''} ${fd.max !== undefined ? `max="${fd.max}"` : ''}>`;
  return `<input type="text" id="${id}" data-key="${esc(fd.key)}" value="${esc(fd.value)}">`;
}

function renderPropsForm(s, path) {
  const f = state.files;
  const known = f.props.fields.filter((x) => x.known), other = f.props.fields.filter((x) => !x.known);
  const row = (fd) => `<div class="prop"><div><div class="p-label">${esc(fd.label)}</div><div class="p-key">${esc(fd.key)}</div></div>
    <div>${propControl(fd)}</div>${fd.desc ? `<div class="p-desc">${esc(fd.desc)}</div>` : ''}</div>`;
  $('#fMain').innerHTML = editorHead(s, path, `${f.props.fields.length} Einstellungen · Änderungen wirken nach einem Neustart des Servers`, true)
    + `<div class="editor-body"><div class="prop-group"><h4>Wichtige Einstellungen</h4>${known.map(row).join('') || '<div class="muted small">–</div>'}</div>
       <div class="prop-group"><h4>Weitere Einstellungen</h4>${other.map(row).join('') || '<div class="muted small">–</div>'}</div></div>`;
  bindEditorHead(s, path, true);
  $$('.editor-body [data-key]').forEach((el) => el.onchange = () => f.dirty = true);
  $('#fSave').onclick = async () => {
    const values = {};
    $$('.editor-body [data-key]:not([disabled])').forEach((el) => values[el.dataset.key] = el.value);
    try {
      await api(`servers/${s.id}/props`, { method: 'POST', body: { values } });
      f.dirty = false; toast('server.properties gespeichert. Wirkt nach dem nächsten Start.'); await refresh();
    } catch (e) { toast(e.message, true); }
  };
}

function renderRawEditor(s, path, d, isProps) {
  const f = state.files;
  $('#fMain').innerHTML = editorHead(s, path, `${fmtBytes(d.size)} · beim Speichern wird eine Sicherung <code>${esc(d.name)}.bak</code> angelegt`, isProps)
    + `<div class="editor-body"><textarea id="fText" spellcheck="false">${esc(d.content)}</textarea></div>`;
  bindEditorHead(s, path, isProps);
  const ta = $('#fText');
  ta.oninput = () => f.dirty = true;
  ta.onkeydown = (e) => { if (e.key === 'Tab') { e.preventDefault(); const st = ta.selectionStart; ta.setRangeText('  ', st, ta.selectionEnd, 'end'); f.dirty = true; } };
  $('#fSave').onclick = async () => {
    try {
      await api(`servers/${s.id}/file`, { method: 'POST', body: { path, content: ta.value } });
      f.dirty = false; toast('Gespeichert.'); if (isProps) await refresh();
    } catch (e) { toast(e.message, true); }
  };
}

function bindFiles(s) {
  const f = state.files;
  $('#fShowAll').onchange = (e) => { f.showAll = e.target.checked; loadDir(s, f.path); };
  $('#fExplorer').onclick = () => api(`servers/${s.id}/folder`, { method: 'POST', body: { path: f.path } }).catch((e) => toast(e.message, true));
  $('#fReload').onclick = () => loadDir(s, f.path);
  $('#fBackup').onclick = () => doBackup(s).then(() => loadDir(s, f.path));
  f.open = null; f.dirty = false;
  loadDir(s, f.path || '');
}

async function doBackup(s) {
  const b = $('#fBackup') || $('#btnBackup');
  if (b) { b.disabled = true; b.textContent = 'Backup läuft …'; }
  try { const d = await api(`servers/${s.id}/backup`, { method: 'POST' }); toast(`Backup erstellt: ${d.file} (${fmtBytes(d.size)})`); }
  catch (e) { toast(e.message, true); }
  finally { if (b) { b.disabled = false; b.textContent = b.id === 'fBackup' ? '🗜️ Welten sichern' : '🗜️ Backup erstellen'; } }
}

/* ---------- Einstellungen */

function tabSettings(s) {
  const busy = s.running || s.installing;
  const lock = s.running ? '<div class="note note-warn mb0" style="margin-top:0">Der Server läuft. Stoppe ihn, um Einstellungen zu ändern.</div><div class="spacer"></div>'
    : s.installing ? '<div class="note note-warn mb0" style="margin-top:0">Die Einrichtung läuft noch – bitte warten.</div><div class="spacer"></div>' : '';
  return `
  ${lock}
  <fieldset style="border:0;padding:0;margin:0" ${busy ? 'disabled' : ''}>
    ${settingsForm(s, 'st_')}
    <div class="btn-row"><button class="btn btn-primary" id="btnSave">Einstellungen speichern</button>
      <button class="btn" data-tab-go="files">Alle Optionen (server.properties) →</button></div>
  </fieldset>

  ${isModpack(s) ? settingsModpack(s, busy) : `
  <h2>Version ändern / neu installieren</h2>
  <p class="muted">Lädt die Server-Software erneut. Welt und Einstellungen bleiben erhalten.
     ${s.type === 'bedrock' ? 'Bedrock-Version exakt eingeben (z. B. <code>' + esc(s.version) + '</code>).' : 'Bei Java: gleiche oder neuere Version wählen – ein Downgrade kann die Welt beschädigen.'}</p>
  <fieldset style="border:0;padding:0;margin:0" ${busy ? 'disabled' : ''}>
    <div class="grid2" style="margin-top:0">
      ${s.type === 'bedrock'
        ? fieldInput('st_version', 'Bedrock-Version', s.version)
        : `<div class="field"><label for="st_version">Paper-Version</label><select id="st_version"><option>${esc(s.version)}</option></select></div>`}
      <div class="field"><label>&nbsp;</label><button class="btn" id="btnReinstall">⟳ Neu installieren</button></div>
    </div>
  </fieldset>`}

  <h2>Server löschen</h2>
  <p class="muted">Entfernt den Server <b>samt Welt</b> von diesem PC. Das lässt sich nicht rückgängig machen.</p>
  <button class="btn btn-danger" id="btnDelete" ${busy ? 'disabled' : ''}>Server endgültig löschen</button>`;
}

/* Einstellungen → Abschnitt „Modpack“: aktuelle Pack-Version, Versionsliste von Modrinth, Update / Neuinstallation. */
function settingsModpack(s, busy) {
  const mp = s.modpack || {};
  return `
  <h2>Modpack</h2>
  <div class="card" style="margin-bottom:14px">
    <div class="mp-row" style="cursor:default;padding:0">
      ${mp.icon_url ? `<img class="mp-icon" src="${esc(mp.icon_url)}" alt="">` : '<div class="mp-icon mp-icon-empty">🧩</div>'}
      <div class="mp-body"><div class="mp-title">${esc(mp.title || 'Modpack')}</div>
        <div class="mp-meta">Installiert: <b>${esc(mp.version_name || '?')}</b> · Minecraft ${esc(mp.mc_version || s.version)} · ${esc(LOADER_NAMES[s.flavor] || s.flavor)} ${esc(mp.loader_version || '')}
          ${mp.url ? ` · <a href="${esc(mp.url)}" target="_blank" rel="noopener noreferrer">Modrinth</a>` : ''}</div></div>
    </div>
  </div>
  <p class="muted">Eine andere Pack-Version installieren: Die Mods der bisherigen Version werden entfernt und die der neuen geladen.
     <b>Welt und eigene Einstellungen bleiben erhalten</b> – ein Backup vorher ist trotzdem eine gute Idee (Übersicht).
     Ein Downgrade kann die Welt beschädigen.</p>
  <fieldset style="border:0;padding:0;margin:0" ${busy ? 'disabled' : ''}>
    <div class="grid2" style="margin-top:0">
      <div class="field"><label for="st_mpversion">Pack-Version</label>
        <select id="st_mpversion"><option value="${esc(mp.version_id || '')}">${esc(mp.version_name || s.version)} (installiert)</option></select>
        <div class="hint" id="st_mpHint">Versionen werden von Modrinth geladen …</div></div>
      <div class="field"><label>&nbsp;</label>
        <div class="btn-row"><button class="btn btn-primary" id="btnMpUpdate">⬆ Modpack-Version aktualisieren</button>
          <button class="btn" id="btnReinstall" title="Gleiche Version erneut einrichten (fehlende Mods/Loader nachladen)">⟳ Neu installieren</button></div></div>
    </div>
  </fieldset>`;
}

function bindSettingsModpack(s) {
  const mp = s.modpack || {};
  const sel = $('#st_mpversion'), hint = $('#st_mpHint');
  let versions = [];
  api(`modpacks/${encodeURIComponent(mp.project_id || '')}/versions`).then((vs) => {
    versions = vs;
    if (!sel) return;
    sel.innerHTML = vs.map((v) => `<option value="${esc(v.id)}" ${v.id === mp.version_id ? 'selected' : ''}>${esc(v.name)} · MC ${esc(v.mc_version)} · ${v.loaders.map((l) => LOADER_NAMES[l] || l).join('/')}${v.id === mp.version_id ? ' (installiert)' : ''}</option>`).join('')
      || `<option value="${esc(mp.version_id || '')}">${esc(mp.version_name || '?')} (installiert)</option>`;
    if (hint) hint.textContent = vs.length ? `${vs.length} Server-Versionen auf Modrinth, neueste zuerst.` : 'Modrinth listet keine weiteren Server-Versionen.';
  }).catch((e) => { if (hint) hint.textContent = 'Versionen konnten nicht geladen werden: ' + e.message; });

  const reinstall = async (v) => {
    const body = { modpack: { project_id: mp.project_id, title: mp.title, version_id: v.id, version_name: v.name, mc_version: v.mc_version,
      loader: v.loaders[0], loader_version: '', icon_url: mp.icon_url || '', url: mp.url || '' } };
    try {
      const d = await api(`servers/${s.id}/reinstall`, { method: 'POST', body });
      state.install = { serverId: s.id, jobId: d.job_id, job: null, after: 'overview' }; state.view = 'install'; render(); pollJob();
    } catch (e) { toast(e.message, true); }
  };
  $('#btnMpUpdate').onclick = async () => {
    const v = versions.find((x) => x.id === sel.value);
    if (!v) { toast('Bitte warten, bis die Versionsliste geladen ist, und eine Version wählen.', true); return; }
    if (v.id === mp.version_id) { toast('Diese Version ist bereits installiert – für ein erneutes Einrichten „Neu installieren“ wählen.'); return; }
    if (!(await askConfirm({
      tone: 'danger', title: `„${s.name}“ auf ${v.name} umstellen?`, confirmText: 'Jetzt umstellen',
      text: `Das Modpack wird auf ${v.name} (Minecraft ${v.mc_version}) gebracht. Die Mods der `
        + 'jetzigen Version werden dabei ersetzt. Die Welt bleibt erhalten – sichere sie '
        + 'trotzdem vorher, falls das neue Modpack nicht dazu passt.',
    }))) return;
    reinstall(v);
  };
  $('#btnReinstall').onclick = async () => {
    if (!(await askConfirm({
      tone: 'danger', title: 'Modpack erneut einrichten?', confirmText: 'Erneut einrichten',
      text: `„${mp.title || ''}“ ${mp.version_name || ''} wird für „${s.name}“ noch einmal `
        + 'heruntergeladen und eingerichtet. Mods und Einstellungen aus dem Pack werden dabei '
        + 'überschrieben, die Welt bleibt erhalten.',
    }))) return;
    reinstall(versions.find((x) => x.id === mp.version_id) || { id: mp.version_id, name: mp.version_name, mc_version: mp.mc_version, loaders: [s.flavor] });
  };
}

/* ---------- Bindings der Server-Ansicht */

function bindServer() {
  const s = server();
  if (!s) return;
  $$('.tab').forEach((el) => el.onclick = () => { state.tab = el.dataset.tab; render(); });
  $$('[data-tab-go]').forEach((el) => el.onclick = (e) => { e.preventDefault(); state.tab = el.dataset.tabGo; render(); });
  $$('[data-gohelp]').forEach((el) => el.onclick = (e) => { e.preventDefault(); showHelp(el.dataset.gohelp); });

  $('#btnStart').onclick = async () => {
    try {
      const d = await api(`servers/${s.id}/start`, { method: 'POST' });
      if (d.job_id) {
        state.install = { serverId: s.id, jobId: d.job_id, job: null, after: 'console' };
        state.view = 'install'; render(); pollJob(); return;
      }
      toast('Server wird gestartet …'); state.tab = 'console'; await refresh(); render();
    } catch (e) { toast(e.message, true); }
  };
  $('#btnStop').onclick = async () => {
    try { $('#btnStop').disabled = true; await api(`servers/${s.id}/stop`, { method: 'POST' }); toast('Server wird gestoppt …'); await refresh(); }
    catch (e) { toast(e.message, true); }
  };

  if (state.tab === 'overview') {
    $('#btnFolder').onclick = () => api(`servers/${s.id}/folder`, { method: 'POST' }).catch((e) => toast(e.message, true));
    $('#btnBackup').onclick = () => doBackup(s).then(() => loadOverviewExtras(s));
    if ($('#xboxCard')) bindXboxCard(s);            // nur Bedrock/Crossplay – Java-only und Modpacks zeigen eine andere Karte
    bindCompanionCard(s);
    bindCloudServerCard(s);
    state.xboxSig = JSON.stringify(s.xbox || {});
    loadOverviewExtras(s);
    if (!publicAddr(s)) ensurePublicIp();
  }

  if (state.tab === 'connect') {
    $('#btnPublicIp').onclick = async () => {
      const b = $('#btnPublicIp'); b.disabled = true;
      try { const d = await api('publicip'); state.publicIp = d.ip; patchPublicAddr(); }
      catch (e) { toast(e.message, true); } finally { b.disabled = false; }
    };
    $('#btnFirewall').onclick = async () => {
      const b = $('#btnFirewall'); b.disabled = true;
      try { const d = await api(`servers/${s.id}/firewall`, { method: 'POST' }); toast(d.message); }
      catch (e) { toast(e.message, true); } finally { b.disabled = false; }
    };
    const pm = async (remove) => {
      const b = $(remove ? '#btnPortmapRemove' : '#btnPortmap'); b.disabled = true;
      $('#pmResult').innerHTML = '<span class="muted small">FritzBox wird angefragt …</span>';
      try {
        const d = await api(`servers/${s.id}/portmap${remove ? '/remove' : ''}`, { method: 'POST' });
        $('#pmResult').innerHTML = pmHtml(d, remove);
        toast(d.ok ? (remove ? 'Freigaben entfernt.' : 'Portfreigaben angelegt.') : 'Nicht alle Freigaben konnten geändert werden.', !d.ok);
      } catch (e) { toast(e.message, true); $('#pmResult').textContent = e.message; } finally { b.disabled = false; }
    };
    $('#btnPortmap').onclick = () => pm(false);
    $('#btnPortmapRemove').onclick = () => pm(true);
  }

  if (state.tab === 'players') { state.players.running = s.running; loadPlayers(s); }

  if (state.tab === 'console') {
    const send = async () => {
      const inp = $('#cmdInput'); const cmd = inp.value.trim(); if (!cmd) return;
      try { await api(`servers/${s.id}/command`, { method: 'POST', body: { command: cmd } }); inp.value = ''; }
      catch (e) { toast(e.message, true); }
    };
    $('#cmdSend').onclick = send;
    $('#cmdInput').onkeydown = (e) => { if (e.key === 'Enter') send(); };
    state.consoleNext = 0;
    $('#console').innerHTML = '';
    startConsolePolling();
  }

  if (state.tab === 'files') bindFiles(s);

  if (state.tab === 'settings') {
    bindSettingsForm('st_');
    $('#btnSave').onclick = async () => {
      try {
        const body = readSettingsForm('st_', {});
        if (s.type === 'java' && body.geyser && body.port === body.bedrock_port) { toast('Java-Port und Bedrock-Port müssen sich unterscheiden.', true); return; }
        await api(`servers/${s.id}/settings`, { method: 'POST', body });
        const crossplayOn = s.type === 'java' && body.geyser && !s.geyser;
        toast('Einstellungen gespeichert.'); await refresh(); render();
        if (crossplayOn) toast('Crossplay eingeschaltet – jetzt „Neu installieren“ klicken, damit Geyser und Floodgate geladen werden.', true);
      } catch (e) { toast(e.message, true); }
    };
    if (isModpack(s)) bindSettingsModpack(s);
    else $('#btnReinstall').onclick = async () => {
      const version = $('#st_version').value.trim();
      if (!(await askConfirm({
        tone: 'danger', title: 'Server-Software neu laden?', confirmText: 'Neu laden',
        text: `Für „${s.name}“ wird die Server-Software als Version ${version} neu `
          + 'heruntergeladen und eingerichtet. Die Welt und die Einstellungen bleiben erhalten.',
      }))) return;
      try {
        const d = await api(`servers/${s.id}/reinstall`, { method: 'POST', body: { version } });
        state.install = { serverId: s.id, jobId: d.job_id, job: null, after: 'overview' }; state.view = 'install'; render(); pollJob();
      } catch (e) { toast(e.message, true); }
    };
    $('#btnDelete').onclick = async () => {
      if (!(await askConfirm({
        tone: 'danger', title: `„${s.name}“ endgültig löschen?`, confirmText: 'Endgültig löschen',
        text: 'Der ganze Ordner dieses Servers wird von diesem PC entfernt – mit Welt, Plugins, '
          + 'Einstellungen und Sicherungen. Das lässt sich nicht zurücknehmen.',
      }))) return;
      try { await api(`servers/${s.id}`, { method: 'DELETE' }); toast('Server gelöscht.'); await refresh(); state.activeId = state.servers[0]?.id || null; state.view = state.activeId ? 'server' : 'welcome'; render(); }
      catch (e) { toast(e.message, true); }
    };
    if (s.type === 'java' && !isModpack(s)) {
      loadVersions('java').then(() => {
        const sel = $('#st_version'); const list = state.versions.java;
        if (sel && Array.isArray(list)) sel.innerHTML = list.map((v) => `<option ${v === s.version ? 'selected' : ''}>${esc(v)}</option>`).join('');
        else if (list && list.error) toast('Paper-Versionen konnten nicht geladen werden: ' + list.error, true);
      });
    }
  }
}

/* Aktualisiert nur Status-Elemente, ohne Formulare neu zu rendern. */
function patchStatus() {
  if (state.view !== 'server') return;
  const s = server();
  if (!s) return;
  const dot = $('[data-status="dot"]'), big = $('[data-status="big"]'), hdot = $('[data-status="hdot"]');
  if (dot) dot.className = 'dot ' + (s.running ? 'dot-on' : '');
  if (hdot) hdot.className = 'h-dot ' + (s.running ? 'on' : '');
  $$('[data-status="text"]').forEach((el) => el.textContent = statusSub(s));   // Kopfzeile und Hero
  if (big) big.textContent = statusBig(s);
  const start = $('#btnStart'), stop = $('#btnStop');
  if (start) {
    start.disabled = s.running || !s.installed || s.installing || s.cloud_locked;
    start.title = s.cloud_locked ? 'Dieser Server liegt gerade auf dem Root-Server – zuerst zurückholen.' : '';
  }
  if (stop) stop.disabled = !s.running;
  // Zieht der Server auf den Root-Server um (oder zurück), muss die Karte in der Übersicht neu.
  const cloudSig = JSON.stringify([s.cloud_locked, (s.cloud || {}).state, (s.cloud || {}).instance, !!cloudData().logged_in]);
  if (state.tab === 'overview' && cloudSig !== state.cloudCardSig) { state.cloudCardSig = cloudSig; render(); return; }
  const inp = $('#cmdInput'), send = $('#cmdSend');
  if (inp) { inp.disabled = !s.running; inp.placeholder = s.running ? 'Befehl eingeben, z. B. list  oder  say Hallo' : 'Server starten, um Befehle zu senden'; }
  if (send) send.disabled = !s.running;
  if (state.tab === 'overview') {
    const sig = JSON.stringify(s.xbox || {});
    if (sig !== state.xboxSig) { state.xboxSig = sig; const card = $('#xboxCard'); if (card) { card.innerHTML = xboxCard(s); bindXboxCard(s); } }
    const csig = JSON.stringify([s.running, s.hardcore, s.companion || {}]);
    if (csig !== state.companionSig) { state.companionSig = csig; const card = $('#companionCard'); if (card) { card.innerHTML = companionCard(s); bindCompanionCard(s); } }
    const bk = $('#btnBackup'); if (bk && bk.textContent.indexOf('läuft') < 0) bk.disabled = s.running;
    if (s.running) loadMiniLog(s);
  }
  if (state.tab === 'players' && state.players.running !== s.running) {
    // Server gestartet oder gestoppt: Knöpfe und Hinweise der Spielerverwaltung stimmen sonst nicht mehr.
    state.players.running = s.running;
    loadPlayers(s);
  }
  if (state.tab === 'files') {
    const bk = $('#fBackup'); if (bk) bk.disabled = s.running;
    const sv = $('#fSave');
    if (sv && isPropsPath(state.files.open)) { sv.disabled = s.running; sv.title = s.running ? 'Server zuerst stoppen' : ''; }
  }
  if (state.tab === 'settings') {
    const wasLocked = !!$('.note-warn.mb0');
    if (wasLocked !== (s.running || !!s.installing)) render();
  }
}

/* ------------------------------------------------------------------ Assistent: Xbox-Freunde-Modus */

const XBOX_STEPS = ['So funktioniert es', 'Deine Angaben', 'Einrichtung', 'Anmeldung'];

function startXboxWizard(s) {
  const x = s.xbox || {};
  state.xbox = {
    serverId: s.id, step: 0,
    host_name: s.xbox_host_name || s.name,
    mode: s.xbox_address && s.xbox_address !== state.system.local_ip ? 'inet' : 'lan',
    address: s.xbox_address && s.xbox_address !== state.system.local_ip ? s.xbox_address : (s.public_address || ''),
    autostart: s.xbox_autostart !== false,
    jobId: null, job: null, status: x,
  };
  state.view = 'xbox';
  render();
}

function renderXboxWizard() {
  const w = state.xbox, s = state.servers.find((x) => x.id === w.serverId);
  if (!s) return renderWelcome();
  const lanIp = state.system.local_ip || '…';
  const port = s.type === 'bedrock' ? s.port : s.bedrock_port;
  let body = '';
  if (w.step === 0) body = `
    <p class="lead">Ein <b>Bot-Konto</b> meldet sich auf diesem PC bei Xbox Live an und „hostet“ deinen Server scheinbar als Welt.
       Jeder, der mit diesem Konto befreundet ist, sieht den Server unter <b>Freunde</b> und tritt mit einem Klick bei –
       auf Xbox, PS5, Switch, Handy und PC. Kein DNS-Trick nötig.</p>
    <div class="grid2">
      <div class="card"><div class="pill pill-green">Du brauchst</div>
        <ul style="margin:8px 0 0"><li>Ein <b>Microsoft-Konto als Bot</b>. Empfohlen ist ein <b>Zweitkonto</b> (kostenlos anlegbar), nicht dein Spielkonto –
            das Werkzeug ahmt einen Spieler nach, Microsoft könnte das theoretisch sperren.</li>
          <li>Das Konto braucht einen <b>Xbox-Gamertag</b> (wird beim ersten Besuch von <code>xbox.com</code> angelegt).</li>
          <li>Deine Freunde <b>folgen dem Bot-Konto</b> auf Xbox Live – es folgt automatisch zurück.</li></ul></div>
      <div class="card"><div class="pill pill-blue">Gut zu wissen</div>
        <ul style="margin:8px 0 0"><li>Der Bot leitet Freunde nur zur Server-Adresse. <b>Freunde außerhalb deines Netzes</b> brauchen deshalb weiterhin die
            Portfreigabe (Tab „Verbinden“). Im gleichen WLAN klappt es sofort.</li>
          <li>Der Bot läuft nur, solange der Server läuft, und startet mit ihm.</li>
          <li>Verwendet wird das quelloffene Projekt <b>MCXboxBroadcast</b> (github.com/MCXboxBroadcast) – wird automatisch geladen.</li></ul></div>
    </div>`;
  else if (w.step === 1) body = `
    <p class="lead">Ein paar Angaben – alles lässt sich später ändern.</p>
    <div class="grid2" style="margin-top:0">
      <div>
        ${fieldInput('xb_host', 'Anzeigename in der Freundesliste', w.host_name, { hint: 'So heißt die „Welt“, die deine Freunde sehen.' })}
        ${fieldCheck('xb_auto', 'Automatisch mit dem Server starten', 'Empfohlen. Der Bot startet und stoppt zusammen mit dem Server.', w.autostart)}
      </div>
      <div>
        <div class="field"><label>Wohin sollen Freunde geleitet werden?</label>
          <label class="check"><input type="radio" name="xb_mode" value="lan" ${w.mode === 'lan' ? 'checked' : ''}>
            <span><div class="t">Heimnetz · <code>${esc(lanIp)}:${port}</code></div><div class="d">Für Mitspieler im selben WLAN. Funktioniert ohne Portfreigabe.</div></span></label>
          <label class="check"><input type="radio" name="xb_mode" value="inet" ${w.mode === 'inet' ? 'checked' : ''}>
            <span><div class="t">Internet · öffentliche IP oder MyFRITZ!-Adresse</div><div class="d">Für Freunde außerhalb. Portfreigabe in FritzBox und Windows-Firewall nötig (Tab „Verbinden“).</div>
            <div class="flex" style="margin-top:8px"><input type="text" id="xb_addr" placeholder="z. B. 84.12.34.56 oder abc123.myfritz.net" value="${esc(w.address)}">
              <button class="btn btn-sm" id="xb_pub" type="button">IP ermitteln</button></div></span></label>
        </div>
      </div>
    </div>`;
  else if (w.step === 2) {
    const job = w.job;
    const steps = job ? job.steps.map((st) => `<div class="steprow ${st.state}"><span class="mark">${st.state === 'done' ? '✓' : st.state === 'failed' ? '!' : ''}</span>${esc(st.text)}</div>`).join('') : '';
    body = `<div class="card"><div class="bar"><i style="width:${job ? job.pct : 0}%"></i></div>
      <div class="muted small">${esc(job ? job.detail : 'Wird gestartet …')}</div><div class="steplist">${steps}</div>
      ${job && job.status === 'error' ? `<div class="note note-err"><b>Einrichtung fehlgeschlagen.</b><p class="mb0">${esc(job.error)}</p></div>` : ''}</div>`;
  } else {
    const x = w.status || {};
    if (x.state === 'online') body = `<div class="note note-ok"><b>Geschafft${x.gamertag ? ' – angemeldet als ' + esc(x.gamertag) : ''}!</b>
      <p>Der Server erscheint jetzt bei allen Freunden des Bot-Kontos unter <b>Freunde</b> als „${esc(w.host_name)}“.</p>
      <p class="mb0"><b>Für neue Mitspieler:</b> Auf Xbox/PS5/Switch/Handy den Gamertag des Bot-Kontos als Freund hinzufügen – der Bot nimmt automatisch an.
      Danach im Spiel auf <b>Freunde</b> gehen und beitreten.</p></div>`;
    else if (x.state === 'login') body = `<div class="card" style="text-align:center">
      <p class="lead">Jetzt das <b>Bot-Konto</b> anmelden:</p>
      <ol class="steps" style="text-align:left;display:inline-block"><li><a href="${MS_LINK_URL}" target="_blank" rel="noopener noreferrer">microsoft.com/link</a> öffnen (Knopf unten)</li>
        <li>Diesen Code eingeben:</li></ol>
      <div class="code-big">${esc(x.code)}</div>
      <ol class="steps" start="3" style="text-align:left;display:inline-block"><li>Mit dem <b>Bot-Konto</b> anmelden (nicht mit deinem Spielkonto)</li><li>Zurück hierher – die Seite erkennt die Anmeldung automatisch</li></ol>
      <div class="btn-row" style="justify-content:center;margin-top:10px"><a class="btn btn-primary" href="${MS_LINK_URL}" target="_blank" rel="noopener noreferrer">microsoft.com/link öffnen</a>
        <button class="btn" id="xb_copy">Code kopieren</button></div>
      <p class="muted small" style="margin-top:12px">Der Code ist ca. 15 Minuten gültig; danach erscheint automatisch ein neuer.</p></div>`;
    else body = `<div class="card"><div class="steprow active"><span class="mark"></span> Bot startet … gleich erscheint der Anmelde-Code${x.token_cached ? ' (oder die gespeicherte Anmeldung wird verwendet)' : ''}.</div>
      ${x.error ? `<div class="small" style="color:var(--red);margin-top:8px">${esc(x.error)}</div>` : ''}</div>`;
  }
  const canBack = w.step === 1 || (w.step === 0);
  return `
  <div class="head"><div><h1>Xbox-Freunde-Modus</h1><div class="head-sub">${esc(s.name)} · Schritt ${w.step + 1} von 4 · ${XBOX_STEPS[w.step]}</div></div>
    <button class="btn btn-sm" id="xbCancel">${w.step >= 2 ? 'Zur Übersicht' : 'Abbrechen'}</button></div>
  <div class="page">
    ${stepBar(XBOX_STEPS, w.step)}
    ${body}
    <div class="wizard-nav">
      <button class="btn" id="xbBack" ${w.step === 1 ? '' : 'disabled'}>← Zurück</button>
      ${w.step === 0 ? '<button class="btn btn-primary" id="xbNext">Weiter →</button>' : ''}
      ${w.step === 1 ? '<button class="btn btn-primary" id="xbGo">Einrichten &amp; Bot starten</button>' : ''}
      ${w.step === 3 && w.status && w.status.state === 'online' ? '<button class="btn btn-primary" id="xbDone">Fertig →</button>' : ''}
    </div>
  </div>`;
}

function bindXboxWizard() {
  const w = state.xbox;
  $('#xbCancel').onclick = () => openServer(w.serverId);
  $('#xbBack').onclick = () => { collectXbox(); w.step = 0; render(); };
  const next = $('#xbNext'); if (next) next.onclick = () => { w.step = 1; render(); };
  const pub = $('#xb_pub'); if (pub) pub.onclick = async () => {
    pub.disabled = true;
    try { const d = await api('publicip'); $('#xb_addr').value = d.ip; $$('input[name=xb_mode]').forEach((r) => r.checked = r.value === 'inet'); }
    catch (e) { toast(e.message, true); } finally { pub.disabled = false; }
  };
  const go = $('#xbGo'); if (go) go.onclick = async () => {
    collectXbox();
    if (w.mode === 'inet' && !w.address) { toast('Bitte die öffentliche Adresse eintragen oder „Heimnetz“ wählen.', true); return; }
    go.disabled = true;
    try {
      const d = await api(`servers/${w.serverId}/xbox/setup`, { method: 'POST', body: {
        xbox_host_name: w.host_name, xbox_address: w.mode === 'lan' ? '' : w.address, xbox_autostart: w.autostart } });
      w.jobId = d.job_id; w.job = null; w.step = 2; render(); pollXboxJob();
    } catch (e) { toast(e.message, true); go.disabled = false; }
  };
  const copy = $('#xb_copy'); if (copy) copy.onclick = () => navigator.clipboard?.writeText(w.status.code).then(() => toast('Code kopiert.'));
  const done = $('#xbDone'); if (done) done.onclick = () => openServer(w.serverId);
  if (w.step === 3) pollXboxStatus();
}

function collectXbox() {
  const w = state.xbox;
  const host = $('#xb_host'); if (host) w.host_name = host.value.trim() || w.host_name;
  const auto = $('#xb_auto'); if (auto) w.autostart = auto.checked;
  const mode = $('input[name=xb_mode]:checked'); if (mode) w.mode = mode.value;
  const addr = $('#xb_addr'); if (addr) w.address = addr.value.trim();
}

function pollXboxJob() {
  // Eigener Timer-Platz: render() räumt state.timers.xbox (Status-Abfrage) ab – dieser Poller ruft
  // selbst render() auf und würde sich sonst nach dem ersten Tick abschalten.
  clearInterval(state.timers.xboxJob);
  state.timers.xboxJob = setInterval(async () => {
    const w = state.xbox;
    if (state.view !== 'xbox' || !w || w.step !== 2) { clearInterval(state.timers.xboxJob); return; }
    try {
      w.job = await api('job/' + w.jobId);
      if (w.job.status === 'done') { clearInterval(state.timers.xboxJob); w.step = 3; await refresh(); render(); return; }
      render();
      if (w.job.status !== 'running') clearInterval(state.timers.xboxJob);
    } catch (e) { clearInterval(state.timers.xboxJob); toast(e.message, true); }
  }, 800);
}

function pollXboxStatus() {
  clearInterval(state.timers.xbox);
  const tick = async () => {
    const w = state.xbox;
    if (state.view !== 'xbox' || !w || w.step !== 3) { clearInterval(state.timers.xbox); return; }
    try {
      const st = await api(`servers/${w.serverId}/xbox`);
      const changed = JSON.stringify(st) !== JSON.stringify(w.status);
      w.status = st;
      if (changed) render();
    } catch { /* nächster Tick */ }
  };
  tick();
  state.timers.xbox = setInterval(tick, 2000);
}

/* ------------------------------------------------------------------ Konsole */

function classify(line) {
  if (line.startsWith('> ')) return 'l-me';
  if (line.startsWith('[Manager]')) return 'l-sys';
  if (/ERROR|SEVERE|Exception|FATAL/.test(line)) return 'l-err';
  if (/WARN/.test(line)) return 'l-warn';
  return 'l-info';
}

function startConsolePolling() {
  stopConsolePolling();
  const tick = async () => {
    const s = server(); const box = $('#console');
    if (!s || !box) return stopConsolePolling();
    try {
      const d = await api(`servers/${s.id}/console?since=${state.consoleNext}`);
      if (d.next < state.consoleNext) { box.innerHTML = ''; }
      if (d.lines.length) {
        const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
        box.insertAdjacentHTML('beforeend', d.lines.map((l) => `<div class="${classify(l)}">${esc(l)}</div>`).join(''));
        while (box.children.length > 3000) box.removeChild(box.firstChild);
        if (atBottom) box.scrollTop = box.scrollHeight;
      }
      state.consoleNext = d.next;
    } catch { /* Verbindung weg – beim nächsten Tick erneut */ }
  };
  tick();
  state.timers.console = setInterval(tick, 900);
}
function stopConsolePolling() { clearInterval(state.timers.console); state.timers.console = null; }

/* ------------------------------------------------------------------ Cloud (Root-Server)
   Gehostete Server laufen auf dem Root-Server (api.arcardia-nexus.de) und werden von hier aus
   gesteuert. Das Sitzungstoken bleibt im Programm – die Oberfläche bekommt es nie zu sehen.
   Angemeldet wird ausschließlich über Discord; kennt der Root-Server das Discord-Konto noch
   nicht, fragt die Oberfläche einmal den Einladungscode nach (siehe cloudAskInvite). */

const CLOUD_STATE = {
  local_only: 'nur auf diesem PC', uploading: 'wird hochgeladen', hosted: 'auf dem Root-Server',
  awaiting_pull: 'wartet auf Rückholung', downloading: 'wird zurückgeholt', suspended: 'ruht',
};
const ROLE_TEXT = { user: 'Benutzer', admin: 'Betreiber', owner: 'Betreiber' };
const cloudData = () => state.cloud.data || {};
const cloudStateText = (x) => CLOUD_STATE[x] || x || '–';
const cloudPullList = () => (cloudData().pull || []);
const cloudUser = () => cloudData().user || {};

/* ---------- kleine Helfer für Zeiten, Größen und Zahlen */

const nowSec = () => Math.floor(Date.now() / 1000);

/** Restzeit in Worten: „2 Tage 5 Std.“, „12 Min. 30 Sek.“, „abgelaufen“. */
function fmtLeft(sec) {
  sec = Math.floor(Number(sec) || 0);
  if (sec <= 0) return 'abgelaufen';
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (d) return `${d} ${d === 1 ? 'Tag' : 'Tage'} ${h} Std.`;
  if (h) return `${h} Std. ${m} Min.`;
  if (m) return `${m} Min. ${s} Sek.`;
  return `${s} Sek.`;
}
/** Kurzform für Ringe und Kacheln: „12 Tage“, „5 Std.“, „40 Min.“. */
function fmtLeftShort(sec) {
  sec = Math.floor(Number(sec) || 0);
  if (sec <= 0) return 'aus';
  if (sec >= 86400) { const d = Math.round(sec / 86400); return `${d} ${d === 1 ? 'Tag' : 'Tage'}`; }
  if (sec >= 3600) return `${Math.round(sec / 3600)} Std.`;
  if (sec >= 60) return `${Math.round(sec / 60)} Min.`;
  return `${sec} Sek.`;
}
/** „seit 3 Tagen“ – für die Zeile unter dem Namen. */
function fmtSince(ts) {
  if (!ts) return '';
  const s = Math.max(0, nowSec() - Number(ts));
  if (s < 120) return 'gerade eben';
  if (s < 5400) return `seit ${Math.round(s / 60)} Min.`;
  if (s < 172800) return `seit ${Math.round(s / 3600)} Std.`;
  const d = Math.round(s / 86400);
  return `seit ${d} ${d === 1 ? 'Tag' : 'Tagen'}`;
}
/** Arbeitsspeicher mit deutschem Komma: „512 MB“, „4 GB“, „2,7 GB“. */
function fmtMb(mb) {
  const n = Number(mb) || 0;
  if (n < 1024) return Math.round(n) + ' MB';
  const g = n / 1024;
  return (g % 1 ? g.toFixed(1).replace('.', ',') : String(Math.round(g))) + ' GB';
}
const komma = (n, stellen = 1) => Number(n).toFixed(stellen).replace('.', ',');
const fmtRate = (bps) => !bps || bps < 1 ? '' : bps < 1048576 ? (bps / 1024).toFixed(0) + ' KB/s' : komma(bps / 1048576) + ' MB/s';
/** Erste Zahl, die wirklich geliefert wurde (der Root nennt nicht alles). */
function firstNum(list) {
  for (const v of list) {
    if (v === null || v === undefined || v === '') continue;
    const n = Number(v);
    if (!Number.isNaN(n)) return n;
  }
  return null;
}
/** Einladungscode formatieren: ABCD-EFGH-IJKL */
const fmtCode = (s) => (String(s || '').toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 12).match(/.{1,4}/g) || []).join('-');

/* ---------- Bausteine: Ring, Balken, Kennzahl, Bild */

function ringBox(pct, big, small, tone = '') {
  const r = 46, c = 2 * Math.PI * r;
  const off = c * (1 - Math.max(0, Math.min(1, Number(pct) || 0)));
  return `<div class="ring ${tone}">
    <svg viewBox="0 0 112 112" width="112" height="112" aria-hidden="true" focusable="false">
      <circle class="ring-bg" cx="56" cy="56" r="${r}"></circle>
      <circle class="ring-fg" cx="56" cy="56" r="${r}" stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}"></circle>
    </svg>
    <div class="ring-txt"><div class="ring-v">${big}</div><div class="ring-k">${esc(small)}</div></div>
  </div>`;
}

function meterBox(label, value, max, text, tone = '') {
  const pct = max > 0 ? Math.min(100, Math.round((Number(value) || 0) / max * 100)) : 0;
  return `<div class="meter">
    <div class="meter-top"><span class="meter-k">${esc(label)}</span><span class="meter-v">${text}</span></div>
    <div class="bar ${tone}" role="img" aria-label="${esc(label)}: ${pct} Prozent belegt"><i style="width:${pct}%"></i></div>
  </div>`;
}

const statBox = (k, v, cls = '') => `<div class="ministat ${cls}"><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`;

/** Bild des Discord-Kontos. Geladen wird **nur** von cdn.discordapp.com. */
function cloudAvatar(u, size = 'av-36') {
  const src = String((u || {}).avatar_url || '');
  const name = String((u || {}).name || '').trim();
  if (src.startsWith('https://cdn.discordapp.com/')) {
    return `<img class="avatar ${size}" src="${esc(src)}" alt="" loading="lazy" referrerpolicy="no-referrer">`;
  }
  return `<span class="avatar ${size} avatar-txt" aria-hidden="true">${esc(name.charAt(0).toUpperCase() || '☁')}</span>`;
}

/* ---------- Laden, Zeichnen, Takt */

function openCloud() {
  state.view = 'cloud';
  render();
  loadCloud(true);
  startCloudPolling();
}
window.openCloud = openCloud;

async function loadCloud(force = false) {
  try {
    const d = await api('cloud/status' + (force ? '?force=1' : ''));
    state.cloud.data = d;
    state.cloud.error = d.auth_error || d.error || '';
  } catch (e) { state.cloud.error = e.message; }
  state.cloud.loading = false;
  renderCloudBanner();
  renderCloudNav();
  paintCloud();
  // Die Erstanmeldung wartet auf den Einladungscode: einmal nachfragen, nicht bei jedem Takt.
  const d = cloudData();
  if (d.needs_invite && !state.cloud.askingInvite && !modalOpen()) cloudAskInvite(d.invite_discord);
}

/** Fingerabdruck alles Sichtbaren – ändert sich nichts, wird auch nicht neu gezeichnet. */
function cloudSig() {
  const d = cloudData();
  const t = state.cloud.job;
  const c = state.cloud;
  return JSON.stringify([
    d.logged_in, d.error, d.needs_invite, d.user, d.passes, d.limits, d.limits_text, d.notices,
    d.machine, d.slots_used, d.ram_used_mb, d.transfer, d.logged_in_at, d.session_expires_at, d.base,
    (d.servers || []).map((s) => [s.id, s.name, s.state, s.state_text, s.running, s.address, s.port,
      s.ram_text, s.size_text, s.can_pull, s.live, s.players_online, s.tps, s.local_name, s.version]),
    state.servers.map((s) => [s.id, s.name, s.installed, s.running, s.installing, s.cloud_locked,
      s.cloud, s.version, s.ram_mb, s.type, s.flavor]),
    c.open, c.busy, c.error, c.loading, c.login, c.hideBanner,
    c.files.path, c.files.open, c.files.entries, c.files.error,
    t ? [t.id, t.label, t.job && [t.job.pct, t.job.detail, t.job.status, t.job.steps]] : null,
  ]);
}

/** Neu zeichnen, ohne dem Benutzer die Arbeit wegzunehmen. */
function paintCloud() {
  if (state.view !== 'cloud') return;
  // Nicht neu zeichnen, während jemand tippt oder ein Dialog offen ist – sonst ist die
  // Eingabe weg bzw. der Dialog hängt an Elementen, die es nicht mehr gibt.
  const act = document.activeElement;
  if (modalOpen()) return;
  if (act && /^(INPUT|TEXTAREA|SELECT)$/.test(act.tagName) && $('#view').contains(act)) return;
  // Hat sich nichts geändert, bleibt die Seite stehen: kein Flackern, keine verlorenen Klicks.
  if (cloudSig() === state.cloud.sig) return;
  const main = $('#main');
  const top = main ? main.scrollTop : 0;
  const log = $('#cloudLog');
  const kleben = !log || (log.scrollHeight - log.scrollTop - log.clientHeight) < 48;
  $('#view').innerHTML = renderCloud();
  bindCloud();
  if (main) main.scrollTop = top;
  const log2 = $('#cloudLog');
  if (log2 && kleben) log2.scrollTop = log2.scrollHeight;
}

function startCloudPolling() {
  clearInterval(state.timers.cloud);
  state.timers.cloud = setInterval(() => loadCloud(), 6000);
  clearInterval(state.timers.cloudTick);
  state.timers.cloudTick = setInterval(cloudTick, 1000);
}
function stopCloudPolling() {
  clearInterval(state.timers.cloud); state.timers.cloud = null;
  clearInterval(state.timers.cloudLog); state.timers.cloudLog = null;
  clearInterval(state.timers.cloudTick); state.timers.cloudTick = null;
}

/** Sekundentakt: nur die Countdown-Texte auffrischen, nichts neu zeichnen. */
function cloudTick() {
  $$('[data-until]').forEach((el) => {
    const rest = Number(el.dataset.until) - nowSec();
    const neu = el.dataset.style === 'short' ? fmtLeftShort(rest) : fmtLeft(rest);
    if (el.textContent !== neu) el.textContent = neu;
    el.classList.toggle('is-urgent', rest > 0 && rest < 3600);
  });
}

function renderCloudNav() {
  const el = $('#navCloud');
  if (!el) return;
  el.classList.toggle('active', state.view === 'cloud');
  const badge = $('#cloudBadge');
  if (badge) {
    const n = cloudPullList().length;
    badge.className = n ? 'pill pill-amber' : 'right';
    badge.textContent = n ? String(n) : '';
  }
  const side = $('#cloudSide');
  if (!side) return;
  const d = cloudData();
  const u = d.user || {};
  const html = !d.logged_in
    ? `<button class="side-acct is-off" id="sideAcct">
         <span class="avatar av-24 avatar-txt" aria-hidden="true">☁</span>
         <span class="sa-txt"><span class="sa-name">Nicht angemeldet</span>
           <span class="sa-meta">Mit Discord anmelden</span></span></button>`
    : `<button class="side-acct" id="sideAcct" aria-label="Konto-Einstellungen öffnen">
         ${cloudAvatar(u, 'av-24')}
         <span class="sa-txt"><span class="sa-name">${esc(u.name || 'Konto')}</span>
           <span class="sa-meta">${esc(u.role_text || ROLE_TEXT[u.role] || 'Benutzer')}${
             d.logged_in_at ? ' · ' + esc(fmtSince(d.logged_in_at)) : ''}</span></span></button>`;
  // Nur schreiben, wenn sich wirklich etwas geändert hat – sonst geht bei jedem Takt der
  // Zeigerzustand (und bei Tastaturbedienung der Fokus) verloren.
  if (side.dataset.stand === html) return;
  side.dataset.stand = html;
  side.innerHTML = html;
  const b = $('#sideAcct');
  if (b) b.onclick = d.logged_in ? () => { openCloud(); openAccountDialog(); } : openCloud;
}

/* ---------- Hinweis beim Programmstart: Server wartet auf die Rückholung */

function renderCloudBanner() {
  const box = $('#cloudBanner');
  if (!box) return;
  const pull = cloudPullList();
  if (!pull.length || state.cloud.hideBanner) { box.innerHTML = ''; return; }
  box.innerHTML = `
    <div class="cloud-banner">
      <div class="cb-head"><span class="cb-ico" aria-hidden="true">⬇</span>
        <div><div class="cb-title">${pull.length === 1 ? 'Ein Server wartet darauf, zurückgeholt zu werden'
          : `${pull.length} Server warten darauf, zurückgeholt zu werden`}</div>
          <div class="cb-sub">Der Pass ist abgelaufen oder zurückgezogen: die Server sind auf dem Root-Server
            gestoppt und die Welten gespeichert. Hole sie auf diesen PC – erst wenn alle Dateien geprüft
            angekommen sind, wird der Ordner auf dem Root-Server gelöscht.</div></div></div>
      ${pull.map((s) => `<div class="cb-row"><div><b>${esc(s.name)}</b>
        <span class="muted small">· ${esc(cloudStateText(s.state))}${s.size_text ? ' · ' + esc(s.size_text) : ''}</span></div>
        <button class="btn btn-sm btn-primary" data-cloud-pull="${esc(s.id)}">⬇ Jetzt zurückholen</button></div>`).join('')}
      <div class="btn-row" style="margin-top:10px">
        <button class="btn btn-sm" id="cbOpen">Bereich „Cloud“ öffnen</button>
        <button class="btn btn-sm" id="cbLater">Später</button></div>
    </div>`;
  $$('[data-cloud-pull]', box).forEach((el) => el.onclick = () => cloudDownload(el.dataset.cloudPull));
  $('#cbOpen').onclick = openCloud;
  $('#cbLater').onclick = () => { state.cloud.hideBanner = true; renderCloudBanner(); };
}

/* ---------- Seite „Cloud“: Kopfbereich */

function renderCloud() {
  state.cloud.sig = cloudSig();
  const d = cloudData();
  const body = state.cloud.loading && !state.cloud.data
    ? '<div class="card cl-skeleton">Der Stand wird geholt …</div>'
    : (d.logged_in ? cloudAccountPage(d) : cloudLoginPage(d));
  const fehler = state.cloud.error && !d.needs_invite
    ? `<div class="note note-err"><b>Der Root-Server hat einen Fehler gemeldet.</b>
       <p class="mb0">${esc(state.cloud.error)}</p></div>` : '';
  return cloudHead(d) + `<div class="page cloud-page">${fehler}${body}</div>`;
}

function cloudHead(d) {
  const verbindung = !d.logged_in
    ? { cls: 'is-off', text: 'nicht angemeldet' }
    : state.cloud.error ? { cls: 'is-bad', text: 'Verbindung gestört' }
      : { cls: 'is-ok', text: 'verbunden' };
  const host = String(d.base || '').replace(/^https?:\/\//, '');
  return `
  <div class="head cloud-head">
    <div class="head-main">
      <h1>Cloud</h1>
      <div class="head-sub">
        <span class="conn ${verbindung.cls}"><span class="conn-dot" aria-hidden="true"></span>${esc(verbindung.text)}</span>
        <span class="sep" aria-hidden="true">·</span>
        <span class="mono-sm">${esc(host)}</span>
      </div>
    </div>
    <div class="head-right">
      ${d.logged_in ? `<button class="acct" id="cloudAcct" aria-label="Konto-Einstellungen öffnen">
          ${cloudAvatar(d.user || {}, 'av-36')}
          <span class="acct-txt">
            <span class="acct-name">${esc((d.user || {}).name || 'Konto')}</span>
            <span class="acct-meta">${esc((d.user || {}).role_text || ROLE_TEXT[(d.user || {}).role] || 'Benutzer')}${
              d.logged_in_at ? ' · ' + esc(fmtSince(d.logged_in_at)) + ' angemeldet' : ''}</span>
          </span>
          <span class="acct-caret" aria-hidden="true">⌄</span>
        </button>` : ''}
      <button class="btn btn-sm" id="cloudReload">↻ Aktualisieren</button>
    </div>
  </div>`;
}

/* ---------- Nicht angemeldet: Schaufenster und der eine Knopf */

function cloudLoginPage(d) {
  const L = state.cloud.login;
  const domain = esc(d.domain || 'arcardia-nexus.de');
  return `
  <section class="cl-hero">
    <div class="cl-hero-main">
      <div class="pill pill-green">Hosted-Modus</div>
      <h2>Dein Server läuft weiter, auch wenn dieser PC aus ist</h2>
      <p class="lead">Der Server zieht in ein Rechenzentrum um: 8 Kerne, schnelle Leitung, kein Stromverbrauch
        bei dir zu Hause. Gesteuert wird er weiterhin aus diesem Programm – Konsole, Dateien, Einstellungen,
        alles wie gewohnt.</p>
      ${L.pending ? `
        <div class="note note-info">
          <b>Bitte im Browser bestätigen.</b>
          <p>Das Fenster hat sich geöffnet – nach der Bestätigung geht es hier von selbst weiter.
          ${L.url ? `<br>Öffnet sich nichts: <a href="${esc(L.url)}" target="_blank" rel="noopener noreferrer">Adresse hier öffnen</a>.` : ''}</p>
          <div class="btn-row"><button class="btn btn-sm" id="cloudLoginCancel">Abbrechen</button></div>
        </div>`
        : `<div class="btn-row">
             <button class="btn btn-primary btn-lg" id="cloudLoginDiscord">
               <span aria-hidden="true">🔗</span> Mit Discord anmelden</button>
           </div>
           <p class="muted small mb0">Dein Passwort sieht dieses Programm nie – Discord bestätigt nur, dass du du bist.
             Beim ersten Mal fragt der Root-Server danach einmal nach deinem Einladungscode.</p>`}
      ${L.error ? `<div class="note note-err mb0">${esc(L.error)}</div>` : ''}
    </div>
    <div class="cl-hero-side">
      <div class="addr addr-pub"><div>
        <div class="a-k">So sieht deine Adresse aus</div>
        <div class="a-v">mein-server.${domain}</div></div></div>
      <ul class="checklist">
        <li><span class="ck on" aria-hidden="true">✓</span><div>Keine Portfreigabe in der FritzBox, kein DynDNS, kein Problem mit DS-Lite.</div></li>
        <li><span class="ck on" aria-hidden="true">✓</span><div>Xbox, PlayStation, Switch, Handy und PC tippen dieselbe Adresse ein.</div></li>
        <li><span class="ck on" aria-hidden="true">✓</span><div>Deine Welt wird vollständig übertragen – mit Prüfsumme für jede Datei.</div></li>
        <li><span class="ck on" aria-hidden="true">✓</span><div>Zurückholen kannst du sie jederzeit.</div></li>
      </ul>
    </div>
  </section>

  <div class="grid3">
    <div class="card"><div class="pill pill-blue">Pass</div>
      <h3>Wie viel darf gleichzeitig laufen?</h3>
      <p class="small mb0">Das steht in deinem Pass – zum Beispiel „2 Server gleichzeitig, zusammen 8 GB“.
        Anlegen und Hochladen darfst du immer, begrenzt ist nur das Einschalten.</p></div>
    <div class="card"><div class="pill pill-green">Umzug</div>
      <h3>Ein Server, ein Ort</h3>
      <p class="small mb0">Solange ein Server auf dem Root-Server liegt, ist die Kopie auf diesem PC gesperrt.
        So kann keine Welt zweimal weiterlaufen und auseinanderdriften.</p></div>
    <div class="card"><div class="pill pill-grey">Ohne Anmeldung</div>
      <h3>Es ändert sich nichts</h3>
      <p class="small mb0">Deine Server auf diesem PC laufen weiter wie bisher. Der Hosted-Modus kommt
        obendrauf, er ersetzt nichts.</p></div>
  </div>`;
}

/* ---------- Angemeldet: die ganze Seite */

function cloudAccountPage(d) {
  return cloudNoticeBox(d)
    + cloudPassBox(d)
    + `<div class="cl-cols">
         <div class="cl-main">
           ${cloudTransferBox(d)}
           ${cloudDetailBox(d)}
           ${cloudRemoteBox(d)}
           ${cloudLocalBox(d)}
         </div>
         <div class="cl-side">
           ${cloudMachineBox(d)}
           ${cloudTipBox(d)}
         </div>
       </div>`;
}

/** Meldungen des Root-Servers – unverändert, sie kommen von dort. */
function cloudNoticeBox(d) {
  const list = d.notices || [];
  if (!list.length) return '';
  return `<div class="cl-notices">${list.map((n) => `<div class="note ${
    n.kind === 'expiring' || n.kind === 'pull' || n.kind === 'pull_running' ? 'note-warn' : 'note-info'}">
    ${esc(n.text)}</div>`).join('')}</div>`;
}

/* ---------- Pässe: Restzeit, Plätze, Arbeitsspeicher */

function cloudPassBox(d) {
  const passes = d.passes || [];
  const lim = d.limits || {};
  const active = passes.filter((p) => p.state === 'active');
  if (!passes.length) {
    return `<section class="card cl-pass is-empty">
      <div class="card-head"><h3>🎫 Dein Pass</h3></div>
      <div class="empty-inline">
        <div class="ei-ico" aria-hidden="true">🎫</div>
        <div>
          <b>Du hast noch keinen Pass.</b>
          <p class="mb0">Server anlegen und hochladen darfst du auch ohne Pass – <b>einschalten</b> lässt sich
            auf dem Root-Server aber erst mit einem gültigen Pass. Pässe stellt der Betreiber aus;
            frag ihn nach einem Tages-, Wochen- oder Monatspass.</p>
        </div>
      </div>
    </section>`;
  }
  const rest = active.map((p) => firstNum([p.remaining_seconds, p.expires_at ? p.expires_at - nowSec() : null]) || 0);
  const kuerzeste = active.length ? Math.min(...rest) : 0;
  const naechster = active.length ? active[rest.indexOf(kuerzeste)] : null;
  const ganz = naechster ? Math.max(1, (Number(naechster.expires_at) || 0) - (Number(naechster.issued_at) || 0)
    || (Number(naechster.days) || 1) * 86400) : 1;
  const anteil = naechster ? kuerzeste / ganz : 0;
  const knapp = active.length > 0 && kuerzeste > 0 && kuerzeste < 86400;
  const tone = !active.length ? 'is-grey' : knapp ? 'is-amber' : 'is-green';

  const slots = Number(d.slots_used || 0), slotsMax = Number(lim.max_concurrent || 0);
  const ram = Number(d.ram_used_mb || 0), ramMax = Number(lim.ram_total_mb || 0);

  return `<section class="card cl-pass">
    <div class="card-head"><h3>🎫 Dein Pass</h3>
      <span class="muted small">${esc(d.limits_text || 'kein gültiger Pass')}</span></div>
    <div class="pass-grid">
      <div class="pass-ring">
        ${ringBox(active.length ? anteil : 0,
          active.length ? `<span data-until="${nowSec() + Math.max(0, kuerzeste)}" data-style="short">${esc(fmtLeftShort(kuerzeste))}</span>` : '–',
          active.length ? 'Restlaufzeit' : 'kein gültiger Pass', tone)}
        ${naechster ? `<div class="pass-ring-sub">läuft ab am ${esc(fmtDate(naechster.expires_at))}<br>
          <span class="mono-sm" data-until="${Number(naechster.expires_at) || 0}">${esc(fmtLeft(kuerzeste))}</span></div>` : ''}
      </div>
      <div class="pass-meters">
        ${meterBox('Belegte Plätze', slots, slotsMax,
          `<b>${slots}</b> von ${slotsMax || '–'} Servern gleichzeitig`,
          slotsMax && slots >= slotsMax ? 'warn' : '')}
        ${meterBox('Arbeitsspeicher', ram, ramMax,
          `<b>${esc(fmtMb(ram))}</b> von ${esc(ramMax ? fmtMb(ramMax) : '–')} in Betrieb`,
          ramMax && ram / ramMax > 0.85 ? 'warn' : '')}
        <div class="pass-list">
          ${passes.map((p) => {
            const zustand = p.state === 'active' ? ['pill-green', 'gültig']
              : p.state === 'expired' ? ['pill-amber', 'abgelaufen'] : ['pill-grey', 'zurückgezogen'];
            return `<div class="pass-row">
              <div><div class="w-name">${esc(p.kind_text || p.kind)}
                  <span class="pill ${zustand[0]}">${zustand[1]}</span></div>
                <div class="w-meta">${p.max_concurrent} Server gleichzeitig · zusammen
                  ${esc(p.ram_total_text || fmtMb(p.ram_total_mb))}${p.note ? ' · ' + esc(p.note) : ''}</div></div>
              <div class="pass-rest">
                <div class="pr-v">${esc(p.remaining_text || '–')}</div>
                <div class="pr-k">${p.expires_at ? 'bis ' + esc(fmtDate(p.expires_at)) : ''}</div></div>
            </div>`;
          }).join('')}
        </div>
      </div>
    </div>
    ${knapp ? `<div class="note note-warn mb0"><b>Dein Pass läuft bald ab.</b>
      <p class="mb0">Noch <span data-until="${nowSec() + kuerzeste}">${esc(fmtLeft(kuerzeste))}</span>.
        Zehn Minuten vor Schluss bekommen die Spieler eine Ansage, danach werden die Server sauber
        gestoppt und die Welten gespeichert. Server aus einem lokalen Pass musst du danach auf
        diesen PC zurückholen – bitte rechtzeitig einen neuen Pass besorgen.</p></div>` : ''}
  </section>`;
}

/* ---------- Übertragungen: laufend, unterbrochen */

function cloudTransferBox(d) {
  const t = state.cloud.job;
  const merk = d.transfer || {};
  if (!t && !merk.kind) return '';
  if (!t && merk.kind) {
    // Nichts läuft, aber es steht noch eine angefangene Übertragung in der Merkdatei.
    const name = merk.kind === 'upload'
      ? (state.servers.find((s) => s.id === merk.local) || {}).name || 'Server'
      : ((d.servers || []).find((s) => s.id === merk.instance) || {}).name || 'Server';
    return `<section class="card cl-transfer is-paused">
      <div class="card-head"><h3>⏸ Unterbrochene Übertragung</h3>
        <span class="pill pill-amber">wartet</span></div>
      <p class="small">„${esc(name)}“ ${merk.kind === 'upload' ? 'wurde auf den Root-Server hochgeladen' : 'wurde auf diesen PC geholt'},
        die Übertragung ist aber abgebrochen. Fertige Dateien liegen schon drüben –
        beim Fortsetzen geht es genau dort weiter, nichts wird doppelt übertragen.</p>
      <div class="w-meta">${merk.files ? merk.files + ' Dateien · ' : ''}${merk.bytes ? fmtBytes(merk.bytes) : ''}
        ${merk.started ? '· begonnen am ' + esc(fmtDate(merk.started)) : ''}</div>
      <div class="btn-row">
        <button class="btn btn-sm btn-primary" id="cloudResume">↻ Fortsetzen</button>
        ${merk.kind === 'upload' ? `<button class="btn btn-sm btn-danger" id="cloudAbortT">Verwerfen</button>` : ''}
      </div>
    </section>`;
  }
  const job = t.job;
  const rate = cloudRate(job);
  const failed = job && job.status === 'error', done = job && job.status === 'done';
  const steps = job ? job.steps.map((st) => `<div class="steprow ${st.state}"><span class="mark">${
    st.state === 'done' ? '✓' : st.state === 'failed' ? '!' : ''}</span>${esc(st.text)}</div>`).join('') : '';
  const rest = rate && rate.bps > 0 && rate.total > rate.done
    ? fmtLeft(Math.round((rate.total - rate.done) / rate.bps)) : '';
  return `<section class="card cl-transfer">
    <div class="card-head"><h3>${done ? '✓ ' : failed ? '⚠ ' : '⇅ '}${esc(t.label)}</h3>
      ${done || failed ? '<button class="btn btn-sm" id="cloudJobClose">Schließen</button>'
        : `<span class="pill pill-blue">${job ? job.pct : 0} %</span>`}</div>
    <div class="bar ${failed ? 'warn' : ''}"><i style="width:${job ? job.pct : 0}%"></i></div>
    <div class="tr-line">
      <span class="muted small">${esc(job ? job.detail : 'Wird vorbereitet …')}</span>
      <span class="tr-rate">${rate && rate.bps ? esc(fmtRate(rate.bps)) : ''}${rest ? ' · noch etwa ' + esc(rest) : ''}</span>
    </div>
    <div class="steplist">${steps}</div>
    ${failed ? `<div class="note note-err mb0"><b>Die Übertragung ist fehlgeschlagen.</b>
        <p class="mb0">${esc(job.error)} Schon übertragene Dateien bleiben liegen – ein neuer Anlauf
        setzt dort auf, wo es abgerissen ist.</p></div>` : ''}
    ${done ? '<div class="note note-ok mb0"><b>Fertig.</b><p class="mb0">Alle Dateien sind geprüft angekommen.</p></div>' : ''}
  </section>`;
}

/** Geschwindigkeit aus „Hochladen: 12.5 / 300.0 MB“ – der Job rechnet in MB. */
function cloudRate(job) {
  const m = /([\d.,]+)\s*\/\s*([\d.,]+)\s*MB/.exec((job && job.detail) || '');
  if (!m) return null;
  const done = parseFloat(m[1].replace(',', '.')) * 1048576;
  const total = parseFloat(m[2].replace(',', '.')) * 1048576;
  const jetzt = Date.now();
  const r = state.cloud.rate;
  if (!r || done < r.bytes) { state.cloud.rate = { at: jetzt, bytes: done, bps: 0 }; return { done, total, bps: 0 }; }
  const dt = (jetzt - r.at) / 1000;
  if (dt >= 1.2) {
    const sofort = (done - r.bytes) / dt;
    // Geglättet, sonst springt die Zahl bei jedem Stück von 0 auf 40 MB/s und zurück.
    state.cloud.rate = { at: jetzt, bytes: done, bps: r.bps ? r.bps * 0.6 + sofort * 0.4 : sofort };
  }
  return { done, total, bps: state.cloud.rate.bps };
}

/* ---------- Server auf dem Root-Server */

function cloudRemoteBox(d) {
  const servers = d.servers || [];
  return `<section class="card">
    <div class="card-head"><h3>☁ Server auf dem Root-Server</h3>
      <span class="muted small">${servers.length ? servers.length + (servers.length === 1 ? ' Server' : ' Server, ' + servers.filter((s) => s.running).length + ' online') : 'noch keiner'}</span></div>
    ${!servers.length ? `<div class="empty-inline">
        <div class="ei-ico" aria-hidden="true">☁</div>
        <div><b>Hier ist noch nichts.</b>
          <p class="mb0">Weiter unten stehen deine Server auf diesem PC – bei jedem gibt es den Knopf
            <b>„Auf den Root-Server verschieben“</b>. Der Server muss dafür gestoppt sein. Nach dem
            Umzug erscheint er hier mit Adresse, Konsole und Dateiverwaltung.</p></div></div>`
      : `<div class="rsv-grid">${servers.map((s) => cloudRemoteCard(s)).join('')}</div>`}
  </section>`;
}

function cloudRemoteCard(s) {
  const busy = state.cloud.busy === s.id;
  const offen = state.cloud.open && state.cloud.open.id === s.id;
  const live = s.live || {};
  const running = !!s.running;
  const canStart = s.state === 'hosted' && !running;
  const spieler = firstNum([s.players_online, s.players, live.players_online, live.players]);
  const spielerMax = firstNum([s.players_max, live.players_max, s.max_players]);
  const tps = firstNum([s.tps, live.tps]);
  const laufzeit = firstNum([s.uptime, live.uptime]);
  const belegt = firstNum([live.memory_mb, s.memory_mb]);
  const ramText = esc(s.ram_text || gb(s.ram_mb || 0));
  // Der Root-Server nennt die Adresse bei Bedrock schon mit Port – dann keinen zweiten anhängen.
  const roh = String(s.address || '');
  const adresse = roh && !roh.includes(':') && s.port ? roh + ':' + s.port : roh;
  const zustand = s.state === 'hosted' ? (running ? ['pill-green', 'läuft'] : ['pill-grey', 'gestoppt'])
    : s.state === 'awaiting_pull' || s.state === 'downloading' ? ['pill-amber', esc(s.state_text || cloudStateText(s.state))]
      : ['pill-grey', esc(s.state_text || cloudStateText(s.state))];
  return `
  <article class="rsv ${running ? 'is-on' : ''}${offen ? ' is-open' : ''}${busy ? ' is-busy' : ''}">
    <header class="rsv-head">
      <span class="dot ${running ? 'dot-on' : ''}" aria-hidden="true"></span>
      <h4 class="rsv-name">${esc(s.name)}</h4>
      <span class="pill ${zustand[0]}">${zustand[1]}</span>
    </header>
    <div class="rsv-sub">${esc(s.type === 'bedrock' ? 'Bedrock' : 'Java')} ${esc(s.version || '')}
      · ${ramText}${s.size_text ? ' · ' + esc(s.size_text) : ''}${s.local_name ? ' · lokale Kopie: ' + esc(s.local_name) : ''}</div>
    ${s.state === 'hosted' && adresse ? `
      <div class="addr addr-pub">
        <div><div class="a-k">Adresse für deine Freunde</div><div class="a-v">${esc(adresse)}</div></div>
        <button class="btn btn-sm" data-copy="${esc(adresse)}" aria-label="Adresse von ${esc(s.name)} kopieren">⧉ Kopieren</button>
      </div>` : ''}
    <div class="rsv-stats">
      ${statBox('Zustand', running ? 'Online' : 'Offline', running ? 'ok' : '')}
      ${spieler !== null ? statBox('Spieler', esc(String(spieler)) + (spielerMax ? ' / ' + esc(String(spielerMax)) : '')) : ''}
      ${running && laufzeit ? statBox('Laufzeit', esc(fmtUptime(laufzeit))) : ''}
      ${statBox('Arbeitsspeicher', belegt ? esc(fmtMb(belegt)) + ' <span class="mini">von ' + ramText + '</span>' : ramText)}
      ${tps !== null ? statBox('TPS', esc(komma(tps)), tps >= 19 ? 'ok' : tps >= 15 ? 'warn' : 'bad') : ''}
    </div>
    <div class="rsv-btns btn-row">
      <button class="btn btn-sm btn-primary" data-cloud-start="${esc(s.id)}" ${canStart && !busy ? '' : 'disabled'}>▶ Starten</button>
      <button class="btn btn-sm btn-danger" data-cloud-stop="${esc(s.id)}" ${running && !busy ? '' : 'disabled'}>■ Stoppen</button>
      <button class="btn btn-sm" data-cloud-open="${esc(s.id)}">🖥 Konsole</button>
      <button class="btn btn-sm" data-cloud-files="${esc(s.id)}">📂 Dateien</button>
      <button class="btn btn-sm" data-cloud-pull="${esc(s.id)}" ${s.can_pull && !busy ? '' : 'disabled'}
        ${running ? 'title="Der Server läuft – er wird beim Zurückholen zuerst gestoppt."' : ''}>⬇ Zurückholen</button>
    </div>
  </article>`;
}

/* ---------- Konsole und Dateien eines entfernten Servers (fest eingebaut) */

function cloudDetailBox(d) {
  const open = state.cloud.open;
  if (!open) return '';
  const s = (d.servers || []).find((x) => x.id === open.id);
  if (!s) return '';
  const tab = open.tab === 'files' ? 'files' : 'console';
  return `<section class="card cl-detail">
    <div class="card-head cl-detail-head">
      <div class="flex">
        <span class="dot ${s.running ? 'dot-on' : ''}" aria-hidden="true"></span>
        <h3>${esc(s.name)}</h3>
        <span class="pill ${s.running ? 'pill-green' : 'pill-grey'}">${s.running ? 'läuft' : 'gestoppt'}</span>
      </div>
      <div class="flex">
        <div class="seg" role="tablist" aria-label="Ansicht wählen">
          <button role="tab" aria-selected="${tab === 'console'}" class="${tab === 'console' ? 'on' : ''}" data-cloud-tab="console">🖥 Konsole</button>
          <button role="tab" aria-selected="${tab === 'files'}" class="${tab === 'files' ? 'on' : ''}" data-cloud-tab="files">📂 Dateien</button>
        </div>
        <button class="btn btn-sm" id="cloudDetailClose">Schließen</button>
      </div>
    </div>
    ${tab === 'files' ? cloudFilesPanel(s) : cloudConsolePanel(s)}
  </section>`;
}

function cloudConsolePanel(s) {
  const log = state.cloud.log;
  return `
  <div class="console cl-console" id="cloudLog" role="log" aria-label="Konsole von ${esc(s.name)}">${
    log.lines.length ? log.lines.map((l) => `<div class="${classify(l)}">${esc(l)}</div>`).join('')
      : (s.running ? 'Wird geladen …' : 'Der Server läuft gerade nicht – die Konsole füllt sich erst nach dem Start.')}</div>
  <div class="cmd-row">
    <input type="text" id="cloudCmd" value="${esc(state.cloud.cmd || '')}" aria-label="Befehl an ${esc(s.name)}"
      placeholder="${s.running ? 'Befehl an den Server, z. B. list' : 'Der Server läuft nicht'}" ${s.running ? '' : 'disabled'}>
    <button class="btn" id="cloudCmdSend" ${s.running ? '' : 'disabled'}>Senden</button>
  </div>
  <div class="muted small" style="margin-top:8px">Befehle gehen unverändert an den Server auf dem Root-Server –
    dieselben wie in der Konsole eines Servers auf diesem PC.</div>`;
}

function cloudFilesPanel(s) {
  const f = state.cloud.files;
  if (f.open !== null) {
    return `
    <div class="cl-editor-head">
      <div class="e-title">${esc(f.open)}</div>
      <div class="btn-row">
        <button class="btn btn-sm" id="cloudFileBack">← Zurück zur Liste</button>
        <button class="btn btn-sm btn-primary" id="cloudFileSave">Speichern</button>
      </div>
    </div>
    ${f.error ? `<div class="note note-err">${esc(f.error)}</div>` : ''}
    <textarea id="cloudFileText" class="cloud-editor" spellcheck="false"
      aria-label="Inhalt von ${esc(f.open)}">${esc(f.text)}</textarea>
    <div class="muted small" style="margin-top:8px">Gespeichert wird direkt auf dem Root-Server.
      Läuft der Server, liest er die Datei meist erst beim nächsten Start neu ein.</div>`;
  }
  const teile = (f.path || '').split('/').filter(Boolean);
  const krumen = ['<a href="#" data-cloud-dir="" class="crumb">Serverordner</a>']
    .concat(teile.map((t, i) => `<span class="sep" aria-hidden="true">/</span>
      <a href="#" data-cloud-dir="${esc(teile.slice(0, i + 1).join('/'))}" class="crumb">${esc(t)}</a>`)).join('');
  const rows = f.entries === null
    ? '<div class="muted small" style="padding:14px 2px">Wird geladen …</div>'
    : (f.entries.length ? f.entries.map((e) => `
        <div class="cl-frow">
          <span class="f-ico" aria-hidden="true">${e.is_dir ? '📁' : (KIND_ICON[e.kind] || '📄')}</span>
          <span class="f-name">${esc(e.name)}</span>
          <span class="f-meta">${e.is_dir ? 'Ordner' : esc(fmtBytes(e.size))}${e.mtime ? ' · ' + esc(fmtDate(e.mtime)) : ''}</span>
          <span class="btn-row">
            ${e.is_dir ? `<button class="btn btn-sm" data-cloud-dir="${esc(e.path)}">Öffnen</button>`
              : (e.editable ? `<button class="btn btn-sm" data-cloud-file="${esc(e.path)}">Bearbeiten</button>` : '')}
            ${e.is_dir ? '' : `<button class="btn btn-sm" data-cloud-get="${esc(e.path)}" aria-label="${esc(e.name)} herunterladen">⬇</button>`}
            <button class="btn btn-sm" data-cloud-ren="${esc(e.path)}" aria-label="${esc(e.name)} umbenennen">✎</button>
            <button class="btn btn-sm btn-danger" data-cloud-del="${esc(e.path)}" aria-label="${esc(e.name)} löschen">🗑</button>
          </span>
        </div>`).join('')
      : `<div class="empty-inline"><div class="ei-ico" aria-hidden="true">📂</div>
           <div><b>Dieser Ordner ist leer.</b><p class="mb0">Lege einen Ordner an oder lade eine Datei
             hierher – zum Beispiel ein Plugin nach <code>plugins</code>.</p></div></div>`);
  return `
  <div class="cl-files-bar">
    <div class="crumbs">${krumen}</div>
    <div class="btn-row">
      <button class="btn btn-sm" id="cloudMkdir">＋ Ordner</button>
      <button class="btn btn-sm" id="cloudPick">📎 Datei wählen</button>
      <input type="file" id="cloudFileAdd" class="visually-hidden" tabindex="-1" aria-hidden="true">
      <span class="muted small" id="cloudFileName">keine gewählt</span>
      <button class="btn btn-sm btn-primary" id="cloudFileAddGo">Hochladen</button>
    </div>
  </div>
  ${f.error ? `<div class="note note-err">${esc(f.error)}</div>` : ''}
  <div class="cl-files">${rows}</div>
  <div class="muted small" style="margin-top:10px">Der Server muss zum Ändern einzelner Dateien gestoppt sein –
    sonst schreibt er sie gleich wieder um.</div>`;
}

/* ---------- Server auf diesem PC */

function cloudLocalBox(d) {
  const rows = state.servers.map((s) => {
    const link = s.cloud || {};
    const hosted = !!s.cloud_locked;
    const busy = state.cloud.busy === s.id || s.installing;
    const wartet = link.state === 'awaiting_pull' || link.state === 'downloading';
    return `<div class="loc-row ${hosted ? 'is-locked' : ''}">
      <div class="loc-main">
        <div class="w-name">${typeTag(s)} ${esc(s.name)}
          ${hosted ? `<span class="pill ${wartet ? 'pill-amber' : 'pill-blue'}">${esc(cloudStateText(link.state))}</span>`
            : '<span class="pill pill-grey">auf diesem PC</span>'}</div>
        <div class="w-meta">${esc(s.version || '')} · ${esc(gb(s.ram_mb))}${link.address ? ' · ' + esc(link.address) : ''}</div>
        ${hosted ? `<div class="loc-lock"><span aria-hidden="true">🔒</span>
          ${wartet ? 'Warte auf die Rückholung – erst danach lässt sich die Kopie hier wieder starten.'
            : 'Die Kopie auf diesem PC ist gesperrt: gespielt wird gerade auf dem Root-Server.'}</div>` : ''}
      </div>
      <div class="btn-row">
        ${hosted
          ? `<button class="btn btn-sm ${wartet ? 'btn-primary' : ''}" data-cloud-pull-local="${esc(link.instance || '')}"
               ${link.instance && !busy ? '' : 'disabled'}>⬇ Zurück auf diesen PC holen</button>`
          : `<button class="btn btn-sm" data-cloud-push="${esc(s.id)}" ${s.installed && !s.running && !busy ? '' : 'disabled'}
               ${s.running ? 'title="Bitte den Server zuerst stoppen."' : (!s.installed ? 'title="Der Server ist noch nicht fertig eingerichtet."' : '')}>⬆ Auf den Root-Server verschieben</button>`}
      </div>
    </div>`;
  }).join('');
  return `<section class="card">
    <div class="card-head"><h3>🖥 Server auf diesem PC</h3>
      <span class="muted small">${state.servers.length || 'keine'}</span></div>
    ${rows || `<div class="empty-inline"><div class="ei-ico" aria-hidden="true">🖥</div>
      <div><b>Noch kein Server auf diesem PC.</b>
        <p class="mb0">Lege links oben mit <b>„Neuer Server“</b> einen an – danach kannst du ihn hier
          auf den Root-Server verschieben.</p></div></div>`}
    <div class="note note-info mb0"><b>Ein Server ist immer nur an einer Stelle spielbar.</b>
      Nach dem Verschieben ist die Kopie auf diesem PC gesperrt, bis du den Server zurückholst.
      Beim Zurückholen wird der Ordner auf dem Root-Server erst gelöscht, wenn hier jede Datei mit
      Prüfsumme angekommen ist. Der Ordner <code>backups</code> bleibt immer auf diesem PC.</div>
  </section>`;
}

/* ---------- Zustand des Root-Servers */

function cloudMachineBox(d) {
  const m = d.machine || {};
  const frei = Number(m.disk_free_bytes || 0);
  const knapp = frei > 0 && frei < 5 * 1024 ** 3;
  const sehrKnapp = frei > 0 && frei < 3 * 1024 ** 3;
  const ramLauf = Number(m.ram_running_mb || 0), ramMasch = Number(m.ram_machine_mb || 0);
  return `<section class="card cl-machine">
    <div class="card-head"><h3>🖧 Zustand des Root-Servers</h3></div>
    ${frei ? `<div class="ministat-row">
      ${statBox('Platte frei', esc(m.disk_free_text || fmtBytes(frei)), sehrKnapp ? 'bad' : knapp ? 'warn' : 'ok')}
      ${ramMasch ? statBox('Speicher in Betrieb', esc(fmtMb(ramLauf)) + ' <span class="mini">von ' + esc(fmtMb(ramMasch)) + '</span>') : ''}
    </div>` : '<div class="muted small">Der Root-Server meldet gerade keine Zahlen.</div>'}
    ${ramMasch ? meterBox('Arbeitsspeicher der Maschine', ramLauf, ramMasch,
      `${esc(fmtMb(ramLauf))} belegt`, ramLauf / ramMasch > 0.85 ? 'warn' : '') : ''}
    ${m.free_ports_java !== undefined ? `<div class="w-meta" style="margin-top:8px">
      Freie Adressen: ${Number(m.free_ports_java || 0)} für Java, ${Number(m.free_ports_bedrock || 0)} für Bedrock.</div>` : ''}
    ${sehrKnapp ? `<div class="note note-err mb0"><b>Die Platte wird eng.</b>
      <p class="mb0">Unter 3 GB freiem Platz nimmt der Root-Server keinen Serverstart mehr an. Hole einen
        Server auf diesen PC zurück oder sag dem Betreiber Bescheid.</p></div>`
      : knapp ? `<div class="note note-warn mb0"><b>Auf der Platte wird es knapp.</b>
      <p class="mb0">Eine gut erkundete Welt wiegt schnell mehrere Gigabyte. Ab 3 GB freiem Platz lehnt
        der Root-Server neue Starts ab.</p></div>` : ''}
  </section>`;
}

function cloudTipBox(d) {
  const pull = cloudPullList();
  if (pull.length) {
    return `<section class="card"><div class="card-head"><h3>⬇ Als Nächstes</h3></div>
      <p class="small">${pull.length === 1 ? 'Ein Server wartet' : pull.length + ' Server warten'} auf die Rückholung.
        Solange er dort liegt, lässt sich die Kopie auf diesem PC nicht starten.</p>
      <div class="btn-row">${pull.map((s) => `<button class="btn btn-sm btn-primary" data-cloud-pull="${esc(s.id)}">⬇ ${esc(s.name)} holen</button>`).join('')}</div>
    </section>`;
  }
  return `<section class="card"><div class="card-head"><h3>💡 Gut zu wissen</h3></div>
    <ul class="tips">
      <li>Die Adresse bleibt einem Server für immer – auch wenn du ihn umbenennst. Deine Freunde müssen
        nichts Neues eintippen.</li>
      <li>Anlegen und Hochladen ist unbegrenzt. Begrenzt ist nur, wie viele Server <b>gleichzeitig
        eingeschaltet</b> sein dürfen.</li>
      <li>Beim Zurückholen wird jede Datei einzeln mit Prüfsumme verglichen. Erst danach verschwindet
        der Ordner auf dem Root-Server.</li>
      <li>Bricht eine Übertragung ab, geht es beim nächsten Anlauf an derselben Stelle weiter.</li>
    </ul>
  </section>`;
}

/* ---------- Anmeldung: Discord, danach einmal der Einladungscode */

async function cloudStartLogin() {
  const btn = $('#cloudLoginDiscord');
  if (btn) btn.disabled = true;
  state.cloud.login.error = '';
  try {
    const r = await api('cloud/login', { method: 'POST' });
    state.cloud.login.pending = true;
    state.cloud.login.url = r.url || '';
    if (r.url) window.open(r.url, '_blank', 'noopener');
    paintCloud();
    clearInterval(state.timers.cloudLogin);
    state.timers.cloudLogin = setInterval(cloudPollLogin, 2000);
  } catch (e) {
    state.cloud.login.error = e.message;
    state.cloud.login.pending = false;
    paintCloud();
  }
}

async function cloudPollLogin() {
  try {
    const p = await api('cloud/login/poll');
    if (p.pending) return;
    clearInterval(state.timers.cloudLogin);
    state.cloud.login.pending = false;
    if (p.needs_invite) { await cloudAskInvite(p.discord); return; }
    toast('Am Root-Server angemeldet.');
    await loadCloud(true);
  } catch (e) {
    clearInterval(state.timers.cloudLogin);
    state.cloud.login.pending = false;
    state.cloud.login.error = e.message;
    paintCloud();
  }
}

/** Erstanmeldung: Discord hat geklappt, der Einladungscode fehlt noch. */
async function cloudAskInvite(person) {
  if (state.cloud.askingInvite) return;
  state.cloud.askingInvite = true;
  const name = (person || {}).name || '';
  let fertig = false;
  try {
    const r = await dialog({
      icon: '🎟', tone: '', title: 'Noch ein Schritt: dein Einladungscode',
      sub: name ? `Discord-Konto: ${name}` : '',
      confirmText: 'Konto anlegen', cancelText: 'Abbrechen',
      html: `<p>Die Anmeldung bei Discord hat geklappt. Der Root-Server kennt dieses Konto aber noch nicht –
        deshalb fragt er einmalig nach deinem Einladungscode.</p>
        <p class="muted small">Den Code bekommst du vom Betreiber (im Discord oder persönlich). Er sieht aus wie
        <code>ABCD-EFGH-IJKL</code>, gilt nur ein einziges Mal und läuft nach kurzer Zeit ab.</p>`,
      fields: [{ id: 'cloudCode', label: 'Einladungscode', placeholder: 'ABCD-EFGH-IJKL', mono: true,
                 maxlength: 14, aria: 'Einladungscode in der Form ABCD-EFGH-IJKL',
                 hint: 'Groß- und Kleinschreibung ist egal – die Striche setzt das Programm selbst.' }],
      validate: (v) => {
        const roh = String(v.cloudCode || '').replace(/[^A-Za-z0-9]/g, '');
        if (!roh) return 'Bitte trage den Einladungscode ein.';
        if (roh.length < 12) return 'Der Code ist noch nicht vollständig – er besteht aus zwölf Zeichen, z. B. ABCD-EFGH-IJKL.';
        return '';
      },
      bind: (ctx) => {
        const el = $('#cloudCode', ctx.body);
        if (!el) return;
        el.oninput = () => {
          const amEnde = el.selectionStart === el.value.length;
          el.value = fmtCode(el.value);
          if (amEnde) el.setSelectionRange(el.value.length, el.value.length);
        };
      },
      onSubmit: async (v) => { await api('cloud/login/register', { method: 'POST', body: { code: v.cloudCode } }); },
    });
    fertig = !!r;
  } finally { state.cloud.askingInvite = false; }
  if (fertig) {
    toast('Konto angelegt – du bist am Root-Server angemeldet.');
  } else {
    try { await api('cloud/login/abort', { method: 'POST' }); } catch { /* war schon weg */ }
    toast('Anmeldung abgebrochen. Du kannst es jederzeit noch einmal versuchen.');
  }
  await loadCloud(true);
}

/* ---------- Konto-Einstellungen */

async function openAccountDialog() {
  const d = cloudData();
  const st = { user: d.user || {}, sessions: null, sessHint: '', nameHint: '', linkHint: '' };
  const laden = async (ctx) => {
    try {
      const r = await api('cloud/sessions');
      st.sessions = r.supported === false ? [] : (r.sessions || []);
      st.sessHint = r.supported === false ? (r.hint || '') : '';
    } catch (e) { st.sessions = []; st.sessHint = e.message; }
    ctx.redraw();
  };
  await dialog({
    wide: true, icon: '', title: 'Konto-Einstellungen', confirmText: null, cancelText: 'Schließen',
    body: () => accountBody(st),
    bind: (ctx) => accountBind(st, ctx, laden),
  });
  await loadCloud(true);
}

function accountBody(st) {
  const u = st.user || {};
  const d = cloudData();
  return `
  <div class="acct-top">
    ${cloudAvatar(u, 'av-64')}
    <div>
      <div class="acct-big">${esc(u.name || 'Konto')}</div>
      <div class="muted small">${esc(u.role_text || ROLE_TEXT[u.role] || 'Benutzer')}
        ${d.logged_in_at ? '· auf diesem PC ' + esc(fmtSince(d.logged_in_at)) + ' angemeldet' : ''}</div>
      <div class="muted small">${u.created_at ? 'Konto angelegt am ' + esc(fmtDate(u.created_at)) : ''}
        ${d.session_expires_at ? '· Sitzung gültig bis ' + esc(fmtDate(d.session_expires_at)) : ''}</div>
    </div>
  </div>

  <h4 class="acct-h">Anzeigename</h4>
  <div class="acct-line">
    <input type="text" id="acctName" value="${esc(u.name || '')}" maxlength="32" aria-label="Anzeigename"
      placeholder="z. B. Luca">
    <button class="btn btn-sm btn-primary" id="acctNameSave">Speichern</button>
  </div>
  <div class="hint">2 bis 32 Zeichen. So stehst du in der Verwaltung des Betreibers.</div>
  ${st.nameHint ? `<div class="note note-info">${esc(st.nameHint)}</div>` : ''}

  <h4 class="acct-h">Discord</h4>
  ${u.discord_linked
    ? `<div class="acct-line acct-static">
         <span class="pill pill-green">verknüpft</span>
         <span>${esc(u.discord_name || u.name || '')}</span>
       </div>
       <div class="hint">Die Anmeldung läuft über dieses Discord-Konto. Dein Passwort sieht der Manager nie.</div>`
    : `<p class="small">Dieses Konto ist noch nicht mit Discord verknüpft. Danach kannst du dich überall
         mit einem Klick anmelden, ohne Einladungscode.</p>
       <div class="btn-row"><button class="btn btn-sm" id="acctLink">🔗 Discord verknüpfen</button></div>`}
  ${st.linkHint ? `<div class="note note-info">${esc(st.linkHint)}</div>` : ''}

  <h4 class="acct-h">Aktive Sitzungen</h4>
  ${st.sessions === null ? '<div class="muted small">Wird geladen …</div>'
    : st.sessHint ? `<div class="note note-info mb0">${esc(st.sessHint)}</div>`
      : (st.sessions.length ? `<div class="acct-sessions">${st.sessions.map((s) => `
          <div class="acct-sess">
            <div>
              <div class="w-name">${esc(s.note || 'Unbenannte Sitzung')}</div>
              <div class="w-meta">${s.created_at ? 'begonnen am ' + esc(fmtDate(s.created_at)) : ''}
                ${s.last_seen ? '· zuletzt ' + esc(fmtDate(s.last_seen)) : ''}
                ${s.expires_at ? '· gültig bis ' + esc(fmtDate(s.expires_at)) : ''}</div>
            </div>
            <button class="btn btn-sm btn-danger" data-sess="${Number(s.created_at) || 0}">Beenden</button>
          </div>`).join('')}</div>`
        : '<div class="muted small">Es ist keine weitere Sitzung offen.</div>')}

  <div class="acct-foot">
    <button class="btn btn-sm btn-red" id="acctLogout">Von diesem PC abmelden</button>
    <span class="muted small">Gehostete Server laufen auf dem Root-Server weiter.</span>
  </div>`;
}

function accountBind(st, ctx, laden) {
  if (st.sessions === null) laden(ctx);

  const save = $('#acctNameSave', ctx.body);
  if (save) save.onclick = async () => {
    const wert = ($('#acctName', ctx.body).value || '').trim();
    if (wert.length < 2 || wert.length > 32) { ctx.setError('Bitte einen Namen mit 2 bis 32 Zeichen eintragen.'); return; }
    ctx.setError(''); save.disabled = true;
    try {
      const r = await api('cloud/account/name', { method: 'POST', body: { name: wert } });
      if (r.supported === false) { st.nameHint = r.hint || 'Das kann dieser Root-Server noch nicht.'; }
      else { st.user = { ...st.user, name: r.name || wert }; st.nameHint = ''; toast('Anzeigename gespeichert.'); }
    } catch (e) { ctx.setError(e.message); }
    save.disabled = false;
    ctx.redraw();
  };

  const link = $('#acctLink', ctx.body);
  if (link) link.onclick = async () => {
    link.disabled = true; ctx.setError('');
    try {
      const r = await api('cloud/link/discord', { method: 'POST', body: {} });
      if (r.supported === false) { st.linkHint = r.hint || 'Das kann dieser Root-Server noch nicht.'; }
      else {
        window.open(r.url, '_blank', 'noopener');
        st.linkHint = 'Der Browser ist offen. Bestätige dort den Zugriff und klicke danach hier auf '
          + '„Schließen“ und oben auf „Aktualisieren“ – dann steht die Verknüpfung.';
      }
    } catch (e) { ctx.setError(e.message); }
    link.disabled = false;
    ctx.redraw();
  };

  $$('[data-sess]', ctx.body).forEach((el) => el.onclick = async () => {
    const ok = await askConfirm({
      tone: 'danger', title: 'Diese Sitzung beenden?', confirmText: 'Sitzung beenden',
      text: 'Das Gerät hinter dieser Sitzung wird abgemeldet und muss sich neu anmelden. '
        + 'Ist es dieser PC, meldet sich der Manager gleich mit ab.',
    });
    if (!ok) return;
    el.disabled = true; ctx.setError('');
    try {
      const r = await api('cloud/session/revoke', { method: 'POST', body: { created_at: Number(el.dataset.sess) } });
      if (r.supported === false) { st.sessHint = r.hint || ''; ctx.redraw(); return; }
      toast('Sitzung beendet.');
      st.sessions = null;
      ctx.redraw();
    } catch (e) { ctx.setError(e.message); el.disabled = false; }
  });

  const out = $('#acctLogout', ctx.body);
  if (out) out.onclick = async () => {
    const ok = await askConfirm({
      tone: 'danger', title: 'Vom Root-Server abmelden?', confirmText: 'Abmelden',
      text: 'Dieser PC vergisst die Anmeldung. Gehostete Server laufen auf dem Root-Server weiter – '
        + 'du kommst nur erst nach einer neuen Anmeldung wieder an sie heran.',
    });
    if (!ok) return;
    try { await api('cloud/logout', { method: 'POST' }); state.cloud.open = null; } catch (e) { toast(e.message, true); }
    ctx.close(null);
    await loadCloud(true);
  };
}

/* ---------- Fortschritt einer Übertragung */

function startCloudJob(res, label) {
  state.cloud.job = { id: res.job_id, serverId: res.server_id || '', label, job: null };
  state.cloud.rate = null;
  if (state.view !== 'cloud') openCloud(); else paintCloud();
  clearInterval(state.timers.cloudJob);
  state.timers.cloudJob = setInterval(async () => {
    const t = state.cloud.job;
    if (!t) { clearInterval(state.timers.cloudJob); return; }
    try {
      const job = await api('job/' + t.id);
      t.job = job;
      paintCloud();
      if (job.status !== 'running') {
        clearInterval(state.timers.cloudJob);
        state.cloud.busy = '';
        toast(job.status === 'done' ? label + ': fertig.' : label + ' fehlgeschlagen: ' + job.error, job.status !== 'done');
        await refresh().catch(() => {});
        await loadCloud(true);
      }
    } catch (e) { clearInterval(state.timers.cloudJob); toast(e.message, true); }
  }, 900);
}

/* ---------- Aktionen */

async function cloudUpload(serverId) {
  const s = state.servers.find((x) => x.id === serverId);
  if (!s) return;
  const ok = await askConfirm({
    tone: 'danger', icon: '⬆', title: `„${s.name}“ auf den Root-Server verschieben?`,
    confirmText: 'Jetzt verschieben',
    text: 'Alle Dateien dieses Servers werden mit Prüfsumme übertragen. Danach ist die Kopie auf diesem PC '
      + 'gesperrt – ein Server ist immer nur an einer Stelle spielbar. Zurückholen kannst du ihn jederzeit.'
      + '\n\nDer Ordner „backups“ bleibt auf diesem PC.',
  });
  if (!ok) return;
  state.cloud.busy = serverId;
  try {
    const d = await api('cloud/upload', { method: 'POST', body: { server: serverId } });
    startCloudJob(d, `„${s.name}“ wird auf den Root-Server verschoben`);
  } catch (e) { state.cloud.busy = ''; toast(e.message, true); await loadCloud(true); }
}

async function cloudDownload(instance) {
  const d = cloudData();
  const s = (d.servers || []).find((x) => x.id === instance);
  const name = s ? s.name : 'Server';
  const ok = await askConfirm({
    tone: 'danger', icon: '⬇', title: `„${name}“ auf diesen PC zurückholen?`,
    confirmText: 'Jetzt zurückholen',
    text: 'Der Server wird auf dem Root-Server gestoppt, alle Dateien werden übertragen und einzeln geprüft. '
      + 'Erst danach wird der Ordner auf dem Root-Server gelöscht und die Kopie auf diesem PC wieder freigegeben.'
      + '\n\nSpieler, die gerade online sind, fliegen dabei vom Server.',
  });
  if (!ok) return;
  state.cloud.busy = instance;
  try {
    const res = await api('cloud/download', { method: 'POST', body: { instance } });
    startCloudJob(res, `„${name}“ wird auf diesen PC geholt`);
  } catch (e) { state.cloud.busy = ''; toast(e.message, true); await loadCloud(true); }
}

async function cloudSimple(path, body, label) {
  try {
    const d = await api(path, { method: 'POST', body });
    if (d.message) toast(d.message); else if (label) toast(label);
    await loadCloud(true);
  } catch (e) { toast(e.message, true); await loadCloud(true); }
  finally { state.cloud.busy = ''; }
}

async function cloudLoadLog() {
  const open = state.cloud.open;
  if (!open || open.tab !== 'console') return;
  try {
    const d = await api(`cloud/console?instance=${encodeURIComponent(open.id)}&since=${state.cloud.log.next}`);
    if (d.lines && d.lines.length) {
      state.cloud.log.lines = state.cloud.log.lines.concat(d.lines).slice(-400);
      state.cloud.log.next = d.next;
      const box = $('#cloudLog');
      if (box) {
        const kleben = (box.scrollHeight - box.scrollTop - box.clientHeight) < 48;
        box.innerHTML = state.cloud.log.lines.map((l) => `<div class="${classify(l)}">${esc(l)}</div>`).join('');
        if (kleben) box.scrollTop = box.scrollHeight;
      }
    } else if (d.next !== undefined) { state.cloud.log.next = d.next; }
  } catch { /* nächster Versuch */ }
}

async function cloudLoadFiles(path) {
  const open = state.cloud.open;
  if (!open) return;
  const f = state.cloud.files;
  f.path = path || ''; f.open = null; f.error = ''; f.entries = null; f.dirty = false;
  paintCloud();
  try {
    const d = await api(`cloud/files?instance=${encodeURIComponent(open.id)}&path=${encodeURIComponent(f.path)}`);
    f.entries = d.entries || [];
    f.path = d.path || f.path;
  } catch (e) { f.error = e.message; f.entries = []; }
  paintCloud();
}

async function cloudOpenFile(path) {
  const open = state.cloud.open;
  const f = state.cloud.files;
  try {
    const d = await api(`cloud/file?instance=${encodeURIComponent(open.id)}&path=${encodeURIComponent(path)}`);
    f.open = d.path || path; f.text = d.text || ''; f.error = ''; f.dirty = false;
  } catch (e) { f.error = e.message; }
  paintCloud();
}

async function copyText(text, was = 'Adresse') {
  try {
    await navigator.clipboard.writeText(text);
    toast(was + ' kopiert.');
  } catch {
    // Ohne Zwischenablage-Recht (älterer Browser): über ein unsichtbares Feld kopieren.
    const el = document.createElement('textarea');
    el.value = text; el.setAttribute('readonly', '');
    el.style.position = 'fixed'; el.style.opacity = '0';
    document.body.appendChild(el); el.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    el.remove();
    if (ok) { toast(was + ' kopiert.'); return; }
    // Auch das ging nicht: dann wenigstens gut lesbar zum Abtippen zeigen.
    askNote({ icon: '⧉', title: 'Kopieren hat nicht geklappt',
      text: 'Dieses Fenster darf nicht in die Zwischenablage schreiben. '
        + 'Hier steht die ' + was.toLowerCase() + ' zum Markieren:',
      html: `<p class="mb0"><code style="user-select:all">${esc(text)}</code></p>` });
  }
}

/* ---------- Bindungen der Cloud-Seite */

function bindCloud() {
  const reload = $('#cloudReload');
  if (reload) reload.onclick = () => { toast('Wird aktualisiert …'); loadCloud(true); };
  const acct = $('#cloudAcct');
  if (acct) acct.onclick = openAccountDialog;

  const discord = $('#cloudLoginDiscord');
  if (discord) discord.onclick = cloudStartLogin;
  const abbruch = $('#cloudLoginCancel');
  if (abbruch) abbruch.onclick = async () => {
    clearInterval(state.timers.cloudLogin);
    state.cloud.login.pending = false;
    try { await api('cloud/login/abort', { method: 'POST' }); } catch { /* war schon weg */ }
    paintCloud();
  };

  $$('[data-copy]').forEach((el) => el.onclick = () => copyText(el.dataset.copy, 'Adresse'));

  $$('[data-cloud-start]').forEach((el) => el.onclick = () => {
    state.cloud.busy = el.dataset.cloudStart; el.disabled = true;
    cloudSimple('cloud/start', { instance: el.dataset.cloudStart }, 'Der Server wird gestartet.');
  });
  $$('[data-cloud-stop]').forEach((el) => el.onclick = async () => {
    const id = el.dataset.cloudStop;
    const s = (cloudData().servers || []).find((x) => x.id === id) || {};
    const ok = await askConfirm({
      tone: 'danger', icon: '■', title: `„${s.name || 'Server'}“ stoppen?`, confirmText: 'Stoppen',
      text: 'Die Spieler bekommen zehn Sekunden Vorwarnung, danach wird der Server sauber beendet '
        + 'und die Welt gespeichert. Wer gerade spielt, fliegt dabei heraus.',
    });
    if (!ok) return;
    state.cloud.busy = id; el.disabled = true;
    cloudSimple('cloud/stop', { instance: id, announce_seconds: 10 });
  });
  $$('[data-cloud-pull]').forEach((el) => el.onclick = () => cloudDownload(el.dataset.cloudPull));
  $$('[data-cloud-pull-local]').forEach((el) => el.onclick = () => cloudDownload(el.dataset.cloudPullLocal));
  $$('[data-cloud-push]').forEach((el) => el.onclick = () => cloudUpload(el.dataset.cloudPush));

  $$('[data-cloud-open]').forEach((el) => el.onclick = () => cloudOpenConsole(el.dataset.cloudOpen));
  $$('[data-cloud-files]').forEach((el) => el.onclick = () => {
    state.cloud.open = { id: el.dataset.cloudFiles, tab: 'files' };
    clearInterval(state.timers.cloudLog);
    cloudLoadFiles('');
  });
  $$('[data-cloud-tab]').forEach((el) => el.onclick = () => {
    const open = state.cloud.open;
    if (!open) return;
    if (el.dataset.cloudTab === 'files') {
      state.cloud.open = { id: open.id, tab: 'files' };
      clearInterval(state.timers.cloudLog);
      cloudLoadFiles('');
    } else cloudOpenConsole(open.id);
  });

  const close = $('#cloudDetailClose');
  if (close) close.onclick = () => {
    state.cloud.open = null;
    clearInterval(state.timers.cloudLog);
    paintCloud();
  };

  const send = $('#cloudCmdSend');
  if (send) send.onclick = async () => {
    const input = $('#cloudCmd');
    const cmd = (input.value || '').trim();
    if (!cmd) return;
    input.value = ''; state.cloud.cmd = '';
    try { await api('cloud/command', { method: 'POST', body: { instance: state.cloud.open.id, command: cmd } }); }
    catch (e) { toast(e.message, true); }
    cloudLoadLog();
  };
  const cmd = $('#cloudCmd');
  if (cmd) {
    cmd.oninput = () => { state.cloud.cmd = cmd.value; };
    cmd.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); send.click(); } };
  }

  $$('[data-cloud-dir]').forEach((el) => el.onclick = (e) => { e.preventDefault(); cloudLoadFiles(el.dataset.cloudDir); });
  $$('[data-cloud-file]').forEach((el) => el.onclick = () => cloudOpenFile(el.dataset.cloudFile));
  $$('[data-cloud-del]').forEach((el) => el.onclick = async () => {
    const pfad = el.dataset.cloudDel;
    const ok = await askConfirm({
      tone: 'danger', icon: '🗑', title: 'Auf dem Root-Server löschen?', confirmText: 'Endgültig löschen',
      text: `„${pfad}“ wird auf dem Root-Server gelöscht. Bei einem Ordner verschwindet alles darin mit. `
        + 'Das lässt sich nicht zurücknehmen – es gibt dort keinen Papierkorb.',
    });
    if (!ok) return;
    el.disabled = true;
    try {
      await api('cloud/file/delete', { method: 'POST', body: { instance: state.cloud.open.id, path: pfad } });
      toast('Gelöscht.');
      await cloudLoadFiles(state.cloud.files.path);
    } catch (e) { toast(e.message, true); el.disabled = false; }
  });
  $$('[data-cloud-ren]').forEach((el) => el.onclick = async () => {
    const pfad = el.dataset.cloudRen;
    const alt = pfad.split('/').pop();
    const neu = await askInput({
      icon: '✎', title: 'Umbenennen', label: 'Neuer Name', value: alt,
      text: `„${alt}“ bekommt auf dem Root-Server einen neuen Namen. Der Ordner bleibt derselbe.`,
      confirmText: 'Umbenennen', maxlength: 120,
      validate: (v) => {
        const n = String(v.mdlInput || '').trim();
        if (!n) return 'Bitte einen Namen eintragen.';
        if (/[\\/:*?"<>|]/.test(n)) return 'Diese Zeichen gehen nicht: \\ / : * ? " < > |';
        return '';
      },
    });
    if (!neu || neu === alt) return;
    const teile = pfad.split('/'); teile.pop();
    try {
      await api('cloud/file/rename', { method: 'POST',
        body: { instance: state.cloud.open.id, path: pfad, new_path: teile.concat(neu).join('/') } });
      toast('Umbenannt.');
      await cloudLoadFiles(state.cloud.files.path);
    } catch (e) { toast(e.message, true); }
  });
  const mk = $('#cloudMkdir');
  if (mk) mk.onclick = async () => {
    const name = await askInput({
      icon: '📁', title: 'Neuen Ordner anlegen', label: 'Name des Ordners',
      placeholder: 'z. B. plugins', confirmText: 'Anlegen', maxlength: 120,
      text: 'Der Ordner entsteht an der Stelle, die gerade offen ist.',
      validate: (v) => (String(v.mdlInput || '').trim() ? '' : 'Bitte einen Namen eintragen.'),
    });
    if (!name) return;
    const p = state.cloud.files.path ? state.cloud.files.path + '/' + name : name;
    try {
      await api('cloud/file/mkdir', { method: 'POST', body: { instance: state.cloud.open.id, path: p } });
      toast('Ordner angelegt.');
      await cloudLoadFiles(state.cloud.files.path);
    } catch (e) { toast(e.message, true); }
  };
  $$('[data-cloud-get]').forEach((el) => el.onclick = async () => {
    el.disabled = true;
    try {
      const r = await api('cloud/file/download', { method: 'POST',
        body: { instance: state.cloud.open.id, path: el.dataset.cloudGet } });
      toast(`Liegt jetzt hier: ${r.local}`);
    } catch (e) { toast(e.message, true); }
    el.disabled = false;
  });
  const wahl = $('#cloudFileAdd');
  const pick = $('#cloudPick');
  if (pick && wahl) pick.onclick = () => wahl.click();
  if (wahl) wahl.onchange = () => {
    const n = $('#cloudFileName');
    if (n) n.textContent = wahl.files && wahl.files[0] ? wahl.files[0].name : 'keine gewählt';
  };
  const add = $('#cloudFileAddGo');
  if (add) add.onclick = async () => {
    const datei = wahl && wahl.files && wahl.files[0];
    if (!datei) { toast('Bitte zuerst eine Datei auswählen.', true); return; }
    add.disabled = true;
    try {
      // Der Browser gibt keinen Dateipfad heraus – also den Inhalt als Base64 an das eigene
      // Programm, das ihn stückweise an den Root-Server weitergibt.
      const puffer = new Uint8Array(await datei.arrayBuffer());
      let roh = '';
      for (let i = 0; i < puffer.length; i += 0x8000) {
        roh += String.fromCharCode.apply(null, puffer.subarray(i, i + 0x8000));
      }
      const ordner = state.cloud.files.path ? state.cloud.files.path + '/' : '';
      await api('cloud/file/upload', { method: 'POST',
        body: { instance: state.cloud.open.id, name: datei.name, path: ordner + datei.name, inhalt: btoa(roh) } });
      toast('Hochgeladen.');
      await cloudLoadFiles(state.cloud.files.path);
    } catch (e) { toast(e.message, true); }
    add.disabled = false;
  };
  const back = $('#cloudFileBack');
  if (back) back.onclick = async () => {
    if (state.cloud.files.dirty && !(await askConfirm({
      tone: 'danger', title: 'Ungespeicherte Änderungen verwerfen?', confirmText: 'Verwerfen',
      text: 'In dieser Datei stehen Änderungen, die noch nicht auf dem Root-Server gespeichert sind.',
    }))) return;
    cloudLoadFiles(state.cloud.files.path);
  };
  const text = $('#cloudFileText');
  if (text) text.oninput = () => { state.cloud.files.text = text.value; state.cloud.files.dirty = true; };
  const save = $('#cloudFileSave');
  if (save) save.onclick = async () => {
    save.disabled = true;
    try {
      await api('cloud/file', { method: 'POST', body: { instance: state.cloud.open.id,
        path: state.cloud.files.open, content: $('#cloudFileText').value } });
      state.cloud.files.dirty = false;
      toast('Gespeichert.');
    } catch (e) { toast(e.message, true); }
    save.disabled = false;
  };

  const resume = $('#cloudResume');
  if (resume) resume.onclick = () => {
    const t = cloudData().transfer || {};
    if (t.kind === 'upload') cloudUpload(t.local); else cloudDownload(t.instance);
  };
  const abortT = $('#cloudAbortT');
  if (abortT) abortT.onclick = async () => {
    const t = cloudData().transfer || {};
    const ok = await askConfirm({
      tone: 'danger', title: 'Übertragung verwerfen?', confirmText: 'Verwerfen',
      text: 'Die angefangene Übertragung wird auf dem Root-Server abgebrochen. Der Server bleibt auf '
        + 'diesem PC spielbar. Beim nächsten Anlauf beginnt die Übertragung von vorn.',
    });
    if (!ok) return;
    try { await api('cloud/abort', { method: 'POST', body: { server: t.local } }); toast('Verworfen.'); }
    catch (e) { toast(e.message, true); }
    await loadCloud(true);
  };

  const jc = $('#cloudJobClose');
  if (jc) jc.onclick = () => { state.cloud.job = null; state.cloud.rate = null; paintCloud(); };
}

function cloudOpenConsole(id) {
  state.cloud.open = { id, tab: 'console' };
  state.cloud.log = { next: 0, lines: [], running: false };
  paintCloud();
  clearInterval(state.timers.cloudLog);
  cloudLoadLog();
  state.timers.cloudLog = setInterval(cloudLoadLog, 1500);
}

/* ---------- Karte in der Übersicht eines lokalen Servers */

function cloudServerCard(s) {
  const link = s.cloud || {};
  const hosted = !!s.cloud_locked;
  const d = cloudData();
  if (!hosted && !d.logged_in) {
    return `<div class="card">
      <div class="card-head"><h3>☁ Auf den Root-Server verschieben</h3><span class="pill pill-grey">Cloud</span></div>
      <p class="small">Dieser Server kann in ein Rechenzentrum umziehen und dort rund um die Uhr laufen –
         mit fester Adresse und ohne Portfreigabe zu Hause. Dazu brauchst du eine Anmeldung und einen Pass.</p>
      <div class="btn-row"><button class="btn btn-sm" data-cloud-goto="1">Bereich „Cloud“ öffnen</button></div></div>`;
  }
  if (!hosted) {
    return `<div class="card">
      <div class="card-head"><h3>☁ Auf den Root-Server verschieben</h3>
        <span class="pill pill-grey">liegt auf diesem PC</span></div>
      <p class="small">Der ganze Serverordner wird mit Prüfsumme je Datei übertragen und läuft danach auf dem
         Root-Server – auch wenn dieser PC aus ist. <b>Die Kopie auf diesem PC ist danach gesperrt</b>
         (ein Server ist immer nur an einer Stelle spielbar); zurückholen kannst du ihn jederzeit.</p>
      <div class="btn-row">
        <button class="btn btn-sm btn-primary" data-cloud-push="${esc(s.id)}" ${s.installed && !s.running && !s.installing ? '' : 'disabled'}
          ${s.running ? 'title="Bitte den Server zuerst stoppen."' : ''}>⬆ Auf den Root-Server verschieben</button>
        <button class="btn btn-sm" data-cloud-goto="1">Cloud öffnen</button></div></div>`;
  }
  return `<div class="card">
    <div class="card-head"><h3>☁ Dieser Server liegt auf dem Root-Server</h3>
      <span class="pill pill-amber">${esc(cloudStateText(link.state))}</span></div>
    <div class="note ${link.state === 'awaiting_pull' || link.state === 'downloading' ? 'note-warn' : 'note-info'}">
      ${link.state === 'awaiting_pull' || link.state === 'downloading'
        ? '<b>Der Server wartet darauf, zurückgeholt zu werden.</b> Auf dem Root-Server ist er gestoppt und die Welt gespeichert. Erst wenn hier jede Datei geprüft angekommen ist, wird der Ordner dort gelöscht.'
        : '<b>Die Kopie auf diesem PC ist gesperrt</b> und lässt sich nicht starten – gespielt wird auf dem Root-Server. Hole den Server zurück, wenn du hier weiterspielen willst.'}
    </div>
    ${link.address ? `<div class="addr addr-pub"><div><div class="a-k">Für Freunde</div>
      <div class="a-v">${esc(link.address)}</div></div>
      <button class="btn btn-sm" data-copy="${esc(link.address)}" aria-label="Adresse kopieren">⧉ Kopieren</button></div>` : ''}
    <div class="btn-row">
      <button class="btn btn-sm btn-primary" data-cloud-pull-local="${esc(link.instance || '')}" ${link.instance && !s.installing ? '' : 'disabled'}>⬇ Zurück auf diesen PC holen</button>
      <button class="btn btn-sm" data-cloud-goto="1">Cloud öffnen</button></div></div>`;
}

function bindCloudServerCard(s) {
  $$('[data-cloud-goto]').forEach((el) => el.onclick = () => openCloud());
  $$('[data-cloud-push]').forEach((el) => el.onclick = () => cloudUpload(el.dataset.cloudPush));
  $$('[data-cloud-pull-local]').forEach((el) => el.onclick = () => cloudDownload(el.dataset.cloudPullLocal));
  $$('[data-copy]').forEach((el) => el.onclick = () => copyText(el.dataset.copy, 'Adresse'));
}


/* ------------------------------------------------------------------ Hilfe */

function showHelp(key) { state.help = key; state.view = 'help'; render(); }
function renderHelp() {
  const tpl = $('#help-' + state.help);
  return tpl ? tpl.innerHTML : '<div class="page"><p>Kein Hilfetext gefunden.</p></div>';
}

/* ------------------------------------------------------------------ Start */

async function init() {
  $('#btnNew').onclick = startWizard;
  const quit = $('#btnQuit');
  if (quit) quit.onclick = async () => {
    if (!(await askConfirm({
      tone: 'danger', title: 'Manager beenden?', confirmText: 'Beenden',
      text: 'Alle laufenden Server werden vorher sauber gestoppt und die Welten gespeichert. '
        + 'Danach ist kein Server mehr erreichbar, bis du das Programm wieder startest.',
    }))) return;
    try {
      await api('shutdown', { method: 'POST' });
      clearInterval(state.timers.status); stopConsolePolling(); clearInterval(state.timers.xbox); clearInterval(state.timers.xboxJob);
      $('#view').innerHTML = '<div class="page"><div class="note note-ok"><b>Der Manager wird beendet.</b><p class="mb0">Alle Server werden gestoppt – dieses Fenster schließt sich gleich.</p></div></div>';
      setTimeout(() => { try { window.close(); } catch { /* Fenster ohne Script geöffnet */ } }, 1500);
    } catch (e) { toast(e.message, true); }
  };
  $$('.nav-item[data-help]').forEach((el) => el.onclick = () => showHelp(el.dataset.help));
  const nav = $('#navCloud');
  if (nav) nav.onclick = openCloud;
  try {
    await refresh();
  } catch (e) {
    $('#view').innerHTML = `<div class="page"><div class="note note-err"><b>Keine Verbindung zum Manager.</b><p>${esc(e.message)}</p>
      <p class="mb0">Bitte das Programm über <code>Start.bat</code> neu starten und das Fenster verwenden, das sich dabei öffnet.</p></div></div>`;
    return;
  }
  if (state.servers.length) { state.activeId = state.servers[0].id; state.view = 'server'; }
  render();
  state.timers.status = setInterval(() => refresh().catch(() => {}), 3000);
  // Cloud: beim Start einmal nachsehen (Hinweis „Jetzt zurückholen“), danach gelegentlich.
  loadCloud(true);
  setInterval(() => { if (state.view !== 'cloud') loadCloud(); }, 60000);
  setTimeout(() => checkUpdate(false), 8000);
  setInterval(() => checkUpdate(false), 30 * 60 * 1000);
}

document.addEventListener('DOMContentLoaded', init);
