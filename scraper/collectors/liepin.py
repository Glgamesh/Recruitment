# -*- coding: utf-8 -*-
"""猎聘数据采集器 — 基于核心框架重构

- XHR 拦截获取列表 API 数据
- 浏览器内 fetch 翻页
- 详情页抓取职位描述和技能标签
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

# 浏览器内 fetch 翻页 JS
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

# 详情页抓取 JS
DETAIL_EXTRACT_JS = """() => {
    let desc = "";
    const descSels = [
        ".job-intro-container", ".job-detail", ".job-main .content",
        "[class*=job-intro]", "[class*=job-desc]", ".detail-content"
    ];
    for (const s of descSels) {
        const el = document.querySelector(s);
        if (el) { desc = (el.innerText || el.textContent || "").trim(); break; }
    }
    const skills = [];
    const jobArea = document.querySelector(".job-intro-container, .job-detail, [class*=job-intro]");
    const root = jobArea || document.body;
    root.querySelectorAll("[class*=tag], [class*=label], [class*=skill], [class*=keyword]").forEach(el => {
        const t = (el.innerText || el.textContent || "").trim();
        if (t && t.length > 1 && t.length < 30) skills.push(t);
    });
    return { description: desc.slice(0, 20000), skills: [...new Set(skills)].slice(0, 20) };
}"""

EXCLUDED_LABELS = {"本科", "大专", "硕士", "博士", "应届", "在校", "实习生"}


@register_scraper("liepin")
class LiepinScraper(BaseScraper):
    """猎聘采集器"""

    name = "liepin"
    display_name = "猎聘"
    tool_type = "playwright"
    requires_login = True
    max_per_query = 100

    def __init__(self, config_dir: str | None = None):
        super().__init__(config_dir)
        self._db_writer = DBWriter(config_dir)

    def _card_to_row(self, job: dict, comp: dict) -> dict:
        jid = str(job.get("jobId", ""))
        sal_min, sal_max, sal_mon = parse_salary(job.get("salary", ""))
        city, district = parse_city(job.get("dq", ""))
        raw_labels = job.get("labels", []) or []
        skill_labels = [l for l in raw_labels if l not in EXCLUDED_LABELS and len(l) > 1]

        return {
            "source": "liepin",
            "source_job_id": jid,
            "job_name": job.get("title", ""),
            "company_name": comp.get("compName", ""),
            "salary_min": sal_min, "salary_max": sal_max, "salary_month": sal_mon,
            "city": city, "district": district,
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

    def _fetch_detail(self, page, detail_url: str, max_retries: int = 2) -> tuple[str, list[str]]:
        if not detail_url:
            return "", []

        for _ in range(max_retries):
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

    def collect(self, request: "ScrapeRequest") -> list[dict]:
        keywords = request.keywords
        cities = request.cities
        max_jobs = request.max_per_keyword_city
        fetch_details = request.fetch_details
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
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )

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
                    city_code = self.map_city(ct_name)
                    if not city_code:
                        continue

                    url = f"https://www.liepin.com/zhaopin/?key={kw}&city={city_code}&page=0"
                    logger.info("[liepin] 搜索 %s @ %s (%s)", kw, ct_name, city_code)

                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    except Exception as e:
                        logger.warning("  页面加载失败: %s", e)
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
                    logger.info("  第1页: %d条 | 共%d条 %d页", len(cards), total_count, total_pages)

                    for card in cards:
                        if job_count >= max_jobs:
                            break
                        j = card.get("job", {})
                        c = card.get("comp", {})
                        jid = str(j.get("jobId", ""))
                        if not jid or jid in seen:
                            continue
                        seen.add(jid)
                        all_rows.append(self._card_to_row(j, c))
                        job_count += 1

                    # 翻页
                    for pg in range(1, min(total_pages, 15)):
                        if job_count >= max_jobs or not ckId:
                            break
                        time.sleep(0.8)
                        try:
                            next_data = page.evaluate(FETCH_PAGE_JS, {
                                "cityCode": city_code, "page": pg,
                                "keyword": kw, "ckId": ckId,
                            })
                        except Exception as e:
                            logger.warning("  翻页%d失败: %s", pg + 1, e)
                            break

                        if not next_data or next_data.get("flag") != 1:
                            break

                        cards2 = next_data.get("data", {}).get("data", {}).get("jobCardList", [])
                        logger.info("  第%d页: %d条", pg + 1, len(cards2))
                        for card in cards2:
                            if job_count >= max_jobs:
                                break
                            j = card.get("job", {})
                            c = card.get("comp", {})
                            jid = str(j.get("jobId", ""))
                            if not jid or jid in seen:
                                continue
                            seen.add(jid)
                            all_rows.append(self._card_to_row(j, c))
                            job_count += 1

                    if job_count >= max_jobs:
                        break
                if job_count >= max_jobs:
                    break

            # 详情抓取
            if fetch_details and all_rows:
                logger.info("[liepin] 抓取 %d 条详情...", len(all_rows))
                detail_page = ctx.new_page()
                detail_page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )

                for i, row in enumerate(all_rows):
                    detail_url = row.get("_detail_url", "")
                    if not detail_url:
                        continue
                    desc, skills = self._fetch_detail(detail_page, detail_url)
                    if desc:
                        row["job_description"] = desc
                    if skills:
                        existing = json.loads(row["skills"]) if row["skills"] else []
                        merged = list(dict.fromkeys(existing + skills))
                        row["skills"] = json.dumps(merged, ensure_ascii=False)
                    if (i + 1) % 5 == 0 or i == len(all_rows) - 1:
                        has_desc_n = sum(1 for r in all_rows if r["job_description"])
                        logger.info("  详情: %d/%d | 有描述: %d", i + 1, len(all_rows), has_desc_n)
                    time.sleep(1.0)

                detail_page.close()

            # 清理临时字段
            for row in all_rows:
                row.pop("_detail_url", None)

            browser.close()

        self._db_writer.write(all_rows, "liepin")
        return all_rows


