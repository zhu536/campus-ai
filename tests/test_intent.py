# -*- coding: utf-8 -*-
"""CampusAI Intent Parser 测试：意图识别 + JSON 解析 + 白名单/字段校验 + 失败处理。

对应任务里的 10 条要求（每条都在下面的测试里被断言）：
    1. 用 requests 调 OpenAI-compatible 接口 —— 断言 ``session.post`` 的参数
    2. API 配置从 .env 读取 —— ``load_config`` 的环境变量链路 + 源码不含 Key
    3. Prompt 从 prompts/intent_parser.md 读取 —— 断言 system message 内容
    4. 使用 JSON Output —— 断言 ``response_format = json_object``
    5. 解析 JSON —— 覆盖纯 JSON / ```json 代码块 / 夹杂解释文字 / 非法 JSON
    6. intent 必须经过白名单验证 —— 白名单外的意图一律拒绝
    7. capacity 等字段需要验证 —— 人数、节次、星期、日期、枚举、长度全部覆盖
    8. AI 调用失败必须有错误处理 —— 超时 / 网络错误 / 非 200 / 结构异常 / 降级
    9. 禁止生成 SQL —— 模型返回的 SQL 片段被拒 + 源码里不出现 SQL 语句
    10. 禁止 AI 直接访问数据库 —— 源码 AST 检查：不 import sqlite3 / backend.db

设计：全部离线。AI 请求通过 ``FakeSession`` 注入，**测试不会真的联网**，
也不会读写 ``data/campus.db``（本模块不碰数据库），因此没有 AI_API_KEY 也能跑。
"""

import ast
import datetime
import json
import os
import re
import sys

import pytest
import requests

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend import ai_parser  # noqa: E402
from backend import time_resolver  # noqa: E402
from backend.errors import ValidationError  # noqa: E402
from backend.validate import (  # noqa: E402
    INTENTS,
    SCHEMAS,
    normalize,
    render_contract,
    validate_intent,
)

#: 测试用的冻结锚点：相对日期测试一律用它，**绝不依赖 date.today()**，
#: 否则测试会在跨天/跨年时随机失败（2026-09-27 是星期日，2025-11-14 是数据最大日期）。
FROZEN_TODAY = datetime.date(2026, 9, 27)
FROZEN_DATA_MAX = datetime.date(2025, 11, 14)
FROZEN_ANCHOR = time_resolver.Anchor(today=FROZEN_TODAY, data_max_date=FROZEN_DATA_MAX)

GOLDEN_PATH = os.path.join(PROJECT_ROOT, "tests", "golden_questions.json")
PROMPT_PATH = os.path.join(PROJECT_ROOT, "prompts", "intent_parser.md")
PARSER_SOURCE = ai_parser.__file__
VALIDATE_SOURCE = os.path.join(PROJECT_ROOT, "backend", "validate.py")

#: 用假 Key 调假 session：既证明「Key 只从配置来」，也保证消息里不会泄露真 Key。
FAKE_API_KEY = "sk-fake-for-tests-do-not-use"

#: 真实 .env 里如果有 Key，也绝不允许出现在任何返回值/错误信息里。
REAL_API_KEY = None
try:
    with open(os.path.join(PROJECT_ROOT, ".env"), "r", encoding="utf-8") as _handle:
        for _line in _handle:
            if _line.strip().startswith("AI_API_KEY="):
                REAL_API_KEY = _line.split("=", 1)[1].strip() or None
except OSError:  # pragma: no cover - .env 不存在时跳过该断言
    REAL_API_KEY = None


# ---------------------------------------------------------------------------
# 测试替身：假的 HTTP 响应与假的 requests session
# ---------------------------------------------------------------------------

class FakeResponse:
    """最小可用的 HTTP 响应替身：``status_code`` / ``.json()`` / ``.text``。"""

    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload if payload is not None else {})

    def json(self):
        if self._payload is None:
            raise ValueError("No JSON object could be decoded")
        return self._payload


class FakeSession:
    """记录每次 ``post`` 的参数，并按队列返回预设响应（或抛网络异常）。"""

    def __init__(self, responses=(), error=None):
        self.responses = list(responses)
        self.error = error
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise AssertionError("FakeSession 没有更多预设响应了")
        return self.responses.pop(0)


def chat_response(answer, status_code=200, content=None):
    """把一份 intent JSON 包成 OpenAI-compatible 的 chat.completions 响应。"""
    if content is None:
        content = json.dumps(answer, ensure_ascii=False)
    return FakeResponse(
        status_code=status_code,
        payload={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": "fake-model",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
            ],
        },
    )


def make_parser(responses=(), error=None, **kwargs):
    """构造一个「配置已注入、请求打到假 session」的 IntentParser。

    默认注入**冻结的时间锚点**，保证涉及相对日期的断言不随真实日历漂移。
    """
    config = {
        "api_key": FAKE_API_KEY,
        "base_url": "https://ai.example.test/v1",
        "model": "fake-model",
        "timeout": 5.0,
        "max_tokens": 512,
        "temperature": 0,
        "prompt_path": PROMPT_PATH,
    }
    session = FakeSession(responses=responses, error=error)
    kwargs.setdefault("anchor", FROZEN_ANCHOR)
    parser = ai_parser.IntentParser(config=config, session=session, **kwargs)
    return parser, session


def golden_questions():
    with open(GOLDEN_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)["questions"]


GOLDEN = golden_questions()
GOLDEN_IDS = [item["id"] for item in GOLDEN]


#: 黄金用例里「待解析的时间原文」字段：由 backend/time_resolver.py 消费成具体日期，
#: 因此它们不会（也不该）出现在 validate_intent 校验后的 params 里。
RESOLVER_INPUT_FIELDS = ("date_text", "date_range_text")

#: 黄金用例里「由 Python 解析后才出现」的元信息：不是模型输出，也不是业务字段，
#: 而是用来断言相对日期确实被解析成了哪个字段（见 golden_resolved_targets）。
RESOLVER_META_FIELDS = ("resolves_to",)


def golden_answer_body(answer):
    """黄金答案里可以喂给 validate_intent 的那部分（保留 intent，去掉时间原文与元信息）。"""
    return {
        key: value
        for key, value in answer.items()
        if key not in RESOLVER_INPUT_FIELDS + RESOLVER_META_FIELDS
    }


def golden_static_expected(answer):
    """黄金答案里校验后应当原样保留的字段（去掉时间原文与元信息）。"""
    return {
        key: value
        for key, value in answer.items()
        if key != "intent" and key not in RESOLVER_INPUT_FIELDS + RESOLVER_META_FIELDS
    }


def golden_resolved_targets(answer):
    """该用例声明的「相对日期应当落到哪些字段」。"""
    return tuple(answer.get("resolves_to", ()))


#: 期望「必须判为 unknown」的用例（结构化断言的数据来源）。
MUST_BE_UNKNOWN = [item for item in GOLDEN if item.get("must_be_unknown")]


# ---------------------------------------------------------------------------
# 黄金用例判定（--golden 的输出口径，独立于真实模型，可离线断言）
# ---------------------------------------------------------------------------

def _golden_result(intent, params, degraded=False, warnings=()):
    return {"intent": intent, "params": params, "degraded": degraded, "warnings": list(warnings)}


def test_golden_ok_accepts_a_fully_matching_case():
    ok, detail = ai_parser._golden_ok(
        _golden_result("room_search", {"capacity": 8, "has_projector": True}),
        {"intent": "room_search", "capacity": 8, "has_projector": True},
    )
    assert ok, detail


def test_golden_ok_rejects_a_wrong_intent_or_degraded_result():
    assert ai_parser._golden_ok(
        _golden_result("unknown", {}), {"intent": "room_search", "capacity": 8}
    )[0] is False
    assert ai_parser._golden_ok(
        _golden_result("room_search", {"capacity": 8}, degraded=True),
        {"intent": "room_search", "capacity": 8},
    )[0] is False


def test_golden_ok_requires_date_text_to_land_on_a_real_date():
    """date_text 是 resolver 的输入，会被消费掉；但必须换来一个具体日期。"""
    ok, detail = ai_parser._golden_ok(
        _golden_result("canteen_analysis", {"date_from": "2026-09-14", "time_slot": "lunch"}),
        {"intent": "canteen_analysis", "date_text": "上周一", "time_slot": "lunch"},
    )
    assert ok, detail

    # 时间被丢了（没有落成任何日期字段）→ 判定失败。
    ok, detail = ai_parser._golden_ok(
        _golden_result("canteen_analysis", {"time_slot": "lunch"}),
        {"intent": "canteen_analysis", "date_text": "上周一", "time_slot": "lunch"},
    )
    assert ok is False
    assert "时间表达未落成具体日期" in detail


def test_golden_ok_accepts_canteen_name_as_an_equivalent_of_canteen_id():
    ok, detail = ai_parser._golden_ok(
        _golden_result("canteen_analysis", {"canteen_name": "一食堂"}),
        {"intent": "canteen_analysis", "canteen_id": 1},
    )
    assert ok, detail


def test_golden_ok_ignores_test_metadata_fields():
    """resolves_to 这类给测试用的元信息不属于模型输出，不能被当成缺失参数。"""
    ok, detail = ai_parser._golden_ok(
        _golden_result("canteen_analysis", {"date_from": "2026-09-14"}),
        {"intent": "canteen_analysis", "date_text": "上周一", "resolves_to": ["date_from"]},
    )
    assert ok, detail


def test_golden_ok_reports_a_missing_param_with_the_reason():
    ok, detail = ai_parser._golden_ok(
        _golden_result("room_search", {"capacity": 8}),
        {"intent": "room_search", "capacity": 8, "has_projector": True},
    )
    assert ok is False
    assert "has_projector" in detail


# ---------------------------------------------------------------------------
# 1. 离线：黄金问题集的答案必须能通过白名单与字段校验
# ---------------------------------------------------------------------------

def test_golden_file_covers_every_intent():
    covered = {item["answer"]["intent"] for item in GOLDEN}
    assert covered == set(INTENTS), "黄金问题集必须覆盖全部意图白名单"
    assert len(GOLDEN) >= 10


@pytest.mark.parametrize("case", GOLDEN, ids=GOLDEN_IDS)
def test_golden_answers_pass_validation(case):
    answer = case["answer"]
    intent, params = validate_intent(golden_answer_body(answer))
    assert intent == answer["intent"]
    if intent == "unknown":
        assert params == {}
        # 无关问题绝不允许携带业务参数（否则下游会拿"猜出来的条件"去查数据）。
        assert set(answer) == {"intent"}, "{} 的答案必须只有 intent".format(case["id"])
    else:
        # 时间原文之外的字段不应被静默丢弃（漏字段会让下游查错数据）。
        expected = golden_static_expected(answer)
        assert set(params) == set(expected)
        for key, value in expected.items():
            assert params[key] == value, "{} 的 {} 被改写：{!r}".format(case["id"], key, params[key])
        # 只有时间原文的用例也要有断言：声明了相对日期就必须声明它落到哪个字段。
        if not expected:
            assert golden_resolved_targets(answer), (
                "用例 {} 既没有静态字段，也没有 resolves_to 声明".format(case["id"])
            )
            assert any(key in answer for key in RESOLVER_INPUT_FIELDS), (
                "用例 {} 声明了 resolves_to 却没有时间原文".format(case["id"])
            )


@pytest.mark.parametrize("case", GOLDEN, ids=GOLDEN_IDS)
def test_golden_resolver_targets_are_valid_schema_fields(case):
    """resolves_to 声明的字段必须是该意图白名单里的日期字段。

    这条断言防止「声明了一个 resolver 根本不会产出、或 schema 不认的字段」，
    让相对日期用例的期望值始终可核对。
    """
    answer = case["answer"]
    targets = golden_resolved_targets(answer)
    assert set(targets) <= set(SCHEMAS[answer["intent"]]), (
        "{} 的 resolves_to 超出该意图白名单".format(case["id"])
    )
    for field in targets:
        assert SCHEMAS[answer["intent"]][field]["kind"] == "date", (
            "{} 的 resolves_to 必须是日期字段：{}".format(case["id"], field)
        )
    if any(key in answer for key in RESOLVER_INPUT_FIELDS):
        assert targets, "{} 有时间原文但没有 resolves_to".format(case["id"])


def test_golden_unknown_cases_are_structured_and_explicit():
    """「完全无关」类问题必须有 must_be_unknown 标记，且答案只含 intent。"""
    assert MUST_BE_UNKNOWN, "黄金问题集必须至少有一个 must_be_unknown 用例"
    unrelated = [item for item in GOLDEN if item.get("category") == "完全无关"]
    assert len(unrelated) >= 6, "「完全无关」类用例太少，无法覆盖鲁棒性"
    for item in unrelated:
        assert item.get("must_be_unknown") is True, "{} 缺少 must_be_unknown 标记".format(item["id"])
        assert item["answer"] == {"intent": "unknown"}, "{} 的答案必须只有 intent".format(item["id"])
    # 话题边界：没有数据支撑的「域内问题」也必须 unknown，不能硬套业务意图。
    boundary = [item for item in GOLDEN if item.get("category") == "能力边界"]
    assert len(boundary) >= 2, "缺少「能力边界」用例"
    for item in boundary:
        assert item.get("must_be_unknown") is True, "{} 缺少 must_be_unknown 标记".format(item["id"])
        assert item["answer"] == {"intent": "unknown"}


@pytest.mark.parametrize("case", MUST_BE_UNKNOWN, ids=[item["id"] for item in MUST_BE_UNKNOWN])
def test_offtopic_questions_must_stay_unknown(case):
    """即使模型吐出了业务字段，无关问题也必须被收敛成纯 unknown。"""
    parser, _ = make_parser([chat_response(case["answer"])])
    result = parser.query(case["question"])
    assert result["intent"] == "unknown"
    assert result["params"] == {}
    assert result["degraded"] is False


def test_offtopic_with_a_valid_looking_intent_is_not_semantically_checkable():
    """记录一个**已知边界**：白名单校验只能保证「形状合法」，不能判断「语义是否切题」。

    模型偶尔会把无关问题套成 dashboard_query 并编出一个合法 dimension，
    这时校验层无法区分它和真实诉求（没有必填字段可核对）。
    因此「无关问题必须 unknown」靠两件事一起保证：
        1. Prompt 的 Intent Boundaries 规则（让模型别乱套）
        2. golden_questions.json 里的 must_be_unknown 用例（真实调用时持续盯住）
    这里把这个边界固定下来，避免误以为校验层能兜住语义错误。
    """
    parser, _ = make_parser(
        [chat_response({"intent": "dashboard_query", "dimension": "industry"})]
    )
    result = parser.query("推荐几只股票。")
    assert result["intent"] == "dashboard_query", "校验层只能放行形状合法的返回"
    assert result["degraded"] is False
    assert result["params"] == {"dimension": "industry"}


def test_unknown_intent_with_hallucinated_params_is_rejected():
    """反过来，模型给 unknown 硬塞业务参数时必须拦住（猜出来的条件不能下游当真）。"""
    parser, _ = make_parser(
        [chat_response({"intent": "unknown", "dimension": "industry"})]
    )
    result = parser.query("推荐几只股票。")
    assert result["intent"] == "unknown"
    assert result["degraded"] is True
    assert result["params"] == {}
    assert result["error"]["code"] == ai_parser.ERROR_INVALID_INTENT


def test_golden_questions_are_non_empty_strings():
    for item in GOLDEN:
        assert item["question"].strip()
        assert item["answer"]["intent"] in INTENTS
        assert item["id"].strip()
        assert item["category"].strip()


def test_golden_covers_the_seven_robustness_categories():
    """7 类鲁棒性问题（相对日期/模糊时间/人数/设备/食堂/产教/无关）都要有用例。"""
    required = {
        "相对日期", "模糊时间", "人数表达", "设备需求",
        "食堂问题", "产教问题", "完全无关",
    }
    covered = {item["category"] for item in GOLDEN}
    assert required <= covered, "缺少类别：{}".format(sorted(required - covered))


# ---------------------------------------------------------------------------
# 2. 端到端（mock 模型）：黄金问题集 → Intent JSON（>= 10 条）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", GOLDEN, ids=GOLDEN_IDS)
def test_parse_end_to_end_for_golden_questions(case):
    parser, session = make_parser([chat_response(golden_answer_body(case["answer"]))])
    result = parser.query(case["question"])

    assert result["degraded"] is False
    assert result["intent"] == case["answer"]["intent"]
    assert result["chart"] == ai_parser.suggest_chart(result["intent"])
    assert result["explanation"]
    assert isinstance(result["warnings"], list)
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "case",
    [item for item in GOLDEN if not item.get("must_be_unknown")],
    ids=[item["id"] for item in GOLDEN if not item.get("must_be_unknown")],
)
def test_golden_static_fields_survive_the_full_pipeline(case):
    """整条链路（调模型 → 解析 → 相对日期 → 校验）不能丢掉静态字段。

    这是「设备/人数/时段条件被静默丢弃」问题的回归守卫：模型的返回原样喂进去，
    校验后的参数必须还带着这些字段。相对日期用例会额外带上解析出的日期字段，
    所以这里断言的是「包含」而不是「相等」。
    """
    parser, _ = make_parser([chat_response(golden_answer_body(case["answer"]))])
    result = parser.query(case["question"])

    expected = golden_static_expected(case["answer"])
    for key, value in expected.items():
        assert result["params"].get(key) == value, (
            "{} 的 {} 在链路中被丢弃或改写：{!r}".format(case["id"], key, result["params"])
        )


# ---------------------------------------------------------------------------
# 3. 意图白名单（要求 6）
# ---------------------------------------------------------------------------

def test_intent_whitelist_accepts_only_known_intents():
    for intent in INTENTS:
        payload = {"intent": intent}
        if intent == "industry_skill_match":
            payload["major"] = "软件工程"
        elif intent == "industry_skill_gap":
            payload["major"] = "软件工程"
        elif intent == "room_search":
            payload["capacity"] = 10
        parsed, _ = validate_intent(payload)
        assert parsed == intent


@pytest.mark.parametrize(
    "payload",
    [
        {"intent": "drop_table"},
        {"intent": "room_search; DROP TABLE classrooms"},
        {"intent": "sql_query", "sql": "SELECT * FROM classrooms"},
        {"intent": "ROOM SEARCH"},  # 空白非法（下划线才合法）
        {"intent": ""},
        {"intent": None},
        {"params": {"capacity": 10}},  # 缺 intent
        {"intent": 3},
    ],
)
def test_intent_whitelist_rejects_everything_else(payload):
    with pytest.raises(ValidationError):
        validate_intent(payload)


def test_rejected_intent_becomes_structured_parser_error():
    parser, _ = make_parser([chat_response({"intent": "delete_all", "params": {}})])
    with pytest.raises(ai_parser.AIParserError) as excinfo:
        parser.parse("把所有教室删掉")
    assert excinfo.value.code == ai_parser.ERROR_INVALID_INTENT


def test_parser_never_accepts_unknown_extra_fields():
    with pytest.raises(ValidationError):
        validate_intent({"intent": "room_search", "capacity": 10, "sql": "SELECT 1"})
    with pytest.raises(ValidationError):
        validate_intent({"intent": "unknown", "capacity": 10})


# ---------------------------------------------------------------------------
# 4. 字段校验：capacity / weekday / 节次 / 日期 / 枚举（要求 7）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("capacity", [8, 8.0, "8"])
def test_capacity_is_normalized_to_int(capacity):
    intent, params = validate_intent({"intent": "room_search", "capacity": capacity})
    assert intent == "room_search"
    assert params["capacity"] == 8
    assert isinstance(params["capacity"], int)


@pytest.mark.parametrize("capacity", [0, -3, 1001, "abc", True, None, "", [8]])
def test_capacity_out_of_range_is_rejected(capacity):
    with pytest.raises(ValidationError) as excinfo:
        validate_intent({"intent": "room_search", "capacity": capacity})
    if capacity in (None, ""):
        # None/空串在归一化阶段被当作「没提取到」，最终会因为缺少筛选条件而被拒。
        assert "room_search" in excinfo.value.message
    else:
        assert excinfo.value.field == "capacity"


def test_weekday_is_1_to_7_and_accepts_chinese():
    for value, expected in ((3, 3), ("3", 3), ("周三", 3), ("星期三", 3), ("Wednesday", 3)):
        _, params = validate_intent({"intent": "room_search", "weekday": value, "capacity": 10})
        assert params["weekday"] == expected
    for bad in (0, 8, "周一上午", "next week"):
        with pytest.raises(ValidationError):
            validate_intent({"intent": "room_search", "weekday": bad, "capacity": 10})


def test_period_range_is_validated_and_aliased():
    # 规范名是 period_start / period_end（与 backend/queries.py、docs/API.md 一致）。
    intent, params = validate_intent(
        {"intent": "room_search", "period_start": "5", "period_end": 8, "capacity": 8}
    )
    assert (params["period_start"], params["period_end"]) == (5, 8)

    # Prompt 旧示例里的 end_period 仍然被当成别名收敛到规范名。
    _, aliased = validate_intent(
        {"intent": "room_search", "period_start": 5, "end_period": 8, "capacity": 8}
    )
    assert aliased["period_end"] == 8
    assert "end_period" not in aliased

    with pytest.raises(ValidationError) as excinfo:
        validate_intent({"intent": "room_search", "period_start": 6, "period_end": 5})
    assert excinfo.value.field == "period_end"
    assert "不能小于" in excinfo.value.message

    for bad in (0, 13, "第八节"):
        with pytest.raises(ValidationError):
            validate_intent({"intent": "room_search", "period_start": bad, "capacity": 8})


def test_boolean_fields_are_validated():
    for value, expected in ((True, True), (1, True), ("true", True), ("需要", True),
                            (False, False), (0, False), ("false", False), ("不需要", False)):
        _, params = validate_intent(
            {"intent": "room_search", "capacity": 10, "has_projector": value}
        )
        assert params["has_projector"] is expected
    for bad in ("maybe", 2, [], "yes please"):
        with pytest.raises(ValidationError) as excinfo:
            validate_intent({"intent": "room_search", "capacity": 10, "has_projector": bad})
        assert excinfo.value.field == "has_projector"


def test_equipment_field_names_match_the_query_layer():
    """设备字段必须收敛成 queries.py 读取的 has_projector / has_computer。

    历史 bug：validate 产出 ``projector``，而 queries 读 ``has_projector``，
    设备筛选条件在链路上被静默丢弃 —— 模型抽对了也没用。
    """
    _, params = validate_intent(
        {
            "intent": "room_search",
            "capacity": 10,
            "projector": True,        # 模型常见写法
            "need_computer": True,    # 另一种常见写法
        }
    )
    assert params == {"capacity": 10, "has_projector": True, "has_computer": True}
    assert "projector" not in params and "computer" not in params

    # 规范名直接给也要能用。
    _, params = validate_intent(
        {"intent": "room_search", "has_projector": False, "has_computer": False}
    )
    assert params == {"has_projector": False, "has_computer": False}


def test_schema_keys_are_read_by_the_real_query_functions():
    """一致性守卫：AI 产出的字段名必须真的被业务查询函数读取。

    这条测试用 AST 从 backend/queries.py 抽出每个业务函数真正 ``params.get(...)``
    的键名，再和 SCHEMAS 比对。字段名一旦漂移（例如有人把
    ``has_projector`` 改回 ``projector``），这里会立刻失败。
    """
    query_source = os.path.join(PROJECT_ROOT, "backend", "queries.py")
    with open(query_source, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    consumed = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        keys = set()
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "get"
                and isinstance(inner.func.value, ast.Name)
                and inner.func.value.id == "params"
                and inner.args
                and isinstance(inner.args[0], ast.Constant)
            ):
                keys.add(inner.args[0].value)
        if keys:
            consumed[node.name] = keys

    # intent → 真正执行该意图的业务函数
    bindings = {
        "room_search": "list_available_classrooms",
        "canteen_analysis": "canteen_stats",
        "industry_skill_match": "industry_skills",
        "industry_skill_gap": "industry_skill_gap",
    }
    # 查询层刻意不读、由上层处理或暂未接线的字段：
    #   date_text / date_range_text —— 已被 time_resolver 消费成具体日期
    #   date         —— 已解析出来，但教室查询目前只按 weekday+semester 过滤（见下方用例）
    #   canteen_name —— 查询层只按 canteen_id 过滤（见下方用例）
    #   sort_by      —— 排序意图（最短/最挤）目前只在解析层表达，查询层固定排序（见下方用例）
    #   company      —— 岗位/企业维度的过滤查询层尚未接线（见下方用例）
    #   limit / method / top_slots —— 分页、算法、展示层参数
    allowed_gaps = {
        "date_text", "date_range_text", "date", "canteen_name", "sort_by", "company",
        "limit", "method", "top_slots",
    }

    assert bindings.keys() <= set(SCHEMAS)
    for intent, function in bindings.items():
        assert function in consumed, "{} 里找不到 {}".format(query_source, function)
        unread = set(SCHEMAS[intent]) - consumed[function] - allowed_gaps
        assert not unread, (
            "意图 {} 会产出 {} 但这些字段 {} 根本不会被读取，"
            "筛选条件会被静默丢弃".format(intent, function, sorted(unread))
        )


def test_known_unwired_filters_stay_documented():
    """锁定几个「已解析但查询层还没接」的字段，避免它们被误当成已生效。

    - ``date``：教室查询只按 weekday+semester 过滤
    - ``sort_by``：食堂排序意图暂时只在解析层表达
    - ``company``：企业维度过滤尚未接线
    哪天业务层真接上了，这里会失败，提醒同步更新一致性守卫的 allowed_gaps。
    """
    query_source = os.path.join(PROJECT_ROOT, "backend", "queries.py")
    with open(query_source, "r", encoding="utf-8") as handle:
        source = handle.read()
    for field in ("date", "sort_by", "company"):
        assert 'params.get("{}")'.format(field) not in source, (
            "queries.py 已开始读取 {}，请更新一致性守卫的 allowed_gaps".format(field)
        )


def test_canteen_name_is_not_silently_accepted_as_a_filter():
    """canteen_name 目前查询层不读 —— 必须能明确看出来，而不是假装生效。

    这里锁定「查询层只认 canteen_id」这一事实；等 canteen_name 接线后，
    这条用例会失败，提醒同步更新上一条一致性守卫的 allowed_gaps。
    """
    query_source = os.path.join(PROJECT_ROOT, "backend", "queries.py")
    with open(query_source, "r", encoding="utf-8") as handle:
        source = handle.read()
    assert 'params.get("canteen_name")' not in source, (
        "queries.py 已开始读取 canteen_name，请更新一致性守卫的 allowed_gaps"
    )
    # 校验层允许它（模型更常给出名称而不是 ID），这条通道保持打开。
    _, params = validate_intent({"intent": "canteen_analysis", "canteen_name": "一食堂"})
    assert params == {"canteen_name": "一食堂"}


def test_enum_fields_are_validated():
    _, params = validate_intent({"intent": "room_search", "capacity": 10, "room_type": "机房"})
    assert params["room_type"] == "机房"
    with pytest.raises(ValidationError) as excinfo:
        validate_intent({"intent": "room_search", "capacity": 10, "room_type": "体育馆"})
    assert excinfo.value.field == "room_type"

    with pytest.raises(ValidationError):
        validate_intent({"intent": "canteen_analysis", "time_slot": "brunch"})
    with pytest.raises(ValidationError):
        validate_intent({"intent": "dashboard_query", "dimension": "salary"})


def test_date_fields_are_validated():
    _, params = validate_intent(
        {"intent": "canteen_analysis", "date_from": "2025-09-01", "date_to": "2025-09-07"}
    )
    assert params["date_from"] == "2025-09-01"
    for bad in ("2025/09/01", "2025-02-30", "09-01-2025"):
        with pytest.raises(ValidationError):
            validate_intent({"intent": "canteen_analysis", "date_from": bad})
    with pytest.raises(ValidationError):
        validate_intent(
            {"intent": "canteen_analysis", "date_from": "2025-09-10", "date_to": "2025-09-01"}
        )


def test_text_fields_are_length_limited_and_trimmed():
    _, params = validate_intent({"intent": "room_search", "building": "  综合楼  ", "capacity": 10})
    assert params["building"] == "综合楼"
    with pytest.raises(ValidationError) as excinfo:
        validate_intent({"intent": "room_search", "building": "楼" * 51, "capacity": 10})
    assert excinfo.value.field == "building"


def test_required_fields_must_be_present():
    for intent in ("industry_skill_gap", "industry_skill_match"):
        with pytest.raises(ValidationError) as excinfo:
            validate_intent({"intent": intent, "industry": "人工智能"})
        assert excinfo.value.field == "major"


def test_room_search_without_any_condition_warns_instead_of_vanishing():
    """条件表达不了时保留意图并留痕，而不是把整个意图判成 unknown。

    历史 bug：「有没有带空调的空教室？」→ 模型确实识别成 room_search，但平台没有
    空调字段 → 参数被丢弃 → 报错 → 整条降级成 unknown，用户连意图都看不到。
    """
    warnings = []
    intent, params = validate_intent(
        {"intent": "room_search", "air_conditioner": True}, warnings=warnings
    )
    assert intent == "room_search"
    assert params == {}
    assert any("air_conditioner" in warning for warning in warnings)
    assert any("没有一个平台支持的筛选条件" in warning for warning in warnings)


def test_room_search_requires_at_least_one_condition():
    # 「只回了一个空对象」＝没有意图证据也没有条件，仍然拒绝。
    with pytest.raises(ValidationError):
        validate_intent({"intent": "room_search"})
    with pytest.raises(ValidationError):
        validate_intent({"intent": "room_search", "params": {"capacity": None}})
    # 明确的布尔条件（哪怕是 False ＝「不要投影」）本身就是有效条件。
    intent, params = validate_intent({"intent": "room_search", "has_projector": False})
    assert (intent, params) == ("room_search", {"has_projector": False})


def test_unsupported_conditions_are_recorded_not_silently_dropped():
    """白名单外的字段要被记录成警告，且绝不放过 SQL 片段。"""
    warnings = []
    intent, params = validate_intent(
        {"intent": "canteen_analysis", "canteen_name": "一食堂", "menu_today": "红烧肉"},
        warnings=warnings,
    )
    assert intent == "canteen_analysis"
    assert params == {"canteen_name": "一食堂"}
    assert any("menu_today" in warning for warning in warnings)

    with pytest.raises(ValidationError):
        validate_intent(
            {"intent": "canteen_analysis", "evil": "'; DROP TABLE meal_records; --"}
        )


def test_intent_keeps_no_warnings_when_everything_is_supported():
    warnings = []
    intent, params = validate_intent(
        {"intent": "room_search", "capacity": 8, "has_projector": True}, warnings=warnings
    )
    assert (intent, params) == ("room_search", {"capacity": 8, "has_projector": True})
    assert warnings == []


# ---------------------------------------------------------------------------
# 5. JSON 解析：纯 JSON / 代码块 / 夹杂解释 / 非法 JSON（要求 4、5）
# ---------------------------------------------------------------------------

def test_response_format_is_json_object():
    parser, session = make_parser([chat_response({"intent": "unknown"})])
    parser.parse("随便问问")
    request = session.calls[0]
    assert request["url"] == "https://ai.example.test/v1/chat/completions"
    assert request["json"]["response_format"] == {"type": "json_object"}
    assert request["json"]["model"] == "fake-model"
    assert request["json"]["messages"][0]["role"] == "system"
    assert request["json"]["messages"][1] == {"role": "user", "content": "随便问问"}
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["timeout"] == 5.0


@pytest.mark.parametrize(
    "content",
    [
        '{"intent": "room_search", "capacity": 8}',
        '```json\n{"intent": "room_search", "capacity": 8}\n```',
        '```\n{"intent": "room_search", "capacity": 8}\n```',
        '好的，这是解析结果：{"intent": "room_search", "capacity": 8} 请查收。',
        '  \n {"intent": "room_search", "params": {"capacity": 8}} \n ',
    ],
)
def test_json_output_parsing_variants(content):
    parser, _ = make_parser([chat_response(None, content=content)])
    assert parser.parse("找个8人的教室") == {
        "intent": "room_search",
        "params": {"capacity": 8},
        "degraded": False,
        "warnings": [],
        "error": None,
    }


def test_nested_params_and_entities_are_flattened():
    parser, _ = make_parser(
        [chat_response(None, content='{"intent":"room_search","entities":{"weekday":3,"capacity":8}}')]
    )
    assert parser.parse("周三8人教室") == {
        "intent": "room_search",
        "params": {"weekday": 3, "capacity": 8},
        "degraded": False,
        "warnings": [],
        "error": None,
    }


@pytest.mark.parametrize(
    "content",
    [
        "对不起，我无法回答。",
        "```json\n{不是合法JSON}\n```",
        "",
        "   ",
        "[1, 2, 3]",
        '"just a string"',
    ],
)
def test_invalid_json_content_raises_invalid_json(content):
    parser, _ = make_parser([chat_response(None, content=content)])
    with pytest.raises(ai_parser.AIParserError) as excinfo:
        parser.parse("找个教室")
    assert excinfo.value.code in (ai_parser.ERROR_INVALID_JSON, ai_parser.ERROR_INVALID_RESPONSE)


def test_extract_json_object_rejects_non_objects():
    with pytest.raises(ai_parser.AIParserError) as excinfo:
        ai_parser.extract_json_object("[1,2,3]")
    assert excinfo.value.code == ai_parser.ERROR_INVALID_JSON


# ---------------------------------------------------------------------------
# 6. AI 调用失败的错误处理（要求 8）
# ---------------------------------------------------------------------------

def test_missing_api_key_raises_missing_config():
    with pytest.raises(ai_parser.AIParserError) as excinfo:
        ai_parser.load_config(env={"AI_BASE_URL": "https://ai.example.test"})
    assert excinfo.value.code == ai_parser.ERROR_MISSING_CONFIG
    assert "AI_API_KEY" in excinfo.value.message


def test_default_config_comes_from_env_not_source_code():
    config = ai_parser.load_config(env={"AI_API_KEY": FAKE_API_KEY})
    assert config["api_key"] == FAKE_API_KEY
    assert config["base_url"] == ai_parser.DEFAULT_BASE_URL
    assert config["model"] == ai_parser.DEFAULT_MODEL
    assert config["timeout"] == ai_parser.DEFAULT_TIMEOUT
    assert config["prompt_path"] == PROMPT_PATH


def test_timeout_is_reported_as_network_error():
    parser, _ = make_parser(error=requests.exceptions.Timeout("read timed out"))
    with pytest.raises(ai_parser.AIParserError) as excinfo:
        parser.parse("帮我找教室")
    assert excinfo.value.code == ai_parser.ERROR_NETWORK
    assert "超时" in excinfo.value.message


def test_connection_error_is_reported_as_network_error():
    parser, _ = make_parser(error=requests.exceptions.ConnectionError("dns failure"))
    with pytest.raises(ai_parser.AIParserError) as excinfo:
        parser.parse("帮我找教室")
    assert excinfo.value.code == ai_parser.ERROR_NETWORK


@pytest.mark.parametrize("status_code", [400, 401, 429, 500, 503])
def test_http_error_status_is_reported(status_code):
    response = FakeResponse(
        status_code=status_code,
        payload={"error": {"message": "upstream failed"}},
        text='{"error": {"message": "upstream failed"}}',
    )
    parser, _ = make_parser([response])
    with pytest.raises(ai_parser.AIParserError) as excinfo:
        parser.parse("帮我找教室")
    assert excinfo.value.code == ai_parser.ERROR_HTTP_STATUS
    assert excinfo.value.status_code == status_code


def test_upstream_error_body_is_reported_as_invalid_response():
    parser, _ = make_parser([FakeResponse(200, payload={"error": {"message": "quota exceeded"}})])
    with pytest.raises(ai_parser.AIParserError) as excinfo:
        parser.parse("帮我找教室")
    assert excinfo.value.code == ai_parser.ERROR_INVALID_RESPONSE
    assert "quota exceeded" in excinfo.value.message


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": ""}}]},
        {"choices": [{"message": {"content": None}}]},
    ],
)
def test_malformed_success_payload_is_reported(payload):
    parser, _ = make_parser([FakeResponse(200, payload=payload)])
    with pytest.raises(ai_parser.AIParserError) as excinfo:
        parser.parse("帮我找教室")
    assert excinfo.value.code == ai_parser.ERROR_INVALID_RESPONSE


def test_non_json_http_body_is_reported():
    parser, _ = make_parser([FakeResponse(200, payload=None, text="<html>502 Bad Gateway</html>")])
    with pytest.raises(ai_parser.AIParserError) as excinfo:
        parser.parse("帮我找教室")
    assert excinfo.value.code == ai_parser.ERROR_INVALID_RESPONSE


def test_empty_question_is_rejected_before_calling_api():
    parser, session = make_parser([chat_response({"intent": "unknown"})])
    for question in ("", "   ", None, 3):
        with pytest.raises(ai_parser.AIParserError) as excinfo:
            parser.parse(question)
        assert excinfo.value.code == ai_parser.ERROR_EMPTY_QUESTION
    assert session.calls == [], "空问题不应该发起 HTTP 请求"


def test_missing_prompt_file_raises_missing_prompt():
    parser = ai_parser.IntentParser(
        config={"api_key": FAKE_API_KEY, "base_url": "https://ai.example.test"},
        session=FakeSession(),
        prompt_path=os.path.join(PROJECT_ROOT, "prompts", "not-there.md"),
    )
    with pytest.raises(ai_parser.AIParserError) as excinfo:
        parser.system_prompt
    assert excinfo.value.code == ai_parser.ERROR_MISSING_PROMPT


def test_query_degrades_to_unknown_instead_of_raising():
    parser, _ = make_parser(error=requests.exceptions.ConnectionError("offline"))
    result = parser.query("周三下午找个8人的空教室")
    assert result["intent"] == "unknown"
    assert result["params"] == {}
    assert result["degraded"] is True
    assert result["error"]["code"] == ai_parser.ERROR_NETWORK
    assert result["chart"] == "none"


def test_query_degrades_when_model_returns_bad_intent():
    parser, _ = make_parser([chat_response({"intent": "rm -rf /"})])
    result = parser.query("帮我找教室")
    assert result["degraded"] is True
    assert result["intent"] == "unknown"
    assert result["error"]["code"] == ai_parser.ERROR_INVALID_INTENT


def test_parser_error_is_serializable_and_carries_field():
    parser, _ = make_parser([chat_response({"intent": "room_search", "capacity": -5})])
    with pytest.raises(ai_parser.AIParserError) as excinfo:
        parser.parse("找个教室")
    payload = excinfo.value.to_dict()
    assert payload["code"] == ai_parser.ERROR_INVALID_INTENT
    assert payload["field"] == "capacity"
    json.dumps(payload, ensure_ascii=False)


def test_last_error_is_remembered():
    parser, _ = make_parser(error=requests.exceptions.Timeout("slow"))
    parser.parse("帮我找教室", use_fallback=True)
    assert isinstance(parser.last_error, ai_parser.AIParserError)
    assert parser.last_error.code == ai_parser.ERROR_NETWORK


# ---------------------------------------------------------------------------
# 7. 密钥不泄露（AGENTS.md：API Key 禁止写入源代码）
# ---------------------------------------------------------------------------

def test_errors_never_contain_api_key():
    parser, _ = make_parser(error=requests.exceptions.ConnectionError("boom"))
    result = parser.query("帮我找教室")
    assert FAKE_API_KEY not in json.dumps(result, ensure_ascii=False)
    assert FAKE_API_KEY not in str(parser.last_error)
    assert FAKE_API_KEY not in str(parser.last_error.to_dict())


def test_api_key_is_only_sent_in_authorization_header():
    parser, session = make_parser([chat_response({"intent": "unknown"})])
    parser.parse("随便问问")
    request = session.calls[0]
    assert request["headers"]["Authorization"] == "Bearer {}".format(FAKE_API_KEY)
    # 请求体里不应出现 Key（防止 Key 混进 prompt 或参数）。
    assert FAKE_API_KEY not in json.dumps(request["json"], ensure_ascii=False)


@pytest.mark.skipif(not REAL_API_KEY, reason="仓库没有 .env 或里面没有 AI_API_KEY")
def test_real_api_key_never_leaks_into_results():
    parser, _ = make_parser([chat_response({"intent": "unknown"})])
    result_json = json.dumps(parser.query("随便问问"), ensure_ascii=False)
    assert REAL_API_KEY not in result_json

    failing, _ = make_parser(error=requests.exceptions.Timeout("slow"))
    payload = json.dumps(failing.query("帮我找教室"), ensure_ascii=False)
    assert REAL_API_KEY not in payload


@pytest.mark.skipif(not REAL_API_KEY, reason="仓库没有 .env 或里面没有 AI_API_KEY")
def test_api_key_is_not_hardcoded_in_source():
    for path in (PARSER_SOURCE, VALIDATE_SOURCE):
        with open(path, "r", encoding="utf-8") as handle:
            assert REAL_API_KEY not in handle.read(), "{} 里出现了真实 API Key".format(path)


def test_load_config_reads_dotenv_file(tmp_path, monkeypatch):
    """``load_config()`` 不带 env 参数时会读 .env（进程环境变量优先，不会被覆盖）。"""
    env_file = os.path.join(str(tmp_path), ".env")
    with open(env_file, "w", encoding="utf-8") as handle:
        handle.write("AI_API_KEY=sk-from-dotenv\nAI_BASE_URL=https://dotenv.example/v1\nAI_MODEL=dotenv-model\n")
    for name in ("AI_API_KEY", "AI_BASE_URL", "AI_MODEL"):
        monkeypatch.delenv(name, raising=False)

    config = ai_parser.load_config(dotenv_path=env_file)
    assert config["api_key"] == "sk-from-dotenv"
    assert config["base_url"] == "https://dotenv.example/v1"
    assert config["model"] == "dotenv-model"

    # 进程环境变量优先级更高：CI / 测试可以通过环境变量覆盖 .env。
    monkeypatch.setenv("AI_MODEL", "override-model")
    assert ai_parser.load_config(dotenv_path=env_file)["model"] == "override-model"


# ---------------------------------------------------------------------------
# 8. Prompt 来源（要求 3）
# ---------------------------------------------------------------------------

def test_system_prompt_is_loaded_from_prompt_file():
    parser, session = make_parser([chat_response({"intent": "unknown"})])
    parser.parse("随便问问")
    system_message = session.calls[0]["json"]["messages"][0]["content"]

    with open(PROMPT_PATH, "r", encoding="utf-8") as handle:
        prompt_file = ai_parser.unescape_prompt(handle.read())
    assert prompt_file.strip() in system_message
    for intent in INTENTS:
        assert intent in system_message
    assert "不生成SQL" in system_message or "不生成 SQL" in system_message


def test_prompt_file_lists_the_same_intents_as_the_whitelist():
    with open(PROMPT_PATH, "r", encoding="utf-8") as handle:
        raw = handle.read()
    prompt_file = ai_parser.unescape_prompt(raw)
    for intent in INTENTS:
        assert intent in prompt_file, "{} 不在 prompts/intent_parser.md 里".format(intent)


def test_markdown_escapes_are_removed_from_prompt():
    assert ai_parser.unescape_prompt(r"room\_search 与 \# 标题 \- 列表 \. 句号") == (
        "room_search 与 # 标题 - 列表 . 句号"
    )
    # 代码块里的反斜杠转义同样被还原，但普通中文与换行不受影响。
    assert ai_parser.unescape_prompt("```json\n{\"intent\": \"room\\_search\"}\n```").endswith(
        '{"intent": "room_search"}\n```'
    )


def test_contract_lists_every_intent_and_field():
    contract = render_contract()
    for intent in INTENTS:
        assert intent in contract
        for field in SCHEMAS[intent]:
            assert field in contract


# ---------------------------------------------------------------------------
# 9/10. 禁止生成 SQL、禁止 AI 访问数据库（要求 9、10）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "field_value",
    [
        "'; DROP TABLE classrooms; --",
        "1 OR 1=1",
        "综合楼 UNION SELECT skill_name FROM skills",
        "DELETE FROM meal_records",
        "A101/**/PRAGMA foreign_keys=OFF",
    ],
)
def test_sql_like_field_values_are_rejected(field_value):
    with pytest.raises(ValidationError):
        validate_intent({"intent": "room_search", "building": field_value, "capacity": 10})
    with pytest.raises(ValidationError):
        validate_intent({"intent": "industry_skill_gap", "major": field_value})


def test_intent_with_sql_payload_is_rejected():
    with pytest.raises(ValidationError):
        validate_intent({"intent": "room_search", "capacity": 10, "sql": "SELECT * FROM classrooms"})
    # 模型把 SQL 塞进 intent 字段也要挡住。
    with pytest.raises(ValidationError):
        validate_intent({"intent": "room_search -- ; DROP TABLE classrooms", "capacity": 10})


def test_parser_source_contains_no_sql_and_no_database_access():
    """AST 检查：解析器不 import sqlite3 / 不 import backend.db，源码里没有 SQL 语句。"""
    with open(PARSER_SOURCE, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
            imported.add(node.module)
    assert "sqlite3" not in imported
    assert "backend.db" not in imported
    assert ".db" not in imported
    # 只允许这些依赖（requests 调 API、dotenv 读配置、其余是标准库）。
    # errors / validate / time_resolver 是 `python backend/ai_parser.py`
    # 直接运行时的相对导入名。
    # 注意：time_resolver 会为「数据里的最近一天」读一个 MAX(record_date) 标量，
    # 因此它自己 import sqlite3 —— 但解析器主流程不碰业务数据，且可用注入锚点完全绕开。
    assert imported <= {
        "argparse", "json", "os", "re", "sys", "pathlib",
        "requests", "dotenv", "backend", "backend.errors", "backend.validate",
        "backend.time_resolver", "errors", "validate", "time_resolver",
    }, imported

    assert not re.search(
        r"\b(SELECT|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|DROP\s+TABLE|CREATE\s+TABLE)\b",
        source,
    ), "解析器源码里不应出现 SQL 语句"


def test_sql_guard_source_has_no_database_access():
    with open(VALIDATE_SOURCE, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "sqlite3" not in imported
    assert "backend.db" not in imported
    assert "requests" not in imported


def test_golden_answers_and_contract_contain_no_sql_statements():
    contract = render_contract()
    assert "SELECT " not in contract and "DROP TABLE" not in contract
    for item in GOLDEN:
        text = json.dumps(item["answer"], ensure_ascii=False)
        assert not re.search(r"\b(SELECT|DROP|DELETE|INSERT|UPDATE)\b", text, re.IGNORECASE)


# ---------------------------------------------------------------------------
# 11. 归一化细节
# ---------------------------------------------------------------------------

def test_normalize_lowercases_fields_and_drops_empty_values():
    intent, params = normalize({"intent": "Room_Search", "Capacity": 8, "Building": "  ", "limit": None})
    assert intent == "room_search"
    assert params == {"capacity": 8}


def test_normalize_wraps_invalid_payloads():
    for payload in ("not a dict", [1, 2], None):
        with pytest.raises(ValidationError):
            normalize(payload)
