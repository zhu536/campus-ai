# -*- coding: utf-8 -*-
"""产教融合技能分析（场景三）：企业岗位技能需求 ↔ 专业课程技能覆盖。

链路与分工（AGENTS.md AI Rules）::

    AI 理解问题      backend/ai_parser.py + backend/validate.py
                     （意图 industry_skill_match / industry_skill_gap 已在白名单里，
                       本模块不重复实现意图识别，只负责拿到 major 之后的部分）
      → 参数校验      backend/validators.py（路由层）+ 本模块 validate_gap_params（业务层）
      → 数据底座      backend/queries.industry_skill_grid（SQLite 只做筛选，不做计算）
      → 匹配与计算    **本模块**（集合运算 + 覆盖率 + 重要度分级，全部是 Python）
      → AI 解释结果   prompts/industry_insight_generator.md（只解释，不计算、不查询）

五个输出（任务要求，与字段名一一对应）::

    1. 技能需求     ``skill_demands``   岗位要求哪些技能、要多重要（带 covered 标记）
    2. 已覆盖技能   ``covered_skills``  技能需求 ∩ 专业课程覆盖的技能
    3. 缺失技能     ``missing_skills``  技能需求 − 专业课程覆盖的技能
    4. 技能覆盖率   ``coverage_rate``   覆盖数 / 要求数（另有重要度加权版）
    5. 技能重要程度 ``importance``      每个技能的 1..5 分与中文档位 + 整体分布

核心方法::

    match_job_to_major(job, coverage)      单个岗位 × 一个专业
    calculate_skill_gap(jobs, coverage)    一个专业 × 全部对口岗位

匹配与覆盖率的定义（与 docs/DATABASE.md「场景三」一致）::

    技能需求     = 该专业对口岗位要求的全部技能（按 min_importance 过滤后）
    已覆盖技能   = 技能需求 ∩ 课程覆盖技能
    缺失技能     = 技能需求 − 课程覆盖技能
    技能覆盖率   = |已覆盖技能| / |技能需求|
    加权覆盖率   = Σ 已覆盖技能的重要度 / Σ 技能需求的重要度
    技能重要程度 = 该技能在所有对口岗位上被要求的**最大**重要度

    重要度取「最大值」而不是求和：``importance`` 是「岗位 × 技能」的属性，
    按技能聚合后用最大值代表「这个技能最多被要求到多重要」，
    因此越重要的技能缺失，对加权覆盖率的拉低越明显。

红线（``tests/test_industry.py`` 逐条锁定）::

    1. 所有匹配与覆盖率都由 Python 算：本模块**不 import sqlite3、不写 SQL**，
       唯一取数入口是 ``backend/queries.industry_skill_grid``。
    2. AI 不算覆盖率：模型只拿到 :func:`build_insight_payload` 生成的只读事实表
       （没有连接、没有 SQL、没有原始明细），想自己算也无从下手。
    3. AI 不能修改计算结果：解释只写进 ``report["explanation"]``；解释前后用
       :func:`results_fingerprint` 校验计算结果没有被改动，被改动就直接抛错。
    4. AI 只能解释最终结果：解释文本由 :class:`IndustryExplainer` 按
       ``prompts/industry_insight_generator.md`` 生成；任何失败都降级为
       :func:`local_explanation` 的 Python 兜底文案，接口不会 500。

测试见 ``tests/test_industry.py``。
"""

import hashlib
import json
import os
import re

import requests

from . import queries
from .ai_parser import (
    ERROR_NETWORK,
    AIParserError,
    extract_json_object,
    load_config,
    load_prompt,
    parse_response_text,
)
from .db import connection, default_db_path
from .errors import ValidationError
from .validate import check_int, check_text

# backend/industry.py → backend/ → 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_INSIGHT_PROMPT_PATH = os.path.join(
    PROJECT_ROOT, "prompts", "industry_insight_generator.md"
)

# ---------------------------------------------------------------------------
# 0. 口径常量
# ---------------------------------------------------------------------------

#: 课程覆盖程度权重：越熟练，说明课程对该技能的支撑越强（与 backend/queries.py 一致）。
LEVEL_RANK = {"了解": 1, "掌握": 2, "熟练": 3}

#: 技能重要程度（job_skills.importance 1..5）→ 中文档位，按分数从高到低排列。
IMPORTANCE_LEVELS = (
    (5, "核心"),
    (4, "重要"),
    (3, "较高"),
    (2, "一般"),
    (1, "次要"),
)

MIN_IMPORTANCE = 1
MAX_IMPORTANCE = 5
#: job_skills.importance 缺失或非法时的兜底分（与 docs/DATABASE.md 的 DEFAULT 3 一致）。
DEFAULT_IMPORTANCE = 3
#: 「核心技能」的门槛：重要度达到该值即视为核心。
CORE_IMPORTANCE_MIN = 5

DEFAULT_MIN_IMPORTANCE = MIN_IMPORTANCE
DEFAULT_TOP = 20
MAX_TOP = 100

#: 文本字段长度上限（与 backend/routes.py 的 _gap_params 保持一致）。
MAX_MAJOR_LENGTH = 100
MAX_INDUSTRY_LENGTH = 50
#: 用户问题长度上限（与 backend/classroom.py、backend/forecast.py 保持一致）。
MAX_QUESTION_LENGTH = 200
#: 解释文本的行长与条数上限（与 backend/forecast.py 保持一致）。
MAX_INSIGHT_LINE = 200
MAX_INSIGHT_ITEMS = 3
#: 解释结果里的错误码。
ERROR_UNSAFE_EXPLANATION = "unsafe_explanation"
ERROR_INVALID_EXPLANATION = "invalid_explanation"

#: 参与「AI 不得修改计算结果」指纹校验的字段：全部是 Python 算出来的东西。
FINGERPRINT_KEYS = (
    "major",
    "summary",
    "skill_demands",
    "covered_skills",
    "missing_skills",
    "required_missing_skills",
    "coverage_rate",
    "importance",
    "job_matches",
    "notes",
)

#: SQL 片段特征：只在**模型返回的解释**上做过滤，不用来改写任何查询。
_SQL_RE = re.compile(
    r"\b(select|insert|update|delete|drop|alter|create|attach|detach|pragma|union|truncate|exec)\b"
    r"|--|/\*|\*/",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# 1. 小工具
# ---------------------------------------------------------------------------

def _round(value, digits=2):
    return None if value is None else round(float(value), digits)


def _coerce_importance(value):
    """把 ``job_skills.importance`` 收敛成 1..5 的整数。

    数据库列本身有 ``CHECK (importance BETWEEN 1 AND 5)``；这里再夹一道，
    是为了让 :func:`match_job_to_major` 成为纯粹的 Python 函数：即使调用方直接
    用手写的 dict 喂进来（测试、脚本、未来的别的数据源），也不会算出越界的权重。
    """
    if value is None or isinstance(value, bool):
        return DEFAULT_IMPORTANCE
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_IMPORTANCE
    return max(MIN_IMPORTANCE, min(MAX_IMPORTANCE, number))


def importance_level(score):
    """把 1..5 的重要度分数翻译成中文档位（技能重要程度）。

    >>> importance_level(5), importance_level(3), importance_level(1)
    ('核心', '较高', '次要')
    """
    if score is None:
        return None
    for minimum, label in IMPORTANCE_LEVELS:
        if score >= minimum:
            return label
    return IMPORTANCE_LEVELS[-1][1]


def check_question(question):
    """校验可选的用户问题，返回去掉首尾空白的文本（``None`` 表示不解释原问题）。"""
    if question is None:
        return None
    if not isinstance(question, str):
        raise ValidationError("参数 question 必须是字符串", field="question")
    text = question.strip()
    if not text:
        return None
    if len(text) > MAX_QUESTION_LENGTH:
        raise ValidationError(
            "参数 question 最长 {} 个字符，实际 {}".format(MAX_QUESTION_LENGTH, len(text)),
            field="question",
        )
    return text


def _sort_key(item):
    """展示与截断的统一排序：重要度降序 → 缺口优先 → 岗位数降序 → 名称升序。

    「缺口优先」放在第二位的理由：这是一张**缺口报表**，同样重要的技能里
    没被课程覆盖的那一条更值得先看到（``top`` 截断时才不会把缺口挤掉）。
    """
    return (
        -item["importance"],
        0 if not item["covered"] else 1,
        -item["job_count"],
        item["skill_name"],
    )


# ---------------------------------------------------------------------------
# 2. 参数校验（业务层第二道；第一道在 backend/validators.py 的路由层）
# ---------------------------------------------------------------------------

def validate_gap_params(params, warnings=None):
    """校验并归一化产教融合技能分析参数。

    即使调用方直接构造 ``params`` 也要经过这一套类型/范围检查，
    非法取值直接抛 ``ValidationError``（HTTP 400），绝不「静默按默认值跑」：

        - ``major``：必填，非空字符串（不存在时在查询层抛 404）
        - ``industry``：可选，只统计该行业的对口岗位
        - ``min_importance``：1..5，默认 1（只看重要度 >= 该值的技能）
        - ``top``：1..100，默认 20（三张技能清单各返回多少条）
    """
    if not isinstance(params, dict):
        raise ValidationError("产教融合技能分析的查询参数必须是对象（object）", field="params")

    raw_major = params.get("major")
    if raw_major is None or (isinstance(raw_major, str) and not raw_major.strip()):
        raise ValidationError(
            "缺少必填参数 major（专业名，例如「软件工程」）", field="major"
        )
    major = check_text(raw_major, "major", max_length=MAX_MAJOR_LENGTH)

    # 空串按「没传」处理（与 backend/validators.text_param 的约定一致）。
    industry = None
    if params.get("industry"):
        industry = check_text(
            params["industry"], "industry", max_length=MAX_INDUSTRY_LENGTH
        )

    min_importance = params.get("min_importance")
    if min_importance is None:
        min_importance = DEFAULT_MIN_IMPORTANCE
    else:
        min_importance = check_int(
            min_importance,
            "min_importance",
            minimum=MIN_IMPORTANCE,
            maximum=MAX_IMPORTANCE,
        )

    top = params.get("top")
    if top is None:
        top = DEFAULT_TOP
    else:
        top = check_int(top, "top", minimum=1, maximum=MAX_TOP)

    if warnings is not None and min_importance > MIN_IMPORTANCE:
        warnings.append(
            "本次只看重要度 >= {} 的技能：重要度更低的技能不计入技能需求、"
            "覆盖率与重要程度分布。".format(min_importance)
        )

    return {
        "major": major,
        "industry": industry,
        "min_importance": min_importance,
        "top": top,
    }


# ---------------------------------------------------------------------------
# 3. 专业课程技能覆盖（输入之二）
# ---------------------------------------------------------------------------

def build_major_coverage(major, course_skill_rows):
    """把「课程 × 技能」明细聚合成该专业的**技能覆盖表**。

    ``course_skill_rows`` 来自 ``backend/queries.industry_skill_grid``：

        [{"course_id": 101, "course_name": "程序设计基础", "skill_id": 1,
          "skill_name": "Python", "skill_category": "编程语言", "level": "熟练"}, ...]

    同一技能被多门课覆盖时，取**覆盖程度最高**的那一门代表该技能的水平
    （了解 < 掌握 < 熟练），并把所有覆盖它的课程名收集起来，方便人工核对
    「这门技能到底是谁在教」。本函数只做汇总与取最大值，不做任何集合运算。
    """
    skills = {}
    courses = {}

    for row in course_skill_rows or ():
        skill_id = row.get("skill_id")
        if skill_id is None:
            continue
        course_id = row.get("course_id")
        if course_id is not None:
            courses[course_id] = row.get("course_name") or ""

        level = row.get("level")
        rank = LEVEL_RANK.get(level, 0)
        entry = skills.get(skill_id)
        if entry is None:
            entry = skills[skill_id] = {
                "skill_id": skill_id,
                "skill_name": row.get("skill_name") or "技能 {}".format(skill_id),
                "skill_category": row.get("skill_category") or "其他",
                "level": level,
                "level_rank": rank,
                "course_count": 0,
                "courses": [],
            }
        entry["course_count"] += 1
        course_name = row.get("course_name")
        if course_name and course_name not in entry["courses"]:
            entry["courses"].append(course_name)
        if rank > entry["level_rank"]:
            entry["level"] = level
            entry["level_rank"] = rank

    for entry in skills.values():
        entry["courses"].sort()

    return {
        "major": major,
        "skills": skills,
        "skill_count": len(skills),
        "course_count": len(courses),
        # 课程侧的总权重：反向指标 course_covered_ratio 的分母。
        "weighted_rank_total": sum(entry["level_rank"] for entry in skills.values()),
    }


# ---------------------------------------------------------------------------
# 4. 核心方法一：match_job_to_major（单个岗位 × 一个专业）
# ---------------------------------------------------------------------------

def _normalize_job_skills(job):
    """把一个岗位的 ``skills[]`` 收敛成 ``{skill_id: 需求条目}``（同技能取最严要求）。

    ``job_skills`` 上有 ``UNIQUE (job_id, skill_id)``，正常一对一；这里做一次合并是
    防御性的：万一调用方喂进来的数据有重复行，也按「重要度取最大、硬性要求取或」
    处理，不会把同一个技能重复计进覆盖率的分母。
    """
    demands = {}
    for raw in job.get("skills") or ():
        skill_id = raw.get("skill_id")
        if skill_id is None:
            continue
        importance = _coerce_importance(raw.get("importance"))
        is_required = bool(raw.get("is_required"))
        entry = demands.get(skill_id)
        if entry is None:
            demands[skill_id] = {
                "skill_id": skill_id,
                "skill_name": raw.get("skill_name") or "技能 {}".format(skill_id),
                "skill_category": raw.get("skill_category") or "其他",
                "importance": importance,
                "is_required": is_required,
            }
        else:
            entry["importance"] = max(entry["importance"], importance)
            entry["is_required"] = entry["is_required"] or is_required
    return demands


def _demand_item(entry, importance, is_required, job_count, required_job_count, avg_importance, coverage_entry):
    """拼一条「技能需求」：Python 侧统一在这里补上覆盖标记与重要程度档位。"""
    item = {
        "skill_id": entry["skill_id"],
        "skill_name": entry["skill_name"],
        "skill_category": entry["skill_category"],
        "importance": importance,
        "avg_importance": _round(avg_importance, 2),
        "importance_level": importance_level(importance),
        "is_required": bool(is_required),
        "job_count": job_count,
        "required_job_count": required_job_count,
        "covered": coverage_entry is not None,
        "course_level": coverage_entry["level"] if coverage_entry else None,
    }
    if coverage_entry:
        item["course_count"] = coverage_entry["course_count"]
        item["courses"] = list(coverage_entry["courses"])
    return item


def _split_items(items):
    """把技能需求拆成「已覆盖 / 缺失 / 缺失且硬性要求」三个子集（都已排好序）。"""
    items = sorted(items, key=_sort_key)
    covered = [item for item in items if item["covered"]]
    missing = [item for item in items if not item["covered"]]
    required_missing = [item for item in missing if item["is_required"]]
    return items, covered, missing, required_missing


def _coverage_block(items):
    """覆盖率（输出 4）：纯集合/权重算术，全部由 Python 完成。"""
    required_count = len(items)
    covered = [item for item in items if item["covered"]]
    total_weight = sum(item["importance"] for item in items)
    covered_weight = sum(item["importance"] for item in covered)
    return {
        "required_skill_count": required_count,
        "covered_skill_count": len(covered),
        "missing_skill_count": required_count - len(covered),
        "required_missing_count": sum(
            1 for item in items if item["is_required"] and not item["covered"]
        ),
        "coverage_rate": _round(len(covered) / required_count, 4) if required_count else None,
        "weighted_coverage_rate": (
            _round(covered_weight / total_weight, 4) if total_weight else None
        ),
        "importance_total": total_weight,
        "covered_importance_total": covered_weight,
    }


def _importance_block(items):
    """技能重要程度（输出 5）：逐档分布 + 整体档位，同样是纯 Python 统计。"""
    scores = [item["importance"] for item in items]
    total = sum(scores)
    average = total / len(scores) if scores else None

    distribution = []
    for score, label in IMPORTANCE_LEVELS:
        bucket = [item for item in items if item["importance"] == score]
        if not bucket:
            continue
        covered = sum(1 for item in bucket if item["covered"])
        distribution.append(
            {
                "importance": score,
                "level": label,
                "skill_count": len(bucket),
                "covered_count": covered,
                "missing_count": len(bucket) - covered,
                "share": _round(len(bucket) / len(items), 4) if items else None,
            }
        )

    return {
        "scale": "{}-{}".format(MIN_IMPORTANCE, MAX_IMPORTANCE),
        "levels": [label for _score, label in IMPORTANCE_LEVELS],
        "max": max(scores) if scores else None,
        "avg": _round(average, 2),
        "total": total,
        "level": importance_level(average),
        "core_skill_count": sum(1 for score in scores if score >= CORE_IMPORTANCE_MIN),
        "distribution": distribution,
    }


def match_job_to_major(job, coverage, min_importance=DEFAULT_MIN_IMPORTANCE):
    """把**一个岗位**的技能需求与**一个专业**的课程技能覆盖做匹配。

    ``job`` 形如::

        {"job_id": 1, "job_title": "Python 后端开发", "company": "某某科技",
         "industry": "软件开发", "major": "软件工程",
         "skills": [{"skill_id": 1, "skill_name": "Python", "skill_category": "编程语言",
                     "importance": 5, "is_required": True}, ...]}

    ``coverage`` 是 :func:`build_major_coverage` 的结果。

    匹配规则（全部是 Python 集合运算，没有任何 SQL、没有任何模型参与）::

        技能需求   = 该岗位的技能（按 min_importance 过滤后，同技能取最严要求）
        已覆盖技能 = 技能需求 ∩ 专业课程覆盖的技能
        缺失技能   = 技能需求 − 专业课程覆盖的技能
        技能覆盖率 = |已覆盖技能| / |技能需求|
        加权覆盖率 = Σ 已覆盖技能的重要度 / Σ 技能需求的重要度

    返回 ``skill_demands``（**全量**技能需求，带 ``covered`` 标记）以及它的两个子集
    ``covered_skills`` / ``missing_skills``，外加 ``coverage``（输出 4）与
    ``importance``（输出 5）。返回的所有数字都只由这个函数算出，没有外部依赖。
    """
    job = job or {}
    major_skills = (coverage or {}).get("skills") or {}

    items = []
    for entry in _normalize_job_skills(job).values():
        if entry["importance"] < min_importance:
            continue
        items.append(
            _demand_item(
                entry,
                importance=entry["importance"],
                is_required=entry["is_required"],
                job_count=1,
                required_job_count=1 if entry["is_required"] else 0,
                avg_importance=entry["importance"],
                coverage_entry=major_skills.get(entry["skill_id"]),
            )
        )

    demands, covered, missing, required_missing = _split_items(items)

    notes = []
    if not demands:
        notes.append(
            "岗位「{}」在本次筛选条件下没有任何技能需求。".format(
                job.get("job_title") or job.get("job_id") or "（未知）"
            )
        )

    return {
        "job_id": job.get("job_id"),
        "job_title": job.get("job_title"),
        "company": job.get("company"),
        "industry": job.get("industry"),
        "major": job.get("major"),
        "skill_demands": demands,
        "covered_skills": covered,
        "missing_skills": missing,
        "required_missing_skills": required_missing,
        "coverage": _coverage_block(demands),
        "importance": _importance_block(demands),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 5. 核心方法二：calculate_skill_gap（一个专业 × 全部对口岗位）
# ---------------------------------------------------------------------------

def _aggregate_job_demands(matches):
    """把各岗位的匹配结果按技能合并（跨岗位的 job_count / 最大重要度 / 硬性要求）。"""
    aggregated = {}
    for order, match in enumerate(matches):
        for item in match["skill_demands"]:
            entry = aggregated.get(item["skill_id"])
            if entry is None:
                aggregated[item["skill_id"]] = {
                    "skill_id": item["skill_id"],
                    "skill_name": item["skill_name"],
                    "skill_category": item["skill_category"],
                    "importance": item["importance"],
                    "importance_sum": item["importance"],
                    "job_count": 1,
                    "required_job_count": 1 if item["is_required"] else 0,
                    "is_required": item["is_required"],
                    "first_seen": order,
                }
            else:
                entry["importance"] = max(entry["importance"], item["importance"])
                entry["importance_sum"] += item["importance"]
                entry["job_count"] += 1
                entry["required_job_count"] += 1 if item["is_required"] else 0
                entry["is_required"] = entry["is_required"] or item["is_required"]
    return aggregated


def calculate_skill_gap(jobs, coverage, min_importance=DEFAULT_MIN_IMPORTANCE, top=None):
    """把**一个专业**的全部对口岗位合并成一份技能缺口报表（五个输出的总表）。

    ``jobs`` 是 ``backend/queries.industry_skill_grid`` 返回的岗位列表（每个岗位自带
    ``skills[]``），``coverage`` 是 :func:`build_major_coverage` 的结果。

    岗位级的匹配逐个委托给 :func:`match_job_to_major`（保证「单个岗位」和「整个专业」
    用的是同一套规则），再按 ``skill_id`` 聚合：

        job_count          有多少个对口岗位要求这项技能
        required_job_count 其中把它标成硬性要求的岗位数
        importance         这些岗位里的**最大**重要度（重要程度的最终口径）
        avg_importance     这些岗位上的平均重要度（供人工核对）

    ``top`` 只截断返回的三张技能清单，``summary`` 里的计数**始终是全量**
    （与 docs/API.md 的 /api/industry/gap 同一约定）。

    ``min_importance`` 是**技能级**门槛（与 queries.industry_skill_gap 同口径）：
    它只决定哪些技能进入覆盖率与三张清单，不改变「岗位数 / 硬性要求 / 平均重要度」
    这些事实 —— 一个技能即使只被某个岗位按低重要度要求，它的 ``is_required``
    仍然按全部岗位汇总，否则「硬性要求却未覆盖」会被门槛悄悄漏掉。
    """
    jobs = list(jobs or ())
    major = (coverage or {}).get("major")
    major_skills = (coverage or {}).get("skills") or {}

    # (a) 技能级聚合：先按「不筛重要度」把所有岗位跑一遍，让 job_count / is_required /
    #     平均重要度反映全部岗位的事实。
    all_matches = [
        match_job_to_major(job, coverage, min_importance=MIN_IMPORTANCE) for job in jobs
    ]
    aggregated = _aggregate_job_demands(all_matches)

    # (b) 岗位级明细：按用户给的门槛过滤（与直接调用 match_job_to_major 完全一致）。
    matches = (
        all_matches
        if min_importance <= MIN_IMPORTANCE
        else [
            match_job_to_major(job, coverage, min_importance=min_importance) for job in jobs
        ]
    )

    # (c) 技能级门槛：只影响参与覆盖率与三张清单的技能。
    items = [
        _demand_item(
            entry,
            importance=entry["importance"],
            is_required=entry["is_required"],
            job_count=entry["job_count"],
            required_job_count=entry["required_job_count"],
            avg_importance=entry["importance_sum"] / entry["job_count"],
            coverage_entry=major_skills.get(entry["skill_id"]),
        )
        for entry in aggregated.values()
        if entry["importance"] >= min_importance
    ]

    demands, covered, missing, required_missing = _split_items(items)
    coverage_block = _coverage_block(demands)
    importance_block = _importance_block(demands)

    # 反向指标：该专业课程覆盖的技能里，有多大比例真的被对口岗位要求
    # （权重是课程覆盖程度 1..3，见 build_major_coverage）。
    course_weight_total = (coverage or {}).get("weighted_rank_total") or 0
    covered_course_weight = sum(
        major_skills[item["skill_id"]]["level_rank"]
        for item in covered
        if item["skill_id"] in major_skills
    )
    course_covered_ratio = (
        _round(covered_course_weight / course_weight_total, 4) if course_weight_total else None
    )

    filtered_out = len(aggregated) - len(items)

    notes = []
    jobs_without_skills = [
        match for match in all_matches if not match["skill_demands"]
    ]
    if not jobs:
        notes.append("该专业在本次筛选条件下没有对口岗位，技能需求为空。")
    else:
        if jobs_without_skills:
            notes.append(
                "有 {} 个对口岗位没有登记技能要求，已计入岗位数但不计入技能需求。".format(
                    len(jobs_without_skills)
                )
            )
        if not demands:
            notes.append("本次筛选条件下没有任何技能需求，覆盖率无法计算。")
    if not (coverage or {}).get("skill_count"):
        notes.append("该专业的课程还没有登记技能覆盖，缺失技能无法被课程支撑。")
    if covered and course_covered_ratio is not None:
        notes.append(
            "该专业课程共覆盖 {} 项技能，其中 {} 项被对口岗位要求。".format(
                (coverage or {}).get("skill_count", 0), len(covered)
            )
        )
    if filtered_out:
        notes.append(
            "已按重要度筛选：重要度低于 {} 的 {} 项技能不计入技能需求、"
            "覆盖率与重要程度分布。".format(min_importance, filtered_out)
        )

    returned = len(demands) if top is None else min(top, len(demands))
    truncated = top is not None and returned < len(demands)

    summary = {
        "major": major,
        "job_count": len(jobs),
        "matched_job_count": len(jobs) - len(jobs_without_skills),
        "course_count": (coverage or {}).get("course_count", 0),
        "course_skill_count": (coverage or {}).get("skill_count", 0),
        "min_importance": min_importance,
        "required_skill_count": coverage_block["required_skill_count"],
        "covered_skill_count": coverage_block["covered_skill_count"],
        "missing_skill_count": coverage_block["missing_skill_count"],
        "required_missing_count": coverage_block["required_missing_count"],
        "coverage_rate": coverage_block["coverage_rate"],
        "weighted_coverage_rate": coverage_block["weighted_coverage_rate"],
        "course_covered_ratio": course_covered_ratio,
        "importance_total": coverage_block["importance_total"],
        "covered_importance_total": coverage_block["covered_importance_total"],
        "returned_skill_count": returned,
        "truncated": truncated,
    }

    return {
        "major": major,
        # 1. 技能需求（带 covered 标记的全集）
        "skill_demands": demands if top is None else demands[:top],
        # 2. 已覆盖技能
        "covered_skills": covered if top is None else covered[:top],
        # 3. 缺失技能
        "missing_skills": missing if top is None else missing[:top],
        "required_missing_skills": (
            required_missing if top is None else required_missing[:top]
        ),
        # 4. 技能覆盖率（同时在 summary 里，方便直接取用）
        "coverage_rate": coverage_block["coverage_rate"],
        # 5. 技能重要程度
        "importance": importance_block,
        "summary": summary,
        # 明细：逐岗位的匹配结果（match_job_to_major 的原始输出，便于下钻）
        "job_matches": matches,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 6. 计算结果指纹（红线：AI 不能修改计算结果）
# ---------------------------------------------------------------------------

def results_fingerprint(report):
    """计算结果指纹：解释阶段前后必须完全一致。

    覆盖 :data:`FINGERPRINT_KEYS` 里的全部数值字段（不含 ``explanation``），
    因此「模型返回里多带了一个 coverage_rate」或者「某段代码顺手改了 summary」
    都会让指纹变化，:func:`analyze_skill_gap` 会立刻抛错而不是把错误结果发出去。
    """
    payload = {key: report.get(key) for key in FINGERPRINT_KEYS}
    dumped = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 7. AI 结果解释器（只解释，不计算、不查询、不改数）
# ---------------------------------------------------------------------------

def _skill_line(item):
    """把一条技能渲染成一行中文（解释与兜底文案共用，保证口径一致）。"""
    parts = [
        item["skill_name"],
        "重要度 {}/{}（{}）".format(item["importance"], MAX_IMPORTANCE, item["importance_level"]),
        "{} 个岗位要求".format(item["job_count"]),
    ]
    if item["covered"]:
        parts.append("课程已覆盖（{}）".format(item.get("course_level") or "已覆盖"))
    else:
        parts.append("课程未覆盖")
    if item["is_required"]:
        parts.append("硬性要求")
    return "，".join(parts)


def build_insight_payload(question, report):
    """构造交给模型的「只读事实表」。

    只有已经算好的数字与中文口径描述：**没有 SQL、没有表名、没有数据库路径、
    没有连接，也没有原始明细**（岗位—技能 / 课程—技能的行），所以模型即使想
    「自己算一遍覆盖率」也没有原料可算；它只能解释下面这些结论。
    """
    summary = report["summary"]
    importance = report["importance"]

    return {
        "question": question,
        "major": report["major"],
        "scope": [report.get("scope") or "专业「{}」".format(report["major"])],
        "notes": list(report.get("notes") or ()),
        "coverage": {
            "required_skill_count": summary["required_skill_count"],
            "covered_skill_count": summary["covered_skill_count"],
            "missing_skill_count": summary["missing_skill_count"],
            "required_missing_count": summary["required_missing_count"],
            "coverage_rate": summary["coverage_rate"],
            "weighted_coverage_rate": summary["weighted_coverage_rate"],
            "course_covered_ratio": summary["course_covered_ratio"],
        },
        "importance": {
            "scale": importance["scale"],
            "max": importance["max"],
            "avg": importance["avg"],
            "level": importance["level"],
            "levels": list(importance["levels"]),
            "distribution": [dict(entry) for entry in importance["distribution"]],
        },
        "skill_demands": [_insight_item(item) for item in report["skill_demands"]],
        "covered_skills": [_insight_item(item) for item in report["covered_skills"]],
        "missing_skills": [_insight_item(item) for item in report["missing_skills"]],
        "required_missing_skills": [
            _insight_item(item) for item in report["required_missing_skills"]
        ],
        "job_count": summary["job_count"],
    }


def _insight_item(item):
    """事实表里的一条技能：只保留解释需要的字段，并且是**新建的 dict**。

    新建（而不是引用原对象）是关键：模型拿到的是副本，就算它就地改这个 dict，
    也碰不到报告里的任何数字 —— 这是「AI 不能修改计算结果」的第一道保障。
    """
    return {
        "skill_name": item["skill_name"],
        "skill_category": item["skill_category"],
        "importance": item["importance"],
        "importance_level": item["importance_level"],
        "is_required": item["is_required"],
        "job_count": item["job_count"],
        "covered": item["covered"],
        "course_level": item["course_level"],
    }


def _render_explanation(summary, highlights, suggestions, source, error=None):
    """统一解释结构：一个摘要 + 少量要点 + 建议（AI 与兜底共用）。"""
    lines = [summary]
    lines.extend("• " + item for item in highlights)
    if suggestions:
        # 建议本身就是短句，统一去掉句末句号再用「；」拼接，避免出现「。；」。
        lines.append("建议：" + "；".join(item.rstrip("。") for item in suggestions) + "。")
    return {
        "text": "\n".join(lines),
        "summary": summary,
        "highlights": list(highlights),
        "suggestions": list(suggestions),
        "source": source,
        "error": error,
    }


def _percent(value):
    return "{:.1%}".format(value) if value is not None else "无法计算"


def local_explanation(report):
    """Python 兜底解释：AI 不可用或输出不合法时使用，仍然是「只解释已算好的结果」。"""
    summary = report["summary"]
    importance = report["importance"]
    major = report["major"]
    required = summary["required_skill_count"]

    if not required:
        head = "专业「{}」的 {} 个对口岗位在本次条件下没有技能需求，技能覆盖率无法计算。".format(
            major, summary["job_count"]
        )
    else:
        head = (
            "专业「{}」对 {} 个对口岗位统计出 {} 项技能需求，课程覆盖 {} 项、"
            "缺失 {} 项，技能覆盖率 {}（重要度加权 {}）。"
        ).format(
            major,
            summary["job_count"],
            required,
            summary["covered_skill_count"],
            summary["missing_skill_count"],
            _percent(summary["coverage_rate"]),
            _percent(summary["weighted_coverage_rate"]),
        )

    highlights = []
    if importance["level"]:
        highlights.append(
            "技能重要程度整体「{}」：平均 {}/{}，最高 {}/{}，其中核心技能 {} 项。".format(
                importance["level"],
                importance["avg"],
                MAX_IMPORTANCE,
                importance["max"],
                MAX_IMPORTANCE,
                importance["core_skill_count"],
            )
        )
    if report["missing_skills"]:
        highlights.append(
            "最该补的缺口：" + "；".join(_skill_line(item) for item in report["missing_skills"][:3])
        )
    if report["covered_skills"]:
        highlights.append(
            "已覆盖的技能：" + "、".join(item["skill_name"] for item in report["covered_skills"][:5])
        )

    suggestions = []
    if report["required_missing_skills"]:
        suggestions.append(
            "优先补齐硬性要求但课程未覆盖的技能：{}".format(
                "、".join(item["skill_name"] for item in report["required_missing_skills"][:3])
            )
        )
    elif report["missing_skills"]:
        suggestions.append(
            "缺口技能暂非硬性要求，可先按重要度排期补充：{}".format(
                "、".join(item["skill_name"] for item in report["missing_skills"][:3])
            )
        )
    if required and summary["coverage_rate"] == 1.0:
        suggestions.append("岗位要求的技能已被课程全覆盖，可把重心放到提高覆盖程度（了解→掌握→熟练）")
    if summary["course_covered_ratio"] is not None and summary["course_covered_ratio"] < 0.5:
        suggestions.append(
            "该专业有不少课程技能并未被对口岗位要求（贴合度 {}），可对照岗位需求调整课程内容".format(
                _percent(summary["course_covered_ratio"])
            )
        )

    return _render_explanation(head, highlights, suggestions, source="local")


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
    """把模型返回的 JSON 收敛成统一结构。

    关键：这里**只取** summary / highlights / suggestions 三个文本字段，
    模型返回的任何其它键（例如它自己编的 ``coverage_rate``）都会被丢弃，
    因此模型没有渠道把数字改回去。
    """
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


class IndustryExplainer:
    """AI 结果解释器：把已经算好的技能缺口报表讲成人话。

    与 :class:`backend.forecast.ForecastExplainer` 同一套约定：配置从 ``.env`` 读、
    Prompt 从 ``prompts/`` 读（正式 Prompt 走 Git 留痕）、Key 不写进代码。
    区别是它只看到**匹配与覆盖率结果**：没有连接、没有 SQL、没有查询入口，
    因此 AI 不可能替 Python 算覆盖率，也不可能改口径。
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
        """system prompt = ``prompts/industry_insight_generator.md``。"""
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
        """解释技能缺口结果，返回统一解释结构（``source = "ai"``）。"""
        return _compose_explanation(extract_json_object(self.call_api(payload)), source="ai")


def explain_report(question, report, explainer=None):
    """AI 解释技能缺口结果；任何失败都退回 Python 兜底解释，绝不让整条链路失败。"""
    try:
        payload = build_insight_payload(question, report)
        instance = explainer if explainer is not None else IndustryExplainer()
        explanation = instance.explain(payload)
        explanation["error"] = None
        return explanation
    except Exception as exc:  # AI 不可用 / 返回不合法：降级但不能 500
        fallback = local_explanation(report)
        fallback["error"] = (
            exc.to_dict() if hasattr(exc, "to_dict") else {"code": "explain_failed", "message": str(exc)}
        )
        return fallback


# ---------------------------------------------------------------------------
# 8. 完整链路
# ---------------------------------------------------------------------------

def _scope_label(grid, params):
    """中文口径描述，供人工核对与 AI 解释使用（不含表名、列名、SQL）。"""
    parts = ["专业「{}」".format(params["major"])]
    if params.get("industry"):
        parts.append("限定行业「{}」".format(params["industry"]))
    parts.append(
        "{} 个对口岗位、{} 门课程、{} 项课程技能".format(
            grid["summary"]["jobs"],
            grid["summary"]["courses"],
            grid["summary"]["course_skills"],
        )
    )
    return "，".join(parts)


def analyze_skill_gap(params, db_path=None, question=None, explainer=None, explain=True):
    """产教融合技能分析完整链路（本模块唯一对外入口）。

    参数：
        params     查询参数（见 :func:`validate_gap_params`）
        db_path    SQLite 文件路径（默认 ``data/campus.db``，可用 ``CAMPUS_DB_PATH`` 覆盖）
        question   用户原始问题（可选，仅用于让 AI 结合原问题解释）
        explainer  结果解释器（默认 :class:`IndustryExplainer`，测试可注入假实现）
        explain    ``False`` 时跳过 AI 解释，直接用 Python 兜底文案（测试/离线可用）

    返回 ``dict``（JSON 可序列化），关键字段就是任务要求的五个输出：
    ``skill_demands`` / ``covered_skills`` / ``missing_skills`` / ``coverage_rate`` /
    ``importance``，另有 ``summary``、``job_matches`` 与统一的 ``explanation``。

    关键约束：

        1. 匹配与覆盖率在 ``with`` 块之后的 **Python 阶段** 完成；
        2. **AI 调用发生在 SQLite 连接关闭之后**，模型只拿到
           :func:`build_insight_payload` 生成的只读事实表；
        3. 解释前后比对 :func:`results_fingerprint`，计算结果被改动就抛 ``RuntimeError``
           （红线：AI 不能修改计算结果）。

    异常：
        :class:`~backend.errors.ValidationError`  参数非法（HTTP 400）
        :class:`~backend.errors.NotFoundError`    专业不存在（HTTP 404）
    """
    question = check_question(question)
    normalized = validate_gap_params(params)

    # 取数阶段：SQLite 只做筛选，不做任何匹配/覆盖率计算。
    with connection(db_path or default_db_path()) as conn:
        grid = queries.industry_skill_grid(conn, normalized)

    # 计算阶段：连接已关闭，全部匹配与覆盖率由 Python 完成，AI 完全不参与。
    coverage = build_major_coverage(normalized["major"], grid["course_skills"])
    report = calculate_skill_gap(
        grid["jobs"],
        coverage,
        min_importance=normalized["min_importance"],
        top=normalized["top"],
    )
    report["question"] = question
    report["query"] = normalized
    report["scope"] = _scope_label(grid, normalized)
    report["data_summary"] = grid["summary"]

    fingerprint = results_fingerprint(report)
    report["results_fingerprint"] = fingerprint

    # 解释阶段：模型只能看到事实表，且只允许往 report 里加 explanation。
    report["explanation"] = (
        explain_report(question, report, explainer=explainer)
        if explain
        else local_explanation(report)
    )

    if results_fingerprint(report) != fingerprint:
        raise RuntimeError(
            "解释阶段修改了计算结果：AI 只能解释结论，不得改动技能需求/覆盖率/重要程度。"
        )
    return report
