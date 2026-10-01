#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CampusAI 模拟数据生成器（scripts/make_data.py）。

用法：
    python scripts/make_data.py

产物（写入 data/raw/，列名与 docs/DATABASE.md 的字段名一一对应）：
    classrooms.csv      50   个教室
    courses.csv         100  门课程
    schedules.csv       500+ 条课程安排（同一教室同一时间不冲突）
    canteens.csv        5    个食堂
    meal_records.csv    3000 条就餐记录（严格落在各食堂营业时间内 + 午餐高峰）
    jobs.csv            100  个企业岗位
    skills.csv          30   个技能
    course_skills.csv   300  条课程—技能关系
    job_skills.csv      400  条岗位—技能关系

约定：
    1. 只使用 Python 标准库（random / csv / hashlib / datetime），不使用 Faker。
    2. 固定随机种子 random.seed(42)，每次运行生成完全一致的数据（字节级可复现）。
    3. 自增主键显式写入 CSV，便于导入时直接建立外键关系；
       meal_records.created_at 由数据库 DEFAULT CURRENT_TIMESTAMP 填充，
       不写入 CSV（否则会破坏可复现性）。
    4. 文件编码 UTF-8（无 BOM）、换行 LF，跨平台字节一致。
    5. 脚本结束前自动执行 8 组检查并打印结果，任一检查失败退出码为 1。
"""

import csv
import hashlib
import os
import random
import sys
from collections import defaultdict
from datetime import date, timedelta

# ---------------------------------------------------------------------------
# 一、基础配置
# ---------------------------------------------------------------------------

SEED = 42

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(BASE_DIR, "data", "raw")

SEMESTER = "2025-2026-1"
SEMESTER_SPRING = "2025-2026-2"

# 数据时间窗口（秋季学期）：2025-09-01(周一) ~ 2025-11-14(周五)，共 75 天
# = 55 个工作日 + 20 个周末日（其中 10-01~10-07 为国庆假期）
TERM_START = date(2025, 9, 1)
TERM_END = date(2025, 11, 14)
HOLIDAYS = {date(2025, 10, 1) + timedelta(days=i) for i in range(7)}

# 春季学期窗口：2026-03-01 ~ 2026-07-15（137 天）
# 含清明（04-04~04-06）与劳动节（05-01~05-05）两个假期，用于区分 holiday / weekend / weekday 客流形状。
SPRING_TERM_START = date(2026, 3, 1)
SPRING_TERM_END = date(2026, 7, 15)
SPRING_HOLIDAYS = (
    {date(2026, 4, 4) + timedelta(days=i) for i in range(3)}
    | {date(2026, 5, 1) + timedelta(days=i) for i in range(5)}
)

MIN_SCHEDULES = 520          # 课表条数下限（需求要求 ≥ 500）
PEAK_OCCUPANCY = 0.62        # 午餐高峰座位占用系数，保证 people_count <= capacity

# 节次时间表（1..12 节），排课只使用 1..10 节（上午 08:00 ~ 晚上 20:40）
PERIOD_START_TIME = {
    1: "08:00", 2: "08:55", 3: "10:00", 4: "10:55", 5: "14:00",
    6: "14:55", 7: "16:00", 8: "16:55", 9: "19:00", 10: "19:55",
    11: "20:50", 12: "21:45",
}
PERIOD_END_TIME = {
    1: "08:45", 2: "09:40", 3: "10:45", 4: "11:40", 5: "14:45",
    6: "15:40", 7: "16:45", 8: "17:40", 9: "19:45", 10: "20:40",
    11: "21:35", 12: "22:30",
}
TWO_PERIOD_BLOCKS = [(1, 2), (3, 4), (5, 6), (7, 8), (9, 10)]   # 理论课 2 节连排
FOUR_PERIOD_BLOCKS = [(1, 4), (5, 8)]                            # 实践课 4 节连排

# ---------------------------------------------------------------------------
# 二、静态业务字典
# ---------------------------------------------------------------------------

# 教学楼：(楼名, 房间前缀, 楼层数, [(教室类型, 数量)])
BUILDING_SPECS = [
    ("第一教学楼", "A", 5, [("普通", 10), ("多媒体", 4)]),
    ("第二教学楼", "B", 4, [("普通", 8), ("多媒体", 3)]),
    ("第三教学楼", "C", 4, [("普通", 2), ("多媒体", 8)]),
    ("实验楼", "D", 3, [("实验室", 7), ("机房", 2)]),
    ("综合楼", "E", 4, [("机房", 6)]),
]

# 每种教室类型的容量池（容量个数与上面需要的数量完全一致，保证属性合理）
CAPACITY_POOLS = {
    "普通": [100, 90, 90, 80, 80, 80, 70, 70, 70, 70, 60, 60, 60, 55, 55, 50, 50, 45, 45, 40],
    "多媒体": [200, 180, 150, 150, 120, 120, 120, 100, 100, 100, 80, 80, 80, 60, 60],
    "机房": [60, 60, 55, 55, 50, 50, 45, 45],
    "实验室": [50, 45, 45, 40, 40, 36, 30],
}
CAPACITY_RANGE = {"普通": (40, 100), "多媒体": (60, 200), "机房": (45, 60), "实验室": (30, 50)}
ROOM_TYPES = ("普通", "多媒体", "机房", "实验室")

DEPARTMENTS = [
    ("计算机科学与技术", "CS"),
    ("软件工程", "SE"),
    ("人工智能", "AI"),
    ("数据科学与大数据技术", "DS"),
    ("网络工程", "NE"),
    ("电子信息工程", "EE"),
    ("自动化", "AU"),
    ("信息管理与信息系统", "IS"),
]
# 每个专业的课程数量，合计 100
DEPARTMENT_COURSE_COUNT = {
    "计算机科学与技术": 14, "软件工程": 13, "人工智能": 13, "数据科学与大数据技术": 13,
    "网络工程": 12, "电子信息工程": 12, "自动化": 12, "信息管理与信息系统": 11,
}

# 各专业课程名池（数量均大于该专业所需课程数）
COURSE_NAMES = {
    "计算机科学与技术": [
        "数据结构", "计算机组成原理", "操作系统", "计算机网络", "编译原理", "离散数学",
        "计算机导论", "汇编语言程序设计", "计算机体系结构", "算法设计与分析",
        "面向对象程序设计", "C语言程序设计", "数据库系统原理", "计算机图形学",
        "并行计算", "计算机系统安全",
    ],
    "软件工程": [
        "软件工程导论", "软件需求工程", "软件设计与体系结构", "软件测试技术", "软件项目管理",
        "人机交互设计", "面向对象分析与设计", "Web应用开发", "移动应用开发", "敏捷开发实践",
        "软件质量保证", "软件工程经济学", "服务端开发技术", "前端开发技术", "软件文档写作",
    ],
    "人工智能": [
        "人工智能导论", "机器学习", "深度学习", "模式识别", "自然语言处理", "计算机视觉",
        "知识图谱", "智能机器人", "强化学习", "语音识别技术", "智能推荐系统",
        "认知科学导论", "机器学习实践", "深度学习框架应用", "博弈论与决策",
    ],
    "数据科学与大数据技术": [
        "大数据技术原理", "数据挖掘", "数据分析与建模", "统计学原理", "概率论与数理统计",
        "线性代数", "数据可视化技术", "分布式计算框架", "数据仓库与ETL", "时间序列分析",
        "商务智能", "云计算与大数据", "数据质量管理", "高等数学",
    ],
    "网络工程": [
        "网络工程导论", "网络协议分析", "网络编程技术", "网络安全技术", "网络管理与维护",
        "无线网络技术", "云计算技术", "密码学基础", "入侵检测技术", "物联网技术",
        "网络性能分析", "网络攻防实践", "信息安全管理",
    ],
    "电子信息工程": [
        "电路分析基础", "模拟电子技术", "数字电子技术", "信号与系统", "数字信号处理",
        "通信原理", "单片机原理与应用", "嵌入式系统设计", "电子设计自动化",
        "传感器与检测技术", "射频电路设计", "电子工艺实践", "电磁场与电磁波",
    ],
    "自动化": [
        "自动控制原理", "现代控制理论", "电机与拖动基础", "电力电子技术", "过程控制系统",
        "运动控制系统", "机器人控制技术", "检测技术与仪表", "PLC控制系统",
        "智能控制导论", "控制系统仿真", "工业互联网技术",
    ],
    "信息管理与信息系统": [
        "管理信息系统", "信息系统分析与设计", "数据库应用技术", "企业资源计划",
        "电子商务概论", "运筹学", "管理学原理", "经济学基础", "信息资源管理",
        "业务流程建模", "商务数据分析",
    ],
}
# 公共基础课：班级规模大，必须安排在容量 ≥ 120 的多媒体大教室
PUBLIC_COURSE_NAMES = {
    "高等数学", "线性代数", "概率论与数理统计", "离散数学", "统计学原理",
    "管理学原理", "经济学基础",
}

# 食堂基础档案（学校真实数据）
# 每个食堂包含：
#   name/location/capacity/window_count/open_time/close_time —— 写入 canteens.csv 的真实档案
#   share          —— 人气系数（仅生成用，不写入 CSV）
#   service_windows—— 真实营业时段（用于校验就餐记录必须落在营业时间内）
#   hours          —— 各日型实际采集就餐记录的小时（必须落在 service_windows 内）
# 说明：
#   * 一食堂 06:00-20:30 为正餐营业时间，部分档口延长至 21:30 供应夜宵（21 点记一条 other 记录）
#   * 琼阁苑餐厅 07:00-14:00、16:30-20:00，中间 14:00-16:30 闭餐休息，仅供应正餐，无夜宵
#   * 佳姐美食驿站 / 红房子餐饮驿站 全天 07:00-20:00，非正餐时段（10/15/16 点）也供餐
CANTEENS = [
    {
        "name": "一食堂",
        "location": "篮球场北侧，AB栋学生宿舍片区旁，东区宿舍核心区域",
        "capacity": 1200,
        "window_count": 32,
        "open_time": "06:00",
        "close_time": "20:30",
        "share": 1.00,
        "service_windows": (("06:00", "20:30"), ("20:30", "21:30")),
        "hours": {
            "weekday": (6, 11, 12, 13, 17, 18, 19, 21),
            "weekend": (8, 9, 11, 12, 13, 17, 18, 21),
            "holiday": (8, 9, 11, 12, 13, 17, 18, 19),
        },
    },
    {
        "name": "二食堂",
        "location": "篮球场东侧，紧邻AB栋宿舍群，与一食堂同属宿舍-篮球场片区",
        "capacity": 900,
        "window_count": 24,
        "open_time": "06:00",
        "close_time": "20:30",
        "share": 0.86,
        "service_windows": (("06:00", "20:30"),),
        "hours": {
            "weekday": (6, 7, 11, 12, 13, 17, 18, 19),
            "weekend": (8, 9, 11, 12, 13, 17, 18, 19),
            "holiday": (8, 9, 11, 12, 13, 17, 18, 19),
        },
    },
    {
        "name": "琼阁苑餐厅",
        "location": "正门保安处（保卫室）旁，入校主路侧边，紧靠校门入口",
        "capacity": 600,
        "window_count": 20,
        "open_time": "07:00",
        "close_time": "20:00",
        "share": 0.72,
        "service_windows": (("07:00", "14:00"), ("16:30", "20:00")),
        "hours": {
            "weekday": (7, 8, 11, 12, 13, 17, 18, 19),
            "weekend": (8, 9, 11, 12, 13, 17, 18, 19),
            "holiday": (8, 9, 11, 12, 13, 17, 18, 19),
        },
    },
    {
        "name": "佳姐美食驿站",
        "location": "正门邮政快递点旁，老校门片区，邮政网点同侧沿街",
        "capacity": 350,
        "window_count": 10,
        "open_time": "07:00",
        "close_time": "20:00",
        "share": 0.60,
        "service_windows": (("07:00", "20:00"),),
        "hours": {
            "weekday": (7, 10, 11, 12, 13, 15, 17, 18),
            "weekend": (8, 9, 11, 12, 13, 16, 17, 18),
            "holiday": (9, 11, 12, 13, 15, 16, 17, 18),
        },
    },
    {
        "name": "红房子餐饮驿站",
        "location": "正门邮政快递点周边，紧邻佳姐美食驿站，同属校门邮政商圈",
        "capacity": 400,
        "window_count": 12,
        "open_time": "07:00",
        "close_time": "20:00",
        "share": 0.52,
        "service_windows": (("07:00", "20:00"),),
        "hours": {
            "weekday": (8, 11, 12, 13, 16, 17, 18, 19),
            "weekend": (8, 9, 11, 12, 13, 16, 17, 18),
            "holiday": (9, 11, 12, 13, 16, 17, 18, 19),
        },
    },
]

# 就餐小时 → 时段（"other" 覆盖课间加餐、下午茶与夜宵）
HOUR_SLOT = {
    6: "breakfast", 7: "breakfast", 8: "breakfast", 9: "breakfast",
    10: "other",
    11: "lunch", 12: "lunch", 13: "lunch",
    15: "other", 16: "other",
    17: "dinner", 18: "dinner", 19: "dinner",
    21: "other",
}
# 人流形状系数：午餐 11:00-13:00 形成明显高峰（12 点最高），夜宵/非正餐时段明显偏低
BASE_FACTORS = {
    "weekday": {6: 0.22, 7: 0.30, 8: 0.36, 10: 0.35, 11: 0.85, 12: 1.00, 13: 0.62,
                15: 0.30, 16: 0.34, 17: 0.66, 18: 0.78, 19: 0.45, 21: 0.22},
    "weekend": {8: 0.20, 9: 0.30, 10: 0.28, 11: 0.72, 12: 0.92, 13: 0.70,
                15: 0.34, 16: 0.38, 17: 0.58, 18: 0.68, 19: 0.60, 21: 0.20},
    "holiday": {8: 0.12, 9: 0.18, 10: 0.16, 11: 0.42, 12: 0.55, 13: 0.40,
                15: 0.22, 16: 0.26, 17: 0.34, 18: 0.40, 19: 0.34, 21: 0.10},
}
DAY_FACTOR = {"weekday": (0.92, 1.08), "weekend": (0.90, 1.10), "holiday": (0.88, 1.12)}

# 技能：(名称, 分类, 说明)，共 30 个
SKILLS = [
    ("Python", "编程语言", "通用脚本与数据科学语言，用于后端开发与算法实现"),
    ("Java", "编程语言", "面向对象编程语言，企业级后端服务开发的主流语言"),
    ("C++", "编程语言", "高性能系统与算法开发语言，兼顾运行效率与抽象能力"),
    ("C", "编程语言", "贴近硬件的编程语言，嵌入式与系统底层开发的基础"),
    ("JavaScript", "编程语言", "浏览器与 Node 环境下的脚本语言，前端交互开发基础"),
    ("Go", "编程语言", "高并发服务端开发语言，语法简洁、部署方便"),
    ("SQL", "数据库", "结构化查询语言，数据查询、统计与报表的基础能力"),
    ("MySQL", "数据库", "最常用的关系型数据库，用于业务数据存储与查询优化"),
    ("Redis", "数据库", "内存键值数据库，用于缓存、会话与排行榜等场景"),
    ("MongoDB", "数据库", "文档型 NoSQL 数据库，适合半结构化数据存储"),
    ("Spring Boot", "框架", "Java 后端主流框架，用于快速构建 REST 服务"),
    ("Django", "框架", "Python 全栈 Web 框架，自带 ORM 与后台管理"),
    ("Flask", "框架", "Python 轻量 Web 框架，适合小型服务与接口开发"),
    ("Vue.js", "框架", "渐进式前端框架，用于构建单页应用与可视化页面"),
    ("React", "框架", "组件化前端框架，生态完善，适合复杂交互界面"),
    ("PyTorch", "框架", "深度学习框架，用于模型训练与推理实现"),
    ("Git", "工具", "分布式版本控制工具，团队协作与代码管理的基础"),
    ("Linux", "工具", "服务器操作系统，部署、运维与脚本自动化的基础"),
    ("Nginx", "工具", "高性能 Web 服务器与反向代理，用于部署与负载均衡"),
    ("Maven", "工具", "Java 项目构建与依赖管理工具"),
    ("Tableau", "工具", "商业数据可视化工具，用于报表与看板制作"),
    ("团队协作", "软技能", "跨角色沟通与配合，保证项目按计划推进"),
    ("沟通表达", "软技能", "清晰表达技术方案与业务需求的能力"),
    ("项目管理", "软技能", "任务拆解、进度控制与风险管理能力"),
    ("学习能力", "软技能", "快速掌握新技术与新业务的学习能力"),
    ("数据结构与算法", "其他", "程序设计基础能力，决定编码效率与问题建模水平"),
    ("机器学习", "其他", "经典机器学习方法，包括回归、分类与聚类"),
    ("深度学习", "其他", "神经网络建模方法，覆盖视觉、语音与文本任务"),
    ("数据分析", "其他", "数据清洗、指标拆解与结论输出的业务分析能力"),
    ("计算机网络", "其他", "网络协议与体系结构知识，网络排障与安全的基础"),
]
SKILL_CATEGORIES = ("编程语言", "数据库", "框架", "工具", "软技能", "其他")

# 专业 ↔ 技能池：课程技能与岗位技能都从这里抽取，保证「产教融合」匹配有意义
DEPARTMENT_SKILL_POOL = {
    "计算机科学与技术": [
        "Python", "Java", "C++", "C", "Go", "SQL", "MySQL", "Redis", "Git", "Linux",
        "数据结构与算法", "计算机网络",
    ],
    "软件工程": [
        "Java", "Python", "JavaScript", "Vue.js", "React", "Spring Boot", "Django",
        "MySQL", "Git", "Linux", "Maven", "项目管理", "团队协作",
    ],
    "人工智能": [
        "Python", "PyTorch", "机器学习", "深度学习", "数据分析", "C++",
        "数据结构与算法", "沟通表达",
    ],
    "数据科学与大数据技术": [
        "Python", "SQL", "MySQL", "MongoDB", "Flask", "数据分析", "机器学习",
        "深度学习", "Tableau", "数据结构与算法", "沟通表达",
    ],
    "网络工程": [
        "Python", "C", "Go", "SQL", "Linux", "Nginx", "计算机网络", "团队协作", "学习能力",
    ],
    "电子信息工程": ["C", "C++", "Python", "Linux", "数据结构与算法", "学习能力", "团队协作"],
    "自动化": ["C", "C++", "Python", "Linux", "数据结构与算法", "学习能力", "项目管理"],
    "信息管理与信息系统": [
        "SQL", "MySQL", "MongoDB", "Python", "数据分析", "Tableau",
        "项目管理", "沟通表达", "团队协作",
    ],
}

# 课程名关键词 → 优先技能：让课程挂载的技能与课程内容真正相关
# （技能必须同时出现在该专业的技能池里才会被采用；每组第一个是「必挂」的主技能，
#   规则顺序 = 优先级，越具体的规则越靠前）
COURSE_SKILL_AFFINITY = [
    (("机器学习", "深度学习", "神经网络", "模式识别", "计算机视觉", "自然语言处理",
      "语音识别", "强化学习", "智能推荐", "知识图谱", "人工智能", "智能机器人"),
     ("机器学习", "深度学习", "PyTorch", "Python")),
    (("数据库",), ("SQL", "MySQL", "MongoDB")),
    (("Web", "网页", "前端"), ("JavaScript", "Vue.js", "React")),
    (("移动应用", "移动端"), ("Java", "JavaScript", "Git")),
    (("服务端", "后端"), ("Java", "Python", "MySQL", "Spring Boot")),
    (("测试",), ("Python", "Git", "Java")),
    (("需求", "项目管理", "质量", "敏捷", "工程经济", "文档"), ("项目管理", "团队协作", "Git")),
    (("人机交互",), ("沟通表达", "JavaScript", "团队协作")),
    (("面向对象",), ("Java", "Python", "C++")),
    (("算法", "数据结构", "编译", "程序设计", "编程"), ("数据结构与算法", "C++", "Python")),
    (("操作系统", "计算机体系结构", "组成原理", "汇编", "并行", "分布式"),
     ("Linux", "C", "C++", "数据结构与算法")),
    (("软件设计", "体系结构", "软件工程"), ("Java", "项目管理", "团队协作")),
    (("计算机网络", "网络协议", "网络编程", "网络管理", "无线网络", "网络性能", "网络工程", "网络"),
     ("计算机网络", "Linux", "Nginx", "Python")),
    (("信息安全", "密码", "攻防", "入侵", "安全"), ("计算机网络", "Python", "Linux")),
    (("云计算", "物联网", "互联网", "虚拟化"), ("Linux", "Python", "计算机网络")),
    (("数据可视化", "可视化"), ("Tableau", "数据分析")),
    (("数据挖掘", "数据分析", "大数据", "商务智能", "数据仓库", "ETL", "数据质量", "时间序列",
      "统计", "概率", "数学", "线性代数", "运筹"),
     ("数据分析", "SQL", "Python", "Tableau")),
    (("嵌入式", "单片机", "传感器", "检测"), ("C", "Linux", "C++")),
    (("电路", "电子", "信号", "通信", "电磁", "射频"), ("C", "C++", "Python")),
    (("智能控制", "机器人"), ("C", "C++", "Python")),
    (("控制", "电机", "电力电子", "拖动"), ("C", "C++", "项目管理")),
    (("管理", "经济", "资源计划", "商务", "电子商务", "信息资源", "业务流程", "信息系统"),
     ("项目管理", "沟通表达", "数据分析", "SQL")),
]

# 岗位模板：(岗位名称, 行业, (目标专业...), (技能...), 职责)
JOB_TEMPLATES = [
    ("后端开发工程师", "软件开发", ("计算机科学与技术", "软件工程"),
     ("Java", "Python", "SQL", "MySQL", "Spring Boot", "Redis", "Linux", "Git", "数据结构与算法"),
     "负责服务端接口设计、业务逻辑开发与数据库表结构优化"),
    ("前端开发工程师", "软件开发", ("软件工程", "计算机科学与技术"),
     ("JavaScript", "Vue.js", "React", "Git", "沟通表达", "团队协作"),
     "负责管理后台与可视化页面的交互开发、组件封装与性能优化"),
    ("全栈开发工程师", "软件开发", ("软件工程", "计算机科学与技术"),
     ("JavaScript", "Java", "Python", "MySQL", "Vue.js", "Flask", "Git"),
     "负责小型业务系统从前端页面到后端接口的独立开发与上线"),
    ("算法工程师", "人工智能", ("人工智能", "计算机科学与技术", "数据科学与大数据技术"),
     ("Python", "PyTorch", "机器学习", "深度学习", "数据结构与算法", "C++"),
     "负责业务场景下的模型选型、训练调优与线上效果迭代"),
    ("机器学习工程师", "人工智能", ("人工智能", "数据科学与大数据技术"),
     ("Python", "机器学习", "PyTorch", "数据分析", "深度学习"),
     "负责特征工程、模型训练与推理服务的工程化落地"),
    ("计算机视觉工程师", "人工智能", ("人工智能", "计算机科学与技术"),
     ("Python", "深度学习", "PyTorch", "C++", "数据结构与算法"),
     "负责图像检测与识别算法的开发、部署与精度优化"),
    ("自然语言处理工程师", "人工智能", ("人工智能", "软件工程"),
     ("Python", "深度学习", "机器学习", "PyTorch", "沟通表达"),
     "负责文本分类、信息抽取与智能问答能力的研发"),
    ("数据分析师", "大数据", ("数据科学与大数据技术", "信息管理与信息系统"),
     ("Python", "SQL", "数据分析", "Tableau", "沟通表达", "MySQL"),
     "负责业务指标体系建设、专题分析与可视化报表输出"),
    ("大数据开发工程师", "大数据", ("数据科学与大数据技术", "计算机科学与技术"),
     ("Python", "SQL", "MySQL", "MongoDB", "Linux", "数据结构与算法"),
     "负责数据采集、清洗与离线计算任务的开发与调度"),
    ("数据仓库工程师", "大数据", ("数据科学与大数据技术", "信息管理与信息系统"),
     ("SQL", "MySQL", "数据分析", "Tableau", "项目管理", "Python"),
     "负责数仓分层建模、指标口径管理与数据质量监控"),
    ("网络工程师", "通信", ("网络工程", "计算机科学与技术"),
     ("计算机网络", "Linux", "Nginx", "Python", "团队协作", "学习能力"),
     "负责校园与企业网络的规划、配置、巡检与故障排查"),
    ("网络安全工程师", "网络安全", ("网络工程", "计算机科学与技术"),
     ("计算机网络", "Linux", "Python", "C", "学习能力", "项目管理"),
     "负责安全加固、漏洞扫描与安全事件应急响应"),
    ("嵌入式软件工程师", "智能制造", ("电子信息工程", "自动化"),
     ("C", "C++", "Linux", "数据结构与算法", "学习能力", "Python"),
     "负责嵌入式固件开发、驱动调试与设备联调"),
    ("自动化控制工程师", "智能制造", ("自动化", "电子信息工程"),
     ("C", "C++", "项目管理", "学习能力", "团队协作", "数据结构与算法"),
     "负责控制系统方案设计、PLC 程序开发与现场调试"),
    ("测试开发工程师", "软件开发", ("软件工程", "计算机科学与技术"),
     ("Java", "Python", "SQL", "Git", "沟通表达", "Linux"),
     "负责自动化测试框架搭建、接口测试与质量看板维护"),
    ("运维开发工程师", "云计算", ("网络工程", "计算机科学与技术"),
     ("Linux", "Nginx", "Python", "Git", "MySQL", "团队协作"),
     "负责服务部署、监控告警与运维自动化脚本开发"),
    ("信息技术支持工程师", "企业信息化", ("信息管理与信息系统", "网络工程"),
     ("SQL", "MySQL", "Linux", "沟通表达", "团队协作", "学习能力"),
     "负责内部系统运维支持、用户问题处理与操作培训"),
    ("数据产品专员", "大数据", ("信息管理与信息系统", "数据科学与大数据技术"),
     ("SQL", "数据分析", "Tableau", "沟通表达", "项目管理"),
     "负责数据需求梳理、指标定义与数据产品方案设计"),
    ("移动端开发工程师", "软件开发", ("软件工程", "网络工程"),
     ("Java", "JavaScript", "Git", "MySQL", "团队协作"),
     "负责移动应用的界面开发、接口联调与版本发布"),
    ("软件实施顾问", "企业信息化", ("信息管理与信息系统", "软件工程"),
     ("SQL", "MySQL", "项目管理", "沟通表达", "团队协作", "Linux"),
     "负责客户现场系统部署、业务流程配置与上线支持"),
]

COMPANIES = [
    "星火智能科技", "云启数据技术", "智联未来信息", "海纳科技", "知行软件",
    "数联科技", "灵犀智能", "恒远网络科技", "锐驰电子", "蓝鲸数据",
    "未来引擎科技", "元知信息", "光速云科技", "天工智造", "汇智软件",
    "星图数据", "启明智联", "微光信息", "长风科技", "万物互联科技",
]
CITIES = ["北京", "上海", "深圳", "广州", "杭州", "成都", "武汉", "南京", "西安", "苏州"]
CITY_WEIGHTS = [16, 15, 14, 10, 12, 8, 8, 6, 5, 6]
EDUCATIONS = ["本科", "本科", "本科", "本科", "硕士", "硕士", "大专", "不限"]
EXPERIENCES = ["应届", "应届", "1-3年", "1-3年", "3-5年", "不限"]
EDUCATION_BASE_SALARY = {"大专": 6500, "本科": 9500, "硕士": 13500, "不限": 8500}
EXPERIENCE_ADJUST = {"应届": -1500, "1-3年": 0, "3-5年": 4000, "不限": 500}
SALARY_SPREAD = [2000, 3000, 4000, 5000, 6000, 8000]

SURNAMES = list("王李张刘陈杨黄赵周吴徐孙马朱胡林郭何高罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛段雷侯龙史陶黎贺顾毛郝龚邵万钱严武戴莫孔向汤")
GIVEN_NAMES = [
    "建国", "志强", "秀英", "慧敏", "晓东", "建华", "春梅", "海燕", "立新", "玉兰",
    "志刚", "晓明", "雨欣", "子豪", "梓涵", "浩然", "欣怡", "宇轩", "思远", "雅琴",
    "伟", "敏", "静", "磊", "洋", "勇", "艳", "杰", "娟", "涛",
    "明", "超", "霞", "平", "刚", "丽", "军", "强", "文", "宁",
]

HEADERS = {
    "classrooms.csv": ["room_id", "room_name", "building", "floor", "capacity",
                       "has_projector", "has_computer", "room_type", "status"],
    "courses.csv": ["course_id", "course_code", "course_name", "department", "credit", "course_type"],
    "schedules.csv": ["schedule_id", "course_id", "room_id", "semester", "weekday",
                      "period_start", "period_end", "start_time", "end_time", "week_range", "teacher"],
    "canteens.csv": ["canteen_id", "canteen_name", "location", "capacity", "window_count",
                     "open_time", "close_time", "status"],
    "meal_records.csv": ["record_id", "canteen_id", "record_date", "record_hour", "time_slot",
                         "people_count", "wait_minutes", "capacity", "source"],
    "jobs.csv": ["job_id", "job_title", "company", "industry", "city", "education", "experience",
                 "salary_min", "salary_max", "major", "job_desc", "publish_date"],
    "skills.csv": ["skill_id", "skill_name", "skill_category", "description"],
    "course_skills.csv": ["id", "course_id", "skill_id", "level"],
    "job_skills.csv": ["id", "job_id", "skill_id", "importance", "is_required"],
}

EXPECTED_COUNTS = {
    "classrooms.csv": 50,
    "courses.csv": 100,
    "schedules.csv": MIN_SCHEDULES,
    "canteens.csv": 5,
    # 秋季 75 天(3000) + 春季 137 天(5480) 两段窗口合计
    "meal_records.csv": 8480,
    "jobs.csv": 100,
    "skills.csv": 30,
    "course_skills.csv": 300,
    "job_skills.csv": 400,
}


# ---------------------------------------------------------------------------
# 三、数据生成
# ---------------------------------------------------------------------------

def gen_classrooms():
    """50 个教室：容量与类型匹配，机房有电脑、多媒体有投影，少量维修中。"""
    pools = {}
    for room_type, capacities in CAPACITY_POOLS.items():
        pool = list(capacities)
        random.shuffle(pool)
        pools[room_type] = pool

    classrooms = []
    room_id = 0
    for building, prefix, floors, type_counts in BUILDING_SPECS:
        total = sum(count for _, count in type_counts)
        per_floor = -(-total // floors)
        index = 0
        for room_type, count in type_counts:
            for _ in range(count):
                floor = index // per_floor + 1
                seq = index % per_floor + 1
                capacity = pools[room_type].pop()
                if room_type == "机房":
                    has_projector, has_computer = 1, 1
                elif room_type == "多媒体":
                    has_projector = 1
                    has_computer = 1 if random.random() < 0.2 else 0
                elif room_type == "实验室":
                    has_projector = 1 if random.random() < 0.7 else 0
                    has_computer = 1 if random.random() < 0.5 else 0
                    if not has_projector and not has_computer:
                        has_projector = 1
                else:  # 普通
                    has_projector = 1 if random.random() < 0.75 else 0
                    has_computer = 0
                room_id += 1
                classrooms.append({
                    "room_id": room_id,
                    "room_name": "{}{}{:02d}".format(prefix, floor, seq),
                    "building": building,
                    "floor": floor,
                    "capacity": capacity,
                    "has_projector": has_projector,
                    "has_computer": has_computer,
                    "room_type": room_type,
                    "status": "available",
                })
                index += 1

    # 维修中的教室：取每种类型中容量最小的一间（不影响大教室排课）
    for room_type in ("普通", "多媒体", "实验室"):
        candidates = [r for r in classrooms if r["room_type"] == room_type and r["status"] == "available"]
        target = min(candidates, key=lambda r: (r["capacity"], r["room_id"]))
        target["status"] = "maintenance"
    return classrooms


def gen_courses():
    """100 门课程：院系、课程性质、学分、班级规模、可用教室类型。"""
    courses = []
    course_id = 0
    for department, prefix in DEPARTMENTS:
        names = COURSE_NAMES[department]
        selected = names[:DEPARTMENT_COURSE_COUNT[department]]
        for offset, name in enumerate(selected, start=1):
            course_type = random.choices(
                ["必修", "选修", "实践"], weights=[58, 27, 15], k=1
            )[0]
            if name in PUBLIC_COURSE_NAMES:
                course_type = "必修"
                class_size = random.choice([120, 130, 150, 150])
                room_types = ("多媒体",)
            elif course_type == "实践":
                if department in ("电子信息工程", "自动化"):
                    class_size = random.choice([30, 36, 40, 45])
                    room_types = ("实验室",)
                else:
                    class_size = random.choice([40, 45, 50, 55])
                    room_types = ("机房",)
            else:
                if course_type == "必修":
                    class_size = random.choice([45, 50, 55, 60, 70, 80])
                else:
                    class_size = random.choice([30, 40, 45, 50, 60])
                room_types = ("普通", "多媒体")
            if course_type == "必修":
                credit = random.choice([2.0, 2.5, 3.0, 3.0, 3.5, 4.0])
            elif course_type == "选修":
                credit = random.choice([1.0, 1.5, 2.0, 2.0, 2.5])
            else:
                credit = random.choice([1.0, 1.5, 2.0])
            course_id += 1
            courses.append({
                "course_id": course_id,
                "course_code": "{}{}".format(prefix, 100 + offset),
                "course_name": name,
                "department": department,
                "credit": credit,
                "course_type": course_type,
                "class_size": class_size,
                "room_types": room_types,
            })
    return courses


def build_teacher_pool(count=70):
    """70 位教师（100 门课，约 30 位教师承担 2 门课）。"""
    names, seen = [], set()
    while len(names) < count:
        name = random.choice(SURNAMES) + random.choice(GIVEN_NAMES)
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    names.sort()
    random.shuffle(names)
    return names


def assign_sessions(courses):
    """决定每门课每周排课次数，并保证课表总条数 >= MIN_SCHEDULES。"""
    for course in courses:
        if course["course_type"] == "必修":
            course["sessions"] = random.randint(6, 8)
        elif course["course_type"] == "选修":
            course["sessions"] = random.randint(3, 4)
        else:
            course["sessions"] = random.choice([2, 3])
    total = sum(c["sessions"] for c in courses)
    index = 0
    while total < MIN_SCHEDULES:
        course = courses[index % len(courses)]
        course["sessions"] += 1
        total += 1
        index += 1


def place_once(order, available_rooms, semester=None):
    """按给定顺序贪心排课；失败返回 None。保证同一教室同一时间只有一门课。

    ``semester`` 只写进结果行；冲突判定仍是「同一学期内」，跨学期可以复用同一教室与教师。
    """
    semester = SEMESTER if semester is None else semester
    room_busy = defaultdict(set)       # room_id -> {(weekday, period)}
    teacher_busy = defaultdict(set)    # teacher -> {(weekday, period)}
    placed = []
    for course in order:
        allowed = set(course["room_types"])
        candidate_rooms = [
            r for r in available_rooms
            if r["room_type"] in allowed and r["capacity"] >= course["class_size"]
        ]
        if not candidate_rooms:
            return None
        blocks = FOUR_PERIOD_BLOCKS if course["course_type"] == "实践" else TWO_PERIOD_BLOCKS
        sessions = course["sessions"]
        max_per_day = max(1, -(-sessions // 5))
        used_blocks = set()
        day_count = defaultdict(int)
        teacher = course["teacher"]
        for _ in range(sessions):
            chosen = None
            for relax in (0, 1):       # relax=1 时放宽「每天最多课次」限制
                feasible = []
                for weekday in range(1, 6):
                    if relax == 0 and day_count[weekday] >= max_per_day:
                        continue
                    for block_index, (period_start, period_end) in enumerate(blocks):
                        if (weekday, block_index) in used_blocks:
                            continue
                        periods = range(period_start, period_end + 1)
                        if any((weekday, p) in teacher_busy[teacher] for p in periods):
                            continue
                        for room in candidate_rooms:
                            room_id = room["room_id"]
                            if any((weekday, p) in room_busy[room_id] for p in periods):
                                continue
                            feasible.append((weekday, block_index, period_start, period_end, room_id))
                if feasible:
                    chosen = random.choice(feasible)
                    break
            if chosen is None:
                return None
            weekday, block_index, period_start, period_end, room_id = chosen
            used_blocks.add((weekday, block_index))
            day_count[weekday] += 1
            for period in range(period_start, period_end + 1):
                room_busy[room_id].add((weekday, period))
                teacher_busy[teacher].add((weekday, period))
            placed.append({
                "course_id": course["course_id"],
                "room_id": room_id,
                "semester": semester,
                "weekday": weekday,
                "period_start": period_start,
                "period_end": period_end,
                "start_time": PERIOD_START_TIME[period_start],
                "end_time": PERIOD_END_TIME[period_end],
                "week_range": course["week_range"],
                "teacher": teacher,
            })
    return placed


def gen_schedules(courses, classrooms, semester=None):
    """排课：优先安排大班课程，避免教室与教师时间冲突（周一至周五）。

    ``semester`` 默认秋季学期；春季学期复用同一批课程与教室，独立排一次课。
    """
    semester = SEMESTER if semester is None else semester
    available = [r for r in classrooms if r["status"] == "available"]
    teachers = build_teacher_pool()
    for index, course in enumerate(courses):
        course["teacher"] = teachers[index % len(teachers)]
        if course["course_type"] == "必修":
            course["week_range"] = "1-16"
        elif course["course_type"] == "选修":
            course["week_range"] = random.choice(["1-16", "1-16", "1-8", "9-16"])
        else:
            course["week_range"] = random.choice(["1-16", "1-8", "9-16"])

    base_order = sorted(courses, key=lambda c: (-c["class_size"], c["course_id"]))
    for attempt in range(30):
        order = list(base_order)
        if attempt:                     # 第一次保持容量降序，之后打乱顺序重试
            random.shuffle(order)
        placed = place_once(order, available, semester)
        if placed is not None:
            placed.sort(key=lambda s: (s["weekday"], s["period_start"], s["room_id"]))
            for schedule_id, row in enumerate(placed, start=1):
                row["schedule_id"] = schedule_id
            return [{key: row[key] for key in HEADERS["schedules.csv"]} for row in placed]
    raise RuntimeError("排课失败：30 次尝试均无法为全部课程找到无冲突教室")


def gen_canteens():
    rows = []
    for canteen_id, canteen in enumerate(CANTEENS, start=1):
        rows.append({
            "canteen_id": canteen_id,
            "canteen_name": canteen["name"],
            "location": canteen["location"],
            "capacity": canteen["capacity"],
            "window_count": canteen["window_count"],
            "open_time": canteen["open_time"],
            "close_time": canteen["close_time"],
            "status": "open",
        })
    return rows


def to_minutes(time_text):
    """'HH:MM' -> 分钟数，用于营业时段判断。"""
    hour, minute = time_text.split(":")
    return int(hour) * 60 + int(minute)


def service_windows(canteen):
    """把 service_windows 转成 (起始分钟, 结束分钟) 列表。"""
    return [(to_minutes(start), to_minutes(end)) for start, end in canteen["service_windows"]]


def in_business_hours(canteen, hour):
    """记录小时的起点必须落在该食堂营业时段内（含一食堂夜宵延长时段）。"""
    minute = hour * 60
    return any(start <= minute < end for start, end in service_windows(canteen))


def day_mode(day, holidays=None):
    """判断某天的客流形状：holiday / weekend / weekday（holidays 为空时用秋季学期假期）。"""
    holiday_set = HOLIDAYS if holidays is None else holidays
    if day in holiday_set:
        return "holiday"
    if day.weekday() >= 5:
        return "weekend"
    return "weekday"


def gen_meal_records(start=None, end=None, holidays=None):
    """就餐记录：按天 × 采集小时 × 5 个食堂生成（默认秋季 75 天 = 3000 条）。

    每个食堂按自己的营业时段取采集小时（琼阁苑午休 14:00-16:30 不生成记录、
    闭餐早的时段不生成夜宵；一食堂 21 点只记一条夜宵 other 记录），
    午餐 11:00-13:00 形成明显高峰。

    参数化后同一逻辑可以生成多个学期窗口（春季学期传 SPRING_TERM_START/END）。
    返回时按 (日期, 小时, 食堂) 排序并从 1 重新编号；跨窗口的主键偏移由调用方处理。
    """
    start = TERM_START if start is None else start
    end = TERM_END if end is None else end
    records = []
    record_id = 0
    day = start
    while day <= end:
        mode = day_mode(day, holidays)
        factor_low, factor_high = DAY_FACTOR[mode]
        day_factor = random.uniform(factor_low, factor_high)
        for canteen_id, canteen in enumerate(CANTEENS, start=1):
            capacity = canteen["capacity"]
            for hour in canteen["hours"][mode]:
                occupancy = (
                    BASE_FACTORS[mode][hour] * canteen["share"] * PEAK_OCCUPANCY
                    * day_factor * random.uniform(0.94, 1.06)
                )
                people = max(0, int(round(capacity * occupancy)))
                wait = 1.0 + 24.0 * (people / capacity) ** 1.8 * random.uniform(0.95, 1.05)
                record_id += 1
                records.append({
                    "record_id": record_id,
                    "canteen_id": canteen_id,
                    "record_date": day.isoformat(),
                    "record_hour": hour,
                    "time_slot": HOUR_SLOT[hour],
                    "people_count": people,
                    "wait_minutes": round(wait, 1),
                    "capacity": canteen["capacity"],
                    "source": "simulated",
                })
        day += timedelta(days=1)
    records.sort(key=lambda row: (row["record_date"], row["record_hour"], row["canteen_id"]))
    for record_id, row in enumerate(records, start=1):
        row["record_id"] = record_id
    return records


def gen_skills():
    rows = []
    for skill_id, (name, category, description) in enumerate(SKILLS, start=1):
        rows.append({
            "skill_id": skill_id,
            "skill_name": name,
            "skill_category": category,
            "description": description,
        })
    return rows


def affinity_skills(course_name, department_pool):
    """按课程名关键词取「优先技能」，只保留该专业池里存在的技能。"""
    pool = set(department_pool)
    result = []
    for keywords, names in COURSE_SKILL_AFFINITY:
        if not any(keyword in course_name for keyword in keywords):
            continue
        for name in names:
            if name in pool and name not in result:
                result.append(name)
    return result


def gen_course_skills(courses, skills):
    """300 条课程—技能关系：每门课 2~4 个技能，且每个技能至少被一门课覆盖。"""
    skill_id_by_name = {s["skill_name"]: s["skill_id"] for s in skills}
    sizes = [2] * 30 + [3] * 40 + [4] * 30        # 合计 300
    random.shuffle(sizes)
    by_course = {}
    order = []
    for course, size in zip(courses, sizes):
        pool = DEPARTMENT_SKILL_POOL[course["department"]]
        # 先按课程内容取相关技能（第一个技能必挂），再用专业技能池补齐
        picked = []
        related = affinity_skills(course["course_name"], pool)
        if related:
            take = min(len(related), max(1, size - 1))
            picked = related[:1] + random.sample(related[1:], min(len(related) - 1, take - 1))
        remaining = [name for name in pool if name not in picked]
        picked += random.sample(remaining, size - len(picked))
        by_course[course["course_id"]] = [skill_id_by_name[name] for name in picked]
        order.append(course["course_id"])

    # 覆盖修复：若某技能没有任何课程覆盖，则在与该技能相关的课程里替换掉一个
    # 「已被多门课覆盖」的技能（总数仍为 300，且不会造成新的未覆盖技能）
    def coverage():
        counter = defaultdict(int)
        for ids in by_course.values():
            for skill_id in ids:
                counter[skill_id] += 1
        return counter

    all_skill_ids = sorted(skill_id_by_name.values())
    name_of_skill = {skill_id: name for name, skill_id in skill_id_by_name.items()}
    for _ in range(len(all_skill_ids) + 1):
        counter = coverage()
        missing = [sid for sid in all_skill_ids if counter[sid] == 0]
        if not missing:
            break
        progressed = False
        for skill_id in missing:
            donor = None
            for course in courses:
                ids = by_course[course["course_id"]]
                if skill_id in ids:
                    continue
                if name_of_skill[skill_id] not in DEPARTMENT_SKILL_POOL[course["department"]]:
                    continue
                # 只牺牲「被别人也覆盖着」的技能，避免拆东墙补西墙
                removable = [sid for sid in sorted(ids) if counter[sid] >= 2]
                if removable:
                    donor = (course["course_id"], max(removable, key=lambda sid: (counter[sid], -sid)))
                    break
            if donor:
                course_id, replaced = donor
                by_course[course_id].remove(replaced)
                by_course[course_id].append(skill_id)
                progressed = True
        if not progressed:
            break

    rows = []
    relation_id = 0
    for course_id in order:
        for skill_id in sorted(by_course[course_id]):
            relation_id += 1
            rows.append({
                "id": relation_id,
                "course_id": course_id,
                "skill_id": skill_id,
                "level": random.choice(["了解", "掌握", "掌握", "熟练"]),
            })
    return rows


def gen_jobs_and_job_skills(skills):
    """100 个岗位（每个模板 5 个）+ 400 条岗位—技能关系（每岗位 3~5 个技能）。"""
    skill_id_by_name = {s["skill_name"]: s["skill_id"] for s in skills}
    sizes = [3] * 20 + [4] * 60 + [5] * 20       # 合计 400
    random.shuffle(sizes)

    jobs = []
    for index in range(100):
        title, industry, majors, pool, duty = JOB_TEMPLATES[index % len(JOB_TEMPLATES)]
        size = sizes[index]
        picked = random.sample(list(pool), min(size, len(pool)))
        importances = [5, 4] + [random.choice([4, 3, 3, 3, 2]) for _ in range(max(0, len(picked) - 2))]
        required_flags = []
        for importance in importances:
            if importance >= 3:
                required_flags.append(1 if random.random() < 0.85 else 0)
            else:
                required_flags.append(1 if random.random() < 0.4 else 0)
        if sum(required_flags) < 2:              # 至少 2 项硬性要求
            for position in range(len(required_flags)):
                if required_flags[position] == 0:
                    required_flags[position] = 1
                    if sum(required_flags) >= 2:
                        break

        education = random.choice(EDUCATIONS)
        experience = random.choice(EXPERIENCES)
        salary_min = EDUCATION_BASE_SALARY[education] + EXPERIENCE_ADJUST[experience]
        salary_min = int(round(salary_min / 500.0) * 500)
        salary_max = salary_min + random.choice(SALARY_SPREAD) + random.choice([0, 0, 500, 1000])

        required_names = [name for name, flag in zip(picked, required_flags) if flag]
        optional_names = [name for name, flag in zip(picked, required_flags) if not flag]
        description = "岗位职责：{}。任职要求：学历要求{}；经验要求{}；必须掌握{}".format(
            duty, education, experience, "、".join(required_names)
        )
        if optional_names:
            description += "；具备{}者优先".format("、".join(optional_names))
        description += "。"

        publish_date = TERM_START + timedelta(days=random.randint(-30, 74))
        jobs.append({
            "job_title": title,
            "company": COMPANIES[(index * 7 + 3) % len(COMPANIES)],
            "industry": industry,
            "city": random.choices(CITIES, weights=CITY_WEIGHTS, k=1)[0],
            "education": education,
            "experience": experience,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "major": random.choice(list(majors)),
            "job_desc": description,
            "publish_date": publish_date.isoformat(),
            "skills": [
                {
                    "skill_id": skill_id_by_name[name],
                    "importance": importance,
                    "is_required": flag,
                }
                for name, importance, flag in zip(picked, importances, required_flags)
            ],
        })

    jobs.sort(key=lambda job: (job["publish_date"], job["job_title"], job["company"]))
    job_skills = []
    relation_id = 0
    for job_id, job in enumerate(jobs, start=1):
        job["job_id"] = job_id
        for item in job["skills"]:
            relation_id += 1
            job_skills.append({
                "id": relation_id,
                "job_id": job_id,
                "skill_id": item["skill_id"],
                "importance": item["importance"],
                "is_required": item["is_required"],
            })
    return jobs, job_skills


def generate_all():
    """生成全部模拟数据。

    学期策略（2025-2026-1 秋季 + 2025-2026-2 春季）：
        1. 先按原顺序生成秋季学期的全部数据，保证既有 CSV 的历史内容不被扰动；
        2. 冻结此时的随机状态，再派生春季学期的数据（排课 + 就餐记录），
           因此春季数据可复现，同时不会改变秋季数据的取值。
        两学期的排课主键（schedule_id）与就餐记录主键（record_id）全局连续。
    """
    classrooms = gen_classrooms()
    courses = gen_courses()
    skills = gen_skills()
    assign_sessions(courses)
    schedules_fall = gen_schedules(courses, classrooms, SEMESTER)
    canteens = gen_canteens()
    meal_records_fall = gen_meal_records()
    jobs, job_skills = gen_jobs_and_job_skills(skills)
    course_skills = gen_course_skills(courses, skills)

    # 之后所有随机数都走「春季学期」这条独立分支（不再改动上面已生成的秋季数据）
    schedules_spring = gen_schedules(courses, classrooms, SEMESTER_SPRING)
    for offset, row in enumerate(schedules_spring, start=len(schedules_fall) + 1):
        row["schedule_id"] = offset

    meal_records_spring = gen_meal_records(SPRING_TERM_START, SPRING_TERM_END, SPRING_HOLIDAYS)
    offset = len(meal_records_fall)
    for index, row in enumerate(meal_records_spring, start=1):
        row["record_id"] = offset + index

    return {
        "classrooms": classrooms,
        "courses": courses,
        "schedules": schedules_fall + schedules_spring,
        "canteens": canteens,
        "meal_records": meal_records_fall + meal_records_spring,
        "jobs": jobs,
        "skills": skills,
        "course_skills": course_skills,
        "job_skills": job_skills,
    }


# ---------------------------------------------------------------------------
# 四、CSV 读写
# ---------------------------------------------------------------------------

FILE_OF_KEY = {
    "classrooms": "classrooms.csv",
    "courses": "courses.csv",
    "schedules": "schedules.csv",
    "canteens": "canteens.csv",
    "meal_records": "meal_records.csv",
    "jobs": "jobs.csv",
    "skills": "skills.csv",
    "course_skills": "course_skills.csv",
    "job_skills": "job_skills.csv",
}


def write_csv(filename, rows):
    path = os.path.join(RAW_DIR, filename)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=HEADERS[filename], extrasaction="ignore", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)
    return path


def read_csv(filename):
    path = os.path.join(RAW_DIR, filename)
    with open(path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return reader.fieldnames, list(reader)


def sha256_file(filename):
    path = os.path.join(RAW_DIR, filename)
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 五、自动检查
# ---------------------------------------------------------------------------

def pearson(xs, ys):
    n = len(xs)
    if n == 0:
        return 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return 0.0
    return cov / (var_x ** 0.5 * var_y ** 0.5)


class Checker:
    def __init__(self, data):
        self.data = data
        self.problems = []
        self.warnings = []

    def check(self, ok, message):
        print("  [{}] {}".format("OK" if ok else "FAIL", message))
        if not ok:
            self.problems.append(message)
        return ok

    def warn(self, ok, message):
        print("  [{}] {}".format("OK" if ok else "WARN", message))
        if not ok:
            self.warnings.append(message)
        return ok

    def info(self, message):
        print("  [INFO] {}".format(message))

    # --- 1. 记录数 ------------------------------------------------------
    def check_counts(self, files):
        print("\n--- 1. 记录数检查 ---")
        for filename in HEADERS:
            rows = files[filename]
            expected = EXPECTED_COUNTS[filename]
            if filename == "schedules.csv":
                ok = len(rows) >= 500
                self.check(ok, "{:<18} {:>5} 条（要求 >= 500）".format(filename, len(rows)))
            else:
                ok = len(rows) == expected
                self.check(ok, "{:<18} {:>5} 条（期望 {}）".format(filename, len(rows), expected))

    # --- 2. 空字段 ------------------------------------------------------
    def check_empty(self, files):
        print("\n--- 2. 空字段检查（必填列均不得为空）---")
        for filename, rows in files.items():
            expected_header = HEADERS[filename]
            bad = []
            if rows:
                for line_no, row in enumerate(rows, start=2):
                    for column in expected_header:
                        value = (row.get(column) or "").strip()
                        if value == "" or value != (row.get(column) or ""):
                            bad.append("第 {} 行 {}={!r}".format(line_no, column, row.get(column)))
            ok = not bad
            detail = "无空字段" if ok else "示例：{}".format("；".join(bad[:3]))
            self.check(ok, "{:<18} {} / {} 行".format(filename, detail, len(rows)))

    # --- 3. 重复 --------------------------------------------------------
    def check_duplicates(self, files):
        print("\n--- 3. 重复检查 ---")
        unique_specs = [
            ("classrooms.csv", ("room_id",), "教室主键唯一"),
            ("classrooms.csv", ("room_name",), "教室名称唯一"),
            ("courses.csv", ("course_id",), "课程主键唯一"),
            ("courses.csv", ("course_code",), "课程代码唯一"),
            ("courses.csv", ("course_name",), "课程名称唯一"),
            ("schedules.csv", ("schedule_id",), "排课主键唯一"),
            ("canteens.csv", ("canteen_id",), "食堂主键唯一"),
            ("canteens.csv", ("canteen_name",), "食堂名称唯一"),
            ("meal_records.csv", ("record_id",), "就餐记录主键唯一"),
            ("meal_records.csv", ("canteen_id", "record_date", "record_hour"), "食堂+日期+小时唯一"),
            ("jobs.csv", ("job_id",), "岗位主键唯一"),
            ("skills.csv", ("skill_id",), "技能主键唯一"),
            ("skills.csv", ("skill_name",), "技能名称唯一"),
            ("course_skills.csv", ("id",), "课程技能主键唯一"),
            ("course_skills.csv", ("course_id", "skill_id"), "课程+技能唯一"),
            ("job_skills.csv", ("id",), "岗位技能主键唯一"),
            ("job_skills.csv", ("job_id", "skill_id"), "岗位+技能唯一"),
        ]
        for filename, key, label in unique_specs:
            rows = files[filename]
            seen = {}
            duplicates = []
            for row in rows:
                value = tuple(row[column] for column in key)
                seen[value] = seen.get(value, 0) + 1
            for value, count in sorted(seen.items()):
                if count > 1:
                    duplicates.append("{}×{}".format("-".join(value), count))
            self.check(
                not duplicates,
                "{:<18} {:<22} 重复 {} 组".format(filename, label, len(duplicates)),
            )
            if duplicates:
                self.info("重复示例：{}".format("；".join(duplicates[:3])))

    # --- 4. 课表冲突 ----------------------------------------------------
    def check_schedule_conflicts(self, files):
        print("\n--- 4. 课表冲突检查 ---")
        schedules = files["schedules.csv"]
        classrooms = {r["room_id"]: r for r in files["classrooms.csv"]}
        courses = {"{}".format(c["course_id"]): c for c in files["courses.csv"]}
        class_size = {str(c["course_id"]): c["class_size"] for c in self.data["courses"]}

        # 4.1 同一教室同一时间不能有两门课
        by_room = defaultdict(list)
        for row in schedules:
            by_room[(row["semester"], row["room_id"], row["weekday"])].append(row)
        room_conflicts = []
        for key, group in sorted(by_room.items()):
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    if int(a["period_start"]) <= int(b["period_end"]) and int(b["period_start"]) <= int(a["period_end"]):
                        room_conflicts.append(
                            "教室 {} 周{} {}-{}节 与 {}-{}节".format(
                                classrooms[key[1]]["room_name"], key[2],
                                a["period_start"], a["period_end"], b["period_start"], b["period_end"],
                            )
                        )
        self.check(not room_conflicts, "同一教室同一时间无两门课（冲突 {} 组）".format(len(room_conflicts)))
        for item in room_conflicts[:3]:
            self.info(item)

        # 4.2 同一教师同一时间不能有两门课
        by_teacher = defaultdict(list)
        for row in schedules:
            by_teacher[(row["semester"], row["teacher"], row["weekday"])].append(row)
        teacher_conflicts = []
        for key, group in sorted(by_teacher.items()):
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    if int(a["period_start"]) <= int(b["period_end"]) and int(b["period_start"]) <= int(a["period_end"]):
                        teacher_conflicts.append("教师 {} 周{} 时段重叠".format(key[1], key[2]))
        self.check(not teacher_conflicts, "同一教师同一时间无两门课（冲突 {} 组）".format(len(teacher_conflicts)))

        # 4.2b 学期覆盖：秋季 + 春季都要有足量排课，且学期取值合法
        per_semester = defaultdict(int)
        for row in schedules:
            per_semester[row["semester"]] += 1
        missing_semesters = [
            name for name in (SEMESTER, SEMESTER_SPRING) if per_semester.get(name, 0) < 500
        ]
        self.check(
            not missing_semesters,
            "两个学期各有 >= 500 条排课（{}）".format(
                "、".join("{}={}".format(name, per_semester.get(name, 0))
                          for name in (SEMESTER, SEMESTER_SPRING))
            ),
        )
        for name in missing_semesters:
            self.info("{} 排课不足：{} 条".format(name, per_semester.get(name, 0)))

        # 4.3 字段合法性
        bad_field = []
        for row in schedules:
            periods = list(range(int(row["period_start"]), int(row["period_end"]) + 1))
            if not (1 <= int(row["weekday"]) <= 5):
                bad_field.append("weekday={}".format(row["weekday"]))
            if not (1 <= int(row["period_start"]) <= 12 and 1 <= int(row["period_end"]) <= 12):
                bad_field.append("节次越界")
            if int(row["period_end"]) < int(row["period_start"]):
                bad_field.append("period_end < period_start")
            if row["start_time"] != PERIOD_START_TIME[int(row["period_start"])]:
                bad_field.append("start_time 与节次不一致")
            if row["end_time"] != PERIOD_END_TIME[int(row["period_end"])]:
                bad_field.append("end_time 与节次不一致")
            if row["semester"] not in (SEMESTER, SEMESTER_SPRING):
                bad_field.append("semester 异常")
            if row["course_id"] not in courses:
                bad_field.append("course_id 不存在")
            if row["room_id"] not in classrooms:
                bad_field.append("room_id 不存在")
            if not periods:
                bad_field.append("节次为空")
        self.check(not bad_field, "排课字段合法性（周一到周五、节次与时间一致、外键存在）")

        # 4.4 排课不使用维修中的教室，且教室容量满足课程班级规模
        maintenance_used = [
            row for row in schedules if classrooms[row["room_id"]]["status"] != "available"
        ]
        capacity_mismatch = [
            row for row in schedules
            if int(classrooms[row["room_id"]]["capacity"]) < class_size[row["course_id"]]
        ]
        self.check(not maintenance_used, "排课未占用维修中的教室（{} 条）".format(len(maintenance_used)))
        self.check(
            not capacity_mismatch,
            "教室容量均不小于课程班级规模（不匹配 {} 条，内部业务规则）".format(len(capacity_mismatch)),
        )

        # 4.5 课程 / 教室覆盖
        scheduled_courses = {row["course_id"] for row in schedules}
        used_rooms = {row["room_id"] for row in schedules}
        self.check(
            len(scheduled_courses) == len(courses),
            "每门课程至少有一条排课（{}/{}）".format(len(scheduled_courses), len(courses)),
        )
        self.info("使用教室 {}/{} 间；周内分布 {}".format(
            len(used_rooms), len(classrooms),
            {w: sum(1 for r in schedules if int(r["weekday"]) == w) for w in range(1, 6)},
        ))

    # --- 5. 教室属性 ----------------------------------------------------
    def check_classrooms(self, files):
        print("\n--- 5. 教室容量与属性检查 ---")
        classrooms = files["classrooms.csv"]
        bad_capacity, bad_type, bad_floor, bad_flag = [], [], [], []
        for row in classrooms:
            room_type = row["room_type"]
            capacity = int(row["capacity"])
            if room_type not in ROOM_TYPES:
                bad_type.append(row["room_name"])
            low, high = CAPACITY_RANGE.get(room_type, (1, 10 ** 6))
            if not (low <= capacity <= high):
                bad_capacity.append("{} {}({})".format(row["room_name"], capacity, room_type))
            if int(row["floor"]) < 1:
                bad_floor.append(row["room_name"])
            if row["has_projector"] not in ("0", "1") or row["has_computer"] not in ("0", "1"):
                bad_flag.append(row["room_name"])
            if row["status"] not in ("available", "maintenance"):
                bad_type.append(row["room_name"])
        self.check(not bad_type, "教室类型 / 状态取值合法")
        self.check(not bad_capacity, "教室容量与类型匹配（{}~{} 区间）".format(
            min(v[0] for v in CAPACITY_RANGE.values()), max(v[1] for v in CAPACITY_RANGE.values())))
        self.check(not bad_floor, "楼层均 >= 1")
        self.check(not bad_flag, "投影 / 电脑标志均为 0 或 1")

        attr_bad = []
        for row in classrooms:
            room_type = row["room_type"]
            projector, computer = int(row["has_projector"]), int(row["has_computer"])
            if room_type == "机房" and computer != 1:
                attr_bad.append("机房无电脑：{}".format(row["room_name"]))
            if room_type == "多媒体" and projector != 1:
                attr_bad.append("多媒体无投影：{}".format(row["room_name"]))
            if room_type == "普通" and computer != 0:
                attr_bad.append("普通教室带电脑：{}".format(row["room_name"]))
            if room_type == "实验室" and projector == 0 and computer == 0:
                attr_bad.append("实验室无任何设备：{}".format(row["room_name"]))
        self.check(not attr_bad, "教室属性合理（机房有电脑、多媒体有投影、普通无电脑）")
        for item in attr_bad[:3]:
            self.info(item)

        stats = defaultdict(int)
        for row in classrooms:
            stats[row["room_type"]] += 1
        maintenance = [r["room_name"] for r in classrooms if r["status"] == "maintenance"]
        self.info("类型分布 {}；维修中 {} 间：{}".format(
            dict(stats), len(maintenance), "、".join(sorted(maintenance))))
        self.info("容量区间 {}~{}".format(
            min(int(r["capacity"]) for r in classrooms), max(int(r["capacity"]) for r in classrooms)))

    # --- 6. 食堂 --------------------------------------------------------
    def check_canteens(self, files):
        print("\n--- 6. 食堂档案 / 营业时间 / 高峰与模式检查 ---")
        records = files["meal_records.csv"]
        canteens = files["canteens.csv"]
        canteen_name = {r["canteen_id"]: r["canteen_name"] for r in canteens}
        capacity = {r["canteen_id"]: int(r["capacity"]) for r in canteens}
        bad_snapshot = [
            r for r in records if int(r["capacity"]) != capacity[r["canteen_id"]]
        ]
        self.check(
            not bad_snapshot,
            "就餐记录中的容量快照与 canteens 表一致（{} 个食堂，异常 {} 条）".format(
                len(capacity), len(bad_snapshot)),
        )
        for canteen_id in sorted(canteen_name):
            count = sum(1 for r in records if r["canteen_id"] == canteen_id)
            self.check(count > 0, "{} 有就餐记录 {} 条".format(canteen_name[canteen_id], count))

        over_capacity = [
            r for r in records if int(r["people_count"]) > capacity[r["canteen_id"]]
        ]
        self.check(not over_capacity, "就餐人数均不超过食堂容量（越界 {} 条）".format(len(over_capacity)))
        bad_wait = [r for r in records if float(r["wait_minutes"]) < 0]
        self.check(not bad_wait, "等待时间均 >= 0")

        # 6.0 就餐记录必须落在对应食堂的营业时间内，时段标记与小时一致
        canteen_of_id = {str(index): canteen for index, canteen in enumerate(CANTEENS, start=1)}
        outside_hours = [
            r for r in records
            if not in_business_hours(canteen_of_id[r["canteen_id"]], int(r["record_hour"]))
        ]
        self.check(
            not outside_hours,
            "就餐记录均落在对应食堂营业时间内（越界 {} 条）".format(len(outside_hours)),
        )
        for item in outside_hours[:3]:
            self.info("{} {}:00 people={}".format(
                canteen_name[item["canteen_id"]], item["record_hour"], item["people_count"]))
        bad_slot = [
            r for r in records if HOUR_SLOT[int(r["record_hour"])] != r["time_slot"]
        ]
        self.check(not bad_slot, "time_slot 与就餐小时一致（异常 {} 条）".format(len(bad_slot)))

        # 6.1 每个食堂午餐高峰
        peak_ok = True
        for canteen_id in sorted(canteen_name):
            rows = [r for r in records if r["canteen_id"] == canteen_id]
            by_slot = defaultdict(list)
            for row in rows:
                by_slot[row["time_slot"]].append(int(row["people_count"]))
            means = {slot: sum(values) / len(values) for slot, values in by_slot.items()}
            ok = means["lunch"] > means["dinner"] and means["lunch"] > means["breakfast"]
            peak_ok = peak_ok and ok
            self.check(ok, "{} 午餐高峰存在（早 {:.0f} / 午 {:.0f} / 晚 {:.0f} 人）".format(
                canteen_name[canteen_id], means["breakfast"], means["lunch"], means["dinner"]))

        # 6.2 全局高峰小时落在午餐时段
        hour_people = defaultdict(list)
        for row in records:
            hour_people[int(row["record_hour"])].append(int(row["people_count"]))
        hour_mean = {hour: sum(values) / len(values) for hour, values in sorted(hour_people.items())}
        peak_hour = max(hour_mean, key=lambda h: hour_mean[h])
        self.check(peak_hour in (11, 12, 13), "全局人流最高小时为 {}:00（属于午餐时段）".format(peak_hour))
        self.info("按小时平均人数 {}".format(
            {hour: int(hour_mean[hour]) for hour in sorted(hour_mean)}))

        # 6.3 工作日 / 周末 / 假期模式不同
        #     两个学期的假期集合不同（国庆 / 清明+劳动节），按日期归属选择对应集合判断。
        def mode_of(row):
            day = date.fromisoformat(row["record_date"])
            holidays = HOLIDAYS if day < SPRING_TERM_START else SPRING_HOLIDAYS
            return day_mode(day, holidays)
        by_mode = defaultdict(list)
        hours_by_mode = defaultdict(set)
        for row in records:
            by_mode[mode_of(row)].append(int(row["people_count"]))
            hours_by_mode[mode_of(row)].add(int(row["record_hour"]))
        mode_mean = {mode: sum(v) / len(v) for mode, v in by_mode.items()}
        self.check(
            hours_by_mode["weekday"] != hours_by_mode["weekend"],
            "工作日与周末就餐小时不同（工作日 {} / 周末 {}）".format(
                sorted(hours_by_mode["weekday"]), sorted(hours_by_mode["weekend"])),
        )
        self.check(
            mode_mean["weekday"] > mode_mean["weekend"] > mode_mean["holiday"],
            "工作日 > 周末 > 假期 平均人流（{:.0f} / {:.0f} / {:.0f}）".format(
                mode_mean["weekday"], mode_mean["weekend"], mode_mean["holiday"]),
        )

        # 6.4 拥挤度越高等待越久
        occupancy = [int(r["people_count"]) / capacity[r["canteen_id"]] for r in records]
        waits = [float(r["wait_minutes"]) for r in records]
        correlation = pearson(occupancy, waits)
        self.check(correlation > 0.5, "拥挤度与等待时间正相关（相关系数 {:.3f}）".format(correlation))
        self.info("就餐记录天数 {}（{} ~ {}），覆盖食堂 {} 个".format(
            len({r["record_date"] for r in records}),
            min(r["record_date"] for r in records),
            max(r["record_date"] for r in records),
            len(canteen_name)))

        # 6.5 每个食堂的营业时间与采集小时（真实档案核对）
        for canteen_id in sorted(canteen_name):
            canteen = canteen_of_id[canteen_id]
            hours = sorted({int(r["record_hour"]) for r in records if r["canteen_id"] == canteen_id})
            peak = max(
                int(r["people_count"]) for r in records if r["canteen_id"] == canteen_id
            )
            self.info("{} 营业 {}~{}（{}）；采集小时 {}；最高 {} 人 / 容量 {} 人（{:.0%}）".format(
                canteen_name[canteen_id], canteen["open_time"], canteen["close_time"],
                "、".join("{}-{}".format(start, end) for start, end in canteen["service_windows"]),
                ",".join(str(hour) for hour in hours), peak, capacity[canteen_id],
                peak / capacity[canteen_id]))

    # --- 7. 技能关联 ----------------------------------------------------
    def check_skills(self, files):
        print("\n--- 7. 课程 / 岗位技能关联检查 ---")
        skills = files["skills.csv"]
        courses = files["courses.csv"]
        jobs = files["jobs.csv"]
        course_skills = files["course_skills.csv"]
        job_skills = files["job_skills.csv"]

        skill_ids = {row["skill_id"] for row in skills}
        course_ids = {row["course_id"] for row in courses}
        job_ids = {row["job_id"] for row in jobs}

        bad_category = [r["skill_name"] for r in skills if r["skill_category"] not in SKILL_CATEGORIES]
        self.check(not bad_category, "技能分类取值合法（{}）".format("、".join(SKILL_CATEGORIES)))

        course_without = sorted(course_ids - {r["course_id"] for r in course_skills})
        self.check(not course_without, "每门课程都关联了技能（缺 {} 门）".format(len(course_without)))
        job_without = sorted(job_ids - {r["job_id"] for r in job_skills})
        self.check(not job_without, "每个岗位都关联了技能（缺 {} 个）".format(len(job_without)))

        bad_fk = [
            r for r in course_skills if r["course_id"] not in course_ids or r["skill_id"] not in skill_ids
        ]
        self.check(not bad_fk, "课程技能外键均有效")
        bad_fk = [
            r for r in job_skills if r["job_id"] not in job_ids or r["skill_id"] not in skill_ids
        ]
        self.check(not bad_fk, "岗位技能外键均有效")

        bad_level = [r for r in course_skills if r["level"] not in ("了解", "掌握", "熟练")]
        self.check(not bad_level, "课程技能 level 取值合法（了解/掌握/熟练）")
        bad_importance = [r for r in job_skills if not (1 <= int(r["importance"]) <= 5)]
        self.check(not bad_importance, "岗位技能 importance 在 1..5")
        bad_required = [r for r in job_skills if r["is_required"] not in ("0", "1")]
        self.check(not bad_required, "岗位技能 is_required 为 0 或 1")

        covered_skill_ids = {r["skill_id"] for r in course_skills}
        uncovered = sorted(skill_ids - covered_skill_ids)
        self.warn(not uncovered, "每个技能至少被一门课程覆盖（未覆盖 {}）".format(len(uncovered)))

        # 产教融合：岗位目标专业必须能对应到开课专业
        departments = {row["department"] for row in courses}
        bad_major = [r["job_title"] for r in jobs if r["major"] not in departments]
        self.check(not bad_major, "岗位目标专业均能对应课程院系（{} 个专业）".format(len(departments)))

        # 技能覆盖率（岗位技能 ∩ 对应专业课程技能）
        course_skill_of_department = defaultdict(set)
        course_department = {row["course_id"]: row["department"] for row in courses}
        for row in course_skills:
            course_skill_of_department[course_department[row["course_id"]]].add(row["skill_id"])
        job_major = {row["job_id"]: row["major"] for row in jobs}
        covered_pairs = 0
        total_pairs = 0
        for row in job_skills:
            total_pairs += 1
            if row["skill_id"] in course_skill_of_department.get(job_major[row["job_id"]], set()):
                covered_pairs += 1
        self.warn(
            total_pairs and covered_pairs / total_pairs >= 0.5,
            "岗位技能中被对口专业课程覆盖的比例 {:.1%}（{}/{}）".format(
                covered_pairs / total_pairs if total_pairs else 0, covered_pairs, total_pairs),
        )

        course_count = defaultdict(int)
        for row in course_skills:
            course_count[row["course_id"]] += 1
        job_count = defaultdict(int)
        for row in job_skills:
            job_count[row["job_id"]] += 1
        self.info("每门课程技能数 {}~{}；每个岗位技能数 {}~{}".format(
            min(course_count.values()), max(course_count.values()),
            min(job_count.values()), max(job_count.values())))
        self.info("岗位硬性要求技能 {} 条 / 加分项 {} 条".format(
            sum(1 for r in job_skills if r["is_required"] == "1"),
            sum(1 for r in job_skills if r["is_required"] == "0")))

    # --- 8. 确定性 ------------------------------------------------------
    def check_determinism(self, files):
        print("\n--- 8. 可复现性检查（SHA-256 前 16 位）---")
        self.info("固定种子 random.seed({})，重复运行哈希应完全一致".format(SEED))
        for filename in HEADERS:
            digest = sha256_file(filename)
            size = os.path.getsize(os.path.join(RAW_DIR, filename))
            self.info("{:<18} {}  {:>8} 字节  {} 行".format(
                filename, digest[:16], size, len(files[filename])))


def run_checks(data):
    files = {}
    for key, filename in FILE_OF_KEY.items():
        fieldnames, rows = read_csv(filename)
        files[filename] = rows
        if list(fieldnames) != HEADERS[filename]:
            print("  [FAIL] {} 表头与 docs/DATABASE.md 不一致：{}".format(filename, fieldnames))
            return ["表头不一致: {}".format(filename)], []

    checker = Checker(data)
    checker.check_counts(files)
    checker.check_empty(files)
    checker.check_duplicates(files)
    checker.check_schedule_conflicts(files)
    checker.check_classrooms(files)
    checker.check_canteens(files)
    checker.check_skills(files)
    checker.check_determinism(files)
    return checker.problems, checker.warnings


# ---------------------------------------------------------------------------
# 六、入口
# ---------------------------------------------------------------------------

def main():
    random.seed(42)          # AGENTS.md Data Rules：模拟数据固定随机种子
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    print("=" * 74)
    print("CampusAI 模拟数据生成器")
    print("随机种子: {}    输出目录: {}".format(SEED, RAW_DIR))
    print("=" * 74)

    os.makedirs(RAW_DIR, exist_ok=True)
    data = generate_all()

    print("\n[生成]")
    for key, filename in FILE_OF_KEY.items():
        write_csv(filename, data[key])
        print("  {:<18} {:>5} 条  ->  {}".format(filename, len(data[key]), os.path.join(RAW_DIR, filename)))

    schedules = data["schedules"]
    print("  课表: {} 条安排 / {} 门课程 / {} 间教室".format(
        len(schedules), len({row["course_id"] for row in schedules}),
        len({row["room_id"] for row in schedules})))

    print("\n" + "=" * 74)
    print("检查结果")
    print("=" * 74)
    problems, warnings = run_checks(data)

    print("\n" + "=" * 74)
    if problems:
        print("结论: 检查失败，共 {} 个问题".format(len(problems)))
        for item in problems:
            print("  - {}".format(item))
        print("=" * 74)
        return 1
    print("结论: 全部检查通过（0 个问题，{} 个提示）".format(len(warnings)))
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
