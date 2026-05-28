#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""BOSS直聘数据采集 (兼容入口)

推荐使用统一 CLI:  python run.py -k Python -c 北京 -s boss --max 20
"""

import argparse
import logging
import os
import sys

# 确保可以从项目根目录导入
SCRAPER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRAPER_DIR)

from core.models import ScrapeRequest
from collectors.boss import BossScraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def main():
    parser = argparse.ArgumentParser(description="BOSS直聘数据采集 (兼容入口)")
    parser.add_argument("-k", "--keywords", default="Python")
    parser.add_argument("-c", "--cities", default="北京")
    parser.add_argument("--max", type=int, default=20, dest="max_jobs")
    parser.add_argument("--no-detail", action="store_true")
    args = parser.parse_args()

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    cities = [c.strip() for c in args.cities.split(",") if c.strip()]

    request = ScrapeRequest(
        keywords=keywords,
        cities=cities,
        max_per_keyword_city=args.max_jobs,
        sources=["boss"],
        fetch_details=not args.no_detail,
    )

    scraper = BossScraper()
    try:
        scraper.setup()
        scraper.collect(request)
    finally:
        scraper.teardown()


if __name__ == "__main__":
    main()
