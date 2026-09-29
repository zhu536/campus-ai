# -*- coding: utf-8 -*-
"""食堂分析（场景二）：历史就餐趋势、高峰时间段、未来午餐人流预测。

链路与分工（AGENTS.md AI Rules）::

    AI 理解问题      backend/ai_parser.py + backend/validate.py
                     （意图 canteen_analysis / canteen_forecast 已在白名单里，
                       本模块不重复实现意图识别，只负责拿到参数之后的部分）
      → 参数校验      backend/validators.py（路由层）+ 本模块 validate_forecast_params（业务层）
      → 数据底座      backend/queries.canteen_meal_grid（SQLite 只做筛选，不写用户输入）
      → 统计与预测    **本模块**（趋势 / 高峰 / 预测，全部是均值与加减法）
      → AI 解释结果   prompts/canteen_insight_generator.md（只解释，不计算、不查询）

预测方法**只有一种**，保持简单（任务红线：禁止复杂机器学习 / 神经网络 / XGBoost / Transformer）::

    预测值 = 历史同期平均值 + 星期修正 + 时间段修正

    B          历史同期平均值 = 训练窗口内「同一日期 + 同一小时」客流的算术平均
    ΔW(w)      星期修正       = 星期 w 的历史平均 − B
    ΔH(h)      时间段修正     = h 点的历史平均 − B
    预测(t, h) = max(0, B + ΔW(weekday(t)) + ΔH(h))

没有任何模型训练、没有梯度下降、没有树、没有注意力；只有算术平均与减法。
预测值只做一次「非负截断」，人数按 ``floor(x + 0.5)`` 取整（不用 ``round``，避免
银行家舍入在 .5 上抖动）。

统一口径（三张报表共用同一份数据底座，避免口径漂移）::

    先把「同一日期 + 同一小时」的多条记录（多食堂）合计成一个**客流单元**，
    再对所有均值做统计。因此 ``avg_people`` 的含义是：
    指定食堂时 = 该食堂该小时的平均人流；不指定食堂时 = 全校该小时的平均总人流。

并行约束：本模块**不 import sqlite3、不写 SQL**，唯一取数入口是
``backend/queries.canteen_meal_grid``；模型只在连接关闭之后被调用，且只拿到
``build_insight_payload`` 生成的只读事实表（没有表名、没有 SQL、没有数据库路径）。

测试见 ``tests/test_forecast.py``。
"""

import datetime
import json
import math
import os
import re

import requests

from . import queries
from .ai_parser import (
    ERROR_NETWORK,
    AIParserError,
    extract_json_object,
    load_config,
    load_prompt,
    parse_response_text,
)
from .db import connection, default_db_path
from .errors import ValidationError
from .validate import check_date, check_int
from .validators import TIME_SLOTS

# backend/forecast.py → backend/ → 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_INSIGHT_PROMPT_PATH = os.path.join(
    PROJECT_ROOT, "prompts", "canteen_insight_generator.md"
)

#: 本模块唯一实现的预测方法（写进响应，方便前端直接展示，也方便测试锁定）。
FORECAST_METHOD = "历史同期平均值 + 星期修正 + 时间段修正"

WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
SLOT_NAMES = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐", "other": "其他"}
#: 时段展示顺序（与后端枚举一致，保证输出稳定）。
SLOT_ORDER = ("breakfast", "lunch", "dinner", "other")

#: 预测默认针对午餐（任务要求：预测未来午餐人流）。
LUNCH_SLOT = "lunch"
#: 默认预测未来 3 天，最多 30 天（与 backend/validate.py 的 horizon_days 上限一致）。
DEFAULT_HORIZON_DAYS = 3
MAX_HORIZON_DAYS = 30
#: 高峰时段边界：人流 >= 峰值 * ratio 的连续小时算作一个高峰时间段。
PEAK_RATIO = 0.8
MIN_PEAK_RATIO = 0.1
MAX_PEAK_RATIO = 1.0
#: 趋势判定阈值：前后半段日均人流变化率不超过 ±5% 视为「持平」。
FLAT_TOLERANCE = 0.05
#: 高峰小时榜与趋势序列展示的条数上限。
MAX_PEAK_HOURS = 5

#: 三张报表的名字（可只算其中一部分，避免单个接口做多余的工作）。
SECTIONS = ("trend", "peak", "forecast")
DEFAULT_SECTIONS = SECTIONS

#: 用户问题长度上限（与 backend/classroom.py 保持一致）。
MAX_QUESTION_LENGTH = 200
#: 解释文本的行长与条数上限（与 backend/classroom.py 保持一致）。
MAX_INSIGHT_LINE = 200
MAX_INSIGHT_ITEMS = 3
#: 解释结果里的错误码。
ERROR_UNSAFE_EXPLANATION = "unsafe_explanation"
ERROR_INVALID_EXPLANATION = "invalid_explanation"

#: SQL 片段特征：只在**模型返回的解释**上做过滤，不用来改写任何查询。
_SQL_RE = re.compile(
    r"\b(select|insert|update|delete|drop|alter|create|attach|detach|pragma|union|truncate|exec)\b"
    r"|--|/\*|\*/",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# 0. 小工具
# ---------------------------------------------------------------------------

def _round(value, digits=2):
    return None if value is None else round(float(value), digits)


def _round_people(value):
    """人数取整：非负数四舍五入。

    不用内置 ``round``：它做的是银行家舍入（``round(0.5) == 0``），
    会让「正好落在 .5 上」的预测值随机感地少 1 人，测试也不好复算。
    """
    return int(math.floor(float(value) + 0.5))


def _mean(values):
    values = list(values)
    return sum(values) / len(values) if values else None


def _parse_date(date_text):
    year, month, day = (int(part) for part in date_text.split("-"))
    return datetime.date(year, month, day)


def _weekday_of(date_text):
    """``YYYY-MM-DD`` → 1..7（1=周一），与 ``schedules.weekday`` 同一套编号。"""
    return _parse_date(date_text).isoweekday()


def _weekday_name(weekday):
    return WEEKDAY_NAMES[weekday - 1] if 1 <= weekday <= 7 else "未知"


def _slot_name(time_slot):
    return SLOT_NAMES.get(time_slot, "全部时段")


def _check_float(value, field, minimum=None, maximum=None):
    """数字校验：接受 ``0.8`` / ``"0.8"``，布尔值不算数字。"""
    if isinstance(value, bool):
        raise ValidationError("参数 {} 必须是数字，实际为布尔值".format(field), field=field)
    if not isinstance(value, (int, float)):
        try:
            value = float(str(value).strip())
        except (TypeError, ValueError):
            raise ValidationError(
                "参数 {} 必须是数字，实际为 {!r}".format(field, value), field=field
            )
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValidationError("参数 {} 必须是有限数字".format(field), field=field)
    if minimum is not None and number < minimum:
        raise ValidationError("参数 {} 必须 >= {}".format(field, minimum), field=field)
    if maximum is not None and number > maximum:
        raise ValidationError("参数 {} 必须 <= {}".format(field, maximum), field=field)
    return number


def check_question(question):
    """校验可选的用户问题，返回去掉首尾空白的文本（``None`` 表示不解释原问题）。"""
    if question is None:
        return None
    if not isinstance(question, str):
        raise ValidationError("参数 question 必须是字符串", field="question")
    text = question.strip()
    if not text:
        return None
    if len(text) > MAX_QUESTION_LENGTH:
        raise ValidationError(
            "参数 question 最长 {} 个字符，实际 {}".format(MAX_QUESTION_LENGTH, len(text)),
            field="question",
        )
    return text


# ---------------------------------------------------------------------------
# 1. 参数校验（业务层第二道；第一道在 backend/validators.py 的路由层）
# ---------------------------------------------------------------------------

def validate_forecast_params(params, warnings=None):
    """校验并归一化食堂分析参数。

    即使调用方直接构造 ``params`` 也要经过这一套类型/范围/互相约束检查，
    非法取值直接抛 ``ValidationError``（HTTP 400），绝不「静默按默认值跑」：

        - ``canteen_id``：>= 1 的整数（不存在时在查询层抛 404）
        - ``date_from`` / ``date_to``：``YYYY-MM-DD`` 且必须真实存在，区间不能颠倒
        - ``time_slot``：``breakfast/lunch/dinner/other``，不传表示全部时段
        - ``target_date``：预测起始日（不传则从历史数据最后一天之后开始）
        - ``horizon_days``：1..30，默认 3
        - ``peak_ratio``：0.1..1.0，默认 0.8（高峰时间段阈值）
    """
    if not isinstance(params, dict):
        raise ValidationError("食堂分析的查询参数必须是对象（object）", field="params")

    canteen_id = params.get("canteen_id")
    if canteen_id is not None:
        canteen_id = check_int(canteen_id, "canteen_id", minimum=1)

    date_from = check_date(params["date_from"], "date_from") if params.get("date_from") else None
    date_to = check_date(params["date_to"], "date_to") if params.get("date_to") else None
    if date_from and date_to and date_to < date_from:
        raise ValidationError(
            "参数 date_to ({}) 不能早于 date_from ({})".format(date_to, date_from), field="date_to"
        )

    time_slot = params.get("time_slot")
    if time_slot is not None:
        if time_slot not in TIME_SLOTS:
            raise ValidationError(
                "参数 time_slot 的取值必须是 {} 之一，实际为 {!r}".format(
                    "/".join(TIME_SLOTS), time_slot
                ),
                field="time_slot",
            )

    target_date = (
        check_date(params["target_date"], "target_date") if params.get("target_date") else None
    )

    horizon_days = params.get("horizon_days")
    if horizon_days is None:
        horizon_days = DEFAULT_HORIZON_DAYS
    else:
        horizon_days = check_int(horizon_days, "horizon_days", minimum=1, maximum=MAX_HORIZON_DAYS)

    peak_ratio = params.get("peak_ratio")
    if peak_ratio is None:
        peak_ratio = PEAK_RATIO
    else:
        peak_ratio = _check_float(
            peak_ratio, "peak_ratio", minimum=MIN_PEAK_RATIO, maximum=MAX_PEAK_RATIO
        )

    if warnings is not None and time_slot and time_slot != LUNCH_SLOT:
        warnings.append(
            "本次请求把 time_slot 设为 {}：预测部分会按该时段训练与预测，"
            "不再是默认的午餐（{}）。".format(time_slot, LUNCH_SLOT)
        )

    return {
        "canteen_id": canteen_id,
        "date_from": date_from,
        "date_to": date_to,
        "time_slot": time_slot,
        "target_date": target_date,
        "horizon_days": horizon_days,
        "peak_ratio": peak_ratio,
    }


def _check_sections(sections):
    if isinstance(sections, str):
        sections = (sections,)
    if not isinstance(sections, (list, tuple)):
        raise ValidationError("参数 sections 必须是字符串或字符串列表", field="sections")
    selected = tuple(section for section in sections)
    for section in selected:
        if section not in SECTIONS:
            raise ValidationError(
                "参数 sections 的取值必须是 {} 之一，实际为 {!r}".format(
                    "/".join(SECTIONS), section
                ),
                field="sections",
            )
    if not selected:
        raise ValidationError("参数 sections 不能为空", field="sections")
    return selected


# ---------------------------------------------------------------------------
# 2. 数据底座
# ---------------------------------------------------------------------------

def load_meal_grid(conn, params):
    """读一次「日期 × 小时」网格。

    ``time_slot`` **故意不推给 SQL**：趋势与高峰默认看全部时段，预测固定看午餐，
    三张报表共用同一次读取，口径天然一致（过滤在 Python 侧按报表做）。
    """
    return queries.canteen_meal_grid(
        conn,
        {
            "canteen_id": params.get("canteen_id"),
            "date_from": params.get("date_from"),
            "date_to": params.get("date_to"),
        },
    )


def _filter_by_slot(records, time_slot):
    if not time_slot:
        return list(records)
    return [record for record in records if record["time_slot"] == time_slot]


def _scoped_cells(records):
    """把多条记录按「日期 + 小时」合计成客流单元 ``{(date, hour): people}``。

    这是本模块所有统计的统一粒度：同一食堂同一天同一小时在库里只有一条记录，
    不指定食堂时则把多个食堂合计成全校客流。
    """
    cells = {}
    for record in records:
        key = (record["record_date"], record["record_hour"])
        cells[key] = cells.get(key, 0) + record["people_count"]
    return cells


def _scoped_slot_cells(records):
    """按「日期 + 时段」合计（高峰时段排行榜用）。"""
    cells = {}
    for record in records:
        key = (record["record_date"], record["time_slot"])
        cells[key] = cells.get(key, 0) + record["people_count"]
    return cells


def _summarize_cells(cells, field, order=None):
    """把 ``{(date, value): people}`` 汇总成 ``[{field, days, people_total, avg_people, share}]``。

    ``avg_people`` 是**客流单元**的平均值（每个单元 = 某天某小时的全部食堂合计），
    ``days`` 是有记录的不同日期数，``share`` 是该取值占人流总量的比例。
    """
    buckets = {}
    for (date_text, value), people in cells.items():
        bucket = buckets.setdefault(value, {"values": [], "total": 0, "dates": set()})
        bucket["values"].append(people)
        bucket["total"] += people
        bucket["dates"].add(date_text)

    grand_total = sum(bucket["total"] for bucket in buckets.values())
    keys = sorted(buckets) if order is None else [key for key in order if key in buckets]
    return [
        {
            field: key,
            "days": len(buckets[key]["dates"]),
            "samples": len(buckets[key]["values"]),
            "people_total": buckets[key]["total"],
            "avg_people": _round(_mean(buckets[key]["values"]), 2),
            "share": _round(buckets[key]["total"] / grand_total, 4) if grand_total else None,
        }
        for key in keys
    ]


def _samples_by_weekday(cells):
    """``{weekday: [客流单元, ...]}``（星期修正的原料）。"""
    samples = {}
    for (date_text, _hour), people in cells.items():
        samples.setdefault(_weekday_of(date_text), []).append(people)
    return samples


def _samples_by_hour(cells):
    """``{hour: [客流单元, ...]}``（时间段修正的原料）。"""
    samples = {}
    for (_date_text, hour), people in cells.items():
        samples.setdefault(hour, []).append(people)
    return samples


def _wait_times(records):
    """``{汇总键: [wait_minutes, ...]}``，用来算「该时段平均等待时间」。"""
    waits = {}
    for record in records:
        if record["wait_minutes"] is None:
            continue
        key = record["record_hour"]
        waits.setdefault(key, []).append(record["wait_minutes"])
    return waits


def _scope_label(grid, params):
    """中文口径描述，供人工核对与 AI 解释使用（不含表名、列名、SQL）。"""
    names = [item["canteen_name"] for item in grid["canteens"]]
    if params.get("canteen_id"):
        scope = names[0] if names else "食堂 {}".format(params["canteen_id"])
    elif names:
        scope = "全校 {} 个食堂合计（{}）".format(len(names), "、".join(names))
    else:
        scope = "无匹配食堂"
    window = grid["summary"]
    if window["date_start"]:
        scope += "，数据区间 {} ~ {}".format(window["date_start"], window["date_end"])
    return scope


# ---------------------------------------------------------------------------
# 3. 功能一：历史就餐趋势
# ---------------------------------------------------------------------------

def _daily_series(records):
    """按天汇总：``people_total`` 是当天的总客流（多食堂合计）。"""
    buckets = {}
    for record in records:
        bucket = buckets.setdefault(
            record["record_date"], {"people": 0, "records": 0, "waits": [], "hours": set()}
        )
        bucket["people"] += record["people_count"]
        bucket["records"] += 1
        bucket["hours"].add(record["record_hour"])
        if record["wait_minutes"] is not None:
            bucket["waits"].append(record["wait_minutes"])

    series = []
    for date_text in sorted(buckets):
        bucket = buckets[date_text]
        weekday = _weekday_of(date_text)
        series.append(
            {
                "date": date_text,
                "weekday": weekday,
                "weekday_name": _weekday_name(weekday),
                "records": bucket["records"],
                "hours": len(bucket["hours"]),
                "people_total": bucket["people"],
                "avg_people_per_hour": _round(
                    bucket["people"] / len(bucket["hours"]) if bucket["hours"] else None, 2
                ),
                "avg_wait_minutes": _round(_mean(bucket["waits"]), 2),
            }
        )
    return series


def _trend_direction(series):
    """前后半段对比定趋势（简单、可复算，不做线性回归）。"""
    totals = [item["people_total"] for item in series]
    half = len(totals) // 2
    first, second = totals[:half], totals[half:]
    first_avg, second_avg = _mean(first), _mean(second)
    change_rate = None
    if first_avg and second_avg is not None:
        change_rate = (second_avg - first_avg) / first_avg
    if change_rate is None:
        direction = "flat"
    elif change_rate > FLAT_TOLERANCE:
        direction = "up"
    elif change_rate < -FLAT_TOLERANCE:
        direction = "down"
    else:
        direction = "flat"
    return {
        "direction": direction,
        "first_half_avg": _round(first_avg, 2),
        "second_half_avg": _round(second_avg, 2),
        "change_rate": _round(change_rate, 4),
        "compared_days": len(first) + len(second),
        "flat_tolerance": FLAT_TOLERANCE,
    }


def _trend_report(grid, params):
    records = _filter_by_slot(grid["records"], params["time_slot"])
    series = _daily_series(records)
    cells = _scoped_cells(records)

    # 星期几画像与预测里的 by_weekday 用同一套「客流单元」口径，避免两张报表互相打架。
    weekday_samples = _samples_by_weekday(cells)
    weekday_profile = [
        {
            "weekday": weekday,
            "weekday_name": _weekday_name(weekday),
            "days": len({date_text for (date_text, _h) in cells if _weekday_of(date_text) == weekday}),
            "samples": len(weekday_samples[weekday]),
            "people_total": sum(weekday_samples[weekday]),
            "avg_people": _round(_mean(weekday_samples[weekday]), 2),
        }
        for weekday in sorted(weekday_samples)
    ]

    days = len(series)
    people_total = sum(item["people_total"] for item in series)
    busiest = max(series, key=lambda item: (item["people_total"], item["date"]), default=None)
    quietest = min(series, key=lambda item: (item["people_total"], item["date"]), default=None)

    return {
        "total": days,
        "time_slot": params["time_slot"],
        "slot_name": _slot_name(params["time_slot"]),
        "scope": _scope_label(grid, params),
        "summary": {
            "days": days,
            "records": len(records),
            "canteens": len(grid["canteens"]),
            "people_total": people_total,
            "avg_people_per_day": _round(people_total / days, 2) if days else None,
            "avg_people_per_hour": _round(_mean(cells.values()), 2) if cells else None,
            "date_start": series[0]["date"] if series else None,
            "date_end": series[-1]["date"] if series else None,
            "busiest_date": busiest,
            "quietest_date": quietest,
        },
        "trend": _trend_direction(series),
        "series": series,
        "weekday_profile": weekday_profile,
    }


def canteen_trend(conn, params):
    """功能一：查询历史就餐趋势（按天 / 按星期几）。

    返回每天的客流、平均每小时人流、平均等待时间，以及前后半段对比得出的趋势方向。
    """
    normalized = validate_forecast_params(params)
    grid = load_meal_grid(conn, normalized)
    return _trend_report(grid, normalized)


# ---------------------------------------------------------------------------
# 4. 功能二：高峰时间段
# ---------------------------------------------------------------------------

def _peak_window(hours, ratio):
    """以峰值为中心向两侧扩张，取「连续且 >= 峰值 * ratio」的小时区间。

    只做比较，不做回归：阈值以上、且与峰值小时**逐个小时相邻**的时段才算同一个高峰。
    """
    if not hours:
        return None
    peak = max(hours, key=lambda item: (item["avg_people"], -item["hour"]))
    threshold = peak["avg_people"] * ratio
    index = hours.index(peak)

    start = index
    while (
        start > 0
        and hours[start - 1]["hour"] == hours[start]["hour"] - 1
        and hours[start - 1]["avg_people"] >= threshold
    ):
        start -= 1
    end = index
    while (
        end + 1 < len(hours)
        and hours[end + 1]["hour"] == hours[end]["hour"] + 1
        and hours[end + 1]["avg_people"] >= threshold
    ):
        end += 1

    window = hours[start : end + 1]
    first, last = window[0]["hour"], window[-1]["hour"]
    return {
        "start_hour": first,
        "end_hour": last,
        "hours": [item["hour"] for item in window],
        "label": "{:02d}:00-{:02d}:00".format(first, min(24, last + 1)),
        "avg_people": _round(_mean([item["avg_people"] for item in window]), 2),
        "peak_hour": peak["hour"],
        "peak_avg_people": peak["avg_people"],
        "threshold_ratio": ratio,
        "threshold_people": _round(threshold, 2),
    }


def _peak_report(grid, params):
    records = _filter_by_slot(grid["records"], params["time_slot"])
    ratio = params["peak_ratio"]

    hours = _summarize_cells(_scoped_cells(records), "hour")
    waits = _wait_times(records)
    for item in hours:
        item["avg_wait_minutes"] = _round(_mean(waits.get(item["hour"], [])), 2)

    window = _peak_window(hours, ratio)
    window_hours = set(window["hours"]) if window else set()
    for item in hours:
        item["is_peak"] = bool(window) and item["hour"] == window["peak_hour"]
        item["in_window"] = item["hour"] in window_hours

    slots = _summarize_cells(_scoped_slot_cells(records), "time_slot", order=SLOT_ORDER)
    slot_waits = {}
    for record in records:
        if record["wait_minutes"] is not None:
            slot_waits.setdefault(record["time_slot"], []).append(record["wait_minutes"])
    for item in slots:
        item["slot_name"] = _slot_name(item["time_slot"])
        item["avg_wait_minutes"] = _round(_mean(slot_waits.get(item["time_slot"], [])), 2)
        # 时段是「一天里的一个区间」，所以它的均值是**每天的合计**，
        # 与 hours[].avg_people（每小时）区分开，避免两个同名字段口径不同。
        item["avg_people_per_day"] = item.pop("avg_people")
    peak_slot = max(
        slots, key=lambda item: (item["avg_people_per_day"], item["time_slot"]), default=None
    )
    for item in slots:
        item["is_peak"] = bool(peak_slot) and item["time_slot"] == peak_slot["time_slot"]

    ranked = sorted(hours, key=lambda item: (-item["avg_people"], item["hour"]))

    return {
        "total": len(hours),
        "time_slot": params["time_slot"],
        "slot_name": _slot_name(params["time_slot"]),
        "scope": _scope_label(grid, params),
        "peak_ratio": ratio,
        "summary": {
            "records": len(records),
            "people_total": sum(item["people_total"] for item in hours),
            "peak_hour": window["peak_hour"] if window else None,
            "peak_avg_people": window["peak_avg_people"] if window else None,
            "peak_window": window["label"] if window else None,
            "peak_slot": peak_slot["time_slot"] if peak_slot else None,
            "peak_slot_name": peak_slot["slot_name"] if peak_slot else None,
        },
        "window": window,
        "hours": hours,
        "ranked_hours": ranked[:MAX_PEAK_HOURS],
        "slots": slots,
    }


def canteen_peak_periods(conn, params):
    """功能二：计算高峰时间段。

    返回逐小时的人流与等待时间、峰值小时、以峰值为中心的高峰区间
    （连续且不低于 ``峰值 * peak_ratio`` 的小时），以及早/午/晚餐的对比。
    """
    normalized = validate_forecast_params(params)
    grid = load_meal_grid(conn, normalized)
    return _peak_report(grid, normalized)


# ---------------------------------------------------------------------------
# 5. 功能三：预测未来午餐人流
# ---------------------------------------------------------------------------

def _forecast_start_date(params, grid):
    """预测起始日：显式给 ``target_date`` 就用它，否则从「数据最后一天」之后开始。

    同时考虑 ``date_to``：如果用户把区间截到更晚的日期（例如只要 9 月的数据，
    却想预测 10 月），预测就从那个更晚的日期之后开始，仍然保证是「未来」。
    """
    if params.get("target_date"):
        return _parse_date(params["target_date"])
    candidates = [value for value in (params.get("date_to"), grid["summary"]["date_end"]) if value]
    if not candidates:
        return datetime.date.today()
    return _parse_date(max(candidates)) + datetime.timedelta(days=1)


def _forecast_report(grid, params):
    time_slot = params["time_slot"] or LUNCH_SLOT
    records = _filter_by_slot(grid["records"], time_slot)
    cells = _scoped_cells(records)

    values = list(cells.values())
    base = _mean(values)

    weekday_values = _samples_by_weekday(cells)
    hour_values = _samples_by_hour(cells)

    weekday_avg = {weekday: _mean(items) for weekday, items in weekday_values.items()}
    hour_avg = {hour: _mean(items) for hour, items in hour_values.items()}
    lunch_hours = sorted(hour_values)

    days = {date_text for date_text, _hour in cells}
    notes = []

    by_weekday = [
        {
            "weekday": weekday,
            "weekday_name": _weekday_name(weekday),
            "days": len({date_text for (date_text, _h) in cells if _weekday_of(date_text) == weekday}),
            "samples": len(weekday_values[weekday]),
            "avg_people": _round(weekday_avg[weekday], 4),
            "adjustment": _round(weekday_avg[weekday] - base, 4),
        }
        for weekday in sorted(weekday_values)
    ]
    by_hour = [
        {
            "hour": hour,
            "samples": len(hour_values[hour]),
            "avg_people": _round(hour_avg[hour], 4),
            "adjustment": _round(hour_avg[hour] - base, 4),
        }
        for hour in lunch_hours
    ]

    predictions = []
    if base is not None and lunch_hours:
        start = _forecast_start_date(params, grid)
        missing_weekdays = set()
        for offset in range(params["horizon_days"]):
            target = start + datetime.timedelta(days=offset)
            weekday = target.isoweekday()
            if weekday not in weekday_avg:
                missing_weekdays.add(_weekday_name(weekday))
            weekday_adjustment = weekday_avg.get(weekday, base) - base
            hours = []
            total = 0
            for hour in lunch_hours:
                hour_adjustment = hour_avg.get(hour, base) - base
                raw = max(0.0, base + weekday_adjustment + hour_adjustment)
                people = _round_people(raw)
                total += people
                hours.append(
                    {
                        "hour": hour,
                        "base": _round(base, 4),
                        "weekday_adjustment": _round(weekday_adjustment, 4),
                        "hour_adjustment": _round(hour_adjustment, 4),
                        "predicted_people": people,
                    }
                )
            predictions.append(
                {
                    "date": target.isoformat(),
                    "weekday": weekday,
                    "weekday_name": _weekday_name(weekday),
                    "predicted_people_total": total,
                    "hours": hours,
                }
            )
        if missing_weekdays:
            notes.append(
                "历史数据里没有 {} 的样本，这些日期的星期修正按 0 处理"
                "（等价于只用平均值 + 时间段修正）。".format("、".join(sorted(missing_weekdays)))
            )
    else:
        notes.append(
            "没有可用于训练的历史记录：请先导入数据"
            "（python scripts/import_data.py）或放宽食堂/日期筛选。"
        )

    return {
        "method": FORECAST_METHOD,
        "total": len(predictions),
        "time_slot": time_slot,
        "slot_name": _slot_name(time_slot),
        "scope": _scope_label(grid, params),
        "training": {
            "time_slot": time_slot,
            "records": len(records),
            "days": len(days),
            "cells": len(cells),
            "date_start": grid["summary"]["date_start"],
            "date_end": grid["summary"]["date_end"],
            "base_avg_people": _round(base, 4),
            "hours": lunch_hours,
            "by_weekday": by_weekday,
            "by_hour": by_hour,
        },
        "predictions": predictions,
        "notes": notes,
    }


def forecast_lunch_traffic(conn, params):
    """功能三：预测未来午餐人流。

    方法固定为 **历史同期平均值 + 星期修正 + 时间段修正**（见模块说明），
    训练窗口是筛选后的全部历史午餐记录，预测窗口是
    ``target_date``（默认历史最后一天之后）起的 ``horizon_days`` 天。
    """
    normalized = validate_forecast_params(params)
    grid = load_meal_grid(conn, normalized)
    return _forecast_report(grid, normalized)


# ---------------------------------------------------------------------------
# 6. AI 结果解释器（只解释，不计算、不查询）
# ---------------------------------------------------------------------------

def _hour_of(prediction):
    if not prediction["hours"]:
        return None, None
    best = max(prediction["hours"], key=lambda item: (item["predicted_people"], -item["hour"]))
    return best["hour"], best["predicted_people"]


def build_insight_payload(question, report):
    """构造交给模型的「只读事实表」。

    只有已经算好的数字与中文口径描述：**没有 SQL、没有表名、没有数据库路径**，
    也没有原始明细，因此模型即使想「自己算一遍」也无从下手。
    """
    payload = {"question": question, "scope": [], "notes": list(report.get("notes") or [])}

    trend = report.get("trend")
    if trend:
        payload["scope"].append(
            "{}（{}，{}）".format(trend["scope"], trend["slot_name"], "趋势口径：按天客流")
        )
        summary = trend["summary"]
        payload["trend"] = {
            "days": summary["days"],
            "people_total": summary["people_total"],
            "avg_people_per_day": summary["avg_people_per_day"],
            "avg_people_per_hour": summary["avg_people_per_hour"],
            "date_start": summary["date_start"],
            "date_end": summary["date_end"],
            "direction": trend["trend"]["direction"],
            "change_rate": trend["trend"]["change_rate"],
            "busiest_date": (summary["busiest_date"] or {}).get("date"),
            "busiest_people": (summary["busiest_date"] or {}).get("people_total"),
            "quietest_date": (summary["quietest_date"] or {}).get("date"),
            "quietest_people": (summary["quietest_date"] or {}).get("people_total"),
            "weekday_profile": [
                {
                    "weekday_name": item["weekday_name"],
                    "avg_people": item["avg_people"],
                    "days": item["days"],
                }
                for item in trend["weekday_profile"]
            ],
        }

    peak = report.get("peak")
    if peak:
        payload["scope"].append(
            "{}（{}，高峰口径：{}）".format(
                peak["scope"], peak["slot_name"], "不低于峰值的 {:.0%}".format(peak["peak_ratio"])
            )
        )
        payload["peak"] = {
            "peak_hour": peak["summary"]["peak_hour"],
            "peak_avg_people": peak["summary"]["peak_avg_people"],
            "peak_window": peak["summary"]["peak_window"],
            "peak_slot_name": peak["summary"]["peak_slot_name"],
            "ranked_hours": [
                {
                    "hour": item["hour"],
                    "avg_people": item["avg_people"],
                    "avg_wait_minutes": item["avg_wait_minutes"],
                }
                for item in peak["ranked_hours"]
            ],
            "slots": [
                {
                    "time_slot": item["time_slot"],
                    "slot_name": item["slot_name"],
                    "avg_people_per_day": item["avg_people_per_day"],
                    "share": item["share"],
                }
                for item in peak["slots"]
            ],
        }

    forecast = report.get("forecast")
    if forecast:
        payload["notes"].extend(forecast.get("notes") or [])
        payload["scope"].append(
            "{}（{}，预测方法：{}）".format(
                forecast["scope"], forecast["slot_name"], forecast["method"]
            )
        )
        payload["forecast"] = {
            "method": forecast["method"],
            "base_avg_people": forecast["training"]["base_avg_people"],
            "training_days": forecast["training"]["days"],
            "trained_hours": forecast["training"]["hours"],
            "targets": [
                {
                    "date": item["date"],
                    "weekday_name": item["weekday_name"],
                    "predicted_people_total": item["predicted_people_total"],
                    "busiest_hour": _hour_of(item)[0],
                    "busiest_hour_people": _hour_of(item)[1],
                    "hours": [
                        {"hour": hour["hour"], "predicted_people": hour["predicted_people"]}
                        for hour in item["hours"]
                    ],
                }
                for item in forecast["predictions"]
            ],
        }
    return payload


def _render_explanation(summary, highlights, suggestions, source, error=None):
    """统一解释结构：一个摘要 + 少量要点 + 建议（AI 与兜底共用）。"""
    lines = [summary]
    lines.extend("• " + item for item in highlights)
    if suggestions:
        # 建议本身就是短句，统一去掉句末句号再用「；」拼接，避免出现「。；」。
        lines.append("建议：" + "；".join(item.rstrip("。") for item in suggestions) + "。")
    return {
        "text": "\n".join(lines),
        "summary": summary,
        "highlights": list(highlights),
        "suggestions": list(suggestions),
        "source": source,
        "error": error,
    }


def _trend_sentence(trend):
    summary = trend["summary"]
    if not summary["days"]:
        return "选定的食堂与日期区间内没有任何就餐记录。"
    direction = {"up": "整体上升", "down": "整体下降", "flat": "整体持平"}[trend["trend"]["direction"]]
    rate = trend["trend"]["change_rate"]
    rate_text = "（后半段日均变化 {:+.1%}）".format(rate) if rate is not None else ""
    busiest = summary["busiest_date"]
    return "{} 到 {} 共 {} 天有记录，合计 {} 人次，日均 {} 人次，{}。最忙的是 {}（{} 人次）。".format(
        summary["date_start"],
        summary["date_end"],
        summary["days"],
        summary["people_total"],
        summary["avg_people_per_day"],
        direction + rate_text,
        busiest["date"],
        busiest["people_total"],
    )


def _peak_sentence(peak):
    summary = peak["summary"]
    if summary["peak_hour"] is None:
        return "没有足够的历史记录来判断高峰时段。"
    top = peak["ranked_hours"][0]
    wait = "，平均等待 {} 分钟".format(top["avg_wait_minutes"]) if top["avg_wait_minutes"] else ""
    return "人流最高的是 {} 点（平均 {} 人{}），高峰时间段为 {}。".format(
        summary["peak_hour"], summary["peak_avg_people"], wait, summary["peak_window"]
    )


def _forecast_sentence(forecast):
    if not forecast["predictions"]:
        return "缺少历史记录，暂时无法给出预测。"
    training = forecast["training"]
    first = forecast["predictions"][0]
    head = "按「{}」用 {} 天历史午餐数据（基准 {} 人/小时）预测：".format(
        forecast["method"], training["days"], training["base_avg_people"]
    )
    parts = [
        "{}（{}）预计 {} 人次".format(item["date"], item["weekday_name"], item["predicted_people_total"])
        for item in forecast["predictions"]
    ]
    return head + "；".join(parts) + "。（示例：{} {} 点 {} 人）".format(
        first["date"], _hour_of(first)[0], _hour_of(first)[1]
    )


def local_explanation(report):
    """Python 兜底解释：AI 不可用或输出不合法时使用，仍然是「只解释已算好的结果」。"""
    sentences = []
    if report.get("trend"):
        sentences.append(_trend_sentence(report["trend"]))
    if report.get("peak"):
        sentences.append(_peak_sentence(report["peak"]))
    if report.get("forecast"):
        sentences.append(_forecast_sentence(report["forecast"]))
    summary = " ".join(sentences) if sentences else "没有可解释的分析结果。"

    highlights = []
    peak = report.get("peak")
    if peak and peak["ranked_hours"]:
        highlights.append(
            "高峰小时榜：" + "、".join(
                "{} 点 {} 人".format(item["hour"], item["avg_people"])
                for item in peak["ranked_hours"][:3]
            )
        )
    trend = report.get("trend")
    if trend and trend["weekday_profile"]:
        busiest_day = max(
            trend["weekday_profile"], key=lambda item: (item["avg_people"], -item["weekday"])
        )
        highlights.append(
            "{}的人流最高（平均 {} 人/小时），可以优先错峰。".format(
                busiest_day["weekday_name"], busiest_day["avg_people"]
            )
        )
    forecast = report.get("forecast")
    if forecast and forecast["predictions"]:
        busiest = max(
            forecast["predictions"], key=lambda item: (-item["predicted_people_total"], item["date"])
        )
        highlights.append("预测期内人流最多的是 {}（{} 人次）。".format(busiest["date"], busiest["predicted_people_total"]))

    suggestions = []
    if report.get("peak") and report["peak"]["summary"]["peak_window"]:
        suggestions.append(
            "错开 {} 用餐，或提前/延后半小时。".format(report["peak"]["summary"]["peak_window"])
        )
    if report.get("forecast") and forecast["training"]["hours"]:
        busiest_hour = max(
            forecast["training"]["by_hour"], key=lambda item: (item["avg_people"], -item["hour"])
        )
        suggestions.append(
            "预测区间内 {} 点最挤，建议分流到相邻时段。".format(busiest_hour["hour"])
        )

    return _render_explanation(summary, highlights, suggestions, source="local")


def _clean_line(value, limit=MAX_INSIGHT_LINE):
    """清洗解释文本的一行：压空白、限长度，出现 SQL 片段直接判为不安全。"""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if not text:
        return ""
    if _SQL_RE.search(text):
        raise AIParserError(
            "解释里出现了 SQL 片段，已拒绝：{}".format(text[:60]), code=ERROR_UNSAFE_EXPLANATION
        )
    return text if len(text) <= limit else text[:limit] + "…"


def _clean_lines(values, limit=MAX_INSIGHT_ITEMS):
    if not isinstance(values, (list, tuple)):
        return []
    cleaned = []
    for value in values:
        text = _clean_line(value)
        if text:
            cleaned.append(text)
        if len(cleaned) >= limit:
            break
    return cleaned


def _compose_explanation(data, source):
    """把模型返回的 JSON 收敛成统一结构（字段缺失/类型不对就报错，由上层兜底）。"""
    if not isinstance(data, dict):
        raise AIParserError("解释结果必须是 JSON 对象", code=ERROR_INVALID_EXPLANATION)
    summary = _clean_line(data.get("summary"))
    if not summary:
        raise AIParserError("解释结果缺少 summary", code=ERROR_INVALID_EXPLANATION)
    return _render_explanation(
        summary,
        _clean_lines(data.get("highlights")),
        _clean_lines(data.get("suggestions")),
        source=source,
    )


class ForecastExplainer:
    """AI 结果解释器：把已经算好的食堂报表讲成人话。

    与 :class:`backend.classroom.ResultExplainer` 同一套约定：配置从 ``.env`` 读、
    Prompt 从 ``prompts/`` 读（正式 Prompt 走 Git 留痕）、Key 不写进代码。
    区别是它只看到**统计与预测结果**：没有明细、没有 SQL、没有连接、没有查询入口，
    因此 AI 不可能替 Python 算数，也不可能改口径。
    """

    def __init__(self, config=None, session=None, prompt=None, prompt_path=None, timeout=None):
        self._config = dict(config) if config else None
        self._session = session if session is not None else requests
        self._prompt = prompt
        self._prompt_path = prompt_path
        self._timeout = timeout

    @property
    def config(self):
        """AI 配置（首次访问时才读 ``.env``，读取失败抛 AIParserError）。"""
        if self._config is None:
            self._config = load_config()
        return self._config

    @property
    def system_prompt(self):
        """system prompt = ``prompts/canteen_insight_generator.md``。"""
        if self._prompt is None:
            self._prompt = load_prompt(self._prompt_path or DEFAULT_INSIGHT_PROMPT_PATH)
        return self._prompt

    def build_messages(self, payload):
        """system = 解释器 Prompt，user = 只读事实表（JSON）。"""
        return [
            {"role": "system", "content": self.system_prompt.rstrip()},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

    def request_payload(self, payload):
        """构造 ``/chat/completions`` 请求体（单独成方法，方便核对与测试）。"""
        return {
            "model": self.config["model"],
            "messages": self.build_messages(payload),
            "response_format": {"type": "json_object"},
            "temperature": self.config["temperature"],
            "max_tokens": self.config["max_tokens"],
        }

    def call_api(self, payload):
        """调用 OpenAI-compatible 接口，返回助手回复文本（失败转 AIParserError）。"""
        url = "{}/chat/completions".format(self.config["base_url"])
        headers = {
            "Authorization": "Bearer {}".format(self.config["api_key"]),
            "Content-Type": "application/json",
        }
        try:
            response = self._session.post(
                url,
                headers=headers,
                json=self.request_payload(payload),
                timeout=self._timeout or self.config["timeout"],
            )
        except requests.exceptions.Timeout as exc:
            raise AIParserError("调用 AI 解释接口超时：{}".format(exc), code=ERROR_NETWORK)
        except requests.exceptions.RequestException as exc:
            raise AIParserError("调用 AI 解释接口失败（网络错误）：{}".format(exc), code=ERROR_NETWORK)
        return parse_response_text(response)

    def explain(self, payload):
        """解释分析结果，返回统一解释结构（``source = "ai"``）。"""
        return _compose_explanation(extract_json_object(self.call_api(payload)), source="ai")


def explain_report(question, report, explainer=None):
    """AI 解释分析结果；任何失败都退回 Python 兜底解释，绝不让整条链路失败。"""
    payload = build_insight_payload(question, report)
    try:
        instance = explainer if explainer is not None else ForecastExplainer()
        explanation = instance.explain(payload)
        explanation["error"] = None
        return explanation
    except Exception as exc:  # AI 不可用 / 返回不合法：降级但不能 500
        fallback = local_explanation(report)
        fallback["error"] = (
            exc.to_dict() if hasattr(exc, "to_dict") else {"code": "explain_failed", "message": str(exc)}
        )
        return fallback


# ---------------------------------------------------------------------------
# 7. 完整链路
# ---------------------------------------------------------------------------

def _scope_notes(grid, params):
    """把口径与数据缺口显式写出来，避免用户误读数字。"""
    notes = []
    if not grid["total"]:
        notes.append("选定的食堂与日期区间内没有就餐记录，所有统计与预测都会是空的。")
    elif grid["summary"]["canteens"] > 1 and not params.get("canteen_id"):
        notes.append(
            "未指定食堂：统计口径是 {} 个食堂合计，"
            "如需单个食堂请传 canteen_id。".format(grid["summary"]["canteens"])
        )
    if params["time_slot"]:
        notes.append("本次只看「{}」时段。".format(_slot_name(params["time_slot"])))
    return notes


def analyze_canteen(params, db_path=None, question=None, explainer=None, explain=True,
                    sections=DEFAULT_SECTIONS):
    """食堂分析完整链路（本模块唯一对外入口）。

    参数：
        params     查询参数（见 :func:`validate_forecast_params`）
        db_path    SQLite 文件路径（默认 ``data/campus.db``，可用 ``CAMPUS_DB_PATH`` 覆盖）
        question   用户原始问题（可选，仅用于让 AI 结合原问题解释）
        explainer  结果解释器（默认 :class:`ForecastExplainer`，测试可注入假实现）
        explain    ``False`` 时跳过 AI 解释，直接用 Python 兜底文案（测试/离线可用）
        sections   只计算哪几张报表（``trend`` / ``peak`` / ``forecast`` 的任意组合）

    返回 ``dict``（JSON 可序列化）：``params`` / ``query`` / ``canteens`` / ``notes``，
    以及被选中的 ``trend`` / ``peak`` / ``forecast`` 和统一的 ``explanation``。

    关键约束：**AI 调用发生在 SQLite 连接关闭之后**（见下方 ``with`` 块），
    模型只拿到 :func:`build_insight_payload` 生成的只读事实表。

    异常：
        :class:`~backend.errors.ValidationError`  参数非法（HTTP 400）
        :class:`~backend.errors.NotFoundError`    食堂编号不存在（HTTP 404）
    """
    question = check_question(question)
    normalized = validate_forecast_params(params)
    selected = _check_sections(sections)

    # 统计 + 预测阶段：只碰 Python + SQLite，模型完全不参与。
    with connection(db_path or default_db_path()) as conn:
        grid = load_meal_grid(conn, normalized)
        report = {
            "question": question,
            "params": normalized,
            "query": grid["query"],
            "canteens": grid["canteens"],
            "summary": grid["summary"],
            "notes": _scope_notes(grid, normalized),
        }
        if "trend" in selected:
            report["trend"] = _trend_report(grid, normalized)
        if "peak" in selected:
            report["peak"] = _peak_report(grid, normalized)
        if "forecast" in selected:
            report["forecast"] = _forecast_report(grid, normalized)

    # 解释阶段：连接已关闭，模型只能看到事实表，没有任何查询能力。
    report["explanation"] = (
        explain_report(question, report, explainer=explainer)
        if explain
        else local_explanation(report)
    )
    return report
