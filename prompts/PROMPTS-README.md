# prompts/ 目录说明（重要：先读这一页）

## 这个目录是「给程序吃」的系统提示词，不是「人机对话日志」

本目录下的 `.md` 文件是 **CampusAI 运行时真正加载的 system prompt**，
由后端 Python 代码通过 `backend/ai_parser.py` 的 `load_prompt()` 读取后
作为 `system` 角色消息发给大模型。

| 文件 | 谁在读它 | 作用 |
|---|---|---|
| `intent_parser.md` | `backend/ai_parser.py:50`（`DEFAULT_PROMPT_PATH`） | 把用户自然语言转成结构化 intent + 参数 |
| `insight_generator.md` | `backend/classroom.py:55` | 只解释「找教室」的查询结果 |
| `canteen_insight_generator.md` | `backend/forecast.py:64` | 只解释食堂趋势/高峰/预测结果 |
| `industry_insight_generator.md` | `backend/industry.py:77` | 只解释产教融合技能匹配结果 |

调用链（以找教室为例）：

```
用户提问
  → backend/classroom.py
  → load_prompt("prompts/intent_parser.md")        # ← 本目录的文件在这里被读走
  → POST {AI_BASE_URL}/chat/completions  (role=system)
  → 返回 JSON → validate.py 白名单校验 → SQLite 查询
  → load_prompt("prompts/insight_generator.md")    # ← 本目录的文件再次被读走
  → AI 只解释结果
```

**因此本目录的约束（与 AGENTS.md 一致）：**

1. **这些文件是代码资产，不是笔记。** 改动会影响线上行为，必须走 Git 留痕。
2. 改动后必须跑 `pytest -q`，确认 597 个用例仍然全绿。
3. **不要**把「我给 Agent 下过什么指令」这类对话记录写进本目录。
4. **不要**在本目录存放临时稿、草稿、截图。

---

## 对话记录应该放哪里？

放到 **`docs/ai-collaboration/`**。两者的区别：

| | `prompts/` | `docs/ai-collaboration/` |
|---|---|---|
| 内容 | 程序运行时的 system prompt | 人如何指挥 AI 开发这个项目的记录 |
| 消费者 | Python 代码（大模型） | 比赛评委、未来的你 |
| 改坏了会怎样 | 功能出错 / 测试失败 | 没有影响 |
| 谁的证据 | 产品能力证据 | **AI 协同开发能力证据（本赛道核心评分项）** |

在传智杯「AI 创新应用挑战赛」的评分语境下，
**`docs/ai-collaboration/` 才是你要拼命补的地方**，本目录已经齐了。

---

## 已知待办：本目录存在历史转义污染

这些文件是从聊天窗口复制粘贴生成的，带有两类残留：

1. **Markdown 转义残留**：标题写成 `\#`、列表写成 `1\.`、连字符写成 `\-`。
   实测 `intent_parser.md`：`\#` × 9、`\.` × 15、`\-` × 26。
   代码里的 `unescape_prompt()` 会在运行时还原，所以**当前不影响功能**，
   但直接阅读体验很差，且是一颗隐患（任何绕过 unescape 的读取路径都会读到脏内容）。
2. **每行之间多出两个空行**（同样是粘贴产生的），使文件体积虚高、可读性下降。

**建议的处理方式（尚未执行，需负责人确认）：**

- 把这些文件整理成正常 UTF-8 Markdown（去掉多余转义与多余空行）；
- 保留 `unescape_prompt()` 作为向后兼容，不删；
- 新增一个测试，断言本目录下不再出现 `\#` / `\*\*` / `1\.` 形式的转义残留，
  防止再次退化；
- 整理后必须重跑 `pytest -q` 全量验证。

> 注意：这些文件当前是**合法 UTF-8**，中文没有损坏。
> 如果你用 PowerShell 的 `Get-Content` 看到乱码，那是控制台编码问题，
> 请改用 `Get-Content -Encoding UTF8`，或直接用编辑器打开确认。
