# -*- coding: utf-8 -*-
"""数据采集系统 — 数据模型定义"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ScrapeRequest:
    """统一的采集请求，描述用户要采集什么"""

    keywords: list[str]              # 搜索关键词列表
    cities: list[str]                # 目标城市列表
    max_per_keyword_city: int = 20   # 每个关键词×城市组合的最大采集数
    sources: list[str] | None = None # None=全部平台, ["boss","job51"]=只采集指定平台
    fetch_details: bool = True       # 是否抓取职位详情
    headless: bool = True            # 浏览器是否无头模式
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class JobItem:
    """清洗后的标准职位数据，与 MySQL jobs 表一一对应"""

    source: str
    source_job_id: str
    job_name: str = ""
    company_name: str = ""
    salary_min: int | None = None
    salary_max: int | None = None
    salary_month: int = 12
    city: str = ""
    district: str = ""
    experience: str = ""
    education: str = ""
    skills: list[str] = field(default_factory=list)
    welfare: list[str] = field(default_factory=list)   # 福利待遇（新增）
    job_description: str = ""
    industry: str = ""
    company_size: str = ""
    publish_date: str = ""
    url: str = ""
    crawl_time: datetime = field(default_factory=datetime.now)

    def to_db_row(self) -> dict:
        """转为 MySQL upsert 参数字典"""
        import json
        return {
            "source": self.source,
            "source_job_id": self.source_job_id,
            "job_name": self.job_name,
            "company_name": self.company_name,
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "salary_month": self.salary_month,
            "city": self.city,
            "district": self.district,
            "experience": self.experience,
            "education": self.education,
            "skills": json.dumps(self.skills, ensure_ascii=False) if self.skills else None,
            "welfare": json.dumps(self.welfare, ensure_ascii=False) if self.welfare else None,
            "job_description": self.job_description,
            "industry": self.industry,
            "company_size": self.company_size,
            "publish_date": self.publish_date,
            "url": self.url,
            "crawl_time": self.crawl_time,
        }
