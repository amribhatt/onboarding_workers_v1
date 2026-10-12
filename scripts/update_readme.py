"""Refresh the generated blocks of README.md from the code: `<!-- AUTO:name -->` ... `<!-- /AUTO:name -->`.

Run by the post-merge hook (scripts/install_hooks.sh) so the README follows main. Blocks: `providers` (the model
providers table in worker.agent), `tickets` (one line per tickets/*.md), `commands` (the CLI subcommands). Prose
outside the markers is never touched. Exit 0 either way; `--check` exits 1 when the file would change.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
sys.path.insert(0, str(ROOT / "src"))

PROVIDER_NOTES = {
    "openai": "OpenAI, with `OPENAI_API_KEY`",
    "openrouter": "OpenRouter (free tool-capable models work), with `OPENROUTER_API_KEY`",
    "ollama": "A local model through Ollama: no key, no provider rate limit (`OLLAMA_BASE_URL`)",
    "compatible": "Any OpenAI-style service: `WORKER_BASE_URL`, optional `WORKER_API_KEY`",
    "vertex": "Google Cloud (Vertex AI): `GOOGLE_APPLICATION_CREDENTIALS` is the path of a service-account JSON",
    "demo": "Offline scripted model for the bundled sample; no key",
}


def providers() -> str:
    from worker.agent import PROVIDERS

    rows = ["| `WORKER_MODEL_PROVIDER` | Use it for |", "|---|---|"]
    rows += [f"| `{name}` | {PROVIDER_NOTES.get(name, 'see worker/agent.py')} |" for name in PROVIDERS]
    return "\n".join(rows)


def tickets() -> str:
    out = []
    for p in sorted((ROOT / "tickets").glob("*.md")):
        first = p.read_text(encoding="utf-8").splitlines()[0].lstrip("# ").strip() if p.stat().st_size else p.stem
        out.append(f"- [{first}](tickets/{p.name})")
    return "\n".join(out)


def commands() -> str:
    import argparse

    from worker import cli

    parser = cli.build_parser() if hasattr(cli, "build_parser") else None
    if parser is None:
        return "Run `uv run onboarding-worker --help`."
    sub = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
    if sub is None:
        return "Run `uv run onboarding-worker --help`."
    helps = {c.dest: (c.help or "") for c in sub._choices_actions}
    rows = ["| Command | What it does |", "|---|---|"]
    rows += [f"| `{name}` | {helps.get(name) or (p.description or '').strip() or 'see --help'} |"
             for name, p in sub.choices.items()]
    return "\n".join(rows)


BLOCKS = {"providers": providers, "tickets": tickets, "commands": commands}


def render(text: str) -> str:
    for name, make in BLOCKS.items():
        pat = re.compile(rf"(<!-- AUTO:{name} -->\n).*?(\n<!-- /AUTO:{name} -->)", re.S)
        if pat.search(text):
            text = pat.sub(lambda m: m.group(1) + make() + m.group(2), text)
    return text


def main(argv: list[str]) -> int:
    old = README.read_text(encoding="utf-8")
    new = render(old)
    if "--check" in argv:
        return 0 if new == old else 1
    if new != old:
        README.write_text(new, encoding="utf-8", newline="\n")
        print("README.md refreshed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
