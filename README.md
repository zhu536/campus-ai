# CampusAI 校园数据智能分析平台

用自然语言提问校园数据：**AI 只负责听懂问题、把它变成结构化参数，再解释已经算好的结果**——查数据、算统计、做预测全部由 Python + SQLite 完成，AI 不写 SQL、不碰数据库。

三个核心功能：

| 功能 | 一句话说明 |
|---|---|
| **AI 找教室** | 说一句「周三下午帮我找一个 8 人、有投影的空教室」，返回候选空教室 + AI 解释（空闲状态是查询时用排课表实时算出来的） |
| **食堂分析与预测** | 历史人流、等待时间、拥挤度、高峰时段，以及未来几天午餐人流的简单预测（均值 + 星期修正 + 时段修正） |
| **产教融合技能匹配** | 企业岗位技能需求 ↔ 专业课程技能覆盖：已覆盖技能、缺口技能、技能覆盖率与重要程度分布 |

> 仓库：<https://github.com/CrestLune/campus-ai.git>

---

## 1. 技术栈

| 层 | 选型 |
|---|---|
| 语言 | Python 3.12 |
| 后端 | Flask 3.1.3（应用工厂 + 蓝图，统一 JSON 错误处理） |
| 数据库 | SQLite（`data/campus.db`，固定 9 张表，只读查询） |
| 数据文件 | CSV（`data/raw/`，9 个文件） |
| 前端 | 原生 HTML / CSS / JavaScript（单文件 `frontend/index.html`，不用任何前端框架） |
| 可视化 | ECharts 5.5.1（CDN 引入，含备用 CDN） |
| 大模型 | OpenAI 兼容接口（`POST {AI_BASE_URL}/chat/completions`） |
| 测试 | pytest（598 个用例，全部离线，不联网） |

依赖清单见 [`requirements.txt`](requirements.txt)（Flask / requests / python-dotenv / pytest 等）。

工程约束（见 [`AGENTS.md`](AGENTS.md)）：不引入 Vue / React / FastAPI / Docker / Redis / Kafka，不使用多智能体，不进行大规模重构。

---

## 2. 目录结构

```
campus-ai/
├── AGENTS.md                  # 项目约束：技术栈、AI 分工红线、数据规则
├── README.md                  # 本文件
├── requirements.txt           # Python 依赖
├── pytest.ini                 # pytest 配置（测试目录、临时目录固定在项目内）
├── .env.example               # 环境变量模板（复制成 .env 后填 Key）
├── backend/                   # Flask 后端（分层：路由 / 业务查询 / AI 编排）
│   ├── app.py                 #   应用工厂、CORS、统一 JSON 错误处理、命令行入口
│   ├── routes.py              #   路由层：解析参数 → 调用业务函数 → 返回 JSON（无 SQL）
│   ├── queries.py             #   业务查询与计算（不 import flask，脚本可直接调用）
│   ├── db.py                  #   SQLite 连接管理与参数化查询工具
│   ├── validators.py          #   参数校验（字符串 → 安全值，非法即 400）
│   ├── validate.py            #   意图参数白名单与学期校验
│   ├── errors.py              #   400 / 404 / 503 业务异常
│   ├── ai_parser.py           #   意图解析器（自然语言 → Intent JSON，不查数据）
│   ├── classroom.py           #   AI 找教室编排：解析 → 校验 → 查询 → 解释
│   ├── forecast.py            #   食堂趋势 / 高峰 / 预测的统计与解释
│   ├── industry.py            #   产教融合技能匹配与解释
│   └── time_resolver.py       #   相对日期解析（明天 / 下周三 …）
├── frontend/
│   └── index.html             # 单文件大屏（HTML + CSS + JS + 内置离线快照）
├── scripts/
│   ├── make_data.py           # 生成 9 个模拟数据 CSV（固定种子 42，自带 8 组检查）
│   ├── import_data.py         # CSV → SQLite 导入（可重复执行）
│   └── test_ai.py             # 小工具：调用一次大模型，确认 Key / 网关可用
├── data/
│   ├── raw/                   # 9 个 CSV（由 make_data.py 生成）
│   └── campus.db              # SQLite 数据库（由 import_data.py 生成）
├── prompts/                   # 运行时 system prompt（程序读，不是对话日志）
│   ├── intent_parser.md       #   意图解析
│   ├── insight_generator.md   #   找教室结果解释
│   ├── canteen_insight_generator.md    # 食堂结果解释
│   ├── industry_insight_generator.md   # 产教融合结果解释
│   └── PROMPTS-README.md      #   本目录说明
├── docs/
│   ├── PRD.md                 # 产品需求文档
│   ├── API.md                 # 接口文档（10 个接口的字段级说明）
│   ├── DATABASE.md            # 数据库设计（9 张表 + 完整 CREATE TABLE SQL）
│   └── ai-collaboration/      # 人机协作开发过程记录（逐字 Prompt + 真实验证）
└── tests/                     # pytest 用例（598 个，离线）
```

---

## 3. 环境要求

| 项目 | 要求 | 本机核对结果 |
|---|---|---|
| Python | 3.12 | `python --version` / `py -3.12 --version` → `Python 3.12.7` |
| Git | 任意较新版本 | `git --version` → `git 2.55.0.windows.5` |
| 操作系统 | Windows 为主（命令按 PowerShell 写） | 已在 Windows + PowerShell 下实测 |

大模型 API Key **不是**启动必需项：不填 Key 也能跑数据脚本、跑测试、打开前端看离线演示（见第 11、19 节）。

---

## 4. 克隆仓库

```powershell
git clone https://github.com/CrestLune/campus-ai.git
cd campus-ai
```

---

## 5. 创建并激活虚拟环境

```powershell
py -3.12 -m venv .venv
```

Windows PowerShell 激活：

```powershell
.\.venv\Scripts\Activate.ps1
```

激活成功后命令行提示符前会出现 `(.venv)`。

> 如果报「禁止运行脚本」，见第 19 节常见问题。
> 也可以不激活，直接用 `.\.venv\Scripts\python.exe` 代替下面所有 `python` 命令。

---

## 6. 安装依赖

```powershell
pip install -r requirements.txt
```

安装内容为 [`requirements.txt`](requirements.txt) 中固定的 19 个包（Flask 3.1.3、requests 2.34.2、python-dotenv 1.2.3、pytest 9.1.1 等）。

---

## 7. 配置 `.env`

把模板复制成 `.env`：

```powershell
Copy-Item .env.example .env
```

然后编辑 `.env`，填入你的 Key：

```ini
AI_API_KEY=你的Key
AI_BASE_URL=https://api.deepseek.com
AI_MODEL=deepseek-flash
AI_MAX_TOKENS=2048
AI_TIMEOUT=30
```

`.env.example` 一共 **5 个变量**，含义如下：

| 变量 | 含义 | 模板中的值 | 代码中的默认值 |
|---|---|---|---|
| `AI_API_KEY` | 大模型 API Key。**没有默认值**，缺失时 AI 解析会以 `missing_config` 失败（找教室接口返回 503，其余接口降级为 Python 兜底文案） | 空 | 无（必填才用 AI） |
| `AI_BASE_URL` | OpenAI 兼容网关地址，最终请求 `{AI_BASE_URL}/chat/completions` | `https://api.deepseek.com` | `https://api.deepseek.com` |
| `AI_MODEL` | 模型名 | `deepseek-flash` | `deepseek-flash` |
| `AI_MAX_TOKENS` | 单次回复的 token 上限。带推理的网关会先花掉 reasoning token，太小会把 JSON 截断（`finish_reason=length`），表现为解释静默降级成兜底文案；回显长解释时可调到 3000 | `2048` | `2048` |
| `AI_TIMEOUT` | 单次调用超时（秒） | `30` | `30.0` |

规则与出处：配置读取见 `backend/ai_parser.py` 的 `load_config()`（默认值常量 `DEFAULT_BASE_URL` / `DEFAULT_MODEL` / `DEFAULT_TIMEOUT` / `DEFAULT_MAX_TOKENS`）。

- `.env` 已在 [`.gitignore`](.gitignore) 中忽略，**不会提交**；
- `AGENTS.md` 明令：**API Key 不得写进源代码**；
- 想先确认 Key / 网关是否可用，可以跑一次小工具：`python scripts\test_ai.py`（它只发一次极小的请求并打印状态码与响应）。

---

## 8. 生成模拟数据

```powershell
python scripts\make_data.py
```

- **固定随机种子 42**（`random.seed(42)`，AGENTS.md Data Rules），每次运行结果字节级一致、可重复；
- 输出 **9 个 CSV** 到 `data\raw\`，正好对应数据库的 9 张表；
- 脚本结束前自动执行 **8 组检查**并打印结果，任一检查失败退出码为 1：

  1. 记录数检查　2. 空字段检查　3. 重复检查　4. 课表冲突检查
  5. 教室容量与属性检查　6. 食堂档案 / 营业时间 / 高峰与模式检查
  7. 课程 / 岗位技能关联检查　8. 可复现性检查（SHA-256 前 16 位）

本次实测的运行结果（全部检查通过，0 个问题）：

| CSV | 行数 | 字节 |
|---|---|---|
| `classrooms.csv` | 50 | 2570 |
| `courses.csv` | 100 | 5848 |
| `schedules.csv` | 1186 | 63270 |
| `canteens.csv` | 5 | 673 |
| `meal_records.csv` | 8480 | 421120 |
| `jobs.csv` | 100 | 32581 |
| `skills.csv` | 30 | 2522 |
| `course_skills.csv` | 300 | 4861 |
| `job_skills.csv` | 400 | 5352 |

可复现性验证：连续生成两次，9 个文件的 SHA-256 与仓库里已有的 `data\raw\*.csv` **完全一致**。

---

## 9. 导入数据库

```powershell
python scripts\import_data.py
```

- 把 `data\raw\*.csv` 导入 `data\campus.db`，固定 **9 张表**；
- 表结构的唯一来源是 [`docs/DATABASE.md`](docs/DATABASE.md) 的「完整 CREATE TABLE SQL」，导入器直接读取该章节，不会和文档漂移；
- 采用「临时库 + 事务 + 原子替换」：任何一步失败都会回滚，**可重复执行**，结果与首次一致（不追加、不重复计数）。

实测输出（全部表导入成功、外键完整性通过）：

| 表名 | 行数 |
|---|---|
| classrooms | 50 |
| courses | 100 |
| schedules | 1186 |
| canteens | 5 |
| meal_records | 8480 |
| jobs | 100 |
| skills | 30 |
| course_skills | 300 |
| job_skills | 400 |
| **合计** | **10651** |

常用参数：`--verify-only` 只统计行数不写库；`--no-rebuild` 不重建只补索引；`--quiet` 只打印每张表行数。

---

## 10. 启动后端

```powershell
python backend\app.py
```

- 监听 **`http://127.0.0.1:5000`**（`backend/app.py` 中 `DEFAULT_HOST = "127.0.0.1"`、`DEFAULT_PORT = 5000`）；
- 也可以用模块方式启动：`python -m backend.app`，或 `$env:FLASK_APP="backend.app"` 后 `python -m flask run`；
- 环境变量覆盖：`HOST` / `PORT` 改监听地址，`CAMPUS_DB_PATH` 改数据库路径；
- 自检：浏览器打开 `http://127.0.0.1:5000/api/meta/semesters`，应返回 `{"status":"ok","data":{...}}`。

---

## 11. 打开前端

**直接双击 `frontend\index.html`** 即可（用浏览器打开本地文件，不需要额外的前端服务器）。

- 前端会向 `http://127.0.0.1:5000/api` 请求数据；以 `file://` 打开时跨域请求带 `Origin: null`，后端的 CORS 钩子会按请求回显 Origin，并放行 `GET, POST, OPTIONS`（见 `backend/app.py` 的 `_register_cors`）；
- 页面内置**离线兜底快照**：后端没启动、请求超时或接口失败时自动回落到内置数据，图表与示例问答不会空白，并会显示「离线示例」标记与数据来源说明；
- **不填 AI Key 也能看离线演示**：不配 Key 时，找教室接口返回 503 + `ai_unavailable`，页面按接口失败处理，回落到内置离线快照。

> 页面设计基准为 1920 × 1080，建议全屏或较宽的窗口查看。

---

## 12. API 说明（10 个接口）

- 统一前缀 `/api`，全部返回 JSON，统一外壳：

  ```json
  { "status": "ok", "data": { "...": "..." } }
  { "status": "error", "error": { "code": "invalid_parameter", "message": "...", "field": "weekday" } }
  ```

- 空串参数（`?a=`）按「未传」处理；所有 SQL 使用 `?` 绑定参数；无数据返回 200 + 空数组，参数非法才返回 4xx。
- `explain` 参数（默认 1）控制是否调用 AI 解释：`0` 时只用 Python 兜底文案，离线可用。
- 字段级细节（响应样例、排序规则、错误码）见 [`docs/API.md`](docs/API.md)。

| # | 接口 | 方法 | 主要参数 | 返回 |
|---|---|---|---|---|
| 1 | `/api/classrooms/available` | GET | `capacity`(≥1)、`has_projector`(0/1)、`has_computer`(0/1)、`room_type`(普通/多媒体/机房/实验室)、`building`、`weekday`(1..7)、`period_start`/`period_end`(1..12)、`semester`、`limit`(1..100，默认 10) | `query`（回显口径）、`summary`（候选数/已占用/空闲数/容量窗口）、`rooms[]`（教室 + `status`）；先「刚好够用」再按容量、楼层升序 |
| 2 | `/api/classrooms/search` | GET / POST | `question`（必填，≤200 字；GET 走查询串，POST 走 JSON 请求体）、`explain`(0/1，默认 1) | `intent`、`params`（校验后的意图参数）、`query`、`total`、`summary`、`rooms[]`、`hint[]`（空结果时的放宽建议）、`notes[]`、`explanation{text,summary,highlights,suggestions,source,error}` |
| 3 | `/api/canteens/stats` | GET | `date_from`/`date_to`(YYYY-MM-DD)、`canteen_id`(≥1)、`time_slot`(breakfast/lunch/dinner/other)、`top_slots`(0..4，默认 3) | `summary`（食堂数/记录数/总人次/平均等待/日期范围）、`canteens[]`（按 `avg_people` 降序：容量、窗口数、平均与最大人流、等待、`congestion`、`peak_hour`、`peak_slots`） |
| 4 | `/api/canteens/trend` | GET | 同上 + `explain`(0/1) | `summary`（含最忙/最闲日期）、`trend{direction,first_half_avg,second_half_avg,change_rate}`、`series[]`（按天客流明细）、`weekday_profile[]`、`notes[]`、`explanation`；另附 `report`（底层完整报表） |
| 5 | `/api/canteens/peak` | GET | 同上 + `peak_ratio`(0.1..1.0，默认 0.8) | `summary`（峰值小时/高峰区间/峰值时段）、`window`、`hours[]`（逐小时人流与等待、`is_peak`、`in_window`）、`ranked_hours[]`、`slots[]`（早/午/晚餐对比）、`explanation` |
| 6 | `/api/canteens/forecast` | GET | 同上 + `target_date`(YYYY-MM-DD，默认历史最后一天之后)、`horizon_days`(1..30，默认 3) | `method`（固定为「历史同期平均值 + 星期修正 + 时间段修正」）、`training`（基准均值、`by_weekday`、`by_hour`）、`predictions[]`（逐日逐小时预测，含 `base`/`weekday_adjustment`/`hour_adjustment` 可原地复算）、`notes[]`、`explanation` |
| 7 | `/api/meta/semesters` | GET | 无 | `semesters[]`（库中真实有排课记录的学期）、`count`、`default`（如 `2025-2026-1`、`2025-2026-2`） |
| 8 | `/api/industry/skills` | GET | `dimension`(industry/city/major/company，默认 industry)、`industry`、`city`、`major`、`top`(1..100，默认 10) | `summary`（分组数/岗位数/技能数）、`groups[]`（按 `job_count` 降序：薪资区间、`top_skills[]` 含需要该技能的岗位数与平均重要度） |
| 9 | `/api/industry/gap` | GET | `major`（**必填**）、`industry`、`min_importance`(1..5，默认 1)、`top`(1..100，默认 20) | `summary`（岗位数、课程技能数、要求技能数、覆盖/缺口数、`coverage_rate`、`weighted_coverage_rate`、`course_covered_ratio`）、`covered_skills[]`、`missing_skills[]`、`required_missing_skills[]` |
| 10 | `/api/industry/match` | GET | `major`（**必填**）、`industry`、`min_importance`(1..5)、`top`(1..100)、`question`(≤200)、`explain`(0/1) | `scope`、`summary`、`coverage_rate`、`importance`（`scale`/`max`/`avg`/`level` + `distribution[]`）、`skill_demands[]`、`covered_skills[]`、`missing_skills[]`、`required_missing_skills[]`、`job_matches[]`、`notes[]`、`explanation`、`results_fingerprint` |

调用示例：

```powershell
# 找教室（自然语言）
curl.exe "http://127.0.0.1:5000/api/classrooms/search?question=%E5%91%A8%E4%B8%89%E4%B8%8B%E5%8D%88%E6%89%BE%E4%B8%80%E4%B8%AA8%E4%BA%BA%E3%80%81%E6%9C%89%E6%8A%95%E5%BD%B1%E7%9A%84%E7%A9%BA%E6%95%99%E5%AE%A4"

# 产教融合技能缺口
curl.exe "http://127.0.0.1:5000/api/industry/gap?major=%E8%BD%AF%E4%BB%B6%E5%B7%A5%E7%A8%8B"
```

错误码约定：`400` 参数非法（附 `field`）、`404` 查询目标不存在（附 `available` 可选清单）、`405` 方法不允许、`503` 数据库不可用或 `ai_unavailable`、`500` 未预期异常。

其中，`/api/industry/match` 有一条硬红线：解释阶段只允许往响应里加 `explanation`，`results_fingerprint`（SHA-256）在解释前后必须一致，否则直接抛 `RuntimeError`——**AI 不能修改计算结果**。

---

## 13. 跑测试

```powershell
pytest -q
```

实测结果：

```
598 passed
```

- 当前 **598 个用例全绿**，全部离线执行，**不联网**；
- 覆盖范围：接口与业务层（`tests\test_api.py`）、AI 找教室链路（`test_classroom.py`）、食堂趋势/高峰/预测与 AI 解释边界（`test_forecast.py`）、产教融合匹配与覆盖率（`test_industry.py`）、意图解析（`test_intent.py`）、相对日期（`test_time_resolver.py`）、数据导入（`test_import_data.py`）；
- 测试把 `data\campus.db` 复制到临时目录后只读使用，**不会修改仓库里的数据库**，也不会触碰 `frontend\`；
- `pytest.ini` 已把 pytest 临时目录固定在项目内（`--basetemp=.pytest_tmp`），Windows 受限环境下也能跑。

> 注意：`pytest` 必须在虚拟环境里执行；如果系统 Python 里没装 pytest，会报 `No module named pytest`，先激活 `.venv` 再跑。

---

## 14. AI 架构：一句话讲清

**意图解析 → 参数校验 → Python 查询 → SQLite → AI 解释，AI 不碰 SQL。**

以「AI 找教室」为例，完整链路（见 `backend/classroom.py` 的模块说明）：

```
用户提问
  → Intent Parser（prompts/intent_parser.md：只识别意图 + 抽参数，不查数据）
  → 参数校验（backend/validate.py 白名单 + classroom.validate_room_params，非法即 400）
  → classroom 服务（backend/classroom.py）
  → SQLite（queries.list_available_classrooms，全部 ? 参数化绑定）
  → 候选教室（Python 排序 + 空结果放宽建议）
  → AI 解释结果（prompts/insight_generator.md，只能看到查询结果）
```

三条红线（对应 AGENTS.md AI Rules）：

1. **AI 不访问数据库、不生成 SQL**：模型只拿到查询结果构成的只读事实表，SQLite 连接在模型调用之前就已关闭；模型返回的解释若出现 SQL 片段（`_SQL_RE` 检查），整条退回 Python 兜底解释。
2. **查询与计算全部由 Python 完成**：教室空闲是查询时用 `classrooms` 左连接该时段 `schedules` 实时算出来的，不落库；食堂的统计与预测、产教的集合运算与覆盖率加权也都在 Python 里做（预测只有均值与加减法，不训练任何模型）。
3. **AI 故障不影响接口可用性**：解释阶段的 `source` 为 `ai` 或 `local`，AI 不可用/输出不合法时用 Python 兜底文案，接口仍返回 200。

---

## 15. `prompts/` 和 `docs/ai-collaboration/` 分别是干嘛的

两个目录**很容易搞混**，区别如下：

| | [`prompts/`](prompts/) | [`docs/ai-collaboration/`](docs/ai-collaboration/) |
|---|---|---|
| 内容 | 程序运行时的 system prompt | 「人如何指挥 AI 把项目做出来」的过程记录 |
| 消费者 | Python 代码（读出来喂给大模型） | 比赛评委、未来的自己和队友 |
| 作用 | 决定线上行为：意图解析、结果解释 | 证明 AI 协同开发过程：逐字 Prompt、真实回复、具体 Bug、验证输出 |
| 改坏了会怎样 | 功能 / 测试失败 | 无影响 |

- **`prompts/`** 里的 `.md` 由 `backend/ai_parser.py` 的 `load_prompt()` 读取后作为 `system` 角色消息发出，共 4 个正式 Prompt：`intent_parser.md`（意图解析）、`insight_generator.md`（找教室解释）、`canteen_insight_generator.md`（食堂解释）、`industry_insight_generator.md`（产教融合解释）。按 AGENTS.md，Prompt 修改必须通过 Git 留痕。详见 [`prompts/PROMPTS-README.md`](prompts/PROMPTS-README.md)。
- **`docs/ai-collaboration/`** 按统一骨架记录每一轮开发（逐字 Prompt 原文 → Agent 回复关键段 → 具体问题 → 改动文件与行号 → 关键 diff → 真实 pytest / HTTP 输出 → 剩余风险），另有 `CONV-01-对话日志.md` 与 `images/`。详见 [`docs/ai-collaboration/README.md`](docs/ai-collaboration/README.md)。

一句话：**`prompts/` 是给程序吃的，`docs/ai-collaboration/` 是给人看的。**

---

## 16. 三个 Demo 问句

在前端页面的问答框里直接输入（对应 `tests/golden_questions.json` 中已固化的用例）：

| 场景 | 问句 | 走哪条链路 |
|---|---|---|
| AI 找教室 | **周三下午帮我找一个 8 个人、有投影的空教室。** | `POST /api/classrooms/search` → 意图 `room_search` → SQLite 查空闲教室 → AI 解释 |
| 食堂分析 | **上周一食堂中午为什么那么挤？** | `/api/canteens/stats` + `/api/canteens/peak` → Python 统计高峰与等待时间 → AI 解释 |
| 产教融合 | **软件工程专业的课程技能和企业岗位需求差在哪里？** | `/api/industry/gap`（或 `/api/industry/match`）→ Python 算覆盖率与缺口 → AI 解释 |

更多可直接用的问句：`明天上午三四节想找一间能坐60人、带电脑的机房。`、`预测下周三中午二食堂会有多少人。`、`数据科学与大数据技术专业对口岗位最需要哪些技能？`

> 未配置 Key / 后端未启动时，这些问句会得到带「离线示例」标记的内置快照回答，仍然可以演示。

---

## 17. 常见问题

### Q1. PowerShell 提示「在此系统上禁止运行脚本」，无法激活虚拟环境

错误形如：`.\ .venv\Scripts\Activate.ps1 : 无法加载文件 … 因为在此系统上禁止运行脚本`。

按需选择一种：

```powershell
# 方式一：只对当前用户放开（推荐，立即生效且不需要管理员）
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

# 方式二：只对当前这个 PowerShell 窗口生效，关掉窗口即失效
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# 方式三：不激活，直接用绝对路径跑（最省事，不受执行策略影响）
.\.venv\Scripts\python.exe -m pytest -q
```

### Q2. 端口 5000 被占用

错误形如：`OSError: [WinError 10048] 通常每个套接字地址…只允许使用一次`。

```powershell
# 看谁占用了 5000
Get-NetTCPConnection -LocalPort 5000 -State Listen | Select-Object OwningProcess
Get-Process -Id <上面查到的PID>

# 方式一：改端口启动（前端默认请求 5000，改端口后需同步改前端的接口基址）
$env:PORT=5001; python backend\app.py

# 方式二：改 HOST，仅本机可见
$env:HOST="127.0.0.1"; python backend\app.py

# 方式三：结束占用进程（确认该进程确实可以结束时再执行）
Stop-Process -Id <PID>
```

前端接口基址在 `frontend/index.html` 顶部的 `API_BASE`（`file://` 打开时为 `http://127.0.0.1:5000/api`）。

### Q3. API Key 失效 / 未配置时会不会报错？

**会自动降级，不会崩**。分工是「解析必须靠 AI，解释可以不要 AI」：

| 接口 | 没有可用 Key / Key 失效时 |
|---|---|
| `/api/classrooms/search` | 意图解析不可用 → 返回 `503` + `error.code = "ai_unavailable"`（消息里带原因，如 `missing_config`） |
| `/api/canteens/trend` / `peak` / `forecast` | 正常返回 `200`，`explanation.source = "local"`（Python 兜底文案），`explanation.error.code` 给出原因（`missing_config` / `network_error` / `invalid_json` / `unsafe_explanation`） |
| `/api/industry/match` | 同上，`200` + `source = "local"`，计算结果与覆盖率完全不受影响 |
| 前端页面 | 接口失败时回落到内置离线快照，问答回答带「离线示例」标记，图表不空白 |

实测：清空 `AI_API_KEY` 后，`/api/canteens/trend` 返回 `200`、`source=local`、`error.code=missing_config`；`/api/classrooms/search` 返回 `503`；`/api/industry/match?major=软件工程` 返回 `200` 且 `coverage_rate=0.5882`（与配置好 Key 时一致）。

排查建议：先跑 `python scripts\test_ai.py` 确认 Key 与网关连通；若报截断（`truncated_response` / `invalid_json`），把 `.env` 中的 `AI_MAX_TOKENS` 调到 3000；若是网络问题，检查 `AI_BASE_URL` 是否可访问。

---

## 18. 参考文档

| 文档 | 内容 |
|---|---|
| [`AGENTS.md`](AGENTS.md) | 项目约束：技术栈、AI 分工红线、数据规则、DoD |
| [`docs/PRD.md`](docs/PRD.md) | 产品需求：背景、目标、三个核心场景、MVP 与非目标 |
| [`docs/API.md`](docs/API.md) | 10 个接口的字段级说明、响应样例、错误码、测试说明 |
| [`docs/DATABASE.md`](docs/DATABASE.md) | 9 张表设计、完整 CREATE TABLE SQL、三大功能如何使用这些表 |
| [`docs/ai-collaboration/README.md`](docs/ai-collaboration/README.md) | AI 协作记录索引与记录规范 |
| [`prompts/PROMPTS-README.md`](prompts/PROMPTS-README.md) | `prompts/` 目录说明与调用链 |

---

## 19. 从零到跑起来的命令速查

```powershell
git clone https://github.com/CrestLune/campus-ai.git
cd campus-ai
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env     # 然后编辑 .env 填入 AI_API_KEY（不填也能跑离线演示）
python scripts\make_data.py     # 生成 9 个 CSV 到 data\raw
python scripts\import_data.py   # 导入 data\campus.db（9 张表，10651 行）
python backend\app.py           # 后端 http://127.0.0.1:5000
# 另开一个窗口：双击 frontend\index.html
pytest -q                       # 598 passed
```
