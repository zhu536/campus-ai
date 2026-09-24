# -*- coding: utf-8 -*-
"""Flask 路由层。

只做三件事：解析查询参数 → 调用 ``queries`` 里的业务函数 → 返回 JSON。
不含 SQL，也不做业务判断（AGENTS.md：业务查询函数和 Flask 路由分离）。
"""

from flask import Blueprint, current_app, jsonify, request

from . import queries
from .db import connection
from .validators import (
    DIMENSIONS,
    ROOM_TYPES,
    TIME_SLOTS,
    bool_param,
    choice_param,
    date_range,
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
        "semester": text_param(request.args, "semester", max_length=30),
        # 本接口只回答「哪些教室现在可用」，因此状态固定按 available 过滤。
        "status": "available",
        "limit": int_param(request.args, "limit", default=10, minimum=1, maximum=100),
    }


def _gap_params():
    return {
        "major": text_param(request.args, "major", max_length=100),
        "industry": text_param(request.args, "industry", max_length=50),
        "min_importance": int_param(request.args, "min_importance", default=1, minimum=1, maximum=5),
        "top": int_param(request.args, "top", default=20, minimum=1, maximum=100),
    }


@api.get("/classrooms/available")
def classrooms_available():
    """找空闲教室。

    查询参数：capacity, has_projector(0/1), has_computer(0/1), room_type, building,
    weekday(1-7), period_start(1-12), period_end(1-12), semester, limit(1-100)
    """
    return _ok({"data": _run(queries.list_available_classrooms, _classroom_params())})


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

    major 为空时由业务层报错（400 + field=major），路由层不自己拼错误信息。
    """
    return _ok({"data": _run(queries.industry_skill_gap, _gap_params())})
