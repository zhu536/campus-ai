# Day 5 开发记录：CSV 导入 SQLite

## 1. 本轮 Prompt（逐字原文）

```text
现在实现CSV导入SQLite。

创建：

scripts/import_data.py

要求：

CSV来源：

data/raw/

数据库：

data/campus.db

要求：

1. 创建9张表。
2. SQLite初始化可以重复执行。
3. 重建数据库时不会出现脏数据。
4. 使用参数化SQL。
5. 创建必要索引。
6. 每次执行打印：
   - 表名
   - 导入数量
7. 出现错误时给出清晰错误信息。
8. 不接AI。
9. 不修改frontend。

完成后运行：

python scripts/import_data.py

并验证9张表均有数据。

增加基础测试。
```

## 2. Agent 的回复（原文关键段）

Agent 通读 DATABASE.md 后，将**文档中的完整 CREATE TABLE SQL 作为表结构的唯一来源**（脚本解析文档追加 `IF NOT EXISTS`，不复制第二份 DDL），采用「DROP + CREATE + 事务导入 + 原子替换」的可重复执行方案。最终输出：

```text
CSV → SQLite 导入完成：D:\Projects\campus-ai\data\campus.db
表名             数量
-------------  ----
classrooms     50
courses        100
schedules      593
canteens       5
meal_records   3000
jobs           100
skills         30
course_skills  300
job_skills     400
-------------  ----
合计           4578

外键完整性检查：通过（PRAGMA foreign_key_check 无异常）
CSV 目录：data/raw（编码 utf-8）
重建方式：DROP + CREATE + 事务导入 + 原子替换（可重复执行）
```

独立复核（不经过导入脚本，直接用 sqlite3）：9 张用户表全部非空、`PRAGMA integrity_check = ok`、`foreign_key_check` 无异常、9 个索引齐全。二次、三次运行行数完全不变。

## 3. 发现的具体问题

- **Windows 文件占用（WinError 32）**：`os.replace` 替换数据库时目标文件被连接句柄占用导致失败。Agent 排查后把 `connect()` 收敛成真正的上下文管理器（`with` 块，cursor 与 connection 确定性关闭），并实现三步替换策略（直接 `os.replace` → 失败则先 rename 目标为 `.old` 再替换 → 清理残留 `.old`），解决重复导入时的文件锁问题。
- **沙箱临时目录权限**：pytest 的 `tmp_path` 在受限环境下创建的目录带特殊 ACL，SQLite 无法写入。Agent 定位到根因后，在 `conftest.py` 里用 `os.mkdir` 自建临时目录替换 `tempfile.mkdtemp`，并将测试临时目录指向仓库内 `.tmp_test_run`（已加入 `.gitignore`），测试恢复稳定。
- **`--verify-only` 输出流向**：空表问题报告原打印到 stdout，测试断言 stderr 时失败。改为 `print_report` 接收 `problems_stream` 参数，失败详情走 stderr。
- **编码检测误报**：`classrooms.csv` 被检测为 `utf-8-sig`，实为无 BOM 的 UTF-8。改为先检查文件头 BOM 字节再判定编码，报告更准确。

## 4. 改动文件

| 文件 | 动作 |
|---|---|
| `scripts/import_data.py` | 新增（解析 DATABASE.md DDL + DROP/CREATE + 事务导入 + 原子替换 + 自检报告） |
| `tests/test_import_data.py` | 新增（导入幂等、缺列报错、原子替换、`--verify-only`、禁用注入依赖等） |
| `tests/conftest.py` | 新增（临时目录方案，规避沙箱 ACL 问题） |
| `tests/__init__.py` | 新增（pytest 根目录导入支持） |
| `.gitignore` | 修改（忽略 `.pytest_cache/`、`.tmp_test_run/`、`pytest-cache-files-*`、`pytest-of-*`） |

Git 提交：`49877ea feat: add csv to sqlite importer`

## 5. 真实验证结果

```text
python scripts/import_data.py        # 9 表全部导入，合计 4578 行
pytest -q                            # 导入相关测试通过
二次、三次运行行数完全不变           # 可重复执行验证
sqlite3 独立复核                     # integrity_check = ok，foreign_key_check 无异常，9 索引齐全
```

## 6. 剩余风险

- 表结构唯一来源是 `docs/DATABASE.md` 的代码块：若文档 DDL 后续被编辑，导入器会同步变化，需保证文档与代码一致。
- CSV 显式带主键 id，导入时按列插入；若生成器调整列序，导入器需同步核对表头。
