"use strict";
/* Existing records, the entity popover and sponsor-specific settings of the onboarding console.
   Markup and class names follow docs/mockup/onboarding-console.html (existing(), the fundpop and the
   "Sponsor-specific settings" card). Every figure comes from /api/console/records/*; nothing is invented.
   Entry point for the shell:  window.RECORDS.render(view, el, {sponsor})
   view: "existing" (the page) | "fund" (entity button + popover) | "settings" (the card on the sponsor page). */
(function () {
  const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const IC = {
    check: '<path d="M5 12l5 5 9-10"/>', dot: '<circle cx="12" cy="12" r="4"/>', chev: '<path d="M6 9l6 6 6-6"/>',
    up: '<path d="M12 16V5M7 10l5-5 5 5M5 19h14"/>',
  };
  const ic = (n) => `<svg viewBox="0 0 24 24" aria-hidden="true">${IC[n] || ""}</svg>`;
  const pill = (c, t, i) => `<span class="pill p-${c}">${i ? ic(i) : ""}${esc(t)}</span>`;
  const st = new WeakMap(); // per host element: {view, opts, data, pop, error}
  const last = {}; // last data per view and sponsor, so a re-render paints at once and then refreshes

  async function api(path, opts) {
    const r = await fetch(path, opts);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || body.detail || r.statusText);
    return body;
  }
  const q = (o) => "?" + new URLSearchParams(o.sponsor ? { sponsor: o.sponsor } : {}).toString();
  const post = (body) => api("/api/console/records/act", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const note = (m) => (window.toast ? window.toast(m, true) : null);

  /* ---------- the existing-records page ---------- */
  function existingHtml(d, s) {
    const card = (t) => {
      const loaded = t.ext.ans === "file";
      return `<div class="card" style="display:grid;gap:8px"><div class="row between"><b>${esc(t.type)}</b>${pill(t.count ? "ok" : "mute", t.count + " on file", t.count ? "check" : "dot")}</div>
      <div class="faint" style="font-size:12.5px">${esc(t.text)}</div>
      <div class="uplo">${ic("up")}<div>${loaded ? `Loaded <span class="mono">${esc(t.ext.file)}</span>`
        : `Upload the Ledgerline ${esc(t.type.toLowerCase())} export (.xlsx) · <label class="lnk" style="cursor:pointer">choose it<input type="file" accept=".xlsx" data-rx-file="${esc(t.type)}" hidden></label> · <button class="lnk" data-rx-op="sample|${esc(t.type)}">use a sample</button>${t.ext.ans ? "" : ` · <button class="lnk" data-rx-op="none|${esc(t.type)}">none outside the app</button>`}`}</div></div>
      <div class="tw"><table>${t.tail.map((x) => `<tr><td class="mono">${esc(x.id)}</td><td>${esc(x.name)}</td><td class="faint">${esc(x.src)}</td></tr>`).join("")
        || '<tr><td class="faint">Nothing on file yet.</td></tr>'}</table></div></div>`;
    };
    return `<button class="lnk" style="justify-self:start" data-act="view|sponsor">‹ Back to ${esc(d.sponsor)}</button>
    <div><h1>Existing in Ledgerline — ${esc(d.sponsor)}</h1><p class="sub">Records already live in Ledgerline. IDs are used as is, never reconciled. New jobs leave matches out and use these as references.</p></div>
    ${s.error ? `<div class="note n-err" role="alert">${esc(s.error)}</div>` : ""}
    <div class="tiles">${d.types.map(card).join("")}</div>`;
  }

  /* ---------- the entity popover ---------- */
  function fundHtml(d, s) {
    const grp = (e) => e.gid ? `<span class="sub">· ${esc(e.gn || e.gid)} <span class="mono">${esc(e.gid)}</span></span>` : '<span class="faint">· standalone</span>';
    const live = (e) => e.live ? `go-live ${esc(e.live)}` : '<span style="color:var(--warn,#b45309)">go-live not set</span>';
    return `<div style="position:relative"><button class="btn" data-rx-pop="1">Entity: ${esc(d.current)} ${ic("chev")}</button>${s.pop
      ? `<div class="pop"><div class="eyebrow">All entities · ${d.total} total</div>${d.entities.map((e) => `<button class="nav" data-act="fund|${esc(e.name)}">${esc(e.name)} <span class="sub mono">${esc(e.id)}</span> <span class="sub">${live(e)}</span> ${grp(e)} ${e.static_complete ? pill("ok", "Static complete", "check") : pill("warn", e.static_done + "/5 static", "dot")}</button>`).join("")}</div>` : ""}</div>`;
  }

  /* ---------- sponsor-specific settings ---------- */
  function settingsHtml(d, s) {
    const ctr = Object.entries(d.counters).map(([t, n]) => `${esc(t)} next ${esc(n)}`).join(" · ") || "not started";
    return `<div class="card" style="display:grid;gap:10px"><h2>Sponsor-specific settings</h2><p class="sub">Override the all-sponsor standards for every fund of this sponsor.</p>
    ${s.error ? `<div class="note n-err" role="alert">${esc(s.error)}</div>` : ""}
    <div class="row" style="align-items:end"><label class="f" style="flex:1;min-width:140px">Go-live date<input id="rx_live" type="date" required value="${esc(d.live)}"></label><button class="btn sm" data-rx-set="golive">Save go-live</button>
      <span class="faint">Sponsor ID <span class="mono">${esc(d.id || "none")}</span></span></div>
    <div class="faint">ID counters, one per record type, continuing across funds: ${ctr}</div>
    <label class="row"><input type="checkbox" data-rx-suffix="1" ${d.suffix ? "checked" : ""}> Treat Ltd and Limited as the same word when matching names</label>
    <div>${d.required.length ? d.required.map((r) => `<div class="row"><span><b>${esc(r.field)}</b> (${esc(r.type)}) is ${r.required ? "required" : "optional"} for this sponsor</span><button class="btn sm" data-rx-set="reset_req|${esc(r.type)}|${esc(r.field)}">Use the standard</button></div>`).join("")
      : '<div class="faint">No Required? overrides for this sponsor.</div>'}</div>
    ${d.preferences.length ? `<div><div class="eyebrow">Rules taught for this sponsor</div>${d.preferences.map((p) => `<div class="sub">${esc(p)}</div>`).join("")}</div>` : ""}</div>`;
  }

  const HTML = { existing: existingHtml, fund: fundHtml, settings: settingsHtml };

  async function load(el) {
    const s = st.get(el);
    try { s.data = await api(`/api/console/records/${s.view}${q(s.opts)}`); s.error = ""; last[s.view + q(s.opts)] = s.data; } catch (e) { s.error = e.message; }
    paint(el);
  }
  function paint(el) {
    const s = st.get(el);
    if (!s.data) { el.innerHTML = s.error ? `<div class="note n-err" role="alert">${esc(s.error)}</div>` : '<div class="faint">Loading…</div>'; return; }
    el.innerHTML = HTML[s.view](s.data, s);
  }
  async function write(el, body) {
    const s = st.get(el);
    try { s.data = await post({ sponsor: s.data ? s.data.sponsor : "", ...body }); s.error = ""; last[s.view + q(s.opts)] = s.data; window.dispatchEvent(new Event("records:changed")); } catch (e) { s.error = e.message; note(e.message); }
    paint(el);
  }

  document.addEventListener("click", (e) => {
    document.querySelectorAll("[data-rx='fund']").forEach((el) => {
      const s = st.get(el);
      if (s && s.pop && !el.contains(e.target)) { s.pop = false; paint(el); }
    });
    const host = e.target.closest("[data-rx]");
    if (!host || !st.has(host)) return;
    const s = st.get(host);
    if (e.target.closest("[data-rx-pop]")) { s.pop = !s.pop; paint(host); return; }
    const op = e.target.closest("[data-rx-op]");
    if (op) { const [o, type] = op.dataset.rxOp.split("|"); write(host, { op: o, type }); return; }
    const set = e.target.closest("[data-rx-set]");
    if (set) {
      const [o, type, field] = set.dataset.rxSet.split("|");
      if (o === "golive") write(host, { op: "golive", value: (host.querySelector("#rx_live") || {}).value || "" });
      else write(host, { op: o, type, value: field });
    }
  });
  document.addEventListener("change", async (e) => {
    const host = e.target.closest("[data-rx]");
    if (!host || !st.has(host)) return;
    if (e.target.matches("[data-rx-suffix]")) { write(host, { op: "suffix", value: e.target.checked ? "on" : "off" }); return; }
    const f = e.target.closest("[data-rx-file]");
    if (f && f.files && f.files[0]) {
      const s = st.get(host), file = f.files[0];
      try {
        s.data = await api(`/api/console/records/file/${encodeURIComponent(file.name)}?sponsor=${encodeURIComponent(s.data.sponsor)}&type=${encodeURIComponent(f.dataset.rxFile)}`,
          { method: "PUT", headers: { "Content-Type": "application/octet-stream" }, body: file });
        s.error = ""; window.dispatchEvent(new Event("records:changed"));
      } catch (err) { s.error = err.message; note(err.message); }
      paint(host);
    }
  });

  window.RECORDS = {
    render(view, el, opts) {
      st.set(el, { view, opts: opts || {}, data: last[view + q(opts || {})] || null, pop: false, error: "" });
      paint(el);
      load(el);
    },
  };
})();
