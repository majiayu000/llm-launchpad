#!/bin/zsh
# Qwen3.8-27B 本地服务一键安装器（幂等，可重复运行）
#
# 用法：
#   ./install.sh           完整安装：预检 → 准备 ollama → 启动服务 → 下载模型 → 冒烟测试
#   ./install.sh --check   只做环境预检，不安装、不改动任何东西
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
STATE_DIR="$HOME/.local/share/qwen38-ollama"
LOG_DIR="$HOME/Library/Logs/Qwen3.8-Ollama"
API="http://127.0.0.1:11439"
MODEL="qwen3.8:27b"

MODEL_MIN_DISK_GB=40   # 模型约 17GB + 运行余量
RUNTIME_MIN_DISK_GB=5  # 模型已装时只需少量余量
MIN_MEM_GB=16          # 低于此值直接拒绝（27B Q4 约需 20GB 空闲内存）
OK_MEM_GB=24           # 达到此值免确认
SMOKE_TIMEOUT_S=900    # 首次请求要把 17GB 模型载入内存，给足时间

# 预检测试钩子：用模拟值覆盖实测内存/磁盘，验证分支逻辑
SIM_MEM_GB="${QWEN38_SIM_MEM_GB:-}"
SIM_DISK_GB="${QWEN38_SIM_DISK_GB:-}"
SIM_FRESH="${QWEN38_SIM_FRESH:-}"   # =1 时视为全新机器（模型未安装）

CHECK_ONLY=0
case "${1:-}" in
  --check) CHECK_ONLY=1 ;;
  -h|--help)
    sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  "") ;;
  *) echo "未知参数：$1（支持 --check）" >&2; exit 2 ;;
esac

say() { printf '\n==> %s\n' "$*"; }
die() { printf '错误：%s\n' "$*" >&2; exit 1; }

# ---------- 1. 环境预检 ----------
say "环境预检"

[[ "$(uname -s)" == "Darwin" ]] || die "只支持 macOS"
ARCH="$(uname -m)"
[[ "$ARCH" == "arm64" ]] || die "只支持 Apple Silicon（arm64），当前架构：$ARCH"
echo "✔ Apple Silicon（$ARCH）"

MEM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
[[ -n "$SIM_MEM_GB" ]] && MEM_GB="$SIM_MEM_GB"
if (( MEM_GB < MIN_MEM_GB )); then
  die "统一内存 ${MEM_GB}GB 低于 ${MIN_MEM_GB}GB 下限，无法稳定运行 ${MODEL}（约需 20GB 空闲内存）"
fi
if (( MEM_GB < OK_MEM_GB )); then
  echo "⚠ 统一内存 ${MEM_GB}GB，低于建议的 ${OK_MEM_GB}GB：可以安装，但长上下文任务可能内存吃紧"
  if (( ! CHECK_ONLY )); then
    [[ -t 0 ]] || die "内存 ${MEM_GB}GB 需要交互确认后才能继续，请在终端中运行本脚本"
    read "reply?仍要继续安装吗？[y/N] "
    [[ "$reply" == y || "$reply" == Y ]] || die "已取消。换 ≥${OK_MEM_GB}GB 内存的机器体验更稳"
  fi
else
  echo "✔ 统一内存 ${MEM_GB}GB"
fi

MODEL_INSTALLED=0
if [[ "$SIM_FRESH" != 1 ]]; then
  if curl -fsS --max-time 3 "$API/api/tags" 2>/dev/null | grep -q "\"$MODEL\"" \
     || [[ -n "$(ls -A "$STATE_DIR/models/manifests" 2>/dev/null)" ]]; then
    MODEL_INSTALLED=1
  fi
fi

DISK_GB=$(( $(df -Pk "$HOME" | awk 'NR==2 {print $4}') / 1048576 ))
[[ -n "$SIM_DISK_GB" ]] && DISK_GB="$SIM_DISK_GB"
NEED_DISK_GB=$MODEL_MIN_DISK_GB
(( MODEL_INSTALLED )) && NEED_DISK_GB=$RUNTIME_MIN_DISK_GB
(( DISK_GB >= NEED_DISK_GB )) || die "磁盘空闲 ${DISK_GB}GB，低于所需的 ${NEED_DISK_GB}GB（模型约 17GB + 余量）"
echo "✔ 磁盘空闲 ${DISK_GB}GB（需要 ≥${NEED_DISK_GB}GB）"

for port in 11439 11440; do
  if lsof -nP -iTCP:$port -sTCP:LISTEN >/dev/null 2>&1; then
    curl -fsS --max-time 3 "http://127.0.0.1:$port/api/version" >/dev/null 2>&1 \
      || die "端口 $port 已被其他程序占用，请先处理后再安装"
  fi
done
echo "✔ 端口 11439 / 11440 可用"

if (( CHECK_ONLY )); then
  say "预检通过。运行 ./install.sh 开始安装"
  exit 0
fi

# ---------- 2. 准备依赖 ----------
say "检查依赖（ollama、python3）"

OLLAMA_BIN="$(command -v ollama || true)"
if [[ -z "$OLLAMA_BIN" ]]; then
  if command -v brew >/dev/null 2>&1; then
    echo "未找到 ollama，通过 Homebrew 安装…"
    brew install ollama
    OLLAMA_BIN="$(command -v ollama || true)"
    [[ -n "$OLLAMA_BIN" ]] || die "brew install ollama 后仍未找到 ollama，请检查 brew 环境"
  else
    die "未找到 ollama，也未找到 Homebrew。请先安装 Homebrew（https://brew.sh）后重跑，或从 https://ollama.com/download 安装并确保 ollama 在 PATH 中"
  fi
else
  echo "✔ ollama：$OLLAMA_BIN"
fi

PYTHON_BIN="$(command -v python3 || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  die "未找到 python3。请先运行 xcode-select --install 安装命令行工具后重试"
fi
echo "✔ python3：$PYTHON_BIN"

mkdir -p "$STATE_DIR"
cat > "$STATE_DIR/env.sh" <<EOF
# 由 install.sh 生成，供 scripts/ 下各脚本解析绝对路径
export QWEN38_OLLAMA_BIN="$OLLAMA_BIN"
export QWEN38_PYTHON_BIN="$PYTHON_BIN"
EOF

# ---------- 3. 启动常驻服务 ----------
say "启动 Ollama 与 Claude 兼容层（launchd 常驻）"
"$SCRIPT_DIR/scripts/start.sh"

# ---------- 4. 下载模型 ----------
if (( MODEL_INSTALLED )); then
  say "模型 $MODEL 已存在，跳过下载"
else
  say "下载模型 $MODEL（约 17GB，支持断点续传；中断后重跑 install.sh 即可续传）"
  OLLAMA_HOST=127.0.0.1:11439 "$OLLAMA_BIN" pull "$MODEL"
fi

# ---------- 5. 冒烟测试 ----------
say "冒烟测试：发送一条真实请求（首次需把模型载入内存，可能等待数分钟）"
SMOKE_RESULT=$(curl -fsS --max-time "$SMOKE_TIMEOUT_S" "$API/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  --data '{"model":"qwen3.8:27b","messages":[{"role":"user","content":"只回答 OK"}],"stream":false,"max_tokens":512}')
REPLY=$(python3 - "$SMOKE_RESULT" <<'PY'
import json
import sys

result = json.loads(sys.argv[1])
message = result["choices"][0]["message"]
print(message.get("content") or "")
PY
)
[[ -n "$REPLY" ]] || die "冒烟测试失败：模型返回为空，请查看 $LOG_DIR/server.stderr.log"
echo "模型回复：$REPLY"

# ---------- 6. 完成 ----------
say "安装完成"
cat <<EOF
本机服务（只监听 127.0.0.1，不暴露局域网）：
  OpenAI 兼容 API    $API/v1
  Anthropic 兼容 API  http://127.0.0.1:11440（供 Claude Code 使用）

三种使用方式：
  1. 命令行对话     ./scripts/chat.sh '你好'
  2. Claude Code    ./scripts/claude-code.sh
  3. OpenAI SDK     base_url="$API/v1"，model="qwen3.8:27b"，api_key 任意非空

管理：
  ./scripts/status.sh    查看状态
  ./scripts/stop.sh      停止服务（保留模型）
  ./uninstall.sh         卸载
EOF
