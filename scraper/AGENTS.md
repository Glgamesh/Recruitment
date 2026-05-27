# AGENTS.md — 招聘数据采集系统

## 项目架构

纯独立 Python 脚本，不依赖 Scrapy 框架。
每个站点一个采集脚本，全部位于 `scripts/collect_*.py`。

```
scripts/
├── collect_boss.py     # Boss直聘 — DrissionPage + API
├── collect_51job.py    # 前程无忧   — Playwright + WAF拦截
├── collect_liepin.py   # 猎聘       — Playwright + XHR拦截
├── collect_zhaopin.py  # 智联招聘   — Playwright + DOM提取
└── init_db.py          # 建库建表
```

**历史**: 最初使用 Scrapy 框架，但因 Scrapy 2.13+ 的 async Reactor 与 Playwright 冲突，
且 Twisted 生态与同步 Playwright API 不兼容，已全部迁移为独立脚本。

## 数据流

```
采集脚本 → [parse_salary/norm_edu/norm_exp] → upsert SQL → MySQL
                                              → JSON Lines 备份
```

MySQL 使用 `INSERT ... ON DUPLICATE KEY UPDATE`，主键为 `(source, source_job_id)`。
重复采集自动更新，不会产生重复行。

## 四个站点的关键差异

### Boss直聘 (`collect_boss.py`)
- **工具**: DrissionPage（非 Playwright！Playwright 会被秒杀重定向到 about:blank）
- **登录**: 必须登录。脚本自动检测登录态，未登录时轮询等待
- **Cookie 生命周期**: 列表 API 不限次数，**详情 API 每 4~5 次调用后失效**
- **架构约束**: 浏览器必须全程保持打开，关闭再开会导致 Cookie 丢失
- **心跳**: 每 4 条详情或每 3 页列表刷新页面 + 滚动
- **API 端点**:
  - 列表: `GET /wapi/zpgeek/search/joblist.json?scene=1&query=XX&city=XXXX`
  - 详情: `GET /wapi/zpgeek/job/detail.json?securityId=XX&scene=1`
- **城市码**: 6位自定义编码，非国标。已知: 北京101010100 上海101020100 广州101280100 深圳101280600 杭州101210100 成都101270100
- **数据目录**: `chrome_data_boss/` (已 gitignore)，存储登录 Cookie

### 前程无忧 (`collect_51job.py`)
- **域名**: 必须用 `we.51job.com`，旧域名 `search.51job.com` 已失效（返回空页面）
- **WAF**: 阿里云 WAF，Playwright 自动执行 JS 通过验证
- **关键限制**: `page.evaluate()` 中的 `fetch()` 不经过 WAF，会被拦截。必须用页面导航或点击触发
- **翻页**: 通过点击"下一页"按钮触发，URL 直接带 `pageNum` 不生效
- **数据丰富度**: API 一次性返回职位描述、薪资 min/max、技能标签，无需二次请求
- **城市码**: 6位编码，如 010000(北京) 020000(上海) 030200(广州) 040000(深圳)

### 猎聘 (`collect_liepin.py`)
- **数据获取**: 列表页 XHR 拦截 + 浏览器内 fetch 翻页
- **翻页依赖**: 需要从首次 API 响应中提取 `ckId`，翻页时必须携带
- **描述获取**: 列表 API 不返回职位描述，必须单独访问详情页
- **详情选择器**: `.job-intro-container`（多次调试才定位到）
- **薪资格式**: `9-11k·14薪` 需要 X-Yk 共享单位模式优先匹配（参见下方薪资解析）
- **城市码**: 如 010(北京) 020(上海) 050020(广州)

### 智联招聘 (`collect_zhaopin.py`)
- **最简单**: SSR 渲染，Playwright 直连即可，反爬最弱
- **翻页**: URL 参数直接控制，最简单
- **描述**: 需访问详情页

## 薪资解析注意事项

`parse_salary()` 的核心坑：**X-Yk 格式的正则匹配顺序**

```python
# ❌ 错误: 先匹配独立 K 值
# "9-11k" → 只匹配到 "11k"=11000 → 缺第二个值 → 返回 None

# ✅ 正确: 先匹配共享单位的 X-Yk 范围
m = re.match(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)\s*[kK]", text)
if m: values = [float(m.group(1))*1000, float(m.group(2))*1000]
```

支持的格式: `17-20K`, `9-11k·14薪`, `1.5-3万/月`, `7千-1.2万/月`, `200-250元/天`, `20-40万/年`, `薪资面议`

## 不要做的事

1. **不要用 `input()`** — 非交互环境下会 EOFError，用轮询代替
2. **不要关闭 Boss直聘浏览器** — Cookie 绑定实例，关闭即失效
3. **不要用 `page.evaluate().fetch()` 调 51job API** — WAF 会拦截
4. **不要用 `search.51job.com`** — 已废弃，用 `we.51job.com`
5. **不要用 Scrapy** — Reactor 冲突，所有脚本用同步 Playwright/DrissionPage
6. **不要用 Playwright 爬 Boss直聘** — headless 直接被重定向 about:blank
7. **不要用 Python 3.8** — 需要 3.10+，项目当前用 3.14

## 文件写入

沙箱环境下 `scripts/` 目录只读。修改文件时：
1. 先写入项目根目录 (`E:\codex\project\Recruitment\_temp.py`)
2. 再用 `Copy-Item -Force` 覆盖目标文件
3. 用 `$content -replace '^\uFEFF', ''` 清除 UTF-8 BOM

## 运行命令

```powershell
cd E:\codex\project\Recruitment\scraper
venv\Scripts\activate

# Boss直聘 (需先登录，脚本自动检测等待)
python scripts\collect_boss.py -k "Python,AI Agent" -c "北京,上海" --max 20 --no-detail

# 前程无忧
python scripts\collect_51job.py -k "Python" -c "北京,上海" --max 20 --pages 2

# 猎聘
python scripts\collect_liepin.py -k "Python" -c "北京" --max 20 --no-detail

# 智联招聘
python scripts\collect_zhaopin.py -k "Python" -c "北京,上海" --max 20

# 初始化数据库
python scripts\init_db.py
```

## 配置

| 文件 | 用途 |
|------|------|
| `config/keywords.yaml` | 8类90个搜索关键词 |
| `config/cities.yaml` | 10个目标城市 |
| `config/mysql.yaml` | MySQL 连接配置 |

## 待完成

- [ ] Boss直聘余下4个城市码（南京/武汉/西安/苏州）
- [ ] 统一批量调度脚本
- [ ] 数据分析与可视化模块
