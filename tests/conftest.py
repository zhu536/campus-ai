# -*- coding: utf-8 -*-
"""pytest 共享夹具。

这里覆盖 pytest 自带的 `tmp_path` / `tmp_path_factory`：
pytest 的实现在部分受控文件系统（受限令牌沙箱、只读挂载的临时目录）下
会因为目录权限扫描失败而报 PermissionError，导致测试无法运行。
本文件改用 `tempfile.mkdtemp` 在仓库内创建临时目录，行为与 pytest 版本一致：
每个测试拿到独立目录、目录名前缀统一、会话结束不残留。
"""

import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile

import pytest

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

TEMP_ROOT = os.path.join(tempfile.gettempdir(), "campus-ai-tests")


def _unique_dir(root, prefix):
    """用 os.mkdir 创建唯一目录。

    不使用 tempfile.mkdtemp：它创建的目录带有严格安全描述符，在受控文件系统
    （受限令牌沙箱）下会让后续写文件、列目录失败；os.mkdir 创建的是普通目录。
    """
    for attempt in range(1000):
        path = os.path.join(root, "{}{}_{}".format(prefix, os.getpid(), attempt))
        try:
            os.mkdir(path)
        except FileExistsError:
            continue
        return path
    raise RuntimeError("无法在 {} 下创建唯一临时目录".format(root))


def _writable(directory):
    """探测目录是否能真正建库、列目录并读回。"""
    try:
        os.makedirs(directory, exist_ok=True)
        probe_dir = _unique_dir(directory, "probe_")
    except OSError:
        return False
    try:
        probe = os.path.join(probe_dir, "probe.db")
        connection = sqlite3.connect(probe)
        connection.execute("CREATE TABLE probe (id INTEGER)")
        connection.close()
        os.listdir(probe_dir)
        return True
    except (OSError, sqlite3.Error):
        return False
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def tmp_path_factory():
    """会话级临时目录工厂：`mktemp(prefix)` 返回一个全新的临时目录。

    优先用系统临时目录；不可写（或无法建 SQLite 文件）时退回到仓库内的
    `.pytest_tmp`，可用环境变量 `CAMPUS_TEST_TMP` 指定其它位置。
    """
    created = []
    candidates = [
        os.environ.get("CAMPUS_TEST_TMP"),
        TEMP_ROOT,
        os.path.join(PROJECT_ROOT, ".tmp_test_run"),
    ]
    root = None
    for candidate in candidates:
        if candidate and _writable(candidate):
            root = candidate
            break
    if root is None:
        raise RuntimeError(
            "找不到可写的临时目录（已尝试 {}）；"
            "请设置环境变量 CAMPUS_TEST_TMP".format("、".join(str(c) for c in candidates if c))
        )

    class Factory:
        def mktemp(self, basename="tmp", numbered=True):
            path = _unique_dir(root, "{}_".format(basename))
            created.append(path)
            return pathlib.Path(path)

    try:
        yield Factory()
    finally:
        for path in created:
            shutil.rmtree(path, ignore_errors=True)
        if os.path.isdir(root) and not os.listdir(root):
            try:
                os.rmdir(root)
            except OSError:
                pass


@pytest.fixture()
def tmp_path(tmp_path_factory):
    """单个测试独占的临时目录。"""
    return tmp_path_factory.mktemp("test")
