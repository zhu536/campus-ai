# Day 2 开发记录：项目架构审查

## 1. 本轮 Prompt（逐字原文）

```text
请阅读：

AGENTS.md
docs/PRD.md

现在进行项目架构审查。

不要修改任何代码。

请输出：

1. 推荐目录结构
2. Flask模块划分
3. 9张数据库表
4. 三个核心功能分别需要哪些数据
5. AI应该负责什么
6. Python应该负责什么
7. 数据库应该负责什么
8. ECharts应该负责什么
9. 哪些内容明确不应该做

要求：

必须保持简单。

禁止：

Vue
React
FastAPI
Docker
Redis
Kafka
多智能体
复杂机器学习

不要创建任何文件。
```

## 2. Agent 的回复（原文关键段）

Agent 先通读了 `AGENTS.md` 和 `docs/PRD.md`，检查了当前仓库结构（`backend/`、`frontend/` 下无文件，`prompts/` 为空，只有 `scripts/`），随后给出了架构审查报告。关键结论：

- **推荐目录结构**：`backend/`（Flask 模块）、`frontend/`（静态页面）、`scripts/`（数据生成与导入）、`data/raw/`（CSV）、`data/campus.db`（SQLite）、`tests/`（pytest）、`prompts/`（运行时 Prompt）、`docs/`（文档与 AI 协作记录）。
- **Flask 模块划分**：路由、业务查询函数、参数校验、数据库连接分层，业务查询与路由分离。
- **9 张数据库表**：按三大核心场景（教室 / 食堂 / 产教）落表，包括 classrooms、courses、schedules、canteens、meal_records、jobs、skills、course_skills、job_skills。
- **职责边界**：
  - AI：自然语言意图识别、参数提取、图表选择、结果解释；
  - Python：参数验证、数据查询、业务逻辑、数据计算、预测；
  - 数据库：保存事实数据；
  - ECharts：可视化。
- **明确不做**：LLM 直接生成任意 SQL 并执行、API Key 写入源代码、引入 Vue/React/FastAPI/Docker/Redis/Kafka/多智能体/复杂机器学习。

## 3. 发现的具体问题

审查过程中发现 `.env.example` 里存在真实 API Key（`sk-cf8f...`），违反 AGENTS.md 的密钥政策：

```text
the .env.example file contains what appears to be a real API key starting with
sk-cf8f, violating the AGENTS.md policy against API keys in source code
```

另外 `.gitignore` 只忽略了 `.env`，而 `.env.example` 被 Git 跟踪，导致该 Key 已进入 Git 历史，需要一并清理。

## 4. 改动文件与关键 diff

本轮为纯审查，按要求**没有创建/修改任何文件**（`不要创建任何文件`）。Key 泄露的修复由独立的 Git 提交完成（`98efca5 fix: remove leaked api key from env example`）。

## 5. 真实验证结果

本轮无代码改动、无测试。验证点是仓库状态确认：`git status --short` 无未提交改动，`backend/`、`frontend/`、`tests/`、`prompts/` 为空目录。

## 6. 剩余风险

- 架构方案尚未被实现验证，后续实际开发中可能需要微调（如路由拆分粒度）。
- `.env.example` 的历史提交里仍留有旧 Key 痕迹，Git 历史本身未重写（仅当前工作区已清除），后续如要彻底脱敏可考虑重写历史或更换 Key。
