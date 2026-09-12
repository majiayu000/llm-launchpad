#!/bin/zsh
# 实时速度计：显示 Claude Code 流经兼容层的生成速度（tok/s）
# 用法：./scripts/meter.sh    （另一个窗口跑 ./scripts/claude-code.sh）
set -euo pipefail

# Use default only when unset; empty QWEN38_METER_FILE disables (matches meter.py).
METER_FILE="${QWEN38_METER_FILE-$HOME/.local/share/qwen38-ollama/meter.json}"
if [[ -z "$METER_FILE" ]]; then
  echo "速度计已关闭（QWEN38_METER_FILE 为空）" >&2
  exit 0
fi

if [[ ! -f "$METER_FILE" ]]; then
  echo "暂无数据（状态文件 $METER_FILE 还没生成，先在别的窗口跑一次 ./scripts/claude-code.sh）" >&2
fi

METER_FILE="$METER_FILE" python3 <<'PY'
import json
import os
import time

PATH = os.environ["METER_FILE"]
PHASES = {"prefill": "预填充中", "thinking": "思考中 ▍", "text": "生成中 ▍", "done": "完成"}


def read_state():
    try:
        with open(PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def fmt_last(state):
    last = (state or {}).get("last_completed")
    if not last:
        return "暂无"
    tok_s = last.get("tok_s")
    speed = f"{tok_s} tok/s" if tok_s is not None else "—"
    return f"{speed} · {last.get('output_tokens', 0)} tok · {last.get('duration_s', 0)}s"


def render(state):
    now = time.time()
    lines = ["⚡ qwen3.8 · Claude Code 实时速度", ""]
    fresh = state is not None and now - state.get("t_update", 0) < 3
    if fresh:
        phase = PHASES.get(state.get("phase"), str(state.get("phase")))
        tokens = state.get("output_tokens", 0)
        first = state.get("t_first_token")
        if first and state.get("phase") in ("thinking", "text"):
            tok_s = tokens / max(now - first, 1e-9)
            lines.append(f"状态    {phase}")
            lines.append(f"token   {tokens}")
            lines.append(f"速度    {tok_s:5.1f} tok/s")
        else:
            lines.append(f"状态    {phase}")
            lines.append(f"token   {tokens}")
        lines.append(f"输入    {state.get('input_tokens', 0):,} tok")
    else:
        lines.append("状态    空闲")
    lines.append("")
    lines.append(f"上一轮  {fmt_last(state)}")
    print("\033[2J\033[H" + "\n".join(lines), flush=True)


try:
    while True:
        render(read_state())
        time.sleep(0.2)
except KeyboardInterrupt:
    pass
PY
