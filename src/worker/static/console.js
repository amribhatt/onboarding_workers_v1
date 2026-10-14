"use strict";
/* Onboarding console: the job screen. A thin renderer over /api/console/state and /api/console/act.
   Layout and class names follow docs/mockup/onboarding-console.html. Every number comes from the server. */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const nf = (n) => Number(n).toLocaleString("en-US");
const IC = {
  check: '<path d="M5 12l5 5 9-10"/>', x: '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
  grid: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 10v10"/>',
  bulb: '<path d="M9 18h6M10 21h4M12 3a6 6 0 00-4 10.5c.8.8 1 1.5 1 2.5h6c0-1 .2-1.7 1-2.5A6 6 0 0012 3z"/>',
  chat: '<path d="M4 5h16v11H9l-5 4z"/>', search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>',
  up: '<path d="M12 16V4M7 9l5-5 5 5M4 20h16"/>', lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 018 0v3"/>',
  bldg: '<rect x="5" y="3" width="14" height="18" rx="1"/><path d="M9 7h2M13 7h2M9 11h2M13 11h2"/>',
  dash: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  inbox: '<path d="M4 13l2-8h12l2 8v6H4z"/><path d="M4 13h5l1 3h4l1-3h5"/>',
  hour: '<path d="M6 3h12M6 21h12M7 3c0 6 10 6 10 12v6M17 3c0 6-10 6-10 12v6"/>',
  book: '<path d="M5 4h12a2 2 0 012 2v14H7a2 2 0 01-2-2z"/>', swap: '<path d="M4 8h14l-3-3M20 16H6l3 3"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3"/>', chev: '<path d="M6 9l6 6 6-6"/>',
  warn: '<path d="M12 4l9 16H3z"/><path d="M12 10v4"/>', dot: '<circle cx="12" cy="12" r="3"/>',
  pulse: '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
};
const ic = (n) => `<svg viewBox="0 0 24 24" aria-hidden="true">${IC[n] || ""}</svg>`;
const pill = (c, t, i) => `<span class="pill p-${c}">${i ? ic(i) : ""}${esc(t)}</span>`;

let S = null;          // the last state from the server
let collapsed = true;  // the worker chat is closed: a launcher at the bottom right (open: an overlay, or docked when pinned)
let pinned = false;    // the chat docked beside the work (remembered)
let navX = false;      // the left navigation as icons only (remembered)
let navO = false;      // the navigation open as an overlay on a narrow screen
const LAYK = "onb.layout";
(function layLoad() {
  let L = null;
  try { L = JSON.parse(localStorage.getItem(LAYK) || "null"); } catch (e) { /* storage blocked: defaults */ }
  navX = L && "nav" in L ? !!L.nav : false;
  pinned = !!(L && L.pin);
  collapsed = !pinned;   // closed unless the person pinned it
})();
function laySave() { try { localStorage.setItem(LAYK, JSON.stringify({ nav: +navX, pin: +pinned })); } catch (e) { /* not remembered */ } }
/* The worker chat is one shell component, enabled on these views only (a one-line change turns it on elsewhere). */
const CHAT_VIEWS = ["job"];
/* the chat belongs to a job or a new-job draft wherever the shell view says it is: a job on screen always has its chat */
const chatHere = () => !!(S && (S.job || S.ask));
let lastMsgKey = "";   // the last chat message seen, to bring a new one into view
let exAuto = "";      // the job whose pending question already opened the chat once
let draft = "";        // the chat box
let toastTimer = null;
let swPop = false;     // the sponsor switcher is open
let onb = null;        // the onboarding modal: {mode, sp, list, file, read, err, skipped}
const std = { g: "All", coaQ: "", ttQ: "" };  // the standards pages' filters
let mdTest = null;     // last model test result
let att = { busy: false, err: "", name: "" };  // the New job screen's attach control
let qsel = 0;          // the selected question card (j / k)
/* EXTENSION POINT (pages): a page module sets PAGES[view] = () => html to add a screen to the shell. */
const PAGES = (window.PAGES = window.PAGES || {});

/* the approver token (demo-grade, this tab only): sent on every same-origin /api call when one is set, so the console
   works where the service requires it (WORKER_REQUIRE_TOKEN=1). Typed once in Settings; never written to disk. */
const approverToken = () => { try { return sessionStorage.getItem("token") || ""; } catch (e) { return ""; } };
const nativeFetch = window.fetch.bind(window);
window.fetch = (path, opts = {}) => {
  const t = approverToken();
  if (!t || typeof path !== "string" || !path.startsWith("/api/")) return nativeFetch(path, opts);
  return nativeFetch(path, { ...opts, headers: { ...(opts.headers || {}), "X-Approver-Token": t } });
};
async function api(path, opts) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (r.status === 401 && S && S.login !== false && !path.startsWith("/api/auth/")) { location.href = "/login"; }
  if (!r.ok) throw new Error(body.error || body.detail || r.statusText);
  return body;
}
/* who is deciding: self-declared and demo-grade, kept the way index.html keeps it (key "reviewer") */
const store = {
  get(k, d = "") { try { return sessionStorage.getItem(k) ?? localStorage.getItem(k) ?? d; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage blocked: the name lives in the field only */ } },
};
let reviewerName = "";
/* stage ids stay as stored; people read Validate and Download */
const STAGE_LABEL = { Check: "Validate", Write: "Download" };
const stageLabel = (s) => STAGE_LABEL[s] || s;
/* the account the console runs under is the declared name unless the person typed another in Settings */
/* signed in: the session's name, always (a typed or stored name is ignored, and the server ignores X-Reviewer too);
   no sign-in (the offline demo): the name the person typed */
const signedIn = () => typeof S !== "undefined" && !!(S && S.login && S.me);
const reviewer = () => (signedIn() ? S.me : (reviewerName || store.get("reviewer") || "")).trim();
const who = () => { const r = reviewer(); return r ? { "X-Reviewer": r } : {}; };
/* where the person is, sent with what they say, so the worker answers about this screen */
function chatContext() {
  const g = (window.RECON && RECON.group()) || (window.ISSUES && ISSUES.state.review ? ISSUES.state.review.gid : "");
  return { job: S.job ? S.job.id : "", group: g };
}
async function act(action, text = "") {
  swPop = false;
  try {
    S = await api("/api/console/act", { method: "POST", headers: { "Content-Type": "application/json", ...who() },
      body: JSON.stringify({ action, text, context: action === "say" ? chatContext() : {} }) });
  } catch (e) { toast(e.message, true); return false; }
  render();
  return true;
}
function toast(msg, bad) {
  const t = $("toast");
  t.hidden = false; t.className = "toast"; t.innerHTML = `${ic(bad ? "x" : "bulb")}<span>${esc(msg)}</span>`;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 6000);
}
const live = (m) => { $("live").textContent = m; };

/* ---------- side: the mockup's left nav, counts from the server ---------- */
function side() {
  const N = S.pages.nav, v = S.view;
  const nv = (view, i, t, c) => `<button class="nav ${v === view ? "on" : ""}" data-act="view|${esc(view)}">${ic(i)}${esc(t)}${c ? `<span class="c">${c}</span>` : ""}</button>`;
  const onSponsor = v === "sponsor" || v === "job";
  const pinned = N.pinned.fund ? `${esc(N.pinned.fund)}${N.pinned.type ? "<br>" + esc(N.pinned.type) : ""}` : "";
  /* EXTENSION POINT (nav): the Harness group. Entries come from pages.nav.harness as {view, label, icon, count};
     the page for each view is registered in PAGES. */
  const harness = N.harness.length ? `<div class="grp">Harness</div>${N.harness.map((h) => nv(h.view, h.icon || "dot", h.label, h.count)).join("")}` : "";
  return `<div style="position:relative"><button class="switch" data-sw="1">${ic("bldg")}<span style="flex:1;text-align:left">${onSponsor ? esc(S.sponsor) : "Choose or onboard"}</span>${ic("chev")}</button>
  ${swPop ? `<div class="pop"><button class="dashbox" data-ob="new"><b>+ Onboard sponsors</b><div class="sub">Sponsor ID, name and go-live date. Type one or upload a list</div></button>
    ${N.sponsors.map((n) => `<button class="nav" data-act="switch|${esc(n)}">${esc(n)}</button>`).join("")}</div>` : ""}</div>
  ${nv("dash", "dash", "Dashboard")}${nv("pending", "inbox", "Pending approval", N.pending)}${nv("rejected", "x", "Rejected", N.rejected)}${nv("aging", "hour", "Aging", N.aging)}
  <div class="grp">All-sponsor standards</div>${nv("tmpls", "grid", "Ledgerline templates")}${nv("coa", "book", "Golden COA template")}${nv("ttm", "swap", "Transaction type mapping")}
  ${harness}
  <div class="grp">Pinned</div><div class="faint" style="padding:4px 10px">${pinned || "nothing open"}</div><div style="flex:1"></div>${nv("settings", "gear", "Settings")}${S.login && S.me ? `<div class="row" id="authNote" style="padding:4px 10px;font-size:12px;gap:8px"><span class="faint">Signed in as <b>${esc(S.me)}</b></span><button class="lnk" data-signout="1">Sign out</button></div>` : '<div class="faint" id="authNote" style="padding:4px 10px;font-size:11px">Identity is self-declared; this is not production authentication.</div>'}`;
}

/* ---------- timeline ---------- */
function timeline(tl) {
  const cls = ["todo", "done", "now", "stop"];
  const said = ["not started", "done", "in progress", "stopped"];   // the colour and the tick say it to the eye; this says it to a screen reader
  return `<ol class="tl" aria-label="Progress">${tl.map((x, i) => `<li class="${cls[x.state]}"${x.state === 2 ? ' aria-current="step"' : ""}>
    <span class="d">${x.state === 1 ? ic("check") : x.state === 3 ? ic("x") : i + 1}</span><b>${esc(stageLabel(x.stage))}<span class="sr">, ${said[x.state] || ""}</span></b>
    <small>${esc(x.fact) || "&nbsp;"}</small></li>`).join("")}</ol>`;
}

/* ---------- the start conversation: what I understood so far ---------- */
function draftMain(a) {
  const none = '<span class="faint">not yet</span>';
  const types = S.types.map((t) => `<option ${t === a.type ? "selected" : ""}>${esc(t)}</option>`).join("");
  const gl = a.type && /^GL/.test(a.type);
  const ents = a.entities.map((e) => `<option ${e === a.fund ? "selected" : ""}>${esc(e)}</option>`).join("");
  return `<div class="card" style="display:grid;gap:14px"><div class="row between"><h2>Request</h2>
   <span class="faint" style="font-size:12.5px">fills in as you talk to the worker · change anything here</span></div>
   <div class="kv">
    <span class="k">Record type</span><span><select data-set="type" aria-label="Record type">${a.type ? "" : '<option value="" selected disabled>choose…</option>'}${types}</select></span>
    <span class="k">Entities</span><span class="row">${!a.type ? none : gl
      ? `<select data-set="fund" aria-label="Entity">${ents}</select>`
      : `<select data-set="cov" aria-label="Which entities the file covers"><option value="" disabled ${a.cov ? "" : "selected"}>which entities?</option>
         <option value="all" ${a.cov === "all" ? "selected" : ""}>all ${a.entities.length} entities</option>
         <option value="one" ${a.cov === "one" ? "selected" : ""}>one entity</option>
         <option value="some" ${a.cov === "some" ? "selected" : ""}>some entities</option></select>${a.cov === "one" ? `<select data-set="fund">${ents}</select>` : ""}
         ${a.cov === "some" ? a.entities.map((e) => `<label class="row" style="gap:4px"><input type="checkbox" style="min-height:0" data-ent="${esc(e)}" ${a.ents.includes(e) ? "checked" : ""}>${esc(e)}</label>`).join("") : ""}`}</span>
    <span class="k">Source file</span><span>${a.file ? `<span class="mono">${esc(a.file)}</span>` : `<button class="lnk" data-act="ask_sample">use a sample</button>`}</span>
    <span class="k">Template</span><span>${a.template ? `<span class="mono">${esc(a.template)}</span><span class="lab">STANDARD</span>` : none}</span>
    <span class="k">Route</span><span>${a.route ? esc(a.route) : none}</span></div>
   <div class="uplo" data-drop="attach" style="padding:14px">${att.busy ? `Reading <span class="mono"><b>${esc(att.name)}</b></span>…`
     : `${ic("up")} Attach your file: drop an Excel workbook (.xlsx) here, <button class="lnk" data-att="browse">choose it</button>${a.file ? ` · <span class="mono">${esc(a.file)}</span> is attached` : ""}`}</div>
   <input id="attfile" type="file" accept=".xlsx,.xlsm" style="position:absolute;left:-9999px" tabindex="-1" aria-label="Attach your file">
   ${att.err ? `<div class="note n-err" role="status">${esc(att.err)}</div>` : ""}
   ${a.blocked ? `<div class="note n-warn" role="status">${ic("lock")} ${esc(a.blocked)}</div>` : ""}
   <div class="row">${a.ready ? '<button class="btn pri" data-act="ask_go">Start →</button><span class="faint">Nothing is written until you approve.</span>'
     : `<span class="faint">Still needed: ${esc(a.need.join(", ") || "see above")}</span>`}</div></div>`;
}

/* a flag on anything the worker chose (learning.js); empty when that script is not loaded */
const flagBtn = (kind, ref, skill) => (window.LEARNING ? window.LEARNING.flag(kind, ref, skill) : "");

/* ---------- plan ---------- */
/* what a recipe decides for a field, in words: shown so a saved decision is never silent */
const OPS = { date_parse: "read as a date", number_parse: "read as a number", strip: "trimmed", upper: "upper-cased", lower: "lower-cased" };
function appliedText(f, j) {
  const r = f.recipe, where = r.scope === "sponsor" ? `your saved rule for ${j.sponsor}` : "your choice for this job";
  const what = r.source === "from" ? `taken from “${r.header}”${(r.ops || []).length ? ", " + r.ops.map((o) => OPS[o] || String(o).replace(/_/g, " ")).join(", ") : ""}`
    : r.source === "supply" ? `set to “${r.value}”` : "not in this file, left blank and reported";
  return `Applied: ${what} · ${where}`;
}
/* the ID field's dropdown is the same on every template: the file's columns, plus the ways to generate IDs */
function idGenSelect(f, heads) {
  const cols = heads || f.columns || [], src = f.kind === "generated" ? "" : f.source;
  const gen = [["sequential", "Generate new IDs"]];   // prefix and numbering are set in the card; a custom format is one link away
  return `<select data-col="${esc(f.field)}" aria-label="Where ${esc(f.field)} comes from">
    ${gen.map(([m, t]) => `<option value="@gen|${m}" ${f.kind === "generated" ? "selected" : ""}>${esc(t)}</option>`).join("")}
    <option value="" ${!src && f.kind !== "generated" ? "selected" : ""}>not in this file, left blank and reported</option>
    ${cols.map((c) => { const [v, why] = (f.col_fit || {})[c] || ["ok", ""]; return `<option value="${esc(c)}" ${c === src ? "selected" : ""} ${v === "no" ? "disabled" : ""}>${esc(c)}${v === "no" ? ` (not an ID: ${esc(why)})` : v === "names" ? " (looks like names)" : ""}</option>`; }).join("")}</select>
    <div data-idgen="1"></div>`;
}
function planCard(j) {
  const p = j.plan;
  const conf = (f) => f.kind === "constant" ? pill("ok", "Fixed", "check") : f.kind === "generated" ? pill("ok", "Code", "check")
    : f.kind === "user" ? pill("info", "Set by you", "check") : f.kind === "sure" ? pill("ok", f.confidence + "%", "check")
    : f.kind === "glance" ? pill("warn", f.confidence + "% · glance at it") : f.required ? pill("err", "Required · not in the file", "x") : pill("mute", "Missing");
  /* the dropdown offers the file's real headers when the job view carries them */
  const heads = [j.headers, p.headers].find((h) => Array.isArray(h) && h.length) || null;
  const table = p.gl
    ? `<div class="tw"><table><tr><th>Ledgerline field</th><th>Comes from</th><th>Confidence</th></tr>${p.fields.map((m) =>
        `<tr><td class="mono">${esc(m.field)}${flagBtn("mapping", m.field, "detect-columns")}</td><td>${esc(m.from)}</td><td>${m.confidence === "H" ? pill("ok", "High", "check") : m.confidence === "M" ? pill("warn", "Medium · glance at it") : pill("mute", "Low · optional")}</td></tr>`).join("")}</table></div>`
    : `<div class="tw"><table><tr><th>Template field</th><th>Required?</th><th>Comes from</th><th>Confidence</th></tr>${p.fields.map((f) => `<tr>
        <td class="mono">${esc(f.field)}${flagBtn("mapping", f.field, "detect-columns")}</td>
        <td><input type="checkbox" style="min-height:0" data-req="${esc(f.field)}" ${f.required ? "checked" : ""} aria-label="${esc(f.field)} required for ${esc(j.sponsor)}">${f.differs ? `<span class="lab j">${esc(j.sponsor.toUpperCase())}</span>` : ""}</td>
        <td>${f.kind === "constant" ? `“${esc(f.constant)}” <span class="faint">sponsor setup</span>` : f.id_field ? idGenSelect(f, heads) : f.kind === "generated" ? esc(f.id_text)
          : `${f.recipe ? `<div class="note n-ok" style="font-size:12.5px;margin-bottom:4px">${esc(appliedText(f, j))}</div>` : ""}<select data-col="${esc(f.field)}" aria-label="Source column for ${esc(f.field)}"><option value="" ${(f.recipe ? f.recipe.header : f.source) ? "" : "selected"}>not in this file, left blank and reported</option>${((src) => (heads ? (src && !heads.includes(src) ? [src, ...heads] : heads) : f.columns).map((c) => `<option ${c === src ? "selected" : ""}>${esc(c)}</option>`).join(""))(f.recipe ? f.recipe.header : f.source)}</select>`}</td>
        <td>${conf(f)}</td></tr>`).join("")}</table></div>
       <div class="faint" style="font-size:12px">Required? applies to every ${esc(j.sponsor)} job; a column you pick under Comes from applies to this job only.</div>`;
  const body = `<div class="row between"><h2>Plan</h2><span class="faint" style="font-size:12.5px"><span class="mono">${esc(p.file)}</span> · sheet ${esc(p.sheet)} · ${nf(p.rows)} rows → <span class="mono">${esc(p.template)}</span>${p.saved ? " · saved workflow" : ""}</span></div>
    ${table}
    ${p.gap.length ? `<div class="note n-warn" style="font-size:12.5px">Required but not in the file: <b>${esc(p.gap.join(", "))}</b></div>` : ""}
    <details class="fold"><summary><b>Rules</b><span class="faint">${p.rules.length}${p.rules.filter((r) => r.yours).length ? ` · ${p.rules.filter((r) => r.yours).length} yours` : ""}</span><span class="faint" style="margin-left:auto;font-size:12px">open</span></summary>
     <ul style="margin:0;padding-left:18px;display:grid;gap:3px">${p.rules.map((r) => `<li>${esc(r.text)}${r.yours ? '<span class="lab j">YOUR RULE</span>' : ` <span class="faint" style="font-size:12px">· ${esc(r.source)}</span>`}</li>`).join("")}</ul></details>
    ${j.ran ? "" : '<div class="row"><button class="btn pri" data-act="go">Looks right, convert it</button></div>'}`;
  if (j.ran) {
    const by = j.columns_by_you;
    return `<details class="card fold"><summary>${ic("check")}<b>Plan</b><span class="faint">${p.fields.length} ${p.gl ? "keys" : "fields"} → <span class="mono">${esc(p.template)}</span>${by ? ` · ${by} column${by > 1 ? "s" : ""} set by you` : ""}${j.rule_changes ? ` · ${j.rule_changes} rule change${j.rule_changes > 1 ? "s" : ""}` : ""}</span><span class="faint" style="margin-left:auto;font-size:12px">open</span></summary><div class="flat" style="display:grid;gap:12px">${body}</div></details>`;
  }
  return `<div class="card" style="display:grid;gap:12px">${body}</div>`;
}

/* ---------- issues ---------- */
const issuesCard = (j) => (window.ISSUES ? ISSUES.table(j) : "");

/* ---------- output, trace, approved ---------- */
function outputCard(j) {
  const o = j.output, t = j.tally;
  if (j.approved) {
    return `<div class="card n-ok" style="display:grid;gap:10px"><div class="row between"><h2>Written</h2><span class="mono">${esc(o.name)}</span></div>
     <div class="faint" style="font-size:12.5px">${nf(j.written)} ${j.plan.gl ? "journal lines" : "records"} · saved as the ${esc(j.sponsor)} ${esc(j.type)} workflow</div>
     ${window.LEARNING ? "" : (j.learn || []).length ? `<div><b>From this job I learned ${j.learn.length} thing${j.learn.length > 1 ? "s" : ""}.</b><ul>${j.learn.map((l) => `<li>${esc(l.t)}</li>`).join("")}</ul></div>` : "<div>Nothing new to learn from this job.</div>"}
     <div class="row" style="border-top:1px solid var(--line);padding-top:10px">${j.download ? `<a class="btn sm" href="${esc(j.download)}" download data-download>Download ${esc(j.file.name)} · ${nf(j.file.rows)} rows</a>` : ""}${j.next ? `<button class="btn sm pri" data-act="next|${esc(j.next)}">Next: ${esc(j.next)}</button>` : '<span class="faint">All record types are done for this fund.</span>'}</div></div>`;
  }
  const s = j.sample;
  const sample = s && s.rows.length ? `<details class="fold"><summary><b>Sample</b><span class="faint">${esc(s.note)}</span><span class="faint" style="margin-left:auto;font-size:12px">open</span></summary>
    <div class="tw sc" style="max-height:260px"><table><tr><th class="n">Source row</th><th>Source</th><th>→ Output</th></tr>${s.rows.map((r) =>
      `<tr><td class="n">${r.row}</td><td class="mono">${esc(r.source)}</td><td class="mono"><button class="lnk mono" data-act="trace|${r.i}" title="Where this value came from">${esc(r.output)}</button> · ${esc(r.name)}</td></tr>`).join("")}</table></div></details>` : "";
  return `<div class="card" style="display:grid;gap:12px;border-color:var(--navy)"><div class="row between"><h2>Output</h2><span class="mono">${esc(o.name)}</span></div>
   <div class="row">${esc(o.facts)} ${pill(o.ties ? "ok" : "err", o.ties ? "ties" : "does not tie", o.ties ? "check" : "x")}</div>${sample}
   <div class="row"><input id="apnote" placeholder="Notes for the record (optional)" aria-label="Approval notes" style="flex:1;min-width:200px;max-width:360px">
   ${reviewer() ? "" : '<label class="row" style="gap:8px"><span class="faint">Approving as</span><input id="reviewer" placeholder="your name" autocomplete="name" aria-label="Your name" style="min-width:150px"></label>'}
   <button class="btn pri" data-act="approve" ${j.can_approve ? "" : "disabled"}>Approve and download</button></div>
   ${j.empty && !j.empty_allowed ? `<div class="note n-warn" role="alert" style="display:grid;gap:6px"><div><b>Every row is left out, so this file would have no records.</b> Nothing is written unless a named person says an empty upload is what is meant.</div>
     <div class="row"><button class="btn sm" data-act="allow_empty">Write an empty upload: this is meant</button><span class="faint">Type the reason in the notes box first; it goes to the record under your name.</span></div></div>` : ""}
   ${j.empty && j.empty_allowed ? '<div class="faint" style="font-size:12px">An empty upload was decided for this job. Approving writes a file with no records.</div>' : ""}
   ${t.warn ? `<div class="faint" style="font-size:12px">Approving acknowledges ${t.warn} check${t.warn > 1 ? "s" : ""}.</div>` : ""}</div>`;
}
function traceCard(j) {
  const t = j.trace;
  if (!t) return "";
  const rows = [["Source", t.source], ["ID", t.id], ["Name", t.name_rule], ["Checks", t.checks], ["Decision", t.decision],
    ["Model calls", t.model_calls], ["Approval", t.approval]];
  return `<div class="card" id="tracecard" style="display:grid;gap:8px"><div class="row between"><h2>Where <span class="mono">${esc(t.output || t.name)}</span> came from</h2><button class="lnk" data-act="trace|">close</button></div>
   <div class="kv">${rows.map((r) => `<span class="k">${r[0]}</span><span>${esc(r[1])}</span>`).join("")}</div></div>`;
}

/* EXTENSION POINT (learning): teach-back, reconcile, feedback and the rulebook are drawn by learning.js into these hosts. */
function learningHosts(j) {
  if (!window.LEARNING || !j.ran || j.ex_pending) return "";
  /* a GL job reconciles in its Show working tab; other types keep the one-line fold */
  return ["teach", ...(/^GL/.test(j.type) ? [] : ["recon"]), "fb", "book"].map((p) => `<div data-learning="${p}"></div>`).join("");
}

/* QUESTIONS:BEGIN (no separate script: the server has a route per static file)
   Question cards: the job screen's "the user decides an unknown column or format".
   Pure view-model + HTML over job.questions (typed Question objects); every number comes from the server.
   Loaded in the browser as window.QUESTIONS and under node (tests) as module.exports.
   Class names are the mockup's (card, note, tile, pill, btn, row, lnk, mono, faint, sub). */
(function (root) {
  const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const nf = (n) => Number(n).toLocaleString("en-US");
  const KINDS = ["unknown_field", "unknown_format", "number_format"];
  /* a day-first / month-first choice is never recommended, whatever the contract says */
  const DAYMONTH = /\b(day[\s_-]?first|month[\s_-]?first|dmy|mdy|dd\/mm|mm\/dd)\b/i;
  const SCOPES = { job: "This job only", sponsor: null };   // 'organisation' is never offered

  const draft = {};   // question id -> {opt, scope, reason}
  const saved = {};   // "<job>|<question id>" -> {text}

  /* The server's contract (STATUS.md, Migration M24a) says question_id / option_id and puts the recommendation on
     the question; older fixtures say id and mark the option. Both are read here, once, by norm(). */
  const CAUSE = { unknown_field: "Columns I do not recognise", unknown_format: "Formats I will not guess" };
  const WHY = { not_a_date: "is not a date in this reading", not_a_number: "is not a number in this reading",
    bad_grouping: "has thousands marks in the wrong place", percent_not_allowed: "has a percent sign" };
  const norm = (q) => {
    if (!q || typeof q !== "object" || q.__n) return q;
    const opts = Array.isArray(q.options) ? q.options : [], first = opts.find((o) => o && o.column);
    return { ...q, __n: true, id: q.id ?? q.question_id, cause: q.cause ?? CAUSE[q.kind],
      subject: q.subject ?? { field: q.field, header: q.column || (first && first.column) || q.field },
      options: opts.map((o) => {
        const oid = o.id ?? o.option_id, p = o.preview && typeof o.preview === "object" ? { ...o.preview } : null;
        if (p && !Array.isArray(p.failing) && Array.isArray(o.examples) && o.examples.length) {
          p.failing = o.examples.map((e) => `${e.src}: ${e.source_value}${e.reason ? " " + (WHY[e.reason] || String(e.reason).replace(/_/g, " ")) : ""}`);
        }
        return { ...o, id: oid, explain: o.explain ?? o.note, preview: p,
          recommended: o.recommended === true || (q.recommended != null && q.recommended === oid) };
      }) };
  };
  const list = (q) => (q && Array.isArray(q.options) ? q.options : []);
  const subjectOf = (q) => (q && q.subject && typeof q.subject === "object" ? q.subject : {});
  /* a person's choice (day or month first, the decimal mark) is never preselected or recommended */
  const isDayMonth = (q) => list(norm(q)).some((o) => o.person_choice === true || DAYMONTH.test(`${o.id} ${o.label}`));
  /* renderable: a known kind with at least one option; anything else is shown as text and answered in the chat */
  const renderable = (q) => { q = norm(q); return !!q && KINDS.includes(q.kind) && list(q).length > 0 && typeof q.id === "string" && q.id !== ""; };
  const recommended = (q) => { q = norm(q); return !renderable(q) || isDayMonth(q) ? null : list(q).find((o) => o.recommended === true) || null; };
  const scopesOf = (q) => (Array.isArray(q.scopes) ? q.scopes : ["job", "sponsor"]).filter((s) => s === "job" || s === "sponsor");
  const stateOf = (q) => {
    const st = (draft[q.id] = draft[q.id] || { opt: "", scope: "job", reason: "" }), sc = scopesOf(q);
    if (!sc.includes(st.scope)) st.scope = sc[0] || "job";
    return st;
  };
  const scopeLabel = (s, sponsor) => (s === "sponsor" ? `Every job for ${sponsor}` : SCOPES.job);

  /* questions batched by cause, in the order the contract delivered them */
  function groups(questions) {
    const m = new Map();
    for (const q of (questions || []).map(norm)) {
      const c = String(q.cause || "Other");
      if (!m.has(c)) m.set(c, []);
      m.get(c).push(q);
    }
    return [...m].map(([cause, items]) => ({ cause, items }));
  }
  const order = (questions) => groups(questions).flatMap((g) => g.items);

  const pill = (c, t) => `<span class="pill p-${c}">${esc(t)}</span>`;
  const refText = (r) => (r !== null && typeof r === "object" ? Object.values(r).join(" ") : String(r));

  function title(q) {
    const s = subjectOf(q), h = s.header || s.field || "this column";
    if (q.kind === "unknown_field") return `What is “${h}”?`;
    if (q.kind === "unknown_format") return `How is “${h}” written?`;
    if (q.kind === "number_format") return `How are the numbers in “${h}” written?`;
    return String(q.text || "A question from the worker");
  }

  function optionHtml(q, o, st) {
    const p = o.preview && typeof o.preview === "object" ? o.preview : null;
    const rec = recommended(q) === o;
    const fail = p && Array.isArray(p.failing) ? p.failing.slice(0, 5) : [];
    const on = st.opt === o.id;
    return `<div style="display:grid;gap:4px;align-content:start"><button class="tile ${on ? "sel" : ""}" data-q-opt="${esc(q.id)}|${esc(o.id)}" aria-pressed="${on ? "true" : "false"}">
      <span class="row" style="gap:6px"><b>${esc(o.label)}</b>${rec ? pill("ok", "Recommended") : ""}${o.proposed_by === "learned" ? pill("info", "Learned from another sponsor") : ""}</span>
      ${o.proposed_by === "learned" ? '<span class="sub" style="white-space:normal">Another sponsor read a column like this one this way. Nothing is applied until you confirm.</span>' : ""}
      ${o.explain ? `<span class="sub" style="white-space:normal">${esc(o.explain)}</span>` : ""}
      <span class="faint" style="font-size:12.5px">${p ? (Number(p.total) > 0 || !p.text ? `${nf(p.conform)} of ${nf(p.total)} rows conform` : esc(p.text)) : "no preview for this option"}</span></button>
      ${fail.length ? `<details><summary class="faint" style="cursor:pointer;font-size:12px">show ${fail.length} failing example${fail.length > 1 ? "s" : ""}</summary>
        <ul class="mono" style="margin:4px 0 0;padding-left:18px;display:grid;gap:2px">${fail.map((f) => `<li>${esc(refText(f))}</li>`).join("")}</ul></details>` : ""}</div>`;
  }

  function cardHtml(q, i, ctx) {
    const sel = i === ctx.sel, border = sel ? "border-color:var(--navy)" : "";
    if (!renderable(q)) {
      /* honest state: a question this screen cannot draw is shown as text, never hidden */
      return `<div class="card ${sel ? "qsel" : ""}" data-q="${esc(q && q.id)}" style="display:grid;gap:8px;${border}">
        <div class="row between"><h2>${esc(q && q.text ? q.text : "A question from the worker")}</h2>${pill("warn", "Answer in the chat")}</div>
        <div class="faint" style="font-size:12.5px">This screen cannot show a “${esc(q && q.kind)}” question. It is still open: answer it in the chat.</div>
        <div class="row"><button class="btn sm" data-q-chat="1">Answer in the chat</button></div></div>`;
    }
    const st = stateOf(q), s = subjectOf(q), rec = recommended(q);
    const refs = Array.isArray(s.sample_refs) ? s.sample_refs.slice(0, 5).map(refText) : [];
    const scopes = scopesOf(q);
    return `<div class="card ${sel ? "qsel" : ""}" data-q="${esc(q.id)}" style="display:grid;gap:12px;${border}">
      <div class="row between"><h2>${esc(title(q))}</h2>${pill("err", "Needs you")}</div>
      <div class="sub" style="white-space:normal">${esc(q.text)}</div>
      ${refs.length ? `<div class="faint mono" style="font-size:11.5px;white-space:normal">seen at ${refs.map(esc).join(" · ")}</div>` : ""}
      <div class="tiles" style="grid-template-columns:repeat(auto-fit,minmax(min(100%,230px),1fr))">${list(q).map((o) => optionHtml(q, o, st)).join("")}</div>
      ${Array.isArray(q.columns) && q.columns.length ? `<div class="row" style="gap:6px"><label class="faint" style="font-size:12.5px" for="qc_${esc(q.id)}">or a column of your own</label>
        <select id="qc_${esc(q.id)}" data-q-col="${esc(q.id)}" aria-label="A column of your own for ${esc(q.field || "this field")}"><option value="">choose a column…</option>${q.columns.map((c) => `<option ${st.custom === c ? "selected" : ""}>${esc(c)}</option>`).join("")}</select>
        ${st.custom ? '<span class="faint" style="font-size:12px">a column of your own needs a reason</span>' : ""}</div>` : ""}
      <div class="row" role="radiogroup" aria-label="Apply this answer to">${scopes.map((sc) =>
        `<label class="row" style="gap:5px"><input type="radio" style="min-height:0" name="qs_${esc(q.id)}" data-q-scope="${esc(q.id)}" value="${sc}" ${st.scope === sc ? "checked" : ""}>${esc(scopeLabel(sc, ctx.sponsor))}</label>`).join("")}</div>
      <div class="row"><input data-q-reason="${esc(q.id)}" placeholder="Reason (optional)" aria-label="Reason for ${esc(title(q))}" value="${esc(st.reason)}" style="flex:1;min-width:180px">
        <button class="btn pri" data-q-confirm="${esc(q.id)}" ${st.opt || st.custom ? "" : "disabled"}>Confirm</button>
        ${rec ? `<span class="faint" style="font-size:12px">a accepts “${esc(rec.label)}”</span>` : ""}</div></div>`;
  }

  function savedHtml(job, qid, v) {
    return `<div class="note n-ok" role="status" data-q-saved="${esc(qid)}"><div class="row between"><span>${esc(v.text)}</span>
      <button class="lnk" data-q-undo="${esc(qid)}">Undo</button></div></div>`;
  }

  /* the questions card for a job view; "" when nothing is asked and nothing was just saved */
  function html(job, ctx) {
    ctx = ctx || {};
    const qs = job && Array.isArray(job.questions) ? job.questions.map(norm) : [];
    const open = new Set(qs.map((q) => q && q.id));
    /* the answers in force come from the server (job.answered), so Undo survives a reload; a note saved on this
       page and not yet in that list (an older server) is kept too */
    const done = new Map();
    for (const a of (job && Array.isArray(job.answered) ? job.answered : [])) if (!open.has(a.question_id)) done.set(a.question_id, { text: a.text });
    for (const k of Object.keys(saved)) {
      const qid = k.slice(String(job && job.id).length + 1);
      if (k.startsWith(`${job && job.id}|`) && !open.has(qid) && !done.has(qid) && !(job && Array.isArray(job.answered))) done.set(qid, saved[k]);
    }
    if (!qs.length && !done.size) return "";
    const c = { sel: ctx.sel || 0, sponsor: (job && job.sponsor) || ctx.sponsor || "this sponsor" };
    const flat = order(qs);
    const body = groups(qs).map((g) => `<div style="display:grid;gap:10px"><div class="row between"><b>${esc(g.cause)}</b><span class="faint" style="font-size:12.5px">${g.items.length} question${g.items.length > 1 ? "s" : ""}</span></div>
      ${g.items.map((q) => cardHtml(q, flat.indexOf(q), c)).join("")}</div>`).join("");
    return `<div class="card" id="questions" style="display:grid;gap:12px"><div class="row between"><h2>Questions</h2>
      <span class="faint" style="font-size:12.5px">${qs.length ? `${qs.length} need${qs.length === 1 ? "s" : ""} your answer · ` : ""}Keys: j / k to move · a to accept the recommended one · / to message the worker</span></div>
      ${ctx.me ? `<div class="row" style="gap:8px"><span class="faint" style="font-size:12.5px">Deciding as <b>${esc(ctx.me)}</b>, recorded with every answer</span></div>`
        : `<div class="row" style="gap:8px"><label for="reviewer" style="font-size:12.5px"><b>Who is deciding</b></label>
        <input id="reviewer" data-q-reviewer="1" placeholder="your name" autocomplete="name" value="${esc(ctx.reviewer || "")}" style="min-width:160px;max-width:240px">
        <span class="faint" style="font-size:12px">self-declared, recorded with every answer; this is not a sign-in</span></div>`}
      ${[...done].map(([qid, v]) => savedHtml(job, qid, v)).join("")}${body}</div>`;
  }

  /* the action for Confirm (or for 'a'): {action, text, savedText}; null when the choice is not complete */
  function build(job, qid, optId) {
    const q = (job.questions || []).map(norm).find((x) => x && x.id === qid);
    if (!renderable(q)) return null;
    const st = stateOf(q), id = optId || st.opt, o = list(q).find((x) => x.id === id);
    const scope = st.scope, forWho = scope === "sponsor" ? `for ${job.sponsor}` : "for this job", s = subjectOf(q);
    if (!o) {   // a column of the person's own: it always needs a reason (the server refuses without one)
      if (optId || !st.custom || !(q.columns || []).includes(st.custom)) return null;
      return { id: qid, option: "", custom: { action: "map", header: st.custom }, scope, reason: st.reason,
        action: "", savedText: `Saved ${forWho}: ${q.field || s.field}, taken from “${st.custom}”` };
    }
    return { id: qid, option: o.id, scope, reason: st.reason, action: `answer|${job.id}|${qid}|${o.id}|${scope}`,
      savedText: `Saved ${forWho}: ${s.header ? s.header + ", " : ""}${o.label}` };
  }

  const api = {
    KINDS, html, build, groups, order, renderable, recommended, isDayMonth, scopesOf, title, cardHtml,
    pick(qid, optId) { const d = (draft[qid] = draft[qid] || { opt: "", scope: "job", reason: "" }); d.opt = optId; d.custom = ""; },
    pickColumn(qid, header) { const d = (draft[qid] = draft[qid] || { opt: "", scope: "job", reason: "" }); d.custom = header; if (header) d.opt = ""; },
    setScope(qid, scope) { (draft[qid] = draft[qid] || { opt: "", scope: "job", reason: "" }).scope = scope; },
    setReason(qid, text) { (draft[qid] = draft[qid] || { opt: "", scope: "job", reason: "" }).reason = text; },
    saved(job, qid, text) { saved[`${job.id}|${qid}`] = { text }; },
    forget(job, qid) { delete saved[`${job.id}|${qid}`]; delete draft[qid]; },
    reset() { for (const k of Object.keys(draft)) delete draft[k]; for (const k of Object.keys(saved)) delete saved[k]; },
    count(job) { return job && Array.isArray(job.questions) ? job.questions.length : 0; },
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.QUESTIONS = api;
})(typeof window !== "undefined" ? window : null);
/* QUESTIONS:END */

/* ---------- the job screen: a slim header, then one tab at a time ---------- */
const JOB_TABS = [["plan", "Plan"], ["issues", "Issues"], ["recon", "Reconcile"], ["output", "Output"], ["activity", "Activity"]];
function jobTab(j) {   // the tab is part of the address (#tab=issues), so a reload keeps it
  const m = /tab=(\w+)/.exec(location.hash || ""), t = m && m[1];
  if (t && JOB_TABS.some((x) => x[0] === t) && (j.ran || t === "plan")) return t;
  return j.ran ? "issues" : "plan";
}
window.addEventListener("hashchange", () => { if (S) render(); });
function jobMain(j) {
  const fail = j.failed ? `<div class="card n-warn" style="display:grid;gap:8px"><h2>Stopped at ${esc(stageLabel(j.failed.stage))}</h2>
    <div class="sub">${esc(j.failed.why)}. Everything up to Convert is saved; resuming starts at ${esc(stageLabel(j.failed.stage))}.</div>
    <div class="row"><button class="btn pri" data-act="resume">Resume from ${esc(stageLabel(j.failed.stage))}</button><span class="faint">or switch the Searcher model first</span></div></div>` : "";
  const waiting = j.ex_pending ? `<div class="card" style="display:grid;gap:6px"><h2>Waiting for your answer</h2>${j.ex_ask ? `<div>${esc(j.ex_ask.title)}</div><div class="row">${j.ex_ask.options.map((o) => `<button class="btn ${o.primary ? "pri" : ""}" data-act="${esc(o.action)}">${esc(o.label)}</button>`).join("")}</div>` : ""}<div class="faint" style="font-size:12.5px">${esc(j.store_text)}</div></div>` : "";
  const asked = QUESTIONS.html(j, { sel: qsel, sponsor: j.sponsor, reviewer: reviewer(), me: signedIn() ? S.me : "" });
  const tab = jobTab(j);
  const need = j.ran ? (j.tally.err || 0) + (j.questions_blocking || []).length : 0;
  let panel = "";
  if (j.ex_pending) panel = waiting + asked;
  else if (tab === "plan") panel = '<div data-reshape="1"></div>' + (/^GL/.test(j.type) || ((j.plan || {}).fields || []).some((f) => f.id_field) ? "" : '<div data-idgen="1"></div>') + planCard(j) + fail + (j.ran ? "" : asked);
  else if (tab === "issues") panel = (j.approved ? "" : '<div data-reshape="1" data-only="1"></div>') + fail + asked + issuesCard(j);
  else if (tab === "recon") panel = window.RECON ? RECON.table(j) : "";
  else if (tab === "output") {
    panel = j.tally.err || j.on_hold ? `<div class="note n-warn"><b>${j.tally.err ? `${j.tally.err} still need you` : `${j.on_hold} on hold`}.</b> Decide${j.tally.err ? "" : " or release"} them on the <a class="lnk" href="#tab=issues">Issues</a> tab first: nothing is written while any is open or on hold.</div>` : outputCard(j);
    panel += traceCard(j) + learningHosts(j);
  } else if (tab === "activity") {
    if (window.WORKING) WORKING.state.open = true;
    panel = `<div class="card" style="display:grid;gap:12px"><h2>Decisions on this job</h2>${window.ISSUES ? ISSUES.activity(j) : ""}</div>${window.WORKING ? WORKING.shell(j) : ""}`;
  }
  const tabs = JOB_TABS.filter((t) => j.ran || t[0] === "plan").map(([k, l]) => `<a role="tab" aria-selected="${tab === k}" class="tabl ${tab === k ? "on" : ""}" href="#tab=${k}">${l}${k === "issues" && need ? `<span class="pill p-err" style="margin-left:6px">${need}</span>` : ""}</a>`).join("");
  return `<div><div class="eyebrow">${esc(j.sponsor)} · ${esc(j.entities)}</div><h1>${esc(j.type)}</h1>${sourceLine(j)}</div>
   <div class="card" style="display:grid;gap:10px;padding:12px 14px">${j.ran && window.ISSUES ? ISSUES.verdict(j) : ""}
     <div class="tl-c" title="Every stage writes a checkpoint; a job resumes from the last one.">${timeline(j.timeline)}</div></div>
   <nav class="tabs" role="tablist" aria-label="Job sections">${tabs}</nav>${panel}`;
}

/* ---------- dashboard, work lists, standards, settings ---------- */
const liveTxt = (d) => d ? `go-live ${esc(d)}` : '<span style="color:var(--warn)">go-live not set</span>';
const bar = (rows) => `<div class="bar">${rows.filter((r) => r.n).map((r) => `<i style="flex:${r.n};background:var(${r.color})"></i>`).join("")}</div>`;
const leg = (rows) => `<div class="leg">${rows.map((r) => `<div><span class="dot" style="background:var(${r.color})"></span><b>${nf(r.n)}</b>${esc(r.label)}<span class="pc">${esc(r.pc)}</span></div>`).join("")}</div>`;
function dashView() {
  const D = S.pages.dashboard, stc = { Aging: "err", "Due soon": "warn", Active: "info", Completed: "ok" };
  const c = D.cycle;
  return `<div class="row between"><div><h1>Dashboard</h1>${S.me ? `<div class="sub" data-welcome>Welcome, ${esc(S.me)}</div>` : ""}</div><button class="btn pri" data-ob="new">+ Onboard sponsors</button></div>
  <div class="tiles">${D.tiles.map((t) => `<div class="card"><div class="eyebrow">${esc(t.title)}</div><div style="font-size:34px;font-weight:300">${nf(t.total)} <small class="eyebrow">total</small></div>${bar(t.rows)}${leg(t.rows)}</div>`).join("")}</div>
  <div class="card band"><div><div style="font-size:24px;font-weight:300">${c.median === null ? "—" : nf(c.median) + "d"}</div><div class="eyebrow">Median sponsor cycle</div></div>
   <span class="sub">${c.completed ? `across ${c.completed} completed sponsor${c.completed === 1 ? "" : "s"}` : "No sponsor has completed yet."}</span></div>
  ${D.sponsors.length ? D.sponsors.map((s) => `<div class="srow"><div class="hd"><span style="flex:1;min-width:140px"><b style="font-size:15px">${esc(s.name)}</b> <span class="sub mono">${esc(s.id || "no ID")}</span> <span class="sub">· ${liveTxt(s.live)}</span></span>${pill(stc[s.status], s.status, s.status === "Aging" ? "hour" : s.status === "Completed" ? "check" : "dot")}${s.entities ? `<span class="sub">${s.entities} entities</span>` : pill("warn", "entities not loaded", "warn")}<span class="prog"><i style="width:${s.pct}%"></i></span><span class="sub">${s.pct}%</span>
   <button class="btn sm" data-act="switch|${esc(s.name)}">Open</button></div></div>`).join("") : '<div class="card faint">No sponsors onboarded yet. Use + Onboard sponsors.</div>'}`;
}
function listPage() {
  const L = S.pages.list, rej = L.title === "Rejected";
  const empty = { "Pending approval": "Nothing is waiting for approval.", Rejected: "No entity has been sent back.", Aging: "Nothing has waited longer than the limit." }[L.title];
  return `<div><h1>${esc(L.title)}</h1><p class="sub">${esc(L.sub)}</p></div>
  <div class="card tw"><table><tr><th>Sponsor</th><th>Fund</th><th>Record type</th><th class="n">Rows</th><th>Prepared by</th><th>Status</th></tr>${L.rows.map((r) => `<tr class="hov"><td>${esc(r.sponsor)}</td><td>${esc(r.fund)}</td>
   <td>${r.id ? `<button class="lnk" data-act="open|${esc(r.id)}">${esc(r.type)}</button>` : esc(r.type)}</td><td class="n">${r.rows === "" ? "" : nf(r.rows)}</td><td>${esc(r.by)}</td>
   <td>${rej ? pill("err", "Rework", "x") : pill("warn", "Awaiting you", "hour")}</td></tr>`).join("") || `<tr><td colspan="6" class="faint">${empty}</td></tr>`}</table></div>`;
}
function stdHead(t, sub) {
  return `<div><h1>${esc(t)}</h1><p class="sub">${esc(sub)}</p><div class="note n-info" style="margin-top:8px">Business users maintain this, no developer needed. Upload a new version or add a template; I compare it with the live one and it goes live only after a person approves it in Improvements.</div></div>`;
}
/* the result of the last upload: the header diff, and where the card is waiting */
function stdNote() {
  if (std.at !== S.view) return "";
  if (std.err) return `<div class="note n-err" role="status">${esc(std.err)}</div>`;
  const r = std.res; if (!r) return "";
  const d = r.header_diff, names = (a) => a.map((x) => `<span class="mono">${esc(x)}</span>`).join(", "), bits = [];
  if (d.added.length) bits.push(`${d.added.length} column${d.added.length > 1 ? "s" : ""} added: ${names(d.added)}`);
  if (d.removed.length) bits.push(`${d.removed.length} removed: ${names(d.removed)}`);
  if (d.renames.length) bits.push(`possible rename: ${d.renames.map((x) => `${esc(x[0])} → ${esc(x[1])}`).join(", ")}`);
  if (d.moved) bits.push("column order changed");
  const q = r.row_diff ? `Rows: ${r.row_diff.added} added, ${r.row_diff.removed} removed, ${r.row_diff.changed} changed. ` : "";
  const bad = r.status === "blocked";
  return `<div class="note ${bad ? "n-warn" : "n-info"}" role="status"><b>${esc(r.file)} read.</b> ${bits.join("; ") || "Header unchanged."} ${q}
   ${bad ? `Blocked, so it cannot be approved: ${esc(r.reasons.join("; "))}. Fix the file and upload it again.` : "It is waiting in Improvements for approval. Nothing live has changed."}
   <button class="lnk" data-act="view|impr">Open Improvements</button></div>`;
}
const stdReview = (w) => w ? ` ${pill(w.status === "blocked" ? "err" : "info", w.status === "blocked" ? "Blocked in review" : "In review", w.status === "blocked" ? "x" : "hour")}` : "";
function stdPick(kind, name) {
  const f = document.createElement("input"); f.type = "file"; f.accept = S.view === "tmpls" ? ".xlsx" : ".xlsx,.csv";
  f.onchange = () => { if (f.files[0]) stdUpload(S.view === "tmpls" ? (kind === "new" ? "new" : "tmpls") : S.view, name, f.files[0]); };
  f.click();
}
async function stdUpload(kind, name, file) {
  std.res = null; std.err = ""; std.at = S.view;
  try { std.res = await api(`/api/console/standards/upload/${kind}/${encodeURIComponent(file.name)}?name=${encodeURIComponent(name || "")}`, { method: "PUT", body: file }); }
  catch (e) { std.err = `${file.name}: ${e.message}`; }
  try { S = await api("/api/console/state"); } catch (e) { toast(e.message, true); }
  render();
}
function stdTemplates() {
  const V = S.pages.standards;
  return stdHead("Ledgerline templates", "The blank upload templates every sponsor starts from. Download a blank, fill it, and upload it back. A sponsor or a single job can still override it.") +
   `<div class="card" style="display:grid;gap:12px"><div class="row between"><div class="row"><b>${V.count} templates</b><span class="faint">blank, ready to fill</span></div><button class="btn sm pri" data-std="new|">+ Add a template</button></div>
   ${stdNote()}${V.added.map((w) => `<div class="note n-info" role="status">${esc(w.summary)}${stdReview(w)}</div>`).join("")}
   <div class="sc"><table><thead><tr><th>Template</th><th>File</th><th class="n">Columns</th><th>Status</th><th></th></tr></thead><tbody>
   ${V.rows.map((u) => `<tr class="hov"><td><b>${esc(u.name)}</b><div class="faint" style="white-space:normal">${esc(u.note)}</div></td><td class="mono">${esc(u.file)}</td><td class="n">${u.columns}</td>
    <td>${u.file ? pill("ok", u.status, "check") : pill("err", u.status, "x")}${stdReview(u.review)}</td>
    <td><div class="row" style="justify-content:flex-end">${u.file ? `<a class="btn sm pri" href="/api/console/standards-file/${encodeURIComponent(u.file)}" download>Download</a>` : ""}<button class="btn sm" data-std="up|${esc(u.type)}">Upload filled</button></div></td></tr>`).join("")}
   </tbody></table></div></div>`;
}
const coaRows = (V) => V.rows.map((a) => `<tr class="hov"><td class="mono">${esc(a.account)}</td><td style="white-space:normal">${esc(a.title)}</td><td>${esc(a.grouping)}</td><td>${esc(a.line)}</td><td>${esc(a.inside)}</td><td class="mono">${esc(a.closing_to || "–")}</td><td>${esc(a.closing_type)}</td></tr>`).join("")
  || `<tr><td colspan="7" class="faint">${V.missing ? "The chart file is not available." : "No account matches."}</td></tr>`;
const ttRows = (V) => V.rows.map((a) => `<tr class="hov"><td class="mono">${esc(a.code)}</td><td style="white-space:normal">${esc(a.name)}</td><td class="mono" style="white-space:normal">${esc(a.accounts)}</td></tr>`).join("")
  || `<tr><td colspan="3" class="faint">${V.missing ? "The mapping file is not available." : "No transaction type matches."}</td></tr>`;
function stdCoa() {
  const V = S.pages.standards, dl = V.missing ? "" : `<a class="btn sm pri" href="/api/console/standards-file/${V.file}" download>Download</a>`;
  return stdHead("Golden COA template", "The all-sponsor standard chart of accounts. Every GL job matches the source accounts to it in tiers: 1 historic match, 2 suggested, 3 unmatched for you to assign. A sponsor can carry its own chart.") +
   `<div class="card" style="display:grid;gap:12px"><div class="row between"><div class="row"><b id="coacount">${V.total} accounts</b><span class="faint mono">${esc(V.file)}</span></div><div class="row">${dl}<button class="btn sm" data-std="up|">Upload filled template</button></div></div>
   ${stdNote()}${V.review ? `<div class="note n-info" role="status">${esc(V.review.summary)}${stdReview(V.review)}</div>` : ""}
   <div class="row"><span class="chips">${V.groups.map((g) => `<button class="${std.g === g ? "on" : ""}" data-std="g|${esc(g)}">${esc(g)}</button>`).join("")}</span><input id="coaq" placeholder="Search account number, title or line item" aria-label="Search the chart" style="flex:1;min-width:220px" value="${esc(std.coaQ)}"></div>
   <div class="sc"><table><thead><tr><th>Account number</th><th>Account title</th><th>FS Grouping</th><th>FS Line Item</th><th>Inside commitment</th><th>Close into account</th><th>Period end close</th></tr></thead><tbody id="coabody">${coaRows(V)}</tbody></table></div></div>`;
}
function stdTtm() {
  const V = S.pages.standards, dl = V.missing ? "" : `<a class="btn sm pri" href="/api/console/standards-file/${V.file}" download>Download</a>`;
  return stdHead("Transaction type mapping", "Which GL accounts each transaction type may post to. The GL job uses this to check every journal line, and the reconciliations use the type to place a line in operating, investing or financing.") +
   `<div class="card" style="display:grid;gap:12px"><div class="row between"><div class="row"><b id="ttcount">${V.total} transaction types</b><span class="faint mono">${esc(V.file)}</span></div><div class="row">${dl}<button class="btn sm" data-std="up|">Upload filled template</button></div></div>
   ${stdNote()}${V.review ? `<div class="note n-info" role="status">${esc(V.review.summary)}${stdReview(V.review)}</div>` : ""}
   <input id="ttq" placeholder="Search transaction type, name or account" aria-label="Search transaction types" value="${esc(std.ttQ)}">
   <div class="sc"><table><thead><tr><th>Transaction Type ID</th><th>Transaction Type name</th><th>GL Accounts</th></tr></thead><tbody id="ttbody">${ttRows(V)}</tbody></table></div></div>`;
}
function stdFetch() {
  const kind = S.view;
  if (kind !== "coa" && kind !== "ttm") return;
  const qs = kind === "coa" ? `?q=${encodeURIComponent(std.coaQ)}&group=${encodeURIComponent(std.g)}` : `?q=${encodeURIComponent(std.ttQ)}`;
  api(`/api/console/standards/${kind}${qs}`).then((V) => {
    const body = $(kind === "coa" ? "coabody" : "ttbody"); if (!body) return;
    body.innerHTML = kind === "coa" ? coaRows(V) : ttRows(V);
    const n = $(kind === "coa" ? "coacount" : "ttcount");
    if (n) n.textContent = V.shown === V.total ? `${V.total} ${kind === "coa" ? "accounts" : "transaction types"}` : `${V.shown} of ${V.total}`;
  }).catch((e) => toast(e.message, true));
}
function modelsV() {
  const M = S.pages.settings;
  if (!M.available) return `<div><h1>Settings · Models</h1><p class="sub">The model settings could not be read from the service.</p></div>`;
  const prov = { openai: "OpenAI", openrouter: "Free models (OpenRouter)", demo: "Demo (offline)" };
  return `<div><h1>Settings · Models</h1><p class="sub">The worker uses the model in the first row for planning, mapping and explaining. The role rows below are the roles the code supports; each takes its own model from the setting named in its row. Code does the copying and checking, so most rows never reach a model. Every change is written to the settings file and applies to new jobs.</p></div>
  <div class="card" style="display:grid;gap:12px"><div class="row between"><span class="seg" role="group" aria-label="Provider">${M.providers.map((p) => `<button class="${M.provider === p ? "on" : ""}" data-md="prov|${p}">${prov[p]}</button>`).join("")}</span>
   <span class="faint">${M.provider === "openrouter" ? "Free models are rate-limited per day; a fallback keeps jobs moving." : M.provider === "demo" ? "The offline demo model, no key needed." : "Paid per token."}</span></div>
   <div class="tw"><table><tr><th>Role</th><th>Does</th><th>Model</th><th>Fallback</th><th></th></tr><tr><td><b>Worker</b></td><td style="white-space:normal">Plans, maps columns, decides distinct keys, explains and talks with you</td>
    <td><input id="md_model" value="${esc(M.model)}" aria-label="Model" style="min-width:260px" class="mono"></td>
    <td><input id="md_fb" value="${esc(M.fallbacks)}" aria-label="Fallback models" style="min-width:200px" class="mono"></td>
    <td><div class="row" style="flex-wrap:nowrap"><button class="btn sm pri" data-md="save">Save</button><button class="btn sm" data-md="test">Test</button>${mdTest ? (mdTest.ok ? pill("ok", `works · ${mdTest.seconds}s`, "check") : pill("err", mdTest.error || "failed", "x")) : ""}</div></td></tr>
    ${(M.roles || []).map((r) => `<tr><td><b>${esc(r.role)}</b></td><td style="white-space:normal">${esc(r.does)}</td><td class="mono">${esc(r.model)}</td><td class="mono">${esc(r.fallbacks || "–")}</td>
     <td>${r.explicit ? pill("info", `set by ${r.env}`, "check") : pill("mute", "inherits the worker model", "dot")}${r.used ? "" : `<div class="faint">no step asks for this role yet</div>`}</td></tr>`).join("")}</table></div>
   <div class="row">${M.provider === "demo" ? "" : `${M.key_configured ? pill("ok", "API key configured", "check") : pill("warn", "No API key", "warn")}<input id="md_key" type="password" placeholder="Paste an API key to replace it" aria-label="API key" style="min-width:260px"><button class="btn sm" data-md="key">Save key</button>`}</div>
   <div class="row"><input id="md_token" type="password" placeholder="${approverToken() ? "Token set for this tab" : "Approver token, where the service requires one"}" aria-label="Approver token" style="min-width:260px" autocomplete="off"><a href="#" data-md="token">Use token</a></div>
   <div class="faint">Rules, tables and templates never depend on which model is set.</div></div>`;
}

/* ---------- onboarding sponsors or loading entities (the mockup's modal) ---------- */
function obKnown() {
  return onb.mode === "new" ? S.sponsors.map((s) => ({ id: s.id, n: s.name })) : S.sponsors.flatMap((s) => s.entities.map((e) => ({ id: e.id, n: e.name })));
}
function obClash(r, skip) {
  const k = (x) => String(x).trim().toLowerCase(), all = obKnown().concat(onb.list.filter((_, i) => i !== skip));
  const a = all.find((x) => x.id && k(x.id) === k(r.id)), b = all.find((x) => k(x.n) === k(r.n));
  return a ? `ID ${r.id} is already used by ${a.n}` : b ? `${r.n} is already ${onb.list.some((x) => x.n === b.n) ? "in this list" : "onboarded"}` : "";
}
function obGroups() {
  const m = new Map(), sp = S.sponsors.find((s) => s.name === onb.sp);
  const have = (sp ? sp.entities : []).map((e) => ({ gid: e.gid, gn: e.gn }));
  have.concat(onb.list).forEach((r) => { if (r.gid && !m.has(r.gid)) m.set(r.gid, { gid: r.gid, gn: r.gn }); });
  return [...m.values()];
}
function onbModal() {
  const o = onb, nw = o.mode === "new", have = nw ? [] : ((S.sponsors.find((s) => s.name === o.sp) || { entities: [] }).entities);
  const miss = o.list.filter((r) => !r.live).length, w = nw ? "Sponsor" : "Entity";
  return `<div class="modal"><div class="card" style="gap:14px;padding:22px" role="dialog" aria-label="${nw ? "Onboard sponsors" : "Entities for " + esc(o.sp)}">
  <div class="row between"><h2 style="font-size:20px">${nw ? "Onboard sponsors" : `Entities for ${esc(o.sp)}`}</h2><button class="btn sm" data-ob="close" aria-label="Close">✕</button></div>
  <div class="sub">${nw ? "A sponsor has a sponsor ID, a name and a go-live date. Its entities are loaded next, from the sponsor page." : "An entity has an entity ID, a name and its own go-live date. It can sit in a fund group (group ID and name) or stand alone. Funds need not go live together: load the ones you have now, add the rest as they arrive."}</div>
  <div class="uplo" data-drop="ob" style="padding:14px">${o.file ? `<span class="mono"><b>${esc(o.file)}</b></span> · read ${o.read} ${nw ? "sponsors" : "entities"} · <button class="lnk" data-ob="clearf">remove</button>`
    : `${ic("up")} Bulk: drop a list of ${nw ? "sponsors" : "entities"} (${nw ? "sponsor ID, name, go-live date" : "entity ID, name, go-live date, optional fund group ID and name"}), <button class="lnk" data-ob="browse">choose it</button>, or <button class="lnk" data-ob="sample">use a sample</button>`}</div>
  <input id="obfile" type="file" accept=".xlsx" style="position:absolute;left:-9999px" tabindex="-1" aria-label="${w} list file">
  ${o.skipped.length ? `<div class="note n-warn" role="status">${o.skipped.length} row${o.skipped.length > 1 ? "s" : ""} not added: ${o.skipped.map((x) => esc(x.why)).join("; ")}.</div>` : ""}
  <div class="row" style="align-items:end"><label class="f" style="flex:1;min-width:110px">${w} ID<input id="ob_id" placeholder="${nw ? "e.g. GP-006" : "e.g. 5317404"}"></label><label class="f" style="flex:2;min-width:160px">${w} name<input id="ob_n" placeholder="${nw ? "e.g. Sponsor F" : "e.g. Fund D"}"></label><label class="f" style="flex:1;min-width:140px">Go-live date<input id="ob_live" type="date" required></label>${nw ? "" : `<label class="f" style="flex:1;min-width:110px">Fund group ID<input id="ob_gid" placeholder="optional, e.g. FG-02" list="ob_grps"></label><label class="f" style="flex:2;min-width:160px">Fund group name<input id="ob_gn" placeholder="optional; blank = standalone fund"></label><datalist id="ob_grps">${obGroups().map((g) => `<option value="${esc(g.gid)}">${esc(g.gn)}</option>`).join("")}</datalist>`}<button class="btn" data-ob="add1">Add</button></div>
  ${have.length ? `<div class="faint">Already loaded: ${have.map((x) => `${esc(x.name)} (${esc(x.live)})`).join(", ")}</div>` : ""}
  ${o.list.length ? `<div class="tw"><table><tr><th>${w} ID</th><th>Name</th><th>Go-live date</th>${nw ? "" : "<th>Fund group</th>"}<th></th></tr>${o.list.map((r, i) => `<tr><td class="mono">${esc(r.id)}</td><td>${esc(r.n)}</td><td><input type="date" data-obr="${i}" value="${esc(r.live)}" aria-label="Go-live date for ${esc(r.n)}">${r.live ? "" : ` ${pill("warn", "MISSING", "warn")}`}</td>${nw ? "" : `<td>${r.gid ? `${esc(r.gn)} <span class="mono sub">${esc(r.gid)}</span>` : '<span class="faint">standalone</span>'}</td>`}<td><button class="lnk" data-ob="rm|${i}" aria-label="Remove ${esc(r.n)}">✕</button></td></tr>`).join("")}</table></div>
   ${miss ? `<div class="note n-warn" role="status">${miss} without a go-live date. The go-live date is mandatory: set it in the table to save.</div>` : ""}` : `<div class="faint">No new ${nw ? "sponsors" : "entities"} yet.</div>`}
  ${o.err ? `<div class="note n-err" role="status">${esc(o.err)}</div>` : ""}
  <div class="row"><button class="btn pri" data-ob="save">${nw ? `Onboard ${o.list.length || ""} sponsor${o.list.length === 1 ? "" : "s"}` : "Save entities"}</button><span class="faint">Recorded in the audit.</span></div></div></div>`;
}
function renderModal() { $("modal").innerHTML = onb ? onbModal() : ""; }
function obOpen(mode, sp) { onb = { mode, sp: sp || "", list: [], file: "", read: 0, err: "", skipped: [] }; swPop = false; render(); }
function obMerge(res) {
  const skipped = res.skipped.map((x) => ({ why: x.why })), rows = [];
  for (const r of res.rows) {
    const why = obClash(r);
    if (why) skipped.push({ why }); else { rows.push({ ...r, fromFile: true }); onb.list.push({ ...r, fromFile: true }); }
  }
  onb.file = res.file; onb.read = res.read; onb.skipped = skipped;
  onb.err = ""; renderModal();
}
async function obUpload(file) {
  try {
    obMerge(await api(`/api/console/onboard/file/${encodeURIComponent(file.name)}?mode=${onb.mode}&sponsor=${encodeURIComponent(onb.sp)}`, { method: "PUT", body: file }));
  } catch (e) { onb.err = e.message; renderModal(); }
}
async function obSample() {
  try { obMerge(await api(`/api/console/onboard/sample?mode=${onb.mode}&sponsor=${encodeURIComponent(onb.sp)}`)); }
  catch (e) { onb.err = e.message; renderModal(); }
}
function obAdd1() {
  const v = (k) => { const el = $(k); return el ? el.value.trim() : ""; }, r = { id: v("ob_id"), n: v("ob_n"), live: v("ob_live") };
  const w = onb.mode === "new" ? "Sponsor" : "Entity";
  if (onb.mode !== "new") {
    r.gid = v("ob_gid"); r.gn = v("ob_gn");
    const g = obGroups().find((x) => x.gid.toLowerCase() === r.gid.toLowerCase());
    if (r.gn && !r.gid) { onb.err = "A fund group needs its group ID as well as its name."; return renderModal(); }
    if (g && !r.gn) r.gn = g.gn;
    else if (g && g.gn !== r.gn) { onb.err = `Fund group ${g.gid} is already called ${g.gn}. Use that name or a new group ID.`; return renderModal(); }
    if (r.gid && !r.gn) { onb.err = `Give fund group ${r.gid} a name.`; return renderModal(); }
  }
  if (!r.id || !r.n || !r.live) { onb.err = `${w} ID, name and go-live date are all required.`; return renderModal(); }
  const c = obClash(r); if (c) { onb.err = c; return renderModal(); }
  onb.list.push(r); onb.err = ""; renderModal();
}
async function obSave() {
  const o = onb, nw = o.mode === "new";
  if (!o.list.length) { o.err = nw ? "Add at least one sponsor." : "Load at least one entity. Jobs run against this list."; return renderModal(); }
  const no = o.list.filter((r) => !r.live);
  if (no.length) { o.err = `Go-live date is mandatory. Missing for ${no.map((r) => r.n).join(", ")}.`; return renderModal(); }
  const rows = o.list.map(({ fromFile, ...r }) => r);
  try {
    S = await api("/api/console/act", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: `onb_save|${o.mode}|${o.sp}|${o.file}`, text: JSON.stringify(rows) }) });
    onb = null; render();
  } catch (e) { o.err = e.message; renderModal(); }
}
async function mdCall(kind) {
  const val = (id) => ($(id) || {}).value;
  const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  try {
    if (kind.startsWith("prov|")) await post("/api/settings", { provider: kind.slice(5) });
    else if (kind === "save") await post("/api/settings", { model: val("md_model"), fallbacks: val("md_fb") });
    else if (kind === "token") { try { sessionStorage.setItem("token", val("md_token")); } catch (e) { /* storage blocked */ } toast("Token set for this tab"); }
    else if (kind === "key") { await post("/api/settings", { api_key: val("md_key") }); toast("Key saved"); }
    else if (kind === "test") mdTest = await post("/api/models/probe", { model: val("md_model") });
    if (kind !== "test") mdTest = null;
    S = await api("/api/console/state"); render();
  } catch (e) { toast(e.message, true); }
}

/* ---------- the job's source file: replace it at any stage, as often as needed ---------- */
let rep = { busy: false, err: "", name: "", last: null };
function sourceLine(j) {
  if (!j.file) return "";
  const last = (j.replaced || []).slice(-1)[0], shown = rep.last || last;
  const list = rep.last && rep.last.reopened_listed && rep.last.reopened_listed.length
    ? `<details><summary class="faint">${rep.last.reopened} decision${rep.last.reopened === 1 ? "" : "s"} open again</summary><ul class="faint" style="margin:4px 0 0 16px">${rep.last.reopened_listed.map((x) => `<li>row ${esc(x.was)} was a ${esc(x.slot)}${x.what ? `: ${esc(x.what)}` : ""}</li>`).join("")}</ul></details>` : "";
  const dropped = rep.last && Object.keys(rep.last.column_choices_dropped || {}).length
    ? `<div class="note n-warn" role="status">The new file has no column for ${Object.values(rep.last.column_choices_dropped).map((h) => `“${esc(h)}”`).join(", ")}; those column choices were dropped.</div>` : "";
  return `<div class="row" style="gap:8px;margin-top:4px;flex-wrap:wrap"><span class="faint">Source file</span><span class="mono">${esc(j.file)}</span>
    ${j.can_replace ? (rep.busy ? `<span class="faint">Reading ${esc(rep.name)}…</span>` : `<button class="lnk" data-src="browse">Replace file</button>`) : `<span class="faint">approved: the file is final</span>`}
    ${shown ? `<span class="faint">· replaced ${esc(shown.at.replace("T", " "))} by ${esc(shown.by)}: ${shown.kept} decision${shown.kept === 1 ? "" : "s"} kept, ${shown.reopened} open again</span>` : ""}</div>
    <input id="srcfile" type="file" accept=".xlsx,.xlsm" style="position:absolute;left:-9999px" tabindex="-1" aria-label="Replace the source file">
    ${list}${dropped}${rep.err ? `<div class="note n-err" role="alert">${esc(rep.err)}</div>` : ""}`;
}
async function replaceSource(file) {
  const j = S && S.job;
  if (!j) return;
  if (!/\.(xlsx|xlsm)$/i.test(file.name)) { rep = { busy: false, err: `${file.name} is not an Excel workbook (.xlsx or .xlsm).`, name: "", last: null }; render(); return; }
  rep = { busy: true, err: "", name: file.name, last: null }; render();
  try {
    const got = await api(`/api/console/jobs/${encodeURIComponent(j.id)}/source?${new URLSearchParams({ name: file.name })}`, { method: "PUT", headers: who(), body: file });
    rep = { busy: false, err: "", name: "", last: got.replace || null };
    S = got && got.pages ? got : await api("/api/console/state");
    if (window.ISSUES) ISSUES.state.key = "";   // the groups are made again from the new file
  } catch (e) { rep = { busy: false, err: `${file.name}: ${e.message}`, name: "", last: null }; toast(e.message, true); }
  render();
}

/* ---------- attach a file to the new job; decide a question ---------- */
async function attach(file) {
  /* the console reads Excel workbooks only; the server refuses anything else with the same reason */
  if (!/\.(xlsx|xlsm)$/i.test(file.name)) { att = { busy: false, err: `${file.name} is not an Excel workbook (.xlsx or .xlsm). Attach one of those.`, name: "" }; render(); return; }
  att = { busy: true, err: "", name: file.name }; render();
  const a = S && S.ask, q = new URLSearchParams({ name: file.name });
  if (a && a.sponsor) q.set("sponsor", a.sponsor);   // the sponsor and record type the ask screen has selected
  if (a && a.type) q.set("type", a.type);
  try {
    const got = await api(`/api/console/jobs/ask/attach?${q.toString()}`, { method: "PUT", headers: who(), body: file });
    att = { busy: false, err: "", name: "" };
    S = got && got.pages ? got : await api("/api/console/state");
  } catch (e) { att = { busy: false, err: `${file.name}: ${e.message}`, name: "" }; toast(e.message, true); }
  render();
}
async function answerQuestion(qid, optId) {
  const j = S && S.job, b = j ? QUESTIONS.build(j, qid, optId) : null;
  if (!b) { toast("Choose one of the options first.", true); return; }
  if (!reviewer()) { toast("Say who is deciding first: type your name in the Who is deciding box.", true); const r = $("reviewer"); if (r) r.focus(); return; }
  const body = { question_id: b.id, scope: b.scope, reason: b.reason, ...(b.custom ? { custom: b.custom } : { option_id: b.option }) };
  if (b.custom && !String(b.reason || "").trim()) { toast("A column of your own needs a reason.", true); const r = document.querySelector(`[data-q-reason="${CSS.escape(b.id)}"]`); if (r) r.focus(); return; }
  try {   // the REST form: it carries a custom column, the reason and the reviewer name
    const got = await api(`/api/console/jobs/${encodeURIComponent(j.id)}/answer`, { method: "POST",
      headers: { "Content-Type": "application/json", ...who() }, body: JSON.stringify(body) });
    S = got;
  } catch (e) { toast(e.message, true); return; }
  QUESTIONS.saved(j, b.id, b.savedText);
  render();
}
async function undoAnswer(qid) {
  const j = S && S.job; if (!j) return;
  if (!reviewer()) { toast("Say who is deciding first: type your name in the Who is deciding box.", true); return; }
  if (await act(`undo_answer|${j.id}|${qid}`, "undone from the question card")) { QUESTIONS.forget(j, qid); render(); }
}

/* ---------- the sponsor workspace ---------- */
function sponsorView() {
  const sp = S.sponsors.find((s) => s.name === S.sponsor), w = S.workspace;
  if (!w.entities.length) {
    return `<div class="sub"><button class="lnk" data-act="view|dash">‹ All sponsors</button> / ${esc(sp.name)}</div><div><h1>${esc(sp.name)}</h1><div class="sub">Data migration</div></div>
      <div class="card" style="display:grid;gap:10px"><div class="eyebrow">Step 2 of onboarding</div><h2>Load the entities you have received so far</h2>
      <div class="sub">Each entity has an entity ID, a name and its own go-live date, and may belong to a fund group. Load only the funds you have now; the rest can follow with later go-live dates.</div>
      <div class="row"><button class="btn pri" data-ob="add|${esc(sp.name)}">Load entities</button><span class="faint">Jobs open once entities are loaded.</span></div></div>`;
  }
  const tile = (t) => `<button class="tile ${t.locked ? "lock" : ""}" ${t.locked ? "" : `data-act="start|${esc(S.sponsor)}|${esc(t.type)}"`}><b>${esc(t.type)}</b>${t.locked
    ? `<span class="faint">${ic("lock")} opens when all five static types are approved for the sponsor (${t.static_done}/5)</span>`
    : `<span>${t.approved ? pill("ok", "Approved", "check") : pill("mute", "Not started")}</span>`}</button>`;
  const statics = w.tiles.filter((t) => !t.gl), gls = w.tiles.filter((t) => t.gl);
  const tabs = [["jobs", "Jobs"], ["signoff", "Sign-off"], ["activity", "Activity"]];
  let tab = "";
  if (w.tab === "jobs") {
    tab = `<div class="tw"><table><tr><th>Job</th><th>Fund</th><th class="n">Rows</th><th>Status</th><th>Updated</th></tr>${S.jobs.map((j) => `<tr class="hov"><td>${j.status === "pending" ? `<button class="lnk" data-act="open|${esc(j.id)}">${esc(j.type)}</button>` : esc(j.type)}</td><td>${esc(j.fund)}</td><td class="n">${j.rows}</td><td>${j.status === "approved" ? pill("ok", "Approved", "check") : pill("warn", "Pending approval")}</td><td>${esc(j.t)}</td></tr>`).join("") || '<tr><td colspan="5" class="faint">No jobs run in this session yet. Start one.</td></tr>'}</table></div>`;
  } else if (w.tab === "activity") {
    tab = `<div class="tw"><table><tr><th>Time</th><th>Who</th><th>Action</th><th>Reason</th></tr>${w.activity.map((a) => `<tr><td class="mono">${esc(a.t)}</td><td>${esc(a.who)}</td><td style="white-space:normal">${esc(a.a)}</td><td style="white-space:normal">${esc(a.r)}</td></tr>`).join("") || '<tr><td colspan="4" class="faint">Nothing yet.</td></tr>'}</table></div>`;
  } else {
    tab = `<div class="sub" style="margin-bottom:10px">Per-fund chain: Static → GL → Bundle → Client services → Migrated. A stage opens when the one before is signed off.</div>${w.chain.map((c) => `<div class="card" style="margin-bottom:10px"><div class="row between"><span><b>${esc(c.entity)}</b> <span class="sub mono">${esc(c.id)}</span> <span class="sub">· go-live ${esc(c.live)}</span></span>${c.migrated ? pill("ok", "MIGRATED TO LEDGERLINE", "check") : ""}</div>
      <div class="chain" style="margin-top:8px">${c.stages.map((x, i) => `<span>${x.state === 1 ? pill("ok", x.name, "check") : x.state === -1 ? pill("err", x.name, "x") : x.open ? pill("info", x.name) : pill("mute", x.name, "lock")}</span>${i < 4 ? '<span class="faint">→</span>' : ""}`).join("")}</div>
      <div class="row" style="margin-top:10px">${c.current >= 0 ? ((x) => `<span class="sub">${esc(x.name)}: ${esc(x.text)}</span><button class="btn sm pri" data-act="so|${x.action}|${esc(c.entity)}" ${x.open ? "" : "disabled"}>${esc(x.label)}</button>`)(c.stages[c.current]) : '<span class="sub">All stages complete.</span>'}</div></div>`).join("")}`;
  }
  return `<div class="sub"><button class="lnk" data-act="view|dash">‹ All sponsors</button> / ${esc(sp.name)}</div>
   <div class="row between"><div><h1>${esc(sp.name)}</h1><div class="sub"><span class="mono">${esc(sp.id)}</span> · go-live ${esc(sp.live)} · Data migration</div></div>
    <div class="row"><span data-rx="fund"></span>
    <button class="btn" data-ob="add|${esc(S.sponsor)}">Add entities</button>
    <button class="btn pri" data-act="start|${esc(S.sponsor)}">+ Start new job</button></div></div>
   <div class="card"><div class="eyebrow">Migration lifecycle</div><div class="life">${w.lifecycle.map((l, i) => `<div class="${l.state}"><b>${i + 1}. ${esc(l.name)}</b><div class="sub">${esc(l.text)}</div></div>`).join("")}</div></div>
   <div class="card" style="display:grid;gap:14px"><div class="row between"><div><h2>Migration workspace · ${esc(w.entity)}</h2><div class="sub">${w.static_done} of 5 static types approved for ${esc(sp.name)}</div></div>${w.static_done === 5 ? pill("ok", "Static complete", "check") : pill("warn", w.static_done + "/5 complete")}</div>
    <div class="tiles" style="grid-template-columns:repeat(auto-fit,minmax(170px,1fr))">${statics.map(tile).join("")}</div>
    <div class="eyebrow">General ledger</div><div class="tiles" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr))">${gls.map(tile).join("")}</div></div>
   <div class="tiles"><div class="card"><h2>Existing in Ledgerline</h2><p class="sub">Records already live in Ledgerline. IDs are used as is. New jobs leave matches out and use these as references.</p><div class="row"><button class="btn" data-act="view|existing">Upload existing records →</button><span class="faint">${Object.entries(sp.store).map(([t, n]) => `${n} ${t.toLowerCase()}`).join(" · ")}</span></div></div>
    <div data-rx="settings"></div>
    <div class="card"><h2>ID counters</h2><p class="sub">One counter per sponsor and record type, continuing across funds.</p><div class="faint">${Object.entries(sp.counters).map(([t, n]) => `${esc(t)} next ${n}`).join(" · ") || "not started"}</div></div></div>
   <div class="card"><div class="chips">${tabs.map((t) => `<button class="${w.tab === t[0] ? "on" : ""}" data-act="tab|${t[0]}">${t[1]}</button>`).join("")}</div><div style="margin-top:14px">${tab}</div></div>`;
}

/* ---------- the review pane ----------
   Issues and Reconcile put one group in the chat as a review: its records first, fitted to the height the pane has (rows per
   page come from the room, not from a fixed 50), one line per row with the full value on hover, a pager with the exact
   total, and an action bar that stays in view. Nothing in it scrolls. Shared helpers only; the data is the module's. */
const PANE = (window.PANE = {
  ROW: 30, HEAD: 30,
  /* rows that fit the records region as drawn now, or 0 before it exists */
  per(id) {
    const el = $(id);
    return el && el.clientHeight ? Math.max(1, Math.min(200, Math.floor((el.clientHeight - PANE.HEAD) / PANE.ROW))) : 0;
  },
  /* a first guess before the region is drawn: the window less the pane's fixed parts */
  guess() { return Math.max(3, Math.min(200, Math.floor(((window.innerHeight || 700) - 330) / PANE.ROW))); },
  /* characters that fit one value cell: the pane is a fixed share of the window (see .cop.rvm) */
  chars() {
    const iw = window.innerWidth || 1280;
    const w = Math.min(620, Math.max(440, iw * 0.34), iw - 32);
    return Math.max(10, Math.floor(((w - 24 - 52) * 0.74 / 2 - 12) / 7.6));
  },
  /* a long value keeps its start and its end (the digits that tell two IDs apart); the whole value is the tooltip */
  mid(t) { const n = PANE.chars(); return t.length <= n ? t : t.slice(0, Math.ceil((n - 1) / 2)) + "…" + t.slice(-Math.floor((n - 1) / 2)); },
  cell(v, cls = "") { const t = String(v ?? ""); return `<td class="${cls}" title="${esc(t)}">${t ? esc(/mono/.test(cls) ? PANE.mid(t) : t) : '<span class="faint">(blank)</span>'}</td>`; },
  /* "rows a-b of N", Prev / Next, a jump box and a Copy link; `k` is the module's data-key (is | rc) */
  pager(k, p, unit, jump, tsv) {
    return `<div class="rv-pg"><span>${unit} ${nf(p.start)}-${nf(p.end)} of ${nf(p.total)}</span>
      <button class="btn sm" data-${k}="rp|-1" ${p.page ? "" : "disabled"}>‹ Prev</button><button class="btn sm" data-${k}="rp|1" ${p.page < p.pages - 1 ? "" : "disabled"}>Next ›</button>
      <input id="${jump}" type="number" min="1" max="${p.pages}" placeholder="page" aria-label="Go to page, of ${nf(p.pages)}"><button class="lnk" data-${k}="rj">Go</button>
      <button class="lnk" data-copy="${esc(tsv)}">Copy this page</button></div>`;
  },
});
let opener = "";   // selector of the Review button that opened the pane (set by issues.js / recon.js): focus goes back to it
function closeChat() {
  collapsed = true; render();
  const t = (opener && document.querySelector(opener)) || document.querySelector(".cop-fab");
  if (t && t.focus) t.focus();
}

/* ---------- chat ---------- */
function chatHtml(msgs, nextMsg, placeholder, where = "", extra = "") {
  const bub = (m) => m.who === "you" ? `<div class="bub you">${esc(m.text)}</div>`
    : `<div class="bub"><div class="row" style="gap:8px"><span aria-hidden="true" style="width:22px;height:22px;border-radius:50%;display:grid;place-items:center;font-size:11px;font-weight:600;background:var(--navy);color:var(--navy-ink)">W</span><span class="eyebrow">Worker</span>${m.role ? `<span class="faint" style="font-size:11.5px">${esc(m.role)}</span>` : ""}</div>
       <div style="white-space:pre-wrap">${esc(m.text)}</div>${(m.options || []).length ? `<div class="row">${m.options.map((o) => `<button class="btn sm ${o.primary ? "pri" : ""}" data-act="${esc(o.action)}">${esc(o.label)}</button>`).join("")}</div>` : ""}</div>`;
  const all = msgs.concat(nextMsg && nextMsg.text ? [{ who: "w", role: "", text: nextMsg.text, options: nextMsg.options }] : []);
  /* a review: the pane shows the group, and the worker's newest answer since it opened sits in the action bar */
  const rv = extra && typeof extra === "object" ? extra : null;
  let fresh = null;   // the worker's answer to what the person sent since the review opened: the preview with its Apply, else the reply
  if (rv) {   // `since` is how many sentences the person had sent when the review opened (a count: the server may trim older status lines)
    const u = msgs.map((m) => m.who).lastIndexOf("you");
    const sent = msgs.filter((m) => m.who === "you").length;
    const reply = sent > (rv.since || 0) ? msgs.slice(u + 1) : [];
    fresh = reply.filter((m) => (m.options || []).length).pop() || reply[0] || null;
  }
  const bar = (m) => `<div class="rv-msg" role="status"><div class="rv-t">${esc(m.text)}</div>${(m.options || []).length ? `<div class="row">${m.options.map((o) => `<button class="btn sm ${o.primary ? "pri" : ""}" data-act="${esc(o.action)}">${esc(o.label)}</button>`).join("")}</div>` : ""}</div>`;
  const body = rv ? `<div class="rvp" id="rvp">${rv.html(fresh ? bar(fresh) : "", !!fresh)}</div>` : `<div class="msgs" id="msgs">${all.map(bub).join("")}${extra}</div>`;
  return `<aside class="cop ${pinned ? "" : "ov"}${rv ? " rvm" : ""}" aria-label="Chat with the worker" role="${pinned ? "complementary" : "dialog"}"><div class="cop-h"><span style="flex:1;min-width:0"><span class="eyebrow">Worker</span><div class="faint" style="font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(where)}</div></span><button class="btn sm" data-lay="pin" aria-pressed="${pinned}" title="${pinned ? "Unpin: float over the page" : "Pin: dock beside the page"}">${pinned ? "Unpin" : "Pin"}</button><button class="btn sm" data-cop="0" aria-label="Close the chat" title="Close ( Esc )">${ic("x")}</button></div>
   ${body}
   <div class="cop-in"><div class="row" style="flex-wrap:nowrap"><input id="rb" style="flex:1;min-height:38px;border-radius:19px;padding:6px 14px" placeholder="${esc(placeholder)}" value="${esc(draft)}" aria-label="Message the worker"><button class="btn pri" id="send" style="border-radius:19px">Send</button></div>
   <div class="faint" style="font-size:12px">Press / to type here</div></div></aside>`;
}

/* the page function for a shell view, or null: PAGES (harness pages register here) first, then the built-in ones */
const HARNESS_VIEWS = ["impr", "health", "skills", "skill"];
function shellPage(v) {
  if (PAGES[v]) return PAGES[v];
  if (HARNESS_VIEWS.includes(v) && window.HARNESS) return () => `<div id="hx" data-hv="${esc(v)}"></div>`;
  const pg = S.pages;
  if (v === "dash" && pg.dashboard) return dashView;
  if (["pending", "rejected", "aging"].includes(v) && pg.list) return listPage;
  if (v === "tmpls" && pg.standards) return stdTemplates;
  if (v === "coa" && pg.standards) return stdCoa;
  if (v === "ttm" && pg.standards) return stdTtm;
  if (v === "settings" && pg.settings) return modelsV;
  if (v === "existing" && window.RECORDS) return () => `<div data-rx="existing" style="display:grid;gap:14px"></div>`;
  return null;
}

function render() {
  const keep = document.activeElement && document.activeElement.id && ["rb", "apnote", "is_q", "rvh", "is_jump", "rc_jump", "is_who", "reviewer"].includes(document.activeElement.id)
    ? { id: document.activeElement.id, start: document.activeElement.selectionStart, end: document.activeElement.selectionEnd } : null;
  $("side").innerHTML = side();
  const j = S.job, a = S.ask;
  let main, chat = "", rvOn = false;
  const chatOn = !!(j || a);
  if (j && j.ex_pending && exAuto !== j.id) { exAuto = j.id; collapsed = false; }  // a pending question opens the chat once
  if (j) {
    main = jobMain(j);
    const ex = (window.RECON && RECON.chat(j)) || (window.ISSUES ? ISSUES.chat(j) : "");
    rvOn = !!ex && typeof ex === "object";
    chat = chatHtml(j.chat, null, j.ex_pending ? "Answer the worker, e.g. “no, none”" : "Ask why or change a rule",
      `${j.sponsor} › ${j.type}`, ex);
  } else if (a) {
    main = `<button class="lnk" style="justify-self:start" data-act="switch|${esc(S.sponsor)}">‹ Back to ${esc(S.sponsor)}</button>
      <div><div class="eyebrow">${esc(S.sponsor)}</div><h1>New job</h1></div><div class="card" style="padding:12px 8px">${timeline(S.ask_timeline || [
        { stage: "Read", state: 0, fact: a.file ? "file in, read on Start" : "waiting for the file" }, { stage: "Map", state: 0, fact: "" },
        { stage: "Convert", state: 0, fact: "" }, { stage: "Check", state: 0, fact: "" }, { stage: "Write", state: 0, fact: "" }])}</div>${draftMain(a)}`;
    chat = chatHtml(a.messages, a.next, "Tell the worker what you need", `${S.sponsor} › New job`);
  } else if (shellPage(S.view)) {
    main = shellPage(S.view)();
  } else {
    main = sponsorView();
  }
  const need = j && j.ran ? (j.tally.err || 0) + (j.questions_blocking || []).length : (j && j.ex_pending ? 1 : 0);
  const docked = chatOn && pinned && !collapsed;
  $("page").className = "page wide";
  $("page").innerHTML = `<div class="ws ${docked ? "" : "one"}${chatOn && !docked && !collapsed ? " ovo" : ""}${rvOn ? " rvm" : ""}"><div class="canvas">${main}</div>${docked ? chat : ""}</div>${
    !chatOn ? "" : collapsed ? `<button class="btn pri cop-fab" data-cop="1" aria-label="Open the chat with the worker${need ? `, ${need} need you` : ""}" title="Worker chat ( ] )">${ic("chat")}${need ? `<span class="badge">${need > 99 ? "99+" : need}</span>` : ""}</button>`
      : docked ? "" : chat}`;
  layApply();
  renderModal();
  if (window.LEARNING && j) document.querySelectorAll("[data-learning]").forEach((h) => window.LEARNING.render(h.dataset.learning, j, h));
  if (window.IDGEN && j) document.querySelectorAll("[data-idgen]").forEach((h) => window.IDGEN.render(h, { job: j, sponsor: S.sponsor }));
  if (window.RESHAPE && j) document.querySelectorAll("[data-reshape]").forEach((h) => window.RESHAPE.render(h, j));
  if (window.RECORDS) document.querySelectorAll("[data-rx]").forEach((el) => window.RECORDS.render(el.dataset.rx, el, { sponsor: S.sponsor }));
  if (window.ISSUES && j) ISSUES.sync(j);
  if (window.RECON && j) RECON.sync(j);
  if (window.ISSUES && j && ISSUES.fit) ISSUES.fit();   // rows per page follow the height the review pane has
  if (window.RECON && j && RECON.fit) RECON.fit();
  const wk = $("wk"); if (wk && j && window.WORKING) window.WORKING.render(wk.dataset.tab, { id: j.id, type: j.type }, wk);
  const hx = $("hx"); if (hx && window.HARNESS) window.HARNESS.render(hx.dataset.hv, hx, { sponsor: S.sponsor });
  if (!j && !a && (S.view === "coa" || S.view === "ttm") && (std.g !== "All" || std.coaQ || std.ttQ)) stdFetch();
  const m = $("msgs");
  if (m) {   // a new worker message (a preview with its Apply) is brought into view even when a review card follows it
    const conv = j ? j.chat : a ? a.messages : [], last = conv.length ? conv[conv.length - 1] : null;
    const key = last ? `${j ? j.id : "ask"}:${conv.length}:${last.text}` : "";
    const lastEl = m.querySelectorAll(":scope > .bub");
    if (key && key !== lastMsgKey && lastEl.length >= conv.length && conv.length) m.scrollTop = Math.max(0, lastEl[conv.length - 1].offsetTop - m.offsetTop - 8);
    else m.scrollTop = m.scrollHeight;
    lastMsgKey = key;
  }
  if (!keep && ((S.ask && !S.job) || (S.job && S.job.ex_pending))) { const rb = $("rb"); if (rb) rb.focus(); }
  if (keep) { const el = $(keep.id); if (el) { el.focus(); try { el.setSelectionRange(keep.start, keep.end); } catch (e) { /* not a text input */ } } }
  const rb = $("rb"); if (rb && draft === "") rb.value = "";
  if (S.toast) {   // the server keeps a "check done" toast until it is closed: show it once, not on every load
    toast(S.toast.text); S.toast = null;
    fetch("/api/console/act", { method: "POST", headers: { "Content-Type": "application/json", ...who() },
      body: JSON.stringify({ action: "toast_close", text: "", context: {} }) }).catch(() => {});
  }
  live(j && j.tally ? `${j.tally.out} ready, ${j.tally.err} need you` : "");
}

/* the shell's top bar and the navigation state: icons only when collapsed, an overlay on a narrow screen */
function layApply() {
  $("app").className = "app" + (navX ? " nc" : "") + (navO ? " no" : "");
  $("tbar").innerHTML = `<button class="btn sm navtg" data-lay="navo" aria-label="Open the navigation" title="Navigation ( [ )">${ic("dash")}</button>
    <button class="btn sm navwide" data-lay="nav" aria-pressed="${navX}" aria-label="Collapse or expand the navigation" title="Navigation ( [ )">${ic("dash")}</button>
    <span style="flex:1"></span>`;
}

/* ---------- events ---------- */
/* a records write (existing records, go-live, ID settings) changes counts and headers: reload the state once */
window.addEventListener("records:changed", async () => { try { S = await api("/api/console/state"); render(); } catch (e) { toast(e.message, true); } });
document.addEventListener("click", (e) => {
  if (swPop && !e.target.closest(".pop,[data-sw]")) { swPop = false; render(); }
  const t = e.target.closest("[data-act],[data-att],[data-src],[data-q-opt],[data-q-confirm],[data-q-undo],[data-q-chat],[data-cop],[data-copy],[data-lay],[data-sw],[data-ob],[data-std],[data-md],#send");
  if (!t) return;
  if (t.tagName === "A") e.preventDefault();
  if (t.dataset.sw !== undefined) { swPop = !swPop; render(); return; }
  if (t.dataset.src !== undefined) { const f = $("srcfile"); if (f) f.click(); return; }
  if (t.dataset.att !== undefined) { const f = $("attfile"); if (f) f.click(); return; }
  if (t.dataset.qOpt !== undefined) { const [qid, oid] = t.dataset.qOpt.split("|"); QUESTIONS.pick(qid, oid); render(); return; }
  if (t.dataset.qConfirm !== undefined) { answerQuestion(t.dataset.qConfirm); return; }
  if (t.dataset.qUndo !== undefined) { undoAnswer(t.dataset.qUndo); return; }
  if (t.dataset.qChat !== undefined) { if (collapsed) { collapsed = false; render(); } const rb = $("rb"); if (rb) rb.focus(); return; }
  if (t.dataset.md !== undefined) { mdCall(t.dataset.md); return; }
  if (t.dataset.std !== undefined) {
    const [k, v] = t.dataset.std.split("|");
    if (k === "g") { std.g = v; render(); stdFetch(); }
    else if (k === "up" || k === "new") stdPick(k, v);
    return;
  }
  if (t.dataset.ob !== undefined) {
    const [k, sp] = t.dataset.ob.split("|");
    if (k === "new" || k === "add") obOpen(k, sp);
    else if (!onb) return;
    else if (k === "close") { onb = null; renderModal(); }
    else if (k === "browse") { const f = $("obfile"); if (f) f.click(); }
    else if (k === "sample") obSample();
    else if (k === "clearf") { onb.list = onb.list.filter((r) => !r.fromFile); onb.file = ""; onb.read = 0; onb.skipped = []; renderModal(); }
    else if (k === "add1") obAdd1();
    else if (k === "rm") { onb.list.splice(+sp, 1); renderModal(); }
    else if (k === "save") obSave();
    return;
  }
  if (navO && t.dataset.act && t.closest && t.closest("#side")) { navO = false; layApply(); }   // a phone drawer closes on a choice
  if (t.id === "send") { sendDraft(); return; }
  if (t.dataset.cop !== undefined) {
    if (t.dataset.cop === "0") { closeChat(); return; }
    collapsed = false; render();
    const f = $("rvh") || $("rb"); if (f) f.focus();
    return;
  }
  if (t.dataset.lay !== undefined) {
    const k = t.dataset.lay;
    if (k === "navo") navO = !navO; else if (k === "navc") navO = false;
    else if (k === "nav") { navX = !navX; laySave(); }
    else if (k === "pin") { pinned = !pinned; laySave(); }
    render(); return;
  }
  if (t.dataset.copy !== undefined) {   // say "Copied" only when the browser did it: a refusal or a missing clipboard is told, not hidden
    const sorry = () => toast("Could not copy: this browser did not allow it. Select the text and copy it by hand.", true);
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(t.dataset.copy).then(() => toast("Copied"), sorry);
    else sorry();
    return;
  }
  if ((t.dataset.act || "").startsWith("goto|")) { location.hash = "tab=" + t.dataset.act.slice(5); return; }   // a chat button that points at a screen
  if (t.dataset.act === "allow_empty") { act("allow_empty", ($("apnote") || {}).value || ""); return; }
  if (t.dataset.act === "approve") {
    act("approve", ($("apnote") || {}).value || "").then(() => {
      if (S.job && S.job.approved && S.job.download) { const a = document.createElement("a"); a.href = S.job.download; a.download = ""; document.body.appendChild(a); a.click(); a.remove(); }
    });
    return;
  }
  if (t.dataset.act) {   // a slow action (Start reads the whole file) shows it is working instead of looking stuck
    const lab = t.textContent;
    t.disabled = true; t.setAttribute("aria-busy", "true"); t.textContent = lab.replace(/\s*→$/, "") + "…";
    act(t.dataset.act).then((ok) => { if (!ok && t.isConnected) { t.disabled = false; t.removeAttribute("aria-busy"); t.textContent = lab; } });
  }
});
document.addEventListener("click", async (e) => {
  if (e.target.closest && e.target.closest("[data-signout]")) {
    try { await fetch("/api/auth/logout", { method: "POST" }); } catch (x) { /* the page moves on either way */ }
    location.href = "/login";
  }
});
document.addEventListener("change", (e) => {
  const t = e.target;
  if (t.id === "srcfile" && t.files[0]) { replaceSource(t.files[0]); t.value = ""; return; }
  if (t.id === "attfile" && t.files[0]) { attach(t.files[0]); t.value = ""; return; }
  if (t.dataset.qScope !== undefined) { QUESTIONS.setScope(t.dataset.qScope, t.value); return; }
  if (t.dataset.qCol !== undefined) { QUESTIONS.pickColumn(t.dataset.qCol, t.value); render(); return; }
  if (t.id === "obfile" && onb && t.files[0]) { obUpload(t.files[0]); t.value = ""; return; }
  if (onb && t.dataset.obr !== undefined) { const r = onb.list[+t.dataset.obr]; if (r) { r.live = t.value; renderModal(); } return; }
  if (t.dataset.set) { act(`ask_set|${t.dataset.set}|${t.value}`); return; }
  if (t.dataset.ent) { act("ask_ent|" + t.dataset.ent); return; }
  if (t.dataset.req && S.job) { act(`req|${S.job.type}|${t.dataset.req}|${t.checked ? 1 : 0}`); return; }
  if (t.dataset.col && t.value.startsWith("@gen|")) { if (window.IDGEN) IDGEN.pick(S.sponsor, S.job.type, t.value.slice(5), ""); return; }
  if (t.dataset.col && S.job && window.IDGEN && IDGEN.isIdField(S.sponsor, S.job.type, t.dataset.col)) { IDGEN.pick(S.sponsor, S.job.type, "source", t.value); return; }
  if (t.dataset.col) { act(`col|${t.dataset.col}|${t.value}`); return; }
  if (t.dataset.f) { act(`${t.dataset.f}|${t.value}`); return; }
  if (t.dataset.actChange) { act(`${t.dataset.actChange}|${t.value}`); }
});
function sendDraft() {
  const rb = $("rb"), v = (rb ? rb.value : "").trim();
  if (!v) return;
  draft = ""; if (rb) rb.value = "";
  act("say", v).then((ok) => { const n = $("rb"); if (n) { if (!ok && !n.value) { n.value = v; draft = v; } n.focus(); } });   // a refused sentence is kept to edit
}
document.addEventListener("keydown", (e) => {
  const tag = (e.target.tagName || "").toLowerCase();
  if (e.key === "Enter" && e.target.id === "rb") { e.preventDefault(); sendDraft(); return; }
  /* Esc closes a floating pane from anywhere in it, a typed box included, and focus goes back to the Review button */
  if (e.key === "Escape" && !collapsed && !pinned && chatHere() && !navO && !onb) { e.preventDefault(); closeChat(); return; }
  if (tag === "input" || tag === "select" || tag === "textarea") return;
  if (e.key === "[" && !e.ctrlKey && !e.metaKey) { if (window.matchMedia("(max-width:820px)").matches) navO = !navO; else { navX = !navX; laySave(); } render(); return; }
  if (e.key === "]" && !e.ctrlKey && !e.metaKey && chatHere()) { if (collapsed) { collapsed = false; render(); } else closeChat(); return; }
  if (e.key === "Escape" && navO) { navO = false; render(); return; }
  if (e.key === "Escape" && !collapsed && !pinned && chatHere()) { collapsed = true; render(); return; }
  if (e.key === "/" && chatHere()) { e.preventDefault(); if (collapsed) { collapsed = false; render(); } const rb = $("rb"); if (rb) rb.focus(); return; }
  const qs = S && S.job && !S.job.ex_pending ? QUESTIONS.order(S.job.questions || []) : [];
  if (qs.length) {   // open questions come first: j / k move between the cards, a accepts the recommended option
    qsel = Math.min(qs.length - 1, Math.max(0, qsel));
    if (e.key === "j" || e.key === "k") {
      qsel = Math.min(qs.length - 1, Math.max(0, qsel + (e.key === "j" ? 1 : -1))); render();
      const el = document.querySelector("[data-q].qsel"); if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
    } else if (e.key === "a") {
      const r = QUESTIONS.recommended(qs[qsel]); if (r) answerQuestion(qs[qsel].id, r.id);
    }
    return;
  }
  if (window.ISSUES && S && S.job && !S.job.ex_pending && ISSUES.key(e)) e.preventDefault();
});
document.addEventListener("dragover", (e) => { if (e.target.closest && e.target.closest("[data-drop]")) e.preventDefault(); });
document.addEventListener("drop", (e) => {
  const z = e.target.closest && e.target.closest("[data-drop]");
  if (z && z.dataset.drop === "attach") {
    e.preventDefault();
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) attach(f);
    return;
  }
  if (z && z.dataset.drop === "ob" && onb) {
    e.preventDefault();
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) obUpload(f);
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && onb && ["ob_id", "ob_n", "ob_live", "ob_gid", "ob_gn"].includes(e.target.id)) { e.preventDefault(); obAdd1(); }
  if (e.key === "Escape" && onb) { onb = null; renderModal(); }
});
document.addEventListener("input", (e) => {
  if (e.target.id === "coaq" || e.target.id === "ttq") {
    if (e.target.id === "coaq") std.coaQ = e.target.value; else std.ttQ = e.target.value;
    clearTimeout(stdTimer); stdTimer = setTimeout(stdFetch, 150);
  }
});
let stdTimer = null;
document.addEventListener("input", (e) => {
  if (e.target.id === "rb") draft = e.target.value;
  if (e.target.dataset && e.target.dataset.qReason !== undefined) QUESTIONS.setReason(e.target.dataset.qReason, e.target.value);
  if (e.target.id === "reviewer" && !signedIn()) { reviewerName = e.target.value; store.set("reviewer", e.target.value.trim()); }
});
(async () => {
  try { S = await api("/api/console/state"); } catch (e) { document.body.textContent = e.message; return; }
  render();
  const rb = $("rb"); if (rb && !S.job) rb.focus();
})();
