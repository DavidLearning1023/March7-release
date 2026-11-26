// index.mjs — DSH 的注入插件。两种模式，由补丁条目的 `config.mode` 决定：
//   profile —— **会话背景**（L0 画像），整轮会话只取一次、之后复用 ⇒ 这就是 B 档
//   turn    —— **每轮按本轮提问召回**（实验性；见下面"为什么默认不是它"）
//
// 为什么默认是 profile 而不是 turn（判据先写死，`ADR-0043` §3）：
// A 档（自动注入）要求"失败不熔断"，实测 **section 回调抛错会让整轮 turn_end.kind=error**。
// B 档的定义是"只能在会话开始注入（画像/项目块），召回靠模型调 March7_*"。
// 所以默认行为必须是"背景一次 + 工具召回"，而 turn 模式只能作为 opt-in 的实验性增强。
//
// 为什么是插件而不是钩子脚本：DSH 没有 WorkBuddy 那种「外部进程 + stdout 回注」
// 的钩子通道（`ADR-0043` §1.2 实测：hooks 桥接插件未随发行版安装）。DSH 的注入点是
// **进程内 seam**：`ctx.systemPrompt.section()` 注册的段落会在**每个 model step**
// 重新求值（`assemble()` 每步一次），所以段落回调里读闭包状态就能"取一次、之后复用"。
//
// 三道防线（任何一条破了都只是"不注入"，不会让这一轮失败）：
//   1. 入站通知执行明确请求，必需的轮次上下文把结果交给模型。
//   2. 桥调用 `spawnSync` + 超时 + try/catch；超时/崩溃/空输出 ⇒ 返回空串。
//   3. 段落文本**只在有内容时**返回；空串会被组装层丢掉（不会留下一段空标题）。
//
// 数据来源：`~/.dsh/march7-adapter.json`（由 `hosts/dsh/install.py` 写出），
// 里面只有三样东西：python 路径、bridge.py 路径、数据目录。**不含密钥**——
// 注入是只读查询，不需要 LLM。

import { spawnSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

export const name = 'march7-inject'

const SECTION_ORDER = 12000 // 在第一方 persona 后缀（10200）之后
const BRIDGE_TIMEOUT_MS = 4000 // 单次预算；超时=不注入，不拖慢这一轮
const INBOX_TIMEOUT_MS = 90000 // 明确补记可能需要判定与编码
const MAX_INJECTED_CHARS = 1600 // 与引擎 injection 的总上界同量级，防插件侧超发

function readConfig() {
  const override = (process.env.MARCH7_DSH_CONFIG || '').trim()
  const home = (process.env.DSH_HOME || '').trim() || join(homedir(), '.dsh')
  const path = override || join(home, 'march7-adapter.json')
  try {
    return JSON.parse(readFileSync(path, 'utf8'))
  } catch {
    return {}
  }
}

function textOf(message) {
  const content = message && message.content
  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    return content.map((b) => (b && typeof b.text === 'string' ? b.text : '')).join('\n')
  }
  return ''
}

export function apply(ctx, config) {
  const cfg = config || {}
  const mode = (cfg.mode || 'profile').trim()
  const adapter = readConfig()
  const python = (cfg.python || process.env.MARCH7_DSH_PYTHON || adapter.python || '').trim()
  const bridge = (cfg.bridge || process.env.MARCH7_DSH_BRIDGE || adapter.bridge || '').trim()
  const dataDir = (cfg.dataDir || '').trim()
  const modelEnv = Object.fromEntries(Object.entries(cfg.modelEnv || {}).filter(
    ([key, value]) => key.startsWith('MARCH7_LLM_') && typeof value === 'string',
  ))

  if (mode === 'none' || !python || !bridge) {
    // 没配置就**什么都不注册**：宁可"不注入"，也不要每轮 spawn 一个必败的进程。
    return
  }

  let current = '' // 本轮用户原话（仅 turn 模式用到，由 inbox 通知更新）
  let profileCache = null // 同一原生用户轮内复用，包括空画像。

  function runBridge(args, stdin, cwd = process.cwd()) {
    try {
      // `--cwd` 让 bridge 能算出 project_id（ADR-0063 §4.3）。
      // 不传也能跑，但那时只能召回 global 记忆 —— 项目里的记忆一条都看不到
      // （这正是 S14 要修的"记了查不到"）。
      // 用 `process.cwd()` 而不是环境变量：实测 `PWD` 是脏的（ADR-0063 §2.2）。
      const result = spawnSync(python, [bridge, '--cwd', cwd, ...args], {
        input: stdin || '',
        encoding: 'utf8',
        timeout: args.includes('attest-source') ? INBOX_TIMEOUT_MS : BRIDGE_TIMEOUT_MS,
        windowsHide: true,
        env: { ...process.env, ...modelEnv, PYTHONUTF8: '1', ...(dataDir ? { MARCH7_HOME: dataDir } : {}) },
      })
      const out = (result && typeof result.stdout === 'string' ? result.stdout : '').trim()
      return out.slice(0, out.startsWith('operation:') ? 4000 : MAX_INJECTED_CHARS)
    } catch {
      return ''
    }
  }

  // 只在主会话的原生用户入站跑处置。子代理委派也可能带 user source。
  const results = new WeakMap()
  const sourceHints = new WeakMap()
  try {
    ctx.on('agent/inbox/claimed', ({ agent, message }) => {
      const header = agent?.session?.header
      if (!header?.id || header.parentSession || header.origin === 'subagent' || header.delegationDepth > 0) return
      results.delete(agent)
      sourceHints.delete(agent)
      if (message?.role !== 'user' || message.source?.kind !== 'user') return
      profileCache = null
      const singleText = Array.isArray(message.content) && message.content.length === 1
        && message.content[0]?.type === 'text'
      const prompt = singleText ? textOf(message) : ''
      const output = runBridge(['--mode', 'attest-source', '--dialogue'], JSON.stringify({
        prompt, session_id: header.id, message_id: message.id,
      }), header.cwd || '')
      if (output.startsWith('operation:')) {
        results.set(agent, output.slice('operation:'.length))
      } else if ((output === 'ok' || output.startsWith('ok:')) && message.id) {
        sourceHints.set(agent, `【March7 对话管理】纠错和删除由原生用户消息触发。`
          + `用户要纠错或删除却没有操作结果时，可请用户先查目标，再说「把这条改成『新事实』」`
          + `或「忘掉这条」。也可以打开记忆面板续办。以当轮实际结果为准。\n`
          + `【March7 补记来源】仅在用户明确要求补记时使用其完整原话，`
          + `conversation_id=${header.id}，source_message_id=${message.id}。审阅较早消息时用其原有来源。`
          + (output.startsWith('ok:') ? '\n' + output.slice(3) : ''))
      } else if (output.startsWith('dialogue:')) {
        sourceHints.set(agent, output.slice('dialogue:'.length))
      } else if (output.startsWith('panel:')) {
        try {
          const panel = JSON.parse(output.slice(6))
          if (typeof panel?.message === 'string' && panel.message) {
            results.set(agent, panel.message)
          }
        } catch {
          // 旧版或中断的桥输出不能阻断宿主。
        }
      }
    })
    const operation = {
      name: 'March7:operation',
      order: SECTION_ORDER + 1,
      interpolate: false,
      text: ({scope} = {}) => results.get(scope) || sourceHints.get(scope) || '',
    }
    // 完整人格提示会替换普通段落，部分预设还会过滤可选的轮次上下文。
    if (typeof ctx.systemPrompt.context === 'function') {
      ctx.systemPrompt.context({ ...operation, required: true })
    } else {
      ctx.systemPrompt.section(operation)
    }
  } catch {
    // 旧版宿主若没有原生事件，保留只读注入。
  }

  if (mode === 'turn') {
    try {
      ctx.on('agent/inbox/inserted', ({ message }) => {
        try {
          if (!message || message.role !== 'user') return
          const text = textOf(message).trim()
          if (text) current = text
        } catch {
          // 捕获失败 = 本轮不注入，不影响这一轮
        }
      })
    } catch {
      return
    }
  }

  try {
    ctx.systemPrompt.section({
      name: 'march7:memory',
      order: SECTION_ORDER,
      text: ({scope} = {}) => {
        try {
          if (results.has(scope)) return ''
          if (mode === 'turn') {
            if (!current) return ''
            const header = scope?.session?.header
            return runBridge(['--mode', 'recall'], JSON.stringify({
              prompt: current, top_k: 5, session_id: header?.id || '',
            }), header?.cwd ?? process.cwd())
          }
          if (profileCache === null) profileCache = runBridge(['--mode', 'profile'], '')
          return profileCache
        } catch {
          return ''
        }
      },
    })
  } catch {
    // 注册失败 = 本宿主退化成"纯工具"档，不影响 MCP 工具面
  }
}
