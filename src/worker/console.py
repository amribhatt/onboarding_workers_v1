"""The console's brain: sponsors, entities, what is approved, and a job that starts in the chat.

State is one JSON-able dict (`Console.S`). A job is a conversation: the person says what they need, the worker
infers the rest and asks one question at a time, then plan -> convert -> check -> approve. Every number comes from
`engine.evaluate` over the real source workbook (through `worker.console_engine`); this module decides what happens
next and writes the audit.
Behaviour follows `docs/mockup/onboarding-console.html` (askTalk, askGo, runJob, talk, applyS, finish).
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from worker import console_demo as D
from worker import console_engine as CE
from worker import engine_console as EC
from worker import idgen as I
from worker import onboarding as OB
from worker import persist
from worker import record_facts as RF
from worker import templates as T
from worker.audit import AuditError
from worker.errors import StatusError
from worker.registry import Registry
from worker.skills import SkillLoadRefused, require_valid_skills

REQUIRED_BY_STATIC = 5
FREE = "free"


USER_NAV = frozenset({"switch", "view", "fund", "tab", "open", "next", "trace", "toast_close"})  # moving around: no data changes


class ConsoleError(StatusError):
    """The person asked for something the current state does not allow; the message says what to do."""


def _blank_ask(sp: str, ents: list[str], type_: str | None = None) -> dict[str, Any]:
    return {"sp": sp, "text": "", "file": "", "path": "", "size": 0, "type": type_, "fund": ents[0] if ents else "",
            "cov": None, "ents": [], "msgs": [], "file2": "", "typeSet": False}


def infer_type(text: str) -> str | None:
    t = (text or "").lower()
    if re.search(r"gl lp|lp ledger|lp level|lp gl", t):
        return "GL LP"
    if re.search(r"commit|allocation", t):
        return "Commitments"
    if re.search(r"investment|deal|soi|roll.?forward", t):
        return "Investments"
    if re.search(r"vendor|payee", t):
        return "Vendors"
    if re.search(r"affiliat", t):
        return "Affiliates"
    if re.search(r"investor", t):
        return "Investors"
    if re.search(r"\bgl\b|ledger|journal|_gl_", t):
        return "GL Fund"
    return None


class _World:
    """The engine's view of one sponsor."""

    def __init__(self, c: Console, sponsor: str) -> None:
        self.c, self.sponsor = c, sponsor

    def store(self, record_type: str) -> list[dict[str, Any]]:
        return self.c.reg_of(self.sponsor).get(record_type, [])

    def required(self, record_type: str, field_name: str, default: bool) -> bool:
        return self.c.required_value(f"{self.sponsor}|{record_type}|{field_name}", default)

    def known_account(self, source_account: str, transaction_type: str) -> str:
        return self.c.known_account(self.sponsor, source_account, transaction_type)


class Console:
    def __init__(self, inbox: str | Path, state_file: str | Path | None = None, provider: str = "paid") -> None:
        self.inbox = Path(inbox)
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.state_file = Path(state_file) if state_file else None
        self.root = self.state_file.parent if self.state_file else self.inbox.parent
        if self.state_file:
            persist.use_state_dir(self.root)
        # the controllers (WORKER_ADMINS): the only people who can lower something the organisation requires
        self.controllers: list[str] = [a.strip() for a in os.environ.get("WORKER_ADMINS", "").split(",") if a.strip()]
        self.out_dir = self.root / "console_out"
        self.learning_key: str | bytes | None = None  # WORKER_LEARNING_KEY when set; none: no cross-sponsor hints
        self.chat_models: dict[str, Any] = {}  # a role's chat model when it is not the one the settings name (tests)
        self._recipes: dict[str, Any] = {}
        self._document = persist.console_document(self.state_file)  # the file, or a compare-and-set database row
        saved = self._document.load() if self._document is not None else None
        if saved is not None:
            self.S = json.loads(saved)
            self._migrate_store()
            self._take_over_counters(self.S.pop("ctr", None) or {})
        else:
            self.S = self._seed(provider)
            self._seed_store()

    # ---- state -------------------------------------------------------------------------------------------
    def _seed(self, provider: str) -> dict[str, Any]:
        st: dict[str, Any] = {
            "sp": "Sponsor A", "fund": "Fund A", "prov": provider,
            "sponsors": [dict(s) for s in D.SPONSORS],
            "ent": {k: list(v) for k, v in D.ENTITIES.items()},
            "EM": {k: dict(v) for k, v in D.ENTITY_META.items()},
            "F": {}, "wf": {}, "ext": {}, "req": {}, "lsuffix": {},
            "audit": [], "jobs": [], "fb": [], "ask": None, "job": None, "toast": None, "book": {}, "seq": 0,
        }
        for names in st["ent"].values():
            for n in names:
                st["F"].setdefault(n, self._new_progress())
        # Sponsor B: five approved static jobs for B Fund I, with their store, counters and audit
        counts = {"Investors": 5, "Commitments": 5, "Investments": 1, "Vendors": 1, "Affiliates": 1}
        for t in RF.static_types():
            st["wf"][f"Sponsor B|{t}"] = True
            st["jobs"].append({"id": uuid.uuid4().hex[:8], "sp": "Sponsor B", "type": t, "fund": "B Fund I",
                               "rows": counts[t], "status": "approved", "t": "2026-10-02", "seed": True})
            st["audit"].append({"t": "2026-10-02", "who": "analyst", "a": f"Approved and wrote the {t.lower()} "
                                f"output for B Fund I ({counts[t]} records)", "r": "human approval; the AI prepared it",
                                "job": t, "sp": "Sponsor B", "d": "2026-10-02"})
            st["F"]["B Fund I"]["static"][t] = 1
        for t in ("Investors", "Vendors", "Affiliates", "Investments"):
            st["ext"][f"Sponsor B|{t}"] = {"ans": "none"}
        return st

    @staticmethod
    def _new_progress() -> dict[str, Any]:
        return {"static": {}, "gl": {"GL Fund": 0, "GL LP": 0}, "signed": {"static": 0, "gl": 0}, "bundle": 0,
                "client": 0, "migrated": 0}

    # ---- the one store of existing records: worker.registry, shared with the agent path ------------------------
    def registry(self, sponsor: str | None = None) -> Registry:
        return persist.registry(self.root, T.sponsor_slug(sponsor or self.S["sp"]))

    def _seed_store(self) -> None:
        for t, items in D.REG0["Sponsor B"].items():
            self.registry("Sponsor B").add(
                t, [{"key": i, "source_key": k if k != i else "", "name": n, "label": D.SEED_LABEL.format(t=t.lower())}
                    for i, k, n in items], source="app batch", batch="seed", fund="B Fund I")
        self._take_over_counters(D.COUNTERS_SEED)

    def _take_over_counters(self, counters: dict[str, Any]) -> None:
        """Lift the counters to a `{"Sponsor|Type": next number}` map (demo seed, or an old file's `ctr`); never lowers."""
        for key, number in counters.items():
            sponsor, _, record_type = key.partition("|")
            self.id_store(sponsor, record_type).lift_to(int(number))

    def _migrate_store(self) -> None:
        """A console state file from before the shared store carries its records in `reg`: move them over, once."""
        for sponsor, types in (self.S.pop("reg", None) or {}).items():
            for t, items in types.items():
                for x in items:
                    src = x.get("src", "")
                    source = "outside export" if "export" in src else "quick entry" if "quick entry" in src                         else "app batch"
                    self.registry(sponsor).add(t, [{"key": x["id"], "name": x["name"], "label": src, "job": x.get("job"),
                                                    "source_key": x["key"] if x["key"] not in (x["id"], x["name"]) else ""}],
                                               source=source, batch=x.get("job") or "migrated")

    def store_add(self, sponsor: str, record_type: str, records: list[dict[str, Any]], source: str, batch: str,
                  fund: str = "") -> int:
        return self.registry(sponsor).add(record_type, records, source=source, batch=batch, fund=fund)

    def save(self) -> None:
        if self._document is not None:
            self._document.save(json.dumps(self.S, ensure_ascii=False))

    @staticmethod
    def _named(who: str = "") -> str:
        """The person a console action is recorded under: the reviewer, by the `contracts.approver_name` rules. A
        blank, a model, a role or a reserved name is refused; there is no default person."""
        from worker.contracts import UnnamedApprover, approver_name

        try:
            return approver_name(who, demo=False)
        except UnnamedApprover as exc:
            raise ConsoleError(str(exc)) from exc

    def by_now(self) -> str:
        """Who is acting right now: `Name <email>` when signed in, else the name the action was given, else nothing."""
        return str(getattr(self, "actor", "") or getattr(self, "_acting", "") or "")

    def mark(self, j: dict[str, Any], slot: str, key: Any, who: str = "") -> None:
        """Who made this choice (a row decision, a typed value, a date, a column ...), for the engine's own audit, which
        names the person who decided each thing and not one name for the whole job."""
        from worker.contracts import DEMO_REVIEWER, is_agent_principal
        from worker.improver import reserved_name

        label = who or self.by_now()
        name = label.split(" <", 1)[0].strip()
        # only a real person is recorded: with no sign-in the name is "local" (the demo reviewer), which is nobody, and an
        # agent or a reserved name never is; such a choice keeps the engine's old principal
        if name and name.casefold() != DEMO_REVIEWER and not is_agent_principal(name) and not reserved_name(name):
            j.setdefault("by", {})[f"{slot}|{key}"] = label

    def log(self, action: str, reason: str, job_type: str | None = None) -> None:
        job = self.job()
        self.S["audit"].append({"t": time.strftime("%H:%M:%S"), "who": getattr(self, "actor", "") or "analyst", "a": action, "r": reason,
                                "job": job_type if job_type is not None else (job["type"] if job else ""),
                                "sp": self.S["sp"], "d": date.today().isoformat()})

    # ---- lookups ------------------------------------------------------------------------------------------
    def world(self, sponsor: str | None = None) -> _World:
        return _World(self, sponsor or self.S["sp"])

    def reg_of(self, sponsor: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """The sponsor's existing records by type, as the engine reads them: {id, key, name, src, job}. `id` is the
        Ledgerline ID, `key` the ID the source file used. A read-only view of the shared registry."""
        reg = self.registry(sponsor)
        out: dict[str, list[dict[str, Any]]] = {}
        for t in RF.stored_types():
            out[t] = [{"id": e["key"] or e["name"], "key": e.get("source_key") or e["key"] or e["name"],
                       "name": e["name"], "src": e.get("label") or f"{e['source']} · {e['batch']}",
                       "job": e.get("job", "")} for e in reg.entries(t)]
        return out

    def field_recipes(self, sponsor: str | None = None) -> Any:
        """The sponsor's field recipes (`<state>/sponsors/<sponsor>/recipes/<RecordType>.yaml`): what the person
        decided about an unknown column or format, applied by the engine's convert stage on every later job."""
        sp = sponsor or self.S["sp"]
        if sp not in self._recipes:
            types = RF.static_types()
            self._recipes[sp] = persist.field_recipes(
                self.root / "sponsors", T.sponsor_slug(sp), record_types=types,
                target_fields={t: T.spec_of(sp, t).fields for t in types})
        return self._recipes[sp]

    def ents_of(self, sponsor: str | None = None) -> list[str]:
        return self.S["ent"].get(sponsor or self.S["sp"], [])

    def entity_id(self, name: str) -> str:
        return self.S["EM"].get(name, {}).get("id", "")

    def id_store(self, sponsor: str, record_type: str) -> Any:
        """The sponsor's ID scheme, decision and counter: the store the agent's `set_id_scheme`/`assign_ids` use."""
        return persist.id_scheme(self.root / "ids", T.sponsor_slug(sponsor), record_type,
                                 RF.target(record_type).id_generation)

    def id_decision(self, sponsor: str, record_type: str) -> dict[str, Any] | None:
        """Who decided the ID logic, why and when; None while no named person has (only generating types decide)."""
        return self.id_store(sponsor, record_type).decided if record_type in RF.id_decision_types() else None

    def id_scheme_of(self, sponsor: str, record_type: str) -> dict[str, Any]:
        """The ID scheme in force: the target file's format, or the person's decision (`worker.console_idgen`)."""
        base = dict(RF.target(record_type).id_generation)
        return {**base, **self.id_store(sponsor, record_type).scheme} if self.id_decision(sponsor, record_type) else base

    def counter_of(self, sponsor: str, record_type: str) -> int:
        """The next generated ID's number: the one counter (`idgen`) the agent path moves too."""
        return int(self.id_store(sponsor, record_type).counter)

    def counters_of(self, sponsor: str) -> dict[str, int]:
        have = self.id_store(sponsor, RF.static_types()[0]).counters()
        return {t: have[t] for t in RF.static_types() if have.get(t, 1) > 1}

    def ext_of(self, record_type: str, sponsor: str | None = None) -> dict[str, Any] | None:
        return self.S["ext"].get(f"{sponsor or self.S['sp']}|{record_type}")

    def static_ok(self, record_type: str, sponsor: str | None = None, fund: str | None = None) -> bool:
        if record_type in RF.unique_types():
            return any(self.S["F"][n]["static"].get(record_type) for n in self.ents_of(sponsor))
        return bool(self.S["F"].get(fund or self.S["fund"], {"static": {}})["static"].get(record_type))

    def static_done(self, fund: str, sponsor: str | None = None) -> int:
        return sum(1 for t in RF.static_types() if self.static_ok(t, sponsor, fund))

    def known_account(self, sponsor: str, source_account: str, transaction_type: str) -> str:
        from worker.accounts import key_of

        key = key_of("", source_account, transaction_type)
        mine = self.S.get("accounts", {}).get(sponsor, {})
        if key in mine:
            return str(mine[key])
        gold = self.S.get("golden", {})
        if key in gold:
            return str(gold[key])
        return D.matched_before(source_account)

    def req_of(self, record_type: str, f: list[Any], sponsor: str | None = None) -> bool:
        return self.required_value(f"{sponsor or self.S['sp']}|{record_type}|{f[0]}", bool(f[2]))

    def required_value(self, key: str, default: bool) -> bool:
        """Is the field required for this sponsor and record type. A person's Required? choice may only raise the
        organisation's default: a default-required field that was unticked counts as unticked only when a named
        controller approved it (`req_ctl`), so an override without one never loosens the check."""
        if key not in self.S["req"]:
            return default
        on = bool(self.S["req"][key])
        if default and not on and key not in self.S.get("req_ctl", {}):
            return True
        return on

    def job(self) -> dict[str, Any] | None:
        jid = self.S.get("job")
        return next((j for j in self.S["jobs"] if j.get("id") == jid), None) if jid else None

    # ---- the existing-records question ---------------------------------------------------------------------
    def store_text(self, record_type: str, sponsor: str | None = None) -> str:
        if record_type not in RF.stored_types():
            return ""
        reg = self.reg_of(sponsor)[record_type]
        by: dict[str, int] = {}
        for x in reg:
            by[x["src"]] = by.get(x["src"], 0) + 1
        parts = [f"{n} from {s}" for s, n in by.items()]
        e = self.ext_of(record_type, sponsor)
        if not e:
            parts.append("not yet asked about records loaded outside the app")
        elif e["ans"] == "none":
            parts.append("none loaded outside the app, you said")
        elif e["ans"] == "unknown":
            parts.append("not checked against records loaded outside the app, you went ahead without the export")
        return f"{len(reg)} {record_type.lower()} already in Ledgerline: {' · '.join(parts)}"

    def ext_load(self, record_type: str, path: str | Path | None = None) -> dict[str, Any]:
        """Read an Ledgerline export (ID and name columns) into the store. Records are used as they are."""
        name0, _rows = D.EXPORTS[record_type]
        if path is None:
            path = D.write_export(self.inbox / name0, record_type)
        file = Path(path).name
        _sheet, header, raw = EC.read_sheet(path)
        id_h = next((h for h in header if h.lower() in ("ledgerline id", "id", "record id", "vendor id")), header[0])
        nm_h = next((h for h in header if "name" in h.lower()), header[1] if len(header) > 1 else header[0])
        src = f"the Ledgerline export {file} ({date.today().isoformat()})"
        found = []
        for _n, cells in raw:
            vals = dict(zip(header, cells, strict=False))
            ident, nm = EC.cell_text(vals.get(id_h)), EC.cell_text(vals.get(nm_h))
            if ident:
                found.append({"key": ident, "name": nm, "label": src})
        added = self.store_add(self.S["sp"], record_type, found, "outside export", file)
        self.S["ext"][f"{self.S['sp']}|{record_type}"] = {"ans": "file", "file": file, "n": len(raw)}
        self.log(f"Loaded {len(raw)} existing {record_type.lower()} from {file}",
                 "uploaded: records in Ledgerline loaded outside the app, IDs used as is")
        return {"read": len(raw), "added": added, "file": file}

    def ex_pending(self, j: dict[str, Any]) -> bool:
        return bool(j.get("exAsk")) and not (j.get("exA") in ("no", "skip") or j.get("exF"))

    def ex_answer(self, j: dict[str, Any], v: str, typed: str = "") -> None:
        lt = j["type"].lower()
        if v == "file":
            self.ext_load(j["type"])
            j["exF"] = True
            return
        j["exA"] = v
        j["exTxt"] = typed or {"yes": f"Yes, some {lt} were loaded outside the app.", "no": "No, none.",
                               "unsure": "I'm not sure.", "skip": "Go ahead without it."}[v]
        if v == "no":
            self.S["ext"][f"{self.S['sp']}|{j['type']}"] = {"ans": "none"}
            self.log(f"No {lt} in Ledgerline outside the app for {self.S['sp']}",
                     "answered; asked once per sponsor and record type")
        if v == "skip":
            self.S["ext"][f"{self.S['sp']}|{j['type']}"] = {"ans": "unknown"}
            self.log(f"Went ahead without checking {lt} loaded outside the app",
                     "export not available, asked again next job")
        if v in ("yes", "unsure"):
            self.log(f"Said {'some' if v == 'yes' else 'maybe some'} {lt} were loaded outside the app",
                     "said in the job conversation")

    # ---- starting a job in the chat -----------------------------------------------------------------------
    def ask_start(self, sponsor: str | None = None, type_: str | None = None) -> dict[str, Any]:
        sp = sponsor or self.S["sp"]
        if sp not in self.S["ent"]:
            raise ConsoleError(f"unknown sponsor {sp!r}")
        self.S["sp"] = sp
        self.S["ask"] = _blank_ask(sp, self.ents_of(sp), type_)
        self.S["job"] = None
        return self.S["ask"]

    def route(self, a: dict[str, Any]) -> tuple[str, str | None]:
        t, sp = a["type"], a["sp"]
        wf = self.S["wf"].get(f"{sp}|{t}")
        eid = self.entity_id(a["fund"])
        table = {
            "Investors": (("Path 3 · a later fund, earlier funds done in the app" if wf else
                           "Path 1 · one common investor file for every entity" if a["cov"] == "all"
                           else "Path 2 · first fund, LP file"), None),
            "Commitments": ("Allocation file · an Fundbook commitment file is optional", None),
            "Investments": ("Roll forward + SOI + GL", "the SOI (PDF) and the fund GL"),
            "Vendors": ("Vendor list, or vendor names taken from the GL", None),
            "Affiliates": ("ID and name list", None),
            "GL Fund": (f"Entity level · Entity ID {eid} from {a['fund']} setup", None),
            "GL LP": (f"LP level · inherits the locked {a['fund']} entity run", None),
        }
        return table.get(t, ("", None))

    def ask_block(self, a: dict[str, Any]) -> str:
        t = a["type"]
        if not t or not RF.is_gl(t):
            return ""
        done = self.static_done(a["fund"], a["sp"])
        if done < REQUIRED_BY_STATIC:
            return (f"{a['sp']} has {done} of {REQUIRED_BY_STATIC} static types approved. GL lines point at those "
                    "records, so finish them first.")
        if t == "GL LP" and not self.S["F"][a["fund"]]["gl"].get("GL Fund"):
            return f"Approve the GL Fund job for {a['fund']} first. LP lines are checked against it."
        return ""

    def ask_need(self, a: dict[str, Any]) -> list[str]:
        n = []
        if not a["type"]:
            n.append("what to build")
        if not a["file"]:
            n.append("the source file")
        if a["type"] and not RF.is_gl(a["type"]):
            if not a["cov"]:
                n.append("which entities the file covers")
            elif a["cov"] == "some" and not a["ents"]:
                n.append("at least one entity ticked")
        if a["type"] and self.route(a)[1] and not a["file2"]:
            n.append(self.route(a)[1] or "")
        return n

    def ask_ready(self, a: dict[str, Any] | None = None) -> bool:
        a = a or self.S["ask"]
        return not self.ask_need(a) and not self.ask_block(a)

    def _cov_text(self, a: dict[str, Any]) -> str:
        return (f"all {len(self.ents_of(a['sp']))} entities" if a["cov"] == "all" else a["fund"] if a["cov"] == "one"
                else ", ".join(a["ents"]) or "some entities" if a["cov"] == "some" else "")

    def sample_name(self, a: dict[str, Any]) -> str:
        entity = a["fund"] if (a["cov"] == "one" or RF.is_gl(a["type"])) else self.ents_of(a["sp"])[0]
        return D.sample_file_name(a["type"], a["sp"], entity)

    def _entities_of_ask(self, a: dict[str, Any]) -> list[str]:
        cov = "one" if RF.is_gl(a["type"]) else a["cov"]
        e = self.ents_of(a["sp"])
        return e[:] if cov == "all" else [x for x in e if x in a["ents"]] if cov == "some" else [a["fund"]]

    def ask_sample(self, a: dict[str, Any]) -> None:
        t = a["type"] or "Investors"
        a["type"] = t
        a["file"] = self.sample_name(a)
        ents = self._entities_of_ask(a) or self.ents_of(a["sp"])
        path = self.inbox / a["file"]
        if RF.is_gl(t):
            D.write_gl(path, t, a["sp"], a["fund"])
        else:
            D.write_static(path, t, a["sp"], ents)
        a["path"], a["size"] = str(path), path.stat().st_size
        self._count_upload(a, path.name, path.read_bytes(), "sample")
        if not a["text"]:
            a["text"] = f"Build the {t.lower()} upload"
        if self.route(a)[1]:
            f = a["fund"].replace(" ", "")
            a["file2"] = f"{f}_SOI_2025.pdf + {f}_GL_2025Q4.xlsx"

    def ask_attach(self, a: dict[str, Any], path: str | Path) -> None:
        p = Path(path)
        a["file"], a["path"], a["size"] = p.name, str(p), p.stat().st_size
        if not a["type"]:
            a["type"] = infer_type(p.name)

    def _count_upload(self, a: dict[str, Any], file: str, data: bytes, kind: str, rows: int | None = None) -> None:
        """Upload number n of this job's source file, on the record with the file's hash. The numbered log goes with the
        job once it starts; a replacement later carries on from it."""
        import hashlib

        log = a.setdefault("upload_log", [])
        entry = {"no": len(log) + 1, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "file": file, "kind": kind,
                 "sha256": "sha256:" + hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        self.audit_user("source.attached", f"source file upload {entry['no']}: {file} ({kind})",
                        {**entry, "sponsor": a["sp"], "record_type": a["type"], **({"rows": rows} if rows is not None else {})}, None)
        log.append(entry)

    def check_upload(self, name: str, data: bytes) -> None:
        """The limits every uploaded source file meets: a plain name, not an answer key, not empty, not too big, an Excel
        workbook (the console reads nothing else)."""
        from worker.service import FILE_RE, MAX_UPLOAD_BYTES, UPLOAD_EXTENSIONS

        ext = Path(name).suffix.lower()
        if (not FILE_RE.match(name) or name.startswith(".") or Path(name).name != name
                or ext not in UPLOAD_EXTENSIONS):
            raise ConsoleError("file name not allowed; use a plain name ending in "
                               + ", ".join(sorted(UPLOAD_EXTENSIONS)))
        if "answer_key" in name.lower() or name.lower().endswith(".answer.json"):
            raise ConsoleError("That file is an answer key, and I never read answer keys.")
        if not data:
            raise ConsoleError("That file is empty.")
        if len(data) > MAX_UPLOAD_BYTES:
            raise ConsoleError(f"That file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
        if ext not in (".xlsx", ".xlsm"):
            raise ConsoleError(f"I read Excel workbooks (.xlsx or .xlsm); {ext} is not one.")

    def store_upload(self, name: str, data: bytes, *, static: bool) -> tuple[Path, str, list[str], list[Any], bool]:
        """Keep the file in the console's inbox (never replacing a different file of the same name) and read it once:
        (path, sheet, header, records, new). `new` is False when the same bytes were already there."""
        ext = Path(name).suffix.lower()
        path = self.inbox / name
        stem, n = path.stem, 1
        while path.exists() and path.read_bytes() != data:  # never replace a different file a job may have read
            n += 1
            path = self.inbox / f"{stem}_v{n}{ext}"
        fresh = not path.exists()
        path.write_bytes(data)
        try:
            if static:  # the records the job will read, kept for it: read once
                sheet, header, rows = EC.read_static_records(path)
            else:
                sheet, header, rows = EC.read_sheet(path)
        except Exception as exc:  # any reader failure means the same thing to the person
            if fresh:
                path.unlink(missing_ok=True)
            raise ConsoleError(f"{name} cannot be read as an Excel workbook: {exc}") from exc
        if not any(header):
            if fresh:
                path.unlink(missing_ok=True)
            raise ConsoleError(f"{name} has no header row on its first sheet, so I cannot tell its columns apart.")
        return path, sheet, header, rows, fresh

    def ask_attach_bytes(self, name: str, data: bytes, *, sponsor: str | None = None,
                         type_: str | None = None) -> dict[str, Any]:
        """Start a job from the person's own file. Same name, size and type limits as the inbox; the console reads
        Excel workbooks, so another inbox type is refused here with that reason. The file is kept in the console's
        inbox (never replacing a different file of the same name) and its own headers are what the plan offers."""
        self.check_upload(name, data)
        a = self.S.get("ask")
        if a is None:
            a = self.ask_start(sponsor, type_)
        elif type_ and not a["type"]:
            a["type"], a["typeSet"] = type_, True
        path, sheet, header, rows, fresh = self.store_upload(name, data, static=bool(a["type"] and not RF.is_gl(a["type"])))
        try:
            self._count_upload(a, path.name, data, "attached", rows=len(rows))
        except ConsoleError:
            if fresh:
                path.unlink(missing_ok=True)
            raise
        self.ask_attach(a, path)
        a["own"] = True
        if a["type"] and not a["text"]:
            a["text"] = f"Build the {a['type'].lower()} upload"
        self.log(f"Attached {name}", "own source file; its columns are what the plan offers",
                 a["type"] or "")
        return {"file": path.name, "bytes": len(data), "sheet": sheet, "rows": len(rows),
                "columns": [h for h in header if h], "type": a["type"] or ""}

    def ask_talk(self, text: str) -> None:
        a = self.S["ask"]
        if a is None:
            raise ConsoleError("No job is being started.")
        e = self.ents_of(a["sp"])
        low = text.lower()
        a["msgs"].append({"who": "you", "text": text})
        self.log(f"Said: “{text}”", "started in the chat", "")
        if re.match(r"^\s*(go|start|yes|ok)\b", text, re.IGNORECASE) and self.ask_ready(a):
            self.ask_go()
            return
        if not a["text"]:
            a["text"] = text
        t0 = infer_type(text)
        if t0:
            a["type"], a["typeSet"] = t0, True
        hit = [x for x in e if x.lower() in low]
        if re.search(r"\ball\b", low):
            a["cov"] = "all"
        elif len(hit) == 1:
            a["cov"], a["fund"] = "one", hit[0]
        elif len(hit) > 1:
            a["cov"], a["ents"] = "some", hit
        if "sample" in low and not a["file"]:
            self.ask_sample(a)
        got = [x for x in (t0, self._cov_text(a) if re.search(r"\ball\b", low) or hit else "",
                           a["file"] if "sample" in low else "") if x]
        if got:
            a["msgs"].append({"who": "w", "text": f"Got it: {' · '.join(got)}."})

    def ask_set(self, key: str, value: Any) -> None:
        a = self.S["ask"]
        if key == "type":
            a["type"], a["typeSet"] = value, True
        elif key == "cov":
            a["cov"] = value
        elif key == "fund":
            a["fund"] = value
        elif key == "ents":
            a["ents"] = list(value)
        elif key == "file2":
            a["file2"] = value
        else:
            raise ConsoleError(f"cannot set {key!r}")

    def ask_next(self) -> dict[str, Any]:
        """The worker's next line in the start conversation, with the buttons that answer it."""
        a = self.S["ask"]
        e = self.ents_of(a["sp"])
        b = self.ask_block(a)
        if not a["type"] and not a["file"] and not a["msgs"]:
            return {"text": "What do you need? Say it in a sentence, for example “build the investor upload”, "
                            "and drop the source file here.", "options": [{"label": "Use a sample", "action": "ask_sample"}]}
        if not a["type"]:
            return {"text": "What should I build?",
                    "options": [{"label": t, "action": f"ask_set|type|{t}"} for t in RF.types()]}
        if b:
            return {"text": b, "options": [], "blocked": True}
        if not a["file"]:
            return {"text": f"Drop the {a['type'].lower()} source file here.",
                    "options": [{"label": "Use a sample", "action": "ask_sample"}]}
        if not RF.is_gl(a["type"]) and not a["cov"]:
            opts = [{"label": f"All {len(e)}", "action": "ask_set|cov|all"}]
            opts += [{"label": x, "action": f"ask_cov_one|{x}"} for x in e]
            opts.append({"label": "Some of them", "action": "ask_set|cov|some"})
            return {"text": f"Which entities does {a['file']} cover?", "options": opts}
        if a["cov"] == "some" and not a["ents"]:
            return {"text": "Which ones?", "options": [{"label": x, "action": f"ask_ent|{x}"} for x in e]}
        if self.route(a)[1] and not a["file2"]:
            return {"text": f"For {a['type'].lower()} I also need {self.route(a)[1]}.",
                    "options": [{"label": "Use a sample", "action": "ask_sample2"}]}
        where = a["fund"] if RF.is_gl(a["type"]) else self._cov_text(a)
        return {"text": f"I'll build {a['type']} for {a['sp']} ({where}) from {a['file']} into "
                        f"{RF.template_stem(a['type'])}.xlsx.",
                "options": [{"label": "Start", "action": "ask_go", "primary": True}], "ready": True}

    def ask_go(self) -> dict[str, Any]:
        a = self.S["ask"]
        if a is None or not self.ask_ready(a):
            raise ConsoleError("Start is waiting for: " + ", ".join(self.ask_need(a) or [self.ask_block(a)]))
        try:  # a refused rule file is audited first and the job does not start; the ask stays as it was
            require_valid_skills(persist.audit(self.out_dir / "audit.jsonl"))
        except SkillLoadRefused as exc:
            raise ConsoleError(f"The job did not start: {exc}") from exc
        t = a["type"]
        cov = "one" if RF.is_gl(t) else a["cov"]
        ents = self._entities_of_ask(a)
        fund = ents[0]
        scope = {"all": "common", "some": "batch", "one": "fund"}[cov]
        self.S["sp"], self.S["fund"], self.S["ask"] = a["sp"], fund, None
        j = self.new_job(t, fund, scope)
        j["ents"] = ents
        j["upload_log"] = list(a.get("upload_log") or [])
        j["ask"] = a["text"].strip() or f"Build the {t} upload"
        j["msgs0"] = a["msgs"]
        j["up"].update({"stage": "ready", "file": a["file"], "path": a["path"], "size": a["size"],
                        "eid": self.entity_id(fund), "file2": a["file2"]})
        if a.get("own"):  # the person's own file: its real headers, sheet and rows are what the screens show
            j["up"]["own"] = True
        if not RF.is_gl(t):
            CE.refresh_recipe(self, j)  # a saved recipe for this sponsor and type shows in the plan at once
        j["route"] = self.route(a)[0]
        e = self.ext_of(t)
        j["exAsk"] = t in RF.stored_types() and (not e or e["ans"] == "unknown")
        self.S["jobs"].append(j)
        self.S["job"] = j["id"]
        self.log(f"Asked: “{j['ask']}”", f"instruction · file {a['file']}")
        if j["rep"] and not self.ex_pending(j):
            self.run_job(j, "saved workflow re-applied by code, no model call")
        return j

    def new_job(self, type_: str, fund: str, scope: str) -> dict[str, Any]:
        sp = self.S["sp"]
        rep = bool(self.S["wf"].get(f"{sp}|{type_}"))
        rules = EC.default_rules()
        rules["id"] = "drop" if rep and type_ == "Investors" else "error"
        rules["suffix"] = bool(self.S["lsuffix"].get(sp))
        self.S["seq"] += 1
        return {"status": "pending", "rows": 0, "t": time.strftime("%H:%M:%S"), "d": date.today().isoformat(), "id": f"j{self.S['seq']}", "sp": sp, "type": type_, "fund": fund, "scope": scope, "ents": [fund],
                "up": {"stage": "empty", "file": "", "path": "", "size": 0}, "rules": rules, "map": {},
                "dec": {}, "qa": {}, "fill": {}, "dates": {}, "acct": {}, "dealfill": {}, "runs": [], "failed": None,
                "failedOnce": False, "approved": False, "rep": rep, "logR": [], "chat": [], "learn": [], "ask": "",
                "okF": {f[0]: 1 for f in (D.plan(type_) if rep else [])},
                "trace": None, "note": "", "written": 0, "retries": 0, "exAsk": False, "exA": "", "exF": False,
                "draft": None, "fb": None}

    # ---- running ------------------------------------------------------------------------------------------
    def run_job(self, j: dict[str, Any], why: str = "") -> None:
        """Convert and check. With the free model profile the first GL run stops at Check (a rate limit);
        resuming starts there, with every earlier stage and decision kept."""
        if RF.is_gl(j["type"]) and self.S["prov"] == FREE and not j["failedOnce"]:
            j["failedOnce"] = True
            j["failed"] = {"stage": "Check", "why": "the free Searcher model hit its daily limit (429) and the "
                                                    "fallback was busy"}
            j["retries"] += 1
            self.log("Stopped at Check: " + j["failed"]["why"], "checkpoint saved after Convert; nothing lost")
            return
        j["failed"] = None
        t = CE.tally(self, j)
        j["runs"].append({"n": len(j["runs"]) + 1, "t": t, "rules": self.rule_line(j),
                          "time": time.strftime("%H:%M:%S")})
        self.log(f"Converted (run {len(j['runs'])})", why or self.rule_line(j))
        from worker import console_issues

        console_issues.refresh(self, j)  # the group counts are made once, at check time, and kept with the job
        self.S["toast"] = {"job": j["id"], "text": (
            f"{j['sp']} · {j['type']}: stopped at {j['failed']['stage']}, saved" if j["failed"]
            else f"{j['sp']} · {j['type']}: Check done · {t['err']} need you" if t["err"]
            else f"{j['sp']} · {j['type']}: no rows were read from the file, so there is nothing to approve" if not t["read"]
            else f"{j['sp']} · {j['type']}: ready for your approval")}

    def resume(self, j: dict[str, Any]) -> None:
        if not j["failed"]:
            raise ConsoleError("Nothing is stopped.")
        stage = j["failed"]["stage"]
        self.run_job(j, f"resumed from checkpoint {stage}; earlier stages not repeated")
        j["chat"].append({"who": "w", "role": "Checker", "text": "Resumed from the saved checkpoint. Read, Map and "
                          "Convert were not repeated, and your decisions are intact."})

    def retally(self, j: dict[str, Any]) -> None:
        if j["runs"]:
            j["runs"][-1]["t"] = CE.tally(self, j)

    @staticmethod
    def rule_line(j: dict[str, Any]) -> str:
        r = j["rules"]
        parts = ["ID: error over limit" if r["id"] == "error" else "ID: cut" if r["id"] == "truncate"
                 else "ID: drop prefix", f"name ≤ {r['nameMax']}", "Ltd = Limited" if r["suffix"] else "",
                 f"tol {r['tolPass']}/{r['tolWarn']}" if RF.is_gl(j["type"]) else ""]
        return " · ".join(p for p in parts if p)

    # ---- the job conversation -------------------------------------------------------------------------------
    def talk(self, j: dict[str, Any], text: str) -> None:
        v = text.strip()
        if self.ex_pending(j) and not j["exA"] or (j["exA"] and self.ex_pending(j)
                                                    and re.search(r"go ahead|skip|without", v, re.IGNORECASE)):
            if re.match(r"^\s*(no|nope|none|n)\b", v, re.IGNORECASE):
                m = "no"
            elif re.search(r"not sure|don.?t know|maybe|unsure", v, re.IGNORECASE):
                m = "unsure"
            elif re.match(r"^\s*(yes|yep|y|some)\b", v, re.IGNORECASE):
                m = "yes"
            elif j["exA"] and re.search(r"go ahead|skip|without", v, re.IGNORECASE):
                m = "skip"
            else:
                m = ""
            if m:
                self.ex_answer(j, m, v)
                return
        fm = re.match(r"^\s*(?:focus on|find|search(?: for)?|show(?: me)?(?: only)?|only)\s+(.+?)\s*$", v, re.IGNORECASE)
        if fm and not j["fb"] and not re.search(r"working|how far", v, re.IGNORECASE):
            from worker import console_view as V

            j["chat"].append({"who": "you", "text": v})
            pf = V.parse_focus(j, fm.group(1))
            st, ent, q = pf.get("st", ""), pf.get("ent", ""), pf.get("q", "")
            n = V.count_found(self, j, q, st, ent)
            label = " · ".join(x for x in (
                {"err": "needs you", "warn": "to check", "skip": "left out", "ok": "written"}.get(st, ""),
                ent, f"“{q}”" if q else "") if x)
            self.log(f"Searched for {label}", "said in the job conversation")
            j["chat"].append({"who": "w", "role": "Searcher", "text": (
                f"{n} row{'' if n == 1 else 's'} for {label}. The Issues table has its own search.")})
            return
        j["chat"].append({"who": "you", "text": v})
        self.log(f"Message: “{v}”", "said in the job conversation")
        from worker import console_chat as CC

        CC.reply(self, j, v)

    def save_rule(self, j: dict[str, Any], scope: str) -> None:
        d = j["draft"]
        if not d or not d["ok"]:
            raise ConsoleError("There is no rule to save.")
        key, val = d["rule"]
        if key == "tol":
            j["rules"]["tolPass"] = val
            j["rules"]["tolWarn"] = max(val, j["rules"]["tolWarn"])
        else:
            j["rules"][key] = val
        if key == "suffix" and scope == "sponsor":
            self.S["lsuffix"][j["sp"]] = True
        j["logR"].append({"scope": scope, "text": d["after"]})
        from worker import console_chat as CC

        CC.saved(self, j, d["read"])
        self.log(f"Rule changed ({d['name']}, {j['sp'] if scope == 'sponsor' else 'this job'}): “{d['text']}”",
                 "instruction in plain English")
        name, j["draft"] = d["name"], None
        if j["runs"]:
            self.run_job(j, "re-converted after a rule change")
            t = CE.tally(self, j)
            j["chat"].append({"who": "w", "role": "Checker", "text": (
                f"Done. {name} changed{' for every ' + j['sp'] + ' job' if scope == 'sponsor' else ''}. "
                f"I converted again: {t['out']} ready, {t['err']} waiting on you.")})
        else:
            j["chat"].append({"who": "w", "role": "Mapper", "text": f"Done. {name} is in the plan. Press Looks right "
                              "when you're happy."})

    # ---- decisions ------------------------------------------------------------------------------------------
    def decide(self, j: dict[str, Any], key: str, decision: str, why: str = "decided in the console") -> None:
        if j.get("approved"):
            raise ConsoleError("This job is approved, so it is final. Nothing changed.")
        j["dec"][str(key)] = decision
        self.mark(j, "dec", key)
        self.log(f"{decision}: {'journal ' if RF.is_gl(j['type']) else 'row '}{key}", why)
        self.retally(j)

    def quick_add(self, j: dict[str, Any], row: str, id_: str, name: str) -> None:
        j["qa"][str(row)] = 1
        self.mark(j, "qa", row)
        self.store_add(j["sp"], "Investors", [{"key": id_, "name": name, "label": "a quick entry by you"}],
                       "quick entry", j["id"])
        self.log(f"Added existing investor {id_} {name}", "loaded outside the app, Ledgerline ID used as is")
        self.retally(j)

    def quick_add_ref(self, j: dict[str, Any], kind: str, value: str) -> None:
        j["qa"][f"{kind}{value}"] = 1
        self.mark(j, "qa", f"{kind}{value}")
        self.store_add(j["sp"], kind, [{"key": value, "name": value, "label": "a quick entry by you"}],
                       "quick entry", j["id"])
        self.log(f"Added existing {kind.lower()[:-1]} {value}", "loaded outside the app")
        self.retally(j)

    def set_date(self, j: dict[str, Any], row: str, value: str = "2025-06-30") -> None:
        j["dates"][str(row)] = value
        self.mark(j, "dates", row)
        self.log(f"Assigned commitment date {value} to row {row}", "set by hand")
        self.retally(j)

    def confirm_fuzzy(self, j: dict[str, Any], row: str) -> None:
        j["qa"][f"f{row}"] = 1
        self.mark(j, "qa", f"f{row}")
        self.log(f"Confirmed fuzzy match on row {row}", "confirmed by hand")
        self.retally(j)

    def fill(self, j: dict[str, Any], row: str, field: str, value: str) -> None:
        if not value.strip():
            raise ConsoleError("A value you supply can't be blank.")
        j["fill"][f"{row}|{field}"] = value.strip()
        self.mark(j, "fill", f"{row}|{field}")
        self.log(f"Set {field} for row {row} = {value.strip()}",
                 "not in any source file, supplied by hand, never inferred")
        self.retally(j)

    def set_account(self, j: dict[str, Any], acct: str, golden: str) -> None:
        j["acct"][acct] = golden
        self.mark(j, "acct", acct)
        self.log(f"Mapped account {acct} to {golden}", "accepted my suggestion")
        self.retally(j)

    def set_required(self, j: dict[str, Any] | None, record_type: str, field: str, on: bool,
                     sponsor: str | None = None, controller: str = "") -> None:
        """Required? is a per-sponsor, per-record-type choice; it applies to every job and is audited. It may only
        raise strictness: ticking a field that is not required by default is the analyst's. Unticking a field that
        the organisation requires is lowering a control, so it needs a named controller (WORKER_ADMINS)."""
        sp = sponsor or (j["sp"] if j else self.S["sp"])
        key = f"{sp}|{record_type}|{field}"
        default = next((bool(f[2]) for f in D.plan(record_type) if f[0] == field), False)
        by = ""
        if default and not on:
            from worker.improver import controller_refusal

            why = controller_refusal(controller, "", self.controllers)
            if why:
                raise ConsoleError(f"{field} is required by the organisation. Only a controller can untick it: {why}.")
            by = controller.strip()
            self.S.setdefault("req_ctl", {})[key] = by
        else:
            self.S.get("req_ctl", {}).pop(key, None)
        self.S["req"][key] = bool(on)
        self.log(f"{field} is {'now' if on else 'no longer'} required for {sp} {record_type.lower()}",
                 f"applies to every {sp} job" if not by else
                 f"controller {by} lowered the organisation's requirement for every {sp} job", record_type)
        if j:
            self.retally(j)

    def set_column(self, j: dict[str, Any], field: str, header: str, who: str = "") -> None:
        """Pick the source column for a field ("" = not in this file) for this job. It is a job-scope answer: the
        `field_decision` goes to the job's audit and the sponsor's log first; if that cannot be written the column
        does not change. The sponsor's recipe is changed only by answering the question at sponsor scope."""
        from worker import console_questions as Q

        if j.get("approved"):
            raise ConsoleError("This job is approved, so it is final. Nothing changed.")
        Q.record_column(self, j, field, header, self._named(who))
        j["map"][field] = header
        self.mark(j, "map", field, self._named(who) if not getattr(self, "actor", "") else "")
        j.get("steps", {}).pop(field, None)  # a plain column choice replaces a step decided earlier for this job
        self.log(f"{field} now comes from {'column ' + header if header else 'no column (left blank, reported)'}",
                 "mapping changed by hand; offered as a sponsor rule", j["type"])
        self.S["fb"].append({"sp": j["sp"], "type": j["type"], "kind": "override", "ref": field,
                             "text": header or "not in this file", "skill": "detect-columns@4"})
        self.retally(j)

    # ---- accepting the worker's suggestions -----------------------------------------------------------------
    def suggestions(self, j: dict[str, Any]) -> list[dict[str, Any]]:
        from worker import console_view as V

        if RF.is_gl(j["type"]):
            return [x["s"] for x in V.gl_items(self, j)]
        return [s for s in (V.sugg(j, r) for r in CE.static_rows(self, j)) if s]

    def apply_command(self, j: dict[str, Any], cmd: str) -> None:
        """One suggestion, applied: the same state changes as the manual buttons, audited as accepted."""
        a = cmd.split("|")
        k, why = a[0], "accepted my suggestion"
        if k == "dec":
            self.decide(j, a[1], a[2], why)
            return
        if k == "qa":
            self.quick_add(j, a[1], a[2], a[3])
        elif k == "quick":
            self.quick_add_ref(j, a[1], a[2])
        elif k == "date":
            self.set_date(j, a[1])
        elif k == "fz":
            self.confirm_fuzzy(j, a[1])
        elif k == "acctset":
            self.set_account(j, a[1], a[2])
        elif k == "rule":
            j["rules"]["id"] = a[1]
            j["logR"].append({"scope": "job", "text": "Drop long ID prefixes"})
            self.log("Rule changed: drop the prefix from long IDs", why)
        else:
            raise ConsoleError(f"unknown suggestion {cmd!r}")
        self.retally(j)

    def accept(self, j: dict[str, Any], cmds: list[str]) -> None:
        sug = {s["c"]: s for s in self.suggestions(j)}
        for c in dict.fromkeys(cmds):
            s = sug.get(c)
            if s and s.get("L") and not str(s.get("d", "")).startswith("Already in Ledgerline? · row"):
                self.learn(j, s["L"], "sponsor")
            self.apply_command(j, c)

    def accept_all(self, j: dict[str, Any]) -> None:
        self.accept(j, [s["c"] for s in self.suggestions(j) if s.get("c")])

    @staticmethod
    def learn(j: dict[str, Any], text: str, scope: str | None) -> int:
        for i, x in enumerate(j["learn"]):
            if x["t"] == text:
                return i
        j["learn"].append({"t": text, "scope": scope, "st": "new"})
        return len(j["learn"]) - 1

    # ---- approval -------------------------------------------------------------------------------------------
    def approve(self, j: dict[str, Any], note: str = "", who: str = "") -> None:
        t = CE.tally(self, j)
        if j["failed"]:
            raise ConsoleError(f"The run stopped at {j['failed']['stage']}. Resume it before approving.")
        if not j["runs"]:
            raise ConsoleError("Convert the file first: there is nothing to approve yet.")
        from worker import console_issues as CI2

        old, changed = CI2.stale(self, j)  # first: what you changed can change what still needs you
        if old:
            raise ConsoleError(f"You changed {' and '.join(changed)} after you decided {len(old)} "
                               f"{'thing' if len(old) == 1 else 'things'}. Keep those decisions or re-open them first.")
        if t["err"]:
            raise ConsoleError(f"{t['err']} still need you. Resolve them first.")
        if self.ex_pending(j):
            raise ConsoleError("Answer my question about records already in Ledgerline first.")
        from worker import console_questions as Q

        open_q = Q.open_blockers(self, j)
        if open_q:  # a column the file writes in a form nobody decided would be written as text
            raise ConsoleError(f"Decide how {', '.join(open_q)} {'is' if len(open_q) == 1 else 'are'} read first: "
                               "the file writes it in a form I will not guess.")
        from worker import console_issues as CI

        held = CI.held_count(self, j)
        if held:
            raise ConsoleError(f"{held} {'row is' if held == 1 else 'rows are'} on hold. Release "
                               f"{'it' if held == 1 else 'them'} and decide first: nothing is written while any is held.")
        name = self._named(who)
        actor = str(getattr(self, "actor", "") or "")
        j["approver"] = actor if actor.startswith(name) and "<" in actor else name  # the engine's Approval, with the email
        self.require_rows(j, t)
        why = I.decision_refusal(j["sp"], j["type"], lambda: self.id_decision(j["sp"], j["type"]))
        if why:  # a refusal is on the record too, before the answer
            CE._engine_log(self).record("id.decision_missing", why, {"sponsor": T.sponsor_slug(j["sp"]),
                                        "record_type": j["type"], "job": j["id"], "by": j["approver"]}, {})
            # the person is on the Plan tab, not calling the agent's tool: say where the decision is made
            why = why.replace("(set_id_scheme, or the plan's ID generation logic)", "(ID generation logic on the Plan tab)")
            raise ConsoleError(why[0].upper() + why[1:] + ".")
        self.write_output(j, t)
        if note.strip():
            j["note"] = note.strip()
            self.S["fb"].append({"sp": j["sp"], "type": j["type"], "kind": "note", "ref": f"the {j['type']} job",
                                 "text": note.strip()})
        if t["warn"]:
            self.log(f"Acknowledged {t['warn']} checks", "approval acknowledges them")
        self.finish(j)
        self.log("Approved and wrote the output workbook", "human approval; the AI prepared it")

    def empty_allowed(self, j: dict[str, Any]) -> bool:
        """True when a named person decided, for the job as it is now, that a file with no rows may be written."""
        d = j.get("empty_ok")
        return bool(d) and d.get("eval_key") == str(CE.evaluation(self, j).manifest.eval_key)

    def require_rows(self, j: dict[str, Any], t: dict[str, int]) -> None:
        """An upload with no rows is written only when a named person said so (`allow_empty`): a job whose rows were
        all left out is far more often a mistake than the intention. The refusal is on the record, with its reason."""
        if t["out"] or self.empty_allowed(j):
            return
        why = (f"every one of the {t['read']} rows is left out, so the file would have no records. Nothing is written. "
               "If an empty upload is what you mean, say so with a reason and name who decides.")
        CE._engine_log(self).record("approve.refused_empty", why, {
            "job": j["id"], "sponsor": T.sponsor_slug(j["sp"]), "record_type": j["type"], "read": t["read"],
            "skipped": t["skip"], "by": j["approver"]}, {})
        raise ConsoleError(why[0].upper() + why[1:])

    def allow_empty(self, j: dict[str, Any], who: str, reason: str) -> None:
        """A named person decides that this job's file may have no records. Bound to what the job evaluates to now:
        any later change to its rows or choices asks again."""
        person = self._named(who)
        if not reason.strip():
            raise ConsoleError("Add a reason an empty upload is right (the reason goes to the record).")
        if CE.tally(self, j)["out"]:
            raise ConsoleError("This job has rows to write; there is nothing to decide.")
        key = str(CE.evaluation(self, j).manifest.eval_key)
        CE._engine_log(self).record("approve.empty_upload_allowed", reason.strip(), {
            "job": j["id"], "sponsor": T.sponsor_slug(j["sp"]), "record_type": j["type"], "by": person,
            "eval_key": key}, {})
        j["empty_ok"] = {"by": person, "why": reason.strip(), "eval_key": key, "at": date.today().isoformat()}
        self.log("Allowed an upload with no records", f"{person}: {reason.strip()}", j["type"])

    def job_dir(self, j: dict[str, Any]) -> Path:
        return self.out_dir / T.sponsor_slug(j["sp"]) / j["id"]

    def write_output(self, j: dict[str, Any], t: dict[str, int]) -> None:
        """The upload file and its audit record, before anything else changes. Fixes the IDs the file carries and
        moves the sponsor's counter past the new ones. If it cannot be written the job stays as it was."""
        first = gen = 0
        if not RF.is_gl(j["type"]):
            first = self.counter_of(j["sp"], j["type"])
            ids, gen = CE.out_ids(self, j)
        try:
            out, manifest = CE.write_upload(self, j)
        except (AuditError, OSError, ValueError) as exc:
            raise ConsoleError(f"I didn't write the file, so the job is not approved: {exc}") from exc
        if gen:  # the one counter moves once the upload and its record exist, and only if nobody moved it first
            store = self.id_store(j["sp"], j["type"])
            try:
                store.commit(I.Reservation(store.scheme, first, (), gen))
            except (OSError, ValueError) as exc:
                raise ConsoleError(f"The job is not approved: {exc}") from exc
        if not RF.is_gl(j["type"]):
            j["ids"] = {str(k): v for k, v in ids.items()}
        j["out"], j["manifest"] = out, manifest

    def download_path(self, job_id: str) -> Path:
        """The written file of an approved job; never a path outside that job's output folder."""
        j = next((x for x in self.S["jobs"] if x.get("id") == job_id), None)
        if j is None or not j.get("approved") or not j.get("out"):
            raise ConsoleError("Only an approved job has a file to download.")
        folder = self.job_dir(j).resolve()
        path = (folder / j["out"]["file"]).resolve()
        if path.parent != folder or not path.is_file():
            raise ConsoleError("I can't find the file I wrote.")
        return path

    def finish(self, j: dict[str, Any]) -> None:
        t = CE.tally(self, j)
        j["written"], j["approved"] = t["out"], True
        self.S["wf"][f"{j['sp']}|{j['type']}"] = True
        if RF.is_gl(j["type"]):
            self.S["F"][j["fund"]]["gl"][j["type"]] = 1
        else:
            for f in j["ents"] or [j["fund"]]:
                self.S["F"][f]["static"][j["type"]] = 1
            ids = {int(k): v for k, v in j["ids"].items()}
            rows = CE.static_rows(self, j)
            if j["type"] in RF.stored_types():
                label = (f"the approved {j['sp'] if j['scope'] == 'common' else self.ent_label(j)} "
                         f"{j['type'].lower()} job")
                self.store_add(j["sp"], j["type"], [
                    {"key": ids[r["i"]], "source_key": r["id"] if r["id"] != ids[r["i"]] else "", "name": r["nm"], "label": label, "job": j["id"],
                     "key_kind": "source id" if r["id"] else "name"}
                    for r in rows if r["st"] in ("ok", "warn")], "app batch", j["id"], j["fund"])
        j["status"], j["rows"] = "approved", t["out"]
        self.S["toast"] = None

    @staticmethod
    def ent_label(j: dict[str, Any]) -> str:
        return ("All entities" if j["scope"] == "common" else ", ".join(j["ents"]) if j["scope"] == "batch"
                else j["fund"])

    def next_job(self, j: dict[str, Any]) -> dict[str, Any]:
        """Offer the next record type for the same fund, as a new conversation."""
        nxt = RF.next_of(j["type"])
        if not nxt:
            raise ConsoleError("All record types are done for this fund.")
        a = self.ask_start(j["sp"], nxt)
        a["fund"] = j["fund"]
        a["cov"] = {"common": "all", "batch": "some", "fund": "one"}[j["scope"]]
        a["ents"] = j["ents"][:]
        a["typeSet"] = True
        return a

    # ---- onboarding sponsors and loading entities --------------------------------------------------------------
    def onboard_read(self, mode: str, sponsor: str, path: str | Path) -> dict[str, Any]:
        """Read a list file into rows the person can review. Rows that clash with what is onboarded are reported,
        not added; a row without a go-live date is kept (it blocks the save until it has one)."""
        if mode not in ("new", "add"):
            raise ConsoleError("mode must be new or add")
        try:
            rows = OB.read_list(path, mode)
        except (ValueError, OSError, KeyError) as exc:
            raise ConsoleError(f"could not read the list: {exc}") from exc
        have = OB.known(self, mode, sponsor)
        kept: list[dict[str, str]] = []
        skipped = []
        for r in rows:
            why = OB.clash(have, r, kept)
            if why:
                skipped.append({"row": r, "why": why})
            else:
                kept.append(r)
        return {"file": Path(path).name, "rows": kept, "skipped": skipped, "read": len(rows)}

    def onboard_sample(self, mode: str, sponsor: str) -> dict[str, Any]:
        return self.onboard_read(mode, sponsor, OB.write_sample(self, mode, sponsor))

    def onboard_save(self, mode: str, sponsor: str, rows: list[dict[str, str]], source: str = "") -> None:
        nw = mode == "new"
        if not rows:
            raise ConsoleError("Add at least one sponsor." if nw else "Load at least one entity. Jobs run against this list.")
        rows = [{k: str(v or "").strip() for k, v in r.items()} for r in rows]
        bad = [r for r in rows if not (r.get("id") and r.get("n") and r.get("live"))]
        if bad:
            raise ConsoleError(f"{'Sponsor' if nw else 'Entity'} ID, name and go-live date are all required. "
                               f"Missing for {', '.join(r.get('n') or r.get('id') or '(no name)' for r in bad)}.")
        have = OB.known(self, mode, sponsor)
        seen: list[dict[str, str]] = []
        for r in rows:
            why = OB.clash(have, r, seen)
            if why:
                raise ConsoleError(why)
            seen.append(r)
        if not nw:
            if sponsor not in self.S["ent"]:
                raise ConsoleError(f"unknown sponsor {sponsor!r}")
            groups = [{"n": n, **{k: self.S["EM"].get(n, {}).get(k, "") for k in ("gid", "gn")}}
                      for n in self.S["ent"][sponsor]]
            why = OB.check_groups(rows, groups)
            if why:
                raise ConsoleError(why)
        fmt = lambda r: (f"{r['n']} ({r['id']}, go-live {r['live']}"
                         f"{', group ' + r['gid'] + ' ' + r['gn'] if r.get('gid') else ''})")
        origin = f"from {source}" if source else "typed in by hand"
        if nw:
            for r in rows:
                self.S["sponsors"].append({"id": r["id"], "n": r["n"], "live": r["live"]})
                self.S["ent"][r["n"]] = []
            self.S.update(sp=rows[0]["n"], view="dash" if len(rows) > 1 else "sponsor", tab="jobs", job=None, ask=None)
            self.log(f"Onboarded {len(rows)} sponsor{'s' if len(rows) > 1 else ''}: {', '.join(fmt(r) for r in rows)}",
                     origin, "")
            return
        for r in rows:
            self.S["EM"][r["n"]] = {"id": r["id"], "live": r["live"],
                                    **({"gid": r["gid"], "gn": r["gn"]} if r.get("gid") else {})}
            self.S["F"].setdefault(r["n"], self._new_progress())
            self.S["ent"][sponsor].append(r["n"])
        self.S.update(sp=sponsor, fund=self.S["ent"][sponsor][0], view="sponsor", tab="jobs", job=None, ask=None)
        self.log(f"Loaded {len(rows)} entit{'ies' if len(rows) > 1 else 'y'} for {sponsor}: "
                 f"{', '.join(fmt(r) for r in rows)}", origin, "")

    def sign_off(self, kind: str, entity: str) -> None:
        """The per-entity chain Static -> GL -> Bundle -> Client services -> Migrated; each stage opens when the
        one before is signed off. Only an external client approves the client stage."""
        f = self.S["F"][entity]
        done = self.static_done(entity)
        order = {"signstatic": lambda: done == 5, "signgl": lambda: f["signed"]["static"] and f["gl"]["GL Fund"]
                 and f["gl"]["GL LP"], "bundle": lambda: f["signed"]["gl"], "client": lambda: f["bundle"],
                 "clientok": lambda: f["bundle"], "clientno": lambda: f["bundle"],
                 "migrate": lambda: f["client"] == 1}
        if kind not in order:
            raise ConsoleError(f"unknown sign-off {kind!r}")
        if not order[kind]():
            raise ConsoleError("The stage before is not signed off yet.")
        if kind == "signstatic":
            f["signed"]["static"] = 1
        elif kind == "signgl":
            f["signed"]["gl"] = 1
        elif kind == "bundle":
            f["bundle"] = 1
        elif kind == "client":
            f["client"], f["bundle"] = 0, 1
        elif kind == "clientok":
            f["client"] = 1
        elif kind == "clientno":
            f["client"], f["bundle"] = -1, 0
        elif kind == "migrate":
            f["migrated"] = 1
        self.log(f"{kind} for {entity}", "sign-off recorded", "")

    # ---- one entry point for the page and the tests ------------------------------------------------------------
    def answer_question(self, job_id: str, body: dict[str, Any], principal: str) -> dict[str, Any]:
        """A person's answer to an open question of a job (unknown column or format). `body` is {question_id,
        option_id | custom, scope, reason}; `principal` is the reviewer's name (self-declared, demo-grade) and must
        be a person. See `worker.console_questions.answer`."""
        from worker import console_questions as Q

        j = next((x for x in self.S["jobs"] if x.get("id") == job_id), None)
        if j is None:
            raise ConsoleError(f"no job {job_id!r}")
        if (self.S.get("sp"), self.S.get("job")) != (j.get("sp"), j.get("id")):
            self.S.update(sp=j["sp"], job=j["id"])
        got = Q.answer(self, j, question_id=str(body.get("question_id") or ""), option_id=body.get("option_id"),
                       custom=body.get("custom"), scope=str(body.get("scope") or ""),
                       reason=str(body.get("reason") or ""), principal=principal)
        self.save()
        return got

    def undo_answer(self, job_id: str, question_id: str, principal: str, reason: str = "") -> dict[str, Any]:
        """A person undoes their answer to a question of a job. See `worker.console_questions.undo`."""
        from worker import console_questions as Q

        j = next((x for x in self.S["jobs"] if x.get("id") == job_id), None)
        if j is None:
            raise ConsoleError(f"no job {job_id!r}")
        got = Q.undo(self, j, question_id=question_id, principal=principal, reason=reason)
        self.save()
        return got

    # ---- every action a person takes is on the record --------------------------------------------------------
    def audit_user(self, kind: str, summary: str, detail: dict[str, Any], j: dict[str, Any] | None = None,
                   actor: str = "") -> None:
        """One entry in the chained audit of the job (or, with no job yet, of the person's actions), naming who did it.
        If it cannot be written the caller does not go on: an action that is not on the record does not happen."""
        from worker.audit import AuditError

        who = str(actor or getattr(self, "actor", "") or detail.get("who") or "unnamed")
        # an approved job's own chain is finished: what a person does after that (or before any job) is in their own log,
        # with the job's id in the entry
        log = persist.job_audit(self, j) if j and not j.get("approved") else persist.audit(Path(self.out_dir) / "user_actions.audit.jsonl")
        try:
            log.record(kind, f"{who}: {summary}", {"actor": who, "job": (j or {}).get("id", ""), **detail}, {})
        except (AuditError, OSError, ValueError) as exc:
            raise ConsoleError(f"I couldn't record that, so nothing changed: {exc}") from exc

    def act(self, action: str, text: str = "", who: str = "", context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Do what a button or a typed sentence asks, after recording it: everything but moving around the screens is
        audited before it takes effect (what, with which values, by whom), and a refusal is audited too. A change to
        a field also records its value before and after. Returns the new state view."""
        name, *a = action.split("|")
        if name in USER_NAV:
            return self._act(action, text, who, context)
        j = self.job()
        before = self._field_value(name, a, j)
        said = f"{len(text)} characters" if name == "say" else text[:300]
        self._acting = who  # the setters below read it for who made each choice
        self.audit_user("user.action", f"{name} {'|'.join(a)[:160]}".strip(),
                        {"who": who, "action": name, "args": [x[:160] for x in a], "text": said, "text_chars": len(text),
                         "group": str((context or {}).get("group") or ""), **({"before": before} if before is not None else {})}, j)
        try:
            out = self._act(action, text, who, context)
        except Exception as exc:
            self.audit_user("user.action_refused", f"{name} was refused: {str(exc)[:200]}",
                            {"who": who, "action": name, "args": [x[:160] for x in a], "reason": str(exc)[:300]}, j)
            raise
        if before is not None:
            self.audit_user("user.field_changed", f"{name} {'|'.join(a)[:160]}: {before!r} -> {self._field_value(name, a, j)!r}",
                            {"who": who, "action": name, "args": [x[:160] for x in a], "before": before,
                             "after": self._field_value(name, a, j)}, j)
        return out

    def _field_value(self, name: str, a: list[str], j: dict[str, Any] | None) -> Any:
        """The value a field action is about to change (or has changed), for the before / after of its record."""
        try:
            if name == "req" and j is not None:
                default = next((bool(f[2]) for f in D.plan(a[0]) if f[0] == a[1]), False)
                return self.required_value(f"{j['sp']}|{a[0]}|{a[1]}", default)
            if name == "col" and j is not None:
                return j["map"].get(a[0], "")
        except (IndexError, KeyError):
            return None
        return None

    def _act(self, action: str, text: str = "", who: str = "", context: dict[str, Any] | None = None) -> dict[str, Any]:
        from worker import console_screens as V

        name, *a = action.split("|")
        j = self.job()
        ask = self.S.get("ask")

        def need_job() -> dict[str, Any]:
            if j is None:
                raise ConsoleError("Open a job first.")
            return j

        if name == "switch":
            if a[0] not in self.S["ent"]:
                raise ConsoleError(f"unknown sponsor {a[0]!r}")
            self.S.update(sp=a[0], ask=None, job=None, view="sponsor", tab="jobs",
                          fund=(self.ents_of(a[0]) or [""])[0])
        elif name == "view":
            self.S["view"] = a[0]
            if a[0] not in ("job", "existing"):
                self.S.update(ask=None, job=None)
        elif name == "fund":
            self.S["fund"] = a[0]
        elif name == "tab":
            self.S["tab"] = a[0]
        elif name == "chat_fb":  # chat_fb|up or chat_fb|down: a person's thumbs on the chat's latest reading
            from worker import console_chat as CC

            CC.rate(self, need_job(), a[0] == "up")
        elif name == "id_decide":  # id_decide|<mode>|<reason>: the person's own decision, with their reason and name
            from worker import console_idgen as G

            j = need_job()
            G.decide(self, {"sponsor": j["sp"], "record_type": j["type"], "reason": "|".join(a[1:]),
                            "scheme": {**dict(RF.target(j["type"]).id_generation), "mode": a[0]}}, who, j["id"])
            from worker import console_chat as CC

            CC.saved(self, j, ("id_decision", a[0]))
        elif name == "so":
            self.sign_off(a[0], a[1])
        elif name == "onb_save":
            try:
                rows = json.loads(text or "[]")
            except ValueError as exc:
                raise ConsoleError("the list could not be read") from exc
            self.onboard_save(a[0], a[1] if len(a) > 1 else "", rows, a[2] if len(a) > 2 else "")
        elif name == "open":
            self.S["job"], self.S["ask"], self.S["view"] = a[0], None, "job"
        elif name == "start":
            self.S["view"] = "job"
            self.ask_start(a[0] if a else None, a[1] if len(a) > 1 else None)
        elif name == "say":
            if not text.strip():
                raise ConsoleError("Say what you need.")
            if j is None and ask is None:  # a sentence on the sponsor page starts the conversation
                ask = self.ask_start()
            if ask is not None and j is None:
                self.ask_talk(text)
            else:
                from worker import console_issue_chat as ICC
                from worker import console_issues as CI
                from worker import console_recon as CR

                gid = str((context or {}).get("group") or "")
                if gid:
                    self.log("Message context", f"group {gid} of the job's issues")
                if not (gid and (CI.chat_reply(self, need_job(), gid, text) or CR.chat_reply(self, need_job(), gid, text)
                                 or ICC.reply(self, need_job(), gid, text))):
                    self.talk(need_job(), text)
        elif name == "ask_set":
            self.ask_set(a[0], a[1])
        elif name == "ask_cov_one":
            ask["cov"], ask["fund"] = "one", a[0]
        elif name == "ask_ent":
            ask["ents"] = [e for e in ask["ents"] if e != a[0]] if a[0] in ask["ents"] else ask["ents"] + [a[0]]
        elif name == "ask_sample":
            self.ask_sample(ask)
        elif name == "ask_sample2":
            f = (ask["fund"] or self.ents_of(ask["sp"])[0]).replace(" ", "")
            ask["file2"] = f"{f}_SOI_2025.pdf + {f}_GL_2025Q4.xlsx"
        elif name == "ask_go":
            self.ask_go()
        elif name == "go":
            self.run_job(need_job(), "plan confirmed")
        elif name == "resume":
            self.resume(need_job())
        elif name == "acceptall":
            self.accept_all(need_job())
        elif name == "issue_apply":  # issue_apply|<group>|<option>|<typed value or reason>|<note>
            from worker import console_issues as CI

            try:
                CI.chat_apply(self, need_job(), a[0], a[1], a[2] if len(a) > 2 else "", who,
                              "|".join(a[3:]))
            except CI.IssueError as exc:
                raise ConsoleError(str(exc)) from exc
        elif name in ("issue_apply_prop", "issue_undo_batch"):  # a sentence read as decisions; the proposal is kept here
            from worker import console_issues as CI

            job = need_job()
            try:
                if name == "issue_apply_prop":
                    prop = job.get("issue_prop")
                    if not prop or prop["id"] != a[0]:
                        raise ConsoleError("That proposal is not current any more: say it again.")
                    CI.chat_apply_many(self, job, prop["picks"], who, prop["note"])
                    job["issue_prop"] = None
                else:
                    CI.chat_undo_batch(self, job, int(a[0]), who, "|".join(a[1:]) or text)
            except CI.IssueError as exc:
                raise ConsoleError(str(exc)) from exc
        elif name in ("issue_keep_stale", "issue_reopen_stale"):  # decisions made before the plan changed
            from worker import console_issues as CI

            try:
                (CI.keep_stale if name == "issue_keep_stale" else CI.reopen_stale)(self, need_job(), who)
            except CI.IssueError as exc:
                raise ConsoleError(str(exc)) from exc
        elif name == "issue_undo":  # issue_undo|<decision>|<note>, a release needs the note
            from worker import console_issues as CI

            try:
                CI.chat_undo(self, need_job(), int(a[0]), who, "|".join(a[1:]) or text)
            except CI.IssueError as exc:
                raise ConsoleError(str(exc)) from exc
        elif name in ("recon_apply", "recon_undo"):  # recon_apply|<group>|<option>|<note>, recon_undo|<decision>|<note>
            from worker import console_recon as CR
            from worker.console_issues import IssueError

            try:
                if name == "recon_apply":
                    CR.chat_apply(self, need_job(), a[0], a[1], who, "|".join(a[2:]))
                else:
                    CR.chat_undo(self, need_job(), int(a[0]), who, "|".join(a[1:]) or text)
            except IssueError as exc:
                raise ConsoleError(str(exc)) from exc
        elif name == "answer":  # answer|<job>|<question>|<option>|<scope>, the reason is the text
            if len(a) != 4:
                raise ConsoleError("answer needs a job, a question, an option and a scope")
            self.answer_question(a[0], {"question_id": a[1], "option_id": a[2], "scope": a[3], "reason": text}, who)
        elif name == "undo_answer":  # undo_answer|<job>|<question>, the reason is the text
            if len(a) != 2:
                raise ConsoleError("undo_answer needs a job and a question")
            self.undo_answer(a[0], a[1], who, text)
        elif name == "req":
            self.set_required(need_job(), a[0], a[1], a[2] == "1", controller=a[3] if len(a) > 3 else "")
        elif name == "col":
            self.set_column(need_job(), a[0], a[1] if len(a) > 1 else "", who)
        elif name == "ex":
            self.ex_answer(need_job(), a[0], text)
        elif name == "save":
            self.save_rule(need_job(), a[0])
        elif name == "disc":
            need_job()["draft"] = None
        elif name == "approve":
            self.approve(need_job(), text or "|".join(a), who)
        elif name == "allow_empty":  # the reason is the text: a file with no records, decided by a named person
            self.allow_empty(need_job(), who, text or "|".join(a))
        elif name == "next":
            self.next_job(need_job())
        elif name == "trace":
            need_job()["trace"] = int(a[0]) if a and a[0] != "" else None
        elif name == "prov":
            self.S["prov"] = a[0]
            self.log("Model provider set to " + ("Claude" if a[0] == "paid" else "free models"),
                     "applies to new jobs")
        elif name == "toast_close":
            self.S["toast"] = None
        else:
            raise ConsoleError(f"unknown action {name!r}")
        self.save()
        return V.state_view(self)
