# -*- coding: utf-8 -*-
"""CampusAI 后端包。

分层（AGENTS.md「业务查询函数和 Flask 路由分离」）：
    db.py          数据库连接与参数化查询工具
    validators.py  查询参数校验（把 ?a=b 字符串变成安全的 Python 值）
    queries.py     业务查询与计算（不 import flask，可被脚本/测试直接调用）
    routes.py      Flask 路由（只做参数解析、调用业务函数、返回 JSON）
    app.py         Flask 应用工厂与统一 JSON 错误处理

本项目当前阶段不接 AI：四大接口全部是确定性的 SQL 查询 + Python 计算。
"""

__all__ = ["create_app"]


def create_app(*args, **kwargs):
    """延迟导入，避免 ``import backend`` 时就把 Flask 拉进来。"""
    from .app import create_app as factory

    return factory(*args, **kwargs)
