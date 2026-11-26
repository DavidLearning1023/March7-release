# Third-Party Notices

本项目的部分设计改写自下列开源项目。按各自许可证要求，保留其版权声明。

---

## Cyrene-Agent

- 来源：`D:\Cyrene-Agent-master`（本地参考副本，v1.2.2）
- 许可证：**MIT License**
- 版权：Copyright (c) 2026 Playa

**本项目使用其哪些内容（派生关系说明）**：

| 本项目产物 | 派生自（Cyrene-Agent 文件） | 派生方式 |
|---|---|---|
| `src/March7/judge.py` 的写入判断 prompt 与四条硬过滤规则 | `src/main/memory/memory-judge.ts` | 改写（TypeScript → Python），保留「保守写入」「宁可不记不可误记」的设计意图与规则结构 |
| `src/March7/schemas.py` 的记忆字段设计 | `src/main/memory/memory-types.ts` | 字段对齐（L0Profile / L2Memory），去掉 Electron / ragId 耦合 |
| `src/March7/conflict.py` 的冲突四分类 | `src/main/memory/memory-conflict.ts` | 概念沿用（unrelated / context_difference / preference_evolution / direct_conflict） |
| `src/March7/retriever.py`（M3）的激活度机制 | `src/main/memory/l2-dmae-manager.ts` + DMAE V5 设计文档 | 方法论借鉴（M3 实验变量，尚未实现） |

**许可证全文**（MIT，按 MIT 要求随本项目一并保留）：

```
MIT License

Copyright (c) 2026 Playa

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 其他参考（未复制代码，仅借鉴架构思路）

| 项目 | 许可证 | 借鉴内容 |
|---|---|---|
| PLUR（github.com/plur-ai/plur） | Apache-2.0 | 混合检索（BM25 + 向量 + RRF）架构思路、scope 命名空间设计、开放格式导出的产品叙事 |
| mem0（github.com/mem0ai） | Apache-2.0 | 评测 harness 骨架与 LongMemEval / LoCoMo 横评方法论（fork 后修改，届时另行登记） |
| mcp-memory-service | （接入前核对） | 同技术栈（Python + sqlite-vec）的 MCP 工具面设计参考 |
| Model Context Protocol Python SDK | MIT | 工具面 SDK 依赖 |

> 上述项目**未在其许可证要求之外复制任何代码**；若后续 fork（如 mem0 的 benchmarks 仓库），需在本文件登记具体提交与许可证全文。

---

## 命名致敬声明

项目名 `March7` 取自《崩坏：星穹铁道》角色三月七（March 7th）。

本项目为独立开源作品，**与 HoYoverse / miHoYo 无任何隶属、赞助或背书关系**，且**不使用任何游戏美术素材、音频、字体或标识**——仅为文字层面的命名致敬。
