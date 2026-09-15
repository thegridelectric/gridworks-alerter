#!/bin/bash
# Run locally everything CI runs, so a push won't go red. Mirrors
# .github/workflows/tests.yml (lint + tests jobs). Usage: ./ci.sh
set -euo pipefail

step() { printf '\n=== %s ===\n' "$1"; shift; "$@"; }

step "uv sync (locked)" uv sync --all-groups --locked

# Directory-form lint first: `pre-commit` only sees git-tracked files, the
# directory form sees a brand-new untracked file too.
step "ruff check" uv run ruff check --no-fix .
step "ruff format --check" uv run ruff format --check .
step "pyright" uv run pyright

# The broker tests need the gwbase dev broker (gw-dev-rabbit, started from
# gridworks-base with ./arm.sh or ./x86.sh); without one they self-skip.
if nc -z localhost 5672 2>/dev/null; then
  step "tests (with broker)" uv run pytest -q
else
  echo "NOTE: no broker on localhost:5672 — broker tests will self-skip."
  step "tests (no broker)" uv run pytest -q
fi

printf '\nAll CI checks passed.\n'
