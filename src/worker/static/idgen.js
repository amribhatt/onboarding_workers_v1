"use strict";
/* The ID generation logic of the onboarding console: one decision per sponsor and record type, asked first on the
   plan. A dropdown of three ways (source, sequential, custom); the custom one shows a field for the pattern. Every
   number and ID on this card comes from /api/console/idgen (code validates, previews and issues; nothing here
   invents an ID).  Entry point for the shell:  window.IDGEN.render(el, {job, sponsor, reviewer}) */
(function () {
  const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const held = {};   // per "sponsor|type": the unsaved form {mode, prefix, width, start, pattern, reason, preview}
  const seen = {};   // per "sponsor|type": the last server view
  let timer = null;

  async function api(path, opts) {
    const r = await fetch(path, opts);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || body.detail || r.statusText);
    return body;
  }
  const json = (body, who) => ({ method: "POST", headers: { "Content-Type": "application/json", ...(who || {}) }, body: JSON.stringify(body) });
  const note = (m, bad) => (typeof toast === "function" ? toast(m, bad) : null);

  const keyOf = (o) => `${o.sponsor}|${o.type}`;
  const form = (v) => held[`${v.sponsor}|${v.record_type}`] || (held[`${v.sponsor}|${v.record_type}`] = {
    mode: (!v.decided && v.suggest) ? "sequential" : v.scheme.mode, ...(!v.decided && v.suggest ? { prefix: v.suggest.prefix, width: v.suggest.width, start: v.suggest.start } : { prefix: v.scheme.prefix || "", width: v.scheme.width || 0, start: v.scheme.start || 1 }),
    pattern: v.scheme.pattern || "", column: v.source_column || "", name_ok: false, reason: "", preview: v.preview || null });
  const body = (v, f) => ({ sponsor: v.sponsor, record_type: v.record_type, job: v.job,
    column: f.mode === "source" ? f.column : "", name_ok: !!f.name_ok,
    scheme: { mode: f.mode, prefix: f.prefix, width: f.width, start: f.start, pattern: f.pattern } });

  const confirmed = (f) => `Source ID confirmed: column “${f.column}”`;

  function previewHtml(v, f) {
    const p = f.preview;
    if (!p) return "";
    const errs = (p.errors || []).map((e) => esc(e.message)).join(" · ");
    const sample = (p.ids || []).slice(0, 3).map((x) => esc(x.id)).join(", ");
    const names = (p.errors || []).some((e) => /reads as names/.test(e.message)) || f.name_ok;
    const d = v.suggest || v.default || {};
    const edited = f.mode === "custom" || String(f.prefix) !== String(d.prefix ?? "") || Number(f.width) !== Number(d.width ?? 0) || (!v.suggest && Number(f.start) !== 1);
    const label = edited ? "CUSTOMISED" : v.suggest ? "MATCHES YOUR EXISTING IDS" : "SYSTEM SUGGESTION";
    const tag = f.mode === "source" ? "" : `<span class="lab ${edited ? "j" : ""}">${label}</span> `;
    const seen = f.mode === "source" || !v.suggest ? "" : `<div class="faint" style="font-size:12.5px">${edited ? "Differs from your existing IDs, which end at" : "Continues your existing IDs, which end at"} <span class="mono">${esc(v.suggest.last)}</span></div>`;
    return `${seen}${sample ? `<div class="faint" style="font-size:12.5px">${tag}e.g. <span class="mono">${sample}</span>${p.rows > 3 ? ` … (${p.rows} rows)` : ""}</div>` : ""}
      ${(p.notes || []).length ? `<div class="faint" style="font-size:12.5px">${(p.notes || []).map(esc).join(" · ")}</div>` : ""}
      ${errs ? `<div class="note n-err" role="alert" data-idg-errors style="margin:0;padding:6px 10px;font-size:12.5px">${errs}</div>` : ""}
      ${f.mode === "source" && names ? `<label class="row" style="gap:6px;font-size:12.5px"><input type="checkbox" data-idg-nameok ${f.name_ok ? "checked" : ""}> These are names and I mean to use them as IDs (add a reason)</label>` : ""}`;
  }

  function fieldsHtml(v, f) {
    const inp = (k, label, type, w, extra = "") => `<label class="f" style="font-size:12px">${label}<input type="${type}" data-idg="${k}" value="${esc(f[k])}" style="width:${w}" autocomplete="off" ${extra}></label>`;
    if (f.mode === "sequential") {
      return `<div class="row" style="gap:10px;align-items:end">${inp("prefix", "Prefix", "text", "110px", `maxlength="${v.pattern_max}"`)}${inp("width", "Digits", "number", "70px", 'min="0"')}${inp("start", "Start at", "number", "80px", 'min="1"')}<button class="lnk" data-idg-mode="custom" style="font-size:12px">custom format…</button></div>`;
    }
    if (f.mode === "custom") {
      const tokens = v.tokens.map((t) => `{${esc(t)}${t === "SEQ" ? ":n" : ""}}`).join(" ");
      return `<div class="row" style="gap:10px;align-items:end;flex-wrap:wrap">${inp("pattern", "Format", "text", "220px", `maxlength="${v.pattern_max}" spellcheck="false" placeholder="{PREFIX}{SEQ:4}" title="Tokens: ${tokens}"`)}${inp("prefix", "Prefix", "text", "90px", `maxlength="${v.pattern_max}"`)}${inp("start", "Start at", "number", "80px", 'min="1"')}<button class="lnk" data-idg-mode="sequential" style="font-size:12px">simple numbering</button></div>
        <div class="faint" style="font-size:12px">Tokens: ${tokens}</div>`;
    }
    const src = v.modes.find((m) => m.mode === "source");
    if (!src.available) return `<div class="faint" style="font-size:12.5px">${esc(src.reason)}</div>`;
    return `<div style="font-size:12.5px">ID is in column <b>${esc(f.column || "(none chosen)")}</b>, used exactly as supplied.</div>`;
  }

  /* the card on its own (a type whose ID is not a row of the plan) carries a heading; inside a plan row it does not */
  const framed = (el, v) => (el.dataset && el.dataset.idgTitle ? `<div class="row" style="gap:8px"><b>${esc(el.dataset.idgTitle)}</b><span class="faint">how each new ID is made</span></div>` : "") + html(v);
  function html(v) {
    const f = form(v);
    return `<div data-idg-card style="display:grid;gap:8px;margin:8px 0 4px">
      ${v.rule ? `<div class="faint" style="font-size:12.5px">${esc(v.rule)}</div>` : ""}
      ${fieldsHtml(v, f)}
      <div data-idg-preview style="display:grid;gap:6px">${previewHtml(v, f)}</div>
      <div class="row" style="align-items:end;flex-wrap:wrap;gap:8px">
        ${typeof reviewer === "function" && !reviewer() ? '<label class="f" style="font-size:12px">Deciding as<input type="text" data-idg-who placeholder="your name" autocomplete="name" aria-label="Your name" style="width:130px"></label>' : ""}
        <label class="f" style="flex:1;min-width:180px;font-size:12px">Reason<input type="text" data-idg="reason" value="${esc(f.reason)}" autocomplete="off" placeholder="recorded with the decision"></label>
        <button class="btn pri sm" data-idg-save>${f.mode === "source" ? "Confirm" : "Save"}</button></div>
      <div class="faint" style="font-size:12px">Applies to every ${esc(v.sponsor)} fund${v.decided ? ` · decided by ${esc(v.by)}: ${esc(v.reason)}` : ""}</div>
    </div>`;
  }

  async function refreshPreview(el, v) {
    const f = form(v);
    try { f.preview = await api("/api/console/idgen/preview", json(body(v, f))); } catch (e) { f.preview = { ids: [], errors: [{ message: e.message }], notes: [] }; }
    const host = el.querySelector("[data-idg-preview]");
    if (host) { host.innerHTML = previewHtml(v, f); bindOk(el, v); }
  }

  function bindOk(el, v) {
    const f = form(v), ok = el.querySelector("[data-idg-nameok]");
    if (ok) ok.addEventListener("change", () => {
      f.name_ok = ok.checked;
      if (ok.checked && f.reason.startsWith("Source ID confirmed")) f.reason = "";
      const why = el.querySelector('[data-idg="reason"]');
      if (why) why.value = f.reason;
      refreshPreview(el, v);
    });
  }

  function wire(el, v) {
    const f = form(v);
    el.querySelectorAll("[data-idg-mode]").forEach((b) => b.addEventListener("click", () => {
      f.mode = b.dataset.idgMode; f.preview = null;
      el.innerHTML = framed(el, v); wire(el, v); refreshPreview(el, v);
    }));
    el.querySelectorAll("[data-idg]").forEach((inp) => {
      const k = inp.dataset.idg;
      inp.addEventListener(k === "mode" ? "change" : "input", () => {
        f[k] = inp.value;
        if (k === "mode") { el.innerHTML = framed(el, v); wire(el, v); refreshPreview(el, v); return; }
        if (k === "reason") return;
        clearTimeout(timer);
        timer = setTimeout(() => refreshPreview(el, v), 150);
      });
    });
    bindOk(el, v);
    const nm = el.querySelector("[data-idg-who]");   // this card may be the only place on the tab that can name the person
    if (nm) nm.addEventListener("input", () => { if (typeof reviewerName !== "undefined") { reviewerName = nm.value; store.set("reviewer", nm.value.trim()); } });
    const save = el.querySelector("[data-idg-save]");
    if (save) save.addEventListener("click", async () => {
      const who = typeof reviewer === "function" ? reviewer() : "";
      if (!who) { note("Say who is deciding first: type your name in the Who is deciding box.", true); return; }
      if (!String(f.reason || "").trim()) { note("Add a reason: the decision is recorded with it.", true); return; }
      try {
        const got = await api("/api/console/idgen", json({ ...body(v, f), reason: f.reason }, { "X-Reviewer": who }));
        delete held[`${v.sponsor}|${v.record_type}`];
        seen[`${v.sponsor}|${v.record_type}`] = got;
        note(`ID logic saved for ${v.sponsor}`);
        if (typeof act === "function") await act("toast_close");   // the job is evaluated again with the new IDs
      } catch (e) { note(e.message, true); }
    });
  }

  async function render(el, o) {
    const q = new URLSearchParams({ sponsor: o.sponsor, record_type: o.job.type, job: o.job.id });
    const k0 = keyOf({ sponsor: o.sponsor, type: o.job.type });
    if (seen[k0]) { el.innerHTML = framed(el, seen[k0]); wire(el, seen[k0]); }   // paint what is known, then ask again
    let v;
    try { v = await api(`/api/console/idgen?${q}`); } catch (e) { el.innerHTML = ""; return; }
    if (!el.isConnected) el = document.querySelector("[data-idgen]") || el;   // the page was drawn again meanwhile
    if (v.offered === false) { el.innerHTML = ""; return; }
    seen[k0] = v;
    const f = form(v);
    const row = ((o.job.plan || {}).fields || []).find((x) => x.id_field);   // the plan's own dropdown says where the ID comes from
    if (row && !f.seeded && !v.decided) {
      f.seeded = true;
      if (row.kind !== "generated" && row.source) { f.mode = "source"; f.column = row.source; f.preview = null; f.reason = confirmed(f); }
    }
    if (!f.preview && v.preview && f.mode !== "source") f.preview = v.preview;
    el.innerHTML = framed(el, v);
    wire(el, v);
    if (!f.preview && f.mode === "source") refreshPreview(el, v);   // the preview of the way the plan already chose
  }

  /* the plan's ID dropdown chose a way to generate IDs: open the card with that way ready; saving stays here */
  function pick(sponsor, type, mode, column) {
    const v = seen[`${sponsor}|${type}`], el = document.querySelector("[data-idgen]");
    if (!v || !el) return;
    const f = form(v);
    f.mode = mode;
    if (mode === "source") { f.column = column; f.name_ok = false; f.reason = confirmed(f); }
    else if (f.reason.startsWith("Source ID confirmed")) f.reason = "";
    el.innerHTML = framed(el, v);
    wire(el, v);
    refreshPreview(el, v);
    el.scrollIntoView({ block: "center" });
  }

  const isIdField = (sponsor, type, field) => { const v = seen[`${sponsor}|${type}`]; return !!v && v.id_field === field; };
  window.IDGEN = { render, pick, isIdField };
})();
