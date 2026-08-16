#!/bin/zsh
set -euo pipefail

DOMAIN="gui/$(id -u)"
STOPPED=0

for label in com.local.qwen38-ollama-compat com.local.qwen38-ollama; do
  if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$label"
    STOPPED=1
  fi
done

if (( STOPPED )); then
  echo "Qwen3.8 Ollama 独立服务已停止；模型文件保留。"
else
  echo "Qwen3.8 Ollama 独立服务当前未运行。"
fi
