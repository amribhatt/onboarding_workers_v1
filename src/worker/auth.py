"""Basic accounts: register once with a full name, an email and a password; sign in; the name is the reviewer.

Plain on purpose. Accounts and sessions are two JSON files in the state directory behind this one class, so a
database can replace them later without the web layer changing. A password is kept only as a salted `scrypt` hash and
is never logged or audited. Every register, sign-in, failed sign-in, password reset and sign-out is one row of the chained audit.

Not SSO and not production authentication: anyone who can reach the server can register.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from worker.audit import AuditLog
from worker.contracts import UnnamedApprover, approver_name
from worker.errors import StatusError
from worker.fsutil import atomic_write_text

SESSION_SECONDS = 12 * 3600
MAX_FAILS = 5  # in a row, per login; then a short wait
WAIT_SECONDS = 30
RESET_SECONDS = 15 * 60  # a reset code lives this long, works once, and allows MAX_RESET_TRIES wrong guesses
MAX_RESET_TRIES = 5
RESET_CODE_DIGITS = 8
_NAME = re.compile(r"^[^\W_][\w .'-]{1,38}[^\W_]$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


class AuthError(StatusError):
    pass


def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT).hex()


class Accounts:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._accounts = self.root / "accounts.json"
        self._sessions = self.root / "sessions.json"
        self._resets = self.root / "resets.json"
        self._audit = AuditLog(self.root / "auth.audit.jsonl")
        self._lock = threading.RLock()
        self._fails: dict[str, tuple[int, float]] = {}

    # ---- the two files -------------------------------------------------------------------------------------
    def _read(self, path: Path) -> dict[str, Any]:
        try:
            return dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {}

    def _write(self, path: Path, data: dict[str, Any]) -> None:
        atomic_write_text(path, json.dumps(data, indent=1, sort_keys=True))

    # ---- register and sign in ------------------------------------------------------------------------------
    def register(self, name: str, email: str, password: str) -> str:
        """Create the account and return a session token. Refuses reserved names, repeats and weak input."""
        name, email = " ".join(str(name).split()), str(email).strip().lower()
        if not _NAME.match(name):
            raise AuthError("Full name: 3 to 40 characters (letters, spaces, dots, dashes or apostrophes).", 400)
        try:
            approver_name(name, demo=False)
        except UnnamedApprover as exc:
            raise AuthError(f"{name!r} is a reserved name, not a person; choose your own.", 400) from exc
        if not _EMAIL.match(email) or len(email) > 120:
            raise AuthError("Enter an email address in the form name@example.com.", 400)
        if not 8 <= len(password) <= 200:
            raise AuthError("Password: at least 8 characters.", 400)
        with self._lock:
            accounts = self._read(self._accounts)
            if email in accounts:  # the email is the one id; two people may share a full name
                raise AuthError("That email is already registered. Sign in instead.", 409)
            salt = os.urandom(16)
            accounts[email] = {"name": name, "email": email, "salt": salt.hex(), "hash": _hash(password, salt),
                               "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            self._audit.record("auth.register", f"account registered for {name}", {"name": name, "email": email})
            self._write(self._accounts, accounts)
            return self._open(name, email)

    def login(self, who: str, password: str) -> tuple[str, str]:
        """(token, name) for the email and password, or `AuthError` (the same words for every miss)."""
        key = " ".join(str(who).split()).casefold()
        with self._lock:
            count, until = self._fails.get(key, (0, 0.0))
            if count >= MAX_FAILS and time.time() < until:
                raise AuthError(f"Too many wrong attempts. Wait {int(until - time.time()) + 1} seconds.", 429)
            hit = self._read(self._accounts).get(key)
            salt = bytes.fromhex(hit["salt"]) if hit else b"\0" * 16  # a miss costs the same work as a hit
            ok = hmac.compare_digest(_hash(str(password), salt), hit["hash"] if hit else "0" * 64) and hit is not None
            if not ok:
                self._fails[key] = (count + 1, time.time() + WAIT_SECONDS)
                self._audit.record("auth.failed", "a sign-in was refused", {"login": key[:120]})
                raise AuthError("Wrong email or password.", 401)
            self._fails.pop(key, None)
            self._audit.record("auth.login", f"{hit['name']} signed in", {"name": hit["name"]})
            return self._open(hit["name"], hit["email"]), str(hit["name"])

    # ---- forgot password -----------------------------------------------------------------------------------
    def forgot(self, email: str) -> tuple[str, str] | None:
        """Start a reset: (name, one-time code) for a registered email, else None. The caller delivers the code and
        answers every email with the same words, so the page never says which emails are registered."""
        key = str(email).strip().lower()
        with self._lock:
            hit = self._read(self._accounts).get(key)
            if not hit:
                self._audit.record("auth.reset_asked", "a reset was asked for an unknown email", {"login": key[:120]})
                return None
            code = "".join(secrets.choice("0123456789") for _ in range(RESET_CODE_DIGITS))
            resets = {k: v for k, v in self._read(self._resets).items() if v["expires"] > time.time()}
            resets[key] = {"hash": hashlib.sha256(code.encode()).hexdigest(), "expires": time.time() + RESET_SECONDS,
                           "tries": 0}
            self._write(self._resets, resets)
            self._audit.record("auth.reset_asked", f"a password reset code was issued for {hit['name']}",
                               {"name": hit["name"]})
            return str(hit["name"]), code

    def reset(self, email: str, code: str, password: str) -> None:
        """Set a new password with a valid one-time code; every open session of the account is signed out."""
        key = str(email).strip().lower()
        if not 8 <= len(password) <= 200:
            raise AuthError("Password: at least 8 characters.", 400)
        bad = AuthError("That code is wrong or has expired. Ask for a new one.", 400)
        with self._lock:
            resets = self._read(self._resets)
            ask = resets.get(key)
            accounts = self._read(self._accounts)
            if not ask or ask["expires"] <= time.time() or key not in accounts:
                raise bad
            given = hashlib.sha256(" ".join(str(code).split()).encode()).hexdigest()
            if not hmac.compare_digest(given, ask["hash"]):
                ask["tries"] += 1
                if ask["tries"] >= MAX_RESET_TRIES:
                    resets.pop(key)
                self._write(self._resets, resets)
                self._audit.record("auth.reset_failed", "a wrong reset code was given", {"login": key[:120]})
                raise bad
            salt = os.urandom(16)
            accounts[key].update({"salt": salt.hex(), "hash": _hash(password, salt)})
            self._audit.record("auth.reset", f"password changed for {accounts[key]['name']}",
                               {"name": accounts[key]["name"]})
            self._write(self._accounts, accounts)
            resets.pop(key)
            self._write(self._resets, resets)
            sessions = {k: v for k, v in self._read(self._sessions).items() if v["email"] != key}
            self._write(self._sessions, sessions)
            self._fails.pop(key, None)

    # ---- sessions -----------------------------------------------------------------------------------------
    def _open(self, name: str, email: str) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        sessions = {k: v for k, v in self._read(self._sessions).items() if v["expires"] > now}
        sessions[hashlib.sha256(token.encode()).hexdigest()] = {"name": name, "email": email,
                                                               "expires": now + SESSION_SECONDS}
        self._write(self._sessions, sessions)
        return token

    def name_of(self, token: str | None) -> str:
        """The signed-in name for a session token, or "" (none, unknown or expired)."""
        if not token:
            return ""
        with self._lock:
            hit = self._read(self._sessions).get(hashlib.sha256(token.encode()).hexdigest())
        return str(hit["name"]) if hit and hit["expires"] > time.time() else ""

    def identity(self, token: str | None) -> tuple[str, str]:
        """(name, email) of a session; the email is the account's one stable id. ("", "") when not signed in."""
        if not token:
            return "", ""
        with self._lock:
            hit = self._read(self._sessions).get(hashlib.sha256(token.encode()).hexdigest())
        return (str(hit["name"]), str(hit["email"])) if hit and hit["expires"] > time.time() else ("", "")

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            sessions = self._read(self._sessions)
            gone = sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)
            if gone:
                self._write(self._sessions, sessions)
                self._audit.record("auth.logout", f"{gone['name']} signed out", {"name": gone["name"]})
