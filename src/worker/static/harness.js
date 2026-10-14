"use strict";
/* Harness pages of the onboarding console: Improvements, Health, Skills, and a skill's decision tables.
   Markup and class names follow docs/mockup/onboarding-console.html (imprV, healthV, skillLibV, skillDetailV).
   Every figure comes from /api/console/harness/*; a page with nothing to show says so.
   Entry point for the shell:  window.HARNESS.render(page, el, {sponsor, skill})
   page: "improvements" | "health" | "skills" | "skill" (a skill's tables; opts.skill names it). */
(function () {
  const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const IC = {
    check: '<path d="M5 12l5 5 9-10"/>', x: '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
    warn: '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18v.5"/>', lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 018 0v3"/>',
  };
  const ic = (n) => `<svg viewBox="0 0 24 24" aria-hidden="true">${IC[n] || ""}</svg>`;
  const pill = (c, t, i) => `<span class="pill p-${c}">${i ? ic(i) : ""}${esc(t)}</span>`;
  const spark = (series, w = 96, h = 22) => {
    const v = series.map((p) => p.value);
    if (!v.length) return '<span class="faint">no keys decided yet</span>';
    const mx = Math.max(...v), mn = Math.min(...v), r = mx - mn || 1;
    const pts = v.map((x, i) => [v.length > 1 ? i * (w / (v.length - 1)) : w / 2, h - 2 - ((x - mn) / r) * (h - 4)]);
    const marks = series.map((p, i) => p.promoted ? `<circle cx="${pts[i][0]}" cy="${pts[i][1]}" r="2.6" style="fill:var(--ok);stroke:none"/>` : "").join("");
    return `<svg viewBox="0 0 ${w} ${h}" style="width:${w}px;height:${h}px;stroke:var(--navy);stroke-width:1.6" aria-hidden="true"><polyline fill="none" points="${pts.map((p) => p.join(",")).join(" ")}"/>${v.length === 1 ? `<circle cx="${pts[0][0]}" cy="${pts[0][1]}" r="2.2" style="fill:var(--navy);stroke:none"/>` : ""}${marks}</svg>`;
  };
  const num = (v, d) => (v === null || v === undefined ? "—" : d === undefined ? String(v) : Number(v).toFixed(d));
  const pc = (v) => (v === null || v === undefined ? "—" : v + "%");

  const st = new WeakMap(); // per host element: {page, opts, data, edits, tests, error}

  async function api(path, opts) {
    const r = await fetch(path, opts);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || body.detail || r.statusText);
    return body;
  }
  const post = (body) => api("/api/console/harness/act", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

  /* ---------- Improvements ---------- */
  function imprCard(c, d, sponsor) {
    const sp = c.sp || sponsor || "this sponsor";
    const border = c.status === "blocked" ? "border-color:var(--err)" : c.status === "ready" ? "border-color:var(--navy)" : "";
    const gates = c.gates ? `<div class="row" style="gap:6px">${c.gates.map((g) => pill(g[2] === "ok" ? "ok" : g[2] === "warn" ? "warn" : "err", `${g[0]}: ${g[1]}`, g[2] === "ok" ? "check" : g[2] === "warn" ? "warn" : "x")).join("")}</div>${c.pred ? `<div class="faint">Predicted: ${esc(c.pred)}. I'll compare it with what happens over the next 2 weeks.</div>` : ""}`
      : `<div class="sub">Collecting evidence before I propose a change: ${c.ev} of ${c.need} similar. Your reason helps me group them.</div>`;
    let buttons = "";
    if (c.status === "ready") {
      buttons = c.scope === "org"
        ? (d.admin ? `<button class="btn pri sm" data-im="promote|${esc(c.id)}">Promote for every sponsor</button>`
          : `<button class="btn sm" disabled title="An organisation change is promoted by an admin">Needs an admin to promote</button>`)
        : `<button class="btn pri sm" data-im="promote|${esc(c.id)}">Promote for ${esc(sp)}</button>`;
    }
    if (c.status === "blocked") buttons = `<span class="sub">Blocked: ${esc((c.reasons || []).join("; ") || "a gate failed")}. Change it, or reject.</span>`;
    if (c.status !== "waiting") buttons += `<button class="btn sm" data-im="reject|${esc(c.id)}">Reject</button>`;
    return `<div class="card" style="display:grid;gap:10px;${border}">
  <div class="row between"><div class="row"><b>${esc(c.cause)}</b></div><div class="row">${c.scope === "org" ? '<span class="lab">ORGANISATION · ADMIN</span>' : `<span class="lab s">${esc(sp)}</span>`}<span class="lab">${esc(c.kind.toUpperCase())}</span></div></div>
  <div class="faint mono" style="font-size:12px">${esc(c.skill)} · evidence ${c.ev}${c.need ? ` of ${c.need}` : ""}</div>
  ${c.before ? `<div class="tiles" style="grid-template-columns:repeat(auto-fit,minmax(240px,1fr))"><div class="note"><div class="eyebrow">Now</div>${esc(c.before)}</div><div class="note n-ok"><div class="eyebrow">Proposed</div>${esc(c.after)}</div></div>` : ""}
  ${gates}
  <div class="row">${buttons}</div></div>`;
  }
  function imprV(d, o) {
    const group = (title, list) => list.length ? `<div class="eyebrow">${title} · ${list.length}</div>${list.map((c) => imprCard(c, d, o.sponsor)).join("")}` : "";
    const hist = d.history.map((p) => `<tr><td class="mono">${p.version != null ? (p.surface === "table_row" ? "table v" : "golden v") + p.version : "—"}</td><td style="white-space:normal">${esc(p.cause)}</td><td>${p.scope === "org" ? "organisation" : esc(p.sp || "sponsor")}</td><td>${p.status === "rolled_back" ? pill("mute", "Rolled back", "x") : p.status === "rejected" ? pill("mute", "Rejected", "x") : pill("ok", "Live", "check")}</td><td>${p.can_rollback ? `<button class="btn sm" data-im="rollback|${esc(p.id)}|${esc(p.surface)}">Roll back</button>` : ""}</td></tr>`).join("");
    const none = !d.ready.length && !d.blocked.length && !d.waiting.length;
    return `<div><h1>Improvements</h1><p class="sub">I group your feedback by cause and propose the smallest change to one skill or rule. Every change is replayed on recorded jobs and checked against held-out work before you see it. I can never approve my own change, and finished work never changes.</p></div>
 ${group("Ready for your decision", d.ready)}${group("Blocked by a gate", d.blocked)}${group("Collecting evidence", d.waiting)}
 ${none ? '<div class="card sub">Nothing is waiting for a decision. Improvements appear here when corrections repeat, or when someone proposes a table change from Skills.</div>' : ""}
 <div class="card" style="display:grid;gap:8px"><h2>History</h2><div class="tw"><table><tr><th>Version</th><th>Change</th><th>Scope</th><th>Status</th><th></th></tr>${hist || '<tr><td colspan="5" class="faint">Nothing has been promoted yet.</td></tr>'}</table></div></div>`;
  }

  /* ---------- Health ---------- */
  function chatTracesV(d) {   // one row per job-chat message the model read: no sentence or answer text is kept
    const rows = (d.chat_traces || []).map((t) => `<tr><td class="mono">${esc(t.trace)}</td><td>${esc(String(t.at).replace("T", " ").slice(0, 19))}</td><td class="mono">${esc(t.model)}${t.escalated ? " " + pill("warn", "escalated") : ""}</td>
  <td>${pill(t.outcome === "read" ? "ok" : "warn", t.outcome === "read" ? "read" : t.outcome === "refused" ? "refused, nothing changed" : "no answer")}${t.problems.length ? `<div class="faint" style="font-size:11.5px">${esc(t.problems[0])}</div>` : ""}</td>
  <td class="n">${t.attempts}</td><td class="n">${t.tokens}</td><td class="n">${t.seconds} s</td></tr>`).join("");
    return `<div class="card" style="display:grid;gap:8px"><h2>Chat messages</h2><div class="faint" style="font-size:12.5px">Each message you type in a job chat is read by the model once, or twice when its first answer is refused. The sentence and the answer are not stored here; the record holds the checksum, the model and the outcome.</div>
 <div class="tw"><table><tr><th>Trace</th><th>When</th><th>Model</th><th>Outcome</th><th class="n">Tries</th><th class="n">Tokens</th><th class="n">Time</th></tr>${rows || '<tr><td colspan="7" class="faint">No chat message has been read by the model yet.</td></tr>'}</table></div></div>`;
  }

  function healthV(d) {
    if (d.empty) return `<div><h1>Health</h1><p class="sub">How well I know the rules, week by week.</p></div><div class="card sub">${esc(d.empty)}</div>${chatTracesV(d)}`;
    const cost = (r) => d.priced && r.cost_per_successful_job !== null
      ? `<b>$${Number(r.cost_per_successful_job).toFixed(2)}</b><div class="faint" style="font-size:11.5px">review $${Number(r.cost_review).toFixed(2)} · model $${Number(r.cost_model).toFixed(3)} · ${r.retries_per_job} retries/job</div>`
      : d.priced ? '<span class="faint">no successful job yet</span>'
        : `<span class="faint" title="Set WORKER_PRICE_IN_PER_M, WORKER_PRICE_OUT_PER_M and WORKER_REVIEWER_RATE to price jobs">no prices set</span>`;
    const rows = d.rows.map((r) => `<tr class="hov"><td><b>${esc(r.workflow)}</b><div class="faint" style="font-size:11.5px">${r.jobs} job${r.jobs === 1 ? "" : "s"} · ${r.rows} rows</div></td>
  <td><div class="row" style="flex-wrap:nowrap;gap:8px">${spark(r.readiness)}${r.readiness.length ? `<b>${r.readiness[r.readiness.length - 1].value}%</b>` : ""}</div></td>
  <td class="n">${num(r.overrides_per_1000)}</td><td class="n">${num(r.questions_per_1000)}</td><td class="n">${pc(r.decided_without_model)}</td><td class="n">${pc(r.first_pass)}</td><td class="n">${r.upload_rejections}</td><td class="n">${num(r.model_calls_per_1000)}</td><td class="n">${pc(r.success_rate)}</td>
  <td class="n" title="(model spend incl. retries + reviewer time) ÷ jobs approved with no revision and no upload rejection">${cost(r)}</td>
  <td class="n">${r.seconds_per_1000_p50 === null ? "—" : `${Number(r.seconds_per_1000_p50).toFixed(1)} s / ${Number(r.seconds_per_1000_p95).toFixed(1)} s`}</td></tr>`).join("");
    const watch = d.watch.map((w) => {
      const lines = [`<tr><td class="mono">${esc(w.skill)}</td><td>Loaded but not needed / needed but not loaded</td><td>${w.loaded_not_needed} / ${w.needed_not_loaded} · <span class="faint">${w.loaded} loaded, ${w.needed} needed</span></td></tr>`];
      if (w.trend) lines.push(`<tr><td class="mono">${esc(w.skill)}</td><td>Keys at L0 or L1 (no person, no model)</td><td><div class="row" style="flex-wrap:nowrap;gap:8px">${spark(w.trend)}${w.trend.length ? `<b>${w.trend[w.trend.length - 1].value}%</b>` : ""}</div></td></tr>`);
      return lines.join("");
    }).join("");
    return `<div><h1>Health</h1><p class="sub">How well I know the rules, week by week. <b>Readiness</b> is the share of account keys decided by approved rules instead of by people. Green dots mark a promoted improvement. Computed from ${d.jobs} finished job${d.jobs === 1 ? "" : "s"}.</p></div>
 <div class="card tw"><table><tr><th>Workflow</th><th>Readiness · ${d.weeks.length} week${d.weeks.length === 1 ? "" : "s"}</th><th class="n">Overrides / 1k rows</th><th class="n">Questions / 1k rows</th><th class="n">Decided without model</th><th class="n">First-pass tie-out</th><th class="n">Upload rejections</th><th class="n">Model calls / 1k rows</th><th class="n">Success rate</th><th class="n">Cost per successful job</th><th class="n">p50 / p95 per 1k rows</th></tr>${rows}</table></div>
 <div class="faint" style="font-size:12.5px">A <b>successful job</b> is approved without “needs revision” and with no upload rejection. Cost per successful job = (model spend, failed and retried runs included, + reviewer time${d.priced ? ` at $${d.reviewer_rate}/hour` : ""}) ÷ successful jobs. Reviewer time is usually the biggest part, so fewer questions per 1,000 rows lowers it fastest.</div>
 <div class="card" style="display:grid;gap:8px"><h2>Skills to watch</h2><div class="tw"><table><tr><th>Skill</th><th>Signal</th><th>Trend</th></tr>${watch || '<tr><td colspan="3" class="faint">No skill has been loaded or needed in a finished job yet.</td></tr>'}</table></div></div>${chatTracesV(d)}`;
  }

  /* ---------- Skills ---------- */
  function skillLibV(d) {
    const rows = d.skills.map((s) => `<tr class="hov"><td class="mono"><button class="lnk mono" data-sk="${esc(s.name)}">${esc(s.stamp)}</button></td><td style="white-space:normal">${esc(s.description)}</td><td>${s.tables.length ? esc(s.tables.join(", ")) : '<span class="faint">none</span>'}</td><td class="n">${s.cases}</td><td class="n">${s.jobs_used}</td></tr>`).join("");
    return `<div><h1>Skills</h1><p class="sub">Everything I know how to do, as versioned skills. Open a skill to change its rules without a developer. Every change is tested on recorded jobs and approved in Improvements before it goes live.</p></div>
 <div class="card tw"><table><tr><th>Skill</th><th>Description (what the model sees until it needs the skill)</th><th>Decision tables</th><th class="n">Test cases</th><th class="n">Jobs used</th></tr>${rows || '<tr><td colspan="5" class="faint">No skills are installed.</td></tr>'}</table></div>`;
  }
  const tkey = (s, t) => `${s}|${t}`;
  function liveRows(t) { return t.rows.map((r) => ({ id: r.id, when: { ...r.when }, then: { ...r.then } })); }
  function skillDetailV(d, s, o) {
    const ts = d.tables.map((t) => {
      const k = tkey(d.name, t.id), rows = s.edits[k] || liveRows(t), dirty = !!s.edits[k], res = s.tests[k];
      const head = `<th class="n">#</th><th>Row</th>${Object.keys(t.inputs).map((x) => `<th>${esc(x)}</th>`).join("")}${t.outputs.map((x) => `<th>→ ${esc(x)}</th>`).join("")}<th></th>`;
      const body = rows.map((r, i) => `<tr><td class="n">${i + 1}</td>
   <td><input data-dm="${esc(k)}|${i}|id|id" value="${esc(r.id)}" aria-label="${esc(t.id)} row ${i + 1} id" style="width:100%;min-width:60px;min-height:28px;padding:2px 8px" class="mono"></td>
   ${Object.keys(t.inputs).map((x) => `<td><input data-dm="${esc(k)}|${i}|when|${esc(x)}" value="${esc(r.when[x] ?? "")}" placeholder="any" aria-label="${esc(t.id)} row ${i + 1} ${esc(x)}" style="width:100%;min-width:90px;min-height:28px;padding:2px 8px"></td>`).join("")}
   ${t.outputs.map((x, j) => `<td><input data-dm="${esc(k)}|${i}|then|${esc(x)}" value="${esc(r.then[x] ?? "")}" aria-label="${esc(t.id)} row ${i + 1} ${esc(x)}" style="width:100%;min-width:110px;min-height:28px;padding:2px 8px;${j === 0 ? "font-weight:600" : ""}"></td>`).join("")}
   <td>${rows.length > 1 ? `<button class="fl" data-dmx="del|${esc(k)}|${i}" aria-label="Remove row ${i + 1}">✕</button>` : ""}</td></tr>`).join("");
      let result = "";
      if (res) {
        if (!res.replayable) result = `<span class="sub">${esc(res.message)}</span>`;
        else {
          const f = res.fixtures, r = res.recorded;
          const pills = [pill(f.broke.length ? "err" : "ok", `${f.pass_after} of ${f.total} reference cases pass`, f.broke.length ? "x" : "check"),
            pill(r.cases ? "ok" : "mute", r.cases ? `Fixes ${r.fixes} of ${r.cases} recorded cases` : "No recorded cases from sponsors yet", r.cases ? "check" : ""),
            pill(r.signed_off_changed ? "err" : "ok", `${r.signed_off_changed} signed-off outputs changed`, r.signed_off_changed ? "x" : "check"),
            pill(r.regressions || f.broke.length ? "err" : "ok", r.regressions || f.broke.length ? `regression: ${[...f.broke].join("; ") || r.regressions + " recorded"}` : "no regression", r.regressions || f.broke.length ? "x" : "check")];
          if (!res.changed) pills.push(pill("mute", "Nothing differs from the live table"));
          result = `<span class="row" style="gap:4px">${pills.join("")}</span>${res.passed ? `<button class="btn sm pri" data-dmx="prop|${esc(k)}|sponsor">Propose for ${esc(o.sponsor || "this sponsor")}</button><button class="btn sm" data-dmx="prop|${esc(k)}|organisation">Propose for every sponsor</button>` : ""}`;
        }
      }
      return `<div class="card" style="display:grid;gap:8px"><div class="row between"><div class="row"><b>${esc(t.id)}</b><span class="lab">FIRST MATCH WINS</span></div>${dirty ? pill("warn", "Edited, not tested", "warn") : ""}</div>
  ${t.description ? `<div class="sub">${esc(t.description)}</div>` : ""}
  <div class="tw"><table><tr>${head}</tr>${body}</table></div>
  <div class="row"><button class="btn sm" data-dmx="add|${esc(k)}">+ Add a row</button><button class="btn sm pri" data-dmx="test|${esc(k)}">Test</button>${dirty ? `<button class="btn sm" data-dmx="reset|${esc(k)}">Discard edits</button>` : ""}${result}</div></div>`;
    }).join("");
    const checks = d.checks.length ? `<div class="card" style="display:grid;gap:8px"><b>Checks this skill applies (read only)</b><div class="tw"><table><tr><th>Rule</th><th>Severity</th><th>Family</th><th>Message</th></tr>${d.checks.map((c) => `<tr><td class="mono">${esc(c.id)}</td><td>${esc(c.severity)}</td><td>${esc(c.family)}</td><td style="white-space:normal">${esc(c.message)}</td></tr>`).join("")}</table></div></div>` : "";
    return `<button class="lnk" style="justify-self:start" data-sk="">‹ All skills</button>
 <div><h1 style="font-family:var(--mono)">${esc(d.stamp)}</h1><p class="sub">${esc(d.description)}</p></div>
 <div class="note n-info">You can change this skill's rules without a developer. Edit a table and press <b>Test</b>: I replay the reference cases and every recorded job. If it passes, propose it, and it goes live after approval in Improvements. Only new kinds of logic (scripts) need a developer.</div>
 ${ts}${checks}
 ${!d.tables.length ? '<div class="card sub">This skill has no editable tables. Its behaviour is in its checks and scripts, so a change here needs a developer.</div>' : ""}`;
  }

  /* ---------- plumbing ---------- */
  async function load(el) {
    const s = st.get(el), o = s.opts;
    const q = s.page === "skill" ? `?name=${encodeURIComponent(o.skill || "")}` : s.page === "improvements" ? `?sponsor=${encodeURIComponent(o.sponsor || "")}` : "";
    try { s.data = await api(`/api/console/harness/${s.page}${q}`); s.error = ""; } catch (e) { s.data = null; s.error = e.message; }
    paint(el);
  }
  function paint(el) {
    const s = st.get(el);
    const banner = s.error ? `<div class="note n-err" role="alert">${esc(s.error)}</div>` : "";
    let html = "";
    if (s.data) {
      html = s.page === "improvements" ? imprV(s.data, s.opts) : s.page === "health" ? healthV(s.data)
        : s.page === "skills" ? skillLibV(s.data) : skillDetailV(s.data, s, s.opts);
    }
    el.innerHTML = banner + html;
  }
  function wire(el) {
    el.addEventListener("click", async (ev) => {
      const b = ev.target.closest("[data-im],[data-sk],[data-dmx]");
      if (!b || !el.contains(b)) return;
      const s = st.get(el);
      try {
        if (b.dataset.sk !== undefined) {
          s.page = b.dataset.sk ? "skill" : "skills"; s.opts = { ...s.opts, skill: b.dataset.sk };
          return load(el);
        }
        if (b.dataset.im) {
          const [a, id, surface] = b.dataset.im.split("|");
          await post(a === "rollback" ? { action: "rollback", surface } : { action: a, card: id });
          s.error = ""; return load(el);
        }
        const [a, skill, table, x] = b.dataset.dmx.split("|");
        const k = tkey(skill, table);
        const t = s.data.tables.find((q) => q.id === table);
        const rows = s.edits[k] || liveRows(t);
        if (a === "add") {
          const blank = { id: "new-" + (rows.length + 1), when: {}, then: { ...rows[rows.length - 1].then } };
          const next = rows.slice(0, -1).concat([blank], rows.slice(-1)); // before the catch-all
          s.edits[k] = next; delete s.tests[k];
        } else if (a === "del") { s.edits[k] = rows.filter((_, i) => i !== Number(x)); delete s.tests[k]; }
        else if (a === "reset") { delete s.edits[k]; delete s.tests[k]; }
        else if (a === "test") { s.tests[k] = await post({ action: "test", skill, table, rows }); s.error = ""; }
        else if (a === "prop") {
          const r = await post({ action: "propose", skill, table, rows, scope: x, sponsor: s.opts.sponsor || "" });
          delete s.edits[k]; delete s.tests[k]; s.error = "";
          s.page = "improvements"; await load(el);
          s.error = `Filed ${r.cards.length} card${r.cards.length === 1 ? "" : "s"}: ${r.status.join(", ")}.`; return paint(el);
        }
        paint(el);
      } catch (e) { s.error = e.message; paint(el); }
    });
    el.addEventListener("input", (ev) => {
      const i = ev.target.closest("[data-dm]");
      if (!i) return;
      const s = st.get(el), [skill, table, idx, part, name] = i.dataset.dm.split("|");
      const k = tkey(skill, table), t = s.data.tables.find((q) => q.id === table);
      const rows = s.edits[k] || (s.edits[k] = liveRows(t));
      if (part === "id") rows[Number(idx)].id = i.value; else rows[Number(idx)][part][name] = i.value;
      delete s.tests[k];
      const pillEl = i.closest(".card").querySelector(".pill.p-warn");
      if (!pillEl) { // first edit of this table: repaint once to show the "Edited" pill, keeping the caret
        paint(el);
        const again = el.querySelector(`[data-dm="${CSS.escape(i.dataset.dm)}"]`);
        if (again) { again.focus(); again.setSelectionRange(i.value.length, i.value.length); }
      }
    });
  }
  function render(page, el, opts) {
    const p = { impr: "improvements", improvements: "improvements", health: "health", skills: "skills", skill: "skill" }[page];
    if (!p) throw new Error(`no harness page ${page}`);
    const o = opts || {};
    let s = st.get(el);
    if (!s) { s = { edits: {}, tests: {} }; st.set(el, s); wire(el); }
    s.page = p; s.opts = { ...o, skill: o.skill || (s.opts && s.opts.skill) || "" };
    return load(el);
  }
  window.HARNESS = { render };
})();
