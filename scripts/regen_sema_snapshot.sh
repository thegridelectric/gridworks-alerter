#!/usr/bin/env bash
#
# regen_sema_snapshot.sh — regenerate gwalerter's vendored Sema snapshot
# (src/gwalerter/sema) from the canonical sema repo. The seed it consumes is
# src/gwalerter/sema_seed_request.yaml.
#
# The vendored tree is GENERATED — never hand-edit it; edit the seed (or the
# sema definitions) and re-run.
#
# The sema CLI refuses to run from a dirty sema checkout and writes only
# under <sema>/output, so the snapshot is reproducible from the sema commit
# the checkout sits on — check out the ref you intend to ship from first.
#
# Usage:
#   scripts/regen_sema_snapshot.sh                 # sibling ../sema checkout
#   SEMA_REPO=/path/to/sema scripts/regen_sema_snapshot.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SEMA_REPO="${SEMA_REPO:-$(cd "${REPO_ROOT}/../sema" 2>/dev/null && pwd || true)}"

PACKAGE_NAME="gwalerter"
SEED="${REPO_ROOT}/src/gwalerter/sema_seed_request.yaml"
VENDOR_DIR="${REPO_ROOT}/src/gwalerter/sema"

if [[ -z "${SEMA_REPO}" || ! -d "${SEMA_REPO}" ]]; then
  echo "error: sema repo not found; set SEMA_REPO=/path/to/sema (default: sibling ../sema)." >&2
  exit 1
fi

echo "==> sema repo: ${SEMA_REPO} @ $(git -C "${SEMA_REPO}" rev-parse --short HEAD)"
echo "==> seed:      ${SEED}"
echo "==> package:   ${PACKAGE_NAME}"

cd "${SEMA_REPO}"
# --allow-staged: the house alert words are staging during the dev-broker
# phase; drop the flag when they are promoted before the shadow run.
uv run sema snapshot prepare --allow-staged "${SEED}"
uv run sema snapshot build --package-name "${PACKAGE_NAME}"

echo "==> mirror ${SEMA_REPO}/output/sema -> ${VENDOR_DIR}"
rsync -a --delete --exclude='__pycache__' "${SEMA_REPO}/output/sema/" "${VENDOR_DIR}/"

echo "==> done. review the diff (git status) and run ./ci.sh."
