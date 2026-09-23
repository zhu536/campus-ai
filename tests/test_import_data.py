# -*- coding: utf-8 -*-
"""scripts/import_data.py 的基础测试。

覆盖 8 条硬要求：
    1. 固定 9 张表
    2. SQLite 初始化可重复执行
    3. 重建数据库不留脏数据
    4. 参数化 SQL
    5. 必要索引
    6. 打印「表名 + 导入数量」
    7. 错误信息清晰（缺文件、缺列、类型错、外键悬空）
    8. 不接 AI、不修改 frontend（实现层面只依赖标准库）

测试全部使用 tmp_path 里的临时 CSV / 临时库，不修改 data/raw 与 data/campus.db。
"""

import contextlib
import io
import os
import shutil
import sqlite3
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts import import_data as importer  # noqa: E402

RAW_DIR = os.path.join(PROJECT_ROOT, "data", "raw")
EXPECTED_COUNTS = {
    "classrooms": 50,
    "courses": 100,
    "schedules": 593,
    "canteens": 5,
    "meal_records": 3000,
    "jobs": 100,
    "skills": 30,
    "course_skills": 300,
    "job_skills": 400,
}


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def connect(db_path):
    """测试用连接：开启外键并保证关闭。

    注意：`with sqlite3.connect(...)` 只提交事务、不关闭连接；Windows 下未关闭的
    连接会锁住数据库文件（这也正是被测导入器要规避的坑）。
    """
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
    finally:
        connection.close()


def rows_of(db_path, table):
    with connect(db_path) as connection:
        return connection.execute("SELECT * FROM {}".format(table)).fetchall()


def table_names(db_path):
    """用户表名（排除 SQLite 内部的 sqlite_sequence）。"""
    with connect(db_path) as connection:
        return {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }


def index_names(db_path):
    with connect(db_path) as connection:
        return {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }


def run_import(raw_dir=RAW_DIR, db_path=None, extra_args=()):
    """以命令行方式运行导入器，返回 (退出码, 标准输出, 标准错误)。"""
    args = ["--raw-dir", str(raw_dir), "--db", str(db_path or (raw_dir + ".db"))]
    args.extend(extra_args)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = importer.main(args)
    return code, out.getvalue(), err.getvalue()


def build_test_db(db_path, raw_dir=RAW_DIR):
    ddl = importer.load_schema_ddl()
    importer.build_database(str(db_path), ddl[0], ddl[1])
    paths = importer.check_csv_files(str(raw_dir))
    return importer.import_database(str(db_path), paths)


@pytest.fixture()
def raw_copy(tmp_path):
    """把真实 CSV 复制到临时目录，便于在测试里篡改单个文件。"""
    target = tmp_path / "raw"
    shutil.copytree(RAW_DIR, target)
    return target


# ---------------------------------------------------------------------------
# 1. 表结构
# ---------------------------------------------------------------------------

def test_schema_creates_exactly_nine_tables(tmp_path):
    db_path = tmp_path / "campus.db"
    table_ddl, index_ddl = importer.load_schema_ddl()

    assert len(table_ddl) == 9
    assert len(importer.TABLES) == 9

    importer.build_database(str(db_path), table_ddl, index_ddl)
    assert table_names(str(db_path)) == set(importer.TABLE_NAMES)


def test_schema_document_and_importer_agree():
    table_ddl, index_ddl = importer.load_schema_ddl()
    assert len(table_ddl) == len(importer.TABLE_NAMES)
    assert index_ddl, "docs/DATABASE.md 必须包含索引定义"
    for name in importer.TABLE_NAMES:
        table = importer.TABLE_BY_NAME[name]
        assert table["keys"], "{} 缺少主键定义".format(name)
        assert table["columns"], "{} 缺少列定义".format(name)


# ---------------------------------------------------------------------------
# 2. 真实数据导入：9 张表都有数据
# ---------------------------------------------------------------------------

def test_import_real_csv_populates_all_nine_tables(tmp_path):
    db_path = tmp_path / "campus.db"
    code, out, err = run_import(RAW_DIR, db_path)

    assert code == 0, err
    assert err.strip() == ""

    counts = {}
    with connect(str(db_path)) as connection:
        for name in importer.TABLE_NAMES:
            counts[name] = connection.execute(
                "SELECT COUNT(*) FROM {}".format(name)
            ).fetchone()[0]

    assert set(counts) == set(EXPECTED_COUNTS)
    assert counts == EXPECTED_COUNTS
    assert all(count > 0 for count in counts.values())
    # 打印「表名 + 数量」：每张表都要出现在输出里
    for name, count in counts.items():
        assert name in out
        assert str(count) in out


def test_imported_database_is_integrity_clean(tmp_path):
    db_path = tmp_path / "campus.db"
    assert run_import(RAW_DIR, db_path)[0] == 0

    with connect(str(db_path)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert list(connection.execute("PRAGMA foreign_key_check")) == []


def test_real_csv_has_no_dangling_foreign_keys(tmp_path):
    db_path = tmp_path / "campus.db"
    assert run_import(RAW_DIR, db_path)[0] == 0

    # 用 JOIN 独立复核关键外键关系（不完全依赖 PRAGMA）
    checks = (
        "SELECT COUNT(*) FROM schedules s LEFT JOIN courses c ON c.course_id = s.course_id WHERE c.course_id IS NULL",
        "SELECT COUNT(*) FROM schedules s LEFT JOIN classrooms r ON r.room_id = s.room_id WHERE r.room_id IS NULL",
        "SELECT COUNT(*) FROM meal_records m LEFT JOIN canteens c ON c.canteen_id = m.canteen_id WHERE c.canteen_id IS NULL",
        "SELECT COUNT(*) FROM course_skills cs LEFT JOIN skills s ON s.skill_id = cs.skill_id WHERE s.skill_id IS NULL",
        "SELECT COUNT(*) FROM job_skills js LEFT JOIN jobs j ON j.job_id = js.job_id WHERE j.job_id IS NULL",
    )
    with connect(str(db_path)) as connection:
        for sql in checks:
            assert connection.execute(sql).fetchone()[0] == 0


def test_created_at_default_is_filled_by_database(tmp_path):
    db_path = tmp_path / "campus.db"
    assert run_import(RAW_DIR, db_path)[0] == 0
    with connect(str(db_path)) as connection:
        nulls = connection.execute(
            "SELECT COUNT(*) FROM meal_records WHERE created_at IS NULL OR created_at = ''"
        ).fetchone()[0]
    assert nulls == 0


# ---------------------------------------------------------------------------
# 3. 可重复执行 / 重建不留脏数据
# ---------------------------------------------------------------------------

def test_import_is_idempotent(tmp_path):
    db_path = tmp_path / "campus.db"
    first_code, _, first_err = run_import(RAW_DIR, db_path)
    assert first_code == 0
    first = {name: rows_of(str(db_path), name) for name in importer.TABLE_NAMES}

    second_code, _, second_err = run_import(RAW_DIR, db_path)
    second = {name: rows_of(str(db_path), name) for name in importer.TABLE_NAMES}
    assert second_code == 0, "二次导入失败：{}".format(second_err)

    for name in importer.TABLE_NAMES:
        assert len(first[name]) == len(second[name]), "{} 表二次导入行数变化".format(name)
        # 行内容完全一致（meal_records.created_at 由数据库 CURRENT_TIMESTAMP 生成）
        if name != "meal_records":
            assert first[name] == second[name]


def test_import_three_times_keeps_counts_stable(tmp_path):
    """连续多次执行不应累加数据（AGENTS.md：初始化可重复执行）。"""
    db_path = tmp_path / "campus.db"
    for attempt in range(3):
        code, _, err = run_import(RAW_DIR, db_path)
        assert code == 0, "第 {} 次导入失败：{}".format(attempt + 1, err)
        for name, expected in EXPECTED_COUNTS.items():
            assert len(rows_of(str(db_path), name)) == expected


def test_rebuild_removes_dirty_rows(tmp_path):
    db_path = tmp_path / "campus.db"
    assert run_import(RAW_DIR, db_path)[0] == 0

    # 人为制造脏数据：多插一行 + 篡改一行
    with connect(str(db_path)) as connection:
        connection.execute(
            "INSERT INTO skills (skill_id, skill_name, skill_category) VALUES (9999, '脏数据技能', '其他')"
        )
        connection.execute("UPDATE canteens SET capacity = 1 WHERE canteen_id = 1")
        connection.commit()
    assert len(rows_of(str(db_path), "skills")) == EXPECTED_COUNTS["skills"] + 1

    assert run_import(RAW_DIR, db_path)[0] == 0

    assert len(rows_of(str(db_path), "skills")) == EXPECTED_COUNTS["skills"]
    with connect(str(db_path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM skills WHERE skill_id = 9999"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT capacity FROM canteens WHERE canteen_id = 1"
        ).fetchone()[0] > 1


def test_rebuild_drops_stale_tables_and_rows(tmp_path):
    """旧库里多出来的表/字段残留不应影响重建结果。"""
    db_path = tmp_path / "campus.db"
    with connect(str(db_path)) as connection:
        connection.execute("CREATE TABLE legacy_table (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO legacy_table (id) VALUES (1)")
        connection.commit()

    assert run_import(RAW_DIR, db_path)[0] == 0
    assert table_names(str(db_path)) == set(importer.TABLE_NAMES)
    assert "legacy_table" not in table_names(str(db_path))


def test_temp_files_are_cleaned_up(tmp_path):
    db_path = tmp_path / "campus.db"
    assert run_import(RAW_DIR, db_path)[0] == 0
    leftovers = [
        name for name in os.listdir(str(tmp_path))
        if name.startswith("campus-build-") or name.endswith(".old")
    ]
    assert leftovers == []


# ---------------------------------------------------------------------------
# 4. 参数化 SQL
# ---------------------------------------------------------------------------

def test_insert_sql_is_parameterized():
    column_names = [column[0] for column in importer.TABLE_BY_NAME["classrooms"]["columns"]]
    sql = importer.INSERT_SQL_TEMPLATE.format(
        table="classrooms",
        columns=", ".join(column_names),
        placeholders=", ".join("?" for _ in column_names),
    )
    assert sql.count("?") == len(column_names)
    assert "INSERT INTO classrooms" in sql


def test_import_source_never_concatenates_values_into_sql():
    """值只能通过 executemany/execute 的参数绑定进入数据库。"""
    source = importer.__file__
    with open(source, "r", encoding="utf-8") as handle:
        text = handle.read()
    assert "executemany(sql, rows)" in text
    # 不允许出现 f-string / format 里内联 CSV 值的写法
    assert "VALUES ('" not in text
    assert 'VALUES ("' not in text


def test_string_values_are_stored_verbatim(tmp_path):
    """带单引号、分号的中文文本应原样入库，证明是参数绑定而非拼接。"""
    raw_dir = tmp_path / "raw"
    shutil.copytree(RAW_DIR, raw_dir)
    csv_path = raw_dir / "jobs.csv"
    with open(csv_path, "r", encoding="utf-8", newline="") as handle:
        text = handle.read()
    text = text.replace(
        "大数据开发工程师",
        "'; DROP TABLE jobs; --工程师",
        1,
    )
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)

    db_path = tmp_path / "campus.db"
    assert run_import(raw_dir, db_path)[0] == 0

    with connect(str(db_path)) as connection:
        hit = connection.execute(
            "SELECT job_title FROM jobs WHERE job_title LIKE '%DROP TABLE jobs%'"
        ).fetchall()
        total = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert hit == [("'; DROP TABLE jobs; --工程师",)]
    assert total == EXPECTED_COUNTS["jobs"]


# ---------------------------------------------------------------------------
# 5. 索引
# ---------------------------------------------------------------------------

def test_required_indexes_exist(tmp_path):
    db_path = tmp_path / "campus.db"
    assert run_import(RAW_DIR, db_path)[0] == 0

    expected = {
        "idx_schedules_time",
        "idx_schedules_room",
        "idx_schedules_course",
        "idx_classrooms_cap",
        "idx_meal_time",
        "idx_meal_slot",
        "idx_course_skills",
        "idx_job_skills",
        "idx_jobs_major",
    }
    assert expected <= index_names(str(db_path))


def test_indexes_are_actually_used_by_query_plan(tmp_path):
    db_path = tmp_path / "campus.db"
    assert run_import(RAW_DIR, db_path)[0] == 0

    with connect(str(db_path)) as connection:
        plan = " ".join(
            str(row) for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT room_id FROM schedules "
                "WHERE semester = ? AND weekday = ? AND period_start <= ? AND period_end >= ?",
                ("2025-2026-1", 3, 4, 6),
            )
        )
        meal_plan = " ".join(
            str(row) for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT SUM(people_count) FROM meal_records "
                "WHERE canteen_id = ? AND record_date BETWEEN ? AND ?",
                (1, "2025-09-01", "2025-09-07"),
            )
        )
    assert "idx_schedules_time" in plan
    assert "idx_meal_time" in meal_plan


def test_no_rebuild_backfills_missing_index(tmp_path):
    db_path = tmp_path / "campus.db"
    assert run_import(RAW_DIR, db_path)[0] == 0

    with connect(str(db_path)) as connection:
        connection.execute("DROP INDEX idx_jobs_major")
        connection.commit()
    assert "idx_jobs_major" not in index_names(str(db_path))

    code, out, _ = run_import(RAW_DIR, db_path, extra_args=["--no-rebuild"])
    assert code == 0
    assert "idx_jobs_major" in index_names(str(db_path))
    assert "idx_jobs_major" in out


# ---------------------------------------------------------------------------
# 6. 校验模式
# ---------------------------------------------------------------------------

def test_verify_only_passes_after_import(tmp_path, capsys):
    db_path = tmp_path / "campus.db"
    assert run_import(RAW_DIR, db_path)[0] == 0
    capsys.readouterr()

    code = importer.main(["--verify-only", "--db", str(db_path)])
    out = capsys.readouterr().out
    assert code == 0
    for name in importer.TABLE_NAMES:
        assert name in out


def test_verify_only_fails_on_empty_database(tmp_path, capsys):
    db_path = tmp_path / "campus.db"
    table_ddl, index_ddl = importer.load_schema_ddl()
    importer.build_database(str(db_path), table_ddl, index_ddl)

    code = importer.main(["--verify-only", "--db", str(db_path)])
    err = capsys.readouterr().err
    assert code == 1
    assert "没有数据" in err


# ---------------------------------------------------------------------------
# 7. 错误信息清晰
# ---------------------------------------------------------------------------

def test_missing_csv_reports_file_name(tmp_path):
    raw_dir = tmp_path / "raw"
    shutil.copytree(RAW_DIR, raw_dir)
    os.remove(raw_dir / "skills.csv")

    db_path = tmp_path / "campus.db"
    code, _, err = run_import(raw_dir, db_path)
    assert code == 1
    assert "skills.csv" in err
    assert not os.path.exists(db_path), "失败时不应留下数据库"


def test_missing_directory_reports_path(tmp_path):
    code, _, err = run_import(tmp_path / "not-exist", tmp_path / "campus.db")
    assert code == 1
    assert "不存在" in err


def test_missing_column_reports_header_problem(raw_copy):
    csv_path = raw_copy / "classrooms.csv"
    with open(csv_path, "r", encoding="utf-8", newline="") as handle:
        lines = handle.read().splitlines()
    lines[0] = lines[0].replace(",capacity", "")
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(lines) + "\n")

    code, _, err = run_import(raw_copy, raw_copy.parent / "campus.db")
    assert code == 1
    assert "表头" in err and "capacity" in err


def test_bad_integer_reports_row_number(raw_copy):
    csv_path = raw_copy / "classrooms.csv"
    with open(csv_path, "r", encoding="utf-8", newline="") as handle:
        lines = handle.read().splitlines()
    cells = lines[1].split(",")
    cells[4] = "很多人"
    lines[1] = ",".join(cells)
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(lines) + "\n")

    code, _, err = run_import(raw_copy, raw_copy.parent / "campus.db")
    assert code == 1
    assert "第 2 行" in err and "capacity" in err and "整数" in err


def test_empty_required_value_reports_row_number(raw_copy):
    csv_path = raw_copy / "canteens.csv"
    with open(csv_path, "r", encoding="utf-8", newline="") as handle:
        lines = handle.read().splitlines()
    cells = lines[1].split(",")
    cells[1] = ""
    lines[1] = ",".join(cells)
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(lines) + "\n")

    code, _, err = run_import(raw_copy, raw_copy.parent / "campus.db")
    assert code == 1
    assert "第 2 行" in err and "canteen_name" in err


def test_duplicate_primary_key_reports_both_rows(raw_copy):
    csv_path = raw_copy / "skills.csv"
    with open(csv_path, "r", encoding="utf-8", newline="") as handle:
        lines = handle.read().splitlines()
    cells = lines[2].split(",")
    cells[0] = lines[1].split(",")[0]
    lines[2] = ",".join(cells)
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(lines) + "\n")

    code, _, err = run_import(raw_copy, raw_copy.parent / "campus.db")
    assert code == 1
    assert "主键重复" in err and "第 3 行" in err and "第 2 行" in err


def test_unknown_enum_value_reports_allowed_values(raw_copy):
    csv_path = raw_copy / "skills.csv"
    with open(csv_path, "r", encoding="utf-8", newline="") as handle:
        lines = handle.read().splitlines()
    cells = lines[1].split(",")
    cells[2] = "外星科技"
    lines[1] = ",".join(cells)
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(lines) + "\n")

    code, _, err = run_import(raw_copy, raw_copy.parent / "campus.db")
    assert code == 1
    assert "skill_category" in err and "编程语言" in err


def test_dangling_foreign_key_reports_reference(raw_copy):
    csv_path = raw_copy / "meal_records.csv"
    with open(csv_path, "r", encoding="utf-8", newline="") as handle:
        lines = handle.read().splitlines()
    cells = lines[1].split(",")
    cells[1] = "999"
    lines[1] = ",".join(cells)
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(lines) + "\n")

    code, _, err = run_import(raw_copy, raw_copy.parent / "campus.db")
    assert code == 1
    assert "外键悬空" in err and "canteens" in err and "第 2 行" in err


def test_failure_keeps_previous_database_intact(tmp_path, raw_copy):
    """导入失败时不能破坏已有数据库（事务 + 临时库替换）。"""
    db_path = tmp_path / "campus.db"
    assert run_import(RAW_DIR, db_path)[0] == 0
    before = {name: rows_of(str(db_path), name) for name in importer.TABLE_NAMES}

    # 破坏 CSV 后重跑：必须失败且旧库保持原样
    csv_path = raw_copy / "courses.csv"
    with open(csv_path, "r", encoding="utf-8", newline="") as handle:
        lines = handle.read().splitlines()
    lines[1] = lines[1].replace(",", ",", 0) + ',"多出来的列"'
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(lines) + "\n")

    code, _, err = run_import(raw_copy, db_path)
    assert code == 1
    assert "错误" in err

    after = {name: rows_of(str(db_path), name) for name in importer.TABLE_NAMES}
    assert after == before


def test_error_message_goes_to_stderr_with_marker(tmp_path, capsys):
    code = importer.main(
        ["--raw-dir", str(tmp_path / "nope"), "--db", str(tmp_path / "campus.db")]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "[错误]" in captured.err
    assert "未被修改" in captured.err


# ---------------------------------------------------------------------------
# 8. 边界
# ---------------------------------------------------------------------------

def test_empty_csv_is_rejected(raw_copy):
    csv_path = raw_copy / "canteens.csv"
    csv_path.write_text("", encoding="utf-8")

    code, _, err = run_import(raw_copy, raw_copy.parent / "campus.db")
    assert code == 1
    assert "空文件" in err


def test_quiet_mode_prints_only_names_and_counts(tmp_path, capsys):
    db_path = tmp_path / "campus.db"
    code = importer.main(
        ["--raw-dir", RAW_DIR, "--db", str(db_path), "--quiet"]
    )
    out = capsys.readouterr().out
    assert code == 0
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == len(importer.TABLE_NAMES)
    for line in lines:
        name, count = line.split()
        assert name in importer.TABLE_NAMES
        assert int(count) > 0


def test_importer_does_not_touch_frontend_or_raw_data():
    """导入器只读 CSV、只写数据库文件；不 import 任何前端 / AI / 网络库。"""
    with open(importer.__file__, "r", encoding="utf-8") as handle:
        lines = handle.readlines()

    imported = set()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("import "):
            imported.add(stripped.split()[1].split(".")[0])
        elif stripped.startswith("from "):
            imported.add(stripped.split()[1].split(".")[0])

    banned = {"flask", "openai", "requests", "urllib", "httpx", "aiohttp", "socket", "subprocess"}
    assert imported == {
        "argparse", "contextlib", "csv", "os", "re", "sqlite3", "sys", "tempfile", "time",
    }
    assert not (banned & imported)
