# -*- coding: utf-8 -*-
"""CampusAI 业务查询层（**不导入 flask**）。

四大接口的业务实现在这里，路由层只调用这些函数：
    list_available_classrooms   GET /api/classrooms/available
    canteen_stats               GET /api/canteens/stats
    industry_skills             GET /api/industry/skills
    industry_skill_gap          GET /api/industry/gap

另外提供一个「数据底座」函数（不是接口，供 backend/forecast.py 做统计与预测）：
    canteen_meal_grid           食堂人流「日期 × 小时」明细

约定：
    1. 每个函数签名是 ``(conn, params: dict) -> dict``，``params`` 是**已经校验过**的值，
       返回值直接就是 JSON 可序列化的 dict（数字都是 int/float，不含 sqlite3.Row）。
    2. 所有 SQL 的值通过 ``?`` 绑定；SQL 文本是常量，不拼接用户输入。
    3. 计算（覆盖率、拥挤度、排序）在 Python 里做，SQL 只做筛选与聚合
       （docs/DATABASE.md：比较、预测、等级判定都在 Python）。
    4. 不调用任何 AI 接口。
"""

from .db import WhereBuilder, query_all, query_one
from .errors import NotFoundError, ValidationError
from .validators import ROOM_TYPES

# 覆盖率使用的「覆盖程度」权重：越熟练，说明课程对该技能的支撑越强。
LEVEL_RANK = {"了解": 1, "掌握": 2, "熟练": 3}

# 「刚好够用」的容量优先：返回上限 = 人数需求 * 该系数（不足 50 时按 50 计）。
CAPACITY_HEADROOM = 1.5
MIN_CAPACITY_WINDOW = 50
DEFAULT_LIMIT = 10
DEFAULT_TOP_SLOTS = 3
MAX_TOP_SLOTS = 4  # time_slot 只有 breakfast/lunch/dinner/other 四种取值


def _round(value, digits=2):
    return None if value is None else round(float(value), digits)


def _distinct_values(conn, column, table):
    """取某列的去重取值（列名/表名由代码常量传入，不来自外部输入）。"""
    sql = "SELECT DISTINCT {} AS value FROM {} WHERE {} IS NOT NULL ORDER BY value".format(
        column, table, column
    )
    return [row["value"] for row in query_all(conn, sql)]


def available_majors(conn):
    """可分析的专业清单（调试/脚本可用；API 的 404 响应里也会带上）。"""
    return _distinct_values(conn, "department", "courses")


# ---------------------------------------------------------------------------
# 1. GET /api/classrooms/available —— 智能找教室
# ---------------------------------------------------------------------------

def list_available_classrooms(conn, params):
    """按「人数 + 设备 + 时段空闲」筛选教室。

    docs/DATABASE.md「教室查询」三步：
        候选集 = classrooms 里满足硬条件的教室；
        冲突集 = schedules 里与该时段区间重叠的教室（period_start <= end AND period_end >= start）；
        差集   = 候选集 - 冲突集，再按容量升序（刚好够用）、楼层升序排序。

    教室是否空闲不落库，全部在查询时算出，因此不会出现「排课更新了但空闲标记过期」。
    """
    capacity = params.get("capacity")
    has_projector = params.get("has_projector")
    has_computer = params.get("has_computer")
    room_type = params.get("room_type")
    building = params.get("building")
    status = params.get("status")
    weekday = params.get("weekday")
    period_start = params.get("period_start")
    period_end = params.get("period_end")
    semester = params.get("semester")
    limit = params.get("limit") or DEFAULT_LIMIT

    # 学期：必须先确认它真的有排课数据，否则「空闲」结论会把全校教室都算成空的。
    available_semesters = _distinct_values(conn, "semester", "schedules")
    if weekday is not None and semester is None:
        if len(available_semesters) == 1:
            semester = available_semesters[0]
        else:
            raise ValidationError(
                "指定 weekday 时必须同时提供 semester（可选：{}）".format(
                    "/".join(available_semesters) or "无排课数据"
                ),
                field="semester",
            )
    elif semester is not None and semester not in available_semesters:
        raise NotFoundError(
            "学期 {} 不存在".format(semester), field="semester", available=available_semesters
        )

    where = (
        WhereBuilder()
        .add("status", "=", status if status is not None else "available")
        .add("capacity", ">=", capacity)
        .add("room_type", "=", room_type)
        .add("building", "=", building)
        .add("has_projector", ">=", has_projector)
        .add("has_computer", ">=", has_computer)
    )

    candidates = query_all(
        conn,
        "SELECT room_id, room_name, building, floor, capacity, has_projector, has_computer,"
        " room_type, status FROM classrooms" + where.sql(),
        where.params,
    )

    occupied_ids = set()
    if weekday is not None:
        conflict_sql = (
            "SELECT DISTINCT room_id FROM schedules"
            " WHERE semester = ? AND weekday = ? AND period_start <= ? AND period_end >= ?"
        )
        # 区间重叠判断：已有排课的 [period_start, period_end] 与查询区间 [start, end]
        # 有交集（含端点）即冲突。单节课查询（start == end，或只传 period_start 时由
        # validators.period_range 补齐为同一节）由此退化为「该节次是否被占用」，
        # 无需单独分支：跨节排课（如 [1,4]）落在单节上同样会被判为冲突。
        occupied_ids = {
            row["room_id"]
            for row in query_all(
                conn, conflict_sql, (semester, weekday, period_end, period_start)
            )
        }

    rooms = []
    for row in candidates:
        if row["room_id"] in occupied_ids:
            continue
        room = {
            "room_id": row["room_id"],
            "room_name": row["room_name"],
            "building": row["building"],
            "floor": row["floor"],
            "capacity": row["capacity"],
            "has_projector": bool(row["has_projector"]),
            "has_computer": bool(row["has_computer"]),
            "room_type": row["room_type"],
            "status": row["status"],
        }
        # 排序键：优先「刚好够用」（超出量最小），超出量相同时按容量、楼层升序。
        if capacity is None:
            room["_capacity_gap"] = 0
        else:
            room["_capacity_gap"] = max(0, row["capacity"] - capacity)
        rooms.append(room)

    rooms.sort(key=lambda item: (item["_capacity_gap"], item["capacity"], item["floor"] or 0))

    capacity_window = None
    if capacity is not None:
        capacity_window = max(int(capacity * CAPACITY_HEADROOM), MIN_CAPACITY_WINDOW)

    return {
        "query": {
            "semester": semester,
            "weekday": weekday,
            "period_start": period_start,
            "period_end": period_end,
            "capacity": capacity,
            "has_projector": None if has_projector is None else bool(has_projector),
            "has_computer": None if has_computer is None else bool(has_computer),
            "room_type": room_type,
            "building": building,
            "status": status if status is not None else "available",
            "limit": limit,
        },
        "total": len(rooms),
        "summary": {
            "candidate_count": len(candidates),
            "occupied_count": len(occupied_ids),
            "available_count": len(rooms),
            "capacity_window": capacity_window,
            "room_types": list(ROOM_TYPES),
        },
        "rooms": [
            {key: value for key, value in room.items() if not key.startswith("_")}
            for room in rooms[:limit]
        ],
    }


# ---------------------------------------------------------------------------
# 2. GET /api/canteens/stats —— 食堂拥挤度与等待时间统计
# ---------------------------------------------------------------------------

def canteen_stats(conn, params):
    """按食堂汇总人流、等待时间、拥挤度、高峰小时与高峰时段。

    拥挤度 = AVG(people_count) / capacity，归一化到 0..1（>1 说明超过同时在店容量，
    保守截断为 1.0，并保留 raw 值供解释「超载多少」）。
    历史数据只聚合、不外推：本阶段不接 AI，也不做预测。
    """
    date_from = params.get("date_from")
    date_to = params.get("date_to")
    canteen_id = params.get("canteen_id")
    time_slot = params.get("time_slot")
    top_slots = params.get("top_slots")
    if top_slots is None:
        top_slots = DEFAULT_TOP_SLOTS

    # COALESCE：优先用记录里的容量快照，缺失时退回 canteens.capacity。
    where = (
        WhereBuilder()
        .add("m.canteen_id", "=", canteen_id)
        .add("m.record_date", ">=", date_from)
        .add("m.record_date", "<=", date_to)
        .add("m.time_slot", "=", time_slot)
    )
    from_clause = " FROM meal_records AS m JOIN canteens AS c ON c.canteen_id = m.canteen_id"

    summary_row = query_one(
        conn,
        "SELECT COUNT(*) AS records, COUNT(DISTINCT m.canteen_id) AS canteens,"
        " SUM(m.people_count) AS people, AVG(m.wait_minutes) AS avg_wait,"
        " MIN(m.record_date) AS date_start, MAX(m.record_date) AS date_end"
        + from_clause
        + where.sql(),
        where.params,
    )
    records = summary_row["records"] or 0
    people = summary_row["people"] or 0

    items = []
    if records:
        overall_sql = (
            "SELECT m.canteen_id, c.canteen_name, c.location, c.status,"
            " c.capacity AS seat_capacity, c.window_count, c.open_time, c.close_time,"
            " COUNT(*) AS records,"
            " COUNT(DISTINCT m.record_date) AS record_days,"
            " SUM(m.people_count) AS people_total,"
            " AVG(m.people_count) AS avg_people,"
            " MAX(m.people_count) AS max_people,"
            " AVG(m.wait_minutes) AS avg_wait,"
            " MAX(m.wait_minutes) AS max_wait,"
            " AVG(COALESCE(m.capacity, c.capacity)) AS avg_capacity"
            + from_clause
            + where.sql()
            + " GROUP BY m.canteen_id, c.canteen_name, c.location, c.status,"
            " c.capacity, c.window_count, c.open_time, c.close_time"
            " ORDER BY avg_people DESC, m.canteen_id ASC"
        )
        overall_rows = query_all(conn, overall_sql, where.params)

        hour_sql = (
            "SELECT m.canteen_id, m.record_hour, AVG(m.people_count) AS avg_people,"
            " SUM(m.people_count) AS people_total, AVG(m.wait_minutes) AS avg_wait"
            + from_clause
            + where.sql()
            + " GROUP BY m.canteen_id, m.record_hour ORDER BY m.canteen_id, avg_people DESC"
        )
        slot_sql = (
            "SELECT m.canteen_id, m.time_slot, AVG(m.people_count) AS avg_people,"
            " SUM(m.people_count) AS people_total, AVG(m.wait_minutes) AS avg_wait"
            + from_clause
            + where.sql()
            + " GROUP BY m.canteen_id, m.time_slot ORDER BY m.canteen_id, avg_people DESC"
        )
        hours_by_canteen = {}
        for row in query_all(conn, hour_sql, where.params):
            hours_by_canteen.setdefault(row["canteen_id"], []).append(row)
        slots_by_canteen = {}
        for row in query_all(conn, slot_sql, where.params):
            slots_by_canteen.setdefault(row["canteen_id"], []).append(row)

        for row in overall_rows:
            avg_capacity = row["avg_capacity"]
            avg_people = row["avg_people"]
            congestion = None
            congestion_raw = None
            if avg_capacity:
                congestion_raw = _round(avg_people / avg_capacity, 4)
                congestion = min(1.0, congestion_raw)

            hours = hours_by_canteen.get(row["canteen_id"], [])
            slots = slots_by_canteen.get(row["canteen_id"], [])
            peak_hour = None
            if hours:
                peak = hours[0]
                peak_hour = {
                    "hour": peak["record_hour"],
                    "avg_people": _round(peak["avg_people"]),
                    "avg_wait_minutes": _round(peak["avg_wait"]),
                }
            peak_slots = [
                {
                    "time_slot": slot["time_slot"],
                    "avg_people": _round(slot["avg_people"]),
                    "avg_wait_minutes": _round(slot["avg_wait"]),
                    "people_total": int(slot["people_total"] or 0),
                }
                for slot in slots[:top_slots]
            ]

            items.append(
                {
                    "canteen_id": row["canteen_id"],
                    "canteen_name": row["canteen_name"],
                    "location": row["location"],
                    "status": row["status"],
                    "seat_capacity": row["seat_capacity"],
                    "window_count": row["window_count"],
                    "open_time": row["open_time"],
                    "close_time": row["close_time"],
                    "records": row["records"],
                    "record_days": row["record_days"],
                    "people_total": int(row["people_total"] or 0),
                    "avg_people": _round(avg_people),
                    "max_people": row["max_people"],
                    "avg_wait_minutes": _round(row["avg_wait"]),
                    "max_wait_minutes": _round(row["max_wait"]),
                    "avg_capacity": _round(avg_capacity),
                    "congestion": _round(congestion, 4),
                    "congestion_raw": congestion_raw,
                    "peak_hour": peak_hour,
                    "peak_slots": peak_slots,
                }
            )

    return {
        "query": {
            "date_from": date_from,
            "date_to": date_to,
            "canteen_id": canteen_id,
            "time_slot": time_slot,
            "top_slots": top_slots,
        },
        "total": len(items),
        "summary": {
            "canteens": len(items),
            "records": records,
            "people_total": int(people),
            "avg_wait_minutes": _round(summary_row["avg_wait"]) if records else None,
            "date_start": summary_row["date_start"],
            "date_end": summary_row["date_end"],
        },
        "canteens": items,
    }


# ---------------------------------------------------------------------------
# 2b. 食堂趋势 / 高峰 / 预测的数据底座（供 backend/forecast.py 使用）
# ---------------------------------------------------------------------------

def canteen_meal_grid(conn, params):
    """取食堂人流「日期 × 小时」明细网格，供 ``backend/forecast.py`` 做统计与预测。

    与 :func:`canteen_stats` 的分工：

        - ``canteen_stats`` 给**人看的报表**：SQL 聚合好的人流/等待/拥挤度/高峰。
        - ``canteen_meal_grid`` 给**算法用的底座**：只按条件筛选并回填容量，
          均值、修正系数、高峰判定、预测全部在 Python 算
          （AGENTS.md AI Rules：数据统计与预测归 Python，SQLite 只提供事实）。

    为什么不做 GROUP BY：``meal_records`` 上有
    ``UNIQUE(canteen_id, record_date, record_hour)``，同一食堂同一天同一小时只有
    一条记录，聚合只会得到同样的行。把粒度留在「日期 × 小时」上，同一份网格就能
    同时服务趋势、高峰、预测三张报表 —— 只读一次库，三个口径天然一致。

    ``time_slot`` 也照常在这里生效（调用方 ``forecast`` 故意不传，改在 Python 侧
    按报表需要过滤，这样一次读取能同时支撑「全部时段」的趋势/高峰与「午餐」的预测）。

    容量优先取 ``meal_records.capacity`` 快照，缺失时回退 ``canteens.capacity``
    （``COALESCE``，与 :func:`canteen_stats` 同一口径）。
    """
    canteen_id = params.get("canteen_id")
    date_from = params.get("date_from")
    date_to = params.get("date_to")
    time_slot = params.get("time_slot")

    # 食堂编号先做存在性校验，避免「写错编号 → 空结果」被误读成「这段时间没人吃饭」。
    if canteen_id is not None and canteen_id not in set(
        _distinct_values(conn, "canteen_id", "canteens")
    ):
        raise NotFoundError(
            "食堂 {} 不存在".format(canteen_id),
            field="canteen_id",
            available=_distinct_values(conn, "canteen_name", "canteens"),
        )

    where = (
        WhereBuilder()
        .add("m.canteen_id", "=", canteen_id)
        .add("m.record_date", ">=", date_from)
        .add("m.record_date", "<=", date_to)
        .add("m.time_slot", "=", time_slot)
    )
    rows = query_all(
        conn,
        "SELECT m.canteen_id, c.canteen_name, m.record_date, m.record_hour, m.time_slot,"
        " m.people_count, m.wait_minutes, COALESCE(m.capacity, c.capacity) AS capacity"
        " FROM meal_records AS m JOIN canteens AS c ON c.canteen_id = m.canteen_id"
        + where.sql()
        + " ORDER BY m.record_date ASC, m.record_hour ASC, m.canteen_id ASC",
        where.params,
    )

    records = [
        {
            "canteen_id": row["canteen_id"],
            "canteen_name": row["canteen_name"],
            "record_date": row["record_date"],
            "record_hour": row["record_hour"],
            "time_slot": row["time_slot"],
            "people_count": row["people_count"],
            "wait_minutes": _round(row["wait_minutes"]),
            "capacity": row["capacity"],
        }
        for row in rows
    ]

    seen = {}
    for record in records:
        seen.setdefault(record["canteen_id"], record["canteen_name"])

    return {
        "query": {
            "canteen_id": canteen_id,
            "date_from": date_from,
            "date_to": date_to,
            "time_slot": time_slot,
        },
        "total": len(records),
        "summary": {
            "records": len(records),
            "canteens": len(seen),
            "date_start": min((item["record_date"] for item in records), default=None),
            "date_end": max((item["record_date"] for item in records), default=None),
            "people_total": sum(item["people_count"] for item in records),
        },
        "canteens": [
            {"canteen_id": key, "canteen_name": seen[key]} for key in sorted(seen)
        ],
        "records": records,
    }


# ---------------------------------------------------------------------------
# 3. GET /api/industry/skills —— 行业/城市/专业的技能需求统计
# ---------------------------------------------------------------------------

def industry_skills(conn, params):
    """统计「哪些技能最被企业需要」，可按行业 / 城市 / 专业 / 公司任选维度分组。

    返回每个分组的岗位数、薪资区间、技能条数与 Top 技能（按需要该技能的岗位数排序，
    同分时按平均重要度、技能名称排序，保证结果稳定可复现）。
    """
    dimension = params.get("dimension") or "industry"
    industry = params.get("industry")
    city = params.get("city")
    major = params.get("major")
    top = params.get("top") or 10

    # 维度列名来自白名单（validators.DIMENSIONS），不是用户原始字符串。
    dimension_column = "j.{}".format(dimension)

    where = (
        WhereBuilder()
        .add("j.industry", "=", industry)
        .add("j.city", "=", city)
        .add("j.major", "=", major)
    )

    group_rows = query_all(
        conn,
        "SELECT {column} AS name, COUNT(DISTINCT j.job_id) AS job_count,"
        " MIN(j.salary_min) AS salary_min, MAX(j.salary_max) AS salary_max,"
        " AVG(j.salary_min) AS avg_salary_min, AVG(j.salary_max) AS avg_salary_max"
        " FROM jobs AS j".format(column=dimension_column)
        + where.sql()
        + " GROUP BY {column} ORDER BY job_count DESC, name ASC".format(column=dimension_column),
        where.params,
    )

    skill_rows = query_all(
        conn,
        "SELECT {column} AS name, s.skill_id, s.skill_name, s.skill_category,"
        " COUNT(DISTINCT j.job_id) AS job_count,"
        " AVG(js.importance) AS avg_importance,"
        " SUM(js.is_required) AS required_jobs"
        " FROM jobs AS j"
        " JOIN job_skills AS js ON js.job_id = j.job_id"
        " JOIN skills AS s ON s.skill_id = js.skill_id".format(column=dimension_column)
        + where.sql()
        + " GROUP BY {column}, s.skill_id, s.skill_name, s.skill_category"
        " ORDER BY {column} ASC, job_count DESC, avg_importance DESC, s.skill_name ASC".format(
            column=dimension_column
        ),
        where.params,
    )

    skills_by_group = {}
    for row in skill_rows:
        skills_by_group.setdefault(row["name"], []).append(
            {
                "skill_id": row["skill_id"],
                "skill_name": row["skill_name"],
                "skill_category": row["skill_category"],
                "job_count": row["job_count"],
                "required_jobs": int(row["required_jobs"] or 0),
                "avg_importance": _round(row["avg_importance"]),
            }
        )

    groups = []
    for row in group_rows:
        top_skills = skills_by_group.get(row["name"], [])[:top]
        groups.append(
            {
                "name": row["name"],
                "job_count": row["job_count"],
                "skill_count": len(skills_by_group.get(row["name"], [])),
                "salary_min": row["salary_min"],
                "salary_max": row["salary_max"],
                "avg_salary_min": _round(row["avg_salary_min"], 1),
                "avg_salary_max": _round(row["avg_salary_max"], 1),
                "top_skills": top_skills,
            }
        )

    return {
        "query": {
            "dimension": dimension,
            "industry": industry,
            "city": city,
            "major": major,
            "top": top,
        },
        "total": len(groups),
        "summary": {
            "groups": len(groups),
            "job_count": sum(group["job_count"] for group in groups),
            "skill_count": len({row["skill_id"] for row in skill_rows}),
        },
        "groups": groups,
    }


# ---------------------------------------------------------------------------
# 4. GET /api/industry/gap —— 岗位技能 vs 专业课程技能 的缺口分析
# ---------------------------------------------------------------------------

def industry_skill_gap(conn, params):
    """计算某专业「企业要求的技能」与「课程覆盖的技能」之间的缺口。

    集合运算（docs/DATABASE.md 场景三）：
        覆盖技能 = 岗位要求的技能 ∩ 该专业课程覆盖的技能
        缺口技能 = 岗位要求的技能 − 该专业课程覆盖的技能
        覆盖率   = 覆盖技能数 / 岗位要求技能总数
        加权覆盖率 = Σ(已覆盖技能的权重) / Σ(要求技能的权重)（docs/API.md 同口径）

    权重取该技能在岗位要求里的**最大重要度** max(job_skills.importance)：一个技能对
    同一岗位只有一条 job_skills 记录（importance 是「岗位×技能」的属性），按技能聚合后
    用最大值代表「这个技能最多被要求到多重要」，因此这里的权重是数值而不是求和；
    越重要的技能缺失，对加权覆盖率的拉低越明显。min_importance 门槛过滤只影响参与
    统计与输出的技能，不影响「课程是否覆盖」的事实。

    硬性要求（is_required = 1）中未覆盖的单独列为必须补齐的缺口。
    """
    major = params.get("major")
    industry = params.get("industry")
    min_importance = params.get("min_importance")
    if min_importance is None:
        min_importance = 1
    top = params.get("top") or 20

    # 专业名先做存在性校验，避免「写错专业名 → 空结果」被误读成「没有缺口」。
    if not major:
        raise ValidationError("缺少必填参数 major", field="major")
    available = _distinct_values(conn, "department", "courses")
    if major not in available:
        raise NotFoundError(
            "专业 {} 不存在（或没有开课记录）".format(major),
            field="major",
            available=available,
        )

    # (a) 该专业课程覆盖的技能 + 覆盖程度
    course_rows = query_all(
        conn,
        "SELECT s.skill_id, s.skill_name, s.skill_category, cs.level"
        " FROM courses AS c"
        " JOIN course_skills AS cs ON cs.course_id = c.course_id"
        " JOIN skills AS s ON s.skill_id = cs.skill_id"
        " WHERE c.department = ?",
        (major,),
    )
    course_skill_level = {}
    for row in course_rows:
        rank = LEVEL_RANK.get(row["level"], 0)
        best = course_skill_level.get(row["skill_id"])
        if best is None or rank > best["rank"]:
            course_skill_level[row["skill_id"]] = {
                "rank": rank,
                "level": row["level"],
                "skill_name": row["skill_name"],
                "skill_category": row["skill_category"],
            }

    # (b) 对口岗位要求的技能（按技能聚合：多少岗位要它、重要度、是否硬性）
    required_where = (
        WhereBuilder().add("j.major", "=", major).add("j.industry", "=", industry)
    )
    required_rows = query_all(
        conn,
        "SELECT s.skill_id, s.skill_name, s.skill_category,"
        " COUNT(DISTINCT j.job_id) AS job_count,"
        " MAX(js.importance) AS max_importance,"
        " AVG(js.importance) AS avg_importance,"
        " MAX(js.is_required) AS required"
        " FROM jobs AS j"
        " JOIN job_skills AS js ON js.job_id = j.job_id"
        " JOIN skills AS s ON s.skill_id = js.skill_id"
        + required_where.sql()
        + " GROUP BY s.skill_id, s.skill_name, s.skill_category",
        required_where.params,
    )

    job_summary = query_one(
        conn,
        "SELECT COUNT(DISTINCT j.job_id) AS jobs,"
        " COUNT(DISTINCT j.industry) AS industries,"
        " MIN(j.salary_min) AS salary_min, MAX(j.salary_max) AS salary_max,"
        " AVG(j.salary_min) AS avg_salary_min, AVG(j.salary_max) AS avg_salary_max"
        " FROM jobs AS j"
        + required_where.sql(),
        required_where.params,
    )

    covered_skills = []
    missing_skills = []
    required_total_weight = 0
    covered_within_filter_weight = 0
    covered_all_weight = 0
    required_count = 0
    covered_count = 0

    for row in required_rows:
        skill_id = row["skill_id"]
        max_importance = row["max_importance"] or 0
        # 门槛过滤只影响输出与统计，不影响「课程是否覆盖」的事实。
        if max_importance < min_importance:
            continue
        required_count += 1
        required_total_weight += max_importance
        in_course = course_skill_level.get(skill_id)
        item = {
            "skill_id": skill_id,
            "skill_name": row["skill_name"],
            "skill_category": row["skill_category"],
            "job_count": row["job_count"],
            "importance": max_importance,
            "avg_importance": _round(row["avg_importance"]),
            "is_required": bool(row["required"]),
        }
        if in_course:
            covered_count += 1
            covered_within_filter_weight += max_importance
            covered_all_weight += in_course["rank"]
            item["course_level"] = in_course["level"]
            covered_skills.append(item)
        else:
            missing_skills.append(item)

    # 覆盖率按 1..3 的覆盖程度加权，更贴近「课程支撑得多扎实」。
    weighted_course_total = sum(entry["rank"] for entry in course_skill_level.values())
    coverage_rate = _round(covered_count / required_count, 4) if required_count else None
    if required_total_weight:
        weighted_coverage_rate = _round(covered_within_filter_weight / required_total_weight, 4)
    else:
        weighted_coverage_rate = None
    course_covered_ratio = (
        _round(covered_all_weight / weighted_course_total, 4) if weighted_course_total else None
    )

    missing_skills.sort(
        key=lambda item: (
            0 if item["is_required"] else 1,
            -item["importance"],
            -item["job_count"],
            item["skill_name"],
        )
    )
    covered_skills.sort(key=lambda item: (-item["importance"], -item["job_count"], item["skill_name"]))

    required_missing = [item for item in missing_skills if item["is_required"]]

    return {
        "query": {
            "major": major,
            "industry": industry,
            "min_importance": min_importance,
            "top": top,
        },
        "summary": {
            "major": major,
            "job_count": job_summary["jobs"] if job_summary else 0,
            "industry_count": job_summary["industries"] if job_summary else 0,
            "course_skill_count": len(course_skill_level),
            "required_skill_count": required_count,
            "covered_skill_count": covered_count,
            "missing_skill_count": len(missing_skills),
            "required_missing_count": len(required_missing),
            "coverage_rate": coverage_rate,
            "weighted_coverage_rate": weighted_coverage_rate,
            "course_covered_ratio": course_covered_ratio,
            "salary_min": job_summary["salary_min"] if job_summary else None,
            "salary_max": job_summary["salary_max"] if job_summary else None,
            "avg_salary_min": _round(job_summary["avg_salary_min"], 1) if job_summary else None,
            "avg_salary_max": _round(job_summary["avg_salary_max"], 1) if job_summary else None,
        },
        "covered_skills": covered_skills[:top],
        "missing_skills": missing_skills[:top],
        "required_missing_skills": required_missing[:top],
    }
