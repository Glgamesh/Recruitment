# 招聘网站数据采集系统 — 技术总结

## 项目概述

从四大主流招聘网站（Boss直聘、前程无忧、智联招聘、猎聘）采集互联网/软件开发行业职位数据，MySQL + JSON 双写存储。

## 技术栈

| 层级 | 技术 | 用途 |
|------|------|------|
| 浏览器自动化 | Playwright / DrissionPage | 绕过反爬、模拟浏览器行为 |
| HTTP 请求 | requests | API 直接调用 |
| 数据存储 | MySQL 8.0 + JSON Lines | 结构化存储 + 备份 |
| 配置管理 | YAML | 关键词、城市、数据库配置 |
| 语言 | Python 3.14 | 全部脚本 |

## 四大站点采集方案

### 1. Boss直聘 (`collect_boss.py`)

**难度**: ⭐⭐⭐⭐⭐ | **方案**: DrissionPage + API 直调

```
┌─────────────────────────────────────────────────┐
│  DrissionPage 浏览器（全程保持打开）              │
│  1. 打开 → 检测登录态 → 未登录则轮询等待          │
│  2. 导航搜索页 → 获取 Cookie                     │
│  3. 调用列表 API → 获取职位卡片                   │
│  4. 调用详情 API → 获取职位描述（每4条刷新Cookie） │
│  5. 心跳：定时刷新+滚动保持 Cookie 活跃           │
└─────────────────────────────────────────────────┘
```

**关键发现**:
- Boss直聘是 WebSocket SPA，headless 模式直接被重定向到 `about:blank`
- Playwright 无法通过反爬检测，必须用 DrissionPage
- Cookie 生命周期极短：列表 API 不限次数，但**详情 API 每 4~5 次调用后失效**
- Cookie 绑定浏览器实例：不能关闭浏览器再开新的，否则 Cookie 丢失
- `zp_at` 和 `wt2` 是核心认证 Cookie

**API 端点**:
```
列表: GET /wapi/zpgeek/search/joblist.json?scene=1&query=XX&city=XXXX&page=N&pageSize=30
详情: GET /wapi/zpgeek/job/detail.json?securityId=XX&scene=1
```

**城市码**: `101010100`(北京) `101020100`(上海) `101280100`(广州) `101280600`(深圳) `101210100`(杭州) `101270100`(成都)

---

### 2. 前程无忧 / 51job (`collect_51job.py`)

**难度**: ⭐⭐⭐⭐ | **方案**: Playwright + API 拦截

```
┌──────────────────────────────────────────────┐
│  Playwright 浏览器                            │
│  1. 导航 we.51job.com/pc/search              │
│  2. 阿里云 WAF 自动执行JS → 获取通行Token      │
│  3. 响应拦截 → 捕获 search-pc API JSON        │
│  4. 点击"下一页"按钮 → 触发新API调用 → 翻页   │
└──────────────────────────────────────────────┘
```

**关键发现**:
- 旧域名 `search.51job.com` 已失效，必须用 `we.51job.com`
- 阿里云 WAF 对 headless Playwright **自动放过**（浏览器执行WAF JS即可）
- `page.evaluate()` 发起的 `fetch` 请求**不经过 WAF**，会被拦截
- 必须用 Playwright 的 `page.goto()` 导航或 `page.click()` 触发
- API 响应包含**完整的职位描述**，无需二次请求！

**API 自动返回字段**: `jobDescribe`(职位描述), `jobSalaryMin/Max`(薪资), `jobTags`(技能标签), `companyName`, `industryType1Str`, `companySizeString`

**城市码**: `010000`(北京) `020000`(上海) `030200`(广州) `040000`(深圳) `080200`(杭州) `090200`(成都) `070200`(南京) `180200`(武汉) `200200`(西安) `070300`(苏州)

---

### 3. 猎聘 / Liepin (`collect_liepin.py`)

**难度**: ⭐⭐⭐ | **方案**: Playwright + XHR 拦截

```
┌──────────────────────────────────────────────┐
│  Playwright 浏览器                            │
│  1. 注入 XHR 拦截脚本 → 捕获 API 响应          │
│  2. 导航列表页 → 提取列表数据 + ckId          │
│  3. 浏览器内 fetch 翻页（需 ckId）             │
│  4. 逐个访问详情页 → 提取职位描述              │
└──────────────────────────────────────────────┘
```

**关键发现**:
- 猎聘搜索页是 SPA，数据通过内部 API `api-c.liepin.com` 加载
- 列表 API 的 XHR 响应中包含 `ckId`，翻页时必须携带
- 列表 API **不返回职位描述**，必须单独访问详情页
- 详情页选择器：`.job-intro-container`（经过多次调试才找到）
- 薪资格式 `9-11k·14薪` 需特殊处理（共享单位 X-Yk 模式）

**API 端点**:
```
列表: POST api-c.liepin.com/api/com.liepin.searchfront4c.pc-search-job
翻页: 浏览器内 fetch (同上接口，需 ckId)
```

---

### 4. 智联招聘 / Zhaopin (`collect_zhaopin.py`)

**难度**: ⭐⭐ | **方案**: Playwright + DOM 提取

```
┌──────────────────────────────────────────────┐
│  Playwright 浏览器                            │
│  1. 导航列表页 → SSR 渲染 → 直接提取 DOM 卡片  │
│  2. 翻页：修改 URL 参数                        │
│  3. 详情页访问 → 提取完整职位描述               │
└──────────────────────────────────────────────┘
```

**关键发现**:
- 智联是 SSR 渲染，反爬最弱，Playwright 开箱即用
- 列表卡片数据在 HTML DOM 中，不需要 API 逆向
- 翻页通过 URL 参数控制，最简单

---

## 反爬对抗策略总结

| 策略 | 适用站点 | 说明 |
|------|----------|------|
| Playwright headless + 标准 UA | 智联 | 反爬最弱 |
| Playwright headless + WAF 自动过 | 51job | 阿里云WAF对浏览器放行 |
| XHR 拦截 + 浏览器内 fetch | 猎聘 | SPA 内部 API，需 ckId 翻页 |
| **DrissionPage + 反指纹 + Cookie心跳** | **Boss直聘** | 最强反爬，需专用工具 |

## 薪资解析经验

### 遇到的格式

| 来源 | 原始格式 | 解析结果 |
|------|----------|----------|
| Boss直聘 | `17-20K` | 17000-20000 |
| Boss直聘 | `14-28K·14薪` | 14000-28000 ×14 |
| 猎聘 | `9-11k·14薪` | 9000-11000 ×14 |
| 猎聘 | `200-250元/天` | 4400-5500 (×22天) |
| 51job | `7千-1.2万` | 7000-12000 |
| 51job | `1.5-3万` | 15000-30000 |
| 全部 | `薪资面议`/`面议` | NULL |

### 核心修复

`9-11k` 格式的坑：正则 `(\d+)\s*[kK]` 只能匹配 `11k`，`9-` 不匹配。必须**优先**匹配 `X-Yk` 共享单位模式：

```python
# ✅ 正确：先匹配共享单位
m = re.match(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)\s*[kK]", text)
if m: values = [float(m.group(1))*1000, float(m.group(2))*1000]
```

## 数据库设计

```sql
CREATE TABLE jobs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    source VARCHAR(20) NOT NULL,        -- boss/job51/zhaopin/liepin
    source_job_id VARCHAR(64) NOT NULL, -- 原始网站职位ID
    job_name VARCHAR(200),
    company_name VARCHAR(200),
    salary_min INT,                     -- 最低月薪(元), NULL=面议
    salary_max INT,                     -- 最高月薪(元)
    salary_month INT DEFAULT 12,        -- 年薪月数
    city VARCHAR(50),
    district VARCHAR(50),
    experience VARCHAR(50),
    education VARCHAR(20),
    skills JSON,                        -- 技能标签数组
    job_description TEXT,               -- 职位描述全文
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

## 运行命令汇总

```powershell
cd E:\codex\project\Recruitment\scraper
venv\Scripts\activate

# Boss直聘（需先登录，脚本会自动检测）
python scripts\collect_boss.py -k "Python,AI Agent" -c "北京,上海" --max 20 --no-detail
python scripts\collect_boss.py -k "AI Agent" -c "北京" --max 10

# 前程无忧
python scripts\collect_51job.py -k "Python,Java" -c "北京,上海" --max 20 --pages 2

# 猎聘
python scripts\collect_liepin.py -k "Python" -c "北京" --max 20 --no-detail
python scripts\collect_liepin.py -k "AI Agent" -c "北京,上海" --max 10

# 智联招聘
python scripts\collect_zhaopin.py -k "Python,AI Agent" -c "北京,上海" --max 20
```

## 数据流架构

```
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│ Boss直聘 │   │ 前程无忧 │   │  猎聘   │   │ 智联招聘 │
│ Drission  │   │Playwright│   │Playwright│   │Playwright│
│  +API    │   │  +WAF拦截│   │ +XHR拦截 │   │ +DOM提取 │
└────┬─────┘   └────┬─────┘   └────┬─────┘   └────┬─────┘
     │              │              │              │
     └──────────────┴──────────────┴──────────────┘
                           │
                    ┌──────▼──────┐
                    │  标准化层    │
                    │ parse_salary│
                    │ norm_edu    │
                    │ norm_exp    │
                    │ parse_city  │
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
        ┌─────▼─────┐ ┌───▼────┐ ┌────▼─────┐
        │  MySQL    │ │ JSON   │ │ 后续分析  │
        │  upsert   │ │ Lines  │ │ (待开发)  │
        └───────────┘ └────────┘ └──────────┘
```

## 踩坑记录

1. **Scrapy 与 Playwright 冲突**: Scrapy 2.13+ 的 `async def start()` 和 Twisted reactor 与 Playwright 不兼容 → **放弃 Scrapy，改用独立脚本**

2. **Boss直聘 headless 被秒杀**: Playwright headless 访问直接重定向 `about:blank` → **换 DrissionPage + 反指纹**

3. **Boss直聘 Cookie 跨浏览器丢失**: 每次新建 `ChromiumPage` 实例无法共享登录态 → **浏览器全程保持打开，不关闭**

4. **51job WAF fetch 拦截**: `page.evaluate()` 中的 `fetch` 不经过 WAF 认证通道 → **改用页面导航/点击触发**

5. **猎聘薪资 `9-11k` 解析失败**: 正则只匹配到 `11k`，缺少第二个值 → **新增 X-Yk 共享单位优先匹配**

6. **猎聘详情选择器**: 多种选择器尝试均失败 → **最终定位 `.job-intro-container`**

7. **51job 旧域名失效**: `search.51job.com` 返回空页面 → **迁移到 `we.51job.com/pc/search`**

8. **Boss直聘城市码**: 非标准行政区划码，需逐个实测验证

## 关键词配置 (8类90个)

| 类别 | 示例关键词 |
|------|-----------|
| 软件开发 | Java, Python, C++, Go, 前端, 后端, 全栈 |
| 嵌入式 | 嵌入式, 单片机, ARM, Linux驱动, RTOS, FPGA |
| 数据工程 | 数据分析, 大数据, 数据仓库, ETL, Hadoop, Spark |
| AI/算法 | 机器学习, 深度学习, NLP, 计算机视觉, 大模型, AIGC |
| **AI Agent** | AI Agent, 智能体开发, RAG, LangChain, Multi-Agent |
| 运维/DevOps | 运维开发, DevOps, SRE, Kubernetes, Docker |
| 测试 | 测试开发, 自动化测试, 性能测试 |
| 网络安全 | 网络安全, 信息安全, 渗透测试 |

## 待完成

- [ ] Boss直聘其余4个城市码确认（南京/武汉/西安/苏州）
- [ ] 统一批量调度脚本（按站点+关键词+城市自动运行）
- [ ] 数据去重与清洗 Pipeline
- [ ] 数据分析与可视化
