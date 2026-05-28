# -*- coding: utf-8 -*-
"""智联招聘数据采集器 — DrissionPage+JS版

- DrissionPage 绕过 EdgeOne WAF
- JS 提取 DOM 数据（绕过 DrissionPage CSS引擎兼容问题）
- 点击卡片获取职位描述浮层
"""

from __future__ import annotations
import json
import logging
import os
import re
import time
import random
from datetime import datetime
from typing import TYPE_CHECKING

from urllib.parse import quote

from core.base import BaseScraper
from core.registry import register_scraper
from core.utils import parse_salary, norm_edu, norm_exp, parse_city
from core.db_writer import DBWriter

if TYPE_CHECKING:
    from core.models import ScrapeRequest

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME_DATA = os.path.join(ROOT, "chrome_data_zhaopin")

WELFARE_BLACKLIST = {
    "五险一金", "六险一金", "公积金", "周末双休", "双休",
    "年终奖", "绩效奖金", "带薪年假", "年假", "餐补",
    "交通补助", "交通补贴", "房补", "住房补贴", "股票期权",
    "弹性工作", "定期体检", "团建", "节日福利", "生日福利",
    "下午茶", "零食", "包吃", "包住", "补充医疗", "商业保险",
    "全额公积金", "全额社保", "不打卡", "不加班", "免费班车",
    "健身房", "员工旅游", "通讯补贴", "高温补贴", "采暖补贴",
    "专业培训", "晋升空间", "扁平管理", "领导好", "大牛",
    "导师带教", "导师", "氛围好", "最佳雇主", "上市公司",
    "已上市", "民营", "国企", "五险", "一金", "股份制企业", "有限责任公司", "股份有限公司", "外资", "合资", "国企", "央企", "事业单位",
}

# JS: 从页面提取所有卡片数据 + 元素索引映射
EXTRACT_CARDS_JS = """
    var cards = document.querySelectorAll('.joblist-box__item');
    var result = [];
    for (var i = 0; i < cards.length; i++) {
        var card = cards[i];
        var titleEl = card.querySelector('a.jobinfo__name');
        if (!titleEl) continue;
        var salaryEl = card.querySelector('p.jobinfo__salary');
        var companyEl = card.querySelector('a[class*="company"]');
        var infoItems = card.querySelectorAll('div.jobinfo__other-info-item');
        var tagEls = card.querySelectorAll('div[class*="joblist-box__item-tag"]');
        var infoArr = [];
        for (var j = 0; j < infoItems.length; j++) infoArr.push(infoItems[j].innerText.trim());
        var tagArr = [];
        for (var k = 0; k < tagEls.length; k++) tagArr.push(tagEls[k].innerText.trim());
        result.push({
            title: titleEl.innerText.trim(),
            url: titleEl.href || '',
            salary: salaryEl ? salaryEl.innerText.trim() : '',
            company: companyEl ? companyEl.innerText.trim() : '',
            info: infoArr,
            tags: tagArr,
        });
    }
    return result;
"""


@register_scraper("zhaopin")
class ZhaopinScraper(BaseScraper):
    """智联招聘采集器 — DrissionPage + JS"""

    name = "zhaopin"
    display_name = "智联招聘"
    tool_type = "drission"
    requires_login = False
    max_per_query = 100

    def __init__(self, config_dir: str | None = None):
        super().__init__(config_dir)
        self._db_writer = DBWriter(config_dir)
        self._page = None

    def setup(self):
        os.makedirs(CHROME_DATA, exist_ok=True)
        from DrissionPage import ChromiumPage, ChromiumOptions
        co = ChromiumOptions()
        co.set_argument("--disable-blink-features=AutomationControlled")
        co.set_argument(f"--user-data-dir={CHROME_DATA}")
        co.auto_port()
        logger.info("[zhaopin] 打开浏览器...")
        self._page = ChromiumPage(co)

    def teardown(self):
        if self._page:
            try: self._page.quit()
            except Exception: pass
            self._page = None

    def _extract_from_js(self, card_data: dict, today: str) -> dict | None:
        """从JS提取的卡片数据构造标准行"""
        jurl = card_data.get("url", "")
        jid = ""
        m = re.search(r"jobdetail/([A-Z0-9]+)", jurl)
        if m: jid = m.group(1)
        if not jid: return None

        sal_min, sal_max, sal_mon = parse_salary(card_data.get("salary", ""))

        # 城市/经验/学历
        city_text = ""; experience = ""; education = ""
        for txt in card_data.get("info", []):
            if "·" in txt or "市" in txt or "省" in txt:
                city_text = txt
            elif any(w in txt for w in ["年", "经验", "应届"]):
                experience = txt
            elif any(w in txt for w in ["本科", "硕士", "博士", "大专", "高中", "中专", "学历"]):
                education = txt

        city, district = parse_city(city_text)

        # skills/welfare 分离
        import re as _re2
        _size_pattern = _re2.compile(r"^\d+[-~]?\d*人$")
        skills = []; welfare_list = []
        for tag in card_data.get("tags", []):
            if tag in WELFARE_BLACKLIST or _size_pattern.match(tag):
                welfare_list.append(tag)
            else:
                skills.append(tag)

        return {
            "source": "zhaopin", "source_job_id": jid,
            "job_name": card_data.get("title", ""),
            "company_name": card_data.get("company", ""),
            "salary_min": sal_min, "salary_max": sal_max, "salary_month": sal_mon,
            "city": city, "district": district,
            "experience": norm_exp(experience), "education": norm_edu(education),
            "skills": json.dumps(skills, ensure_ascii=False),
            "welfare": json.dumps(welfare_list, ensure_ascii=False),
            "job_description": "", "industry": "", "company_size": "",
            "publish_date": today,
            "crawl_time": datetime.now().isoformat(), "url": jurl,
        }

    def _fetch_detail_hover(self, all_rows: list[dict]) -> int:
        """通过悬停卡片获取职位描述（轮询等待，无需登录）"""
        ok = 0
        hover_delay = float(self._sources_config.get("hover_delay", 0.8))

        for i in range(len(all_rows)):
            try:
                # 触发悬停
                self._page.run_js(f"""
                    var cards = document.querySelectorAll('.joblist-box__item');
                    if (cards[{i}]) {{
                        var title = cards[{i}].querySelector('a.jobinfo__name');
                        if (title) {{
                            ['mouseenter','mouseover','pointerenter'].forEach(function(evt) {{
                                title.dispatchEvent(new MouseEvent(evt, {{bubbles: true}}));
                            }});
                            cards[{i}].dispatchEvent(new MouseEvent('mouseenter', {{bubbles: true}}));
                        }}
                    }}
                """)

                # 轮询等待描述出现（最多2秒，每0.3秒检查一次）
                desc = ""
                for _poll in range(7):
                    time.sleep(0.3)
                    desc = self._page.run_js("""
                        var body = document.body.innerText;
                        var start = body.indexOf('\u804c\u4f4d\u63cf\u8ff0');
                        if (start < 0) return '';
                        var endMarkers = ['\u4efb\u804c\u8981\u6c42', '\u5c97\u4f4d\u8981\u6c42',
                            '\u4efb\u804c\u8d44\u683c', '\u516c\u53f8\u4fe1\u606f',
                            '\u8054\u7cfb\u65b9\u5f0f', '\u5de5\u4f5c\u5730\u5740'];
                        var end = body.length;
                        for (var j = 0; j < endMarkers.length; j++) {
                            var idx = body.indexOf(endMarkers[j], start + 4);
                            if (idx > start && idx < end) end = idx;
                        }
                        return body.slice(start, end).trim().slice(0, 10000);
                    """)
                    if desc and len(desc) > 10:
                        break

                if desc and len(desc) > 10:
                    all_rows[i]["job_description"] = desc
                    ok += 1

                # 移开鼠标
                self._page.run_js(
                    "document.body.dispatchEvent(new MouseEvent('mouseleave', {bubbles: true}));"
                )
                time.sleep(random.uniform(0.15, 0.3))

            except Exception as e:
                logger.debug("  悬停详情失败: %s", e)
        return ok

    def collect(self, request: "ScrapeRequest") -> list[dict]:
        if not self._page:
            raise RuntimeError("[zhaopin] 浏览器未初始化")

        keywords = request.keywords
        cities = request.cities
        max_jobs = request.max_per_keyword_city
        pages = max(request.options.get("pages", 1), 1)
        fetch_details = request.fetch_details

        seen: set[str] = set()
        job_count = 0
        all_rows: list[dict] = []
        today = datetime.now().strftime("%Y-%m-%d")

        for kw in keywords:
            for ct in cities:
                if job_count >= max_jobs: break
                city_id = self.map_city(ct)
                if not city_id: continue

                for pg in range(1, pages + 1):
                    if job_count >= max_jobs: break
                    url = f"https://sou.zhaopin.com/?kw={quote(kw)}&city={city_id}&p={pg}"
                    logger.info("[zhaopin] %s @ %s 第%d页", kw, ct, pg)

                    try:
                        self._page.get(url, timeout=20)
                        self._page.wait(random.uniform(3, 5))

                        cards_data = None
                        for _retry in range(3):
                            cards_data = self._page.run_js(EXTRACT_CARDS_JS)
                            if cards_data and len(cards_data) > 0:
                                break
                            self._page.wait(1)
                        if not cards_data:
                            logger.warning("  卡片数据为空")
                            continue
                        logger.info("  %d cards", len(cards_data))

                        for card_data in cards_data:
                            if job_count >= max_jobs: break
                            row = self._extract_from_js(card_data, today)
                            if row is None: continue
                            jid = row["source_job_id"]
                            if jid in seen: continue
                            seen.add(jid)
                            all_rows.append(row)
                            job_count += 1

                    except Exception as e:
                        logger.error("  Page error: %s", e)

                if job_count >= max_jobs: break

        # 详情抓取（内联方式：在搜索页直接点击卡片提取描述浮层）
        if fetch_details and all_rows:
            logger.info("[zhaopin] 抓取 %d 条详情...", len(all_rows))
            detail_ok = self._fetch_detail_hover(all_rows)
            logger.info("  详情完成: %d/%d", detail_ok, len(all_rows))

        self._db_writer.write(all_rows, "zhaopin")
        return all_rows
