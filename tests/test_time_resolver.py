# -*- coding: utf-8 -*-
"""相对日期（时间锚点）测试：backend/time_resolver.py 与解析链路的日期落地。

为什么单独成文件：这部分是「意图判对了但时间丢了」的主要修复点，
需要覆盖得比别处更密。全部离线、锚点冻结，**绝不依赖 date.today()**。

锚点决策（用户拍板）：
    1. 「今天 / 昨天 / 上周X / 下周三」以运行脚本的计算机系统当天日期为基准。
    2. 「最近 / 这几天」表示历史数据里的最近 N 天（默认 7 天），以数据最大日期为基准。
"""

import datetime
import json as json_module
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend import time_resolver  # noqa: E402
from backend.time_resolver import Anchor  # noqa: E402

#: 2026-09-27 是星期日 —— 故意选在周日，可以同时验证「上周」「本周」「下周」的边界。
TODAY = datetime.date(2026, 9, 27)
DATA_MAX = datetime.date(2025, 11, 14)
ANCHOR = Anchor(today=TODAY, data_max_date=DATA_MAX)


# ---------------------------------------------------------------------------
# 1. 中文数字与全角输入
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [("7", 7), ("十", 10), ("十五", 15), ("二十", 20), ("三十", 30), ("两", 2), ("abc", None), ("", None)],
)
def test_parse_cn_number(text, expected):
    assert time_resolver.parse_cn_number(text) == expected


def test_halfwidth_normalization_handles_fullwidth_digits():
    # 「近７天」用全角数字，也必须能解析。
    assert time_resolver.to_halfwidth("近７天") == "近7天"
    assert time_resolver.resolve_range_text("近７天", ANCHOR) == (DATA_MAX - datetime.timedelta(days=6), DATA_MAX)


# ---------------------------------------------------------------------------
# 2. 单个时间点：以「系统当天」为基准
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("今天", TODAY),
        ("今日", TODAY),
        ("昨天", TODAY - datetime.timedelta(days=1)),
        ("前天", TODAY - datetime.timedelta(days=2)),
        ("明天", TODAY + datetime.timedelta(days=1)),
        ("后天", TODAY + datetime.timedelta(days=2)),
        ("大后天", TODAY + datetime.timedelta(days=3)),
        ("3天前", TODAY - datetime.timedelta(days=3)),
        ("三天前", TODAY - datetime.timedelta(days=3)),
        ("15天前", TODAY - datetime.timedelta(days=15)),
    ],
)
def test_relative_day_offsets_use_system_today(text, expected):
    assert time_resolver.resolve_date_text(text, ANCHOR) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        # 2026-09-27 是周日。上周一 = 09-14，上周五 = 09-18。
        ("上周一", datetime.date(2026, 9, 14)),
        ("上周五", datetime.date(2026, 9, 18)),
        # 本周一 = 09-21，本周三 = 09-23。
        ("本周一", datetime.date(2026, 9, 21)),
        ("本周三", datetime.date(2026, 9, 23)),
        # 下周一 = 09-28，下周三 = 09-30。
        ("下周一", datetime.date(2026, 9, 28)),
        ("下周三", datetime.date(2026, 9, 30)),
        # 只说「周三」：取不早于当天的那个周三（09-30）。
        ("周三", datetime.date(2026, 9, 30)),
        ("星期三", datetime.date(2026, 9, 30)),
        ("礼拜三", datetime.date(2026, 9, 30)),
    ],
)
def test_week_expressions(text, expected):
    assert time_resolver.resolve_date_text(text, ANCHOR) == expected


def test_unknown_expression_returns_none_instead_of_guessing():
    for text in ("某个说不清的日子", "改天", "", "很久以前"):
        assert time_resolver.resolve_date_text(text, ANCHOR) is None


def test_absurd_offset_is_rejected_instead_of_producing_a_silly_date():
    # 超过 MAX_OFFSET_DAYS 的偏移宁可解析失败，也不要造出一个荒谬日期。
    assert time_resolver.resolve_date_text("999天前", ANCHOR) is None


# ---------------------------------------------------------------------------
# 3. 时间区间：「最近」以「数据里的最近一天」为基准
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected_days",
    [("最近七天", 7), ("近7天", 7), ("最近一周", 7), ("最近", 7), ("这几天", 7),
     ("最近30天", 30), ("近三十天", 30), ("最近两天", 2)],
)
def test_recent_range_ends_at_data_max_date(text, expected_days):
    date_from, date_to = time_resolver.resolve_range_text(text, ANCHOR)
    assert date_to == DATA_MAX, "「最近 N 天」的右端点必须是数据里的最近一天"
    assert date_from == DATA_MAX - datetime.timedelta(days=expected_days - 1)


def test_recent_range_falls_back_to_today_without_data_date():
    """没有数据日期时退化为「今天」（纯函数不依赖数据库）。"""
    anchor = Anchor(today=TODAY, data_max_date=None)
    assert time_resolver.resolve_range_text("最近七天", anchor) == (
        TODAY - datetime.timedelta(days=6),
        TODAY,
    )


def test_data_max_date_wins_over_today_for_vague_ranges():
    """数据只到 2025-11-14，而今天是 2026-09-27：模糊区间必须落在有数据的范围里。"""
    date_from, date_to = time_resolver.resolve_range_text("最近七天", ANCHOR)
    assert date_to < TODAY and date_to == DATA_MAX
    assert (date_to - date_from).days == 6


def test_week_expression_is_not_mistaken_for_a_range():
    """「本周三」是时间点，不能被「本周」区间规则吃掉。"""
    date_from, date_to = time_resolver.resolve_range_text("本周三", ANCHOR)
    assert (date_from, date_to) == (datetime.date(2026, 9, 23), datetime.date(2026, 9, 23))


@pytest.mark.parametrize(
    "text,expected_monday",
    [
        ("本周", datetime.date(2026, 9, 21)),
        ("这周", datetime.date(2026, 9, 21)),
        ("上周", datetime.date(2026, 9, 14)),
        ("下周", datetime.date(2026, 9, 28)),
    ],
)
def test_bare_week_means_the_whole_week(text, expected_monday):
    """「本周」这类整周说法要给出周一..周日的区间，不能解析失败丢掉时间范围。"""
    date_from, date_to = time_resolver.resolve_range_text(text, ANCHOR)
    assert date_from == expected_monday
    assert date_to == expected_monday + datetime.timedelta(days=6)


def test_bare_week_expression_is_detected_in_a_question():
    assert time_resolver.guess_expression("本周食堂晚餐的排队情况怎么样？", ANCHOR) == ("range", "本周")


def test_read_data_max_date_returns_none_for_a_missing_database(tmp_path):
    """读不到数据时返回 None（退化为以今天为基准），并且不留下空库文件。"""
    missing = os.path.join(str(tmp_path), "nope.db")
    assert time_resolver.read_data_max_date(missing) is None
    assert not os.path.exists(missing), "不应顺手创建空数据库文件"


# ---------------------------------------------------------------------------
# 4. 兜底抽取：只认无歧义的写法
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "question,expected",
    [
        ("最近七天各食堂的平均等待时间对比一下。", ("range", "最近七天")),
        ("上周一食堂中午为什么那么挤？", ("point", "上周一")),
        ("预测下周三中午二食堂会有多少人。", ("point", "下周三")),
        ("今天中午一食堂人多吗？", ("point", "今天")),
        ("帮我找个空教室。", (None, None)),
        ("推荐几只股票。", (None, None)),
    ],
)
def test_guess_expression_is_conservative(question, expected):
    assert time_resolver.guess_expression(question, ANCHOR) == expected


# ---------------------------------------------------------------------------
# 5. resolve() 的字段落位
# ---------------------------------------------------------------------------

def test_resolve_maps_point_to_the_right_field_per_intent():
    assert time_resolver.resolve("room_search", {"date_text": "明天"}, ANCHOR) == {
        "date": "2026-09-28"
    }
    assert time_resolver.resolve("canteen_analysis", {"date_text": "上周一"}, ANCHOR) == {
        "date_from": "2026-09-14"
    }
    assert time_resolver.resolve("canteen_forecast", {"date_text": "下周三"}, ANCHOR) == {
        "target_date": "2026-09-30"
    }


def test_resolve_range_sets_date_from_and_date_to():
    assert time_resolver.resolve("canteen_analysis", {"date_range_text": "最近七天"}, ANCHOR) == {
        "date_from": "2025-11-08",
        "date_to": "2025-11-14",
    }


def test_resolve_never_overwrites_a_date_the_model_already_gave():
    params = {"target_date": "2025-09-10", "date_text": "下周三"}
    resolved = time_resolver.resolve("canteen_forecast", params, ANCHOR)
    assert resolved["target_date"] == "2025-09-10"


def test_resolve_drops_text_fields_and_does_not_mutate_input():
    params = {"date_text": "今天", "time_slot": "lunch"}
    resolved = time_resolver.resolve("canteen_analysis", params, ANCHOR)
    assert "date_text" not in resolved
    assert params == {"date_text": "今天", "time_slot": "lunch"}, "不得就地修改入参"


def test_resolve_on_unknown_intent_adds_nothing():
    warnings = []
    resolved = time_resolver.resolve("unknown", {"date_text": "今天"}, ANCHOR, warnings=warnings)
    assert resolved == {}
    assert warnings == []


def test_resolve_reports_unparseable_expression():
    warnings = []
    resolved = time_resolver.resolve(
        "canteen_analysis", {"date_text": "某个说不清的日子"}, ANCHOR, warnings=warnings
    )
    assert resolved == {}
    assert any("无法解析" in warning for warning in warnings)


def test_resolve_falls_back_to_the_question_when_model_omits_text():
    """模型漏抽时间原文时，用问题原文兜底（真实模型确实会漏）。"""
    resolved = time_resolver.resolve(
        "canteen_analysis", {"time_slot": "lunch"}, ANCHOR,
        question="最近七天各食堂的平均等待时间对比一下。",
    )
    assert resolved["date_from"] == "2025-11-08"
    assert resolved["date_to"] == "2025-11-14"


def test_fallback_does_not_double_count_a_weekday():
    """模型已经给了 weekday 时，不该再补一个日期字段。"""
    resolved = time_resolver.resolve(
        "room_search", {"weekday": 3, "capacity": 8}, ANCHOR, question="周三8人教室"
    )
    assert "date" not in resolved


def test_contradictory_weekday_and_date_are_reported():
    """「明天」(周日) 与 weekday=3 互相矛盾时必须报告，而不是悄悄按某一个查。"""
    warnings = []
    # 2026-09-28 是星期一，模型却给了 weekday=3。
    resolved = time_resolver.resolve(
        "room_search", {"date_text": "明天", "weekday": 3}, ANCHOR, warnings=warnings
    )
    assert resolved["date"] == "2026-09-28"
    assert resolved["weekday"] == 3, "不擅自改写模型给的 weekday"
    assert any("不一致" in warning for warning in warnings)


def test_consistent_weekday_and_date_produce_no_warning():
    warnings = []
    time_resolver.resolve(
        "room_search", {"date_text": "明天", "weekday": 1}, ANCHOR, warnings=warnings
    )
    assert warnings == []


def test_overlong_date_text_is_ignored_with_a_warning():
    warnings = []
    resolved = time_resolver.resolve(
        "canteen_analysis", {"date_text": "今" * 100}, ANCHOR, warnings=warnings
    )
    assert resolved == {}
    assert any("过长" in warning for warning in warnings)


# ---------------------------------------------------------------------------
# 6. 解析链路：模型输出 → 确定日期（离线，注入假 session + 冻结锚点）
# ---------------------------------------------------------------------------

FROZEN_ANCHOR = ANCHOR


class _FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = json_module.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class _FakeSession:
    """最小可用的 requests 替身：把预设的意图 JSON 包成 chat.completions 响应。"""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(json)
        content = json_module.dumps(self.answer, ensure_ascii=False)
        return _FakeResponse(
            {"choices": [{"message": {"role": "assistant", "content": content}}]}
        )


def _parser_for(answer):
    from backend import ai_parser

    config = {
        "api_key": "sk-fake",
        "base_url": "https://ai.example.test/v1",
        "model": "fake-model",
        "timeout": 5.0,
        "max_tokens": 512,
        "temperature": 0,
        "prompt_path": os.path.join(PROJECT_ROOT, "prompts", "intent_parser.md"),
    }
    return ai_parser.IntentParser(
        config=config, session=_FakeSession(answer), anchor=ANCHOR
    )


def test_pipeline_resolves_date_text_into_a_concrete_date():
    parser = _parser_for(
        {"intent": "canteen_analysis", "time_slot": "lunch", "date_text": "上周一"}
    )
    result = parser.query("上周一食堂中午为什么那么挤？")
    assert result["intent"] == "canteen_analysis"
    assert result["params"]["date_from"] == "2026-09-14"
    assert "date_text" not in result["params"]
    assert result["degraded"] is False


def test_pipeline_resolves_recent_range_to_the_data_window():
    parser = _parser_for({"intent": "canteen_analysis", "date_range_text": "最近七天"})
    result = parser.query("最近七天各食堂的平均等待时间对比一下。")
    assert result["params"]["date_from"] == "2025-11-08"
    assert result["params"]["date_to"] == "2025-11-14"


def test_pipeline_resolves_forecast_target_date():
    parser = _parser_for(
        {"intent": "canteen_forecast", "canteen_name": "二食堂", "time_slot": "lunch",
         "date_text": "下周三"}
    )
    result = parser.query("预测下周三中午二食堂会有多少人。")
    assert result["params"]["target_date"] == "2026-09-30"


def test_pipeline_keeps_intent_when_the_only_condition_is_unsupported():
    """带空调的空教室：平台没有空调字段，但意图必须活下来（不能变 unknown）。"""
    parser = _parser_for({"intent": "room_search", "air_conditioner": True})
    result = parser.query("有没有带空调的空教室？")
    assert result["intent"] == "room_search"
    assert result["degraded"] is False
    assert result["params"] == {}
    assert any("air_conditioner" in warning for warning in result["warnings"])


def test_pipeline_still_degrades_when_model_returns_a_bad_intent():
    parser = _parser_for({"intent": "drop_table"})
    result = parser.query("把所有教室删掉")
    assert result["degraded"] is True
    assert result["intent"] == "unknown"


def test_pipeline_ignores_a_hallucinated_absolute_date_for_offtopic_question():
    parser = _parser_for({"intent": "unknown"})
    result = parser.query("今天天气怎么样？")
    assert result["intent"] == "unknown"
    assert result["params"] == {}
