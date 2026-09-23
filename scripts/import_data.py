#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CampusAI CSV → SQLite 导入器（scripts/import_data.py）。

用法（在项目根目录执行）：
    python scripts/import_data.py                # 重建 data/campus.db 并导入 data/raw/*.csv
    python scripts/import_data.py --verify-only   # 只统计各表行数，不写库
    python scripts/import_data.py --no-rebuild    # 不建表不导入，只补齐缺失的索引
    python scripts/import_data.py --quiet         # 只打印每张表的行数

约定（与 docs/DATABASE.md、AGENTS.md 保持一致）：
    1. 固定 9 张表；表结构唯一来源是 docs/DATABASE.md「完整 CREATE TABLE SQL」，
       本脚本只读取它并在每个 CREATE TABLE / CREATE INDEX 后追加 IF NOT EXISTS，
       因此不复制第二份 DDL，也不会与文档漂移。
    2. 初始化可重复执行：
       - 默认走「构建临时库 + 事务 + 原子替换」，先 DROP 再 CREATE，
         任何一步失败都会回滚，data/campus.db 要么是旧的完整库，要么是新的完整库，
         不会留下半张表或残留脏数据。
       - 二次运行结果与首次完全一致（不追加、不重复计数）。
    3. 全部 INSERT 使用参数化 SQL（`?` 占位符），不拼接任何字面量。
    4. 每次执行打印「表名 + 导入数量」，并汇总外键完整性检查结果。
    5. CSV 缺失 / 表头不匹配 / 类型非法 / 枚举越界 / 外键悬空 / 唯一键重复，
       都会给出带文件名与行号的清晰错误信息，退出码为 1。
    6. 不接 AI、不修改 frontend、不联网。

产物：
    data/campus.db   9 张表 + 索引，数据来自 data/raw/ 的 9 个 CSV
"""

import argparse
import contextlib
import csv
import os
import re
import sqlite3
import sys
import tempfile
import time

# ---------------------------------------------------------------------------
# 一、列模型
# ---------------------------------------------------------------------------
# 每个列是一个四元组：
#   (列名, 类型, 是否允许空值, 中文说明)
# 类型只用于 CSV 解析与取值校验，真实约束以 docs/DATABASE.md 的 DDL 为准。

KIND_INT = "int"           # CSV 整数 → SQLite INTEGER
KIND_REAL = "real"         # CSV 小数 → SQLite REAL
KIND_TEXT = "text"         # 任意文本，空串按 NULL 处理（交给 DEFAULT 生效）
KIND_TEXT_REQUIRED = "text!"  # 任意文本，空串报错（非空列）

# 顺序即入库顺序，保证父表先于子表。
TABLES = (
    {
        "name": "classrooms",
        "file": "classrooms.csv",
        "description": "教室",
        "columns": (
            ("room_id", KIND_INT, False, "教室主键"),
            ("room_name", KIND_TEXT_REQUIRED, False, "教室名称/编号"),
            ("building", KIND_TEXT_REQUIRED, False, "所在楼栋"),
            ("floor", KIND_INT, True, "楼层"),
            ("capacity", KIND_INT, False, "容纳人数"),
            ("has_projector", KIND_INT, False, "是否有投影"),
            ("has_computer", KIND_INT, False, "是否有电脑"),
            ("room_type", KIND_TEXT_REQUIRED, False, "教室类型"),
            ("status", KIND_TEXT_REQUIRED, False, "教室状态"),
        ),
        "required": ("room_id", "room_name", "building", "capacity"),
        "keys": ("room_id",),
        "unique": (("room_name",),),
        "flags": ("has_projector", "has_computer"),
        "enums": {
            "room_type": ("普通", "多媒体", "机房", "实验室"),
            "status": ("available", "maintenance"),
        },
    },
    {
        "name": "courses",
        "file": "courses.csv",
        "description": "课程",
        "columns": (
            ("course_id", KIND_INT, False, "课程主键"),
            ("course_code", KIND_TEXT_REQUIRED, False, "课程代码"),
            ("course_name", KIND_TEXT_REQUIRED, False, "课程名称"),
            ("department", KIND_TEXT, True, "开课院系/专业"),
            ("credit", KIND_REAL, True, "学分"),
            ("course_type", KIND_TEXT_REQUIRED, False, "课程性质"),
        ),
        "required": ("course_id", "course_code", "course_name"),
        "keys": ("course_id",),
        "unique": (("course_code",), ("course_name",)),
        "enums": {"course_type": ("必修", "选修", "实践")},
    },
    {
        "name": "schedules",
        "file": "schedules.csv",
        "description": "排课/教室占用",
        "columns": (
            ("schedule_id", KIND_INT, False, "排课主键"),
            ("course_id", KIND_INT, False, "课程 → courses.course_id"),
            ("room_id", KIND_INT, False, "教室 → classrooms.room_id"),
            ("semester", KIND_TEXT_REQUIRED, False, "学期"),
            ("weekday", KIND_INT, False, "星期几 1..7"),
            ("period_start", KIND_INT, False, "起始节次"),
            ("period_end", KIND_INT, False, "结束节次"),
            ("start_time", KIND_TEXT_REQUIRED, False, "开始时间 HH:MM"),
            ("end_time", KIND_TEXT_REQUIRED, False, "结束时间 HH:MM"),
            ("week_range", KIND_TEXT, True, "上课周次"),
            ("teacher", KIND_TEXT, True, "任课教师"),
        ),
        "required": ("schedule_id", "course_id", "room_id", "semester", "weekday",
                     "period_start", "period_end", "start_time", "end_time"),
        "keys": ("schedule_id",),
        "foreign_keys": (
            ("course_id", "courses", "course_id"),
            ("room_id", "classrooms", "room_id"),
        ),
        "ranges": (
            ("weekday", 1, 7, "星期必须是 1..7"),
            ("period_start", 1, 12, "起始节次必须在 1..12"),
            ("period_end", 1, 12, "结束节次必须在 1..12"),
        ),
    },
    {
        "name": "canteens",
        "file": "canteens.csv",
        "description": "食堂",
        "columns": (
            ("canteen_id", KIND_INT, False, "食堂主键"),
            ("canteen_name", KIND_TEXT_REQUIRED, False, "食堂名称"),
            ("location", KIND_TEXT, True, "位置描述"),
            ("capacity", KIND_INT, False, "同时在店容量"),
            ("window_count", KIND_INT, True, "窗口数量"),
            ("open_time", KIND_TEXT, True, "开始营业时间 HH:MM"),
            ("close_time", KIND_TEXT, True, "结束营业时间 HH:MM"),
            ("status", KIND_TEXT_REQUIRED, False, "状态"),
        ),
        "required": ("canteen_id", "canteen_name", "capacity"),
        "keys": ("canteen_id",),
        "unique": (("canteen_name",),),
        "enums": {"status": ("open", "closed")},
    },
    {
        "name": "meal_records",
        "file": "meal_records.csv",
        "description": "食堂人流记录",
        "columns": (
            ("record_id", KIND_INT, False, "记录主键"),
            ("canteen_id", KIND_INT, False, "食堂 → canteens.canteen_id"),
            ("record_date", KIND_TEXT_REQUIRED, False, "日期 YYYY-MM-DD"),
            ("record_hour", KIND_INT, False, "小时 0..23"),
            ("time_slot", KIND_TEXT_REQUIRED, False, "时段"),
            ("people_count", KIND_INT, False, "就餐人数"),
            ("wait_minutes", KIND_REAL, True, "平均等待时间（分钟）"),
            ("capacity", KIND_INT, True, "采集时容量快照"),
            ("source", KIND_TEXT_REQUIRED, False, "数据来源"),
        ),
        "required": ("record_id", "canteen_id", "record_date", "record_hour",
                     "time_slot", "people_count"),
        "keys": ("record_id",),
        "foreign_keys": (("canteen_id", "canteens", "canteen_id"),),
        "unique": (("canteen_id", "record_date", "record_hour"),),
        "ranges": (("record_hour", 0, 23, "小时必须在 0..23"),),
        "enums": {
            "time_slot": ("breakfast", "lunch", "dinner", "other"),
            "source": ("simulated", "real"),
        },
    },
    {
        "name": "jobs",
        "file": "jobs.csv",
        "description": "企业岗位",
        "columns": (
            ("job_id", KIND_INT, False, "岗位主键"),
            ("job_title", KIND_TEXT_REQUIRED, False, "岗位名称"),
            ("company", KIND_TEXT_REQUIRED, False, "企业名称"),
            ("industry", KIND_TEXT, True, "所属行业"),
            ("city", KIND_TEXT, True, "工作城市"),
            ("education", KIND_TEXT, True, "学历要求"),
            ("experience", KIND_TEXT, True, "经验要求"),
            ("salary_min", KIND_INT, True, "月薪下限"),
            ("salary_max", KIND_INT, True, "月薪上限"),
            ("major", KIND_TEXT, True, "目标专业"),
            ("job_desc", KIND_TEXT, True, "岗位描述"),
            ("publish_date", KIND_TEXT, True, "发布日期 YYYY-MM-DD"),
        ),
        "required": ("job_id", "job_title", "company"),
        "keys": ("job_id",),
        "enums": {
            "education": ("大专", "本科", "硕士", "不限"),
            "experience": ("应届", "1-3年", "3-5年", "不限"),
        },
    },
    {
        "name": "skills",
        "file": "skills.csv",
        "description": "技能",
        "columns": (
            ("skill_id", KIND_INT, False, "技能主键"),
            ("skill_name", KIND_TEXT_REQUIRED, False, "技能名称"),
            ("skill_category", KIND_TEXT_REQUIRED, False, "技能分类"),
            ("description", KIND_TEXT, True, "技能说明"),
        ),
        "required": ("skill_id", "skill_name"),
        "keys": ("skill_id",),
        "unique": (("skill_name",),),
        "enums": {
            "skill_category": ("编程语言", "数据库", "框架", "工具", "软技能", "其他"),
        },
    },
    {
        "name": "course_skills",
        "file": "course_skills.csv",
        "description": "课程—技能",
        "columns": (
            ("id", KIND_INT, False, "主键"),
            ("course_id", KIND_INT, False, "课程 → courses.course_id"),
            ("skill_id", KIND_INT, False, "技能 → skills.skill_id"),
            ("level", KIND_TEXT, True, "覆盖程度"),
        ),
        "required": ("id", "course_id", "skill_id"),
        "keys": ("id",),
        "foreign_keys": (
            ("course_id", "courses", "course_id"),
            ("skill_id", "skills", "skill_id"),
        ),
        "unique": (("course_id", "skill_id"),),
        "enums": {"level": ("了解", "掌握", "熟练")},
    },
    {
        "name": "job_skills",
        "file": "job_skills.csv",
        "description": "岗位—技能",
        "columns": (
            ("id", KIND_INT, False, "主键"),
            ("job_id", KIND_INT, False, "岗位 → jobs.job_id"),
            ("skill_id", KIND_INT, False, "技能 → skills.skill_id"),
            ("importance", KIND_INT, True, "重要度 1..5"),
            ("is_required", KIND_INT, True, "是否硬性要求"),
        ),
        "required": ("id", "job_id", "skill_id"),
        "keys": ("id",),
        "foreign_keys": (
            ("job_id", "jobs", "job_id"),
            ("skill_id", "skills", "skill_id"),
        ),
        "unique": (("job_id", "skill_id"),),
        "ranges": (("importance", 1, 5, "重要度必须在 1..5"),),
        "flags": ("is_required",),
    },
)

TABLE_NAMES = tuple(table["name"] for table in TABLES)
TABLE_BY_NAME = {table["name"]: table for table in TABLES}
TABLE_BY_FILE = {table["file"]: table for table in TABLES}

# ---------------------------------------------------------------------------
# 二、表结构（唯一来源：docs/DATABASE.md）
# ---------------------------------------------------------------------------

CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\n\);",
    re.IGNORECASE | re.DOTALL,
)
CREATE_INDEX_RE = re.compile(
    r"CREATE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+ON\s+(\w+)\s*\(([^)]*)\)",
    re.IGNORECASE,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(PROJECT_ROOT, "data", "raw")
DB_PATH = os.path.join(PROJECT_ROOT, "data", "campus.db")
SCHEMA_DOC = os.path.join(PROJECT_ROOT, "docs", "DATABASE.md")
SCHEMA_MARKER = "## 四、完整 CREATE TABLE SQL"

# 占位符数量由表结构决定，SQL 文本本身是常量，值全部通过参数绑定写入。
INSERT_SQL_TEMPLATE = "INSERT INTO {table} ({columns}) VALUES ({placeholders})"

INSERT_ORDER = {name: index for index, name in enumerate(TABLE_NAMES)}


class ImportError_(Exception):
    """导入失败（数据或环境问题），消息面向使用者。"""


def load_schema_ddl(doc_path=SCHEMA_DOC):
    """从 docs/DATABASE.md 读取建表/建索引 DDL，返回 (建表语句, 建索引语句)。"""
    if not os.path.exists(doc_path):
        raise ImportError_("找不到表结构文档：{}".format(doc_path))
    with open(doc_path, "r", encoding="utf-8") as handle:
        text = handle.read()

    marker = text.find(SCHEMA_MARKER)
    if marker < 0:
        raise ImportError_(
            "{} 中找不到章节「{}」，无法读取表结构。".format(doc_path, SCHEMA_MARKER)
        )
    section = text[marker:]

    # 追加 IF NOT EXISTS，使同一份 DDL 可以重复执行（文档本身不重复写）。
    tables = [
        "CREATE TABLE IF NOT EXISTS {}({});".format(name, body)
        for name, body in CREATE_TABLE_RE.findall(section)
    ]
    indexes = [
        "CREATE INDEX IF NOT EXISTS {} ON {} ({});".format(name, table, columns)
        for name, table, columns in CREATE_INDEX_RE.findall(section)
    ]

    doc_tables = CREATE_TABLE_RE.findall(section)
    found = tuple(name for name, _ in doc_tables)
    if set(found) != set(TABLE_NAMES):
        raise ImportError_(
            "表结构文档与导入器不一致：文档 {} 张表 {}，导入器 {} 张表 {}".format(
                len(found), sorted(found), len(TABLE_NAMES), sorted(TABLE_NAMES)
            )
        )
    if len(tables) != len(TABLE_NAMES):
        raise ImportError_(
            "解析 {} 得到 {} 条建表语句，期望 {} 条。".format(doc_path, len(tables), len(TABLE_NAMES))
        )
    return tuple(tables), tuple(indexes)


# ---------------------------------------------------------------------------
# 三、CSV 读取与取值校验
# ---------------------------------------------------------------------------

def check_csv_files(raw_dir):
    """检查 9 个 CSV 是否齐全，返回 {表名: 绝对路径}。"""
    if not os.path.isdir(raw_dir):
        raise ImportError_("原始数据目录不存在：{}".format(raw_dir))
    paths = {}
    missing = []
    for table in TABLES:
        path = os.path.join(raw_dir, table["file"])
        if os.path.isfile(path):
            paths[table["name"]] = path
        else:
            missing.append(table["file"])
    if missing:
        raise ImportError_(
            "{} 缺少 {} 个 CSV：{}".format(
                raw_dir, len(missing), "、".join(sorted(missing))
            )
        )
    return paths


def detect_encoding(path):
    """UTF-8（含 BOM）优先，兼容 GBK。UTF-16/二进制等文本编码无法解码时报错。"""
    with open(path, "rb") as handle:
        head = handle.read(4)
    if head.startswith(b"\xff\xfe") or head.startswith(b"\xfe\xff"):
        raise ImportError_(
            "{} 不是 UTF-8 文本（检测到 UTF-16 BOM），请另存为 UTF-8".format(path)
        )
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for _ in handle:
                pass
        return "utf-8"
    except UnicodeDecodeError as exc:
        offset = getattr(exc, "start", None)
        raise ImportError_(
            "{} 无法按 UTF-8 解码（偏移 {}）：{}；"
            "请将该文件另存为 UTF-8（可带 BOM）后重试".format(path, offset, exc.reason)
        )


def load_table_rows(table, path):
    """读取并校验一个 CSV，返回可以安全绑定到 SQL 的行列表。

    校验内容：必需列存在、必需列非空、类型可解析、范围合法、枚举取值合法，
    以及主键与唯一键不重复。所有问题都带上文件名、表名与行号。
    """
    name = table["name"]
    encoding = detect_encoding(path)
    size = os.path.getsize(path)
    if size == 0:
        raise ImportError_("{} 是空文件，无法导入 {} 表".format(path, name))

    with open(path, "r", encoding=encoding, newline="") as handle:
        reader = csv.DictReader(handle)
        header = [column.strip() if column else column for column in (reader.fieldnames or [])]
        if not header or all(column is None for column in header):
            raise ImportError_("{} 没有表头行，无法导入 {} 表".format(path, name))

        column_names = [column[0] for column in table["columns"]]
        missing = [column for column in column_names if column not in header]
        if missing:
            raise ImportError_(
                "{} 表头不匹配（{}）：缺少列 {}；表头为 {}".format(
                    table["file"], name, "、".join(missing), "、".join(str(c) for c in header)
                )
            )
        unknown = [column for column in header if column not in column_names]
        if unknown:
            raise ImportError_(
                "{} 表头出现未知列 {}：请检查是不是拿错了 CSV（期望列 {}）".format(
                    table["file"], "、".join(str(c) for c in unknown), "、".join(column_names)
                )
            )
        missing_required = [column for column in table["required"] if column not in header]
        if missing_required:
            raise ImportError_(
                "{} 缺少必需列 {}（{} 表的非空字段）".format(
                    table["file"], "、".join(missing_required), name
                )
            )

        rows = []
        seen_keys = {}
        seen_unique = {}
        for line_number, raw in enumerate(reader, start=2):
            if raw.get(None) is not None:
                raise ImportError_(
                    "{} 第 {} 行多出 {} 个字段（期望 {} 列）："
                    "请在字段内用双引号包住逗号".format(
                        table["file"], line_number, len(raw[None]), len(column_names)
                    )
                )
            row = []
            for column, kind, nullable, label in table["columns"]:
                row.append(parse_value(table, raw.get(column, ""), column, kind, nullable, line_number, label))
            row = tuple(row)
            check_keys(table, row, line_number, seen_keys, seen_unique)
            rows.append(row)
    return rows


def parse_value(table, leaf, column, kind, nullable, line_number, label):
    """把一个 CSV 字段转换成数据库可绑定的值。"""
    text = leaf.strip() if isinstance(leaf, str) else ""
    if text == "":
        if not nullable or column in table["required"]:
            raise ImportError_(
                "{} 第 {} 行「{}」（{}）为空，但该列不允许为空".format(
                    table["file"], line_number, column, label
                )
            )
        # 空串一律按 NULL 处理，让 DDL 里的 DEFAULT 生效。
        return None

    if kind == KIND_INT:
        try:
            return int(text)
        except ValueError:
            raise ImportError_(
                "{} 第 {} 行「{}」（{}）不是整数：{!r}".format(
                    table["file"], line_number, column, label, leaf
                )
            )
    if kind == KIND_REAL:
        try:
            return float(text)
        except ValueError:
            raise ImportError_(
                "{} 第 {} 行「{}」（{}）不是数字：{!r}".format(
                    table["file"], line_number, column, label, leaf
                )
            )
    return text


def check_keys(table, row, line_number, seen_keys, seen_unique):
    """检查主键、唯一键、取值范围、枚举与 0/1 标记。"""
    values = dict(zip((column[0] for column in table["columns"]), row))
    for key in table["keys"]:
        if values[key] in seen_keys:
            raise ImportError_(
                "{} 第 {} 行主键重复：{}={}（第 {} 行已出现）".format(
                    table["file"], line_number, key, values[key], seen_keys[values[key]]
                )
            )
        seen_keys[values[key]] = line_number

    for columns in table.get("unique", ()):
        token = tuple(values[column] for column in columns)
        if token in seen_unique:
            raise ImportError_(
                "{} 第 {} 行唯一键重复：{}={}（第 {} 行已出现）".format(
                    table["file"], line_number,
                    "+".join(columns), "、".join(str(value) for value in token),
                    seen_unique[token],
                )
            )
        seen_unique[token] = line_number

    for column, low, high, message in table.get("ranges", ()):
        value = values[column]
        if value is not None and not low <= value <= high:
            raise ImportError_(
                "{} 第 {} 行 {}：{}={}".format(
                    table["file"], line_number, message, column, value
                )
            )

    for column, allowed in table.get("enums", {}).items():
        value = values[column]
        if value is not None and value not in allowed:
            raise ImportError_(
                "{} 第 {} 行 {}={!r} 不在允许取值 {} 中".format(
                    table["file"], line_number, column, value, "/".join(allowed)
                )
            )

    for column in table.get("flags", ()):
        value = values[column]
        if value is not None and value not in (0, 1):
            raise ImportError_(
                "{} 第 {} 行 {}={} 必须是 0 或 1".format(
                    table["file"], line_number, column, value
                )
            )

    if table["name"] == "schedules" and values["period_end"] < values["period_start"]:
        raise ImportError_(
            "{} 第 {} 行 period_end({}) 小于 period_start({})".format(
                table["file"], line_number, values["period_end"], values["period_start"]
            )
        )

    if table["name"] == "jobs":
        low, high = values["salary_min"], values["salary_max"]
        if low is not None and high is not None and high < low:
            raise ImportError_(
                "{} 第 {} 行 salary_max({}) 小于 salary_min({})".format(
                    table["file"], line_number, high, low
                )
            )


def check_foreign_keys(table, rows):
    """校验本表外键取值都存在于已加载的父表行里，提前给出可定位的错误。"""
    column_names = [column[0] for column in table["columns"]]
    for column, parent_name, parent_column in table.get("foreign_keys", ()):
        parent_rows = _LOADED_ROWS.get(parent_name)
        if parent_rows is None:
            continue
        parent_index = [column[0] for column in TABLE_BY_NAME[parent_name]["columns"]].index(parent_column)
        valid = {row[parent_index] for row in parent_rows}
        column_index = column_names.index(column)
        for position, row in enumerate(rows, start=2):
            value = row[column_index]
            if value not in valid:
                raise ImportError_(
                    "{} 第 {} 行外键悬空：{}.{}={} 在 {} 表中不存在".format(
                        table["file"], position, table["name"], column, value, parent_name
                    )
                )


_LOADED_ROWS = {}


# ---------------------------------------------------------------------------
# 四、写库
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def connect(db_path):
    """打开连接并开启外键约束（SQLite 默认关闭），退出时一定关闭连接。

    注意：`with sqlite3.connect(...)` 只管理事务提交，不会关连接；
    Windows 下未关闭的连接会锁住数据库文件，导致替换/删除失败。
    """
    try:
        connection = sqlite3.connect(db_path, timeout=30.0)
    except sqlite3.Error as exc:
        raise ImportError_("无法打开数据库 {}：{}".format(db_path, exc))
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def build_database(db_path, table_ddl, index_ddl):
    """创建 9 张表与索引（可重复执行）。

    先按依赖逆序 DROP 再 CREATE：即便目标文件里已有旧表（例如上一次运行留下的），
    也会得到全新的空表，不会残留任何脏数据。
    """
    with connect(db_path) as connection:
        connection.execute("BEGIN")
        connection.execute("PRAGMA foreign_keys = OFF")  # DROP/CREATE 期间无需检查引用
        for name in reversed(TABLE_NAMES):
            connection.execute("DROP TABLE IF EXISTS {}".format(name))
        for ddl in table_ddl:
            connection.execute(ddl)
        connection.execute("PRAGMA foreign_keys = ON")
        for ddl in index_ddl:
            connection.execute(ddl)
        connection.commit()


def insert_rows(connection, table, rows):
    """用参数化 INSERT 写入一张表的所有行。"""
    column_names = [column[0] for column in table["columns"]]
    sql = INSERT_SQL_TEMPLATE.format(
        table=table["name"],
        columns=", ".join(column_names),
        placeholders=", ".join("?" for _ in column_names),
    )
    connection.executemany(sql, rows)


def check_integrity(connection):
    """检查外键完整性，返回问题列表（空列表代表通过）。"""
    problems = []
    for row in connection.execute("PRAGMA foreign_key_check"):
        # 返回 (表名, rowid, 父表名, 外键序号)
        table_name, row_id, parent, _ = row
        problems.append("{} 表 rowid={} 引用 {} 失败".format(table_name, row_id, parent))
    return problems


def import_database(db_path, paths):
    """在一个事务里导入 9 张表；任一步失败即回滚。"""
    global _LOADED_ROWS
    _LOADED_ROWS = {}

    loaded = []
    for table in TABLES:
        rows = load_table_rows(table, paths[table["name"]])
        check_foreign_keys(table, rows)
        _LOADED_ROWS[table["name"]] = rows
        loaded.append((table, rows))

    with connect(db_path) as connection:
        for table, rows in loaded:
            try:
                insert_rows(connection, table, rows)
            except sqlite3.Error as exc:
                raise ImportError_(
                    "写入 {} 表（{}）失败，已回滚：{}".format(
                        table["name"], table["file"], exc
                    )
                )
        problems = check_integrity(connection)
        if problems:
            raise ImportError_(
                "外键完整性检查未通过，已回滚：{}".format("；".join(problems[:5]))
            )

    # 顺带确认落库行数与 CSV 行数一致，避免出现「导入成功但数量不符」。
    with connect(db_path) as check:
        for table, rows in loaded:
            actual = check.execute(
                "SELECT COUNT(*) FROM {}".format(table["name"])
            ).fetchone()[0]
            if actual != len(rows):
                raise ImportError_(
                    "{} 表落库行数 {} 与 CSV 行数 {} 不一致".format(
                        table["name"], actual, len(rows)
                    )
                )
    return loaded


def count_rows(db_path):
    """返回 {表名: 行数}；只统计已存在的表。"""
    counts = {}
    with connect(db_path) as connection:
        existing = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        for name in TABLE_NAMES:
            if name in existing:
                counts[name] = connection.execute(
                    "SELECT COUNT(*) FROM {}".format(name)
                ).fetchone()[0]
    return counts


def ensure_indexes(db_path, index_ddl):
    """补齐缺失的索引，返回新建的索引名。"""
    created = []
    with connect(db_path) as connection:
        existing = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        connection.execute("BEGIN")
        for ddl in index_ddl:
            name = re.search(r"CREATE INDEX IF NOT EXISTS (\w+)", ddl).group(1)
            if name not in existing:
                connection.execute(ddl)
                created.append(name)
        connection.commit()
    return created


# ---------------------------------------------------------------------------
# 五、数据库替换（保证重建期间不留脏数据）
# ---------------------------------------------------------------------------

def replace_database(build_path, db_path):
    """用新库替换旧库，并清理 SQLite 的 -wal / -shm 残留。

    Windows 上旧文件可能被其它程序短暂占用（杀毒扫描、DB 浏览器等），
    这里先直接 os.replace，失败则短暂重试；始终不做「先改名旧库再就位新库」，
    因此不会留下半成品状态，失败时整体放弃并给出可执行的提示。
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    last_error = None
    for attempt in range(5):
        try:
            os.replace(build_path, db_path)
            break
        except OSError as exc:
            last_error = exc
            time.sleep(0.2 * (attempt + 1))
    else:
        raise ImportError_(
            "无法把新数据库替换为 {}：{}。"
            "请关闭正在占用该文件的程序（如 DB 浏览器或仍在运行的 Python 进程）后重试；"
            "本次导入没有写入 {}，需要重新执行一次。".format(db_path, last_error, db_path)
        )
    for suffix in ("-wal", "-shm"):
        leftover = db_path + suffix
        if os.path.exists(leftover):
            try:
                os.remove(leftover)
            except OSError:
                pass


def cleanup(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def cleanup_stale_builds(directory, keep=None):
    """清理上一次运行中断留下的 campus-build-*.db，避免目录里堆临时库。"""
    if not directory or not os.path.isdir(directory):
        return
    for entry in os.listdir(directory):
        if entry.startswith("campus-build-") and entry.endswith(".db"):
            path = os.path.join(directory, entry)
            if os.path.abspath(path) != os.path.abspath(keep or ""):
                cleanup(path)


# ---------------------------------------------------------------------------
# 六、命令行
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="把 data/raw/*.csv 导入 data/campus.db（固定 9 张表）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--raw-dir", default=RAW_DIR, help="CSV 目录，默认 data/raw")
    parser.add_argument("--db", default=DB_PATH, help="SQLite 文件，默认 data/campus.db")
    parser.add_argument(
        "--no-rebuild",
        action="store_true",
        help="不做任何写入，只校验数据并补齐缺失的索引",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="只统计并打印各表行数，不读取 CSV、不写库",
    )
    parser.add_argument("--quiet", action="store_true", help="只打印表名与数量")
    return parser


def print_report(rows, problems=(), title=None, quiet=False, integrity=None):
    """打印「表名 + 数量」，并按需打印外键完整性结论。

    表格始终写 stdout；`problems` 里的失败原因写 stderr（出错时便于管道区分）。
    """
    if title and not quiet:
        print(title)
    width = max(len("表名"), max(len(name) for name in TABLE_NAMES))
    lines = []
    if not quiet:
        lines.append("{}  {}".format("表名".ljust(width), "数量"))
        lines.append("{}  {}".format("-" * width, "----"))
    for name, count in rows:
        lines.append("{}  {}".format(name.ljust(width), count))
    if not quiet:
        lines.append("{}  {}".format("-" * width, "----"))
        lines.append("{}  {}".format("合计".ljust(width), sum(count for _, count in rows)))
    print("\n".join(lines))

    if not quiet and problems:
        print("\n检查未通过：", file=sys.stderr)
        for problem in problems:
            print("  - {}".format(problem), file=sys.stderr)
    if not quiet and integrity is True:
        print("\n外键完整性检查：通过（PRAGMA foreign_key_check 无异常）")
    elif not quiet and integrity is False:
        print("\n外键完整性检查：失败")


def print_error(message):
    print("\n[错误] {}".format(message), file=sys.stderr)
    print("导入已终止，data/campus.db 未被修改。", file=sys.stderr)


def run(args):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    db_path = os.path.abspath(args.db)
    raw_dir = os.path.abspath(args.raw_dir)

    try:
        table_ddl, index_ddl = load_schema_ddl()

        if args.verify_only:
            if not os.path.exists(db_path):
                raise ImportError_("数据库不存在：{}（先执行 python scripts/import_data.py）".format(db_path))
            counts = count_rows(db_path)
            empty = [name for name in TABLE_NAMES if not counts.get(name)]
            print_report(
                [(name, counts.get(name, 0)) for name in TABLE_NAMES],
                problems=["{} 表没有数据".format(name) for name in empty],
                title="数据校验：{}".format(db_path),
                quiet=args.quiet,
            )
            return 1 if empty else 0
        paths = check_csv_files(raw_dir)
        encoding_label = detect_encoding(paths[TABLE_NAMES[0]])

        if args.no_rebuild:
            if not os.path.exists(db_path):
                raise ImportError_(
                    "--no-rebuild 需要已存在的数据库，但 {} 不存在；"
                    "请先执行 python scripts/import_data.py".format(db_path)
                )
            has_tables = count_rows(db_path)
            if len(has_tables) != len(TABLE_NAMES):
                missing = [name for name in TABLE_NAMES if name not in has_tables]
                raise ImportError_(
                    "--no-rebuild 不会建表，但数据库缺少表 {}".format("、".join(missing))
                )
            created = ensure_indexes(db_path, index_ddl)
            print("未重建数据库（--no-rebuild），已补齐索引：{}".format("、".join(created) if created else "无"))
            print_report(list(has_tables.items()), title="{} 当前数据".format(db_path))
            return 0

        # 默认路径：先在临时库里完整导入，成功后再原子替换正式库。
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        cleanup_stale_builds(os.path.dirname(db_path))
        handle, build_path = tempfile.mkstemp(
            prefix="campus-build-", suffix=".db", dir=os.path.dirname(db_path) or None
        )
        os.close(handle)
        cleanup(build_path)
        try:
            build_database(build_path, table_ddl, index_ddl)
            loaded = import_database(build_path, paths)
        except BaseException:
            cleanup(build_path)
            raise
        replace_database(build_path, db_path)

        print_report(
            [(table["name"], len(rows)) for table, rows in loaded],
            title="CSV → SQLite 导入完成：{}".format(db_path),
            quiet=args.quiet,
            integrity=True,
        )
        if not args.quiet:
            print("CSV 目录：{}（编码 {}）".format(raw_dir, encoding_label))
            print("重建方式：DROP + CREATE + 事务导入 + 原子替换（可重复执行）")
        return 0
    except ImportError_ as exc:
        print_error(str(exc))
        return 1


def main(argv=None):
    args = build_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
