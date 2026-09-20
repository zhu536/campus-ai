\# CampusAI Agent Instructions



\## Project Goal



CampusAI 是一个校园数据智能分析与产教协同平台。



核心功能：



1\. AI智能找教室

2\. 食堂拥挤分析与预测

3\. 企业岗位与专业课程技能匹配



\## Tech Stack



\- Python

\- Flask

\- SQLite

\- CSV

\- JSON

\- HTML

\- CSS

\- JavaScript

\- ECharts

\- OpenAI-compatible API

\- pytest



\## Engineering Rules



1\. 优先使用简单架构。

2\. 不引入不必要框架。

3\. 不使用Vue。

4\. 不使用React。

5\. 不使用FastAPI。

6\. 不使用Docker。

7\. 不使用Redis。

8\. 不使用Kafka。

9\. 不使用多智能体。

10\. 不进行大规模重构。

11\. 一次只完成一个明确任务。

12\. 不修改与任务无关的文件。



\## AI Rules



AI负责：



\- 自然语言意图识别

\- 参数提取

\- 图表选择

\- 结果解释



Python负责：



\- 参数验证

\- 数据查询

\- 业务逻辑

\- 数据计算

\- 预测



SQLite负责：



\- 保存事实数据



ECharts负责：



\- 可视化



禁止：



LLM直接生成任意SQL并执行。



禁止：



API Key写入源代码。



\## Data Rules



模拟数据必须使用固定随机种子：



random.seed(42)



原始数据：



data/raw/



数据库：



data/campus.db



\## Prompt Rules



正式Prompt必须保存到：



prompts/



Prompt修改必须通过Git保留历史。



\## Development Workflow



执行任务前：



1\. 阅读AGENTS.md

2\. 阅读相关代码

3\. 说明修改计划

4\. 只修改必要文件



执行任务后：



1\. 运行测试

2\. 检查代码

3\. 检查Git diff

4\. 汇报修改文件

5\. 汇报测试结果

6\. 汇报剩余风险



\## Definition of Done



功能必须：



\- 可以运行

\- 有测试

\- 不泄露密钥

\- 不破坏旧功能

\- 有Git提交

\- 有AI协作记录

