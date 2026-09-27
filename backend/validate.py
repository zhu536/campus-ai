# -*- coding: utf-8 -*-
"""意图校验：把 AI 返回的 JSON 收敛成 Python 能安全使用的结构化请求。

分层（AGENTS.md AI Rules）：
    AI      → 只负责「识别意图 + 抽取参数」，输出一段 JSON
    Python  → 负责「白名单 + 类型 + 范围」校验，校验不过就报错（本模块）
    SQLite  → 只保存事实数据；本模块**不 import sqlite3、不连数据库、不拼 SQL**

校验的三个方面：
    1. intent 必须命中白名单 :data:`INTENTS`（模型编出来的意图一律拒绝）
    2. 字段必须属于该意图的字段白名单（字段名写错 = 无法安全翻译成查询）
    3. 每个字段按自己的规格校验类型与范围（capacity/period/weekday/length...）
       并且任何字段值里都不允许出现 SQL 片段（双保险，见 :func:`reject_sql`）

对外主要接口：
    :func:`validate_intent` 校验并归一化，返回 ``(intent, params)``
    :func:`normalize`       只归一化不校验（测试与调试用）
"""

import re

from .errors import ValidationError

# ---------------------------------------------------------------------------
# 1. 白名单：意图
# ---------------------------------------------------------------------------

#: 允许的意图全集，与 prompts/intent_parser.md 的 Supported Intents 保持一致。
INTENTS = (
    "room_search",
    "canteen_analysis",
    "canteen_forecast",
    "industry_skill_match",
    "industry_skill_gap",
    "dashboard_query",
    "unknown",
)

ROOM_TYPES = ("普通", "多媒体", "机房", "实验室")
DIMENSIONS = ("industry", "city", "major", "company")
TIME_SLOTS = ("breakfast", "lunch", "dinner", "other")
TREND_TYPES = ("trend", "moving_average", "linear")
SORT_FIELDS = ("people", "coverage")

#: 字符串字段的长度上限（防止模型把整段文本塞进一个参数）。
TEXT_MAX_LENGTH = 100

# ---------------------------------------------------------------------------
# 2. 字段规格
# ---------------------------------------------------------------------------

def _spec(kind, required=False, **rules):
    rules["kind"] = kind
    rules["required"] = required
    return rules


def _text(max_length=TEXT_MAX_LENGTH, choices=None, required=False):
    return _spec("text", max_length=max_length, choices=choices, required=required)


def _int(minimum=None, maximum=None, required=False):
    return _spec("int", minimum=minimum, maximum=maximum, required=required)


def _bool(required=False):
    return _spec("bool", required=required)


def _enum(choices, required=False):
    return _spec("enum", choices=tuple(choices), required=required)


def _date(required=False):
    return _spec("date", required=required)


#: 各意图的参数白名单：字段名 → 规格。key 用小写，模型返回的 ``Weekday`` 也能被接受。
SCHEMAS = {
    # 场景一：AI 智能找教室
    # 设备字段用 has_projector / has_computer，与 backend/queries.py 的
    # list_available_classrooms 读取的键名、以及 docs/API.md 的查询参数名完全一致。
    # 这些 "_text" 字段是「待解析的时间原文」，由 backend/time_resolver.py 在
    # 校验前替换成真正的日期字段（date / target_date / date_from + date_to）。
    "room_search": {
        "weekday": _int(1, 7),
        "period_start": _int(1, 12),
        "period_end": _int(1, 12),
        "capacity": _int(1, 1000),
        "has_projector": _bool(),
        "has_computer": _bool(),
        "room_type": _enum(ROOM_TYPES),
        "building": _text(50),
        "semester": _text(30),
        "date": _date(),
        "date_text": _text(30),
        "date_range_text": _text(30),
        "limit": _int(1, 100),
    },
    # 场景二：食堂拥挤分析（只看历史）
    "canteen_analysis": {
        "canteen_id": _int(1, 100000),
        "canteen_name": _text(50),
        "date_from": _date(),
        "date_to": _date(),
        "date_range_text": _text(30),
        "time_slot": _enum(TIME_SLOTS),
        "top_slots": _int(0, 4),
        "sort_by": _enum(SORT_FIELDS),
        "limit": _int(1, 100),
    },
    # 场景二：食堂拥挤预测（Python 算，不由 AI 算）
    "canteen_forecast": {
        "canteen_id": _int(1, 100000),
        "canteen_name": _text(50),
        "target_date": _date(),
        "date_text": _text(30),
        "time_slot": _enum(TIME_SLOTS),
        "horizon_days": _int(1, 30),
        "method": _enum(TREND_TYPES),
        "limit": _int(1, 100),
    },
    # 场景三：岗位技能 → 课程匹配
    "industry_skill_match": {
        "major": _text(required=True),
        "industry": _text(50),
        "city": _text(50),
        "company": _text(100),
        "top": _int(1, 100),
    },
    # 场景三：技能缺口 / 覆盖率
    "industry_skill_gap": {
        "major": _text(required=True),
        "industry": _text(50),
        "min_importance": _int(1, 5),
        "top": _int(1, 100),
    },
    # 看板查询
    "dashboard_query": {
        "dimension": _enum(DIMENSIONS),
        "date_from": _date(),
        "date_to": _date(),
        "limit": _int(1, 500),
    },
    # 无法识别：不允许带任何业务参数，避免「猜出来的参数」被下游当真
    "unknown": {},
}

#: 字段别名：同一语义的不同写法统一成规范名（只放通用、无歧义的映射）。
#:
#: 规范名以 **backend/queries.py 实际读取的键名** 为准：
#:   ``has_projector`` / ``has_computer`` / ``period_start`` / ``period_end`` / ``date_from`` / ``date_to``
#: 这样 AI 的产出可以**直接**喂给业务函数，不需要中间再翻译一层
#: （历史 bug：validate 产出 ``projector``，而 queries 读 ``has_projector``，
#: 设备筛选条件被静默丢弃 —— 见 tests/test_intent.py 的一致性守卫测试）。
#: 别名表的方向是「别名 → 规范名」，两边的写法都收在这里，模型写哪种都能落地。
ALIASES = {
    # 节次区间：Prompt 旧示例写 end_period，API/DB/查询层统一用 period_end
    "end_period": "period_end",
    "start_period": "period_start",
    # 设备：模型常见的 projector / need_projector 都收敛到 has_projector
    "projector": "has_projector",
    "need_projector": "has_projector",
    "computer": "has_computer",
    "need_computer": "has_computer",
    # 人数
    "people": "capacity",
    "people_count": "capacity",
    # 食堂 / 预测 / 专业
    "canteen": "canteen_name",
    "forecast_date": "target_date",
    "predict_date": "target_date",
    "profession": "major",
    # 相对日期原文（模型只抄原文，日期由 Python 解析）
    "date_expr": "date_text",
    "time_text": "date_text",
}

#: 意图专属别名：同一个词在不同意图里含义不同，必须分开处理。
#: 例如「date」在 room_search 里指具体日期，在 canteen_forecast 里指预测目标日。
INTENT_ALIASES = {
    "canteen_forecast": {"date": "target_date"},
    "industry_skill_match": {"skill": "major"},
    "industry_skill_gap": {"skill": "major"},
}

#: 中文星期 → 1..7（模型偶尔会返回「周三」而不是 3，归一化时兜底）。
_WEEKDAY_WORDS = {
    "周一": 1, "星期一": 1, "礼拜一": 1, "monday": 1, "mon": 1,
    "周二": 2, "星期二": 2, "礼拜二": 2, "tuesday": 2, "tue": 2,
    "周三": 3, "星期三": 3, "礼拜三": 3, "wednesday": 3, "wed": 3,
    "周四": 4, "星期四": 4, "礼拜四": 4, "thursday": 4, "thu": 4,
    "周五": 5, "星期五": 5, "礼拜五": 5, "friday": 5, "fri": 5,
    "周六": 6, "星期六": 6, "礼拜六": 6, "saturday": 6, "sat": 6,
    "周日": 7, "周天": 7, "星期日": 7, "星期天": 7, "礼拜天": 7, "sunday": 7, "sun": 7,
}

#: 布尔字段的中文说法（模型可能返回「需要」/「有」）。
_TRUE_WORDS = ("true", "yes", "y", "1", "需要", "要", "有", "是", "必须")
_FALSE_WORDS = ("false", "no", "n", "0", "不需要", "不要", "没有", "无", "否")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NUMBER_RE = re.compile(r"^[+-]?\d+$")
#: SQL 片段特征：注释、多语句、危险关键字、恒真条件。只用于拒绝，不用来改写。
_SQL_RE = re.compile(
    r"(--|/\*|\*/|;|')|"
    r"\b(select|insert|update|delete|drop|alter|create|attach|detach|pragma|union|truncate|grant|exec)\b|"
    r"\b(or|and)\b\s+['\"]?\w+['\"]?\s*=\s*['\"]?\w+",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# 3. 基础校验工具
# ---------------------------------------------------------------------------

def check(condition, message, field=None):
    """条件不成立就抛 :class:`ValidationError`（沿用 backend/errors.py 的异常体系）。"""
    if not condition:
        raise ValidationError(message, field=field)


def reject_sql(value, field):
    """字段值里出现 SQL 语法特征时直接拒绝。

    这是双保险：CampusAI 的查询全部走参数绑定（backend/db.py），
    这里额外拦一道，保证「模型就算吐了 SQL 片段也绝不会被当成查询使用」。
    """
    if isinstance(value, str) and _SQL_RE.search(value):
        raise ValidationError(
            "参数 {} 含疑似 SQL 片段，已拒绝：{!r}".format(field, value[:40]), field=field
        )
    return value


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_int(value, field, minimum=None, maximum=None):
    """整数校验：接受 ``5`` / ``5.0`` / ``"5"``，布尔值不算整数。"""
    if isinstance(value, bool):
        raise ValidationError("参数 {} 必须是整数，实际为布尔值".format(field), field=field)
    if isinstance(value, str):
        value = value.strip()
        if not _NUMBER_RE.match(value):
            raise ValidationError("参数 {} 必须是整数，实际为 {!r}".format(field, value), field=field)
    elif _is_number(value) and float(value).is_integer():
        value = int(value)
    elif not isinstance(value, int):
        raise ValidationError("参数 {} 必须是整数，实际为 {!r}".format(field, value), field=field)

    number = int(value)
    if minimum is not None:
        check(number >= minimum, "参数 {} 必须 >= {}".format(field, minimum), field)
    if maximum is not None:
        check(number <= maximum, "参数 {} 必须 <= {}".format(field, maximum), field)
    return number


def check_bool(value, field):
    """布尔校验：接受 bool 与 ``"true"/"1"/"需要"`` 等常见写法，返回真正的 ``bool``。"""
    if isinstance(value, bool):
        return value
    if _is_number(value):
        if int(value) in (0, 1):
            return bool(int(value))
        raise ValidationError("参数 {} 只能是 true/false，实际为 {!r}".format(field, value), field=field)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
    raise ValidationError("参数 {} 只能是 true/false，实际为 {!r}".format(field, value), field=field)


def check_text(value, field, max_length=TEXT_MAX_LENGTH, choices=None):
    """文本校验：必须是非空字符串、长度受限、必要时命中枚举白名单。"""
    check(isinstance(value, str), "参数 {} 必须是字符串，实际为 {!r}".format(field, value), field)
    text = value.strip()
    check(bool(text), "参数 {} 不能为空字符串".format(field), field)
    check(
        len(text) <= max_length,
        "参数 {} 最长 {} 个字符，实际 {}".format(field, max_length, len(text)),
        field,
    )
    if choices is not None:
        check(
            text in choices,
            "参数 {} 的取值必须是 {} 之一，实际为 {!r}".format(field, "/".join(choices), text),
            field,
        )
    return reject_sql(text, field)


def check_date(value, field):
    """日期校验：``YYYY-MM-DD`` 且必须真实存在（``2025-02-30`` 会被拒）。"""
    import datetime

    text = check_text(value, field, max_length=10)
    check(
        bool(_DATE_RE.match(text)),
        "参数 {} 必须是 YYYY-MM-DD 格式，实际为 {!r}".format(field, text),
        field,
    )
    year, month, day = (int(part) for part in text.split("-"))
    try:
        datetime.date(year, month, day)
    except ValueError:
        raise ValidationError("参数 {} 不是有效日期：{}".format(field, text), field=field)
    return text


def _coerce(kind, value, field, rules):
    """按字段规格校验单个值。"""
    if kind == "int":
        return check_int(value, field, rules.get("minimum"), rules.get("maximum"))
    if kind == "bool":
        return check_bool(value, field)
    if kind == "date":
        return check_date(value, field)
    if kind == "text":
        return check_text(value, field, rules.get("max_length", TEXT_MAX_LENGTH))
    if kind == "enum":
        return check_text(value, field, max_length=50, choices=rules["choices"])
    raise ValidationError("校验器内部错误：未知字段类型 {}".format(kind))  # pragma: no cover


# ---------------------------------------------------------------------------
# 4. 归一化 + 校验
# ---------------------------------------------------------------------------

def normalize(payload):
    """把模型返回的 JSON 拍平成 ``{字段: 值}``。

    兼容三种常见写法（都在同一次调用里兜住，避免因为多一层包装就整条失败）：
        ``{"intent": "room_search", "capacity": 8}``          → 扁平
        ``{"intent": "room_search", "params": {...}}``        → 参数在 params/parameters/fields/slots/args
        ``{"intent": "room_search", "entities": {...}}``      → 参数在 entities

    另外做两件小事：字段名统一小写、下划线；值为 ``None`` 或空串的字段丢弃
    （「没提取到」与「提取到空值」在下游是同一种语义）。
    """
    check(isinstance(payload, dict), "AI 返回的 JSON 必须是对象（object）")

    intent = payload.get("intent", payload.get("name", payload.get("type")))
    check(isinstance(intent, str) and intent.strip(), "AI 返回的 JSON 缺少 intent 字段")
    intent = intent.strip().lower()
    reject_sql(intent, "intent")

    body = {}
    for key in ("params", "parameters", "fields", "slots", "args", "entities"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            body.update(nested)

    for key, value in payload.items():
        if key in ("intent", "name", "type", "params", "parameters", "fields", "slots", "args", "entities"):
            continue
        if isinstance(value, dict):
            continue  # 嵌套对象（例如模型自带的 explanation）不是业务参数
        body[key] = value

    params = {}
    for key, value in body.items():
        if not isinstance(key, str) or not key.strip():
            continue
        field = key.strip().lower()
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        params[field] = value
    return intent, params


def _check_required(intent, params):
    """必填字段校验（目前只有产教融合的两个意图需要 major）。"""
    for field, rules in SCHEMAS[intent].items():
        if rules.get("required") and field not in params:
            raise ValidationError("意图 {} 缺少必填参数 {}".format(intent, field), field=field)


def _canonical_field(intent, field):
    """把字段名收敛成该意图的规范名；规范名已在白名单里则原样返回。

    只做「别名 → 规范名」的机械替换，不认识的名字原样返回，交给调用方报错。
    """
    schema = SCHEMAS[intent]
    if field in schema:
        return field
    # 最多跟随 3 跳，避免别名成环时死循环。
    for _ in range(3):
        field = INTENT_ALIASES.get(intent, {}).get(field, ALIASES.get(field, field))
        if field in schema:
            return field
    return field


def _check_pairs(intent, params):
    """互相约束的字段：节次区间、日期区间、预测目标日期。"""
    start = params.get("period_start")
    end = params.get("period_end")
    if start is not None and end is not None and end < start:
        raise ValidationError(
            "参数 period_end ({}) 不能小于 period_start ({})".format(end, start),
            field="period_end",
        )
    date_from = params.get("date_from")
    date_to = params.get("date_to")
    if date_from is not None and date_to is not None and date_to < date_from:
        raise ValidationError(
            "参数 date_to ({}) 不能早于 date_from ({})".format(date_to, date_from),
            field="date_to",
        )
    check(
        not (intent == "canteen_forecast" and "target_date" in params and date_to is not None),
        "预测意图不能同时出现 target_date 与 date_to",
        "target_date",
    )


def validate_intent(payload, warnings=None):
    """校验 AI 返回的意图 JSON。

    返回 ``(intent, params)``；任何问题都抛 :class:`ValidationError`：

    >>> validate_intent({"intent": "room_search", "weekday": 3, "capacity": 8})
    ('room_search', {'weekday': 3, 'capacity': 8})
    >>> validate_intent({"intent": "hack", "sql": "DROP TABLE"})  # doctest: +SKIP
    ValidationError: ...

    ``warnings`` 传入一个 list 时，会写入「不能沉默丢弃」的提示（例如模型识别出
    了意图、但某个筛选条件平台表达不了）。此时**保留 intent**、只丢掉那个条件，
    由上层决定是提示用户还是降级 —— 而不是把整个意图判成 unknown
    （历史 bug：「有没有带空调的空教室」被降级成 unknown，用户连意图都看不到了）。

    规则：
        1. intent 必须命中白名单（白名单外一律拒绝，不做任何猜测）
        2. 字段必须在该意图的参数白名单内；不认识的字段被丢弃并记入 ``warnings``
        3. 字段类型/范围按 :data:`SCHEMAS` 校验；字符串不得含 SQL 片段
        4. ``unknown`` 不允许携带任何业务参数
        5. ``room_search`` 至少要有一个筛选条件，否则记入 ``warnings`` 而不是报错
           （调用方确实明确说了要「找教室」，只是条件平台支持不了）
    """
    intent, params = normalize(payload)

    check(intent in INTENTS, "不支持的 intent：{!r}（白名单：{}）".format(intent, "/".join(INTENTS)))
    schema = SCHEMAS[intent]

    if intent == "unknown":
        check(not params, "unknown 意图不允许携带业务参数：{}".format(sorted(params)))

    _check_required(intent, params)

    cleaned = {}
    unsupported = []
    for raw_field, value in params.items():
        field = _canonical_field(intent, raw_field)
        rules = schema.get(field)
        if rules is None:
            # 白名单外的字段：SQL 片段仍然要挡（安全红线不放宽）。
            reject_sql(value, raw_field)
            unsupported.append(field)
            continue
        # 同一字段既给了别名又给了规范名时，以规范名为准（先出现的别名胜出）。
        if field in cleaned and field != raw_field:
            continue
        if field == "weekday" and isinstance(value, str):
            word = value.strip().lower()
            if word in _WEEKDAY_WORDS:
                cleaned[field] = _WEEKDAY_WORDS[word]
                continue
        cleaned[field] = _coerce(rules["kind"], value, field, rules)

    _check_pairs(intent, cleaned)

    if warnings is not None:
        for name in unsupported:
            warnings.append(
                "意图 {} 不支持参数 {}，已丢弃该条件".format(intent, name)
            )
        if intent == "room_search" and not cleaned:
            warnings.append(
                "识别为 room_search，但没有一个平台支持的筛选条件"
                "（人数/时段/设备/楼栋/教室类型）"
            )

    if intent == "room_search" and not cleaned and not unsupported:
        # 模型只回了一个空对象：没有意图证据，也没有任何条件，属于无效返回。
        raise ValidationError("room_search 至少需要一个筛选条件（人数/时段/设备/楼栋/教室类型）")

    return intent, cleaned


# 向后兼容的小别名：下游写成 ``validate(...)`` 也读得懂。
validate = validate_intent


# ---------------------------------------------------------------------------
# 5. 给 AI 的「校验契约」说明
# ---------------------------------------------------------------------------

def render_contract():
    """渲染字段契约文本，拼到 system prompt 后面。

    目的：prompts/intent_parser.md 是正式 Prompt（改动要走 Git 留痕），
    而字段白名单会随业务增长；把校验规则自动渲染出来，可以避免
    「Prompt 里没写、Python 又不认」的字段漂移，两边永远一致。
    """
    lines = [
        "",
        "## Python 校验契约（自动生成，务必遵守）",
        "",
        "intent 只能是：" + ", ".join(INTENTS),
        "每个 intent 允许的字段（不在表内的字段一律不要输出）：",
    ]
    for intent in INTENTS:
        schema = SCHEMAS[intent]
        if not schema:
            lines.append("- {}: 不输出任何字段".format(intent))
            continue
        parts = []
        for field in sorted(schema):
            rules = schema[field]
            detail = _describe_rules(rules)
            if rules.get("required"):
                detail += "，必填"
            parts.append("{} ({})".format(field, detail))
        lines.append("- {}: {}".format(intent, "; ".join(parts)))
    lines.extend(
        [
            "规则：",
            "1. 只输出 JSON 对象，不要解释文字、不要 Markdown 代码块。",
            "2. 无法判断时输出 {\"intent\": \"unknown\"}，并且不要输出任何业务字段。",
            "3. 不要输出 SQL，不要输出字段白名单以外的字段。",
            "4. 没提到的人数、时段、设备一律不输出，不要编造。",
            "5. 日期用 YYYY-MM-DD；星期用 1..7（1=周一）；节次用整数 1..12。",
        ]
    )
    return "\n".join(lines)


def _describe_rules(rules):
    kind = rules["kind"]
    if kind == "int":
        minimum, maximum = rules.get("minimum"), rules.get("maximum")
        if minimum is not None and maximum is not None:
            return "整数 {}..{}".format(minimum, maximum)
        if minimum is not None:
            return "整数 >= {}".format(minimum)
        return "整数"
    if kind == "bool":
        return "true/false"
    if kind == "date":
        return "日期 YYYY-MM-DD"
    if kind == "enum":
        return "枚举 {}".format("/".join(rules["choices"]))
    return "字符串(<= {} 字)".format(rules.get("max_length", TEXT_MAX_LENGTH))
