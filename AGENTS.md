# AGENTS.md — 招聘网站数据统计系统

> 项目根目录开发指南，涵盖全流程技术栈、数据约定、后续开发关键信息。

---

## 一、项目概述

从四大主流招聘网站采集互联网/软件开发行业职位数据，经清洗、分析后通过 Web 前端展示。

| 阶段 | 状态 | 目录 |
|------|:--:|------|
| 数据采集 | ✅ 完成(模块化v2) | `scraper/` |
| 数据清洗 | ⬜ 待开发 | — |
| 数据分析 | ⬜ 待开发 | — |
| 后端 API | ⬜ 待开发 | — |
| 前端展示 | ⬜ 待开发 | — |

---

## 二、技术栈

| 层级 | 选型 | 原因 |
|------|------|------|
| 语言 | **Python 3.14** | 全项目统一语言 |
| 采集-浏览器 | Playwright / DrissionPage | 过反爬，DrissionPage 专攻 Boss直聘 |
| 采集-浏览器 | Playwright / DrissionPage | 过反爬，DrissionPage 用于 Boss/智联 |
| 采集-HTTP | requests | API 直调 |
| 数据库 | **MySQL 8.0** | 结构化存储，已运行在 localhost:3306 |
| 配置 | YAML | 关键词/城市/数据库连接 |
| 后端框架 | FastAPI（推荐） | 轻量、异步、Python 原生 |
| 前端框架 | React + ECharts（推荐） | 数据可视化强 |
| 数据分析 | pandas + matplotlib + jieba + wordcloud | Python 数据分析标准栈 |

> **技术栈选型原则**: 全链路 Python，降低上下文切换成本。采集→清洗→分析→API 都用 Python，
> 只有前端展示层使用 JavaScript。

---

## 三、数据库（上下游契约）

### 连接信息

```
Host: localhost:3306
User: root
Password: 123456
Database: recruitment
```

### 核心表 `jobs`

```sql
CREATE TABLE jobs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    source VARCHAR(20) NOT NULL,          -- boss / job51 / zhaopin / liepin
    source_job_id VARCHAR(64) NOT NULL,   -- 原始网站职位ID
    job_name VARCHAR(200),
    company_name VARCHAR(200),
    salary_min INT,                       -- 最低月薪(元), NULL=面议
    salary_max INT,                       -- 最高月薪(元)
    salary_month INT DEFAULT 12,          -- 年薪月数
    city VARCHAR(50),
    district VARCHAR(50),
    experience VARCHAR(50),
    education VARCHAR(20),
    skills JSON,                          -- 技能标签数组
    welfare JSON DEFAULT NULL,              -- 福利待遇数组（v2新增）
    job_description TEXT,                 -- 职位描述全文
    industry VARCHAR(100),
    company_size VARCHAR(50),
    publish_date VARCHAR(30),
    crawl_time DATETIME,
    url VARCHAR(500),
    UNIQUE KEY uk_source_job (source, source_job_id),
    INDEX idx_city_date (city, publish_date),
    INDEX idx_crawl_time (crawl_time)
);
```

### 数据约定（后续开发必须遵守）

| 约定 | 说明 |
|------|------|
| `salary_min/max` 单位 | **元/月**（年薪已除以12，日薪已×22折算） |
| `salary_min = NULL` | 表示"面议"或薪资未公开 |
| `salary_month` | 默认12，如 `14薪` 则为14 |
| `experience` | 归一化值: 应届生/1年以内/1-3年/3-5年/5-10年/10年以上/不限 |
| `education` | 归一化值: 博士/硕士/本科/大专/高中/中专/不限 |
| `skills` | JSON 数组，如 `["Python","MySQL","Docker"]`，不含福利词 |
| `welfare` | JSON 数组，如 `["五险一金","周末双休"]`（v2新增）|
| `city` | 不带"市"后缀，如 "北京" 而非 "北京市" |
| `source` | 固定值: `boss` / `job51` / `zhaopin` / `liepin` |
| 去重 | `(source, source_job_id)` 联合唯一，upsert 自动更新 |

---

## 四、数据采集子系统 (`scraper/`)

详见 `scraper/AGENTS.md`（v2 模块化重构）。关键信息摘要：

| 站点 | 脚本 | 工具 | 反爬难度 |
|------|------|------|:--:|
| Boss直聘 | `collect_boss.py` | DrissionPage | ⭐⭐⭐⭐⭐ |
| 前程无忧 | `collect_51job.py` | Playwright | ⭐⭐⭐⭐ |
| 猎聘 | `collect_liepin.py` | Playwright | ⭐⭐⭐ |
| 智联招聘 | `collect_zhaopin.py` | Playwright | ⭐⭐ |

运行依赖 `scraper/venv/`，安装：

```powershell
cd scraper
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

---

## 五、后续开发路线

### Phase 2: 数据清洗 (`data_cleaning/`)

```
输入: MySQL jobs 表 + JSON 文件
输出: 清洗后的 jobs_clean 表

任务:
- 去重（同公司同职位不同来源合并）
- 薪资异常值检测与修正
- 技能标签统一（"Python开发" → "Python"）
- 城市补全（根据 district 反查 city）
- 日期标准化
```

**技术栈**: pandas + pymysql

**注意事项**:
- `salary_min/max` 为 NULL 的记录在统计时需单独处理
- skills JSON 字段需要用 `JSON_EXTRACT` 或 pandas 展开分析
- job_description 字段包含 HTML 实体，清洗时需 decode

### Phase 3: 数据分析 (`data_analysis/`)

```
输入: jobs_clean 表
输出: 统计图表 + 报告

分析维度:
- 各城市/各岗位薪资分布（中位数、分位数）
- 技能需求热力图 / 词云
- 经验-薪资关联分析
- 行业薪资对比
- AI Agent 等新兴岗位趋势
```

**技术栈**: pandas + matplotlib/seaborn + jieba + wordcloud

### Phase 4: 后端 API (`backend/`)

```
框架: FastAPI
端口: 8000
数据库: MySQL (直连)

API 设计:
GET  /api/jobs           # 职位列表（分页、筛选）
GET  /api/jobs/:id       # 职位详情
GET  /api/stats/salary   # 薪资统计（按城市/岗位/经验）
GET  /api/stats/skills   # 技能热度
GET  /api/stats/trends   # 趋势分析
GET  /api/cities         # 城市列表
GET  /api/keywords       # 关键词/岗位列表
```

**注意事项**:
- salary_min/max 为 NULL 的记录接口返回时标记 `nullable`
- skills 字段从 MySQL JSON 类型反序列化后返回数组
- 分页参数统一: `page`(默认1) + `pageSize`(默认20)
- 前端跨域需配置 CORS 中间件

### Phase 5: 前端展示 (`frontend/`)

```
框架: React (Vite)
图表: ECharts / Recharts
UI: Ant Design 或 shadcn/ui
端口: 3000

页面:
- 首页仪表盘（总览统计卡片）
- 职位列表页（搜索/筛选/排序）
- 薪资分析页（箱线图/分布图）
- 技能词云页
- 城市对比页
- AI Agent 专题页
```

---

## 六、环境与工具

| 项目 | 详情 |
|------|------|
| Python | 3.14.5 @ `D:\Python314` |
| MySQL | localhost:3306, root/123456, recruitment |
| 虚拟环境 | `scraper/venv/` |
| 包管理 | pip |
| 操作系统 | Windows |
| 编辑器 | 任意 |

---

## 七、代码规范

- 所有注释、文档、日志使用**简体中文**
- Python 文件头: `# -*- coding: utf-8 -*-`
- 配置用 YAML，数据用 JSON Lines
- 采集脚本入口统一: `-k` 关键词, `-c` 城市, `--max` 最大条数
- MySQL upsert 模式，不做 DELETE
- 敏感配置（密码）在 `config/mysql.yaml`，已 gitignore 或需手动管理

---

## 八、采集 → 后续阶段的衔接要点

1. **skills 字段是 JSON 数组**，前端/分析时需 `JSON.parse()` 或 `json.loads()`
2. **salary 可能为 NULL**，统计时用 `WHERE salary_min IS NOT NULL` 过滤
3. **同一职位可能被多个来源采集**，去重逻辑需在清洗阶段实现
4. **crawl_time 记录采集时间**，可据此判断数据新鲜度
5. **publish_date 格式不统一**（有的带时间，有的只有日期），清洗阶段统一为 `YYYY-MM-DD`
6. **job_description 是全文**，文本分析前需先分词（jieba）和去停用词
