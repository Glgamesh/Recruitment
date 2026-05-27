#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
猎聘数据采集脚本 (v2)
- XHR 拦截获取列表 API 数据
- 浏览器内 fetch 翻页
- 详情页抓取职位描述和技能标签
- 修复薪资解析（支持 X-Yk 共享单位格式）
"""

import argparse, json, logging, os, re, time
from datetime import datetime
import pymysql, yaml
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")

LP_CITY = {
    "北京": "010", "上海": "020", "广州": "050020", "深圳": "050090",
    "杭州": "060020", "成都": "280020", "南京": "060080", "武汉": "170020",
    "西安": "270020", "苏州": "060040"
}

def load_yaml(p):
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

# ============================================================
# 薪资解析 —— 修复版
# ============================================================
def parse_salary(text):
    """
    解析薪资格式为 (min, max, months)

    支持的格式:
    - "9-11k·14薪"      → (9000, 11000, 14)
    - "15k-25k"         → (15000, 25000, 12)
    - "1.5-2.5万/月"    → (15000, 25000, 12)
    - "200-250元/天"    → (4400, 5500, 12)    按22天折算
    - "20-40万/年"      → (16666, 33333, 12)
    - "薪资面议"         → (None, None, None)
    - "薪"              → (None, None, None)   ← 修复: "薪" 开头视为无效
    """
    if not text:
        return None, None, None
    # "薪资面议" / "面议" / "面谈" / 以"薪"带头的不完整字符串
    if any(w in text for w in ["面议", "面谈", "薪资面议"]):
        return None, None, None
    # 只有"薪"字且很短 → 无效
    if text.strip() in ("薪",):
        return None, None, None

    text = text.strip()
    months = 12

    # 提取年薪月数
    m = re.search(r"[\xb7\s]*(\d+)\s*薪", text)
    if m:
        months = int(m.group(1))
        text = re.sub(r"[\xb7\s]*\d+\s*薪", "", text).strip()

    # 判断是否为年薪 (必须在月数提取之后)
    is_yearly = False
    if "年" in text and "月" not in text:
        is_yearly = True

    values = []

    # 1. X-Yk / X-YK 共享单位 (核心修复)
    m = re.match(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)\s*[kK]", text)
    if m:
        values.append(float(m.group(1)) * 1000)
        values.append(float(m.group(2)) * 1000)

    # 2. X-Y万 共享单位
    if not values:
        m = re.match(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)\s*万", text)
        if m:
            values.append(float(m.group(1)) * 10000)
            values.append(float(m.group(2)) * 10000)

    # 3. X-Y千 共享单位
    if not values:
        m = re.match(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)\s*千", text)
        if m:
            values.append(float(m.group(1)) * 1000)
            values.append(float(m.group(2)) * 1000)

    # 4. X-Y元/天 日薪格式
    if not values:
        m = re.match(r"(\d+)\s*[-~]\s*(\d+)\s*元/天", text)
        if m:
            values.append(int(m.group(1)) * 22)
            values.append(int(m.group(2)) * 22)

    # 5. 单值带K匹配
    if not values:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[kK]", text):
            values.append(float(m.group(1)) * 1000)

    # 6. 单值带万匹配
    if not values:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*万", text):
            v = float(m.group(1)) * 10000
            if not any(abs(v - n) < 100 for n in values):
                values.append(v)

    # 7. 单值带千匹配
    if not values:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*千", text):
            v = float(m.group(1)) * 1000
            if not any(abs(v - n) < 100 for n in values):
                values.append(v)

    # 8. 纯数字备用: "15000-25000元/月"
    if not values:
        for m in re.finditer(r"(\d{4,})", text):
            v = float(m.group(1))
            if 1000 <= v <= 500000:
                values.append(v)

    if len(values) < 2:
        return None, None, None

    lo, hi = min(values), max(values)
    if is_yearly:
        lo, hi = lo / 12, hi / 12
    return int(lo), int(hi), months


# ============================================================
# 城市/学历/经验 解析
# ============================================================
def parse_city(text):
    if not text:
        return "", ""
    parts = re.split(r"[\xb7\-－]", text.strip(), maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip().rstrip("市"), parts[1].strip()
    m = re.match(r"^(.+?)市(.+)$", text.strip())
    if m:
        return m.group(1), m.group(2)
    return text.strip().rstrip("市"), ""


EDU_MAP = {
    "博士": "博士", "硕士": "硕士", "研究生": "硕士",
    "统招本科": "本科", "本科": "本科", "学士": "本科",
    "大专": "大专", "专科": "大专",
    "高中": "高中", "中专": "中专",
    "学历不限": "不限", "不限": "不限"
}

def norm_edu(t):
    if not t: return "不限"
    for k, v in EDU_MAP.items():
        if k in t: return v
    return t


EXP_MAP = {
    "应届生": "应届生", "在校生": "应届生",
    "经验不限": "不限", "不限": "不限",
    "1年以内": "1年以内",
    "1年以上": "1-3年", "2年以上": "1-3年", "3年以上": "3-5年",
    "1-3年": "1-3年", "2年": "1-3年", "3年": "3-5年",
    "3-5年": "3-5年", "4年": "3-5年", "5年": "3-5年",
    "5-10年": "5-10年", "6年": "5-10年", "8年": "5-10年",
    "10年以上": "10年以上",
}

def norm_exp(t):
    if not t: return "不限"
    for k, v in EXP_MAP.items():
        if k in t: return v
    return t


# ============================================================
# SQL
# ============================================================
UPSERT_SQL = """INSERT INTO jobs (
    source,source_job_id,job_name,company_name,salary_min,salary_max,salary_month,
    city,district,experience,education,skills,job_description,industry,company_size,
    publish_date,crawl_time,url
) VALUES (
    %(source)s,%(source_job_id)s,%(job_name)s,%(company_name)s,
    %(salary_min)s,%(salary_max)s,%(salary_month)s,
    %(city)s,%(district)s,%(experience)s,%(education)s,
    %(skills)s,%(job_description)s,%(industry)s,%(company_size)s,
    %(publish_date)s,%(crawl_time)s,%(url)s
) ON DUPLICATE KEY UPDATE
    job_name=VALUES(job_name),company_name=VALUES(company_name),
    salary_min=VALUES(salary_min),salary_max=VALUES(salary_max),
    salary_month=VALUES(salary_month),city=VALUES(city),district=VALUES(district),
    experience=VALUES(experience),education=VALUES(education),
    skills=VALUES(skills),job_description=VALUES(job_description),
    industry=VALUES(industry),company_size=VALUES(company_size),
    publish_date=VALUES(publish_date),crawl_time=VALUES(crawl_time),url=VALUES(url)"""


# ============================================================
# 浏览器内 fetch 翻页 JS
# ============================================================
FETCH_PAGE_JS = """async (opts) => {
    const body = {
        data: {
            mainSearchPcConditionForm: {
                city: opts.cityCode, dq: '410', pubTime: '',
                currentPage: opts.page, pageSize: 40, key: opts.keyword,
                suggestTag: '', workYearCode: '', compId: '', compName: '',
                compTag: '', industry: '', salaryCode: '', jobKind: '',
                compScale: '', compKind: '', compStage: '', eduLevel: '',
                salaryLow: '', salaryHigh: ''
            },
            passThroughForm: {
                scene: 'init', skId: '', fkId: '',
                ckId: opts.ckId, suggest: null
            }
        }
    };
    const resp = await fetch('https://api-c.liepin.com/api/com.liepin.searchfront4c.pc-search-job', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json;charset=UTF-8',
            'X-Requested-With': 'XMLHttpRequest',
        },
        body: JSON.stringify(body),
        credentials: 'include'
    });
    return await resp.json();
}"""


# ============================================================
# 从列表 API 数据构建基础行
# ============================================================
def card_to_row(job, comp):
    jid = str(job.get("jobId", ""))
    sal_min, sal_max, sal_mon = parse_salary(job.get("salary", ""))
    city, district = parse_city(job.get("dq", ""))

    # labels 中过滤可能的技能标签（排除学历、经验类词汇）
    raw_labels = job.get("labels", []) or []
    EXCLUDED_LABELS = {"本科", "大专", "硕士", "博士", "应届", "在校", "实习生"}
    skill_labels = [l for l in raw_labels if l not in EXCLUDED_LABELS and len(l) > 1]

    return {
        "source": "liepin",
        "source_job_id": jid,
        "job_name": job.get("title", ""),
        "company_name": comp.get("compName", ""),
        "salary_min": sal_min,
        "salary_max": sal_max,
        "salary_month": sal_mon,
        "city": city,
        "district": district,
        "experience": norm_exp(job.get("requireWorkYears", "")),
        "education": norm_edu(job.get("requireEduLevel", "")),
        "skills": json.dumps(skill_labels, ensure_ascii=False),
        "job_description": "",
        "industry": comp.get("compIndustry", ""),
        "company_size": comp.get("compScale", ""),
        "publish_date": str(job.get("refreshTime", ""))[:8],
        "crawl_time": datetime.now().isoformat(),
        "url": job.get("link", ""),
        "_detail_url": job.get("link", ""),
    }


# ============================================================
# 详情页抓取 JS
# ============================================================
DETAIL_EXTRACT_JS = """() => {
    // ????: .job-intro-container ???????????
    let desc = "";
    const descSels = [
        ".job-intro-container", ".job-detail", ".job-main .content",
        "[class*=job-intro]", "[class*=job-desc]", ".detail-content"
    ];
    for (const s of descSels) {
        const el = document.querySelector(s);
        if (el) { desc = (el.innerText || el.textContent || "").trim(); break; }
    }

    // ????: ????????????
    const skills = [];
    const jobArea = document.querySelector(".job-intro-container, .job-detail, [class*=job-intro]");
    const root = jobArea || document.body;
    root.querySelectorAll("[class*=tag], [class*=label], [class*=skill], [class*=keyword]").forEach(el => {
        const t = (el.innerText || el.textContent || "").trim();
        if (t && t.length > 1 && t.length < 30) skills.push(t);
    });

    return { description: desc.slice(0, 20000), skills: [...new Set(skills)].slice(0, 20) };
}"""


def fetch_detail(page, detail_url, max_retries=2):
    """访问职位详情页获取描述和技能"""
    if not detail_url:
        return "", []

    for attempt in range(max_retries):
        try:
            page.goto(detail_url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(1500)
            result = page.evaluate(DETAIL_EXTRACT_JS)
            desc = result.get("description", "")
            skills = result.get("skills", [])
            if desc or skills:
                return desc, skills
            page.wait_for_timeout(1500)
            result = page.evaluate(DETAIL_EXTRACT_JS)
            desc = result.get("description", "")
            skills = result.get("skills", [])
            if desc or skills:
                return desc, skills
        except Exception:
            page.wait_for_timeout(1000)
    return "", []


# ============================================================
# 主函数
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="猎聘数据采集 v2")
    parser.add_argument("-k", "--keywords", default="Python")
    parser.add_argument("-c", "--cities", default="北京")
    parser.add_argument("--max", type=int, default=20, dest="max_jobs")
    parser.add_argument("--no-detail", action="store_true", help="跳过详情页抓取")
    parser.add_argument("--headless", type=lambda x: x.lower() != "false", default=True)
    args = parser.parse_args()

    keywords = [k.strip() for k in args.keywords.split(",")]
    cities = [c.strip() for c in args.cities.split(",")]
    max_jobs = args.max_jobs
    fetch_details = not args.no_detail

    mysql_cfg = load_yaml(os.path.join(ROOT, "config", "mysql.yaml"))
    db = pymysql.connect(
        host=mysql_cfg["host"], port=mysql_cfg["port"],
        user=mysql_cfg["user"], password=mysql_cfg["password"],
        database=mysql_cfg["database"], charset="utf8mb4"
    )

    today = datetime.now().strftime("%Y-%m-%d")
    out_dir = os.path.join(DATA_DIR, "liepin", today)
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "jobs.jsonl")

    seen = set()
    job_count = 0
    all_rows = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        ctx = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            locale="zh-CN"
        )
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        # XHR 拦截器
        page.add_init_script("""
            window.__lpData = null;
            window.__lpCkId = null;
            const _o = XMLHttpRequest.prototype.open;
            XMLHttpRequest.prototype.open = function(m, u) {
                this.__u = u;
                return _o.apply(this, arguments);
            };
            const _s = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.send = function(b) {
                if (this.__u && this.__u.includes('pc-search-job') &&
                    !this.__u.includes('init') && !this.__u.includes('hot')) {
                    this.addEventListener('load', function() {
                        try {
                            const d = JSON.parse(this.responseText);
                            window.__lpData = d;
                            window.__lpCkId = d?.data?.passThroughData?.ckId || '';
                        } catch(e) {}
                    });
                }
                return _s.apply(this, arguments);
            };
        """)

        for kw in keywords:
            for ct_name in cities:
                if job_count >= max_jobs:
                    break
                city_code = LP_CITY.get(ct_name, "010")
                url = f"https://www.liepin.com/zhaopin/?key={kw}&city={city_code}&page=0"
                logger.info(f"[搜索] {kw} @ {ct_name} ({city_code})")

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=20000)
                except Exception as e:
                    logger.warning(f"  页面加载失败: {e}")
                    continue

                time.sleep(8)

                data = page.evaluate("window.__lpData")
                ckId = page.evaluate("window.__lpCkId") or ""

                if not data or data.get("flag") != 1:
                    logger.warning("  API 未加载，可能需要验证码")
                    continue

                cards = data.get("data", {}).get("data", {}).get("jobCardList", [])
                pag = data.get("data", {}).get("pagination", {})
                total_pages = pag.get("totalPage", 1)
                total_count = pag.get("totalCounts", 0)
                logger.info(f"  第1页: {len(cards)}条 | 共{total_count}条 {total_pages}页")

                for card in cards:
                    if job_count >= max_jobs:
                        break
                    j = card.get("job", {})
                    c = card.get("comp", {})
                    jid = str(j.get("jobId", ""))
                    if not jid or jid in seen:
                        continue
                    seen.add(jid)
                    all_rows.append(card_to_row(j, c))
                    job_count += 1

                # 翻页
                for pg in range(1, min(total_pages, 15)):
                    if job_count >= max_jobs or not ckId:
                        break
                    time.sleep(0.8)
                    try:
                        next_data = page.evaluate(FETCH_PAGE_JS, {
                            "cityCode": city_code, "page": pg,
                            "keyword": kw, "ckId": ckId
                        })
                    except Exception as e:
                        logger.warning(f"  翻页{pg+1}失败: {e}")
                        break

                    if not next_data or next_data.get("flag") != 1:
                        break

                    cards2 = next_data.get("data", {}).get("data", {}).get("jobCardList", [])
                    logger.info(f"  第{pg+1}页: {len(cards2)}条")
                    for card in cards2:
                        if job_count >= max_jobs:
                            break
                        j = card.get("job", {})
                        c = card.get("comp", {})
                        jid = str(j.get("jobId", ""))
                        if not jid or jid in seen:
                            continue
                        seen.add(jid)
                        all_rows.append(card_to_row(j, c))
                        job_count += 1

                if job_count >= max_jobs:
                    break
            if job_count >= max_jobs:
                break

        # ============================================================
        # Phase 2: 详情页抓取
        # ============================================================
        if fetch_details and all_rows:
            logger.info(f"\n[详情] 开始抓取 {len(all_rows)} 条职位描述...")
            detail_page = ctx.new_page()
            detail_page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )

            for i, row in enumerate(all_rows):
                detail_url = row.get("_detail_url", "")
                if not detail_url:
                    continue

                desc, skills = fetch_detail(detail_page, detail_url)

                if desc:
                    row["job_description"] = desc
                if skills:
                    existing = json.loads(row["skills"]) if row["skills"] else []
                    merged = list(dict.fromkeys(existing + skills))
                    row["skills"] = json.dumps(merged, ensure_ascii=False)

                if (i + 1) % 5 == 0 or i == len(all_rows) - 1:
                    has_desc_n = sum(1 for r in all_rows if r["job_description"])
                    logger.info(f"  详情: {i+1}/{len(all_rows)} | 有描述: {has_desc_n}")

                time.sleep(1.0)

            detail_page.close()

        # 清理临时字段
        for row in all_rows:
            row.pop("_detail_url", None)

        browser.close()

    # ============================================================
    # JSON + MySQL 双写
    # ============================================================
    with open(out_file, "w", encoding="utf-8") as f:
        for row in all_rows:
            item = dict(row)
            if isinstance(item["skills"], str):
                item["skills"] = json.loads(item["skills"])
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    ins = ups = 0
    with db.cursor() as cur:
        for row in all_rows:
            cur.execute(UPSERT_SQL, row)
            if cur.rowcount == 1:
                ins += 1
            else:
                ups += 1
        db.commit()
    db.close()

    has_desc = sum(1 for r in all_rows if r["job_description"])
    has_salary = sum(1 for r in all_rows if r["salary_min"] is not None)
    logger.info(f"\n[完成] 共 {job_count} 条")
    logger.info(f"  JSON: {out_file}")
    logger.info(f"  MySQL: {ins} 新增 / {ups} 更新")
    logger.info(f"  有薪资: {has_salary}/{job_count}  有描述: {has_desc}/{job_count}")


if __name__ == "__main__":
    main()
