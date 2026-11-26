# AI-SETUP.md — 给 AI 助手的安装与验收协议

> **这份文件是写给你的，不是写给用户的。**
> 用户把 March7 测试包解压后，可能会说「帮我装一下」。照这份文件做，不要凭印象猜命令。
> 人类的图文说明在 `用户手册.html`；这份是**可执行流程**，两者冲突时以本文件为准（它更细）。

- 适用版本：0.1.0（测试版）
- **前提：你需要能在这台电脑上执行终端命令。** 如果你跑在网页或手机上、没有本地终端，把这份文件原样交给用户，让他找能执行命令的工具；不要凭想象给出命令。
- 包内自述：`WHICH-BUILD.txt` 写明这包对应哪个提交，报问题时先读它

---

## 0. 三条硬规矩

1. **不要改代码、不要改依赖。** 不要编辑 `pyproject.toml`、`uv.lock`、`src/` 下的任何文件来「绕过」报错。装不上就按 §6 报告，让人类决定。
2. **不要替用户做删除。** 任何 `erase`、`--uninstall`、`rm` 数据库的操作，必须用户明确同意后才能执行。
3. **不要把密钥回显。** 读配置时不要打印 api_key；写总结时用 `<已配置>` 代替。日志、截图同理。

---

## 1. 先侦察，再动手（30 秒）

在解压目录（能看到 `pyproject.toml` 和 `src/` 的那一层）执行：

```bash
# 当前目录对不对
ls pyproject.toml src hosts 用户手册.html

# 系统与架构
uv run python -c "import platform,sys;print(platform.system(),platform.machine(),sys.version)"

# 磁盘余量（Windows PowerShell）
Get-PSDrive C | Select-Object Used,Free
# macOS / Linux
df -h .
```

判断标准：

| 条件 | 结论 |
|---|---|
| `Windows` + `AMD64` | 可装。继续 §2 |
| `Darwin` + `arm64` | 可装（引擎已验证，桌面宿主未验收，要告诉用户这一点） |
| `Darwin` + `x86_64` | **停**。锁文件没有 Intel Mac 的 PyTorch，装不上 |
| `Windows` + `ARM64` / 32 位 | **停**。没有轮子 |
| `Linux` | **停**。未适配 |
| 可用空间 < 10 GB | **停**。告诉用户至少留 10 GB（依赖 3.5 GB + 模型缓存 4.6 GB） |

再确认两件：

```bash
uv --version          # 没有 uv 就先装：见 §2 第 1 步
nvidia-smi            # Windows 有独显时能列 GPU；报错=核显，能装但首次编码慢
```

### 包结构（哪些能动、哪些别动）

| 路径 | 是什么 | 你能动吗 |
|---|---|---|
| `src/March7/` | 引擎代码 | **不能** |
| `hosts/` | 三个宿主的安装器与钩子 | 只能运行，不要编辑 |
| `scripts/` | 预检、打包、诊断脚本 | 只能运行 |
| `docs/` | 安装指引、宿主矩阵、反馈表 | 可读 |
| `用户手册.html` | 给用户看的图文手册 | 可读 |
| `AI-SETUP.md` | 本文件 | 可读 |
| `pyproject.toml` / `uv.lock` | 依赖声明与锁定 | **不能改**（改了就不可复现） |
| `WHICH-BUILD.txt` | 这包对应哪个提交 | 报问题时附上 |

**核显机器要提前告诉用户**：可以装，但首次写入会多等 10–15 秒；且这条路径在真机上还没完全验收（虚拟机里首编崩过一次）。让用户决定是否继续。

---

## 2. 装依赖（5–15 分钟，取决于网速）

```bash
uv sync --locked --python 3.12
```

- 期望：退出码 0，最后出现 `Installed …`。
- 第一次会下载约 3 GB，**不要因为「看起来卡住」就中断**。
- uv 不在 PATH 时：Windows 用 `%USERPROFILE%\.local\bin\uv.exe`，macOS 用 `~/.local/bin/uv`。
- 没装 uv：Windows `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`；macOS `curl -LsSf https://astral.sh/uv/install.sh | sh`。装完**新开一个终端**再继续。

失败分支：

| 报错关键词 | 处理 |
|---|---|
| `No solution found` / `no wheels` | 架构不对或锁文件不匹配 → 停，报 §6 |
| `Microsoft Visual C++ Redistributable` / `WinError 126` | 缺 VC++ 运行库 → 让用户装 https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist/ 后重跑 |
| 网络超时 / SSL | 换网络重试，不要改索引地址 |

---

## 3. 预检（10 秒–4 分钟）

```bash
uv run python scripts/doctor.py --preflight
```

这一步会分项报告：安装基础、完整写读依赖、SQLite 扩展、各盘空间。**任何一项 `✗` 都不要往下走**，按它旁边的中文提示修。

想连模型一起验（会下载权重、多花几分钟）：

```bash
uv run python scripts/doctor.py --preflight --probe-embedder
```

期望输出里出现 `dim=1024` 和编码耗时。**这一步失败就停下**——它说明这台机器跑不了记忆引擎，接进宿主也只会反复报错。

---

## 4. 接入宿主（按用户装了哪个来）

三个宿主可以都装，共用一份记忆。装完**必须完全退出宿主再重开**（配置在启动时快照）。

### 4.1 WorkBuddy

```bash
uv run python hosts/workbuddy/install.py --doctor    # 期望四行 OK
uv run python hosts/workbuddy/install.py --set-llm   # 交互式，见 §5
uv run python hosts/workbuddy/install.py --apply
```

`--apply` 一次写三处（钩子脚本 / `settings.json` 的 hooks 块 / `mcp.json` 的 March7 条目），任一失败整体回滚。

### 4.2 Codex

```bash
uv run python hosts/codex/install.py --doctor
uv run python hosts/codex/install.py --apply
```

注意：Codex 的 MCP 子进程**不继承用户环境变量**，只认 `~/.codex/config.toml` 里 `[mcp_servers.March7.env]` 那张表。安装器负责写；**不要手工再补一份**。

### 4.3 DeepSeek Harness

```bash
uv run python hosts/dsh/install.py --apply                 # profile 档（默认）
uv run python hosts/dsh/install.py --apply --mode turn     # 每轮召回（实验）
uv run python hosts/dsh/install.py --apply --mode none     # 只装工具面
```

DSH 没有轮末写入钩子：要记东西必须由用户在聊天里明确说「帮我记住……」。

### 4.4 装多个宿主的顺序

顺序无关：三个安装器各写各的配置文件，互不覆盖。但有两个共同点：

1. 每个宿主装完都要**完全退出再重开**，不能只关窗口；
2. 三个安装器默认指向同一个数据目录 `~/.March7`，所以记忆天然共用。若给某个宿主传了 `--data-dir`，另外两个也要指到同一目录，否则会出现「A 记的 B 查不到」。

### 4.5 退出码语义（三个安装器通用）

| 码 | 含义 | 你该做什么 |
|---|---|---|
| 0 | 全绿 | 让用户重启宿主 |
| 2 | 数据目录不存在/不可写/非空但没加 `--reuse` | 问用户：复用旧库（`--reuse`）还是换新库（`--data-dir <路径>`） |
| 3 | 解释器或 March7 不可用 | 回 §2 重跑 `uv sync` |
| 4 | 写入校验失败（**已自动回滚**） | 把完整输出给用户，不要重试超过一次 |
| 5 | 模型探针失败 | key / 余额 / 端点不对 → §5 |
| 1 | **不是契约码**，未预期异常 | 把完整 traceback 交给用户，这是 bug |

---

## 5. 配置模型（只做一次）

必须由**用户本人**提供 base_url、api_key、模型名。你不要猜、不要用示例 key。

交互式（推荐，用户自己敲）：

```bash
uv run python hosts/workbuddy/install.py --set-llm
```

参数式（用户已经把三项给你时）：

```bash
uv run python hosts/workbuddy/install.py --set-llm \
  --llm-base-url "<用户给的端点>" \
  --llm-model "<用户给的模型名>"
# api_key 走环境变量或交互输入，不要写进命令行历史
```

- 端点必须是 OpenAI 兼容、支持 JSON 输出，通常以 `/v1` 结尾。
- 探针失败时**一个字节都不写**，旧配置保留。
- 多宿主：Codex 与 DSH 的配置是分开的，需要各自跑一次对应安装器（或 `scripts/rotate_llm_key.py` 一次改齐）。
- 改完必须**完全重启**宿主。

---

## 6. 验收清单（必须全过，缺一不算装好）

按顺序执行，每步记下实际输出：

| # | 命令 / 动作 | 通过标准 |
|---|---|---|
| 1 | `uv run python scripts/doctor.py` | `vec: ready`，编码器行有设备与预估 |
| 2 | 让用户在宿主里请模型调 `March7_status` | 返回 `march7_version` / `db_schema_version` / `memory_count` |
| 3 | 用户说一句低敏事实（如「帮我记住，我每周日参加青苔星钟合奏」） | 回复确认补记；等 10–20 秒 |
| 4 | `uv run python -m March7.control list` | 能看到第 3 步那条 |
| 5 | 用户在同一项目里请模型 `March7_recall` | 返回该条 + 原话 |
| 6 | 用户说「打开 March7 记忆面板」 | 浏览器打开面板，标题「记忆」，能看到库编号 |

第 2、5、6 步依赖宿主模型主动调工具，**必须在真实宿主里做**，不能用命令行代替。

如果 3 或 4 失败：

```bash
uv run python -m March7.control why --last
```

它会给五档结论之一（`failed` / `capped` / `rejected` / `written` / `empty`），照结论处理：
- `failed` → 模型请求失败，查 key/余额/网络
- `rejected` → 被过滤规则拒收（任务指令、一次性参数），换一句「长期事实」再试
- `empty` → 模型判定这句不值得记，正常
- `capped` → 达到本轮预算上限
- `written` → 已写入，去 `list` 看

---

## 7. 使用阶段：你要知道的能力边界

装好之后，用户会在聊天里使唤它。你需要在被问到时不夸大：

| 能做 | 不能做 |
|---|---|
| 记住跨会话的偏好、项目决定、进度 | 自动把记忆接到另一个工具（要用户主动说「查一下」） |
| 按语义或关键词查回，带原话 | 保证回答更准（实测低于模型代写的笔记） |
| 纠正、归档、恢复、永久删除（带预览与回执） | 多设备/多人同步 |
| 体检、批量整理、撤销 | 本地小模型兜底（必须调用户配的模型） |
| 导出 Markdown / JSON | 云端备份 |

**项目身份**：项目记忆按「当前工作目录」识别。换项目后查不到，先确认两边是同一个目录；没有可信身份时，项目事实会**拒写**，不会静默写进全局。

**删除的授权边界**：预览只对同一聊天 + 同一项目 + 同一版本有效。用户改口、换会话、条目变动后必须重新预览。永久删除无法撤销。

---

## 8. 常见故障（按症状）

| 症状 | 先查 | 处理 |
|---|---|---|
| 工具列不出来 | 宿主是否完全重启、是否点了「信任」 | 重启宿主；WorkBuddy 到「连接器 → 自定义连接器」点信任 |
| 工具能列，一调报配置错 | `mcp.json` 里有没有 `MARCH7_LLM_*` | 重跑 `--set-llm`，完全重启 |
| 面板打开是空库 | 面板顶部显示的库路径 | 从原入口重开；用「设置与切换库」核对 |
| Codex 换库后写入落旧库 | MCP 配置与钩子是否同目录 | 重跑 `hosts/codex/install.py --apply`，完整重启 |
| `mcp dev` 起得来但真机不通 | 不要用 `mcp dev` 证明可用 | 必须在真实宿主里真调一次 `March7_status` |
| 首次写入特别慢 | 有没有独显 | 正常，CPU 编码 10–15 秒；告知用户 |

---

## 9. 装完之后怎么向用户汇报

用这个结构，不要长篇大论：

```
装好了 / 卡在第 N 步
- 系统：Windows 11 x64，有/无独显
- 宿主：WorkBuddy（+ Codex）
- 数据目录：C:\Users\<user>\.March7
- 验收：6 项过了 5 项，第 5 项（召回）没通过，原因是 …
下一步：请你在 WorkBuddy 里说「打开 March7 记忆面板」确认能看到库编号。
```

失败时**必须**附：完整命令、完整报错、退出码、`WHICH-BUILD.txt` 里的树 ID。不要只说「装不上」。

---

## 10. 用户要求卸载或删数据时

先分清两件事，不要合并执行：

| 用户说 | 做什么 | 命令 |
|---|---|---|
| 「不想用了 / 卸掉」 | 只移除宿主配置，**保留记忆库** | `uv run python hosts/<宿主>/install.py --uninstall` |
| 「把记忆删掉 / 忘掉我」 | 先确认是否要导出，再清库 | `uv run python -m March7.control export --format md`，然后 `uv run python -m March7.control erase --all` |

**执行删除前必须让用户明确说一次「确认删除」**，并把「不可恢复」写在前面。不要把 `--uninstall` 和 `erase --all` 一起跑——前者是「不想用了」，后者是「忘掉我」。

卸载只移除 March7 自己写的条目；用户自己的其他钩子、键值一个不动（安装器按内容剥离）。钩子文件是移走不是删除。
