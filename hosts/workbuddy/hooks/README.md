# hosts/workbuddy/hooks/ — 钩子脚本（T14 交付）

> **任务归属**：T14（WorkBuddy 接入）。事实依据：T3 探针五项契约验证
> （`evals/reports/spike-hooks-memo.md`）+ 2026-10-20 真机实测。
> **安装方式**：`python hosts/workbuddy/install.py`（复制四个脚本 + 装 SKILL.md +
> 注入 `~/.workbuddy/settings.json` 的 `hooks` 块；`--status` 只看状态，
> `--uninstall` 还原且**只移走不删**）。**改完必须完全退出 WorkBuddy 再重开。**

## 三通道职责

| 通道 | 事件 | 作用 | 确定性 |
|---|---|---|---|
| 钩子 | `SessionStart` | 新会话注入 L0 画像 | 每会话必触发（`startup` + `resume`） |
| 钩子 | `UserPromptSubmit` | 按本轮 prompt 召回记忆 → `additionalContext` | **每轮必触发** |
| 钩子 | `Stop` | 读 `transcript_path` → 引擎跑 Judge → 写库 | 每轮必触发（与上条 1:1） |

MCP 工具（模型主动）与 SKILL.md（指令式）作为兜底通道并存。

## 四个脚本

| 文件 | 职责 |
|---|---|
| `_common.py` | 协议外壳 + 纯逻辑（**不 import March7**，任何解释器都能加载） |
| `session_start.py` | 读 `profile.json` → 注入 L0 画像（**只读**，天然幂等） |
| `user_prompt_submit.py` | 纯 BM25 **OR** 召回（FTS5 MATCH 是隐式 AND，整句提问会查不中） |
| `stop.py` | 读转写 → 取当前分支 → 按轮组装「对话转写」→ 调 `server.March7_write` → 记水位线 |

**为什么召回走纯 BM25**：钩子是独立进程，走混合检索等于**每轮重载 BGE-M3**
（实测 7.5s + 数 GB 显存），宿主会卡到不可用。纯 BM25 走 FTS5 索引，毫秒级。

**为什么 Stop 复用 `server.March7_write`**：与 MCP 工具面**同一条代码路径** ⇒
硬过滤 / 画像护栏 / 来源落库的行为**不可能分叉**。

## 数据目录：与 MCP server 同源（不可能分叉）

`MARCH7_HOME` 写在 `~/.workbuddy/mcp.json` 的 server env 里——**只对 MCP server 生效**，
钩子进程拿不到 ⇒ 两边会落到不同的库（钩子默认 `~/.March7`、server 用 `.March7-dev`）。
那种分叉的代价是**静默写错库**：钩子写进生产库、server 读演练库，界面却「看起来一切正常」。

安装副本按 **宿主 `mcp.json` → 进程环境 → 引擎默认 `~/.March7`** 查找，
并回写 `MARCH7_HOME`，让工具和钩子读写同一个库。直接运行仓库里的钩子时，进程环境优先。
Stop 的安装入口也从同一份 `mcp.json` 读取 March7 模型凭据。
**读同一份配置 ⇒ 钩子与 server 不可能分叉**。

⚠️ **这条保证的边界（T14 验收 P1-3，2026-10-23）**：**它只覆盖钩子进程，不覆盖手工启动的引擎**。
任何绕过宿主启动的路径——`mcp dev src/March7/server.py`、手写探针、`python -m March7`——
在没有显式设置 `MARCH7_HOME` 时，`config.load_config()` 仍然**只看环境变量**
⇒ **一律落默认生产库 `~/.March7`**。
**这不是假设**：`~/.March7/March7.db` 就是 2026-10-20 15:01:40 这样被创建出来的
（同一分钟 `hooks-20261020.jsonl` 零事件 ⇒ 不是钩子写的）。
⇒ **做任何实验前先 `export MARCH7_HOME=~/.March7-dev`**，别指望钩子那套护栏替你兜住。

## 审计日志

落 `<data_dir>/logs/hooks-YYYYMMDD.jsonl`（**stdout/stderr 是宿主协议通道，不能当日志**）。

| event | 含义 |
|---|---|
| `session_start_injected` | 画像已注入（含 `chars` / `fields`） |
| `ups_injected` / `ups_no_hit` | 每轮召回的结果（`hits` / `chars`） |
| `stop_ingested` | 写入完成（含 `written` / `updated_l0` / `rejected` / **`rejected_reasons`**） |
| `stop_extraction_failed` | **LLM 调用失败** ⇒ 本轮不写、**不打水位线**，下一轮自动重试 |
| `stop_no_user_message` / `stop_no_transcript` | 转写里没找到可处理的消息（已留痕，不静默） |

⚠️ **「没写进去」有两种长得一样的形态**：① Judge 判定无需记；② LLM 调用失败。
两者在 `WriteResult` 里都是四个字段全空 ⇒ 靠 `server.last_extraction_failed()`（只读观测位）区分。
不区分的后果**实测发生过**：模型收到空结果以为记忆库坏了，转而自己建了
`.workbuddy/memory/MEMORY.md` ⇒ **失败变成静默失败**。

## 九个坑（实现时逐条对照）

> 标题原写「十个坑」，实际是 6（CONVENTIONS §5）+ 3（T14 真机）= **9 条**
> ——2026-10-23 T14 验收 P3-1 更正，不是漏了一条，是标题数错了。

**CONVENTIONS §5 的六个坑**（原文照录）：

1. 仅接受 `type:"command"`；2. 失败路径必须 exit 0（`exit 2` = 整轮熔断 ⇒ 无 Stop ⇒ 写入闭环断）；
3. Windows 强制 Git Bash + 路径引号；4. Python 强制 UTF-8 stdout；5. 配置启动时快照（完全退出重开）；
6. 树状转写解析。

**T14 真机新坑三个**（2026-10-20 实测，全部是「静默失效」型）：

7. **载荷可能带 UTF-8 BOM**（实测还是**双 BOM**）⇒ `json.loads` 抛错 ⇒ **整个载荷被丢弃** ⇒
   钩子「跑了但什么都没做」。⇒ 解码用 `utf-8-sig` + `lstrip("\ufeff")` 兜多个 BOM。
8. **宿主把自己的上下文塞进用户消息**：一条用户消息的 content 实测 **6882 字符**，
   其中只有末尾 19 个字符是用户真正说的话，前面全是宿主注入的
   `<system-reminder>`（SOUL.md / IDENTITY.md / USER.md / memory 提示）。
   不剥掉 ⇒ **把宿主的 USER.md 当成「用户原话」喂给 Judge**（来源却标成用户原话）。
   ⇒ 只取 `<user_query>…</user_query>` 里的正文。
9. **兄弟节点冒充叶子**：转写里 `session-meta` / `ai-title` / `file-history-snapshot`
   **没有 `parentId`**（不属于对话树）却**时间戳更晚** ⇒ 「最新叶子回溯」会选中 `ai-title`
   ⇒ 分支里一条用户消息都没有 ⇒ Stop 静默不写。⇒ 叶子只认 `type == "message"`。

## ⚠️ Stop 的固定成本：每轮冷启动 BGE-M3 ≈14s（T14 验收 P2-2 实测）

钩子是**独立进程**，而 `Stop` 走的 `March7_write → process_transcript` 需要嵌入
⇒ **每一轮对话结束都要重新加载 4.3GB 编码器**。2026-10-23 实测一轮完整 Stop = **27s**：

```
[14:17:57] INFO  已注册 8 个工具（March7_*）
[14:18:09] INFO  Loading SentenceTransformer model from BAAI/bge-m3.
[14:18:21] INFO  编码模型就绪：dim=1024 device=cuda  耗时=14.4s
[14:18:24] INFO  写入管线完成（host=workbuddy, conversation_id=s1）：候选 2 条 → L2 写入 1 条 / L0 1 个
```

- 这解释了「为什么用户会感觉卡」：**14.4s 是固定开销**，再叠加 LLM 的 2–40s；
- 本文档上游那句「UPS 不走向量检索，因为每轮重载 BGE-M3（7.5s）」只解释了
  **为什么召回侧不用向量**，**没有**解释 Stop 正在每轮付这笔钱；
- 已做加固：写入失败不写水位线（下一轮自动重试）+ 宿主侧 `Stop.timeout=120`；
- ✅ **T20 已处理（2026-11-06，`ADR-0036`）**：`server._build_engine()` 改用
  `LazyEmbedder` ⇒ 只有真正要编码的轮次才加载权重（真机 111/124 轮 `written=0`，
  以前这些轮次白付 14.4s）；单条 LLM 超时在钩子进程收紧到 20s（引擎默认 60s 不变），
  Stop 内部硬截止 45s，超时记 `stop_deadline_exceeded` 且**不打水位线**。
  `scripts/doctor.py` 现在会直接打印近 7 天延迟分位，可复算。
- ✅ **已部署**：4 份脚本与仓库**逐字节一致**；
  `settings.json` 报 `already-current`（命令串没变）⇒ 这次**不需要再重启**
  （钩子每次起新进程读磁盘，替换文件即生效）。doctor 的「钩子预算 N/M」要等
  **下一个 Stop 事件**才翻数，旧日志的 111 轮永远是旧副本读数。
- ⚠️ **改了仓库副本 ≠ 生效**：必须用 `hosts/workbuddy/install.py` 把 4 个钩子脚本 +
  `SKILL.md` 同步到 `~/.workbuddy/`。若 `settings.json` 被改过（报 `insert` / `replace`），
  还要**完全退出 WorkBuddy 重开**（配置启动快照）。
  ⚠️ **但要部署的只是「钩子侧」**：`src/March7` 的改动（`LazyEmbedder`、超时读取）走
  editable 包**即时生效**，不改宿主也已经在跑；只有本目录的 `_common.py` / `stop.py`
  （20s 超时 + 45s 截止）需要 `install.py`。判据看 `doctor.py` 的
  「钩子预算 N/M 轮」（`stop_ingested.deadline_ms` 标记），**不要**用延迟高低推断。

## 已有资产

- **T3 探针源码归档**（不在本仓库，勿照抄里面的路径）：文档层
  `March7-懂你的AI搭子/06-钩子spike/probe-archive-20260929/`（`hook_probe.py` = 探针，
  `t3_inject.py` = `settings.json` 注入/还原的参考实现）。
  ⚠️ 旧版 `hooks/README.md` 写的是**别人机器上的用户目录路径**
  （`<另一个用户名>/.workbuddy/hooks/hook_probe.py`），本机不存在——已修正。
- 探针配置片段：文档层 `spike/settings-hooks-探针片段.json`；
- spike 五项契约验证 = T3（M1 W1），结论写 `evals/reports/spike-hooks-memo.md`。
