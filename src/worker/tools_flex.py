"""Tools of the model's own work: ask the job, layout programs, the two read-only role tools, and the notes,
skills and rules it carries in from the human."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool

from worker import workbook as wb
from worker.agentspec import job_name, released_by
from worker.memory import replay
from worker.skills import load_skills
from worker.tools_kit import Kit, dump, record_feedback

MAX_BINDING_ROWS = 1000  # rows of one column `propose_binding` previews on


def flex_tools(k: Kit) -> list[BaseTool]:
    ctx, run, resolve = k.ctx, k.run, k.resolve
    sandbox, sandbox_box, layout_roles = k.sandbox, k.sandbox_box, k.layout_roles

    @tool
    def ask_job(question: str, sql: str, why: str, summary: str = "") -> str:
        """Read-only. Answer a question about THIS job with one DuckDB SELECT over its tables: `src_1`, `src_2`...
        (each staged table) and `t_<RecordType>` (the latest staged table of a type), plus the views of the
        semantic layer (`v_investor_count`, `v_total_commitment`, `v_Investors__Commitments`...). It runs in the
        sandbox and writes nothing. Put the one sentence you intend to tell the human in `summary`: every number
        in it must be in the result rows, or it is shown as not verified. The answer is information: it never
        becomes a value of the job, so a value the human wants goes through `supply_value`."""
        def go() -> Any:
            from worker.flex.ask import ask, stage_tables
            from worker.flex.runner import runner_from_env
            from worker.replay import BlobStore

            blobs = BlobStore(Path(os.environ.get("WORKER_SANDBOX_BLOBS") or Path(ctx.out_dir) / "flex_blobs"))
            sbx = sandbox_box.setdefault("v", sandbox if sandbox is not None else runner_from_env(blobs))
            tables, aliases = stage_tables(ctx.stage)
            return ask(runner=sbx, audit=ctx.audit, blobs=blobs, tables=tables, aliases=aliases, question=question,
                       sql=sql, why=why, summary=summary, subject={"sponsor": ctx.sponsor})

        def render(res: Any) -> str:
            return dump({**res, "rows": res.get("rows", [])[:30]})

        return run("ask_job", why, {"question": question, "sql": sql, "summary": summary}, go, render=render)

    def reshape_flex() -> Any:
        """The sponsor's layout-program service: its blobs and artifacts under the state folder, this run's audit."""
        from worker import persist
        from worker.flex.artifacts import Artifacts
        from worker.flex.runner import runner_from_env
        from worker.flex.service import Flex
        from worker.replay import BlobStore

        if "flex" not in sandbox_box:
            state_root = Path(ctx.accounts_root).parent if ctx.accounts_root else Path(ctx.out_dir)
            blobs = BlobStore(Path(os.environ.get("WORKER_SANDBOX_BLOBS") or state_root / "flex" / "blobs"))
            runner = sandbox if sandbox is not None else runner_from_env(blobs)
            sandbox_box["flex"] = Flex(ctx.sponsor, Artifacts(persist.artifact_store(state_root / "flex" / "artifacts"),
                                                              ctx.audit), blobs, runner, ctx.audit)
        return sandbox_box["flex"]

    @tool
    def propose_layout_program(path: str, sheet: str, template: str, why: str) -> str:
        """For a sheet no `lift` places (a pivot, a transposed register, side by side blocks with subtotals): the
        Layout Drafter writes a program that names cells, the sandbox runs it, and code checks every cell and every
        total. Changes nothing: the result is a proposal with its plain-word steps, its BLOCKs and WARNs and exact
        counts. Show it to the human; only `approve_layout_program` (human approval) makes it a source. A BLOCK has no
        override. Without the sandbox this refuses; there is no other way to run model-written code."""
        def go() -> Any:
            from worker.flex.grid import grid_from_workbook
            from worker.roles import RoleRunner, draft_layout

            flex = reshape_flex()
            grid = grid_from_workbook(resolve(path))
            grid["sheets"] = [s for s in grid["sheets"] if s["name"] == sheet]
            if not grid["sheets"]:
                raise ValueError(f"no sheet named {sheet!r} in {path}")
            roles = layout_roles or RoleRunner(audit=ctx.audit, budget=ctx.budget, env=ctx.env)
            out = draft_layout(roles, flex, unit_id=job_name(ctx), record_type=template, grid=grid, why=why)
            p = out.decided or {}
            rep = flex.blobs.get_json(p["verification_report_sha256"]) if p else {}
            return {"status": out.status, "proposal_id": p.get("proposal_id", ""), "steps": p.get("steps", []),
                    "blocks": [b["code"] for b in rep.get("blocks", [])], "warns": [w["code"] for w in rep.get("warns", [])],
                    "info": rep.get("info", {}), "bound": {key: p[key] for key in (
                        "input_grid_sha256", "output_table_sha256", "verification_report_sha256")} if p else {},
                    "next": "Show the steps, the BLOCKs/WARNs and the counts to the human; do not approve it yourself."
                    if out.status == "ok" else "The program could not be made to pass; ask the human."}

        return run("propose_layout_program", why, {"path": path, "sheet": sheet, "template": template}, go, [path])

    @tool
    def approve_layout_program(proposal_id: str, bound: dict[str, str], acknowledged: list[str], scope: str,
                               why: str) -> str:
        """Needs human approval; you can never approve it yourself. Records the person's approval of a layout
        proposal bound to the grid, table and report hashes they saw (`bound` from the proposal), with every WARN
        acknowledged, scope `job` or `sponsor`. Then the approved table is a file in the inbox
        (`reshaped_<id>.csv`, header row 1) that `lift` can read."""
        def go() -> Any:
            by = released_by(ctx.audit.read())  # the person whose gate decision released this call
            flex = reshape_flex()
            done = flex.approve(proposal_id, by=by, scope=scope, bound=bound, acknowledged=acknowledged, reason=why)
            from worker.flex.service import DerivedReader

            src = DerivedReader(None, flex.blobs).path(done["source"])
            target = Path(ctx.out_dir) / "derived" / f"reshaped_{done['table_sha256'][:12]}.csv"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(src.read_bytes())
            if target.parent not in [Path(r) for r in ctx.inbox_roots]:
                ctx.inbox_roots.append(target.parent)
            return {"file": target.name, "artifact_id": done["artifact_id"], "approved_by": by,
                    "next": "lift this file (header row 1) into the template; its values are the sheet's cells"}

        return run("approve_layout_program", why, {"proposal_id": proposal_id, "scope": scope,
                                                   "acknowledged": acknowledged}, go)

    proposed: dict[str, Any] = {}  # option id -> contracts.Option that code built in this run

    @tool
    def propose_binding(
        path: str, sheet: str, col: str, field: str, why: str, header_row: int = 1, record_type: str = ""
    ) -> str:
        """Read-only. How could target `field` get its value from column `col` (header in `header_row`)? Code
        returns up to 3 options (map, or a step from the closed transform vocabulary), each with how many rows
        conform, plus declaring the field MISSING. A day-first or month-first reading is never recommended.
        Nothing is applied: pass the option ids to ask_question."""
        def go() -> Any:
            from dataclasses import replace

            from worker.contracts import choose_recommended
            from worker.roles import binding_options
            from worker.transforms import rule_for

            if record_type:
                spec = ctx.stage.templates.get(record_type)
                if spec is None:
                    raise ValueError(f"unknown record type {record_type!r}")
                if field not in spec.fields:
                    raise ValueError(f"{field} is not a field of {record_type}")
            letter = col.strip().upper()
            if not re.fullmatch(r"[A-Z]{1,3}", letter) or header_row < 1:
                raise ValueError("col is a column letter and header_row a row number from 1")
            data = wb.read_range(
                resolve(path), sheet, f"{letter}{header_row}:{letter}{header_row + MAX_BINDING_ROWS}",
                max_cells=MAX_BINDING_ROWS + 1,
            )
            cells = {int(c["addr"][len(letter):]): c["value"] for c in data["cells"]}
            header = cells.pop(header_row, None)
            if header is None or not str(header).strip():
                raise ValueError(f"no header text in {letter}{header_row}")
            last = max(cells, default=header_row)
            values = [cells.get(r) for r in range(header_row + 1, last + 1)]
            rule = rule_for(field)
            if record_type:
                rule = replace(rule, required=field in ctx.stage.templates[record_type].required)
            options = binding_options(field, rule, str(header).strip(), letter, values, first_row=header_row + 1,
                                      src=f"{sheet}!")
            for opt in options:
                proposed[opt.option_id] = opt
            return {
                "field": field,
                "column": letter,
                "rows": len(values),
                "recommended": choose_recommended(options) or "",
                "options": [o.to_dict() for o in options],
            }

        inputs = {"path": path, "sheet": sheet, "col": col, "field": field, "header_row": header_row,
                  "record_type": record_type}
        return run("propose_binding", why, inputs, go, [path])

    @tool
    def ask_question(field: str, option_ids: list[str], why: str, unit_id: str = "job") -> str:
        """Read-only. Turn options that propose_binding returned into one Question for the human about `field`.
        Code checks every option and picks the recommended one; you never type an option or a number. Nothing
        is applied: relay the question and wait for the human's answer."""
        def go() -> Any:
            from worker.contracts import Question, Subject

            unknown = [o for o in option_ids if o not in proposed]
            if unknown or not option_ids:
                raise ValueError(f"unknown option id {unknown[0] if unknown else '(none)'}: use propose_binding")
            options = [proposed[o] for o in dict.fromkeys(option_ids)]
            other = [o.option_id for o in options if o.payload.get("field") != field]
            if other:
                raise ValueError(f"option {other[0]} is for another field")
            kind = "unknown_format" if any(o.payload.get("person_choice") for o in options) else "unknown_field"
            body = json.dumps([unit_id, field, sorted(o.option_id for o in options)])
            qid = "q-" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
            q = Question.build(question_id=qid, unit_id=unit_id, kind=kind, subject=[Subject(kind="field", ref=field)],
                               options=options, blocking=("convert",))
            return {"question_id": q.question_id, "recommended": q.recommended or "", "question": q.to_dict()}

        return run("ask_question", why, {"field": field, "option_ids": option_ids, "unit_id": unit_id}, go)

    return [ask_job, propose_layout_program, approve_layout_program, propose_binding, ask_question]


def memory_tools(k: Kit) -> list[BaseTool]:
    ctx, run = k.ctx, k.run
    job_skills = k.state.skills

    @tool
    def note_correction(text: str, why: str, scope: str = "job") -> str:
        """Record that the human corrected you in the chat (what they said, in their words). Call it whenever the
        human tells you something you did was wrong, before you act on it. `scope` is row, job, sponsor or
        organisation: how far they said it should reach; leave it as job when they did not say."""
        def go() -> Any:
            rec = record_feedback(ctx, kind="chat", action="comment", reason=text, scope=scope, shown="agent output")
            return {"recorded": True, "scope": rec["scope"]}

        return run("note_correction", why, {"text": text, "scope": scope}, go, render=lambda r: "Recorded.")

    @tool
    def use_skill(skill: str, why: str, scenario: str = "") -> str:
        """Load a skill's know-how. `skill` is a skill from the catalog in your instructions. Without `scenario`
        you get the body of its SKILL.md (which lists its scenario files); pass the file name only when that
        case occurs (e.g. scenario="bank-accounts.md"). The skill version used is recorded for the job."""
        def go() -> Any:
            found = load_skills()
            if skill not in found:
                raise ValueError(f"no skill {skill!r}; available: {sorted(found)}")
            chosen = found[skill]
            used = job_skills.setdefault(skill, chosen.version)
            ctx.audit.record(
                "skill.loaded", why, {"skill": skill, "version": used, "scenario": scenario or None}, {}
            )
            return {"skill": chosen.stamp, "scenario": scenario or None,
                    "text": chosen.scenario(scenario) if scenario else chosen.body}

        return run("use_skill", why, {"skill": skill, "scenario": scenario}, go, render=lambda r: r["text"])

    @tool
    def propose_rule(text: str, evidence: str, why: str) -> str:
        """Propose a small rule for this sponsor (e.g. 'Inv ID means Investor_ID') with its evidence.

        Pauses for the human to approve, edit or reject; runs only after approval.
        """
        mem = ctx.memory
        if mem is None or mem.sponsor != ctx.sponsor:
            return "ERROR: no sponsor memory is configured for this run."

        def go() -> Any:
            rid = mem.propose(text, evidence)
            # reaching this point means the human gate approved (or edited) the call
            ctx.audit.record("rule.approved", why, {"rule_id": rid, "text": text, "evidence": evidence})
            mem.approve(rid)
            results = replay(ctx)
            changed = [r for r in results if r["changed"]]
            ctx.audit.record(
                "replay", f"regression replay after rule {rid}", {"rule_id": rid}, {"cases": results}
            )
            msg = f"Rule {rid} saved for {ctx.sponsor}. Replay of {len(results)} approved case(s): "
            msg += (
                "no output would change."
                if not changed
                else f"{len(changed)} would change: "
                + "; ".join(f"{r['case_id']} ({r.get('reason', 'changed')})" for r in changed)
            )
            return {"rule_id": rid, "replayed": len(results), "would_change": len(changed), "message": msg}

        return run("propose_rule", why, {"text": text, "evidence": evidence}, go, render=lambda r: r["message"])

    return [note_correction, use_skill, propose_rule]
