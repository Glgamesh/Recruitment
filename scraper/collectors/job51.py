# -*- coding: utf-8 -*-
"""前程无忧(51job)数据采集器 — 基于核心框架重构

- Playwright 自动处理阿里云 WAF
- 原生响应拦截 + JS 注入双保障
- 页面点击翻页
- 使用 DBWriter 统一写入
"""

from __future__ import annotations
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING

from playwright.sync_api import sync_playwright

from core.base import BaseScraper
from core.registry import register_scraper
from core.utils import parse_salary, norm_edu, norm_exp, parse_city
from core.db_writer import DBWriter

if TYPE_CHECKING:
    from core.models import ScrapeRequest

logger = logging.getLogger(__name__)

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

# 从标签列表中排除学历/经验等非技能标签
EXCLUDE_TAGS = {
    "应届", "在校", "本科", "大专", "硕士", "博士", "中专", "高中",
    "经验不限", "无需经验", "1年以内", "1年以上", "1年以下",
    "2年以上", "2年以下", "3年以上", "3年以下", "5年以上", "5年以下",
    "10年以上", "1年及以上", "2年及以上", "3年及以上", "5年及以上",
    "1-3年", "3-5年", "5-10年",
}


@register_scraper("job51")
class Job51Scraper(BaseScraper):
    """前程无忧采集器"""

    name = "job51"
    display_name = "前程无忧"
    tool_type = "playwright"
    requires_login = False
    max_per_query = 100

    def __init__(self, config_dir: str | None = None):
        super().__init__(config_dir)
        self._db_writer = DBWriter(config_dir)

    # ---- 数据提取 ----
    def _extract_job(self, j: dict) -> dict:
        jid = str(j.get("jobId", ""))
        sal_min = j.get("jobSalaryMin")
        sal_max = j.get("jobSalaryMax")
        s_min, s_max, s_mon = parse_salary(j.get("provideSalaryString", ""))
        # 如果有直接的 min/max 数值，优先使用
        if sal_min is not None and sal_max is not None and sal_min != "" and sal_max != "":
            s_min, s_max, s_mon = int(sal_min), int(sal_max), 12

        city, district = parse_city(j.get("jobAreaString", ""))
        raw_tags = j.get("jobTags", []) or []
        skills = [t for t in raw_tags if t not in EXCLUDE_TAGS
                  and not re.match(r"^\d+[-~]\d+?", str(t))
                  and not re.match(r"^\d+?", str(t))]
        welfare = j.get("jobWelfareCodeDataList", []) or []
        welfare_names = [w.get("chineseTitle", "") for w in welfare if w.get("chineseTitle")]
        # 从 skills 中移除与 welfare 重复的标签
        skills = [s for s in skills if s not in welfare_names]

        return {
            "source": "job51",
            "source_job_id": jid,
            "job_name": j.get("jobName", ""),
            "company_name": j.get("companyName", ""),
            "salary_min": s_min, "salary_max": s_max, "salary_month": s_mon,
            "city": city, "district": district,
            "experience": norm_exp(j.get("workYearString", "")),
            "education": norm_edu(j.get("degreeString", "")),
            "skills": json.dumps(skills, ensure_ascii=False),
            "welfare": json.dumps(welfare_names, ensure_ascii=False),
            "job_description": j.get("jobDescribe", ""),
            "industry": ", ".join(filter(None, [
                j.get("industryType1Str", ""), j.get("industryType2Str", "")
            ])),
            "company_size": j.get("companySizeString", ""),
            "publish_date": (j.get("issueDateString", "") or "")[:10],
            "crawl_time": datetime.now().isoformat(),
            "url": j.get("jobHref", ""),
        }

    # ---- 核心采集 ----
    def collect(self, request: "ScrapeRequest") -> list[dict]:
        keywords = request.keywords
        cities = request.cities
        max_jobs = request.max_per_keyword_city
        max_pages = max(request.options.get("pages", 1), 1)
        headless = request.headless

        seen: set[str] = set()
        job_count = 0
        all_rows: list[dict] = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=headless, args=["--no-sandbox"])
            ctx = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                locale="zh-CN",
            )
            page = ctx.new_page()
            page.add_init_script(PAGE_JS)

            # 原生响应拦截（更可靠）
            native_jobs: list = []

            def on_response(resp):
                if "search-pc" in resp.url and resp.status == 200:
                    try:
                        body = resp.body().decode("utf-8", errors="replace")
                        if body.startswith("{"):
                            d = json.loads(body)
                            if d.get("status") == "1":
                                items = d.get("resultbody", {}).get("job", {}).get("items", [])
                                if items:
                                    native_jobs.clear()
                                    native_jobs.extend(items)
                    except Exception:
                        pass

            page.on("response", on_response)

            for kw in keywords:
                for ct_name in cities:
                    if job_count >= max_jobs:
                        break
                    city_code = self.map_city(ct_name)
                    if not city_code:
                        continue

                    url = f"https://we.51job.com/pc/search?keyword={kw}&jobArea={city_code}"
                    logger.info("[job51] 搜索 %s @ %s", kw, ct_name)

                    try:
                        page.goto(url, wait_until="networkidle", timeout=30000)
                    except Exception as e:
                        logger.warning("  加载失败: %s", e)
                        continue
                    time.sleep(3)

                    for pg in range(max_pages):
                        if job_count >= max_jobs:
                            break

                        items = native_jobs if native_jobs else page.evaluate("window.__jobs")
                        total = page.evaluate("window.__jobTotal")

                        if not items:
                            logger.warning("  第%d页 无数据", pg + 1)
                            break

                        if pg == 0:
                            logger.info("  第1页: %d条(共%d条)", len(items), total)

                        for j in items:
                            if job_count >= max_jobs:
                                break
                            jid = str(j.get("jobId", ""))
                            if not jid or jid in seen:
                                continue
                            seen.add(jid)
                            all_rows.append(self._extract_job(j))
                            job_count += 1

                        # 翻页
                        if pg < max_pages - 1 and job_count < max_jobs:
                            native_jobs.clear()
                            try:
                                next_btn = page.query_selector(
                                    ".page-next:not(.disabled), [class*=pagination] [class*=next]:not([class*=disabled]), text=下一页")
                                if next_btn:
                                    logger.info("  翻到第%d页", pg + 2)
                                    next_btn.click()
                                    time.sleep(2.5)
                                else:
                                    logger.info("  无更多页")
                                    break
                            except Exception as e:
                                logger.warning("  翻页失败: %s", e)
                                break

                    if job_count >= max_jobs:
                        break
                if job_count >= max_jobs:
                    break
            browser.close()

        self._db_writer.write(all_rows, "job51")
        return all_rows


