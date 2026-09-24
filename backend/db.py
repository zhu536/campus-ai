# -*- coding: utf-8 -*-
"""SQLite 连接与参数化查询工具。

规则（AGENTS.md / docs/DATABASE.md）：
    1. 数据库固定为 ``data/campus.db``，只读查询，本模块不做任何写入。
    2. 所有 SQL 的值一律用 ``?`` 占位符绑定，禁止把参数拼进 SQL 字符串。
       本模块的 :func:`build_where` 只拼接**受控的列名与占位符**，
       用户传入的值永远走 params 列表。
    3. 连接在使用后一定关闭（Windows 下未关闭的连接会锁住数据库文件）。
"""

import os
import sqlite3
from contextlib import contextmanager

from .errors import DatabaseUnavailableError

# backend/db.py → backend/ → 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(PROJECT_ROOT, "data", "campus.db")


def default_db_path():
    """默认数据库文件路径；可用环境变量 ``CAMPUS_DB_PATH`` 覆盖（测试/演练用）。"""
    return os.environ.get("CAMPUS_DB_PATH") or DEFAULT_DB_PATH


def connect(db_path):
    """打开一个只读语义的连接（开启外键、Row 工厂）。

    调用方负责关闭；推荐用 :func:`connection` 上下文管理器。
    """
    if not db_path or not os.path.isfile(db_path):
        raise DatabaseUnavailableError(
            "数据库文件不存在：{}（请先执行 python scripts/import_data.py）".format(db_path)
        )
    try:
        connection = sqlite3.connect(db_path, timeout=30.0)
    except sqlite3.Error as exc:
        raise DatabaseUnavailableError("无法打开数据库 {}：{}".format(db_path, exc))
    connection.row_factory = sqlite3.Row
    # SQLite 默认关闭外键约束，显式打开以保证与 docs/DATABASE.md 一致。
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def connection(db_path):
    """上下文管理器：正常时提交（只读场景无副作用），异常时回滚，退出必关闭。"""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 参数化查询辅助
# ---------------------------------------------------------------------------

class WhereBuilder:
    """按顺序拼接 ``col = ?`` / ``col >= ?`` 等条件，值全部进 params。

    ``column`` 与 ``operator`` 由业务代码写死（常量），只有 ``value`` 来自外部，
    因此不存在把用户输入拼进 SQL 的路径。
    """

    __slots__ = ("_clauses", "params")

    def __init__(self):
        self._clauses = []
        self.params = []

    def add(self, column, operator, value):
        # 只跳过 None（未传）；0 与 False 是合法取值，必须照常绑定。
        if value is None:
            return self
        self._clauses.append("{} {} ?".format(column, operator))
        self.params.append(value)
        return self

    def add_raw(self, clause, params):
        """整条子句是代码常量时使用（例如 ``room_id NOT IN (?, ?)``），值仍走绑定。"""
        self._clauses.append(clause)
        self.params.extend(params)
        return self

    def sql(self):
        """返回 ``" WHERE a = ? AND b >= ?"`` 形式（无条件时返回空串）。"""
        if not self._clauses:
            return ""
        return " WHERE " + " AND ".join(self._clauses)


def query_all(conn, sql, params=()):
    """执行参数化查询并返回 ``list[sqlite3.Row]``。"""
    return conn.execute(sql, tuple(params)).fetchall()


def query_one(conn, sql, params=()):
    """执行参数化查询并返回第一行（无结果为 ``None``）。"""
    return conn.execute(sql, tuple(params)).fetchone()


def query_value(conn, sql, params=(), default=None):
    """执行参数化查询并返回第一列的值，便于取 COUNT/SUM 这类单值。"""
    row = query_one(conn, sql, params)
    if row is None or row[0] is None:
        return default
    return row[0]
