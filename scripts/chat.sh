#!/bin/zsh
set -euo pipefail

if (( $# == 0 )); then
  echo "用法：./scripts/chat.sh '你的问题'" >&2
  exit 2
fi

MODEL="${QWEN38_MODEL:-qwen3.8:27b-mlx}"

PAYLOAD=$(python3 - "$*" "$MODEL" <<'PY'
import json
import sys

print(json.dumps({
    "model": sys.argv[2],
    "messages": [{"role": "user", "content": sys.argv[1]}],
    "stream": False,
    "max_tokens": 2048,
    "reasoning_effort": "medium",
}, ensure_ascii=False))
PY
)

RESULT=$(curl -fsS --max-time 1800 http://127.0.0.1:11439/v1/chat/completions \
  -H 'Content-Type: application/json' \
  --data "$PAYLOAD")

python3 - "$RESULT" <<'PY'
import json
import sys

result = json.loads(sys.argv[1])
message = result["choices"][0]["message"]
print(message.get("content") or "")
usage = result.get("usage") or {}
print(
    f"\n[用量] prompt={usage.get('prompt_tokens', 0)}, "
    f"completion={usage.get('completion_tokens', 0)}",
    file=sys.stderr,
)
PY
