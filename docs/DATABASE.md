# CampusAI 数据库设计（SQLite）

- 数据库文件：`data/campus.db`（遵循 AGENTS.md Data Rules）
- 表数量：固定 9 张
- 设计原则：简单优先、不加维度表、不用触发器；事实数据入库，计算与预测放在 Python
- 一个学期一份数据：`schedules.semester` 用于区分学期，查询时由 Python 固定传入

约定：
- 主键统一用 `INTEGER PRIMARY KEY AUTOINCREMENT`
- 布尔用 `INTEGER` + `CHECK (x IN (0,1))`（SQLite 无原生 BOOLEAN）
- 时间用 `TEXT`，格式 `HH:MM`；日期用 `TEXT`，格式 `YYYY-MM-DD`（SQLite date 函数可直接处理）
- 枚举值用 `TEXT` + `CHECK` 约束，避免额外字典表
- 外键需要 `PRAGMA foreign_keys = ON;`

---

## 1. classrooms（教室）

| 字段名 | 数据类型 | 主键 | 外键 | 允许为空 | 字段含义 |
|---|---|---|---|---|---|
| room_id | INTEGER | PK | - | 否 | 教室主键，自增 |
| room_name | TEXT | - | - | 否 | 教室名称/编号，如「A101」，唯一 |
| building | TEXT | - | - | 否 | 所在楼栋，如「第一教学楼」 |
| floor | INTEGER | - | - | 是 | 楼层，用于「低楼层优先」排序 |
| capacity | INTEGER | - | - | 否 | 容纳人数，用于「人数」筛选，必须 > 0 |
| has_projector | INTEGER | - | - | 否 | 是否有投影，1=有 0=无，默认 0 |
| has_computer | INTEGER | - | - | 否 | 是否有电脑，1=有 0=无，默认 0 |
| room_type | TEXT | - | - | 否 | 教室类型：普通/多媒体/机房/实验室，默认「普通」 |
| status | TEXT | - | - | 否 | 教室状态：available/maintenance，默认 available |

## 2. courses（课程）

| 字段名 | 数据类型 | 主键 | 外键 | 允许为空 | 字段含义 |
|---|---|---|---|---|---|
| course_id | INTEGER | PK | - | 否 | 课程主键，自增 |
| course_code | TEXT | - | - | 否 | 课程代码，如「CS101」，唯一 |
| course_name | TEXT | - | - | 否 | 课程名称 |
| department | TEXT | - | - | 是 | 开课院系/专业，产教融合按专业分析用 |
| credit | REAL | - | - | 是 | 学分，必须 >= 0 |
| course_type | TEXT | - | - | 否 | 课程性质：必修/选修/实践，默认「必修」 |

## 3. schedules（排课/占用记录）

| 字段名 | 数据类型 | 主键 | 外键 | 允许为空 | 字段含义 |
|---|---|---|---|---|---|
| schedule_id | INTEGER | PK | - | 否 | 排课主键，自增 |
| course_id | INTEGER | - | FK → courses.course_id | 否 | 占用该时段的课程 |
| room_id | INTEGER | - | FK → classrooms.room_id | 否 | 被占用的教室 |
| semester | TEXT | - | - | 否 | 学期，如「2025-2026-1」 |
| weekday | INTEGER | - | - | 否 | 星期几，1=周一 … 7=周日 |
| period_start | INTEGER | - | - | 否 | 起始节次，1..12 |
| period_end | INTEGER | - | - | 否 | 结束节次，1..12，且 >= period_start |
| start_time | TEXT | - | - | 否 | 开始时间 HH:MM，如 08:00 |
| end_time | TEXT | - | - | 否 | 结束时间 HH:MM，如 09:40 |
| week_range | TEXT | - | - | 是 | 上课周次，如「1-16」或「1-16单」 |
| teacher | TEXT | - | - | 是 | 任课教师 |

> 「教室当前是否空闲」不落库，由 `classrooms` 左连接该时段的 `schedules` 实时算出，避免冗余状态不一致。

## 4. canteens（食堂）

| 字段名 | 数据类型 | 主键 | 外键 | 允许为空 | 字段含义 |
|---|---|---|---|---|---|
| canteen_id | INTEGER | PK | - | 否 | 食堂主键，自增 |
| canteen_name | TEXT | - | - | 否 | 食堂名称，唯一 |
| location | TEXT | - | - | 是 | 位置描述，如「东区宿舍旁」 |
| capacity | INTEGER | - | - | 否 | 座位/同时在店容量，算拥挤度用 |
| window_count | INTEGER | - | - | 是 | 窗口数量，解释等待时间用 |
| open_time | TEXT | - | - | 是 | 开始营业时间 HH:MM |
| close_time | TEXT | - | - | 是 | 结束营业时间 HH:MM |
| status | TEXT | - | - | 否 | 状态：open/closed，默认 open |

## 5. meal_records（食堂人流记录）

| 字段名 | 数据类型 | 主键 | 外键 | 允许为空 | 字段含义 |
|---|---|---|---|---|---|
| record_id | INTEGER | PK | - | 否 | 记录主键，自增 |
| canteen_id | INTEGER | - | FK → canteens.canteen_id | 否 | 所属食堂 |
| record_date | TEXT | - | - | 否 | 日期 YYYY-MM-DD |
| record_hour | INTEGER | - | - | 否 | 小时，0..23，按小时分组用 |
| time_slot | TEXT | - | - | 否 | 时段：breakfast/lunch/dinner/other |
| people_count | INTEGER | - | - | 否 | 该时段就餐人数，>= 0 |
| wait_minutes | REAL | - | - | 是 | 平均等待时间（分钟），>= 0 |
| capacity | INTEGER | - | - | 是 | 采集时该食堂容量快照，算拥挤度用 |
| source | TEXT | - | - | 否 | 数据来源：simulated/real，默认 simulated |
| created_at | TEXT | - | - | 否 | 入库时间，默认 CURRENT_TIMESTAMP |

同一食堂 + 日期 + 小时 唯一（`UNIQUE(canteen_id, record_date, record_hour)`），保证重复导入不会翻倍。

## 6. jobs（企业岗位）

| 字段名 | 数据类型 | 主键 | 外键 | 允许为空 | 字段含义 |
|---|---|---|---|---|---|
| job_id | INTEGER | PK | - | 否 | 岗位主键，自增 |
| job_title | TEXT | - | - | 否 | 岗位名称，如「Python 后端开发」 |
| company | TEXT | - | - | 否 | 企业名称 |
| industry | TEXT | - | - | 是 | 所属行业 |
| city | TEXT | - | - | 是 | 工作城市 |
| education | TEXT | - | - | 是 | 学历要求：大专/本科/硕士/不限 |
| experience | TEXT | - | - | 是 | 经验要求：应届/1-3年/3-5年/不限 |
| salary_min | INTEGER | - | - | 是 | 月薪下限（元） |
| salary_max | INTEGER | - | - | 是 | 月薪上限（元），>= salary_min |
| major | TEXT | - | - | 是 | 目标专业，与 courses.department 对应 |
| job_desc | TEXT | - | - | 是 | 岗位描述原文，抽取技能的依据 |
| publish_date | TEXT | - | - | 是 | 发布日期 YYYY-MM-DD |

## 7. skills（技能）

| 字段名 | 数据类型 | 主键 | 外键 | 允许为空 | 字段含义 |
|---|---|---|---|---|---|
| skill_id | INTEGER | PK | - | 否 | 技能主键，自增 |
| skill_name | TEXT | - | - | 否 | 技能名称，如「Python」「SQL」，唯一 |
| skill_category | TEXT | - | - | 否 | 分类：编程语言/数据库/框架/工具/软技能/其他，默认「其他」 |
| description | TEXT | - | - | 是 | 技能说明 |

## 8. course_skills（课程—技能，多对多）

| 字段名 | 数据类型 | 主键 | 外键 | 允许为空 | 字段含义 |
|---|---|---|---|---|---|
| id | INTEGER | PK | - | 否 | 主键，自增 |
| course_id | INTEGER | - | FK → courses.course_id | 否 | 课程 |
| skill_id | INTEGER | - | FK → skills.skill_id | 否 | 该课程覆盖的技能 |
| level | TEXT | - | - | 否 | 覆盖程度：了解/掌握/熟练，默认「掌握」 |
| UNIQUE(course_id, skill_id) | - | - | - | - | 防止同一课程重复挂同一技能 |

## 9. job_skills（岗位—技能，多对多）

| 字段名 | 数据类型 | 主键 | 外键 | 允许为空 | 字段含义 |
|---|---|---|---|---|---|
| id | INTEGER | PK | - | 否 | 主键，自增 |
| job_id | INTEGER | - | FK → jobs.job_id | 否 | 岗位 |
| skill_id | INTEGER | - | FK → skills.skill_id | 否 | 岗位要求的技能 |
| importance | INTEGER | - | - | 否 | 重要度 1..5，越大越关键，默认 3；覆盖率加权用 |
| is_required | INTEGER | - | - | 否 | 是否硬性要求，1=必须 0=加分项，默认 1 |
| UNIQUE(job_id, skill_id) | - | - | - | - | 防止同一岗位重复挂同一技能 |

---

## 三、三大功能如何使用这些表

### 1. 教室查询（场景一：AI 智能找教室）

意图参数 → 表的映射：

| 用户提问里的条件 | 落到哪里 |
|---|---|
| 人数「能坐 50 人」 | `classrooms.capacity >= 50` |
| 投影 / 电脑 | `classrooms.has_projector` / `classrooms.has_computer` |
| 日期 + 时间（如「周三 3-4 节」） | `schedules.weekday / period_start / period_end` |
| 「空闲」 | `schedules` 中该时段**没有**记录 |
| 教室类型（机房/实验室） | `classrooms.room_type` |

查询流程（三步，全部参数化）：

1. **候选集**：从 `classrooms` 按硬条件筛选
   ```sql
   SELECT room_id, room_name, building, capacity
   FROM classrooms
   WHERE capacity >= ? AND has_projector >= ? AND has_computer >= ? AND status = 'available';
   ```
2. **冲突集**：找出该时段已被占用的教室（用结束节次互斥判断，覆盖「3-4 节」和「2-3 节」交叉的情况）
   ```sql
   SELECT room_id FROM schedules
   WHERE semester = ? AND weekday = ?
     AND period_start <= ? AND period_end >= ?;
   ```
3. **差集**：`候选集 - 冲突集`，由 Python 做集合差，再按 `capacity` 升序（刚好够用）、`floor` 升序排序，返回前 N 个推荐。

要点：教室是否空闲是**查询时计算**出来的，不存状态字段，因此不会出现「排课更新了但空闲标记没更新」的脏数据。`idx_schedules_time(semester, weekday, period_start, period_end)` 保证第 2 步走索引。

### 2. 食堂分析（场景二：食堂拥挤分析与预测）

数据全部来自 `meal_records`（事实）+ `canteens`（维度）。

- **历史人流**：按天/按时段聚合
  ```sql
  SELECT record_date, SUM(people_count) AS people
  FROM meal_records
  WHERE canteen_id = ? AND record_date BETWEEN ? AND ?
  GROUP BY record_date ORDER BY record_date;
  ```
- **高峰时段**：按小时求平均人数，取最大值所在小时
  ```sql
  SELECT record_hour, AVG(people_count) AS avg_people
  FROM meal_records
  WHERE canteen_id = ? AND record_date >= ?
  GROUP BY record_hour ORDER BY avg_people DESC;
  ```
- **平均等待时间**：按 `time_slot` 聚合 `wait_minutes`，并与 `people_count` 一起返回，便于解释「人多 → 等得久」。
- **拥挤度**：`people_count / canteens.capacity`（或记录里的 `capacity` 快照），归一化为 0-1，前端用颜色区分。
- **未来预测**：Python 用同星期几 + 同时段的历史均值（或简单移动平均/线性外推）预测下一天人流，**不在 SQL 里做**（AGENTS.md：预测归 Python）。预测只需要读上面「按 weekday/hour 聚合」的结果集。

数据库职责边界：SQLite 只提供事实与聚合；比较、预测、拥挤等级判定都在 Python；ECharts 画折线（日期-人数）、柱状（时段-等待时间）、热力图（星期×小时）。

### 3. 产教融合（场景三：岗位技能 ↔ 课程技能匹配）

用 `jobs` / `skills` / `job_skills` / `courses` / `course_skills` 五张表，`skills` 是唯一的连接枢纽。

- **某专业课程覆盖了哪些技能**：
  ```sql
  SELECT DISTINCT s.skill_id, s.skill_name
  FROM courses c
  JOIN course_skills cs ON cs.course_id = c.course_id
  JOIN skills s        ON s.skill_id   = cs.skill_id
  WHERE c.department = ?;
  ```
- **某岗位要求哪些技能**（按重要度排序）：
  ```sql
  SELECT s.skill_id, s.skill_name, js.importance, js.is_required
  FROM job_skills js JOIN skills s ON s.skill_id = js.skill_id
  WHERE js.job_id = ? ORDER BY js.importance DESC;
  ```
- **技能覆盖率与缺口**：Python 取两个 `skill_id` 集合，做集合运算
  - 覆盖技能 = 岗位技能 ∩ 课程技能
  - 缺口技能 = 岗位技能 − 课程技能
  - 覆盖率 = 覆盖数 / 岗位技能总数（加权版：分子分母都用 `job_skills.importance` 求和）
  - 硬性要求（`is_required = 1`）中未覆盖的，单独标为「必须补齐的缺口」
- **反向分析（专业建设建议）**：对某专业所有对口岗位的缺口技能按出现次数排序，得到最该新增的课程内容。

---

## 四、完整 CREATE TABLE SQL

```sql
PRAGMA foreign_keys = ON;

-- 1. 教室
CREATE TABLE IF NOT EXISTS classrooms (
    room_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    room_name     TEXT    NOT NULL UNIQUE,
    building      TEXT    NOT NULL,
    floor         INTEGER,
    capacity      INTEGER NOT NULL CHECK (capacity > 0),
    has_projector INTEGER NOT NULL DEFAULT 0 CHECK (has_projector IN (0, 1)),
    has_computer  INTEGER NOT NULL DEFAULT 0 CHECK (has_computer  IN (0, 1)),
    room_type     TEXT    NOT NULL DEFAULT '普通'
                  CHECK (room_type IN ('普通', '多媒体', '机房', '实验室')),
    status        TEXT    NOT NULL DEFAULT 'available'
                  CHECK (status IN ('available', 'maintenance'))
);

-- 2. 课程
CREATE TABLE IF NOT EXISTS courses (
    course_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    course_code TEXT    NOT NULL UNIQUE,
    course_name TEXT    NOT NULL,
    department  TEXT,
    credit      REAL    CHECK (credit IS NULL OR credit >= 0),
    course_type TEXT    NOT NULL DEFAULT '必修'
                CHECK (course_type IN ('必修', '选修', '实践'))
);

-- 3. 排课 / 教室占用
CREATE TABLE IF NOT EXISTS schedules (
    schedule_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id    INTEGER NOT NULL REFERENCES courses(course_id)    ON DELETE CASCADE,
    room_id      INTEGER NOT NULL REFERENCES classrooms(room_id)   ON DELETE CASCADE,
    semester     TEXT    NOT NULL,
    weekday      INTEGER NOT NULL CHECK (weekday BETWEEN 1 AND 7),
    period_start INTEGER NOT NULL CHECK (period_start BETWEEN 1 AND 12),
    period_end   INTEGER NOT NULL CHECK (period_end BETWEEN 1 AND 12),
    start_time   TEXT    NOT NULL,
    end_time     TEXT    NOT NULL,
    week_range   TEXT,
    teacher      TEXT,
    CHECK (period_end >= period_start)
);

-- 4. 食堂
CREATE TABLE IF NOT EXISTS canteens (
    canteen_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    canteen_name TEXT    NOT NULL UNIQUE,
    location     TEXT,
    capacity     INTEGER NOT NULL CHECK (capacity > 0),
    window_count INTEGER CHECK (window_count IS NULL OR window_count > 0),
    open_time    TEXT,
    close_time   TEXT,
    status       TEXT    NOT NULL DEFAULT 'open'
                 CHECK (status IN ('open', 'closed'))
);

-- 5. 食堂人流记录
CREATE TABLE IF NOT EXISTS meal_records (
    record_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    canteen_id   INTEGER NOT NULL REFERENCES canteens(canteen_id) ON DELETE CASCADE,
    record_date  TEXT    NOT NULL,
    record_hour  INTEGER NOT NULL CHECK (record_hour BETWEEN 0 AND 23),
    time_slot    TEXT    NOT NULL
                 CHECK (time_slot IN ('breakfast', 'lunch', 'dinner', 'other')),
    people_count INTEGER NOT NULL CHECK (people_count >= 0),
    wait_minutes REAL    CHECK (wait_minutes IS NULL OR wait_minutes >= 0),
    capacity     INTEGER CHECK (capacity IS NULL OR capacity > 0),
    source       TEXT    NOT NULL DEFAULT 'simulated'
                 CHECK (source IN ('simulated', 'real')),
    created_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (canteen_id, record_date, record_hour)
);

-- 6. 企业岗位
CREATE TABLE IF NOT EXISTS jobs (
    job_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_title    TEXT    NOT NULL,
    company      TEXT    NOT NULL,
    industry     TEXT,
    city         TEXT,
    education    TEXT,
    experience   TEXT,
    salary_min   INTEGER CHECK (salary_min IS NULL OR salary_min >= 0),
    salary_max   INTEGER CHECK (salary_max IS NULL OR salary_max >= 0),
    major        TEXT,
    job_desc     TEXT,
    publish_date TEXT,
    CHECK (salary_max IS NULL OR salary_min IS NULL OR salary_max >= salary_min)
);

-- 7. 技能
CREATE TABLE IF NOT EXISTS skills (
    skill_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name     TEXT NOT NULL UNIQUE,
    skill_category TEXT NOT NULL DEFAULT '其他'
                   CHECK (skill_category IN ('编程语言', '数据库', '框架', '工具', '软技能', '其他')),
    description    TEXT
);

-- 8. 课程—技能
CREATE TABLE IF NOT EXISTS course_skills (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
    skill_id  INTEGER NOT NULL REFERENCES skills(skill_id)   ON DELETE CASCADE,
    level     TEXT    NOT NULL DEFAULT '掌握'
              CHECK (level IN ('了解', '掌握', '熟练')),
    UNIQUE (course_id, skill_id)
);

-- 9. 岗位—技能
CREATE TABLE IF NOT EXISTS job_skills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(job_id)     ON DELETE CASCADE,
    skill_id    INTEGER NOT NULL REFERENCES skills(skill_id) ON DELETE CASCADE,
    importance  INTEGER NOT NULL DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
    is_required INTEGER NOT NULL DEFAULT 1 CHECK (is_required IN (0, 1)),
    UNIQUE (job_id, skill_id)
);

-- 索引：支撑三大场景的查询
CREATE INDEX IF NOT EXISTS idx_schedules_time   ON schedules (semester, weekday, period_start, period_end);
CREATE INDEX IF NOT EXISTS idx_schedules_room   ON schedules (room_id);
CREATE INDEX IF NOT EXISTS idx_schedules_course ON schedules (course_id);
CREATE INDEX IF NOT EXISTS idx_classrooms_cap   ON classrooms (capacity);
CREATE INDEX IF NOT EXISTS idx_meal_time        ON meal_records (canteen_id, record_date, record_hour);
CREATE INDEX IF NOT EXISTS idx_meal_slot        ON meal_records (canteen_id, time_slot);
CREATE INDEX IF NOT EXISTS idx_course_skills    ON course_skills (skill_id);
CREATE INDEX IF NOT EXISTS idx_job_skills       ON job_skills (skill_id);
CREATE INDEX IF NOT EXISTS idx_jobs_major       ON jobs (major);
```

---

## 五、设计取舍说明

1. **不加字典表**：`weekday`、`time_slot`、`room_type`、`skill_category` 都用 `TEXT/INTEGER` + `CHECK`，把表数严格控制在 9 张，符合 AGENTS.md「优先使用简单架构」。
2. **不存派生字段**：教室空闲、拥挤度、覆盖率都不落库，避免数据不一致；分别在查询、Python 计算时得到。
3. **`meal_records` 同时存日期与小时**：避免只存 `datetime` 后每次聚合都要 `strftime`，也让按小时分组的索引直接可用。
4. **多对多只做两跳**：岗位与课程之间不建直连表，全部经 `skills` 关联，这样「任取一个岗位 × 任取一个专业」都能现算，不用预生成组合。
5. **`importance` 是覆盖率的权重来源**：让「核心技能缺失」比「加分技能缺失」扣分更多，比单纯计数更贴合产教融合语义。
6. **日期/时间统一 `TEXT` 格式**：SQLite 的 `date()`、`strftime()` 可直接处理，Python 侧读写也无需转换。
7. **后续如需迁移**：`courses.department` 与 `jobs.major` 是文本而非外键，若将来要精确对应，需新增专业表（会破坏「固定 9 张表」约束，属于下一阶段决策）。
