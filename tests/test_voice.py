"""The worker speaks as "I" to "you" (docs/voice-guide.md): no role words for the person, and the one shape for a refusal.
Checks the strings a person can see: the console modules, the chat, the sign-in, and the pages' scripts."""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "worker"
LIVE_PY = sorted(SRC.glob("console*.py")) + [SRC / "auth.py"]
LIVE_JS = sorted((SRC / "static").glob("*.js")) + sorted((SRC / "static").glob("*.html"))
ROLE_WORDS = re.compile(r"\b(the person|a person|the analyst|analyst|the worker|the human|a human|the reviewer|the user|"
                        r"the system|named (person|human|reviewer|controller|approver))\b", re.IGNORECASE)
# a check that something is a real name: it says what is wrong with the name, it does not call the reader a role
ALLOWED = ("isn't a person's name", "is not a person's name")


def _py_strings(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and n.body
            and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant)}
    return [(n.lineno, " ".join(n.value.split())) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs and len(n.value) > 8]


def _js_lines(path: Path) -> list[tuple[int, str]]:
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), path.read_text(encoding="utf-8"), flags=re.DOTALL)
    return [(i, re.sub(r"(^|\s)//.*$", "", line)) for i, line in enumerate(text.splitlines(), 1)]


def test_nothing_a_person_sees_calls_them_or_the_worker_by_a_role_word() -> None:
    bad = []
    for p in LIVE_PY:
        bad += [f"{p.name}:{ln}: {s[:110]}" for ln, s in _py_strings(p) if ROLE_WORDS.search(s) and not any(a in s for a in ALLOWED)]
    for p in LIVE_JS:
        bad += [f"{p.name}:{ln}: {s.strip()[:110]}" for ln, s in _js_lines(p) if ROLE_WORDS.search(s)]
    assert not bad, "role words in what a person reads (say I / you, docs/voice-guide.md):\n" + "\n".join(bad)


def test_a_failed_record_is_said_in_the_one_shape() -> None:
    old = re.compile(r"Nothing was changed, because|Nothing was written")
    bad = [f"{p.name}:{ln}: {s[:100]}" for p in LIVE_PY for ln, s in _py_strings(p) if old.search(s)]
    assert not bad, "say \"I couldn't record ..., so nothing changed\" / \"I didn't write the file\":\n" + "\n".join(bad)


def test_the_model_is_told_to_say_i_and_you() -> None:
    from worker.prompts import _BODY
    from worker.role_runner import COMMON_RULES

    for text in (COMMON_RULES, _BODY):
        assert 'Say "I" for yourself and "you"' in text and '"the user"' in text  # and it is told not to write the role words
