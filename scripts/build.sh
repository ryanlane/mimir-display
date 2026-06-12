#!/usr/bin/env bash
set -euo pipefail

# Build wheel + sdist and emit SHA256 sums.

echo "[build] Ensuring build backend tooling"
python3 -m pip install --upgrade build pip wheel > /dev/null

echo "[build] Cleaning old dist/"
rm -rf dist

python3 -m build

if command -v sha256sum >/dev/null 2>&1; then
  echo "[build] Generating SHA256SUMS"
  (cd dist && sha256sum * > SHA256SUMS)
fi

echo "[build] Artifacts:"
ls -1 dist

echo "[build] Done"
