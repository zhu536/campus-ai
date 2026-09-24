\# Day 6 开发记录：Flask API 搭建



&#x20;1. 今日任务

编写基础后端接口，实现 4 个核心 API（教室、食堂、产教、技能缺口），要求使用 Flask + SQLite，参数化查询，不接入 AI，并且包含单元测试。



2\. AI 协作与实现方案

&#x09;AI 拆解方案：Harness 阅读了 `AGENTS.md`、`docs/PRD.md` 和 `docs/DATABASE.md` 后，建议按模块化拆分代码。

&#x09;生成文件：新增 9 个文件，包括 `app.py`、`routes.py`（路由解析）、`queries.py`（SQL查询与计算，不包含 Flask 逻辑）、`validators.py`（类型与范围校验）、`db.py`（连接管理）等。

关键实现口径：

&#x20;   1. 教室搜索使用时间段相交判定，并附带排序。

&#x20;   2. 食堂拥挤度使用历史日均人数除以食堂容量进行归一化处理。

&#x20;   3. 产教缺口采用加权覆盖率计算。

&#x09;参数校验：使用 `whereBuilder` 动态生成 SQL 片段，严格进行参数绑定，避免 SQL 注入。



&#x20;3. 异常测试与截图证据

&#x09;在浏览器中测试 `/api/industry/gap` 接口时，故意不传参数，系统正确返回了 400 错误提示：

&#x09;`{"error": {"code": "invalid\_parameter", "field": "major", "message": "缺少必填参数 major"}, "status": "error"}`



&#x09;这说明后端参数校验逻辑正常工作，成功拦截了非法请求。

&#x09;!\[API异常测试](https://raw.githubusercontent.com/zhu536/campus-ai/main/docs/ai-collaboration/images/day6-api-test.png)

4. 测试结果与提交

&#x09;单元测试：`pytest -q` 显示 \*\*68 个测试用例全部通过（68 passed）。

&#x09;Git 提交：`feat: add flask API for classrooms, canteens and industry skills`（仅新增文件，未修改前端和原有代码）。

