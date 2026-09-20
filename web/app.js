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
  versions: { java: null, bedrock: null },
  consoleNext: 0,
  publicIp: null,
  timers: { status: null, console: null, job: null, xbox: null, xboxJob: null },
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
const typeLabel = (s) => s.type === 'bedrock' ? 'Bedrock Dedicated Server' : 'Java (Paper)' + (s.geyser ? ' + Crossplay' : '');
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

/* Öffentliche IP einmalig vom Router holen (UPnP, kein Fremddienst) und im Dashboard eintragen. */
async function ensurePublicIp() {
  if (state.publicIp || state.publicIpTried) return;
  state.publicIpTried = true;
  try { const d = await api('publicip?auto=1'); state.publicIp = d.ip; }
  catch { state.publicIpError = true; }
  const s = server();
  if (!s) return;
  $$('[data-pub-port]').forEach((el) => {
    el.textContent = publicAddr(s) ? `${publicAddr(s)} : ${el.dataset.pubPort}` : 'unbekannt – unter „Verbinden“ ermitteln';
  });
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
        <span class="muted small">${s.type === 'bedrock' ? 'BE' : 'JE'}</span>
      </a>`).join('');
  }
  $$('.srv-item', list).forEach((el) => el.onclick = () => openServer(el.dataset.id));
  $$('.nav-item').forEach((el) => el.classList.toggle('active', state.view === 'help' && el.dataset.help === state.help));
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
  return html;
}

/* ------------------------------------------------------------------ Ansichten */

function render() {
  const view = $('#view');
  stopConsolePolling();
  clearInterval(state.timers.xbox);
  switch (state.view) {
    case 'wizard': view.innerHTML = renderWizard(); bindWizard(); break;
    case 'install': view.innerHTML = renderInstall(); break;
    case 'server': view.innerHTML = renderServer(); bindServer(); break;
    case 'help': view.innerHTML = renderHelp(); break;
    case 'xbox': view.innerHTML = renderXboxWizard(); bindXboxWizard(); break;
    case 'update': view.innerHTML = renderUpdate(); bindUpdate(); break;
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
      <p class="lead">Richte in wenigen Minuten einen Minecraft-Server ein, auf den Xbox, PlayStation,
         Switch, Handy und PC beitreten können. Alles Nötige wird automatisch geladen.</p>
      <div class="spacer"></div>
      <button class="btn btn-primary" id="welcomeNew">+ Ersten Server anlegen</button>
    </div>
    <div class="grid3">
      <div class="card"><div class="pill pill-green">1</div><h3>Typ &amp; Version wählen</h3>
        <p class="muted small">Bedrock für Konsolen oder Java mit Crossplay – die Versionen kommen direkt von Mojang und PaperMC.</p></div>
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
    step: 0, type: 'bedrock', version: '', versionMode: 'latest',
    name: 'Mein Server', motd: 'Willkommen auf meinem Server', port: 19132, bedrock_port: 19132,
    max_players: 10, ram_mb: 4096, gamemode: 'survival', difficulty: 'easy', view_distance: 10,
    level_seed: '', public_address: '', auto_portmap: false, online_mode: true, allow_cheats: false, pvp: true, geyser: true, eula_accepted: false,
  };
  state.view = 'wizard';
  render();
  loadVersions('bedrock');
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

const STEP_NAMES = ['Server-Typ', 'Version', 'Einstellungen', 'Fertigstellen'];

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

  return `
  <div class="head"><div><h1>Neuer Server</h1><div class="head-sub">Schritt ${w.step + 1} von 4 · ${STEP_NAMES[w.step]}</div></div>
    <button class="btn btn-sm" id="wzCancel">Abbrechen</button></div>
  <div class="page">
    ${stepBar(STEP_NAMES, w.step)}
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
    <button class="choice ${w.type === 'bedrock' ? 'sel' : ''}" data-type="bedrock">
      <span class="pill pill-green">Empfohlen für Konsolen</span>
      <h3>Bedrock Dedicated Server</h3>
      <p>Offizieller Server von Mojang. Für <b>Xbox, PS4/PS5, Switch, Handy</b> und die Windows-App.
         Keine Java-Spieler.</p>
    </button>
    <button class="choice ${w.type === 'java' ? 'sel' : ''}" data-type="java">
      <span class="pill pill-blue">Crossplay</span>
      <h3>Java + Bedrock (Crossplay)</h3>
      <p>Paper-Server mit Geyser &amp; Floodgate. <b>Java-Spieler und Konsolen</b> spielen gemeinsam.
         Braucht etwas mehr RAM.</p>
    </button>
  </div>
  <div class="note note-info">Unsicher? Unter <b>Hilfe → Bedrock &amp; Crossplay</b> in der Seitenleiste ist der Unterschied erklärt.
    Der Typ lässt sich später nicht ändern, alle anderen Einstellungen schon.</div>`;
}

function wizardStepVersion(w) {
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
  <p class="lead">Java-Spieler müssen die gleiche Version wie der Server haben. Bedrock-Spieler
     (über Crossplay) kommen mit jeder aktuellen Version.</p>
  <div class="field"><label>Minecraft-Version (Paper)</label>
    <select id="wzJavaVersion">${list.map((x) => `<option ${x === w.version ? 'selected' : ''}>${esc(x)}</option>`).join('')}</select>
    <div class="hint">Die passende Java-Laufzeit (z.&nbsp;B. Java 25 für 26.x, Java 21 für 1.21) wird automatisch mitgeladen – nichts muss installiert werden.</div></div>
  <div class="note note-info mb0">Für Crossplay wird die <b>neueste Version</b> empfohlen: Geyser unterstützt
    immer die aktuellste Java-Version am besten.</div>`;
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
  return `
  <div class="grid2" style="margin-top:0">
    <div>
      ${fieldInput(prefix + 'name', 'Name des Servers', s.name, { hint: 'Nur für dich in dieser Liste.' })}
      ${fieldInput(prefix + 'motd', 'MOTD – Text in der Serverliste', s.motd, { hint: 'Das sehen Spieler, wenn sie den Server in ihrer Liste haben.' })}
      ${fieldInput(prefix + 'public_address', 'Öffentliche Adresse / MyFRITZ! (optional)', s.public_address || '',
        { placeholder: 'z. B. abc123.myfritz.net', hint: 'Wird im Dashboard als Adresse für Freunde angezeigt. Leer = öffentliche IP automatisch von der FritzBox ermitteln.' })}
      ${fieldInput(prefix + 'port', java ? 'Port für Java (TCP)' : 'Port (TCP, Verbindungsaufbau)', s.port,
        { type: 'number', min: 1024, max: 65535, hint: java ? 'Standard: 25565' : 'Standard: 19132 – Konsolen erwarten diesen Port.' })}
      ${java && s.geyser !== false ? fieldInput(prefix + 'bedrock_port', 'Port für Bedrock / Konsolen (UDP)', s.bedrock_port,
        { type: 'number', min: 1024, max: 65535, hint: 'Standard: 19132' }) : ''}
      ${fieldInput(prefix + 'max_players', 'Maximale Spieler', s.max_players, { type: 'number', min: 1, max: 200,
        hint: java ? '' : 'Bestimmt auch die Größe des UDP-Bereichs für Spieldaten – nach einer Änderung Firewall-Regel und FritzBox-Freigabe neu anlegen (Tab „Verbinden“).' })}
      ${java ? '' : `<div class="field"><label for="${prefix}public_ip">Öffentliche IPv4 für Freunde aus dem Internet (optional)</label>
        <div class="flex"><input type="text" id="${prefix}public_ip" value="${esc(s.public_ip || '')}" placeholder="leer = beim Start automatisch vom Router (UPnP)">
          <button class="btn btn-sm" type="button" id="${prefix}pubip">IP ermitteln</button></div>
        <div class="hint">Bedrock (NetherNet) bietet Spielern nur die Adressen an, die es kennt – ohne öffentliche IPv4 erreichen Freunde aus dem Internet den Server trotz Portfreigabe nicht.
          Leer lassen, wenn deine FritzBox die IP per UPnP meldet; sonst hier eintragen (ändert sich die IP, neu eintragen). Bei DS-Lite gibt es keine eigene IPv4.</div></div>`}
      ${java ? `<div class="field"><label for="${prefix}ram_mb">Arbeitsspeicher: <b id="${prefix}ramLabel">${gb(s.ram_mb)}</b></label>
        <input type="range" id="${prefix}ram_mb" min="1024" max="16384" step="512" value="${s.ram_mb}">
        <div class="hint">2–4 GB reichen für kleine Runden. Für Crossplay mindestens 3 GB. Nicht mehr als die Hälfte deines PC-RAMs.</div></div>` : ''}
    </div>
    <div>
      ${fieldSelect(prefix + 'gamemode', 'Spielmodus', s.gamemode, [['survival', 'Überleben'], ['creative', 'Kreativ'], ['adventure', 'Abenteuer']])}
      ${fieldSelect(prefix + 'difficulty', 'Schwierigkeit', s.difficulty, [['peaceful', 'Friedlich'], ['easy', 'Leicht'], ['normal', 'Normal'], ['hard', 'Schwer']])}
      <div class="field"><label for="${prefix}view_distance">Sichtweite: <b id="${prefix}vdLabel">${s.view_distance} Chunks</b></label>
        <input type="range" id="${prefix}view_distance" min="4" max="32" value="${s.view_distance}">
        <div class="hint">Höher = schöner, aber mehr Last. 8–12 ist ein guter Wert.</div></div>
      ${fieldInput(prefix + 'level_seed', 'Welt-Seed (optional)', s.level_seed, { placeholder: 'leer = zufällig', hint: 'Nur für neue Welten wirksam.' })}
      ${fieldCheck(prefix + 'online_mode', 'Xbox-Konto erforderlich (online-mode)',
        'Empfohlen. Spieler werden bei Xbox Live geprüft – so kann sich niemand als jemand anderes ausgeben.', s.online_mode)}
      ${fieldCheck(prefix + 'allow_cheats', java ? 'Befehlsblöcke erlauben' : 'Cheats erlauben',
        java ? 'Aktiviert Befehlsblöcke auf dem Server.' : 'Spieler mit Operator-Rechten dürfen Befehle wie /gamemode nutzen.', s.allow_cheats)}
      ${java ? fieldCheck(prefix + 'pvp', 'PvP', 'Spieler können sich gegenseitig verletzen.', s.pvp) : ''}
      ${java ? fieldCheck(prefix + 'geyser', 'Bedrock-Crossplay (Geyser + Floodgate)', 'Konsolen und Handys können beitreten. Sehr empfohlen.', s.geyser) : ''}
      ${fieldCheck(prefix + 'auto_portmap', 'Portfreigabe beim Start automatisch anfordern (FritzBox UPnP)',
        'Legt die nötigen Freigaben bei jedem Serverstart per UPnP an. In der FritzBox muss „Selbstständige Portfreigaben“ für diesen PC erlaubt sein.', !!s.auto_portmap)}
    </div>
  </div>`;
}

function readSettingsForm(prefix, base) {
  const get = (k) => $('#' + prefix + k);
  const out = { ...base };
  for (const k of ['name', 'motd', 'level_seed', 'gamemode', 'difficulty', 'public_ip', 'public_address']) if (get(k)) out[k] = get(k).value;
  for (const k of ['port', 'bedrock_port', 'max_players', 'ram_mb', 'view_distance']) if (get(k)) out[k] = Number(get(k).value);
  for (const k of ['online_mode', 'allow_cheats', 'pvp', 'geyser', 'auto_portmap']) if (get(k)) out[k] = get(k).checked;
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
  const rows = [
    ['Typ', w.type === 'bedrock' ? 'Bedrock Dedicated Server' : 'Java (Paper)' + (w.geyser ? ' + Crossplay' : '')],
    ['Version', w.version], ['Name', w.name], ['MOTD', w.motd],
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
    <p>Der Server wird direkt von Mojang bzw. PaperMC heruntergeladen. Dafür musst du die
       <a href="${EULA_URL}" target="_blank" rel="noopener noreferrer">Minecraft-EULA</a> und die
       <a href="${PRIVACY_URL}" target="_blank" rel="noopener noreferrer">Datenschutzbestimmungen von Microsoft</a> akzeptieren.</p>
    ${fieldCheck('wz_eula', 'Ich akzeptiere die Minecraft-EULA', 'Ohne diese Zustimmung darf der Server nicht betrieben werden.', w.eula_accepted)}
  </div>
  <div class="note note-info mb0">Es werden je nach Typ <b>40–150 MB</b> heruntergeladen. Der Server startet danach
     noch nicht automatisch – du startest ihn selbst auf der Übersichtsseite.</div>`;
}

function bindWizard() {
  const w = state.wizard;
  $('#wzCancel').onclick = () => { state.view = state.servers.length ? 'server' : 'welcome'; if (!state.activeId && state.servers[0]) state.activeId = state.servers[0].id; render(); };
  $('#wzBack').onclick = () => { collectWizard(); w.step = Math.max(0, w.step - 1); render(); };
  const next = $('#wzNext');
  if (next) next.onclick = () => {
    if (!collectWizard()) return;
    if (w.step === 0) { w.port = w.type === 'bedrock' ? 19132 : 25565; w.version = ''; w.versionMode = 'latest'; loadVersions(w.type); }
    if (w.step === 1 && !w.version) { toast('Bitte eine Version wählen.', true); return; }
    w.step++; render();
  };
  const install = $('#wzInstall');
  if (install) install.onclick = async () => {
    w.eula_accepted = $('#wz_eula').checked;
    if (!w.eula_accepted) { toast('Bitte zuerst die Minecraft-EULA bestätigen.', true); return; }
    install.disabled = true;
    try {
      const data = await api('servers', { method: 'POST', body: w });
      state.install = { serverId: data.server.id, jobId: data.job_id, job: null, after: 'overview' };
      state.activeId = data.server.id;
      state.view = 'install';
      await refresh();
      render();
      pollJob();
    } catch (e) { toast(e.message, true); install.disabled = false; }
  };

  $$('[data-type]').forEach((el) => el.onclick = () => { w.type = el.dataset.type; w.geyser = true; render(); });
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
    if (!w.name.trim()) { toast('Bitte einen Namen eingeben.', true); return false; }
    if (w.type === 'java' && w.geyser && w.port === w.bedrock_port) { toast('Java-Port und Bedrock-Port müssen sich unterscheiden.', true); return false; }
  }
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
  const tabs = [['overview', 'Übersicht'], ['connect', 'Verbinden'], ['console', 'Konsole'], ['files', 'Dateien'], ['settings', 'Einstellungen']];
  let body = '';
  if (state.tab === 'overview') body = tabOverview(s);
  else if (state.tab === 'connect') body = tabConnect(s);
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
      <button class="btn btn-primary" id="btnStart" ${s.running || !s.installed || s.installing ? 'disabled' : ''}>▶ Starten</button>
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
          <div class="a-v" data-pub-port="${p.port}">${publicAddr(s) ? esc(publicAddr(s)) + ' : ' + p.port : '… wird ermittelt'}</div></div><span class="pill pill-green">${p.proto}</span></div>`).join('')}
        <div class="muted small">${s.public_address
          ? 'Feste Adresse aus den <a href="#" data-tab-go="settings">Einstellungen</a>.'
          : 'Öffentliche IP von der FritzBox ermittelt – für eine feste MyFRITZ!-Adresse: <a href="#" data-tab-go="settings">Einstellungen</a>.'}
          Portfreigabe nötig: <a href="#" data-tab-go="connect">Verbinden</a>.</div>
        <div class="lan-line muted small">Im Heimnetz: ${addrPorts(s).map((p) => `<code>${esc(ip)}:${p.port}</code>`).join(' · ')}</div>
      </div>
    </div>

    <div class="kpis">
      <div class="stat"><div class="k">Spieler</div><div class="v">max. ${s.max_players}</div></div>
      <div class="stat"><div class="k">${s.type === 'java' ? 'Arbeitsspeicher' : 'Sichtweite'}</div><div class="v">${s.type === 'java' ? gb(s.ram_mb) : s.view_distance + ' Chunks'}</div></div>
      <div class="stat"><div class="k">Spielmodus</div><div class="v sm">${GM[s.gamemode] || s.gamemode} · ${DF[s.difficulty] || s.difficulty}</div></div>
      <div class="stat"><div class="k">Xbox-Konto nötig</div><div class="v sm">${s.online_mode ? 'Ja (empfohlen)' : 'Nein'}</div></div>
    </div>

    <div class="grid2" style="margin:0">
      <div class="card" id="xboxCard">${xboxCard(s)}</div>
      <div class="card">
        <div class="card-head"><h3>🌍 Welten &amp; Backups</h3>
          <button class="btn btn-sm" id="btnBackup" ${s.running ? 'disabled title="Server zuerst stoppen"' : ''}>🗜️ Backup erstellen</button></div>
        <div id="worldsBox" class="muted small">Wird geladen …</div>
      </div>
    </div>

    <div class="grid2" style="margin:0">
      <div class="card">
        <h3 style="margin-top:0">So geht es weiter</h3>
        <ul class="checklist">
          <li><span class="ck ${s.installed ? 'on' : ''}">${s.installed ? '✓' : ''}</span><span><b>Server eingerichtet</b></span></li>
          <li><span class="ck ${s.running ? 'on' : ''}">${s.running ? '✓' : ''}</span><span><b>Server starten</b> – oben rechts. Der erste Start erzeugt die Welt (20–90 s).</span></li>
          <li><span class="ck"></span><span><b>Im Heimnetz beitreten:</b> Handy/Windows-App → <b>Server → Server hinzufügen</b> mit
              <code>${esc(ip)}</code> und Port <code>${s.type === 'java' && s.geyser ? s.bedrock_port : s.port}</code>${s.type === 'java' ? `, Java-Spieler <code>${esc(ip)}:${s.port}</code>` : ''}.</span></li>
          <li><span class="ck ${s.xbox && s.xbox.state === 'online' ? 'on' : ''}">${s.xbox && s.xbox.state === 'online' ? '✓' : ''}</span><span><b>Xbox / PS5 / Switch:</b> am einfachsten über den <b>Xbox-Freunde-Modus</b> links – oder über den DNS-Weg (Hilfe).</span></li>
          <li><span class="ck"></span><span><b>Freunde von außerhalb:</b> Port in FritzBox und Windows-Firewall freigeben – siehe <a href="#" data-tab-go="connect">Verbinden</a>.</span></li>
        </ul>
        <div class="note note-warn" style="margin:14px 0 0"><b>Alle Bedrock-Spieler brauchen ein Microsoft-/Xbox-Konto</b> im Spiel –
          sonst „Verbindung zur Welt nicht möglich“. <a href="#" data-gohelp="xbox">Hilfe → Xbox-Konto verbinden</a>.</div>
      </div>
      <div class="card">
        <div class="card-head"><h3>🖥 Konsole – letzte Zeilen</h3><button class="btn btn-sm" data-tab-go="console">Öffnen</button></div>
        <div class="minilog" id="miniLog">${s.running ? 'Wird geladen …' : 'Server ist gestoppt.'}</div>
      </div>
    </div>
  </div>`;
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
    if (!confirm('Gespeicherte Anmeldung verwerfen? Beim nächsten Start wird ein neuer Code angezeigt.')) return;
    try { await api(`servers/${s.id}/xbox/reset`, { method: 'POST' }); toast('Anmeldung verworfen.'); await refresh(); } catch (e) { toast(e.message, true); }
  };
  const disable = $('#btnXboxDisable'); if (disable) disable.onclick = async () => {
    if (!confirm('Xbox-Freunde-Modus ausschalten? Der Server verschwindet aus der Freundesliste.')) return;
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
  <div class="addr"><div><div class="a-k">Öffentliche Adresse</div>
    <div class="a-v" id="pubAddr">${pub ? esc(pub) + ' : ' + addrPorts(s)[0].port : '– noch nicht ermittelt –'}</div></div>
    <button class="btn btn-sm" id="btnPublicIp">Öffentliche IP ermitteln</button></div>
  <p class="muted small">Die Abfrage fragt zuerst deine FritzBox (UPnP), sonst einmalig den Dienst <code>api.ipify.org</code> – nur wenn du den Knopf drückst.</p>
  ${s.type === 'bedrock' ? `<div class="note note-info"><b>Bedrock (NetherNet) muss seine öffentliche IPv4 kennen.</b> Der Server bietet Spielern nur Adressen an, die er kennt –
     die Portfreigabe allein reicht nicht. Der Manager fragt die IP bei jedem Start automatisch von der FritzBox ab (UPnP) und trägt sie ein; klappt das nicht
     (Konsole meldet es), die IP unter <a href="#" data-tab-go="settings">Einstellungen</a> eintragen. Bei DS-Lite (keine eigene IPv4) ist ein Beitritt aus dem Internet nicht möglich.</div>` : ''}

  <h2>5 · Xbox, PlayStation und Switch</h2>
  <p>Konsolen haben keinen „Server hinzufügen“-Knopf. Drei Wege:</p>
  <ul>
    <li><b>Xbox-Freunde-Modus (empfohlen):</b> Ein Bot-Konto zeigt den Server in der Freundesliste – Einrichtung auf der <a href="#" data-tab-go="overview">Übersicht</a>.</li>
    <li><b>Über einen Freund:</b> Jemand tritt per Handy/Windows-App bei, die Konsole folgt über die Freundesliste.</li>
    <li><b>Per DNS-Trick (BedrockConnect):</b> Auf der Konsole den DNS ändern, dann lässt sich die Adresse eingeben.</li>
  </ul>
  <p>Schritt für Schritt: <a href="#" data-gohelp="friends">Hilfe → Xbox-Freunde-Modus</a> · <a href="#" data-gohelp="console">Hilfe → Xbox &amp; PS5 verbinden</a> ·
     Anmeldeprobleme: <a href="#" data-gohelp="xbox">Hilfe → Xbox-Konto verbinden</a></p>
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
  if (f.dirty && !confirm('Ungespeicherte Änderungen verwerfen?')) return;
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
  </fieldset>

  <h2>Server löschen</h2>
  <p class="muted">Entfernt den Server <b>samt Welt</b> von diesem PC. Das lässt sich nicht rückgängig machen.</p>
  <button class="btn btn-danger" id="btnDelete" ${busy ? 'disabled' : ''}>Server endgültig löschen</button>`;
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
    bindXboxCard(s);
    state.xboxSig = JSON.stringify(s.xbox || {});
    loadOverviewExtras(s);
    if (!publicAddr(s)) ensurePublicIp();
  }

  if (state.tab === 'connect') {
    $('#btnPublicIp').onclick = async () => {
      const b = $('#btnPublicIp'); b.disabled = true;
      try { const d = await api('publicip'); state.publicIp = d.ip; $('#pubAddr').textContent = `${d.ip} : ${addrPorts(s)[0].port}`; }
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
        toast('Einstellungen gespeichert.'); await refresh(); render();
      } catch (e) { toast(e.message, true); }
    };
    $('#btnReinstall').onclick = async () => {
      const version = $('#st_version').value.trim();
      if (!confirm(`Server-Software für "${s.name}" jetzt als Version ${version} neu laden?`)) return;
      try {
        const d = await api(`servers/${s.id}/reinstall`, { method: 'POST', body: { version } });
        state.install = { serverId: s.id, jobId: d.job_id, job: null, after: 'overview' }; state.view = 'install'; render(); pollJob();
      } catch (e) { toast(e.message, true); }
    };
    $('#btnDelete').onclick = async () => {
      if (!confirm(`"${s.name}" wirklich löschen? Die Welt wird dabei entfernt.`)) return;
      try { await api(`servers/${s.id}`, { method: 'DELETE' }); toast('Server gelöscht.'); await refresh(); state.activeId = state.servers[0]?.id || null; state.view = state.activeId ? 'server' : 'welcome'; render(); }
      catch (e) { toast(e.message, true); }
    };
    if (s.type === 'java') {
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
  if (start) start.disabled = s.running || !s.installed || s.installing;
  if (stop) stop.disabled = !s.running;
  const inp = $('#cmdInput'), send = $('#cmdSend');
  if (inp) { inp.disabled = !s.running; inp.placeholder = s.running ? 'Befehl eingeben, z. B. list  oder  say Hallo' : 'Server starten, um Befehle zu senden'; }
  if (send) send.disabled = !s.running;
  if (state.tab === 'overview') {
    const sig = JSON.stringify(s.xbox || {});
    if (sig !== state.xboxSig) { state.xboxSig = sig; const card = $('#xboxCard'); if (card) { card.innerHTML = xboxCard(s); bindXboxCard(s); } }
    const bk = $('#btnBackup'); if (bk && bk.textContent.indexOf('läuft') < 0) bk.disabled = s.running;
    if (s.running) loadMiniLog(s);
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
    if (!confirm('Manager beenden? Alle laufenden Server werden dabei sauber gestoppt.')) return;
    try {
      await api('shutdown', { method: 'POST' });
      clearInterval(state.timers.status); stopConsolePolling(); clearInterval(state.timers.xbox); clearInterval(state.timers.xboxJob);
      $('#view').innerHTML = '<div class="page"><div class="note note-ok"><b>Der Manager wird beendet.</b><p class="mb0">Alle Server werden gestoppt – dieses Fenster schließt sich gleich.</p></div></div>';
      setTimeout(() => { try { window.close(); } catch { /* Fenster ohne Script geöffnet */ } }, 1500);
    } catch (e) { toast(e.message, true); }
  };
  $$('.nav-item[data-help]').forEach((el) => el.onclick = () => showHelp(el.dataset.help));
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
  setTimeout(() => checkUpdate(false), 8000);
  setInterval(() => checkUpdate(false), 30 * 60 * 1000);
}

document.addEventListener('DOMContentLoaded', init);
