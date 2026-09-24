# CampusAI 基础 API（Flask + SQLite）

- 代码位置：`backend/`（分层见下）
- 数据库：`data/campus.db`（由 `python scripts/import_data.py` 生成，**只读查询**）
- 本阶段**不接 AI**：所有接口都是确定性的 SQL 查询 + Python 计算，没有 LLM 参与
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
| `backend/validators.py` | 参数校验（字符串 → 安全值，非法即 400） |
| `backend/db.py` | 连接管理与参数化查询工具 |
| `backend/errors.py` | `ValidationError(400)` / `NotFoundError(404)` / `DatabaseUnavailableError(503)` |

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
```

测试会把 `data/campus.db` 复制到临时目录后只读使用，不会修改仓库里的数据库，也不会触碰 `frontend/`。
