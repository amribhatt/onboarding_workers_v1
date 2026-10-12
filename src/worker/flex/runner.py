"""Running one program in a locked-down container, and the three ways to reach a container runtime.

The argv is built by `build_argv` from constants and a validated image digest; nothing a program, a model or a
request says becomes a docker argument. `DockerRunner` runs it through the `docker` CLI on this host (development,
CI). `SidecarRunner` asks `sandboxd` (a separate service that alone holds the daemon): the worker never mounts the
docker socket. There is no other runner and no fallback to an in-process subprocess: with neither, tiers 2 and 3
are off (`runner_from_env` returns None).

Output is untrusted. The host reads exactly one file from /out (not a symlink, a regular file under a size cap)
and nothing else; stdout is discarded and stderr is clipped.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

DIGEST = re.compile(r"(^|@)sha256:[0-9a-f]{64}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,59}$")
OUT_FILE = {"layout": "table.json", "query": "result.json"}
STDERR_CLIP = 2000


@dataclass(frozen=True)
class Limits:
    seconds: int
    memory_mb: int = 512
    pids: int = 64
    cpus: int = 1
    tmpfs_mb: int = 64
    fsize_mb: int = 100
    nofile: int = 64
    out_bytes: int = 100 * 1024 * 1024


PROFILES: dict[str, Limits] = {"layout": Limits(seconds=60), "query": Limits(seconds=20, out_bytes=10 * 1024 * 1024)}


@dataclass(frozen=True)
class RunResult:
    status: str  # ok | timeout | oom | exit | no_output | violation
    exit_code: int | None
    output: bytes | None
    stderr: str
    seconds: float
    argv_sha256: str
    exec_id: str


class Runner(Protocol):
    """What the rest of the code needs: a name for the image it runs and `run`."""

    image: str

    def run(self, kind: str, program: bytes, inputs: Mapping[str, bytes], seed: int, exec_id: str) -> RunResult: ...


def build_argv(*, name: str, exec_dir: Path, image: str, kind: str, seed: int, limits: Limits) -> list[str]:
    """The whole `docker run` command. Every isolation flag is here and nowhere else."""
    if not DIGEST.search(image):
        raise ValueError("the image is run by digest, never by tag")
    if kind not in OUT_FILE:
        raise ValueError(f"unknown kind {kind!r}")
    d = Path(exec_dir)
    mounts = [("in", "/in", True), ("prog", "/prog", True), ("out", "/out", False)]
    argv = [
        "docker", "run", "--name", name, "--label", f"ow.exec={name}", "--label", f"ow.kind={kind}",
        "--network=none", "--read-only", "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmpfs_mb}m",
        "--user", "65534:65534", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        f"--pids-limit={limits.pids}", f"--memory={limits.memory_mb}m", f"--memory-swap={limits.memory_mb}m",
        f"--cpus={limits.cpus}", "--ulimit", f"nofile={limits.nofile}:{limits.nofile}",
        "--ulimit", f"fsize={limits.fsize_mb * 1024 * 1024}", "--ulimit", "core=0",
        "--ulimit", f"cpu={limits.seconds}:{limits.seconds}", "--ipc=none", "--init",
        "--log-driver=none", "-e", f"PYTHONHASHSEED={seed}", "-e", "TZ=UTC", "-e", "LC_ALL=C.UTF-8",
        "-e", "LANG=C.UTF-8", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONIOENCODING=utf-8",
    ]
    for sub, dst, ro in mounts:
        argv += ["--mount", f"type=bind,src={d / sub},dst={dst}" + (",readonly" if ro else "")]
    return argv + [image, "python", "-B", "-s", f"/harness/{kind}.py"]


def _drain(stream: Any, keep: list[bytes]) -> None:
    """Read stderr to the end, keeping only the first STDERR_CLIP bytes (a flood must not fill the host)."""
    got = 0
    for chunk in iter(lambda: stream.read(4096), b""):
        if got < STDERR_CLIP:
            keep.append(chunk[: STDERR_CLIP - got])
            got += len(chunk)


def read_one_file(path: Path, cap: int) -> bytes | None:
    """The file if it is a regular file (not a link) within `cap` bytes; None otherwise."""
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > cap:
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        return fh.read(cap + 1) if st.st_size <= cap else None


class DockerRunner:
    def __init__(self, image: str, docker: str = "docker", scratch: str | Path | None = None,
                 start_grace: float | None = None) -> None:
        self.image, self.docker = image, docker
        # container start is not program time: the wall clock is the profile's seconds plus this
        env_grace = float(os.environ.get("WORKER_SANDBOX_START_GRACE", "60"))
        self.start_grace = env_grace if start_grace is None else start_grace
        self.scratch = Path(scratch) if scratch else None  # outside OneDrive on Windows; default: the temp dir

    def run(self, kind: str, program: bytes, inputs: Mapping[str, bytes], seed: int, exec_id: str) -> RunResult:
        limits = PROFILES[kind]
        name = f"ow-sbx-{exec_id}"
        if not _NAME.match(exec_id):
            raise ValueError("bad exec id")
        d = Path(tempfile.mkdtemp(prefix="ow-exec-", dir=self.scratch))
        try:
            for sub in ("in", "prog", "out"):
                (d / sub).mkdir()
            (d / "out").chmod(0o777)  # the container user is nobody; the host reads one file back
            (d / "prog" / "program.py").write_bytes(program)
            for fname, data in inputs.items():
                if not _NAME.match(fname.lower()):
                    raise ValueError("bad input name")
                (d / "in" / fname).write_bytes(data)
            argv = build_argv(name=name, exec_dir=d, image=self.image, kind=kind, seed=seed, limits=limits)
            argv[0] = self.docker
            return self._exec(argv, kind, name, d, limits, exec_id)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def _exec(self, argv: list[str], kind: str, name: str, d: Path, limits: Limits, exec_id: str) -> RunResult:
        t0 = time.monotonic()
        err: list[bytes] = []
        proc = subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)
        pump = threading.Thread(target=_drain, args=(proc.stderr, err), daemon=True)
        pump.start()
        timed_out = False
        try:
            proc.wait(timeout=limits.seconds + self.start_grace)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            proc.wait()
            for _ in range(5):  # the daemon may still be creating it when the CLI is killed: remove until gone
                subprocess.run([self.docker, "rm", "-f", name], capture_output=True, check=False)
                if name not in self.remaining():
                    break
                time.sleep(1)
        pump.join(timeout=5)
        out = None if timed_out else read_one_file(d / "out" / OUT_FILE[kind], limits.out_bytes)
        code = proc.returncode
        oom = False if timed_out else self._reap(name)
        elapsed = round(time.monotonic() - t0, 3)
        # exit 137 is SIGKILL from two causes: the kernel's memory kill (the container's OOMKilled state) or the
        # cpu ulimit's hard limit (a time limit, so "timeout"); a kill that came earlier than the limit is "exit"
        status = ("timeout" if timed_out else "oom" if oom else
                  "timeout" if code == 137 and elapsed >= limits.seconds else "exit" if code != 0
                  else "ok" if out is not None else "no_output")
        if status == "ok" and not out:
            status = "no_output"
        return RunResult(status, code, out if status == "ok" else None,
                         b"".join(err).decode("utf-8", errors="replace"), elapsed,
                         hashlib.sha256(" ".join(argv[1:]).encode("utf-8")).hexdigest(), exec_id)

    def _reap(self, name: str) -> bool:
        """Whether the daemon killed the container for memory (its OOMKilled state), then remove it. The container
        is not started with --rm precisely so that this state can be read; it is removed here, always."""
        p = subprocess.run([self.docker, "inspect", "--format", "{{.State.OOMKilled}}", name], capture_output=True,
                           text=True, check=False)
        subprocess.run([self.docker, "rm", "-f", name], capture_output=True, check=False)
        return p.returncode == 0 and p.stdout.strip() == "true"

    def remaining(self) -> list[str]:
        """Containers this runner started that still exist (there should be none)."""
        p = subprocess.run([self.docker, "ps", "-a", "--filter", "label=ow.kind", "--format", "{{.Names}}"],
                           capture_output=True, text=True, check=False)
        return [n for n in p.stdout.split() if n.startswith("ow-sbx-")]


def resolve_image(ref: str, docker: str = "docker") -> str | None:
    """The image ID (a digest) of a local image, or None when Docker or the image is not there."""
    try:
        p = subprocess.run([docker, "image", "inspect", ref, "--format", "{{.Id}}"], capture_output=True, text=True,
                           timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    out = p.stdout.strip()
    return out if p.returncode == 0 and DIGEST.search(out) else None


class SidecarRunner:
    """Calls `sandboxd`. Programs and inputs travel as blob hashes in the shared content-addressed store; the
    request carries no docker argument, no path and no limit."""

    def __init__(self, url: str, blobs: Any, image: str) -> None:
        self.url, self.blobs, self.image = url.rstrip("/"), blobs, image

    def run(self, kind: str, program: bytes, inputs: Mapping[str, bytes], seed: int, exec_id: str) -> RunResult:
        body = {"kind": kind, "exec_id": exec_id, "seed": seed, "image": self.image,
                "program": self.blobs.put(program), "inputs": {n: self.blobs.put(b) for n, b in inputs.items()}}
        req = urllib.request.Request(self.url + "/run", data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=PROFILES[kind].seconds + 150) as resp:
                r = json.loads(resp.read())
        except (OSError, ValueError) as exc:  # sandboxd down or answering nonsense: a refusal, never a fallback
            return RunResult("violation", None, None, f"sandboxd unreachable: {exc}"[:300], 0.0, "", exec_id)
        out = self.blobs.get(r["output"]) if r.get("output") else None
        return RunResult(r["status"], r.get("exit_code"), out, r.get("stderr", ""), r.get("seconds", 0.0),
                         r.get("argv_sha256", ""), exec_id)


def runner_from_env(blobs: Any = None, env: Mapping[str, str] | None = None) -> Runner | None:
    """`SANDBOXD_URL` (the sidecar) or, on a developer host with Docker, the CLI. None: the sandbox is off."""
    e = env if env is not None else os.environ
    image_ref = e.get("WORKER_SANDBOX_IMAGE", "ow-sandbox:1")
    pin = e.get("WORKER_SANDBOX_DIGEST", "").strip()
    if e.get("SANDBOXD_URL") and blobs is not None:
        return SidecarRunner(e["SANDBOXD_URL"], blobs, pin or image_ref)
    if e.get("WORKER_SANDBOX", "docker") == "off":
        return None
    digest = resolve_image(image_ref)
    if digest is None or (pin and digest != pin):
        return None
    return DockerRunner(digest, scratch=e.get("WORKER_SANDBOX_SCRATCH") or None)


def new_exec_id() -> str:
    return uuid.uuid4().hex[:16]
