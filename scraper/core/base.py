# -*- coding: utf-8 -*-
"""数据采集系统 — 抽象基类

所有平台采集器必须继承 BaseScraper 并实现 collect() 方法。
"""

from __future__ import annotations
import os
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from .models import ScrapeRequest, JobItem

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    """采集器抽象基类

    子类必须定义:
        name: str         — 唯一标识，如 "boss"
        display_name: str — 显示名，如 "Boss直聘"
        tool_type: str    — "playwright" 或 "drission"

    子类必须实现:
        collect(request) -> list[JobItem]
    """

    # ---- 子类覆盖 ----
    name: str = ""
    display_name: str = ""
    tool_type: str = "playwright"  # "playwright" | "drission"

    # ---- 配置 ----
    requires_login: bool = False  # 是否需要登录
    max_per_query: int = 100      # 单次查询最大采集数

    def __init__(self, config_dir: str | None = None):
        """初始化采集器

        Args:
            config_dir: config 目录路径，默认 auto-detect
        """
        if config_dir is None:
            config_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "config"
            )
        self._config_dir = config_dir
        self._sources_config = self._load_sources_config()

    def _load_sources_config(self) -> dict:
        """加载 config/sources.yaml 中当前平台的配置"""
        path = os.path.join(self._config_dir, "sources.yaml")
        if not os.path.exists(path):
            logger.warning("sources.yaml 不存在，城市码映射将不可用")
            return {}
        with open(path, "r", encoding="utf-8") as f:
            all_cfg = yaml.safe_load(f) or {}
        return all_cfg.get("sources", {}).get(self.name, {})

    @property
    def city_codes(self) -> dict[str, str]:
        """城市名 → 平台城市码 映射"""
        return self._sources_config.get("city_codes", {})

    def map_city(self, city_name: str) -> str | None:
        """将标准城市名映射为平台专用的城市码"""
        code = self.city_codes.get(city_name)
        if not code:
            logger.warning("[%s] 未知城市 %s，跳过", self.name, city_name)
        return code

    @classmethod
    def supports(cls, request: "ScrapeRequest") -> bool:
        """判断是否处理该请求（子类可覆盖）"""
        return True

    # ---- 生命周期 ----
    def setup(self):
        """采集前准备（如启动浏览器、登录等）"""
        pass

    def teardown(self):
        """采集后清理"""
        pass

    # ---- 核心方法 ----
    @abstractmethod
    def collect(self, request: "ScrapeRequest") -> list["JobItem"]:
        """执行采集，返回 JobItem 列表

        Args:
            request: 统一的采集请求

        Returns:
            采集到的职位数据列表
        """
        ...
