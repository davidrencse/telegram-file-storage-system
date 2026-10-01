#!/usr/bin/env bash
# Local validation pipeline. Stages run in order; the first failure stops the pipeline.
#   scripts/validate.sh            # lint + all offline tests + hook smoke + real-transcript dry audit
#   scripts/validate.sh --live     # also run the live test against the real `claude` CLI (costs ≤ $1)
set -euo pipefail
cd "$(dirname "$0")/.."

LIVE=0
[[ "${1:-}" == "--live" ]] && LIVE=1
PATHS=(src tests session_auditor)
stage() { printf '\n\033[1;34m▶ %s\033[0m\n' "$*"; }
ok() { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }

stage "1/7 lint (ruff check + format --check)"
uv run --frozen ruff check "${PATHS[@]}"
uv run --frozen ruff format --check "${PATHS[@]}"
ok lint

stage "2/7 tgfs unit, FUSE-op, web API and CLI tests"
uv run --frozen pytest -q tests/test_storage.py tests/test_fusefs.py tests/test_web.py tests/test_cli.py tests/test_system.py
ok tgfs

stage "3/7 auditor unit tests (transcript, checks, git, validate, agent, hook)"
uv run --frozen pytest -q tests/session_auditor -m "not e2e and not live"
ok "auditor unit"

stage "4/7 auditor end-to-end pipeline (real git + gates + subprocess agent)"
uv run --frozen pytest -q tests/session_auditor -m e2e
ok "auditor e2e"

stage "5/7 hook runs under system python ≥ 3.12 (what Claude Code invokes)"
python3 -c 'import sys; assert sys.version_info >= (3, 12), sys.version'
SMOKE=$(mktemp -d)
trap 'rm -rf "$SMOKE"' EXIT
git -C "$SMOKE" init -q && echo "x" > "$SMOKE/a.txt"
echo "{\"hook_event_name\":\"SessionStart\",\"session_id\":\"smoke\",\"cwd\":\"$SMOKE\"}" \
  | CLAUDE_PROJECT_DIR="$PWD" bash -c "$(python3 -c 'import json;print(json.load(open(".claude/settings.json"))["hooks"]["SessionStart"][0]["hooks"][0]["command"])')"
test -s "$SMOKE/.claude/audits/smoke/session.json"
ok "hook smoke"

stage "6/7 static dry-run audit of every stored real transcript (no LLM, no gates)"
LOGS="$HOME/.claude/projects/$(pwd | sed 's#/#-#g')"
shopt -s nullglob
n=0
for t in "$LOGS"/*.jsonl; do
  python3 -m session_auditor --repo "$SMOKE" audit --transcript "$t" --baseline empty --no-agent --no-gates \
    --session-id "dry-$(basename "$t" .jsonl)" >/dev/null
  n=$((n + 1))
done
ok "dry audits: $n transcripts"

if [[ $LIVE == 1 ]]; then
  stage "7/7 LIVE: headless claude critique + redo on scratch repo"
  AUDITOR_LIVE=1 uv run --frozen pytest -q -m live tests/session_auditor/test_live.py -rs
  ok live
else
  stage "7/7 live test skipped (pass --live to run; costs ≤ \$1)"
fi

printf '\n\033[1;32mALL STAGES PASSED\033[0m\n'
