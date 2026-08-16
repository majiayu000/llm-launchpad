#!/bin/zsh
set -euo pipefail

if ! command -v claude >/dev/null 2>&1; then
  echo "未找到 claude 命令，请先安装 Claude Code。" >&2
  exit 1
fi

CLAUDE_STATE_DIR="$HOME/.local/share/qwen38-ollama/claude"
mkdir -p "$CLAUDE_STATE_DIR"

# Claude Code 2.1.233 会从 ~/.claude/settings.json 重新注入 env。
# 使用独立配置目录，避免全局智谱/Anthropic配置覆盖本地 Ollama。
export CLAUDE_CONFIG_DIR="$CLAUDE_STATE_DIR"
export ANTHROPIC_BASE_URL="http://127.0.0.1:11440"
export ANTHROPIC_AUTH_TOKEN="ollama"
export ANTHROPIC_API_KEY=""
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1"
export CLAUDE_CODE_MAX_RETRIES="${CLAUDE_CODE_MAX_RETRIES:-1}"
export CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS="${CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS:-2500}"
export API_TIMEOUT_MS="${API_TIMEOUT_MS:-900000}"
export CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS="${CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS:-900000}"
export CLAUDE_STREAM_IDLE_TIMEOUT_MS="${CLAUDE_STREAM_IDLE_TIMEOUT_MS:-900000}"
# Claude Code 不认识本地模型名，默认按 200k 窗口做自动压缩；实际服务是 64k，必须显式声明
export CLAUDE_CODE_MAX_CONTEXT_TOKENS="${CLAUDE_CODE_MAX_CONTEXT_TOKENS:-65536}"

LOCAL_SYSTEM_PROMPT="You are a local coding agent. The current working directory is: $PWD. Use absolute paths under this directory when calling file tools. For large files, first use rg or Read with offset/limit and inspect only relevant ranges; do not re-read saved full tool outputs. Use the provided tools when needed and follow the user's request exactly."

exec claude \
  --model qwen3.8:27b \
  --tools "Bash,Edit,Read,Write" \
  --system-prompt "$LOCAL_SYSTEM_PROMPT" \
  "$@"
