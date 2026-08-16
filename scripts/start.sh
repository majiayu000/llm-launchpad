#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${SCRIPT_DIR:h}"
OLLAMA_LABEL="com.local.qwen38-ollama"
COMPAT_LABEL="com.local.qwen38-ollama-compat"
DOMAIN="gui/$(id -u)"
STATE_DIR="$HOME/.local/share/qwen38-ollama"
LOG_DIR="$HOME/Library/Logs/Qwen3.8-Ollama"

# 优先使用 install.sh 记录的绝对路径，launchd 不继承当前 shell 的 PATH
if [[ -f "$STATE_DIR/env.sh" ]]; then
  source "$STATE_DIR/env.sh"
fi
OLLAMA_BIN="${QWEN38_OLLAMA_BIN:-$(command -v ollama || true)}"
PYTHON_BIN="${QWEN38_PYTHON_BIN:-$(command -v python3 || true)}"
if [[ -z "$OLLAMA_BIN" ]]; then
  echo "未找到 ollama，请先运行 ./install.sh" >&2
  exit 1
fi
if [[ -z "$PYTHON_BIN" ]]; then
  echo "未找到 python3，请先运行 ./install.sh" >&2
  exit 1
fi

mkdir -p "$STATE_DIR/models" "$STATE_DIR/compat" "$LOG_DIR" "$HOME/Library/LaunchAgents"
cp "$PROJECT_ROOT/compat/anthropic_proxy.py" "$STATE_DIR/compat/anthropic_proxy.py"

reload_agent() {
  local label="$1"
  local source_plist="$PROJECT_ROOT/launchd/$label.plist"
  local installed_plist="$HOME/Library/LaunchAgents/$label.plist"

  if [[ -L "$installed_plist" ]]; then
    unlink "$installed_plist"
  fi
  sed \
    -e "s|__OLLAMA_BIN__|$OLLAMA_BIN|g" \
    -e "s|__PYTHON_BIN__|$PYTHON_BIN|g" \
    -e "s|__STATE_DIR__|$STATE_DIR|g" \
    -e "s|__LOG_DIR__|$LOG_DIR|g" \
    "$source_plist" > "$installed_plist"
  if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  fi
  # bootout 返回时旧服务可能尚未完全注销，立即 bootstrap 会报 error 5，需重试
  local attempt
  for attempt in {1..15}; do
    if launchctl bootstrap "$DOMAIN" "$installed_plist" 2>/dev/null; then
      return 0
    fi
    sleep 2
  done
  echo "launchd 服务 $label 启动失败，请检查 $LOG_DIR 下的日志" >&2
  return 1
}

reload_agent "$OLLAMA_LABEL"

for attempt in {1..60}; do
  if version=$(curl -fsS http://127.0.0.1:11439/api/version 2>/dev/null); then
    break
  fi
  sleep 1
done

if ! curl -fsS http://127.0.0.1:11439/api/version >/dev/null 2>&1; then
  echo "Ollama 启动超时，请检查 $LOG_DIR/server.stderr.log" >&2
  exit 1
fi

reload_agent "$COMPAT_LABEL"

for attempt in {1..30}; do
  if curl -fsS http://127.0.0.1:11440/api/version >/dev/null 2>&1; then
    echo "Qwen3.8 Ollama 独立服务已启动：$version"
    echo "原生 API: http://127.0.0.1:11439"
    echo "Claude Code API: http://127.0.0.1:11440"
    exit 0
  fi
  sleep 1
done

echo "Claude 兼容入口启动超时，请检查 $LOG_DIR/compat.stderr.log" >&2
exit 1
