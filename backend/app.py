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

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

# 支持 `python backend/app.py` 直接运行（此时包上下文不存在，需要补 sys.path）。
if __package__ in (None, ""):  # pragma: no cover - 仅影响命令行启动方式
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from backend.classroom import ClassroomSearchError
    from backend.db import default_db_path
    from backend.errors import DatabaseUnavailableError, NotFoundError, ValidationError
    from backend.routes import api
else:
    from .classroom import ClassroomSearchError
    from .db import default_db_path
    from .errors import DatabaseUnavailableError, NotFoundError, ValidationError
    from .routes import api

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000

# 前端大屏（frontend/index.html）允许被 file:// 直接打开，此时浏览器会带 Origin: null
# 发跨域请求。这里按请求回显 Origin（不写死 *，避免把接口暴露给任意站点），
# 只放行只读 GET/POST，不引入 flask-cors 依赖。
CORS_ALLOW_METHODS = "GET, POST, OPTIONS"
CORS_ALLOW_HEADERS = "Content-Type"
#: 预检结果缓存秒数：同一页面反复切学期/快照时不必每次都发 OPTIONS。
CORS_MAX_AGE = "600"


def create_app(db_path=None):
    """创建 Flask 应用。``db_path`` 为空时用 ``CAMPUS_DB_PATH`` 或 ``data/campus.db``。"""
    app = Flask(__name__)
    app.config["DATABASE"] = db_path or default_db_path()
    app.json.ensure_ascii = False  # 中文直接输出，便于人工核对与前端展示
    app.url_map.strict_slashes = False

    app.register_blueprint(api, url_prefix="/api")
    _register_error_handlers(app)
    _register_cors(app)
    return app


def _register_cors(app):
    """给带 Origin 头的请求回显 CORS 响应头。

    只有浏览器跨域请求才会带 Origin，测试用的 ``app.test_client()`` 不带，
    因此该钩子对既有接口的行为与测试结果都是无侵入的。

    预检（OPTIONS）额外做两件事，避免浏览器在边界场景下直接拦住请求：
        1. 回显 ``Access-Control-Request-Headers``（浏览器要什么就答应什么），
           而不是死守一个固定的 Content-Type 列表；
        2. 回 ``Access-Control-Max-Age``，省掉重复预检。
    仍然只回显请求方 Origin，不放行任意站点。
    """

    @app.after_request
    def add_cors_headers(response):
        origin = request.headers.get("Origin")
        if origin:
            headers = response.headers
            headers["Access-Control-Allow-Origin"] = origin
            headers["Access-Control-Allow-Methods"] = CORS_ALLOW_METHODS
            headers["Access-Control-Allow-Headers"] = (
                request.headers.get("Access-Control-Request-Headers") or CORS_ALLOW_HEADERS
            )
            headers["Access-Control-Max-Age"] = CORS_MAX_AGE
            vary = "Origin"
            if request.method == "OPTIONS":
                vary += ", Access-Control-Request-Headers"
            headers["Vary"] = vary
        return response


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

    @app.errorhandler(ClassroomSearchError)
    def handle_classroom_search_error(exc):
        """找教室链路失败：AI 不可用 503 / 意图不是找教室 400（见 backend/classroom.py）。"""
        return _error_response(exc.status_code, exc.code, exc.message, _extra(exc))

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
