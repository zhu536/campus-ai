# -*- coding: utf-8 -*-
"""相对日期解析：把「今天 / 上周一 / 最近七天 / 下周三」变成确定的 ``YYYY-MM-DD``。

为什么必须由 Python 做（而不是让模型直接算）：
    模型**不知道今天是几号**，也没有数据的日期范围。让它自己把「上周一」算成
    ``2025-09-08`` 只会让它编日期（比丢弃更危险）。所以这里把时间解析做成
    确定性的纯函数，锚点由调用方显式注入，测试可以冻结锚点、不依赖真实日历。

锚点由用户拍板（见项目决策）：
    1. 「今天 / 昨天 / 上周X / 下周三」以**运行脚本的计算机系统当天日期**
       （``datetime.date.today()``）为基准。
    2. 模糊的「最近 / 这几天」表示**历史数据里的最近 N 天**（默认 7 天），
       因此它以 ``data_max_date``（数据里最大日期）为基准，而不是今天。
       这样在数据没有覆盖到今天的现实下，「最近七天」查的是真正有数据的最近一周，
       而不是一个必然为空的区间。

本模块是**纯函数 + 无 I/O**：不连数据库、不读系统时钟（``today`` 由调用方传入），
因此可以离线单测。取数据库最大日期的能力在 :func:`default_anchor`，
仅在未显式传入锚点时才使用，由 backend/ai_parser.py 负责调用。

字段约定（与 backend/validate.py 的别名表一致）：
    ``date_text``       —— 单个时间点，如「今天」「下周三」
    ``date_range_text`` —— 时间区间，如「最近七天」「本周」
    ``N天前``           —— 反向相对天数
"""

import datetime
import os
import re
from dataclasses import dataclass

#: 「最近 / 这几天」默认取多少天（用户决策：历史数据里的最近 7 天）。
DEFAULT_RECENT_DAYS = 7

#: 解析结果最多往前/往后多少天，防止模型或输入给出荒谬的偏移。
MAX_OFFSET_DAYS = 366

#: 时间原文的长度上限（超过就忽略，避免把整段问题当日期表达式）。
_TEXT_MAX_LENGTH = 30

#: 中文数字 → 整数（够用即可：日期表达不会出现「廿」「卅」这类写法）。
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}

#: 星期写法 → 1..7（1=周一，与 ValidateError 的 weekday 规范一致）。
_WEEKDAY_WORDS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7,
}

#: 「今天/昨天/明天」这类以系统当天为基准的偏移。
#: **顺序敏感**：必须长的写法在前，否则「大后天」会被「后天」先匹配到（子串命中）。
_DAY_OFFSETS = (
    ("大后天", 3), ("后天", 2), ("明天", 1), ("明日", 1),
    ("今天", 0), ("今日", 0), ("本日", 0), ("当天", 0),
    ("昨天", -1), ("昨日", -1), ("前天", -2),
)

#: 数量 + 单位的时间区间。
#: 两个负向先行断言很关键：
#:   1. ``(?![周星期月])``：前缀「这/前」不带数量时不能吞掉「这周」「前月」这类整段时间
#:      （「这周」要按整周区间处理，而不是「最近 1 周」）。
#:   2. 末尾 ``(?![一二三四五六日天])``：否则「本周三」会被当成「本周」区间，
#:      而它其实是「本周的周三」这个时间点。
_RANGE_RE = re.compile(
    r"(?:最近|近|这(?!\s*(?:周|星期|月))|前(?!\s*(?:周|星期|月)))\s*"
    r"([0-9]+|[零〇一二两三四五六七八九十]+)?\s*"
    r"(天|日|周|星期|个?月)(?![一二三四五六日天])"
)
_AGO_RE = re.compile(r"([0-9]+|[零〇一二两三四五六七八九十]+)\s*(天|日)\s*前")
_WEEK_RE = re.compile(r"(上上|下下|上|本|这|下)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天])")
_VAGUE_RE = re.compile(r"最近|这几天|近期|近来|这段时间")

#: 光说「本周 / 上周 / 下周」而不指星期几 —— 表示整周区间（周一..周日）。
_BARE_WEEK_RE = re.compile(r"(上上|下下|上|本|这|下)?\s*(?:周|星期|礼拜)(?![一二三四五六日天])")

#: 「前几天」这类没有明确数量的模糊写法 → 按默认天数处理（不能当成 1 天）。
_VAGUE_PREFIXES = ("最近", "近", "这", "前")


@dataclass(frozen=True)
class Anchor:
    """时间解析的基准。

    Attributes:
        today: 系统当天日期（用户决策：相对日期以此为准）。
        data_max_date: 历史数据里的最大日期；模糊的「最近」以此为准。
            为 ``None`` 时退化为 ``today``。
    """

    today: datetime.date
    data_max_date: datetime.date = None

    @property
    def recent_base(self):
        """「最近 N 天」的右端点：优先数据最大日期。"""
        return self.data_max_date or self.today


def to_halfwidth(text):
    """全角数字/符号 → 半角（``近７天`` → ``近7天``），并去掉空白。"""
    if not isinstance(text, str):
        return ""
    out = []
    for char in text:
        code = ord(char)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(char)
    return "".join(out).replace(" ", "").replace("\t", "")


def parse_cn_number(text):
    """中文数字 → 整数；``"十"``→10、``"十五"``→15、``"二十"``→20。识别不了返回 None。"""
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(text) == 1:
        return _CN_DIGITS.get(text)
    # 「一五」这类逐位写法（极少见，但兜一下）
    digits = [_CN_DIGITS.get(char) for char in text]
    if any(digit is None for digit in digits):
        return None
    return int("".join(str(digit) for digit in digits))


def _week_monday(day):
    """返回 ``day`` 所在周的周一。"""
    return day - datetime.timedelta(days=day.weekday())


def _clamp_offset(days):
    """把偏移限制在合理范围内；超出返回 ``None``（宁可解析失败也不要荒谬日期）。"""
    if days is None or abs(days) > MAX_OFFSET_DAYS:
        return None
    return days


def _weekday_date(anchor_day, week_word, weekday):
    """按「上周X / 本周X / 下周X」求具体日期。

    ``week_word`` 为 ``None`` 时表示只说「周三」：取**不早于当天**的那个周三
    （即最近一个未过去的周三），这是用户口语里最常见的理解。
    """
    monday = _week_monday(anchor_day)
    if week_word in (None, "", "本", "这"):
        target = monday + datetime.timedelta(days=weekday - 1)
        if week_word is None and target < anchor_day:
            target += datetime.timedelta(days=7)
        return target
    if week_word in ("上", "上上"):
        return monday - datetime.timedelta(days=7 if week_word == "上" else 14) + datetime.timedelta(days=weekday - 1)
    if week_word in ("下", "下下"):
        return monday + datetime.timedelta(days=7 if week_word == "下" else 14) + datetime.timedelta(days=weekday - 1)
    return None


def resolve_date_text(text, anchor):
    """把单个时间点表达式解析成 ``datetime.date``；解析不了返回 ``None``。

    支持：今天/昨天/前天/明天/后天/大后天、上周三/本周三/下周三/周三、
    N天前、前天等。
    """
    normalized = to_halfwidth(text)
    if not normalized:
        return None

    for word, offset in _DAY_OFFSETS:
        if word in normalized:
            days = _clamp_offset(offset)
            return None if days is None else anchor.today + datetime.timedelta(days=days)

    ago = _AGO_RE.search(normalized)
    if ago:
        count = parse_cn_number(ago.group(1))
        if count is not None and count >= 0:
            days = _clamp_offset(-count)
            return None if days is None else anchor.today + datetime.timedelta(days=days)

    week = _WEEK_RE.search(normalized)
    if week:
        weekday = _WEEKDAY_WORDS.get(week.group(2))
        if weekday is not None:
            return _weekday_date(anchor.today, week.group(1), weekday)

    # 模糊的「最近」在单点上没有意义（它表示区间），交给 resolve_range_text。
    return None


def resolve_range_text(text, anchor):
    """把时间区间表达式解析成 ``(date_from, date_to)``；解析不了返回 ``(None, None)``。

    「最近七天 / 近7天 / 最近一周」= 历史数据里的最近 N 天（以 ``data_max_date`` 为准）。
    「最近 / 这几天」= 默认最近 :data:`DEFAULT_RECENT_DAYS` 天。
    """
    normalized = to_halfwidth(text)
    if not normalized:
        return None, None

    match = _RANGE_RE.search(normalized)
    if match:
        raw_number, unit = match.group(1), match.group(2)
        base = anchor.recent_base
        # 「前几天」「近几天」这种没有数量的写法按默认天数算，不能当成 1 天。
        if not raw_number:
            days = DEFAULT_RECENT_DAYS
        else:
            count = parse_cn_number(raw_number)
            if count is None or count < 1:
                return None, None
            if unit in ("周", "星期"):
                days = count * 7
            elif unit.endswith("月"):
                days = count * 30
            else:
                days = count
        if days > MAX_OFFSET_DAYS:
            return None, None
        return base - datetime.timedelta(days=days - 1), base

    if _VAGUE_RE.search(normalized):
        base = anchor.recent_base
        return base - datetime.timedelta(days=DEFAULT_RECENT_DAYS - 1), base

    # 「本周 / 上周 / 下周」这类整周说法（不带星期几）。
    week = resolve_bare_week(text, anchor)
    if week[0] is not None:
        return week

    # 不是区间写法，但可能是单点（例如「上周一」被放进范围字段）
    single = resolve_date_text(text, anchor)
    if single is not None:
        return single, single
    return None, None


def resolve_bare_week(text, anchor):
    """「本周 / 这周 / 上周 / 下周」（不带星期几）→ 该周的 (周一, 周日)。

    这类说法在实践中很常见（「本周食堂晚餐的排队情况怎么样？」），
    如果解析不出来就会丢掉整个时间范围，所以按整周区间处理。
    基准是系统当天；解析不了返回 ``(None, None)``。
    """
    normalized = to_halfwidth(text)
    if not normalized:
        return None, None
    match = _BARE_WEEK_RE.search(normalized)
    if not match:
        return None, None
    monday = _week_monday(anchor.today)
    word = match.group(1)
    if word in ("上", "上上"):
        monday -= datetime.timedelta(days=7 if word == "上" else 14)
    elif word in ("下", "下下"):
        monday += datetime.timedelta(days=7 if word == "下" else 14)
    return monday, monday + datetime.timedelta(days=6)


def guess_expression(question, anchor):
    """从问题原文里兜底抽取时间表达（模型没给 ``*_text`` 字段时用）。

    只认**明确无歧义**的写法；认不出来返回 ``(None, None)``，绝不猜。
    """
    normalized = to_halfwidth(question)
    if not normalized:
        return None, None

    match = _RANGE_RE.search(normalized)
    if match:
        return "range", match.group(0)

    ago = _AGO_RE.search(normalized)
    if ago:
        return "point", ago.group(0)

    week = _WEEK_RE.search(normalized)
    if week:
        return "point", week.group(0)

    bare_week = _BARE_WEEK_RE.search(normalized)
    if bare_week:
        return "range", bare_week.group(0)

    for word, _offset in _DAY_OFFSETS:
        if word in normalized:
            return "point", word

    if _VAGUE_RE.search(normalized):
        return "range", "最近"
    return None, None


#: 意图 → 单个时间点落到哪个规范日期字段。
_POINT_FIELD = {
    "room_search": "date",
    "canteen_analysis": "date_from",
    "canteen_forecast": "target_date",
    "dashboard_query": "date_from",
}

#: 意图 → 一旦这些字段已经存在，就说明模型已经把时间抽出来了，兜底解析必须让位。
#: 例如模型给出 ``weekday=3`` 时不该再补一个 ``date``，否则同一个「周三」出现两次。
_ALREADY_RESOLVED = {
    "room_search": ("date", "weekday", "date_text"),
    "canteen_analysis": ("date_from", "date_to", "date_range_text"),
    "canteen_forecast": ("target_date", "horizon_days", "date_text"),
    "dashboard_query": ("date_from", "date_to", "date_range_text"),
}


def _check_weekday_consistency(params, day, messages):
    """模型同时给了 weekday 和相对日期时，核对两者是否指向同一星期。

    例如「明天」解析成周日，模型却写了 ``weekday=3``，这两个条件互相矛盾；
    查询层读的是 weekday，用户拿到的结果就会莫名其妙。这里只**报告**不一致，
    不擅自改写模型给的 weekday（改哪个都可能改错）。
    """
    weekday = params.get("weekday")
    if not isinstance(weekday, int) or isinstance(weekday, bool):
        return
    if not 1 <= weekday <= 7:
        return
    if weekday != day.isoweekday():
        messages.append(
            "weekday={} 与解析出的日期 {}（星期{}）不一致，两个条件互相矛盾".format(
                weekday, day.isoformat(), day.isoweekday()
            )
        )


def resolve(intent, params, anchor, question=None, warnings=None):
    """把意图参数里的时间**原文**替换成确定的日期。

    输入 ``params`` 不会被就地修改，返回一份新的 dict。处理两件事：

    1. 模型明确给出的 ``date_text`` / ``date_range_text`` → 具体日期
    2. 模型没给原文、但问题里有明确时间表达 → 兜底解析
       （``question`` 由调用方传入；这是「模型偶尔漏抽」的保险，不是主路径）

    解析不出来的 ``*_text`` 字段一律删除：``date_text`` 不是合法日期字段，
    留着会被 ``validate_intent`` 当成未知字段。
    """
    resolved = dict(params)
    messages = warnings if warnings is not None else []

    if intent == "unknown":
        # 无法识别意图时不做任何日期推导，避免给无关问题塞时间条件。
        return _drop_text_fields(resolved)

    point_text = resolved.pop("date_text", None)
    range_text = resolved.pop("date_range_text", None)
    for name, value in (("date_text", point_text), ("date_range_text", range_text)):
        if value is not None and (not isinstance(value, str) or len(value) > _TEXT_MAX_LENGTH):
            messages.append("时间表达 {} 非法或过长，已忽略".format(name))
            if name == "date_text":
                point_text = None
            else:
                range_text = None

    already = _ALREADY_RESOLVED.get(intent, ())
    if question and not any(name in resolved for name in already):
        kind, expression = guess_expression(question, anchor)
        if expression:
            if kind == "range":
                range_text = expression
            else:
                point_text = expression

    if point_text:
        day = resolve_date_text(point_text, anchor)
        if day is not None:
            field = _POINT_FIELD.get(intent)
            if field and field not in resolved:
                resolved[field] = day.isoformat()
            _check_weekday_consistency(resolved, day, messages)
        else:
            messages.append("无法解析时间表达：{}".format(point_text))

    if range_text:
        date_from, date_to = resolve_range_text(range_text, anchor)
        if date_from is not None:
            resolved.setdefault("date_from", date_from.isoformat())
            resolved.setdefault("date_to", date_to.isoformat())
        else:
            messages.append("无法解析时间区间：{}".format(range_text))

    return resolved


def _drop_text_fields(params):
    """删掉所有「待解析时间原文」字段（unknown 分支用）。"""
    return {
        key: value
        for key, value in params.items()
        if key not in ("date_text", "date_range_text")
    }


def read_data_max_date(db_path=None):
    """读取历史数据的最大日期（``meal_records.record_date`` 的 MAX）。

    只读、只查一个值；出错返回 ``None`` 并退化为「以今天为基准」。
    这是本模块唯一的 I/O，且只在调用方没有显式注入锚点时才会用到。
    """
    import sqlite3

    from .db import default_db_path

    path = db_path or default_db_path()
    if not os.path.isfile(path):
        # 不主动创建空库文件（sqlite3.connect 会创建），否则会留下垃圾文件。
        return None
    try:
        connection = sqlite3.connect(path, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute("SELECT MAX(record_date) FROM meal_records").fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if not row or not row[0]:
        return None
    try:
        return datetime.date.fromisoformat(str(row[0]))
    except ValueError:
        return None


def default_anchor(today=None, data_max_date=None):
    """构造锚点：``today`` 默认取系统当天，``data_max_date`` 默认查库。"""
    if today is None:
        today = datetime.date.today()
    if data_max_date is None:
        data_max_date = read_data_max_date()
    return Anchor(today=today, data_max_date=data_max_date)
