# -*- coding: utf-8 -*-
"""CampusAI 意图解析器：自然语言 → 结构化业务请求（Intent JSON）。

职责边界（AGENTS.md AI Rules）：
    AI      → 识别意图、抽取参数、选图表、解释结果
    Python  → 参数校验、数据查询、业务逻辑、计算、预测（本模块只做「解析 + 校验」）
    SQLite  → 只保存事实数据

因此本模块：
    1. 只用 ``requests`` 调 OpenAI-compatible 的 ``/chat/completions``
    2. 配置（API Key / BASE_URL / MODEL）一律从 ``.env`` 读，不写死在代码里
    3. system prompt 从 ``prompts/intent_parser.md`` 读（正式 Prompt 走 Git 留痕）
    4. 要求模型返回 JSON（``response_format={"type": "json_object"}``）并解析
    5. 交给 :mod:`backend.validate` 做白名单与字段校验
    6. **不连数据库、不生成 SQL**（本模块不 import sqlite3，也不 import backend.db）
    7. 网络/HTTP/JSON/校验任何一步失败都抛 :class:`AIParserError` 或降级返回 unknown

命令行（便于人工核对，不会写入任何数据）::

    python -m backend.ai_parser "周三下午帮我找一个8个人、有投影的空教室"
    python -m backend.ai_parser --golden            # 只读跑一遍 tests/golden_questions.json
    python -m backend.ai_parser "帮我找教室" --dry-run   # 不发请求，只打印 prompt 与契约
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests

# 支持 `python backend/ai_parser.py` 直接运行（此时包上下文不存在）。
if __package__ in (None, ""):  # pragma: no cover - 仅影响命令行启动方式
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from backend.errors import ValidationError
    from backend.validate import INTENTS, render_contract, validate_intent
else:
    from .errors import ValidationError
    from .validate import INTENTS, render_contract, validate_intent

# backend/ai_parser.py → backend/ → 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PROMPT_PATH = os.path.join(PROJECT_ROOT, "prompts", "intent_parser.md")
GOLDEN_PATH = os.path.join(PROJECT_ROOT, "tests", "golden_questions.json")

#: 默认 base_url / model（.env 缺失时的兜底；API Key 没有兜底，缺了就直接报错）。
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_TOKENS = 800
DEFAULT_TEMPERATURE = 0

#: 降级意图：AI 不可用时返回它，让调用方（前端）能给出「稍后再试」而不是崩掉。
FALLBACK_INTENT = "unknown"

#: 错误码（供上层判断该重试还是该报错）。
ERROR_MISSING_CONFIG = "missing_config"
ERROR_MISSING_PROMPT = "missing_prompt"
ERROR_EMPTY_QUESTION = "empty_question"
ERROR_NETWORK = "network_error"
ERROR_HTTP_STATUS = "http_error"
ERROR_INVALID_RESPONSE = "invalid_response"
ERROR_INVALID_JSON = "invalid_json"
ERROR_INVALID_INTENT = "invalid_intent"

#: 放进错误消息里的响应片段上限，避免把模型/网关的长文本原样回显。
_EXCERPT_LIMIT = 300


class AIParserError(RuntimeError):
    """意图解析失败（配置缺失 / 网络失败 / 返回非 JSON / 校验不过）。

    带 ``code`` 与可选 ``field``，方便上层按错误类型决定「重试 / 降级 / 报 400」。
    **绝不包含 API Key**：消息里只出现错误码、状态码与响应片段。
    """

    def __init__(self, message, code="ai_error", field=None, status_code=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.field = field
        self.status_code = status_code

    def to_dict(self):
        payload = {"code": self.code, "message": self.message}
        if self.field:
            payload["field"] = self.field
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        return payload


def load_config(env=None, dotenv_path=None):
    """读取 AI 配置：``AI_API_KEY`` / ``AI_BASE_URL`` / ``AI_MODEL``（+ 可选超时等）。

    优先级：显式传入的 ``env`` → 进程环境变量 → ``.env``（python-dotenv）→ 默认值。
    API Key 没有默认值：缺失时抛 :class:`AIParserError`（code=missing_config）。
    """
    if env is None:
        try:
            from dotenv import load_dotenv  # 延迟导入，便于无 dotenv 环境也能用
        except ImportError:  # pragma: no cover - requirements.txt 里已固定 python-dotenv
            load_dotenv = None
        if load_dotenv is not None:
            # override=False：已经存在的环境变量优先，便于测试/CI 覆盖 .env。
            load_dotenv(dotenv_path or os.path.join(PROJECT_ROOT, ".env"), override=False)
        env = os.environ

    api_key = (env.get("AI_API_KEY") or "").strip()
    base_url = (env.get("AI_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    model = (env.get("AI_MODEL") or DEFAULT_MODEL).strip()

    if not api_key:
        raise AIParserError(
            "缺少 AI_API_KEY：请在项目根目录的 .env 里配置（可参考 .env.example），"
            "不要把 Key 写进源代码",
            code=ERROR_MISSING_CONFIG,
        )
    if not base_url:
        raise AIParserError("AI_BASE_URL 为空", code=ERROR_MISSING_CONFIG)
    if not model:
        raise AIParserError("AI_MODEL 为空", code=ERROR_MISSING_CONFIG)

    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "timeout": _env_float(env, "AI_TIMEOUT", DEFAULT_TIMEOUT),
        "max_tokens": _env_int(env, "AI_MAX_TOKENS", DEFAULT_MAX_TOKENS),
        "temperature": _env_float(env, "AI_TEMPERATURE", DEFAULT_TEMPERATURE),
        "prompt_path": env.get("AI_PROMPT_PATH") or DEFAULT_PROMPT_PATH,
    }


def _env_float(env, name, default):
    try:
        return float(env.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(env, name, default):
    try:
        return int(env.get(name, default))
    except (TypeError, ValueError):
        return default


#: Markdown 转义字符（``\_`` / ``\#`` / ``\*`` ...）。从聊天窗口复制出来的 Prompt
#: 常常带着这层转义，直接当 system prompt 发出去虽然也能用，但会污染示例里的字段名。
_MARKDOWN_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")


def unescape_prompt(text):
    """还原 Markdown 转义：``room\\_search`` → ``room_search``，``\\# 标题`` → ``# 标题``。

    只处理「反斜杠 + ASCII 标点」这一种最常见的复制粘贴转义，
    代码块里的内容与中文不会被改动。
    """
    return _MARKDOWN_ESCAPE_RE.sub(r"\1", text)


def load_prompt(prompt_path=None, unescape=True):
    """从文件读取 system prompt（默认 ``prompts/intent_parser.md``）。"""
    path = prompt_path or DEFAULT_PROMPT_PATH
    if not os.path.isfile(path):
        raise AIParserError(
            "找不到 Prompt 文件：{}（正式 Prompt 必须放在 prompts/ 下）".format(path),
            code=ERROR_MISSING_PROMPT,
        )
    with open(path, "r", encoding="utf-8") as handle:
        prompt = handle.read()
    if not prompt.strip():
        raise AIParserError("Prompt 文件为空：{}".format(path), code=ERROR_MISSING_PROMPT)
    return unescape_prompt(prompt) if unescape else prompt


def build_messages(question, prompt=None, prompt_path=None, contract=True):
    """组装 chat 消息：system = Prompt 文件（+ 自动生成的字段契约），user = 用户问题。"""
    system_prompt = prompt if prompt is not None else load_prompt(prompt_path)
    if contract:
        system_prompt = system_prompt.rstrip() + "\n" + render_contract()
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]


def extract_json_object(text):
    """从模型回复里取出 JSON 对象。

    正常情况下 ``response_format=json_object`` 会直接给纯 JSON；
    但不同网关可能包一层 Markdown 代码块或加一句解释，这里做容错解析：
        1. 去掉 ```json ... ``` 代码块围栏
        2. 直接 ``json.loads``
        3. 退化为「截取第一个 { 到最后一个 }」再解析
    """
    if not isinstance(text, str) or not text.strip():
        raise AIParserError("模型返回内容为空", code=ERROR_INVALID_JSON)

    candidate = text.strip()
    fence = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()

    for attempt in (candidate, _slice_object(candidate)):
        if not attempt:
            continue
        try:
            payload = json.loads(attempt)
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict):
            return payload
    raise AIParserError(
        "模型返回的不是合法 JSON 对象：{}".format(_excerpt(text)), code=ERROR_INVALID_JSON
    )


def _slice_object(text):
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return ""
    return text[start : end + 1]


def _excerpt(text, limit=_EXCERPT_LIMIT):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "..."


def parse_response_text(response):
    """把 HTTP 响应解析成「助手回复文本」。

    失败时抛 :class:`AIParserError`，并带上状态码与脱敏后的响应片段
    （错误消息里只有服务端返回的内容，不会有我们的 Authorization 头）。
    """
    status = getattr(response, "status_code", None)
    if status != 200:
        raise AIParserError(
            "调用 AI 接口失败：HTTP {}，响应片段：{}".format(status, _excerpt(_safe_text(response))),
            code=ERROR_HTTP_STATUS,
            status_code=status,
        )
    try:
        body = response.json()
    except ValueError:
        raise AIParserError(
            "AI 接口返回的不是 JSON：{}".format(_excerpt(_safe_text(response))),
            code=ERROR_INVALID_RESPONSE,
            status_code=status,
        )

    if isinstance(body, dict) and body.get("error"):
        raise AIParserError(
            "AI 接口返回错误：{}".format(_excerpt(json.dumps(body["error"], ensure_ascii=False))),
            code=ERROR_INVALID_RESPONSE,
            status_code=status,
        )

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise AIParserError(
            "AI 接口返回结构不符合 OpenAI 规范：{}".format(_excerpt(_safe_text(response))),
            code=ERROR_INVALID_RESPONSE,
            status_code=status,
        )
    if not isinstance(content, str) or not content.strip():
        raise AIParserError("AI 接口返回的 message.content 为空", code=ERROR_INVALID_RESPONSE)
    return content


def _safe_text(response):
    try:
        return response.text
    except Exception:  # pragma: no cover - 兜底：响应对象没有 text 属性时
        return "<无响应正文>"


class IntentParser:
    """意图解析器。默认从 ``.env`` 读配置、从 ``prompts/intent_parser.md`` 读 Prompt。

    典型用法::

        parser = IntentParser()                     # 配置来自 .env
        result = parser.query("周三下午找个8人有投影的空教室")
        # {'intent': 'room_search', 'params': {'weekday': 3, ...}, 'degraded': False}

    也可以直接 ``IntentParser(config={...})`` 注入配置（测试用，不读 .env）。
    """

    def __init__(self, config=None, session=None, prompt=None, prompt_path=None,
                 timeout=None, enrich=True, contract=True):
        self._config = dict(config) if config else None
        self._session = session if session is not None else requests
        self._prompt = prompt
        self._prompt_path = prompt_path
        self._timeout = timeout
        #: 是否在返回结果里附带「可读说明 + 建议图表」（只做映射，不额外调模型）。
        self.enrich = enrich
        #: 是否把字段契约拼到 system prompt 后面（保证模型只产出 Python 认识的字段）。
        self.contract = contract
        self.last_error = None

    # -- 配置 ---------------------------------------------------------------

    @property
    def config(self):
        """AI 配置（首次访问时才读 ``.env``，读取失败会抛 AIParserError）。"""
        if self._config is None:
            self._config = load_config()
        return self._config

    @property
    def system_prompt(self):
        if self._prompt is None:
            self._prompt = load_prompt(self._prompt_path or self.config["prompt_path"])
        return self._prompt

    # -- 调用链 -------------------------------------------------------------

    def request_payload(self, question):
        """构造发给 ``/chat/completions`` 的请求体（单独成方法，方便核对与测试）。"""
        return {
            "model": self.config["model"],
            "messages": build_messages(
                question, prompt=self.system_prompt, contract=self.contract
            ),
            "response_format": {"type": "json_object"},
            "temperature": self.config["temperature"],
            "max_tokens": self.config["max_tokens"],
        }

    def call_api(self, question):
        """调用 OpenAI-compatible 接口，返回助手回复文本。

        网络异常（超时、连接失败、DNS）+ 非 200 状态码统一转成 :class:`AIParserError`。
        请求失败时不会打印任何包含 API Key 的内容。
        """
        url = "{}/chat/completions".format(self.config["base_url"])
        headers = {
            "Authorization": "Bearer {}".format(self.config["api_key"]),
            "Content-Type": "application/json",
        }
        try:
            response = self._session.post(
                url,
                headers=headers,
                json=self.request_payload(question),
                timeout=self._timeout or self.config["timeout"],
            )
        except requests.exceptions.Timeout as exc:
            raise AIParserError("调用 AI 接口超时：{}".format(exc), code=ERROR_NETWORK)
        except requests.exceptions.RequestException as exc:
            raise AIParserError("调用 AI 接口失败（网络错误）：{}".format(exc), code=ERROR_NETWORK)
        return parse_response_text(response)

    def parse(self, question, use_fallback=False):
        """完整链路：提问 → 调模型 → 解析 JSON → 白名单与字段校验。

        返回::

            {"intent": "room_search", "params": {...}}

        ``use_fallback=True`` 时不抛异常，改为降级返回 ``unknown`` +
        ``degraded=True`` + ``error`` 说明（前端可以照常渲染一个提示）。
        """
        try:
            return self._parse_or_raise(question)
        except (AIParserError, ValidationError) as exc:
            self.last_error = exc
            if not use_fallback:
                raise _as_parser_error(exc)
            return _degraded_result(exc)

    def _parse_or_raise(self, question):
        if not isinstance(question, str) or not question.strip():
            raise AIParserError("用户问题不能为空", code=ERROR_EMPTY_QUESTION)
        content = self.call_api(question.strip())
        payload = extract_json_object(content)
        try:
            intent, params = validate_intent(payload)
        except ValidationError as exc:
            raise AIParserError(
                "AI 返回的意图未通过校验：{}".format(exc.message),
                code=ERROR_INVALID_INTENT,
                field=exc.field,
            )
        return {"intent": intent, "params": params, "degraded": False, "error": None}

    def query(self, question):
        """给上层用的入口：永不抛异常，失败降级为 ``unknown``。

        返回 ``{"intent", "params", "explanation", "chart", "degraded"}``。
        """
        result = self.parse(question, use_fallback=True)
        if self.enrich:
            result["chart"] = suggest_chart(result["intent"])
            result["explanation"] = explain(result["intent"], result["params"])
        return result


# ---------------------------------------------------------------------------
# 返回结果的人话说明与图表建议（AI 负责选图，这里用确定性映射兜底）
# ---------------------------------------------------------------------------

#: 意图 → 建议图表（ECharts 负责渲染）。
CHART_BY_INTENT = {
    "room_search": "table",
    "canteen_analysis": "bar",
    "canteen_forecast": "line",
    "industry_skill_match": "bar",
    "industry_skill_gap": "bar",
    "dashboard_query": "pie",
    "unknown": "none",
}

INTENT_LABELS = {
    "room_search": "查找空闲教室",
    "canteen_analysis": "食堂人流分析",
    "canteen_forecast": "食堂拥挤预测",
    "industry_skill_match": "岗位技能匹配",
    "industry_skill_gap": "技能缺口分析",
    "dashboard_query": "看板查询",
    "unknown": "无法识别",
}


def suggest_chart(intent):
    return CHART_BY_INTENT.get(intent, "none")


def explain(intent, params):
    """把结构化意图翻译成一句中文说明（纯文本拼接，不做任何计算）。"""
    label = INTENT_LABELS.get(intent, intent)
    if intent == "unknown" or not params:
        return label
    parts = ["{}={}".format(key, json.dumps(value, ensure_ascii=False)) for key, value in sorted(params.items())]
    return "{}（{}）".format(label, "，".join(parts))


def _as_parser_error(exc):
    """把 :class:`ValidationError` 统一包装成 :class:`AIParserError` 再抛给上层。"""
    if isinstance(exc, AIParserError):
        return exc
    return AIParserError(exc.message, code=ERROR_INVALID_INTENT, field=getattr(exc, "field", None))


def _degraded_result(exc):
    """降级返回：意图为 unknown，并说明失败原因（不含任何密钥）。"""
    error = _as_parser_error(exc)
    return {
        "intent": FALLBACK_INTENT,
        "params": {},
        "degraded": True,
        "error": error.to_dict(),
    }


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------

def load_golden_questions(path=None):
    """读取 ``tests/golden_questions.json``（黄金问题集，只读）。"""
    target = path or GOLDEN_PATH
    with open(target, "r", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv=None):  # pragma: no cover - 命令行入口
    parser = argparse.ArgumentParser(description="CampusAI 意图解析器（自然语言 → Intent JSON）")
    parser.add_argument("question", nargs="?", help="用户自然语言问题")
    parser.add_argument("--golden", action="store_true", help="只读跑一遍黄金问题集（会真实调用 AI）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 Prompt 与请求体，不发起请求")
    parser.add_argument("--allow-degraded", action="store_true", help="失败时降级为 unknown 而不是退出码 2")
    args = parser.parse_args(argv)

    try:
        instance = IntentParser()
    except AIParserError as exc:
        print("[配置错误] {}".format(exc.message), file=sys.stderr)
        return 2

    if args.dry_run:
        question = args.question or "周三下午帮我找一个8个人、有投影的空教室"
        print("== system prompt ==")
        print(instance.system_prompt)
        print("== request ==")
        print(json.dumps(instance.request_payload(question), ensure_ascii=False, indent=2))
        return 0

    if args.golden:
        questions = load_golden_questions()
        matched = 0
        for item in questions:
            try:
                result = instance.query(item["question"])
            except Exception as exc:  # 单条失败不影响整轮
                print("[{}] ERROR {}".format(item["question"], exc))
                continue
            ok = result["intent"] == item["answer"]["intent"] and not result.get("degraded")
            matched += 1 if ok else 0
            print("[{}] {} -> {}".format("OK " if ok else "DIFF", item["question"], result["intent"]))
        print("命中 {}/{}".format(matched, len(questions)))
        return 0 if matched == len(questions) else 1

    if not args.question:
        parser.print_help()
        return 1

    try:
        result = instance.parse(args.question, use_fallback=args.allow_degraded)
    except AIParserError as exc:
        print("[解析失败] {} ({})".format(exc.message, exc.code), file=sys.stderr)
        return 2
    result["chart"] = suggest_chart(result["intent"])
    result["explanation"] = explain(result["intent"], result["params"])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
