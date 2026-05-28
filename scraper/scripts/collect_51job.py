#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""前程无忧数据采集 (兼容入口)

推荐使用统一 CLI:  python run.py -k Python -c 北京 -s job51 --max 20
"""

import argparse
import logging
import os
import sys

SCRAPER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRAPER_DIR)

from core.models import ScrapeRequest
from collectors.job51 import Job51Scraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def main():
    parser = argparse.ArgumentParser(description="前程无忧数据采集 (兼容入口)")
    parser.add_argument("-k", "--keywords", default="Python")
    parser.add_argument("-c", "--cities", default="北京")
    parser.add_argument("--max", type=int, default=20, dest="max_jobs")
    parser.add_argument("--pages", type=int, default=1, help="翻页数(每页20条)")
    parser.add_argument("--headless", type=lambda x: x.lower() != "false", default=True)
    args = parser.parse_args()

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    cities = [c.strip() for c in args.cities.split(",") if c.strip()]

    request = ScrapeRequest(
        keywords=keywords,
        cities=cities,
        max_per_keyword_city=args.max_jobs,
        sources=["job51"],
        headless=args.headless,
        options={"pages": args.pages},
    )

    scraper = Job51Scraper()
    scraper.collect(request)


if __name__ == "__main__":
    main()
