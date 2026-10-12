"use strict";
/* "Reshape this sheet": the job screen's card for a sheet the reader cannot place (ticket 11, tier 2).
   Markup and class names are the console's (card, tw, pill, note, stat, btn, lnk). Every figure comes from
   /api/console/jobs/{job}/reshape; nothing is computed here. A BLOCK has no override: its approve button is
   disabled and the server refuses anyway. Each WARN must be ticked. The approval is bound to the three hashes the
   person saw (grid, table, report), which the server checks.
   Entry point:  window.RESHAPE.render(hostElement, {id, type}) */
(function () {
  const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const nf = (n) => Number(n).toLocaleString("en-US");
  const pill = (c, t) => `<span class="pill p-${c}">${esc(t)}</span>`;
  const state = {};                       // per job id: {ticks: {code: bool}, reason, scope, err, busy}
  const st = (id) => (state[id] = state[id] || { ticks: {}, reason: "", scope: "job", err: "", busy: "" });
  const FATE = {
    used: ["var(--ok-bg)", "used"], open: ["var(--err-bg)", "not accounted for"], title: ["var(--bg)", "title"],
    header: ["var(--bg)", "header"], note: ["var(--bg)", "note"], label: ["var(--bg)", "label"], blank: ["var(--bg)", "blank"],
    subtotal: ["var(--info-bg)", "subtotal, set aside"], grand_total: ["var(--info-bg)", "grand total, set aside"],
  };
  const LEGEND = ["used", "subtotal", "header", "open"];

  const issues = (v, p) => {
    const rows = [...p.blocks.map((b) => ["BLOCK", b]), ...p.warns.map((w) => ["WARN", w])];
    if (!rows.length) return `<div class="note n-ok">Every cell is accounted for and every total ties out.</div>`;
    const s = st(v.id);
    return `<div class="tw"><table><tr><th>Level</th><th>What the check found</th><th class="n">Count</th><th>Examples</th><th>Your decision</th></tr>
      ${rows.map(([lvl, i]) => `<tr><td>${lvl === "BLOCK" ? pill("err", "BLOCK") : pill("warn", "WARN")}</td>
        <td style="white-space:normal">${esc(i.text)}</td><td class="n">${nf(i.count)}</td>
        <td class="mono" style="white-space:normal">${i.examples.map(esc).join(", ")}</td>
        <td>${lvl === "BLOCK" ? '<span class="faint">no override: nobody can approve this</span>'
          : `<label><input type="checkbox" data-rs="tick|${esc(i.code)}" ${s.ticks[i.code] ? "checked" : ""}> I have checked this</label>`}</td></tr>`).join("")}
      </table></div>`;
  };
  const before = (p) => `<div class="tw"><table class="mono" style="font-size:12px"><tr><th></th>${p.before_cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr>
    ${p.before.map((r) => `<tr><td class="n">${r.row}</td>${r.cells.map((c) => {
      const f = FATE[c.fate] || FATE.header;
      return `<td title="${esc(c.ref)}: ${esc(f[1])}" style="background:${f[0]};white-space:nowrap;max-width:190px;overflow:hidden;text-overflow:ellipsis">${esc(c.text ?? "")}</td>`;
    }).join("")}</tr>`).join("")}</table></div>
    <div class="faint">${LEGEND.map((k) => `<span style="background:${FATE[k][0]};padding:1px 7px;border-radius:6px">${esc(FATE[k][1])}</span>`).join(" ")} · hover a cell for its address</div>`;
  const after = (p) => (!p.after ? '<div class="faint">No table: a BLOCK stopped it.</div>'
    : `<div class="tw"><table><tr>${p.after.columns.map((c) => `<th>${esc(c.name)} <span class="faint">${esc(c.role)}</span></th>`).join("")}</tr>
      ${p.after.rows.map((r) => `<tr>${r.map((c) => `<td title="copied from ${esc(c.from || "nothing: left blank")}">${esc(c.text)}</td>`).join("")}</tr>`).join("")}</table></div>`);

  function proposalHtml(v) {
    const p = v.proposal, s = st(v.id), k = p.counts;
    const blocked = p.blocks.length > 0;
    const unticked = p.warns.filter((w) => !s.ticks[w.code]).length;
    const why = Object.entries(k.set_aside_by_reason).map(([r, n]) => `${nf(n)} ${esc(r.replace("_", " "))}`).join(", ");
    return `<div style="display:grid;gap:6px"><b>What the program does</b>
      ${p.steps.length ? `<ol style="margin:0;padding-left:20px">${p.steps.map((x) => `<li>${esc(x)}</li>`).join("")}</ol>
        <div class="faint">Written by the model to describe its program. It is not checked; the table below is.</div>`
        : p.reused ? `<div>A saved program for this sponsor, run again on this file.</div>` : ""}
      ${p.summary ? `<div class="faint">${esc(p.summary)}</div>` : ""}</div>
      <div class="stat"><div><b>${nf(k.cells_in)}</b><span>Cells in the sheet</span></div><div><b>${nf(k.used)}</b><span>Used in the table</span></div>
        <div><b>${nf(k.set_aside)}</b><span>Set aside${why ? ": " + why : ""}</span></div><div><b>${nf(k.rows_out)}</b><span>Rows in the table</span></div>
        <div><b>${nf(k.subtotals_proven)}</b><span>Subtotals proven by exact sum</span></div></div>
      ${p.after ? `<div class="faint">Numbers read ${esc(k.numbers_in)} · numbers written ${esc(k.numbers_out)} (the rest are proven subtotals and labels).</div>` : ""}
      <div style="display:grid;gap:6px"><b>Checks</b>${issues(v, p)}</div>
      <div style="display:grid;gap:6px"><b>Before: the sheet</b>${before(p)}</div>
      <div style="display:grid;gap:6px"><b>After: the table the job will read</b>${after(p)}</div>
      <details><summary class="faint">The program (for an engineer)</summary><pre class="mono" style="white-space:pre-wrap">${esc(p.program)}</pre></details>
      <div style="display:grid;gap:8px"><b>Your approval</b>
        <label class="f">Reason <span class="faint">(recorded with the decision)</span><input data-rs="reason" value="${esc(s.reason)}" ${blocked ? "disabled" : ""}></label>
        <div class="row"><label>Use it for <select data-rs="scope"><option value="job" ${s.scope === "job" ? "selected" : ""}>this file only</option>
          <option value="sponsor" ${s.scope === "sponsor" ? "selected" : ""}>this file, and offer it for the next file of this layout</option></select></label>
          <button class="btn pri" data-rs="approve" ${blocked || unticked || s.busy ? "disabled" : ""}>Approve this reshape</button>
          <span class="faint">${blocked ? "A BLOCK is open: no one can approve it." : unticked ? `${unticked} WARN to tick first.` : "Bound to the sheet, table and checks shown here."}</span></div></div>`;
  }

  function html(v, id, only) {
    const s = st(id);
    if (only && (!v.show || v.applied || !v.reuse.length)) return "";   // the offer alone, shown first on the tab the job lands on
    if (!v.show) {
      return v.can_open ? `<div><button class="lnk" data-rs="open">Reshape this sheet</button> <span class="faint">${esc(v.note || "")}</span></div>` : (v.note ? `<div class="faint">${esc(v.note)}</div>` : "");
    }
    if (v.applied) {
      return `<div class="card" style="display:grid;gap:8px"><div class="row between"><h2>Sheet reshaped</h2>${pill("ok", "Approved by " + v.applied.by)}</div>
        <div>${esc(v.file)} was reshaped into <b>${nf(v.applied.rows)}</b> rows by a checked program. The job reads that table, not the sheet,
        and it plays back without running anything. Program <span class="mono">${esc(v.applied.artifact_id.slice(0, 14))}</span>.</div></div>`;
    }
    const who = typeof reviewer === "function" && !reviewer() ? `<label class="row" style="gap:8px"><span class="faint" style="font-size:12px">Deciding as</span><input data-rs-who="1" placeholder="your name" autocomplete="name" aria-label="Your name" style="min-width:160px;min-height:30px"></label>` : "";
    const note = v.sandbox ? "" : '<div class="note n-warn" role="alert">Reshaping needs the sandbox, which is not running. Enter the values by hand or raise a ticket; there is no fallback.</div>';
    const reuse = v.reuse.length ? `<div class="note n-info" style="display:grid;gap:6px"><div>${v.reuse.length > 1 ? "These programs" : "A program"} approved for this sponsor fit${v.reuse.length > 1 ? "" : "s"} this exact layout.
      One click runs ${v.reuse.length > 1 ? "one" : "it"} again with every check.</div>${v.reuse.map((r) => `<div class="row"><span class="mono">${esc(r.artifact_id.slice(0, 14))}</span>
      <span class="faint">${esc(r.case)} · columns ${esc(r.columns.join(", "))}</span><button class="btn sm" data-rs="reuse|${esc(r.artifact_id)}" ${v.sandbox && !s.busy ? "" : "disabled"}>Use this one</button></div>`).join("")}</div>` : "";
    if (only) return `<div class="card" style="display:grid;gap:8px" id="reshape-offer"><div class="eyebrow">Saved program</div>
      <div>This sheet is not a table yet, so the rows below are blocked. Run the saved program first; the checks run again and you approve what it makes.</div>
      ${s.err ? `<div class="note n-err" role="alert">${esc(s.err)}</div>` : ""}${reuse}${who}</div>`;
    return `<div class="card" style="display:grid;gap:12px" id="reshape-card">
      <div class="row between"><div><div class="eyebrow">Reshape this sheet</div><h2>I cannot place ${esc(v.file)} as a table</h2></div>${pill(v.proposal ? "warn" : "info", v.proposal ? "Waiting for your check" : "Proposal needed")}</div>
      ${v.reasons.length ? `<ul style="margin:0;padding-left:20px">${v.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : '<div class="faint">You opened this card by hand.</div>'}
      ${note}${v.note ? `<div class="note n-warn">${esc(v.note)}</div>` : ""}${s.err ? `<div class="note n-err" role="alert">${esc(s.err)}</div>` : ""}${reuse}${who}
      ${v.proposal ? proposalHtml(v) : `<div class="row"><button class="btn pri" data-rs="propose" ${v.sandbox && !s.busy ? "" : "disabled"}>${s.busy === "propose" ? "Working on it, this takes a minute…" : "Propose a reshape program"}</button>
        <span class="faint">The model writes a program that names cells; code runs it in the sandbox and checks every cell and total.</span></div>`}</div>`;
  }

  const cache = new Map();
  async function fetchView(id) {
    const r = await fetch(`/api/console/jobs/${encodeURIComponent(id)}/reshape`);
    const b = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(b.detail || r.statusText);
    b.id = id;   // the server view has no id; the card keeps its ticks and reason under it
    cache.set(id, b);
    return b;
  }
  async function render(host, job) {
    if (!host) return;
    const only = host.dataset.only === "1";
    if (cache.has(job.id)) host.innerHTML = html(cache.get(job.id), job.id, only);
    host.dataset.job = job.id;
    try {
      const v = await fetchView(job.id);
      if (host.isConnected) host.innerHTML = html(v, job.id, only);
    } catch (e) { if (host.isConnected) host.innerHTML = `<div class="note n-err">${esc(e.message)}</div>`; }
  }
  async function act(id, body) {
    const s = st(id);
    const who = typeof reviewer === "function" ? reviewer() : "";
    if (!who) { s.err = "Say who is deciding first: type your name in the Who is deciding box."; redraw(id); return; }
    s.err = ""; s.busy = body.action;
    redraw(id);
    try {
      const r = await fetch(`/api/console/jobs/${encodeURIComponent(id)}/reshape`, {
        method: "POST", headers: { "Content-Type": "application/json", "X-Reviewer": who },
        body: JSON.stringify(body) });
      const b = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(b.detail || b.error || r.statusText);
      b.id = id;
      cache.set(id, b);
      if (body.action === "propose" || body.action === "reuse") s.ticks = {};
      if (b.applied && typeof window.render === "function") { s.busy = ""; window.render(); return; }
    } catch (e) { s.err = e.message; }
    s.busy = "";
    redraw(id);
  }
  function redraw(id) {
    const host = document.querySelector(`[data-reshape][data-job="${CSS.escape(id)}"]`);
    if (host && cache.has(id)) host.innerHTML = html(cache.get(id), id, host.dataset.only === "1");
  }
  document.addEventListener("input", (e) => {
    if (e.target.dataset && e.target.dataset.rsWho !== undefined && typeof reviewerName !== "undefined") {   // the card asks for the name itself: the Who box may not be on this tab
      reviewerName = e.target.value; store.set("reviewer", e.target.value.trim()); return;
    }
    const t = e.target.closest("[data-rs]"), host = e.target.closest("[data-reshape]");
    if (!t || !host) return;
    if (t.dataset.rs === "reason") st(host.dataset.job).reason = t.value;
  });
  document.addEventListener("change", (e) => {
    const t = e.target.closest("[data-rs]"), host = e.target.closest("[data-reshape]");
    if (!t || !host) return;
    const id = host.dataset.job, [k, v] = t.dataset.rs.split("|");
    if (k === "scope") st(id).scope = t.value;
    if (k === "tick") { st(id).ticks[v] = t.checked; redraw(id); }
  });
  document.addEventListener("click", (e) => {
    const t = e.target.closest("button[data-rs]"), host = e.target.closest("[data-reshape]");
    if (!t || !host) return;
    const id = host.dataset.job, [k, v] = t.dataset.rs.split("|"), s = st(id), view = cache.get(id) || {};
    if (k === "open") act(id, { action: "open" });
    else if (k === "propose") act(id, { action: "propose" });
    else if (k === "reuse") act(id, { action: "reuse", artifact_id: v });
    else if (k === "approve" && view.proposal) {
      act(id, { action: "approve", proposal_id: view.proposal.id, bound: view.proposal.bound, scope: s.scope, reason: s.reason,
        acknowledged: Object.keys(s.ticks).filter((c) => s.ticks[c]) });
    }
  });
  window.RESHAPE = { render };
})();
