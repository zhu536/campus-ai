# -*- coding: utf-8 -*-
"""CampusAI 产教融合技能分析测试（``backend/industry.py`` + ``/api/industry/match``）。

覆盖任务要求：

    1. 技能需求          → test_*_skill_demands_*
    2. 已覆盖技能        → test_*_covered_*
    3. 缺失技能          → test_*_missing_*
    4. 技能覆盖率        → test_coverage_rate_* / test_real_database_*
    5. 技能重要程度      → test_importance_*
    6. 所有匹配由 Python 算 → 全部断言都用直连 sqlite3 的参考实现独立复算
    7. AI 不算覆盖率      → test_ai_payload_contains_no_sql_table_names_or_rows
    8. AI 不能改计算结果  → test_ai_cannot_mutate_results / test_explanation_stage_cannot_change_results
    9. AI 只能解释结果    → test_ai_*（降级、拒 SQL、连接先关闭）
   10. 不修改教室/食堂功能 → test_classroom_and_canteen_features_still_work

两张数据源：

    - **合成库**：值完全可控（6 项技能 / 4 门课程 / 5 个岗位），用来做**精确**断言；
    - **真实库副本**：``data/campus.db`` 的临时拷贝，用独立参考实现核对覆盖率。

全部离线：AI 用注入的替身，**不会联网**；真实库只读副本，不改动仓库里的库。
"""

import ast
import contextlib
import copy
import io
import json
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

from backend import industry  # noqa: E402
from backend import queries  # noqa: E402
from backend.app import create_app  # noqa: E402
from backend.errors import NotFoundError, ValidationError  # noqa: E402

REAL_DB = os.path.join(PROJECT_ROOT, "data", "campus.db")
INDUSTRY_SOURCE = industry.__file__
PROMPT_PATH = os.path.join(PROJECT_ROOT, "prompts", "industry_insight_generator.md")

#: 假 Key：证明「Key 只从配置来」，同时保证任何返回/请求里都不会出现真 Key。
FAKE_API_KEY = "sk-fake-for-tests-do-not-use"

# ---------------------------------------------------------------------------
# 合成数据（与 docs/DATABASE.md 的 9 张表结构一致，只取场景三用到的 5 张）
# ---------------------------------------------------------------------------

MAJOR = "软件工程"
OTHER_MAJOR = "人工智能"

#: 覆盖程度权重（测试里独立写一份，避免复用被测代码的常量）。
LEVEL_RANK = {"了解": 1, "掌握": 2, "熟练": 3}

# (skill_id, skill_name, skill_category)
SKILLS = (
    (1, "Python", "编程语言"),
    (2, "SQL", "数据库"),
    (3, "Git", "工具"),
    (4, "沟通表达", "软技能"),
    (5, "Docker", "工具"),
    (6, "机器学习", "其他"),
)

# (course_id, course_code, course_name, department)
COURSES = (
    (101, "CS101", "程序设计基础", MAJOR),
    (102, "CS102", "数据库原理", MAJOR),
    (103, "CS103", "软件工程实践", MAJOR),
    (201, "AI201", "机器学习导论", OTHER_MAJOR),
)

# (course_id, skill_id, level)
COURSE_SKILLS = (
    (101, 1, "熟练"),
    (101, 3, "了解"),   # 同一技能被两门课覆盖：取最高的「熟练」
    (102, 2, "掌握"),
    (103, 3, "熟练"),
    (103, 6, "了解"),
    (201, 6, "熟练"),   # 人工智能专业的课：按专业过滤后不能出现在软件工程的覆盖里
)

# (job_id, job_title, company, industry, major)
JOBS = (
    (1, "Python 后端开发", "甲科技", "软件开发", MAJOR),
    (2, "数据平台工程师", "乙数据", "大数据", MAJOR),
    (3, "算法实习生", "丙智能", "人工智能", MAJOR),
    (4, "算法工程师", "丁智能", "人工智能", OTHER_MAJOR),  # 非本专业：必须被过滤掉
    (5, "无技能岗位", "戊科技", "软件开发", MAJOR),        # 没有任何技能记录
)

# (job_id, skill_id, importance, is_required)
JOB_SKILLS = (
    (1, 1, 5, 1),
    (1, 2, 4, 1),
    (1, 3, 2, 0),
    (1, 4, 3, 1),
    (2, 1, 4, 1),
    (2, 2, 5, 1),
    (2, 5, 3, 0),
    (3, 6, 5, 1),
    (3, 1, 3, 0),
    (4, 6, 5, 1),
)

SCHEMA = """
CREATE TABLE courses (
    course_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    course_code TEXT    NOT NULL UNIQUE,
    course_name TEXT    NOT NULL,
    department  TEXT,
    credit      REAL    CHECK (credit IS NULL OR credit >= 0),
    course_type TEXT    NOT NULL DEFAULT '必修'
                CHECK (course_type IN ('必修', '选修', '实践'))
);
CREATE TABLE jobs (
    job_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_title    TEXT    NOT NULL,
    company      TEXT    NOT NULL,
    industry     TEXT,
    city         TEXT,
    education    TEXT,
    experience   TEXT,
    salary_min   INTEGER CHECK (salary_min IS NULL OR salary_min >= 0),
    salary_max   INTEGER CHECK (salary_max IS NULL OR salary_max >= 0),
    major        TEXT,
    job_desc     TEXT,
    publish_date TEXT,
    CHECK (salary_max IS NULL OR salary_min IS NULL OR salary_max >= salary_min)
);
CREATE TABLE skills (
    skill_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name     TEXT NOT NULL UNIQUE,
    skill_category TEXT NOT NULL DEFAULT '其他'
                   CHECK (skill_category IN ('编程语言', '数据库', '框架', '工具', '软技能', '其他')),
    description    TEXT
);
CREATE TABLE course_skills (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
    skill_id  INTEGER NOT NULL REFERENCES skills(skill_id)   ON DELETE CASCADE,
    level     TEXT    NOT NULL DEFAULT '掌握'
              CHECK (level IN ('了解', '掌握', '熟练')),
    UNIQUE (course_id, skill_id)
);
CREATE TABLE job_skills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(job_id)     ON DELETE CASCADE,
    skill_id    INTEGER NOT NULL REFERENCES skills(skill_id) ON DELETE CASCADE,
    importance  INTEGER NOT NULL DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
    is_required INTEGER NOT NULL DEFAULT 1 CHECK (is_required IN (0, 1)),
    UNIQUE (job_id, skill_id)
);
"""


def build_industry_db(path):
    """建一个只含场景三五张表的合成库，返回值完全可控。"""
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT INTO courses (course_id, course_code, course_name, department, credit,"
            " course_type) VALUES (?, ?, ?, ?, ?, ?)",
            [(cid, code, name, dept, 3.0, "必修") for cid, code, name, dept in COURSES],
        )
        connection.executemany(
            "INSERT INTO jobs (job_id, job_title, company, industry, major)"
            " VALUES (?, ?, ?, ?, ?)",
            JOBS,
        )
        connection.executemany(
            "INSERT INTO skills (skill_id, skill_name, skill_category) VALUES (?, ?, ?)",
            SKILLS,
        )
        connection.executemany(
            "INSERT INTO course_skills (course_id, skill_id, level) VALUES (?, ?, ?)",
            COURSE_SKILLS,
        )
        connection.executemany(
            "INSERT INTO job_skills (job_id, skill_id, importance, is_required)"
            " VALUES (?, ?, ?, ?)",
            JOB_SKILLS,
        )
        connection.commit()
    finally:
        connection.close()
    return path


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

@pytest.fixture()
def industry_db(tmp_path):
    """合成数据库（每个测试独占，值完全可控）。"""
    return build_industry_db(os.path.join(str(tmp_path), "industry.db"))


@pytest.fixture()
def conn(industry_db):
    connection = sqlite3.connect(industry_db)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def client(industry_db):
    application = create_app(db_path=industry_db)
    application.config.update(TESTING=True)
    return application.test_client()


@pytest.fixture(scope="module")
def real_db_path(tmp_path_factory):
    """真实数据库的临时副本（模块级共用；只读，不动仓库里的库）。"""
    if not os.path.isfile(REAL_DB):
        pytest.skip("缺少 data/campus.db，请先执行 python scripts/import_data.py")
    target = os.path.join(str(tmp_path_factory.mktemp("industry_db")), "campus.db")
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
    """假的 AI 解释器：只记录收到的 payload，返回固定文案（不联网）。"""

    def __init__(self, summary="AI 解释：已按结果说明。", error=None):
        self.summary = summary
        self.error = error
        self.payloads = []

    def explain(self, payload):
        self.payloads.append(payload)
        if self.error is not None:
            raise self.error
        return industry._render_explanation(self.summary, ["要点"], ["建议"], source="ai")


class AdversarialExplainer:
    """失控的解释器：既就地篡改事实表，又返回自己编的数字与多余字段。

    用来证明红线 3/4：模型（或任何被注入的解释器）**没有任何渠道**改到报告里的计算结果。
    """

    def __init__(self):
        self.payloads = []

    def explain(self, payload):
        self.payloads.append(payload)
        payload["major"] = "被改掉的专业"
        payload["coverage"]["coverage_rate"] = 1.0
        for item in payload["missing_skills"]:
            item["covered"] = True
        payload["skill_demands"].append({"skill_name": "凭空捏造"})
        explanation = industry._render_explanation("覆盖率 100%。", ["全部覆盖"], [], source="ai")
        # 模型常见的越权行为：直接把自己算的覆盖率塞回结果里。
        explanation["coverage_rate"] = 1.0
        explanation["summary_data"] = {"coverage_rate": 1.0}
        return explanation


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
    """构造一个「配置已注入、请求打到假 session」的真 IndustryExplainer（不联网）。"""
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
    return industry.IndustryExplainer(config=config, session=session), session


def sent_payload(session):
    """取出真正发给模型的只读事实表（user message 的 JSON）。"""
    assert len(session.calls) == 1
    body = session.calls[0]["json"]
    return json.loads(body["messages"][1]["content"])


def body_of(response):
    assert response.mimetype == "application/json", response.mimetype
    return json.loads(response.get_data(as_text=True))


def names(items):
    return [item["skill_name"] for item in items]


def by_name(items):
    return {item["skill_name"]: item for item in items}


def reference_gap(connection, major, min_importance=1):
    """直连 sqlite3 的参考实现：集合运算与覆盖率在测试里**重新写一遍**。

    刻意不复用 ``backend/industry.py`` 的任何函数，这样「被测代码算对了」这句话
    才有独立证据（AGENTS.md：计算归 Python，但测试要能独立复算）。
    """
    covered_rank = {}
    for row in connection.execute(
        "SELECT cs.skill_id AS skill_id, cs.level AS level"
        " FROM courses AS c JOIN course_skills AS cs ON cs.course_id = c.course_id"
        " WHERE c.department = ?",
        (major,),
    ):
        rank = LEVEL_RANK[row["level"]]
        covered_rank[row["skill_id"]] = max(covered_rank.get(row["skill_id"], 0), rank)

    demand = {}
    for row in connection.execute(
        "SELECT js.skill_id AS skill_id, js.importance AS importance,"
        " js.is_required AS is_required"
        " FROM jobs AS j JOIN job_skills AS js ON js.job_id = j.job_id"
        " WHERE j.major = ?",
        (major,),
    ):
        entry = demand.setdefault(
            row["skill_id"], {"importance": 0, "job_count": 0, "required": 0}
        )
        entry["importance"] = max(entry["importance"], row["importance"])
        entry["job_count"] += 1
        entry["required"] += 1 if row["is_required"] else 0

    kept = {sid: entry for sid, entry in demand.items() if entry["importance"] >= min_importance}
    required = len(kept)
    covered = [sid for sid in kept if sid in covered_rank]
    total_weight = sum(entry["importance"] for entry in kept.values())
    covered_weight = sum(kept[sid]["importance"] for sid in covered)
    jobs_total = connection.execute(
        "SELECT COUNT(*) FROM jobs WHERE major = ?", (major,)
    ).fetchone()[0]

    return {
        "job_count": jobs_total,
        "required_skill_count": required,
        "covered_skill_count": len(covered),
        "missing_skill_count": required - len(covered),
        "required_missing_count": sum(
            1 for sid, entry in kept.items() if entry["required"] and sid not in covered_rank
        ),
        "coverage_rate": round(len(covered) / required, 4) if required else None,
        "weighted_coverage_rate": (
            round(covered_weight / total_weight, 4) if total_weight else None
        ),
    }


# ---------------------------------------------------------------------------
# 1. 课程技能覆盖表（输入之二）
# ---------------------------------------------------------------------------

def test_build_major_coverage_picks_highest_level_and_counts_courses():
    """同一技能被多门课覆盖时取最高覆盖程度，并记录覆盖它的课程。"""
    rows = [
        {"course_id": 101, "course_name": "程序设计基础", "skill_id": 3,
         "skill_name": "Git", "skill_category": "工具", "level": "了解"},
        {"course_id": 103, "course_name": "软件工程实践", "skill_id": 3,
         "skill_name": "Git", "skill_category": "工具", "level": "熟练"},
        {"course_id": 102, "course_name": "数据库原理", "skill_id": 2,
         "skill_name": "SQL", "skill_category": "数据库", "level": "掌握"},
    ]
    coverage = industry.build_major_coverage(MAJOR, rows)

    assert coverage["major"] == MAJOR
    assert coverage["skill_count"] == 2
    assert coverage["course_count"] == 3
    assert coverage["skills"][3]["level"] == "熟练"
    assert coverage["skills"][3]["level_rank"] == LEVEL_RANK["熟练"]
    assert coverage["skills"][3]["course_count"] == 2
    assert coverage["skills"][3]["courses"] == ["程序设计基础", "软件工程实践"]
    # 权重总和 = 熟练(3) + 掌握(2)
    assert coverage["weighted_rank_total"] == 5


def test_build_major_coverage_of_empty_input_is_empty():
    coverage = industry.build_major_coverage(MAJOR, [])
    assert coverage["skills"] == {}
    assert coverage["skill_count"] == 0
    assert coverage["weighted_rank_total"] == 0


# ---------------------------------------------------------------------------
# 2. match_job_to_major：单个岗位 × 一个专业
# ---------------------------------------------------------------------------

def synthetic_coverage():
    rows = [
        {"course_id": cid, "course_name": dict((c, n) for c, _code, n, _d in COURSES)[cid],
         "skill_id": sid, "skill_name": dict((s, n) for s, n, _c in SKILLS)[sid],
         "skill_category": dict((s, c) for s, _n, c in SKILLS)[sid], "level": level}
        for cid, sid, level in COURSE_SKILLS
        if cid in (101, 102, 103)
    ]
    return industry.build_major_coverage(MAJOR, rows)


JOB_ONE = {
    "job_id": 1,
    "job_title": "Python 后端开发",
    "company": "甲科技",
    "industry": "软件开发",
    "major": MAJOR,
    "skills": [
        {"skill_id": 1, "skill_name": "Python", "skill_category": "编程语言",
         "importance": 5, "is_required": True},
        {"skill_id": 2, "skill_name": "SQL", "skill_category": "数据库",
         "importance": 4, "is_required": True},
        {"skill_id": 3, "skill_name": "Git", "skill_category": "工具",
         "importance": 2, "is_required": False},
        {"skill_id": 4, "skill_name": "沟通表达", "skill_category": "软技能",
         "importance": 3, "is_required": True},
    ],
}


def test_match_job_to_major_five_outputs():
    """五个输出一次核对：技能需求 / 已覆盖 / 缺失 / 覆盖率 / 重要程度。"""
    match = industry.match_job_to_major(JOB_ONE, synthetic_coverage())

    # 1. 技能需求：全量 4 项，按「重要度降序 → 缺口优先 → 名称」排序
    assert len(match["skill_demands"]) == 4
    assert names(match["skill_demands"]) == ["Python", "SQL", "沟通表达", "Git"]
    assert all(item["covered"] in (True, False) for item in match["skill_demands"])

    # 2. 已覆盖技能 = 技能需求 ∩ 课程覆盖（Python / SQL / Git）
    assert names(match["covered_skills"]) == ["Python", "SQL", "Git"]
    assert by_name(match["covered_skills"])["Python"]["course_level"] == "熟练"
    assert by_name(match["covered_skills"])["SQL"]["course_level"] == "掌握"
    assert by_name(match["covered_skills"])["Git"]["course_level"] == "熟练"

    # 3. 缺失技能 = 技能需求 − 课程覆盖（沟通表达）
    assert names(match["missing_skills"]) == ["沟通表达"]
    assert names(match["required_missing_skills"]) == ["沟通表达"]
    assert match["missing_skills"][0]["is_required"] is True
    assert match["missing_skills"][0]["course_level"] is None

    # 4. 技能覆盖率 = 3/4；加权 = (5+4+2)/(5+4+2+3)
    coverage = match["coverage"]
    assert coverage["required_skill_count"] == 4
    assert coverage["covered_skill_count"] == 3
    assert coverage["missing_skill_count"] == 1
    assert coverage["required_missing_count"] == 1
    assert coverage["coverage_rate"] == 0.75
    assert coverage["weighted_coverage_rate"] == round(11 / 14, 4) == 0.7857

    # 5. 技能重要程度：最高 5、平均 3.5 → 「较高」
    importance = match["importance"]
    assert importance["max"] == 5
    assert importance["avg"] == 3.5
    assert importance["level"] == "较高"
    assert importance["core_skill_count"] == 1
    assert by_name(match["skill_demands"])["Python"]["importance_level"] == "核心"


def test_match_job_to_major_respects_min_importance():
    """只看重要度 >= 4 的技能时，Git(2) 与沟通表达(3) 都不进技能需求。"""
    match = industry.match_job_to_major(JOB_ONE, synthetic_coverage(), min_importance=4)
    assert names(match["skill_demands"]) == ["Python", "SQL"]
    assert names(match["covered_skills"]) == ["Python", "SQL"]
    assert match["missing_skills"] == []
    assert match["coverage"]["coverage_rate"] == 1.0
    assert match["coverage"]["weighted_coverage_rate"] == 1.0
    assert match["importance"]["max"] == 5
    assert match["importance"]["avg"] == 4.5
    assert match["importance"]["level"] == "重要"


def test_match_job_to_major_without_skills_has_no_coverage():
    match = industry.match_job_to_major({"job_id": 5, "job_title": "无技能岗位"}, None)
    assert match["skill_demands"] == []
    assert match["covered_skills"] == [] and match["missing_skills"] == []
    assert match["coverage"]["coverage_rate"] is None
    assert match["coverage"]["weighted_coverage_rate"] is None
    assert match["importance"]["max"] is None
    assert match["importance"]["level"] is None
    assert match["importance"]["distribution"] == []
    assert match["notes"]  # 必须显式说明「没有技能需求」，不能静默返回空


def test_match_job_to_major_merges_duplicate_rows_and_clamps_importance():
    """同一技能重复出现按最严要求合并；越界/缺失的重要度被夹回 1..5。"""
    job = {
        "job_id": 9,
        "skills": [
            {"skill_id": 1, "skill_name": "Python", "importance": 9, "is_required": False},
            {"skill_id": 1, "skill_name": "Python", "importance": None, "is_required": True},
        ],
    }
    match = industry.match_job_to_major(job, None)
    assert len(match["skill_demands"]) == 1
    item = match["skill_demands"][0]
    assert item["importance"] == 5          # 9 被夹到上限 5
    assert item["is_required"] is True      # 任一行为硬性要求即为硬性要求
    assert item["job_count"] == 1           # 重复行不会把分母算成 2
    assert match["coverage"]["coverage_rate"] == 0.0


def test_importance_level_scale():
    assert [industry.importance_level(score) for score in (5, 4, 3, 2, 1)] == [
        "核心", "重要", "较高", "一般", "次要"
    ]
    assert industry.importance_level(None) is None


# ---------------------------------------------------------------------------
# 3. calculate_skill_gap：一个专业 × 全部对口岗位
# ---------------------------------------------------------------------------

@pytest.fixture()
def report(industry_db):
    """合成库上的完整分析结果（不解释，纯 Python 计算）。"""
    return industry.analyze_skill_gap(
        {"major": MAJOR}, db_path=industry_db, explain=False
    )


def test_calculate_skill_gap_aggregates_exactly(report):
    """五项输出与手算结果逐一对齐（合成库的值是完全可控的）。"""
    # 只有 major=软件工程的岗位参与：1/2/3 有技能，5 没有技能，4 是别的专业
    assert report["summary"]["job_count"] == 4
    assert report["summary"]["matched_job_count"] == 3

    demands = by_name(report["skill_demands"])
    assert set(demands) == {"Python", "SQL", "Git", "沟通表达", "Docker", "机器学习"}

    python = demands["Python"]
    assert python["job_count"] == 3
    assert python["required_job_count"] == 2
    assert python["importance"] == 5          # 岗位 1 给了 5、岗位 2 给了 4、岗位 3 给了 3
    assert python["avg_importance"] == 4.0
    assert python["importance_level"] == "核心"
    assert python["covered"] is True and python["course_level"] == "熟练"

    sql = demands["SQL"]
    assert (sql["job_count"], sql["importance"], sql["avg_importance"]) == (2, 5, 4.5)
    assert sql["course_level"] == "掌握"

    git = demands["Git"]
    assert (git["job_count"], git["importance"], git["is_required"]) == (1, 2, False)
    assert git["course_count"] == 2           # 两门课覆盖它

    # 2. 已覆盖 / 3. 缺失（排序：重要度降序 → 缺口优先 → 名称升序）
    assert names(report["covered_skills"]) == ["Python", "SQL", "机器学习", "Git"]
    assert names(report["missing_skills"]) == ["Docker", "沟通表达"]
    assert names(report["required_missing_skills"]) == ["沟通表达"]

    # 4. 技能覆盖率
    assert report["coverage_rate"] == 0.6667
    assert report["summary"]["covered_skill_count"] == 4
    assert report["summary"]["missing_skill_count"] == 2
    assert report["summary"]["required_missing_count"] == 1
    assert report["summary"]["weighted_coverage_rate"] == round(17 / 23, 4) == 0.7391
    assert report["summary"]["course_covered_ratio"] == 1.0

    # 5. 技能重要程度：6 项的分值 5/5/2/3/3/5（Python/SQL/机器学习 = 5）
    importance = report["importance"]
    assert importance["total"] == 23
    assert importance["avg"] == 3.83
    assert importance["max"] == 5
    assert importance["level"] == "较高"
    assert importance["core_skill_count"] == 3
    assert importance["distribution"] == [
        {"importance": 5, "level": "核心", "skill_count": 3, "covered_count": 3,
         "missing_count": 0, "share": 0.5},
        {"importance": 3, "level": "较高", "skill_count": 2, "covered_count": 0,
         "missing_count": 2, "share": 0.3333},
        {"importance": 2, "level": "一般", "skill_count": 1, "covered_count": 1,
         "missing_count": 0, "share": 0.1667},
    ]


def test_calculate_skill_gap_notes_explain_gaps(report):
    """口径与数据缺口必须显式写出来，不能用空结果糊弄。"""
    text = "".join(report["notes"])
    assert "1 个对口岗位没有登记技能要求" in text
    assert "课程共覆盖 4 项技能" in text


def test_calculate_skill_gap_job_matches_use_the_same_rule(report):
    """专业级汇总与岗位级匹配必须同源：逐岗位结果就是 match_job_to_major 的输出。"""
    assert len(report["job_matches"]) == 4
    by_id = {match["job_id"]: match for match in report["job_matches"]}
    assert names(by_id[2]["missing_skills"]) == ["Docker"]
    assert by_id[5]["coverage"]["coverage_rate"] is None
    # 汇总里某个技能的 job_count，等于各岗位匹配里出现它的次数
    assert report["summary"]["job_count"] == len(report["job_matches"])


def test_min_importance_filters_demands_but_not_the_fact():
    """门槛过滤只影响参与统计的技能，不影响「课程是否覆盖」的事实。"""
    full = industry.calculate_skill_gap(
        _synthetic_jobs(), synthetic_coverage(), min_importance=1
    )
    filtered = industry.calculate_skill_gap(
        _synthetic_jobs(), synthetic_coverage(), min_importance=4
    )
    assert names(filtered["skill_demands"]) == ["Python", "SQL", "机器学习"]
    assert filtered["coverage_rate"] == 1.0
    assert filtered["missing_skills"] == []
    # 门槛提到 4 之后，Git(2)/沟通表达(3)/Docker(3) 被挡掉
    assert filtered["summary"]["required_skill_count"] == 3
    assert "已按重要度筛选" in "".join(filtered["notes"])
    assert full["summary"]["required_skill_count"] == 6


def test_industry_filter_limits_jobs():
    """行业过滤只保留该行业的对口岗位（合成库断言见下）。"""
    jobs = [job for job in _synthetic_jobs() if job["industry"] == "大数据"]
    assert [job["job_id"] for job in jobs] == [2]
    report = industry.calculate_skill_gap(jobs, synthetic_coverage())
    assert report["summary"]["job_count"] == 1
    assert names(report["skill_demands"]) == ["SQL", "Python", "Docker"]
    assert names(report["missing_skills"]) == ["Docker"]
    assert report["coverage_rate"] == round(2 / 3, 4)


def test_top_truncates_lists_but_summary_stays_full(industry_db):
    full = industry.analyze_skill_gap({"major": MAJOR}, db_path=industry_db, explain=False)
    cut = industry.analyze_skill_gap(
        {"major": MAJOR, "top": 1}, db_path=industry_db, explain=False
    )
    assert len(cut["skill_demands"]) == 1
    assert len(cut["covered_skills"]) == 1
    assert len(cut["missing_skills"]) == 1
    assert cut["summary"]["returned_skill_count"] == 1
    assert cut["summary"]["truncated"] is True
    # summary 里的计数始终是全量
    for key in ("required_skill_count", "covered_skill_count", "missing_skill_count"):
        assert cut["summary"][key] == full["summary"][key]
    assert cut["coverage_rate"] == full["coverage_rate"]
    assert full["summary"]["truncated"] is False


def test_industry_filter_on_synthetic_db(industry_db):
    report = industry.analyze_skill_gap(
        {"major": MAJOR, "industry": "大数据"}, db_path=industry_db, explain=False
    )
    assert report["summary"]["job_count"] == 1
    assert names(report["skill_demands"]) == ["SQL", "Python", "Docker"]
    assert names(report["covered_skills"]) == ["SQL", "Python"]
    assert names(report["missing_skills"]) == ["Docker"]
    assert report["coverage_rate"] == round(2 / 3, 4)


def test_min_importance_keeps_all_job_evidence_for_kept_skills():
    """门槛是**技能级**的：留下来的技能，其 job_count / is_required 仍按全部岗位汇总。

    回归测试：曾经把门槛作用在「岗位 × 技能」上再聚合，于是「只被低重要度岗位标成
    硬性要求」的技能会丢掉 is_required，硬性缺口被悄悄漏掉（与 queries.industry_skill_gap
    的口径也不一致）。
    """
    coverage = synthetic_coverage()
    jobs = [
        {"job_id": 1, "job_title": "A", "industry": "软件开发", "major": MAJOR,
         "skills": [{"skill_id": 5, "skill_name": "Docker", "skill_category": "工具",
                     "importance": 5, "is_required": False}]},
        {"job_id": 2, "job_title": "B", "industry": "软件开发", "major": MAJOR,
         "skills": [{"skill_id": 5, "skill_name": "Docker", "skill_category": "工具",
                     "importance": 2, "is_required": True}]},
    ]
    report = industry.calculate_skill_gap(jobs, coverage, min_importance=4)

    item = report["skill_demands"][0]
    assert item["skill_name"] == "Docker"
    assert item["importance"] == 5          # 技能级重要度取最大值
    assert item["job_count"] == 2           # 全部岗位的事实都保留
    assert item["required_job_count"] == 1
    assert item["is_required"] is True
    assert item["avg_importance"] == 3.5
    # Docker 没有被课程覆盖，且它是硬性要求 → 必须出现在硬性缺口里
    assert report["summary"]["required_missing_count"] == 1
    assert names(report["required_missing_skills"]) == ["Docker"]
    # 岗位级明细仍然按用户给的门槛过滤（每个岗位自己的视角）
    assert report["job_matches"][0]["skill_demands"][0]["skill_name"] == "Docker"
    assert report["job_matches"][1]["skill_demands"] == []


def _synthetic_jobs():
    """把合成库的岗位/技能明细写成纯 Python 结构（供不连库的测试使用）。"""
    skill_names = {sid: name for sid, name, _cat in SKILLS}
    skill_cats = {sid: cat for sid, _name, cat in SKILLS}
    jobs = {}
    for job_id, title, company, ind, major in JOBS:
        if major != MAJOR:
            continue
        jobs[job_id] = {
            "job_id": job_id, "job_title": title, "company": company,
            "industry": ind, "major": major, "skills": [],
        }
    for job_id, skill_id, importance, required in JOB_SKILLS:
        if job_id in jobs:
            jobs[job_id]["skills"].append(
                {"skill_id": skill_id, "skill_name": skill_names[skill_id],
                 "skill_category": skill_cats[skill_id],
                 "importance": importance, "is_required": bool(required)}
            )
    return [jobs[key] for key in sorted(jobs)]


# ---------------------------------------------------------------------------
# 4. 参数校验与错误码
# ---------------------------------------------------------------------------

def test_validate_gap_params_defaults_and_normalization():
    normalized = industry.validate_gap_params({"major": "  软件工程  "})
    assert normalized == {
        "major": "软件工程", "industry": None, "min_importance": 1, "top": 20,
    }
    warnings = []
    industry.validate_gap_params({"major": MAJOR, "min_importance": 4}, warnings=warnings)
    assert warnings and "重要度 >= 4" in warnings[0]


@pytest.mark.parametrize(
    "params, field",
    [
        ({}, "major"),
        ({"major": ""}, "major"),
        ({"major": MAJOR, "min_importance": 0}, "min_importance"),
        ({"major": MAJOR, "min_importance": 6}, "min_importance"),
        ({"major": MAJOR, "min_importance": "x"}, "min_importance"),
        ({"major": MAJOR, "top": 0}, "top"),
        ({"major": MAJOR, "top": 101}, "top"),
        ({"major": MAJOR, "industry": "x" * 51}, "industry"),
        ({"major": "x" * 101}, "major"),
    ],
)
def test_invalid_params_are_rejected(params, field):
    with pytest.raises(ValidationError) as excinfo:
        industry.validate_gap_params(params)
    assert excinfo.value.field == field


def test_non_mapping_params_are_rejected():
    with pytest.raises(ValidationError):
        industry.validate_gap_params(["软件工程"])


def test_unknown_major_is_404_with_available_list(industry_db):
    with pytest.raises(NotFoundError) as excinfo:
        industry.analyze_skill_gap(
            {"major": "霍格沃茨魔法学"}, db_path=industry_db, explain=False
        )
    assert excinfo.value.field == "major"
    assert excinfo.value.available == [OTHER_MAJOR, MAJOR]


# ---------------------------------------------------------------------------
# 5. 真实数据库 + 独立参考实现
# ---------------------------------------------------------------------------

def test_real_database_coverage_matches_independent_reference(real_db_path, real_conn):
    """真实库上，每个专业的覆盖率都必须与「直连 sqlite3 重写一遍」的结果一致。"""
    majors = [
        row["department"]
        for row in real_conn.execute(
            "SELECT DISTINCT department FROM courses WHERE department IS NOT NULL"
            " ORDER BY department"
        )
    ]
    assert majors, "真实库里没有专业，请先执行 python scripts/import_data.py"

    for major in majors:
        for min_importance in (1, 3, 4):
            report = industry.analyze_skill_gap(
                {"major": major, "min_importance": min_importance, "top": 100},
                db_path=real_db_path,
                explain=False,
            )
            expected = reference_gap(real_conn, major, min_importance)
            for key, value in expected.items():
                assert report["summary"][key] == value, (major, min_importance, key)

            # 交叉校验：三张清单的条数必须与 summary 里对应的全量计数一致
            assert report["summary"]["covered_skill_count"] == len(report["covered_skills"])
            assert report["summary"]["missing_skill_count"] == len(report["missing_skills"])
            assert (
                report["summary"]["covered_skill_count"]
                + report["summary"]["missing_skill_count"]
                == report["summary"]["required_skill_count"]
            )
            assert len(report["skill_demands"]) == report["summary"]["required_skill_count"]


def test_real_database_is_deterministic(real_db_path):
    first = industry.analyze_skill_gap(
        {"major": MAJOR}, db_path=real_db_path, explain=False
    )
    second = industry.analyze_skill_gap(
        {"major": MAJOR}, db_path=real_db_path, explain=False
    )
    assert first["results_fingerprint"] == second["results_fingerprint"]
    assert json.dumps(first["summary"], ensure_ascii=False, sort_keys=True) == json.dumps(
        second["summary"], ensure_ascii=False, sort_keys=True
    )
    assert first["coverage_rate"] is not None
    assert 0.0 <= first["coverage_rate"] <= 1.0


def test_real_database_importance_matches_independent_reference(real_db_path, real_conn):
    report = industry.analyze_skill_gap(
        {"major": MAJOR, "top": 100}, db_path=real_db_path, explain=False
    )
    rows = real_conn.execute(
        "SELECT js.skill_id AS skill_id, MAX(js.importance) AS importance"
        " FROM jobs AS j JOIN job_skills AS js ON js.job_id = j.job_id"
        " WHERE j.major = ? GROUP BY js.skill_id",
        (MAJOR,),
    ).fetchall()
    expected = {row["skill_id"]: row["importance"] for row in rows}
    got = {item["skill_id"]: item["importance"] for item in report["skill_demands"]}
    assert got == expected
    assert report["importance"]["max"] == max(expected.values())
    # 分布里的技能数之和 = 技能需求总数
    assert sum(
        entry["skill_count"] for entry in report["importance"]["distribution"]
    ) == len(expected)


# ---------------------------------------------------------------------------
# 6. 数据底座只筛选不计算
# ---------------------------------------------------------------------------

def test_queries_layer_grid_is_pure_filtering(conn):
    grid = queries.industry_skill_grid(conn, {"major": MAJOR})
    assert grid["summary"]["jobs"] == 4          # 岗位 4 是别的专业，被过滤
    assert grid["summary"]["courses"] == 3       # 课程 201 是别的专业，被过滤
    assert grid["summary"]["course_skills"] == 5
    assert {job["major"] for job in grid["jobs"]} == {MAJOR}
    assert {row["course_id"] for row in grid["course_skills"]} <= {101, 102, 103}
    by_id = {job["job_id"]: job for job in grid["jobs"]}
    assert by_id[5]["skills"] == []
    assert [item["skill_id"] for item in by_id[1]["skills"]] == [1, 2, 4, 3]
    assert by_id[1]["skills"][0]["is_required"] is True


# ---------------------------------------------------------------------------
# 7. 路由（不碰教室/食堂）
# ---------------------------------------------------------------------------

def test_route_matches_direct_module_result(client, industry_db):
    response = client.get("/api/industry/match", query_string={"major": MAJOR, "explain": 0})
    assert response.status_code == 200
    payload = body_of(response)
    assert payload["status"] == "ok"
    data = payload["data"]

    direct = industry.analyze_skill_gap({"major": MAJOR}, db_path=industry_db, explain=False)
    assert data["results_fingerprint"] == direct["results_fingerprint"]
    assert data["coverage_rate"] == direct["coverage_rate"]
    assert names(data["missing_skills"]) == ["Docker", "沟通表达"]
    assert data["explanation"]["source"] == "local"


def test_route_rejects_bad_parameters(client):
    response = client.get("/api/industry/match", query_string={"min_importance": 9, "major": MAJOR})
    assert response.status_code == 400
    payload = body_of(response)
    assert payload["error"]["code"] == "invalid_parameter"
    assert payload["error"]["field"] == "min_importance"

    response = client.get("/api/industry/match")
    assert response.status_code == 400
    assert body_of(response)["error"]["field"] == "major"


def test_route_unknown_major_is_404(client):
    response = client.get("/api/industry/match", query_string={"major": "霍格沃茨魔法学"})
    assert response.status_code == 404
    payload = body_of(response)
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["field"] == "major"
    assert MAJOR in payload["error"]["available"]


def test_classroom_and_canteen_features_still_work(real_client):
    """本任务不修改教室/食堂功能：老接口在同一个应用里必须照常返回。"""
    rules = {rule.rule for rule in real_client.application.url_map.iter_rules()}
    assert {
        "/api/classrooms/available",
        "/api/classrooms/search",
        "/api/canteens/stats",
        "/api/canteens/trend",
        "/api/canteens/peak",
        "/api/canteens/forecast",
        "/api/industry/skills",
        "/api/industry/gap",
        "/api/industry/match",
    } <= rules

    response = real_client.get(
        "/api/classrooms/available", query_string={"capacity": 10, "limit": 2}
    )
    assert response.status_code == 200
    assert body_of(response)["status"] == "ok"

    response = real_client.get("/api/canteens/trend", query_string={"explain": 0})
    assert response.status_code == 200
    assert body_of(response)["data"]["trend"]["summary"]["days"] > 0

    # 老的纯 Python 缺口接口不受影响
    response = real_client.get("/api/industry/gap", query_string={"major": MAJOR})
    assert response.status_code == 200
    assert body_of(response)["data"]["summary"]["required_skill_count"] > 0


# ---------------------------------------------------------------------------
# 8. AI 只能解释结果：不算覆盖率、不改数、不查库
# ---------------------------------------------------------------------------

def test_ai_payload_contains_no_sql_table_names_or_rows(industry_db):
    """交给模型的事实表里不能出现 SQL、表名、数据库路径、明细或连接。"""
    explainer, session = make_explainer([insight_response(summary="覆盖率 66.7%。")])
    report = industry.analyze_skill_gap(
        {"major": MAJOR},
        db_path=industry_db,
        question="软件工程的课程覆盖企业技能需求了吗？",
        explainer=explainer,
    )

    assert report["explanation"]["source"] == "ai"
    assert report["explanation"]["summary"] == "覆盖率 66.7%。"
    assert session.calls[0]["url"] == "https://ai.example.test/v1/chat/completions"
    assert FAKE_API_KEY not in json.dumps(report, ensure_ascii=False)

    payload = sent_payload(session)
    assert payload["question"] == "软件工程的课程覆盖企业技能需求了吗？"
    assert set(payload) == {
        "question", "major", "scope", "notes", "coverage", "importance",
        "skill_demands", "covered_skills", "missing_skills", "required_missing_skills",
        "job_count",
    }
    # 只给结论，不给原料：没有逐岗位明细、没有课程明细、没有数据库痕迹。
    assert "job_matches" not in payload
    assert payload["coverage"]["coverage_rate"] == 0.6667
    assert payload["importance"]["level"] == "较高"

    dumped = json.dumps(payload, ensure_ascii=False).lower()
    for forbidden in ("select", "insert into", "job_skills", "course_skills", "campus.db",
                      "sqlite", "from job", "from course", "job_matches", "skill_id"):
        assert forbidden not in dumped, forbidden


def test_ai_payload_items_are_copies_not_references(industry_db):
    """事实表里的每条技能都是新建的 dict：模型改它碰不到报告。"""
    report = industry.analyze_skill_gap({"major": MAJOR}, db_path=industry_db, explain=False)
    payload = industry.build_insight_payload(None, report)
    assert payload["skill_demands"][0] is not report["skill_demands"][0]
    assert payload["covered_skills"][0] is not report["covered_skills"][0]
    payload["skill_demands"][0]["skill_name"] = "被改掉"
    payload["skill_demands"].append({"skill_name": "凭空捏造"})
    assert report["skill_demands"][0]["skill_name"] != "被改掉"
    assert "凭空捏造" not in names(report["skill_demands"])


def test_ai_cannot_mutate_results(industry_db):
    """红线：解释器既改事实表又返回自编数字，报告里的计算结果必须纹丝不动。"""
    explainer = AdversarialExplainer()
    report = industry.analyze_skill_gap(
        {"major": MAJOR}, db_path=industry_db, explainer=explainer
    )

    assert report["major"] == MAJOR                              # 专业没被改
    assert report["coverage_rate"] == 0.6667                     # 覆盖率没被改成 1.0
    assert report["summary"]["coverage_rate"] == 0.6667
    assert report["summary"]["covered_skill_count"] == 4
    assert report["summary"]["missing_skill_count"] == 2
    assert names(report["missing_skills"]) == ["Docker", "沟通表达"]   # 缺失没被改成已覆盖
    assert all(item["covered"] is False for item in report["missing_skills"])
    assert "凭空捏造" not in names(report["skill_demands"])
    # 模型塞回来的 coverage_rate 只能待在 explanation 里，不能成为报告字段
    assert report["explanation"]["coverage_rate"] == 1.0
    assert report["results_fingerprint"] == industry.results_fingerprint(report)


def test_results_fingerprint_detects_any_change(industry_db):
    report = industry.analyze_skill_gap({"major": MAJOR}, db_path=industry_db, explain=False)
    baseline = industry.results_fingerprint(report)
    assert industry.results_fingerprint(copy.deepcopy(report)) == baseline

    tampered = copy.deepcopy(report)
    tampered["summary"]["coverage_rate"] = 0.99
    assert industry.results_fingerprint(tampered) != baseline

    tampered = copy.deepcopy(report)
    tampered["missing_skills"] = []
    assert industry.results_fingerprint(tampered) != baseline

    # explanation 不算计算结果，改动它不影响指纹（AI 只被允许写这里）
    tampered = copy.deepcopy(report)
    tampered["explanation"]["summary"] = "随便说说"
    assert industry.results_fingerprint(tampered) == baseline


def test_explanation_stage_cannot_change_results(industry_db, monkeypatch):
    """如果解释阶段越权改了计算结果，整条链路必须直接失败，而不是把错数发出去。"""

    def tampering_explain(question, report, explainer=None):
        report["summary"]["covered_skill_count"] = 99
        return industry.local_explanation(report)

    monkeypatch.setattr(industry, "explain_report", tampering_explain)
    with pytest.raises(RuntimeError) as excinfo:
        industry.analyze_skill_gap({"major": MAJOR}, db_path=industry_db)
    assert "修改了计算结果" in str(excinfo.value)


def test_ai_is_called_only_after_the_database_connection_is_closed(industry_db, monkeypatch):
    """红线：模型调用必须发生在 SQLite 连接关闭之后（否则「AI 不碰数据库」无从保证）。"""
    timeline = []
    real_connection = industry.connection

    @contextlib.contextmanager
    def traced_connection(path):
        with real_connection(path) as connection:
            yield connection
        timeline.append("connection_closed")

    class TimelineExplainer:
        def explain(self, payload):
            timeline.append("ai_called")
            return industry._render_explanation("ok", [], [], source="ai")

    monkeypatch.setattr(industry, "connection", traced_connection)
    industry.analyze_skill_gap(
        {"major": MAJOR}, db_path=industry_db, explainer=TimelineExplainer()
    )
    assert timeline == ["connection_closed", "ai_called"]


def test_ai_output_with_sql_is_rejected_and_falls_back(industry_db):
    """模型就算吐 SQL 片段也没用：整条解释被拒并降级到 Python 兜底文案。"""
    explainer, _ = make_explainer(
        [insight_response(summary="SELECT skill_id FROM job_skills")]
    )
    report = industry.analyze_skill_gap(
        {"major": MAJOR}, db_path=industry_db, explainer=explainer
    )
    explanation = report["explanation"]
    assert explanation["source"] == "local"
    assert explanation["error"]["code"] == industry.ERROR_UNSAFE_EXPLANATION
    assert "SELECT" not in explanation["text"]


def test_ai_network_failure_falls_back_to_local_explanation(industry_db):
    explainer, _ = make_explainer(error=requests.exceptions.Timeout("slow"))
    report = industry.analyze_skill_gap(
        {"major": MAJOR}, db_path=industry_db, explainer=explainer
    )
    explanation = report["explanation"]
    assert explanation["source"] == "local"
    assert explanation["error"]["code"] in ("network_error", "explain_failed")
    assert explanation["text"] and explanation["summary"]
    # 降级之后计算结果的指纹不变
    assert report["results_fingerprint"] == industry.results_fingerprint(report)


def test_ai_invalid_json_falls_back_to_local_explanation(industry_db):
    explainer, _ = make_explainer([insight_response(content="这不是 JSON")])
    report = industry.analyze_skill_gap(
        {"major": MAJOR}, db_path=industry_db, explainer=explainer
    )
    assert report["explanation"]["source"] == "local"
    assert report["explanation"]["error"]["code"] == "invalid_json"


def test_explain_false_never_constructs_the_ai(industry_db, monkeypatch):
    def explode(*args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("explain=False 时不应构造 AI 解释器")

    monkeypatch.setattr(industry, "IndustryExplainer", explode)
    report = industry.analyze_skill_gap(
        {"major": MAJOR}, db_path=industry_db, explain=False
    )
    assert report["explanation"]["source"] == "local"
    text = report["explanation"]["text"]
    # 兜底文案必须引用真实数字，并且不留占位符
    assert "66.7%" in text and "73.9%" in text
    assert "技能覆盖率" in text and "缺失 2 项" in text
    assert "{}" not in text and "{0}" not in text
    assert "。；" not in text


# ---------------------------------------------------------------------------
# 9. 源码级红线：Python 算匹配与覆盖率，不写 SQL，不接 AI 参与计算
# ---------------------------------------------------------------------------

def _industry_imports():
    with open(INDUSTRY_SOURCE, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
            else:
                # `from . import queries`：node.module 是 None，模块名在 names 里。
                imported.update(alias.name for alias in node.names)
    return imported


def test_industry_module_has_no_sql_and_no_sqlite_import():
    """匹配模块既不能自己查库/写 SQL：唯一取数入口是 queries.industry_skill_grid。"""
    imported = _industry_imports()
    assert "sqlite3" not in imported
    assert "queries" in imported  # 唯一的取数入口

    with open(INDUSTRY_SOURCE, "r", encoding="utf-8") as handle:
        source = handle.read()
    for keyword in ("SELECT ", "INSERT INTO", "UPDATE ", "DELETE FROM", "GROUP BY", "JOIN "):
        assert keyword not in source.upper(), keyword


def test_matching_functions_are_pure_python():
    """match_job_to_major / calculate_skill_gap 不碰数据库、不碰网络就能算完。"""
    coverage = synthetic_coverage()
    match = industry.match_job_to_major(JOB_ONE, coverage)
    report = industry.calculate_skill_gap(_synthetic_jobs(), coverage)
    assert match["coverage"]["coverage_rate"] == 0.75
    assert report["summary"]["required_skill_count"] == 6
    # 纯函数：同样的输入必然得到同样的输出（可复算、可测试）
    assert industry.match_job_to_major(JOB_ONE, coverage) == match
    assert industry.calculate_skill_gap(_synthetic_jobs(), coverage) == report


def test_matching_layer_does_not_touch_ai_or_network():
    """计算层（match_job_to_major → calculate_skill_gap）里不出现 AI / 网络调用。"""
    code = _code_only(INDUSTRY_SOURCE)
    start = code.index("def match_job_to_major")
    end = code.index("def results_fingerprint")
    calculation = code[start:end].lower()
    for forbidden in ("requests", "explainer", "call_api", "session", "load_config", "load_prompt"):
        assert forbidden not in calculation, forbidden
    # 解释层才允许出现 AI 相关名字
    explanation_layer = code[end:].lower()
    assert "requests" in explanation_layer
    assert "explainer" in explanation_layer


def _code_only(path):
    """去掉注释与字符串字面量，只留可执行代码（文档里提到 SQL 不算违规）。"""
    with open(path, "r", encoding="utf-8") as handle:
        tokens = tokenize.generate_tokens(io.StringIO(handle.read()).readline)
        return " ".join(
            token.string
            for token in tokens
            if token.type not in (tokenize.COMMENT, tokenize.STRING)
        )


def test_prompt_file_exists_and_states_the_red_lines():
    """正式 Prompt 必须落在 prompts/ 下，并且写明「不计算、不改数、不提表名」。"""
    assert os.path.isfile(PROMPT_PATH)
    with open(PROMPT_PATH, "r", encoding="utf-8") as handle:
        prompt = handle.read()
    for phrase in ("不重新算覆盖率", "覆盖技能", "缺失技能", "重要程度", "job_skills", "course_skills"):
        assert phrase in prompt, phrase
    assert "coverage_rate" in prompt
