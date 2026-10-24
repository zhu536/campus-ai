# Day18 · README 编写 + 陌生环境从零复现验证

今天开 1 个 Harness 会话给项目写根目录 README.md（赛道提交硬性材料之一：10 分钟极速复现指南），
写完**我没有直接信**，先自己全量核实了 README 里的命令、数字、文件名，然后亲手做了一遍"陌生人测试"——
从拿源码到点开页面，每一步都按 README 的流程在全新目录里真实跑了一遍，验证"第一次拿到项目的人能复现"。

最终状态：只新增根目录 `README.md` 一个文件（git status 仅 `?? README.md`），`backend/`、`frontend/`、`prompts/`、`tests/`、`data/` 全部 unchanged；未提交（commit 由我手动执行）。

---

## 会话 1：让 Harness 写 README（19:19 发起，用时 13 分 30 秒）

### 1. 本轮 Prompt（逐字原文）

```text
帮我给项目 D:\Projects\campus-ai 写一个 README.md，这个项目根目录现在还没有 README。
项目是 CampusAI 校园数据智能分析平台，技术栈：Python 3.12 + Flask + SQLite，前端是原生 HTML/CSS/JS + ECharts，大模型走 OpenAI 兼容接口。GitHub 仓库 https://github.com/CrestLune/campus-ai.git。
写之前先读 AGENTS.md、docs/PRD.md、docs/API.md、docs/DATABASE.md、docs/ai-collaboration/README.md，搞清楚项目再写，别编。
目标：一个第一次拿到项目的人，10 分钟内能跑起来。要覆盖这些：
1. 一句话简介 + 三个核心功能（AI 找教室 / 食堂分析与预测 / 产教融合技能匹配）
2. 技术栈
3. 目录结构
4. 环境要求（Python 3.12、Git）
5. 克隆仓库：git clone https://github.com/CrestLune/campus-ai.git
6. 建虚拟环境：py -3.12 -m venv .venv，PowerShell 激活
7. 装依赖：pip install -r requirements.txt
8. .env 配置：把 .env.example 复制成 .env 填 AI_API_KEY，再说明 AI_BASE_URL / AI_MODEL / AI_MAX_TOKENS / AI_TIMEOUT 四个变量含义（env.example 一共 5 个变量）
9. 生成模拟数据：python scripts/make_data.py，固定 seed 42，生成 9 个 CSV 到 data/raw，可重复，跑完自带 8 组检查
10. 导入数据库：python scripts/import_data.py
11. 启动后端：python backend/app.py，127.0.0.1:5000
12. 打开前端：直接双击 frontend/index.html 就行（CORS 已处理，有离线兜底，不填 Key 也能看离线演示）
13. API 说明：10 个接口全列出来——classrooms/available、classrooms/search、canteens/stats、canteens/trend、canteens/peak、canteens/forecast、meta/semesters、industry/skills、industry/gap、industry/match，每个写方法、参数、返回
14. 跑测试：pytest -q，现在 598 个用例全绿
15. AI 架构一句话讲清：意图解析 → 参数校验 → Python 查询 → SQLite → AI 解释，AI 不碰 SQL
16. prompts/ 和 docs/ai-collaboration/ 分别是干嘛的
17. 三个 demo 问句（找教室 / 食堂 / 产教各一句）
18. 常见问题：PowerShell 执行策略、端口占用、API Key 失效时自动降级离线示例
硬性要求：
1. 每条命令、每个文件、每个环境变量，都得先在项目里实际核对过再写，不存在的别写，数字别编
2. 全文中文为主
3. 只新建 README.md 一个文件，其他文件一律不动
4. 写完告诉我：1.章节结构 2.每节写了什么 3.你核对过哪些命令
5.不要执行 git commit
```

### 2. Agent 的回复（原文关键段）

> 已完成，用时 13 分 30 秒。
>
> 执行轨迹：先读取指定文档、在隔离的临时副本里逐个验证命令，再写 README（598 passed confirmed → 临时副本验证数据管线 → I have all the facts verified. Now writing the README）。
>
> **跑过的命令与结果：**

| 命令 | 结果 |
|---|---|
| `.\.venv\Scripts\python.exe -m pytest -q` | 598 passed in 7.89s |
| `python scripts\make_data.py` | 在项目临时副本执行：8 组检查全过、退出码 0；连续两次生成的 9 个 CSV 与仓库现有文件 SHA-256 完全一致 |
| `python scripts\import_data.py` | 临时副本执行成功：9 张表 10651 行、外键检查通过 |
| `python backend\app.py` | 临时副本执行，`/api/meta/semesters`、`/api/classrooms/available`、`/api/canteens/peak`、`/api/industry/match` 均 200 |
| `python -m backend.app` | 启动成功，`/api/meta/semesters` 200 |
| CORS 预检（Origin: null + OPTIONS） | 200，`Access-Control-Allow-Origin: null`、`Allow-Methods: GET, POST, OPTIONS` |
| 去掉 `AI_API_KEY` 的降级 | `/api/canteens/trend` → 200 + `source=local` + `error.code=missing_config`；`/api/classrooms/search` → 503；`/api/industry/match?major=软件工程` → 200 且 `coverage_rate=0.5882` |
| SQLite 直查 | 9 张表行数、两个学期 2025-2026-1/2、8 个专业、5 个食堂名、30 个技能 |

> **未能实测的一项（如实说明）**：`pip install -r requirements.txt` 在全新虚拟环境中没有跑通——本会话的沙箱里 `py -3.12 -m venv` 的 ensurepip 引导因临时目录被拒绝删除而失败。我用替代方式证明该步骤成立：现有 `.venv` 中 requirements.txt 的 19 个包全部已安装（版本逐条比对一致），且该 venv 就是用 `py -3.12 -m venv` 建的。这是沙箱限制，不是项目问题。
>
> 我没有执行 git commit；`git status` 显示仓库根目录只有 `?? README.md` 一个新增文件，其余文件（含 data/raw、data/campus.db）未改动，核验用的临时副本已删除。
>
> **一个顺带发现**：`prompts/PROMPTS-README.md` 里写的是「597 个用例」，实际已是 598，属于该文档的过期数字。

### 3. 发现的具体问题

| 问题 | 位置 | 现象 |
|---|---|---|
| 文档计数陈旧 | `prompts/PROMPTS-README.md`（Agent 报告"597 个用例"，实际 598） | 与测试现状不符，列入缓冲期清理待办 |
| pip 全新环境安装未实测 | Harness 沙箱限制（ensurepip 引导失败） | Agent 已如实标注并用替代方式证明；由下方"陌生人测试"补齐真机验证 |

README 本身 18 项内容经逐条核对**没有发现编造项**（见下节）。

### 4. 人的核实（我抽查 + 全量核对，不只信报告）

写完 README 后，我把最容易被 AI 编出来的内容逐条对了一遍：

| 核对项 | 结果 |
|---|---|
| `py -3.12 --version` |  本机真实可用（Python 3.12.7；系统默认 python 是 3.14.7，README 用 py launcher 指定 3.12 是对的） |
| 10 个接口清单 |  与 `backend/routes.py` 的注册一致（全部前缀 /api，classrooms/search 为 GET+POST，其余 GET） |
| requirements.txt 19 个包及版本号 |  逐条属实（Flask 3.1.3 / requests 2.34.2 / python-dotenv 1.2.3 / pytest 9.1.1 等） |
| `.env.example` 恰好 5 个变量 |  属实（AI_API_KEY / AI_BASE_URL / AI_MODEL / AI_MAX_TOKENS / AI_TIMEOUT），变量含义与 `backend/ai_parser.py` 默认值一致 |
| 9 个 CSV 行数（1186 / 8480 / 100 / 50 / 100 / 30 / 5 / 300 / 400） |  与 make_data.py 实测输出一致 |
| "598 个用例全绿" |  真实（pytest 实跑 598 passed） |
| Key 失效降级行为描述 |  与代码实际行为一致（search→503 ai_unavailable；trend→200 source=local） |

结论：README 里没有虚构命令、文件、环境变量，数字全部真实。

---

## 5. 陌生人测试（我亲手执行，模拟第一次拿到项目的人）

按 README 第 19 节命令速查，在全新目录里从零跑一遍。**这步故意不用 Harness，就是要证明真人照着 README 能跑通。**

### 5.1 拿源码：clone 失败 → 本地复制方案

`git clone https://github.com/CrestLune/campus-ai.git` 连续两次 **Connection was reset**（国内网络访问 GitHub 不稳定）→ 改用本地复制模拟"陌生人拿到源码"：

```powershell
Copy-Item D:\Projects\campus-ai D:\Projects\campus-ai-test -Recurse
cd D:\Projects\campus-ai-test
Remove-Item .git -Recurse -Force     # 模拟从 GitHub 克隆下来，没有 .git
Remove-Item .venv -Recurse -Force    # 模拟陌生环境，没有虚拟环境
```

### 5.2 建环境 + 装依赖 + 配 Key

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1         # 首次弹"不可信发布者"，选 R 运行一次
pip install -r requirements.txt      # 官方源卡住 → Ctrl+C → 换清华源
Copy-Item .env.example .env          # 未填 Key（正好验证 README Q3 的降级行为）
```

- 清华源一次装完 **19 个包**（`Successfully installed Flask-3.1.3 ... pytest-9.1.1 ...`），弥补了 Harness 沙箱里没实测的那一步；
- 判断"pip 卡住 vs 正常等待"：等待下载时屏幕不刷新行是正常的，出 `Successfully installed` 即完成。

### 5.3 生成数据 + 导入数据库

```powershell
python scripts\make_data.py
```

- 9 个 CSV 全部生成，**8 组检查全过（0 问题 0 提示）**，双学期各 593 条课表；
- 连续生成的 CSV 与仓库现有文件一致（README 第 8 节的"可复现"成立）。

```powershell
python scripts\import_data.py
```

- 9 张表 **合计 10651 行**，外键完整性检查通过（与 README 第 9 节表格完全一致）。

### 5.4 启动后端 + 打开前端

```powershell
python backend\app.py
```

- `Running on http://127.0.0.1:5000`，启动成功；
- 浏览器打开 `frontend\index.html`，把 5 个快捷按钮各点一遍：

| 按钮 | 实测结果 |
|---|---|
| 找空教室 | **离线示例**（测试目录 `.env` 未填 Key → 后端返回 503 + `ai_unavailable` → 前端按 README Q3 预期回落内置快照，显示「离线示例」标注） |
| 食堂拥挤预测 |  实时：一食堂最挤（拥挤度 51% · 平均等待 8.4 分钟），未来午餐预测 3951 人 |
| 技能缺口分析 |  实时：计算机科学与技术 覆盖 11 项 / 缺口 14 项 / 覆盖率 44.0%（加权 42.5%） |
| 教室利用率 |  实时：该时段占用 41 间 / 空闲 6 间 / 利用率 87.2% |
| 岗位技能需求 |  实时：在招岗位 100 个 / 覆盖 8 个行业 / 27 项技能 |

- 除找教室外的 4 个按钮全部走实时接口（200），找教室走离线降级——**和 README 第 17 节 Q3 写的降级行为完全一致，不是 bug**。

### 5.5 后端接口直测（我的抽查）

用 Python requests 直测核心接口，确认服务端数据真实：

| 接口 | 结果 |
|---|---|
| `/api/meta/semesters` | 200，双学期 2025-2026-1 / 2025-2026-2 |
| `/api/classrooms/available?capacity=8&has_projector=1&weekday=3...` | 200，返回 15 间具体空教室（D301 / D201 / B103 …） |
| `/api/canteens/stats?time_slot=lunch` | 200，5 个食堂拥挤度 / 等待 / 高峰全部返回 |
| `/api/industry/gap?major=计算机科学与技术` | 200，覆盖 11 项 / 缺口 14 项 / 覆盖率 44.0% |
| `/api/classrooms/search`（自然语言，无 Key） | 503 + `ai_unavailable`（README Q3 预期降级） |

（注：用 curl 在 PowerShell 里传中文参数会因编码变成乱码导致"专业不存在"，是测试方式问题不是后端问题，浏览器内点击不受影响。）

---

## 6. 改动文件汇总

只新增根目录 `README.md`（19 个章节，git status 确认无其他文件；核验用的临时副本已删）。

- README 内容全部来自真实项目：命令都真跑过、行数/版本号/测试数都与实测一致；
- 未提交（commit 由我手动执行）。

## 7. 剩余风险

1. GitHub clone 在国内网络不稳定（本次连续两次 Connection reset），提交后评委克隆仓库也可能遇到；建议保留 Gitee 镜像或提醒评审用代理。
2. Harness 沙箱里 `pip install` 全新环境未实测（已由人真机验证补齐，但 README 写的是官方源命令，国内用户建议按需换镜像源，README 未写镜像源提示，可后续补充）。
3. `prompts/PROMPTS-README.md` "597 个用例" → 实际 598，待缓冲期清理。
4. 陌生人测试目录 `.env` 未填 Key，找教室走离线降级（预期行为）；评委拿到仓库后填 Key 即可看到完整 AI 实时回答。

## 8. 验证截图清单（截图由我手动截取，均已放入 images/ 目录）

| 文件名（docs/ai-collaboration/images/） | 内容 | 用途 |
|---|---|---|
| `day18-harness-readme-report.png` | Harness 写 README 的完整报告（13 分 30 秒，真跑命令表 + 未实测项说明 + 597→598 发现） | 会话 1 证据 |
| `day18-clone-fail.png` | `git clone` 两次 Connection was reset（网络问题 → 改用本地复制方案） | 陌生人测试 5.1 证据 |
| `day18-pip-mirror-success.png` | 清华源 19 个包 `Successfully installed` | 陌生人测试 5.2 证据 |
| `day18-make-import-pass.png` | make_data 8 组检查全过 + import_data 9 表 10651 行外键通过 | 陌生人测试 5.3 证据 |
| `day18-backend-running.png` | `Running on http://127.0.0.1:5000` | 陌生人测试 5.4 证据 |
| `day18-frontend-5-buttons.png` | 前端 5 个按钮实测（4 个实时 + 找教室离线示例降级） | 陌生人测试 5.4 证据 |

> 配图说明：正文以上面的文字与代码块为准，截图只作辅助证据（README 规范）。
> 截图均为真机操作实拍：陌生人在全新目录里按 README 从零跑通，最后前端 5 按钮验证通过。
