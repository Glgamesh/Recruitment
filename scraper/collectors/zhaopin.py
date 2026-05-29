# -*- coding: utf-8 -*-
"""智联招聘数据采集器 — DrissionPage+JS版 (v2: CDP定位+关键词迭代修复)

- DrissionPage 绕过 EdgeOne WAF
- CDP 覆盖地理位置实现城市筛选
- JS 提取 DOM 数据
- 悬停浮层获取详情
"""

from __future__ import annotations
import json, logging, os, re, time, random
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

# 城市坐标映射（用于 CDP 地理位置覆盖）


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
    "已上市", "民营", "国企", "五险", "一金",
    "股份制企业", "有限责任公司", "股份有限公司",
    "外资", "合资", "央企", "事业单位",
}

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
    """智联招聘采集器 — DrissionPage+JS (jl参数城市筛选)"""

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

        city_text = ""; experience = ""; education = ""
        for txt in card_data.get("info", []):
            if "市" in txt or "省" in txt or "·" in txt:
                city_text = txt
            elif any(w in txt for w in ["年", "经验", "应届"]):
                experience = txt
            elif any(w in txt for w in ["本科", "硕士", "博士", "大专", "高中", "中专", "学历"]):
                education = txt

        city, district = parse_city(city_text)
        skills = []; welfare = []
        for tag in card_data.get("tags", []):
            tag_clean = tag.strip()
            if not tag_clean: continue
            if tag_clean in WELFARE_BLACKLIST:
                welfare.append(tag_clean)
            elif re.match(r"^\d+人$", tag_clean) or re.match(r"^\d+-\d+人$", tag_clean):
                continue
            else:
                skills.append(tag_clean)

        return {
            "source": "zhaopin",
            "source_job_id": jid,
            "job_name": card_data.get("title", ""),
            "company_name": card_data.get("company", ""),
            "salary_min": sal_min,
            "salary_max": sal_max,
            "salary_month": sal_mon,
            "city": city,
            "district": district,
            "experience": norm_exp(experience),
            "education": norm_edu(education),
            "skills": json.dumps(skills, ensure_ascii=False) if skills else None,
            "welfare": json.dumps(welfare, ensure_ascii=False) if welfare else None,
            "job_description": "",
            "industry": "",
            "company_size": "",
            "publish_date": today,
            "url": jurl,
        }

    def _fetch_detail_hover(self, all_rows: list[dict]) -> int:
        """悬停提取职位描述 — 轮询检测浮层"""
        page = self._page
        ok = 0
        for i in range(len(all_rows)):
            if all_rows[i].get("job_description"):
                ok += 1; continue
            try:
                page.run_js(f"""
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
                desc = ""
                for _poll in range(7):
                    time.sleep(0.3)
                    desc = page.run_js("""
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
                    if desc and len(desc) > 10: break
                if desc and len(desc) > 10:
                    all_rows[i]["job_description"] = desc
                    ok += 1
                page.run_js("document.body.dispatchEvent(new MouseEvent('mouseleave', {bubbles: true}));")
                time.sleep(random.uniform(0.15, 0.3))
            except Exception as e:
                logger.debug("  悬停详情失败: %s", e)
        return ok

    def collect(self, request: "ScrapeRequest") -> list[dict]:
        if not self._page:
            raise RuntimeError("[zhaopin] 浏览器未初始化")

        keywords = request.keywords
        cities = request.cities
        max_jobs = request.max_per_keyword_city  # 每个关键词×城市组合的最大条数
        pages = max(request.options.get("pages", 1), 1)
        fetch_details = request.fetch_details

        seen: set[str] = set()
        all_rows: list[dict] = []
        today = datetime.now().strftime("%Y-%m-%d")

        for kw in keywords:
            for ct in cities:
                combo_count = 0
                city_rows = []
                city_id = self.map_city(ct)
                if not city_id:
                    continue

                for pg in range(1, pages + 1):
                    if combo_count >= max_jobs:
                        break

                    url = f"https://sou.zhaopin.com/?kw={quote(kw)}&jl={city_id}&p={pg}"
                    logger.info("[zhaopin] %s @ %s p%d (max=%d)", kw, ct, pg, max_jobs)

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
                            if combo_count >= max_jobs:
                                break
                            row = self._extract_from_js(card_data, today)
                            if row is None:
                                continue
                            jid = row["source_job_id"]
                            if jid in seen:
                                continue
                            seen.add(jid)
                            city_rows.append(row)
                            combo_count += 1

                    except Exception as e:
                        logger.error("  Page error: %s", e)

                # === 每个城市采集完后立即抓取详情（页面还在当前城市） ===
                if fetch_details and city_rows:
                    logger.info("  [zhaopin] %s @%s 抓取 %d 条详情...", kw, ct, len(city_rows))
                    detail_ok = self._fetch_detail_hover(city_rows)
                    logger.info("  详情完成: %d/%d", detail_ok, len(city_rows))

                all_rows.extend(city_rows)

        self._db_writer.write(all_rows, "zhaopin")
        return all_rows
