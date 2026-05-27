#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
前程无忧(51job) 数据采集 v2
- Playwright 自动处理阿里云 WAF
- 原生响应拦截 + JS 注入双保险
- 页面点击翻页
- MySQL + JSON 双写
"""

import argparse, json, logging, os, re, time
from datetime import datetime
import pymysql, yaml
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")

JOB51_CITY = {
    "北京":"010000","上海":"020000","广州":"030200","深圳":"040000",
    "杭州":"080200","成都":"090200","南京":"070200","武汉":"180200",
    "西安":"200200","苏州":"070300"
}

def load_yaml(p):
    with open(p,"r",encoding="utf-8") as f: return yaml.safe_load(f)

# ============ 薪资解析 ============
def parse_salary(text, sal_min=None, sal_max=None):
    if sal_min is not None and sal_max is not None and sal_min != "" and sal_max != "":
        return int(sal_min), int(sal_max), 12
    if not text or any(w in text for w in ["面议","面谈"]):
        return None, None, None
    text = text.strip(); months = 12
    m = re.search(r"[\xb7\s]*(\d+)\s*薪", text)
    if m: months = int(m.group(1)); text = re.sub(r"[\xb7\s]*\d+\s*薪","",text).strip()
    is_yearly = "年" in text and "月" not in text
    values = []
    for pat, mul in [(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)\s*[kK]",1000),
                     (r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)\s*万",10000),
                     (r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)\s*千",1000)]:
        m = re.match(pat, text)
        if m: values.extend([float(m.group(1))*mul, float(m.group(2))*mul]); break
    if not values:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[kK]",text): values.append(float(m.group(1))*1000)
    if not values:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*万",text):
            v=float(m.group(1))*10000
            if not any(abs(v-n)<100 for n in values): values.append(v)
    if not values:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*千",text):
            v=float(m.group(1))*1000
            if not any(abs(v-n)<100 for n in values): values.append(v)
    if not values:
        for m in re.finditer(r"(\d{4,})",text):
            v=float(m.group(1))
            if 1000<=v<=500000: values.append(v)
    if len(values)<2: return None,None,None
    lo,hi=min(values),max(values)
    if is_yearly: lo,hi=lo/12,hi/12
    return int(lo),int(hi),months

def parse_city(text):
    if not text: return "",""
    parts=re.split(r"[\xb7\-－]",text.strip(),maxsplit=1)
    if len(parts)==2: return parts[0].strip().rstrip("市"),parts[1].strip()
    return text.strip().rstrip("市"),""

EDU_MAP={"博士":"博士","硕士":"硕士","研究生":"硕士","统招本科":"本科","本科":"本科","学士":"本科","大专":"大专","专科":"大专","高中":"高中","中专":"中专","学历不限":"不限","不限":"不限"}
def norm_edu(t):
    if not t: return "不限"
    for k,v in EDU_MAP.items():
        if k in t: return v
    return t

EXP_MAP={"应届生":"应届生","在校生":"应届生","应届毕业生":"应届生","经验不限":"不限","不限":"不限","无需经验":"不限","1年以下":"1年以内","1年以内":"1年以内","1年及以上":"1-3年","1年以上":"1-3年","2年及以上":"1-3年","2年以上":"1-3年","3年及以上":"3-5年","3年以上":"3-5年","1-3年":"1-3年","3-5年":"3-5年","5年及以上":"5-10年","5年以上":"5-10年","5-10年":"5-10年","10年以上":"10年以上"}
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

# JS: 注入页面拦截 XHR + fetch 并存到 window.__jobs
PAGE_JS = """
window.__jobs = [];
window.__jobTotal = 0;
(function(){
    const _fetch = window.fetch;
    window.fetch = async function(...args){
        const resp = await _fetch.apply(this, args);
        const url = (args[0]&&typeof args[0]==='string')?args[0]:(args[0]?.url||'');
        if(url.includes('search-pc')){
            try{ const clone=resp.clone(); const d=await clone.json();
                if(d.status==='1'){ window.__jobs=d.resultbody?.job?.items||[];
                    window.__jobTotal=d.resultbody?.job?.totalCount||0; }
            }catch(e){}
        }
        return resp;
    };
})();
"""

EXCLUDE_TAGS = {"应届","在校","本科","大专","硕士","博士","中专","高中","经验不限","无需经验","1年以内","1年以下","1年以上","2年以上","3年以上","5年以上","10年以上","1年及以上","2年及以上","3年及以上","5年及以上","1-3年","3-5年","5-10年"}

def extract_job(j):
    jid=str(j.get("jobId",""))
    sal_min=j.get("jobSalaryMin"); sal_max=j.get("jobSalaryMax")
    s_min,s_max,s_mon=parse_salary(j.get("provideSalaryString",""),sal_min,sal_max)
    city,district=parse_city(j.get("jobAreaString",""))
    raw_tags=j.get("jobTags",[]) or []
    skills = [t for t in raw_tags if t not in EXCLUDE_TAGS
              and not re.match(r"^\d+[-~]\d+?", str(t))
              and not re.match(r"^\d+?", str(t))]
    welfare=j.get("jobWelfareCodeDataList",[]) or []
    welfare_names=[w.get("chineseTitle","") for w in welfare if w.get("chineseTitle")]
    return {
        "source":"job51","source_job_id":jid,
        "job_name":j.get("jobName",""),"company_name":j.get("companyName",""),
        "salary_min":s_min,"salary_max":s_max,"salary_month":s_mon,
        "city":city,"district":district,
        "experience":norm_exp(j.get("workYearString","")),
        "education":norm_edu(j.get("degreeString","")),
        "skills":json.dumps(skills+welfare_names,ensure_ascii=False),
        "job_description":j.get("jobDescribe",""),
        "industry":", ".join(filter(None,[j.get("industryType1Str",""),j.get("industryType2Str","")])),
        "company_size":j.get("companySizeString",""),
        "publish_date":(j.get("issueDateString","") or "")[:10],
        "crawl_time":datetime.now().isoformat(),
        "url":j.get("jobHref",""),
    }


def main():
    parser=argparse.ArgumentParser(description="前程无忧数据采集 v2")
    parser.add_argument("-k","--keywords",default="Python")
    parser.add_argument("-c","--cities",default="北京")
    parser.add_argument("--max",type=int,default=20,dest="max_jobs")
    parser.add_argument("--pages",type=int,default=1,help="翻页数(每页20条)")
    parser.add_argument("--headless",type=lambda x:x.lower()!="false",default=True)
    args=parser.parse_args()

    keywords=[k.strip() for k in args.keywords.split(",")]
    cities=[c.strip() for c in args.cities.split(",")]
    max_jobs=args.max_jobs; max_pages=args.pages

    mysql_cfg=load_yaml(os.path.join(ROOT,"config","mysql.yaml"))
    db=pymysql.connect(host=mysql_cfg["host"],port=mysql_cfg["port"],
        user=mysql_cfg["user"],password=mysql_cfg["password"],
        database=mysql_cfg["database"],charset="utf8mb4")

    today=datetime.now().strftime("%Y-%m-%d")
    out_dir=os.path.join(DATA_DIR,"job51",today)
    os.makedirs(out_dir,exist_ok=True)
    out_file=os.path.join(out_dir,"jobs.jsonl")

    seen=set(); job_count=0; all_rows=[]

    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=args.headless)
        ctx=browser.new_context(viewport={"width":1920,"height":1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            locale="zh-CN")
        page=ctx.new_page()
        page.add_init_script(PAGE_JS)

        # === 原生响应拦截 (更可靠) ===
        native_jobs = []
        def on_response(resp):
            if 'search-pc' in resp.url and resp.status==200:
                try:
                    body=resp.body().decode('utf-8',errors='replace')
                    if body.startswith('{'):
                        d=json.loads(body)
                        if d.get("status")=="1":
                            items=d.get("resultbody",{}).get("job",{}).get("items",[])
                            if items:
                                native_jobs.clear()
                                native_jobs.extend(items)
                except: pass
        page.on('response', on_response)

        for kw in keywords:
            for ct_name in cities:
                if job_count>=max_jobs: break
                city_code=JOB51_CITY.get(ct_name)
                if not city_code:
                    logger.warning(f"未知城市: {ct_name}")
                    continue

                url=f"https://we.51job.com/pc/search?keyword={kw}&jobArea={city_code}"
                logger.info(f"[搜索] {kw} @ {ct_name}")

                try:
                    page.goto(url,wait_until="networkidle",timeout=30000)
                except Exception as e:
                    logger.warning(f"  加载失败: {e}")
                    continue
                time.sleep(3)

                for pg in range(max_pages):
                    if job_count>=max_jobs: break

                    # 获取数据: 优先原生拦截, 回退到 JS 注入
                    items = native_jobs if native_jobs else page.evaluate("window.__jobs")
                    total = page.evaluate("window.__jobTotal")

                    if not items:
                        logger.warning(f"  第{pg+1}页: 无数据")
                        break

                    if pg==0:
                        logger.info(f"  第1页: {len(items)}条 (共{total}条)")

                    for j in items:
                        if job_count>=max_jobs: break
                        jid=str(j.get("jobId",""))
                        if not jid or jid in seen: continue
                        seen.add(jid)
                        all_rows.append(extract_job(j))
                        job_count+=1

                    # 翻页
                    if pg<max_pages-1 and job_count<max_jobs:
                        native_jobs.clear()
                        try:
                            next_btn=page.query_selector(
                                ".page-next:not(.disabled), [class*=pagination] [class*=next]:not([class*=disabled]), text=下一页")
                            if next_btn:
                                logger.info(f"  翻到第{pg+2}页")
                                next_btn.click()
                                time.sleep(2.5)
                            else:
                                logger.info("  无更多页")
                                break
                        except Exception as e:
                            logger.warning(f"  翻页失败: {e}")
                            break

                if job_count>=max_jobs: break
            if job_count>=max_jobs: break
        browser.close()

    # === JSON + MySQL ===
    with open(out_file,"w",encoding="utf-8") as f:
        for row in all_rows:
            item=dict(row)
            if isinstance(item["skills"],str): item["skills"]=json.loads(item["skills"])
            f.write(json.dumps(item,ensure_ascii=False)+"\n")

    ins=ups=0
    with db.cursor() as cur:
        for row in all_rows:
            cur.execute(UPSERT_SQL,row)
            if cur.rowcount==1: ins+=1
            else: ups+=1
        db.commit()
    db.close()

    has_desc=sum(1 for r in all_rows if r["job_description"])
    has_salary=sum(1 for r in all_rows if r["salary_min"] is not None)
    logger.info(f"\n[完成] 共 {job_count} 条")
    logger.info(f"  JSON: {out_file}")
    logger.info(f"  MySQL: {ins} 新增 / {ups} 更新")
    logger.info(f"  有薪资: {has_salary}/{job_count}  有描述: {has_desc}/{job_count}")

if __name__=="__main__":
    main()
