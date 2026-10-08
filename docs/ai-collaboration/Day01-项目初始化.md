# Day 1 开发记录：项目初始化与首次 AI 协作

## 1. 本轮 Prompt（逐字原文）

初始化阶段没有独立会话，Git 历史保留了当时的工作内容。首次 Harness 协作的发起 Prompt 如下：

```text
请阅读：

AGENTS.md
docs/PRD.md

现在设计CampusAI数据库。

数据库使用SQLite。

固定9张表：

1. classrooms
2. courses
3. schedules
4. canteens
5. meal_records
6. jobs
7. skills
8. course_skills
9. job_skills
```

说明：本轮发起后 Agent 只进入过一次 attempt（读取文档阶段），回合未完成、没有产出文件。正式的数据库表设计在后续 Day 3 完成（见 `Day03-数据库表设计.md`）。

## 2. Agent 的回复（原文关键段）

本轮回合未完成，无有效回复文本。当日工作以 Git 提交的形式落地：

```text
398e797 chore: initialize project
1d3f44b docs: add agent instructions       （新增 AGENTS.md）
e29096b docs: add product requirements     （新增 docs/PRD.md）
98efca5 fix: remove leaked api key from env example
```

## 3. 发现的具体问题

- `.env.example` 中写入过真实 API Key（`sk-cf8f...`），违反 AGENTS.md 中"API Key 不得写入源代码"的规定。已通过提交 `98efca5` 移除。
- 项目目录结构当时为空，`backend/`、`frontend/`、`tests/`、`prompts/` 均无文件，仅 `scripts/` 有测试脚本。后续开发顺序需要先定架构、再建表、再填数据。

## 4. 改动文件

| 文件 | 动作 | 提交 |
|---|---|---|
| `AGENTS.md` | 新增（项目规约：技术栈、工程规则、AI/Python/数据库职责边界） | `1d3f44b` |
| `docs/PRD.md` | 新增（产品需求：三大核心功能） | `e29096b` |
| `.env.example` | 修复（移除泄露的真实 Key，改为占位符） | `98efca5` |

## 5. 真实验证结果

初始化阶段无测试。`git log` 验证提交链存在：

```text
git log --oneline --reverse
398e797 chore: initialize project
1d3f44b docs: add agent instructions
e29096b docs: add product requirements
98efca5 fix: remove leaked api key from env example
```

## 6. 剩余风险

- 数据库表结构、架构模块划分尚未定稿，存在返工可能。
- 首轮 AI 协作回合未完成，说明对"一次一个明确任务"的约束需要在后续 Prompt 里写得更具体。
