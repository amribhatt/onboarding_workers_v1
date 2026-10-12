"""Scenarios for the parity test: every check on the known fixtures, with and without the client's report.

Built only from the public engine API so the same file ran against the engine before the rules moved into the
reconcile-gl skill (the golden file `fixtures/recon_parity.json` was written then)."""

from __future__ import annotations

from typing import Any

from test_console_recon import (
    ALIASES,
    COA,
    FUND,
    L_,
    LP,
    NAMES,
    R,
    S,
    _resolve,
    rep,
    rows_from,
    run,
)

CHECKS = ["tb", "bs", "soi", "is", "cap", "cf", "pcap", "irr", "unf", "wf"]


def _x(inv: str, line: str, a: float) -> dict[str, Any]:
    return {**rep(line, a), "investor": inv}


def _cf(inv: str, d: str, a: float, line: str = "") -> dict[str, Any]:
    return {**rep(line, a), "investor": inv, "date": d}


def _view(res: dict[str, Any]) -> dict[str, Any]:
    return {"state": res["state"], "notes": res["notes"], "rows": res["rows"]}


def scenarios() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for cid in CHECKS:
        out[f"{cid}/no-report"] = _view(run(cid, LP if cid in ("pcap", "irr", "unf", "wf") else FUND))
    no_shares = [S.Line(x.j, x.ln, x.acct, x.an, x.tt, x.dr, x.cr, x.desc, date=x.date, invst=x.invst) for x in FUND]
    no_dates = [S.Line(x.j, x.ln, x.acct, x.an, x.tt, x.dr, x.cr, x.desc) for x in FUND]
    no_pr = [S.Line(x.j, x.ln, x.acct, x.an, x.tt, x.dr, x.cr, x.desc, inv=x.inv, date=x.date) for x in LP]
    no_agn = [S.Line(x.j, x.ln, x.acct, x.an, x.tt, x.dr, x.cr, x.desc, inv=x.inv, date=x.date, pr=x.pr) for x in LP]
    out["soi/no-shares"] = _view(run("soi", no_shares))
    out["is/no-dates"] = _view(run("is", no_dates))
    out["cf/no-dates"] = _view(run("cf", no_dates))
    out["irr/no-pr"] = _view(run("irr", no_pr))
    out["wf/no-pr"] = _view(run("wf", no_pr))
    out["pcap/not-lp"] = _view(run("pcap", FUND))
    out["unf/no-agn"] = _view(run("unf", no_agn))
    out["tb/incomplete"] = _view(run("tb", FUND[:-1], incomplete=True))
    out["tb/complete-unbalanced"] = _view(run("tb", FUND[:-1]))
    out["tb/report"] = _view(run("tb", FUND, [
        rep("11010 Operating bank account", 382_500.40), rep("15010", 400_003.0), rep("32010", 245_000.0),
        rep("31010", -1_000_000.0), rep("15910", 80_000.0), rep("42010", -45_000.0), rep("42020", -80_000.0),
        rep("51020", 12_500.0), rep("9999 Unmapped client account", 10.0)]))
    out["tb/missing-sides"] = _view(run("tb", FUND, [rep("11010", 382_500.0), rep("31010", -1_000_000.0),
                                                     rep("11020", 50.0)]))
    out["tb/tier-edges"] = _view(run("tb", FUND, [
        rep("11010", 382_499.0), rep("15010", 400_001.0), rep("31010", -1_000_005.0), rep("32010", 250_005.01)]))
    out["is/report"] = _view(run("is", FUND, [
        rep("Realised gains and losses", 45_000.5), rep("Unrealised gains and losses", 80_002.0),
        rep("Professional services", 12_500.0), rep("Net income", 112_500.0)]))
    out["bs/rename"] = _view(run("bs", FUND, [rep("Cash", 382_500.0), rep("Total assets", 862_500.0)]))
    out["soi/report"] = _view(run("soi", FUND, [
        {**rep("Beta Energy Series B", 400_000.0), "measure": "Cost"},
        {**rep("Beta Energy Series B", 480_000.0), "measure": "Fair value"},
        {**rep("Beta Energy Series B", 1001.0), "measure": "Shares"}]))
    out["irr/report"] = _view(run("irr", LP, [
        _cf("Investor Alpha", "2024-06-30", -600_000.0), _cf("Investor Alpha", "2025-05-01", 160_000.0),
        _cf("Investor Beta", "2024-07-20", -400_000.0), _cf("Investor Beta", "2025-05-01", 100_000.0),
        _cf("Investor Alpha", "2025-12-31", 0.0, "Ending capital"),
        _cf("Investor Alpha", "2025-12-31", 3.1, "IRR")]))
    out["unf/report"] = _view(run("unf", LP, [
        _x("Investor Alpha", "Commitment", 1_000_000.0), _x("Investor Alpha", "Unfunded", 410_000.0),
        _x("Investor Beta", "Commitment", 500_000.0), _x("Investor Beta", "Unfunded", 99_000.0)]))
    out["wf/long"] = _view(run("wf", LP, [
        {**rep("", -600_000.0), "investor": "LP-A", "date": "2024-06-30", "tt": "Capital call"},
        {**rep("", -400_000.0), "investor": "LP-B", "date": "2024-07-15", "tt": "Capital call"},
        {**rep("", 150_000.0), "investor": "LP-A", "date": "2025-05-01", "tt": "Distribution"},
        {**rep("", 10_000.0), "investor": "LP-A", "date": "2025-05-01", "tt": "Recallable distribution"},
        {**rep("", 100_000.0), "investor": "LP-B", "date": "2025-05-01", "tt": "Distribution"}]))
    out["wf/transposed"] = _view(run("wf", LP, [
        {**rep("", -600_000.0), "investor": "LP-A", "date": "2024-06-30"},
        {**rep("", -400_000.0), "investor": "LP-B", "date": "2024-07-15"},
        {**rep("", 160_000.0), "investor": "LP-A", "date": "2025-05-01"},
        {**rep("", 100_000.0), "investor": "LP-B", "date": "2025-05-01"}]))
    out["pcap/report"] = _view(run("pcap", LP, [
        _x("Investor Alpha", "Contributions", 600_000.0), _x("Investor Alpha", "Distributions", 150_000.0),
        _x("Investor Alpha", "Ending balance", 450_000.0), _x("Investor Beta", "Ending balance", 300_000.0)]))
    fund = rows_from([L_("F1", 1, "3000", "Capital call", 0, 1_000_000, "2024-06-30"),
                      L_("F2", 1, "3100", "Distribution", 260_000, 0, "2025-05-01")])
    out["pcap/fund-run"] = _view(run("pcap", LP, fund_rows=fund))
    lp = rows_from(LP)
    lp += [R.Row(r.acct, r.fsg, r.fsl, -r.amt, r.date, "", r.tt, offset=True) for r in lp]
    ctx = R.make_ctx(lp, NAMES)
    out["evl/ok"] = _view(R.run_check(R.SPEC["evl"], lp, ctx, COA, None, _resolve, ALIASES, fund, lp))
    short = rows_from([L_("F1", 1, "3000", "Capital call", 0, 999_000, "2024-06-30")])
    out["evl/short"] = _view(R.run_check(R.SPEC["evl"], lp, ctx, COA, None, _resolve, ALIASES, short, lp))
    return out
