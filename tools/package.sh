#!/usr/bin/env bash
# Build code.zip from the repository root and verify it is safe to submit.
#
# Includes : README.md, requirements.txt and code/ (code/evaluation/*.md, custom cases, any multimodal_manual fallback)
# Excludes : dataset/, .env, .venv, __pycache__, code/.cache/, *.zip, .git, log.txt, output.csv
# Fails (non-zero) if the finished zip contains dataset/, a .env file, nested archives,
# log.txt / output.csv, or any string matching an API-key / token pattern (sk-ant-, AIza..., ghp_).
#
# Usage: bash tools/package.sh                  build and verify code.zip
#        bash tools/package.sh --verify-only X  verify an existing zip X (no build)
set -euo pipefail
cd "$(dirname "$0")/.."

verify_zip() {
  local zip_path="$1" bad=0 tmp listing
  # Read the listing ONCE. Piping `unzip` into `grep -q` under `set -o pipefail` is flaky:
  # grep exits at the first match, unzip gets SIGPIPE and the pipeline reports failure.
  listing="$(unzip -Z1 "$zip_path")"
  echo "---- contents of ${zip_path} ----"
  sort <<<"$listing"
  echo "---- checks ----"
  if grep -E '(^|/)dataset(/|$)' <<<"$listing"; then
    echo "FAIL: dataset/ is inside the zip"; bad=1; fi
  if grep -E '(^|/)\.env(\..*)?$' <<<"$listing"; then
    echo "FAIL: a .env file is inside the zip"; bad=1; fi
  if grep -Ei '\.(zip|tar|gz|tgz|7z|rar|bundle)$' <<<"$listing"; then
    echo "FAIL: nested archive inside the zip"; bad=1; fi
  if grep -E '(^|/)(log\.txt|output\.csv)$' <<<"$listing"; then
    echo "FAIL: log.txt or output.csv inside the zip (they are submitted separately)"; bad=1; fi
  if grep -E '(^|/)(__pycache__|\.cache|\.venv|venv|\.git)(/|$)' <<<"$listing"; then
    echo "FAIL: cache / venv / git directory inside the zip"; bad=1; fi
  tmp="$(mktemp -d)"
  unzip -q "$zip_path" -d "$tmp"
  # API-key / token patterns (Anthropic keys, Google/Gemini keys, GitHub tokens).
  if grep -rIE 'sk-ant-[A-Za-z0-9_-]{10,}|AIza[0-9A-Za-z_-]{35}|AQ\.[A-Za-z0-9_-]{30,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}' "$tmp" >/dev/null 2>&1; then
    echo "FAIL: a string matching an API-key / token pattern is inside the zip:"
    grep -rIlE 'sk-ant-[A-Za-z0-9_-]{10,}|AIza[0-9A-Za-z_-]{35}|AQ\.[A-Za-z0-9_-]{30,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}' "$tmp" | sed "s|$tmp/||"
    bad=1
  fi
  # Literal values from a local .env (any key format) must not appear in the zip. Only file names are printed.
  if [ -f .env ]; then
    while IFS='=' read -r k v; do
      v="${v%$'\r'}"; v="${v#[\"\']}"; v="${v%[\"\']}"
      [ "${#v}" -ge 8 ] || continue
      case "$k" in \#*|"") continue;; esac
      if grep -rIlF -- "$v" "$tmp" >/dev/null 2>&1; then
        echo "FAIL: the value of $k from .env appears in the zip, in:"
        grep -rIlF -- "$v" "$tmp" | sed "s|$tmp/||"
        bad=1
      fi
    done < .env
  fi
  rm -rf "$tmp"
  if ! grep -qx 'README.md' <<<"$listing"; then echo "FAIL: README.md missing from zip root"; bad=1; fi
  if ! grep -qx 'requirements.txt' <<<"$listing"; then echo "FAIL: requirements.txt missing"; bad=1; fi
  if ! grep -qx 'code/main.py' <<<"$listing"; then echo "FAIL: code/main.py missing"; bad=1; fi
  for f in code/evaluation/evaluation_report.md code/evaluation/usage_report.md; do
    grep -qx "$f" <<<"$listing" || { echo "FAIL: $f missing"; bad=1; }
  done
  if [ "$bad" -ne 0 ]; then echo "PACKAGE CHECK FAILED"; return 1; fi
  echo "all checks passed"
}

if [ "${1:-}" = "--verify-only" ]; then
  verify_zip "${2:?usage: package.sh --verify-only ZIP}"; exit $?
fi

rm -f code.zip
find code -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
zip -rq code.zip README.md requirements.txt code \
  -x 'dataset/*' '*/dataset/*' '.env' '*/.env' '.env.*' '*/.env.*' \
     '*/.venv/*' '*/venv/*' '*/node_modules/*' '*/__pycache__/*' '*.pyc' \
     'code/.cache/*' '*/.cache/*' '*.zip' '*.bundle' '.git/*' '*/.git/*' \
     'log.txt' 'output.csv' '*/.DS_Store'
verify_zip code.zip
echo "---- size ----"
ls -l code.zip | awk '{printf "%s bytes", $5}'; du -h code.zip | awk '{print " (" $1 ")"}'
