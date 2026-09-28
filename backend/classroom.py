# -*- coding: utf-8 -*-
"""AI 智能找教室（场景一）—— 完整链路的编排层。

链路：:

    用户输入
      → Intent Parser（backend/ai_parser.py：只识别意图 + 抽参数，不查数据）
      → 参数验证（backend/validate.py 白名单 + 本模块的业务校验）
      → 教室查询（backend/queries.list_available_classrooms：纯 Python + SQLite）
      → 返回候选教室（Python 计算排序与「放宽条件」建议）
      → AI 解释结果（prompts/insight_generator.md：只解释，不查询）

分工与红线（AGENTS.md AI Rules，逐条对应任务的 8 条要求）：

1. AI 不访问数据库：模型只拿到 :func:`build_insight_payload` 生成的**只读事实表**，
   没有连接、没有凭据；模型调用发生在 SQLite 连接关闭之后（见 :func:`search_classrooms`）。
2. AI 不生成 SQL：发给模型的 payload 只有查询结果与中文条件描述；模型返回的解释
   会被 :data:`_SQL_RE` 检查，出现 SQL 片段就整条退回 Python 兜底解释。
3. 教室查询由 Python 完成：唯一查询入口是 ``queries.list_available_classrooms``，
   本模块不写 SQL、不 import sqlite3。
4. 查询参数必须经过校验：:func:`validate_room_params` 走 ``validate.validate_intent``
   白名单 + 类型/范围校验，再补业务规则（例如只给 weekday 不给节次时无法判断空闲），
   非法参数直接抛 ``ValidationError``（HTTP 400）。
5. 结果必须来自 SQLite：``rooms`` / ``total`` / ``summary`` 原样取自 queries 层，
   本模块不做二次拼装或补数据。
6. AI 只能解释查询结果：解释器只接收事实表，输出被收敛成
   ``summary`` / ``highlights`` / ``suggestions`` 三个字段。
7. 查询为空时给出合理提示：:func:`build_hints` **再查一次 SQLite**，给出
   「放宽某个条件后有 N 间」的可执行建议，而不是让模型猜。
8. 测试见 ``tests/test_classroom.py``。
"""

import json
import os
import re

import requests

from . import queries
from .ai_parser import (
    ERROR_NETWORK,
    AIParserError,
    IntentParser,
    extract_json_object,
    load_config,
    load_prompt,
    parse_response_text,
)
from .db import connection, default_db_path
from .errors import NotFoundError, ValidationError
from .validate import validate_intent

# backend/classroom.py → backend/ → 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_INSIGHT_PROMPT_PATH = os.path.join(PROJECT_ROOT, "prompts", "insight_generator.md")

#: 本模块只处理「找教室」这一个意图。
ROOM_INTENT = "room_search"
#: 用户问题长度上限（防止把整段文本塞进 question）。
MAX_QUESTION_LENGTH = 200
#: 未指定时返回的教室条数（与 backend/queries.DEFAULT_LIMIT 一致）。
DEFAULT_RESULT_LIMIT = 10
#: 兜底解释里最多列出几间教室 / 解释文本里每行、每项的长度与条数上限。
MAX_HIGHLIGHT_ROOMS = 5
MAX_INSIGHT_LINE = 200
MAX_INSIGHT_ITEMS = 3
#: 空结果时最多做几次「放宽条件」的补充查询（每次只取 total，limit=1）。
MAX_RELAXATION_PROBES = 4

#: 解释结果里的错误码（供上层判断「AI 不可用」还是「模型返回了危险内容」）。
ERROR_UNSAFE_EXPLANATION = "unsafe_explanation"
ERROR_INVALID_EXPLANATION = "invalid_explanation"

#: SQL 片段特征：只在**模型返回的解释**上做过滤，不用来改写任何查询。
_SQL_RE = re.compile(
    r"\b(select|insert|update|delete|drop|alter|create|attach|detach|pragma|union|truncate|exec)\b"
    r"|--|/\*|\*/",
    re.IGNORECASE,
)

WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

#: 意图 → 中文名，用于「这不是找教室的问题」时的提示。
INTENT_LABELS = {
    "canteen_analysis": "食堂人流分析",
    "canteen_forecast": "食堂拥挤预测",
    "industry_skill_match": "岗位技能匹配",
    "industry_skill_gap": "技能缺口分析",
    "dashboard_query": "看板查询",
    "unknown": "无法识别",
}

#: 意图参数里与「教室空闲查询」无关的字段：教室按 星期 + 节次 定位空闲，不按日期。
_IGNORED_ROOM_FIELDS = ("date", "date_text", "date_range_text")


class ClassroomSearchError(RuntimeError):
    """找教室链路失败（意图解析不可用 / 意图不是找教室）。

    带 ``status_code`` / ``code`` / ``field``，由 ``backend/app.py`` 统一翻译成 JSON。
    """

    def __init__(self, message, code="search_failed", field=None, status_code=400, available=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.field = field
        self.status_code = status_code
        self.available = list(available) if available else []

    def to_dict(self):
        payload = {"code": self.code, "message": self.message}
        if self.field:
            payload["field"] = self.field
        if self.available:
            payload["available"] = self.available
        return payload


# ---------------------------------------------------------------------------
# 1. 输入校验
# ---------------------------------------------------------------------------

def check_question(question):
    """校验用户问题，返回去掉首尾空白的文本。"""
    if not isinstance(question, str):
        raise ValidationError("参数 question 必须是字符串", field="question")
    text = question.strip()
    if not text:
        raise ValidationError("参数 question 不能为空", field="question")
    if len(text) > MAX_QUESTION_LENGTH:
        raise ValidationError(
            "参数 question 最长 {} 个字符，实际 {}".format(MAX_QUESTION_LENGTH, len(text)),
            field="question",
        )
    return text


def _tri_state(value):
    """布尔参数 → SQLite 绑定值：``None`` 不筛选，``True/False`` → ``1/0``。"""
    if value is None:
        return None
    return 1 if value else 0


def validate_room_params(params, warnings=None):
    """校验意图参数，返回 ``(校验后的意图参数, 查询参数)``。

    这里是**第二道校验**（第一道在 ai_parser → validate）：即使调用方直接构造
    ``params`` 也要经过同一套白名单、类型、范围与 SQL 片段检查，再补业务规则：

        - 至少要有一个平台支持的筛选条件（人数/设备/类型/楼栋/星期/节次/学期）
        - 只给 ``weekday`` 不给节次时要报错：只凭「周三」无法判断教室是否空闲
          （查询层会把节次当 ``NULL`` 绑定，静默地不过滤占用，属于危险的假空闲）
        - 节次只给一端时按同一节处理（与 ``validators.period_range`` 的语义一致）
        - 模型编出来的 ``room_id`` 这类字段不在白名单里，会被丢弃并记入 warnings，
          因此 AI 无法直接指定教室（选哪几间教室只能由 SQLite 查询决定）
    """
    if not isinstance(params, dict):
        raise ValidationError("找教室的查询参数必须是对象（object）", field="params")

    body = {key: value for key, value in params.items() if key != "intent"}
    if not body:
        raise ValidationError(
            "缺少查询条件：至少需要 人数/星期/节次/设备/楼栋/教室类型/学期 中的一项",
            field="params",
        )

    intent, cleaned = validate_intent(dict(body, intent=ROOM_INTENT), warnings=warnings)
    if intent != ROOM_INTENT:  # pragma: no cover - validate_intent 保证意图一致
        raise ValidationError("意图 {} 不是找教室".format(intent), field="intent")

    # 日期条件对教室空闲没有意义（空闲按 星期 + 节次 算），显式丢弃并留痕。
    for field in _IGNORED_ROOM_FIELDS:
        if field in cleaned:
            cleaned.pop(field)
            if warnings is not None:
                warnings.append(
                    "教室查询按 星期+节次 定位空闲，参数 {} 无法参与查询，已忽略".format(field)
                )

    if not set(cleaned) - set(_IGNORED_ROOM_FIELDS):
        raise ValidationError(
            "缺少查询条件：至少需要 人数/星期/节次/设备/楼栋/教室类型/学期 中的一项",
            field="params",
        )

    weekday = cleaned.get("weekday")
    period_start = cleaned.get("period_start")
    period_end = cleaned.get("period_end")
    if weekday is not None and period_start is None and period_end is None:
        raise ValidationError(
            "只给出 weekday 无法判断教室是否空闲：请同时给出节次"
            "（period_start/period_end，1..12，例如下午 = 5..8）",
            field="period_start",
        )
    if period_start is None and period_end is not None:
        period_start = period_end
    if period_end is None and period_start is not None:
        period_end = period_start

    query = {
        "capacity": cleaned.get("capacity"),
        "has_projector": _tri_state(cleaned.get("has_projector")),
        "has_computer": _tri_state(cleaned.get("has_computer")),
        "room_type": cleaned.get("room_type"),
        "building": cleaned.get("building"),
        "weekday": weekday,
        "period_start": period_start,
        "period_end": period_end,
        "semester": cleaned.get("semester"),
        # 本接口只回答「哪些教室现在可用」，状态固定为 available。
        "status": "available",
        "limit": cleaned.get("limit") or DEFAULT_RESULT_LIMIT,
    }
    return cleaned, query


def resolve_query_params(params, warnings=None):
    """只要查询参数（:func:`validate_room_params` 的薄封装，供调用方与测试使用）。"""
    return validate_room_params(params, warnings)[1]


# ---------------------------------------------------------------------------
# 2. 结果说明（Python 侧：条件描述、空结果提示、兜底解释）
# ---------------------------------------------------------------------------

def describe_filters(query):
    """把查询参数翻译成中文条件清单（同时给人工核对与 AI 解释用）。"""
    parts = []
    if query.get("capacity"):
        parts.append("至少 {} 人".format(query["capacity"]))
    if query.get("has_projector"):
        parts.append("需要投影")
    if query.get("has_computer"):
        parts.append("需要电脑")
    if query.get("room_type"):
        parts.append("类型：{}".format(query["room_type"]))
    if query.get("building"):
        parts.append("楼栋：{}".format(query["building"]))
    weekday = query.get("weekday")
    if weekday is not None:
        parts.append(
            WEEKDAY_NAMES[weekday - 1] if 1 <= weekday <= 7 else "weekday={}".format(weekday)
        )
    start, end = query.get("period_start"), query.get("period_end")
    if start is not None and end is not None:
        parts.append("第 {} 节".format(start) if start == end else "第 {}-{} 节".format(start, end))
    if query.get("semester"):
        parts.append("学期 {}".format(query["semester"]))
    return parts or ["未加筛选条件"]


def known_limitations(query):
    """把查询层的已知语义缺口显式说明，避免用户误以为条件生效了。"""
    notes = []
    for field, label in (("has_projector", "投影"), ("has_computer", "电脑")):
        if query.get(field) == 0:
            notes.append(
                "已知限制：{}=false 目前不会排除带{}的教室"
                "（查询层按「不筛选」处理，见 tests/test_api.py）。".format(field, label)
            )
    return notes


def truncation_notes(result):
    """``limit`` 截断说明（``total`` 始终是完整命中数）。"""
    shown = len(result["rooms"])
    total = result["total"]
    if total > shown:
        return [
            "共 {} 间符合条件，受 limit={} 限制这里只返回前 {} 间。".format(
                total, result["query"].get("limit"), shown
            )
        ]
    return []


def _probe_total(conn, params):
    """放宽条件后再查一次 SQLite。只要 1 条就能拿到 ``total``，查不动就返回 0。"""
    try:
        return queries.list_available_classrooms(conn, dict(params, limit=1))["total"]
    except (ValidationError, NotFoundError):
        # 宽松条件本身不合法（例如学期缺失）：这条建议不成立，安静跳过。
        return 0


def _render(template, **values):
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value))
    return template


def _relaxation_probes(base):
    """构造「放宽一个条件」的候选查询（值全部由 Python 决定，不来自模型）。"""
    probes = []
    if base.get("has_projector") or base.get("has_computer"):
        probes.append(
            (
                dict(base, has_projector=None, has_computer=None),
                "放宽设备要求（不要求投影/电脑）后，同一时段有 {count} 间空教室。",
            )
        )
    if base.get("capacity"):
        probes.append(
            (
                dict(base, capacity=None),
                "不限制人数时，同一时段有 {count} 间空教室，但可能坐不下 {capacity} 人。",
            )
        )
    if base.get("room_type"):
        probes.append(
            (
                dict(base, room_type=None),
                "不限教室类型时，同一时段有 {count} 间空教室。",
            )
        )
    if base.get("building"):
        probes.append(
            (
                dict(base, building=None),
                "不限楼栋时，同一时段有 {count} 间空教室。",
            )
        )
    start, end = base.get("period_start"), base.get("period_end")
    if base.get("weekday") and start and end:
        if start > 1:
            probes.append(
                (
                    dict(base, period_start=start - 1, period_end=end - 1),
                    "往前挪一节（第 {a}-{b} 节）有 {count} 间空教室。",
                )
            )
        if end < 12:
            probes.append(
                (
                    dict(base, period_start=start + 1, period_end=end + 1),
                    "往后挪一节（第 {a}-{b} 节）有 {count} 间空教室。",
                )
            )
    return probes


def build_hints(conn, result):
    """空结果提示：每条都来自**再查一次 SQLite**，不是让 AI 猜。"""
    if result.get("total"):
        return []

    base = {key: value for key, value in result["query"].items() if key != "limit"}
    hints = []
    for params, template in _relaxation_probes(base):
        if len(hints) >= MAX_RELAXATION_PROBES:
            break
        count = _probe_total(conn, params)
        if count:
            hints.append(
                _render(
                    template,
                    count=count,
                    capacity=base.get("capacity"),
                    a=params.get("period_start"),
                    b=params.get("period_end"),
                )
            )
    if not hints:
        hints.append(
            "当前条件下全校没有空闲教室：可以换一天/换节次，"
            "或降低要求（人数、投影/电脑、教室类型、楼栋）。"
        )
    return hints


def _room_line(room):
    floor = room["floor"] if room["floor"] is not None else "-"
    devices = [
        "有投影" if room["has_projector"] else "无投影",
        "有电脑" if room["has_computer"] else "无电脑",
    ]
    return "{}（{} {} 层，可坐 {} 人，{}，{}）".format(
        room["room_name"],
        room["building"],
        floor,
        room["capacity"],
        "、".join(devices),
        room["room_type"],
    )


def _render_explanation(summary, highlights, suggestions, source, error=None):
    """统一解释结构：一个摘要 + 少量要点 + 建议（AI 与兜底共用）。"""
    lines = [summary]
    lines.extend("• " + item for item in highlights)
    if suggestions:
        lines.append("建议：" + "；".join(suggestions))
    return {
        "text": "\n".join(lines),
        "summary": summary,
        "highlights": list(highlights),
        "suggestions": list(suggestions),
        "source": source,
        "error": error,
    }


def local_explanation(result, hints=()):
    """Python 兜底解释：AI 不可用或输出不合法时使用，仍然是「只解释结果」。"""
    filters = "、".join(describe_filters(result["query"]))
    total = result["total"]
    rooms = result["rooms"]

    if not total:
        summary = "按 {} 查询，没有找到满足条件的空教室（共 0 间）。".format(filters)
        return _render_explanation(
            summary,
            [],
            list(hints) or ["换一天/换节次，或放宽人数、设备、教室类型等要求。"],
            source="local",
        )

    summary = "按 {} 查询，找到 {} 间空教室。".format(filters, total)
    highlights = [_room_line(room) for room in rooms[:MAX_HIGHLIGHT_ROOMS]]
    if total > len(rooms):
        highlights.append("共 {} 间，这里只列出前 {} 间。".format(total, len(rooms)))
    return _render_explanation(
        summary,
        highlights,
        ["结果按「刚好够用」排序：容量越接近需求的教室越靠前。"],
        source="local",
    )


# ---------------------------------------------------------------------------
# 3. AI 结果解释器（只解释，不查询）
# ---------------------------------------------------------------------------

def build_insight_payload(question, result, hints=(), notes=()):
    """构造交给模型的「只读事实表」。

    只包含已查好的结果与中文条件描述：**没有 SQL、没有表名、没有数据库路径**
    （要求 1/2/5/6），因此模型即使想查数据也无从下手。
    """
    return {
        "question": question,
        "filters": describe_filters(result["query"]),
        "total": result["total"],
        "rooms": [
            {
                "room_name": room["room_name"],
                "building": room["building"],
                "floor": room["floor"],
                "capacity": room["capacity"],
                "has_projector": room["has_projector"],
                "has_computer": room["has_computer"],
                "room_type": room["room_type"],
            }
            for room in result["rooms"]
        ],
        "shown": len(result["rooms"]),
        "hint": list(hints),
        "notes": list(notes),
    }


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


class ResultExplainer:
    """AI 结果解释器：把「已经查好的结果」讲成人话。

    与 :class:`backend.ai_parser.IntentParser` 一样的约定：配置从 ``.env`` 读、
    Prompt 从 ``prompts/`` 读（正式 Prompt 走 Git 留痕）、Key 不写进代码。
    区别是它**只能看到查询结果**：没有 SQL、没有连接、没有查询入口。
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
        """system prompt = ``prompts/insight_generator.md``（含 Markdown 转义还原）。"""
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
        """解释查询结果，返回统一解释结构（``source = "ai"``）。"""
        return _compose_explanation(extract_json_object(self.call_api(payload)), source="ai")


def explain_result(question, result, hints=(), notes=(), explainer=None):
    """AI 解释结果；任何失败都退回 Python 兜底解释，绝不让整条链路失败。"""
    payload = build_insight_payload(question, result, hints, notes)
    try:
        instance = explainer if explainer is not None else ResultExplainer()
        explanation = instance.explain(payload)
        explanation["error"] = None
        return explanation
    except Exception as exc:  # AI 不可用 / 返回不合法：降级但不能 500
        fallback = local_explanation(result, hints)
        fallback["error"] = (
            exc.to_dict() if hasattr(exc, "to_dict") else {"code": "explain_failed", "message": str(exc)}
        )
        return fallback


# ---------------------------------------------------------------------------
# 4. 完整链路
# ---------------------------------------------------------------------------

def _parse_question(question, parser=None):
    """调意图解析器并检查意图；失败抛 :class:`ClassroomSearchError`。"""
    instance = parser if parser is not None else IntentParser()
    try:
        parsed = instance.parse(question, use_fallback=True)
    except Exception as exc:
        raise ClassroomSearchError(
            "AI 意图解析失败：{}".format(exc), code="ai_unavailable", status_code=503
        )
    if not isinstance(parsed, dict):  # pragma: no cover - 防御性：解析器契约被破坏
        raise ClassroomSearchError("意图解析器返回了非对象结果", code="ai_unavailable", status_code=503)

    if parsed.get("degraded"):
        error = parsed.get("error") or {}
        raise ClassroomSearchError(
            "AI 意图解析不可用（{}）：{}".format(
                error.get("code", "ai_error"), error.get("message", "未知原因")
            ),
            code="ai_unavailable",
            status_code=503,
        )

    intent = parsed.get("intent")
    if intent != ROOM_INTENT:
        raise ClassroomSearchError(
            _unsupported_intent_message(intent),
            code="unsupported_intent",
            status_code=400,
            field="intent",
        )
    return parsed


def _unsupported_intent_message(intent):
    if intent == "unknown" or not intent:
        return (
            "没有识别出找教室的条件（intent=unknown）：请说明人数、星期/节次、"
            "设备（投影/电脑）、楼栋或教室类型中的至少一项"
        )
    label = INTENT_LABELS.get(intent, intent)
    return "本接口只负责找教室（room_search），本次问题识别为{}（{}）".format(label, intent)


def search_classrooms(question, db_path=None, parser=None, explainer=None, explain=True,
                      probe_relaxations=True):
    """AI 智能找教室的完整链路（本模块唯一对外入口）。

    参数：
        question            用户自然语言问题（例如「周三下午帮我找一个8人、有投影的空教室」）
        db_path             SQLite 文件路径（默认 ``data/campus.db``，可用 ``CAMPUS_DB_PATH`` 覆盖）
        parser              意图解析器（默认 :class:`IntentParser`，测试可注入假实现）
        explainer           结果解释器（默认 :class:`ResultExplainer`，仅在 ``explain=True`` 时构造）
        explain             ``False`` 时跳过 AI 解释，直接用 Python 兜底文案（测试/离线可用）
        probe_relaxations   ``False`` 时不做「放宽条件」的补充查询

    返回 ``dict``（JSON 可序列化）：

        {
          "question", "intent", "params",      # 用户问题 + 校验后的意图参数
          "query", "total", "summary", "rooms",  # 原样来自 queries 层（SQLite）
          "hint", "notes",                     # Python 生成的提示与已知限制
          "explanation": {"text", "source", "error"}
        }

    异常：
        :class:`~backend.errors.ValidationError`  参数非法（HTTP 400）
        :class:`ClassroomSearchError`             AI 不可用（503）/ 意图不是找教室（400）
    """
    question = check_question(question)

    parsed = _parse_question(question, parser)
    notes = list(parsed.get("warnings") or [])
    params, query_params = validate_room_params(parsed.get("params") or {}, warnings=notes)

    # 查询阶段：只碰 Python + SQLite，模型完全不参与。
    with connection(db_path or default_db_path()) as conn:
        result = queries.list_available_classrooms(conn, query_params)
        hints = (
            build_hints(conn, result)
            if probe_relaxations and not result["total"]
            else []
        )

    notes.extend(known_limitations(result["query"]))
    notes.extend(truncation_notes(result))

    # 解释阶段：连接已关闭，模型只能看到事实表，没有任何查询能力。
    explanation = (
        explain_result(question, result, hints, notes, explainer=explainer)
        if explain
        else local_explanation(result, hints)
    )

    return {
        "question": question,
        "intent": ROOM_INTENT,
        "params": params,
        "query": result["query"],
        "total": result["total"],
        "summary": result["summary"],
        "rooms": result["rooms"],
        "hint": hints,
        "notes": notes,
        "explanation": explanation,
    }
