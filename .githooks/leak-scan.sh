#!/usr/bin/env bash
# Pre-commit leak scan for this PUBLIC repo.
#
# Blocks a commit whose staged content contains private strings — personal
# names, internal hostnames/domains, the private infrastructure repo this
# integration is developed alongside, or other unrelated private projects.
#
# The actual sensitive terms live ONLY in a gitignored local denylist
# (.leak-denylist.local) so they never themselves land in this public repo.
# Copy .leak-denylist.local.example to .leak-denylist.local to enable; without
# it, only a couple of universal secret patterns are checked (and a warning is
# printed).
#
# Bypass a false positive with:  LEAK_SCAN_SKIP=1 git commit ...
set -euo pipefail

[ "${LEAK_SCAN_SKIP:-}" = "1" ] && exit 0

repo_root="$(git rev-parse --show-toplevel)"
denylist="$repo_root/.leak-denylist.local"

# Universal secret patterns — safe to hardcode (they don't match their own text).
patterns=(
  'AKIA[0-9A-Z]{16}'
  'BEGIN [A-Z ]*PRIVATE KEY'
)

if [ -f "$denylist" ]; then
  while IFS= read -r line; do
    line="${line%%#*}"
    line="$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [ -n "$line" ] && patterns+=("$line")
  done <"$denylist"
else
  echo "leak-scan: WARNING - no .leak-denylist.local found; personal/private-term" >&2
  echo "           scanning is OFF (copy .leak-denylist.local.example to enable)." >&2
fi

fail=0
while IFS= read -r f; do
  # The hook scripts and the example denylist legitimately describe patterns.
  case "$f" in
    .githooks/* | .leak-denylist.local.example) continue ;;
  esac
  # Skip binary blobs.
  git show ":$f" 2>/dev/null | grep -Iq . || continue
  content="$(git show ":$f" 2>/dev/null)"
  for p in "${patterns[@]}"; do
    if printf '%s' "$content" | grep -nEi -- "$p" >/dev/null 2>&1; then
      echo "leak-scan: BLOCKED - '$f' matches forbidden pattern /$p/i:" >&2
      printf '%s' "$content" | grep -nEi -- "$p" | sed "s|^|    $f:|" >&2
      fail=1
    fi
  done
done < <(git diff --cached --name-only --diff-filter=ACM)

if [ "$fail" -ne 0 ]; then
  echo "" >&2
  echo "leak-scan: commit blocked - this is a PUBLIC repo. Scrub the matches above," >&2
  echo "           or 'LEAK_SCAN_SKIP=1 git commit ...' to override (rarely correct)." >&2
  exit 1
fi
exit 0
