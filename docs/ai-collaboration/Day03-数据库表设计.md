# Day 3 开发记录：SQLite 数据库表设计

## 1. 本轮 Prompt（逐字原文）

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

要求：

对每张表输出：

- 字段名
- 数据类型
- 主键
- 外键
- 是否允许为空
- 字段含义

然后说明：

1. 教室查询如何使用这些表
2. 食堂分析如何使用这些表
3. 产教融合如何使用这些表

最后输出完整CREATE TABLE SQL。

不要修改项目代码。
```

## 2. Agent 的回复（原文关键段）

Agent 通读文档后，将完整设计写入 `docs/DATABASE.md`（文档形式交付，未修改任何项目代码），并在**临时 SQLite 库**中真实执行验证。回复原文关键段：

```text
验证通过：这段 DDL 在临时 SQLite 库中真实执行成功，9 张表 / 9 个索引全部创建，
PRAGMA foreign_key_check 为空，三大场景的查询语句也能正常解析（临时库已删除，未写入项目）。
git status 显示唯一改动是新增 docs\DATABASE.md，没有修改任何项目代码。
```

设计约定（写入 DATABASE.md）：

- 数据库文件：`data/campus.db`；
- 全局约定：主键统一 `INTEGER PRIMARY KEY AUTOINCREMENT`；布尔用 `INTEGER` + `CHECK(x IN (0,1))`；日期 `TEXT 'YYYY-MM-DD'`、时间 `TEXT 'HH:MM'`；枚举用 `TEXT/INTEGER` + `CHECK`；
- 一个学期一份数据：`schedules.semester` 用于区分学期，查询时由 Python 固定传入；
- 事实数据入库，计算与预测放在 Python。

## 3. 发现的具体问题

- 用临时脚本提取文档中的 DDL 时，PowerShell 的 `-split` 正则把 Markdown 代码围栏处理错了，导致第一次取到的是文档里的"部分 SQL 片段"而非完整 CREATE TABLE。修正为**提取最后一个 SQL 代码块**后验证通过。
- 需要确认文档中是否还有第二个 SQL 块（示例片段），最终统一以"最后一个代码块"作为权威 DDL。

## 4. 改动文件

| 文件 | 动作 |
|---|---|
| `docs/DATABASE.md` | 新增（9 张表的字段/类型/主键/外键/含义 + 三大场景查询说明 + 完整 CREATE TABLE SQL） |

Git 提交：`f4e13c7 docs: define database schema`

## 5. 真实验证结果（临时库执行，未写入项目）

```text
- 9 张表 / 9 个索引全部创建
- PRAGMA foreign_key_check 为空（外键完整）
- 三大场景（教室/食堂/产教）的查询语句解析正常
- 临时库已删除，data/campus.db 未生成
```

## 6. 剩余风险

- 表结构虽经 DDL 验证，但尚未经过真实数据导入与查询联调，字段细节可能在实现阶段微调。
- 课程与技能的关联表（course_skills / job_skills）需要层级字段（level），含义由后续数据生成器落实。
