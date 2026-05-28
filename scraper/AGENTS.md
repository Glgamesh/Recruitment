# AGENTS.md — 招聘数据采集系统 v2

## 项目架构

模块化框架，统一入口 `run.py` + 装饰器注册的采集器模块。

```
scraper/
├── run.py                    # 统一 CLI 入口（自然语言 + 结构化参数）
├── core/                     # 共享框架
│   ├── models.py             # ScrapeRequest / JobItem 数据模型
│   ├── base.py               # BaseScraper 抽象基类
│   ├── registry.py           # @register_scraper 装饰器 + dispatch() 并发调度
│   ├── utils.py              # parse_salary / norm_edu / norm_exp / parse_city
│   ├── db_writer.py          # 统一 MySQL upsert + JSON Lines 双写
│   └── query_parser.py       # 自然语言查询解析
├── collectors/               # 各平台采集器（@register_scraper 注册）
│   ├── __init__.py           # 导入所有采集器触发注册
│   ├── boss.py               # Boss直聘 — DrissionPage + API
│   ├── job51.py              # 前程无忧   — Playwright + XHR拦截
│   ├── liepin.py             # 猎聘       — Playwright + XHR拦截
│   └── zhaopin.py            # 智联招聘   — DrissionPage + 悬停浮层
├── config/
│   ├── sources.yaml          # 平台城市码 + 速率控制配置（新增）
│   ├── keywords.yaml         # 搜索关键词分类
│   ├── cities.yaml           # 目标城市列表
│   └── mysql.yaml            # 数据库连接
└── scripts/                  # 兼容入口（内部转调对应 Collector）
    ├── collect_*.py          # 旧CLI兼容，推荐用 run.py
    └── init_db.py            # 建库建表
```

## 数据流

```
run.py (CLI) → query_parser (解析) → ScrapeRequest
                                    → dispatch() 并发调度
                                      ├── boss.setup() → boss.collect() → boss.teardown()
                                      ├── job51.collect()
                                      ├── liepin.collect()
                                      └── zhaopin.setup() → zhaopin.collect() → zhaopin.teardown()
                                    → DBWriter.write() → MySQL + JSON Lines
```

MySQL 使用 `INSERT ... ON DUPLICATE KEY UPDATE`，主键 `(source, source_job_id)`。

## 新增平台只需三步

```python
# 1. collectors/new_site.py
from core.base import BaseScraper
from core.registry import register_scraper

@register_scraper("new_site")
class NewSiteScraper(BaseScraper):
    name = "new_site"
    display_name = "新站点"
    tool_type = "playwright"  # 或 "drission"

    def collect(self, request):
        # 实现采集逻辑，返回 list[dict]
        ...

# 2. collectors/__init__.py 加一行
from . import new_site

# 3. config/sources.yaml 加城市码
sources:
  new_site:
    city_codes:
      北京: "xxx"
```

## CLI 使用

```bash
# 自然语言
python run.py "查询成都的Agent相关岗位信息"
python run.py "北京Python开发，只要Boss猎聘，最多10条"

# 结构化参数
python run.py -k "Python,AI Agent" -c 北京,成都 -s boss,job51 --max 20
python run.py -k "Python" -c 北京 --max 50 --no-detail --retries 3

# 管理
python run.py --sources-list        # 列出已注册平台
python scripts/collect_boss.py -k Python -c 北京 --max 5  # 兼容旧接口
```

## 四平台关键差异

| 平台 | 工具 | 数据获取 | 描述获取 | 需登录 | skills/welfare |
|------|------|---------|---------|:--:|:--:|
| Boss直聘 | DrissionPage + API | 列表API | 详情API | 是 | API分离字段 |
| 前程无忧 | Playwright + XHR | 拦截API | API自带 | 否 | API分离字段 |
| 猎聘 | Playwright + XHR | 拦截API | 详情页DOM | 否 | API labels（无独立welfare） |
| 智联招聘 | DrissionPage + JS | DOM提取 | 悬停浮层 | 否 | DOM标签+黑名单分离 |

### Boss直聘

- **工具**: DrissionPage（Playwright 会被秒杀重定向到 about:blank）
- **登录**: 必须登录，脚本自动检测并轮询等待
- **API 端点**: 列表 `GET /wapi/zpgeek/search/joblist.json`，详情 `GET /wapi/zpgeek/job/detail.json`
- **限制**: 详情 API 每 4~5 次调用后失效，需刷新页面（心跳）
- **chrome_data**: `chrome_data_boss/` 持久化 Cookie
- **skills/welfare**: API 自带 `skills` + `welfareList`，已分离写入

### 前程无忧

- **工具**: Playwright
- **WAF**: 阿里云 WAF，Playwright 自动执行 JS 通过验证
- **数据获取**: 原生响应拦截（`page.on("response")`）+ JS 注入双保障
- **翻页**: 点击"下一页"按钮触发，URL 直接带 `pageNum` 不生效
- **skills/welfare**: API 自带 `jobTags` + `jobWelfareCodeDataList`，已分离写入，并去重

### 猎聘

- **工具**: Playwright
- **数据获取**: XHR 拦截获取列表 API，浏览器内 fetch 翻页（需 ckId）
- **详情**: 需单独访问详情页，`DETAIL_EXTRACT_JS` 提取描述和技能
- **skills/welfare**: API 的 `labels` 以技能为主，无独立 welfare 字段
- **DBWriter 容错**: welfare 字段缺失时自动补 NULL

### 智联招聘

- **工具**: DrissionPage（绕过腾讯云 EdgeOne WAF）
- **数据获取**: JS 提取 DOM（因 DrissionPage CSS 引擎不支持 `[class*=...]`）
- **描述**: **悬停浮层**（非点击），通过 `mouseenter/mouseover/pointerenter` 事件触发，轮询检测 body 文本中"职位描述"出现后提取
- **skills/welfare**: 43词黑名单 + 公司规模正则过滤（`\d+人`），其余归入 skills
- **chrome_data**: `chrome_data_zhaopin/` 持久化 Cookie

## 架构增强（v2.1）

| 增强 | 说明 |
|------|------|
| 失败重试 | `_run_one_with_retry()` 指数退避，默认 2 次 |
| 速率控制 | `sources.yaml` 中 `request_delay`/`max_retries`/`retry_backoff` |
| 浏览器复用 | `dispatch()` 中 setup→collect→teardown 统一生命周期 |
| 悬停轮询 | 智联描述提取改为 0.3s 间隔轮询，替代固定 2s 等待 |

## 数据库

MySQL `recruitment.jobs` 表：

| 字段 | 类型 | 说明 |
|------|------|------|
| welfare | JSON | **v2 新增**，福利待遇数组，如 `["五险一金","周末双休"]` |
| skills | JSON | 技能标签数组，与 welfare 完全分离 |

## 不要做的事

1. 不要用 `input()` — 非交互环境下会 EOFError
2. 不要关闭 Boss/智联浏览器 — Cookie 绑定实例
3. 不要用 `page.evaluate().fetch()` 调 51job API — WAF 拦截
4. 不要用 `search.51job.com` — 已废弃，用 `we.51job.com`
5. 不要用 Playwright 爬 Boss/智联 — 秒杀/WAF
6. 智联采集器不要用 DrissionPage CSS 选择器 — 用 JS `run_js()` 代替
