# CampusAI 基础 API（Flask + SQLite）

- 代码位置：`backend/`（分层见下）
- 数据库：`data/campus.db`（由 `python scripts/import_data.py` 生成，**只读查询**）
- 确定性接口（食堂汇总 / 产教 / 教室列表）不接 AI：SQL 查询 + Python 计算，没有 LLM 参与
- `GET/POST /api/classrooms/search`、`GET /api/canteens/trend|peak|forecast` 是接入 AI 的接口：
  AI 只负责**识别意图**与**解释结果**，参数校验、数据查询、统计与预测全部由 Python + SQLite 完成
  （AI 不碰数据库，也不生成 SQL，更不替 Python 算数）
- 所有响应都是 JSON，统一外壳：

```json
{ "status": "ok", "data": { "...": "..." } }
{ "status": "error", "error": { "code": "invalid_parameter", "message": "...", "field": "weekday" } }
```

## 启动

```bash
# 项目根目录
python -m backend.app            # 监听 127.0.0.1:5000
# 或
set FLASK_APP=backend.app && python -m flask run
```

环境变量：`CAMPUS_DB_PATH` 覆盖数据库路径，`HOST` / `PORT` 覆盖监听地址。

## 分层（AGENTS.md：业务查询函数与 Flask 路由分离）

| 文件 | 职责 |
|---|---|
| `backend/app.py` | 应用工厂 + 统一 JSON 错误处理 |
| `backend/routes.py` | 路由：解析参数 → 调用业务函数 → 返回 JSON（无 SQL） |
| `backend/queries.py` | 业务查询与计算（不 import flask，可被脚本直接调用） |
| `backend/classroom.py` | AI 智能找教室的编排：解析 → 校验 → 查询 → 解释（不写 SQL） |
| `backend/forecast.py` | 食堂分析：趋势 / 高峰 / 午餐预测的统计与预测 + 结果解释（不写 SQL） |
| `backend/ai_parser.py` | 意图解析器（自然语言 → Intent JSON，不查数据） |
| `backend/validators.py` | 参数校验（字符串 → 安全值，非法即 400） |
| `backend/db.py` | 连接管理与参数化查询工具 |
| `backend/errors.py` | `ValidationError(400)` / `NotFoundError(404)` / `DatabaseUnavailableError(503)` |

正式 Prompt 放在 `prompts/`：`intent_parser.md`（意图解析）、`insight_generator.md`（教室结果解释）、
`canteen_insight_generator.md`（食堂结果解释）。

## 通用约定

- 空串参数（`?a=`）按「未传」处理。
- 所有 SQL 使用 `?` 绑定参数，业务层不拼接用户输入。
- 无数据时返回 `200` + 空数组（「查得到，只是没有」），参数非法才返回 `4xx`。

---

## 1. GET /api/classrooms/available

按「人数 + 设备 + 时段空闲」推荐教室；空闲是**查询时算出来的**（`classrooms` 左连接该时段 `schedules`），不落库。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `capacity` | int ≥ 1 | - | 最低容纳人数 |
| `has_projector` | 0/1 | - | 需要投影 |
| `has_computer` | 0/1 | - | 需要电脑 |
| `room_type` | 普通/多媒体/机房/实验室 | - | 教室类型 |
| `building` | text | - | 楼栋 |
| `weekday` | 1..7 | - | 星期几（传了就要能确定 `semester`） |
| `period_start` / `period_end` | 1..12 | - | 节次区间（只传一端则按同一节处理） |
| `semester` | text | 库里唯一学期时自动补齐 | 学期，如 `2025-2026-1` |
| `limit` | 1..100 | 10 | 返回条数上限 |

响应要点：

```json
{
  "data": {
    "query":  { "semester": "2025-2026-1", "weekday": 3, "period_start": 3, "period_end": 4, "capacity": 60 },
    "total": 8,
    "summary": { "candidate_count": 25, "occupied_count": 24, "available_count": 8, "capacity_window": 90 },
    "rooms": [
      { "room_id": 48, "room_name": "E202", "building": "综合楼", "floor": 2, "capacity": 60,
        "has_projector": true, "has_computer": true, "room_type": "机房", "status": "available" }
    ]
  }
}
```

排序：先「刚好够用」（`capacity - 需求` 最小），再容量、楼层升序。`limit` 只截断 `rooms`，`total` 始终是完整命中数。

错误：`400`（weekday/period/capacity/limit/room_type 非法）、`404`（semester 不存在，附 `available` 学期清单）。

## 1.1 GET/POST /api/classrooms/search（AI 智能找教室）

用一句自然语言找空教室：`周三下午帮我找一个8人、有投影的空教室。`

完整链路：

```
question
  → Intent Parser（prompts/intent_parser.md，只识别意图 + 抽参数）
  → 参数校验（validate.py 白名单 + classroom.validate_room_params，非法即 400）
  → classroom service（backend/classroom.py）
  → SQLite（queries.list_available_classrooms）
  → 候选教室（Python 排序 + 空结果放宽建议）
  → AI 解释结果（prompts/insight_generator.md，只能看到查询结果）
```

| 参数 | 位置 | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `question` | POST JSON 请求体 / GET 查询串 | text ≤ 200 | **必填** | 用户自然语言问题 |
| `explain` | 同上 | 0/1 | 1 | 是否调用 AI 解释结果（0 = 只用 Python 兜底文案，离线可用） |

响应要点：

```json
{
  "data": {
    "question": "周三下午帮我找一个8人、有投影的空教室。",
    "intent": "room_search",
    "params": { "capacity": 8, "has_projector": true, "weekday": 3, "period_start": 5, "period_end": 8 },
    "query": { "semester": "2025-2026-1", "weekday": 3, "period_start": 5, "period_end": 8,
               "capacity": 8, "has_projector": true, "status": "available", "limit": 10 },
    "total": 14,
    "summary": { "candidate_count": 31, "occupied_count": 17, "available_count": 14, "capacity_window": 50 },
    "rooms": [ { "room_id": 24, "room_name": "B103", "building": "第二教学楼", "floor": 1,
                 "capacity": 45, "has_projector": true, "has_computer": false,
                 "room_type": "普通", "status": "available" } ],
    "hint": [],
    "notes": ["共 14 间符合条件，受 limit=10 限制这里只返回前 10 间。"],
    "explanation": {
      "text": "按 至少 8 人、需要投影、周三、第 5-8 节 查询，找到 14 间空教室。\n• …",
      "summary": "…", "highlights": ["…"], "suggestions": ["…"],
      "source": "ai", "error": null
    }
  }
}
```

- `params` 是**校验后**的意图参数（白名单外的字段会被丢弃并在 `notes` 里留痕，因此 AI 无法指定 `room_id`）。
- `query` / `total` / `summary` / `rooms` 原样来自 `backend/queries.py`，即来自 SQLite。
- `explanation.source`：`ai` = 模型解释；`local` = AI 不可用或返回不合法时的 Python 兜底文案，
  此时 `explanation.error` 里带错误码（`missing_config` / `network_error` / `invalid_json` / `unsafe_explanation`）。
- `hint`：**查询为空时**由 Python 再查一次 SQLite 得出的放宽建议，例如
  「不限制人数时，同一时段有 14 间空教室，但可能坐不下 1000 人。」；AI 不会独自编造建议。
- `notes`：解析层的丢弃提示、`limit` 截断说明、查询层的已知语义缺口（如 `has_projector=false` 不生效）。

错误：

- `400 unsupported_intent`：识别到的意图不是 `room_search`（例如食堂问题），附 `field=intent`
- `400 invalid_parameter`：`question` 非法 / 意图参数没通过校验（附 `field`）
- `503 ai_unavailable`：意图解析不可用（缺 `AI_API_KEY` 或网络失败），消息里带原因

> 设计约束：AI 不能访问数据库、不能生成 SQL、不能选教室；SQLite 只保存事实数据；
> 教室是否空闲由 Python 在查询时实时计算（见 `backend/classroom.py` 的模块说明）。

## 2. GET /api/canteens/stats

食堂人流、等待时间、拥挤度与高峰时段统计（只聚合历史，不做预测）。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `date_from` / `date_to` | `YYYY-MM-DD` | - | 日期区间（闭区间） |
| `canteen_id` | int ≥ 1 | - | 指定食堂 |
| `time_slot` | breakfast/lunch/dinner/other | - | 指定时段 |
| `top_slots` | 0..4 | 3 | 每个食堂返回几个高峰时段 |

响应要点（`canteens[]` 按 `avg_people` 降序）：

```json
{
  "data": {
    "summary": { "canteens": 5, "records": 3000, "people_total": 1234567,
                 "avg_wait_minutes": 6.4, "date_start": "2025-09-01", "date_end": "2025-11-14" },
    "canteens": [
      { "canteen_id": 1, "canteen_name": "一食堂", "status": "open", "seat_capacity": 1200,
        "window_count": 32, "records": 225, "record_days": 75, "people_total": 131378,
        "avg_people": 583.9, "max_people": 847, "avg_wait_minutes": 7.83, "max_wait_minutes": 14.2,
        "avg_capacity": 1200.0, "congestion": 0.4866, "congestion_raw": 0.4866,
        "peak_hour": { "hour": 12, "avg_people": 702.35, "avg_wait_minutes": 10.34 },
        "peak_slots": [ { "time_slot": "lunch", "avg_people": 583.9, "avg_wait_minutes": 7.83, "people_total": 131378 } ] }
    ]
  }
}
```

- `congestion = avg_people / avg_capacity`，归一化到 `0..1`（`congestion_raw` 保留原值，> 1 表示超过同时在店容量）。
- 容量优先取 `meal_records.capacity` 快照，缺失时回退 `canteens.capacity`。

错误：`400`（日期格式/区间颠倒、`canteen_id`、`time_slot`、`top_slots` 非法）。

## 2.1 GET /api/canteens/trend

历史就餐趋势：按天汇总人流，并给出前后半段对比的趋势方向。**统计全部在 Python**（`backend/forecast.py`）。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `date_from` / `date_to` | `YYYY-MM-DD` | - | 日期区间（闭区间） |
| `canteen_id` | int ≥ 1 | - | 指定食堂；不传表示全校合计 |
| `time_slot` | breakfast/lunch/dinner/other | - | 指定时段；不传表示全部时段 |
| `explain` | 0/1 | 1 | 是否调用 AI 解释结果（`0` 只用 Python 兜底文案） |

口径：先把「同一日期 + 同一小时」的多食堂记录合计成一个**客流单元**，再对单元做统计，
因此不指定食堂时 `people_total` 是全校合计、`avg_people` 是每小时的校园平均客流。

```json
{
  "data": {
    "params": { "canteen_id": 1, "time_slot": "lunch", "date_from": null, "date_to": null,
                "target_date": null, "horizon_days": 3, "peak_ratio": 0.8 },
    "query": { "canteen_id": 1, "date_from": null, "date_to": null, "time_slot": null },
    "summary": { "days": 75, "records": 225, "canteens": 1, "people_total": 131378,
                 "avg_people_per_day": 1751.71, "avg_people_per_hour": 583.9,
                 "date_start": "2025-09-01", "date_end": "2025-11-14",
                 "busiest_date": { "date": "2025-09-05", "weekday_name": "周五", "people_total": 2034 },
                 "quietest_date": { "date": "2025-10-04", "weekday_name": "周六", "people_total": 910 } },
    "trend": { "direction": "up", "first_half_avg": 1668.35, "second_half_avg": 1832.87,
               "change_rate": 0.0986, "compared_days": 75, "flat_tolerance": 0.05 },
    "series": [ { "date": "2025-09-01", "weekday": 1, "weekday_name": "周一", "records": 3,
                  "hours": 3, "people_total": 1706, "avg_people_per_hour": 568.67,
                  "avg_wait_minutes": 7.33 } ],
    "weekday_profile": [ { "weekday": 1, "weekday_name": "周一", "days": 11, "samples": 33,
                           "people_total": 19468, "avg_people": 589.94 } ],
    "notes": [ "本次只看「午餐」时段。" ],
    "explanation": { "text": "…", "summary": "…", "highlights": ["…"], "suggestions": ["…"],
                     "source": "ai", "error": null }
  }
}
```

`trend.direction`：`up` / `down` / `flat`（前后半段日均人流变化率在 ±`flat_tolerance` 内即为 `flat`）。

## 2.2 GET /api/canteens/peak

高峰时间段：逐小时人流与等待时间、峰值小时、**高峰区间**，以及早/午/晚餐对比。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `date_from` / `date_to` | `YYYY-MM-DD` | - | 日期区间 |
| `canteen_id` | int ≥ 1 | - | 指定食堂 |
| `time_slot` | breakfast/lunch/dinner/other | - | 指定时段 |
| `peak_ratio` | 0.1..1.0 | 0.8 | 高峰边界：人流 ≥ 峰值 × 该比例算高峰 |
| `explain` | 0/1 | 1 | 是否调用 AI 解释结果 |

高峰区间算法（纯比较，不做回归）：从峰值小时向两侧扩张，只吸收**逐个小时相邻**且
`avg_people >= 峰值 × peak_ratio` 的小时。`label` 里的右端点已 +1 小时，例如 `"11:00-13:00"`
表示 11 点与 12 点这两个小时。

```json
{
  "data": {
    "peak_ratio": 0.8,
    "summary": { "records": 225, "people_total": 131379, "peak_hour": 12,
                 "peak_avg_people": 702.35, "peak_window": "11:00-13:00",
                 "peak_slot": "lunch", "peak_slot_name": "午餐" },
    "window": { "start_hour": 11, "end_hour": 12, "hours": [11, 12], "label": "11:00-13:00",
                "avg_people": 644.92, "peak_hour": 12, "peak_avg_people": 702.35,
                "threshold_ratio": 0.8, "threshold_people": 561.88 },
    "hours": [ { "hour": 12, "days": 75, "samples": 75, "people_total": 52676,
                 "avg_people": 702.35, "share": 0.4009, "avg_wait_minutes": 10.34,
                 "is_peak": true, "in_window": true } ],
    "ranked_hours": [ { "hour": 12, "avg_people": 702.35 } ],
    "slots": [ { "time_slot": "lunch", "slot_name": "午餐", "days": 75, "samples": 75,
                 "people_total": 131378, "avg_people_per_day": 1751.71, "share": 1.0,
                 "avg_wait_minutes": 7.83, "is_peak": true } ],
    "explanation": { "source": "ai", "error": null, "…": "…" }
  }
}
```

- `hours[].avg_people` 是**每小时**的平均客流；`slots[].avg_people_per_day` 是**每天**该时段
  的合计平均（时段是一天里的一个区间，两者口径不同，故意用不同字段名）。

## 2.3 GET /api/canteens/forecast

未来午餐人流预测。预测方法**只有一种**，保持简单：

```
预测值 = 历史同期平均值 + 星期修正 + 时间段修正
B       = 训练窗口内「同一日期 + 同一小时」客流的算术平均
ΔW(w)   = 星期 w 的历史平均 − B
ΔH(h)   = h 点的历史平均 − B
预测(t,h) = max(0, B + ΔW(weekday(t)) + ΔH(h))
```

不训练任何模型、不引入机器学习库（无神经网络 / XGBoost / Transformer），只有均值与加减法。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `date_from` / `date_to` | `YYYY-MM-DD` | - | 训练窗口 |
| `canteen_id` | int ≥ 1 | - | 指定食堂 |
| `time_slot` | breakfast/lunch/dinner/other | lunch | 训练与预测的时段 |
| `target_date` | `YYYY-MM-DD` | 历史最后一天之后 | 预测起始日 |
| `horizon_days` | 1..30 | 3 | 预测几天 |
| `explain` | 0/1 | 1 | 是否调用 AI 解释结果 |

```json
{
  "data": {
    "method": "历史同期平均值 + 星期修正 + 时间段修正",
    "training": { "time_slot": "lunch", "records": 225, "days": 75, "cells": 225,
                  "date_start": "2025-09-01", "date_end": "2025-11-14",
                  "base_avg_people": 583.9022, "hours": [11, 12, 13],
                  "by_weekday": [ { "weekday": 1, "weekday_name": "周一", "days": 11, "samples": 33,
                                    "avg_people": 589.9394, "adjustment": 6.0372 } ],
                  "by_hour": [ { "hour": 12, "samples": 75, "avg_people": 702.3467,
                                 "adjustment": 118.4444 } ] },
    "predictions": [ { "date": "2025-11-15", "weekday": 6, "weekday_name": "周六",
                       "predicted_people_total": 1663,
                       "hours": [ { "hour": 12, "base": 583.9022, "weekday_adjustment": -29.5022,
                                    "hour_adjustment": 118.4444, "predicted_people": 673 } ] } ],
    "notes": [],
    "explanation": { "source": "ai", "error": null, "…": "…" }
  }
}
```

- 每个小时的 `base` / `weekday_adjustment` / `hour_adjustment` 都返回，可原地复算，没有黑箱。
- `predicted_people_total` = 各小时 `predicted_people`（`floor(x+0.5)` 取整）之和。
- 该时段没有历史记录时返回 `predictions: []` + `notes` 说明，而不是硬凑数字。
- 说明：`/api/canteens/stats` 的 `avg_people` 是「每条记录」平均，本节口径是「客流单元」，
  两者在指定单个食堂时数值相同；不指定食堂时本节是全校合计。

错误：`400`（日期/时段/`horizon_days`/`peak_ratio` 非法）、`404`（`canteen_id` 不存在，附 `available` 食堂名）。

## 3. GET /api/industry/skills

企业技能需求统计，可按行业 / 城市 / 专业 / 公司任选分组维度。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `dimension` | industry/city/major/company | industry | 分组维度 |
| `industry` / `city` / `major` | text | - | 过滤条件（可组合） |
| `top` | 1..100 | 10 | 每个分组返回几个 Top 技能 |

响应要点（`groups[]` 按 `job_count` 降序；`top_skills[]` 按需要该技能的岗位数降序）：

```json
{
  "data": {
    "summary": { "groups": 8, "job_count": 100, "skill_count": 27 },
    "groups": [
      { "name": "软件开发", "job_count": 25, "skill_count": 14,
        "salary_min": 5000, "salary_max": 23500, "avg_salary_min": 10760.0, "avg_salary_max": 15320.0,
        "top_skills": [ { "skill_id": 17, "skill_name": "Git", "skill_category": "工具",
                          "job_count": 13, "required_jobs": 13, "avg_importance": 3.69 } ] }
    ]
  }
}
```

错误：`400`（`dimension` 不在白名单、`top` 越界/非整数）。

## 4. GET /api/industry/gap

专业课程技能 vs 企业岗位技能的**覆盖率与缺口**分析（集合运算 + 重要度加权）。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `major` | text | **必填** | 专业名，对应 `courses.department` / `jobs.major` |
| `industry` | text | - | 只统计该行业的对口岗位 |
| `min_importance` | 1..5 | 1 | 只看重要度 ≥ 该值的技能 |
| `top` | 1..100 | 20 | 每组技能返回条数 |

口径：

- 覆盖技能 = 岗位要求技能 ∩ 该专业课程覆盖技能（带 `course_level`：了解/掌握/熟练）
- 缺口技能 = 岗位要求技能 − 该专业课程覆盖技能
- `coverage_rate` = 覆盖数 / 要求技能总数
- `weighted_coverage_rate` = Σ 覆盖技能的 `importance` / Σ 全部要求技能的 `importance`
- `required_missing_skills` = 缺口里 `is_required = true` 的（必须补齐）

```json
{
  "data": {
    "summary": { "major": "软件工程", "job_count": 17, "industry_count": 3,
                 "course_skill_count": 12, "required_skill_count": 17, "covered_skill_count": 10,
                 "missing_skill_count": 7, "required_missing_count": 7,
                 "coverage_rate": 0.5882, "weighted_coverage_rate": 0.6081, "course_covered_ratio": 0.871 },
    "covered_skills":        [ { "skill_id": 17, "skill_name": "Git", "skill_category": "工具", "job_count": 8,
                                 "importance": 5, "avg_importance": 3.75, "is_required": true, "course_level": "掌握" } ],
    "missing_skills":        [ { "skill_id": 23, "skill_name": "沟通表达", "skill_category": "软技能", "job_count": 5,
                                 "importance": 5, "avg_importance": 4.8, "is_required": true } ],
    "required_missing_skills": [ "…missing_skills 中 is_required = true 的子集…" ]
  }
}
```

错误：`400`（缺 `major`、`min_importance`/`top` 越界）、`404`（专业不存在，附 `available` 专业清单）。
返回的 `covered_skills` / `missing_skills` 受 `top` 截断，但 `summary` 里的计数始终是全量。

## 测试

```bash
pytest tests -q          # tests/test_api.py 覆盖四个接口 + 业务层 + 参数化查询 + 错误码
                         # tests/test_classroom.py 覆盖 AI 找教室链路（离线，不联网）
                         # tests/test_forecast.py 覆盖食堂趋势 / 高峰 / 预测与 AI 解释边界（离线，不联网）
```

测试会把 `data/campus.db` 复制到临时目录后只读使用，不会修改仓库里的数据库，也不会触碰 `frontend/`。
`tests/test_forecast.py` 另建一个**值完全可控的合成库**，用来对预测公式做精确断言。
