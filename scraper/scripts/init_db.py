# -*- coding: utf-8 -*-
"""数据库初始化脚本 —— 建库建表

用法:
    python scripts/init_db.py
    python scripts/init_db.py --config config/mysql.yaml
"""

import argparse
import os
import sys

# 添加项目根目录到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
import pymysql


CREATE_DATABASE_SQL = """\
CREATE DATABASE IF NOT EXISTS {database}
DEFAULT CHARACTER SET utf8mb4
COLLATE utf8mb4_unicode_ci;
"""

CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS jobs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    source VARCHAR(20) NOT NULL COMMENT '来源: boss/job51/zhaopin/liepin',
    source_job_id VARCHAR(64) NOT NULL COMMENT '原始职位ID',
    job_name VARCHAR(200) DEFAULT '' COMMENT '职位名称',
    company_name VARCHAR(200) DEFAULT '' COMMENT '公司名称',
    salary_min INT DEFAULT NULL COMMENT '最低月薪(元)',
    salary_max INT DEFAULT NULL COMMENT '最高月薪(元)',
    salary_month INT DEFAULT 12 COMMENT '年薪月数',
    city VARCHAR(50) DEFAULT '' COMMENT '城市',
    district VARCHAR(50) DEFAULT '' COMMENT '区/县',
    experience VARCHAR(50) DEFAULT '' COMMENT '经验要求',
    education VARCHAR(20) DEFAULT '' COMMENT '学历要求',
    skills JSON DEFAULT NULL COMMENT '技能标签',
    welfare JSON DEFAULT NULL COMMENT '福利待遇',
    job_description TEXT COMMENT '职位描述',
    industry VARCHAR(100) DEFAULT '' COMMENT '行业分类',
    company_size VARCHAR(50) DEFAULT '' COMMENT '公司规模',
    publish_date VARCHAR(30) DEFAULT '' COMMENT '发布日期',
    crawl_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '抓取时间',
    url VARCHAR(500) DEFAULT '' COMMENT '原始链接',
    UNIQUE KEY uk_source_job (source, source_job_id),
    INDEX idx_city_date (city, publish_date),
    INDEX idx_job_name (job_name),
    INDEX idx_crawl_time (crawl_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='招聘职位数据表';
"""


def load_config(config_path):
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def init_database(config):
    db_name = config['database']

    # 先连接不指定数据库，创建数据库
    conn = pymysql.connect(
        host=config['host'],
        port=config['port'],
        user=config['user'],
        password=config['password'],
        charset=config.get('charset', 'utf8mb4'),
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(CREATE_DATABASE_SQL.format(database=db_name))
        conn.commit()
        print(f"[OK] 数据库 '{db_name}' 已创建/确认")
    finally:
        conn.close()

    # 连接指定数据库，创建表
    conn = pymysql.connect(
        host=config['host'],
        port=config['port'],
        user=config['user'],
        password=config['password'],
        database=db_name,
        charset=config.get('charset', 'utf8mb4'),
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(CREATE_TABLE_SQL)
        conn.commit()
        print(f"[OK] 表 'jobs' 已创建/确认")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description='初始化招聘数据数据库')
    parser.add_argument(
        '--config', '-c',
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             'config', 'mysql.yaml'),
        help='MySQL 配置文件路径'
    )
    args = parser.parse_args()

    config = load_config(args.config)
    print(f"[INFO] 连接 MySQL: {config['host']}:{config['port']}, 用户: {config['user']}")

    try:
        init_database(config)
        print("\n[SUCCESS] 数据库初始化完成!")
    except pymysql.err.OperationalError as e:
        print(f"\n[ERROR] 数据库连接失败: {e}")
        print("请检查 MySQL 是否已启动，以及 config/mysql.yaml 中的连接信息是否正确")
        sys.exit(1)


if __name__ == '__main__':
    main()

