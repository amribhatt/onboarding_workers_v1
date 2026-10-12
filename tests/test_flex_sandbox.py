"""Step 2 of ticket 11, the container: the argv contract (no Docker), the sidecar's schema (no Docker), and the
malicious suite (`sandbox` marker: needs Docker and the `ow-sandbox:1` image; skipped with the reason when absent).

Each hostile program is handed straight to the runner, past the usability lint, because the container is the
boundary and the lint is not."""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from worker.flex import runner as R
from worker.flex import sandboxd
from worker.flex.derive import run_layout
from worker.flex.grid import canonical_json, grid_from_rows
from worker.flex.verify import verify
from worker.replay import BlobStore

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "sha256:" + "ab" * 32


# ---- argv contract (no Docker) ---------------------------------------------------------------------------
def argv(**over: Any) -> list[str]:
    base: dict[str, Any] = {"name": "ow-sbx-abc", "exec_dir": Path("/x/exec"), "image": IMAGE, "kind": "layout",
                            "seed": 0, "limits": R.PROFILES["layout"]}
    base.update(over)
    return R.build_argv(**base)


def test_argv_has_every_isolation_flag() -> None:
    a = argv()
    joined = " ".join(a)
    for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                 "--pids-limit=64", "--memory=512m", "--memory-swap=512m", "--cpus=1", "--ipc=none",
                 "--log-driver=none", "--user 65534:65534", "nofile=64:64", "core=0", "PYTHONHASHSEED=0", "TZ=UTC",
                 "/tmp:rw,noexec,nosuid,nodev,size=64m"):
        assert flag in joined, flag
    assert "--rm" not in a  # the runner reads OOMKilled, then removes the container itself
    assert a[-5:] == [IMAGE, "python", "-B", "-s", "/harness/layout.py"]
    assert "PYTHONHASHSEED=1" in " ".join(argv(seed=1))


def _classified(monkeypatch: pytest.MonkeyPatch, code: int, oom: bool, wall: float, timed_out: bool = False) -> str:
    """The status for a fake container: exit code, the daemon's OOMKilled state and the seconds it ran."""
    import types

    class Proc:
        returncode = code
        stderr = types.SimpleNamespace(read=lambda n: b"")

        def __init__(self, *a: Any, **k: Any) -> None: ...
        def wait(self, timeout: float | None = None) -> int:
            if timed_out and timeout is not None:
                raise R.subprocess.TimeoutExpired("docker", timeout or 0)
            return code
        def kill(self) -> None: ...

    clock = iter([0.0, wall, wall, wall])
    monkeypatch.setattr(R.subprocess, "Popen", Proc)
    monkeypatch.setattr(R.time, "monotonic", lambda: next(clock, wall))
    monkeypatch.setattr(R.time, "sleep", lambda s: None)
    r = R.DockerRunner(IMAGE, start_grace=0)
    monkeypatch.setattr(r, "_reap", lambda name: oom)
    monkeypatch.setattr(r, "remaining", list)
    monkeypatch.setattr(R.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout="", returncode=0))
    lim = R.PROFILES["layout"]
    return r._exec(["docker"], "layout", "ow-sbx-x", Path("/nonexistent"), lim, "x").status


def test_exit_137_is_oom_only_when_the_daemon_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    lim = R.PROFILES["layout"].seconds
    assert _classified(monkeypatch, 137, True, 3.0) == "oom"
    assert _classified(monkeypatch, 137, False, lim + 0.5) == "timeout"  # the cpu ulimit's SIGKILL is a time limit
    assert _classified(monkeypatch, 137, False, 1.0) == "exit"  # killed early by something else: not a timeout, not oom
    assert _classified(monkeypatch, 1, False, 1.0) == "exit"
    assert _classified(monkeypatch, 0, False, lim + 5.0, timed_out=True) == "timeout"


def test_argv_mounts_only_the_exec_directory_and_never_the_socket() -> None:
    mounts = [a for a in argv() if a.startswith("type=bind")]
    assert len(mounts) == 3 and all(str(Path("/x/exec")) in m for m in mounts)
    assert [m.endswith(",readonly") for m in mounts] == [True, True, False]  # /in and /prog read-only, /out not
    text = " ".join(argv())
    assert "docker.sock" not in text and " -v " not in text and "--privileged" not in text
    assert "--network=host" not in text and "--volumes-from" not in text


def test_the_image_is_run_by_digest_never_by_tag() -> None:
    for bad in ("ow-sandbox:1", "python:3.12-slim", "latest", "ow-sandbox@sha256:short"):
        with pytest.raises(ValueError):
            argv(image=bad)
    argv(image="ow-sandbox@sha256:" + "0" * 64)  # a repository digest is fine
    with pytest.raises(ValueError):
        argv(kind="shell")


def test_nothing_from_a_request_becomes_a_docker_argument() -> None:
    ok = {"kind": "layout", "exec_id": "abcdef0123456789", "seed": 0, "image": IMAGE, "program": "a" * 64,
          "inputs": {"grid.json": "b" * 64}}
    assert sandboxd.validate(ok) == ok
    for extra in ({"argv": ["--privileged"]}, {"mounts": ["/:/host"]}, {"limits": {"memory_mb": 99999}},
                  {"network": "host"}):
        with pytest.raises(sandboxd.BadRequest):
            sandboxd.validate({**ok, **extra})
    for field, bad in (("kind", "shell"), ("seed", 5), ("exec_id", "../x"), ("program", "not-a-hash"),
                       ("inputs", {"../../etc/passwd": "b" * 64})):
        with pytest.raises(sandboxd.BadRequest):
            sandboxd.validate({**ok, field: bad})


def test_the_sidecar_refuses_another_image(tmp_path: Path) -> None:
    svc = sandboxd.Service(BlobStore(tmp_path / "blobs"), R.DockerRunner(IMAGE))
    blobs = svc.blobs
    body = {"kind": "layout", "exec_id": "abcdef0123456789", "seed": 0, "image": "sha256:" + "cd" * 32,
            "program": blobs.put(b"x"), "inputs": {}}
    r = svc.run(body)
    assert r["status"] == "violation" and r["stderr"] == "image_mismatch"


def test_the_runner_is_off_without_docker_or_a_sidecar() -> None:
    assert R.runner_from_env(env={"WORKER_SANDBOX": "off"}) is None
    assert R.runner_from_env(env={"WORKER_SANDBOX_IMAGE": "no-such-image:0", "WORKER_SANDBOX": "docker"}) is None


# ---- the container (needs Docker) ------------------------------------------------------------------------
@pytest.fixture(scope="session")
def docker_runner() -> R.DockerRunner:
    digest = R.resolve_image("ow-sandbox:1")
    if digest is None:
        try:
            subprocess.run(["docker", "build", "-t", "ow-sandbox:1", str(ROOT / "sandbox")], check=True,
                           capture_output=True, timeout=600)
        except (OSError, subprocess.SubprocessError):
            pytest.skip("Docker or the ow-sandbox image is not available")
        digest = R.resolve_image("ow-sandbox:1")
    if digest is None:
        pytest.skip("the ow-sandbox image is not available")
    return R.DockerRunner(digest, start_grace=150.0)  # a loaded Docker Desktop takes minutes to start a container


@pytest.fixture
def short(monkeypatch: pytest.MonkeyPatch, docker_runner: R.DockerRunner) -> None:
    monkeypatch.setitem(R.PROFILES, "layout", R.Limits(seconds=5))
    monkeypatch.setattr(docker_runner, "start_grace", 25.0)  # container start on a slow host is not program time


GRID = grid_from_rows({"S": [["Name", "Amt"], ["A", 10], ["B", 5]]})
GRID_BYTES = canonical_json(GRID)
GOOD = """
def build(book):
    refs = [(r, book.ref('S', r, 1), book.ref('S', r, 2)) for r in book.rows('S') if r > 1]
    return {'columns': [{'name': 'Name', 'role': 'dimension'}, {'name': 'Amount', 'role': 'measure'}],
            'rows': [{'cells': [{'ref': a}, {'ref': b}]} for _, a, b in refs],
            'dropped': [{'ref': 'S!A1', 'reason': 'header'}, {'ref': 'S!B1', 'reason': 'header'}], 'case': 'other'}
"""


def raw(runner: R.DockerRunner, program: str, seed: int = 0, grid: bytes = GRID_BYTES) -> R.RunResult:
    return runner.run("layout", program.encode(), {"grid.json": grid}, seed, R.new_exec_id())


class Sink:
    def __init__(self) -> None:
        self.rows: list[str] = []

    def record(self, action: str, why: str, i: dict | None = None, o: dict | None = None) -> dict:
        self.rows.append(action)
        return {}


@pytest.mark.sandbox
def test_a_real_program_runs_twice_in_the_container_and_verifies(docker_runner: R.DockerRunner) -> None:
    sink = Sink()
    run = run_layout(runner=docker_runner, audit=sink, grid=GRID, program=GOOD, why="real container")
    assert run.verified.ok, run.verified.blocks
    assert run.statuses == ("ok", "ok") and run.output_sha256s[0] == run.output_sha256s[1]
    assert run.verified.table["rows"] == [["A", "10"], ["B", "5"]]
    assert sink.rows == ["flex.exec.started", "flex.exec.finished", "flex.verify"]
    assert docker_runner.remaining() == []  # the runner removes it after reading its state


HOSTILE = """
import json, os, socket

def attempt(fn):
    try:
        fn()
        return "SUCCEEDED"
    except BaseException as exc:
        return type(exc).__name__ + ": " + str(exc)[:80]

def udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    s.sendto(b"x", ("1.1.1.1", 53))

def write(path):
    def go():
        with open(path, "w") as fh:
            fh.write("x")
    return go

def fill(path, size):
    def go():
        with open(path, "wb") as fh:
            fh.write(b"x" * size)
    return go

def build(book):
    seen = {
        "tcp": attempt(lambda: socket.create_connection(("1.1.1.1", 443), timeout=3)),
        "udp": attempt(udp),
        "dns": attempt(lambda: socket.getaddrinfo("example.com", 80)),
        "write_in": attempt(write("/in/grid.json")),
        "write_prog": attempt(write("/prog/program.py")),
        "write_harness": attempt(write("/harness/layout.py")),
        "write_etc": attempt(write("/etc/hostname")),
        "tmp_fill": attempt(fill("/tmp/big", 300 * 1024 ** 2)),
        "out_fill": attempt(fill("/out/junk", 300 * 1024 ** 2)),
        "host_path": attempt(lambda: open(HOST_CANARY).read()),
        "env": sorted(os.environ),
        "environ1": attempt(lambda: open("/proc/1/environ").read().replace(chr(0), " ")),
        "root": sorted(os.listdir("/")),
    }
    if os.path.exists("/out/junk"):
        os.remove("/out/junk")
    return {"columns": [], "rows": [], "dropped": [], "seen": seen}
"""


@pytest.mark.sandbox
def test_hostile_program_gets_no_network_no_writes_no_host_data(docker_runner: R.DockerRunner, tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    canary = tmp_path / "canary.txt"
    canary.write_text("host data", encoding="utf-8")
    before = hashlib.sha256(canary.read_bytes()).hexdigest()
    r = raw(docker_runner, f"HOST_CANARY = {str(canary)!r}\n" + HOSTILE)
    assert r.status == "ok" and r.output is not None, r.stderr
    seen = json.loads(r.output)["seen"]
    for probe in ("tcp", "udp", "dns"):
        assert "SUCCEEDED" not in seen[probe] and seen[probe].startswith(
            ("OSError", "gaierror", "TimeoutError", "ConnectionRefused", "PermissionError")), (probe, seen[probe])
    for probe in ("write_in", "write_prog", "write_harness", "write_etc"):
        assert "SUCCEEDED" not in seen[probe] and ("Read-only" in seen[probe] or "Permission" in seen[probe]), probe
    assert "No space" in seen["tmp_fill"]  # the tmpfs is 64 MB
    assert "SUCCEEDED" not in seen["out_fill"]  # the fsize ulimit
    assert "FileNotFoundError" in seen["host_path"]
    assert not any(w in k for k in seen["env"] for w in ("OPENAI", "OPENROUTER", "DATABASE", "TOKEN", "SECRET", "PASSWORD"))
    assert "OPENAI" not in seen["environ1"] and "Users" not in seen["root"]
    assert hashlib.sha256(canary.read_bytes()).hexdigest() == before


@pytest.mark.sandbox
def test_a_symlink_as_the_output_is_refused(docker_runner: R.DockerRunner) -> None:
    program = "import os\ndef build(book):\n    os.symlink('/in/grid.json', '/out/table.json')\n    return {}\n"
    r = raw(docker_runner, program)
    assert r.output is None and r.status in ("no_output", "exit")


@pytest.mark.sandbox
def test_an_infinite_loop_times_out_and_the_container_is_gone(docker_runner: R.DockerRunner, short: None) -> None:
    r = raw(docker_runner, "def build(book):\n    while True:\n        pass\n")
    assert r.status in ("timeout", "exit") and r.output is None  # exit: the cpu ulimit got there first
    assert docker_runner.remaining() == []


@pytest.mark.sandbox
def test_a_memory_bomb_is_killed(docker_runner: R.DockerRunner) -> None:
    r = raw(docker_runner, "def build(book):\n    x = bytearray(2 * 1024 ** 3)\n    return {'n': len(x)}\n")
    assert r.status in ("oom", "exit") and r.output is None
    assert r.exit_code in (137, 1)


@pytest.mark.sandbox
def test_a_fork_bomb_is_stopped_by_the_pid_limit(docker_runner: R.DockerRunner, short: None) -> None:
    program = ("import os, time\ndef build(book):\n    while True:\n        try:\n            "
               "if os.fork() == 0:\n                time.sleep(30)\n                os._exit(0)\n"
               "        except OSError:\n            raise SystemExit(7)\n")
    r = raw(docker_runner, program)
    assert r.output is None and r.status in ("exit", "timeout", "oom")
    assert docker_runner.remaining() == []


@pytest.mark.sandbox
def test_a_stdout_flood_is_bounded(docker_runner: R.DockerRunner, short: None) -> None:
    program = ("import sys\ndef build(book):\n    sys.stdout.write('x' * (60 * 1024 ** 2))\n"
               "    sys.stderr.write('e' * (5 * 1024 ** 2))\n    return {'columns': [], 'rows': [], 'dropped': []}\n")
    r = raw(docker_runner, program)
    assert r.status in ("ok", "timeout") and len(r.stderr) <= R.STDERR_CLIP  # run to the end, or killed
    assert docker_runner.remaining() == []


@pytest.mark.sandbox
def test_nondeterminism_is_caught_by_the_second_seed(docker_runner: R.DockerRunner) -> None:
    program = """
def build(book):
    order = sorted({f'n{i}' for i in range(20)}, key=hash)
    return {'columns': [{'name': c, 'role': 'dimension'} for c in order], 'rows': [], 'dropped': []}
"""
    run = run_layout(runner=docker_runner, audit=Sink(), grid=GRID, program=program, why="seeds")
    assert "nondeterministic" in [b["code"] for b in run.verified.blocks]
    assert run.verified.table is None


@pytest.mark.sandbox
def test_a_forged_value_is_refused_not_trusted(docker_runner: R.DockerRunner) -> None:
    program = """
def build(book):
    return {'columns': [{'name': 'Name', 'role': 'dimension'}, {'name': 'Amount', 'role': 'measure'}],
            'rows': [{'cells': [{'ref': 'S!A2'}, {'value': '99999'}]}], 'dropped': []}
"""
    r = raw(docker_runner, program)  # past the lint on purpose: the host must refuse it on its own
    assert r.status == "ok"
    v = verify(GRID, r.output)
    assert [b["code"] for b in v.blocks] == ["bad_output"] and v.table is None


@pytest.mark.sandbox
def test_the_sidecar_runs_the_same_program_by_hash(docker_runner: R.DockerRunner, tmp_path: Path) -> None:
    blobs = BlobStore(tmp_path / "blobs")
    server = sandboxd.make_server(sandboxd.Service(blobs, docker_runner), "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        side = R.SidecarRunner(f"http://127.0.0.1:{port}", blobs, docker_runner.image)
        run = run_layout(runner=side, audit=Sink(), grid=GRID, program=GOOD, why="via sidecar")
        assert run.verified.ok and run.statuses == ("ok", "ok")
        wrong = R.SidecarRunner(f"http://127.0.0.1:{port}", blobs, "sha256:" + "ef" * 32)
        bad = run_layout(runner=wrong, audit=Sink(), grid=GRID, program=GOOD, why="wrong image")
        assert not bad.verified.ok
    finally:
        server.shutdown()


def test_only_sandboxd_mounts_the_docker_socket() -> None:
    import yaml

    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    holders = [n for n, s in services.items() if any("docker.sock" in str(v) for v in s.get("volumes", []))]
    assert holders == ["sandboxd"]
    assert services["sandboxd"]["profiles"] == ["sandbox"] and "ports" not in services["sandboxd"]
    assert services["sandboxd"]["networks"] == ["backend"]


def test_an_unreachable_sidecar_is_a_refusal_not_a_fallback(tmp_path: Path) -> None:
    side = R.SidecarRunner("http://127.0.0.1:9", BlobStore(tmp_path / "b"), IMAGE)  # nothing listens on port 9
    r = side.run("layout", b"def build(book): return {}", {"grid.json": b"{}"}, 0, "abcdef0123456789")
    assert r.status == "violation" and r.output is None and "unreachable" in r.stderr
