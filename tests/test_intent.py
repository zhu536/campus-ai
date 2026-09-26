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
from backend.errors import ValidationError  # noqa: E402
from backend.validate import (  # noqa: E402
    INTENTS,
    SCHEMAS,
    normalize,
    render_contract,
    validate_intent,
)

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
    """构造一个「配置已注入、请求打到假 session」的 IntentParser。"""
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
    parser = ai_parser.IntentParser(config=config, session=session, **kwargs)
    return parser, session


def golden_questions():
    with open(GOLDEN_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)["questions"]


GOLDEN = golden_questions()
GOLDEN_IDS = [item["id"] for item in GOLDEN]


def golden_by_id(case_id):
    for item in GOLDEN:
        if item["id"] == case_id:
            return item
    raise KeyError(case_id)  # pragma: no cover


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
    intent, params = validate_intent(answer)
    assert intent == answer["intent"]
    if intent == "unknown":
        assert params == {}
    else:
        assert params, "非 unknown 意图应当至少解析出一个参数：{}".format(case["id"])
        assert set(params) <= set(SCHEMAS[intent])
        # 除 intent 外不应有其它字段被静默丢弃（漏字段会让下游查错数据）。
        extra = set(answer) - {"intent"}
        assert set(params) == {key for key in extra if answer[key] is not None}


def test_golden_questions_are_non_empty_strings():
    for item in GOLDEN:
        assert item["question"].strip()
        assert item["answer"]["intent"] in INTENTS


# ---------------------------------------------------------------------------
# 2. 端到端（mock 模型）：黄金问题集 → Intent JSON（>= 10 条）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", GOLDEN, ids=GOLDEN_IDS)
def test_parse_end_to_end_for_golden_questions(case):
    parser, session = make_parser([chat_response(case["answer"])])
    result = parser.query(case["question"])

    assert result["degraded"] is False
    assert result["intent"] == case["answer"]["intent"]
    assert result["chart"] == ai_parser.suggest_chart(result["intent"])
    assert result["explanation"]
    assert len(session.calls) == 1


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
    intent, params = validate_intent(
        {"intent": "room_search", "period_start": "5", "period_end": 8, "capacity": 8}
    )
    assert (params["period_start"], params["end_period"]) == (5, 8)
    assert "period_end" not in params

    with pytest.raises(ValidationError) as excinfo:
        validate_intent({"intent": "room_search", "period_start": 6, "end_period": 5})
    assert excinfo.value.field == "end_period"
    assert "不能小于" in excinfo.value.message

    for bad in (0, 13, "第八节"):
        with pytest.raises(ValidationError):
            validate_intent({"intent": "room_search", "period_start": bad, "capacity": 8})


def test_boolean_fields_are_validated():
    for value, expected in ((True, True), (1, True), ("true", True), ("需要", True),
                            (False, False), (0, False), ("false", False), ("不需要", False)):
        _, params = validate_intent(
            {"intent": "room_search", "capacity": 10, "projector": value}
        )
        assert params["projector"] is expected
    for bad in ("maybe", 2, [], "yes please"):
        with pytest.raises(ValidationError) as excinfo:
            validate_intent({"intent": "room_search", "capacity": 10, "projector": bad})
        assert excinfo.value.field == "projector"


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


def test_room_search_requires_at_least_one_condition():
    with pytest.raises(ValidationError):
        validate_intent({"intent": "room_search"})
    with pytest.raises(ValidationError):
        validate_intent({"intent": "room_search", "params": {"capacity": None}})
    intent, params = validate_intent({"intent": "room_search", "projector": False})
    assert (intent, params) == ("room_search", {"projector": False})


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
    # errors / validate 是 `python backend/ai_parser.py` 直接运行时的相对导入名。
    assert imported <= {
        "argparse", "json", "os", "re", "sys", "pathlib",
        "requests", "dotenv", "backend", "backend.errors", "backend.validate",
        "errors", "validate",
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
