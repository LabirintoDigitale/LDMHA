// Pannello "LDMHA" per Home Assistant (solo amministratori): mette in ordine la musica. Riconosce i brani
// dall'audio (AcoustID + MusicBrainz) o dal nome, propone tag e nome nuovo, li applica, sposta i doppioni nel
// cestino; ogni modifica resta nella cronologia e si può annullare.
// Fatto per librerie grandi: albero delle cartelle, tabella compatta che disegna solo le righe visibili,
// selezione multipla e scheda di dettaglio del brano aperto.

// deve coincidere con VERSION in const.py: se il backend ne riporta un'altra manca un riavvio
const PANEL_VERSION = "0.4.2";
const ICON = new URL("./icon.png", import.meta.url).href;
const STORE_KEY = "ldmha-panel";
const SAFE = 0.9; // sopra questa sicurezza (impronta audio) il brano si può sistemare in automatico
const ROW = 30; // altezza di una riga della tabella (px)
const WORKERS = 2; // brani riconosciuti insieme (l'impronta si calcola mentre si aspetta la rete)
const PATTERNS = [
  ["{artist} - {title}", "Artista - Titolo"],
  ["{track} - {title}", "Numero - Titolo"],
  ["{track} - {artist} - {title}", "Numero - Artista - Titolo"],
  ["{artist}/{album}/{track} - {title}", "Artista / Album / Numero - Titolo"],
];
const FIELDS = [["title", "Titolo"], ["artist", "Artista"], ["album", "Album"], ["albumartist", "Artista album"],
  ["date", "Anno"], ["tracknumber", "N."]];
const FILTERS = [["tutti", "Tutti"], ["dafare", "Da riconoscere"], ["proposte", "Con proposta"], ["sicuri", `Sicuri (≥ ${Math.round(SAFE * 100)}%)`],
  ["incerti", "Incerti"], ["doppioni", "Doppioni"], ["nontrovati", "Non trovati / errori"], ["fatti", "Sistemati"], ["cestino", "Nel cestino"]];
const COLS = [["st", "", "38px"], ["name", "File", "minmax(180px,3fr)"], ["cur", "Tag attuali", "minmax(140px,2.2fr)"],
  ["prop", "Proposta", "minmax(140px,2.4fr)"], ["score", "%", "48px"], ["len", "Durata", "56px"]];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const size = (b) => b > 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.round(b / 1024)} kB`;
const pct = (s) => `${Math.round((s || 0) * 100)}%`;
const dur = (t) => t ? `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}` : "";
const load = () => { try { return JSON.parse(localStorage.getItem(STORE_KEY) || "{}"); } catch (e) { return {}; } };
const save = (o) => { try { localStorage.setItem(STORE_KEY, JSON.stringify({ ...load(), ...o })); } catch (e) { /* niente */ } };
const base = (p) => p.split("/").pop();

class LdmhaPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    const st = load();
    this.s = {
      tab: "brani", info: null, path: st.path || "", recursive: !!st.recursive, pattern: st.pattern ?? PATTERNS[0][0],
      filter: "tutti", q: "", sort: st.sort || "name", desc: !!st.desc, list: null, loading: false, running: false,
      done: 0, total: 0, open: null,
      // sul telefono l'albero parte chiuso (copre l'elenco); sullo schermo grande come lo si è lasciato
      tree: window.innerWidth > 900 && (st.tree ?? true),
    };
    this.rows = new Map(); // percorso -> stato del brano
    this.view = []; // righe visibili (filtrate e ordinate)
    this.sel = new Set(); // percorsi selezionati
    this.nodes = new Map(); // albero: percorso -> {dirs: [...] | null, open}
    this._raf = 0;
    this.logs = []; // registro di questa sessione del pannello (il più recente in cima)
  }

  set hass(h) { const first = !this._hass; this._hass = h; if (first) this.start(); }
  get hass() { return this._hass; }
  set narrow(v) {
    this._narrow = v;
    this.shadowRoot.querySelector(".menu")?.toggleAttribute("hidden", !v);
  }

  ws(msg) { return this._hass.callWS(msg); }

  async start() {
    this.renderShell();
    try { this.s.info = await this.ws({ type: "ldmha/info" }); } catch (e) { this.s.err = e.message || String(e); }
    const roots = this.s.info?.roots || [];
    if (!roots.some((r) => this.s.path === r || this.s.path.startsWith(r + "/"))) this.s.path = roots[0] || "";
    for (const r of roots) this.nodes.set(r, { dirs: null, open: false });
    this.renderTop();
    if (this.s.path) {
      await this.expandTo(this.s.path);
      this.openDir(this.s.path);
    } else this.renderAll();
  }

  // ================================================================ struttura
  renderShell() {
    this.shadowRoot.innerHTML = `<style>${CSS}</style>
      <header><button class="menu" title="Menu" ${this._narrow ? "" : "hidden"}>☰</button><img class="logo" src="${ICON}" alt=""><h1>LDMHA</h1>
        <nav><button data-tab="brani" class="on">Brani</button><button data-tab="registro">Registro</button><button data-tab="cronologia">Cronologia</button></nav></header>
      <div id="top"></div>
      <div id="main"><aside id="tree"></aside><section id="list"></section><section id="det" hidden></section></div>
      <div id="hist" hidden></div><div id="logv" hidden></div>`;
    const r = this.shadowRoot;
    r.querySelector(".menu").onclick = () => this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true }));
    r.querySelectorAll("[data-tab]").forEach((b) => { b.onclick = () => this.setTab(b.dataset.tab); });
    this.addEventListener("keydown", (e) => this.onKey(e));
    this.tabIndex = 0;
  }

  setTab(t) {
    this.s.tab = t;
    const r = this.shadowRoot;
    r.querySelectorAll("[data-tab]").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
    r.getElementById("top").hidden = t !== "brani";
    r.getElementById("main").hidden = t !== "brani";
    r.getElementById("hist").hidden = t !== "cronologia";
    r.getElementById("logv").hidden = t !== "registro";
    if (t === "cronologia") this.renderHistory();
    if (t === "registro") this.ws({ type: "ldmha/info" }).then((i) => { this.s.info = i; }).catch(() => {}).finally(() => this.renderLog());
  }

  renderAll() { this.renderTop(); this.renderTree(); this.renderList(); this.renderDetail(); }

  // ================================================================ barra in alto
  renderTop() {
    const top = this.shadowRoot.getElementById("top");
    const i = this.s.info;
    if (!i) { top.innerHTML = `<div class="warn">${esc(this.s.err || "Carico…")}</div>`; return; }
    const warn = [];
    if (i.version !== PANEL_VERSION) warn.push(`Pannello ${PANEL_VERSION}, integrazione ${i.version}: riavvia Home Assistant e ricarica la pagina.`);
    if (!i.acoustid) warn.push(`Manca la chiave AcoustID: i brani si cercano solo per nome e tag. Creala gratis su <a href="https://acoustid.org/new-application" target="_blank" rel="noopener">acoustid.org</a> e inseriscila nelle opzioni dell'integrazione LDMHA.`);
    if (/non disponibile/.test(i.fpcalc)) warn.push(`Impronta audio ${esc(i.fpcalc)}.`);
    if (!i.roots.length) warn.push(`Nessuna delle cartelle configurate esiste (${esc(i.configured.join(", "))}).`);
    const n = this.sel.size;
    const run = this.s.running;
    top.innerHTML = `${warn.map((w) => `<div class="warn">${w}</div>`).join("")}
      <div class="bar">
        <button id="treeb" class="ic" title="Mostra / nascondi le cartelle">☰ Cartelle</button>
        <span class="crumbs">${this.crumbs()}</span>
        <label class="chk" title="Mostra anche i brani nelle sottocartelle"><input type="checkbox" id="rec" ${this.s.recursive ? "checked" : ""}> Sottocartelle</label>
        <button id="reload" class="ic" title="Aggiorna dal disco (apre solo i file nuovi o cambiati)">⟳ Aggiorna</button>
      </div>
      <div class="bar">
        <input id="q" type="search" placeholder="Cerca nell'elenco (nome, artista, titolo…)" value="${esc(this.s.q)}">
        <select id="filter">${FILTERS.map(([v, t]) => `<option value="${v}" ${v === this.s.filter ? "selected" : ""}>${t}</option>`).join("")}</select>
        <label class="pat" title="Schema del nome nuovo: {artist} {title} {album} {albumartist} {year} {track}; con / si creano sottocartelle; vuoto = solo i tag">Nome nuovo
          <input id="pattern" value="${esc(this.s.pattern)}" spellcheck="false" placeholder="(vuoto: solo i tag)"></label>
        <select id="preset" title="Schemi pronti"><option value="">Schemi…</option>${PATTERNS.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("")}</select>
      </div>
      <div class="bar acts">
        ${run ? `<button id="stop" class="pri">■ Ferma (${this.s.done}/${this.s.total})</button><progress max="${this.s.total}" value="${this.s.done}"></progress>`
          : `<button id="idall" class="pri" title="Riconosce i brani dell'elenco non ancora riconosciuti">Riconosci tutti</button>`}
        <button id="safe" title="Applica tag e nome ai brani riconosciuti dall'audio con sicurezza ≥ ${pct(SAFE)}">Applica i sicuri</button>
        <span class="sep"></span>
        <span class="hint">${n ? `<b>${n}</b> selezionati:` : "Selezione:"}</span>
        <button id="sid" ${n && !run ? "" : "disabled"}>Riconosci</button>
        <button id="sap" ${n ? "" : "disabled"}>Applica</button>
        <button id="str" ${n ? "" : "disabled"}>Cestino</button>
        <button id="snone" ${n ? "" : "disabled"}>Deseleziona</button>
        <span id="count" class="hint grow"></span>
      </div>`;
    const $ = (id) => top.querySelector("#" + id);
    $("treeb").onclick = () => { this.s.tree = !this.s.tree; save({ tree: this.s.tree }); this.renderTree(); };
    top.querySelectorAll("[data-go]").forEach((b) => { b.onclick = () => this.openDir(b.dataset.go); });
    $("rec").onchange = (e) => { this.s.recursive = e.target.checked; save({ recursive: this.s.recursive }); this.openDir(this.s.path); };
    $("reload").onclick = () => this.openDir(this.s.path, true);
    $("q").oninput = (e) => { this.s.q = e.target.value; clearTimeout(this._qt); this._qt = setTimeout(() => this.refreshView(), 200); };
    $("filter").onchange = (e) => { this.s.filter = e.target.value; this.refreshView(); };
    $("pattern").onchange = (e) => this.setPattern(e.target.value);
    $("preset").onchange = (e) => { if (e.target.value) { $("pattern").value = e.target.value; this.setPattern(e.target.value); } e.target.value = ""; };
    if (run) $("stop").onclick = () => { this.s.running = false; };
    else $("idall").onclick = () => this.identifyMany(this.view.filter((r) => r.status === "nuovo"));
    $("safe").onclick = () => this.applySafe();
    $("sid").onclick = () => this.identifyMany(this.selected().filter((r) => r.status !== "fatto" && r.status !== "cestino"));
    $("sap").onclick = () => this.applyMany(this.selected().filter((r) => r.edit && r.status !== "fatto" && r.status !== "cestino"));
    $("str").onclick = () => this.trashMany(this.selected().filter((r) => r.status !== "cestino"));
    $("snone").onclick = () => { this.sel.clear(); this.renderTop(); this.paint(); };
    this.updateCount();
  }

  crumbs() {
    const i = this.s.info, p = this.s.path;
    const root = (i?.roots || []).filter((r) => p === r || p.startsWith(r + "/")).sort((a, b) => b.length - a.length)[0];
    if (!root) return "";
    const out = [`<button data-go="${esc(root)}">${esc(root)}</button>`];
    let acc = root;
    for (const part of p.slice(root.length).split("/").filter(Boolean)) {
      acc += "/" + part;
      out.push(`<button data-go="${esc(acc)}">${esc(part)}</button>`);
    }
    return out.join("<i>/</i>");
  }

  updateCount() {
    const c = this.shadowRoot.getElementById("count");
    if (!c) return;
    const all = [...this.rows.values()];
    const n = (f) => all.filter(f).length;
    c.textContent = all.length ? `${this.view.length} di ${all.length} brani · ${n((r) => r.status === "fatto")} sistemati · ${n((r) => r.edit && r.status === "trovato")} con proposta · ${n((r) => r.dup)} doppioni` : "";
  }

  setPattern(v) {
    this.s.pattern = v.trim();
    save({ pattern: this.s.pattern });
    this.preview([...this.rows.values()].filter((r) => r.edit && r.status !== "fatto"));
  }

  // ================================================================ albero delle cartelle
  async loadNode(path) {
    const n = this.nodes.get(path) || { dirs: null, open: false };
    this.nodes.set(path, n);
    if (n.dirs) return n;
    try { n.dirs = (await this.ws({ type: "ldmha/dirs", path })).dirs; } catch (e) { n.dirs = []; n.err = e.message || String(e); }
    for (const d of n.dirs) if (!this.nodes.has(path + "/" + d)) this.nodes.set(path + "/" + d, { dirs: null, open: false });
    return n;
  }

  async expandTo(path) {
    const root = (this.s.info?.roots || []).find((r) => path === r || path.startsWith(r + "/"));
    if (!root) return;
    let acc = root;
    const parts = [root, ...path.slice(root.length).split("/").filter(Boolean).map((p) => (acc += "/" + p))];
    for (const p of parts.slice(0, -1)) { const n = await this.loadNode(p); n.open = true; }
    await this.loadNode(path);
  }

  renderTree() {
    const el = this.shadowRoot.getElementById("tree");
    el.hidden = !this.s.tree;
    if (!this.s.tree) return;
    const roots = this.s.info?.roots || [];
    const item = (path, name, depth) => {
      const n = this.nodes.get(path) || {};
      const leaf = n.dirs && !n.dirs.length;
      let h = `<div class="tn ${path === this.s.path ? "on" : ""}" style="padding-left:${4 + depth * 14}px">
        <button class="tw" data-tw="${esc(path)}" ${leaf ? "disabled" : ""}>${leaf ? "" : n.open ? "▾" : "▸"}</button>
        <button class="tl" data-go="${esc(path)}" title="${esc(path)}">${esc(name)}</button></div>`;
      if (n.open && n.dirs) for (const d of n.dirs) h += item(path + "/" + d, d, depth + 1);
      return h;
    };
    el.innerHTML = roots.map((r) => item(r, r, 0)).join("") || `<div class="note">Nessuna cartella</div>`;
    el.querySelectorAll("[data-tw]").forEach((b) => {
      b.onclick = async () => {
        const n = await this.loadNode(b.dataset.tw);
        n.open = !n.open;
        this.renderTree();
      };
    });
    el.querySelectorAll(".tl").forEach((b) => { b.onclick = () => { if (this._narrow) { this.s.tree = false; } this.openDir(b.dataset.go); }; });
    el.querySelector(".tn.on")?.scrollIntoView({ block: "nearest" });
  }

  // ================================================================ elenco dei brani
  // lettura della cartella a blocchi: i brani arrivano a gruppi e compaiono subito, con quante cartelle sono state
  // lette e dove si è arrivati; si può fermare (restano quelli già letti)
  // refresh: rilegge dal disco (aprendo solo i file cambiati) invece di usare l'elenco salvato nell'archivio
  async openDir(path, refresh = false) {
    this.s.running = false;
    this.stopScan();
    this.s.path = path;
    save({ path });
    this.s.list = { path, dirs: [], files: 0, truncated: false };
    this.s.scan = { count: 0, folders: 0, dir: "", done: false, t: Date.now() };
    this.s.open = null;
    this.rows.clear();
    this.sel.clear();
    const n = await this.loadNode(path);
    n.open = true;
    this.renderAll();
    const scan = this.s.scan;
    let pending = false;
    const onEvent = (ev) => {
      if (scan !== this.s.scan) return; // lettura vecchia (nel frattempo si è aperta un'altra cartella)
      if (ev.type === "start") { this.s.list.path = ev.path; this.s.list.dirs = ev.dirs; scan.cached = ev.cached || 0; }
      else if (ev.type === "files") {
        for (const f of ev.files) this.rows.set(f.path, this.restore(f));
        scan.count = ev.count; scan.folders = ev.folders; scan.dir = ev.dir;
      } else if (ev.type === "done") {
        scan.done = true; scan.count = ev.count; scan.folders = ev.folders; this.s.list.truncated = ev.truncated;
        const secs = ((Date.now() - scan.t) / 1000).toFixed(1);
        this.log(ev.cached
          ? `Cartella ${path}: ${ev.count} brani dall'archivio (letti dal disco il ${new Date(ev.cached * 1000).toLocaleString()}) in ${secs} s`
          : `Letta la cartella ${path}: ${ev.count} brani in ${ev.folders} cartelle, ${ev.opened} file aperti (nuovi o cambiati)${ev.removed ? `, ${ev.removed} tolti dall'archivio perché non ci sono più` : ""} (${secs} s)`);
        this.stopScan(false);
        this.markDups();
        this.preview([...this.rows.values()].filter((r) => r.edit && r.status === "trovato"));
      } else if (ev.type === "error") {
        scan.done = true; scan.error = ev.message;
        this.log(`Errore leggendo ${path}: ${ev.message}`);
        this.stopScan(false);
      }
      this.s.list.files = this.rows.size;
      if (!pending) { pending = true; setTimeout(() => { pending = false; this.refreshView(); }, ev.type === "files" ? 400 : 0); }
    };
    try {
      this._scanUnsub = await this._hass.connection.subscribeMessage(onEvent, { type: "ldmha/scan", path, recursive: this.s.recursive, refresh });
    } catch (e) {
      scan.done = true; scan.error = e.message || String(e);
      this.refreshView();
    }
  }

  // brano dall'archivio: tornano proposte, registro e "sistemato" dei riconoscimenti già fatti
  restore(f) {
    const r = { file: f, status: f.applied ? "fatto" : "nuovo" };
    const id = f.ident;
    if (id && !f.applied) {
      r.cands = id.candidates || [];
      r.note = id.note || "";
      r.log = id.log || [];
      r.status = r.cands.length ? "trovato" : "nontrovato";
      if (r.cands.length) this.choose(r, r.cands[0], false);
    } else if (id) r.log = id.log || [];
    delete f.ident;
    return r;
  }

  stopScan(user = true) {
    const u = this._scanUnsub;
    this._scanUnsub = null;
    if (u) u().catch?.(() => {});
    if (user && this.s.scan && !this.s.scan.done) {
      this.s.scan.done = true; this.s.scan.stopped = true;
      this.log(`Lettura fermata: ${this.s.scan.count} brani letti`);
      this.refreshView();
    }
  }

  match(r) {
    switch (this.s.filter) {
      case "dafare": if (r.status !== "nuovo") return false; break;
      case "proposte": if (!(r.edit && r.status === "trovato")) return false; break;
      case "sicuri": if (!this.isSafe(r)) return false; break;
      case "incerti": if (!(r.status === "trovato" && !this.isSafe(r))) return false; break;
      case "doppioni": if (!r.dup) return false; break;
      case "nontrovati": if (!["nontrovato", "errore"].includes(r.status)) return false; break;
      case "fatti": if (r.status !== "fatto") return false; break;
      case "cestino": if (r.status !== "cestino") return false; break;
      default: if (r.status === "cestino") return false;
    }
    const q = this.s.q.trim().toLowerCase();
    if (!q) return true;
    const t = r.file.tags || {}, c = r.edit || {};
    return [r.file.name, t.artist, t.title, t.album, c.artist, c.title, c.album].some((v) => v && String(v).toLowerCase().includes(q));
  }

  isSafe(r) { return !!(r.edit && r.cand && r.cand.source === "impronta" && r.cand.score >= SAFE && !r.dup && r.status === "trovato"); }

  sortKey(r) {
    const t = r.file.tags || {};
    switch (this.s.sort) {
      case "st": return ["errore", "nontrovato", "trovato", "nuovo", "lavoro", "salvo", "fatto", "cestino"].indexOf(r.status) + (r.dup ? -0.5 : 0);
      case "cur": return `${t.artist || "￿"} ${t.title || ""}`.toLowerCase();
      case "prop": return r.edit ? `${r.edit.artist} ${r.edit.title}`.toLowerCase() : "￿";
      case "score": return r.cand ? -r.cand.score : 1;
      case "len": return +t._length || 0;
      default: return r.file.name.toLowerCase();
    }
  }

  // struttura dell'elenco (una volta per cartella o filtro); i dati si aggiornano con refreshView
  renderList() {
    const el = this.shadowRoot.getElementById("list");
    if (!this.s.list) { el.innerHTML = ""; this.view = []; return; }
    const grid = COLS.map((c) => c[2]).join(" ");
    el.innerHTML = `<div id="scanbar"></div><div id="lnote"></div>
      <div class="vp" id="vp">
      <div class="th" style="grid-template-columns:${grid}">
        <span><input type="checkbox" id="all" title="Seleziona tutti quelli in elenco"></span>
        ${COLS.slice(1).map(([key, lab]) => `<button data-sort="${key}" class="${this.s.sort === key ? "on" : ""}">${lab}${this.s.sort === key ? (this.s.desc ? " ▾" : " ▴") : ""}</button>`).join("")}
      </div>
      <div class="sp" id="sp"></div></div>`;
    el.querySelector("#all").onchange = (e) => {
      for (const r of this.view) e.target.checked ? this.sel.add(r.file.path) : this.sel.delete(r.file.path);
      this.renderTop(); this.paint();
    };
    el.querySelectorAll("[data-sort]").forEach((b) => {
      b.onclick = () => {
        if (this.s.sort === b.dataset.sort) this.s.desc = !this.s.desc; else { this.s.sort = b.dataset.sort; this.s.desc = false; }
        save({ sort: this.s.sort, desc: this.s.desc });
        this.renderList();
      };
    });
    const vp = el.querySelector("#vp");
    vp.onscroll = () => { if (!this._raf) this._raf = requestAnimationFrame(() => { this._raf = 0; this.paint(); }); };
    vp.onclick = (e) => this.onRowClick(e);
    this.refreshView();
  }

  // ricalcola le righe visibili (filtro, ricerca, ordine) senza perdere la posizione di scorrimento
  refreshView() {
    const el = this.shadowRoot.getElementById("list");
    if (!el.querySelector("#vp")) { this.renderList(); return; }
    const k = (r) => this.sortKey(r);
    this.view = [...this.rows.values()].filter((r) => this.match(r))
      .sort((a, b) => { const x = k(a), y = k(b); return (x < y ? -1 : x > y ? 1 : 0) * (this.s.desc ? -1 : 1); });
    const all = el.querySelector("#all");
    all.checked = !!this.view.length && this.view.every((r) => this.sel.has(r.file.path));
    const sc = this.s.scan || {};
    el.querySelector("#scanbar").innerHTML = !sc.done
      ? `<div class="scan"><span class="spin"></span><span>Leggo… <b>${sc.count}</b> brani in ${sc.folders} cartelle${sc.dir && sc.dir !== "." ? ` · ora: ${esc(sc.dir)}` : ""}</span><button id="sstop">Ferma</button></div>`
      : sc.error ? `<div class="warn">${esc(sc.error)}</div>`
      : this.s.list.truncated ? `<div class="warn">Troppi brani: letti i primi ${sc.count}. Apri una sottocartella.</div>`
      : sc.stopped ? `<div class="warn">Lettura fermata: ${sc.count} brani letti. <button id="sagain">Rileggi</button></div>`
      : sc.cached ? `<div class="cache">Elenco salvato, letto dal disco il <b>${new Date(sc.cached * 1000).toLocaleString()}</b>. Se nel frattempo hai aggiunto o cambiato brani:
          <button id="srefresh">⟳ Aggiorna dal disco</button> <span class="mut">(apre solo i file nuovi o cambiati)</span></div>` : "";
    el.querySelector("#srefresh")?.addEventListener("click", () => this.openDir(this.s.path, true));
    el.querySelector("#sstop")?.addEventListener("click", () => this.stopScan());
    el.querySelector("#sagain")?.addEventListener("click", () => this.openDir(this.s.path));
    const files = this.rows.size;
    el.querySelector("#lnote").innerHTML = sc.done && !sc.error && !files
      ? `<div class="note">Nessun brano in questa cartella${this.s.recursive ? "" : " (attiva «Sottocartelle» per vedere anche quelli dentro)"}.</div>`
      : files && !this.view.length ? `<div class="note">Nessun brano con questo filtro.</div>` : "";
    this.paint();
    this.updateCount();
  }

  // disegna solo le righe che si vedono (con un margine): migliaia di brani restano fluidi
  paint() {
    const vp = this.shadowRoot.getElementById("vp");
    const sp = this.shadowRoot.getElementById("sp");
    if (!vp || !sp) return;
    sp.style.height = `${this.view.length * ROW}px`;
    const top = Math.max(0, vp.scrollTop - ROW); // sopra le righe c'è l'intestazione delle colonne (alta ROW)
    const from = Math.max(0, Math.floor(top / ROW) - 10);
    const to = Math.min(this.view.length, Math.ceil((top + vp.clientHeight) / ROW) + 10);
    const grid = COLS.map((c) => c[2]).join(" ");
    let h = "";
    for (let i = from; i < to; i++) h += this.rowHtml(this.view[i], i, grid);
    sp.innerHTML = h;
  }

  rowHtml(r, i, grid) {
    const f = r.file, t = f.tags || {}, p = f.path;
    const st = {
      nuovo: ["·", "", "da riconoscere"], lavoro: ["⟳", "run", "riconosco…"], salvo: ["⟳", "run", "salvo…"],
      trovato: ["✓", this.isSafe(r) ? "ok" : "mid", "proposta pronta"], nontrovato: ["?", "ko", "non trovato"],
      errore: ["!", "ko", r.err || "errore"], fatto: ["★", "done", "sistemato"], cestino: ["🗑", "ko", "nel cestino"],
    }[r.status] || ["", "", ""];
    const cur = [t.artist, t.title].filter(Boolean).join(" — ");
    const prop = r.edit ? [r.edit.artist, r.edit.title].filter(Boolean).join(" — ") : "";
    const tip = r.newName ? ` → ${r.newName}` : "";
    return `<div class="tr ${this.sel.has(p) ? "sel" : ""} ${this.s.open === p ? "open" : ""} ${r.status === "fatto" || r.status === "cestino" ? "dim" : ""}" data-i="${i}"
        style="top:${i * ROW}px;grid-template-columns:${grid}">
      <span class="c-st"><input type="checkbox" data-c ${this.sel.has(p) ? "checked" : ""}><b class="st ${st[1]}" title="${esc(st[2])}">${st[0]}</b></span>
      <span title="${esc(f.name)}">${esc(f.name)}</span>
      <span class="${cur ? "" : "mut"}" title="${esc(cur)}">${esc(cur || "senza tag")}</span>
      <span class="prop" title="${esc(prop + tip)}">${r.dup ? `<b class="dup" title="Stesso brano di: ${esc(r.dup)}">doppione</b> ` : ""}${esc(prop)}</span>
      <span class="num">${r.cand ? pct(r.cand.score) : ""}</span>
      <span class="num">${dur(+t._length)}</span></div>`;
  }

  onRowClick(e) {
    const tr = e.target.closest(".tr");
    if (!tr) return;
    const r = this.view[+tr.dataset.i];
    if (!r) return;
    const p = r.file.path;
    if (e.target.matches("[data-c]")) {
      // con Maiusc si seleziona tutto l'intervallo dall'ultima casella toccata
      const i = +tr.dataset.i;
      if (e.shiftKey && this._last != null) {
        const [a, b] = [Math.min(this._last, i), Math.max(this._last, i)];
        for (let k = a; k <= b; k++) e.target.checked ? this.sel.add(this.view[k].file.path) : this.sel.delete(this.view[k].file.path);
      } else e.target.checked ? this.sel.add(p) : this.sel.delete(p);
      this._last = i;
      this.renderTop(); this.paint();
      return;
    }
    this.openRow(r);
  }

  openRow(r) {
    this.s.open = r ? r.file.path : null;
    this.paint();
    this.renderDetail();
  }

  onKey(e) {
    if (this.s.tab !== "brani" || !this.s.open) return;
    if (e.composedPath()[0]?.matches?.("input, select, textarea")) return;
    const i = this.view.findIndex((r) => r.file.path === this.s.open);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const r = this.view[i + (e.key === "ArrowDown" ? 1 : -1)];
      if (r) { this.openRow(r); this.scrollTo(r); }
    } else if (e.key === "Enter" && this.view[i]?.edit) {
      e.preventDefault();
      this.apply(this.view[i]).then(() => { const n = this.view[i + 1]; if (n) { this.openRow(n); this.scrollTo(n); } });
    } else if (e.key === "Escape") this.openRow(null);
  }

  scrollTo(r) {
    const vp = this.shadowRoot.getElementById("vp");
    const i = this.view.indexOf(r);
    if (!vp || i < 0) return;
    const y = (i + 1) * ROW; // +1: l'intestazione delle colonne
    if (y - ROW < vp.scrollTop + ROW) vp.scrollTop = y - 2 * ROW;
    else if (y + ROW > vp.scrollTop + vp.clientHeight) vp.scrollTop = y + ROW - vp.clientHeight;
  }

  // una riga è cambiata: si ridisegna lei (e la scheda se è quella aperta)
  touch(r) {
    if (!this._paintT) this._paintT = requestAnimationFrame(() => { this._paintT = 0; this.paint(); this.updateCount(); });
    if (r && this.s.open === r.file.path) this.renderDetail();
  }

  // ================================================================ scheda del brano
  renderDetail() {
    const el = this.shadowRoot.getElementById("det");
    const r = this.s.open && this.rows.get(this.s.open);
    el.hidden = !r;
    if (!r) return;
    const f = r.file, t = f.tags || {};
    const closed = r.status === "fatto" || r.status === "cestino";
    let h = `<div class="dh"><b title="${esc(f.path)}">${esc(base(f.path))}</b><button class="ic" id="dx" title="Chiudi (Esc)">✕</button></div>
      <div class="mut small">${esc(f.path)} · ${size(f.size)}${t._length ? " · " + dur(+t._length) : ""}</div>
      <table class="kv">${FIELDS.map(([k, lab]) => `<tr><td>${lab}</td><td>${esc(t[k] || "")}</td></tr>`).join("")}</table>`;
    if (r.dup) h += `<div class="warn small">Stesso brano di «${esc(r.dup)}»: puoi spostarlo nel cestino.</div>`;
    if (r.note) h += `<div class="mut small">${esc(r.note)}</div>`;
    if (r.err) h += `<div class="warn small">${esc(r.err)}</div>`;
    if (r.cands?.length && !closed) {
      h += `<div class="lab">Proposte</div><div class="cands">${r.cands.map((c, i) => `<button data-cand="${i}" class="${c === r.cand ? "on" : ""}">
        <span class="sc ${c.source === "impronta" && c.score >= SAFE ? "ok" : ""}">${pct(c.score)}</span>
        <span><b>${esc(c.artist)} — ${esc(c.title)}</b><br><small>${esc([c.album, c.year].filter(Boolean).join(", "))}${c.source === "impronta" ? " · audio" : " · nome"}${c.duration ? " · " + dur(c.duration) : ""}</small></span></button>`).join("")}</div>`;
    }
    if (r.edit && !closed) {
      h += `<div class="lab">Tag nuovi</div><div class="ed">${FIELDS.map(([k, lab]) => `<label class="f-${k}">${lab}<input data-k="${k}" value="${esc(r.edit[k] || "")}"></label>`).join("")}</div>
        <div class="nn">${this.s.pattern ? (r.newName ? `Nome nuovo: <code>${esc(r.newName)}</code>` : r.nameErr ? `<span class="ko">${esc(r.nameErr)}</span>` : "") : "Solo i tag (nessuno schema per il nome)"}</div>`;
    }
    if (r.log?.length) h += `<details class="dlog" ${r.status === "nontrovato" || r.status === "errore" ? "open" : ""}><summary>Cosa ha fatto (${r.log.length} passaggi)</summary>
      <ol>${r.log.map((l) => `<li>${esc(l)}</li>`).join("")}</ol></details>`;
    if (r.searching) h += `<div class="srch"><input id="sq" placeholder="Artista - Titolo, o testo libero" value="${esc(r.q || "")}"><button id="sgo">Cerca</button></div>`;
    h += `<div class="dacts">${closed
      ? (r.entry ? `<button data-a="undo">Annulla la modifica</button>` : "")
      : `${r.edit ? `<button data-a="apply" class="pri" title="Invio">Applica</button>` : ""}
         <button data-a="id" ${r.status === "lavoro" ? "disabled" : ""}>Riconosci</button>
         <button data-a="search">Cerca…</button>
         <button data-a="trash">Cestino</button>`}</div>
      <div class="mut small">↑ ↓ brano precedente / successivo · Invio applica e passa al successivo · Esc chiude</div>`;
    el.innerHTML = h;
    el.querySelector("#dx").onclick = () => this.openRow(null);
    el.querySelectorAll("[data-cand]").forEach((b) => { b.onclick = () => this.choose(r, r.cands[+b.dataset.cand]); });
    el.querySelectorAll(".ed input").forEach((inp) => inp.addEventListener("change", () => { r.edit[inp.dataset.k] = inp.value.trim(); this.preview([r]); }));
    el.querySelectorAll("[data-a]").forEach((b) => { b.onclick = () => this.action(r, b.dataset.a); });
    if (r.searching) {
      const q = el.querySelector("#sq");
      const go = () => this.search(r, q.value);
      el.querySelector("#sgo").onclick = go;
      q.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.stopPropagation(); go(); } });
      requestAnimationFrame(() => q.focus());
    }
  }

  // ================================================================ azioni
  selected() { return [...this.sel].map((p) => this.rows.get(p)).filter(Boolean); }

  action(r, a) {
    if (a === "id") this.identify(r);
    else if (a === "search") {
      const t = r.file.tags || {};
      r.searching = !r.searching;
      r.q = r.q || [t.artist, t.title].filter(Boolean).join(" - ") || base(r.file.path).replace(/\.[^.]+$/, "");
      this.renderDetail();
    } else if (a === "apply") this.apply(r);
    else if (a === "trash") this.trashMany([r], true);
    else if (a === "undo") this.undo(r);
  }

  choose(r, c, update = true) {
    r.cand = c;
    if (!c) { r.edit = null; this.touch(r); return; }
    const t = r.file.tags || {};
    r.edit = {
      title: c.title || t.title || "", artist: c.artist || t.artist || "", album: c.album || t.album || "",
      albumartist: c.albumartist || t.albumartist || "", date: c.year || t.date || "", tracknumber: c.track || t.tracknumber || "",
      musicbrainz_trackid: c.mbid || "",
    };
    if (!update) return;
    this.markDups();
    this.preview([r]);
  }

  async identify(r) {
    r.status = "lavoro"; r.err = ""; r.note = "";
    this.touch(r);
    try {
      this.log("Riconoscimento avviato", r.file.path, "run");
      const res = await this.ws({ type: "ldmha/identify", path: r.file.path });
      r.cands = res.candidates || [];
      r.note = res.note || "";
      r.log = res.log || [];
      for (const line of r.log) this.log(line, r.file.path);
      r.status = r.cands.length ? "trovato" : "nontrovato";
      if (r.cands.length) this.choose(r, r.cands[0]); else r.edit = null;
    } catch (e) {
      r.status = "errore"; r.err = e.message || String(e);
      r.log = [...(r.log || []), `Errore: ${r.err}`];
      this.log(`Errore: ${r.err}`, r.file.path, "ko");
    }
    this.touch(r);
  }

  async identifyMany(list) {
    if (!list.length || this.s.running) return;
    this.s.running = true; this.s.done = 0; this.s.total = list.length;
    this.renderTop();
    let k = 0;
    const work = async () => {
      while (this.s.running && k < list.length) {
        const r = list[k++];
        if (r.status !== "cestino") await this.identify(r);
        this.s.done++;
        const pb = this.shadowRoot.querySelector("#top progress");
        if (pb) { pb.value = this.s.done; this.shadowRoot.getElementById("stop").textContent = `■ Ferma (${this.s.done}/${this.s.total})`; }
      }
    };
    await Promise.all(Array.from({ length: WORKERS }, work));
    this.s.running = false;
    this.renderTop();
    if (this.s.filter !== "tutti") this.refreshView();
  }

  async search(r, text) {
    text = (text || "").trim();
    if (!text) return;
    r.q = text; r.status = "lavoro";
    this.touch(r);
    const parts = text.split(/\s+[-–—]\s+/);
    const msg = parts.length >= 2 ? { artist: parts[0], title: parts.slice(1).join(" - ") } : { query: text };
    try {
      const res = await this.ws({ type: "ldmha/search", path: r.file.path, ...msg });
      r.cands = res.candidates || [];
      r.searching = false;
      r.note = r.cands.length ? "" : "Nessun risultato su MusicBrainz";
      r.status = r.cands.length ? "trovato" : "nontrovato";
      this.log(`Ricerca «${text}» su MusicBrainz: ${r.cands.length} risultati`, r.file.path);
      if (r.cands.length) this.choose(r, r.cands[0]);
    } catch (e) { r.status = "errore"; r.err = e.message || String(e); this.log(`Ricerca non riuscita: ${r.err}`, r.file.path, "ko"); }
    this.touch(r);
  }

  tagsFor(r) {
    const t = { ...(r.file.tags || {}) };
    delete t._length;
    for (const [k, v] of Object.entries(r.edit || {})) if (v) t[k] = v;
    return t;
  }

  async preview(rows) {
    rows = rows.filter((r) => r.edit);
    if (!rows.length) return;
    if (!this.s.pattern) { rows.forEach((r) => { r.newName = ""; r.nameErr = ""; this.touch(r); }); return; }
    for (let i = 0; i < rows.length; i += 200) {
      const part = rows.slice(i, i + 200);
      try {
        const res = await this.ws({ type: "ldmha/preview", pattern: this.s.pattern, items: part.map((r) => ({ path: r.file.path, tags: this.tagsFor(r) })) });
        for (const it of res.items) {
          const r = this.rows.get(it.path);
          if (!r) continue;
          r.newName = it.name || ""; r.nameErr = it.error || "";
          if (r.newName === base(r.file.path)) r.newName = `${r.newName} (uguale)`;
        }
      } catch (e) { /* anteprima non essenziale */ }
    }
    rows.forEach((r) => this.touch(r));
  }

  relName(p) { const d = this.s.list?.path; return d && p.startsWith(d + "/") ? p.slice(d.length + 1) : p; }

  // un brano cambia percorso (rinominato o annullato): si aggiornano elenco, selezione e scheda
  move(r, to) {
    const from = r.file.path;
    if (from === to) return;
    this.rows.delete(from);
    r.file = { ...r.file, path: to, name: this.relName(to) };
    this.rows.set(to, r);
    if (this.sel.delete(from)) this.sel.add(to);
    if (this.s.open === from) this.s.open = to;
  }

  async apply(r) {
    if (!r.edit || r.status === "fatto") return;
    r.status = "salvo"; r.err = "";
    this.touch(r);
    try {
      const res = await this.ws({ type: "ldmha/apply", path: r.file.path, tags: r.edit, pattern: this.s.pattern });
      const old = r.file.path;
      this.move(r, res.path);
      r.file.tags = res.tags;
      r.entry = res.entry; r.status = "fatto";
      this.log(`Tag scritti${old !== res.path ? `, rinominato in «${base(res.path)}»` : ""}`, old, "ok");
    } catch (e) { r.status = "trovato"; r.err = e.message || String(e); this.log(`Applica non riuscito: ${r.err}`, r.file.path, "ko"); }
    this.touch(r);
  }

  async applyMany(list) {
    if (!list.length) return;
    if (!confirm(`Applicare tag${this.s.pattern ? " e nome nuovo" : ""} a ${list.length} brani?\n(Ogni modifica si può annullare dalla cronologia.)`)) return;
    for (const r of list) await this.apply(r);
    this.renderTop();
  }

  applySafe() {
    const list = [...this.rows.values()].filter((r) => this.isSafe(r));
    if (!list.length) { alert(`Nessun brano riconosciuto dall'audio con sicurezza ≥ ${pct(SAFE)} (usa prima «Riconosci tutti»).`); return; }
    this.applyMany(list);
  }

  async trashMany(list, one = false) {
    if (!list.length) return;
    if (!confirm(one ? `Spostare nel cestino «${base(list[0].file.path)}»?\n(Si può rimettere a posto dalla cronologia.)`
      : `Spostare nel cestino ${list.length} brani?\n(Si possono rimettere a posto dalla cronologia.)`)) return;
    for (const r of list) {
      try {
        const res = await this.ws({ type: "ldmha/trash", path: r.file.path });
        r.entry = res.entry; r.status = "cestino"; r.err = "";
        this.log("Spostato nel cestino", r.file.path, "ok");
        this.sel.delete(r.file.path);
      } catch (e) { r.err = e.message || String(e); this.log(`Cestino non riuscito: ${r.err}`, r.file.path, "ko"); }
      this.touch(r);
    }
    this.markDups();
    this.renderTop();
  }

  async undo(r) {
    if (!r.entry) return;
    try {
      const res = await this.ws({ type: "ldmha/undo", id: r.entry.id });
      if (r.entry.kind === "apply") r.file.tags = { ...(r.entry.old_tags || {}), _length: r.file.tags?._length };
      this.move(r, res.path);
      r.entry = null; r.status = r.cands?.length ? "trovato" : "nuovo"; r.err = "";
      this.log("Modifica annullata", res.path, "ok");
      this.markDups();
      this.preview([r]);
    } catch (e) { r.err = e.message || String(e); }
    this.touch(r);
  }

  // stesso brano (stesso codice MusicBrainz) in più file: tutti tranne il primo sono doppioni
  markDups() {
    const seen = new Map();
    for (const r of this.rows.values()) {
      r.dup = "";
      const id = r.status !== "cestino" && (r.cand?.mbid || r.file.tags?.musicbrainz_trackid);
      if (!id) continue;
      if (seen.has(id)) r.dup = seen.get(id).file.name; else seen.set(id, r);
    }
    this.touch();
  }

  // ================================================================ registro
  log(text, file = "", kind = "") {
    this.logs.unshift({ t: new Date(), text, file, kind });
    if (this.logs.length > 1000) this.logs.length = 1000;
    if (this.s.tab === "registro") { clearTimeout(this._lt); this._lt = setTimeout(() => this.renderLog(), 200); }
  }

  renderLog() {
    const el = this.shadowRoot.getElementById("logv");
    el.innerHTML = `<div class="bar"><span class="hint grow" style="text-align:left">Cosa ha fatto LDMHA in questa sessione (il più recente in cima). Con il pannello chiuso il registro si azzera; le modifiche ai file restano nella Cronologia.</span>
      <button id="lclear">Svuota il registro</button></div>
      <div class="bar"><span class="hint grow" style="text-align:left">Archivio: ${this.s.info?.db ? `${this.s.info.db.files} brani salvati, ${this.s.info.db.identified} riconosciuti, ${this.s.info.db.applied} sistemati` : "—"}.
        Svuotandolo, la prossima apertura di ogni cartella rilegge tutto dal disco e i riconoscimenti salvati si perdono (i file non vengono toccati).</span>
        <button id="dbclear">Svuota l'archivio</button></div>
      ${this.logs.length ? `<div class="lg">${this.logs.map((l) => `<div class="lr ${l.kind}"><span class="mut">${l.t.toLocaleTimeString()}</span>
        <span>${l.file ? `<b>${esc(base(l.file))}</b> ` : ""}${esc(l.text)}</span></div>`).join("")}</div>` : `<div class="note">Ancora niente.</div>`}`;
    el.querySelector("#lclear").onclick = () => { this.logs = []; this.renderLog(); };
    el.querySelector("#dbclear").onclick = async () => {
      if (!confirm("Svuotare l'archivio di LDMHA?\nI file non vengono toccati: si perdono solo l'elenco salvato e i riconoscimenti non applicati.")) return;
      try { await this.ws({ type: "ldmha/forget" }); this.log("Archivio svuotato"); } catch (e) { alert(e.message || e); }
      this.s.info = await this.ws({ type: "ldmha/info" }).catch(() => this.s.info);
      this.renderLog();
    };
  }

  // ================================================================ cronologia
  async renderHistory() {
    const el = this.shadowRoot.getElementById("hist");
    el.innerHTML = `<div class="note">Carico…</div>`;
    let h;
    try { h = (await this.ws({ type: "ldmha/history" })).history; } catch (e) { el.innerHTML = `<div class="warn">${esc(e.message || e)}</div>`; return; }
    if (!h.length) { el.innerHTML = `<div class="note">Nessuna modifica.</div>`; return; }
    el.innerHTML = `<div class="ht">${h.map((e) => `<div class="hr ${e.undone ? "dim" : ""}">
        <span class="mut">${new Date(e.t * 1000).toLocaleString()}</span>
        <span><b class="st ${e.kind === "trash" ? "ko" : "done"}">${e.kind === "trash" ? "🗑" : "★"}</b></span>
        <span title="${esc(e.old)}">${esc(base(e.old))}${e.old !== e.new ? ` <span class="mut">→ ${esc(e.kind === "trash" ? "cestino" : base(e.new))}</span>` : ""}
          ${e.tags && (e.tags.artist || e.tags.title) ? `<br><small class="mut">${esc([e.tags.artist, e.tags.title].filter(Boolean).join(" — "))}</small>` : ""}</span>
        <span>${e.undone ? `<span class="mut">annullato</span>` : `<button data-u="${esc(e.id)}">Annulla</button>`}</span></div>`).join("")}</div>`;
    el.querySelectorAll("[data-u]").forEach((b) => {
      b.onclick = async () => {
        b.disabled = true;
        try { await this.ws({ type: "ldmha/undo", id: b.dataset.u }); } catch (e) { alert(e.message || e); }
        this.renderHistory();
      };
    });
  }
}

const CSS = `
:host { display: flex; flex-direction: column; height: 100%; background: var(--primary-background-color); color: var(--primary-text-color);
  font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif); font-size: 13px; outline: none; }
[hidden] { display: none !important; }
header { display: flex; align-items: center; gap: 12px; height: 56px; padding: 0 12px; flex: none; background: var(--app-header-background-color, var(--primary-color)); color: var(--app-header-text-color, #fff); }
header .logo { width: 36px; height: 36px; border-radius: 8px; flex: none; }
header h1 { font-size: 20px; font-weight: 400; margin: 0; flex: 1; }
header button { background: none; border: none; color: inherit; font-size: 14px; padding: 8px 12px; border-radius: 8px; cursor: pointer; }
header .menu { font-size: 22px; } header nav button.on { background: rgba(255,255,255,.2); }
#top { flex: none; padding: 8px 10px 0; }
.bar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-bottom: 6px; }
.bar.acts { padding-bottom: 6px; border-bottom: 1px solid var(--divider-color); }
.crumbs { display: flex; flex-wrap: wrap; align-items: center; flex: 1; min-width: 0; }
.crumbs i { opacity: .5; font-style: normal; margin: 0 2px; }
.crumbs button { background: none; border: none; color: var(--primary-color); padding: 2px 3px; cursor: pointer; font-size: 13px; }
button, select, input { font: inherit; color: var(--primary-text-color); background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 6px; padding: 4px 8px; }
button { cursor: pointer; white-space: nowrap; } button:disabled { opacity: .45; cursor: default; }
button.pri { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
#q { flex: 1; min-width: 180px; }
.chk { display: flex; align-items: center; gap: 4px; }
.pat { display: flex; align-items: center; gap: 4px; flex: 1; min-width: 240px; }
.pat input { flex: 1; font-family: monospace; }
.sep { width: 1px; height: 20px; background: var(--divider-color); margin: 0 4px; }
.grow { flex: 1; text-align: right; }
progress { width: 120px; }
.hint, .mut { color: var(--secondary-text-color); } .small { font-size: 12px; }
.note { padding: 24px; text-align: center; color: var(--secondary-text-color); }
.warn { background: rgba(255,152,0,.15); border-left: 4px solid var(--warning-color, #ff9800); padding: 6px 10px; border-radius: 6px; margin: 4px 0; }
.warn a { color: var(--primary-color); }
#main { flex: 1; min-height: 0; display: flex; }
#tree { width: 250px; flex: none; overflow: auto; border-right: 1px solid var(--divider-color); padding: 4px 0; }
.tn { display: flex; align-items: center; height: 26px; }
.tn.on { background: rgba(3,169,244,.15); }
.tn button { border: none; background: none; padding: 0; height: 26px; }
.tw { width: 18px; flex: none; color: var(--secondary-text-color); }
.tl { flex: 1; min-width: 0; text-align: left; overflow: hidden; text-overflow: ellipsis; padding: 0 4px !important; }
.tn.on .tl { font-weight: 600; color: var(--primary-color); }
#list { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.th { display: grid; position: sticky; top: 0; z-index: 2; min-width: 720px; align-items: center; height: 30px; border-bottom: 1px solid var(--divider-color); background: var(--secondary-background-color, #eee); }
.th span { padding-left: 6px; }
.th button { border: none; background: none; text-align: left; font-weight: 600; padding: 0 6px; height: 30px; overflow: hidden; text-overflow: ellipsis; }
.th button.on { color: var(--primary-color); }
.vp { flex: 1; min-height: 0; overflow: auto; position: relative; }
.sp { position: relative; min-width: 720px; }
.tr { position: absolute; left: 0; right: 0; height: ${ROW}px; display: grid; align-items: center; border-bottom: 1px solid var(--divider-color); cursor: pointer; }
.tr:hover { background: rgba(127,127,127,.08); }
.tr.sel { background: rgba(3,169,244,.10); }
.tr.open { background: rgba(3,169,244,.22); }
.tr.dim { opacity: .55; }
.tr > span { padding: 0 6px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tr .num { text-align: right; color: var(--secondary-text-color); }
.c-st { display: flex; align-items: center; gap: 2px; padding: 0 2px !important; }
.c-st input { margin: 0; }
.st { display: inline-block; width: 16px; text-align: center; font-weight: 700; color: var(--secondary-text-color); }
.st.ok { color: #2e7d32; } .st.mid { color: #ef6c00; } .st.ko { color: var(--error-color, #c62828); } .st.run { color: #1976d2; } .st.done { color: #2e7d32; }
.prop { color: #2e7d32; }
.dup { font-size: 10px; background: rgba(255,152,0,.35); color: var(--primary-text-color); padding: 1px 5px; border-radius: 8px; font-weight: 600; }
#det { width: 380px; flex: none; overflow: auto; border-left: 1px solid var(--divider-color); padding: 10px; box-sizing: border-box; background: var(--card-background-color); }
.dh { display: flex; align-items: flex-start; gap: 8px; } .dh b { flex: 1; word-break: break-all; font-size: 14px; }
.ic { padding: 4px 8px; }
.kv { width: 100%; border-collapse: collapse; margin: 8px 0; } .kv td { padding: 2px 4px; border-bottom: 1px solid var(--divider-color); vertical-align: top; }
.kv td:first-child { color: var(--secondary-text-color); width: 90px; }
.lab { font-weight: 600; margin: 10px 0 4px; }
.cands { display: flex; flex-direction: column; gap: 3px; max-height: 260px; overflow: auto; }
.cands button { display: flex; gap: 8px; align-items: center; text-align: left; white-space: normal; padding: 4px 6px; }
.cands button.on { border-color: var(--primary-color); background: rgba(3,169,244,.12); }
.sc { flex: none; width: 38px; font-weight: 700; color: #ef6c00; } .sc.ok { color: #2e7d32; }
.ed { display: grid; grid-template-columns: 1fr 1fr; gap: 4px 6px; }
.ed label { display: flex; flex-direction: column; font-size: 11px; color: var(--secondary-text-color); }
.ed .f-title, .ed .f-artist, .ed .f-album { grid-column: span 2; }
.ed input { width: 100%; box-sizing: border-box; }
.nn { margin-top: 8px; word-break: break-all; } .nn code { background: var(--secondary-background-color); padding: 1px 4px; border-radius: 4px; }
.ko { color: var(--error-color, #c62828); }
.srch { display: flex; gap: 6px; margin-top: 8px; } .srch input { flex: 1; }
.dacts { display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0 8px; }
#hist, #logv { flex: 1; overflow: auto; padding: 10px; }
.lg { display: flex; flex-direction: column; font-family: monospace; font-size: 12px; }
.lr { display: grid; grid-template-columns: 80px 1fr; gap: 8px; padding: 3px 4px; border-bottom: 1px solid var(--divider-color); }
.lr.ko { color: var(--error-color, #c62828); } .lr.ok { color: #2e7d32; } .lr.run { color: #1976d2; }
.dlog { margin-top: 10px; font-size: 12px; } .dlog summary { cursor: pointer; font-weight: 600; }
.dlog ol { margin: 6px 0; padding-left: 20px; } .dlog li { margin: 2px 0; }
.cache { padding: 6px 10px; background: rgba(76,175,80,.12); border-radius: 6px; margin: 4px 6px; }
.cache button { margin: 0 4px; }
.scan { display: flex; align-items: center; gap: 10px; padding: 6px 10px; background: rgba(3,169,244,.12); border-radius: 6px; margin: 4px 6px; }
.scan span:nth-child(2) { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.spin { width: 14px; height: 14px; border: 2px solid rgba(3,169,244,.3); border-top-color: var(--primary-color); border-radius: 50%; animation: sp .8s linear infinite; flex: none; }
@keyframes sp { to { transform: rotate(360deg); } }
.ht { display: flex; flex-direction: column; }
.hr { display: grid; grid-template-columns: 150px 24px 1fr auto; gap: 8px; align-items: center; padding: 5px 4px; border-bottom: 1px solid var(--divider-color); }
.hr.dim { opacity: .55; }
@media (max-width: 900px) {
  #tree { position: absolute; z-index: 3; top: 0; bottom: 0; left: 0; background: var(--card-background-color); box-shadow: 2px 0 8px rgba(0,0,0,.3); }
  #main { position: relative; }
  #det { position: absolute; z-index: 4; top: 0; bottom: 0; right: 0; width: min(380px, 100%); box-shadow: -2px 0 8px rgba(0,0,0,.3); }
  .pat { min-width: 100%; }
}
`;

customElements.define("ldmha-panel", LdmhaPanel);
