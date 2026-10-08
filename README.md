# LLM Launchpad · Qwen3.8-27B 本地部署（Ollama）

在你的 Apple Silicon Mac 上一条命令跑起 Qwen3.8-27B，提供 OpenAI 兼容 API，并可直接作为 Claude Code 或 Codex CLI 的后端模型。默认只监听 `127.0.0.1`，模型推理在本机完成；首次安装需要联网下载运行时和模型。

[硬件要求](#硬件要求) · [按内存看文件](#按内存看文件放不放得下) · [安装与预检](#一键安装) · [Claude Code、Codex 和 API 用法](#四种使用方式) · [服务管理](#管理) · [本地入口排障](#本地入口排障先分清模型服务与兼容层) · [已知限制](#已知限制)

## 硬件要求

| 项目 | 要求 |
|---|---|
| 芯片 | Apple Silicon（M1/M2/M3/M4 系列） |
| 统一内存 | **24GB 起步**（16–24GB 安装时会要求确认；低于 16GB 拒绝安装） |
| 磁盘空闲 | 40GB（模型约 17GB + 运行余量） |
| 软件 | macOS + Homebrew（ollama 未安装时会自动 `brew install`） |

默认使用 MLX 引擎的 4bit 量化版（`qwen3.8:27b-mlx`，约 18GB），加载后约占 20GB 内存。参考速度：M2 Max 96GB 上生成约 16 token/s；改用 GGUF 版（`QWEN38_MODEL=qwen3.8:27b`）约 8 token/s，但新开对话的首次响应更快。

## 按内存看文件放不放得下

安装脚本仍然只下载 `qwen3.8:27b-mlx`。下表只比较公开发布的文件大小和统一内存，核对日期是 2026-10-04。文件大于或等于内存，就不能整文件放进内存。文件小于内存，只说明文件本身更小，不说明加上加载和上下文之后还能跑。博客里的「能跑」和速度没有写进来。

| 统一内存 | 文件小于内存 | 文件大于或等于内存 |
|---|---|---|
| 16GB | Qwen3 8B Q4 5.2GB；Qwen2.5-Coder 7B 4.7GB、14B 9.0GB。这些不是本仓库安装的模型 | Qwen3.8-27B Q4 / MLX 18GB。低于 16GB 时安装脚本会拒绝 |
| 24GB | Qwen3.8-27B Q4 / MLX 18GB；Qwen3-Coder 30B-A3B Q4 19GB | Qwen3.8-27B Q8 30GB、mxfp8 32GB |
| 32GB | 上面的 Q4，再加上 Qwen3 32B Q4 20GB、Qwen3.8-27B Q8 30GB | mxfp8 正好 32GB，不算小于内存。bf16 是 56GB |
| 64GB | Qwen3.8-27B 的 Q4 18GB、Q8 30GB、bf16 56GB | Qwen3.8-Flash-Next 这次能读到的最小标签是 105GB |
| 96GB | Qwen3.8-27B 各档都小于 96GB | Flash-Next 105GB；DeepSeek-V4-Flash 的 Unsloth Q4 155.1GB。GLM-5.3-Flash 1-bit 文件 93.09GB，Unsloth 写的是 100GB，所以不记成放得下 |
| 128GB | Flash-Next 105GB。GLM 1-bit 文件 93.09GB，Unsloth 写的内存要求是 100GB | DeepSeek Q4 155.1GB；GLM Q4 199.71GB。Unsloth 写 DeepSeek 3-bit 至少 110GB，并说可以放在 128GB 内存的设备上。那是他们的说明，不是本仓库在 Mac 上测过 |

Qwen3.8-Flash（接口名 `qwen3.8-flash`）没有本地量化文件，安装脚本下不到。

体积来源：

- [Ollama qwen3.8 标签](https://ollama.com/library/qwen3.8/tags)：`27b` / `27b-mlx` / `q4_K_M` / `nvfp4` 18GB，`q8_0` 30GB，`mxfp8` 32GB，`bf16` 56GB
- [Ollama qwen3 标签](https://ollama.com/library/qwen3/tags)：8B Q4 5.2GB，32B Q4 20GB
- [Ollama qwen3-coder 标签](https://ollama.com/library/qwen3-coder/tags)：30B-A3B Q4 19GB
- [Ollama qwen2.5-coder 标签](https://ollama.com/library/qwen2.5-coder/tags)：7B 4.7GB，14B 9.0GB
- [Ollama qwen3.8-flash-next 标签](https://ollama.com/library/qwen3.8-flash-next/tags)：`125b-a6b-nvfp4` 105GB
- [Unsloth DeepSeek V4](https://unsloth.ai/docs/models/deepseek-v4)：`UD-Q4_K_XL` 155.1GB；3-bit 写的是至少 110GB，并写可放在 128GB 内存的设备上
- [Unsloth GLM-5.3-Flash](https://unsloth.ai/docs/models/glm-5.3-flash)：`UD-IQ1_S` 93.09GB，1-bit 行 100GB；`UD-Q4_K_XL` 199.71GB

## 一键安装

```bash
git clone https://github.com/majiayu000/llm-launchpad.git
cd llm-launchpad
./install.sh
```

`install.sh` 幂等可重跑：预检硬件 → 准备 ollama → 启动 launchd 常驻服务 → 下载模型（断点续传）→ 发一条真实请求冒烟测试 → 打印使用入口。

先检查机器是否符合安装门槛？用干跑模式只做预检：

```bash
./install.sh --check
```

`--check` 检查系统、架构、统一内存、磁盘和端口，可能查询已运行的服务或已有模型记录；它在依赖准备前退出，不会安装依赖、下载模型或发起生成。预检通过不代表 Ollama、Python 和目标模型已准备好，也不保证所选模型与上下文能在这台机器上运行。

## 四种使用方式

**1. 命令行对话**（thinking 默认开启，`reasoning_effort=medium`）

```bash
./scripts/chat.sh '解释一下什么是向量数据库'
```

**2. Claude Code 直接用本地模型**

```bash
./scripts/claude-code.sh
```

**3. Codex CLI 直接用本地模型**

```bash
./scripts/codex.sh
```

入口使用独立的 `~/.local/share/qwen38-ollama/codex`，不会改动现有
`~/.codex`。默认保留命令确认和工作区沙箱。

已安装其他 Ollama 模型时可显式选择，包括用户自行评估的 uncensored
模型；这只改变模型，不会关闭 Codex 的命令审批或沙箱：

```bash
QWEN38_MODEL=example/uncensored:27b ./scripts/codex.sh
```

**4. OpenAI SDK / 任意兼容客户端**

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:11439/v1", api_key="local")
response = client.chat.completions.create(
    model="qwen3.8:27b-mlx",
    messages=[{"role": "user", "content": "你好"}],
)
print(response.choices[0].message.content)
```

Anthropic Messages 兼容入口默认在 `http://127.0.0.1:11440`。请求必须带与 `QWEN38_COMPAT_TOKEN`（默认 `ollama`）一致的 `x-api-key` 或 `Authorization: Bearer`；缺失或不匹配返回 401。`scripts/claude-code.sh` 和 `scripts/status.sh` 读取已安装 launchd plist 中的监听地址和 token，显式设置的环境变量优先；新终端无需重复导出安装时的配置。

`QWEN38_COMPAT_HOST` 可设为 IPv4 地址或主机名，Claude Code 入口会使用同一地址；通配监听地址使用回环连接。安装时拒绝 IPv6 地址、首尾含空白的监听地址或 token；token 仅接受可打印 ASCII 字符。非回环监听必须显式设置非空且不同于公开默认值 `ollama` 的 `QWEN38_COMPAT_TOKEN`。

```bash
curl http://127.0.0.1:11440/v1/messages \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: ollama' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{
    "model": "qwen3.8:27b-mlx",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "只回答 OK"}]
  }'
```

## 架构

```
你的程序 / OpenAI SDK / Codex CLI      Claude Code
            │ OpenAI Responses API          │ Anthropic Messages API
            ▼                               ▼
Ollama 独立实例 127.0.0.1:11439        兼容层 127.0.0.1:11440
            │                               │ 转换请求格式
            └────────────► qwen3.8:27b-mlx ◄─┘
```

- 与机器上已有的 Ollama 完全隔离：独立端口、独立模型目录（`~/.local/share/qwen38-ollama`）、独立 launchd 服务（`com.local.qwen38-ollama`）
- 上下文 65536 tokens，模型常驻内存（`keep_alive=-1`），不与其他实例抢内存
- 兼容层（`compat/anthropic_proxy.py`，仅 Python 标准库）把 Claude Code 的 Anthropic 请求转成 Ollama 格式。它做两件对性能关键的事：
  1. Claude Code 会在对话中途插入 system 消息，Ollama 直接拒绝；兼容层把它们并入相邻 user 消息，顶层 system 前缀保持不变，**已完成轮次的字节不变**，从而不破坏 Ollama 的前缀缓存
  2. 超长 `Read`/`Bash` 输出按确定性规则缩略到最多 2000 字符，同一段历史每轮截得完全一样，同样为了缓存命中；模型需要更多源码时会按 offset/limit 定向重读
- Claude Code 入口使用独立配置目录（`~/.local/share/qwen38-ollama/claude`），不影响 `~/.claude` 里的其他配置。为避免 27B 每轮冷处理 Claude Code 默认 25 个工具的超长提示，默认精简系统提示、只开放 `Bash/Edit/Read/Write` 四个工具、单次 `Read` 输出限 2500 token
- Codex 入口通过 Ollama 的 Responses API 直连 `11439`，使用独立 `CODEX_HOME`，不经过 Claude 兼容层

## 管理

```bash
./scripts/start.sh     # 启动（安装后开机自启，一般无需手动）
./scripts/status.sh    # 状态（JSON：运行/已装/已加载/内存占用）
./scripts/meter.sh     # 实时速度计（Claude Code 流量的 tok/s 仪表盘，Ctrl-C 退出）
./scripts/stop.sh      # 停止服务，保留模型
./uninstall.sh         # 卸载服务；模型数据默认保留，确认后才删
```

速度计由兼容层驱动：它统计流经 `11440` 的生成 token 并发布实时快照（默认写 `/tmp/qwen38-ollama-meter.json`，设 `QWEN38_METER_FILE=""` 可关闭）。适合录屏演示或观察真实吞吐。

`QWEN38_METER_FILE` 的相对路径统一以 `~/.local/share/qwen38-ollama/compat` 为基准，与启动命令所在目录无关。速度计读取已安装 plist 中的路径（包括空值），显式环境变量优先。

日志在 `~/Library/Logs/Qwen3.8-Ollama/`。

## 本地入口排障：先分清模型服务与兼容层

从仓库目录运行 `./scripts/status.sh`，按输出定位问题。这个命令读取本地服务状态，不会发起模型生成；如果 `11439` 不可达，会输出 `running:false` 并以非零状态退出。

`running` 来自 `/api/version` 探测，`installed` 和 `loaded` 分别表示所选模型名出现在 `/api/tags` 和 `/api/ps` 列表中；这些字段不校验权重完整性，也不保证实际生成成功或速度。完整安装末尾的冒烟测试会检查 `11439/v1/chat/completions` 的非空回复，只证明所选模型在该入口完成过一次请求，不覆盖 Claude Messages、Codex Responses 或后续持续健康。

| 状态或症状 | 含义与下一步 |
|---|---|
| `running:false` | 独立 Ollama 实例不可达。安装完成后先运行 `./scripts/start.sh`，再查 `~/Library/Logs/Qwen3.8-Ollama/`。不要把默认 Ollama 的 `11434` 当作这里的 `11439`。 |
| `running:true`、`installed:false` | 当前 `QWEN38_MODEL` 未出现在这个实例的模型列表中。运行 `./scripts/pull.sh`；自定义模型时，下载、状态查询和客户端入口都使用同一个 `QWEN38_MODEL`。 |
| `installed:true`、`loaded:false` | 模型名在这个实例的模型列表中，但当前不在加载列表里；首次实际请求可能需要加载时间。 |
| Codex / SDK 可用，Claude Code 不可用 | 前者直连 `11439`，后者经过 `11440`。`status.sh` 读取已安装 plist 的地址和 token（显式环境变量优先），携带 `x-api-key` 探测兼容层；鉴权失败、不可达、上游失败或超时都可能使 `claude_compat_running:false`，不能单独据此判断进程未启动。查看兼容层日志，并用下面的查询核实。 |
| Claude Code 或 `/v1/messages` 返回 401 | 请求密钥与兼容层的 `QWEN38_COMPAT_TOKEN` 不一致。客户端与服务端须使用相同配置；兼容层所有代理请求都要鉴权，包括 `/api/version`。 |
| `claude` 或 `codex` 命令找不到 | 启动脚本调用已有的客户端。先安装对应 CLI，并确认它能被当前终端找到；模型安装成功不代表客户端已安装。 |

核实兼容层是否可达时，使用与服务端一致的地址和密钥。以下命令仅示例默认回环地址与 token，不会自动读取已安装 plist；使用自定义配置时请相应替换：

```bash
curl -fsS http://127.0.0.1:11440/api/version \
  -H "x-api-key: ${QWEN38_COMPAT_TOKEN:-ollama}"
```

如果选择自定义模型，可以先查询它在独立实例中的状态：

```bash
QWEN38_MODEL=your-installed-model ./scripts/status.sh
```

确认已下载后，以相同模型名运行对应的客户端入口。请求延迟需要在实际机器上测量，下面的速度与首轮等待示例不能作为每台机器的保证。

### 与直接使用 Ollama 有什么区别？

这里的 LLM Launchpad 是 macOS 安装和启动脚本仓库，使用独立端口、模型目录、launchd 服务与客户端配置目录。它与 [PyPI 上同名的 llm-launchpad](https://pypi.org/project/llm-launchpad/) 是不同项目，不要用 `pip install llm-launchpad` 安装本仓库。

如果已经有合适的 Ollama 服务和模型，可以先看 Ollama 官方的 [Claude Code 接入](https://docs.ollama.com/integrations/claude-code)与 [Codex 接入](https://docs.ollama.com/integrations/codex)。本仓库的具体端口、兼容层与隔离目录以上面的架构和脚本为准；两套配置不要混用。

## 常见问题

- **新开对话的第一轮要等 2~3 分钟？** 默认的 MLX 引擎生成快（约 16 token/s）但预填充较慢，Claude Code 首轮约 1.8 万 token 的系统提示需要时间处理；同一对话从第二轮起命中前缀缓存，会明显变快。更看重首轮响应速度可换回 GGUF 版（约 8 token/s）：`QWEN38_MODEL=qwen3.8:27b ./scripts/pull.sh` 下载后，各脚本同样加 `QWEN38_MODEL=qwen3.8:27b` 运行即可。
- **生成速度只有 8~16 token/s？** 27B 量化模型在 Apple Silicon 上的正常水平（受内存带宽限制），不是软件问题。想更快就换更小的模型。
- **Claude Code 第二轮开始明显变快？** 前缀缓存命中了；这是兼容层缩略规则设计的直接目的。
- **局域网其他设备能访问吗？** 默认不能：只监听回环地址。若安装时把 `QWEN38_COMPAT_HOST` 改成非回环地址，必须显式配置非空且不同于公开默认值 `ollama` 的 `QWEN38_COMPAT_TOKEN`，否则安装会拒绝启动兼容层；未带匹配密钥的请求会 401。

## 已知限制

- 兼容层针对 Claude Code 的请求行为调校，其他 Anthropic 客户端未经测试。
- 模型会补全未提供的事实，内容发布前需人工核验。
