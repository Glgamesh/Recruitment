# -*- coding: utf-8 -*-
"""数据采集系统 — 自然语言查询解析器"""

from __future__ import annotations
import logging
import os
import re
from typing import TYPE_CHECKING

from .utils import load_yaml

if TYPE_CHECKING:
    from .models import ScrapeRequest

logger = logging.getLogger(__name__)

SOURCE_ALIASES = {
    "boss": ["boss", "boss直聘", "直聘", "b"],
    "job51": ["51", "51job", "前程无忧", "前程", "无忧"],
    "liepin": ["猎聘", "猎", "liepin"],
    "zhaopin": ["智联", "智联招聘", "zhaopin"],
}

_DEFAULT_CITIES = ["北京", "上海", "广州", "深圳", "杭州", "成都", "南京", "武汉", "西安", "苏州"]

# 所有平台别名的扁平列表（用于过滤）
_ALL_SOURCE_ALIASES = [a for aliases in SOURCE_ALIASES.values() for a in aliases]


def _load_cities(config_dir: str | None = None) -> list[str]:
    if config_dir is None:
        config_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config"
        )
    path = os.path.join(config_dir, "cities.yaml")
    try:
        data = load_yaml(path)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return _DEFAULT_CITIES


def parse_natural_language(text: str, config_dir: str | None = None) -> dict | None:
    """解析自然语言查询文本"""
    if not text or not text.strip():
        return None

    text = text.strip()
    available_cities = _load_cities(config_dir)

    # ---- 1. 城市识别 ----
    cities_found: list[str] = []
    for city in available_cities:
        if city in text:
            cities_found.append(city)
    cities: list[str] = list(dict.fromkeys(cities_found))

    if not cities and any(w in text for w in ["不限城市", "全国", "不限"]):
        cities = available_cities[:4]

    # ---- 2. 来源过滤 ----
    sources: list[str] | None = None
    text_lower = text.lower()
    for source_name, aliases in SOURCE_ALIASES.items():
        for alias in aliases:
            if alias in text_lower or alias in text:
                if sources is None:
                    sources = []
                if source_name not in sources:
                    sources.append(source_name)
                break

    # ---- 3. 参数提取 ----
    max_count = 20
    m = re.search(r"(?:最多|前|只取|只要|最多取)\s*(\d+)\s*条", text)
    if m:
        max_count = int(m.group(1))

    fetch_details = True
    if any(w in text for w in ["不要详情", "跳过详情", "无详情", "只看列表"]):
        fetch_details = False

    # ---- 4. 关键词提取 ----
    remaining = text
    for c in cities_found:
        remaining = remaining.replace(c, " ")
    for alias in _ALL_SOURCE_ALIASES:
        remaining = re.sub(re.escape(alias), " ", remaining, flags=re.IGNORECASE)
    for kw in ["最多", "条", "前", "只取", "只要", "不要详情", "跳过详情", "无详情", "只看列表",
               "不限城市", "全国", "不限", "查", "搜", "搜索", "查询", "找", "看看",
               "并", "与", "和", "的", "了", "啊"]:
        remaining = remaining.replace(kw, " ")

    keywords_raw: list[str] = []
    tech_patterns = [
        r"[A-Za-z+#]+\s*(?:Agent|开发|工程师|架构师|运维|测试|前端|后端|全栈|算法)",
        r"(?:AI|ai)\s*Agent",
        r"(?:大模型|大语言模型|智能体|Agent开发|RAG|LangChain|AutoGPT)",
        r"[A-Za-z+#]+",
        r"[\u4e00-\u9fff]+(?:开发|工程师|分析|架构|管理|设计|测试|运维|运营|产品)",
        r"[\u4e00-\u9fff]{2,6}",
    ]
    for pattern in tech_patterns:
        matches = re.findall(pattern, remaining)
        for match in matches:
            match = match.strip()
            if len(match) >= 2 and match not in keywords_raw:
                keywords_raw.append(match)

    if not keywords_raw:
        en_words = re.findall(r"[A-Za-z+#]{2,}", remaining)
        keywords_raw = en_words

    if not keywords_raw:
        keywords_raw = [w for w in remaining.split() if len(w.strip()) >= 2]

    # 过滤来源别名
    keywords = [k for k in dict.fromkeys(keywords_raw)
                if k.lower() not in [a.lower() for a in _ALL_SOURCE_ALIASES]]

    if not keywords and not cities:
        return None
    if not keywords:
        keywords = ["Python"]
    if not cities:
        cities = ["北京"]
    if max_count < 1:
        max_count = 20

    return {
        "keywords": keywords,
        "cities": cities,
        "max_per_keyword_city": max_count,
        "sources": sources,
        "fetch_details": fetch_details,
        "headless": True,
    }


def build_from_args(keywords_str: str, cities_str: str,
                     max_count: int = 20, sources_str: str | None = None,
                     fetch_details: bool = True, headless: bool = True) -> dict:
    keywords = [k.strip() for k in keywords_str.split(",") if k.strip()]
    cities = [c.strip() for c in cities_str.split(",") if c.strip()]
    sources = None
    if sources_str:
        sources = [s.strip() for s in sources_str.split(",") if s.strip()]

    return {
        "keywords": keywords,
        "cities": cities,
        "max_per_keyword_city": max_count,
        "sources": sources,
        "fetch_details": fetch_details,
        "headless": headless,
    }
