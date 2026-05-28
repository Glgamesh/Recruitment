#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""招聘数据采集系统 — 统一 CLI 入口

用法:
    # 自然语言模式
    python run.py "查询成都的Agent相关岗位信息"
    python run.py "北京上海Python开发，只要Boss和猎聘，最多10条"

    # 结构化参数模式
    python run.py -k "Agent,AI Agent" -c 成都 -s boss,liepin --max 20

    # 管理命令
    python run.py --sources          # 列出已注册平台
"""

from __future__ import annotations
import argparse
import logging
import os
import sys

# 确保项目根目录在 path 中
SCRAPER_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRAPER_DIR)

from core.models import ScrapeRequest
from core.registry import dispatch, list_sources, _registry
from core.query_parser import parse_natural_language, build_from_args

# 触发全部采集器注册
import collectors  # noqa: F401

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("run")


def print_sources():
    """打印已注册的平台列表"""
    sources = list_sources()
    if not sources:
        print("没有注册的采集平台")
        return
    print(f"\n已注册的采集平台 ({len(sources)}):\n")
    print(f"{'名称':<12} {'显示名':<12} {'工具':<12} {'需登录':<6}")
    print("-" * 45)
    for s in sources:
        login = "是" if s["requires_login"] else "否"
        print(f"{s['name']:<12} {s['display_name']:<12} {s['tool_type']:<12} {login:<6}")
    print()


def run_natural_language(query: str):
    """自然语言查询模式"""
    logger.info("查询: %s", query)
    parsed = parse_natural_language(query)
    if not parsed:
        logger.error("无法解析查询，请使用结构化参数: -k 关键词 -c 城市")
        logger.info("示例: python run.py -k 'Agent' -c 成都 -s boss --max 10")
        return

    logger.info("解析结果: keywords=%s, cities=%s, sources=%s, max=%d",
                parsed["keywords"], parsed["cities"],
                parsed["sources"] or "全部", parsed["max_per_keyword_city"])

    request = ScrapeRequest(**parsed)
    results = dispatch(request, global_retries=args.retries)

    # 汇总输出
    total = sum(len(items) for items in results.values())
    print(f"\n{'=' * 50}")
    print(f"采集完成 — {len(results)} 个平台, 共 {total} 条")
    for source, items in results.items():
        print(f"  {source}: {len(items)} 条")
    print(f"{'=' * 50}")


def run_structured(args):
    """结构化参数模式"""
    parsed = build_from_args(
        keywords_str=args.keywords,
        cities_str=args.cities,
        max_count=args.max,
        sources_str=args.sources,
        fetch_details=not args.no_detail,
        headless=args.headless,
    )

    logger.info("keywords=%s, cities=%s, sources=%s, max=%d, headless=%s, detail=%s",
                parsed["keywords"], parsed["cities"],
                parsed["sources"] or "全部", parsed["max_per_keyword_city"],
                parsed["headless"], parsed["fetch_details"])

    request = ScrapeRequest(**parsed)
    results = dispatch(request, global_retries=args.retries)

    total = sum(len(items) for items in results.values())
    print(f"\n{'=' * 50}")
    print(f"采集完成 — {len(results)} 个平台, 共 {total} 条")
    for source, items in results.items():
        print(f"  {source}: {len(items)} 条")
    print(f"{'=' * 50}")


def main():
    parser = argparse.ArgumentParser(
        description="招聘数据采集系统 — 统一入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 自然语言模式
  python run.py "查询成都的Agent相关岗位信息"
  
  # 结构化参数
  python run.py -k "Agent,AI Agent" -c 成都 -s boss,liepin --max 20
  
  # 列出已注册平台
  python run.py --sources
        """,
    )

    parser.add_argument("query", nargs="?", help="自然语言查询文本")
    parser.add_argument("-k", "--keywords", default="Python",
                        help="搜索关键词，逗号分隔 (默认: Python)")
    parser.add_argument("-c", "--cities", default="北京",
                        help="目标城市，逗号分隔 (默认: 北京)")
    parser.add_argument("-s", "--sources", default=None,
                        help="采集平台，逗号分隔 (默认: 全部) 可选: boss,job51,liepin,zhaopin")
    parser.add_argument("--max", type=int, default=20,
                        help="每个关键词×城市组合的最大采集数 (默认: 20)")
    parser.add_argument("--no-detail", action="store_true",
                        help="跳过详情页抓取")
    parser.add_argument("--headless", type=lambda x: x.lower() != "false", default=True,
                        help="是否无头模式 (默认: True)")
    parser.add_argument("--retries", type=int, default=None,
                        help="失败重试次数 (默认: 使用各平台配置)")
    parser.add_argument("--sources-list", dest="list_sources", action="store_true",
                        help="列出已注册的采集平台")

    args = parser.parse_args()

    # 管理命令
    if args.list_sources:
        print_sources()
        return

    # 判断模式
    if args.query and not any([args.keywords != "Python", args.cities != "北京",
                                args.sources, args.max != 20, args.no_detail]):
        # 自然语言模式
        run_natural_language(args.query)
    elif args.query:
        # 同时有 query 和结构化参数 → 结构化优先
        logger.info("同时检测到自然语言查询和结构化参数，以结构化参数为准")
        run_structured(args)
    else:
        # 纯结构化参数
        run_structured(args)


if __name__ == "__main__":
    main()
