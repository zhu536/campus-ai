# -*- coding: utf-8 -*-
"""Flask 应用工厂与统一 JSON 错误处理。

启动（项目根目录）：
    set FLASK_APP=backend.app            # PowerShell: $env:FLASK_APP="backend.app"
    python -m flask run                  # 或 python -m backend.app
    # 直接运行脚本也可以：python backend/app.py

环境变量：
    CAMPUS_DB_PATH   覆盖 SQLite 文件路径（默认 data/campus.db），测试/演练用
    HOST / PORT      监听地址与端口（默认 127.0.0.1:5000）

错误约定（全部返回 JSON，便于前端与 AI 层统一处理）：
    400  参数缺失或非法
    404  查询目标不存在（例如专业名写错）
    405  方法不允许
    500  未预期的服务端异常
    503  数据库文件缺失/无法打开
"""

import logging
import os
import sqlite3
import sys

from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException

# 支持 `python backend/app.py` 直接运行（此时包上下文不存在，需要补 sys.path）。
if __package__ in (None, ""):  # pragma: no cover - 仅影响命令行启动方式
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from backend.db import default_db_path
    from backend.errors import DatabaseUnavailableError, NotFoundError, ValidationError
    from backend.routes import api
else:
    from .db import default_db_path
    from .errors import DatabaseUnavailableError, NotFoundError, ValidationError
    from .routes import api

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000


def create_app(db_path=None):
    """创建 Flask 应用。``db_path`` 为空时用 ``CAMPUS_DB_PATH`` 或 ``data/campus.db``。"""
    app = Flask(__name__)
    app.config["DATABASE"] = db_path or default_db_path()
    app.json.ensure_ascii = False  # 中文直接输出，便于人工核对与前端展示
    app.url_map.strict_slashes = False

    app.register_blueprint(api, url_prefix="/api")
    _register_error_handlers(app)
    return app


def _error_response(status, code, message, extra=None):
    payload = {"status": "error", "error": {"code": code, "message": message}}
    if extra:
        payload["error"].update(extra)
    return jsonify(payload), status


def _register_error_handlers(app):
    """把各类异常统一翻译成带合适 HTTP 状态码的 JSON。"""

    @app.errorhandler(ValidationError)
    def handle_validation_error(exc):
        return _error_response(400, exc.to_dict()["code"], exc.message, _extra(exc))

    @app.errorhandler(NotFoundError)
    def handle_not_found(exc):
        return _error_response(404, exc.to_dict()["code"], exc.message, _extra(exc))

    @app.errorhandler(DatabaseUnavailableError)
    def handle_database_unavailable(exc):
        app.logger.error("数据库不可用：%s", exc.message)
        return _error_response(503, "database_unavailable", exc.message)

    @app.errorhandler(HTTPException)
    def handle_http_exception(exc):
        code = "method_not_allowed" if exc.code == 405 else "http_error"
        message = (
            "该地址只支持 {}".format(", ".join(sorted(exc.valid_methods)))
            if exc.code == 405 and getattr(exc, "valid_methods", None)
            else exc.description
        )
        return _error_response(exc.code or 500, code, message)

    @app.errorhandler(sqlite3.Error)
    def handle_sqlite_error(exc):
        app.logger.exception("SQLite 查询失败")
        return _error_response(500, "database_error", "数据库查询失败：{}".format(exc))

    @app.errorhandler(Exception)
    def handle_unexpected(exc):
        app.logger.exception("未处理的异常")
        return _error_response(500, "internal_error", "服务器内部错误：{}".format(exc))


def _extra(exc):
    extra = {}
    field = getattr(exc, "field", None)
    if field:
        extra["field"] = field
    available = getattr(exc, "available", None)
    if available:
        extra["available"] = available
    return extra


def main():  # pragma: no cover - 命令行入口
    logging.basicConfig(level=logging.INFO)
    host = os.environ.get("HOST", DEFAULT_HOST)
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    create_app().run(host=host, port=port, debug=False)


if __name__ == "__main__":  # pragma: no cover
    main()
