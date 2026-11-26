<h1 align="center">March7</h1>
<p align="center"><strong>换个 AI 搭子，不用从头讲。</strong></p>
<p align="center">
  <a href="用户手册.html">用户手册</a> ·
  <a href="AI-SETUP.md">让 AI 帮你装</a> ·
  <a href="docs/ONBOARDING.md">安装指引</a> ·
  <a href="docs/FEEDBACK.md">试用反馈</a>
</p>

**你是被用户叫来安装的 AI 助手？** 直接读 [`AI-SETUP.md`](AI-SETUP.md)：命令、期望输出、退出码和验收清单都在里面。

March7 是一个给 AI 助手共用的本地记忆插件。你在一个工具里说过的偏好或项目决定，可以在同一台电脑的另一个工具里找回。召回结果可以附上来源原话，方便核对和纠错。

| 电脑 | 安装情况 |
|---|---|
| Windows 10 / 11 x64 | 已验证本机宿主，无 NVIDIA 冷装待验 |
| macOS Apple Silicon | macOS 15 ARM64 原生冷装与引擎已验证，桌面宿主待验 |
| macOS Intel、Windows ARM 或 32 位 | 当前锁文件缺少匹配的 PyTorch 安装包，测试包装不上 |
| Linux x64 | 未验证，适配延后 |

## 你可以用它做什么

| 想做的事 | 可以这样开始 |
|---|---|
| 记住长期偏好 | 「我希望周报先列结论，再列细节。」 |
| 换工具继续同一个项目 | 「用 March7 查这个项目上次确认了什么，给我来源原话。」 |
| 查找、检查和更正 | 用关键词找回记忆。在同一聊天说「把这条改成……」或「把这条归档」，也可以打开面板核对原话和历史。 |
| 授权擦除、核对回执 | 给出明确目标，或先看擦除预览再确认。回执可查，已擦除的正文不会写进回执。 |
| 把记忆带走 | 导出为 Markdown 或 JSON，在本机查看。 |

## 支持的工具

| 工具 | 怎么记 | 怎么用 |
|---|---|---|
| **WorkBuddy** | 每轮结束尝试自动写入 | 新会话可带入个人背景，具体事项可以主动查 |
| **Codex** | 接入写入功能后，每轮结束尝试写入 | 可查找。纠错和删除时核对库、历史与回执，必要时用面板 |
| **DeepSeek Harness** | 加载插件后可在聊天中明确补记 | 可查找、纠错和授权删除，桌面 Pi 模式已实测 |

WorkBuddy 和 DeepSeek Harness 的对话内记忆管理已完成本机 GUI 验收。项目事实正确分类并取得项目身份后按项目隔离。因工作目录和宿主行为不同，接力前请核对项目与数据目录。见[宿主能力与证据](docs/HOST-MATRIX.md)。

## 快速开始

1. 使用 **Windows 10 / 11 x64**，准备约 **8 GB** 空间和可以配置给 March7 的模型账号。
2. 从 [Releases](https://github.com/DavidLearning1023/March7-release/releases) 下载 zip 解压，或 `git clone` 本仓库；在解压目录按[安装指引](docs/ONBOARDING.md)完成依赖、模型配置和 WorkBuddy 接入。
3. 完全退出并重开 WorkBuddy，首次连接时对 March7 点「信任」。
4. 用一句低敏事实试写入，在新会话里请它用 March7 查找并给出原话。

双击解压后的 `用户手册.html` 即可离线阅读（也可以让 AI 按 `AI-SETUP.md` 帮你装）。在已接入的宿主里说「打开 March7 记忆面板」，可按权限纠正、归档、恢复或永久擦除单条记忆。终端的只读视图见手册。

## 隐私与试用状态

记忆文件存放在本机。配置模型判断后，相关对话内容会发送给所选模型服务。召回内容也可能随宿主对话发给宿主使用的模型。擦除回执说明 March7 能覆盖的范围，宿主原对话、已导出文件、备份及供应商日志需分别处理。见[数据与擦除说明](SECURITY.md)。

本地测试版已验收记忆体检和逐批整理，并在无 NVIDIA 的 Windows 与 macOS Apple Silicon 上完成冷装验证。之后才进入自用签收与外部试点。见[宿主支持](docs/HOST-MATRIX.md) · [已知局限](docs/LIMITATIONS.md)。

REAL-30 是单用户的固定评测，完整记忆在这批题上的答对率低于模型代写的交接笔记。完整口径与误差带见[已知局限](docs/LIMITATIONS.md)。

<details>
<summary><strong>技术要点</strong></summary>

- 纯 Python 记忆引擎、本地 SQLite、中文混合检索，通过 MCP 或宿主适配层接入。
- 安装开发依赖：`uv sync`。测试环境使用隔离库与合成数据。
- 任务板、决策记录与评测报告属于研发材料，**不随包分发**；本包只带安装、使用、安全与已知局限四类文档。

</details>

## 许可证与致谢

[MIT](LICENSE) © 2026 XU Zhongrong · David Hui。记忆写入判断提示词改写自 Cyrene-Agent，沿用其 MIT 声明。见[第三方声明](THIRD_PARTY_NOTICES.md)。

项目名称致意《崩坏：星穹铁道》的三月七。本项目独立开发，未使用游戏素材，与 HoYoverse / miHoYo 无官方关系。
