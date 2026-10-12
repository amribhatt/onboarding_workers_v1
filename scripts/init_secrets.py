"""Write ./secrets/*.txt for docker compose: random passwords and the two database URLs built from them.

Existing files are never overwritten (a rotated password is a deliberate act: delete the file, run the migrate
job again, restart). Run once:  python scripts/init_secrets.py
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path
from urllib.parse import quote

DIR = Path(__file__).resolve().parents[1] / "secrets"


def main() -> int:
    DIR.mkdir(exist_ok=True)
    owner_pw, app_pw = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    files = {
        "pg_owner_password.txt": owner_pw,
        "app_password.txt": app_pw,
        "owner_database_url.txt": f"postgresql+psycopg://ow_owner:{quote(owner_pw, safe='')}@postgres:5432/onboarding",
        "app_database_url.txt": f"postgresql+psycopg://ow_app:{quote(app_pw, safe='')}@postgres:5432/onboarding",
    }
    existing = [name for name in files if (DIR / name).exists()]
    if existing:
        print(f"Not overwriting: {', '.join(existing)}. Delete them first to start again.", file=sys.stderr)
        return 1
    for name, value in files.items():
        path = DIR / name
        path.write_text(value + "\n", encoding="utf-8")
        path.chmod(0o400)
    print(f"Wrote {len(files)} files to {DIR}. They are git-ignored; keep them out of backups you share.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
