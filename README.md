# Qwen3.8-27B · 本地一键部署（Ollama）

在你的 Apple Silicon Mac 上一条命令跑起 Qwen3.8-27B，提供 OpenAI 兼容 API，并可直接作为 Claude Code 的后端模型。全程只监听 `127.0.0.1`，数据不出本机。

## 硬件要求

| 项目 | 要求 |
|---|---|
| 芯片 | Apple Silicon（M1/M2/M3/M4 系列） |
| 统一内存 | **24GB 起步**（16–24GB 安装时会要求确认；低于 16GB 拒绝安装） |
| 磁盘空闲 | 40GB（模型约 17GB + 运行余量） |
| 软件 | macOS + Homebrew（ollama 未安装时会自动 `brew install`） |

27B Q4 量化模型加载后约占 20GB 内存。参考速度：M2 Max 96GB 上生成约 8 token/s——这是 27B 的硬件推理极限，不是软件问题。

## 一键安装

```bash
git clone https://github.com/majiayu000/llm-launchpad.git
cd llm-launchpad
./install.sh
```

`install.sh` 幂等可重跑：预检硬件 → 准备 ollama → 启动 launchd 常驻服务 → 下载模型（断点续传）→ 发一条真实请求冒烟测试 → 打印使用入口。

先不确定机器能不能跑？用干跑模式只做预检：

```bash
./install.sh --check
```

## 三种使用方式

**1. 命令行对话**（thinking 默认开启，`reasoning_effort=medium`）

```bash
./scripts/chat.sh '解释一下什么是向量数据库'
```

**2. Claude Code 直接用本地模型**

```bash
./scripts/claude-code.sh
```

**3. OpenAI SDK / 任意兼容客户端**

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:11439/v1", api_key="local")
response = client.chat.completions.create(
    model="qwen3.8:27b",
    messages=[{"role": "user", "content": "你好"}],
)
print(response.choices[0].message.content)
```

Anthropic Messages 兼容入口在 `http://127.0.0.1:11440`（`x-api-key` 任意非空）：

```bash
curl http://127.0.0.1:11440/v1/messages \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: ollama' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{
    "model": "qwen3.8:27b",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "只回答 OK"}]
  }'
```

## 架构

```
你的程序 / OpenAI SDK          Claude Code
        │ OpenAI API                │ Anthropic Messages API
        ▼                           ▼
Ollama 独立实例 127.0.0.1:11439    兼容层 127.0.0.1:11440
        │                           │ 转换请求格式
        └─────────► qwen3.8:27b ◄───┘
```

- 与机器上已有的 Ollama 完全隔离：独立端口、独立模型目录（`~/.local/share/qwen38-ollama`）、独立 launchd 服务（`com.local.qwen38-ollama`）
- 上下文 65536 tokens，模型常驻内存（`keep_alive=-1`），不与其他实例抢内存
- 兼容层（`compat/anthropic_proxy.py`，仅 Python 标准库）把 Claude Code 的 Anthropic 请求转成 Ollama 格式。它做两件对性能关键的事：
  1. Claude Code 会在对话中途插入 system 消息，Ollama 直接拒绝；兼容层把它们并入相邻 user 消息，顶层 system 前缀保持不变，**已完成轮次的字节不变**，从而不破坏 Ollama 的前缀缓存
  2. 超长 `Read`/`Bash` 输出按确定性规则缩略到最多 2000 字符，同一段历史每轮截得完全一样，同样为了缓存命中；模型需要更多源码时会按 offset/limit 定向重读
- Claude Code 入口使用独立配置目录（`~/.local/share/qwen38-ollama/claude`），不影响 `~/.claude` 里的其他配置。为避免 27B 每轮冷处理 Claude Code 默认 25 个工具的超长提示，默认精简系统提示、只开放 `Bash/Edit/Read/Write` 四个工具、单次 `Read` 输出限 2500 token

## 管理

```bash
./scripts/start.sh     # 启动（安装后开机自启，一般无需手动）
./scripts/status.sh    # 状态（JSON：运行/已装/已加载/内存占用）
./scripts/stop.sh      # 停止服务，保留模型
./uninstall.sh         # 卸载服务；模型数据默认保留，确认后才删
```

日志在 `~/Library/Logs/Qwen3.8-Ollama/`。

## 常见问题

- **第一次请求等很久？** 模型正在从磁盘载入内存（约 17GB），之后常驻不重复加载。
- **速度只有个位数 token/s？** 27B Q4 在 Apple Silicon 上的正常水平。想快就换更小的模型。
- **Claude Code 第二轮开始明显变快？** 前缀缓存命中了；这是兼容层缩略规则设计的直接目的。
- **局域网其他设备能访问吗？** 不能，只监听回环地址，这是有意的隐私边界。

## 已知限制

- 兼容层针对 Claude Code 的请求行为调校，其他 Anthropic 客户端未经测试。
- 模型会补全未提供的事实，内容发布前需人工核验。
