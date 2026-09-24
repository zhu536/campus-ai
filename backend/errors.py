# -*- coding: utf-8 -*-
"""统一的业务异常。

``queries`` 层只抛这个异常，``routes`` 层只负责把它翻译成 HTTP 状态码，
两边都不需要知道对方的存在（AGENTS.md：业务查询函数和 Flask 路由分离）。
"""


class ValidationError(Exception):
    """参数缺失或取值非法（HTTP 400）。"""

    status_code = 400

    def __init__(self, message, field=None):
        super().__init__(message)
        self.message = message
        self.field = field

    def to_dict(self):
        payload = {"code": "invalid_parameter", "message": self.message}
        if self.field:
            payload["field"] = self.field
        return payload


class NotFoundError(Exception):
    """查询目标不存在（HTTP 404），例如专业名写错了。"""

    status_code = 404

    def __init__(self, message, field=None, available=None):
        super().__init__(message)
        self.message = message
        self.field = field
        # 可用取值清单，帮助调用方（或 AI 层）纠正参数。
        self.available = list(available) if available else []

    def to_dict(self):
        payload = {"code": "not_found", "message": self.message}
        if self.field:
            payload["field"] = self.field
        if self.available:
            payload["available"] = self.available
        return payload


class DatabaseUnavailableError(Exception):
    """数据库文件缺失或无法打开（HTTP 503，属于服务端环境问题）。"""

    status_code = 503

    def __init__(self, message):
        super().__init__(message)
        self.message = message

    def to_dict(self):
        return {"code": "database_unavailable", "message": self.message}
