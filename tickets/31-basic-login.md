# 31 A basic login: register once, sign in, and the name is yours

Why: every decision (ID logic, approve, answer a question) needs a named person, and today the name is typed into a
box (`X-Reviewer`, self-declared). A user should register once and sign in; the console then knows who they are and
never asks. It stays basic: this is not SSO and not production authentication.

Depends on: 30 (clean tree first). Interim in place: `console_screens.signed_in()` offers the machine's account name.

Do:
- `/register` (full name, email, password) and `/login` (email, password), one small page each, served
  by `web.py`; `/logout`. User names and emails are unique; reserved and agent names are refused
  (`contracts.approver_name`). Email is checked for shape only and is not verified.
- Accounts file in the state dir: user name, email, salted `hashlib.scrypt` hash, created-at. Never the password,
  never in a log.
- A session cookie (random token, HttpOnly, SameSite=Lax, server-side table with an expiry). No cookie, no console:
  `/console` redirects to `/login`.
- The actor of every gate is the session's name, read on the server. `X-Reviewer` is ignored for a signed-in session;
  it is accepted only when the server runs with `WORKER_DEMO=1` (existing tests keep working through that switch).
- The console shows the signed-in name once (top of the sidebar) with a Sign out button. The "Deciding as" and
  "Approving as" boxes go away.
- The audit record carries the name as before; one `auth.register` / `auth.login` / `auth.logout` row each.
- A few failed sign-ins in a row wait a little before the next try (plain counter in the session table).

Tests: register (name, email, password), sign in by email or name, wrong password refused and audited, duplicate name or email and reserved names refused, a malformed email refused, no cookie means
redirect, a forged `X-Reviewer` does not change the actor of a signed-in session, expired session refused, password
never appears in the audit or the logs, the demo switch keeps the old header path.

Acceptance: green tests (`> .logs/t31.log`), ruff clean, STATUS.md updated, one commit.
