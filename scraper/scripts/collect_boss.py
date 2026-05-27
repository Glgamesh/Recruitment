#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
BOSS直聘数据采集 v2
- DrissionPage 浏览器全程保持打开（Cookie 不丢失）
- 登录态自动检测 + 轮询等待
- 列表 API + 详情 API（每4条刷新）
- MySQL + JSON 双写
"""

import argparse, json, logging, os, re, time
from datetime import datetime
import pymysql, yaml, requests
from DrissionPage import ChromiumPage, ChromiumOptions

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
CHROME_DATA = os.path.join(ROOT, "chrome_data_boss")
os.makedirs(CHROME_DATA, exist_ok=True)

BOSS_CITY = {
    "北京":"101010100","上海":"101020100","广州":"101280100",
    "深圳":"101280600","杭州":"101210100","成都":"101270100",
}

def load_yaml(p):
    with open(p,"r",encoding="utf-8") as f: return yaml.safe_load(f)

# ========== 薪资解析 ==========
def parse_salary(text):
    if not text or "面议" in text: return None, None, 12
    text = text.strip(); months = 12
    m = re.search(r"[\xb7\s]*(\d+)\s*薪", text)
    if m: months = int(m.group(1)); text = re.sub(r"[\xb7\s]*\d+\s*薪", "", text).strip()
    is_yearly = "年" in text and "月" not in text
    # X-Yk
    m = re.match(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)\s*[kK]", text)
    if m: lo, hi = float(m.group(1))*1000, float(m.group(2))*1000
    else:
        vals = []
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[kK]", text): vals.append(float(m.group(1))*1000)
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*万", text):
            v = float(m.group(1))*10000
            if not any(abs(v-n)<100 for n in vals): vals.append(v)
        if len(vals) < 2: return None, None, months
        lo, hi = min(vals), max(vals)
    if is_yearly: lo, hi = lo/12, hi/12
    return int(lo), int(hi), months

EDU_MAP = {"博士":"博士","硕士":"硕士","研究生":"硕士","本科":"本科","学士":"本科","统招本科":"本科","大专":"大专","专科":"大专","高中":"高中","中专":"中专","学历不限":"不限","不限":"不限"}
def norm_edu(t):
    if not t: return "不限"
    for k,v in EDU_MAP.items():
        if k in t: return v
    return t

EXP_MAP = {"应届生":"应届生","在校生":"应届生","经验不限":"不限","不限":"不限","无需经验":"不限","1年以内":"1年以内","1-3年":"1-3年","3-5年":"3-5年","5-10年":"5-10年","10年以上":"10年以上","1年以上":"1-3年","2年以上":"1-3年","3年以上":"3-5年","5年以上":"5-10年"}
def norm_exp(t):
    if not t: return "不限"
    for k,v in EXP_MAP.items():
        if k in t: return v
    return t

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


def build_session(page):
    """从当前浏览器页面构建 requests.Session"""
    sess = requests.Session()
    for c in page.cookies():
        sess.cookies.set(c["name"], c["value"], domain=".zhipin.com")
    return sess


def refresh_page(page):
    """刷新页面保持 cookie 活跃（心跳）"""
    logger.info("  [心跳] 刷新...")
    page.refresh()
    page.wait(4)
    page.scroll.to_bottom()
    page.wait(1)
    page.scroll.to_top()
    page.wait(1)


def extract_job(j):
    """从列表 API 提取标准字段"""
    jid = j.get("encryptJobId", "")
    s_min, s_max, s_mon = parse_salary(j.get("salaryDesc", ""))
    skills = j.get("skills", []) or []
    welfare = j.get("welfareList", []) or []
    return {
        "source": "boss",
        "source_job_id": jid,
        "job_name": j.get("jobName", ""),
        "company_name": j.get("brandName", ""),
        "salary_min": s_min, "salary_max": s_max, "salary_month": s_mon,
        "city": j.get("cityName", ""),
        "district": j.get("areaDistrict", ""),
        "experience": norm_exp(j.get("jobExperience", "")),
        "education": norm_edu(j.get("jobDegree", "")),
        "skills": json.dumps(skills + welfare, ensure_ascii=False),
        "job_description": "",
        "industry": j.get("brandIndustry", ""),
        "company_size": j.get("brandScaleName", ""),
        "publish_date": "",
        "crawl_time": datetime.now().isoformat(),
        "url": f"https://www.zhipin.com/job_detail/{jid}.html",
        "_security_id": j.get("securityId", ""),
    }


def fetch_detail(sess, security_id, headers):
    """获取职位详情"""
    if not security_id: return "", ""
    try:
        r = sess.get(
            "https://www.zhipin.com/wapi/zpgeek/job/detail.json",
            params={"securityId": security_id, "scene": "1"},
            headers=headers, timeout=10
        )
        if r.status_code != 200: return "", ""
        d = r.json()
        if d.get("code") != 0: return "", ""
        zp = d.get("zpData", {})
        ji = zp.get("jobInfo", {}) or {}
        desc = ji.get("jobDetail", "") or ji.get("postDescription", "") or ""
        pub = (ji.get("activeTimeDesc", "") or "")[:10]
        return desc, pub
    except Exception:
        return "", ""


def main():
    parser = argparse.ArgumentParser(description="BOSS直聘数据采集 v2")
    parser.add_argument("-k","--keywords",default="Python")
    parser.add_argument("-c","--cities",default="北京")
    parser.add_argument("--max",type=int,default=20,dest="max_jobs")
    parser.add_argument("--no-detail",action="store_true")
    args = parser.parse_args()

    keywords = [k.strip() for k in args.keywords.split(",")]
    cities = [c.strip() for c in args.cities.split(",")]
    max_jobs = args.max_jobs
    fetch_details = not args.no_detail

    mysql_cfg = load_yaml(os.path.join(ROOT,"config","mysql.yaml"))
    db = pymysql.connect(host=mysql_cfg["host"],port=mysql_cfg["port"],
        user=mysql_cfg["user"],password=mysql_cfg["password"],
        database=mysql_cfg["database"],charset="utf8mb4")

    today = datetime.now().strftime("%Y-%m-%d")
    out_dir = os.path.join(DATA_DIR,"boss",today)
    os.makedirs(out_dir,exist_ok=True)
    out_file = os.path.join(out_dir,"jobs.jsonl")

    # ========== 打开浏览器（全程保持） ==========
    co = ChromiumOptions()
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument(f"--user-data-dir={CHROME_DATA}")
    logger.info("打开浏览器...")
    page = ChromiumPage(co)

    # 检测登录状态
    page.get("https://www.zhipin.com/web/geek/job?query=Python&city=101010100", timeout=20)
    page.wait(6)
    is_logged = "user" not in page.url and "passport" not in page.url
    logger.info(f"登录状态: {'已登录' if is_logged else '未登录'}")

    if not is_logged:
        logger.info("等待登录（浏览器已打开，请完成登录）...")
        for i in range(120):  # 最多等6分钟
            try:
                if "user" not in page.url and "passport" not in page.url:
                    logger.info("检测到登录成功！")
                    is_logged = True
                    break
            except: pass
            if i % 20 == 0: logger.info("  等待中...")
            time.sleep(3)
        if not is_logged:
            logger.error("登录超时")
            page.quit(); db.close(); return

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.zhipin.com/web/geek/job",
    }

    seen = set(); job_count = 0; all_rows = []

    try:
        for kw in keywords:
            for ct_name in cities:
                if job_count >= max_jobs: break
                city_code = BOSS_CITY.get(ct_name)
                if not city_code:
                    logger.warning(f"未知城市: {ct_name}")
                    continue

                # 导航到搜索页
                logger.info(f"[搜索] {kw} @ {ct_name}")
                page.get(f"https://www.zhipin.com/web/geek/job?query={kw}&city={city_code}", timeout=20)
                page.wait(6)

                if "user" in page.url or "passport" in page.url:
                    logger.warning("  登录态失效，跳过")
                    continue

                sess = build_session(page)
                headers["Referer"] = f"https://www.zhipin.com/web/geek/job?query={kw}&city={city_code}"

                # 列表 API
                r = sess.get(
                    "https://www.zhipin.com/wapi/zpgeek/search/joblist.json",
                    params={"scene":"1","query":kw,"city":city_code,"page":"1","pageSize":"30"},
                    headers=headers, timeout=15
                )
                data = r.json()
                if data.get("code") != 0:
                    logger.warning(f"  API 失败: {data.get('message')}")
                    continue

                job_list = data.get("zpData",{}).get("jobList",[])
                total = data.get("zpData",{}).get("resCount",0)
                has_more = data.get("zpData",{}).get("hasMore",False)
                logger.info(f"  第1页: {len(job_list)}条 (共{total}条)")

                for j in job_list:
                    if job_count >= max_jobs: break
                    jid = j.get("encryptJobId","")
                    if not jid or jid in seen: continue
                    seen.add(jid)
                    all_rows.append(extract_job(j))
                    job_count += 1

                # 翻页
                pg = 2
                while has_more and job_count < max_jobs:
                    time.sleep(0.5)
                    r = build_session(page).get(
                        "https://www.zhipin.com/wapi/zpgeek/search/joblist.json",
                        params={"scene":"1","query":kw,"city":city_code,"page":str(pg),"pageSize":"30"},
                        headers=headers, timeout=15
                    )
                    data = r.json()
                    if data.get("code") != 0: break
                    job_list = data.get("zpData",{}).get("jobList",[])
                    has_more = data.get("zpData",{}).get("hasMore",False)
                    logger.info(f"  第{pg}页: {len(job_list)}条")
                    for j in job_list:
                        if job_count >= max_jobs: break
                        jid = j.get("encryptJobId","")
                        if not jid or jid in seen: continue
                        seen.add(jid)
                        all_rows.append(extract_job(j))
                        job_count += 1
                    pg += 1
                    # 每3页刷新一次
                    if pg % 3 == 0:
                        refresh_page(page)

                if job_count >= max_jobs: break
            if job_count >= max_jobs: break

        # ========== 详情抓取 ==========
        if fetch_details and all_rows:
            logger.info(f"\n[详情] 抓取 {len(all_rows)} 条...")
            detail_ok = 0
            for i, row in enumerate(all_rows):
                sid = row.get("_security_id","")
                if not sid: continue

                # 每4条刷新
                if i > 0 and i % 4 == 0:
                    refresh_page(page)

                sess = build_session(page)
                desc, pub = fetch_detail(sess, sid, headers)
                if desc:
                    row["job_description"] = desc[:10000]
                    detail_ok += 1
                if pub:
                    row["publish_date"] = pub

                if (i+1) % 5 == 0 or i == len(all_rows)-1:
                    logger.info(f"  详情: {i+1}/{len(all_rows)} | OK:{detail_ok}")

    finally:
        page.quit()

    # 清理
    for row in all_rows:
        row.pop("_security_id", None)

    # ========== JSON + MySQL ==========
    with open(out_file,"w",encoding="utf-8") as f:
        for row in all_rows:
            item = dict(row)
            if isinstance(item["skills"],str): item["skills"] = json.loads(item["skills"])
            f.write(json.dumps(item,ensure_ascii=False)+"\n")

    ins = ups = 0
    with db.cursor() as cur:
        for row in all_rows:
            cur.execute(UPSERT_SQL, row)
            if cur.rowcount == 1: ins += 1
            else: ups += 1
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
