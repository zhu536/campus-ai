# -*- coding: utf-8 -*-
"""CampusAI 食堂分析与预测测试（``backend/forecast.py`` + 三个食堂接口）。

覆盖任务要求：

    1. 查询历史就餐趋势        → test_trend_*
    2. 计算高峰时间段          → test_peak_*
    3. 预测未来午餐人流        → test_forecast_*
    4. 预测方法保持简单        → test_forecast_formula_is_base_plus_weekday_plus_hour
       （历史同期平均值 + 星期修正 + 时间段修正，逐项用参考实现核对）
    5. 禁止复杂机器学习        → test_forecast_module_contains_no_sql_and_no_ml
    6. Python 负责统计与预测   → 全部断言都用直连 sqlite3 的参考实现独立复算
    7. AI 只负责解释结果        → test_ai_*（payload 无 SQL/表名/路径；连接先关闭再调模型）
    8. 不修改教室功能          → test_classroom_endpoints_still_work

两张数据源：

    - **合成库**：14 天（含 2 个周一）、值完全可控，用来做**精确**公式断言；
    - **真实库副本**：``data/campus.db`` 的临时拷贝，用来证明真数据上也能跑。

全部离线：AI 用注入的假解释器，**不会联网**；真实库只读副本，不改动仓库里的库。
"""

import ast
import contextlib
import datetime
import io
import json
import math
import os
import shutil
import sqlite3
import sys
import tokenize

import pytest
import requests

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend import forecast  # noqa: E402
from backend import queries  # noqa: E402
from backend.app import create_app  # noqa: E402
from backend.errors import NotFoundError, ValidationError  # noqa: E402

REAL_DB = os.path.join(PROJECT_ROOT, "data", "campus.db")
FORECAST_SOURCE = forecast.__file__

#: 假 Key：证明「Key 只从配置来」，同时保证任何返回/请求里都不会出现真 Key。
FAKE_API_KEY = "sk-fake-for-tests-do-not-use"

# ---------------------------------------------------------------------------
# 合成数据：2025-09-01 是周一，共 14 天（2 个周一 + 12 个非周一）
# ---------------------------------------------------------------------------

CANTEEN_ID = 1  # 一食堂：有午餐数据
OTHER_CANTEEN_ID = 2  # 二食堂：只有早餐数据，用来验证「某食堂该时段没有记录」
START_DATE = datetime.date(2025, 9, 1)
DAYS = 14
LUNCH_HOURS = (11, 12, 13)
BREAKFAST_HOUR = 8
#: 非周一：11/12/13 点分别 100/200/50 人。
WEEKDAY_PEOPLE = {11: 100, 12: 200, 13: 50}
#: 周一翻倍：11/12/13 点分别 200/400/100 人（星期修正因此非零且可精确复算）。
MONDAY_PEOPLE = {11: 200, 12: 400, 13: 100}
WAIT_MINUTES = {11: 2.0, 12: 5.0, 13: 1.0}
BREAKFAST_PEOPLE = 30
NEXT_MONDAY = "2025-09-15"

SCHEMA = """
CREATE TABLE canteens (
    canteen_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    canteen_name TEXT    NOT NULL UNIQUE,
    location     TEXT,
    capacity     INTEGER NOT NULL CHECK (capacity > 0),
    window_count INTEGER CHECK (window_count IS NULL OR window_count > 0),
    open_time    TEXT,
    close_time   TEXT,
    status       TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed'))
);
CREATE TABLE meal_records (
    record_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    canteen_id   INTEGER NOT NULL REFERENCES canteens(canteen_id) ON DELETE CASCADE,
    record_date  TEXT    NOT NULL,
    record_hour  INTEGER NOT NULL CHECK (record_hour BETWEEN 0 AND 23),
    time_slot    TEXT    NOT NULL
                 CHECK (time_slot IN ('breakfast', 'lunch', 'dinner', 'other')),
    people_count INTEGER NOT NULL CHECK (people_count >= 0),
    wait_minutes REAL    CHECK (wait_minutes IS NULL OR wait_minutes >= 0),
    capacity     INTEGER CHECK (capacity IS NULL OR capacity > 0),
    source       TEXT    NOT NULL DEFAULT 'simulated'
                 CHECK (source IN ('simulated', 'real')),
    created_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (canteen_id, record_date, record_hour)
);
"""


def build_dining_db(path):
    """建一个只含食堂两表的合成库，返回值完全可控（与 scripts/import_data.py 同结构）。"""
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT INTO canteens (canteen_id, canteen_name, location, capacity,"
            " window_count, open_time, close_time, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (CANTEEN_ID, "一食堂", "东区宿舍旁", 1000, 20, "06:00", "20:30", "open"),
                (OTHER_CANTEEN_ID, "二食堂", "西区教学楼旁", 500, 10, "06:00", "20:30", "open"),
            ],
        )
        rows = []
        for offset in range(DAYS):
            day = START_DATE + datetime.timedelta(days=offset)
            date_text = day.isoformat()
            table = MONDAY_PEOPLE if day.isoweekday() == 1 else WEEKDAY_PEOPLE
            for hour in LUNCH_HOURS:
                rows.append(
                    (CANTEEN_ID, date_text, hour, "lunch", table[hour], WAIT_MINUTES[hour], 1000)
                )
            rows.append(
                (OTHER_CANTEEN_ID, date_text, BREAKFAST_HOUR, "breakfast",
                 BREAKFAST_PEOPLE, 1.0, 500)
            )
        connection.executemany(
            "INSERT INTO meal_records (canteen_id, record_date, record_hour, time_slot,"
            " people_count, wait_minutes, capacity) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()
    return path


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

@pytest.fixture()
def dining_db(tmp_path):
    """合成数据库（每个测试独占，值完全可控）。"""
    return build_dining_db(os.path.join(str(tmp_path), "dining.db"))


@pytest.fixture()
def conn(dining_db):
    connection = sqlite3.connect(dining_db)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def client(dining_db):
    application = create_app(db_path=dining_db)
    application.config.update(TESTING=True)
    return application.test_client()


@pytest.fixture(scope="module")
def real_db_path(tmp_path_factory):
    """真实数据库的临时副本（模块级共用；只读，不动仓库里的库）。"""
    if not os.path.isfile(REAL_DB):
        pytest.skip("缺少 data/campus.db，请先执行 python scripts/import_data.py")
    target = os.path.join(str(tmp_path_factory.mktemp("forecast_db")), "campus.db")
    shutil.copyfile(REAL_DB, target)
    return target


@pytest.fixture()
def real_conn(real_db_path):
    connection = sqlite3.connect(real_db_path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def real_client(real_db_path):
    application = create_app(db_path=real_db_path)
    application.config.update(TESTING=True)
    return application.test_client()


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------

class FakeExplainer:
    """假的 AI 解释器：把收到的 payload 记进 ``payloads``，返回一段固定文案。

    实现的是 :class:`backend.forecast.ForecastExplainer` 的**返回契约**
    （``text`` / ``summary`` / ``highlights`` / ``suggestions`` / ``source`` / ``error``），
    用来在完全不联网的前提下检查「模型能拿到什么」。
    """

    def __init__(self, summary="AI 解释：已按结果说明。", error=None):
        self.summary = summary
        self.error = error
        self.payloads = []

    def explain(self, payload):
        self.payloads.append(payload)
        if self.error is not None:
            raise self.error
        return forecast._render_explanation(
            self.summary, ["要点"], ["建议"], source="ai"
        )


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
                {"index": 0, "message": {"role": "assistant", "content": content},
                 "finish_reason": "stop"}
            ],
        },
    )


def make_explainer(responses=(), error=None):
    """构造一个「配置已注入、请求打到假 session」的真 ForecastExplainer（不联网）。"""
    config = {
        "api_key": FAKE_API_KEY,
        "base_url": "https://ai.example.test/v1",
        "model": "fake-model",
        "timeout": 5.0,
        "max_tokens": 512,
        "temperature": 0,
        "prompt_path": os.path.join(PROJECT_ROOT, "prompts", "canteen_insight_generator.md"),
    }
    session = FakeSession(responses=responses, error=error)
    return forecast.ForecastExplainer(config=config, session=session), session


def sent_payload(session):
    """取出真正发给模型的只读事实表（user message 的 JSON）。"""
    assert len(session.calls) == 1
    body = session.calls[0]["json"]
    return json.loads(body["messages"][1]["content"])


def section(report, name):
    assert name in report, "响应里缺少 {} 段：{}".format(name, sorted(report))
    return report[name]


def body_of(response):
    assert response.mimetype == "application/json", response.mimetype
    return json.loads(response.get_data(as_text=True))


# ---------------------------------------------------------------------------
# 1. 历史就餐趋势
# ---------------------------------------------------------------------------

def test_trend_matches_raw_sqlite(conn):
    """趋势序列必须与直连 sqlite3 的参考实现逐日一致。"""
    report = forecast.canteen_trend(conn, {"canteen_id": CANTEEN_ID, "time_slot": "lunch"})
    expected = conn.execute(
        "SELECT record_date, SUM(people_count) AS people, COUNT(*) AS records"
        " FROM meal_records WHERE canteen_id = ? AND time_slot = 'lunch'"
        " GROUP BY record_date ORDER BY record_date",
        (CANTEEN_ID,),
    ).fetchall()

    assert report["summary"]["days"] == len(expected) == DAYS
    for item, row in zip(report["series"], expected):
        assert item["date"] == row["record_date"]
        assert item["people_total"] == row["people"]
        assert item["records"] == row["records"]
        assert item["weekday"] == datetime.date.fromisoformat(row["record_date"]).isoweekday()
        assert item["hours"] == len(LUNCH_HOURS)


def test_trend_summary_and_direction(conn):
    """汇总口径：周一 700 人、其余 350 人 → 日均 400，前后半段一致 → 持平。"""
    report = forecast.canteen_trend(conn, {"canteen_id": CANTEEN_ID, "time_slot": "lunch"})
    summary = report["summary"]

    assert summary["people_total"] == 2 * 700 + 12 * 350 == 5600
    assert summary["avg_people_per_day"] == 400.0
    # 客流单元 = 「日期 × 小时」，均值是单元平均而不是每天合计。
    assert summary["avg_people_per_hour"] == round(5600 / (DAYS * len(LUNCH_HOURS)), 2)
    assert summary["date_start"] == "2025-09-01"
    assert summary["date_end"] == "2025-09-14"
    # 并列时按日期取最大 / 最小，结果稳定可复现。
    assert summary["busiest_date"]["date"] == "2025-09-08"
    assert summary["busiest_date"]["people_total"] == 700
    assert summary["quietest_date"]["date"] == "2025-09-02"
    assert summary["quietest_date"]["people_total"] == 350

    assert report["trend"]["direction"] == "flat"
    assert report["trend"]["change_rate"] == 0.0
    assert report["trend"]["first_half_avg"] == report["trend"]["second_half_avg"] == 400.0


def test_trend_weekday_profile(conn):
    """按星期几聚合：周一 233.33 人/小时，其余 116.67 人/小时。"""
    report = forecast.canteen_trend(conn, {"canteen_id": CANTEEN_ID, "time_slot": "lunch"})
    profile = {item["weekday"]: item for item in report["weekday_profile"]}

    assert sorted(profile) == [1, 2, 3, 4, 5, 6, 7]
    assert profile[1]["weekday_name"] == "周一"
    assert profile[1]["days"] == 2
    assert profile[1]["people_total"] == 1400
    assert profile[1]["avg_people"] == round(1400 / 6, 2)
    assert profile[2]["avg_people"] == round(350 * 2 / 6, 2)
    # 星期几的 `days` 是「有几个不同日期有过记录」，14 天刚好每个星期几都出现。
    assert {item["days"] for item in report["weekday_profile"]} == {2}


def test_trend_empty_window_is_200_with_zeroes(conn):
    report = forecast.canteen_trend(conn, {"date_from": "1999-01-01", "date_to": "1999-01-02"})
    assert report["summary"]["days"] == 0
    assert report["series"] == []
    assert report["summary"]["avg_people_per_day"] is None
    assert report["trend"]["direction"] == "flat"


# ---------------------------------------------------------------------------
# 2. 高峰时间段
# ---------------------------------------------------------------------------

def test_peak_hour_and_window(conn):
    """峰值在 12 点（228.57），默认阈值 0.8 → 只有 12 点自己落在高峰区间。"""
    peak = forecast.canteen_peak_periods(
        conn, {"canteen_id": CANTEEN_ID, "time_slot": "lunch"}
    )

    assert peak["summary"]["peak_hour"] == 12
    assert peak["summary"]["peak_avg_people"] == round(3200 / 14, 2)
    assert peak["summary"]["peak_window"] == "12:00-13:00"
    assert peak["summary"]["peak_slot"] == "lunch"

    hours = {item["hour"]: item for item in peak["hours"]}
    assert sorted(hours) == list(LUNCH_HOURS)
    assert hours[12]["is_peak"] is True
    assert hours[11]["is_peak"] is False and hours[13]["is_peak"] is False
    assert peak["window"]["hours"] == [12]
    assert peak["window"]["label"] == "12:00-13:00"
    assert hours[12]["in_window"] is True and hours[11]["in_window"] is False
    # 占比之和为 1（人流总量的分解）。
    assert sum(item["share"] for item in peak["hours"]) == pytest.approx(1.0, abs=1e-6)
    # 等待时间按记录平均，11/12/13 点分别是 2/5/1 分钟。
    assert hours[11]["avg_wait_minutes"] == 2.0
    assert hours[12]["avg_wait_minutes"] == 5.0


def test_peak_window_ratio_is_configurable(conn):
    """阈值降到 0.4 时，11 点（114.29）也进入高峰，且必须与峰值小时**逐小时相邻**。"""
    wide = forecast.canteen_peak_periods(
        conn, {"canteen_id": CANTEEN_ID, "time_slot": "lunch", "peak_ratio": 0.4}
    )["window"]
    assert wide["hours"] == [11, 12]
    assert wide["label"] == "11:00-13:00"
    assert wide["start_hour"] == 11 and wide["end_hour"] == 12

    narrow = forecast.canteen_peak_periods(
        conn, {"canteen_id": CANTEEN_ID, "time_slot": "lunch", "peak_ratio": 1.0}
    )["window"]
    assert narrow["hours"] == [12]


def test_peak_ranking_and_slots(conn):
    """小时榜按人流降序；早/午/晚餐排行榜里午餐最高。"""
    peak = forecast.canteen_peak_periods(conn, {"canteen_id": CANTEEN_ID})

    assert [item["hour"] for item in peak["ranked_hours"]] == [12, 11, 13]
    averages = [item["avg_people"] for item in peak["ranked_hours"]]
    assert averages == sorted(averages, reverse=True)

    slots = {item["time_slot"]: item for item in peak["slots"]}
    assert set(slots) == {"lunch"}  # 一食堂只有午餐数据
    assert slots["lunch"]["is_peak"] is True
    assert peak["summary"]["peak_slot_name"] == "午餐"


# ---------------------------------------------------------------------------
# 3. 未来午餐人流预测
# ---------------------------------------------------------------------------

def test_forecast_formula_is_base_plus_weekday_plus_hour(conn):
    """逐项复算「历史同期平均值 + 星期修正 + 时间段修正」。

    合成数据：非周一 100/200/50，周一 200/400/100（翻倍）。
    因此 B = 5600/42 = 133.33，ΔW(周一) = +100，
    ΔH(11/12/13) = -19.05 / +95.24 / -76.19。
    预测下一个周一 = 214 + 329 + 157 = 700（正好等于周一历史日均）。
    """
    report = forecast.forecast_lunch_traffic(
        conn, {"canteen_id": CANTEEN_ID, "horizon_days": 2}
    )
    training = report["training"]

    assert report["method"] == forecast.FORECAST_METHOD == "历史同期平均值 + 星期修正 + 时间段修正"
    assert training["base_avg_people"] == pytest.approx(5600 / 42, abs=1e-4)
    assert training["days"] == DAYS
    assert training["hours"] == list(LUNCH_HOURS)

    by_weekday = {item["weekday"]: item for item in training["by_weekday"]}
    by_hour = {item["hour"]: item for item in training["by_hour"]}
    assert by_weekday[1]["adjustment"] == pytest.approx(100.0, abs=1e-4)
    assert by_weekday[1]["avg_people"] == pytest.approx(233.3333, abs=1e-3)
    assert by_hour[11]["adjustment"] == pytest.approx(-19.0476, abs=1e-3)
    assert by_hour[12]["adjustment"] == pytest.approx(95.2381, abs=1e-3)
    assert by_hour[13]["adjustment"] == pytest.approx(-76.1905, abs=1e-3)

    monday, tuesday = report["predictions"]
    assert monday["date"] == NEXT_MONDAY and monday["weekday_name"] == "周一"
    assert [item["predicted_people"] for item in monday["hours"]] == [214, 329, 157]
    assert monday["predicted_people_total"] == 700
    # 每个小时都能用返回的分量原地复算，没有黑箱。
    for item in monday["hours"]:
        raw = item["base"] + item["weekday_adjustment"] + item["hour_adjustment"]
        assert item["predicted_people"] == math.floor(raw + 0.5)

    assert tuesday["date"] == "2025-09-16" and tuesday["weekday_name"] == "周二"
    assert tuesday["predicted_people_total"] == 350
    assert [item["predicted_people"] for item in tuesday["hours"]] == [98, 212, 40]


def test_forecast_horizon_and_target_date(conn):
    """horizon_days 决定长度；target_date 决定起点；默认从数据最后一天之后开始。"""
    default = forecast.forecast_lunch_traffic(conn, {"canteen_id": CANTEEN_ID})
    assert default["total"] == forecast.DEFAULT_HORIZON_DAYS == 3
    assert [item["date"] for item in default["predictions"]] == [
        "2025-09-15", "2025-09-16", "2025-09-17",
    ]

    explicit = forecast.forecast_lunch_traffic(
        conn, {"canteen_id": CANTEEN_ID, "target_date": "2025-10-06", "horizon_days": 1}
    )
    # 2025-10-06 也是周一，预测值与第一个周一相同（同期平均值口径）。
    assert [item["date"] for item in explicit["predictions"]] == ["2025-10-06"]
    assert explicit["predictions"][0]["weekday_name"] == "周一"
    assert explicit["predictions"][0]["predicted_people_total"] == 700

    # 区间截断到 9-07 时，预测从 9-08（周一）开始。
    truncated = forecast.forecast_lunch_traffic(
        conn, {"canteen_id": CANTEEN_ID, "date_from": "2025-09-01", "date_to": "2025-09-07",
               "horizon_days": 2}
    )
    assert [item["date"] for item in truncated["predictions"]] == ["2025-09-08", "2025-09-09"]
    assert truncated["training"]["days"] == 7
    assert truncated["predictions"][0]["predicted_people_total"] == 700


def test_forecast_without_history_returns_empty_with_note(conn):
    """某食堂在该时段没有记录时：不硬凑数字，给出空预测 + 说明。"""
    report = forecast.forecast_lunch_traffic(conn, {"canteen_id": OTHER_CANTEEN_ID})
    assert report["predictions"] == []
    assert report["total"] == 0
    assert report["training"]["base_avg_people"] is None
    assert report["notes"] and "没有可用于训练的历史记录" in report["notes"][0]


def test_forecast_is_deterministic_and_non_negative(conn):
    params = {"canteen_id": CANTEEN_ID, "horizon_days": 7}
    first = forecast.forecast_lunch_traffic(conn, params)
    second = forecast.forecast_lunch_traffic(conn, params)

    assert first == second, "同样的输入必须给同样的预测（无随机、无训练）"
    for item in first["predictions"]:
        assert item["predicted_people_total"] == sum(
            hour["predicted_people"] for hour in item["hours"]
        )
        for hour in item["hours"]:
            assert isinstance(hour["predicted_people"], int)
            assert hour["predicted_people"] >= 0


# ---------------------------------------------------------------------------
# 4. 参数校验与错误
# ---------------------------------------------------------------------------

def test_unknown_canteen_returns_404(conn):
    with pytest.raises(NotFoundError) as excinfo:
        forecast.canteen_trend(conn, {"canteen_id": 999})
    assert excinfo.value.field == "canteen_id"
    assert "一食堂" in excinfo.value.available


@pytest.mark.parametrize(
    "params, field",
    [
        ({"canteen_id": 0}, "canteen_id"),
        ({"canteen_id": "abc"}, "canteen_id"),
        ({"date_from": "2025/09/01"}, "date_from"),
        ({"date_from": "2025-02-30"}, "date_from"),
        ({"date_from": "2025-09-10", "date_to": "2025-09-01"}, "date_to"),
        ({"time_slot": "brunch"}, "time_slot"),
        ({"target_date": "2025-13-01"}, "target_date"),
        ({"horizon_days": 0}, "horizon_days"),
        ({"horizon_days": forecast.MAX_HORIZON_DAYS + 1}, "horizon_days"),
        ({"peak_ratio": 0}, "peak_ratio"),
        ({"peak_ratio": 1.5}, "peak_ratio"),
        ({"peak_ratio": "wide"}, "peak_ratio"),
    ],
)
def test_invalid_params_are_rejected(conn, params, field):
    with pytest.raises(ValidationError) as excinfo:
        forecast.canteen_trend(conn, params)
    assert excinfo.value.field == field
    assert excinfo.value.status_code == 400


def test_non_mapping_params_are_rejected(conn):
    with pytest.raises(ValidationError):
        forecast.canteen_trend(conn, ["canteen_id", 1])


# ---------------------------------------------------------------------------
# 5. 真实数据库（data/campus.db 的临时副本）
# ---------------------------------------------------------------------------

def test_real_database_trend_and_peak_are_sane(real_conn, real_db_path):
    report = forecast.analyze_canteen(
        {"canteen_id": 1}, db_path=real_db_path, explain=False
    )
    trend, peak = report["trend"], report["peak"]

    assert trend["summary"]["days"] > 30
    assert trend["summary"]["people_total"] > 0
    assert trend["trend"]["direction"] in ("up", "down", "flat")
    assert len(trend["series"]) == trend["summary"]["days"]

    assert peak["summary"]["peak_hour"] in range(6, 22)
    assert peak["summary"]["peak_window"]
    assert peak["window"]["start_hour"] <= peak["summary"]["peak_hour"] <= peak["window"]["end_hour"]
    for item in peak["hours"]:
        assert item["avg_people"] >= 0
        assert 0.0 <= item["share"] <= 1.0


def test_real_database_forecast_matches_independent_reference(real_conn, real_db_path):
    """真实数据上，用直连 sqlite3 的参考实现独立复算预测值（允许 ±1 的浮点取整差）。"""
    slot, canteen_id, horizon = "lunch", 1, 3
    report = forecast.forecast_lunch_traffic(
        real_conn, {"canteen_id": canteen_id, "time_slot": slot, "horizon_days": horizon}
    )

    rows = real_conn.execute(
        "SELECT record_date, record_hour, people_count FROM meal_records"
        " WHERE canteen_id = ? AND time_slot = ?",
        (canteen_id, slot),
    ).fetchall()
    cells = {}
    for row in rows:
        key = (row["record_date"], row["record_hour"])
        cells[key] = cells.get(key, 0) + row["people_count"]

    values = list(cells.values())
    base = sum(values) / len(values)
    weekday_values, hour_values = {}, {}
    for (date_text, hour), people in cells.items():
        weekday = datetime.date.fromisoformat(date_text).isoweekday()
        weekday_values.setdefault(weekday, []).append(people)
        hour_values.setdefault(hour, []).append(people)
    weekday_avg = {key: sum(v) / len(v) for key, v in weekday_values.items()}
    hour_avg = {key: sum(v) / len(v) for key, v in hour_values.items()}

    start = datetime.date.fromisoformat(report["predictions"][0]["date"])
    for offset, prediction in enumerate(report["predictions"]):
        day = start + datetime.timedelta(days=offset)
        weekday = day.isoweekday()
        assert prediction["date"] == day.isoformat()
        expected_total = 0
        for hour in sorted(hour_values):
            raw = max(
                0.0,
                base + (weekday_avg.get(weekday, base) - base) + (hour_avg[hour] - base),
            )
            expected = math.floor(raw + 0.5)
            actual = next(
                item["predicted_people"] for item in prediction["hours"] if item["hour"] == hour
            )
            assert abs(actual - expected) <= 1
            expected_total += expected
        assert abs(prediction["predicted_people_total"] - expected_total) <= len(hour_values)


def test_real_database_unknown_canteen_404(real_conn):
    with pytest.raises(NotFoundError):
        forecast.forecast_lunch_traffic(real_conn, {"canteen_id": 9999})


# ---------------------------------------------------------------------------
# 6. HTTP 接口
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path, section_name",
    [
        ("/api/canteens/trend", "trend"),
        ("/api/canteens/peak", "peak"),
        ("/api/canteens/forecast", "forecast"),
    ],
)
def test_routes_return_each_section(client, path, section_name):
    response = client.get(path, query_string={"canteen_id": CANTEEN_ID, "explain": "0"})
    assert response.status_code == 200, path
    payload = body_of(response)
    assert payload["status"] == "ok"

    data = payload["data"]
    assert section_name in data
    assert data["query"]["canteen_id"] == CANTEEN_ID
    assert data["explanation"]["source"] == "local"
    # 只算被请求的那一段，不做多余工作。
    assert [name for name in forecast.SECTIONS if name in data] == [section_name]


def test_route_forecast_supports_horizon_and_slot(client):
    response = client.get(
        "/api/canteens/forecast",
        query_string={"canteen_id": CANTEEN_ID, "horizon_days": 2, "explain": "0"},
    )
    data = body_of(response)["data"]
    assert [item["date"] for item in data["forecast"]["predictions"]] == [
        "2025-09-15", "2025-09-16",
    ]
    assert data["forecast"]["training"]["time_slot"] == "lunch"


def test_route_rejects_bad_parameters(client):
    cases = [
        ({"canteen_id": 0}, "canteen_id"),
        ({"time_slot": "brunch"}, "time_slot"),
        ({"date_from": "2025-09-10", "date_to": "2025-09-01"}, "date_from"),
        ({"horizon_days": 31}, "horizon_days"),
        ({"peak_ratio": 2}, "peak_ratio"),
        ({"target_date": "2025-09-31"}, "target_date"),
    ]
    for params, field in cases:
        query = dict(params, explain="0")
        response = client.get("/api/canteens/forecast", query_string=query)
        assert response.status_code == 400, params
        payload = body_of(response)
        assert payload["error"]["code"] == "invalid_parameter"
        assert payload["error"]["field"] == field, params


def test_route_unknown_canteen_is_404(client):
    response = client.get("/api/canteens/peak", query_string={"canteen_id": 999, "explain": "0"})
    assert response.status_code == 404
    payload = body_of(response)
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["field"] == "canteen_id"
    assert "一食堂" in payload["error"]["available"]


def test_classroom_endpoints_still_work(real_client):
    """本任务不修改教室功能：老接口在同一个应用里必须照常返回。"""
    response = real_client.get("/api/classrooms/available", query_string={"capacity": 10, "limit": 2})
    assert response.status_code == 200
    payload = body_of(response)
    assert payload["status"] == "ok"
    assert payload["data"]["total"] >= len(payload["data"]["rooms"])
    rules = {rule.rule for rule in real_client.application.url_map.iter_rules()}
    assert {"/api/canteens/stats", "/api/canteens/trend", "/api/canteens/peak",
            "/api/canteens/forecast"} <= rules


# ---------------------------------------------------------------------------
# 7. AI 只负责解释结果（不查库、不算数、不生成 SQL）
# ---------------------------------------------------------------------------

def test_ai_payload_contains_no_sql_table_names_or_paths(dining_db):
    """交给模型的事实表里不能出现 SQL、表名、数据库路径或明细数据。"""
    explainer, session = make_explainer([insight_response(summary="一食堂预计 700 人次。")])
    report = forecast.analyze_canteen(
        {"canteen_id": CANTEEN_ID},
        db_path=dining_db,
        question="下周一中午一食堂挤不挤？",
        explainer=explainer,
    )

    assert report["explanation"]["source"] == "ai"
    assert report["explanation"]["summary"] == "一食堂预计 700 人次。"
    # 请求确实打到配置里的地址，Key 只出现在 Authorization 头里。
    call = session.calls[0]
    assert call["url"] == "https://ai.example.test/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer {}".format(FAKE_API_KEY)
    assert FAKE_API_KEY not in json.dumps(report, ensure_ascii=False)

    payload = sent_payload(session)
    assert payload["question"] == "下周一中午一食堂挤不挤？"
    assert set(payload) <= {"question", "scope", "notes", "trend", "peak", "forecast"}
    # 预测部分只给结果与分量，不给任何原始明细。
    assert payload["forecast"]["targets"][0]["predicted_people_total"] == 700
    assert "records" not in json.dumps(payload["trend"])

    dumped = json.dumps(payload, ensure_ascii=False).lower()
    for forbidden in ("select", "insert into", "meal_records", "canteens ", "campus.db",
                      "sqlite", "import sqlite3", "from meal"):
        assert forbidden not in dumped, forbidden


def test_ai_is_called_only_after_the_database_connection_is_closed(dining_db, monkeypatch):
    """红线：模型调用必须发生在 SQLite 连接关闭之后（否则「AI 不碰数据库」无从保证）。"""
    timeline = []
    real_connection = forecast.connection

    @contextlib.contextmanager
    def traced_connection(path):
        with real_connection(path) as connection:
            yield connection
        timeline.append("connection_closed")

    class TimelineExplainer:
        def explain(self, payload):
            timeline.append("ai_called")
            return forecast._render_explanation("ok", [], [], source="ai")

    monkeypatch.setattr(forecast, "connection", traced_connection)
    forecast.analyze_canteen(
        {"canteen_id": CANTEEN_ID}, db_path=dining_db, explainer=TimelineExplainer()
    )
    assert timeline == ["connection_closed", "ai_called"]


def test_ai_output_with_sql_is_rejected_and_falls_back(dining_db):
    """模型就算吐 SQL 片段也没用：整条解释被拒并降级到 Python 兜底文案。"""
    explainer, _ = make_explainer([insight_response(summary="SELECT * FROM meal_records")])
    report = forecast.analyze_canteen(
        {"canteen_id": CANTEEN_ID}, db_path=dining_db, explainer=explainer
    )
    explanation = report["explanation"]
    assert explanation["source"] == "local"
    assert explanation["error"]["code"] == forecast.ERROR_UNSAFE_EXPLANATION
    assert "SELECT" not in explanation["text"]


def test_ai_network_failure_falls_back_to_local_explanation(dining_db):
    explainer, _ = make_explainer(error=requests.exceptions.Timeout("slow"))
    report = forecast.analyze_canteen(
        {"canteen_id": CANTEEN_ID}, db_path=dining_db, explainer=explainer
    )
    explanation = report["explanation"]
    assert explanation["source"] == "local"
    assert explanation["error"]["code"] in ("network_error", "explain_failed")
    assert explanation["text"] and explanation["summary"]


def test_ai_invalid_json_falls_back_to_local_explanation(dining_db):
    explainer, _ = make_explainer([insight_response(content="这不是 JSON")])
    report = forecast.analyze_canteen(
        {"canteen_id": CANTEEN_ID}, db_path=dining_db, explainer=explainer
    )
    assert report["explanation"]["source"] == "local"
    assert report["explanation"]["error"]["code"] == "invalid_json"


def test_explain_false_never_constructs_the_ai(dining_db, monkeypatch):
    def explode(*args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("explain=False 时不应构造 AI 解释器")

    monkeypatch.setattr(forecast, "ForecastExplainer", explode)
    report = forecast.analyze_canteen({"canteen_id": CANTEEN_ID}, db_path=dining_db, explain=False)
    assert report["explanation"]["source"] == "local"
    # 兜底解释同样只引用已算好的结果，并且包含关键结论。
    assert "5600" in report["explanation"]["text"]
    assert "12 点" in report["explanation"]["text"]


def test_local_explanation_mentions_method_and_window(dining_db):
    report = forecast.analyze_canteen(
        {"canteen_id": CANTEEN_ID, "horizon_days": 2}, db_path=dining_db, explain=False
    )
    text = report["explanation"]["text"]
    assert forecast.FORECAST_METHOD in text
    assert "12:00-13:00" in text
    assert "2025-09-15" in text and "700" in text
    # 兜底文案里的数字必须真的被格式化进去，不能漏出占位符。
    assert "{}" not in text and "{0}" not in text
    assert "。；" not in text


# ---------------------------------------------------------------------------
# 8. 源码级红线：Python 统计/预测，不写 SQL，不引入任何机器学习
# ---------------------------------------------------------------------------

def _forecast_imports():
    with open(FORECAST_SOURCE, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return imported


def _code_only(path):
    """去掉注释与字符串字面量，只留可执行代码（文档里提到 XGBoost 不算违规）。"""
    with open(path, "r", encoding="utf-8") as handle:
        tokens = tokenize.generate_tokens(io.StringIO(handle.read()).readline)
        return " ".join(
            token.string
            for token in tokens
            if token.type not in (tokenize.COMMENT, tokenize.STRING)
        )


def test_forecast_module_contains_no_sql_and_no_ml():
    """预测模块既不能自己查库/写 SQL，也不能引入任何机器学习依赖。"""
    imported = _forecast_imports()
    assert "sqlite3" not in imported
    assert not imported & {"sklearn", "xgboost", "torch", "tensorflow", "numpy", "pandas",
                           "transformers", "lightgbm", "catboost"}

    with open(FORECAST_SOURCE, "r", encoding="utf-8") as handle:
        source = handle.read()
    for keyword in ("SELECT ", "INSERT INTO", "UPDATE ", "DELETE FROM", "GROUP BY", "JOIN "):
        assert keyword not in source.upper(), keyword


def test_prediction_is_only_average_plus_two_corrections():
    """方法表里只有均值与加减法：代码里不出现训练/拟合/梯度/树/神经网络。"""
    code = _code_only(FORECAST_SOURCE).lower()
    assert forecast.FORECAST_METHOD == "历史同期平均值 + 星期修正 + 时间段修正"
    for banned in ("xgboost", "transformer", "神经网络", "梯度下降", "backward(",
                   "fit(", "train(", "predict_proba", "sklearn", "torch"):
        assert banned not in code, banned


def test_queries_layer_grid_is_pure_filtering(conn):
    """数据底座只筛选不计算：行数、总量、容量回退都必须与直连 sqlite3 一致。"""
    grid = queries.canteen_meal_grid(conn, {"canteen_id": CANTEEN_ID, "time_slot": "lunch"})
    assert grid["total"] == DAYS * len(LUNCH_HOURS)
    assert grid["summary"]["records"] == grid["total"]
    assert grid["summary"]["people_total"] == 5600
    assert grid["summary"]["date_start"] == "2025-09-01"
    assert grid["summary"]["date_end"] == "2025-09-14"
    assert grid["canteens"] == [{"canteen_id": CANTEEN_ID, "canteen_name": "一食堂"}]
    assert {item["capacity"] for item in grid["records"]} == {1000}
    assert grid["records"][0]["record_date"] <= grid["records"][-1]["record_date"]
