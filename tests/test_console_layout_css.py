"""Two layout rules found in the browser pass: the closed chat launcher must not cover the last button of a page
(125% zoom), and a phone must show every job tab (Activity was clipped at 390 px). Checked on the stylesheet text;
the pixels were measured in a real browser (STATUS.md)."""

from __future__ import annotations

import re
from pathlib import Path

HTML = (Path(__file__).parents[1] / "src" / "worker" / "static" / "console.html").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    m = re.search(re.escape(selector) + r"\{([^}]*)\}", HTML)
    assert m, selector
    return m.group(1)


def test_the_page_scroll_area_reserves_the_launcher_and_its_margin_and_some_air() -> None:
    main = _rule(".main")
    pad = re.search(r"padding:[^;]*calc\(var\(--fab,(\d+)px\) \+ (\d+)px \+ (\d+)px\)", main)
    assert pad, main
    fab, margin, air = map(int, pad.groups())
    assert fab >= 54 and margin >= 16 and air >= 16
    fab_rule = _rule(".cop-fab")
    assert "bottom:16px" in fab_rule and f"width:var(--fab,{fab}px)" in fab_rule  # one size for both


def test_phone_tabs_wrap_instead_of_clipping() -> None:
    m = re.search(r"@media\(max-width:(\d+)px\)\{\.tabs\{([^}]*)\}", HTML)
    assert m, "no phone rule for .tabs"
    assert int(m.group(1)) >= 430 and "flex-wrap:wrap" in m.group(2)


def test_a_grid_card_has_one_column_that_may_shrink() -> None:
    """A card laid out as a grid sized its one auto column to its widest child, so the Reconcile tab was 7 px wider
    than a 312 px screen (a 390 px phone at 125% zoom) and the page scrolled sideways (browser pass 3)."""
    assert ".card{grid-template-columns:minmax(0,1fr)}" in HTML


def _tokens(start: str) -> dict[str, str]:
    i = HTML.index(start)
    block = HTML[i:HTML.index("}", i)]
    out = {}
    for name, value in re.findall(r"--([a-z-]+):(#[0-9a-fA-F]{3,6})", block):
        out[name] = value if len(value) == 7 else "#" + "".join(ch * 2 for ch in value[1:])
    return out


def _lum(color: str) -> float:
    def lin(v: int) -> float:
        s = v / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(int(color[1:3], 16)) + 0.7152 * lin(int(color[3:5], 16)) + 0.0722 * lin(int(color[5:7], 16))


def _ratio(a: str, b: str) -> float:
    x, y = _lum(a), _lum(b)
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)


TEXT_ON = [("ink", "bg"), ("ink", "card"), ("sub", "bg"), ("sub", "card"), ("sub", "nav"), ("sub", "line"), ("faint", "bg"),
           ("faint", "card"), ("faint", "nav"), ("faint", "sel"), ("link", "card"), ("link", "bg"), ("link", "sel"),
           ("navy-ink", "navy"), ("navy-ink", "ok"), ("ok", "ok-bg"), ("warn", "warn-bg"), ("err", "err-bg"),
           ("info", "info-bg")]


def test_the_text_colours_meet_wcag_aa_on_the_surfaces_they_sit_on() -> None:
    """Computed in the browser pass: the grey `faint` text was 4.24:1 on the navigation and 3.98:1 on a selected row,
    and the warning pill 4.45:1, both under the 4.5:1 AA floor for text this size."""
    for theme, start in (("light", ":root{"), ("dark", ':root[data-theme="dark"]{')):
        t = _tokens(start)
        for fg, bg in TEXT_ON:
            assert _ratio(t[fg], t[bg]) >= 4.5, f"{theme}: {fg} {t[fg]} on {bg} {t[bg]} is {_ratio(t[fg], t[bg]):.2f}:1"
