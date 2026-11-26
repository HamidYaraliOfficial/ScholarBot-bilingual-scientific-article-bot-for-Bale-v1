#!/usr/bin/env bash
# ScholarBot launcher for Linux and macOS.
#   chmod +x start_unix.sh && ./start_unix.sh
# It never needs root: everything is installed into ./.venv

# --- 1. make sure we are running under Bash (checked with POSIX syntax on purpose) ---------------
if [ -z "${BASH_VERSION:-}" ]; then
  echo "This script needs Bash. Run it with:  bash start_unix.sh" >&2
  exit 1
fi

set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8   # Persian text in logs must never break the terminal

step() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
on_error() {
  local code=$?
  printf '\nScholarBot could not start (a step failed with exit code %s).\nRead the message above, or see "Troubleshooting" in README.md.\n' "$code" >&2
  exit "$code"
}
trap on_error ERR

# --- 2/3. Python present and recent enough (3.8 or newer; any newer version works) -----------------
step "Checking Python"
MIN_MAJOR=3
MIN_MINOR=8
PY=""
for candidate in python3 python python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python3.8; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c "import sys; sys.exit(0 if sys.version_info[:2] >= ($MIN_MAJOR, $MIN_MINOR) else 1)" >/dev/null 2>&1; then
      PY="$candidate"
      break
    fi
    echo "  - $candidate is too old or unusable: $("$candidate" --version 2>&1 | head -n 1)"
  fi
done
[ -n "$PY" ] || fail "Python $MIN_MAJOR.$MIN_MINOR or newer was not found. Install it from https://www.python.org/downloads/ (macOS: 'brew install python'; Debian/Ubuntu: install the python3 and python3-venv packages)."
echo "Using $("$PY" --version 2>&1) ($(command -v "$PY"))"
if [ "${1:-}" = "--check-python" ]; then
  exit 0   # used by the automated tests: stop after the interpreter was found
fi

# --- 4/5. virtual environment (created once, reused afterwards) ---------------------------------
if [ ! -x ".venv/bin/python" ]; then
  step "Creating virtual environment (.venv)"
  "$PY" -m venv .venv || fail "Could not create a virtual environment. On Debian/Ubuntu install the python3-venv package and run this script again."
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# --- 6. pip ----------------------------------------------------------------------------------------
step "Updating pip"
python -m pip install --quiet --disable-pip-version-check --upgrade pip || echo "Warning: could not update pip (continuing)."

# --- 8. .env ------------------------------------------------------------------------------------------
if [ ! -f .env ]; then
  step "Creating .env from .env.example"
  cp .env.example .env
  chmod 600 .env
fi

# --- 7. dependencies from requirements.txt (+ local session key in .env) --------------------------
step "Installing dependencies and checking .env"
python tools/bootstrap.py

# --- 9. database migrations ----------------------------------------------------------------------------
step "Applying database migrations"
python -m app.cli migrate

# --- 10. first administrator (asks interactively only when none exists yet) ---------------------------
step "Checking administrator account"
python -m app.cli create-admin --if-none

step "Configuration summary"
python -m app.cli check
echo
echo "To enable the Bale bot, put its token in .env (BALE_BOT_TOKEN=...) or in Dashboard > Settings, then restart."

# --- 11/13. run the app; Ctrl+C stops it cleanly -------------------------------------------------------
PID=""
stop() {
  printf '\nStopping ScholarBot...\n'
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    kill -TERM "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
  echo "Stopped."
  exit 0
}
trap stop INT TERM
trap - ERR

step "Starting ScholarBot (press Ctrl+C to stop)"
python run.py &
PID=$!
code=0
wait "$PID" || code=$?
if [ "$code" -ne 0 ]; then
  printf '\nScholarBot stopped unexpectedly (exit code %s). The message above explains why; common causes: the port is already in use (change DASHBOARD_PORT in .env) or SECRET_KEY is missing.\n' "$code" >&2
fi
exit "$code"
