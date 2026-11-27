// Pannello "LDMHA" per Home Assistant (solo amministratori): mette in ordine la musica. Riconosce i brani
// dall'audio (AcoustID + MusicBrainz) o dal nome, propone tag e nome nuovo, li applica, sposta i doppioni nel
// cestino; ogni modifica resta nella cronologia e si può annullare.

// deve coincidere con VERSION in const.py: se il backend ne riporta un'altra manca un riavvio
const PANEL_VERSION = "0.1.0";
const STORE_KEY = "ldmha-panel";
const SAFE = 0.9; // sopra questa sicurezza (impronta audio) il brano si può sistemare in automatico
const PATTERNS = [
  ["{artist} - {title}", "Artista - Titolo"],
  ["{track} - {title}", "Numero - Titolo"],
  ["{track} - {artist} - {title}", "Numero - Artista - Titolo"],
  ["{artist}/{album}/{track} - {title}", "Artista / Album / Numero - Titolo"],
];
const FIELDS = [["title", "Titolo"], ["artist", "Artista"], ["album", "Album"], ["albumartist", "Artista album"],
  ["date", "Anno"], ["tracknumber", "N."]];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const size = (b) => b > 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.round(b / 1024)} kB`;
const pct = (s) => `${Math.round((s || 0) * 100)}%`;
const load = () => { try { return JSON.parse(localStorage.getItem(STORE_KEY) || "{}"); } catch (e) { return {}; } };
const save = (o) => { try { localStorage.setItem(STORE_KEY, JSON.stringify({ ...load(), ...o })); } catch (e) { /* niente */ } };

class LdmhaPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    const st = load();
    this.s = {
      tab: "brani", info: null, path: st.path || "", recursive: !!st.recursive, pattern: st.pattern || PATTERNS[0][0],
      filter: "tutti", list: null, loading: false, running: false, error: "",
    };
    this.rows = new Map(); // percorso -> stato della riga
    this.els = new Map(); // percorso -> elemento della riga
  }

  set hass(h) {
    const first = !this._hass;
    this._hass = h;
    if (first) this.start();
  }
  get hass() { return this._hass; }
  set narrow(v) { this._narrow = v; this.shadowRoot.querySelector(".menu")?.toggleAttribute("hidden", !v); }

  ws(msg) { return this._hass.callWS(msg); }

  async start() {
    this.renderShell();
    try {
      this.s.info = await this.ws({ type: "ldmha/info" });
    } catch (e) {
      this.s.error = e.message || String(e);
    }
    const roots = this.s.info?.roots || [];
    if (!roots.some((r) => this.s.path === r || this.s.path.startsWith(r + "/"))) this.s.path = roots[0] || "";
    this.renderTop();
    if (this.s.path) this.openDir(this.s.path);
  }

  // ------------------------------------------------------------ struttura
  renderShell() {
    this.shadowRoot.innerHTML = `<style>${CSS}</style>
      <header><button class="menu" title="Menu" ${this._narrow ? "" : "hidden"}>☰</button><h1>LDMHA</h1>
        <nav><button data-tab="brani" class="on">Brani</button><button data-tab="cronologia">Cronologia</button></nav></header>
      <main><section id="top"></section><section id="body"></section></main>`;
    const r = this.shadowRoot;
    r.querySelector(".menu").onclick = () => this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true }));
    r.querySelectorAll("[data-tab]").forEach((b) => { b.onclick = () => this.setTab(b.dataset.tab); });
  }

  setTab(t) {
    this.s.tab = t;
    this.shadowRoot.querySelectorAll("[data-tab]").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
    if (t === "cronologia") this.renderHistory(); else { this.renderTop(); this.renderList(); }
  }

  renderTop() {
    const top = this.shadowRoot.getElementById("top");
    if (this.s.tab !== "brani") { top.innerHTML = ""; return; }
    const i = this.s.info;
    if (this.s.error && !i) { top.innerHTML = `<div class="warn">${esc(this.s.error)}</div>`; return; }
    if (!i) { top.innerHTML = `<div class="note">Carico…</div>`; return; }
    const warn = [];
    if (i.version !== PANEL_VERSION) warn.push(`Pannello ${PANEL_VERSION}, integrazione ${i.version}: riavvia Home Assistant e ricarica la pagina.`);
    if (!i.acoustid) warn.push(`Manca la chiave AcoustID: i brani si cercano solo per nome e tag. Creala gratis su <a href="https://acoustid.org/new-application" target="_blank" rel="noopener">acoustid.org</a> e inseriscila nelle opzioni dell'integrazione LDMHA.`);
    if (/non disponibile/.test(i.fpcalc)) warn.push(`Impronta audio ${esc(i.fpcalc)}.`);
    if (!i.roots.length) warn.push(`Nessuna delle cartelle configurate esiste (${esc(i.configured.join(", "))}). Cambiale nelle opzioni dell'integrazione.`);
    const p = this.s.path;
    const root = i.roots.filter((r) => p === r || p.startsWith(r + "/")).sort((a, b) => b.length - a.length)[0] || "";
    const crumbs = [];
    if (root) {
      crumbs.push(`<button data-go="${esc(root)}">${esc(root)}</button>`);
      let acc = root;
      for (const part of p.slice(root.length).split("/").filter(Boolean)) {
        acc += "/" + part;
        crumbs.push(`<button data-go="${esc(acc)}">${esc(part)}</button>`);
      }
    }
    top.innerHTML = `${warn.map((w) => `<div class="warn">${w}</div>`).join("")}
      <div class="bar">
        <select id="root" title="Cartella principale">${i.roots.map((r) => `<option ${r === root ? "selected" : ""}>${esc(r)}</option>`).join("")}</select>
        <span class="crumbs">${crumbs.join("<i>/</i>")}</span>
        <label class="chk"><input type="checkbox" id="rec" ${this.s.recursive ? "checked" : ""}> Sottocartelle</label>
        <button id="reload" title="Aggiorna l'elenco">⟳</button>
      </div>
      <div class="dirs" id="dirs"></div>
      <div class="bar">
        <label class="pat">Nome nuovo <input id="pattern" value="${esc(this.s.pattern)}" spellcheck="false"></label>
        <select id="preset" title="Schemi pronti"><option value="">Schemi…</option>${PATTERNS.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("")}</select>
        <span class="hint">{artist} {title} {album} {albumartist} {year} {track} · con / si creano sottocartelle · vuoto = solo i tag</span>
      </div>
      <div class="bar">
        <button id="all" class="pri">${this.s.running ? "■ Ferma" : "Riconosci tutti"}</button>
        <button id="safe" title="Applica i brani riconosciuti dall'audio con sicurezza almeno ${pct(SAFE)}">Applica i sicuri (≥ ${pct(SAFE)})</button>
        <select id="filter">${[["tutti", "Tutti"], ["dafare", "Da riconoscere"], ["proposte", "Con proposta"], ["doppioni", "Doppioni"], ["nontrovati", "Non trovati"], ["fatti", "Sistemati"]]
          .map(([v, t]) => `<option value="${v}" ${v === this.s.filter ? "selected" : ""}>${t}</option>`).join("")}</select>
        <span id="count" class="hint"></span>
      </div>`;
    const $ = (id) => top.querySelector("#" + id);
    $("root").onchange = (e) => this.openDir(e.target.value);
    top.querySelectorAll("[data-go]").forEach((b) => { b.onclick = () => this.openDir(b.dataset.go); });
    $("rec").onchange = (e) => { this.s.recursive = e.target.checked; save({ recursive: this.s.recursive }); this.openDir(this.s.path); };
    $("reload").onclick = () => this.openDir(this.s.path);
    $("pattern").onchange = (e) => this.setPattern(e.target.value);
    $("preset").onchange = (e) => { if (e.target.value) { $("pattern").value = e.target.value; this.setPattern(e.target.value); } e.target.value = ""; };
    $("all").onclick = () => (this.s.running ? (this.s.running = false, this.renderTop()) : this.identifyAll());
    $("safe").onclick = () => this.applySafe();
    $("filter").onchange = (e) => { this.s.filter = e.target.value; this.renderList(); };
    this.renderDirs();
    this.updateCount();
  }

  renderDirs() {
    const el = this.shadowRoot.getElementById("dirs");
    if (!el) return;
    const l = this.s.list;
    const i = this.s.info;
    const up = l && !i.roots.includes(l.path) ? l.path.replace(/\/[^/]+$/, "") : "";
    el.innerHTML = (up ? `<button data-dir="${esc(up)}">↑ Su</button>` : "")
      + (l?.dirs || []).map((d) => `<button data-dir="${esc(l.path + "/" + d)}">📁 ${esc(d)}</button>`).join("");
    el.querySelectorAll("[data-dir]").forEach((b) => { b.onclick = () => this.openDir(b.dataset.dir); });
  }

  setPattern(v) {
    this.s.pattern = v.trim();
    save({ pattern: this.s.pattern });
    this.preview([...this.rows.values()].filter((r) => r.edit));
  }

  // ------------------------------------------------------------ elenco
  async openDir(path) {
    if (this.s.running) this.s.running = false;
    this.s.path = path;
    save({ path });
    this.s.loading = true;
    this.s.list = null;
    this.rows.clear();
    this.renderTop();
    this.renderList();
    try {
      this.s.list = await this.ws({ type: "ldmha/list", path, recursive: this.s.recursive });
      for (const f of this.s.list.files) this.rows.set(f.path, { file: f, status: "nuovo" });
    } catch (e) {
      this.s.list = { path, dirs: [], files: [], error: e.message || String(e) };
    }
    this.s.loading = false;
    this.renderTop();
    this.renderList();
  }

  visible(r) {
    switch (this.s.filter) {
      case "dafare": return r.status === "nuovo";
      case "proposte": return !!r.edit && r.status !== "fatto";
      case "doppioni": return !!r.dup;
      case "nontrovati": return r.status === "nontrovato" || r.status === "errore";
      case "fatti": return r.status === "fatto";
      default: return r.status !== "cestino";
    }
  }

  renderList() {
    const body = this.shadowRoot.getElementById("body");
    if (this.s.tab !== "brani") return;
    this.els.clear();
    const l = this.s.list;
    if (this.s.loading || !l) { body.innerHTML = `<div class="note">${this.s.path ? "Leggo la cartella…" : ""}</div>`; return; }
    if (l.error) { body.innerHTML = `<div class="warn">${esc(l.error)}</div>`; return; }
    const rows = [...this.rows.values()].filter((r) => this.visible(r));
    body.innerHTML = (l.truncated ? `<div class="warn">Troppi brani: ne mostro solo i primi ${l.files.length}. Apri una sottocartella.</div>` : "")
      + (rows.length ? "" : `<div class="note">${l.files.length ? "Nessun brano con questo filtro." : "Nessun brano in questa cartella."}</div>`);
    const frag = document.createDocumentFragment();
    for (const r of rows) {
      const el = document.createElement("div");
      el.className = "row";
      this.els.set(r.file.path, el);
      this.fillRow(r, el);
      frag.appendChild(el);
    }
    body.appendChild(frag);
    this.updateCount();
  }

  updateCount() {
    const c = this.shadowRoot.getElementById("count");
    if (!c) return;
    const all = [...this.rows.values()];
    const n = (f) => all.filter(f).length;
    c.textContent = all.length ? `${all.length} brani · ${n((r) => r.status === "fatto")} sistemati · ${n((r) => r.edit && r.status !== "fatto")} con proposta · ${n((r) => r.dup)} doppioni` : "";
  }

  refreshRow(r) {
    const el = this.els.get(r.file.path);
    if (el) this.fillRow(r, el);
    this.updateCount();
  }

  fillRow(r, el) {
    const f = r.file, t = f.tags || {};
    const cur = [t.artist, t.title].filter(Boolean).join(" — ") || "senza tag";
    const badge = {
      nuovo: "", lavoro: `<b class="b run">riconosco…</b>`, trovato: `<b class="b ok">${pct(r.cand?.score)} ${r.cand?.source === "impronta" ? "audio" : "nome"}</b>`,
      nontrovato: `<b class="b ko">non trovato</b>`, errore: `<b class="b ko">errore</b>`, fatto: `<b class="b done">sistemato</b>`,
      cestino: `<b class="b ko">nel cestino</b>`, salvo: `<b class="b run">salvo…</b>`,
    }[r.status] || "";
    const dup = r.dup ? `<b class="b dup" title="Stesso brano di: ${esc(r.dup)}">doppione</b>` : "";
    let html = `<div class="h"><span class="n" title="${esc(f.path)}">${esc(f.name)}</span>${badge}${dup}
      <span class="m">${esc(cur)}${t.album ? ` · ${esc(t.album)}` : ""} · ${size(f.size)}${t._length ? ` · ${Math.floor(t._length / 60)}:${String(t._length % 60).padStart(2, "0")}` : ""}</span></div>`;
    if (r.note) html += `<div class="hint">${esc(r.note)}</div>`;
    if (r.err) html += `<div class="warn">${esc(r.err)}</div>`;
    if (r.cands?.length && r.status !== "fatto" && r.status !== "cestino") {
      html += `<select class="cands">${r.cands.map((c, i) => `<option value="${i}" ${c === r.cand ? "selected" : ""}>${pct(c.score)} · ${esc(c.artist)} — ${esc(c.title)}${c.album ? ` (${esc(c.album)}${c.year ? ", " + esc(c.year) : ""})` : ""}</option>`).join("")}</select>`;
    }
    if (r.edit && r.status !== "fatto" && r.status !== "cestino") {
      html += `<div class="ed">${FIELDS.map(([k, lab]) => `<label class="f-${k}">${lab}<input data-k="${k}" value="${esc(r.edit[k] || "")}"></label>`).join("")}</div>
        <div class="nn">${this.s.pattern ? (r.newName ? `→ <code>${esc(r.newName)}</code>` : r.nameErr ? `<span class="ko">${esc(r.nameErr)}</span>` : "") : "Solo i tag (nessuno schema per il nome)"}</div>`;
    }
    if (r.searching) {
      html += `<div class="srch"><input class="q" placeholder="Artista - Titolo, o testo libero" value="${esc(r.q || "")}"><button class="go">Cerca</button><button class="x">Annulla</button></div>`;
    }
    const acts = [];
    if (r.status === "fatto" || r.status === "cestino") {
      if (r.entry) acts.push(`<button data-a="undo">Annulla</button>`);
    } else {
      acts.push(`<button data-a="id" ${r.status === "lavoro" ? "disabled" : ""}>Riconosci</button>`, `<button data-a="search">Cerca…</button>`);
      if (r.edit) acts.push(`<button data-a="apply" class="pri">Applica</button>`);
      acts.push(`<button data-a="trash" title="Sposta nel cestino (${esc(".ldmha_cestino")}), si può rimettere a posto">Cestino</button>`);
    }
    html += `<div class="acts">${acts.join("")}</div>`;
    el.innerHTML = html;
    el.classList.toggle("done", r.status === "fatto" || r.status === "cestino");
    el.querySelector(".cands")?.addEventListener("change", (e) => { this.choose(r, r.cands[+e.target.value]); });
    el.querySelectorAll(".ed input").forEach((inp) => inp.addEventListener("change", () => { r.edit[inp.dataset.k] = inp.value.trim(); this.preview([r]); }));
    el.querySelectorAll("[data-a]").forEach((b) => { b.onclick = () => this.action(r, b.dataset.a); });
    if (r.searching) {
      const q = el.querySelector(".q");
      const go = () => this.search(r, q.value);
      el.querySelector(".go").onclick = go;
      q.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
      el.querySelector(".x").onclick = () => { r.searching = false; this.refreshRow(r); };
      requestAnimationFrame(() => q.focus());
    }
  }

  // ------------------------------------------------------------ azioni
  action(r, a) {
    if (a === "id") this.identify(r);
    else if (a === "search") {
      const t = r.file.tags || {};
      r.searching = true;
      r.q = r.q || [t.artist, t.title].filter(Boolean).join(" - ");
      this.refreshRow(r);
    } else if (a === "apply") this.apply(r);
    else if (a === "trash") this.trash(r);
    else if (a === "undo") this.undo(r);
  }

  choose(r, c) {
    r.cand = c;
    if (!c) { r.edit = null; this.refreshRow(r); return; }
    const t = r.file.tags || {};
    r.edit = {
      title: c.title || t.title || "", artist: c.artist || t.artist || "", album: c.album || t.album || "",
      albumartist: c.albumartist || t.albumartist || "", date: c.year || t.date || "", tracknumber: c.track || t.tracknumber || "",
      musicbrainz_trackid: c.mbid || "",
    };
    this.markDups();
    this.preview([r]);
  }

  async identify(r) {
    r.status = "lavoro"; r.err = ""; r.note = "";
    this.refreshRow(r);
    try {
      const res = await this.ws({ type: "ldmha/identify", path: r.file.path });
      r.cands = res.candidates || [];
      r.note = res.note || "";
      r.status = r.cands.length ? "trovato" : "nontrovato";
      if (r.cands.length) this.choose(r, r.cands[0]); else { r.edit = null; this.refreshRow(r); }
    } catch (e) {
      r.status = "errore"; r.err = e.message || String(e);
      this.refreshRow(r);
    }
  }

  async search(r, text) {
    text = (text || "").trim();
    if (!text) return;
    r.q = text;
    r.status = "lavoro";
    this.refreshRow(r);
    const parts = text.split(/\s+[-–—]\s+/);
    const msg = parts.length >= 2 ? { artist: parts[0], title: parts.slice(1).join(" - ") } : { query: text };
    try {
      const res = await this.ws({ type: "ldmha/search", path: r.file.path, ...msg });
      r.cands = res.candidates || [];
      r.searching = false;
      r.note = r.cands.length ? "" : "Nessun risultato su MusicBrainz";
      r.status = r.cands.length ? "trovato" : "nontrovato";
      if (r.cands.length) this.choose(r, r.cands[0]); else this.refreshRow(r);
    } catch (e) {
      r.status = "errore"; r.err = e.message || String(e);
      this.refreshRow(r);
    }
  }

  async preview(rows) {
    rows = rows.filter((r) => r.edit);
    if (!rows.length) return;
    if (!this.s.pattern) { rows.forEach((r) => { r.newName = ""; r.nameErr = ""; this.refreshRow(r); }); return; }
    try {
      const res = await this.ws({ type: "ldmha/preview", pattern: this.s.pattern, items: rows.map((r) => ({ path: r.file.path, tags: { ...this.tagsFor(r) } })) });
      for (const it of res.items) {
        const r = this.rows.get(it.path);
        if (!r) continue;
        r.newName = it.name || ""; r.nameErr = it.error || "";
        const curName = r.file.path.split("/").pop();
        if (r.newName === curName) r.newName = `${curName} (uguale)`;
      }
    } catch (e) { /* anteprima non essenziale */ }
    rows.forEach((r) => this.refreshRow(r));
  }

  tagsFor(r) {
    // ai tag nuovi si aggiungono quelli già nel file che la proposta lascia vuoti (come fa il backend)
    const t = { ...(r.file.tags || {}) };
    delete t._length;
    for (const [k, v] of Object.entries(r.edit || {})) if (v) t[k] = v;
    return t;
  }

  async apply(r) {
    if (!r.edit) return;
    r.status = "salvo"; r.err = "";
    this.refreshRow(r);
    try {
      const res = await this.ws({ type: "ldmha/apply", path: r.file.path, tags: r.edit, pattern: this.s.pattern });
      this.rows.delete(r.file.path);
      const el = this.els.get(r.file.path);
      this.els.delete(r.file.path);
      const old = r.file.path;
      r.file = { ...r.file, path: res.path, name: this.s.list.path && res.path.startsWith(this.s.list.path + "/") ? res.path.slice(this.s.list.path.length + 1) : res.path, tags: res.tags };
      r.prevPath = old;
      r.entry = res.entry;
      r.status = "fatto";
      this.rows.set(r.file.path, r);
      if (el) this.els.set(r.file.path, el);
      this.refreshRow(r);
    } catch (e) {
      r.status = "trovato"; r.err = e.message || String(e);
      this.refreshRow(r);
    }
  }

  async trash(r) {
    if (!confirm(`Spostare nel cestino «${r.file.name}»?\n(Si può rimettere a posto dalla cronologia.)`)) return;
    try {
      const res = await this.ws({ type: "ldmha/trash", path: r.file.path });
      r.entry = res.entry; r.status = "cestino"; r.err = "";
      this.markDups();
      this.refreshRow(r);
    } catch (e) {
      r.err = e.message || String(e);
      this.refreshRow(r);
    }
  }

  async undo(r) {
    if (!r.entry) return;
    try {
      const res = await this.ws({ type: "ldmha/undo", id: r.entry.id });
      this.rows.delete(r.file.path);
      const el = this.els.get(r.file.path);
      this.els.delete(r.file.path);
      const back = res.path;
      r.file = { ...r.file, path: back, name: this.s.list.path && back.startsWith(this.s.list.path + "/") ? back.slice(this.s.list.path.length + 1) : back,
        tags: r.entry.kind === "apply" ? { ...(r.entry.old_tags || {}), _length: r.file.tags?._length } : r.file.tags };
      r.entry = null; r.status = r.cands?.length ? "trovato" : "nuovo"; r.err = "";
      this.rows.set(back, r);
      if (el) this.els.set(back, el);
      this.markDups();
      this.preview([r]);
      this.refreshRow(r);
    } catch (e) {
      r.err = e.message || String(e);
      this.refreshRow(r);
    }
  }

  // stesso brano (stesso codice MusicBrainz) in più file: tutti tranne il primo sono doppioni
  markDups() {
    const seen = new Map();
    for (const r of this.rows.values()) {
      const was = r.dup;
      r.dup = "";
      const id = r.status !== "cestino" && (r.cand?.mbid || r.file.tags?.musicbrainz_trackid);
      if (id) {
        if (seen.has(id)) r.dup = seen.get(id).file.name; else seen.set(id, r);
      }
      if (was !== r.dup) this.refreshRow(r);
    }
  }

  async identifyAll() {
    this.s.running = true;
    this.renderTop();
    for (const r of [...this.rows.values()]) {
      if (!this.s.running) break;
      if (r.status !== "nuovo") continue;
      await this.identify(r);
    }
    this.s.running = false;
    this.renderTop();
  }

  async applySafe() {
    const list = [...this.rows.values()].filter((r) => r.edit && r.cand && r.cand.source === "impronta" && r.cand.score >= SAFE
      && !r.dup && ["trovato"].includes(r.status));
    if (!list.length) { alert(`Nessun brano riconosciuto dall'audio con sicurezza ≥ ${pct(SAFE)} (usa prima «Riconosci tutti»).`); return; }
    if (!confirm(`Applicare tag${this.s.pattern ? " e nome nuovo" : ""} a ${list.length} brani?\n(Ogni modifica si può annullare dalla cronologia.)`)) return;
    for (const r of list) await this.apply(r);
  }

  // ------------------------------------------------------------ cronologia
  async renderHistory() {
    this.shadowRoot.getElementById("top").innerHTML = "";
    const body = this.shadowRoot.getElementById("body");
    body.innerHTML = `<div class="note">Carico…</div>`;
    let h;
    try { h = (await this.ws({ type: "ldmha/history" })).history; } catch (e) { body.innerHTML = `<div class="warn">${esc(e.message || e)}</div>`; return; }
    if (!h.length) { body.innerHTML = `<div class="note">Nessuna modifica.</div>`; return; }
    const base = (p) => p.split("/").pop();
    body.innerHTML = h.map((e) => `<div class="row hist ${e.undone ? "done" : ""}"><div class="h">
        <span class="m">${new Date(e.t * 1000).toLocaleString()}</span>
        <b class="b ${e.kind === "trash" ? "ko" : "ok"}">${e.kind === "trash" ? "cestino" : "sistemato"}</b>${e.undone ? `<b class="b">annullato</b>` : ""}</div>
        <div class="n" title="${esc(e.old)}">${esc(base(e.old))}</div>
        ${e.old !== e.new ? `<div class="m" title="${esc(e.new)}">→ ${esc(e.kind === "trash" ? e.new : base(e.new))}</div>` : ""}
        ${e.tags && Object.keys(e.tags).length ? `<div class="m">${esc([e.tags.artist, e.tags.title].filter(Boolean).join(" — "))}</div>` : ""}
        ${e.undone ? "" : `<div class="acts"><button data-u="${esc(e.id)}">Annulla</button></div>`}</div>`).join("");
    body.querySelectorAll("[data-u]").forEach((b) => {
      b.onclick = async () => {
        b.disabled = true;
        try { await this.ws({ type: "ldmha/undo", id: b.dataset.u }); } catch (e) { alert(e.message || e); }
        this.renderHistory();
      };
    });
  }
}

const CSS = `
:host { display: block; height: 100%; background: var(--primary-background-color); color: var(--primary-text-color); font-family: var(--paper-font-body1_-_font-family, sans-serif); }
header { display: flex; align-items: center; gap: 12px; height: 56px; padding: 0 12px; background: var(--app-header-background-color, var(--primary-color)); color: var(--app-header-text-color, #fff); }
header h1 { font-size: 20px; font-weight: 400; margin: 0; flex: 1; }
header button { background: none; border: none; color: inherit; font-size: 15px; padding: 8px 12px; border-radius: 8px; cursor: pointer; }
header .menu { font-size: 22px; }
header nav button.on { background: rgba(255,255,255,.2); }
main { height: calc(100% - 56px); overflow: auto; padding: 12px; box-sizing: border-box; }
.bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 8px; }
.crumbs { display: flex; flex-wrap: wrap; align-items: center; gap: 2px; flex: 1; min-width: 0; }
.crumbs i { opacity: .5; font-style: normal; }
.crumbs button { background: none; border: none; color: var(--primary-color); padding: 4px; cursor: pointer; font-size: 14px; }
.dirs { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
button, select, input { font: inherit; font-size: 14px; color: var(--primary-text-color); background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 8px; padding: 6px 10px; }
button { cursor: pointer; } button:disabled { opacity: .5; cursor: default; }
button.pri { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
.chk { display: flex; align-items: center; gap: 4px; font-size: 14px; }
.pat { display: flex; align-items: center; gap: 6px; font-size: 14px; flex: 1; min-width: 260px; }
.pat input { flex: 1; font-family: monospace; }
.hint { font-size: 12px; color: var(--secondary-text-color); }
.note { padding: 24px; text-align: center; color: var(--secondary-text-color); }
.warn { background: rgba(255,152,0,.15); border-left: 4px solid var(--warning-color, #ff9800); padding: 8px 10px; border-radius: 6px; margin: 6px 0; font-size: 14px; }
.warn a { color: var(--primary-color); }
.row { background: var(--card-background-color); border-radius: 10px; padding: 10px 12px; margin-bottom: 8px; box-shadow: var(--ha-card-box-shadow, 0 1px 3px rgba(0,0,0,.15)); }
.row.done { opacity: .7; }
.h { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px; }
.n { font-weight: 500; word-break: break-all; }
.m { font-size: 13px; color: var(--secondary-text-color); word-break: break-word; }
.b { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 10px; background: var(--divider-color); white-space: nowrap; }
.b.ok { background: rgba(76,175,80,.25); } .b.ko { background: rgba(244,67,54,.25); } .b.run { background: rgba(33,150,243,.25); }
.b.done { background: rgba(76,175,80,.5); } .b.dup { background: rgba(255,152,0,.4); }
.cands { width: 100%; margin-top: 8px; }
.ed { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 6px; margin-top: 8px; }
.ed label { display: flex; flex-direction: column; font-size: 11px; color: var(--secondary-text-color); gap: 2px; }
.ed .f-title, .ed .f-artist { grid-column: span 2; }
.ed input { width: 100%; box-sizing: border-box; }
.nn { font-size: 13px; margin-top: 6px; word-break: break-all; }
.nn code { background: var(--secondary-background-color); padding: 1px 4px; border-radius: 4px; }
.ko { color: var(--error-color, #f44336); }
.srch { display: flex; gap: 6px; margin-top: 8px; } .srch input { flex: 1; }
.acts { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; justify-content: flex-end; }
`;

customElements.define("ldmha-panel", LdmhaPanel);
