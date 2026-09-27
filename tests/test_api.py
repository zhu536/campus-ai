# -*- coding: utf-8 -*-
"""CampusAI 基础 Flask API 测试。

覆盖任务里的 9 条要求：
    1. Flask 应用工厂可用
    2. SQLite 只读查询 data/campus.db（测试用临时副本，不动真实库）
    3. 参数化查询：注入字符串被当成数据（结果为空/400），不是 SQL 代码
    4. 不接 AI：实现层只依赖标准库 + Flask，路由不调用任何模型
    5. 四个接口都返回 JSON
    6. 非法参数返回合理的 HTTP 状态码（400 / 404 / 405 / 503）
    7. 业务查询函数与 Flask 路由分离：业务函数可直接用 sqlite3 连接调用
    8. 覆盖四个接口的正常路径与边界
    9. 不修改 frontend（测试只读 data/，不触碰 frontend/）

测试数据来源：把真实库 ``data/campus.db`` 复制到临时目录后只读使用，
因此既能验证真实数据分布，也不会写坏仓库里的数据库。
"""

import json
import os
import shutil
import sqlite3
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend import queries  # noqa: E402
from backend.app import create_app  # noqa: E402

REAL_DB = os.path.join(PROJECT_ROOT, "data", "campus.db")
SEMESTER = "2025-2026-1"

pytestmark = pytest.mark.skipif(
    not os.path.isfile(REAL_DB),
    reason="缺少 data/campus.db，请先执行 python scripts/import_data.py",
)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    """真实数据库的临时副本（模块级共用；只读，不产生 -wal/-shm）。"""
    target = os.path.join(str(tmp_path_factory.mktemp("api_db")), "campus.db")
    shutil.copyfile(REAL_DB, target)
    return target


@pytest.fixture()
def conn(db_path):
    """直连 sqlite3 连接，供「业务层分离」的断言与参考数据查询使用。"""
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


def body_of(response):
    """解析 JSON 响应（顺带证明响应确实是 JSON）。"""
    assert response.mimetype == "application/json", response.mimetype
    return json.loads(response.get_data(as_text=True))


def call(client, path, **params):
    response = client.get(path, query_string=params)
    return response.status_code, body_of(response)


def rooms_matching(conn, capacity=None, projector=None, computer=None, room_type=None,
                    weekday=None, period_start=None, period_end=None, semester=SEMESTER):
    """按 docs/DATABASE.md 的三步查询独立算出期望结果（参考实现，全部参数化）。"""
    sql = "SELECT room_id, room_name, capacity, floor FROM classrooms WHERE status = 'available'"
    params = []
    if capacity is not None:
        sql += " AND capacity >= ?"
        params.append(capacity)
    if projector is not None:
        sql += " AND has_projector >= ?"
        params.append(projector)
    if computer is not None:
        sql += " AND has_computer >= ?"
        params.append(computer)
    if room_type is not None:
        sql += " AND room_type = ?"
        params.append(room_type)

    if weekday is None:
        return conn.execute(sql, params).fetchall()

    occupied = [
        row["room_id"]
        for row in conn.execute(
            "SELECT DISTINCT room_id FROM schedules WHERE semester = ? AND weekday = ?"
            " AND period_start <= ? AND period_end >= ?",
            (semester, weekday, period_end, period_start),
        )
    ]
    if occupied:
        sql += " AND room_id NOT IN ({})".format(",".join("?" * len(occupied)))
        params.extend(occupied)
    return conn.execute(sql, params).fetchall()



# ---------------------------------------------------------------------------
# 元信息
# ---------------------------------------------------------------------------

def test_app_factory_uses_given_database(app, db_path):
    assert app.config["DATABASE"] == db_path
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert {
        "/api/classrooms/available",
        "/api/canteens/stats",
        "/api/industry/skills",
        "/api/industry/gap",
    } <= rules


def test_unknown_route_returns_json_404(client):
    status, payload = call(client, "/api/does-not-exist")
    assert status == 404
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "http_error"


def test_wrong_method_returns_405(client):
    response = client.post("/api/classrooms/available")
    assert response.status_code == 405
    payload = json.loads(response.get_data(as_text=True))
    assert payload["error"]["code"] == "method_not_allowed"


def test_missing_database_returns_503(tmp_path):
    missing = os.path.join(str(tmp_path), "not-there.db")
    application = create_app(db_path=missing)
    application.config.update(TESTING=True)
    response = application.test_client().get("/api/canteens/stats")
    assert response.status_code == 503
    payload = json.loads(response.get_data(as_text=True))
    assert payload["error"]["code"] == "database_unavailable"


# ---------------------------------------------------------------------------
# 1. GET /api/classrooms/available
# ---------------------------------------------------------------------------

def test_available_classrooms_basic(client, conn):
    status, payload = call(client, "/api/classrooms/available", capacity=50, limit=5)
    assert status == 200
    assert payload["status"] == "ok"

    data = payload["data"]
    assert data["total"] == data["summary"]["available_count"]
    assert len(data["rooms"]) == 5

    expected = rooms_matching(conn, capacity=50)
    assert data["summary"]["candidate_count"] == len(expected)

    for room in data["rooms"]:
        assert room["capacity"] >= 50
        assert room["status"] == "available"
        assert isinstance(room["has_projector"], bool)
        assert set(room) >= {
            "room_id", "room_name", "building", "floor", "capacity",
            "has_projector", "has_computer", "room_type", "status",
        }

    # 排序：刚好够用（容量升序）优先。
    capacities = [room["capacity"] for room in data["rooms"]]
    assert capacities == sorted(capacities)


def test_available_classrooms_filters_and_free_slot(client, conn):
    status, payload = call(
        client,
        "/api/classrooms/available",
        capacity=60,
        has_projector=1,
        has_computer=0,
        room_type="机房",
        weekday=1,
        period_start=3,
        period_end=4,
        semester=SEMESTER,
        limit=100,
    )
    assert status == 200
    data = payload["data"]
    assert data["query"]["weekday"] == 1
    assert data["query"]["period_start"] == 3
    assert data["query"]["period_end"] == 4

    expected = rooms_matching(
        conn, capacity=60, projector=1, room_type="机房", weekday=1, period_start=3, period_end=4
    )
    # 前提：请求里的 has_computer=0 按 docs/API.md 的语义是「需要电脑」，并不是「不要电脑」，
    # 所以机房候选里必须真的有 has_computer=1 的教室，这条断言才有意义
    #（否则两个结果集会因为候选集本来就没有电脑教室而巧合相等）。
    assert conn.execute(
        "SELECT COUNT(*) FROM classrooms WHERE status = 'available'"
        " AND room_type = '机房' AND has_computer = 1"
    ).fetchone()[0] > 0, "测试数据里应当有机房带电脑，否则 has_computer 过滤没有被验证"
    assert data["total"] == len(expected)
    for room in data["rooms"]:
        assert room["capacity"] >= 60
        assert room["room_type"] == "机房"
        assert room["has_projector"] is True

    # 与排课表直接对比，确认「空闲」结论正确。
    busy = {
        row["room_id"]
        for row in conn.execute(
            "SELECT DISTINCT room_id FROM schedules WHERE semester = ? AND weekday = 1"
            " AND period_start <= 4 AND period_end >= 3",
            (SEMESTER,),
        )
    }
    assert busy, "测试数据里周一 3-4 节应当有排课，否则这条断言没有意义"
    assert not busy & {room["room_id"] for room in data["rooms"]}


def test_has_projector_zero_is_not_negation(client, conn):
    """锁定已知语义缺口：``has_projector=0`` 不是「不要投影」，而是不过滤。

    queries.py 用的是 ``has_projector >= ?``，所以 0 匹配全部教室。
    意图解析器允许用户说「找个没有投影的教室」（has_projector=false），
    但在查询层这条条件目前等于没生效。等业务层实现真正的取反，这里会失败，
    提醒同步更新解析层到查询层的语义映射。
    """
    unfiltered = queries.list_available_classrooms(conn, {"capacity": 10})
    zero = queries.list_available_classrooms(conn, {"capacity": 10, "has_projector": 0})
    one = queries.list_available_classrooms(conn, {"capacity": 10, "has_projector": 1})

    assert zero["total"] == unfiltered["total"], "has_projector=0 目前等于不过滤（已知缺口）"
    assert one["total"] < unfiltered["total"], "has_projector=1 必须真的起到过滤作用"

    status, payload = call(
        client, "/api/classrooms/available", capacity=10, has_projector=0, limit=100
    )
    assert status == 200
    assert payload["data"]["query"]["has_projector"] is False
    assert payload["data"]["total"] == unfiltered["total"]


def test_available_classrooms_overlapping_period_is_excluded(client, conn):
    """查询第 3 节时，占用 [2,3] 或 [3,4] 的教室都必须被判为冲突（区间重叠而非相等）。"""
    cross = {
        row["room_id"]
        for row in conn.execute(
            "SELECT DISTINCT room_id FROM schedules WHERE semester = ? AND weekday = 2"
            " AND period_start <= 3 AND period_end >= 3",
            (SEMESTER,),
        )
    }
    assert cross, "测试数据里周二第 3 节应当有排课"
    # 数据里确实存在跨过第 3 节的排课（例如 [1,4]、[3,4]），这条断言才有意义。
    assert conn.execute(
        "SELECT COUNT(*) FROM schedules WHERE semester = ? AND weekday = 2"
        " AND period_start < 3 AND period_end >= 3",
        (SEMESTER,),
    ).fetchone()[0] > 0

    status, payload = call(
        client,
        "/api/classrooms/available",
        weekday=2,
        period_start=3,
        period_end=3,
        semester=SEMESTER,
        limit=100,
    )
    assert status == 200
    returned = {room["room_id"] for room in payload["data"]["rooms"]}
    assert not (returned & cross)


def test_available_classrooms_rejects_bad_parameters(client):
    cases = [
        ({"weekday": 0}, "weekday"),
        ({"weekday": 8}, "weekday"),
        ({"weekday": "周一"}, "weekday"),
        ({"weekday": "1.5"}, "weekday"),
        ({"period_start": 13}, "period_start"),
        ({"period_start": 6, "period_end": 5}, "period_start"),
        ({"capacity": 0}, "capacity"),
        ({"capacity": -10}, "capacity"),
        ({"capacity": "abc"}, "capacity"),
        ({"limit": 0}, "limit"),
        ({"limit": 101}, "limit"),
        ({"has_projector": "maybe"}, "has_projector"),
        ({"room_type": "体育馆"}, "room_type"),
    ]
    for params, field in cases:
        status, payload = call(client, "/api/classrooms/available", **params)
        assert status == 400, params
        assert payload["error"]["code"] == "invalid_parameter"
        assert payload["error"]["field"] == field, params


def test_available_classrooms_unknown_semester_404(client):
    status, payload = call(
        client, "/api/classrooms/available", weekday=1, period_start=1, period_end=2,
        semester="1999-2000-1",
    )
    assert status == 404
    assert payload["error"]["field"] == "semester"
    assert SEMESTER in payload["error"]["available"]


def test_available_classrooms_weekday_without_semester_uses_single_semester(client):
    """库里只有一个学期时，可以省略 semester，业务层自动补齐。"""
    status, payload = call(client, "/api/classrooms/available", weekday=3, period_start=1, period_end=2)
    assert status == 200
    assert payload["data"]["query"]["semester"] == SEMESTER


# ---------------------------------------------------------------------------
# 2. GET /api/canteens/stats
# ---------------------------------------------------------------------------

def test_canteen_stats_matches_database(client, conn):
    status, payload = call(client, "/api/canteens/stats")
    assert status == 200
    data = payload["data"]

    expected = conn.execute(
        "SELECT m.canteen_id, COUNT(*) AS records, SUM(m.people_count) AS people"
        " FROM meal_records AS m GROUP BY m.canteen_id ORDER BY m.canteen_id"
    ).fetchall()
    assert data["total"] == len(expected)
    assert data["summary"]["records"] == sum(row["records"] for row in expected)
    assert data["summary"]["people_total"] == sum(row["people"] for row in expected)

    by_id = {item["canteen_id"]: item for item in data["canteens"]}
    for row in expected:
        item = by_id[row["canteen_id"]]
        assert item["records"] == row["records"]
        assert item["people_total"] == row["people"]
        assert item["record_days"] >= 1
        assert 0.0 <= item["congestion"] <= 1.0
        assert item["congestion_raw"] >= item["congestion"] - 1e-9
        assert item["peak_hour"]["hour"] in range(6, 22)
        assert item["avg_wait_minutes"] > 0

    # 默认按平均人数降序，方便一眼看出最挤的食堂。
    averages = [item["avg_people"] for item in data["canteens"]]
    assert averages == sorted(averages, reverse=True)


def test_canteen_stats_peak_hour_is_real_max(client, conn):
    _, payload = call(client, "/api/canteens/stats", canteen_id=1)
    item = payload["data"]["canteens"][0]
    best = conn.execute(
        "SELECT record_hour, AVG(people_count) AS avg_people FROM meal_records"
        " WHERE canteen_id = ? GROUP BY record_hour ORDER BY avg_people DESC LIMIT 1",
        (1,),
    ).fetchone()
    assert item["peak_hour"]["hour"] == best["record_hour"]
    assert item["peak_hour"]["avg_people"] == round(best["avg_people"], 2)


def test_canteen_stats_filters(client, conn):
    status, payload = call(
        client,
        "/api/canteens/stats",
        canteen_id=2,
        date_from="2025-09-01",
        date_to="2025-09-07",
        time_slot="lunch",
        top_slots=1,
    )
    assert status == 200
    data = payload["data"]
    assert data["total"] == 1
    assert data["canteens"][0]["canteen_id"] == 2
    assert len(data["canteens"][0]["peak_slots"]) == 1
    assert data["canteens"][0]["peak_slots"][0]["time_slot"] == "lunch"

    expected = conn.execute(
        "SELECT COUNT(*) AS records, SUM(people_count) AS people FROM meal_records"
        " WHERE canteen_id = 2 AND time_slot = 'lunch'"
        " AND record_date BETWEEN '2025-09-01' AND '2025-09-07'"
    ).fetchone()
    assert data["summary"]["records"] == expected["records"]
    assert data["summary"]["people_total"] == expected["people"]


def test_canteen_stats_empty_result_is_200(client):
    status, payload = call(client, "/api/canteens/stats", date_from="1999-01-01", date_to="1999-01-02")
    assert status == 200
    assert payload["data"]["total"] == 0
    assert payload["data"]["canteens"] == []
    assert payload["data"]["summary"]["records"] == 0


def test_canteen_stats_rejects_bad_parameters(client):
    cases = [
        {"canteen_id": 0},
        {"canteen_id": "abc"},
        {"date_from": "2025/09/01"},
        {"date_from": "2025-02-30"},
        {"date_from": "2025-09-10", "date_to": "2025-09-01"},
        {"time_slot": "brunch"},
        {"top_slots": 5},
        {"top_slots": -1},
    ]
    for params in cases:
        status, payload = call(client, "/api/canteens/stats", **params)
        assert status == 400, params
        assert payload["error"]["code"] == "invalid_parameter"


# ---------------------------------------------------------------------------
# 3. GET /api/industry/skills
# ---------------------------------------------------------------------------

def test_industry_skills_matches_database(client, conn):
    status, payload = call(client, "/api/industry/skills")
    assert status == 200
    data = payload["data"]
    assert data["query"]["dimension"] == "industry"

    expected = conn.execute(
        "SELECT industry AS name, COUNT(*) AS job_count FROM jobs"
        " WHERE industry IS NOT NULL GROUP BY industry ORDER BY job_count DESC, name ASC"
    ).fetchall()
    assert data["total"] == len(expected)
    assert data["summary"]["job_count"] == sum(row["job_count"] for row in expected)

    for group, row in zip(data["groups"], expected):
        assert group["name"] == row["name"]
        assert group["job_count"] == row["job_count"]
        assert group["top_skills"], "每个行业都应当至少有一个技能"
        counts = [skill["job_count"] for skill in group["top_skills"]]
        assert counts == sorted(counts, reverse=True)
        assert group["skill_count"] >= len(group["top_skills"])
        for skill in group["top_skills"]:
            assert skill["job_count"] <= group["job_count"]
            assert 1 <= skill["avg_importance"] <= 5
            assert skill["skill_category"]


def test_industry_skills_top_limits_skills(client):
    _, payload = call(client, "/api/industry/skills", top=3)
    for group in payload["data"]["groups"]:
        assert len(group["top_skills"]) <= 3


def test_industry_skills_filter_and_dimension(client, conn):
    status, payload = call(client, "/api/industry/skills", dimension="city", major="软件工程")
    assert status == 200
    data = payload["data"]
    assert data["query"]["dimension"] == "city"

    expected = conn.execute(
        "SELECT city AS name, COUNT(*) AS job_count FROM jobs WHERE major = ?"
        " GROUP BY city ORDER BY job_count DESC, name ASC",
        ("软件工程",),
    ).fetchall()
    assert [(group["name"], group["job_count"]) for group in data["groups"]] == [
        (row["name"], row["job_count"]) for row in expected
    ]


def test_industry_skills_unknown_filter_returns_empty_200(client):
    status, payload = call(client, "/api/industry/skills", industry="星际矿业")
    assert status == 200
    assert payload["data"]["groups"] == []
    assert payload["data"]["summary"]["job_count"] == 0


def test_industry_skills_rejects_bad_parameters(client):
    for params in ({"dimension": "salary"}, {"top": 0}, {"top": 101}, {"top": "many"}):
        status, payload = call(client, "/api/industry/skills", **params)
        assert status == 400, params
        assert payload["error"]["field"] in params


# ---------------------------------------------------------------------------
# 4. GET /api/industry/gap
# ---------------------------------------------------------------------------

def test_industry_gap_coverage_matches_database(client, conn):
    major = "软件工程"
    status, payload = call(client, "/api/industry/gap", major=major)
    assert status == 200
    data = payload["data"]
    summary = data["summary"]

    rows = conn.execute(
        "SELECT DISTINCT js.skill_id, js.importance, js.is_required FROM jobs AS j"
        " JOIN job_skills AS js ON js.job_id = j.job_id WHERE j.major = ?",
        (major,),
    ).fetchall()
    required_ids = {row["skill_id"] for row in rows}
    course_ids = {
        row["skill_id"]
        for row in conn.execute(
            "SELECT DISTINCT cs.skill_id FROM courses AS c"
            " JOIN course_skills AS cs ON cs.course_id = c.course_id WHERE c.department = ?",
            (major,),
        )
    }

    assert summary["job_count"] == conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE major = ?", (major,)
    ).fetchone()[0]
    assert summary["required_skill_count"] == len(required_ids)
    assert summary["covered_skill_count"] == len(required_ids & course_ids)
    assert summary["missing_skill_count"] == len(required_ids - course_ids)
    assert summary["course_skill_count"] == len(course_ids)
    assert summary["coverage_rate"] == round(
        len(required_ids & course_ids) / len(required_ids), 4
    )

    # 输出三组技能必须与集合运算一致，且互不重叠。
    covered_ids = {skill["skill_id"] for skill in data["covered_skills"]}
    missing_ids = {skill["skill_id"] for skill in data["missing_skills"]}
    required_missing_ids = {skill["skill_id"] for skill in data["required_missing_skills"]}
    assert covered_ids <= course_ids & required_ids
    assert missing_ids <= required_ids - course_ids
    assert not covered_ids & missing_ids
    assert required_missing_ids == {
        skill["skill_id"] for skill in data["missing_skills"] if skill["is_required"]
    }
    assert summary["required_missing_count"] == len(required_missing_ids)

    # 缺口按「硬性要求优先 + 重要度降序」排序，方便直接给课程建设建议。
    keys = [
        (0 if skill["is_required"] else 1, -skill["importance"], -skill["job_count"])
        for skill in data["missing_skills"]
    ]
    assert keys == sorted(keys)


def test_industry_gap_weighted_coverage(client, conn):
    major = "网络工程"
    status, payload = call(client, "/api/industry/gap", major=major, top=100)
    assert status == 200
    summary = payload["data"]["summary"]

    # 加权覆盖率 = Σ(覆盖技能的重要度) / Σ(全部要求技能的重要度)，重要度取该技能最大值。
    rows = conn.execute(
        "SELECT js.skill_id, MAX(js.importance) AS importance FROM jobs AS j"
        " JOIN job_skills AS js ON js.job_id = j.job_id WHERE j.major = ? GROUP BY js.skill_id",
        (major,),
    ).fetchall()
    weights = {row["skill_id"]: row["importance"] for row in rows}
    course_ids = {
        row["skill_id"]
        for row in conn.execute(
            "SELECT DISTINCT cs.skill_id FROM courses AS c"
            " JOIN course_skills AS cs ON cs.course_id = c.course_id WHERE c.department = ?",
            (major,),
        )
    }
    total = sum(weights.values())
    covered = sum(value for key, value in weights.items() if key in course_ids)
    assert summary["weighted_coverage_rate"] == round(covered / total, 4)
    assert 0.0 <= summary["weighted_coverage_rate"] <= 1.0


def test_industry_gap_min_importance_and_industry_filter(client, conn):
    major = "计算机科学与技术"
    status, payload = call(
        client, "/api/industry/gap", major=major, min_importance=4, industry="人工智能"
    )
    assert status == 200
    data = payload["data"]
    assert data["query"]["min_importance"] == 4
    assert data["query"]["industry"] == "人工智能"

    rows = conn.execute(
        "SELECT DISTINCT js.skill_id, MAX(js.importance) AS importance FROM jobs AS j"
        " JOIN job_skills AS js ON js.job_id = j.job_id"
        " WHERE j.major = ? AND j.industry = ? GROUP BY js.skill_id",
        (major, "人工智能"),
    ).fetchall()
    high = {row["skill_id"] for row in rows if row["importance"] >= 4}
    assert data["summary"]["job_count"] == conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE major = ? AND industry = ?", (major, "人工智能")
    ).fetchone()[0]
    assert data["summary"]["required_skill_count"] == len(high)
    for skill in data["covered_skills"] + data["missing_skills"]:
        assert skill["importance"] >= 4


def test_industry_gap_requires_major(client):
    status, payload = call(client, "/api/industry/gap")
    assert status == 400
    assert payload["error"]["field"] == "major"
    assert payload["error"]["code"] == "invalid_parameter"


def test_industry_gap_unknown_major_404(client):
    status, payload = call(client, "/api/industry/gap", major="霍格沃茨魔法学")
    assert status == 404
    assert payload["error"]["field"] == "major"
    assert "软件工程" in payload["error"]["available"]


def test_industry_gap_rejects_bad_parameters(client):
    for params in ({"major": "软件工程", "min_importance": 0},
                   {"major": "软件工程", "min_importance": 6},
                   {"major": "软件工程", "top": 0},
                   {"major": "软件工程", "top": "all"}):
        status, payload = call(client, "/api/industry/gap", **params)
        assert status == 400, params
        assert payload["error"]["code"] == "invalid_parameter"


# ---------------------------------------------------------------------------
# 参数化查询 / 业务层分离 / 不接 AI
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "payload",
    ["' OR 1=1 --", "1; DROP TABLE classrooms; --", "%", "_", "\" OR \"\"=\""],
)
def test_values_are_bound_not_interpolated(client, conn, payload):
    """注入串只能当作字符串值：既不能扩大结果，也不能破坏数据库。"""
    before = conn.execute("SELECT COUNT(*) FROM classrooms").fetchone()[0]

    status, body = call(client, "/api/classrooms/available", room_type=payload, limit=100)
    assert status in (200, 400)
    if status == 200:
        assert body["data"]["rooms"] == []
        assert body["data"]["total"] == 0

    status, body = call(client, "/api/canteens/stats", time_slot=payload)
    assert status == 400  # 枚举白名单先拦住

    status, body = call(client, "/api/industry/gap", major=payload)
    assert status == 404
    assert body["error"]["code"] == "not_found"

    after = conn.execute("SELECT COUNT(*) FROM classrooms").fetchone()[0]
    assert after == before, "注入串不应改变数据库内容"


def test_not_equal_query_result_is_not_widened(client, conn):
    """正常取值与注入取值的结果必须不同（证明参数真的按值绑定，而不是拼进 SQL）。"""
    _, normal = call(client, "/api/classrooms/available", room_type="普通", limit=100)
    assert normal["data"]["total"] > 0

    # 注入串不在 room_type 白名单里 → 400，绝不会被当成 SQL 片段执行。
    status, injected = call(client, "/api/classrooms/available", room_type="' OR '1'='1", limit=100)
    assert status == 400
    assert injected["error"]["code"] == "invalid_parameter"

    # 外形合法但不存在的取值：返回 200 + 空结果，而不是「全部教室」。
    status, absent = call(client, "/api/classrooms/available", building="' OR '1'='1", limit=100)
    assert status == 200
    assert absent["data"]["total"] == 0


def test_business_module_does_not_import_flask():
    """业务查询层保持与 Web 框架解耦，也不引入任何 AI SDK / 网络库。"""
    import ast

    tree = ast.parse(open(queries.__file__, "r", encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "flask" not in imported
    assert imported <= {"db", "errors", "validators", "datetime", "math"}


def test_business_functions_work_without_flask(conn):
    """业务层可直接被 sqlite3 连接调用（路由层只做参数搬运）。"""
    rooms = queries.list_available_classrooms(conn, {"capacity": 40, "limit": 3})
    assert rooms["total"] > 3 and len(rooms["rooms"]) == 3

    stats = queries.canteen_stats(conn, {"canteen_id": 3})
    assert stats["total"] == 1

    skills = queries.industry_skills(conn, {"dimension": "industry", "top": 2})
    assert skills["groups"][0]["name"]

    gap = queries.industry_skill_gap(conn, {"major": "数据科学与大数据技术"})
    assert gap["summary"]["required_skill_count"] > 0

    assert "软件工程" in queries.available_majors(conn)


def test_every_response_is_json_and_serializable(client):
    for path in (
        "/api/classrooms/available?limit=2",
        "/api/canteens/stats?top_slots=2",
        "/api/industry/skills?top=2",
        "/api/industry/gap?major=软件工程&top=2",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.mimetype == "application/json"
        payload = json.loads(response.get_data(as_text=True))
        assert payload["status"] == "ok"
        assert payload["data"]["query"] is not None


def test_frontend_folder_is_untouched():
    """测试只读取数据与后端代码，不应产生 frontend 目录下的任何文件。"""
    frontend = os.path.join(PROJECT_ROOT, "frontend")
    if os.path.isdir(frontend):
        assert os.listdir(frontend) == []
