# -*- coding: utf-8 -*-
"""统一的业务异常。

``queries`` 层只抛这个异常，``routes`` 层只负责把它翻译成 HTTP 状态码，
两边都不需要知道对方的存在（AGENTS.md：业务查询函数和 Flask 路由分离）。
"""


class ValidationError(Exception):
    """参数缺失或取值非法（HTTP 400）。

    ``available``（可选）是「本参数可选取值清单」：必填参数缺失时把它带给前端，
    400 也能给出可操作提示（例如「缺少 major，可选：软件工程/网络工程/…」），
    不用先撞一次 404 才知道有哪些专业。取值非法时不带它（避免把无关清单塞进错误）。
    """

    status_code = 400

    def __init__(self, message, field=None, available=None):
        super().__init__(message)
        self.message = message
        self.field = field
        # 与 NotFoundError 同形：可用取值清单，帮助调用方（或 AI 层）纠正参数。
        self.available = list(available) if available else []

    def to_dict(self):
        payload = {"code": "invalid_parameter", "message": self.message}
        if self.field:
            payload["field"] = self.field
        if self.available:
            payload["available"] = self.available
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
