"use strict";
/* The Issues tab of the job screen and its work in the worker chat.
   Table: one row per cause and pattern (Cause, Pattern example, Rows, Source, Status, one Review button) with a search
   and status chips. Review puts the group in the chat: its records first (paged, exact total) and a few suggestions
   (light chips whose text comes from the cause's option table). A chip only fills the message box; the person edits
   it or writes their own, and Send goes through the worker, which answers with a preview and ONE Apply button
   (Undo afterwards, both audited with the reviewer). Nothing on this page counts, decides or invents a value:
   every number comes from /api/console/jobs/{job}/issues....
   Entry points: ISSUES.table(job), ISSUES.verdict(job), ISSUES.activity(job), ISSUES.chat(job), ISSUES.sync(job),
   ISSUES.key(event), ISSUES.badge(). */
(function () {
  const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const nf = (n) => Number(n).toLocaleString("en-US");
  const icon = (p) => `<svg viewBox="0 0 24 24" aria-hidden="true">${p}</svg>`;
  const FIND = icon('<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>');
  const GRID = icon('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 10v10"/>');
  const st = { job: "", data: null, key: "", chip: "need", q: "", page: 0, per: 50, kb: 0, review: null, src: null, err: "" };
  const short = (f) => (f.length > 26 ? f.slice(0, 14) + "…" + f.slice(-10) : f);
  const STATE = { open: "Open", fix: "Fixed", out: "Left out", hold: "On hold" };
  const IMPACT = { block: ["err", "Blocks upload"], check: ["warn", "Validate"], info: ["mute", "Info"] };
  const COLORS = { open: "--warn", fix: "--ok", out: "--faint", hold: "--info" };

  async function call(path) {
    const who = typeof reviewer === "function" ? reviewer() : "";
    const r = await fetch(path, { headers: who ? { "X-Reviewer": who } : {} });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || body.detail || r.statusText);
    return body;
  }
  const base = (j) => `/api/console/jobs/${encodeURIComponent(j.id)}`;
  const jobOf = () => (typeof S !== "undefined" && S && S.job) || null;
  const redraw = () => { if (typeof render === "function") render(); };

  /* ---------- loading: the groups once per change, the open group's rows once per page ---------- */
  const keyOf = (j) => [j.id, JSON.stringify(j.tally), (j.chat || []).length, st.chip, st.q, st.page, st.per].join("|");
  async function fetchRows() {
    const r = st.review, j = jobOf();
    if (!r || !j) return;
    const qs = new URLSearchParams({ page: r.page, per: r.per });
    try { r.rows = await call(`${base(j)}/issues/${encodeURIComponent(r.gid)}/rows?${qs}`); r.err = ""; }
    catch (e) { r.err = e.message; }
  }
  async function sync(j) {
    if (!j || !j.ran || st.loading) return;
    if (st.job !== j.id) Object.assign(st, { job: j.id, data: null, key: "", page: 0, review: null, src: null, chip: "need" });
    const k = keyOf(j);
    if (k === st.key) return;
    st.loading = true;
    try {
      const qs = new URLSearchParams({ page: st.page, per: st.per, status: st.chip === "all" ? "" : st.chip, q: st.q });
      st.data = await call(`${base(j)}/issues?${qs}`);
      st.key = k; st.err = "";
      if (st.review) {
        const g = st.data.groups.find((x) => x.id === st.review.gid);
        if (g) { st.review.group = g; await fetchRows(); }
        else st.review = null;   // decided or gone: the worker's message above says what happened
      }
    } catch (e) { st.err = e.message; st.key = k; }
    st.loading = false;
    redraw();
  }

  /* ISSUES:TABLE (the collapsed view: no cell ranges, no spreadsheet steps; those are asked for in the chat) */
  function verdict(j) {
    const d = st.data;
    if (!d || st.job !== j.id) return `<div class="faint">${nf(j.tally.read)} read · ${nf(j.tally.err)} need you</div>`;
    const v = d.verdict, u = v.units, p = (x) => (v.N ? x / v.N * 100 : 0);
    const seg = [[v.ready, "--ok"], [v.blocked, "--err"], [v.hold, "--info"], [v.out, "--faint"]];
    return `<div style="display:grid;gap:6px"><div class="row" style="gap:4px 16px;align-items:baseline">
      <span><b style="font-size:20px;font-weight:500;font-variant-numeric:tabular-nums">${nf(v.ready)}</b> <span class="sub">of ${nf(v.N)} ${u} ready</span></span>
      <span class="row" style="gap:5px"><i class="dot" style="background:var(--err)"></i><b>${nf(v.blocked)}</b> blocked</span>
      <span class="row" style="gap:5px"><i class="dot" style="background:var(--info)"></i><b>${nf(v.hold)}</b> on hold</span>
      <span class="row" style="gap:5px"><i class="dot" style="background:var(--faint)"></i><b>${nf(v.out)}</b> left out</span>
      <span class="faint" style="font-size:12px">${nf(v.ready)} + ${nf(v.blocked)} + ${nf(v.hold)} + ${nf(v.out)} = ${nf(v.sum)}${v.sum === v.N ? "" : " (does not tie)"}</span></div>
      <div class="bar" style="margin:0" role="img" aria-label="${nf(v.ready)} ready, ${nf(v.blocked)} blocked, ${nf(v.hold)} on hold, ${nf(v.out)} left out">${seg.filter((x) => x[0] > 0).map((x) => `<i style="width:${p(x[0])}%;background:var(${x[1]})"></i>`).join("")}</div></div>`;
  }
  function rowHtml(g, i, u) {
    const imp = IMPACT[g.impact] || IMPACT.check, src = g.source;
    const status = `<span class="row" style="gap:6px;flex-wrap:nowrap"><i class="dot" style="background:var(${COLORS[g.state] || "--warn"})"></i>${esc(STATE[g.state] || "Open")}</span>`;
    return `<tr class="hov ${st.kb === i ? "kbsel" : ""}">
      <td style="white-space:normal;min-width:180px"><b>${esc(g.label)}</b>
        <div class="faint" style="font-size:12px">${g.state === "open" ? `<span class="pill p-${imp[0]}">${esc(imp[1])}</span>` : esc(g.decision || "")}</div>
        <div class="mact"><div class="faint" style="font-size:12px">${nf(g.n)} ${u}</div>${status}</div></td>
      <td class="c-pat" style="white-space:normal;min-width:170px"><div class="faint" style="font-size:12px">${esc(g.pattern)}${g.patterns > 1 ? ` · ${g.patterns} patterns` : ""}</div>
        <div class="mono" style="font-size:12px">${esc(g.before)} → ${esc(g.after)}</div></td>
      <td class="n c-rows" style="white-space:nowrap"><b>${nf(g.n)}</b></td>
      <td class="c-src"><button class="lnk src" data-is="src|${esc(g.id)}" title="${esc(src.file)} · sheet ${esc(src.sheet)}">${GRID}<span class="trunc">${esc(short(src.file))}</span></button></td>
      <td class="c-st">${status}${g.by ? `<div class="faint" style="font-size:11.5px">${esc(g.by)}</div>` : ""}</td>
      <td class="c-act" style="white-space:nowrap"><button class="btn sm ${g.state === "open" ? "pri" : ""}" data-is="rv|${esc(g.id)}" aria-label="Review: ${esc(g.label)}">Review</button></td></tr>`;
  }
  function table(j) {
    if (!j.ran) return "";
    const d = st.data;
    if (!d || st.job !== j.id) return `<div class="card"><div class="faint">Counting the issues…</div></div>`;
    const u = d.units, c = d.chips || {}, n = d.total;
    const chip = (k, l, cnt) => `<button class="${st.chip === k ? "on" : ""}" data-is="chip|${k}" aria-pressed="${st.chip === k}">${l}${cnt === undefined ? "" : " " + nf(cnt)}</button>`;
    const first = n ? st.page * d.per + 1 : 0, last = Math.min(n, (st.page + 1) * d.per);
    const empty = d.verdict.blocked === 0 && c.need === 0
      ? (c.hold ? `<div class="note n-warn"><b>Nothing needs you now, but ${nf(c.hold)} on hold.</b> ${nf(d.verdict.ready)} ${u} ready. Release or decide the held ones before approval: nothing is written while any is on hold.</div>`
        : `<div class="note n-ok"><b>Nothing needs you.</b> ${nf(d.verdict.ready)} ${u} ${j.approved ? "approved. The upload file is on the Output tab." : "ready. The upload file is on the Output tab, for your approval."}</div>`) : "";
    return `<div class="card" style="display:grid;gap:12px">${empty}
      <div class="row between" style="gap:8px"><div class="chips" role="group" aria-label="Status">${chip("need", "Needs you", c.need)}${chip("hold", "On hold", c.hold)}${chip("done", "Done", c.done)}${chip("all", "All")}</div>
        <div class="row" style="gap:8px;flex-wrap:nowrap">${FIND}<input id="is_q" value="${esc(st.q)}" placeholder="Search causes and patterns" aria-label="Search causes and patterns" style="min-height:30px;min-width:140px"></div></div>
      <div class="tw sc" style="max-height:62vh"><table class="itab"><tr><th>Cause</th><th class="c-pat">Pattern example</th><th class="n c-rows">Rows</th><th class="c-src">Source</th><th class="c-st">Status</th><th class="c-act"><span class="sr">Action</span></th></tr>
        ${d.groups.map((g, i) => rowHtml(g, i, u)).join("") || `<tr><td colspan="6" class="faint">No group matches.</td></tr>`}</table></div>
      <div class="row between"><span class="faint" style="font-size:12px">Keys: j / k move · Enter reviews in the chat</span>
        <span class="row" style="gap:8px"><span class="faint">groups ${nf(first)}-${nf(last)} of ${nf(n)}</span>
          <button class="btn sm" data-is="gp|-1" ${st.page ? "" : "disabled"}>‹ Prev</button><button class="btn sm" data-is="gp|1" ${d.page < d.pages - 1 ? "" : "disabled"}>Next ›</button></span></div>
      ${st.err ? `<div class="note n-err" role="alert">${esc(st.err)}</div>` : ""}</div>`;
  }
  /* ISSUES:END */

  /* the Activity tab: every decision on this job, newest first, each with its Undo */
  function activity(j) {
    const d = st.data;
    if (!d || st.job !== j.id) return `<div class="faint">Loading…</div>`;
    const h = d.history || [];
    return `<div class="tw"><table style="font-size:12.5px"><tr><th>When</th><th>Decision</th><th>Cause · pattern</th><th class="n">Rows</th><th>By</th><th>Reason or note</th><th></th></tr>
      ${h.map((e) => `<tr class="${e.reverted ? "faint" : ""}"><td class="mono">${esc(e.when)}</td><td>${esc(e.label)}${e.reverted ? " (reverted)" : ""}${e.rule ? " · saved as a rule" : ""}</td>
        <td style="white-space:normal">${esc(e.cause)}<div class="faint" style="font-size:11.5px">${esc(e.pattern)}</div>${(e.changes || []).map((c) => `<div class="mono" style="font-size:11.5px">row ${nf(c.row)}: ${esc(c.before)} → ${esc(c.after)}</div>`).join("")}</td><td class="n">${nf(e.rows)}</td><td>${esc(e.who)}</td>
        <td style="white-space:normal">${esc(e.note || "")}${e.released ? `<div class="faint" style="font-size:11.5px">released by ${esc(e.released.by)} (${esc(e.released.at)}): ${esc(e.released.note)}</div>` : ""}${(e.from_hold || []).length ? `<div class="faint" style="font-size:11.5px">decided after hold #${e.from_hold.join(", #")}</div>` : ""}</td>
        <td>${e.reverted ? "" : e.kind === "hold" ? '<span class="faint">release it in the chat</span>' : e.irreversible ? `<span class="faint">kept: ${esc(e.irreversible)}</span>` : `<button class="btn sm" data-act="issue_undo|${e.id}">${e.release ? "Release" : "Undo"}</button>`}</td></tr>`).join("")
        || '<tr><td colspan="7" class="faint">No decisions yet. Everything you decide is listed here, each with its undo.</td></tr>'}</table></div>`;
  }

  /* ---------- the review pane: this group's records first, then what to do ----------
     One screen that never scrolls: the group's title and cause, the records (one line each, as many as fit), the pager,
     and a bar with the worker's newest answer (the preview and its one Apply) or the suggestions. "Why", "Open the
     source" and "Where it is" take the place of the records until "Show rows". Layout and classes: console.html (.rv*). */
  const rowsTsv = (p) => ["Row\tRecord\tWas\tBecomes"].concat(p.rows.map((x) => [x.row, x.name, x.before, x.after].join("\t"))).join("\n");
  /* one row at a time: a box for this row's own value (or a note) and a button per option the cause offers for one row.
     Each press is one audited decision naming this row; the rest of the group stays open. */
  const ROW_BTN = { edit: "Set value", out: "Skip row", park: "Hold", fix: "Use suggestion" };
  const ROW_HINT = { edit: "type the value for this row, then press Set value", out: "an optional note, then press Skip row",
    park: "why it is on hold (required), then press Hold", fix: "an optional note, then press Use suggestion" };
  function rowControls(r, x) {
    const g = r.group, opts = (r.rows && r.rows.row_options) || [];
    if (!g || g.state !== "open" || !opts.length) return "";
    const hint = opts.map((o) => ROW_HINT[o.id]).filter(Boolean).join("; ");
    const cols = (r.rows && r.rows.columns) || [];
    const cell = !cols.length ? "" : `<details class="rowcell"><summary class="faint">Edit any cell of this row</summary><div class="rowact">
      <select data-is-col="${x.row}" aria-label="Column to edit in row ${x.row}">${cols.map((h) => `<option>${esc(h)}</option>`).join("")}</select>
      <input data-is-cv="${x.row}" maxlength="200" placeholder="New value" aria-label="New value for row ${x.row}">
      <input data-is-cw="${x.row}" maxlength="200" placeholder="Why (optional)" aria-label="Why, for row ${x.row}">
      <button class="btn sm" data-is="rc|${x.row}">Save cell</button></div></details>`;
    return `<tr class="ractr"><td></td><td colspan="3"><div class="rowact"><input data-is-v="${x.row}" maxlength="200" placeholder="Value for this row, or a note" aria-label="Value or note for row ${x.row}" title="${esc(hint)}">`
      + opts.map((o) => `<button class="btn sm" data-is="ra|${x.row}|${esc(o.id)}" title="${esc(o.label)}">${esc(ROW_BTN[o.id] || o.label)}</button>`).join("") + `</div>${cell}</td></tr>`;
  }
  function recordsHtml(r) {
    const p = r.rows;
    if (!p) return r.err ? `<div class="note n-err">${esc(r.err)}</div>` : `<div class="faint" role="status">Loading the rows…</div>`;
    const act = r.group && r.group.state === "open" && (p.row_options || []).length > 0;
    return `${r.rowErr ? `<div class="note n-err" role="alert">${esc(r.rowErr)}</div>` : ""}${r.rowOk ? `<div class="note n-ok" role="status">${esc(r.rowOk)}</div>` : ""}
      <table class="rvt${act ? " rowedit" : ""}"><colgroup><col class="c1"><col class="c-rec"><col><col></colgroup>
      <tr><th class="n">Row</th><th class="c-rec">Record</th><th>Was</th><th>Becomes</th></tr>
      ${p.rows.map((x) => `<tr><td class="n">${nf(x.row)}</td>${PANE.cell(x.name, "c-rec")}${PANE.cell(x.before, "mono")}${PANE.cell(x.after, "mono")}</tr>${act ? rowControls(r, x) : ""}`).join("")
        || '<tr><td colspan="4" class="faint">No rows match.</td></tr>'}</table>`;
  }
  async function rowAct(row, option) {
    const j = jobOf(), r = st.review;
    if (!j || !r) return;
    const box = document.querySelector(`[data-is-v="${row}"]`), text = box ? box.value.trim() : "";
    if (option === "edit" && !text) { r.rowErr = `Type the value for row ${row} first.`; r.rowOk = ""; redraw(); return; }
    if (option === "park" && !text) { r.rowErr = `Say why row ${row} is on hold first.`; r.rowOk = ""; redraw(); return; }
    await postApply({ id: r.gid, option, rows: [row], ...(option === "edit" || option === "park" ? { value: text } : {}) },
      option === "edit" || option === "park" ? "" : text, `Row ${row}: ${ROW_BTN[option].toLowerCase()} done and recorded under your name.`);
  }
  async function cellAct(row) {
    const j = jobOf(), r = st.review;
    if (!j || !r) return;
    const get = (a) => { const el = document.querySelector(`[data-is-${a}="${row}"]`); return el ? el.value.trim() : ""; };
    if (!get("cv")) { r.rowErr = `Type the new value for row ${row} first.`; r.rowOk = ""; redraw(); return; }
    await postApply({ id: r.gid, option: "cell", rows: [row], column: get("col"), value: get("cv") }, get("cw"),
      `Row ${row}: ${get("col")} changed and recorded under your name. I checked the row again.`);
  }
  async function postApply(pick, note, okMsg) {
    const j = jobOf(), r = st.review;
    const who = typeof reviewer === "function" ? reviewer() : "";
    try {
      const res = await fetch(`${base(j)}/issues/apply`, { method: "POST", headers: { "Content-Type": "application/json", ...(who ? { "X-Reviewer": who } : {}) },
        body: JSON.stringify({ picks: [pick], note }) });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.error || body.detail || res.statusText);
      r.rowErr = ""; r.rowOk = okMsg;
      st.key = "";
      window.dispatchEvent(new Event("records:changed"));   // the console reloads its state; the pane redraws from it
    } catch (e) { r.rowErr = e.message; r.rowOk = ""; redraw(); }
  }
  function sourceRegion(w) {
    if (w.err) return `<div class="note n-err">${esc(w.err)}</div>`;
    if (!w.data) return `<div class="faint" role="status">Opening the source…</div>`;
    const d = w.data;
    const hit = (n) => n >= w.first && n <= w.last;
    return `<div class="tw"><table class="rvt wide"><tr><th class="n">#</th>${d.letters.map((l, i) => `<th>${l} · ${esc(d.header[i] || "")}</th>`).join("")}</tr>
      ${d.rows.map((r) => `<tr class="${hit(r.row) ? "hit" : ""}"><td class="n faint">${nf(r.row)}</td>${d.letters.map((_, i) => PANE.cell(r.cells[i] ?? "", "mono")).join("")}</tr>`).join("")}</table></div>`;
  }
  function paneHtml(j) {
    const r = st.review, w = st.src;
    if (!r && !w) return "";
    const g = r && r.group, u = st.data ? st.data.units : "rows";
    const first = (g && g.consequence ? g.consequence : "").split(/(?<=\.)\s/)[0];
    const imp = g ? IMPACT[g.impact] || IMPACT.check : null;
    const head = g ? `<div class="rv-h"><b id="rvh" tabindex="-1">${esc(g.label)}</b><span class="faint">· ${nf(g.n)} ${u}</span>${g.state === "open" ? `<span class="pill p-${imp[0]}">${esc(imp[1])}</span>` : ""}</div>
        <div class="rv-sub" title="${esc(g.consequence)}">${esc(g.pattern)}${first ? ` · ${esc(first)}` : ""}</div>`
      : r ? `<div class="rv-h"><b id="rvh" tabindex="-1">That group is not in this view any more.</b></div>`
      : `<div class="rv-h"><b id="rvh" tabindex="-1">The source file</b><span class="faint mono">${esc(w.data ? w.data.file : "")}</span></div>`;
    let region, pager = "";
    if (w) {
      region = sourceRegion(w);
      if (w.data) {
        const d = w.data, last = d.rows.length ? d.rows[d.rows.length - 1].row : d.start;
        pager = `<div class="rv-pg"><span class="pill p-mute">${esc(d.sheet)}</span><span>rows ${nf(d.start)}-${nf(last)} of ${nf(d.total)}</span>
          <button class="btn sm" data-is="sp|-1" ${d.start > 1 ? "" : "disabled"}>‹ Prev</button><button class="btn sm" data-is="sp|1" ${last < d.total ? "" : "disabled"}>Next ›</button>
          <span class="faint">a viewer: nothing here changes the file</span></div>`;
      }
    } else if (r.where) region = `<div class="rv-text mono">${esc(r.where)}</div>`;
    else if (r.why && g) {
      const holds = (g.hold_history || []).map((h) => `<p>Was on hold: parked by ${esc(h.parked_by)} (${esc(h.parked_at)}): ${esc(h.why)}. Released by ${esc(h.released_by)} (${esc(h.released_at)}): ${esc(h.note)}. Say why you decide them now: add “because …”.</p>`).join("");
      region = `<div class="rv-text"><p>${esc(g.consequence)}${g.recommended ? ` I suggest one option only when ${esc(g.recommended_when)}; that holds for every ${u.slice(0, -1)} in this group.` : ""}</p>${holds}</div>`;
    } else {
      region = recordsHtml(r);
      if (r.rows) pager = PANE.pager("is", r.rows, "rows", "is_jump", rowsTsv(r.rows));
    }
    return { head, region, pager, g };
  }
  function bar(lastHtml, hasLast) {
    const r = st.review, g = r && r.group;
    const showSug = g && g.state === "open" && (!hasLast || r.alt);
    const sug = !showSug ? "" : `<div class="faint">Pick one and press Send, or say it in your own words, for example “row 9 is INV-A, skip row 10”. Nothing changes until you press Apply.${g.recommended ? "" : ` I suggest none in particular: ${esc(g.recommended_when)}.`}</div>
      <div class="row" style="gap:6px">${g.options.map((o) => `<button class="sug" data-is="sug|${esc(o.id)}|${esc(o.label)}">${esc(o.label)}${g.recommended === o.id ? ' <span class="tag">suggested</span>' : ""}</button>`).join("")}</div>`;
    const decided = !g || g.state === "open" ? "" : `<div class="row" style="gap:8px"><span class="sub">${esc(STATE[g.state] || "")}${g.decision ? ` · ${esc(g.decision)}` : ""}${g.by ? ` · by ${esc(g.by)}` : ""}${g.rule ? " · saved as a rule" : ""}</span>
      ${g.release ? `<span class="faint">${esc(g.when || "")} · reason: ${esc(g.note || "")}</span><button class="sug" data-is="sug|release|Release">Release</button><span class="faint">click it, add why after the colon, then Send</span>` : g.undo ? `<button class="btn sm" data-act="issue_undo|${g.undo}">Undo</button>` : g.irreversible ? `<span class="faint">kept: ${esc(g.irreversible)}</span>` : ""}</div>`;
    const name = (typeof reviewer === "function" && reviewer()) ? "" : `<label class="row" style="gap:8px"><span class="faint">Deciding as</span><input id="is_who" placeholder="your name" autocomplete="name" aria-label="Your name" style="min-width:140px;min-height:28px"></label>`;
    const viewing = st.src || (r && (r.why || r.where));
    const links = [
      viewing ? `<button class="lnk" data-is="rows">Show rows</button>` : r ? `<button class="lnk" data-is="why">Reason</button>` : "",
      g && !st.src ? `<button class="lnk" data-is="src|${esc(g.id)}">Open the source</button>` : "",
      r && !viewing ? `<button class="lnk" data-is="where">Where it is</button>` : "",
      hasLast && g && g.state === "open" ? `<button class="lnk" data-is="alt">${r.alt ? "Hide other choices" : "Other choices"}</button>` : "",
      st.data && st.data.next ? `<button class="lnk" data-is="next">Next group</button>` : "",
      `<button class="lnk" data-is="rvx">Back to chat</button>`].filter(Boolean).join("");
    return `${lastHtml}${sug}${decided}${name}<div class="rv-links">${links}</div>`;
  }
  /* the pane as the shell wants it: {since, html(lastHtml, hasLast)}, or "" when nothing is under review */
  function chat(j) {
    if (!j || !j.ran || (!st.review && !st.src)) return "";
    const since = st.review ? st.review.since || 0 : st.src.since || 0;
    return { since, html: (lastHtml, hasLast) => {
      const p = paneHtml(j);
      return `<section class="rv" aria-label="Review">${p.head}<div class="rv-recs" id="rvrecs">${p.region}</div>${p.pager}<div class="rv-bar">${bar(lastHtml, hasLast)}</div></section>`;
    } };
  }
  /* rows per page follow the height the pane has now (called after every draw and on resize) */
  function fit() {
    const want = PANE.per("rvrecs"), r = st.review, w = st.src;
    if (!want) return;
    if (w) { if (w.data && want !== w.count) { w.count = want; moveSource(w.data.start); } return; }
    if (r && r.rows && !r.why && !r.where && want !== r.per) {
      r.per = want; r.page = Math.max(0, Math.floor((r.rows.start - 1) / want));
      fetchRows().then(redraw);
    }
  }
  let rzTimer = null;
  window.addEventListener("resize", () => { clearTimeout(rzTimer); rzTimer = setTimeout(fit, 150); });

  /* ---------- actions ---------- */
  async function review(gid) {
    const j = jobOf();
    if (!j || !st.data) return;
    st.review = { gid, group: st.data.groups.find((x) => x.id === gid), page: 0, per: PANE.guess(), rows: null, why: false, where: "", err: "",
      since: (j.chat || []).filter((m) => m.who === "you").length, alt: false };
    st.src = null;
    if (typeof opener !== "undefined") opener = `[data-is="rv|${gid}"]`;
    if (typeof collapsed !== "undefined") collapsed = false;
    redraw();
    await fetchRows();
    redraw();
    const h = $("rvh"); if (h && h.focus) h.focus();   // the pane is where the person is now; Tab goes on from its title
  }
  async function openSource(gid) {
    const j = jobOf();
    if (!j || !st.data) return;
    const g = st.data.groups.find((x) => x.id === gid) || (st.review && st.review.group);
    if (!g) return;
    st.src = { first: g.source.first, last: g.source.last, data: null, err: "", count: PANE.guess(), since: (j.chat || []).filter((m) => m.who === "you").length };
    if (typeof collapsed !== "undefined") collapsed = false;
    redraw();
    await moveSource(g.source.first);
  }
  async function moveSource(row) {
    const j = jobOf(), w = st.src;
    if (!j || !w) return;
    try { w.data = await call(`${base(j)}/source?row=${Math.max(1, row)}&count=${w.count || 12}`); w.err = ""; } catch (e) { w.err = e.message; }
    redraw();
  }
  async function where() {
    const j = jobOf(), r = st.review;
    if (!j || !r) return;
    r.why = false;
    try {
      const d = await call(`${base(j)}/issues/${encodeURIComponent(r.gid)}/detail`);
      r.where = d.locator ? `${d.locator.label}\n${d.excel.text || ""}` : "";
    } catch (e) { r.where = e.message; }
    redraw();
  }
  const next = () => { if (st.data && st.data.next) review(st.data.next); else { st.review = null; redraw(); } };
  /* a suggestion only fills the message box; "Edit value" waits for the value after its colon */
  function suggest(id, label) {
    const box = $("rb");
    const text = id === "edit" || id === "park" || id === "release" ? `${label}: ` : label;
    draft = text;
    if (box) { box.value = text; box.focus(); box.setSelectionRange(text.length, text.length); }
  }

  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-is]");
    if (!t) return;
    const [k, a, b] = t.dataset.is.split("|");
    if (k === "rv") review(a);
    else if (k === "src") openSource(a);
    else if (k === "chip") { st.chip = a; st.page = 0; st.key = ""; sync(jobOf()); }
    else if (k === "gp") { st.page = Math.max(0, st.page + +a); st.key = ""; sync(jobOf()); }
    else if (k === "sug") suggest(a, b);
    else if (k === "ra") rowAct(+a, b);
    else if (k === "rc") cellAct(+a);
    else if (k === "rp" && st.review && st.review.rows) { st.review.page = Math.max(0, st.review.rows.page + +a); fetchRows().then(redraw); }
    else if (k === "rj" && st.review) { const n = +($("is_jump") || {}).value; if (n > 0) { st.review.page = n - 1; fetchRows().then(redraw); } }
    else if (k === "why" && st.review) { st.review.why = true; st.review.where = ""; redraw(); }
    else if (k === "rows") { if (st.review) { st.review.why = false; st.review.where = ""; } st.src = null; redraw(); }
    else if (k === "alt" && st.review) { st.review.alt = !st.review.alt; redraw(); }
    else if (k === "rvx") { st.review = null; st.src = null; redraw(); }
    else if (k === "where") where();
    else if (k === "next") next();
    else if (k === "sp" && st.src && st.src.data) { const d = st.src.data; moveSource(a === "1" ? d.start + d.count : d.start - d.count); }
    else if (k === "srcx") { st.src = null; redraw(); }
  });
  let qTimer = null;
  document.addEventListener("input", (e) => {
    if (e.target.id === "is_who" && typeof reviewerName !== "undefined") { reviewerName = e.target.value; store.set("reviewer", e.target.value.trim()); return; }
    if (e.target.id !== "is_q") return;
    clearTimeout(qTimer);
    const v = e.target.value;
    qTimer = setTimeout(() => { st.q = v; st.page = 0; st.key = ""; sync(jobOf()).then(() => { const i = $("is_q"); if (i) { i.focus(); i.setSelectionRange(v.length, v.length); } }); }, 250);
  });
  /* j / k move, Enter reviews in the chat; returns true when it used the key */
  function key(e) {
    const d = st.data;
    if (!d || !d.groups.length) return false;
    if (e.key === "j" || e.key === "k") {
      st.kb = Math.max(0, Math.min(d.groups.length - 1, st.kb + (e.key === "j" ? 1 : -1)));
      redraw();
      const el = document.querySelector("tr.kbsel"); if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
      return true;
    }
    const g = d.groups[Math.min(st.kb, d.groups.length - 1)];
    if (e.key === "Enter" && g) { review(g.id); return true; }
    return false;
  }
  window.ISSUES = { table, verdict, activity, chat, sync, fit, key, state: st,
    badge: () => (st.data ? st.data.verdict.blocked : null) };
})();
