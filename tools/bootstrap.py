"""Stdlib-only bootstrap used by start_windows.bat / start_unix.sh (it runs BEFORE dependencies exist).

    python tools/bootstrap.py env    create .env from .env.example and make sure a local SECRET_KEY exists
    python tools/bootstrap.py deps   install requirements*.txt into the active environment when they changed
    python tools/bootstrap.py        both
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 8):
    sys.exit("ScholarBot needs Python 3.8 or newer (found %d.%d). Install a newer Python and run the start script again." % sys.version_info[:2])

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = Path(os.environ.get("SCHOLARBOT_ENV_FILE", ROOT / ".env"))


def ensure_env() -> None:
    example = ROOT / ".env.example"
    if not ENV_FILE.exists():
        if example.exists():
            shutil.copy(example, ENV_FILE)
        else:
            ENV_FILE.write_text("SECRET_KEY=\n", encoding="utf-8")
        print(f"Created {ENV_FILE.name} from .env.example")
    text = ENV_FILE.read_text(encoding="utf-8")
    match = re.search(r"^SECRET_KEY=(.*)$", text, re.M)
    current = (match.group(1).strip().strip("'\"") if match else "")
    if not current:
        # Only the session-signing key is generated (locally, never transmitted). Tokens/API keys/passwords are never invented.
        line = f'SECRET_KEY="{secrets.token_urlsafe(48)}"'
        text = re.sub(r"^SECRET_KEY=.*$", lambda _m: line, text, count=1, flags=re.M) if match else text.rstrip("\n") + "\n" + line + "\n"
        ENV_FILE.write_text(text, encoding="utf-8")
        print("Generated a random local SECRET_KEY (signs dashboard session cookies only).")
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass  # not supported on this platform


def _database_url() -> str:
    try:
        m = re.search(r"^DATABASE_URL=(.*)$", ENV_FILE.read_text(encoding="utf-8"), re.M)
    except OSError:
        return ""
    return (m.group(1).strip().strip("'\"") if m else "") or os.environ.get("DATABASE_URL", "")


def ensure_deps() -> None:
    files = [ROOT / "requirements.txt"]
    if _database_url().startswith(("postgres://", "postgresql")):
        files.append(ROOT / "requirements-postgres.txt")
    digest = hashlib.sha256(b"".join(f.read_bytes() for f in files) + sys.version.encode()).hexdigest()
    stamp = Path(sys.prefix) / ".scholarbot_deps"
    if stamp.exists() and stamp.read_text().strip() == digest:
        print("Dependencies are up to date.")
        return
    print("Installing dependencies (first run can take a minute)...")
    for f in files:
        code = subprocess.call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r", str(f)])
        if code != 0:
            print(f"\nERROR: installing {f.name} failed. Check your internet connection / proxy settings and run the script again.", file=sys.stderr)
            sys.exit(code)
    stamp.write_text(digest)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("env", "all"):
        ensure_env()
    if what in ("deps", "all"):
        ensure_deps()
