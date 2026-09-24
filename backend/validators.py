# -*- coding: utf-8 -*-
"""查询参数校验：把 URL 里的字符串变成安全、可绑定的 Python 值。

设计原则：
    1. 每个字段一个函数，取值非法一律抛 :class:`ValidationError`（→ HTTP 400）。
    2. 空串（``?a=``）按「没传」处理，使用默认值；必填字段则直接报错。
    3. 不做任何 AI 解析：这里只有确定性的类型与范围校验（AGENTS.md AI Rules）。
"""

import datetime
import re

from .errors import ValidationError

# 与 docs/DATABASE.md 的 CHECK 约束保持一致（分类枚举，供校验与错误提示共用）。
ROOM_TYPES = ("普通", "多媒体", "机房", "实验室")
CLASSROOM_STATUSES = ("available", "maintenance")
TIME_SLOTS = ("breakfast", "lunch", "dinner", "other")
SORT_FIELDS = ("people", "coverage")
DIMENSIONS = ("industry", "city", "major", "company")

_INT_RE = re.compile(r"^[+-]?\d+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def raw_text(raw, name):
    """取原始字符串；空串视为未传（返回 ``None``）。"""
    value = raw.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def text_param(raw, name, default=None, max_length=100):
    value = raw_text(raw, name)
    if value is None:
        return default
    if len(value) > max_length:
        raise ValidationError(
            "参数 {} 最长 {} 个字符，实际 {} 个".format(name, max_length, len(value)), field=name
        )
    return value


def int_param(raw, name, default=None, minimum=None, maximum=None):
    value = raw_text(raw, name)
    if value is None:
        return default
    if not _INT_RE.match(value):
        raise ValidationError("参数 {} 必须是整数，实际为 {!r}".format(name, value), field=name)
    number = int(value)
    if minimum is not None and number < minimum:
        raise ValidationError("参数 {} 必须 >= {}".format(name, minimum), field=name)
    if maximum is not None and number > maximum:
        raise ValidationError("参数 {} 必须 <= {}".format(name, maximum), field=name)
    return number


def float_param(raw, name, default=None, minimum=None, maximum=None):
    value = raw_text(raw, name)
    if value is None:
        return default
    try:
        number = float(value)
    except ValueError:
        raise ValidationError("参数 {} 必须是数字，实际为 {!r}".format(name, value), field=name)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValidationError("参数 {} 必须是有限数字".format(name), field=name)
    if minimum is not None and number < minimum:
        raise ValidationError("参数 {} 必须 >= {}".format(name, minimum), field=name)
    if maximum is not None and number > maximum:
        raise ValidationError("参数 {} 必须 <= {}".format(name, maximum), field=name)
    return number


def bool_param(raw, name, default=None):
    """接受 1/0、true/false、yes/no，返回 ``int`` (1/0) 以便直接绑定给 SQLite。"""
    value = raw_text(raw, name)
    if value is None:
        return default
    lowered = value.lower()
    if lowered in ("1", "true", "yes", "y"):
        return 1
    if lowered in ("0", "false", "no", "n"):
        return 0
    raise ValidationError(
        "参数 {} 只接受 1/0（或 true/false），实际为 {!r}".format(name, value), field=name
    )


def choice_param(raw, name, choices, default=None):
    value = raw_text(raw, name)
    if value is None:
        return default
    if value not in choices:
        raise ValidationError(
            "参数 {} 的取值必须是 {} 之一，实际为 {!r}".format(name, "/".join(choices), value),
            field=name,
        )
    return value


def date_param(raw, name, default=None, minimum=None, maximum=None):
    """校验 ``YYYY-MM-DD`` 格式（并确认是真实存在的日期，例如 2025-02-30 会被拒）。"""
    value = raw_text(raw, name)
    if value is None:
        return default
    if not _DATE_RE.match(value):
        raise ValidationError(
            "参数 {} 必须是 YYYY-MM-DD 格式，实际为 {!r}".format(name, value), field=name
        )
    year, month, day = (int(part) for part in value.split("-"))
    try:
        datetime.date(year, month, day)
    except ValueError:
        raise ValidationError("参数 {} 不是有效日期：{}".format(name, value), field=name)
    if minimum is not None and value < minimum:
        raise ValidationError("参数 {} 不能早于 {}".format(name, minimum), field=name)
    if maximum is not None and value > maximum:
        raise ValidationError("参数 {} 不能晚于 {}".format(name, maximum), field=name)
    return value


def date_range(raw, from_name, to_name, default_from=None, default_to=None):
    """解析日期区间；起止顺序颠倒时直接报错，避免返回空结果让人误解。"""
    start = date_param(raw, from_name, default=default_from)
    end = date_param(raw, to_name, default=default_to)
    if start and end and start > end:
        raise ValidationError(
            "参数 {} ({}) 不能晚于 {} ({})".format(from_name, start, to_name, end),
            field=from_name,
        )
    return start, end


def period_range(raw, start_name="period_start", end_name="period_end"):
    """解析节次区间，返回 ``(start, end)``；未传则返回 ``(None, None)``。

    只传一个端点时，另一端自动补齐为同值，语义等价于「查询该节次」。
    """
    start = int_param(raw, start_name, minimum=1, maximum=12)
    end = int_param(raw, end_name, minimum=1, maximum=12)
    if start is None and end is None:
        return None, None
    if start is None:
        start = end
    if end is None:
        end = start
    if end < start:
        raise ValidationError(
            "参数 {} ({}) 不能大于 {} ({})".format(start_name, start, end_name, end),
            field=start_name,
        )
    return start, end
