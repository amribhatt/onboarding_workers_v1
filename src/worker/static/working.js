"use strict";
/* "Show working" of the job screen: fields, decisions, every row, crosswalks, skills, context, runs and audit.
   Markup and class names follow docs/mockup/onboarding-console.html (jobView, fieldsV, dmV, resultsV, xwalk, skillsV,
   graphV, runsV, auditV). Every figure comes from /api/console/jobs/{job}/working/{tab}; a tab with nothing to show
   says so. Entry point:  window.WORKING.render(tab, job, el)   job = {id, type}
   The job screen also calls WORKING.button() and WORKING.shell(job) for the toggle and the tab strip. */
(function () {
  const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const nf = (n) => Number(n).toLocaleString("en-US");
  const IC = {
    check: '<path d="M5 12l5 5 9-10"/>', x: '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
    warn: '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18v.5"/>', dot: '<circle cx="12" cy="12" r="3"/>',
    hour: '<path d="M6 3h12M6 21h12M7 3c0 6 10 6 10 12v6M17 3c0 6-10 6-10 12v6"/>',
  };
  const ic = (n) => `<svg viewBox="0 0 24 24" aria-hidden="true">${IC[n] || ""}</svg>`;
  const pill = (c, t, i) => `<span class="pill p-${c}">${i ? ic(i) : ""}${esc(t)}</span>`;
  const head = (v) => `<div><h2>${esc(v.title)}</h2><div class="sub">${esc(v.sub || "")}</div></div>`;
  const emptyNote = (t) => `<div class="note n-info">${esc(t)}</div>`;
  const GL = (t) => /^GL/.test(t || "");
  const TABS = [["fields", "Fields"], ["dm", "Decisions"], ["results", "Every row"], ["skills", "Skills"], ["graph", "Context"],
    ["runs", "Runs & trace"], ["ask", "Ask the job"], ["audit", "Audit"]];
  const tabsOf = (type) => (GL(type)
    ? [["fields", "Mapping"], ["dm", "Decisions"], ["results", "Every row"], ["xwalk", "Crosswalks"], ["recon", "Reconcile"], ["skills", "Skills"],
      ["graph", "Context"], ["runs", "Runs & trace"], ["ask", "Ask the job"], ["audit", "Audit"]] : TABS);

  const state = { open: false, tab: {}, rt: "all", asked: {}, askSql: {} };  // tab per job id; results filter; last answer per job
  const cache = new Map();                              // "job|tab" -> last html, painted before the fetch returns
  const rerender = () => { if (typeof window.render === "function") window.render(); };

  /* ---------- one renderer per tab ---------- */
  function fieldsV(v) {
    const st = {
      required_missing: pill("err", "Required · fill it in Results", "x"), missing: pill("warn", "MISSING, you can fill it in Results", "warn"),
      set: pill("info", "Set by you", "check"), sponsor: pill("ok", "Sponsor setup", "check"), approved: pill("ok", "Approved", "check"),
      pending: pill("warn", "Pending your approval", "hour"),
    };
    return `${head(v)}<div class="tw"><table><tr><th>Target field</th><th>Required?</th><th>Source column</th><th class="n">Confidence</th><th>Status</th></tr>
      ${v.rows.map((r) => `<tr><td class="mono">${esc(r.field)}</td><td>${r.required ? "Yes" : "No"}</td>
        <td>${r.kind === "constant" ? `“${esc(r.source)}” <span class="faint">sponsor setup</span>` : r.kind === "generated"
          ? '<span class="faint">generated from the sponsor counter</span>' : r.source ? esc(r.source) : '<span class="faint">not in the file</span>'}</td>
        <td class="n">${r.confidence === null || r.confidence === undefined ? "–" : r.confidence + "%"}</td><td>${st[r.status] || ""}</td></tr>`).join("")}</table></div>
      <div class="faint">${esc(v.note)} Change a mapping in the plan above; it asks for a reason, which goes to the audit.</div>`;
  }
  function mappingV(v) {
    const s = v.stats;
    const st = { matched: pill("ok", "Matched", "check"), set: pill("ok", "Matched", "check"), needs: pill("err", "Needs assignment", "x"), no_match: pill("warn", "No match", "warn") };
    return `${head(v)}
      <div class="stat"><div><b>${nf(s.matched)}</b><span>Tier 1 · matched</span></div><div><b>${nf(s.unmatched)}</b><span>Tier 3 · unmatched</span></div>
        <div><b>${s.types_mapped}/${s.types}</b><span>Transaction types mapped</span></div></div>
      <div class="tw"><table><tr><th>Source account</th><th>Ledgerline account</th><th class="n">Tier</th><th>Status</th></tr>
      ${v.accounts.map((a) => `<tr><td class="mono">${esc(a.account)} <span class="faint">${esc(a.name)}</span></td>
        <td>${a.ledgerline ? esc(a.ledgerline) : '<span class="faint">no match</span>'}${a.status === "needs" && a.suggest ? `<div class="faint">suggested: ${esc(a.suggest)}</div>` : ""}</td>
        <td class="n">${esc(a.tier)}</td><td>${st[a.status] || ""}</td></tr>`).join("") || '<tr><td colspan="4" class="faint">No accounts read from the file.</td></tr>'}</table></div>`;
  }
  function dmV(v) {
    const tbl = (t) => `<div style="display:grid;gap:6px"><div class="row"><b>${esc(t.id)}</b><span class="lab">HIT POLICY ${esc(String(t.hit).toUpperCase())}</span>
      <span class="lab">${esc(t.skill)}</span></div>${t.description ? `<div class="faint">${esc(t.description)}</div>` : ""}
      <div class="tw"><table><tr><th class="n">#</th>${Object.keys(t.inputs).map((k) => `<th>${esc(k)}</th>`).join("")}<th>→ ${esc(t.outputs.join(", "))}</th></tr>
      ${t.rows.map((r, i) => `<tr><td class="n">${i + 1}</td>${Object.keys(t.inputs).map((k) => `<td>${esc(r.when[k] ?? "any")}</td>`).join("")}
        <td style="font-weight:600">${esc(t.outputs.map((o) => r.then[o] ?? "").filter(Boolean).join(" · "))}</td></tr>`).join("")}</table></div></div>`;
    return `${head(v)}${v.empty ? emptyNote(v.empty) : v.tables.map(tbl).join("")}
      <div style="display:grid;gap:6px"><b>Rules in force for this job</b><div class="tw"><table><tr><th>Rule</th><th>From</th></tr>
      ${v.rules.map((r) => `<tr><td style="white-space:normal">${esc(r.rule)}</td><td class="faint">${esc(r.source)}</td></tr>`).join("")}</table></div></div>`;
  }
  const WRITTEN = { ok: pill("ok", "Written", "check"), warn: pill("warn", "Written, check", "warn"), skip: pill("mute", "Left out", "dot"), err: pill("err", "ERR", "x") };
  function resultsV(v) {
    if (v.empty) return emptyNote(v.empty);
    const s = v.stats, f = { all: () => 1, out: (r) => r.st === "ok" || r.st === "warn", skip: (r) => r.st === "skip", err: (r) => r.st === "err", warn: (r) => r.st === "warn" }[state.rt] || (() => 1);
    const tabs = [["all", "All", v.total], ["out", "Written", s.out], ["skip", "Left out", s.skip], ["err", "ERR", s.err], ["warn", "WARN", s.warn]];
    const chips = `<div class="chips" role="tablist">${tabs.map((t) => `<button role="tab" aria-selected="${state.rt === t[0]}" class="${state.rt === t[0] ? "on" : ""}" data-wk="rt|${t[0]}">${t[1]} ${nf(t[2])}</button>`).join("")}</div>`;
    const more = v.total > v.rows.length ? `<div class="faint">Showing the first ${nf(v.rows.length)} of ${nf(v.total)}. Use the find box above for any other row.</div>` : "";
    const rows = v.rows.filter(f);
    if (v.gl) {
      return `<div class="row between"><h2>Results · run ${v.run.n}</h2><span class="faint">${esc(v.run.time)}</span></div>
      <div class="stat"><div><b>${nf(s.read)}</b><span>Lines read</span></div><div><b>${nf(s.journals)}</b><span>Journals</span></div>
        <div><b style="color:var(--err)">${nf(s.err)}</b><span>ERR lines</span></div><div><b style="color:var(--warn)">${nf(s.warn)}</b><span>WARN lines</span></div>
        ${s.offsets ? `<div><b>${nf(s.offsets)}</b><span>Offset lines made</span></div>` : ""}<div><b>${nf(s.out)}</b><span>Written</span></div></div>
      <div class="eyebrow">Journals</div><div class="tw"><table><tr><th>Journal</th><th class="n">Lines</th><th class="n">Debit</th><th class="n">Credit</th><th class="n">Difference</th><th>Balance</th></tr>
      ${v.journals.map((x) => `<tr><td class="mono">${esc(x.journal)}</td><td class="n">${x.lines}</td><td class="n">${esc(x.debit)}</td><td class="n">${esc(x.credit)}</td><td class="n">${esc(x.diff)}</td>
        <td>${x.st === "ok" ? pill("ok", "Balanced", "check") : x.st === "warn" ? pill("warn", "Within tolerance", "warn") : pill("err", "Out of balance", "x")}</td></tr>`).join("")}</table></div>
      ${chips}<div class="tw"><table><tr><th>Status</th><th>Journal</th><th class="n">Line</th><th>Account</th><th>Type</th><th class="n">Debit</th><th class="n">Credit</th><th>Reference</th><th>Why</th></tr>
      ${rows.map((l) => `<tr class="hov"><td>${WRITTEN[l.st]}</td><td class="mono">${esc(l.journal)}</td><td class="n">${l.line}</td><td>${esc(l.account)}</td><td>${esc(l.type)}</td>
        <td class="n">${esc(l.debit)}</td><td class="n">${esc(l.credit)}</td><td class="mono">${esc(l.ref) || "–"}</td><td style="white-space:normal;min-width:220px">${esc(l.why)}</td></tr>`).join("")
        || '<tr><td colspan="9" class="faint">Nothing here.</td></tr>'}</table></div>${more}`;
    }
    return `<div class="row between"><h2>Results · run ${v.run.n}</h2><span class="faint">${esc(v.run.time)}</span></div>
      <div class="stat"><div><b>${nf(s.read)}</b><span>Rows read</span></div><div><b>${nf(s.already)}</b><span>Already on file</span></div>
        <div><b style="color:var(--err)">${nf(s.err)}</b><span>ERR to resolve</span></div><div><b style="color:var(--warn)">${nf(s.warn)}</b><span>WARN to check</span></div>
        <div><b>${nf(s.out)}</b><span>Written</span></div></div>
      ${chips}<div class="tw"><table><tr><th class="n">Row</th><th>Status</th><th>Source ID</th><th>New ID</th><th>Name</th><th>Reason</th></tr>
      ${rows.map((r) => `<tr class="hov"><td class="n">${r.i}</td><td>${WRITTEN[r.st]}</td><td class="mono">${esc(r.id) || "(blank)"}</td><td class="mono">${esc(r.new_id) || "–"}</td>
        <td style="white-space:normal;min-width:180px">${esc(r.name) || '<span class="faint">(blank)</span>'}${r.x ? `<div class="faint">${esc(r.x)}</div>` : ""}</td>
        <td style="white-space:normal;min-width:220px">${esc(r.why)}${r.hit && r.st !== "ok" ? `<div class="mono faint">on file / matched: ${esc(r.hit)}</div>` : ""}</td></tr>`).join("")
        || '<tr><td colspan="6" class="faint">Nothing here.</td></tr>'}</table></div>${more}`;
  }
  function xwalkV(v) {
    return `${head(v)}<div class="tw"><table><tr><th>Reference</th><th class="n">Distinct</th><th class="n">Resolved</th><th>Status</th><th>Unresolved</th></tr>
      ${v.rows.map((r) => `<tr><td><b>${esc(r.ref)}</b></td><td class="n">${nf(r.distinct)}</td><td class="n">${nf(r.resolved)}</td>
        <td>${r.unresolved.length ? pill("err", r.unresolved.length + (r.ref.startsWith("Accounts") ? " unmapped" : " unknown"), "x") : pill("ok", "All resolved", "check")}</td>
        <td>${r.unresolved.map((u) => `<span class="mono">${esc(u)}</span>`).join(" ")}</td></tr>`).join("")}</table></div>
      <div class="note n-info">A reference that is missing here was probably added outside the app. Add it as an existing record from the check card, with its Ledgerline ID.</div>`;
  }
  function skillsV(v) {
    return `${head(v)}${v.empty ? emptyNote(v.empty) : `<div class="tw"><table><tr><th>Skill</th><th>What it does</th><th>Decision tables</th><th>Used by this job</th></tr>
      ${v.skills.map((s) => `<tr><td class="mono">${esc(s.name)}@${esc(s.version)}</td><td style="white-space:normal">${esc(s.description)}</td>
        <td>${s.tables.length ? esc(s.tables.join(", ")) : '<span class="faint">none</span>'}</td>
        <td style="white-space:normal">${s.used ? `${pill("ok", "Yes", "check")}<div class="faint">${esc(s.evidence)}</div>` : '<span class="faint">Not by this job</span>'}</td></tr>`).join("")}</table></div>`}`;
  }
  function graphV(v) {
    return `${head(v)}<ul class="sub" style="margin:0;padding-left:18px;display:grid;gap:6px"><li><b>${esc(v.sponsor)}</b> · GP_ID ${esc(v.sponsor_id) || "not set"}
      <ul style="padding-left:18px;display:grid;gap:4px;margin-top:4px">
      ${v.funds.map((f) => `<li><b>${esc(f.name)}</b> · static ${f.static}/5 · GL ${f.gl}/2</li>`).join("") || '<li class="faint">No entities loaded yet.</li>'}
      ${v.facts.map((f) => `<li><b>${esc(f.label)}</b> · ${esc(f.value)}</li>`).join("")}</ul></li></ul>`;
  }
  function runsV(v) {
    const d = (x, good) => (x ? `<b style="color:var(--${(x < 0) === good ? "ok" : "err"})">${x > 0 ? "+" : ""}${x}</b>` : "");
    const cls = ["todo", "done", "now", "stop"];
    const runs = v.runs.length ? `<div class="tw"><table><tr><th class="n">Run</th><th>Time</th><th>Rules in force</th><th class="n">ERR</th><th class="n">WARN</th><th class="n">Written</th></tr>
      ${v.runs.map((r) => `<tr><td class="n">${r.n}</td><td>${esc(r.time) || "–"}</td><td>${esc(r.rules)}</td><td class="n">${r.err} ${d(r.d_err, true)}</td>
        <td class="n">${r.warn} ${d(r.d_warn, true)}</td><td class="n">${r.out} ${d(r.d_out, false)}</td></tr>`).join("")}</table></div>` : emptyNote("No runs yet.");
    return `${head(v)}${runs}<div><h2 style="margin-top:6px">Stages</h2></div>
      <div class="tw"><table><tr><th>Stage</th><th>State</th><th>Fact</th></tr>${v.stages.map((s) => `<tr><td>${esc(s.stage)}</td>
        <td>${["Not yet", "Done", "Waiting for you", "Stopped"][s.state]}</td><td>${esc(s.fact)}</td></tr>`).join("")}
      ${v.retries ? `<tr><td colspan="3" class="faint">Retries: ${v.retries}. A stopped job resumes from its last checkpoint; only the stopped stage ran again.</td></tr>` : ""}</table></div>
      <div class="faint">${esc(v.note)}</div>`;
  }
  function auditV(v) {
    return `${head(v)}<div class="tw"><table><tr><th>Time</th><th>Who</th><th>What</th><th>Reason</th></tr>
      ${v.rows.map((x) => `<tr><td class="mono">${esc(x.t)}</td><td>${esc(x.who)}</td><td style="white-space:normal">${esc(x.a)}</td><td style="white-space:normal">${esc(x.reason)}</td></tr>`).join("")
        || `<tr><td colspan="4" class="faint">${esc(v.empty || "Empty.")}</td></tr>`}</table></div>`;
  }
  /* Ask the job: one read-only SELECT over the job's rows, run in the sandbox; the answer never changes the job. */
  function askV(v) {
    const jobId = (document.getElementById("wk") || { dataset: {} }).dataset.job;
    const a = state.asked[jobId];
    const ans = !a ? "" : a.status !== "ok"
      ? `<div class="note n-warn">${esc(a.message || a.reason || "The question was not answered.")}</div>`
      : `<div class="eyebrow">Answer · ${nf(a.row_count)} row${a.row_count === 1 ? "" : "s"}${a.truncated ? " (first " + nf(a.rows.length) + " shown)" : ""}</div>
        ${a.summary ? `<div>${esc(a.summary)}</div>` : a.summary_status === "not verified" ? '<div class="faint">Summary not verified: it holds a number the result does not.</div>' : ""}
        <div class="tw"><table><tr>${a.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr>
        ${a.rows.map((r) => `<tr>${r.map((x) => `<td>${esc(x ?? "")}</td>`).join("")}</tr>`).join("")}</table></div>
        <details><summary class="faint">The SELECT that ran</summary><pre class="mono">${esc(a.sql || "")}</pre></details>`;
    return `${head(v)}${v.empty ? emptyNote(v.empty) : ""}
      ${v.sandbox ? "" : emptyNote("Questions need the sandbox, which is not running. Nothing is asked without it.")}
      <div class="faint">Tables: ${v.tables.map((t) => `<span class="mono">${esc(t.name)}</span> (${nf(t.rows)} rows: ${esc(t.columns.join(", "))})`).join("; ") || "none yet"}.
        Names: ${v.terms.map((t) => `<span class="mono">v_${esc(t)}</span>`).join(", ")}.</div>
      <div style="display:grid;gap:6px"><input id="ask-q" placeholder="Your question" style="width:100%">
        <textarea id="ask-sql" rows="3" class="mono" placeholder="SELECT ... FROM job_rows ..." style="width:100%">${esc(state.askSql[jobId] || "")}</textarea>
        <div><button class="btn sm" data-wk="ask" ${v.sandbox && !v.empty ? "" : "disabled"}>Ask</button></div></div>
      ${ans}
      ${v.history.length ? `<div class="eyebrow">Earlier questions</div><div class="tw"><table><tr><th>Question</th><th>Result</th><th class="n">Rows</th></tr>
        ${v.history.map((h) => `<tr><td style="white-space:normal">${esc(h.question)}</td><td>${esc(h.status)}</td><td class="n">${nf(h.rows)}</td></tr>`).join("")}</table></div>` : ""}
      <div class="faint">${v.glossary.map((g) => `<b>${esc(g.term)}</b>: ${esc(g.means)}`).join(" · ")}</div>`;
  }
  const VIEWS = { fields: (v) => (v.kind === "mapping" ? mappingV(v) : fieldsV(v)), dm: dmV, results: resultsV, xwalk: xwalkV, skills: skillsV, graph: graphV, runs: runsV, ask: askV, audit: auditV };

  /* ---------- the job screen's hooks ---------- */
  const tabOf = (job) => {
    const t = state.tab[job.id];
    return t && tabsOf(job.type).some((x) => x[0] === t) ? t : job.ran ? "results" : "fields";
  };
  function button() {
    return `<button class="btn sm" data-wk="toggle">${state.open ? "Hide working" : "Show working"}</button>`;
  }
  function shell(job) {
    if (!state.open) return "";
    const cur = tabOf(job);
    return `<div class="card" style="display:grid;gap:14px;grid-template-columns:minmax(0,1fr)"><div class="chips" role="tablist">${tabsOf(job.type).map((t) => `<button role="tab" aria-selected="${cur === t[0]}" class="${cur === t[0] ? "on" : ""}" data-wk="tab|${t[0]}">${t[1]}</button>`).join("")}</div>
      <div id="wk" data-job="${esc(job.id)}" data-type="${esc(job.type)}" data-tab="${esc(cur)}"></div></div>`;
  }
  async function render(tab, job, el) {
    if (!el) return;
    if (tab === "recon") {   // drawn by the learning module, not fetched here
      el.innerHTML = `<div data-learning="recon" data-bare="1"></div>`;
      if (window.LEARNING) window.LEARNING.render("recon", job, el.firstChild);
      return;
    }
    const key = job.id + "|" + tab + "|" + state.rt;
    if (cache.has(key)) el.innerHTML = cache.get(key);
    else el.innerHTML = '<div class="faint">Loading…</div>';
    try {
      const r = await fetch(`/api/console/jobs/${encodeURIComponent(job.id)}/working/${encodeURIComponent(tab)}`);
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.detail || body.error || r.statusText);
      const html = VIEWS[tab] ? VIEWS[tab](body) : "";
      cache.set(key, html);
      if (el.isConnected) el.innerHTML = html;
    } catch (e) {
      if (el.isConnected) el.innerHTML = `<div class="note n-err">${esc(e.message)}</div>`;
    }
  }
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-wk]");
    if (!t) return;
    const [k, v] = t.dataset.wk.split("|");
    const host = document.getElementById("wk");
    if (k === "ask" && host) {
      const sql = (document.getElementById("ask-sql") || {}).value || "";
      const question = (document.getElementById("ask-q") || {}).value || "";
      state.askSql[host.dataset.job] = sql;
      fetch(`/api/console/jobs/${encodeURIComponent(host.dataset.job)}/ask`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question, sql }),
      }).then((r) => r.json().then((b) => (r.ok ? b : { status: "refused", message: b.detail || r.statusText })))
        .catch((err) => ({ status: "refused", message: String(err) }))
        .then((b) => { state.asked[host.dataset.job] = { ...b, sql }; cache.clear(); rerender(); });
      return;
    }
    if (k === "toggle") state.open = !state.open;
    else if (k === "tab" && host) { state.tab[host.dataset.job] = v; state.rt = "all"; }
    else if (k === "rt") state.rt = v;
    rerender();
  });
  window.WORKING = { render, button, shell, tabs: tabsOf, state };
})();
