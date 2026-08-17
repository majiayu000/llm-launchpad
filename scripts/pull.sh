#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
STATE_DIR="$HOME/.local/share/qwen38-ollama"

if [[ -f "$STATE_DIR/env.sh" ]]; then
  source "$STATE_DIR/env.sh"
fi
OLLAMA_BIN="${QWEN38_OLLAMA_BIN:-$(command -v ollama || true)}"
if [[ -z "$OLLAMA_BIN" ]]; then
  echo "未找到 ollama，请先运行 ./install.sh" >&2
  exit 1
fi

"$SCRIPT_DIR/start.sh"

MODEL="${QWEN38_MODEL:-qwen3.8:27b-mlx}"
echo "开始下载 $MODEL；首次约 18GB，支持断点续传。"
OLLAMA_HOST=127.0.0.1:11439 "$OLLAMA_BIN" pull "$MODEL"
