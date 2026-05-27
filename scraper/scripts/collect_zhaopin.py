#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""智联招聘数据采集脚本 v2 —— 修正选择器"""

import argparse, json, logging, os, re, time, random
from datetime import datetime
from urllib.parse import quote
import pymysql, yaml
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
ZP_CITY = {"北京":"530","上海":"538","广州":"763","深圳":"765","杭州":"653","成都":"801","南京":"635","武汉":"736","西安":"854","苏州":"639"}

def load_yaml(path):
    with open(path,"r",encoding="utf-8") as f: return yaml.safe_load(f)

def parse_salary(text):
    if not text or any(w in text for w in ["面议","面谈"]): return None,None,12
    months=12
    m=re.search(r"[\xb7\s]*(\d+)\s*薪",text)
    if m: months=int(m.group(1)); text=re.sub(r"[\xb7\s]*\d+\s*薪","",text)
    is_yearly="/年" in text or "年" in text
    rm=re.match(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*万",text)
    if rm: lo,hi=float(rm.group(1))*10000,float(rm.group(2))*10000
    else:
        nums=[]
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[kK]",text): nums.append(float(m.group(1))*1000)
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*万",text): nums.append(float(m.group(1))*10000)
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*千",text): nums.append(float(m.group(1))*1000)
        if len(nums)<2: return None,None,months
        lo,hi=min(nums[0],nums[1]),max(nums[0],nums[1])
    if is_yearly: lo,hi=lo/12,hi/12
    return int(lo),int(hi),months

def parse_city(text):
    if not text: return "",""
    parts=re.split(r"[\xb7\-－]",text.strip(),maxsplit=1)
    if len(parts)==2: return parts[0].strip().rstrip("市"),parts[1].strip()
    m=re.match(r"^(.+?)市(.+)$",text.strip())
    if m: return m.group(1),m.group(2)
    return text.strip().rstrip("市"),""

EDU_MAP={"博士":"博士","硕士":"硕士","研究生":"硕士","本科":"本科","学士":"本科","大专":"大专","专科":"大专","高中":"高中","中专":"中专","学历不限":"不限","不限":"不限"}
def norm_edu(t):
    if not t: return "不限"
    for k,v in EDU_MAP.items():
        if k in t: return v
    return t

EXP_MAP={"应届生":"应届生","经验不限":"不限","不限":"不限","1年以内":"1年以内","1-3年":"1-3年","3-5年":"3-5年","5-10年":"5-10年","10年以上":"10年以上"}
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

def main():
    parser=argparse.ArgumentParser(description="智联招聘数据采集")
    parser.add_argument("-k","--keywords",default="Python")
    parser.add_argument("-c","--cities",default="北京")
    parser.add_argument("--max",type=int,default=20,dest="max_jobs")
    parser.add_argument("--pages",type=int,default=1,help="搜索页数")
    args=parser.parse_args()

    keywords=[k.strip() for k in args.keywords.split(",")]
    cities=[c.strip() for c in args.cities.split(",")]
    max_jobs=args.max_jobs
    pages=args.pages

    mysql_cfg=load_yaml(os.path.join(ROOT,"config","mysql.yaml"))
    db=pymysql.connect(host=mysql_cfg["host"],port=mysql_cfg["port"],user=mysql_cfg["user"],
        password=mysql_cfg["password"],database=mysql_cfg["database"],charset="utf8mb4")

    today=datetime.now().strftime("%Y-%m-%d")
    out_dir=os.path.join(DATA_DIR,"zhaopin",today)
    os.makedirs(out_dir,exist_ok=True)
    out_file=os.path.join(out_dir,"jobs.jsonl")

    seen=set(); job_count=0; all_rows=[]

    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        ctx=browser.new_context(viewport={"width":1920,"height":1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            locale="zh-CN")
        page=ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

        for kw in keywords:
            for ct in cities:
                if job_count>=max_jobs: break
                for pg in range(1,pages+1):
                    if job_count>=max_jobs: break
                    city_id=ZP_CITY.get(ct,"530")
                    url=f"https://sou.zhaopin.com/?kw={quote(kw)}&city={city_id}&p={pg}"
                    logger.info(f"{kw}@{ct} page {pg}")

                    try:
                        page.goto(url,wait_until="domcontentloaded",timeout=20000)
                        time.sleep(random.uniform(2,3))
                        cards=page.query_selector_all("div.joblist-box__item")
                        logger.info(f"  {len(cards)} cards")

                        for card in cards[:max_jobs-job_count]:
                            try:
                                # 职位名称
                                title_el=card.query_selector("a.jobinfo__name")
                                job_name=title_el.inner_text().strip() if title_el else ""
                                jurl=title_el.get_attribute("href") if title_el else ""

                                # 职位ID
                                jid=""
                                m=re.search(r"jobdetail/([A-Z0-9]+)",jurl)
                                if m: jid=m.group(1)
                                if not jid: jid=str(abs(hash(jurl)))
                                if jid in seen: continue
                                seen.add(jid)

                                # 薪资
                                salary_el=card.query_selector("p.jobinfo__salary")
                                salary_text=salary_el.inner_text().strip() if salary_el else ""

                                # 公司
                                company_el=card.query_selector("a[class*='company']")
                                company_name=company_el.inner_text().strip() if company_el else ""

                                # 城市/经验/学历
                                info_items=card.query_selector_all("div.jobinfo__other-info-item")
                                city_text=""; experience=""; education=""
                                for item in info_items:
                                    txt=item.inner_text().strip()
                                    if "·" in txt or "市" in txt or "省" in txt:
                                        city_text=txt
                                    elif any(w in txt for w in ["年","经验","应届"]):
                                        experience=txt
                                    elif any(w in txt for w in ["本科","硕士","博士","大专","高中","中专","学历"]):
                                        education=txt

                                # 技能标签
                                skill_els=card.query_selector_all("div.joblist-box__item-tag")
                                skills=[]
                                for s in skill_els:
                                    txt=s.inner_text().strip()
                                    if txt and txt not in ["最佳雇主","上市公司","已上市","民营","国企"]:
                                        skills.append(txt)

                                sal_min,sal_max,sal_mon=parse_salary(salary_text)
                                city,district=parse_city(city_text)

                                row={
                                    "source":"zhaopin","source_job_id":jid,
                                    "job_name":job_name,"company_name":company_name,
                                    "salary_min":sal_min,"salary_max":sal_max,"salary_month":sal_mon,
                                    "city":city,"district":district,
                                    "experience":norm_exp(experience),"education":norm_edu(education),
                                    "skills":json.dumps(skills,ensure_ascii=False),
                                    "job_description":"","industry":"","company_size":"",
                                    "publish_date":today,"crawl_time":datetime.now().isoformat(),"url":jurl,
                                }
                                all_rows.append(row); job_count+=1
                            except Exception as e:
                                logger.debug(f"  Card: {e}")
                    except Exception as e:
                        logger.error(f"  Page: {e}")
                if job_count>=max_jobs: break

        browser.close()

    # JSON
    with open(out_file,"w",encoding="utf-8") as f:
        for row in all_rows:
            item=dict(row); item["skills"]=json.loads(item["skills"])
            f.write(json.dumps(item,ensure_ascii=False)+"\n")

    # MySQL
    ins=ups=0
    with db.cursor() as cur:
        for row in all_rows:
            cur.execute(UPSERT_SQL,row)
            if cur.rowcount==1: ins+=1
            else: ups+=1
        db.commit()
    db.close()
    logger.info(f"DONE: {job_count} items | JSON={out_file} | MySQL={ins}new/{ups}upd")

if __name__=="__main__":
    main()
