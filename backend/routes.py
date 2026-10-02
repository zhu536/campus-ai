# -*- coding: utf-8 -*-
"""Flask 路由层。

只做三件事：解析查询参数 → 调用 ``queries`` 里的业务函数 → 返回 JSON。
不含 SQL，也不做业务判断（AGENTS.md：业务查询函数和 Flask 路由分离）。
"""

from flask import Blueprint, current_app, jsonify, request

from . import classroom, forecast, industry, queries
from .db import connection
from .errors import ValidationError
from .validate import check_semester
from .validators import (
    DIMENSIONS,
    ROOM_TYPES,
    TIME_SLOTS,
    bool_param,
    choice_param,
    date_param,
    date_range,
    float_param,
    int_param,
    period_range,
    text_param,
)

api = Blueprint("api", __name__)


def _ok(payload, status=200):
    """统一成功响应外壳：``{"status": "ok", ...}``。"""
    body = {"status": "ok"}
    body.update(payload)
    return jsonify(body), status


def _run(business_function, params):
    """打开连接、调用业务函数；连接一定关闭，异常交给统一错误处理器。"""
    db_path = current_app.config["DATABASE"]
    with connection(db_path) as conn:
        return business_function(conn, params)


def _semester_param():
    """学期参数：空串按「没传」处理；有值时规范化并校验形状。

    与 AI 路径（``backend/validate.check_semester``）共用同一套规则，避免「同一参数、
    两条入口、两种行为」：``2025-2026-1学期`` 在两边都会被收敛成 ``2025-2026-1``，
    形状不对的取值在解析层就 400，而不是拿它去查库报 404。
    """
    raw = request.args.get("semester")
    if raw is None or not raw.strip():
        return None
    return check_semester(raw, "semester")


def _classroom_params():
    period_start, period_end = period_range(request.args)
    return {
        "capacity": int_param(request.args, "capacity", minimum=1, maximum=100000),
        "has_projector": bool_param(request.args, "has_projector"),
        "has_computer": bool_param(request.args, "has_computer"),
        "room_type": choice_param(request.args, "room_type", ROOM_TYPES),
        "building": text_param(request.args, "building", max_length=50),
        "weekday": int_param(request.args, "weekday", minimum=1, maximum=7),
        "period_start": period_start,
        "period_end": period_end,
        "semester": _semester_param(),
        # 本接口只回答「哪些教室现在可用」，因此状态固定按 available 过滤。
        "status": "available",
        "limit": int_param(request.args, "limit", default=10, minimum=1, maximum=100),
    }


def _json_body():
    """读取 JSON 请求体；没有请求体返回空 dict，类型不对直接 400。"""
    body = request.get_json(silent=True)
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise ValidationError('请求体必须是 JSON 对象，例如 {"question": "..."}', field="question")
    return body


def _question_input(body):
    """取用户问题：POST 用请求体 ``question``，GET 用查询串 ``question``。"""
    raw = body.get("question", request.args.get("question"))
    if raw is None:
        raise ValidationError(
            "缺少必填参数 question（用户问题，例如「周三下午帮我找一个8人、有投影的空教室」）",
            field="question",
        )
    if not isinstance(raw, str):
        raise ValidationError("参数 question 必须是字符串，实际为 {!r}".format(raw), field="question")
    value = raw.strip()
    if not value:
        raise ValidationError("参数 question 不能为空", field="question")
    if len(value) > classroom.MAX_QUESTION_LENGTH:
        raise ValidationError(
            "参数 question 最长 {} 个字符，实际 {}".format(
                classroom.MAX_QUESTION_LENGTH, len(value)
            ),
            field="question",
        )
    return value


def _explain_flag(body):
    """是否调用 AI 解释结果（默认开启；``explain=0`` 时只用 Python 兜底文案）。"""
    raw = body.get("explain", request.args.get("explain"))
    if raw is None:
        return True
    return bool(bool_param({"explain": str(raw)}, "explain", default=1))


def _gap_params():
    return {
        "major": _major_param(),
        "industry": text_param(request.args, "industry", max_length=50),
        "min_importance": int_param(request.args, "min_importance", default=1, minimum=1, maximum=5),
        "top": int_param(request.args, "top", default=20, minimum=1, maximum=100),
    }


#: major 的等价参数名：同一个需求换个名字（profession / department / skill）不该直接 400。
MAJOR_PARAM_NAMES = ("major", "major_name", "profession", "department", "skill")


def _major_param():
    """取专业名：按 :data:`MAJOR_PARAM_NAMES` 依次尝试，都为空返回 ``None``。"""
    for name in MAJOR_PARAM_NAMES:
        value = text_param(request.args, name, max_length=100)
        if value:
            return value
    return None


def _available_majors():
    """错误提示里的可选专业清单（只读）。

    查库失败时返回空清单：**提示信息本身不应该再失败一次**（数据库不可用时另有 503）。
    """
    try:
        with connection(current_app.config["DATABASE"]) as conn:
            return queries.available_majors(conn)
    except Exception:  # pragma: no cover - 仅在数据库不可用时走到
        return []


def _require_major(params):
    """必填校验：缺 major 时返回带「可选专业清单」的 400，而不是让人再撞一次 404。

    只补错误信息与参数别名，不改变业务口径：专业是否存在仍由业务层判断（404）。
    """
    if not params.get("major"):
        raise ValidationError(
            "缺少必填参数 major（专业名，例如「软件工程」）",
            field="major",
            available=_available_majors(),
        )
    return params


def _forecast_params():
    """食堂趋势 / 高峰 / 预测的查询参数（字段与 backend/forecast 的校验层一一对应）。"""
    date_from, date_to = date_range(request.args, "date_from", "date_to")
    return {
        "canteen_id": int_param(request.args, "canteen_id", minimum=1),
        "date_from": date_from,
        "date_to": date_to,
        "time_slot": choice_param(request.args, "time_slot", TIME_SLOTS),
        "target_date": date_param(request.args, "target_date"),
        "horizon_days": int_param(
            request.args,
            "horizon_days",
            default=forecast.DEFAULT_HORIZON_DAYS,
            minimum=1,
            maximum=forecast.MAX_HORIZON_DAYS,
        ),
        "peak_ratio": float_param(
            request.args,
            "peak_ratio",
            default=forecast.PEAK_RATIO,
            minimum=forecast.MIN_PEAK_RATIO,
            maximum=forecast.MAX_PEAK_RATIO,
        ),
    }


def _canteen_section(section):
    """跑一张食堂报表：Python 统计/预测 → 连接关闭 → AI 只解释结果（见 backend/forecast.py）。

    响应里除了 ``data`` 本身，还把底层报表放在 ``data.report``、并按需把
    ``data.series``（趋势接口的按天明细）平铺到顶层：``report.trend.series``
    本来就算好了，但只暴露 ``trend`` 片段时前端拿不到逐日客流，无法按任意日期
    窗口重绘趋势图。这里只做「搬运」，不做任何额外计算或口径变更。
    """
    report = forecast.analyze_canteen(
        _forecast_params(),
        db_path=current_app.config["DATABASE"],
        explain=_explain_flag({}),
        sections=(section,),
    )
    payload = dict(report)
    payload["report"] = report
    payload["series"] = (report.get("trend") or {}).get("series", [])
    return _ok({"data": payload})


@api.get("/classrooms/available")
def classrooms_available():
    """找空闲教室。

    查询参数：capacity, has_projector(0/1), has_computer(0/1), room_type, building,
    weekday(1-7), period_start(1-12), period_end(1-12), semester, limit(1-100)
    """
    return _ok({"data": _run(queries.list_available_classrooms, _classroom_params())})


@api.route("/classrooms/search", methods=["GET", "POST"])
def classrooms_search():
    """AI 智能找教室：自然语言问题 → 候选空教室 + AI 解释。

    请求参数（POST JSON 请求体 / GET 查询串）：

    - ``question``（必填）用户问题，例如「周三下午帮我找一个8人、有投影的空教室」
    - ``explain``（0/1，默认 1）是否调用 AI 解释结果

    链路：Intent Parser（只识别意图+抽参数）→ 参数校验 → classroom 业务函数 →
    SQLite（queries.list_available_classrooms）→ AI 只解释查询结果。
    本路由不含 SQL，也不让 AI 接触数据库（详见 backend/classroom.py 的模块说明）。
    """
    body = _json_body()
    data = classroom.search_classrooms(
        _question_input(body),
        db_path=current_app.config["DATABASE"],
        explain=_explain_flag(body),
    )
    return _ok({"data": data})


@api.get("/canteens/stats")
def canteens_stats():
    """食堂人流 / 等待时间 / 拥挤度统计。

    查询参数：date_from, date_to (YYYY-MM-DD), canteen_id, time_slot, top_slots(0-4)
    """
    date_from, date_to = date_range(request.args, "date_from", "date_to")
    params = {
        "date_from": date_from,
        "date_to": date_to,
        "canteen_id": int_param(request.args, "canteen_id", minimum=1),
        "time_slot": choice_param(request.args, "time_slot", TIME_SLOTS),
        "top_slots": int_param(
            request.args,
            "top_slots",
            default=queries.DEFAULT_TOP_SLOTS,
            minimum=0,
            maximum=queries.MAX_TOP_SLOTS,
        ),
    }
    return _ok({"data": _run(queries.canteen_stats, params)})


@api.get("/canteens/trend")
def canteens_trend():
    """历史就餐趋势（按天 / 按星期几）。

    查询参数：date_from, date_to (YYYY-MM-DD), canteen_id, time_slot, explain(0/1)

    统计与趋势判定全部在 Python（见 backend/forecast.py），AI 只解释已算好的结果。
    """
    return _canteen_section("trend")


@api.get("/canteens/peak")
def canteens_peak():
    """高峰时间段：逐小时人流、峰值小时、高峰区间与早/午/晚餐对比。

    查询参数：date_from, date_to (YYYY-MM-DD), canteen_id, time_slot,
    peak_ratio(0.1-1.0，默认 0.8), explain(0/1)
    """
    return _canteen_section("peak")


@api.get("/canteens/forecast")
def canteens_forecast():
    """未来午餐人流预测，方法固定为「历史同期平均值 + 星期修正 + 时间段修正」。

    查询参数：date_from, date_to (YYYY-MM-DD), canteen_id, time_slot(默认午餐),
    target_date (YYYY-MM-DD), horizon_days(1-30，默认 3), explain(0/1)

    预测由 Python 用均值与加减法完成（不训练任何模型），AI 只解释结果。
    """
    return _canteen_section("forecast")


@api.get("/meta/semesters")
def meta_semesters():
    """元信息：库里真实有排课记录的学期清单（只读，供前端下拉框）。

    响应：``{"status": "ok", "data": {"semesters": ["2025-2026-1"], "count": 1, "default": "2025-2026-1"}}``

    前端用它渲染「学期」下拉框，并据此判断某个学期是否有数据：
    请求没有数据 → 由前端走「保持上一份快照」的降级分支，后端不会返回假数据。
    """
    data = _run(queries.list_semesters, {})
    return _ok({"data": data})


@api.get("/industry/skills")
def industry_skills():
    """行业（或城市/专业/公司）技能需求统计。

    查询参数：dimension(industry|city|major|company), industry, city, major, top(1-100)
    """
    params = {
        "dimension": choice_param(request.args, "dimension", DIMENSIONS, default="industry"),
        "industry": text_param(request.args, "industry", max_length=50),
        "city": text_param(request.args, "city", max_length=50),
        "major": text_param(request.args, "major", max_length=100),
        "top": int_param(request.args, "top", default=10, minimum=1, maximum=100),
    }
    return _ok({"data": _run(queries.industry_skills, params)})


@api.get("/industry/gap")
def industry_gap():
    """专业课程技能与企业岗位技能的覆盖 / 缺口分析。

    查询参数：major(必填), industry, min_importance(1-5), top(1-100)

    major 为空时由这一层报错（400 + field=major + available 可选清单），路由层不自己拼业务口径。
    """
    return _ok({"data": _run(queries.industry_skill_gap, _require_major(_gap_params()))})


@api.get("/industry/match")
def industry_match():
    """产教融合技能分析：岗位技能需求 ↔ 专业课程技能覆盖（Python 匹配 + AI 只解释）。

    查询参数：major(必填), industry, min_importance(1-5), top(1-100), question, explain(0/1)

    与 ``/api/industry/gap`` 的区别：本接口走 ``backend/industry.py`` 的完整链路 ——
    SQLite 只取事实 → **Python 完成集合运算与覆盖率**（match_job_to_major /
    calculate_skill_gap）→ 连接关闭 → AI 只解释已经算好的结果，且不得改动计算结果。
    """
    data = industry.analyze_skill_gap(
        _require_major(_gap_params()),
        db_path=current_app.config["DATABASE"],
        question=request.args.get("question"),
        explain=_explain_flag({}),
    )
    return _ok({"data": data})
