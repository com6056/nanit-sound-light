#!/bin/bash
# Run the test suite in a throwaway Python container — no local installs, and the
# block_nanit_network guard means it can never touch the real device.
#
#   ./tests/run.sh            # run everything
#   ./tests/run.sh -k color   # pass extra args through to pytest
set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Prefer podman (matches the host); fall back to docker.
if command -v podman >/dev/null 2>&1; then
    RUNNER="podman"
elif command -v docker >/dev/null 2>&1; then
    RUNNER="docker"
else
    echo "Need podman or docker to run the containerized tests." >&2
    exit 1
fi

exec "$RUNNER" run --rm -v "$REPO_ROOT":/app:z -w /app docker.io/library/python:3.13-slim \
    bash -c "pip install -q -r requirements-test.txt && python -m pytest \"\$@\"" -- "$@"
