# 安装 March7 测试版

想先看怎么用，可打开[用户手册](../用户手册.html)。想让你自己的 AI 助手代装，把 `AI-SETUP.md` 交给它。

## 0. 先确认你的机器在表里

| 平台 | 支持 | 说明 |
|---|---|---|
| Windows 10 / 11 **x64** | ✅ 本机宿主已验证 | 无 NVIDIA 冷装待验 |
| macOS Apple Silicon | 原生冷装与引擎已验证 | macOS 15 ARM64、uv 管理的 Python 3.12。实际桌面宿主待验 |
| macOS Intel | 当前包**装不上** | 锁文件缺少匹配的 PyTorch 安装包 |
| Linux x86_64 | 未验证 | 适配延后 |
| Windows ARM / 32 位 | ❌ | 无轮子 |

没有合适的独立显卡时会使用 CPU。首次加载和写入耗时因电脑而异。
还需 **约 8 GB 磁盘**、一个 **OpenAI 兼容** API key 和所选宿主应用。干净 Windows x64 请先装[微软 Visual C++ 运行库 x64](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist/)，PyTorch 的 CPU 路径也需要它。

## 1. 拿到代码 + 装依赖（5–15 分钟）

| 你拿到的 | 怎么做 |
|---|---|
| **一个 zip 文件**（`March7-0.1.0-public-….zip`，从 Releases 下载） | **先新建一个空文件夹**（例如 `C:\March7`），再把 zip 解压进去——**zip 内没有顶层文件夹**，直接「解压到当前文件夹」会把整包文件铺在桌面或下载目录。路径不含中文和空格，然后 `cd` 进去 |
| GitHub 仓库 | `git clone https://github.com/DavidLearning1023/March7-release.git March7 && cd March7` |

Apple Silicon 探针可用 `ditto -x -k "March7-0.1.0.zip" "./March7"` 解压，以保留中文路径。
在解压目录运行下方命令。Apple Silicon 使用 `uv sync --locked --managed-python --python 3.12`，确保所选 Python 可加载 SQLite 扩展。

```bash
uv sync --locked --python 3.12
```

首次下载依赖和模型权重的时间取决于网络。完成后看安装基础、完整写读依赖和各目标盘的可用空间：

```bash
uv run python scripts/doctor.py --preflight
```

> `uv` 不在 PATH 时用 `~/.local/bin/uv.exe`（Windows）或 `~/.local/bin/uv`。加 `--probe-embedder` 会实跑两次编码，首次可能下载权重。分享报错前请删去用户名、密钥和对话正文。

## 2. 前置检查（10 秒，只读）

```bash
uv run python hosts/workbuddy/install.py --doctor
```

期望四行全 `OK`。任何一行 `✗` 就按它右边那句话修，**不要往下走**。

## 3. 配置模型 key（约 2 分钟）

```bash
uv run python hosts/workbuddy/install.py --set-llm
```

- 只填一组主模型即可（`MARCH7_LLM_MAIN_*`）。遇到记忆冲突时，可以由你确认。
- 选支持 OpenAI Chat Completions 和 JSON 输出的端点。`March7_status` 显示
  `model_unverified: true` 时，March7 使用通用请求参数。
- key 只写进 `~/.workbuddy/mcp.json`。MCP 工具和 Stop 写入钩子都从这里读取主模型配置。
- 已配置多个宿主时，换 key 按[密钥更换说明](MANUAL.md#2-更换模型密钥)同步配置。
- 填完会发一次短请求验证连接。失败时保留旧配置。能否写入和找回记忆，用第 5 步确认。

## 4. 装进宿主（约 1 分钟）

```bash
uv run python hosts/workbuddy/install.py --apply
```

**一次写三处**，任一失败整体回滚：① `~/.workbuddy/hooks/*.py`（四个脚本）；
② `settings.json` 的 `hooks` 块（**不动你自己的其它钩子**）；③ `mcp.json` 的 `March7` 条目（工具面靠它）。
**然后必须完全退出 WorkBuddy 再重开**（只关窗口不算——配置在启动时快照）。

> 提示"数据目录已存在且非空"？说明你装过。加 `--reuse` 复用，或 `--data-dir <新路径>` 换新库。
> **不会静默合并旧库**——半新半旧的库会让记忆面板自相矛盾，且不报错。

### 退出码

| 码 | 含义 |
|---|---|
| 退出码 0 | 全绿，重启宿主 |
| 退出码 2 | 数据目录不存在 / 不可写 / 非空但没加 `--reuse` |
| 退出码 3 | 解释器或 March7 不可用 ⇒ 先 `uv sync` |
| 退出码 4 | 写入校验失败（**已回滚**） |
| 退出码 5 | LLM 探针失败，查 key / 余额 / 端点 |
| 退出码 1 | ⚠️ **不是契约码**：出现了未预期的异常（带 Python traceback）。这是 bug，请把完整输出贴给我 |

> 前五个是**契约**（每种都有对应用例钉住）。见到 1 说明我们漏了某条路径——
> 它的价值恰恰在于"它不该出现"。

## 5. 验证能写入并召回

```bash
uv run python scripts/doctor.py                        # ① 看 vec 是否 ready、编码器设备
# 首次连接时，在「专家·技能·连接器 → 连接器 → 自定义连接器」里对 March7 点「信任」。
# ② 在 WorkBuddy 里让它调 March7_status —— 期望 memory_count / schema_version / march7_version
# ③ 在 WorkBuddy 里说一句你愿意长期保存的低敏事实，等它回答结束，再等后台写入：
uv run python -m March7.control list
# ④ 在同一个 WorkBuddy 项目里请它调用 March7_recall，找回刚才那条事实。
```

如果安装时指定了 `--data-dir`，运行本节 CLI 时把 `MARCH7_HOME` 设为同一个目录。
列表与召回都出现刚才的事实，说明自动写入和读取已连通。列表为空时，先运行
`uv run python -m March7.control why --last` 查看本轮判定或请求失败原因。

## 6. 装完能干什么

| 想做的事 | 怎么做 |
|---|---|
| 补记与查找 | 说「帮我记住，我用蓝册记录演练步骤」，再说「帮我查一下蓝册」并核对原话 |
| **看"为什么没记住"** | `uv run python -m March7.control why --last` |
| 打开面板 | 说「开启 March7 记忆面板」，或双击库旁的 `March7 记忆面板.cmd` |
| 纠错或归档 | 查找后说「把这条改成……」或「把这条记忆归档」。多个候选时指明第几条 |
| 永久删除 | 查找后说「忘掉这条」，核对预览，再另发「确认删除」或「取消删除」 |
| 核对操作 | 说「查看擦除回执」「看看记忆库现在怎么样」或「导出我的记忆」 |
| 验证"换工具还记得" | 见 `docs/HOST-MATRIX.md`。**Codex** 装 `hosts/codex/install.py` |

## 6.5 想装到别的宿主

- **Codex**：按 `hosts/codex/install.py --apply` 安装并在宿主审阅入站钩子。已有绑定可用 `--panel-only` 刷新，按需查看 `--chain-existing-notify`。受限时在面板续办。
- **DeepSeek Harness**：按[接入说明](../hosts/dsh/README.md)重新应用实际使用的档位。默认 `profile` 加载对话入口，`none` 可走面板；已有 WorkBuddy 则用原绑定库重跑安装器刷新钩子与技能，完全退出宿主再重开。
- 双宿主接力时，请确认两边指向同一个数据目录，并在同一个项目目录操作。详见[宿主矩阵](HOST-MATRIX.md)。

## 7. 卸载（回到装前状态）

```bash
uv run python hosts/workbuddy/install.py --uninstall   # WorkBuddy（三处）
uv run python hosts/codex/install.py --uninstall       # Codex（两处）
```

只移除它自己写的，**钩子文件移走不删**，配置文件**按内容剥离**（你的键一个不少）。重跑安全。

⚠️ **卸载不动记忆库**。删数据用 `uv run python -m March7.control erase --all`——
两件事刻意分开：卸载是"不想用了"，删数据是"忘掉我"。

## 8. 装不上怎么办

| 症状 | 原因 | 处理 |
|---|---|---|
| `uv sync` 找不到 torch 轮子 | 当前锁文件缺少对应架构的安装包 | 对照 §0，反馈系统、架构及脱敏后的 `uv sync` 报错 |
| 退出码 2，说目录非空 | 之前装过 | 加 `--reuse` 或 `--data-dir` 换新库 |
| 退出码 5，探针失败 | key 无效 / 余额 / 端点不兼容 | 重跑 `--set-llm` |
| 工具能列出来，一调就报配置错 | key 没进 `mcp.json` | 重跑 `--set-llm`，**完全重启**宿主 |
| 说了事实但没记住 | 任务指令 / 判不值得记 / key 欠费 | `control why --last` 直接给结论 |
| 想不起昨天说的 | 可能关键词检索降级 | `doctor.py` 看 `vec` 是否 `ready` |
| 每次等很久 | 无 N 卡时 CPU 编码慢 | 正常；`doctor.py` 的编码器行写明预估 |

**已知边界（不是 bug）**：不是"自动接力"（Codex/DSH 靠主动查）；写侧保守（任务指令不入库）；
## 9. 反馈什么最有价值

见 `docs/FEEDBACK.md`（4 个单选题，不到 3 分钟）。最要紧的三件：**哪一步卡住了**（贴命令 +
完整报错）；**它记错了什么**（比漏记严重得多）；**删了之后还能不能搜到**（硬承诺，残留即 P0）。
