#!/bin/zsh
set -euo pipefail

if ! command -v codex >/dev/null 2>&1; then
  echo "未找到 codex 命令，请先安装 Codex CLI。" >&2
  exit 1
fi

# 使用独立状态目录，避免改动用户现有的 ~/.codex 配置与会话。
export CODEX_HOME="$HOME/.local/share/qwen38-ollama/codex"
mkdir -p "$CODEX_HOME"

MODEL="${QWEN38_MODEL:-qwen3.8:27b-mlx}"

exec codex \
  --model "$MODEL" \
  --config 'model_provider="qwen38_local"' \
  --config 'model_providers.qwen38_local.name="Qwen3.8 Ollama"' \
  --config 'model_providers.qwen38_local.base_url="http://127.0.0.1:11439/v1"' \
  --config 'model_providers.qwen38_local.wire_api="responses"' \
  --config 'model_context_window=65536' \
  --config 'approval_policy="on-request"' \
  --config 'sandbox_mode="workspace-write"' \
  "$@"
