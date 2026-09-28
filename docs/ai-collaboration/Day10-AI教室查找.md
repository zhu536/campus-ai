# AI协作记录：AI 智能找教室（Day 10）

## 1. 今日任务

让 Harness 实现「AI 智能找教室」的完整链路：

```
用户输入 → Intent Parser → 参数验证 → classroom service → SQLite
        → 返回候选教室 → AI 解释结果
```

硬性要求（原话）：

1. AI 不能直接访问数据库
2. AI 不能生成 SQL
3. 教室查询必须由 Python 完成
4. 查询参数必须经过 validate
5. 查询结果必须来自 SQLite
6. AI 只能解释查询结果
7. 查询为空时给出合理提示
8. 增加测试

新增 `backend/classroom.py`、`prompts/insight_generator.md`，修改必要的后端路由，
并且**不修改食堂与产教模块**。

## 2. 第一轮 Prompt（关键约束）

> 现在实现 CampusAI AI 智能找教室。
> 用户示例：“周三下午帮我找一个8人、有投影的空教室。”
> 新增：backend/classroom.py；修改必要的 backend 路由；新增：prompts/insight_generator.md
> 要求：AI 不能直接访问数据库、不能生成 SQL、教室查询必须由 Python 完成……
> 不要修改食堂和产教模块。

## 3. AI 建议与执行亮点

- **分层不改架构**：AI 没有新造查询层，而是复用已有的
  `queries.list_available_classrooms`（Python + 参数绑定 SQLite）与
  `validate.validate_intent`（白名单校验），只在上面加一层编排
  `backend/classroom.py`：解析 → 校验 → 查询 → 解释。
- **连接先关、再调模型**：`search_classrooms` 用 `with connection(...)` 做查询，
  连接关闭之后才调用解释器，模型拿到的只有 `build_insight_payload` 生成的只读事实表
  （没有连接、没有表名、没有数据库路径），从结构上保证「AI 不访问数据库」。
- **AI 输出也要过闸**：解释文本经 `_SQL_RE` 过滤，出现 `SELECT/DROP/--` 等片段时
  整条解释作废，降级为 Python 兜底文案（`explanation.source = "local"` + 错误码），
  既有测试断言「模型想塞 SQL 也会被拒」。
- **空结果不靠猜**：`build_hints` 会**再查一次 SQLite**，给出
  「不限制人数时，同一时段有 14 间空教室，但可能坐不下 1000 人。」这类可复算的建议；
  测试逐条把提示里的数字与真实查询结果对齐。
- **发现并修掉一个真 bug**：第一轮测试跑出
  `test_model_cannot_inject_room_selection` 失败 —— `validate_room_params` 调
  `validate_intent` 时漏传 `warnings`，导致「模型编出来的 `room_id` 被丢弃」这件事
  没有留痕。补上 `warnings=warnings` 后测试转绿，说明「留痕」不是可选项。

## 4. 执行的命令与结果

```bash
# 单文件（离线，不联网、不写真实库）
python -m pytest tests/test_classroom.py -q
# 66 passed

# 全量回归（含原有 405 个用例）
python -m pytest -q
# 471 passed
```

手动冒烟（真实库、真查询、强制无 Key，确认失败也是优雅的）：

```
[AI 不可用] 503 ai_unavailable | AI 意图解析不可用（missing_config）：缺少 AI_API_KEY…
[命中] 14 间 | 条件: {'semester': '2025-2026-1', 'weekday': 3, 'period_start': 5, 'period_end': 8, …}
按 至少 8 人、需要投影、周三、第 5-8 节、学期 2025-2026-1 查询，找到 14 间空教室。
• D301（实验楼 3 层，可坐 36 人，有投影、有电脑，实验室）
• 共 14 间，这里只列出前 10 间。
```

空结果（`capacity=1000`，数据里最大容量是 200）：

```
按 … 查询，没有找到满足条件的空教室（共 0 间）。
建议：不限制人数时，同一时段有 14 间空教室，但可能坐不下 1000 人。
```

## 5. 剩余风险

- **`has_projector=false` 仍是已知缺口**：查询层用 `has_projector >= ?`，`false` 等于
  「不筛选」而不是「排除投影」。本次**没有**改动 `queries.py`（避免影响既有接口与
  `tests/test_api.py` 的锁定用例），而是在响应 `notes` 里显式提示该限制，留待专门任务修复。
- **只给 `weekday` 不给节次时返回 400**：「周三任意节次有空」需要新的查询语义
  （某时段无排课 vs 全天无排课），当前选择明确报错，而不是返回把占用教室也算进去的假空闲。
- **解释质量依赖模型**：`prompts/insight_generator.md` 已写死「只能引用输入事实、不编造」，
  Python 侧只做了长度/字段/SQL 片段三层约束；如果模型把事实说反（例如把 60 人说成 8 人），
  目前只能靠 Prompt 与 `params` 回归测试约束，尚未做数字级一致性校验。
- **AI 路径需要真实 Key 才能人工验收**：自动化测试全部用替身离线跑，真机演示前需在
  `.env` 配置 `AI_API_KEY`。

## 6. Git 提交

`feat: add AI classroom search (intent → validate → sqlite → insight)`
