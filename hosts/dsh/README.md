# hosts/dsh/ — DeepSeek Harness 接入（第二宿主）

> 任务归属：S4（吸收 T24 / T18）。判据与实测：`docs/decisions/0043-DSH契约spike.md`。

## 形态：**B 档（开局注入 + 工具）**

三档的定义与判据**在实测之前就写死在** `ADR-0043` §3。逐条判定结果：

| 判据 | 结果 |
|---|---|
| ① 插件能读到本轮 user message | ✅ `agent/inbox/inserted` 给到原话 |
| ② 注入文本进 prompt | ✅ 模型能复述只有插件才知道的内容 |
| ③ **失败不熔断** | ❌ section 回调抛错 ⇒ `turn_end.kind=error`，该轮无输出 |

三条缺一不可 ⇒ **A 档不成立，落 B 档**。而 B 档要求"会话开始注入"，所以

| `--mode` | 装什么 | 档 |
|---|---|---|
| `profile`（**默认**） | 会话开始注入 L0 画像块 + MCP 工具面；召回靠模型调 `March7_*` | **B**（实测：0 次工具调用答出"我是谁、在哪读书"） |
| `turn` | 每轮按本轮提问召回（实验性） | 不构成 A（熔断判据没过），只能说"实验性每轮注入" |
| `none` | 只装 MCP 工具面 | C（最保守） |

⚠️ 表述分工（`ADR-0043` §3.2）：

| 你说的话 | 允许吗 |
|---|---|
| 「跨工具同一份记忆，开机带上你的背景」 | ✅ B 档口径（默认档就是它） |
| 「要它查的时候能查到」 | ✅ 同一份记忆 + 工具面 |
| 「每轮自动生效 / 自动接力」 | ❌ 判据没过，不许说 |
| 「换了工具它就全自动记得你说的话」 | ❌ 写入在 DSH 侧仍靠工具调用 |

## 装（三步）

```bash
# 1) 预演：打印将要写进 DSH 用户层的补丁（不写盘）
python hosts/dsh/install.py

# 2) 真装（会先备份原文件到 ~/.dsh/_backup/<时间戳>/）
python hosts/dsh/install.py --apply                 # 默认装进 desktop profile（mode=profile）
python hosts/dsh/install.py --apply --mode turn      # 每轮召回（实验性）
python hosts/dsh/install.py --apply --mode none      # 只装工具面

# 3) 完全退出 DSH 再重开（配置在启动时快照，只关窗口不算）
```

默认 `profile` 档在聊天里可以说「开启 March7 记忆面板」或「把 March7 的记忆管理页面调出来」。原生入口打开本档绑定的库，助手用正常聊天消息确认结果。规则外的明确请求及纯工具档可走 `March7_open_panel`。宿主限制本机工具时可双击库旁的 `March7 记忆面板.cmd`。用 `python hosts/dsh/install.py --uninstall` 移除默认档的接入，指定其他档位时同时传 `--profile`。

安装器做三件**必须**做的事，任一条缺失都会变成"看起来接上了"：

1. **数据目录必须解析出来**，否则退出码 2 并停下。
   回落到默认库 `~/.March7` 会**新建一个空生产库**，而 DSH 界面一切正常
   ——这是本卡的头号数据风险（`ADR-0043` §4）。
2. **缺 LLM 密钥就停**（退出码 3）。缺密钥时 MCP **工具照样能列出来**，
   但每次调用都抛 `ConfigError`（实测形态见 `ADR-0043` §1.1 坑 1）。
3. **重装幂等**：补丁里我们那部分被 `# >>> March7 adapter` 标记框住，
   重装只替换框内内容；用户自己写的条目（UI/模型配置）不动。

⚠️ 渲染出来的补丁文件**含密钥**（`MARCH7_LLM_*_API_KEY`），所以：
**只写到 `~/.dsh/` 下、绝不进仓库、不要贴到聊天里**。`install.py` 的默认输出会打印全文，
要机器可读就 `--emit` 并重定向到仓外文件。

## 验（真机命令）

`dsh.cmd` 是批处理，**参数带空格会被 cmd.exe 拆坏**（`ADR-0043` §1.7），
脚本化调用要绕开它：

```bash
EXE="<安装目录>/DeepSeek Harness.exe"
CLI="<安装目录>/resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"

# 只组合配置、不跑模型、不花钱
ELECTRON_RUN_AS_NODE=1 "$EXE" --expose-internals "$CLI" \
  --profile headless --patch <补丁> --dump-config

# 真跑一轮，看 --json 事件流里的 tool_call / tool_result / usage
ELECTRON_RUN_AS_NODE=1 "$EXE" --expose-internals "$CLI" \
  --profile headless --patch <补丁> --json "调用 March7_status，把 schema_version 报出来"
```

**握手判据不是"工具列出来"**：要 `March7_status` 回 `schema_version`（stdio 双工通了）
且 `March7_recall` 回带 `source_quote` 的真实记忆（数据目录指向同一个库）。

## 四个文件

| 文件 | 干什么 | 不干什么 |
|---|---|---|
| `bridge.py` | 注入文本：`--mode profile` 画像或 `--mode recall` 召回；原生主会话入站处理明确补记和处置 | 普通注入只读。明确补记使用安装配置的判定模型 |

默认 `profile` 档可直接说「帮我记住，我用蓝册记录演练步骤」「查一下蓝册」或「忘掉这条」。收到永久删除预览后可在同一会话回复「确认删除」或「取消删除」。升级时对正在使用的档位重新运行安装器并重启。使用 `none` 档可通过本机记忆面板处置。

Pi 模式下，用户入站产生的处置结果通过必需的轮次上下文送达助手。可以先查目标，再说「把这条改成『新事实』」，或明确引用旧正文。以当轮回复和面板里的当前版本、历史或删除回执核对结果。
| `plugin/index.mjs` | DSH 侧的 Cordis 插件：`inbox/inserted` → section 回调 → 渲染 | **不挂 `agent/pre-step`**（那是 waterfall，返回 undefined 会打断整轮） |
| `march7.patch.yml.in` | 补丁模板（`@PYTHON@` / `@DATA_DIR@` / `@PLUGIN@` / `@LLM_ENV@`） | 不含任何真实路径或密钥 |
| `install.py` | 渲染 + 预演/落盘 + 备份 + 幂等 | 不碰 `app.asar` / `runtime/` / 会话数据 |

## S8 真机实测结果（2026-11-12）

**A2 ✅ 四段全在宿主进程内**，证据 `evals/reports/s8-evidence/`。

| 段 | 谁在调 | 动作 | 结果 |
|---|---|---|---|
| ① | **WorkBuddy 宿主** | `March7_write`「办公室抽屉备用钥匙 TX-9021」 | 1 条，`host=workbuddy` |
| ② | **DSH 宿主** | `March7_recall("备用钥匙 抽屉 编号")` | 读出①，`host` 回显 `workbuddy` |
| ③ | **DSH 宿主** | `March7_write`「《数据密集型应用系统设计》2024 年第二版」 | 1 条，`host=dsh` |
| ④ | **WorkBuddy 宿主** | `March7_recall("书架上那本数据密集型应用系统设计")` | 读出③，`host` 回显 `dsh` |

> ⚠️ **11-12 白天那版不算数**：当时宿主跑的还是旧代码（`ADR-0047` 的跨线程
> 问题未修），WorkBuddy 侧那两段只能由**脚本复刻 `mcp.json`** 直连完成。
> 同命令同 env 同库，但**不在宿主进程内**——配置一致 ≠ 宿主内调用。
> 重启宿主后已全部重做。

⚠️ **实测踩到的两个坑，装的时候会重复遇到**：

1. **`env` 里必须显式写全 9 个 `MARCH7_LLM_*`**。`dsh-mcp-client` 的 `env` 是
   **替换**不是追加（`ADR-0043` §1.1 坑 1），而 WorkBuddy 那边是**追加**——
   两边语义不同，照抄 WorkBuddy 的 3 个键会让 DSH 侧每次调用抛 `ConfigError`。
   `install.py` 已按替换语义处理（从注册表读 9 个变量写全）。
2. **`dsh.cmd` 吞带空格的路径**（`ADR-0043` §1.7）。脚本化调用必须绕开它：

```bash
EXE="C:/Users/<user>/AppData/Local/Programs/DeepSeek Harness/DeepSeek Harness.exe"
CLI="$EXE_DIR/resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"
ELECTRON_RUN_AS_NODE=1 "$EXE" --expose-internals "$CLI" --profile headless --patch <patch> --json "<prompt>"
```

> `app.asar` 是**打包文件**，`ls` 进不去，但 Electron 自己能加载 asar 内路径。
> 别因为 `ls` 报 "Not a directory" 就以为路径错了。

## 已知边界（别当成已解决）

- **`desktop` profile 里的效果没验过**：CLI 对 desktop 拒绝 `--dump-config`
  （Electron 独占），所以 S4 的全部验收都在 `headless` 上做。装进 desktop 后
  要人眼确认一次。
- **写侧没有自动通道**：DSH 有 `agent/turn-stopping`（serial），但我们**没接**——
  接 Judge 管线还要验"它拿到的是不是同一条消息、水位线怎么记"，属后续卡。
  现在写记忆靠模型主动调 `March7_write`。
- **注入原文不可见**：DSH 的 `--json` 事件流不含 system prompt，只能靠
  "模型复述"或 token 差判断。WorkBuddy 侧能从转写复核，DSH 侧不能。
- **注入有延迟**：插件每轮 `spawnSync` 一个 python 进程（实测 0.9–1.3s，样本 1 次），
  超时 4s 后放弃本次注入。
- 两个宿主**同时高频写**的压力没测过；A4 只验了"断开后另一个仍能 recall"。
