# -*- coding: utf-8 -*-
"""数据采集系统 — 统一数据写入器

MySQL upsert + JSON Lines 双写，按日期分目录。
"""

from __future__ import annotations
import json
import logging
import os
from datetime import datetime

import pymysql

from .utils import load_mysql_config

logger = logging.getLogger(__name__)

UPSERT_SQL = """INSERT INTO jobs (
    source, source_job_id, job_name, company_name,
    salary_min, salary_max, salary_month,
    city, district, experience, education,
    skills, welfare, job_description,
    industry, company_size, publish_date,
    crawl_time, url
) VALUES (
    %(source)s, %(source_job_id)s, %(job_name)s, %(company_name)s,
    %(salary_min)s, %(salary_max)s, %(salary_month)s,
    %(city)s, %(district)s, %(experience)s, %(education)s,
    %(skills)s, %(welfare)s, %(job_description)s,
    %(industry)s, %(company_size)s, %(publish_date)s,
    %(crawl_time)s, %(url)s
) ON DUPLICATE KEY UPDATE
    job_name=VALUES(job_name),
    company_name=VALUES(company_name),
    salary_min=VALUES(salary_min),
    salary_max=VALUES(salary_max),
    salary_month=VALUES(salary_month),
    city=VALUES(city),
    district=VALUES(district),
    experience=VALUES(experience),
    education=VALUES(education),
    skills=VALUES(skills),
    welfare=VALUES(welfare),
    job_description=VALUES(job_description),
    industry=VALUES(industry),
    company_size=VALUES(company_size),
    publish_date=VALUES(publish_date),
    crawl_time=VALUES(crawl_time),
    url=VALUES(url)
"""


class DBWriter:
    """统一数据写入器：MySQL + JSON Lines"""

    def __init__(self, config_dir: str | None = None):
        if config_dir is None:
            config_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "config"
            )
        self._config_dir = config_dir
        self._mysql_config = load_mysql_config(
            os.path.join(config_dir, "mysql.yaml")
        )

    def write(self, items: list, source: str, data_dir: str | None = None) -> dict:
        """写入一批 JobItem 到 MySQL 和 JSON Lines"""
        if not items:
            logger.info("[%s] 无数据可写入", source)
            return {"inserted": 0, "updated": 0, "total": 0,
                    "with_salary": 0, "with_desc": 0, "json_file": ""}

        # ---- JSON Lines ----
        if data_dir is None:
            data_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "data"
            )
        today = datetime.now().strftime("%Y-%m-%d")
        out_dir = os.path.join(data_dir, source, today)
        os.makedirs(out_dir, exist_ok=True)
        json_path = os.path.join(out_dir, "jobs.jsonl")

        with open(json_path, "w", encoding="utf-8") as f:
            for item in items:
                if hasattr(item, "to_db_row"):
                    row = item.to_db_row()
                else:
                    row = dict(item)
                # 序列化列表字段
                for key in ("skills", "welfare"):
                    if isinstance(row.get(key), list):
                        row[key] = json.dumps(row[key], ensure_ascii=False)
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

        logger.info("[%s] JSON 写入: %s (%d 条)", source, json_path, len(items))

        # ---- MySQL ----
        db = pymysql.connect(
            host=self._mysql_config["host"],
            port=self._mysql_config["port"],
            user=self._mysql_config["user"],
            password=self._mysql_config["password"],
            database=self._mysql_config["database"],
            charset=self._mysql_config.get("charset", "utf8mb4"),
            autocommit=False,
        )
        inserted = 0
        updated = 0
        try:
            with db.cursor() as cur:
                for item in items:
                    if hasattr(item, "to_db_row"):
                        row = item.to_db_row()
                    else:
                        row = dict(item)
                    # 序列化列表字段 + 补默认值
                    for key in ("skills", "welfare"):
                        if key not in row:
                            row[key] = None
                        elif isinstance(row[key], list):
                            row[key] = json.dumps(row[key], ensure_ascii=False)
                    if "crawl_time" not in row or not row["crawl_time"]:
                        row["crawl_time"] = datetime.now()
                    cur.execute(UPSERT_SQL, row)
                    if cur.rowcount == 1:
                        inserted += 1
                    else:
                        updated += 1
                db.commit()
        except Exception as e:
            db.rollback()
            logger.error("[%s] MySQL 写入失败: %s", source, e)
        finally:
            db.close()

        has_salary = sum(1 for r in items if (
            (hasattr(r, "salary_min") and r.salary_min is not None) or
            (hasattr(r, "get") and r.get("salary_min"))))
        has_desc = sum(1 for r in items if (
            (hasattr(r, "job_description") and r.job_description) or
            (hasattr(r, "get") and r.get("job_description"))))

        stats = {
            "inserted": inserted, "updated": updated,
            "total": len(items),
            "with_salary": has_salary, "with_desc": has_desc,
            "json_file": json_path
        }
        logger.info("[%s] MySQL: %d 新增 / %d 更新 | 有薪资 %d/%d 有描述 %d/%d",
                     source, inserted, updated,
                     has_salary, len(items), has_desc, len(items))
        return stats
