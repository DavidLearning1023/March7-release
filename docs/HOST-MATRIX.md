# 宿主适配矩阵（2026-11-16 实测）

> 三种宿主 × 五种能力。**这张表的每个 ✅/❌ 都有实测依据**，依据写在 §2。
> 对外表述以本表为准：`README` / `ONBOARDING` / `SECURITY` 里的三档说法都与它对齐。

---

## 1. 矩阵

下表的桌面宿主证据来自 Windows x64。本机既往实测和无 NVIDIA 冷装是两项证据。macOS Intel 与 Windows ARM 未列为已支持，Linux 平台验证延后。冷装的复现步骤属研发材料，不随包分发。

macOS 15 ARM64 上已从发布包冷装，使用 uv 管理的 Python 3.12 验证 SQLite 扩展、BGE-M3 CPU 编码、WorkBuddy 安装器生成的 MCP 配置、已装 Stop 脚本与本机 HTTP 面板。探针向安装好的脚本提交合成输入，面板使用测试打开回调。WorkBuddy、Codex、DeepSeek Harness 的 Apple Silicon 真实桌面会话均待验。

| 能力 | WorkBuddy | Codex CLI | DeepSeek Harness (DSH) |
|---|---|---|---|
| **宿主触发写入**（不用模型决定） | ✅ `Stop` 钩子 | ✅ `notify` | ❌ 当前适配器未接写入钩子 |
| **开局注入**（画像 / 记忆） | ✅ `SessionStart` + `UserPromptSubmit` | ❌ **没有注入通道** | ✅ 插件注入 `system_prompt` |
| **工具面**（模型主动查） | ✅ 9 个 `March7_*`（含 `March7_open_panel`） | ✅ `[mcp_servers.March7]` | ✅ `bridge.py` |
| **一句话打开面板** | 用户入站钩子 | 已信任的本机 App 后端及 `codex exec` 已验 | 默认 `profile` 及实验 `turn` 档有原生入口，`none` 档用双击入口 |
| **工具面默认可用** | ✅ 现行安装器启用；宿主首次连接可能需要信任 | ✅ 装完即可用 | ✅ |
| **装机步骤数** | 3 处（钩子脚本 / `settings.json` / `mcp.json`） | **2 处**（同一文件的 notify + MCP 表） | 1 处（`--patch` 插件） |
| **配置需要重启** | 是（启动时快照） | 是 | 是 |
| **已知坑** | 钩子 6 个坑（`CONVENTIONS` §5）；`settings.json` 被别的插件共用 | `notify` 是**单值**，与 computer-use 等冲突；配置入口未公开 | 写入依赖模型调用 `March7_write`，可主动调用或应用户请求调用 |

**分档**（对外只说这三句，不多说）：

| 档 | 宿主 | 一句话 |
|---|---|---|
| **A 档：记 + 说** | WorkBuddy | 自动记，开局就告诉你 |
| **B 档：记 + 查** | Codex | 自动记，但要它主动查 |
| **C 档：查 + 补记** | DSH | 模型可调用工具查找或补记 |

---

## 2. 每条判据的依据

### WorkBuddy

- 自动写入：`hosts/workbuddy/hooks/stop.py`，实测在真实会话里跑通（`docs/PROGRESS.md` 的多段记录）。
- 开局注入：`session_start.py` 注入 L0 画像；`user_prompt_submit.py` 注入记忆 + working。
- 工具面：八个记忆工具和面板工具在 `server.py` 注册。现行安装器已去掉首发配置的 `disabled`，GUI 首次连接以信任状态和实际工具回执验收。

### Codex（`ADR-0061`）

- 自动写入：`notify` 通道，**实测**抓到载荷（`ADR-0061` §3 原文）并验证入库。
- 注入：**没有**。有完整 hooks schema（7 个事件、字段与 WorkBuddy 同源），
  但配置入口试了 4 个位置都不生效且**不给错误**。
- 工具面：`[mcp_servers.March7]`，装完即可用（`Codex` 不做 `disabled` 这种默认关闭）。
- 单值 `notify`：本机真实配置里已有 `codex-computer-use`，安装器**拒绝覆盖**（`ADR-0061` §4.2）。

### DSH（`ADR-0060`）

- 补记由模型调用 `March7_write`。当前适配器没有 WorkBuddy `Stop` 式轮次结束写入。插件的用户入站事件用于核对补记来源和执行用户明确授权的记忆处置，见 `ADR-0071`。
- 注入：有（`plugin/index.mjs`，`MAX_INJECTED_CHARS = 1600`）。
- 工具面：`bridge.py`。
- 语料侧：S12 已在本机只读解析原生压缩转写。采集范围与工具结果见 `evals/reports/handoff-real30-s12-reaudit.md`。

### 一句话打开面板（`ADR-0075`）

| 宿主 | 入口 | 结果 |
|---|---|---|
| WorkBuddy 5.11.3 | `UserPromptSubmit` 先开浏览器，再把结果交给正常助手轮次 | 升级后的本机 GUI 已确认通过。升级需刷新安装副本并彻底退出重开 |
| DSH Desktop | 默认 `profile` 与实验 `turn` 档通过原生入站打开。纯工具 `none` 档可走 MCP 工具 | 本机 GUI 正常，实测用时在 10 秒内。实验和纯工具档见隔离证据 |
| Codex | 已审阅并信任的用户级 `UserPromptSubmit` 先打开。具备本机 MCP 时可走工具 | 本机 GUI 正常，实测用时在 10 秒内 |

可以说「开启 March7 记忆面板」或「把 March7 的记忆管理页面调出来」。其他明确说法由 `March7_open_panel` 承接，可用性取决于宿主能否调用本机工具。已有 Codex MCP 绑定可运行 `python hosts/codex/install.py --panel-only` 更新面板钩子，重启并在宿主审阅。受限时仍可双击绑定库旁的 `March7 记忆面板.cmd`。卸载宿主接入保留共用入口。

---

## 3. `project_id` 从哪来（2026-11-16 实测，`ADR-0063`）

**结论：只能来自宿主进程的 cwd。MCP 协议根本不传它。**

`mcp` 2.x 的 `initialize` 只有 `protocol_version` / `capabilities` / `client_info`，
**没有 cwd、也没有 roots**。所以没有标准做法可抄。

但子进程会继承父进程的 cwd。实测对照（真实 stdio 客户端）：

| 父进程 cwd | server 的 `os.getcwd()` | `PWD` 环境变量 |
|---|---|---|
| `C:\tmp\projA` | **`C:\tmp\projA`** ✅ | `D:/March7` ❌ |
| `C:\tmp\projB` | **`C:\tmp\projB`** ✅ | `D:/March7` ❌ |

⚠️ **`PWD` 是脏的**（固定为启动 shell 的目录）。实现里**明确不用它**。

| 宿主 | 来源 | 可靠吗 |
|---|---|---|
| WorkBuddy | 钩子载荷的 `cwd` | ✅ 最可靠（载荷显式带） |
| Codex | `notify` 载荷的 `cwd` | ⚠️ **`resume` 时会漂**（见下） |
| DSH | `bridge.py --cwd`（插件传 `process.cwd()`） | ✅ 已接（S14 前是**完全没有**） |
| **工具面（9 个 MCP 工具）** | 宿主起的 server 进程的 `os.getcwd()` | ✅ 可靠（S14 起**真的用它**；此前恒 `None`） |

**统一判据（S14）**：以上四个来源最终都进同一个纯函数
`privacy.resolve_active_project`，优先级 `MARCH7_PROJECT` → 载荷 cwd → 进程 cwd → `None`。
`MARCH7_PROJECT` 是**逃生舱**（用户显式指定，默认不设），设了却解析不出来时
**直接失败、不向后回落**——静默回落等于把"我以为记到 A"变成"其实记到 B"。

**Codex 的 `resume` 漂移（真 bug，已部分缓解）**：

```
第 1 次  codex exec -C C:\tmp\projA -- "…"   → cwd = C:\tmp\projA     ✅
第 2 次  codex exec resume --last -- "…"     → cwd = 敲命令的那个目录   ❌
```

`resume` 不支持 `-C`（实测 `unexpected argument '-C' found`）⇒ 复用进程 cwd。
**后果**：在 A 项目 resume，记忆可能记到 B 项目，且完全静默。
现在**不改它记到哪**（跨轮状态在无状态的 notify 里拿不到，理由见 `ADR-0063` §3.1），
但会**留痕**：写入账本记一行 `codex_cwd_drift`，`control why` 直接显示
（S14 前只有 stderr，而 notify 的 stderr 宿主不展示 ⇒ 等于看不见）。

---

## 4. 适配度的真实瓶颈不在宿主，在引擎

本轮做 Codex 首测时发现一个**结构性缺口**，它比任何宿主适配都更要紧：

```
写：memories.scope = "project:p-xxxxxx"   ← 项目身份解析成功
读：filter_by_scope(project_id=None)      ← 工具面永远拿不到 project_id
    ⇒ 只放行 scope == "global"
    ⇒ project 记忆一条都召回不到
```

**实测**（隔离库，3 条 `project` 记忆）：

| 调用 | 结果 |
|---|---|
| `March7_recall('喜欢钓鱼')` | **0 条** |
| `retriever.retrieve('喜欢钓鱼', project_id='p-c320e3')` | **3 条** |

📌 **这不是 Codex 的问题**：WorkBuddy 侧一样（它的注入只带 L0 画像，L2 靠工具召回）。
Codex 只是**让它暴露了**——因为以前开发测试都用 `~/.March7-dev`，写进去的多是 `global`。

处置：**✅ 2026-11-16 已修（S14）**。工具面改为**按调用**解析项目身份
（`server._call_project_id()` → `privacy.resolve_active_project`，优先级
`MARCH7_PROJECT` → 进程 cwd → `None`），并把 id 传给
`Retriever.retrieve / search_keyword`。上面那张"0 条 / 3 条"的对照现在应当变成
"两边都 3 条"——`tests/test_project_identity.py::test_a1_*` 就是这条的钉子。
设计与口径见 `ADR-0063` §4.1/§4.3/§4.4。

本机 Codex App 已实调 `March7_status` 与 `March7_recall`，反向项目隔离也通过。配置细节见源码仓的 Codex 交接文档 §8.4（不随包分发）。
WorkBuddy GUI 写入后由 Codex 读取的跨宿主场景，以及外部用户试点，仍待验收。

---

## 5. 三宿主的"体验"差异（用户视角）

| 场景 | WorkBuddy | Codex | DSH |
|---|---|---|---|
| 第一次装完，说一句话 | 5–20 秒后 `control list` 能看到 | **同左** | ❌ 什么都没有（不会自动写） |
| 第二天开新会话 | **自动**知道你是谁 | 要它主动查才知道 | 自动知道（注入） |
| 想删干净 | `--uninstall` | `--uninstall` | 删插件目录 |
| 报 bug 时能提供的 | `control why` + 三个日志 | 同左 | 只有 `bridge.py` 的日志 |

**结论**：Codex 的体验**介于两者之间**——有"自动记"这个最难的半边，缺"开局就说"那半边。
而"开局就说"恰恰是用户感知最强的一半（第一次对话就知道你是谁）。

---

## 6. 可落地的优化项（按性价比排序）

| # | 优化 | 收益 | 成本 | 状态 |
|---|---|---|---|---|
| 1 | **工具面传 `project_id`**（`_build_engine` 之外加一层"按调用解析项目"） | 修掉"记了查不到"——**当前最大的可用性缺陷** | 中（动引擎，需 ADR） | ✅ **2026-11-16 已修**（S14；`ADR-0063` §4.3） |
| 2 | ~~DSH 侧 `project_id` 永远 None~~ | — | — | ✅ **2026-11-16 已修**（`bridge.py --cwd`） |
| 3 | ~~Codex 累积 `input-messages` 重复写入~~ | — | — | ✅ **2026-11-16 已修**（只取最后一条） |
| 4 | **Codex `resume` 的 cwd 漂移** | 防"记忆记到别的项目" | 中（需跨轮状态，而 notify 无状态） | ⚠️ **只做"可见"，不做"纠正"**（S14：落账本 + `why` 显示 `codex_cwd_drift`，`ADR-0066` §6.2 / `ADR-0067`） |
| 5 | ~~Codex 包一层 `notify` 包装脚本~~ | 让本机这种"已有 notify"的机器也能装 | 小 | ✅ **2026-11-16 已做**（`codex_notify_chain.py`） |
| 5b | **`notify` 数据目录注入**（`--data-dir`） | 修"自动写了但工具查不到"（P0） | 小 | ✅ **2026-11-16 已修**（S14；`ADR-0066`）。⚠️ 装过的机器要**重跑一次 `--apply`** 才会带上该参数 |
| 6 | 工具面在 Codex 侧加一句 AGENTS 提示（"开局先查一次记忆"） | 缓解"没有注入" | 小 | 待做 |
| 7 | WorkBuddy 首发配置把 MCP 打开 | 少一个"装了却用不了"的坑 | 极小 | ✅ **2026-11-16 已做**（含剥掉残留 `disabled`） |
| 8 | Codex hooks 配置入口 | S17 面板 | 用户审阅 | 本机 App Server 已验打开面板，画像注入仍沿用既有路径（`ADR-0073`） |

**为什么 1 排第一**：它是唯一一个"三个宿主都受影响、且用户会直接抱怨"的问题。
其余都是单宿主的体验优化。

**4 为什么"未修"而不是"修了"**：`notify` 是一次一个进程、无状态。
要"纠正"漂移就得靠读库猜"上次是哪个项目"，而用户真的换了项目时那个猜测是错的
——**比漂移更糟**（漂移至少原因是可复现的）。所以选择"照它给的记 + 明确提示"。


## 7. S15 对话内记忆管理 · 2026-11-21

| 宿主 | 本机验收入口 | 隔离预验结果 |
|---|---|---|
| WorkBuddy | 安装器配置、随产品发布的 CLI、宿主钩子及实际 MCP 工具调用 | `WORKBUDDY_NATIVE_OK 2`，11 个对话步骤，2 份擦除回执 |
| DeepSeek Harness | 已安装产品的 headless CLI、插件、桥接及实际 MCP 工具调用 | `DSH_NATIVE_OK 2`，11 个对话步骤，2 份擦除回执 |

两边均以独立配置、空白测试库及本地假模型跑写、查、纠错、归档、恢复、取消、确认和擦除。2 份回执分别对应用户明确指令和模型预览经用户确认，均只包含元数据。图形界面、真实模型回答、异机首装和外部试点各有自己的验收口径。

## 8. 原生聊天处置

| 宿主 | 需要加载的入口 | 本机核对方式 |
|---|---|---|
| WorkBuddy | 用户提交钩子和新版技能，普通聊天沿用 Stop | 用安装器刷新旧副本并完全重启。CLI 可执行实际安装的钩子并对账库、来源、历史和回执 |
| DeepSeek Harness | 默认 `profile` 或实验性的 `turn` 插件，主会话原生用户入站 | 更新正在使用的档位并重启。操作结果经必需的轮次上下文交给助手，可在采用完整人格提示的 Pi 模式复验 |
| Codex | 已审阅的 March7 用户提交钩子 | 新旧绑定都可用 `--panel-only` 安装或刷新。CLI 可核对真实轮次和模型收到的结果；受限时说「打开 March7 记忆面板」续办 |

同一聊天中的上一轮选择和删除预览有效十分钟，并绑定项目与条目版本。明确删除唯一目标时直接执行，「忘掉」先预览。模型提议只能在原生删除请求后生成一个单条预览。工具模式与未信任钩子的宿主可从面板管理记忆。WorkBuddy 与 DSH 的 GUI 已在本机复验；Codex 的验收覆盖终端纠错的兼容入口，并记录了两项现场偏差。

## 9. 记忆体检与逐批整理

| 宿主 | 原生入口 | 验收方式 |
|---|---|---|
| WorkBuddy | 用户提交钩子 | 隔离 CLI 核对分组、取消、确认及撤销。装机后用真实桌面核对助手呈现和面板 |
| DeepSeek Harness | `profile` 或 `turn` 的原生用户提交 | 隔离 headless CLI 核对真实消息和回执。桌面另用当前档位复验 |
| Codex | 已审阅的用户提交钩子 | 用户提交和已登记的轮末回调读取本宿主 MCP 的库配置。换库后完整重启再检查。隔离 `codex exec` 及续聊核对同会话授权。 |

三个宿主共用体检、计划、原生用户授权及事务。模型可在聊天中提出同义合并、指定保留与归档或撤销方案，用户在同一聊天核对并带草稿编号确认或取消。面板仍可独立管理。隔离测试先核对 MCP 与钩子绑定一致，DSH 使用带原生入站的档位。桌面实测与复测步骤属研发材料，不随包分发。

本机签收已通过。Codex 和 DSH 有用户桌面聊天回执。WorkBuddy 有原生隔离 CLI 回执，用户桌面另核对了终端逐条归档的结果。
