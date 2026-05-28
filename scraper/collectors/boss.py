# -*- coding: utf-8 -*-
"""Boss直聘数据采集器 — 基于核心框架重构"""

from __future__ import annotations
import json
import logging
import os
import time
from datetime import datetime
from typing import TYPE_CHECKING

import requests
from DrissionPage import ChromiumPage, ChromiumOptions

from core.base import BaseScraper
from core.registry import register_scraper
from core.utils import parse_salary, norm_edu, norm_exp
from core.db_writer import DBWriter

if TYPE_CHECKING:
    from core.models import ScrapeRequest

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME_DATA = os.path.join(ROOT, "chrome_data_boss")


@register_scraper("boss")
class BossScraper(BaseScraper):
    """Boss直聘采集器"""

    name = "boss"
    display_name = "Boss直聘"
    tool_type = "drission"
    requires_login = True
    max_per_query = 50

    def __init__(self, config_dir: str | None = None):
        super().__init__(config_dir)
        self._page: ChromiumPage | None = None
        self._db_writer = DBWriter(config_dir)

    def setup(self):
        """启动浏览器并确保已登录"""
        os.makedirs(CHROME_DATA, exist_ok=True)
        co = ChromiumOptions()
        co.set_argument("--disable-blink-features=AutomationControlled")
        co.set_argument(f"--user-data-dir={CHROME_DATA}")
        logger.info("[boss] 打开浏览器...")
        self._page = ChromiumPage(co)

        self._page.get("https://www.zhipin.com/web/geek/job?query=Python&city=101010100", timeout=20)
        self._page.wait(6)
        url = self._page.url
        is_logged = "user" not in url and "passport" not in url
        logger.info("[boss] 登录状态: %s", "已登录" if is_logged else "未登录")

        if not is_logged:
            logger.info("[boss] 等待登录（浏览器已打开，请完成登录）...")
            for i in range(120):
                try:
                    if "user" not in self._page.url and "passport" not in self._page.url:
                        logger.info("[boss] 检测到登录成功！")
                        is_logged = True
                        break
                except Exception:
                    pass
                if i % 20 == 0:
                    logger.info("  等待中...")
                time.sleep(3)
            if not is_logged:
                raise RuntimeError("[boss] 登录超时，请手动完成登录后重试")

    def teardown(self):
        """关闭浏览器"""
        if self._page:
            try:
                self._page.quit()
            except Exception:
                pass
            self._page = None

    def _build_session(self) -> requests.Session:
        """从当前浏览器页面构建 requests.Session"""
        sess = requests.Session()
        for c in self._page.cookies():
            sess.cookies.set(c["name"], c["value"], domain=".zhipin.com")
        return sess

    def _refresh_page(self):
        """刷新页面保持 cookie 活跃（心跳）"""
        logger.info("  [心跳] 刷新...")
        self._page.refresh()
        self._page.wait(4)
        self._page.scroll.to_bottom()
        self._page.wait(1)
        self._page.scroll.to_top()
        self._page.wait(1)

    def _get_headers(self) -> dict:
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.zhipin.com/web/geek/job",
        }

    def _extract_job(self, j: dict) -> dict:
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
            "skills": json.dumps(skills, ensure_ascii=False),
            "welfare": json.dumps(welfare, ensure_ascii=False),
            "job_description": "",
            "industry": j.get("brandIndustry", ""),
            "company_size": j.get("brandScaleName", ""),
            "publish_date": "",
            "crawl_time": datetime.now().isoformat(),
            "url": f"https://www.zhipin.com/job_detail/{jid}.html",
            "_security_id": j.get("securityId", ""),
        }

    def _fetch_detail(self, sess: requests.Session, security_id: str, headers: dict) -> tuple[str, str]:
        """获取职位详情"""
        if not security_id:
            return "", ""
        try:
            r = sess.get(
                "https://www.zhipin.com/wapi/zpgeek/job/detail.json",
                params={"securityId": security_id, "scene": "1"},
                headers=headers, timeout=10
            )
            if r.status_code != 200:
                return "", ""
            d = r.json()
            if d.get("code") != 0:
                return "", ""
            zp = d.get("zpData", {})
            ji = zp.get("jobInfo", {}) or {}
            desc = ji.get("jobDetail", "") or ji.get("postDescription", "") or ""
            pub = (ji.get("activeTimeDesc", "") or "")[:10]
            return desc, pub
        except Exception:
            return "", ""

    def collect(self, request: "ScrapeRequest") -> list[dict]:
        """执行采集，返回 JobItem 兼容的 dict 列表"""
        if not self._page:
            raise RuntimeError("[boss] 浏览器未初始化，请先调用 setup()")

        keywords = request.keywords
        cities = request.cities
        max_jobs = request.max_per_keyword_city
        fetch_details = request.fetch_details

        headers = self._get_headers()
        seen: set[str] = set()
        job_count = 0
        all_rows: list[dict] = []

        for kw in keywords:
            for ct_name in cities:
                if job_count >= max_jobs:
                    break
                city_code = self.map_city(ct_name)
                if not city_code:
                    continue

                logger.info("[boss] 搜索 %s @ %s", kw, ct_name)
                self._page.get(
                    f"https://www.zhipin.com/web/geek/job?query={kw}&city={city_code}",
                    timeout=20
                )
                self._page.wait(6)

                if "user" in self._page.url or "passport" in self._page.url:
                    logger.warning("  登录态失效，跳过")
                    continue

                sess = self._build_session()
                headers["Referer"] = f"https://www.zhipin.com/web/geek/job?query={kw}&city={city_code}"

                r = sess.get(
                    "https://www.zhipin.com/wapi/zpgeek/search/joblist.json",
                    params={"scene": "1", "query": kw, "city": city_code, "page": "1", "pageSize": "30"},
                    headers=headers, timeout=15
                )
                data = r.json()
                if data.get("code") != 0:
                    logger.warning("  API 失败: %s", data.get("message"))
                    continue

                job_list = data.get("zpData", {}).get("jobList", [])
                total = data.get("zpData", {}).get("resCount", 0)
                has_more = data.get("zpData", {}).get("hasMore", False)
                logger.info("  第1页: %d条(共%d条)", len(job_list), total)

                for j in job_list:
                    if job_count >= max_jobs:
                        break
                    jid = j.get("encryptJobId", "")
                    if not jid or jid in seen:
                        continue
                    seen.add(jid)
                    all_rows.append(self._extract_job(j))
                    job_count += 1

                pg = 2
                while has_more and job_count < max_jobs:
                    time.sleep(0.5)
                    r = self._build_session().get(
                        "https://www.zhipin.com/wapi/zpgeek/search/joblist.json",
                        params={"scene": "1", "query": kw, "city": city_code, "page": str(pg), "pageSize": "30"},
                        headers=headers, timeout=15
                    )
                    data = r.json()
                    if data.get("code") != 0:
                        break
                    job_list = data.get("zpData", {}).get("jobList", [])
                    has_more = data.get("zpData", {}).get("hasMore", False)
                    logger.info("  第%d页: %d条", pg, len(job_list))
                    for j in job_list:
                        if job_count >= max_jobs:
                            break
                        jid = j.get("encryptJobId", "")
                        if not jid or jid in seen:
                            continue
                        seen.add(jid)
                        all_rows.append(self._extract_job(j))
                        job_count += 1
                    pg += 1
                    if pg % 3 == 0:
                        self._refresh_page()

                if job_count >= max_jobs:
                    break
            if job_count >= max_jobs:
                break

        if fetch_details and all_rows:
            logger.info("[boss] 抓取 %d 条详情...", len(all_rows))
            detail_ok = 0
            for i, row in enumerate(all_rows):
                sid = row.get("_security_id", "")
                if not sid:
                    continue
                if i > 0 and i % 4 == 0:
                    self._refresh_page()
                sess = self._build_session()
                desc, pub = self._fetch_detail(sess, sid, headers)
                if desc:
                    row["job_description"] = desc[:10000]
                    detail_ok += 1
                if pub:
                    row["publish_date"] = pub
                if (i + 1) % 5 == 0 or i == len(all_rows) - 1:
                    logger.info("  详情: %d/%d | OK:%d", i + 1, len(all_rows), detail_ok)

        for row in all_rows:
            row.pop("_security_id", None)

        self._db_writer.write(all_rows, "boss")
        return all_rows

