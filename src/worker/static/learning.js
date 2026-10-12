"use strict";
/* Learning parts of the job screen: reconcile, teach-back, feedback and the rulebook.
   Markup and class names follow docs/mockup/onboarding-console.html (rcView, teachBack, fbV, bookV).
   Every figure comes from /api/console/learning/*; a part with nothing to show says so.
   Entry point for the shell:  window.LEARNING.render(part, job, el)
   part: "recon" | "teach" | "fb" | "book".   window.LEARNING.flag(kind, ref, skill) returns the flag button's HTML. */
(function () {
  const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const IC = {
    check: '<path d="M5 12l5 5 9-10"/>', x: '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
    lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 018 0v3"/>',
    hour: '<path d="M6 3h12M6 21h12M7 3c0 6 10 6 10 12v6M17 3c0 6-10 6-10 12v6"/>', dot: '<circle cx="12" cy="12" r="3"/>',
  };
  const ic = (n) => `<svg viewBox="0 0 24 24" aria-hidden="true">${IC[n] || ""}</svg>`;
  const pill = (c, t, i) => `<span class="pill p-${c}">${i ? ic(i) : ""}${esc(t)}</span>`;
  const nf = (n) => Number(n).toLocaleString("en-US");
  const money = (n) => Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const PILL = { PASS: ["ok", "check"], WARN: ["warn", "dot"], ERROR: ["err", "x"], REVIEW: ["info", "hour"] };
  const stPill = (s) => pill(PILL[s][0], s, PILL[s][1]);
  const fmt = (v, kind) => (typeof v !== "number" ? esc(v) : kind === "int" ? nf(v) : money(v));
  const open = new Set();            // folds the person has opened, kept across re-renders
  const view = {};                   // per job: {ft, sel}
  const FOLDS = {
    recon: ["Reconcile", "Every figure is computed from this job's own lines"],
    fb: ["Feedback", "Everything flagged, overridden or noted, and where it went"],
    book: ["Rulebook", "Rules people taught for this sponsor"],
  };

  async function api(path, opts) {
    const r = await fetch(path, opts);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || body.detail || r.statusText);
    return body;
  }
  const say = (m, bad) => { if (typeof toast === "function") toast(m, bad); };
  const q = (part, job) => api(`/api/console/learning/${part}?job=${encodeURIComponent(job)}`);
  const post = (body) => api("/api/console/learning/act", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

  /* ---------- reconcile (rcView) ---------- */
  function recon(d, jobId) {
    if (d.empty) return `<div class="faint">${esc(d.empty)}</div>`;
    const vs = (view[jobId] = view[jobId] || {});
    const ft = vs.ft || d.default_ft;
    const app = (c) => !ft || c.scope.includes(ft);
    const cur = d.checks.find((c) => c.id === vs.sel && app(c)) || d.checks.find(app) || d.checks[0];
    const tile = d.by_ft[ft] || d.by_ft[""] || { applies: 0, pass: 0, error: 0, warn: 0, review: 0, needs_report: 0, gap: 0, waiting: 0 };
    const guide = d.checks.filter((c) => c.layout || c.id === "evl");
    const gOn = guide.filter(app).length;
    const cross = (d.cross && d.cross[ft]) || [];
    const seg = d.fund_types.length ? `<span class="seg" role="group" aria-label="Fund type">${d.fund_types.map((f) =>
      `<button class="${f.id === ft ? "on" : ""}" data-lr="ft|${esc(f.id)}" aria-pressed="${f.id === ft}">${esc(f.label)}</button>`).join("")}</span>` : "<span></span>";
    const state = (c) => c.state === "run" ? stPill(c.worst || "PASS")
      : c.state === "needs-report" ? (c.worst === "ERROR" ? stPill("ERROR") : pill("mute", "needs report", "lock"))
      : c.state === "gap" ? pill("mute", "field missing", "lock") : pill("mute", "waiting", "hour");
    const result = (c) => !app(c) ? pill("mute", "n/a", "lock")
      : state(c) + (c.bad ? ` <span class="faint">${c.bad} line${c.bad > 1 ? "s" : ""}</span>` : "");
    const cell = (v, kind) => v === null || v === undefined ? '<span class="faint">—</span>' : fmt(v, kind);
    const rowFlag = (r) => r.status ? stPill(r.status) : pill("mute", "no report", "lock");
    const reportBox = (c) => !c.layout ? "" : `<div class="note n-info" style="display:grid;gap:6px"><div><b>Client's report</b>${c.report
      ? ` · loaded: ${esc(c.report.file)} (${esc(c.report.at)}, ${esc(c.report.by)})` : " · not loaded"}</div>
      <div class="sub">Columns: ${c.layout.columns.map(esc).join(" · ")}. ${esc(c.layout.text)}</div>
      <div class="row" style="gap:8px"><a class="btn sm" href="/api/console/learning/layout/${esc(c.id)}">Download blank layout</a>
      <label class="btn sm pri" style="cursor:pointer">${c.report ? "Replace the report" : "Upload the client's report"}
      <input type="file" accept=".xlsx,.csv" data-lrf="${esc(c.id)}" style="display:none"></label></div></div>`;
    const detail = (c) => `<div style="display:grid;gap:10px;align-content:start"><div class="row between"><b style="font-size:15px">${esc(c.n)}</b>${state(c)}</div>
      <div><div class="eyebrow">Rows it selects</div><div class="sub">${esc(c.sel)}</div></div>
      <div><div class="eyebrow">How it runs</div><ol class="sub" style="margin:4px 0 0;padding-left:18px">${c.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol></div>
      ${c.note ? `<div class="note ${c.state === "run" ? "n-info" : "n-warn"}" role="status">${esc(c.note)}</div>` : ""}
      ${reportBox(c)}
      ${c.rows.length ? `<div class="tw"><table><tr><th>${esc(c.cols[0])}</th><th class="n">${esc(c.cols[1])}</th><th class="n">${esc(c.cols[2])}</th><th class="n">Diff</th><th>Flag</th></tr>${c.rows.map((r) =>
        `<tr><td style="white-space:normal">${esc(r.label)}${r.note ? ` <span class="faint">${esc(r.note)}</span>` : ""}</td><td class="n">${cell(r.a, r.kind)}</td><td class="n">${cell(r.b, r.kind)}</td><td class="n">${r.diff === null || r.diff === undefined ? "" : r.diff === 0 ? (r.kind === "int" ? "0" : "0.00") : fmt(r.diff, r.kind)}</td><td>${rowFlag(r)}</td></tr>`).join("")}</table></div>` : ""}
      <div class="faint"><b>Rule:</b> ${esc(c.rule)}</div></div>`;
    return `<div><h2>Reconcile</h2><div class="sub">${d.gl
      ? "The upload is tied out against itself, and the guide's eleven checks against the client's own reports. Upload figures are worked out from this job's lines; the client's figures come only from the report you load for each check. Without one the Client column stays blank."
      : esc(d.note || "")}</div></div>
      <div class="row between">${seg}<span class="faint">${d.gl ? `${gOn} of 11 checks apply` : `${tile.applies} check${tile.applies === 1 ? "" : "s"} apply`}</span></div>
      <div class="stat"><div><b>${d.gl ? gOn : tile.applies}</b><span>Checks apply</span></div><div><b style="color:var(--ok)">${tile.pass}</b><span>Pass</span></div>
       <div><b style="color:var(--warn)">${tile.warn}</b><span>Warn</span></div><div><b style="color:var(--err)">${tile.error}</b><span>Error</span></div>
       <div><b style="color:var(--info)">${tile.review}</b><span>Review</span></div><div><b>${tile.needs_report}</b><span>Need a report</span></div>${tile.gap ? `<div><b>${tile.gap}</b><span>Field missing</span></div>` : ""}</div>
      ${cross.length ? `<div class="note n-err" role="status"><b>One break shows up in several checks.</b> ${cross.map((x) => `${money(x.amount)} appears in ${x.checks.map(esc).join(", ")}`).join("; ")}. Fix the cause once, then rerun.</div>` : ""}
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px">
       <div class="tw"><table><tr><th>#</th><th>Validation</th><th>Period</th><th>Result</th></tr>${d.checks.map((c) =>
        `<tr class="hov" style="${app(c) ? "" : "opacity:.5"}"><td class="n">${guide.includes(c) ? guide.indexOf(c) + 1 : "·"}</td><td><button class="lnk" style="text-align:left" data-lr="sel|${esc(c.id)}">${esc(c.n)}</button></td><td class="mono">${esc(c.per)}</td><td>${result(c)}</td></tr>`).join("")}</table></div>
       ${cur ? detail(cur) : ""}</div>`;
  }

  /* ---------- teach-back (teachBack) ---------- */
  function teach(d) {
    const L = d.items, open1 = L.filter((x) => x.st === "new").length;
    const scopeBtns = (x) => `<span class="seg" role="group" aria-label="How far this applies">${d.scopes.map((s) =>
      `<button class="${x.scope === s.id ? "on" : ""}" data-lr="lscope|${x.i}|${esc(s.id)}">${esc(s.label)}</button>`).join("")}</span>`;
    const share = d.rules_pct === null ? "no decisions yet" : `${d.rules_pct}% of decisions by rules · ${d.by_you} by you`;
    return `<div style="border-top:1px solid var(--line);padding-top:10px;display:grid;gap:8px"><div class="row between"><b>${esc(d.title)}</b><span class="faint" title="Readiness: decisions made by approved rules ÷ all decisions">${esc(share)}</span></div>
      ${L.map((x) => `<div class="row" style="gap:8px">${x.st === "ok" ? pill("ok", "Learned", "check") : x.st === "no" ? pill("mute", "Not a rule", "x") : pill("info", "New", "dot")}<span style="flex:1;min-width:220px">${esc(x.t)}</span>${x.st === "new"
        ? `${scopeBtns(x)}<button class="btn sm pri" data-lr="lok|${x.i}" ${x.scope ? "" : "disabled"}>Confirm</button><button class="lnk" data-lr="lno|${x.i}">not a rule</button>`
        : `<span class="faint">${esc(x.where)}</span>`}</div>`).join("")}
      ${open1 && L.some((x) => x.st === "new" && !x.scope) ? '<div class="faint" style="font-size:12.5px">Pick how far each one applies, then confirm. Nothing joins a rulebook until you do.</div>' : ""}</div>`;
  }

  /* ---------- feedback (fbV) ---------- */
  function fb(d) {
    return `<div><h2>Feedback</h2><div class="sub">Everything flagged, overridden or noted in this job, and where it went. Overrides count even without a comment. To flag something, type the reason in the message box, then press its flag.</div></div>
      <div class="tw"><table><tr><th>Time</th><th>Kind</th><th>On</th><th>Reason</th><th>Routed to</th></tr>${d.items.map((x) =>
        `<tr><td class="mono">${esc(x.t)}</td><td>${esc(x.kind)}</td><td>${esc(x.ref)}</td><td style="white-space:normal">${x.text ? esc(x.text) : '<span class="faint">none given</span>'}</td><td class="mono">${esc(x.skill || x.routed || "job")}</td></tr>`).join("")
        || `<tr><td colspan="5" class="faint">${esc(d.empty)}</td></tr>`}</table></div>`;
  }

  /* ---------- rulebook (bookV) ---------- */
  function book(d) {
    const n = d.rules.length;
    return `<div style="display:grid;gap:6px"><div class="row"><b>${esc(d.sponsor)} rulebook</b><span class="faint">${n} rule${n === 1 ? "" : "s"} taught by people${d.pending.length ? `, ${d.pending.length} waiting for approval` : ""}. The organisation's rules from the process flows apply to every sponsor and are not repeated here.</span></div>
      <div class="tw"><table><tr><th>Rule</th><th>Taught by</th><th>From job</th><th>Added</th></tr>${d.rules.map((x) =>
        `<tr><td style="white-space:normal">${esc(x.text)}${x.status && x.status !== "active" ? ` ${pill("mute", x.status)}` : ""}</td><td>${esc(x.by)}</td><td>${esc(x.job)}</td><td class="mono">${esc(x.when)}</td></tr>`).join("")
        + d.pending.map((x) => `<tr><td style="white-space:normal">${esc(x.text)} ${pill("info", "Waiting for approval", "hour")}</td><td colspan="3" class="faint">${esc(x.evidence)}</td></tr>`).join("")
        || `<tr><td colspan="4" class="faint">${esc(d.empty)}</td></tr>`}</table></div></div>`;
  }

  const body = { recon, teach, fb, book };
  const summary = (part, d) => part === "recon"
    ? (d.empty ? "" : d.gl ? `${(d.by_ft[view[d.job]?.ft || d.default_ft] || {}).applies ?? ""} checks · ${d.checks.filter((c) => c.state === "run").length} run` : "rows in = rows out")
    : part === "fb" ? `${d.items.length} item${d.items.length === 1 ? "" : "s"}` : `${d.rules.length} rule${d.rules.length === 1 ? "" : "s"}`;

  function draw(el) {
    const m = el._lr;
    if (!m) return;
    const inner = body[m.part](m.data, m.job);
    if (m.part === "teach") {
      el.innerHTML = inner;
      return;
    }
    if (el.dataset.bare) {
      el.innerHTML = inner;
      return;
    }
    const f = FOLDS[m.part];
    el.innerHTML = `<details class="card fold" data-fold="${m.part}" ${open.has(m.part) ? "open" : ""}><summary><b>${f[0]}</b><span class="faint">${esc(summary(m.part, m.data) || f[1])}</span><span class="faint" style="margin-left:auto;font-size:12px">open</span></summary><div class="flat" style="display:grid;gap:12px">${inner}</div></details>`;
  }

  async function render(part, job, el) {
    if (!body[part] || !job || !el) return;
    el.dataset.lr = part;
    try {
      const data = await q(part, job.id);
      el._lr = { part, job: job.id, data };
      draw(el);
    } catch (e) {
      el.innerHTML = `<div class="faint">${esc(e.message)}</div>`;
    }
  }

  async function act(el, payload, okMsg) {
    const m = el._lr;
    try {
      await post({ job: m.job, ...payload });
      if (okMsg) say(okMsg);
    } catch (e) { say(e.message, true); return false; }
    // everything that depends on the learned list or the feedback log is fetched again
    document.querySelectorAll("[data-learning]").forEach(async (h) => {
      if (h._lr && h._lr.job === m.job) { try { h._lr.data = await q(h._lr.part, m.job); draw(h); } catch (e) { /* keep what is shown */ } }
    });
    return true;
  }

  document.addEventListener("toggle", (e) => {
    const d = e.target;
    if (d && d.dataset && d.dataset.fold) { if (d.open) open.add(d.dataset.fold); else open.delete(d.dataset.fold); }
  }, true);

  document.addEventListener("change", async (e) => {
    const t = e.target.closest("[data-lrf]");
    if (!t || !t.files || !t.files[0]) return;
    const host = t.closest("[data-learning]");
    if (!host || !host._lr) return;
    const f = t.files[0];
    try {
      await api(`/api/console/learning/report/${encodeURIComponent(host._lr.job)}/${encodeURIComponent(t.dataset.lrf)}?name=${encodeURIComponent(f.name)}`, { method: "PUT", body: f });
      say(`Loaded ${f.name}. The check has run against it.`);
      host._lr.data = await q(host._lr.part, host._lr.job);
      draw(host);
    } catch (err) { say(err.message, true); }
  });

  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-lr],[data-lfl]");
    if (!t) return;
    if (t.dataset.lfl !== undefined) {   // a flag on anything the worker chose
      const host = document.querySelector('[data-learning="fb"]');
      const job = host && host._lr ? host._lr.job : null;
      if (!job) return;
      const kind = t.dataset.k, ref = t.dataset.r, skill = t.dataset.s;
      const rb = document.getElementById("rb"), text = rb ? rb.value.trim() : "";
      post({ action: "flag", job, kind, ref, skill, text }).then(() => {
        if (rb && text) { rb.value = ""; rb.dispatchEvent(new Event("input", { bubbles: true })); }
        say(`Flagged ${ref}${text ? `: “${text}”` : ""}`);
        return q("fb", job).then((data) => { host._lr.data = data; draw(host); });
      }).catch((err) => say(err.message, true));
      return;
    }
    const host = t.closest("[data-learning]");
    if (!host || !host._lr) return;
    const [k, a, b] = t.dataset.lr.split("|");
    const vs = (view[host._lr.job] = view[host._lr.job] || {});
    if (k === "ft") { vs.ft = a; const c = host._lr.data.checks.find((x) => x.id === vs.sel); if (c && !c.scope.includes(a)) vs.sel = null; draw(host); }
    else if (k === "sel") { vs.sel = a; draw(host); }
    else if (k === "lscope") act(host, { action: "scope", i: +a, scope: b });
    else if (k === "lok") act(host, { action: "confirm", i: +a }, "Learned. It applies from the next job.");
    else if (k === "lno") act(host, { action: "reject", i: +a }, "Noted: not a rule.");
  });

  window.LEARNING = {
    render,
    flag: (kind, ref, skill) => `<button class="fl" data-lfl="1" data-k="${esc(kind)}" data-r="${esc(ref)}" data-s="${esc(skill || "")}" title="Flag this as wrong" aria-label="Flag ${esc(ref)}">⚑</button>`,
  };
})();
