#!/bin/bash
# Run the Home-Assistant-fixture suite in a throwaway container. Heavier than
# tests/run.sh (it installs Home Assistant), so it's kept separate. The api is
# mocked and a socket guard blocks *.nanit.com — no real device is touched.
#
#   ./tests_ha/run.sh             # run everything
#   ./tests_ha/run.sh -k pin      # pass extra args through to pytest
set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if command -v podman >/dev/null 2>&1; then
    RUNNER="podman"
elif command -v docker >/dev/null 2>&1; then
    RUNNER="docker"
else
    echo "Need podman or docker to run the containerized tests." >&2
    exit 1
fi

# python:3.13 (not -slim) so HA's deps with C extensions have a compiler available.
exec "$RUNNER" run --rm -v "$REPO_ROOT":/app:z -w /app docker.io/library/python:3.13 \
    bash -c "pip install -q -r requirements-test-ha.txt && python -m pytest tests_ha \"\$@\"" -- "$@"
