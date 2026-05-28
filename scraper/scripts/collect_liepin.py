#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""猎聘数据采集 (兼容入口)

推荐使用统一 CLI:  python run.py -k Python -c 北京 -s liepin --max 20
"""

import argparse
import logging
import os
import sys

SCRAPER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRAPER_DIR)

from core.models import ScrapeRequest
from collectors.liepin import LiepinScraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def main():
    parser = argparse.ArgumentParser(description="猎聘数据采集 (兼容入口)")
    parser.add_argument("-k", "--keywords", default="Python")
    parser.add_argument("-c", "--cities", default="北京")
    parser.add_argument("--max", type=int, default=20, dest="max_jobs")
    parser.add_argument("--no-detail", action="store_true", help="跳过详情页抓取")
    parser.add_argument("--headless", type=lambda x: x.lower() != "false", default=True)
    args = parser.parse_args()

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    cities = [c.strip() for c in args.cities.split(",") if c.strip()]

    request = ScrapeRequest(
        keywords=keywords,
        cities=cities,
        max_per_keyword_city=args.max_jobs,
        sources=["liepin"],
        fetch_details=not args.no_detail,
        headless=args.headless,
    )

    scraper = LiepinScraper()
    scraper.collect(request)


if __name__ == "__main__":
    main()
