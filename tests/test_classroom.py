# -*- coding: utf-8 -*-
"""CampusAI「AI 智能找教室」测试：完整链路 解析 → 校验 → 查询 → 解释。

逐条对应任务的 8 条要求：

    1. AI 不能直接访问数据库 —— 交给模型的 payload 里没有数据库路径、连接或 SQL，
       且模型调用发生在 SQLite 连接关闭之后（源码 AST + 请求体双重断言）
    2. AI 不能生成 SQL —— 源码里没有查询语句；模型返回的解释里带 SQL 片段时整条被拒并降级
    3. 教室查询必须由 Python 完成 —— AST 检查：唯一查询入口是 queries.list_available_classrooms
    4. 查询参数必须经过 validate —— 白名单/类型/范围/互相约束/缺节次 全部被拒（400）
    5. 查询结果必须来自 SQLite —— 用直连 sqlite3 的参考实现逐项核对 rooms/total
    6. AI 只能解释查询结果 —— 解释器只拿到事实表，字段被收敛为 summary/highlights/suggestions
    7. 查询为空时给出合理提示 —— hint 里的数字来自再查一次 SQLite，可独立复算
    8. 增加测试 —— 本文件

全部离线：AI 用 FakeParser / FakeSession 注入，**不会联网**；数据库用 ``data/campus.db``
的临时副本只读访问，不会改动仓库里的库，也不触碰食堂与产教模块。
"""

import ast
import json
import os
import re
import shutil
import sqlite3
import sys

import pytest
import requests

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend import ai_parser  # noqa: E402
from backend import classroom  # noqa: E402
from backend import queries  # noqa: E402
from backend.app import create_app  # noqa: E402
from backend.errors import ValidationError  # noqa: E402

REAL_DB = os.path.join(PROJECT_ROOT, "data", "campus.db")
INSIGHT_PROMPT_PATH = os.path.join(PROJECT_ROOT, "prompts", "insight_generator.md")
CLASSROOM_SOURCE = classroom.__file__
SEMESTER = "2025-2026-1"

#: 假 Key：证明「Key 只从配置来」，同时保证任何返回/请求里都不会出现真 Key。
FAKE_API_KEY = "sk-fake-for-tests-do-not-use"

#: 用户示例问题（任务书里的原句）。
EXAMPLE_QUESTION = "周三下午帮我找一个8人、有投影的空教室。"

pytestmark = pytest.mark.skipif(
    not os.path.isfile(REAL_DB),
    reason="缺少 data/campus.db，请先执行 python scripts/import_data.py",
)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    """真实数据库的临时副本（模块级共用；只读使用，不动仓库里的库）。"""
    target = os.path.join(str(tmp_path_factory.mktemp("classroom_db")), "campus.db")
    shutil.copyfile(REAL_DB, target)
    return target


@pytest.fixture()
def conn(db_path):
    """直连 sqlite3 连接：供参考实现与「结果来自 SQLite」的断言使用。"""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def app(db_path):
    application = create_app(db_path=db_path)
    application.config.update(TESTING=True)
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# 测试替身：意图解析器 / HTTP 响应 / HTTP session / 解释器
# ---------------------------------------------------------------------------

class FakeParser:
    """冒充 :class:`backend.ai_parser.IntentParser`（只记录问题，不联网）。"""

    def __init__(self, result=None, error=None):
        self.result = dict(result or {})
        self.error = error
        self.questions = []

    def parse(self, question, use_fallback=False, anchor=None):
        self.questions.append(question)
        if self.error is not None:
            raise self.error
        return dict(self.result)


def parsed_result(intent, params=None, degraded=False, warnings=(), error=None):
    """构造 ``IntentParser.parse`` 的返回值形状。"""
    return {
        "intent": intent,
        "params": dict(params or {}),
        "degraded": degraded,
        "warnings": list(warnings),
        "error": error,
    }


def room_search_parser(**params):
    """一个「模型正确识别为 room_search」的解析器替身。"""
    return FakeParser(parsed_result("room_search", params))


class FakeResponse:
    """最小可用的 HTTP 响应替身。"""

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


def insight_response(summary=None, highlights=(), suggestions=(), content=None):
    """把解释器的 JSON 包成 OpenAI-compatible 的 chat.completions 响应。"""
    if content is None:
        body = {}
        if summary is not None:
            body["summary"] = summary
        if highlights:
            body["highlights"] = list(highlights)
        if suggestions:
            body["suggestions"] = list(suggestions)
        content = json.dumps(body, ensure_ascii=False)
    return FakeResponse(
        status_code=200,
        payload={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": "fake-model",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
            ],
        },
    )


def make_explainer(responses=(), error=None, prompt_path=None):
    """构造一个「配置已注入、请求打到假 session」的 ResultExplainer。"""
    config = {
        "api_key": FAKE_API_KEY,
        "base_url": "https://ai.example.test/v1",
        "model": "fake-model",
        "timeout": 5.0,
        "max_tokens": 512,
        "temperature": 0,
        "prompt_path": INSIGHT_PROMPT_PATH,
    }
    session = FakeSession(responses=responses, error=error)
    return classroom.ResultExplainer(config=config, session=session, prompt_path=prompt_path), session


class FakeExplainer:
    """假的 AI 解释器：把收到的 payload 记进 ``calls``，然后返回一段固定文案。"""

    def __init__(self, calls, text=None):
        self.calls = calls
        self.text = text

    def explain(self, payload):
        self.calls.append(payload)
        text = self.text or "AI 解释：找到 {} 间空教室。".format(payload["total"])
        return {
            "text": text,
            "summary": text,
            "highlights": [],
            "suggestions": [],
            "source": "ai",
            "error": None,
        }


def patch_parser(monkeypatch, parser):
    """让路由/服务里的 IntentParser() 产出替身（不读 .env、不联网）。"""
    monkeypatch.setattr(classroom, "IntentParser", lambda *args, **kwargs: parser)


def patch_explainer(monkeypatch, calls, text=None):
    monkeypatch.setattr(
        classroom, "ResultExplainer", lambda *args, **kwargs: FakeExplainer(calls, text=text)
    )


# ---------------------------------------------------------------------------
# 参考实现（独立于被测代码，直接用 sqlite3 算期望值）
# ---------------------------------------------------------------------------

def free_room_ids(conn, weekday, period_start, period_end, capacity=None, projector=None,
                  computer=None, room_type=None, building=None, semester=SEMESTER):
    """按 docs/DATABASE.md 的三步查询独立算出空闲教室（参数全部绑定）。"""
    sql = "SELECT room_id FROM classrooms WHERE status = 'available'"
    params = []
    for column, operator, value in (
        ("capacity", ">=", capacity),
        ("has_projector", ">=", projector),
        ("has_computer", ">=", computer),
        ("room_type", "=", room_type),
        ("building", "=", building),
    ):
        if value is not None:
            sql += " AND {} {} ?".format(column, operator)
            params.append(value)
    if weekday is not None:
        sql += (
            " AND room_id NOT IN (SELECT DISTINCT room_id FROM schedules"
            " WHERE semester = ? AND weekday = ? AND period_start <= ? AND period_end >= ?)"
        )
        params.extend([semester, weekday, period_end, period_start])
    return {row["room_id"] for row in conn.execute(sql, params).fetchall()}


def find_searchable_slot(conn, capacity=8, projector=1):
    """数据驱动地找一个「有 0 < 空闲数」的 (weekday, period) 组合（取空闲最少的一个）。

    不假设测试数据的具体分布，避免数据重新生成后用例失效。
    """
    best = None
    for weekday in range(1, 8):
        for period in range(1, 13):
            free = free_room_ids(
                conn, weekday, period, period, capacity=capacity, projector=projector
            )
            if free and (best is None or len(free) < len(best[2])):
                best = (weekday, period, free)
    assert best is not None, "测试数据里找不到任何符合条件的空教室"
    return best


# ===========================================================================
# 1/3/5. 完整链路：解析 → 校验 → SQLite 查询 → 候选教室
# ===========================================================================

def test_search_returns_candidates_from_sqlite(db_path, conn):
    """结果必须来自 SQLite：与直连 sqlite3 的参考实现逐间核对。"""
    weekday, period, free = find_searchable_slot(conn)
    parser = room_search_parser(
        capacity=8, has_projector=True, weekday=weekday, period_start=period,
        period_end=period, semester=SEMESTER, limit=100,
    )

    data = classroom.search_classrooms(
        EXAMPLE_QUESTION, db_path=db_path, parser=parser, explain=False
    )

    assert parser.questions == [EXAMPLE_QUESTION]
    assert data["intent"] == "room_search"
    assert data["query"]["status"] == "available"
    assert data["total"] == len(free)
    assert {room["room_id"] for room in data["rooms"]} == free
    for room in data["rooms"]:
        assert room["capacity"] >= 8
        assert room["has_projector"] is True
        assert room["status"] == "available"

    # 空闲结论再用排课表复核一次：这些教室在该时段确实没有排课。
    busy = {
        row["room_id"]
        for row in conn.execute(
            "SELECT DISTINCT room_id FROM schedules WHERE semester = ? AND weekday = ?"
            " AND period_start <= ? AND period_end >= ?",
            (SEMESTER, weekday, period, period),
        )
    }
    assert busy, "测试数据里该时段应当有排课，否则「空闲」断言没有意义"
    assert not (busy & free)


def test_search_maps_ai_params_to_query_params(db_path):
    """AI 抽出的参数 → 校验后的查询参数（键名与 queries 层完全一致）。

    库里有两个学期，所以模型必须把 semester 也抽出来（否则查询层会以「无法确定学期」拒绝）。
    """
    parser = room_search_parser(
        capacity=8, has_projector=True, weekday=3, period_start=5, period_end=8,
        semester=SEMESTER,
    )
    data = classroom.search_classrooms(
        EXAMPLE_QUESTION, db_path=db_path, parser=parser, explain=False
    )

    assert data["params"] == {
        "capacity": 8,
        "has_projector": True,
        "weekday": 3,
        "period_start": 5,
        "period_end": 8,
        "semester": SEMESTER,
    }
    assert data["query"] == {
        "semester": SEMESTER,  # 显式指定学期，不再依赖「单学期自动补齐」
        "weekday": 3,
        "period_start": 5,
        "period_end": 8,
        "capacity": 8,
        "has_projector": True,
        "has_computer": None,
        "room_type": None,
        "building": None,
        "status": "available",
        "limit": classroom.DEFAULT_RESULT_LIMIT,
    }


def test_result_ordering_is_just_enough_capacity(db_path):
    """候选教室按「刚好够用」排序：容量越接近需求越靠前（Python 计算，不是 AI）。"""
    parser = room_search_parser(capacity=30, has_projector=True, limit=100)
    data = classroom.search_classrooms(
        "有投影、能坐30人的空教室", db_path=db_path, parser=parser, explain=False
    )
    capacities = [room["capacity"] for room in data["rooms"]]
    assert capacities == sorted(capacities)


def test_single_period_endpoint_is_filled_by_python():
    """只给一端节次时由 Python 补齐（与 validators.period_range 语义一致）。"""
    query = classroom.resolve_query_params({"capacity": 8, "period_end": 6})
    assert query["period_start"] == 6
    assert query["period_end"] == 6


def test_has_projector_false_keeps_the_documented_gap(db_path):
    """已知缺口留痕：has_projector=false 不会被当成「排除投影」，必须显式提示。"""
    parser = room_search_parser(capacity=8, has_projector=False)
    data = classroom.search_classrooms(
        "找个没有投影的空教室", db_path=db_path, parser=parser, explain=False
    )
    assert data["query"]["has_projector"] is False
    assert any("已知限制" in note for note in data["notes"])


# ===========================================================================
# 4. 查询参数必须经过 validate
# ===========================================================================

@pytest.mark.parametrize(
    "params, field",
    [
        ({"capacity": 0}, "capacity"),
        ({"capacity": 1001}, "capacity"),
        ({"capacity": "8人"}, "capacity"),
        ({"has_projector": "maybe"}, "has_projector"),
        ({"room_type": "体育馆"}, "room_type"),
        ({"weekday": 8}, "weekday"),
        ({"period_start": 13}, "period_start"),
        ({"capacity": 8, "weekday": 3}, "period_start"),  # 只有星期几 → 无法判断空闲
        ({"capacity": 8, "period_start": 6, "period_end": 4}, "period_end"),
        ({"capacity": 8, "limit": 999}, "limit"),
        ({"date": "2025-09-10"}, "params"),  # 只有日期 → 没有可用的筛选条件
        ({}, "params"),
        ({"capacity": 8, "building": "综合楼'; DROP TABLE classrooms; --"}, "building"),
        ({"capacity": 8, "room_type": "普通 UNION SELECT skill_name FROM skills"}, "room_type"),
    ],
)
def test_query_params_are_validated(params, field):
    with pytest.raises(ValidationError) as excinfo:
        classroom.resolve_query_params(params)
    assert excinfo.value.field == field, (params, excinfo.value.message)


def test_search_rejects_invalid_params_from_the_model(db_path):
    """模型抽出来的非法参数同样会被挡在查询之前（400，不是静默查错数据）。"""
    parser = room_search_parser(capacity=8, period_start=6, period_end=4)
    with pytest.raises(ValidationError) as excinfo:
        classroom.search_classrooms(
            EXAMPLE_QUESTION, db_path=db_path, parser=parser, explain=False
        )
    assert excinfo.value.field == "period_end"


def test_model_cannot_inject_room_selection(db_path):
    """AI 编出 room_id 之类字段会被丢弃并留痕：选哪几间教室只能由 SQLite 决定。"""
    parser = room_search_parser(capacity=8, weekday=3, period_start=5, period_end=8,
                                room_id=999, semester=SEMESTER)
    data = classroom.search_classrooms(
        EXAMPLE_QUESTION, db_path=db_path, parser=parser, explain=False
    )
    assert "room_id" not in data["params"]
    assert "room_id" not in data["query"]
    assert any("room_id" in note for note in data["notes"]), data["notes"]


def test_date_condition_is_ignored_with_a_note(db_path):
    """教室空闲按 星期+节次 算：模型多给的日期条件必须显式丢弃并留痕。"""
    parser = room_search_parser(capacity=8, date="2025-09-10")
    data = classroom.search_classrooms(
        EXAMPLE_QUESTION, db_path=db_path, parser=parser, explain=False
    )
    assert "date" not in data["params"] and "date" not in data["query"]
    assert any("date" in note for note in data["notes"]), data["notes"]


@pytest.mark.parametrize("question", ["", "   ", None, 123, ["周三"], "长" * 201])
def test_question_is_validated(question, db_path):
    with pytest.raises(ValidationError) as excinfo:
        classroom.search_classrooms(
            question, db_path=db_path, parser=room_search_parser(capacity=8), explain=False
        )
    assert excinfo.value.field == "question"


# ===========================================================================
# 链路失败：AI 不可用 / 意图不是找教室
# ===========================================================================

def test_parser_failure_is_ai_unavailable(db_path):
    parser = FakeParser(error=RuntimeError("gateway down"))
    with pytest.raises(classroom.ClassroomSearchError) as excinfo:
        classroom.search_classrooms(EXAMPLE_QUESTION, db_path=db_path, parser=parser, explain=False)
    assert excinfo.value.code == "ai_unavailable"
    assert excinfo.value.status_code == 503


def test_degraded_parser_is_ai_unavailable(db_path):
    parser = FakeParser(
        parsed_result(
            "unknown", degraded=True, error={"code": "missing_config", "message": "缺少 AI_API_KEY"}
        )
    )
    with pytest.raises(classroom.ClassroomSearchError) as excinfo:
        classroom.search_classrooms(EXAMPLE_QUESTION, db_path=db_path, parser=parser, explain=False)
    assert excinfo.value.code == "ai_unavailable"
    assert excinfo.value.status_code == 503
    assert "missing_config" in excinfo.value.message


@pytest.mark.parametrize(
    "intent", ["canteen_analysis", "canteen_forecast", "industry_skill_match", "unknown"]
)
def test_non_room_intent_is_rejected(intent, db_path):
    parser = FakeParser(parsed_result(intent, {"canteen_id": 1}))
    with pytest.raises(classroom.ClassroomSearchError) as excinfo:
        classroom.search_classrooms("食堂为什么这么挤", db_path=db_path, parser=parser, explain=False)
    assert excinfo.value.code == "unsupported_intent"
    assert excinfo.value.status_code == 400
    assert excinfo.value.field == "intent"


# ===========================================================================
# 7. 查询为空时给出合理提示（提示里的数字来自 SQLite，可复算）
# ===========================================================================

def test_empty_result_hints_are_data_backed(db_path, conn):
    weekday, period, _ = find_searchable_slot(conn)
    # 200 是测试数据里的最大容量，1000(白名单上限) 必然一间都匹配不到。
    parser = room_search_parser(
        capacity=1000, weekday=weekday, period_start=period, period_end=period,
        semester=SEMESTER,
    )
    data = classroom.search_classrooms(
        "找一个能坐1000人的空教室", db_path=db_path, parser=parser, explain=False
    )

    assert data["total"] == 0
    assert data["rooms"] == []
    assert data["hint"], "空结果必须给出提示"

    expected = queries.list_available_classrooms(
        conn,
        {
            "weekday": weekday,
            "period_start": period,
            "period_end": period,
            "semester": SEMESTER,
            "status": "available",
            "limit": 1,
        },
    )["total"]
    assert expected > 0, "该时段应当有不限人数的空教室，这条提示才有意义"
    assert any("不限制人数" in hint and str(expected) in hint for hint in data["hint"])

    # 空结果也必须给出解释（此时是 Python 兜底文案，不能编造教室）。
    explanation = data["explanation"]
    assert explanation["source"] == "local"
    assert "没有找到" in explanation["text"]
    assert all(room["room_name"] not in explanation["text"] for room in data["rooms"])


def test_hint_numbers_come_from_sqlite(conn):
    """每条放宽建议里的数字都必须等于该放宽条件真实查出来的 total（可独立复算）。"""
    weekday, period, _ = find_searchable_slot(conn)
    result = queries.list_available_classrooms(
        conn,
        {
            "capacity": 1000,
            "has_projector": 1,
            "weekday": weekday,
            "period_start": period,
            "period_end": period,
            "semester": SEMESTER,
            "status": "available",
            "limit": 1,
        },
    )
    assert result["total"] == 0

    hints = classroom.build_hints(conn, result)
    assert hints
    base = {key: value for key, value in result["query"].items() if key != "limit"}

    checked = 0
    for params, template in classroom._relaxation_probes(base):
        count = queries.list_available_classrooms(conn, dict(params, limit=1))["total"]
        if not count:
            continue
        expected = classroom._render(
            template,
            count=count,
            capacity=base.get("capacity"),
            a=params.get("period_start"),
            b=params.get("period_end"),
        )
        assert expected in hints, expected
        checked += 1
    assert checked >= 1, "至少应有一条放宽建议能被复算"


def test_hints_fall_back_when_relaxation_does_not_help(conn, monkeypatch):
    """任何放宽都查不到时，也必须给一句可执行的建议，而不是空数组。"""
    monkeypatch.setattr(classroom, "_probe_total", lambda connection, params: 0)
    result = queries.list_available_classrooms(
        conn,
        {
            "capacity": 1000,
            "weekday": 1,
            "period_start": 1,
            "period_end": 1,
            "semester": SEMESTER,
            "status": "available",
            "limit": 1,
        },
    )
    assert result["total"] == 0
    hints = classroom.build_hints(conn, result)
    assert len(hints) == 1
    assert "换一天" in hints[0]


def test_non_empty_result_has_no_empty_hint(db_path):
    parser = room_search_parser(capacity=30, has_projector=True, limit=1)
    data = classroom.search_classrooms(
        "有投影、能坐30人的空教室", db_path=db_path, parser=parser, explain=False
    )
    assert data["total"] >= 1
    assert data["hint"] == []
    assert any("limit" in note for note in data["notes"])  # limit 截断要说明
    assert "找到" in data["explanation"]["text"]


# ===========================================================================
# 1/2/6. AI 只能解释查询结果（不能访问数据库、不能生成 SQL）
# ===========================================================================

def run_explained_search(db_path, conn, responses=(), error=None, **overrides):
    """跑一次「带 AI 解释」的完整链路，返回 (data, session)。全程离线。"""
    weekday, period, _ = find_searchable_slot(conn)
    params = {
        "capacity": 8,
        "has_projector": True,
        "weekday": weekday,
        "period_start": period,
        "period_end": period,
        "semester": SEMESTER,
        "limit": 3,
    }
    params.update(overrides)
    explainer, session = make_explainer(responses=responses, error=error)
    data = classroom.search_classrooms(
        EXAMPLE_QUESTION,
        db_path=db_path,
        parser=room_search_parser(**params),
        explainer=explainer,
    )
    return data, session


def test_ai_explanation_only_sees_the_query_result(db_path, conn):
    data, session = run_explained_search(
        db_path, conn, responses=[insight_response("找到空教室。", ["容量刚好够用"])]
    )

    assert data["explanation"]["source"] == "ai"
    assert data["explanation"]["summary"] == "找到空教室。"
    assert data["explanation"]["highlights"] == ["容量刚好够用"]
    assert data["explanation"]["error"] is None

    request = session.calls[0]["json"]
    assert request["response_format"] == {"type": "json_object"}

    # 发给模型的东西里不能有数据库路径 / SQL / sqlite 字样（要求 1、2）。
    body_text = json.dumps(request, ensure_ascii=False)
    assert "campus.db" not in body_text
    assert "sqlite" not in body_text.lower()
    assert not re.search(r"\b(SELECT|INSERT|UPDATE|DELETE|DROP)\b", body_text)

    # 事实表：只有已经查好的结果，字段固定。
    payload = json.loads(request["messages"][1]["content"])
    assert set(payload) == {"question", "filters", "total", "rooms", "shown", "hint", "notes"}
    assert payload["question"] == EXAMPLE_QUESTION
    assert payload["total"] == data["total"]
    assert payload["shown"] == len(data["rooms"])
    assert [room["room_name"] for room in payload["rooms"]] == [
        room["room_name"] for room in data["rooms"]
    ]
    assert "至少 8 人" in payload["filters"] and "需要投影" in payload["filters"]

    # system prompt 来自 prompts/insight_generator.md（正式 Prompt 走 Git 留痕）。
    with open(INSIGHT_PROMPT_PATH, "r", encoding="utf-8") as handle:
        prompt_file = ai_parser.unescape_prompt(handle.read())
    assert prompt_file.strip() in request["messages"][0]["content"]


def test_explanation_falls_back_when_it_contains_sql(db_path, conn):
    """模型想在解释里塞 SQL：整条拒绝，退回 Python 兜底文案。"""
    data, _ = run_explained_search(
        db_path, conn, responses=[insight_response("SELECT * FROM classrooms WHERE capacity >= 8")]
    )
    assert data["explanation"]["source"] == "local"
    assert data["explanation"]["error"]["code"] == "unsafe_explanation"
    assert "SELECT" not in data["explanation"]["text"]
    assert "没有找到" not in data["explanation"]["text"]  # 有教室时不能谎称没找到


def test_explanation_falls_back_on_invalid_json(db_path, conn):
    data, _ = run_explained_search(
        db_path, conn, responses=[insight_response(content="这不是JSON")]
    )
    assert data["explanation"]["source"] == "local"
    assert data["explanation"]["error"]["code"] == "invalid_json"


def test_explanation_falls_back_when_summary_is_missing(db_path, conn):
    data, _ = run_explained_search(db_path, conn, responses=[insight_response()])
    assert data["explanation"]["source"] == "local"
    assert data["explanation"]["error"]["code"] == "invalid_explanation"


def test_explanation_falls_back_on_network_failure(db_path, conn):
    data, _ = run_explained_search(db_path, conn, error=requests.exceptions.Timeout("slow"))
    assert data["explanation"]["source"] == "local"
    assert data["explanation"]["error"]["code"] == "network_error"
    assert FAKE_API_KEY not in json.dumps(data, ensure_ascii=False)


def test_explanation_uses_default_prompt_path():
    """不传 prompt_path 时用 prompts/insight_generator.md（默认路径必须存在）。"""
    explainer, session = make_explainer(responses=[insight_response("ok")])
    explainer.explain({"question": "q", "filters": [], "total": 0, "rooms": [], "shown": 0,
                       "hint": [], "notes": []})
    assert classroom.DEFAULT_INSIGHT_PROMPT_PATH == INSIGHT_PROMPT_PATH
    assert os.path.isfile(INSIGHT_PROMPT_PATH)
    assert session.calls[0]["json"]["messages"][0]["content"].strip()


def test_explain_false_never_constructs_the_ai(db_path, monkeypatch):
    calls = []
    patch_explainer(monkeypatch, calls)
    data = classroom.search_classrooms(
        EXAMPLE_QUESTION,
        db_path=db_path,
        parser=room_search_parser(
            capacity=8, weekday=3, period_start=5, period_end=8, semester=SEMESTER
        ),
        explain=False,
    )
    assert calls == []
    assert data["explanation"]["source"] == "local"


def test_clean_line_limits_and_rejects_sql():
    assert classroom._clean_line("  多余   空白  ") == "多余 空白"
    assert classroom._clean_line(123) == ""
    long_text = classroom._clean_line("很" * 500)
    assert long_text.endswith("…") and len(long_text) == classroom.MAX_INSIGHT_LINE + 1
    for value in ("DROP TABLE classrooms", "select * from rooms", "1; --"):
        with pytest.raises(ai_parser.AIParserError) as excinfo:
            classroom._clean_line(value)
        assert excinfo.value.code == "unsafe_explanation"


def test_build_insight_payload_has_no_database_or_sql(db_path):
    parser = room_search_parser(capacity=8, has_projector=True, limit=2)
    data = classroom.search_classrooms(
        "有投影、8人、空教室", db_path=db_path, parser=parser, explain=False
    )
    payload = classroom.build_insight_payload(
        EXAMPLE_QUESTION, data, data["hint"], data["notes"]
    )
    text = json.dumps(payload, ensure_ascii=False)
    assert "campus.db" not in text
    assert not re.search(r"\b(SELECT|INSERT|UPDATE|DELETE|DROP)\b", text)
    assert set(payload["rooms"][0]) == {
        "room_name", "building", "floor", "capacity", "has_projector", "has_computer", "room_type",
    }


# ===========================================================================
# 2/3. 源码守卫：Python 查数据、AI 不碰 SQL 与数据库
# ===========================================================================

def test_classroom_module_queries_only_through_the_business_layer():
    with open(CLASSROOM_SOURCE, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
            imported.update(alias.name.split(".")[0] for alias in node.names)

    assert "sqlite3" not in imported, "教室查询必须由 queries 层完成，本模块不直连 sqlite3"
    assert "queries" in imported
    assert "list_available_classrooms" in source, "唯一查询入口必须是业务查询函数"
    assert not re.search(
        r"\b(SELECT|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|DROP\s+TABLE|CREATE\s+TABLE)\b",
        source,
    ), "编排层里不应出现任何 SQL 语句"


def test_insight_prompt_states_the_hard_rules():
    assert os.path.isfile(INSIGHT_PROMPT_PATH), "正式 Prompt 必须放在 prompts/ 下"
    with open(INSIGHT_PROMPT_PATH, "r", encoding="utf-8") as handle:
        prompt = ai_parser.unescape_prompt(handle.read())

    for phrase in ("你不访问数据库", "不生成SQL", "你只解释", "不编造"):
        assert phrase in prompt, phrase
    for field in ("summary", "highlights", "suggestions"):
        assert field in prompt, field
    assert not re.search(
        r"\b(SELECT|INSERT\s+INTO|DELETE\s+FROM|DROP\s+TABLE|UPDATE\s+\w+\s+SET)\b", prompt
    )


def test_canteen_and_industry_modules_are_untouched():
    """本次任务只碰教室链路：食堂/产教的业务函数与路由保持原样可用。"""
    with open(queries.__file__, "r", encoding="utf-8") as handle:
        source = handle.read()
    # 教室查询函数签名与食堂/产教函数都还在（没有被改名或删除）。
    for name in (
        "def list_available_classrooms(",
        "def canteen_stats(",
        "def industry_skills(",
        "def industry_skill_gap(",
    ):
        assert name in source, name


# ===========================================================================
# 路由：/api/classrooms/search
# ===========================================================================

def test_route_search_returns_candidates_and_ai_explanation(client, conn, monkeypatch):
    weekday, period, free = find_searchable_slot(conn)
    patch_parser(
        monkeypatch,
        room_search_parser(
            capacity=8, has_projector=True, weekday=weekday, period_start=period,
            period_end=period, semester=SEMESTER, limit=100,
        ),
    )
    calls = []
    patch_explainer(monkeypatch, calls, text="AI 解释：按条件找到空教室。")

    response = client.post("/api/classrooms/search", json={"question": EXAMPLE_QUESTION})
    assert response.status_code == 200
    assert response.mimetype == "application/json"
    payload = json.loads(response.get_data(as_text=True))

    assert payload["status"] == "ok"
    data = payload["data"]
    assert data["intent"] == "room_search"
    assert data["query"]["status"] == "available"
    assert data["total"] == len(free)
    assert {room["room_id"] for room in data["rooms"]} == free
    assert data["explanation"]["source"] == "ai"
    assert data["explanation"]["text"] == "AI 解释：按条件找到空教室。"
    # AI 只拿到事实表，拿不到数据库路径。
    assert len(calls) == 1
    assert "campus.db" not in json.dumps(calls[0], ensure_ascii=False)


def test_route_search_accepts_get_with_question(client, conn, monkeypatch):
    weekday, period, free = find_searchable_slot(conn)
    patch_parser(
        monkeypatch,
        room_search_parser(
            capacity=8, has_projector=True, weekday=weekday, period_start=period,
            period_end=period, semester=SEMESTER, limit=100,
        ),
    )
    response = client.get(
        "/api/classrooms/search", query_string={"question": EXAMPLE_QUESTION, "explain": "0"}
    )
    assert response.status_code == 200
    data = json.loads(response.get_data(as_text=True))["data"]
    assert data["total"] == len(free)
    assert data["explanation"]["source"] == "local"


def test_route_explain_zero_skips_the_ai(client, monkeypatch):
    calls = []
    patch_parser(monkeypatch, room_search_parser(capacity=8, has_projector=True))
    patch_explainer(monkeypatch, calls)
    response = client.post(
        "/api/classrooms/search", json={"question": EXAMPLE_QUESTION, "explain": 0}
    )
    assert response.status_code == 200
    assert calls == []


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {},  # 完全没有请求体
        {"json": {}},  # 没有 question
        {"json": {"question": "   "}},
        {"json": {"question": 123}},
        {"json": {"question": "长" * 201}},
        {"json": ["不是对象"]},
        {"json": {"question": EXAMPLE_QUESTION, "explain": "maybe"}},
    ],
)
def test_route_search_rejects_bad_requests(client, request_kwargs):
    response = client.post("/api/classrooms/search", **request_kwargs)
    assert response.status_code == 400
    payload = json.loads(response.get_data(as_text=True))
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "invalid_parameter"
    assert payload["error"]["field"] in ("question", "explain")


def test_route_search_get_without_question_is_400(client):
    response = client.get("/api/classrooms/search")
    assert response.status_code == 400
    payload = json.loads(response.get_data(as_text=True))
    assert payload["error"]["field"] == "question"


def test_route_search_wrong_method_is_405(client):
    response = client.put("/api/classrooms/search", json={"question": EXAMPLE_QUESTION})
    assert response.status_code == 405
    payload = json.loads(response.get_data(as_text=True))
    assert payload["error"]["code"] == "method_not_allowed"


def test_route_search_unsupported_intent_is_400(client, monkeypatch):
    patch_parser(monkeypatch, FakeParser(parsed_result("canteen_analysis", {"canteen_id": 1})))
    response = client.post("/api/classrooms/search", json={"question": "二食堂中午为什么这么挤"})
    assert response.status_code == 400
    payload = json.loads(response.get_data(as_text=True))
    assert payload["error"]["code"] == "unsupported_intent"
    assert payload["error"]["field"] == "intent"


def test_route_search_ai_unavailable_is_503(client, monkeypatch):
    patch_parser(
        monkeypatch,
        FakeParser(
            parsed_result("unknown", degraded=True,
                          error={"code": "missing_config", "message": "缺少 AI_API_KEY"})
        ),
    )
    response = client.post("/api/classrooms/search", json={"question": EXAMPLE_QUESTION})
    assert response.status_code == 503
    payload = json.loads(response.get_data(as_text=True))
    assert payload["error"]["code"] == "ai_unavailable"
    assert "missing_config" in payload["error"]["message"]


def test_route_search_invalid_ai_params_is_400(client, monkeypatch):
    patch_parser(monkeypatch, room_search_parser(capacity=8, period_start=6, period_end=4))
    response = client.post("/api/classrooms/search", json={"question": EXAMPLE_QUESTION})
    assert response.status_code == 400
    payload = json.loads(response.get_data(as_text=True))
    assert payload["error"]["code"] == "invalid_parameter"
    assert payload["error"]["field"] == "period_end"


def test_existing_endpoints_still_work(client):
    """新增路由不影响原有四个接口（食堂与产教模块未改动）。"""
    for path in (
        "/api/classrooms/available?limit=2",
        "/api/canteens/stats?top_slots=1",
        "/api/industry/skills?top=2",
        "/api/industry/gap?major=软件工程&top=2",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.mimetype == "application/json"
