# Day 7 开发记录：第一周代码验收与稳定性修复

## 本轮 Prompt 链（逐字原文）

### 第一轮：代码审查

```text
今天只进行代码审查和稳定性检查。

不要新增功能。
不要大规模重构。

检查：

1. Flask能否启动
2. SQLite是否正常
3. CSV是否能重新生成
4. 数据导入是否正常
5. 9张表是否正常
6. API是否正常
7. SQL是否参数化
8. 是否泄露API Key
9. pytest是否通过
10. 项目结构是否符合AGENTS.md

请先输出问题清单。

不要直接修改代码。
```

### 第二轮：按审查结果做安全加固

```text
请根据刚才的代码审查报告，优先修复以下几个问题（不要修改业务逻辑，只做安全加固和稳定性修复）：
1. 修复 pytest.ini 的 UTF-8 BOM 字符问题。
2. 修复 Windows 环境下 pytest 临时目录不可写的问题，将缓存目录配置在项目内部（例如 .pytest_cache）。
3. 统一 backend/queries.py 中关于 industry_skill 的文档和实现逻辑。
4. 更新 .env.example 中的 AI_MODEL 名称，使其与实际模型一致。
5. 在 backend/queries.py 中补全单节课查询的空闲时间交叉判定逻辑。
修改完成后，重新运行 pytest -q，确保 68 个测试仍然全部通过。
```

1. 今日任务

不新增功能，进行第一周的全面代码审查与稳定性检查。



2. 第一轮：代码审查与安全审计

安全红线：Harness 发现早期 Git 提交中存在旧 API Key 泄露的隐患。我立刻确认该旧 Key 已在 DeepSeek 平台吊销，现使用 `.env` 中的新 Key。

审查报告：AI 给出 10 项检查结果，诊断出 pytest 缓存目录权限不足、BOM 字符问题等。

![代码审查报告](images/day7-code-review-part1.png)



 3. 第二轮：修复与重构

修复过程：要求 AI 仅修复权限、BOM字符、注释不一致等问题，严禁修改业务逻辑。

AI 反馈：AI 实测确认原有的教室空闲逻辑完全正确，拒绝无意义重构，最终只修改了 4 个文件（+20/-4 行）。

![修复结果与测试](images/day7-code-review-part2.png)



 4. 最终验收结果

单元测试：`pytest -q` 最终输出 **68 passed**，测试全绿。

Git 提交：`fix: stabilize week one foundation`

