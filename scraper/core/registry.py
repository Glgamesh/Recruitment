# -*- coding: utf-8 -*-
"""数据采集系统 — 采集器注册与调度

增强版:
- 失败重试 + 指数退避
- 速率控制 (从 sources.yaml 读取)
- 浏览器会话复用 (setup一次, collect多次, teardown统一)
"""

from __future__ import annotations
import concurrent.futures
import logging
import time
import os
import yaml
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import BaseScraper
    from .models import ScrapeRequest, JobItem

logger = logging.getLogger(__name__)

_registry: dict[str, type[BaseScraper]] = {}

# 缓存的 sources.yaml 配置
_sources_config: dict | None = None


def _load_sources_config() -> dict:
    """加载 sources.yaml（缓存）"""
    global _sources_config
    if _sources_config is not None:
        return _sources_config
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "sources.yaml"
    )
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            _sources_config = yaml.safe_load(f) or {}
    except Exception:
        _sources_config = {}
    return _sources_config


def _get_source_config(source_name: str) -> dict:
    """获取单个平台的配置"""
    cfg = _load_sources_config()
    return cfg.get("sources", {}).get(source_name, {})


def register_scraper(name: str):
    """装饰器：将类注册到采集器注册表"""
    def decorator(cls: type[BaseScraper]) -> type[BaseScraper]:
        cls.name = name
        _registry[name] = cls
        logger.debug("注册采集器: %s → %s", name, cls.__name__)
        return cls
    return decorator


def get_scraper_class(name: str) -> type[BaseScraper]:
    if name not in _registry:
        raise KeyError(f"未注册的采集器: {name}，可用: {list(_registry.keys())}")
    return _registry[name]


def list_sources() -> list[dict]:
    result = []
    for name, cls in _registry.items():
        cfg = _get_source_config(name)
        result.append({
            "name": name,
            "display_name": cls.display_name,
            "tool_type": cls.tool_type,
            "requires_login": cls.requires_login,
            "request_delay": cfg.get("request_delay", 1.0),
            "max_retries": cfg.get("max_retries", 2),
        })
    return result


def _run_one_with_retry(request: ScrapeRequest, source_name: str,
                         scraper: BaseScraper | None = None,
                         global_retries: int | None = None
                         ) -> tuple[str, list[JobItem], str | None]:
    """运行单个采集器，带重试机制"""
    cfg = _get_source_config(source_name)
    max_retries = global_retries if global_retries is not None else cfg.get("max_retries", 2)
    backoff_base = cfg.get("retry_backoff", 2.0)

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            if scraper is None:
                cls = get_scraper_class(source_name)
                if not cls.supports(request):
                    logger.info("[%s] 不支持此请求，跳过", source_name)
                    return source_name, [], None
                scraper = cls()

            items = scraper.collect(request)
            logger.info("[%s] 采集完成: %d 条%s",
                         source_name, len(items),
                         f" (重试{attempt}次后成功)" if attempt > 0 else "")
            return source_name, items, None

        except Exception as e:
            last_error = str(e)
            if attempt < max_retries:
                wait = backoff_base * (2 ** attempt)
                logger.warning("[%s] 采集失败 (尝试 %d/%d): %s — %0.1fs后重试",
                               source_name, attempt + 1, max_retries + 1, last_error, wait)
                time.sleep(wait)
            else:
                logger.error("[%s] 采集失败 (已重试%d次): %s",
                             source_name, max_retries, last_error)

    return source_name, [], last_error


def dispatch(request: ScrapeRequest, global_retries: int | None = None) -> dict[str, list[JobItem]]:
    """并发调度：浏览器复用 + 速率控制"""
    if request.sources:
        targets = [s for s in request.sources if s in _registry]
    else:
        targets = list(_registry.keys())

    if not targets:
        logger.warning("没有可用的采集平台")
        return {}

    logger.info("调度 %d 个平台: %s", len(targets), ", ".join(targets))

    # === 1. 创建并 setup 所有采集器（浏览器复用） ===
    scrapers: dict[str, BaseScraper] = {}
    for name in targets:
        try:
            cls = get_scraper_class(name)
            if not cls.supports(request):
                continue
            scraper = cls()
            scraper.setup()
            scrapers[name] = scraper
            logger.debug("[%s] setup 完成", name)
        except Exception as e:
            logger.error("[%s] setup 失败: %s", name, e)

    # === 2. 执行采集 ===
    results: dict[str, list[JobItem]] = {}

    try:
        if len(scrapers) == 1:
            name = list(scrapers.keys())[0]
            name, items, err = _run_one_with_retry(
                request, name, scraper=scrapers[name], global_retries=global_retries
            )
            results[name] = items
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(scrapers)) as executor:
                futures = {
                    executor.submit(
                        _run_one_with_retry, request, name,
                        scraper=scraper, global_retries=global_retries
                    ): name
                    for name, scraper in scrapers.items()
                }
                for future in concurrent.futures.as_completed(futures):
                    name, items, err = future.result()
                    results[name] = items

    finally:
        # === 3. 统一 teardown ===
        for name, scraper in scrapers.items():
            try:
                scraper.teardown()
                logger.debug("[%s] teardown 完成", name)
            except Exception as e:
                logger.warning("[%s] teardown 异常: %s", name, e)

    return results
