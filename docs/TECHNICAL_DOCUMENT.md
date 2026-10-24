# CampusAI 技术文档

**项目**：CampusAI 校园数据智能分析平台
**仓库**：<https://github.com/CrestLune/campus-ai.git>　**代码截止提交**：`34d857b`（2026-10-24，GitHub 改名后链接同步）
**文档状态**：v1.2 完整版　**编写日期**：2026-10-07（2026-10-08 修订：同步 Day01–05 补写、Day20 记录与提交数 50；2026-10-24 修订：GitHub 改名后仓库链接同步，提交总数 52）

## 摘要

CampusAI 用一句中文就能查校园数据：用户问「周三下午帮我找一个 8 人、有投影的空教室」，系统返回候选空教室与一段解释。它最核心的工程主张不是「用了大模型」，而是**把大模型的使用范围压到极小**——AI 只做两件事：**把自然语言变成结构化参数**（意图解析）、**把已经算好的结果讲成人话**（结果解释）。查数据、算统计、做预测、做集合运算全部由 Python + SQLite 完成；AI 不生成 SQL、不访问数据库、不改动计算结果。

这份文档要回答三个问题：

1. **为什么这样分层**——四层职责（AI / Python / SQLite / ECharts）各自负责什么，边界画在哪里；
2. **边界怎么落到代码**——每一条「AI 不许做什么」如何变成可执行、可测试的断言；
3. **哪些数字是真的**——全文所有数字都注明来源，未实测的一律标注为「原文档陈述」或「UNVERIFIED」，不编造。

> **事实纪律**：本文按 `实测` / `原文档陈述` / `UNVERIFIED` 三级标注证据强度。凡是本次编写过程中真实执行过命令、读取过输出的结论，标为「实测」并给出命令行与输出；凡是来自仓库既有文档而未二次实测的，标为「原文档陈述」并给出文档位置；凡是无法核实的，**留空并标注**，绝不补编。
>
> 本次实跑过的唯一验证命令：`.\.venv\Scripts\python.exe -m pytest -q` → `598 passed`（先后三次实测：`8.55s` / `7.64s` / `7.65s`）。

---

## 文档定位与阅读对象

- 读者：评审（传智杯 AI 创新应用挑战赛 / 赛道 id 10039）、后续接手开发者、未来的自己。
- 目标：一次讲清「产品为什么这么设计 → 系统怎么分层 → AI 边界划在哪 → 怎么证明它真的跑得起来」。
- 与既有文档的分工：
  - `README.md` = 陌生环境 10 分钟跑通指南（操作向）。
  - `docs/PRD.md` = 需求与范围（做什么、不做什么）。
  - `docs/API.md` = 10 个接口的字段级契约。
  - `docs/DATABASE.md` = 9 张表的唯一权威 DDL + 三大功能用表方式。
  - `docs/ai-collaboration/` = 开发过程证据（人如何指挥 AI）。
  - **`docs/TECHNICAL_DOCUMENT.md`（本文件）= 把上面这些串成一份可讲、可验、可评审的技术叙事**，重点回答「为什么这样分层」「红线怎么落到代码」「哪些数字是真的」。

### 证据引用约定（全文统一）

| 标记 | 含义 |
|---|---|
| `路径:行号` | 该结论来自代码/文档原文，可点开核对 |
| `实测` | 本次编写过程中真实执行过命令或读取过输出 |
| `原文档陈述` | 来自仓库既有文档的描述（如 CSV 行数、导入行数），本文未二次实测以避免覆盖数据文件 |
| `UNVERIFIED` | 未能核实的项，明确留空标注，不猜 |

### 一句话架构

```
用户（前端大屏 / 问答框）
  → Intent 解析      prompts/intent_parser.md + backend/ai_parser.py   【AI：只抽参数】
  → 参数校验         backend/validate.py 白名单 + 各业务模块业务校验    【Python】
  → 查询与计算       backend/queries.py（SQLite 取事实）
                     + classroom.py / forecast.py / industry.py（统计、预测、集合运算）【Python】
  → SQLite           data/campus.db（9 张表，只读，全部 ? 绑定）
  → 可视化           frontend/index.html + ECharts（只画已算好的数据）
  → 结果解释         prompts/*_insight_generator.md                     【AI：只解释】
```

三个核心功能与首屏入口：

| 功能 | 实现文件 | 首屏入口 |
|---|---|---|
| AI 找教室 | `backend/classroom.py`、`backend/queries.py` | 快捷按钮「找空教室」`frontend/index.html:606` |
| 食堂分析与预测 | `backend/forecast.py`、`backend/queries.py` | 快捷按钮「食堂拥挤预测」`frontend/index.html:607` |
| 产教融合技能匹配 | `backend/industry.py`、`backend/queries.py` | 快捷按钮「技能缺口分析」`frontend/index.html:608` |

---

## 1. 项目背景与用户痛点

### 1.1 背景：数据是有的，但用不起来

校园里客观存在四类高价值数据：**课程与排课**、**教室资源**、**食堂人流**、**企业岗位与技能需求**。问题不是「不存在」，而是：

- **分散**：排课、教室属性、食堂人流、企业岗位分属不同系统或文件，采集口径各不相同（`docs/PRD.md:5-15`，原文档陈述）；
- **形态不匹配**：人想问「现在哪间教室空着、能坐 8 人、有投影吗」，数据却按 `room_id / capacity / has_projector / semester / weekday / period_start` 存储；
- **缺统一入口**：没有哪个入口能同时回答「空间够不够」「时间挤不挤」「能力差在哪」（`docs/PRD.md:19-31`）。

痛点可概括为一句话：**问题是用自然语言问的，数据是按表和字段存的**。中间缺两层——「自然语言 → 参数」的翻译，和「参数 → 计算结果」的加工。

### 1.2 三类用户与具体痛点

| 用户 | 会问的问题 | 现在的做法 | 卡在哪 |
|---|---|---|---|
| 学生 | 周三下午想找 8 人、有投影的空教室开会 | 挨个楼走 / 问同学 | 排课表不公开，空闲状态无法快速判断 |
| 教师 | 调课到下午三四节有合适教室吗；中午哪个食堂不挤 | 手工对课表 | 需同时满足人数/设备/时段 |
| 管理人员 | 教室利用率、食堂高峰、专业对口岗位缺哪些技能 | 各系统分别导数据拼报表 | 缺统一口径与「算好的结论」 |

用户范围见 `docs/PRD.md:33-42`。

### 1.3 为什么选择「AI + 确定性计算」

最容易想到的方案是「让大模型直接连数据库、自己写 SQL」，本项目**明确否掉**，依据是 `AGENTS.md:133-138` 的硬性禁令：**禁止 LLM 直接生成任意 SQL 并执行**。理由是：

- 自然语言的不确定性必须被限制在「意图 + 参数」这一层，不能蔓延到数据访问层；
- 统计与预测必须有**唯一、可复现**的口径，否则同一问题两次问出不同答案，结论无法用于决策；
- 接口必须**可测试**——若结果由模型现场生成，测试只能断言「像不像」，无法断言「对不对」。

因此技术主张是：**用 AI 解决「听不懂人话」，用 Python 解决「算得准不准」**，两者之间用参数白名单硬隔开。

### 1.4 明确不做的事（非目标）

不实现用户注册、微信登录、手机 App、真实教务系统对接、在线支付、复杂权限体系、生产级部署、多智能体系统、复杂机器学习模型（`docs/PRD.md:137-162`）；MVP 只做三个核心功能（`docs/PRD.md:129-133`）。工程约束同样写死在 [AGENTS.md](D:\Projects\campus-ai\AGENTS.md)：简单架构优先，不用 Vue / React / FastAPI / Docker / Redis / Kafka，不用多智能体，不做大规模重构（`AGENTS.md:57-75`）。

**本章证据**：`docs/PRD.md`、`AGENTS.md`。

---

## 2. 产品设计（三个核心场景）

**目标**：一句话讲清每个场景「用户问什么 → 系统算什么 → 用户看到什么」，并说明为什么只做三个。

### 2.1 为什么只做三个场景

三个场景分别覆盖三类校园资源，且**每一类都能落成可核验的数字**——这是它们被选中的原因：

| 场景 | 覆盖的资源（可核验的数字） | 实现文件 |
|---|---|---|
| AI 找教室 | **空间资源**（候选数 / 已占用数 / 空闲数 / 容量窗口） | `backend/classroom.py` |
| 食堂拥挤分析与预测 | **时间资源**（人次 / 平均等待 / 拥挤度 / 峰值小时 / 预测人数） | `backend/forecast.py` |
| 产教融合技能匹配 | **能力资源**（要求技能数 / 覆盖数 / 缺口数 / 覆盖率） | `backend/industry.py` |

「能算出确切数字」这条标准，把项目从「AI 聊天玩具」拉回到「决策辅助工具」：如果某个场景只能给出「很像」的回答，它就不适合作为核心功能。

### 2.2 场景一 · AI 找教室

**用户怎么说**：「周三下午帮我找一个 8 个人、有投影的空教室。」**系统算什么**：把这句话解析成 `{capacity: 8, has_projector: true, weekday: 3, period_start: 5, period_end: 8}`，然后回答「哪些教室这个时段没课」。**关键技术点**：教室的**空闲状态不落库**，而是在查询时用 `classrooms` 左连接该时段的 `schedules` 实时算出来（`docs/DATABASE.md:58,169`）——排课一更新空闲结论立即正确，不会出现「排课改了但空闲标记没改」的脏数据。**输出**：候选教室列表 + `summary`（候选/已占用/空闲/容量窗口）+ AI 解释，失败时给 Python 兜底文案 + `hint[]` 放宽建议。**代码入口**：`routes.py:221` 的 `/api/classrooms/search` → `classroom.py` 的 `search_classrooms()`。

### 2.3 场景二 · 食堂拥挤分析与预测

**用户怎么说**：「上周一食堂中午为什么那么挤？」「预测下周三中午二食堂会有多少人。」**系统算什么**：历史人流、平均等待时间、拥挤度、高峰时段，以及未来几天午餐人流预测。**关键技术点**：预测方法**只有一种**，保持极简（`forecast.py:14-25`、`docs/API.md:280-288`）：

```
预测值 = 历史同期平均值 + 星期修正 + 时间段修正
B = 训练窗口内「同一日期 + 同一小时」客流的算术平均；ΔW(w) = 星期 w 的历史平均 − B
ΔH(h) = h 点的历史平均 − B；预测(t,h) = max(0, B + ΔW(weekday(t)) + ΔH(h))
```

没有模型训练、没有梯度下降、没有树、没有注意力——只有算术平均与减法（`forecast.py:23`）。**可复算性**：响应里每个小时都返回 `base` / `weekday_adjustment` / `hour_adjustment` 三个分量，评审可原地复算（`docs/API.md:320`）。**代码入口**：`routes.py:243/266/277/287` 四个接口 → `forecast.py` 的 `analyze_canteen()`。

### 2.4 场景三 · 产教融合技能匹配

**用户怎么说**：「软件工程专业的课程技能和企业岗位需求差在哪里？」**系统算什么**：以 `skills` 表为枢纽，把「岗位要求的技能」和「专业课程覆盖的技能」两个集合做交/差运算，得到五个输出（`industry.py:14-20`）：

| 输出字段 | 含义 |
|---|---|
| `skill_demands` | 技能需求：岗位要求哪些技能、要多重要 |
| `covered_skills` | 已覆盖技能 = 技能需求 ∩ 课程技能 |
| `missing_skills` | 缺失技能 = 技能需求 − 课程技能（另有 `required_missing_skills`） |
| `coverage_rate` | 技能覆盖率 = 覆盖数 / 技能需求数（另有重要度加权版） |
| `importance` | 技能重要程度：1..5 分 + 中文档位（核心/重要/较高/一般/次要）与分布 |

- **关键技术点**：全部计算在 Python；AI 只解释，且解释前后用 SHA-256 指纹校验结果未被改动（`backend/industry.py:40-51`）。
- **代码入口**：`backend/routes.py:328`（`/api/industry/gap`）与 `backend/routes.py:339`（`/api/industry/match`）→ `backend/industry.py` 的 `analyze_skill_gap()`。

### 2.5 三个场景如何在前端呈现

前端是**单文件大屏** `frontend/index.html`（实测 2772 行 / 139,101 字节），由三部分组成：

1. **5 个快捷按钮**（`frontend/index.html:606-610`），每个绑定一句真实问句，点击即走完整链路：
   - 找空教室 → 「周三下午帮我找一个 8 人、有投影的空教室」
   - 食堂拥挤预测 → 「今天中午哪个食堂最拥挤？需要等多久？」
   - 技能缺口分析 → 「计算机科学与技术专业和岗位技能还差哪些？」
   - 教室利用率 → 「本学期的教室利用率怎么样？」
   - 岗位技能需求 → 「现在全校有多少个在招岗位？平均薪资多少？」
2. **自由输入问答框**：直接输入任意校园问题，走 `/api/classrooms/search` 的意图解析链路。
3. **图表区**：ECharts 画趋势折线、时段柱状、技能对比等，数据全部来自接口已算好的 `series` / `report`。

### 2.6 MVP 边界

只实现三个核心功能；`docs/PRD.md:129-133` 明确写为 MVP 范围。前端空结果、AI 不可用、后端不可用三种情况都有确定行为（分别是放宽建议、Python 兜底文案、离线快照），不存在「白屏」状态。

**本章证据**：`docs/PRD.md`、`README.md`、`docs/API.md`、`docs/DATABASE.md`、`backend/classroom.py`、`backend/forecast.py`、`backend/industry.py`、`frontend/index.html`、`tests/golden_questions.json`（40 条固化问句）。

---

## 3. 总体架构：用户 → Intent 解析 → 参数校验 → Python 查询/计算 → SQLite → ECharts → AI 解释

**目标**：给出全书的主图，并把「谁负责什么」钉死。

### 3.1 主链路

以「AI 找教室」为例，完整链路如下（`README.md:320-332`、`backend/classroom.py:4-11`）：

```
用户提问（前端大屏 / 问答框）
  → Intent 解析      ai_parser.py 读 prompts/intent_parser.md
                     （AI：只识别意图 + 抽参数，不查数据、不生成 SQL）
  → 参数校验         validators.py + validate.py 白名单 + 业务规则，非法即 400
  → Python 查询/计算 queries.py（唯一取数入口）+ classroom/forecast/industry.py
  → SQLite           data/campus.db（9 张表，只读，值全部 ? 绑定）
  → 可视化           frontend/index.html + ECharts（只画已算好的 series / report）
  → AI 解释          classroom.py 读 prompts/insight_generator.md（只拿事实表，只出文字）
```

三条链路的代码位置一一对应：

| 环节 | 找教室 | 食堂 | 产教融合 |
|---|---|---|---|
| 意图解析 / 白名单校验 | `ai_parser.py` / `validate.py` | 同左 | 同左 |
| 业务校验 | `classroom.py::validate_room_params` | `forecast.py::validate_forecast_params` | `industry.py::validate_gap_params` |
| 取数（唯一入口） | `queries.list_available_classrooms` | `queries.canteen_meal_grid` | `queries.industry_skill_grid` |
| 计算 | `classroom.py`（排序 + 放宽建议） | `forecast.py`（趋势/高峰/预测） | `industry.py`（集合运算 + 覆盖率） |
| 解释 Prompt | `insight_generator.md` | `canteen_insight_generator.md` | `industry_insight_generator.md` |

### 3.2 三个关键设计点

**（1）AI 处在链路两端，不在中间。** 解析在最前，解释在最后，中间全是确定性代码。即使模型完全不可用，中间部分（校验 → 查询 → 计算）依然 100% 可运行——这是第 6 章「AI 故障不影响接口可用性」的结构前提。

**（2）数据流单向。** 数据只能沿 `SQLite → Python → JSON → 图表/解释`流动，没有反向写入路径。`backend/db.py:5-9` 规定「只读查询，本模块不做任何写入」；三个计算模块不持有连接（不 `import sqlite3`），只能通过 `queries.py` 取数。

**（3）AI 不可用不等于接口不可用。** 解释阶段 `explanation.source` 只会是 `ai` 或 `local`；AI 失败时用 Python 兜底文案，接口仍返回 200（食堂、产教），仅「意图解析不可用」的找教室接口返回 503（`README.md:416-429`）。这是**有意的不对称**：没有解析就不知道用户想查什么，而结果解释只是锦上添花。

### 3.3 目录与分层映射

```
backend/
├── app.py           应用工厂 create_app()、统一 JSON 错误处理、CORS、命令行入口
├── routes.py        路由层：解析参数 → 调业务函数 → 返回 JSON（无 SQL）
├── queries.py       业务查询与计算（不 import flask，脚本可直接调用）
├── db.py            连接管理（上下文管理器）与参数化工具 WhereBuilder
├── validators.py    路由层参数校验（字符串 → 安全值，非法即 400）
├── validate.py      意图白名单、学期/日期/整数校验、SQL 片段检查
├── errors.py        ValidationError(400) / NotFoundError(404) / DatabaseUnavailableError(503)
├── ai_parser.py     意图解析器（自然语言 → Intent JSON，不查数据）
├── time_resolver.py 相对日期解析（纯函数无 I/O）
├── classroom.py / forecast.py / industry.py   三条业务链路的编排、计算与解释
```

分层硬约束（`docs/API.md:27-39`）：`routes.py` **不含 SQL**；`queries.py` **不 import flask**（可被脚本与测试直接调用，不必起 Web 服务）；三个 AI 模块**不 import sqlite3、不写 SQL**。这三条不是口头约定，而是有 AST 级测试断言的（见 13.3.1 节）。

### 3.4 反例对照：为什么不走「AI 直接生成 SQL」

如果让 LLM 生成 SQL 并执行，会同时失去四样东西：**参数校验**（无法保证 `capacity` 是 1..1000 的整数）、**错误码契约**（前端无法区分「参数错了」与「真的没数据」）、**可测试性**（只能断言「回答像不像」，无法断言「数字对不对」）、**可复现性**（同一问题两次提问可能得到不同口径）。这正是 `AGENTS.md:133-138` 把「LLM 直接生成任意 SQL 并执行」列为**禁止项**的原因。

### 3.5 一张图看懂职责边界

```
AI 层（唯一的不确定性来源）：① 听懂问题 → 结构化参数    ② 把算好的结果讲成人话
        ↓ 只能输出 JSON 参数                    ↓ 只能输出文字解释
Python 层（确定性）：白名单校验 → 取数 → 排序 / 统计 / 预测 / 集合运算
        ↓ ? 参数化只读查询
SQLite 层：data/campus.db（9 张表，只存事实）
        ↓ 已算好的 JSON
ECharts 层：前端大屏（只画图，不做业务计算）
```

**本章证据**：`README.md:318-340`、`backend/classroom.py:1-31`、`backend/forecast.py:1-38`、`backend/industry.py:1-53`、`backend/db.py`、`docs/API.md:27-39`、`AGENTS.md:83-146`。

---

## 4. 后端设计：Flask 应用工厂、10 个 API、统一 JSON 错误处理（400/404/405/500/503）、CORS 自实现

**目标**：讲清后端「小但完整」的工程形态。

### 4.1 应用工厂

`backend/app.py:54-64` 的 `create_app(db_path=None)` 是整个后端的组装点：

```python
app.config["DATABASE"] = db_path or default_db_path()
app.json.ensure_ascii = False       # 中文直接输出，便于人工核对与前端展示
app.url_map.strict_slashes = False  # /api/x 与 /api/x/ 等价
app.register_blueprint(api, url_prefix="/api")
_register_error_handlers(app); _register_cors(app)
```

三个细节：① **`db_path` 可注入**——这是测试能把真实库复制到临时目录后跑接口的前提（`tests/test_api.py:67-71`）；② **数据库路径三级解析**——显式参数 → 环境变量 `CAMPUS_DB_PATH` → 默认 `data/campus.db`（`db.py:23-25`）；③ **`python backend/app.py` 直接运行也能工作**——此时没有包上下文，`app.py:29-40` 会补 `sys.path`，模块方式运行时走相对导入分支。

三种启动方式（`README.md:232-239`）：`python backend\app.py` / `python -m backend.app` / `$env:FLASK_APP="backend.app"; python -m flask run`。监听地址默认 `127.0.0.1:5000`（`DEFAULT_HOST` / `DEFAULT_PORT`，`app.py:42-43`），可用 `HOST` / `PORT` 覆盖（`:160-161`）。

### 4.2 10 个 API

全部挂在 `/api` 前缀下、全部返回 JSON（`backend/routes.py`）：

| # | 接口（方法） | 路由代码 | 业务函数 | 主要参数 |
|---|---|---|---|---|
| 1 | `/classrooms/available`（GET） | `routes.py:211` | `queries.list_available_classrooms` | `capacity`、`has_projector`、`has_computer`、`room_type`、`building`、`weekday`、`period_start/end`、`semester`、`limit` |
| 2 | `/classrooms/search`（GET+POST） | `routes.py:221` | `classroom.search_classrooms` | `question`（必填，≤200）、`explain`（0/1，默认 1） |
| 3 | `/canteens/stats`（GET） | `routes.py:243` | `queries.canteen_stats` | `date_from/date_to`、`canteen_id`、`time_slot`、`top_slots`（0..4） |
| 4 | `/canteens/trend`（GET） | `routes.py:266` | `forecast.analyze_canteen`（trend） | 同 3 + `explain` |
| 5 | `/canteens/peak`（GET） | `routes.py:277` | `forecast.analyze_canteen`（peak） | 同 3 + `peak_ratio`（0.1..1.0，默认 0.8）、`explain` |
| 6 | `/canteens/forecast`（GET） | `routes.py:287` | `forecast.analyze_canteen`（forecast） | 同 3 + `target_date`、`horizon_days`（1..30，默认 3）、`explain` |
| 7 | `/meta/semesters`（GET） | `routes.py:299` | `queries.list_semesters` | 无 |
| 8 | `/industry/skills`（GET） | `routes.py:312` | `queries.industry_skills` | `dimension`（industry/city/major/company）、`industry`、`city`、`major`、`top` |
| 9 | `/industry/gap`（GET） | `routes.py:328` | `queries.industry_skill_gap` | `major`（必填）、`industry`、`min_importance`（1..5）、`top` |
| 10 | `/industry/match`（GET） | `routes.py:339` | `industry.analyze_skill_gap` | 同 9 + `question`（≤200）、`explain` |

字段级契约（响应样例、排序规则、截断语义）以 [docs/API.md](D:\Projects\campus-ai\docs\API.md) 为准，本文不重复抄写。**接口的两类性格**（`docs/API.md:5-8`）：**确定性接口**（1、3、7、8、9）纯 SQL + Python，**完全没有 LLM 参与**，可脱离 AI 独立工作；**接入 AI 的接口**（2、4、5、6、10）AI 只做意图识别与结果解释，其中接口 2 走全链路，接口 4/5/6/10 只走「解释」这一段（参数从查询串来）。**路由层三条实现约定**（`routes.py:31-115`）：`_ok(payload)` 统一成功外壳；`_run()` 统一「开连接 → 调业务 → 关连接」，业务函数不自己管连接；`_json_body()` / `_question_input()` / `_explain_flag()` 统一 POST/GET 取参，缺 `question` 或超长直接 400。

### 4.3 统一 JSON 错误处理

**成功与失败的外壳**（`README.md:257-262`）：

```json
{ "status": "ok",    "data":  { "...": "..." } }
{ "status": "error", "error": { "code": "invalid_parameter", "message": "...", "field": "weekday" } }
```

**完整错误映射表**（实现位置 `backend/app.py:98-155`）：

| HTTP | `error.code` | 触发条件 | 处理位置（附字段） |
|---|---|---|---|
| 400 | `invalid_parameter` | 参数缺失或取值非法（`ValidationError`） | `app.py:108-110`、`errors.py:9-32`（`field`，缺失必填时附 `available`） |
| 400 | `unsupported_intent` | 意图不是 `room_search` | `app.py:116-119`、`classroom.py:623`（`field=intent`） |
| 404 | `not_found` | 查询目标不存在（专业/学期/食堂） | `app.py:112-114`、`errors.py:35-53`（`available`） |
| 405 | `method_not_allowed` | 方法不允许，消息列出可用方法 | `app.py:126-134` |
| 503 | `database_unavailable` | 数据库文件缺失/无法打开 | `app.py:121-124`、`db.py:33-40` |
| 503 | `ai_unavailable` | 意图解析不可用（缺 Key / 网络失败 / 输出非法） | `app.py:116-119`、`classroom.py:604-617`（消息含原因） |
| 500 | `database_error` | `sqlite3.Error` | `app.py:136-139` |
| 500 | `internal_error` | 未预期异常兜底 | `app.py:141-144` |

错误处理集中在 `_register_error_handlers()`（`app.py:105-144`），用一张「异常 → JSON」注册表把所有异常翻译完，路由与业务层都不自己拼错误响应：`ValidationError` → 400、`NotFoundError` → 404、`DatabaseUnavailableError` → 503；`ClassroomSearchError` → 用它自带的 `status_code`（400 或 503），把「意图不对」与「AI 不可用」区分开；`HTTPException` → 405 及其他（`valid_methods` 拼进消息）；`sqlite3.Error` → 500 `database_error`；其余 `Exception` → 500 `internal_error` 并记 `logger.exception`。

**错误码设计原则**：`field` 告诉调用方「哪个参数错了」，`available` 告诉它「能填什么」（`errors.py:10-15`）。例如 `/api/industry/gap` 缺 `major` 时，400 里直接带可选专业清单，不必先撞一次 404（`routes.py:139-162` 的 `_require_major()`）。

### 4.4 CORS 自实现

不引入 `flask-cors`，因为需求只有一条：**让 `file://` 直接打开的前端（浏览器带 `Origin: null`）能发请求**（`app.py:45-51`）。实现是一个 `after_request` 钩子（`app.py:80-95`）：

```python
@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin")
    if origin:
        headers["Access-Control-Allow-Origin"] = origin        # 按请求回显，不写死 *
        headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        headers["Access-Control-Allow-Headers"] = (
            request.headers.get("Access-Control-Request-Headers") or "Content-Type")
        headers["Access-Control-Max-Age"] = "600"               # 省掉重复预检
    return response
```

三个设计决定：**回显 Origin 而不是写死 `*`**（写死等于把接口开放给任意站点）；**预检回显 `Access-Control-Request-Headers`**（浏览器要什么头就答应什么）；**`Vary: Origin`**（避免中间缓存把 A 站点的响应复用给 B 站点）。**对既有测试无侵入的原因**：只有浏览器跨域请求才带 `Origin`，`app.test_client()` 不带，钩子直接跳过（`app.py:70-72` 注释明确写了这一点）。

### 4.5 连接管理与参数化查询

**连接生命周期**（`db.py:47-58`）：

```python
@contextmanager
def connection(db_path):
    conn = connect(db_path)
    try:    yield conn; conn.commit()
    except BaseException: conn.rollback(); raise
    finally: conn.close()          # 退出必关闭
```

「退出必关闭」不是洁癖：Windows 下未关闭的 SQLite 连接会锁住数据库文件。打开连接时还会显式执行 `PRAGMA foreign_keys = ON`（`db.py:42-43`），因为 SQLite 默认关闭外键约束。

**参数化查询**：`WhereBuilder`（`db.py:65-97`）按顺序拼 `col = ?` / `col >= ?`，`column` 与 `operator` 由业务代码写死（常量），**只有 `value` 来自外部并且永远进 `params` 列表**；配套 `query_all` / `query_one` / `query_value`（`:99-114`）。这条设计从结构上消灭了「把用户输入拼进 SQL」的路径。

### 4.6 工程约束的落地对照

| `AGENTS.md` 约束 | 实际做法（位置） |
|---|---|
| 不使用 Vue / React | 原生 HTML/CSS/JS 单文件（`frontend/index.html`） |
| 不使用 FastAPI | Flask 3.1.3 蓝图（`app.py:61`） |
| 不使用 Docker / Redis / Kafka | 无容器/缓存/消息队列，SQLite 单文件（`requirements.txt`，19 个包） |
| 不使用多智能体 | 单一 Intent Parser + 单一 Explainer，无 Agent 编排（`ai_parser.py`） |
| 不引入不必要框架 | CORS 自己写（约 40 行），不装 `flask-cors`（`app.py:67-95`） |
| 不进行大规模重构 | 每轮改动范围在协作记录中逐一列出（Day16 只动 `frontend/index.html`，见 `Day16-稳定性检查.md:8-9`） |

**本章证据**：`backend/app.py`、`backend/routes.py`、`backend/db.py`、`backend/errors.py`、`docs/API.md`、`README.md:255-295`、`AGENTS.md:57-75`。

---

## 5. 数据架构：9 张表、CSV → SQLite 导入、`random.seed(42)` 复现

**目标**：讲清「事实数据入库、派生数据现算」这一条主线。

### 5.1 9 张表全景

数据结构的设计原则一句话：**事实数据入库，派生数据现算**（`docs/DATABASE.md:5-6`）。完整 DDL 的唯一权威来源是 [docs/DATABASE.md](D:\Projects\campus-ai\docs\DATABASE.md) 的「完整 CREATE TABLE SQL」章节（`:222-355`），共 9 张表：

| 表 | 角色 | 关键字段（行数） |
|---|---|---|
| `classrooms` | 教室（维度） | `room_name`、`building`、`floor`、`capacity`、`has_projector`、`has_computer`、`room_type`、`status`（50） |
| `courses` | 课程（维度） | `course_code`、`course_name`、`department`、`credit`、`course_type`（100） |
| `schedules` | 排课/占用（**事实**） | `course_id`、`room_id`、`semester`、`weekday`、`period_start/end`（1186） |
| `canteens` | 食堂（维度） | `canteen_name`、`capacity`、`window_count`、`open_time`、`close_time`、`status`（5） |
| `meal_records` | 食堂人流（**事实**） | `canteen_id`、`record_date`、`record_hour`、`time_slot`、`people_count`、`wait_minutes`、`capacity`、`source`（8480） |
| `jobs` | 企业岗位（维度） | `job_title`、`company`、`industry`、`city`、`salary_min/max`、`major`、`job_desc`（100） |
| `skills` | 技能（**枢纽维度**） | `skill_name`、`skill_category`（30） |
| `course_skills` | 课程—技能（多对多） | `course_id`、`skill_id`、`level`（了解/掌握/熟练）（300） |
| `job_skills` | 岗位—技能（多对多） | `job_id`、`skill_id`、`importance`（1..5）、`is_required`（400） |
| **合计** | | **10651** |

> **数据来源说明（重要）**：上表行数是**原文档陈述**，来自 `README.md:185-195`（生成阶段实测）与 `README.md:211-224`（导入阶段实测），并与 `make_data.py:480-491` 的期望常量 `EXPECTED_COUNTS` 三处互证（`schedules` 在脚本里只断言「≥500」，下限常量 `MIN_SCHEDULES = 520`）。**本文档编写时未重跑 `make_data.py` 与 `import_data.py`**（它们会覆盖 `data/raw/*.csv` 与 `data/campus.db`），因此这组数字属于「原文档已实测 + 脚本周内常量互证」，**不是本文实测**。

### 5.2 设计取舍：为什么恰好是 9 张表

`docs/DATABASE.md:359-367` 记录了七条取舍，与「简单优先」最相关的四条：

1. **不加字典表**：`weekday`、`time_slot`、`room_type`、`skill_category` 用 `TEXT`/`INTEGER` + `CHECK` 表达，表数被严格锁在 9 张（符合 `AGENTS.md:57`）。
2. **不存派生字段**：教室空闲、食堂拥挤度、技能覆盖率**都不落库**，否则会产生「排课更新了但空闲标记没更新」这类脏数据。
3. **多对多只做两跳**：岗位与课程之间不建直连表，全部经 `skills` 关联，「任取岗位 × 任取专业」都能现算。
4. **`importance` 是覆盖率的权重来源**：让「核心技能缺失」比「加分技能缺失」扣分更多。

其它约定：主键统一 `INTEGER PRIMARY KEY AUTOINCREMENT`；布尔用 `INTEGER + CHECK (x IN (0,1))`；时间/日期统一 `TEXT`（`HH:MM` / `YYYY-MM-DD`），便于 SQLite 的 `date()` / `strftime()` 直接处理；外键 `ON DELETE CASCADE` 并显式 `PRAGMA foreign_keys = ON`。

**防重复导入的唯一约束**：`UNIQUE(canteen_id, record_date, record_hour)`、`UNIQUE(course_id, skill_id)`、`UNIQUE(job_id, skill_id)`（`docs/DATABASE.md:88,124,135`）。

**索引**：共 9 个（`docs/DATABASE.md:345-354`），支撑找教室的核心是 `idx_schedules_time(semester, weekday, period_start, period_end)`——让「找出该时段已被占用的教室」走索引而不是全表扫描。

### 5.3 派生数据在哪里现算

三类派生数据全部在 Python 里现算：

**（1）教室是否空闲**——`queries.py::list_available_classrooms`（`:83-212`）分三步（全部参数化）：① **候选集**——按硬条件筛 `classrooms`（`capacity >= ?`、`has_projector >= ?`、`has_computer >= ?`、`status = 'available'`）；② **冲突集**——`SELECT room_id FROM schedules WHERE semester = ? AND weekday = ? AND period_start <= ? AND period_end >= ?`；③ **差集**由 Python 做（候选集 − 冲突集），再排序取前 N。

第 ② 步的 `period_start <= ? AND period_end >= ?` 是「区间相交」判断：既覆盖「3-4 节」，也覆盖「2-3 节」与「3-4 节」部分重叠（`docs/DATABASE.md:161-167`）。

**（2）食堂拥挤度**：`congestion = avg_people / avg_capacity`，归一化到 `0..1`；容量优先取 `meal_records.capacity` 快照，缺失时回退 `canteens.capacity`（`docs/API.md:182-183`）。原始值另存为 `congestion_raw`，`> 1` 表示超过同时在店容量。

**（3）技能覆盖率**：`backend/industry.py` 用集合运算 + `importance` 加权（`docs/DATABASE.md:213-218`、`industry.py:27-38`）——技能需求 = 对口岗位要求的技能（按 `min_importance` 过滤）；已覆盖 = 需求 ∩ 课程技能；缺失 = 需求 − 课程技能；覆盖率 = 已覆盖 / 需求；加权覆盖率 = Σ 已覆盖重要度 / Σ 需求重要度。

### 5.4 CSV → SQLite 导入管线

导入器是 [scripts/import_data.py](D:\Projects\campus-ai\scripts\import_data.py)（900 行），三个关键设计：

**（1）表结构的唯一来源是文档，且有反向校验。** `load_schema_ddl()`（`import_data.py:287-323`）在 `docs/DATABASE.md` 中定位标记 `SCHEMA_MARKER = "## 四、完整 CREATE TABLE SQL"`，从该标记起截取全文，用正则抽出 `CREATE TABLE` / `CREATE INDEX` 并补 `IF NOT EXISTS`；读完还会**反向校验**「文档表名集合 == 代码里的 `TABLE_NAMES`」且语句条数一致，否则直接失败（`import_data.py:313-322`）。因此文档写错、或改了表却漏改文档，都会在导入时立刻暴露，**代码里不存在第二份 DDL 副本**。

**（2）临时库 + 单事务 + 原子替换**（`import_data.py:863-877`）：

```
清理遗留 campus-build-*.db → mkstemp 建临时库
  → build_database()：一个事务内 DROP TABLE IF EXISTS（逆依赖序）→ CREATE → 建索引 → COMMIT
  → import_database()：先载入全部行并做 PRAGMA foreign_key_check 校验，再在一个事务里插入
  → 任何异常：清理临时库并原样抛出（保证原子性）
  → replace_database()：os.replace 原子替换（重试 5 次，退避 0.2×(n+1)s）
                        刻意不采用「先改名旧库再放新库」→ 避免「两个都不在」的中间态
  → 清理 -wal / -shm 残留
```

结果：`data/campus.db` 任何时刻要么是旧的完整库，要么是新的完整库，不存在半成品（`import_data.py:593-602,619,632-645,713-737`）。

**（3）可重复执行。** 每次都在全新临时库里 `DROP` + `CREATE` 重建，二次运行**不追加、不重复计数**，结果与首次一致（`import_data.py:595-598`，docstring `:15-19`）；所有插入语句都使用 `?` 参数化（`import_data.py:278,605-613`）。

**CLI 参数**（`import_data.py:763-781,831-861`）：`--verify-only`（要求库已存在，只数行数，任一表为空则退出码 1，不读 CSV 不写库）、`--no-rebuild`（只补索引）、`--quiet`（只打印表名与行数）、`--raw-dir` / `--db`（指定 CSV 目录 / 目标库）。导入器**只依赖标准库**（`argparse, contextlib, csv, os, re, sqlite3, sys, tempfile, time`），有测试断言（`tests/test_import_data.py:607-611`）。

### 5.5 `random.seed(42)` 与可复现性

模拟数据生成器是 [scripts/make_data.py](D:\Projects\campus-ai\scripts\make_data.py)（1618 行）。可复现性由三件事保证：

**（1）固定随机种子**：`SEED = 42`（`make_data.py:41`），实际播种 `random.seed(42)` 在 `main()` 内（`:1576`），依据 `AGENTS.md:149-157`。输出 9 个 CSV 到 `data/raw/`（写盘点 `:1591-1593`）。

**（2）生成后自动跑 8 组检查**（`run_checks()`，`make_data.py:1560-1567`，任一失败退出码 1）：

| # | 检查（代码位置） |
|---|---|
| 1 | 记录数检查（`:1123`） |
| 2 | 空字段检查（`:1136`） |
| 3 | 重复检查（`:1152`） |
| 4 | 课表冲突检查（`:1191`） |
| 5 | 教室容量与属性检查（`:1299`） |
| 6 | 食堂档案 / 营业时间 / 高峰与模式检查（`:1349`） |
| 7 | 课程 / 岗位技能关联检查（`:1463`） |
| 8 | 可复现性检查（SHA-256 前 16 位）（`:1540`） |

第 8 组就是「自己验自己可复现」：算一遍 9 个文件的 SHA-256 并比对。

**（3）期望行数写成脚本内断言**。`EXPECTED_COUNTS`（`make_data.py:480-491`）把期望行数固化在代码里，与 `README.md:185-195` 的 9 个 CSV 行数/字节数一致（classrooms 50/2570、courses 100/5848、schedules 1186/63270、canteens 5/673、meal_records 8480/421120、jobs 100/32581、skills 30/2522、course_skills 300/4861、job_skills 400/5352）。

> 上表为**原文档陈述**（`README.md:185-195` 的实测输出）。可复现性验证方式同样是原文档陈述：连续生成两次，9 个文件的 SHA-256 与仓库里已有的 `data\raw\*.csv` **完全一致**（`README.md:197`）。本文未重跑该脚本以保持数据文件不变（见附录 C）。

**双学期数据**：`schedules.semester` 区分学期，库中两个学期各有排课记录（`README.md:276` 列出 `2025-2026-1` / `2025-2026-2`；`Day18-…md:131` 记录陌生人测试时「双学期各 593 条课表」，即 `schedules` 合计 1186 = 593 × 2）。

### 5.6 数据真实性的诚实说明

`meal_records.source` 默认值为 `simulated`（`docs/DATABASE.md:293-294`），即**当前库中的人流数据全部是模拟生成的**，不是真实校园采集数据。必须讲清楚三件事：① 系统的**计算逻辑、分层架构、AI 边界**是真实可验证的；② 数据的**量级与分布**是模拟的，因此「预测准确度」这类结论不能直接外推到真实校园数据；③ 字段设计已为真实数据预留位置（`source` 可取 `real`），接入真实数据不需要改表结构。这一点在第 14 章作为独立不足条目呼应。

**本章证据**：`docs/DATABASE.md`、`scripts/make_data.py`、`scripts/import_data.py`、`backend/queries.py`、`backend/industry.py`、`AGENTS.md:149-173`、`README.md:169-227`、`docs/ai-collaboration/Day18-README与陌生环境复现.md`。

---

## 6. AI 架构：四层职责划分、意图白名单、AI 不生成 SQL、AI 不直接访问数据库

**目标**：这是全书最该讲透的一章——把「AI 只做两件事」变成可核验的工程事实。

### 6.1 四层职责划分

这是整个项目的架构基础，来自 `AGENTS.md:83-131` 的硬性规定，并在每个模块的 docstring 里逐条落实：

| 层 | 负责 | 明确**不**负责 | 代码位置 |
|---|---|---|---|
| **AI（大模型）** | ① 自然语言意图识别 ② 参数提取 ③ 图表选择 ④ 结果解释 | 不算数、不查库、不写 SQL、不编造事实 | `prompts/*.md` + `backend/ai_parser.py` |
| **Python** | ① 参数验证 ② 数据查询 ③ 业务逻辑 ④ 数据计算 ⑤ 预测 | 不解析自然语言 | `backend/validate.py`、`queries.py`、`classroom.py`、`forecast.py`、`industry.py` |
| **SQLite** | 保存事实数据 | 不做预测、不存派生状态 | `backend/db.py` + 9 张表 |
| **ECharts** | 可视化 | 不做业务计算 | `frontend/index.html` |

模块 docstring 里的原文表述（可直接引用）：`ai_parser.py:4-7`「AI → 识别意图、抽取参数、选图表、解释结果 / Python → 参数校验、数据查询、业务逻辑、计算、预测 / SQLite → 只保存事实数据」；`validate.py:4-7`「本模块**不 import sqlite3、不连数据库、不拼 SQL**」；`forecast.py:33-35`「唯一取数入口是 `queries.canteen_meal_grid`；模型只在连接关闭之后被调用，且只拿到只读事实表（没有表名、没有 SQL、没有数据库路径）」；`industry.py:40-51` 四条红线（Python 算全部匹配与覆盖率 / AI 不算覆盖率 / AI 不能修改计算结果 / AI 只能解释最终结果）。

### 6.2 意图白名单：两层防御

**第一层：Prompt 层（约束模型行为）。** `prompts/intent_parser.md` 声明 7 个受支持意图（`room_search`、`canteen_analysis`、`canteen_forecast`、`industry_skill_match`、`industry_skill_gap`、`dashboard_query`、`unknown`），并在 Rules 里写死约束（`prompts/intent_parser.md:25-60`）：只能使用上述 intent；无法判断必须返回 `unknown`；**不生成 SQL**；**不访问数据库**；不编造事实；时间必须结构化。

**第二层：代码层（约束不守规矩的模型）。** 白名单常量在 `validate.py:29-37`：

```python
INTENTS = ("room_search", "canteen_analysis", "canteen_forecast",
           "industry_skill_match", "industry_skill_gap", "dashboard_query", "unknown")
```

`validate_intent()`（`:471-541`）做四件事：① 意图必须在 `INTENTS` 里，否则 `ValidationError("不支持的 intent")`（`:496`）；② 每个意图有一张**参数字段白名单**（`room_search` 见 `:90-104`，其余见 `:106-151`）；③ **白名单外的字段被丢弃**并在 `notes` 留痕（提示「意图 X 不支持参数 Y，已丢弃该条件」，`:509-513,526-530`）；④ 每个字段值都过 `reject_sql()` 做 SQL 片段检查（见 13.3）。

**实际意义**：模型输出 `{"intent":"room_search","params":{"room_id":24}}` 时，`room_id` 不在白名单内会被直接丢掉——**AI 无法指定具体教室，只能指定条件**。边界细节：`room_search` 一个可用条件都没有时只给警告，只有传「空对象」才报错（`:531-539`）；`unknown` 不允许携带任何参数（`:499-500`）；`INTENT_ALIASES`（`:190-194`）与 `ALIASES`（`:162-186`）把口语化输出收敛到标准字段（`projector → has_projector`、`people → capacity`、`term → semester`、`end_period → period_end`）。

**两层的关系**：Prompt 是「请守规矩」，白名单是「不守也没用」。只靠前者，一次幻觉就能穿透；只靠后者，模型会频繁产出被丢弃的字段、浪费一次调用。两层都需要。

### 6.3 证明「AI 不生成 SQL」

**证据一：发给模型的内容里没有 SQL。** 三个 `build_insight_payload()`（`classroom.py:436-461`、`forecast.py:785-881`、`industry.py:740-779`）只组装**已算好的数值与中文条件描述**，不含表名、SQL、数据库路径；`classroom.py:439-440` 的注释写明这是「只读事实表」。

**证据二：模型输出的解释会被反向检查。** `_SQL_RE`（三个模块各一份：`classroom.py:75`、`forecast.py:104`、`industry.py:135`，正则匹配 `select|insert|update|delete|drop|alter|create|attach|detach|pragma|union|truncate|exec` 与 `--` / `/*` / `*/`，`re.IGNORECASE`）匹配 SQL 关键字与注释符，命中即抛 `AIParserError(code="unsafe_explanation")`，**整条解释退回 Python 兜底文案**（`classroom.py:464-475`、`forecast.py:1002-1013`、`industry.py:892-903`）。注意这是**输出侧过滤**，不用来改写任何查询——检查对象只有模型返回的解释文本。

**证据三：所有 SQL 都是 `?` 参数化的。** `WhereBuilder`（`db.py:65-97`）只拼接**由业务代码写死的列名与操作符**加占位符，值全部进 `params`（代码见 4.5 节）；参数入口另有一道 `validate.reject_sql()`（`validate.py:218-245`）拦截可疑取值（见 13.3）。这三条证据合起来的效果是：**AI 既拿不到数据库，也无法让 SQL 被改写**。

### 6.4 证明「AI 不直接访问数据库」

**最强的证据是执行顺序**：模型调用发生在 SQLite 连接关闭**之后**。三条链路结构一致：

```python
# classroom.py:672-688（结构示意）
with connection(db_path) as conn:            # ← 取数阶段：连接打开
    result = queries.list_available_classrooms(conn, query)
    hints = build_hints(conn, ...)           # 空结果时的放宽建议也在这里查
# ← with 块结束，连接已关闭
explanation = explain_result(result, hints, ...)   # ← 解释阶段：模型在这里被调用
```

`classroom.py:683-688` 的注释原文：「解释阶段：连接已关闭，模型只能看到事实表，没有任何查询能力」。同样结构见 `forecast.py:1178-1200`（报表在块内、解释在块后）与 `industry.py:1076-1100`（取数在块内、计算在块后、解释最后）。

**第二个证据是导入边界**：三个模块都**不 `import sqlite3`、不写 SQL**，唯一取数入口分别是 `queries.list_available_classrooms`（`classroom.py:19-20`）、`queries.canteen_meal_grid`（`forecast.py:33-35`）、`queries.industry_skill_grid`（`industry.py:42-43`）。这条约束有 AST 级测试断言（`tests/test_classroom.py:745-765`、`test_forecast.py:749,863`、`test_industry.py:1202`），见 13.3.1 节。第三个证据是每个模块的 `_SQL_RE` 复查（见 6.3）——模型即使「想」输出 SQL，也会被拦下并降级。

### 6.5 最强的一条红线：AI 不能修改计算结果

`/api/industry/match` 有一条硬红线（`README.md:293`、`docs/API.md:467-468`）：**解释阶段只允许往响应里加 `explanation`；`results_fingerprint`（SHA-256）在解释前后必须一致，否则直接抛 `RuntimeError`。**

实现细节（`industry.py`）：`results_fingerprint(report)` 对 `FINGERPRINT_KEYS` 做**排序 JSON 序列化后取 SHA-256**（`:121-132` 定义键、`:708-717` 实现）；解释**前**计算一次（`:1092-1093`），解释**后**重新计算并比对（`:1102-1105`）；不一致即 `RuntimeError("解释阶段修改了计算结果：AI 只能解释结论，不得改动技能需求/覆盖率/重要程度。")`，最终由通用异常处理翻译成 500（`app.py:141-144`）。

**必须写准的边界**：这道指纹校验**只有 `industry.py` 有**——`classroom.py` 与 `forecast.py` 中不存在指纹或等价的前后一致性校验（本文实测核对）。这两条链路的防改动手段是**输出形状收敛**：`_compose_explanation()` 只从模型输出里保留 `summary` / `highlights` / `suggestions` 三个字段（`classroom.py:491-503`、`forecast.py:1029-1041`）；`industry.py` 在此之上还有 `_insight_item()` 重建新字典并显式丢弃模型自造字段（如模型自己算的 `coverage_rate`）（`:782-797,919-936`）。

所以准确的说法是：**三条链路都保证「AI 只写解释」；其中产教链路额外用指纹把这条保证变成运行时可断言**。文档不应把指纹描述成三条链路的共有机制。

### 6.6 降级与容错：AI 故障不影响接口可用性

**统一约定**：解释结果里的 `explanation.source` 只有两个取值——`ai`（模型解释）或 `local`（Python 兜底文案）；失败原因写在 `explanation.error.code`（`docs/API.md:137-138`）。

**AI 调用可能产生的错误码**（常量定义在 `ai_parser.py:67-76`）：`missing_config`（缺配置）、`missing_prompt`（Prompt 缺失/为空）、`empty_question`、`network_error`（超时或连接失败）、`http_error`（非 200）、`invalid_response`（响应结构不符，含网关返回 `error`）、`invalid_json`、`truncated_response`（截断且修复失败）、`invalid_intent`（意图/参数未过白名单）。另有三个模块级解释错误码 `unsafe_explanation` / `invalid_explanation`（`classroom.py:71-72`、`forecast.py:100-101`、`industry.py:117-118`）。

**行为分叉（有意的不对称）**：

| 接口 | 解析不可用时 | 解释不可用时 |
|---|---|---|
| `/api/classrooms/search` | **503 + `ai_unavailable`**（消息带原因如 `missing_config`） | **200**，`source=local` |
| `/api/canteens/trend` / `peak` / `forecast` | 不涉及（参数来自查询串） | **200**，`source=local`，`error.code` 给出原因 |
| `/api/industry/match` | 不涉及 | **200**，`source=local`，覆盖率计算结果完全不变 |

理由很直白：**没有意图解析就不知道用户想查什么**，所以找教室接口必须失败；而结果解释只是「把数字讲成人话」，缺了它接口依然有完整价值（`README.md:416-429`）。

**截断响应的补救**：`extract_json_object()`（`ai_parser.py:201-247`）依次尝试直接解析 → 截取最外层对象 → `_repair_truncated_json()` 修复截断 JSON（最多修剪 40 次，`:317-340`）；仍失败且明显被截断则归 `truncated_response`，否则归 `invalid_json`。代码围栏也会被剥掉（`:218-221`）。

### 6.7 AI 调用参数与请求形态

| 项 | 值（位置） |
|---|---|
| 端点 / 认证 | `POST {AI_BASE_URL}/chat/completions`，`Authorization: Bearer <AI_API_KEY>`（`ai_parser.py:474-478`） |
| 请求体 | `model` / `messages` / `response_format` / `temperature` / `max_tokens`（`:458-466`） |
| 输出格式 | `response_format = {"type": "json_object"}`（强制 JSON）（`:458-466`） |
| `temperature` / `max_tokens` | `0` / `2048`（默认，可被 `AI_TEMPERATURE` / `AI_MAX_TOKENS` 覆盖）（`:60-61,141-142`） |
| 超时 | `30.0` 秒，作为 `requests.post(timeout=...)` 传入，**不是请求体字段**（`:56,484`） |
| 相对日期 | 由 Python 在解析后重算（`time_resolver.resolve`），不信任模型给的绝对日期（`:557-578`） |

**为什么温度是 0**：意图解析需要「同一句话稳定落到同一意图与同一参数结构」，温度非 0 会让同一问题在不同时刻得到不同参数，破坏可复现性；配合 `response_format=json_object` 把输出空间压到最小。

**一处工程现状**：三处 AI 调用点各自实现了同一套 HTTP 细节（`classroom.py:554`、`forecast.py:1093`、`industry.py:988`），而非共用 `ai_parser` 里的函数。这是一致性的重复，可列为可选重构点，但 `AGENTS.md:75` 明确「不进行大规模重构」，因此只适合在独立任务中谨慎处理。

**本章证据**：`AGENTS.md:83-147`、`ai_parser.py`、`classroom.py`、`forecast.py`、`industry.py`、`validate.py`、`db.py`、`docs/API.md`、`README.md:318-340`。

---

## 7. Prompt 设计：`prompts/` 下 4 个运行时 Prompt 各自职责与调用链

**目标**：把「prompts/ 是代码资产」这件事讲清楚，并给出可追溯的加载点。

### 7.1 两个目录别搞混

项目里有两个长得像的目录，职责完全不同（`prompts/PROMPTS-README.md:9-14`、`README.md:346-356`）：

| | `prompts/` | `docs/ai-collaboration/` |
|---|---|---|
| 内容 / 消费者 | 程序**运行时**的 system prompt / Python 代码 | 「人如何指挥 AI 把项目做出来」的过程记录 / 评审与未来的自己 |
| 作用 / 改坏会怎样 | 决定线上行为 / 功能与测试失败 | 证明 AI 协同开发过程（逐字 Prompt、真实回复、具体 Bug、验证输出） / 无影响 |
| 谁的证据 | 产品能力证据 | **AI 协同开发能力证据** |

一句话：**`prompts/` 是给程序吃的，`docs/ai-collaboration/` 是给人看的。**

### 7.2 4 个 Prompt 的职责与加载点

| Prompt 文件 | 职责 | 加载位置（代码）→ 输出被谁消费 |
|---|---|---|
| [intent_parser.md](D:\Projects\campus-ai\prompts\intent_parser.md) | 自然语言 → 结构化 intent + 参数；**不回答用户问题** | `ai_parser.py:50`（`DEFAULT_PROMPT_PATH`），由 `load_prompt()`（`:175-187`）在 `IntentParser.system_prompt`（`:448-452`）懒加载 → `validate.py` 白名单校验 |
| [insight_generator.md](D:\Projects\campus-ai\prompts\insight_generator.md) | 只解释「找教室」结果；原文明确「你不是查询器，也不是分析器」「你不访问数据库 / 你不生成 SQL」 | `classroom.py:55`（`DEFAULT_INSIGHT_PROMPT_PATH`），消费于 `ResultExplainer.system_prompt`（`:531-533`）→ `explanation{text,summary,highlights,suggestions}` |
| [canteen_insight_generator.md](D:\Projects\campus-ai\prompts\canteen_insight_generator.md) | 只解释已算好的食堂统计与预测；原文明确「不重新统计人流、不重新算高峰、不重新预测」「不做任何加减乘除」 | `forecast.py:64-66`，消费于 `ForecastExplainer.system_prompt`（`:1067-1072`）→ 同上 |
| [industry_insight_generator.md](D:\Projects\campus-ai\prompts\industry_insight_generator.md) | 只解释已算好的岗位技能需求与课程覆盖；原文明确「不重新做技能匹配、不重新取交集或差集、不重新算覆盖率」 | `backend/industry.py:77-79`，消费于 `IndustryExplainer.system_prompt`（`industry.py:962-967`） | 同上 |

> 上表的加载点行号与 `prompts/PROMPTS-README.md:9-14` 的声明一致，本文在代码中逐条核对通过。

**一个设计上的关键分工**：4 个 Prompt 里只有 `intent_parser.md` 是「输入端」Prompt，另外 3 个都是「解释端」Prompt，分别绑定一条业务链路。它们不会互相串用——找教室链路永远不会读到食堂的解释 Prompt，因为路径是各模块内的常量（`DEFAULT_INSIGHT_PROMPT_PATH`），不是运行时拼出来的。

### 7.3 调用链

以找教室为例（`prompts/PROMPTS-README.md:16-26`、`classroom.py:4-11`）：

```
用户提问 → classroom.py::search_classrooms()
  → load_prompt("prompts/intent_parser.md")      # ← 本目录的文件在这里被读走
  → POST {AI_BASE_URL}/chat/completions（role=system）
  → 返回 JSON → validate.py 白名单校验 → SQLite 查询（查询完连接关闭）
  → load_prompt("prompts/insight_generator.md")   # ← 本目录的文件再次被读走
  → AI 只解释结果 → explanation{text,summary,highlights,suggestions}
```

同一个 `intent_parser.md` 服务三条链路的解析阶段；三个 `*_insight_generator.md` 各服务一条链路的解释阶段。

### 7.4 机械实现细节

**（1）加载与转义还原。** `load_prompt()`（`ai_parser.py:175-187`）逻辑为：文件不存在或内容为空白 → 抛 `AIParserError(code="missing_prompt")`；否则 UTF-8 读取并在 `unescape=True` 时过 `unescape_prompt()`——后者（`:166-172`）只还原「反斜杠 + ASCII 标点」这一种形态（正则 `_MARKDOWN_ESCAPE_RE`，`:163`），例如 `room\_search` → `room_search`，不触碰中文与代码块内容；该函数有单测锁定（`tests/test_intent.py:1049-1056`）。

```python
if not os.path.isfile(path): raise AIParserError(..., code="missing_prompt")
prompt = open(path, encoding="utf-8").read()
if not prompt.strip():       raise AIParserError(..., code="missing_prompt")
return unescape_prompt(prompt) if unescape else prompt
```

**（2）参数契约由代码注入 Prompt。** `build_messages()`（`ai_parser.py:190-198`）在 `contract=True` 时，把 `validate.render_contract()`（`validate.py:552`）生成的**参数契约文本**拼到 system prompt 之后——白名单改了，Prompt 里的契约自动跟着变，不存在两处各维护一份、改一处漏一处的风险。

**（3）历史遗留问题：文档陈述与实测不一致（本文实测）。** `prompts/PROMPTS-README.md:53-60` 声称存在两类残留，实测结果如下：

| 文档声称 | 本文实测结果 |
|---|---|
| **Markdown 转义残留**（`#`×9 / `.`×15 / `-`×26） | `prompts/` 下 5 个 `.md` 文件的反斜杠数为 **0**，转义标点序列匹配数为 **0**——该残留**已不存在**，文档该段与当前代码不符 |
| **每行之间多出两个空行** | 确实存在，且**只在 `intent_parser.md`**：全文 323 行中 **211 行为空行**（内容仅 112 行）；其余三个 Prompt 各约 20 个空行（正常形态） |

因此本文**不照抄该段结论**，按实测状态记录为「`intent_parser.md` 多余空行」问题。该文档提出的整理方案（`prompts/PROMPTS-README.md:63-69`）仍有价值，但第 3 条「断言不再出现转义残留」在当前状态下属于**防未来退化**，而非修复现存问题。

### 7.5 治理规则

Prompt 是**代码资产**，不是笔记（`prompts/PROMPTS-README.md:28-32`、`AGENTS.md:177-189`）：

1. 改动会影响线上行为，**必须走 Git 留痕**；
2. 改动后必须跑 `pytest -q`，确认全量用例仍然全绿；
3. 不要把「我给 Agent 下过什么指令」这类对话记录写进本目录；
4. 不要在本目录存放临时稿、草稿、截图。

这条治理规则在 Git 历史里有直接体现：仓库中存在以 `prompt:` 为主体的提交类型，例如 `7660937` 与 `529af05` 的提交主题都是 `prompt: improve intent parsing robustness and add time resolver`（2026-09-27）——把 Prompt 修改单独成一次提交，便于回溯（见 11.3 节）。

**一处已知的文档陈旧（如实记录）**：`prompts/PROMPTS-README.md:31` 写「确认 597 个用例仍然全绿」，而当前实际为 **598**。这个发现最初由 AI 在 Day18 会话中主动报出，随后由人工核实确认（`docs/ai-collaboration/Day18-README与陌生环境复现.md:69,75,186`），截至本文档编写时**尚未修正**。详见第 14 章不足 #1。

**本章证据**：`prompts/PROMPTS-README.md`、`prompts/intent_parser.md`、`prompts/insight_generator.md`、`prompts/canteen_insight_generator.md`、`prompts/industry_insight_generator.md`、`backend/ai_parser.py:50,105-198,448-452`、`backend/classroom.py:55,531-533`、`backend/forecast.py:64-66,1067-1072`、`backend/industry.py:77-79,962-967`、`backend/validate.py:552`、`tests/test_intent.py:1049-1056`。

---

## 8. 三个核心流程详述

**目标**：每个流程都给出「输入 → 校验 → SQL/计算 → 输出 → AI 位置」的完整可核验链条。

### 8.1 流程一：AI 找教室

**实现文件**：`backend/classroom.py`（编排与解释）、`backend/queries.py`（查询）、`backend/ai_parser.py`（解析）、`backend/validate.py`（白名单）、`prompts/intent_parser.md` + `prompts/insight_generator.md`。

#### 8.1.1 端到端链路

```
question（自然语言）
  → routes.py:221 classrooms_search() → classroom.py:640 search_classrooms()
  → check_question()（长度 ≤200，:124-136）→ _parse_question()（:597-627）
      → IntentParser.parse()（ai_parser.py:492-508）→ call_api() → parse_response_text()
        → extract_json_object() → _resolve_payload_dates() → time_resolver.resolve()
      → validate_intent() 白名单（validate.py:471-541）
  → validate_room_params() 业务校验（classroom.py:146-216）
  → with connection(): queries.list_available_classrooms()（queries.py:83-212）
      + build_hints()（仅在空结果时，classroom.py:344-370）
  ← 连接关闭
  → explain_result() → ResultExplainer.explain()（classroom.py:577-590）
  → 封装为 {intent, params, query, total, summary, rooms, hint, notes, explanation}
```

链路中有两处「解析失败即中止」的分支（`classroom.py:602-626`）：解析整体不可用（缺 Key、网络失败、输出不合法）→ 抛 `ClassroomSearchError(code="ai_unavailable", status_code=503)`；解析成功但意图不是 `room_search`（例如问了食堂）→ 抛 `ClassroomSearchError(code="unsupported_intent", status_code=400)`，并在消息里提示这属于哪类问题（`INTENT_LABELS`，`:83-91`）。

#### 8.1.2 业务层校验（第二道校验，比白名单更具体）

`validate_room_params()`（`:146-216`）在意图白名单之外补了业务规则：① 与「教室空闲」无关的字段被丢弃并给警告——`date` / `date_text` / `date_range_text`（常量 `_IGNORED_ROOM_FIELDS`，`:94`，处理在 `:173-180`），理由是教室按**星期 + 节次**定位空闲、不按具体日期（`:93` 注释）；② 至少要有一个可用筛选条件，否则报错（`:163-167,182-186`）；③ **只给 `weekday` 不给节次**时无法判断空闲，直接 `ValidationError`（`:191-196`）；④ 节次只给一端时按同一节处理（`:197-200`）；⑤ 最终查询条件由代码拼装，`status` 固定 `available`，`limit` 缺省 `DEFAULT_RESULT_LIMIT = 10`（`:202-215`，常量在 `:62`）。

#### 8.1.3 查询三步与排序

详见 5.3 节的 SQL。排序规则在 `queries.py:173-180`：`room["_capacity_gap"] = max(0, row["capacity"] - capacity) if capacity else 0`，然后 `rooms.sort(key=lambda r: (r["_capacity_gap"], r["capacity"], r["floor"] or 0))`——即**先「刚好够用」**（容量超出需求最少的优先），再容量升序，再楼层升序（低楼层优先）。`limit` 只截断 `rooms` 数组，`total` 始终是完整命中数（`docs/API.md:84`）。

#### 8.1.4 双学期兜底（本项目的一个真实修复）

**问题是什么**：库里有两个学期的排课数据之后，前端请求**从不带 `semester`**（用户问句里也常常没有学期），而旧逻辑要求「指定 `weekday` 时必须同时提供 `semester`」——结果招牌问句**必然返回 400**。

**修复后的准确行为**（必须写准，避免把两个不同机制混为一谈）：① **服务端「最新学期默认」**（`queries.py:102-122`）——未传 `semester` 且库中存在排课数据时自动采用**最新学期**（`available_semesters()` 已按学期倒序，取首个）；② **服务端「无排课数据」**（`:113-118`）——库中完全没有排课数据时报 400，因为此时无法判断「空闲」，报错比把全校教室都算成空的更安全；③ **服务端「指定学期不存在」**（`:119-122`）——404 + `available` 学期清单，**不重试其它学期**；④ **前端「保持上一份快照」**（`index.html:1857-1877`）——用户选了「库里没有排课数据」的学期时保持上一份快照，并提示「学期 X 暂无排课数据，已回退至 Y 快照」。

**关键表述**：后台**并不存在**「请求的学期没数据就换另一个学期重试」的逻辑——后端要么用最新学期、要么报错，「回退」是前端快照层面的行为。另一个容易混淆的命名：`SEMESTER_PENDING = '2026-2027-1'`（`index.html:1008`）是**业务预留学期**（库里暂无排课数据），被选中时走上面的「回退提示」分支，与「双学期兜底」不是同一件事。**历史证据**：commit `dd56c8f`（`fix: resolve semester fallback for dual-semester data`，2026-10-03），改动 `backend/queries.py` 与 `tests/test_api.py`（本文实测 `git show --stat`）——「真实 Bug → 真实修复 → 测试加固」的完整闭环样本。

#### 8.1.5 空结果放宽建议（`hint[]`）

当查询命中 0 间教室时，系统不让模型猜「可能是因为…」，而是**再查一次 SQLite**给出可执行建议（`classroom.py:28-29`）：① 6 类候选放宽（`_relaxation_probes`，`:294-341`）——去掉设备条件、去掉人数、去掉教室类型、去掉楼栋、节次前移 1 节、节次后移 1 节；② 每个候选都重查一次数据库且**只取 `limit=1` 的计数**（`_probe_total`，`:279-285`；遇校验错误或不存在返回 0，不让提示本身失败）；③ 上限 `MAX_RELAXATION_PROBES = 4`（`:68`，执行处 `:352-353`）；④ 全部候选无果时给通用提示（`:365-369`）。

这样得到的提示是「不限制人数时，同一时段有 14 间空教室，但可能坐不下 1000 人」这种**有数据支撑**的建议，而不是模型的推测。

#### 8.1.6 输出结构与错误码

响应字段（`docs/API.md:109-141`）：`intent`（固定 `room_search`）、`params`（**校验后**的意图参数）、`query`（回显查询口径）、`total`（完整命中数，不受 limit 影响）、`summary`（候选/已占用/空闲/容量窗口）、`rooms[]`、`hint[]`（空结果放宽建议，由 `build_hints` 再查库）、`notes[]`（解析层丢弃提示、limit 截断说明、已知语义缺口）、`explanation{text, summary, highlights, suggestions, source, error}`。错误：400 `unsupported_intent`（问的不是找教室）、400 `invalid_parameter`、503 `ai_unavailable`（`:143-147`）。

### 8.2 流程二：食堂预测

**实现文件**：`backend/forecast.py`（统计、预测、解释）、`backend/queries.py`（取数 `canteen_meal_grid`）、`prompts/canteen_insight_generator.md`。

#### 8.2.1 统一口径：先聚「客流单元」

三张报表（趋势 / 高峰 / 预测）共用同一份数据底座（`forecast.py:27-31`）：**先把「同一日期 + 同一小时」的多条记录（多食堂）合计成一个客流单元，再对所有均值做统计**。因此 `avg_people` 的含义随筛选变化：指定单个食堂时 = 该食堂该小时平均人流；不指定时 = 全校该小时平均总人流。这个口径差异必须写明，否则「平均 583.9 人」这类数字会被误读。

#### 8.2.2 预测公式与实现

公式（`docs/API.md:280-286`、`forecast.py:14-25`）：

```
B = 训练窗口内「同一日期 + 同一小时」客流的算术平均
ΔW(w) = 星期 w 的历史平均 − B ；ΔH(h) = h 点的历史平均 − B
预测(t, h) = max(0, B + ΔW(weekday(t)) + ΔH(h))
```

实现细节（`forecast.py::_forecast_report`，`:654-759`）：取时段缺省 `LUNCH_SLOT = "lunch"`（`:655,77`）→ `_scoped_cells()`（`:310-320`）聚客流单元 → `_mean(cells.values())` 得基准均值 `B`（`:659-660`，无数据时 `None`）→ `_samples_by_weekday()` / `_samples_by_hour()` 得星期与小时均值（`:360-373`）→ 星期修正 `weekday_avg.get(wd, base) - base`（`:702`，缺样本时为 0 并追加 note）→ 时段修正 `hour_avg.get(h, base) - base`（`:706`）→ **唯一的非负截断** `max(0.0, base + wd_adj + h_adj)`（`:707`）→ `_round_people()` 取整（`:119-125,708`）→ 各小时求和得日合计（`:709`）。起始日由 `_forecast_start_date()`（`:640-651`）决定（显式 `target_date` → 训练窗口最后一天 +1 → 今天），天数默认 3、上限 30（`:79-80`）。

取整**刻意不用 `round()`** 而用 `floor(x + 0.5)`，避免 .5 上的银行家舍入抖动（注释在 `:121-124`）。

**三件事让预测可被检验**：① 每小时都返回 `base` / `weekday_adjustment` / `hour_adjustment`，可原地复算（`docs/API.md:320`）；② 没有历史数据时返回 `predictions: []` + `notes` 说明（`forecast.py:733-737`），**不硬凑数字**；③ `method` 字段固定回显 `FORECAST_METHOD = "历史同期平均值 + 星期修正 + 时间段修正"`（`:69`），让前端与测试都能锁定方法未变。

#### 8.2.3 趋势判定与高峰区间

**趋势**：`trend.direction` 取 `up` / `down` / `flat`——把序列按前后半段比较日均人流，变化率在 ±`FLAT_TOLERANCE`（`0.05`，`forecast.py:85-86`）内即为 `flat`（`docs/API.md:233`），另有 `compared_days` / `first_half_avg` / `second_half_avg` / `change_rate` 便于核对。**高峰**：算法是**纯比较，不做回归**（`docs/API.md:247-249`）——从峰值小时向两侧扩张，只吸收**逐个小时相邻**且 `avg_people >= 峰值 × peak_ratio` 的小时；`peak_ratio` 默认 `0.8`、范围 `0.1..1.0`（`forecast.py:81-84`）；`window.label` 右端点已 +1 小时，例如 `"11:00-13:00"` 表示 11 点与 12 点这两个小时。

#### 8.2.4 字段口径陷阱与错误码

`hours[].avg_people` 是**每小时**的平均客流，而 `slots[].avg_people_per_day` 是**每天**该时段的合计平均——两者口径不同，因此**故意用了不同的字段名**（`docs/API.md:273-274`）。这是实际踩过的坑，文档里要显式提醒。错误：400（日期 / 时段 / `horizon_days` / `peak_ratio` 非法）、404（`canteen_id` 不存在，附 `available` 食堂名）（`:326`）。

### 8.3 流程三：产教技能匹配

**实现文件**：`industry.py`（匹配、覆盖率、解释）、`queries.py`（取数 `industry_skill_grid`）、`prompts/industry_insight_generator.md`（解释 Prompt）。

#### 8.3.1 五个输出

`industry.py:14-20` 定义了与任务口径一一对应的五个输出：① **技能需求** `skill_demands[]`（岗位要求的技能全集，每条含 `importance`、`importance_level`、`job_count`、`required_job_count`、`is_required`、`covered`、`course_level`）；② **已覆盖技能** `covered_skills[]` = 技能需求 ∩ 课程技能；③ **缺失技能** `missing_skills[]` = 技能需求 − 课程技能，另有 `required_missing_skills[]`（硬性要求里未覆盖的）；④ **技能覆盖率** `coverage_rate` = 覆盖数 / 技能需求数，另有 `summary.weighted_coverage_rate`；⑤ **技能重要程度** `importance`（`scale`/`max`/`avg`/`level` + 逐档 `distribution[]`，每个技能上还有 `importance_level`）。

#### 8.3.2 计算定义（全部 Python）

`industry.py:27-38` 写明了定义：

```
技能需求     = 该专业对口岗位要求的全部技能（按 min_importance 过滤后）
已覆盖技能   = 技能需求 ∩ 课程覆盖技能        缺失技能 = 技能需求 − 课程覆盖技能
技能覆盖率   = |已覆盖技能| / |技能需求|       加权覆盖率 = Σ 覆盖重要度 / Σ 需求重要度
技能重要程度 = 该技能在所有对口岗位上被要求的最大重要度
```

**为什么重要度取「最大」而不是求和**（`:36-38`）：`importance` 是「岗位 × 技能」的属性，按技能聚合后用最大值代表「这个技能最多被要求到多重要」，因此越重要的技能缺失，对加权覆盖率的拉低越明显。

关键实现位置：`build_major_coverage()`（`:278-332`，`LEVEL_RANK = {"了解":1,"掌握":2,"熟练":3}` 在 `:86`，每技能取跨课程最大等级 `:318-320`）；主计算 `calculate_skill_gap()`（`:555-701`）；覆盖率 `_coverage_block()`（`:398-417`，`coverage_rate = round(len(covered)/required_count, 4)` 在 `:411`）；加权覆盖率（`:402-403,412-414`）；反向指标 `course_covered_ratio`（`:616-626`）；交/差运算 `_split_items()`（`:389-395`）；重要度分档 `IMPORTANCE_LEVELS`（`:89-95`，5=核心 / 4=重要 / 3=较高 / 2=一般 / 1=次要）；逐岗位匹配 `match_job_to_major()`（`:24` 声明）。

#### 8.3.3 AI 在这条链路里的位置

**取数与计算在连接内，解释在连接外**：`industry.py:1076-1077`（取数）→ `:1079-1090`（Python 计算）→ `:1096-1100`（AI 解释）。**AI 只拿到聚合结果**：`build_insight_payload()`（`:740-779`）只复制聚合值与技能行，每一行经 `_insight_item()` 重建为新字典（`:782-797`），模型自造的 `coverage_rate` 字段会被丢弃（`:919-936`）。**指纹校验**：解释前 `:1092-1093` 计算 SHA-256，解释后 `:1102-1105` 比对，不一致抛 `RuntimeError`（详见 6.5 节）。

#### 8.3.4 交叉校验：两个实现互为证据

`/api/industry/gap`（`queries.industry_skill_gap`）与 `/api/industry/match`（`industry.analyze_skill_gap`）是**两份独立实现**，对同一专业的 `summary` 必须逐项一致（`docs/API.md:458-459`）；`tests/test_industry.py` 也用它做交叉核对（另建合成库，并用「直连 sqlite3 重写一遍」的参考实现核对真实库上的覆盖率）。

`docs/API.md:426-432` 给出的「软件工程」示例输出为：17 个对口岗位、13 门课程、`coverage_rate = 0.5882`、`weighted_coverage_rate = 0.6081`、`course_covered_ratio = 0.871`。

> **数据来源说明**：以上数字来自 `docs/API.md` 的响应样例，属**原文档陈述**。本文未对 `data/campus.db` 实跑该接口复核（保持数据文件不变）。若要写成「真实输出」，应在隔离副本中实跑并粘贴原始响应。`README.md:427` 另记录一次实测：清空 `AI_API_KEY` 后 `/api/industry/match?major=软件工程` 返回 `coverage_rate=0.5882`，与配好 Key 时一致。

#### 8.3.5 排序与截断

三张技能清单的排序规则：**重要度降序 → 缺口优先 → 岗位数降序 → 名称升序**；`top` 只截断清单本身，`summary` 里的计数**始终是全量**（`docs/API.md:461-463`）。`min_importance` 是**技能级**门槛：留下来的技能，其 `job_count` / `is_required` 仍按全部岗位汇总，不会因为某个岗位重要度低就丢掉「硬性要求」这一事实（`:463-464`）。

错误：400（缺 `major`，或 `min_importance` / `top` 越界；缺 `major` 时附可选专业清单）、404（专业不存在，附 `available` 清单）（`docs/API.md:470`）。

**本章证据**：`docs/API.md`、`docs/DATABASE.md:139-218`、`classroom.py`、`queries.py:57-212`、`forecast.py`、`industry.py`、`validate.py`、`frontend/index.html:1008,1857-1877`、`tests/test_api.py`、`tests/test_industry.py`。

---

## 9. 前端设计：大屏、5 个快捷按钮、离线降级标注、双学期切换

**目标**：讲清「无框架单文件」如何做到既有大屏观感、又有真实降级策略。

### 9.1 技术选择

前端是**单文件** [frontend/index.html](D:\Projects\campus-ai\frontend\index.html)，HTML + CSS + JS 全部内置，不用任何前端框架——这是 `AGENTS.md:61-63` 明确禁止 Vue / React 的结果。可视化用 ECharts 5.5.1，走 CDN 引入并配了备用 CDN（`README.md:25-26`）。

单文件实测 **2772 行 / 139,101 字节**（LF 换行；用 Python 的 `splitlines()` 计得；注意 PowerShell 的 `(Get-Content).Count` 会少报成 2661，因为它对无尾换行的最后一行处理不同）。

### 9.2 大屏形态与接口基址

设计基准 **1920 × 1080**，直接双击打开（`file://`）即可，**不需要额外的前端服务器**（`README.md:243-251`）。接口基址 `API_BASE` 按协议自动选择（`index.html:1000-1006`）：

```js
// http(s) 打开 → 同源 /api；file:// 打开（无 origin）→ 本机后端
protocol 匹配 /^https?:$/ ? location.origin + '/api' : 'http://127.0.0.1:5000/api'
```

这让同一个文件既能「双击打开」又能「被 Web 服务器托管」，两种方式都不用改代码。

### 9.3 5 个快捷按钮

按钮定义在 `index.html:606-610`（本文实测确认原文），每个绑定一句真实问句，并在在线时打对应实时接口（映射来自 `Day16-稳定性检查.md:42`）：

| 按钮（绑定问句） | 在线时调用的接口 |
|---|---|
| 找空教室（「周三下午帮我找一个 8 人、有投影的空教室」） | `/api/classrooms/search`（完整 AI 链路） |
| 食堂拥挤预测（「今天中午哪个食堂最拥挤？需要等多久？」） | `/api/canteens/stats` + `/api/canteens/forecast`（并发） |
| 技能缺口分析（「计算机科学与技术专业和岗位技能还差哪些？」） | `/api/industry/gap` |
| 教室利用率（「本学期的教室利用率怎么样？」） | `/api/classrooms/available` |
| 岗位技能需求（「现在全校有多少个在招岗位？平均薪资多少？」） | `/api/industry/skills` + `/api/industry/gap`（并发） |

> **必须写清的历史事实**：「在线时走实时接口」**是 Day16 修复的结果，不是一开始就这样**。修复前 `ask()` 里是 `useBackend = apiOnline && (!localHit || localHit.key === 'room_search')`——只要问题命中知识库条目（食堂/技能/岗位/利用率），`useBackend` 即为 `false`，**无论后端在不在线都走内置快照**（`Day16-稳定性检查.md:41-42,53`）。

### 9.4 离线降级标注

本项目在「诚实性」上做得比较扎实的一处：**离线就是离线，页面会明确标出来**。四个可见元素（`index.html`）：离线来源署名「数据源：页面内置离线快照（未连接后端）」（`:2054`）、在线/离线后缀 ` · 离线示例数据（非后端实时结果）`（`:2067-2068`）、可见徽标 `离线示例`（`:2346`，title 说明「本条回答来自页面内置离线快照，不是后端实时查询结果」）、状态角标「离线快照」（`:2669`）与图表兜底「图表引擎未加载，无法渲染」（`:527,545,563`）。

徽标显示条件是该条回答的 `data-origin` 属性（`:2340-2343`）：`live` = 后端实时接口，`offline` = 内置离线快照。**两个容易被忽略的细节**：① **不含数据的回答不打徽标**——例如「这个问题我不会，我只能回答教室、食堂、产教技能相关的问题」本来就是能力范围说明（`:2298,2343-2346`）；② **回落条件很严格**，只有「后端真的连不上」或「该主题接口全部失败」才回落（`:2602`），单个接口失败不会静默降级成假数据。

### 9.5 双学期切换

交互结构（本文实测行号）：`#semester-field`（`:441`）内含 `<select id="semester-select">`（`:443`），另有快照窗口 `<select id="snapshot-select">`（`:447-449`）与提示条 `#semester-notice`（`:459`）；内置快照覆盖两个学期，标注「秋季学期」（`:869`）/「春季学期」（`:922`），顺序常量 `semesterOrder: ['2025-2026-1','2025-2026-2']`（`:977`）。

**运行时行为**：① 下拉框**先按内置快照渲染**（离线也能用），再请求 `GET /api/meta/semesters` 拿库里真实有排课记录的清单并刷新（`:2626-2653,2680`），因此「页面能打开」不依赖后端；② 切成「库里没有排课数据」的学期（如预留的 `SEMESTER_PENDING = '2026-2027-1'`，`:1008`）时走 `selectSemester()` 的降级分支，**保持上一份快照**并提示「学期 X 暂无排课数据，已回退至 Y 快照」（`:1857-1877`）；③ 后端不会为「没有数据的学期」编造数据——`queries.list_semesters` 只返回 `schedules` 里真实出现过的学期（`queries.py:57-65`）。

### 9.6 超时与降级的三条规则

Day16 修复留下的三条规则（`Day16-稳定性检查.md:36,39,51`）：① **含 AI 解释的接口**用 `AI_TIMEOUT_MS = 15000`——找教室要走两次模型调用、实测 3.3–5.7 秒，而旧代码写死 **2600ms** abort，**必然**每次落入降级分支使页面永远显示「离线示例」（这就是 Day16 修复的 Bug）；② **纯元信息探测**用 `PROBE_TIMEOUT_MS` = 2.5 秒——`/api/meta/semesters` 不吃大模型，给 15 秒会让「后端没起来」时页面愣 15 秒反像卡死；③ **回落条件**是「后端连不上」或「该主题接口**全部**失败」，避免单个接口抖动就整页降级。配套的 `ensureOnline()` 会在提问时若 `apiOnline` 仍为 `false`，按需再轻探一次（2.5 秒），探到就转在线并刷新角标。

### 9.7 前端不承担业务计算

在线回答只做一件事：**把接口字段拼成展示文案**，不做任何业务运算，并标注来源（如「实时接口: GET /api/canteens/stats」）（`Day16-稳定性检查.md:42`），与 `AGENTS.md:125-131` 的分工一致。图表数据直接取接口返回的 `series` / `report`——`/api/canteens/trend` 专门把 `report.trend.series` 平铺到 `data.series`，就是为了让前端按任意日期窗口重绘趋势图而不用自己算（`routes.py:191-208`）。

**本章证据**：`frontend/index.html`（实测 2772 行）、`README.md:243-251`、`docs/ai-collaboration/Day16-稳定性检查.md`、`backend/routes.py:191-208,299-307`、`backend/queries.py:57-65`、`AGENTS.md:61-63,125-131`。

---

## 10. 测试体系：598 个用例怎么分层

**目标**：不只报「598 passed」，而是讲清这 598 个用例覆盖了哪些层、怎么保证离线可跑。

### 10.1 真实结果：598 个用例全绿

**实测命令与输出**（项目根目录，项目虚拟环境）：`.\.venv\Scripts\python.exe -m pytest -q` → `598 passed`。

本文编写期间先后跑了三次，三次都是 `598 passed`：**8.55s**（写大纲阶段）/ **7.64s**（写正文阶段复跑）/ **7.65s**（定稿前复跑）。采集计数也是 598：`pytest -q --collect-only` → `598 tests collected`。

### 10.2 596 与 598 的差异必须写清楚

**结论：598 是「本机（`.env` 已配置 Key）跑出来的数字」；在没配 `.env` 的机器上会是 596 passed + 2 skipped。**

原因：`tests/test_intent.py` 中有 **2 个用例**以「真实 `AI_API_KEY` 存在」为执行条件（`pytest.mark.skipif`，读取逻辑在 `tests/test_intent.py:60-67`）：

| 用例 | 作用 |
|---|---|
| `test_real_api_key_never_leaks_into_results`（`:988-996`） | 断言真实 Key 不出现在成功或失败的响应/结果里 |
| `test_api_key_is_not_hardcoded_in_source`（`:999-1003`） | 断言真实 Key 没有被硬编码进 `ai_parser.py` / `validate.py` |

- **本机 `.env` 有 Key** → 条件成立 → 两个用例**实际执行**并计入 → **598 passed**；
- **陌生环境没配 `.env`** → 条件不成立 → 两个用例 **skip** → **596 passed, 2 skipped**。

**这不是缺陷，是设计**：这两个用例本身就是「Key 不泄漏」的断言，没有 Key 时无从断言，跳过是正确行为。但引用数字时**必须同时说明这个前提**，否则评审在自己机器上跑出 596 会以为对不上。另外，这两个用例即使执行也用 `FakeSession` 注入假响应（仍然离线），只检查「真实 Key 字符串有没有出现在任何输出里」。

### 10.3 分层结构

**逐文件实测采集数**（`pytest -q --collect-only tests/<file>.py`）：

| 测试文件 | 用例 / 函数 | 覆盖层级与做法 |
|---|---|---|
| `tests/test_api.py` | 48 / 44 | **接口层**：真实 `app.test_client()`（`:74-76`），覆盖 4 个确定性接口、200/400/404/405/503、参数化查询注入安全、路由与业务分离、断言路由层不调用 AI |
| `tests/test_classroom.py` | 66 / 39 | **端到端 + 单元**：完整走 解析→校验→查询→解释；`ast` 检查不 import sqlite3、不含 SQL（`:745-765`）；注入 `FakeExplainer`（`:183-195`）；与直连 `sqlite3` 的参考实现核对 |
| `tests/test_forecast.py` | 45 / 32 | **单元（精确）+ 集成**：另建 84 天**合成库**（`:142-146`）对预测公式做精确断言；含「无 SQL、无 ML」断言（`:749,863`） |
| `tests/test_industry.py` | 49 / 41 | **单元（合成）+ 接口集成**：覆盖率/覆盖与缺口/重要度；合成库 6 技能 / 4 课程 / 5 岗位（`:205-209`）；含对抗性 explainer（`:1053,1093`）与「AI payload 不含 SQL/表名/明细行」断言 |
| `tests/test_intent.py` | 288 / 82 | **单元 + golden 集成**：`FakeSession`（`:84-96`）、配置、Prompt 文件、`response_format=json_object`、白名单校验、超时/网络/非 200/降级路径、40 条 golden 问句 |
| `tests/test_time_resolver.py` | 70 / 31 | **纯单元**：时间冻结在 `ANCHOR = Anchor(today=2026-09-27, data_max_date=2025-11-14)`（`:25-27`），**从不调用 `date.today()`** |
| `tests/test_import_data.py` | 32 / 32 | **导入单元/集成**：临时 CSV + 临时库上验证 9 张表、重复执行幂等、回滚、参数化 SQL、索引、错误信息 |
| **合计** | **598 / 301** | — |

**为什么用例数 598 ≠ 测试函数数 301？** 差额来自 `pytest.mark.parametrize` 展开：全仓库 30 处 parametrize 装饰器，其中 `test_intent.py` 占 16 处（82 个函数展开成 288 个用例）；其余差额分散在其他文件（如 `test_time_resolver.py` 31 个函数展开为 70 个用例）。**另注**：`scripts/test_ai.py` 名字里有 `test_` 但**不属于测试体系**——`pytest.ini:2` 的 `testpaths = tests` 把它排除在外（实测采集输出中 `scripts/` 零命中），它是手动排查 Key / 网关连通性的小工具（`:7,12`）。

### 10.4 三层划分口径与隔离设计

**三层口径**：**单元层** = 直接 import 业务模块调用函数、不经过 Flask（用合成库把输入值钉死、对公式做精确断言，时间与 AI 都用替身）；**接口层** = Flask `app.test_client()` + 真实库副本（断言状态码、`error.code`、`field`、`available`、字段形状与口径）；**端到端层** = 从自然语言 `question` 一路走到 `rooms[]` / `explanation`（验证整条链路，且 AI 部分离线可跑）。

**隔离设计**（真实数据库永远不被就地打开）：`shutil.copyfile(REAL_DB, target)` 复制到临时目录并做模块级 fixture（`test_api.py:48-53`、`test_classroom.py:62-67`、`test_forecast.py:165-173`、`test_industry.py:228-236`）；真实库不存在时模块级 `pytestmark = pytest.mark.skipif(...)` **干净跳过**而非报错（`test_api.py:38-39`、`test_classroom.py:52-53`）；另有**完全受控的合成库**用于精确断言（食堂 84 天合成库 `test_forecast.py:142-146`；产教合成库 `test_industry.py:205-209`）。因此测试**不会修改仓库里的 `data/campus.db`**，也不触碰 `frontend/`（`README.md:311`、`docs/API.md:481-484`）。

**`conftest.py` 覆盖了 pytest 自带的临时目录夹具**（`tests/conftest.py:1-9,27-102`）：pytest 默认的 `tmp_path` / `tmp_path_factory` 在**受限文件系统**（受限令牌沙箱、只读挂载的临时目录）下会因目录权限扫描失败抛 `PermissionError`，导致测试根本跑不起来；本文件改用 `os.mkdir` 创建**普通目录**（不用 `tempfile.mkdtemp`，它创建的目录带严格安全描述符），并做一次真实可写性探测，候选根目录顺序为 `$CAMPUS_TEST_TMP` → 系统临时目录 → 仓库内 `.tmp_test_run`，每个测试拿独立目录、会话结束清理。**这是「测试体系本身也被工程化过」的证据**：它解释了为什么在权限受限的 Windows 环境里测试依然能全绿。`pytest.ini` 另有配套设置：`testpaths = tests`、`addopts = -ra -p no:cacheprovider --basetemp=.pytest_tmp`、`tmp_path_retention_policy = none`。

**测试资产 `golden_questions.json`**（13,483 字节）：`description` / `answer_field_conventions` / `intents` / `questions` 四个顶层键；**共 40 条问句**（`id` / `category` / `question` / `answer` 各 40 条，`note` 10 条，`must_be_unknown` 8 条），分 9 类（找教室 4、食堂 6、相对日期 4、模糊时间 4、人数表达 4、设备需求 4、产教 6、完全无关 6、能力边界 2）。既是测试数据，也是演示脚本（`README.md:362`）。

### 10.5 「598 个用例全部离线，不联网」是怎么保证的

这是 README 的一句强声明（`README.md:28,309`），本文核对后结论是**成立**：

| 核对项 | 结果 |
|---|---|
| `tests/` 中对 `requests` 的使用 | 只用于构造异常类型（`requests.exceptions.Timeout` 等），见 `test_classroom.py:29,683`、`test_forecast.py:37,789`、`test_industry.py:37,1141`、`test_intent.py:28,851,859,933,961,972,994` |
| `requests.Session` / `urlopen` / `http.client` / `socket.socket` / `requests.get` / `requests.post` / `openai.` | 在 `tests/*.py` 中匹配数为 **0** |
| AI 阶段如何被替换 | 全部注入替身：`FakeSession`（`test_intent.py:84-96`）、`test_time_resolver.py:331-333`、`test_classroom.py:195`（`:223` 直接替换类）、`test_forecast.py:286`、`test_industry.py:365` |
| 后端默认是否用真客户端 | 是（`ai_parser.py:419` 为 `session if session is not None else requests`；`classroom.py:516`、`forecast.py:1055`、`industry.py:950` 同构），但**没有任何测试依赖这个默认值** |
| 导入器依赖 | `test_import_data.py:607-611` 断言导入器 import 集合恰好为 `{argparse, contextlib, csv, os, re, sqlite3, sys, tempfile, time}`，且与 `{flask, openai, requests, urllib, httpx, aiohttp, socket, subprocess}` 不相交 |

此外 API 层专门设计了 `explain=0` 参数（默认 1），可跳过 AI 解释、只走 Python 兜底文案（`docs/API.md:107`）——既服务离线演示，也让测试只验证确定性部分。

### 10.6 已知不一致

`prompts/PROMPTS-README.md:31` 仍写「确认 **597** 个用例仍然全绿」，实际为 **598**。该发现由 AI 在 Day18 会话中主动报出、人工核实确认（`Day18-README与陌生环境复现.md:69,75,186`），截至本文档编写时**尚未修正**（见第 14 章不足 #1）。另需说明：**架构红线的防退化断言**（如「模块不得 import sqlite3」）在 13.3.1 节统一列出，本节不重复。

**本章证据**：本次实测的 `pytest -q` 与 `--collect-only` 输出、`tests/` 全部 7 个测试文件、`tests/conftest.py`、`tests/golden_questions.json`、`pytest.ini`、`README.md:297-316`、`docs/API.md:472-484`。

---

## 11. Git 迭代与 AI 协作过程：`docs/ai-collaboration/` 里 Day01~20 的记录怎么证明开发过程

**目标**：把「过程记录」讲成可核验的证据链，而不是叙事。

### 11.1 方法论：`docs/ai-collaboration/` 的定位与记录规范

**赛道背景**：2026 第九届传智杯全国大学生数智创新与 AI 应用大赛 · **AI 创新应用挑战赛**（赛道 id 10039）。该赛道官方简介的核心表述是「Vibe Coding 以『意图主导、AI 生成代码』的全新开发范式」与「锻炼 AI 协同开发能力」（`docs/ai-collaboration/README.md:5-11`）。

因此 `docs/ai-collaboration/` 的份量**不低于** `backend/` 里的代码：它证明的不是「系统能跑」，而是「这套系统是人在指挥下逐步迭代出来的」。

**统一记录骨架（8 段）**（`docs/ai-collaboration/README.md:26-47`）：① 本轮 Prompt（逐字原文）→ ② Agent 的回复（原文关键段）→ ③ 发现的具体问题（**要求落到文件与行号**）→ ④ 第二轮 Prompt（逐字原文）→ ⑤ 改动文件与行号 → ⑥ 关键 diff（代码块，**不要用截图代替**）→ ⑦ 真实验证结果（pytest 输出 / HTTP 响应，原样粘贴）→ ⑧ 剩余风险。

**三条硬规矩**（`docs/ai-collaboration/README.md:49-57`）：

1. **Prompt 必须逐字**。发送前先把原文落盘到记录文件，再发出去——这样「文件时间戳 + git 提交时间」就构成「**prompt 先于代码存在**」的时序证据；
2. **正文主体用文字与代码块，截图为辅**。反例已修复：`Day08-Intent-Parser.md` 曾整段只有一张 PNG，评委在网页端读不到任何内容；现已补文字版 Prompt 原文（「整段截图不算数」）；
3. **不虚构**。没有发生过的交互不要写；数字必须能在本机真实复现；Agent 没执行的步骤就写「未实测」。

第 3 条是本项目最值得强调的一条：它把「过程记录」从宣传材料变成了**可证伪的证据**。

### 11.2 覆盖清单与达标情况

`docs/ai-collaboration/` 共有 20 份开发记录（Day01 ～ Day20，**Day01–Day05 已于 2026-10-08 依据本地 AI 会话存档补写**），另有 `CONV-01-对话日志.md` 与 `images/`（截图辅助材料）。按 `docs/ai-collaboration/README.md:63-83` 的自评表（列：逐字 Prompt / 真实 AI 回复 / 具体 Bug / 第二轮 Prompt）：Day01 ✅发起轮原文 / —（回合未完成，如实标注）/ ✅ Key 泄露发现 / —；Day02 ✅ / ✅有关键段原文 / ✅ .env.example Key 泄露 / —；Day03 ✅ / ✅有原文引用 / ✅ DDL 提取/临时库验证 / —；Day04 ✅三轮 / ✅ / ✅技能覆盖缺口 / ✅三轮迭代；Day05 ✅ / ✅有原文引用 / ✅ WinError 32/沙箱 tmp_path / ✅含多轮自查；Day06 ✅ / ⚠️摘要 / ✅ 400 校验实测 / —（单轮）；Day07 ✅两轮 / ⚠️摘要 / ✅ / ✅；Day08 ✅文字版 / ⚠️摘要 / ✅ / —；Day09 ✅三轮 / ⚠️部分 / ✅ / ✅含人工干预；Day10 ✅ / ✅有原文引用 / ✅有代码级根因 / —；Day11 ✅ / ✅ / ✅3 个真 Bug / —；Day12 ✅ / ✅ / ✅ / —；Day13 ✅六轮 / ⚠️部分 / ✅ / ✅含拍板决策；Day14 ✅两轮 / ⚠️部分 / ✅ / ✅；Day15 ✅三会话 / ✅ / ✅ / ✅；Day16 ✅两会话 / ✅ / ✅ / ✅含拍板决策；Day17 —（人工验证日，无 Harness 协作）；Day18 ✅单轮 / ✅有报告原文引用 / ✅（597→598 过期数字发现）/ —；Day19 ✅三轮 / ✅有报告原文引用 / ✅（36 页超限/597 陈旧/转义不符）/ ✅三轮；Day20 —（人工整理日，无 Harness 协作，补写 Day01–05 并校准全仓文档）。

**结论**：20 份记录中 18 份含**逐字 Prompt 原文**；`Day17` 为纯人工验证日、`Day20` 为纯人工整理日，**均如实标注无 Prompt**——与「不虚构」规矩自洽。Day10 / 11 / 12 三个核心功能各有一轮完整 Prompt；Day04 / 09 / 13 / 14 / 15 / 16 / 18 / 19 含多轮迭代链，覆盖「停止修改」「拍板决策」「真机验收再修」等真实引导过程。**Day01–Day05 的处理方式**：仓库 Git 历史从 `398e797 chore: initialize project`（2026-09-20）起算；原无对应记录文件，2026-10-08 依据本地 AI 会话存档（`C:\Users\Zqy\.dsh\sessions\--D-Projects--`）还原补写，每份含逐字 Prompt 与真实问题/验证记录；Day01 发起回合未完成、Day02 纯审查日无代码提交，均如实标注，未虚构任何 AI 回复（`docs/ai-collaboration/README.md:89-96`）。

### 11.3 Git 与记录的对应关系

以下全部为**本文实测**（`git log` / `git rev-list --count` / `git ls-files --diff-filter=A`）。**总量**：提交总数 **52**（2026-10-24 实测，含 Day01–05 补写 `05e9f91`、Day20 记录 `15bcb04` 与 GitHub 改名后链接同步 `34d857b`；**本技术文档本次修订尚未提交**，由人工提交后总数为 53）；首次 `398e797`（2026-09-20）；末次 `34d857b`（2026-10-24，见 `git log --oneline -1`）；工作树干净。

**DayNN 记录与提交逐日对应**（按「新增该文件的提交」统计；Day01–05 记录文件由 2026-10-08 的 `05e9f91` 补写统一新增，括号内为其当日代码/文档提交）：Day01 `05e9f91`（`398e797`/`1d3f44b`/`e29096b`/`98efca5`，09-20，初始化与 Key 泄露修复）→ Day02 `05e9f91`（纯审查，无代码提交）→ Day03 `05e9f91`（`f4e13c7`，09-21）→ Day04 `05e9f91`（`8e6a81b`/`1f6d0b8`/`23c96ff`，09-22，三轮）→ Day05 `05e9f91`（`49877ea`，09-23）→ Day06 `e608004` 09-24 → Day07 `07da5a3` 09-25 → Day08 `c85bfa6` 09-26 → Day09 `529af05` 09-27 → Day10 `88a85cf` 09-28 → Day11 `2b7e178` 09-29 → Day12 `ae82711` 09-30 → Day13 `a445417` 10-01 → Day14 `77c3548` 10-02 → Day15 `85db4dc` 10-03 → Day16 `1440163` 10-04 → Day17 `da6131a` 10-05 → Day18 `4fc6dce` 10-06 → Day19 `a19aa49` 10-07 → Day20 `15bcb04` 10-08。

**20 份记录逐日连续，每天都有对应提交。** 但有两处**不对齐必须如实写出**：

1. **Day15 的「记录提交」与「内容提交」不是同一个**：记录文件由 `85db4dc docs: tone down overpraise and fix contradictions in collaboration logs`（10-03）新增，而提交主题与 Day15 标题对应的是前一天（10-02）的 `be35f6a fix: stabilize core demo workflows and fix P0 boundary issues`——**修复动作发生在 10-02，记录落盘在 10-03**。
2. **Day08 的记录是被改名，而非在该日新写**：`c85bfa6 docs: rename day 8 ai collaboration record to match naming convention`；`--diff-filter=A` 会把重命名计为「新增新路径」，因此不能据此宣称「Day08 记录于 09-26 撰写」。

**其它实测细节**：`7660937` 与 `529af05` 在 09-27 拥有**完全相同**的提交主题；Day16 在 10-04 有两次提交（`1440163` 功能与验证 + `46ab0bb` 日志用时更正）。

**阶段划分的诚实说明**：提交主题按前缀统计为 `docs 27 / feat 9 / fix 7 / data 2 / prompt 2 / other 2 / chore 1`（`other` 为两条无前缀的中文日志提交，如 `46ab0bb`）。**没有任何提交主题以 `test:` 开头**——测试用例随功能/修复提交一起落库，**不存在独立的「测试阶段」提交**，本文不臆造该阶段。另有一个高价值细节：仓库存在以 **prompt 为主体**的提交类型（`7660937`、`529af05`），正好对应 `AGENTS.md:184-189` 的「Prompt 修改必须通过 Git 保留历史」（见 7.5 节）。

### 11.4 记录里的真实 Bug 证据

这是「过程记录」最有说服力的部分——**每个 Bug 都能落到代码与行号**：

- **Day11 · 食堂模块修复 3 个真 Bug**（`Day11-食堂分析预测.md` 第 3 节「核心亮点：修复 3 个真实 Bug」）：三个核心功能里记录最完整的一轮，可直接作为「AI 发现并修复真实缺陷」的样本。
- **Day15 · 修复前端 `API_BASE` 重复拼接 `/api`**（`Day15-修复P0与前端隐患.md` 会话 1，2026-10-03，35 秒）：前端基址已含 `/api`，请求又拼一次导致后端 404；对应提交 `4b26b8c`。
- **Day15 · 修复「招牌问句 400」**（`Day15-…md` 会话 2，用时 1 分 49 秒，记录标注「★ 唯一录屏会话」）：双学期数据引入后的回归问题，服务端修复见 8.1.4 节（`dd56c8f`）。
- **Day16 · 修复「后端在线但 AI 助手永远回离线示例」**（`Day16-稳定性检查.md:20,36,51`，14 分 59 秒）：后端跑起来、页面显示「已连接」，但问找教室永远返回「离线示例」。根因经实测确认：**后端 `/api/classrooms/search` 需 3.3–5.7 秒**（要走大模型做意图识别 + 结果解释），而前端写死 **2600ms** 就 abort，必然落入降级分支；修复方案是 `AI_TIMEOUT_MS = 15000`（详见 9.6 节）。
- **Day16 · 修复「4 个快捷按钮不走实时接口」**（`Day16-…md:41-42,53`）：根因是 `useBackend = apiOnline && (!localHit || localHit.key === 'room_search')`——命中知识库主题即 `false`，后端在线也走内置快照（详见 9.3 节）。
- **Day18 · 发现文档与测试现状不符（597 → 598）**（`Day18-README与陌生环境复现.md:69,75,186`）：AI 在写 README 时主动报出「`PROMPTS-README.md` 写的是 597 个用例，实际已是 598」，人工随后确认。这是「不虚构」规矩被真实执行的样本——**AI 报告了自己没被问到的问题**。

### 11.5 人机分工的形态

| 角色 | 动作 |
|---|---|
| **人（主导）** | 真机复现现象 → 定位根因方向 → **拍板改法** → 限定改动边界 → 浏览器/接口验收 → 决定是否提交 |
| **AI（执行）** | 在明确边界内改代码 → 跑测试 → 报告改动文件、验证结果与剩余风险 → **不自行 `git commit`** |

**证据形态**：逐字 Prompt（落盘文件）+ git 提交时间 + 真实终端/HTTP 输出，三者构成「prompt 先于代码」的时序链。例如 Day16 的 Prompt 明确写了「只改 `frontend/index.html` 和它引用的东西，不要动 backend 和 prompts 里的内容，`classroom.py`、`forecast.py`、`industry.py` 这三个核心文件更不能动」，而记录最后确认「`git status` 全仓库只有 `frontend/index.html` 被改动」（`Day16-稳定性检查.md:8-9,28`）——**边界由人指定，由 AI 遵守，由 git 验证**。该记录还写明完整的四步闭环（`Day16-稳定性检查.md:5`），并对应到赛道 2.1 的要求。

### 11.6 记录规范本身的执行缺口

如实列出，不粉饰：

1. **部分 Agent 回复是摘要而非原文引用**：`docs/ai-collaboration/README.md:63-83` 的自评表里多处标为「⚠️ 摘要」（Day06 / Day07 / Day08 / Day09 / Day13 / Day14）。这是已知的、被自己记录下来的质量缺口。
2. **Day01–Day05 记录为后期补写**：原缺失，已于 2026-10-08 依据本地 AI 会话存档还原补写（`C:\Users\Zqy\.dsh\sessions\--D-Projects--`），提交 `05e9f91`；Day01 发起回合未完成、Day02 纯审查日无代码提交均如实标注，未虚构任何 AI 回复——补写行为本身如实记录，不与原记录混淆。
3. **一条被改写的记录叙述**：`85db4dc` 的提交主题是 `docs: tone down overpraise and fix contradictions in collaboration logs`（调低过度赞誉、修正记录里的自相矛盾）——说明记录本身也经过了一轮自我审查，这一点可以在文档中如实提及。

**本章证据**：`docs/ai-collaboration/README.md`、`docs/ai-collaboration/Day01-*` ～ `Day19-*`、`docs/ai-collaboration/CONV-01-对话日志.md`、`docs/ai-collaboration/images/`；本次实测 `git log` / `git rev-list --count HEAD` / `git show --stat` / `git status --short` / `git ls-files --diff-filter=A`。

---

## 12. 可复现性：陌生环境从零跑通（README 已验证）

**目标**：证明「别人拿到项目能跑起来」，并区分「文档声明」与「实测证据」。

### 12.1 从零到跑起来的完整命令序列

`README.md:446-460` 的「命令速查」，也是陌生环境复现的唯一入口：

```powershell
git clone https://github.com/CrestLune/campus-ai.git; cd campus-ai
py -3.12 -m venv .venv ;  .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env      # 填入 AI_API_KEY（不填也能跑离线演示）
python scripts\make_data.py      # 生成 9 个 CSV 到 data\raw
python scripts\import_data.py    # 导入 data\campus.db（9 张表，10651 行）
python backend\app.py            # 后端 http://127.0.0.1:5000
# 另开窗口：双击 frontend\index.html ；再跑 pytest -q（598 passed）
```

### 12.2 复现的四个可验证支点

| # | 支点（为什么可验证） |
|---|---|
| 1 | **数据可复现**：`random.seed(42)` + 生成后 8 组自动检查 + SHA-256 一致性比对（见 5.5 节） |
| 2 | **导入可重复**：临时库 + 单事务 + 原子替换；二次执行结果与首次一致（见 5.4 节） |
| 3 | **服务可启动**：`python backend\app.py` 或 `python -m backend.app`；自检接口 `/api/meta/semesters` 返回 `{"status":"ok","data":{...}}`（`README.md:239`） |
| 4 | **测试可跑**：`pytest -q` 全绿且**不联网**；`pytest.ini` + `tests/conftest.py` 已处理受限文件系统（见第 10 章） |

### 12.3 「陌生人测试」的真实证据链

`Day18-README与陌生环境复现.md:98-171` 记录了一次**由人亲手执行**的陌生环境复现（原文档陈述）：做法是复制一份全新目录 → 删 `.git` 与 `.venv` 模拟陌生环境 → **故意不用 AI 辅助**，严格按 README 命令逐条执行。结果：

| 步骤 | 结果 |
|---|---|
| 装依赖 | 清华源一次装完 **19 个包**（官方源卡住后切换） |
| `make_data.py` | 9 个 CSV 全部生成，**8 组检查全过（0 问题 0 提示）** |
| `import_data.py` | 9 张表**合计 10651 行**，外键完整性通过 |
| 启动后端 | `Running on http://127.0.0.1:5000` |
| 前端 5 个按钮 | 4 个走实时接口（200），1 个（找空教室）因未配 Key 走离线降级 + 「离线示例」标注 |
| 接口直测 | `/api/meta/semesters` 200 双学期；`/api/classrooms/available` 200 返回 15 间空教室；`/api/canteens/stats` 200；`/api/industry/gap?major=计算机科学与技术` 200（覆盖 11 项 / 缺口 14 项 / 覆盖率 44.0%） |

**诚实标注的两个意外**（`Day18-…md:65,102-111,185`）：① **`git clone` 连续两次 `Connection was reset`**——国内访问 GitHub 不稳定，改用「本地复制模拟陌生环境」，是网络问题不是项目问题；② **Harness 沙箱内 `pip install` 未跑通**——`py -3.12 -m venv` 的 `ensurepip` 引导因临时目录被拒绝删除而失败（沙箱限制），替代证明是「现有 `.venv` 中 19 个包全部已安装且版本一致」，并由上述陌生人测试在真机补齐。这两条正是「不虚构」规矩的直接体现：**没跑通就明确写没跑通**，并给出替代证据。

### 12.4 环境前提与无 Key 演示

环境前提（`README.md:85-91`，原文档陈述）：Python 3.12（本机核对 `Python 3.12.7`）、Git（`git 2.55.0.windows.5`）、操作系统以 Windows + PowerShell 为主。**大模型 API Key 不是启动必需项**：不填 Key 也能跑数据脚本、跑测试、打开前端看离线演示（`:91`）——这是本项目在「可评审性」上最重要的一个决定。不配 Key 时的行为：

| 场景 | 不配 Key 时的行为 |
|---|---|
| 数据脚本 / 测试 | 完全正常（都不依赖 AI）；测试里 2 个 Key 相关用例会 skip → **596 passed + 2 skipped**（见 10.2） |
| `/api/classrooms/search` | 503 + `error.code = "ai_unavailable"`（消息带原因如 `missing_config`） |
| `/api/canteens/trend` / `peak` / `forecast` | 200 + `explanation.source = "local"`，`explanation.error.code` 给出原因 |
| `/api/industry/match` | 200 + `source = "local"`，**计算结果与覆盖率完全不受影响** |
| 前端页面 | 接口失败时回落内置离线快照，问答带「离线示例」徽标，图表不空白 |

`README.md:427` 另记录一次实测：清空 `AI_API_KEY` 后 `/api/canteens/trend` 返回 `200` + `source=local` + `error.code=missing_config`；`/api/classrooms/search` 返回 `503`；`/api/industry/match?major=软件工程` 返回 `200` 且 `coverage_rate=0.5882`（与配好 Key 时一致）——**这证明 AI 的缺失不影响计算结果的正确性**，是 6.6 节的实证。

**`.env` 的 5 个变量**（`.env.example:1-9`，实测恰好 5 个）：`AI_API_KEY`（模板为空，**代码无默认值**，缺失时 AI 解析以 `missing_config` 失败）、`AI_BASE_URL`（模板与默认值均 `https://api.deepseek.com`）、`AI_MODEL`（`deepseek-flash`）、`AI_MAX_TOKENS`（`2048`）、`AI_TIMEOUT`（`30` / 默认 `30.0`）。`.env` 已被 `.gitignore:4` 忽略（详见 13.1）。

### 12.5 复现风险

**依赖源不可达**：`pip install` 在官方源卡住 → 换国内镜像源（陌生人测试用清华源一次装完 19 个包）；**README 目前未写镜像源提示，属待补**。**GitHub 不可达**：`git clone` 两次 `Connection was reset` → 建议保留 Gitee 镜像或用代理（`Day18:185` 已列该风险）。**`AI_MAX_TOKENS` 过小**：带推理的网关先花 reasoning token，JSON 被截断（`finish_reason=length`），表现为**解释静默降级成兜底文案** → 调到 3000（`README.md:158,429`）。**受限文件系统**：pytest 默认临时目录实现报 `PermissionError` → `tests/conftest.py` 已覆盖夹具（见 10.4）。

### 12.6 复现核验清单（可勾选）

| # | 核验项 → 期望结果（依据） | # | 核验项 → 期望结果（依据） |
|---|---|---|---|
| 1 | `py -3.12 --version` → `Python 3.12.x`（`README.md:87`） | 6 | `python scripts\import_data.py` → 9 张表合计 **10651** 行、外键检查通过（`:211-224`） |
| 2 | `py -3.12 -m venv .venv` → 生成 `.venv` 目录（`:105-107`） | 7 | `python backend\app.py` → `Running on http://127.0.0.1:5000`（`:236`） |
| 3 | `pip install -r requirements.txt` → 装完 19 个包（`:129`） | 8 | `GET /api/meta/semesters` → `{"status":"ok","data":{...}}`（`:239`） |
| 4 | `.env` 变量数 → 恰好 5 个（`.env.example:1-9`） | 9 | 双击 `frontend\index.html` → 页面可打开、5 个按钮可用（无 Key 时 1 个显示离线示例）（`:245-249`） |
| 5 | `python scripts\make_data.py` → 9 个 CSV + 8 组检查全过、退出码 0（`:172-181`） | 10 | `pytest -q` → **598 passed**（无 `.env` 时 **596 passed + 2 skipped**）（本次实测 + 10.2） |

**本章证据**：`README.md:83-316,446-460`、`.env.example`、`.gitignore`、`docs/ai-collaboration/Day18-README与陌生环境复现.md`、`scripts/make_data.py`、`scripts/import_data.py`。

---

## 13. 安全与边界：API Key 保护、意图控制、错误码约定

**目标**：说明「不泄露、不放任、不静默失败」三条线怎么落到代码。

### 13.1 API Key 保护

**硬规则**：API Key **不得写入源代码**（`AGENTS.md:141-146`）。

**实现**（`ai_parser.py::load_config()`，`:105-144`）：

```python
load_dotenv(dotenv_path or <ROOT>/.env, override=False)     # :116-118
api_key = (env.get("AI_API_KEY") or "").strip()             # :121
if not api_key:
    raise AIParserError("缺少 AI_API_KEY：请在项目根目录的 .env 里配置…",
                        code="missing_config")              # :125-130
```

三个要点：① **Key 没有默认值**，缺失即抛 `missing_config`（`README.md:155`「无（必填才用 AI）」的实现依据）；② **`override=False`**，不覆盖进程已有的环境变量，避免意外覆盖部署配置；③ **只通过 `Authorization` 头发送**（`Bearer <key>`，`:475-478`），不放进请求体——有测试断言（`tests/test_intent.py:983-985`）。

**仓库层面**：`.env` 被忽略且未被跟踪（`.gitignore:4`；实测 `git check-ignore -v .env` 命中、`git ls-files --error-unmatch .env` 失败）；只提交 `.env.example` 且其 `AI_API_KEY` 行为空（`.env.example:1`）；实测 `git grep -n -I -E 'sk-[A-Za-z0-9]{12,}'` 在 HEAD 上 **0 命中**。

**必须如实记录的历史事件**：项目历史里确实**曾把一个真实 Key 提交进 `.env.example`**，随后在同一天被修掉——`398e797 chore: initialize project`（2026-09-20）进入，`98efca5 fix: remove leaked api key from env example`（2026-09-20）移除。**残留风险（必须写，不能略）**：移除只改了工作树，**旧 blob 仍留在 Git 历史里**——实测 `git grep` 在 `98efca5^` 的 `.env.example` 上仍能命中 `sk-` 形态字符串。因此对该已泄漏的 Key，正确处置是**在服务端吊销 / 轮换**，而不是仅仅提交一次删除；本文档不打印该值。**额外的自动化防线**：`tests/test_intent.py:988-1003` 有 2 个以「真实 Key 存在」为条件的用例，断言真实 Key 不出现在任何结果/错误里、也不被硬编码进源码。其余配置项默认值见 `ai_parser.py:54-61`。

### 13.2 意图控制：防「AI 越权」的三道闸

| 闸 | 位置 → 作用 |
|---|---|
| ① Prompt 层约束 | `prompts/intent_parser.md:45-60` → 只能用白名单 intent、无法判断返回 `unknown`、不生成 SQL、不访问数据库、不编造事实 |
| ② 代码层白名单 | `validate.py:29-37,471-541` → 意图必须在 `INTENTS` 内；每个意图有参数字段白名单；白名单外字段**丢弃并留痕**（`notes`），因此 AI 无法指定 `room_id` |
| ③ 职责不符即拒绝 | `classroom.py:83-91,619-626` → 意图不是 `room_search` 时返回 400 + `unsupported_intent`，消息说明这属于哪类问题 |

**前端配套**：无关问题明确拒答，不再答非所问。Day16 修复前的行为是「问『帮我推荐股票』会返回一段校园介绍」，修复后改为明确回答「这个问题我不会，我只能回答教室、食堂、产教技能相关的问题」（`Day16-稳定性检查.md:26,45`）。

### 13.3 注入与越权防护

① **数据访问层全参数化**：所有 SQL 的值走 `?` 绑定；`WhereBuilder`（`db.py:65-97`）只拼接**由业务代码写死的列名与操作符**加占位符，值一律进 `params`，从结构上不存在「把用户输入拼进 SQL」的路径（见 6.3）。

② **参数入口 SQL 片段闸门**：`validate.reject_sql()`（`validate.py:235-245`）用 `_SQL_RE`（`:218-223`）拦截注释符 `--` / `/*` / `*/`、分号 `;`、SQL 关键字（`select|insert|update|delete|drop|alter|create|attach|detach|pragma|union|truncate|grant|exec`）以及 `or/and x = y` 形态，命中即 `ValidationError("参数 X 含疑似 SQL 片段，已拒绝")`；作用于 `intent`（`:399`）、文本字段（`:306`）、`semester`（`:359`）与被丢弃的未知字段值（`:511`）。

③ **模型输出复查**：解释文本若含 SQL 片段 → `unsafe_explanation` → 整条退回 Python 兜底（`classroom.py:74-79`；三处复查在 `classroom.py:464-475`、`forecast.py:1002-1013`、`industry.py:892-903`）。

④ **AI 拿不到任何数据访问能力**：只读事实表，没有连接、没有凭据、没有表名、没有数据库路径（`classroom.py:15-18`、`forecast.py:785-881`、`industry.py:740-779`）。

⑤ **结果完整性**：`/api/industry/match` 用 `results_fingerprint`（SHA-256）保证解释阶段不改动计算结果（见 6.5；注意该机制**仅此一条链路有**）。

#### 13.3.1 防退化测试：把架构红线变成可执行断言

**红线不靠 Code Review 记忆维持，违反即测试失败**：解析器不得 import `sqlite3` / `backend.db`、源码不得出现 SQL（`test_intent.py:1096-1126`）；`validate.py` 不得 import `sqlite3` / `backend.db` / `requests`（`:1129-1141`）；`classroom.py` 不 import `sqlite3` 且唯一查询入口是 `list_available_classrooms`（`test_classroom.py:745-765`）；`industry.py` 同类断言（`test_industry.py:1202`）；`forecast.py` 同类断言含「无 ML」（`test_forecast.py:749,863`）；导入器只依赖标准库、与网络库集合不相交（`test_import_data.py:607-611`）；产教链路对抗性测试让 explainer 试图篡改结果、断言指纹校验抛错（`test_industry.py:1053,1093`）；真实 Key 不出现于任何结果/错误、不被硬编码（`test_intent.py:988-1003`）。

### 13.4 参数校验边界

**意图参数白名单与范围**（`validate.py:90-104`，以 `room_search` 为例）：`weekday` 1..7、`period_start`/`period_end` 1..12、`capacity` 1..1000、`has_projector`/`has_computer` 布尔、`room_type` 枚举、`building` ≤50、`limit` 1..100。

**路由层另有接口级上限**（与意图层不完全相同——**两层各管一段，不要混为一谈**）：`capacity` 1..100000（`routes.py:61`）、`limit` 1..100 默认 10（`:72`）、`question` ≤200 字符（常量在 `classroom.py:60`、`forecast.py:95`、`industry.py:112`；执行处 `routes.py:99-105`、`classroom.py:131`、`forecast.py:181`、`industry.py:189`）、`horizon_days` 1..30 默认 3（`routes.py:174-180`）、`peak_ratio` 0.1..1.0 默认 0.8（`:181-187`）、`min_importance` 1..5 默认 1（`:121`）、`top` 1..100 默认 20/10（`:122,323`）。

**跨字段规则**（`validate.py:448-468`）：`period_end >= period_start`、`date_to >= date_from`、`canteen_forecast` 不允许同时给 `target_date` 与 `date_to`。

**学期形状**：`_SEMESTER_RE = ^\d{4}-\d{4}-[12]$`，先剥掉「学期 / 学年 / semester / term」后缀再匹配（`validate.py:213-215,327-359`）。**注意这里只校验形状，不校验库里是否存在**——存在性由业务层负责（设计理由见 `validate.py:330-331`），这样「形状不对」在解析层就 400，而不会拿一个格式合法的假学期去查库报 404。

**其它对外约定**：空串参数（`?a=`）按「未传」处理（`README.md:264`）；**无数据返回 200 + 空数组**（「查得到，只是没有」），只有参数非法才 4xx（`docs/API.md:44-48`），避免把「没有结果」和「请求错了」混为一谈；专业名支持等价参数名 `major` / `major_name` / `profession` / `department` / `skill`（`routes.py:126-136`）。

### 13.5 错误码约定（对外契约）

`400 invalid_parameter`（参数缺失或非法，附 `field`；缺失必填时附 `available`）、`400 unsupported_intent`（问的类型不在本接口职责内，附 `field=intent`）、`404 not_found`（查询目标不存在，附 `available`）、`405 method_not_allowed`（消息列出可用方法）、`503 database_unavailable`（库缺失/打不开）、`503 ai_unavailable`（意图解析不可用，消息带原因）、`500 database_error`（SQLite 查询失败）、`500 internal_error`（未预期异常）。完整映射与处理位置见 4.3 节表格（`app.py:98-155`）。

这套约定让前端能区分「用户写错了」「查不到」「服务坏了」三种情况，而不是笼统弹一句「请求失败」。

### 13.6 已知安全边界与残留

如实列出，避免被误认为「遗漏」：① **CORS 按请求回显 `Origin`**，任何域名都能拿到响应——这是**为 `file://` 演示做的取舍**（`app.py:45-47` 注释说明「不写死 `*`」比 `*` 好，但仍不等于生产级白名单）；② **无认证、无鉴权、无速率限制**，属项目非目标（`docs/PRD.md:137-162`），不具备生产部署条件；③ **已泄漏的 Key 仍在 Git 历史里**（见 13.1），需要轮换 Key 而非仅删除文件；④ **`prompts/` 的历史遗留描述与实测不符**（见 7.4），需把「转义残留」修正为「空行问题」。 

**本章证据**：`AGENTS.md:141-146`、`backend/ai_parser.py:54-144`、`backend/db.py`、`backend/validate.py`、`backend/routes.py`、`backend/classroom.py`、`backend/errors.py`、`backend/app.py`、`.gitignore`、`.env.example`、`docs/API.md`、`README.md:291,416-429`、`docs/ai-collaboration/Day16-稳定性检查.md`、`tests/test_intent.py`、`tests/test_classroom.py`、`tests/test_forecast.py`、`tests/test_industry.py`、`tests/test_import_data.py`。

---

## 14. 不足与后续方向

**目标**：只写有依据的不足，每条都给出「现象 → 证据 → 可执行的下一步」。

### 14.1 不足（每条都有证据，不泛谈）

| # | 不足 | 现象与证据 | 影响 |
|---|---|---|---|
| 1 | `prompts/PROMPTS-README.md` 用例数陈旧 | 文档写「597 个用例」，实际 598（`prompts/PROMPTS-README.md:31`；`docs/ai-collaboration/Day18-…md:69,186`） | 一处数字漂移；评审若按该数字核对会疑惑 |
| 2 | `prompts/PROMPTS-README.md` 的历史遗留描述与实测不符 | 文档声称有**转义残留**（`#`×9 / `.`×15 / `-`×26），实测 `prompts/*.md` 反斜杠数为 **0**；真正存在的是 `intent_parser.md` 的**多余空行**（323 行中 211 行为空行） | 文档可信度；`intent_parser.md` 直接阅读体验差 |
| 3 | 部分协作记录的 Agent 回复是摘要而非逐字原文 | `docs/ai-collaboration/README.md:63-83` 自评表多处标注「⚠️ 摘要」 | 过程证据强度打折 |
| 4 | 无认证 / 无鉴权 / 无速率限制；CORS 回显任意 Origin | `docs/PRD.md:137-162`（属非目标）、`backend/app.py:45-47` | 仅适合演示，不能直接上生产 |
| 5 | 数据全部为模拟数据 | `meal_records.source` 默认 `simulated`（`docs/DATABASE.md:293-294`） | 结论的可迁移性未经真实数据验证；预测准确度不可外推 |
| 6 | 单文件前端体量偏大 | `frontend/index.html` 实测 **2772 行 / 139,101 字节**，HTML+CSS+JS 全在一处（Day16 记录当时记为「2760 行」） | 可读性与多人协作成本 |
| 7 | 预测方法单一 | 只有「均值 + 星期修正 + 时段修正」，无节假日 / 天气 / 考试周因素（`backend/forecast.py:14-25`、`docs/API.md:288`） | 精度上限明确但较低（**这是有意的设计取舍**） |
| 8 | `courses.department` / `jobs.major` 是文本而非外键 | 专业匹配靠字符串相等（`docs/DATABASE.md:367`） | 专业名写法不一致会导致匹配遗漏 |
| 9 | 依赖外网可达性 | pip 源、GitHub、AI 网关三项都可能不可达（`docs/ai-collaboration/Day18-…md:104,118,185`） | 复现与现场演示的稳定性风险 |
| 10 | `AI_MAX_TOKENS` 偏小会导致解释静默降级 | 带推理的网关先花 reasoning token，JSON 被截断（`finish_reason=length`），表现为解释变成兜底文案（`README.md:158,429`） | 故障表现为「AI 不生效」而非报错，排查成本高 |
| 11 | 三处 AI 调用点各写了一遍 HTTP 细节 | `backend/classroom.py:554`、`forecast.py:1093`、`industry.py:988` | 一致性靠人工维持；改动需同步三处 |
| 12 | `backend/ai_parser.py` 中 `INTENTS` 被 import 但未在模块内使用 | 实测 grep：仅出现在 `ai_parser.py:42,46` 两处 import（真正的白名单在 `backend/validate.py:29-37`） | 无功能影响，但属未清理的冗余导入 |
| 13 | 接口示例数字未在本文档实跑复核 | `coverage_rate = 0.5882` 等来自 `docs/API.md` 示例（**原文档陈述**） | 若被当作「本文档实测」引用会产生证据强度错配 |

### 14.2 后续方向

按「低成本 → 高成本」排序，并标注是否与现有约束冲突。

**第一梯队：修正文档一致性（低成本，无约束风险）**

1. **修正 `prompts/PROMPTS-README.md` 的两处过期描述**：把「597 个用例」改为可自动核验的来源或去掉硬编码数字（`:31`）；把「转义残留」改写为实测状态（空行问题，且只在 `intent_parser.md`）——同时保留「新增防退化断言」的建议，它的价值从「修问题」变成「**防未来退化**」，仍然值得加。
2. **清理 `intent_parser.md` 的多余空行**，保留 `unescape_prompt()` 作向后兼容（`prompts/PROMPTS-README.md:63-71` 已提出该方案）。
3. **补齐协作记录质量**：把自评表中标为「摘要」的 Agent 回复补成原文引用（Day06 / 07 / 08 / 09 / 13 / 14）。Day01–05 已按本地 AI 会话存档补写完成（2026-10-08，提交 `05e9f91`），不再列入待办。
4. **清理 `ai_parser.py` 的冗余导入**（不足 #12）；**README 增加国内镜像源提示与 clone 失败备选方案**（`Day18:185` 已列该风险）。

**第二梯队：提升数据与匹配的可靠性（中等成本，需注意既有约束）**

5. **专业名规范化**：为 `major` 增加别名 / 归一化层，或在导入阶段做一致性检查。注意 `docs/DATABASE.md:367` 已指出新增专业表会破坏「固定 9 张表」约束，属**下一阶段决策**，不能顺手改。
6. **接入真实数据的灰度方案**：`meal_records.source` 已预留 `real` 取值（`docs/DATABASE.md:293-294`），可在**不动表结构**的前提下先导入一个食堂的真实数据做对照。

**第三梯队：结构性调整（较高成本，需先明确约束边界）**

7. **前端拆分**：把单文件大屏拆成模块。这会触碰 `AGENTS.md:61-63`「不使用 Vue / React」，可行路径是**保持原生 JS，只做文件级拆分**。
8. **统一三处 AI 调用实现**（不足 #12）：抽出共用函数；但 `AGENTS.md:75` 明确「不进行大规模重构」，应作为独立小任务谨慎推进。
9. **预测方法扩展位**：若未来引入更多因素（节假日、天气），**必须保持「Python 算、AI 只解释」的红线**（`AGENTS.md:83-131`）——扩展的是 Python 侧的修正项，不是把计算交给模型。

**第四梯队：生产化（明确超出当前范围）**

10. **鉴权、限流、CORS 白名单化、部署与监控**：`docs/PRD.md:137-162` 已把「生产级部署」「复杂权限体系」列为非目标；若要真正上线，这些是前置项。

### 14.3 一句话总结

这个项目最扎实的部分不是「用了大模型」，而是**把大模型关进了两道栅栏**：输入端用参数白名单，输出端用形状收敛 + （产教链路的）指纹校验；同时用 598 个离线用例把「AI 不许做什么」固化成断言。最不扎实的部分是**文档与数据**：两三处描述已过期（597 / 转义残留），数据本身全部是模拟的，且接口示例数字未在本文档中实跑复核——这两点已在上面逐条列出，不做隐藏。

**本章证据**：本文各章引用；`docs/PRD.md`、`docs/DATABASE.md`、`prompts/PROMPTS-README.md`、`docs/ai-collaboration/README.md`、`docs/ai-collaboration/Day16-稳定性检查.md`、`docs/ai-collaboration/Day18-README与陌生环境复现.md`、`README.md`、`backend/ai_parser.py`、`backend/forecast.py`、`backend/industry.py`、`frontend/index.html`。

---

## 附录 A · 术语与缩略语（合并精简）

- **Intent（意图）**：用户问题被归类到的业务类型，白名单共 7 个（`room_search` / `canteen_analysis` / `canteen_forecast` / `industry_skill_match` / `industry_skill_gap` / `dashboard_query` / `unknown`）。
- **意图白名单**：`validate.py:29-37` 的 `INTENTS` 常量，以及每个意图对应的参数字段白名单（`:90-151`）。
- **客流单元**：食堂分析的聚合单位——把「同一日期 + 同一小时」的多条记录（多食堂）合计成一条（`forecast.py:27-31`）。
- **覆盖技能 / 缺口技能**：技能需求 ∩ 课程技能 / 技能需求 − 课程技能（`industry.py:389-395`）。
- **覆盖率 / 加权覆盖率**：覆盖数 ÷ 技能需求数 / Σ 覆盖技能重要度 ÷ Σ 技能需求重要度（`industry.py:398-417`）；**反向指标 `course_covered_ratio`** 是课程技能里被岗位要求的比例（`:616-626`）。
- **指纹（fingerprint）**：`results_fingerprint`——对结果关键字段做排序 JSON 的 SHA-256，用于校验 AI 解释未改动计算结果（**仅产教链路有**，`industry.py:121-132,708-717`）。
- **兜底解释（local）**：AI 不可用或输出不合法时由 Python 生成的解释文案，响应里 `explanation.source = "local"`。
- **离线快照**：前端内置的兜底数据，后端不可达时使用并显示「离线示例」徽标（`index.html:2054,2346`）。
- **DoD**：Definition of Done——可运行、有测试、不泄露密钥、不破坏旧功能、有 Git 提交、有 AI 协作记录（`AGENTS.md:229-248`）。

## 附录 B · 文件清单与职责

**根目录**：`AGENTS.md`（项目约束：技术栈、AI 红线、数据规则、DoD）、`README.md`（陌生人 10 分钟复现指南，19 节）、`requirements.txt`（19 个包）、`pytest.ini`（`testpaths = tests`、`--basetemp=.pytest_tmp`）、`.env.example`（5 个变量）、`.gitignore`（忽略 `.env` / `.venv` / 缓存）。

**`backend/`（12 个模块）**：`app.py`（应用工厂、统一错误处理、CORS、命令行入口）、`routes.py`（解析参数 → 调业务 → 返回 JSON，无 SQL）、`queries.py`（业务查询与计算，不 import flask）、`db.py`（连接管理 + `WhereBuilder`）、`validators.py`（路由层参数校验）、`validate.py`（意图白名单、字段校验、`reject_sql`、参数契约渲染）、`errors.py`（400/404/503 三个业务异常）、`ai_parser.py`（读 Prompt、调模型、解析 JSON、错误码）、`time_resolver.py`（相对日期解析，纯函数无 I/O）、`classroom.py`（找教室编排）、`forecast.py`（食堂统计与预测）、`industry.py`（产教融合匹配）、`__init__.py`。

**`scripts/`（3 个）**：`make_data.py`（生成 9 个模拟 CSV，seed 42 + 8 组检查）、`import_data.py`（CSV → SQLite，读文档 DDL、原子替换、可重复执行）、`test_ai.py`（手动确认 Key / 网关，**不属于 pytest 测试体系**）。

**`prompts/`（4 个运行时 Prompt + 1 份说明）**：`intent_parser.md`、`insight_generator.md`、`canteen_insight_generator.md`、`industry_insight_generator.md`、`PROMPTS-README.md`。

**`docs/`**：`PRD.md`（需求与范围）、`API.md`（10 个接口字段级说明）、`DATABASE.md`（9 张表 + **DDL 唯一来源**）、`ai-collaboration/`（Day01～Day20 + CONV-01 + images/）、`TECHNICAL_DOCUMENT.md`（本文件）。

**`tests/`（7 个测试文件 + 1 份固化解集）**：`test_api.py`、`test_classroom.py`、`test_forecast.py`、`test_industry.py`、`test_intent.py`、`test_time_resolver.py`、`test_import_data.py`、`conftest.py`、`golden_questions.json`（40 条问句）。

## 附录 C · 待补实测清单（数据文件的数字来自 README，不重跑）

**处理原则**：`make_data.py` 与 `import_data.py` 会**覆盖** `data/raw/*.csv` 与 `data/campus.db`。按本项目要求（不修改数据文件），本文档**不重跑这两个脚本**，相关数字直接引用 `README.md` 中**已经实测过**的数值并标注来源。

**C.1 直接引用 README 的已实测数值**

| 数据 | 数值（出处） |
|---|---|
| CSV 行数 | classrooms 50 / courses 100 / schedules 1186 / canteens 5 / meal_records 8480 / jobs 100 / skills 30 / course_skills 300 / job_skills 400（`README.md:185-195`，实测输出） |
| CSV 字节数 | 2570 / 5848 / 63270 / 673 / 421120 / 32581 / 2522 / 4861 / 5352（同上） |
| 数据库表行数 | 同上 9 张表，**合计 10651**（`README.md:213-224`，实测输出） |
| 可复现性 | 连续生成两次，9 个文件的 **SHA-256 与仓库已有 CSV 完全一致**（`README.md:197`） |
| 生成 / 导入检查 | **8 组检查全部通过、0 个问题**（`:183`）；全部表导入成功、**外键完整性通过**（`:211`） |
| 陌生人环境复现 | 9 张表 10651 行、外键通过；双学期各 593 条课表（`Day18-…md:131,138`） |
| 无 Key 降级实测 | `trend` → 200 + `source=local` + `missing_config`；`search` → 503；`match` → 200 且 `coverage_rate=0.5882`（`README.md:427`） |

**C.2 需要在隔离副本中实跑复核的项（非覆盖式）**

以下项在本文档中标注为「原文档陈述」；若要升级为「本文实测」，应在**项目副本**（而非原仓库）中执行，以免覆盖数据文件：

| # | 复核项 → 命令/方式 → 期望 |
|---|---|
| 1 | CSV 行数与 SHA-256 一致性 → 副本中 `python scripts\make_data.py` 后对比 `data/raw/*.csv` → 行数同 C.1、SHA-256 与仓库文件一致 |
| 2 | 导入行数与外键检查 → 副本中 `python scripts\import_data.py` → 9 张表合计 10651 行、外键通过 |
| 3 | 两个实现的一致性 → 副本中分别请求 `/api/industry/gap?major=软件工程` 与 `/api/industry/match?major=软件工程` → 两份 `summary` 逐项一致 |
| 4 | 预测可复算 → `/api/canteens/forecast` 取任一小时的 `base` / `weekday_adjustment` / `hour_adjustment` 手算 → 与 `predicted_people` 一致（注意 `floor(x+0.5)`） |
| 5 | 双学期兜底 → `/api/classrooms/available?capacity=8&has_projector=1&weekday=3&period_start=5&period_end=8`（**不带** `semester`） → 200 且 `query.semester` 为库中最新学期 |
| 6 | CORS 预检 → 带 `Origin: null` 的 `OPTIONS` 请求 → `Access-Control-Allow-Origin: null`、`Allow-Methods: GET, POST, OPTIONS` |
| 7 | 测试计数 → `pytest -q` → 598 passed（无 `.env` 时 596 passed + 2 skipped） |

> **说明**：第 7 项已在本文档中实测三次（`598 passed`，8.55s / 7.64s / 7.65s），其余 1–6 项为待补。上表不填任何未执行的数字，避免「看起来跑过」。

## 附录 D · 可复现性核验清单

见 12.6 节的 10 项勾选表（Python 版本 → venv → 19 个依赖 → `.env` 5 变量 → 9 个 CSV → 9 张表 10651 行 → 后端 200 → 前端 5 按钮 → `pytest -q`）。

## 附录 E · 变更记录与证据强度说明

**本文件版本与证据强度**：v1.2，2026-10-07 编写、2026-10-08 修订（同步 Day01–05 补写、Day20 记录与提交数 50）、2026-10-24 修订（GitHub 改名后链接同步，提交总数 52），对应末次提交 `34d857b`（2026-10-24，提交总数 52）。**已实测**：`pytest -q`（三次，均 598 passed）、`pytest --collect-only`（全量与逐文件）、`git log` / `rev-list --count` / `show --stat` / `status` / `ls-files`、`frontend/index.html` 行数与字节数、`prompts/*.md` 空行与反斜杠统计、`tests/` 网络调用扫描。**原文档陈述**：CSV 行数与字节数、数据库 10651 行、SHA-256 一致性、陌生人测试各项结果、`docs/API.md` 的接口示例数字、环境版本号。**留空/未核实**：附录 C.2 的 1–6 项；`frontend/` 的运行时渲染效果（ECharts 实际绘制）未执行验证。**本次修订执行**：`git commit` 已由助手按用户授权执行（2026-10-24 链接同步 `34d857b`；本文档同步修订将新增提交）；`make_data.py` / `import_data.py`（会覆盖数据文件）未执行。

---

### 附：本文档引用与核对的真实文件

**必读并逐字阅读**：`AGENTS.md`、`README.md`、`docs/PRD.md`、`docs/API.md`、`docs/DATABASE.md`、`prompts/PROMPTS-README.md`、`docs/ai-collaboration/README.md`。

**为核实事实逐条核对**：`backend/` 下 `app.py`、`routes.py`、`db.py`、`errors.py`、`classroom.py`、`forecast.py`、`industry.py`、`queries.py`、`validate.py`、`validators.py`、`ai_parser.py`、`time_resolver.py`；`tests/` 下 `conftest.py`、`test_api.py`、`test_classroom.py`、`test_forecast.py`、`test_industry.py`、`test_intent.py`、`test_time_resolver.py`、`test_import_data.py`、`golden_questions.json`；`pytest.ini`、`scripts/` 下 `make_data.py`、`import_data.py`、`test_ai.py`；`prompts/` 下 4 个 Prompt；`frontend/index.html`、`.env.example`、`.gitignore`、`requirements.txt`；`docs/ai-collaboration/` 下 `Day16-稳定性检查.md`、`Day18-README与陌生环境复现.md`。

**执行的命令**：`git log --oneline`、`git log --pretty=format:"%h %ad %s" --date=short`、`git rev-list --count HEAD`、`git show --stat dd56c8f`、`git status --short`（含 `--ignored`）、`git ls-files --diff-filter=A`、`.\.venv\Scripts\python.exe -m pytest -q`（三次）、`pytest --collect-only`（全量与逐文件）、逐文件 `def test_` 计数、`Select-String` 检索协作记录标题与前端关键标记、前端与提示词行数/字符统计。

**副作用说明（诚实列出）**：`pytest -q` 运行时需写临时目录，会产生被 `.gitignore` 忽略的 `pytest-cache-files-*` / `.tmp_test_run` / `.pytest_tmp` 目录；**仓库中受版本控制的文件除本文件外一律未改动**，`git status --short` 仅显示 `?? docs/TECHNICAL_DOCUMENT.md`。未执行 `git commit`；未运行 `make_data.py` 与 `import_data.py`。
