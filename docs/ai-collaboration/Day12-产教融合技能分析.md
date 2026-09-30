# AI协作记录：产教融合技能分析（Day 12）

## 1. 今日任务
实现产教融合技能分析：把**企业岗位技能需求**与**专业课程技能覆盖**做匹配，输出
①技能需求 ②已覆盖技能 ③缺失技能 ④技能覆盖率 ⑤技能重要程度，核心方法 `match_job_to_major()` 与 `calculate_skill_gap()`，实现落在 `backend/industry.py`。

**严格约束**：所有匹配计算由 Python 完成；不让 AI 计算覆盖率；AI 不能修改计算结果；AI 只能解释最终结果；至少 8 个测试；不修改教室功能；不修改食堂功能。

## 2. 架构落地与职责分离

*   **数据底座（只筛选，不算数）**：新增 `queries.industry_skill_grid()`，只做条件筛选与字段回填，返回「各岗位的技能明细」+「该专业课程覆盖的技能明细」。**纯新增，未改动 `queries.py` 任何已有函数。**
*   **Python 负责全部计算**：新增 `backend/industry.py`
    *   `match_job_to_major(job, coverage)`：单个岗位 × 一个专业，做集合运算（∩ / −）、算覆盖率与重要程度档位；
    *   `calculate_skill_gap(jobs, coverage)`：一个专业 × 全部对口岗位，按技能聚合（岗位数、最大重要度、硬性要求），产出五输出的总表。
    *   模块**不 import sqlite3、不写 SQL**（有源码级测试守着），唯一取数入口是 `queries.industry_skill_grid`。
*   **AI 只解释**：新增 `prompts/industry_insight_generator.md`（正式 Prompt 走 Git 留痕）与 `IndustryExplainer`；AI 只拿到 `build_insight_payload()` 生成的**只读事实表**（无连接、无 SQL、无表名、无原始明细），失败一律降级为 Python 兜底文案。
*   **接口**：新增 `GET /api/industry/match`（21 行，纯新增），老的 `GET /api/industry/gap` 一行未改。

## 3. 四个「AI 红线」是怎么被锁死的
1.  **AI 不算覆盖率**：事实表只给已算好的数字（`coverage` / `importance`），不给任何原料（无逐岗位明细、无 `skill_id`、无课程明细）。测试断言 payload 的键集合固定且不含 `job_matches` / 表名 / `campus.db` / `sqlite`。
2.  **AI 不能修改计算结果**：`results_fingerprint()` 对整个计算结果做 SHA-256，`analyze_skill_gap()` 在解释前后各取一次指纹，不一致直接抛 `RuntimeError`。测试用一个「恶意解释器」（既就地篡改事实表、又返回自编 `coverage_rate: 1.0`）证明报告纹丝不动。
3.  **事实表是副本**：`build_insight_payload()` 里每条技能都新建 dict，模型改它碰不到报告（有专门测试）。
4.  **AI 只能解释最终结果**：`explain_report()` 任何异常都降级，`explain=False` 时根本不构造解释器（用 monkeypatch 成「一调用就失败」来证明）。

## 4. 自测中发现并修复 1 个真实 Bug
`min_importance` 的口径错了：最初把门槛作用在「岗位 × 技能」上再聚合，导致**只被低重要度岗位标成硬性要求的技能丢掉 `is_required`**，硬性缺口被悄悄漏掉。真实库「网络工程」在 `min_importance=4` 时 `required_missing_count` 算出 6，而独立参考实现是 7。

修复：门槛改成**技能级**（与 `queries.industry_skill_gap` 同口径）——先把全部岗位汇总，再用技能的最大重要度决定它是否进入覆盖率与清单；`job_count` / `is_required` / 平均重要度始终反映全部岗位的事实。并补了一条专门的回归测试 `test_min_importance_keeps_all_job_evidence_for_kept_skills`。

## 5. 真实数据人工验证（核心证据）
在 `data/campus.db` 的「软件工程」上跑真实链路：

| 指标 | 本模块 `/api/industry/match` | 老接口 `/api/industry/gap` |
|---|---|---|
| 技能需求数 | 17 | 17 ✅ |
| 已覆盖技能数 | 10 | 10 ✅ |
| 缺失技能数 | 7 | 7 ✅ |
| 硬性要求缺失数 | 7 | 7 ✅ |
| 技能覆盖率 | 0.5882 | 0.5882 ✅ |
| 重要度加权覆盖率 | 0.6081 | 0.6081 ✅ |
| 课程贴合度 | 0.871 | 0.871 ✅ |

两个独立实现逐项一致，且**已覆盖/缺失技能名单的集合完全相同**，互为交叉校验。
规模：17 个对口岗位、13 门课程、12 项课程技能；重要程度 `avg = 4.35`（档位「重要」），核心技能 10 项。
兜底解释：`专业「软件工程」对 17 个对口岗位统计出 17 项技能需求，课程覆盖 10 项、缺失 7 项，技能覆盖率 58.8%（重要度加权 60.8%）。`

## 6. 测试
*   新增 `tests/test_industry.py`：**49 个用例**（远超「至少 8 个」的要求），覆盖两个核心方法、五个输出、参数校验、404/400、真实库独立复算、AI 四条红线、源码级红线、教室/食堂回归。
*   全量回归：**565 passed**（Day11 为 516 passed，516 + 49 = 565）。

## 7. 剩余风险
*   `data_summary` / `job_matches` 让响应体偏大（100 个岗位 × 每岗位技能明细）；已用 `top` 截断三张清单，如需进一步瘦身可加 `include_job_matches` 开关。
*   `importance_level` 的五档划分（核心/重要/较高/一般/次要）是本项目自定的展示口径，不是行业标准；改档位只需改 `IMPORTANCE_LEVELS` 一处。
*   覆盖率目前只区分「覆盖/未覆盖」，不对「了解级覆盖」降权（加权覆盖率用的是岗位重要度，不是课程覆盖程度）；`summary.course_covered_ratio` 是唯一的课程侧反向指标。
*   AI 解释质量依赖上游模型；不可用时降级为 `source: "local"`，接口始终 200。
*   教室功能（`backend/classroom.py`、`queries.list_available_classrooms`）与食堂功能（`backend/forecast.py`、`queries.canteen_*`）**一行未改**（`git diff` 为空），并有回归测试守着。

## 8. Git 提交
`feat: add industry-education skill gap analysis (python-only matching + AI explanation)`
