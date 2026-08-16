#!/bin/zsh
# 卸载 Qwen3.8-27B 本地服务
#
# 用法：
#   ./uninstall.sh          停止服务并移除 launchd 配置；模型数据默认保留，交互确认后才删除
#   ./uninstall.sh --purge  同上，并直接删除全部模型数据（约 17GB），不再询问
set -euo pipefail

DOMAIN="gui/$(id -u)"
STATE_DIR="$HOME/.local/share/qwen38-ollama"
LOG_DIR="$HOME/Library/Logs/Qwen3.8-Ollama"

PURGE=0
case "${1:-}" in
  --purge) PURGE=1 ;;
  "") ;;
  -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "未知参数：$1（支持 --purge）" >&2; exit 2 ;;
esac

for label in com.local.qwen38-ollama-compat com.local.qwen38-ollama; do
  if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$label"
    echo "已停止 $label"
  fi
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done
echo "服务已停止，launchd 配置已移除。"

if (( PURGE )); then
  DELETE_DATA=1
elif [[ -d "$STATE_DIR" ]]; then
  SIZE_GB=$(( $(du -sk "$STATE_DIR" 2>/dev/null | awk '{print $1}') / 1048576 ))
  DELETE_DATA=0
  if [[ -t 0 ]]; then
    read "reply?是否同时删除模型数据（约 ${SIZE_GB}GB，路径 $STATE_DIR）？[y/N] "
    [[ "$reply" == y || "$reply" == Y ]] && DELETE_DATA=1
  fi
fi

if (( ${DELETE_DATA:-0} )); then
  rm -rf "$STATE_DIR" "$LOG_DIR"
  echo "模型数据与日志已删除：$STATE_DIR、$LOG_DIR"
else
  echo "模型数据保留在 $STATE_DIR；重新运行 ./install.sh 可恢复服务。"
  echo "如需彻底删除：./uninstall.sh --purge"
fi
