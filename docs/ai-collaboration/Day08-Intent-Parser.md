# AI协作记录：Intent Parser（Day 8）

## 本轮 Prompt（逐字原文）

```text
现在实现CampusAI Intent Parser。

读取：

AGENTS.md
prompts/intent_parser.md

创建：

backend/ai_parser.py
backend/validate.py
tests/test_intent.py
tests/golden_questions.json

要求：

1. 使用requests调用OpenAI-compatible API。
2. API配置从.env读取。
3. Prompt从prompts/intent_parser.md读取。
4. 使用JSON Output。
5. 解析JSON。
6. intent必须经过白名单验证。
7. capacity等字段需要验证。
8. AI调用失败必须有错误处理。
9. 禁止生成SQL。
10. 禁止AI直接访问数据库。

增加至少10个Intent测试。

完成后运行pytest。
```

1. 今日任务

让 Harness 编写 `backend/ai_parser.py` 和 `validate.py`，将用户自然语言解析为结构化 JSON，要求绝对禁止生成 SQL 或访问数据库，并增加至少 10 个测试。



2. 第一轮 Prompt

![Intent Parser Prompt](images/day8-prompt.png)



3. AI 建议与执行亮点

安全防线：AI 使用 AST 静态分析确保代码中无 SQL 注入，无法识别的问题统一降级为 `unknown` 意图，不抛异常。

测试覆盖：AI 为 `tests/test_intent.py` 补充 Intent 解析测试用例，全量 `pytest -q` 显示 **119 passed**。

边界遵守：AI 严格遵守了“不修改与任务无关文件”的纪律，未强行将解析器塞入 Flask 路由，留到 Day 10 对接。



4. 执行结果与剩余风险

![Intent Parser Execution](images/day8-execution.png)

待解决：当时 `prompts/` 目录还未被 Git 追踪，随后已补上并提交。

业务决策：保留 `room_search` 至少一个筛选条件的限制，防止全库扫描，符合安全防御要求。

字段命名：`end_period` 与 Flask 路由暂不一致，留到 Day 10 对接真实 API 时统一。



5. Git 提交

`feat: add natural language intent parser`

