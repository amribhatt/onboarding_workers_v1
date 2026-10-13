"""Basic accounts: register, sign in, the session is the reviewer, nothing secret is kept or logged."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from worker.auth import Accounts, AuthError
from worker.web import create_app

GOOD = {"name": "Priya Rao", "email": "Priya@Example.com", "password": "correct horse 1"}


def client(tmp_path: Path, login: bool = True) -> TestClient:
    env = {"WORKER_MODEL_PROVIDER": "demo", "WORKER_ENV_FILE": str(tmp_path / "e.env")}
    if login:
        env["WORKER_REQUIRE_LOGIN"] = "1"
    return TestClient(create_app(tmp_path / "state", environ=env), follow_redirects=False)


def test_no_session_means_the_login_page_and_a_401_on_the_api(tmp_path: Path) -> None:
    c = client(tmp_path)
    assert c.get("/console").status_code in (302, 307) and c.get("/console").headers["location"] == "/login"
    assert c.get("/api/console/state").status_code == 401
    assert c.get("/login").status_code == 200 and c.get("/register").status_code == 200


def test_register_signs_you_in_and_the_session_is_the_reviewer(tmp_path: Path) -> None:
    c = client(tmp_path)
    assert c.post("/api/auth/register", json=GOOD).status_code == 200
    me = c.get("/api/auth/me").json()
    assert me == {"login_required": True, "name": "Priya Rao"}
    st = c.get("/api/console/state").json()
    assert st["me"] == "Priya Rao" and st["login"] is True
    # a forged header does not change who acts
    r = c.post("/api/console/act", json={"action": "view|dash", "text": ""}, headers={"X-Reviewer": "Someone Else"})
    assert r.status_code == 200


def test_sign_in_by_email_or_name_and_a_wrong_password_is_refused_and_audited(tmp_path: Path) -> None:
    c = client(tmp_path)
    c.post("/api/auth/register", json=GOOD)
    c.post("/api/auth/logout")
    assert c.get("/api/auth/me").json()["name"] == ""
    assert c.post("/api/auth/login", json={"login": "priya@example.com", "password": GOOD["password"]}).status_code == 200
    c.post("/api/auth/logout")
    # the email is the only sign-in id: the full name is not one
    assert c.post("/api/auth/login", json={"login": "Priya Rao", "password": GOOD["password"]}).status_code == 401
    bad = c.post("/api/auth/login", json={"login": "priya@example.com", "password": "not it at all"})
    assert bad.status_code == 401 and "Wrong" in bad.json()["detail"]
    audit = (tmp_path / "state" / "auth" / "auth.audit.jsonl").read_text(encoding="utf-8")
    assert "auth.failed" in audit and "auth.register" in audit and "auth.login" in audit


def test_duplicates_reserved_names_and_weak_input_are_refused(tmp_path: Path) -> None:
    c = client(tmp_path)
    assert c.post("/api/auth/register", json=GOOD).status_code == 200
    assert c.post("/api/auth/register", json={**GOOD, "name": "Someone"}).status_code == 409  # same email
    assert c.post("/api/auth/register", json={**GOOD, "email": "x@example.com"}).status_code == 200  # same full name
    for bad in ({"name": "agent"}, {"name": "ab"}, {"email": "not-an-email"}, {"password": "short"}):
        got = c.post("/api/auth/register", json={**GOOD, "name": "Other Person", "email": "o@example.com", **bad})
        assert got.status_code == 400, bad


def test_the_password_is_never_stored_or_logged(tmp_path: Path) -> None:
    c = client(tmp_path)
    c.post("/api/auth/register", json=GOOD)
    c.post("/api/auth/login", json={"login": "priya@example.com", "password": "wrong password!"})
    text = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "state" / "auth").glob("*") if p.is_file())
    assert GOOD["password"] not in text and "wrong password!" not in text
    stored = json.loads((tmp_path / "state" / "auth" / "accounts.json").read_text(encoding="utf-8"))
    assert set(stored["priya@example.com"]) == {"name", "email", "salt", "hash", "created"}


def test_too_many_wrong_attempts_wait_and_an_expired_session_is_refused(tmp_path: Path) -> None:
    a = Accounts(tmp_path / "auth")
    token = a.register("Priya Rao", "p@example.com", "correct horse 1")
    assert a.name_of(token) == "Priya Rao" and a.name_of("nope") == ""
    for _ in range(5):
        with pytest.raises(AuthError):
            a.login("p@example.com", "wrong wrong")
    with pytest.raises(AuthError) as waited:
        a.login("p@example.com", "correct horse 1")
    assert waited.value.status == 429
    sessions = json.loads((tmp_path / "auth" / "sessions.json").read_text(encoding="utf-8"))
    for v in sessions.values():
        v["expires"] = 1.0
    (tmp_path / "auth" / "sessions.json").write_text(json.dumps(sessions), encoding="utf-8")
    assert a.name_of(token) == ""


def test_without_the_switch_the_old_header_path_still_works(tmp_path: Path) -> None:
    c = client(tmp_path, login=False)
    assert c.get("/api/console/state").status_code == 200
    assert c.get("/api/auth/me").json()["login_required"] is False


def test_the_audit_log_names_the_person_with_their_email(tmp_path: Path) -> None:
    c = client(tmp_path)
    c.post("/api/auth/register", json=GOOD)
    c.post("/api/console/act", json={"action": "prov|paid", "text": ""})
    audit = c.get("/api/console/state").json()["audit"]
    assert audit[-1]["who"] == "Priya Rao <priya@example.com>"  # the new row names the person by their email


def _code_from(capsys: pytest.CaptureFixture[str]) -> str:
    """The reset code is in the server log (no SMTP in tests); the log writes JSON lines to stdout."""
    import re

    return re.findall(r"reset code for .*?: (\d{8})", capsys.readouterr().out)[-1]


def test_forgot_password_resets_with_a_one_time_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    c = client(tmp_path)
    c.post("/api/auth/register", json=GOOD)
    other = client(tmp_path)  # same state dir: a second browser with its own session
    other.post("/api/auth/login", json={"login": GOOD["email"], "password": GOOD["password"]})
    # the same words for a known and an unknown email
    unknown = c.post("/api/auth/forgot", json={"email": "nobody@example.com"}).json()
    known = c.post("/api/auth/forgot", json={"email": GOOD["email"]}).json()
    assert known == unknown
    code = _code_from(capsys)
    assert c.post("/api/auth/reset", json={"email": GOOD["email"], "code": "00000000", "password": "brand new pw 2"}).status_code == 400
    assert c.post("/api/auth/reset", json={"email": GOOD["email"], "code": code, "password": "short"}).status_code == 400
    assert c.post("/api/auth/reset", json={"email": GOOD["email"], "code": code, "password": "brand new pw 2"}).status_code == 200
    # the code works once, the old password is dead, old sessions are signed out, the new one signs in
    assert c.post("/api/auth/reset", json={"email": GOOD["email"], "code": code, "password": "again again 3"}).status_code == 400
    assert c.post("/api/auth/login", json={"login": GOOD["email"], "password": GOOD["password"]}).status_code == 401
    assert other.get("/api/console/state").status_code == 401
    assert c.post("/api/auth/login", json={"login": GOOD["email"], "password": "brand new pw 2"}).status_code == 200


def test_reset_code_is_burned_after_too_many_wrong_guesses(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    c = client(tmp_path)
    c.post("/api/auth/register", json=GOOD)
    c.post("/api/auth/forgot", json={"email": GOOD["email"]})
    code = _code_from(capsys)
    for _ in range(5):
        c.post("/api/auth/reset", json={"email": GOOD["email"], "code": "11111111", "password": "brand new pw 2"})
    assert c.post("/api/auth/reset", json={"email": GOOD["email"], "code": code, "password": "brand new pw 2"}).status_code == 400
