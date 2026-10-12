"use strict";
/* The Reconcile tab of the job screen and its work in the worker chat, in the grammar of the Issues tab.
   Table: one row per check and cause (Check, Example, Records, Status, one Review button) with status chips and the
   fund type. Review puts the group in the chat: its affected records first (paged on the server, exact total, jump to a
   page) and the options of the cause (light chips whose text comes from the skill table). A chip only fills the
   message box; Send goes through the worker, which answers with a preview and ONE Apply (Undo afterwards; a hold needs
   a note to release; every decision needs a note and a named reviewer). Nothing here computes or decides a figure:
   every number comes from /api/console/jobs/{job}/recon....
   Entry points: RECON.table(job), RECON.chat(job), RECON.sync(job), RECON.group(). */
(function () {
  const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const nf = (n) => Number(n).toLocaleString("en-US");
  const money = (n, kind) => (n === null || n === undefined ? "" : kind === "int" ? nf(n) : Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
  const st = { job: "", data: null, key: "", chip: "need", page: 0, per: 50, ft: "", review: null, err: "" };
  const STATE = { open: "Open", fix: "Fixed", out: "Left out", hold: "On hold" };
  const IMPACT = { block: ["err", "Blocks approval"], check: ["warn", "Validate"], info: ["mute", "Info"] };
  const COLORS = { open: "--warn", fix: "--ok", out: "--faint", hold: "--info" };
  const onTab = () => /tab=recon/.test(location.hash || "");

  async function call(path, opts) {
    const who = typeof reviewer === "function" ? reviewer() : "";
    const r = await fetch(path, { ...(opts || {}), headers: { ...((opts && opts.headers) || {}), ...(who ? { "X-Reviewer": who } : {}) } });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || body.detail || r.statusText);
    return body;
  }
  const base = (j) => `/api/console/jobs/${encodeURIComponent(j.id)}`;
  const jobOf = () => (typeof S !== "undefined" && S && S.job) || null;
  const redraw = () => { if (typeof render === "function") render(); };

  /* ---------- loading: the table once per change, the open group's records once per page ---------- */
  const keyOf = (j) => [j.id, JSON.stringify(j.tally), (j.chat || []).length, st.chip, st.page, st.per, st.ft, st.ver || 0].join("|");
  async function fetchRows() {
    const r = st.review, j = jobOf();
    if (!r || !j) return;
    try { r.rows = await call(`${base(j)}/recon/${encodeURIComponent(r.gid)}/rows?${new URLSearchParams({ page: r.page, per: r.per })}`); r.err = ""; }
    catch (e) { r.err = e.message; }
  }
  async function sync(j) {
    if (!j || !j.ran || st.loading || !onTab()) return;
    if (st.job !== j.id) Object.assign(st, { job: j.id, data: null, key: "", page: 0, review: null, chip: "need", ft: "" });
    const k = keyOf(j);
    if (k === st.key) return;
    st.loading = true;
    try {
      const qs = new URLSearchParams({ page: st.page, per: st.per, status: st.chip === "all" ? "" : st.chip, ft: st.ft });
      st.data = await call(`${base(j)}/recon?${qs}`);
      st.key = k; st.err = "";
      if (st.review) {
        const g = st.data.groups.find((x) => x.id === st.review.gid);
        if (g) { st.review.group = g; await fetchRows(); } else st.review = null;
      }
    } catch (e) { st.err = e.message; st.key = k; }
    st.loading = false;
    redraw();
  }

  /* ---------- the table ---------- */
  const status = (g) => `<span class="row" style="gap:6px;flex-wrap:nowrap"><i class="dot" style="background:var(${COLORS[g.state] || "--warn"})"></i>${esc(STATE[g.state] || "Open")}</span>`;
  function example(g) {
    const e = g.example;
    if (!e) return "";
    const fig = e.a === null && e.b === null ? "" : ` · ${esc(money(e.a, e.kind) || "–")} against ${esc(money(e.b, e.kind) || "–")}`;
    return `<div class="mono" style="font-size:12px">${esc(e.label)}${fig}</div>`;
  }
  function rowHtml(g) {
    const imp = IMPACT[g.impact] || IMPACT.check, c = g.counts;
    return `<tr class="hov">
      <td style="white-space:normal;min-width:190px"><b>${esc(g.name)}</b>
        <div class="faint" style="font-size:12px">${esc(g.label)} · <span class="pill p-${imp[0]}">${esc(imp[1])}</span></div>
        <div class="mact"><div class="faint" style="font-size:12px">${nf(g.n)} records</div>${status(g)}</div></td>
      <td class="c-pat" style="white-space:normal;min-width:170px">${example(g)}</td>
      <td class="n c-rows" style="white-space:nowrap"><b>${nf(g.n)}</b>${c.open !== g.n ? `<div class="faint" style="font-size:11.5px">${nf(c.open)} open</div>` : ""}</td>
      <td class="c-st">${status(g)}${g.by ? `<div class="faint" style="font-size:11.5px">${esc(g.by)}</div>` : ""}</td>
      <td class="c-act" style="white-space:nowrap"><button class="btn sm ${g.state === "open" ? "pri" : ""}" data-rc="rv|${esc(g.id)}" aria-label="Review: ${esc(g.name)}, ${esc(g.label)}">Review</button></td></tr>`;
  }
  function decisions(d) {
    const h = (d.history || []).filter((e) => !e.reverted);
    if (!h.length) return "";
    return `<div class="tw"><table style="font-size:12.5px"><tr><th>When</th><th>Decision</th><th>Validation</th><th class="n">Records</th><th>By</th><th>Note</th><th></th></tr>
      ${h.map((e) => `<tr><td class="mono">${esc(e.when)}</td><td>${esc(e.label)}</td><td style="white-space:normal">${esc(e.check)}<div class="faint" style="font-size:11.5px">${esc(e.cause)}</div></td>
        <td class="n">${nf(e.rows)}</td><td>${esc(e.who)}</td><td style="white-space:normal">${esc(e.note)}</td>
        <td>${e.release ? '<span class="faint">release it in the chat</span>' : `<button class="btn sm" data-act="recon_undo|${e.id}">Undo</button>`}</td></tr>`).join("")}</table></div>`;
  }
  function table(j) {
    if (!j.ran) return "";
    const d = st.data;
    if (!d || st.job !== j.id) return `<div class="card"><div class="faint">Working out the reconciliations…</div></div>`;
    const c = d.chips || {}, k = d.checks, n = d.total;
    const chip = (key, l, cnt) => `<button class="${st.chip === key ? "on" : ""}" data-rc="chip|${key}" aria-pressed="${st.chip === key}">${l}${cnt === undefined ? "" : " " + nf(cnt)}</button>`;
    const seg = d.fund_types.length ? `<span class="seg" role="group" aria-label="Fund type">${d.fund_types.map((f) =>
      `<button class="${f.id === d.ft ? "on" : ""}" data-rc="ft|${esc(f.id)}" aria-pressed="${f.id === d.ft}">${esc(f.label)}</button>`).join("")}</span>` : "";
    const first = n ? d.page * d.per + 1 : 0, last = Math.min(n, (d.page + 1) * d.per);
    const empty = c.need === 0 ? `<div class="note n-ok"><b>Nothing here needs you.</b> ${nf(k.clean)} of ${nf(k.applies)} checks tie.</div>` : "";
    return `<div class="card" style="display:grid;gap:12px"><div class="row between"><div><h2>Reconcile</h2><div class="sub">${nf(k.applies)} check${k.applies === 1 ? "" : "s"} apply · ${nf(k.clean)} tie · ${nf(k.open)} record${k.open === 1 ? "" : "s"} still open. Figures come from this job's own lines; the client's figures only from a report you load.</div></div>${seg}</div>${empty}
      <div class="chips" role="group" aria-label="Status">${chip("need", "Needs you", c.need)}${chip("hold", "On hold", c.hold)}${chip("done", "Done", c.done)}${chip("all", "All")}</div>
      <div class="tw sc" style="max-height:62vh"><table class="itab"><tr><th>Validation</th><th class="c-pat">Example</th><th class="n c-rows">Records</th><th class="c-st">Status</th><th class="c-act"><span class="sr">Action</span></th></tr>
        ${d.groups.map(rowHtml).join("") || `<tr><td colspan="5" class="faint">No check matches.</td></tr>`}</table></div>
      <div class="row between"><span class="faint" style="font-size:12px">${d.skill ? esc(d.skill) : ""}</span>
        <span class="row" style="gap:8px"><span class="faint">checks ${nf(first)}-${nf(last)} of ${nf(n)}</span>
          <button class="btn sm" data-rc="gp|-1" ${d.page ? "" : "disabled"}>‹ Prev</button><button class="btn sm" data-rc="gp|1" ${d.page < d.pages - 1 ? "" : "disabled"}>Next ›</button></span></div>
      ${decisions(d)}${st.err ? `<div class="note n-err" role="alert">${esc(st.err)}</div>` : ""}</div>`;
  }

  /* ---------- the review pane: this check's records first, then what to do (same grammar as the Issues pane) ---------- */
  const PILL = { PASS: "ok", WARN: "warn", ERROR: "err", REVIEW: "info" };
  const rowsTsv = (p) => ["Record\tUpload\tCompared with\tDifference\tFlag"].concat(p.rows.map((x) =>
    [x.label, money(x.a, x.kind), money(x.b, x.kind), money(x.diff, x.kind), x.status || "no report"].join("\t"))).join("\n");
  function recordsHtml(r) {
    const p = r.rows;
    if (!p) return r.err ? `<div class="note n-err">${esc(r.err)}</div>` : `<div class="faint" role="status">Loading the records…</div>`;
    const num = (v, kind) => { const t = money(v, kind); return `<td class="n" title="${esc(t)}">${esc(t)}</td>`; };
    const cmp = p.rows.some((x) => x.b !== null && x.b !== undefined || x.diff !== null && x.diff !== undefined);   // no client report: no empty columns
    const cols = cmp ? '<col><col class="c4"><col class="c4"><col class="c4"><col class="c5">' : '<col><col class="c4"><col class="c5">';
    return `<table class="rvt"><colgroup>${cols}</colgroup>
      <tr><th>Record</th><th class="n">Upload</th>${cmp ? '<th class="n" title="Compared with: the client\'s report">Client</th><th class="n">Difference</th>' : ""}<th>Flag</th></tr>
      ${p.rows.map((x) => `<tr>${PANE.cell(x.label + (x.note ? " · " + x.note : ""))}${num(x.a, x.kind)}${cmp ? num(x.b, x.kind) + num(x.diff, x.kind) : ""}
        <td title="${esc((x.status || "no report") + (STATE[x.state] ? " · " + STATE[x.state] : ""))}">${x.status ? `<span class="pill p-${PILL[x.status] || "mute"}">${esc(x.status)}</span>` : '<span class="faint">no report</span>'}</td></tr>`).join("")
        || `<tr><td colspan="${cmp ? 5 : 3}" class="faint">No records.</td></tr>`}</table>`;
  }
  function reportLine(g) {
    const l = g.layout;
    if (!l) return "";
    return `<div class="row" style="gap:6px 12px" title="${esc("Columns: " + l.columns.join(" · ") + ". " + l.text)}"><span class="sub">Client's report: ${l.report ? `loaded, ${esc(l.report.file)} (${esc(l.report.by)})` : "not loaded"}</span>
      <a class="lnk" href="/api/console/learning/layout/${esc(g.check)}">Download blank layout</a>
      <label class="lnk" style="cursor:pointer">${l.report ? "Replace the report" : "Upload the client's report"}<input type="file" accept=".xlsx,.csv" data-rcf="${esc(g.check)}" style="display:none"></label></div>`;
  }
  function chat(j) {
    const r = st.review;
    if (!j || !j.ran || !onTab() || !r) return "";
    return { since: r.since || 0, html: (lastHtml, hasLast) => {
      const g = r.group;
      if (!g) return `<section class="rv"><div class="rv-h"><b id="rvh" tabindex="-1">That group is not in this view any more.</b></div><div class="rv-recs" id="rvrecs"></div>
        <div class="rv-bar"><div class="rv-links"><button class="lnk" data-rc="rvx">Back to chat</button></div></div></section>`;
      const imp = IMPACT[g.impact] || IMPACT.check;
      const go = g.options.some((o) => o.id === "go"), opts = g.options.filter((o) => o.id !== "go");
      const open = !!opts.length && (!hasLast || r.alt);
      const sug = !open ? "" : `<div class="faint">Pick one, add why after the colon (a note is required), then press Send. Nothing is recorded until you press Apply.</div>
        <div class="row" style="gap:6px">${opts.map((o) => `<button class="sug" data-rc="sug|${esc(o.id)}|${esc(o.label)}">${esc(o.label)}</button>`).join("")}</div>`;
      const goto = go ? `<div class="row" style="gap:8px"><a class="lnk" href="#tab=issues">Decide these on the Issues tab</a><span class="faint">A change there takes effect; nothing is decided here.</span></div>` : "";
      const done = g.decisions.map((d) => `<div class="row" style="gap:8px"><span class="sub">${esc(STATE[d.state] || "")} · ${esc(d.label)} · ${nf(d.rows)} records · by ${esc(d.who)}</span>
        <span class="faint">note: ${esc(d.note)}</span>${d.release ? `<button class="sug" data-rc="sug|release|Release">Release</button><span class="faint">click it, add why after the colon, then Send</span>` : `<button class="btn sm" data-act="recon_undo|${d.id}">Undo</button>`}</div>`).join("");
      const name = (typeof reviewer === "function" && reviewer()) ? "" : `<label class="row" style="gap:8px"><span class="faint">Deciding as</span><input id="is_who" placeholder="your name" autocomplete="name" aria-label="Your name" style="min-width:140px;min-height:28px"></label>`;
      const first = (g.consequence || "").split(/(?<=\.)\s/)[0];
      const region = r.why ? `<div class="rv-text"><p>${esc(g.consequence)}</p></div>` : recordsHtml(r);
      const pager = !r.why && r.rows ? PANE.pager("rc", r.rows, "records", "rc_jump", rowsTsv(r.rows)) : "";
      const links = [r.why ? `<button class="lnk" data-rc="rows">Show records</button>` : `<button class="lnk" data-rc="why">Reason</button>`,
        hasLast && opts.length ? `<button class="lnk" data-rc="alt">${r.alt ? "Hide other choices" : "Other choices"}</button>` : "",
        st.data && st.data.next ? `<button class="lnk" data-rc="next">Next group</button>` : "",
        `<button class="lnk" data-rc="rvx">Back to chat</button>`].filter(Boolean).join("");
      return `<section class="rv" aria-label="Review"><div class="rv-h"><b id="rvh" tabindex="-1">${esc(g.name)}</b><span class="faint">· ${nf(g.n)} records</span><span class="pill p-${imp[0]}">${esc(imp[1])}</span></div>
        <div class="rv-sub" title="${esc(g.consequence)}">${esc(g.label)}${first ? ` · ${esc(first)}` : ""}</div>
        <div class="rv-recs" id="rvrecs">${region}</div>${pager}
        <div class="rv-bar">${lastHtml}${reportLine(g)}${sug}${goto}${done}${name}<div class="rv-links">${links}</div></div></section>`;
    } };
  }
  const group = () => (st.review && onTab() ? st.review.gid : "");
  /* rows per page follow the height the pane has now */
  function fit() {
    const want = PANE.per("rvrecs"), r = st.review;
    if (!want || !r || !r.rows || r.why || !onTab() || want === r.per) return;
    r.per = want; r.page = Math.max(0, Math.floor((r.rows.start - 1) / want));
    fetchRows().then(redraw);
  }
  let rzTimer = null;
  window.addEventListener("resize", () => { clearTimeout(rzTimer); rzTimer = setTimeout(fit, 150); });

  /* ---------- actions ---------- */
  async function review(gid) {
    const j = jobOf();
    if (!j || !st.data) return;
    st.review = { gid, group: st.data.groups.find((x) => x.id === gid), page: 0, per: PANE.guess(), rows: null, why: false, err: "", since: (j.chat || []).filter((m) => m.who === "you").length, alt: false };
    if (typeof opener !== "undefined") opener = `[data-rc="rv|${gid}"]`;
    if (typeof collapsed !== "undefined") collapsed = false;
    redraw();
    await fetchRows();
    redraw();
    const h = $("rvh"); if (h && h.focus) h.focus();
  }
  const next = () => { if (st.data && st.data.next) review(st.data.next); else { st.review = null; redraw(); } };
  function suggest(id, label) {
    const box = $("rb");
    const text = `${label}: `;
    draft = text;
    if (box) { box.value = text; box.focus(); box.setSelectionRange(text.length, text.length); }
  }
  async function upload(file, check) {
    const j = jobOf();
    if (!j || !file) return;
    try {
      await call(`/api/console/learning/report/${encodeURIComponent(j.id)}/${encodeURIComponent(check)}?name=${encodeURIComponent(file.name)}`,
        { method: "PUT", body: await file.arrayBuffer() });
      st.ver = (st.ver || 0) + 1; st.key = "";
      if (typeof toast === "function") toast("Report loaded; the check is compared now.");
    } catch (e) { if (typeof toast === "function") toast(e.message, true); }
    await sync(jobOf());
  }

  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-rc]");
    if (!t) return;
    const [k, a, b] = t.dataset.rc.split("|");
    if (k === "rv") review(a);
    else if (k === "chip") { st.chip = a; st.page = 0; st.key = ""; sync(jobOf()); }
    else if (k === "ft") { st.ft = a; st.page = 0; st.key = ""; if (st.review) st.review = null; sync(jobOf()); }
    else if (k === "gp") { st.page = Math.max(0, st.page + +a); st.key = ""; sync(jobOf()); }
    else if (k === "sug") suggest(a, b);
    else if (k === "rp" && st.review && st.review.rows) { st.review.page = Math.max(0, st.review.rows.page + +a); fetchRows().then(redraw); }
    else if (k === "rj" && st.review) { const n = +($("rc_jump") || {}).value; if (n > 0) { st.review.page = n - 1; fetchRows().then(redraw); } }
    else if (k === "why" && st.review) { st.review.why = true; redraw(); }
    else if (k === "rows" && st.review) { st.review.why = false; redraw(); }
    else if (k === "alt" && st.review) { st.review.alt = !st.review.alt; redraw(); }
    else if (k === "rvx") { st.review = null; redraw(); }
    else if (k === "next") next();
  });
  document.addEventListener("change", (e) => {
    const f = e.target && e.target.dataset && e.target.dataset.rcf;
    if (f && e.target.files && e.target.files[0]) upload(e.target.files[0], f);
  });
  window.RECON = { table, chat, sync, fit, group, state: st };
})();
