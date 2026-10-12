"""`sandboxd`: the one process that talks to the container runtime, so the worker never mounts the docker socket.

Two verbs: `POST /run` and `GET /health`. A request names blobs by hash and a kind (`layout` or `query`); the
limits come from the kind, the argv from `runner.build_argv`, the image from this service's own configuration
(`WORKER_SANDBOX_DIGEST`): a request that names another image is refused. Any field outside the schema is a 400.
Run it with `python -m worker.flex.sandboxd` on an internal network with no published port.
"""

from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from worker.flex.runner import PROFILES, DockerRunner, RunResult
from worker.replay import BlobStore, ReplayError

FIELDS = {"kind", "exec_id", "seed", "image", "program", "inputs"}
_SHA = re.compile(r"^[0-9a-f]{64}$")
_EXEC = re.compile(r"^[a-z0-9]{8,32}$")
_FILE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,59}$")


class BadRequest(Exception):
    pass


def validate(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict) or set(body) != FIELDS:
        raise BadRequest(f"the request has exactly the fields {sorted(FIELDS)}")
    if body["kind"] not in PROFILES:
        raise BadRequest("unknown kind")
    if not (isinstance(body["exec_id"], str) and _EXEC.match(body["exec_id"])):
        raise BadRequest("bad exec_id")
    if body["seed"] not in (0, 1):
        raise BadRequest("seed is 0 or 1")
    if not (isinstance(body["program"], str) and _SHA.match(body["program"])):
        raise BadRequest("program is a sha256")
    ins = body["inputs"]
    if not (isinstance(ins, dict) and len(ins) <= 100 and all(
            _FILE.match(k) and isinstance(v, str) and _SHA.match(v) for k, v in ins.items())):
        raise BadRequest("inputs are {file name: sha256}")
    return body


class Service:
    def __init__(self, blobs: BlobStore, runner: DockerRunner, slots: int = 4) -> None:
        self.blobs, self.runner = blobs, runner
        self._slots = threading.Semaphore(slots)

    def run(self, body: dict[str, Any]) -> dict[str, Any]:
        body = validate(body)
        if body["image"] != self.runner.image:
            return {"status": "violation", "stderr": "image_mismatch", "seconds": 0.0}
        try:
            program = self.blobs.get(body["program"])
            inputs = {n: self.blobs.get(h) for n, h in body["inputs"].items()}
        except ReplayError as exc:
            raise BadRequest(str(exc)) from exc
        with self._slots:
            r: RunResult = self.runner.run(body["kind"], program, inputs, body["seed"], body["exec_id"])
        return {"status": r.status, "exit_code": r.exit_code, "stderr": r.stderr, "seconds": r.seconds,
                "argv_sha256": r.argv_sha256, "output": self.blobs.put(r.output) if r.output is not None else ""}


def make_server(service: Service, host: str = "0.0.0.0", port: int = 8077) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, doc: dict[str, Any]) -> None:
            data = json.dumps(doc).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._send(200, {"ok": True, "image": service.runner.image}) if self.path == "/health" \
                else self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/run":
                return self._send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("content-length", "0"))
                if n > 65536:
                    raise BadRequest("request too large")
                self._send(200, service.run(json.loads(self.rfile.read(n))))
            except (BadRequest, ValueError) as exc:
                self._send(400, {"error": str(exc)})

        def log_message(self, *a: Any) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    digest = os.environ["WORKER_SANDBOX_DIGEST"]
    blobs = BlobStore(Path(os.environ.get("WORKER_SANDBOX_BLOBS", "/blobs")))
    scratch = os.environ.get("WORKER_SANDBOX_SCRATCH") or None
    make_server(Service(blobs, DockerRunner(digest, scratch=scratch)),
                port=int(os.environ.get("SANDBOXD_PORT", "8077"))).serve_forever()


if __name__ == "__main__":
    main()
