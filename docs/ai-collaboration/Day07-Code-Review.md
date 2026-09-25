\# Day 7 开发记录：第一周代码验收与稳定性修复



1\. 今日任务

不新增功能，进行第一周的全面代码审查与稳定性检查。



2\. 第一轮：代码审查与安全审计

安全红线：Harness 发现早期 Git 提交中存在旧 API Key 泄露的隐患。我立刻确认该旧 Key 已在 DeepSeek 平台吊销，现使用 `.env` 中的新 Key。

审查报告：AI 给出 10 项检查结果，诊断出 pytest 缓存目录权限不足、BOM 字符问题等。

!\[代码审查报告](images/day7-code-review-part1.png)



&#x20;3. 第二轮：修复与重构

修复过程：要求 AI 仅修复权限、BOM字符、注释不一致等问题，严禁修改业务逻辑。

AI 反馈：AI 实测确认原有的教室空闲逻辑完全正确，拒绝无意义重构，最终只修改了 4 个文件（+20/-4 行）。

!\[修复结果与测试](images/day7-code-review-part2.png)



&#x20;4. 最终验收结果

单元测试：`pytest -q` 最终输出 \*\*68 passed\*\*，测试全绿。

Git 提交：`fix: stabilize week one foundation`

