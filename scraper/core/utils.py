# -*- coding: utf-8 -*-
"""数据采集系统 — 公共工具函数

从各平台采集脚本提取并统一：
- parse_salary: 薪资文本解析（取猎聘最完整版本）
- parse_city: 城市+区县分离
- norm_edu: 学历归一化
- norm_exp: 经验归一化
- load_yaml: YAML 配置加载
- load_mysql_config / get_mysql_connection: MySQL 配置与连接
"""

import os
import re
import logging

import yaml
import pymysql

logger = logging.getLogger(__name__)

# ============================================================
# 学历归一化映射
# ============================================================
EDU_MAP = {
    "博士": "博士", "硕士": "硕士", "研究生": "硕士",
    "本科": "本科", "统招本科": "本科", "学士": "本科",
    "大专": "大专", "专科": "大专",
    "高中": "高中", "中专": "中专",
    "学历不限": "不限", "不限": "不限",
}


def norm_edu(text: str | None) -> str:
    """将各平台学历文本归一化为标准值"""
    if not text:
        return "不限"
    for k, v in EDU_MAP.items():
        if k in text:
            return v
    return text


# ============================================================
# 经验归一化映射
# ============================================================
EXP_MAP = {
    "应届生": "应届生", "在校生": "应届生", "应届毕业生": "应届生",
    "经验不限": "不限", "不限": "不限", "无需经验": "不限",
    "1年以内": "1年以内", "1年以下": "1年以内",
    "1年及以上": "1-3年", "1年以上": "1-3年",
    "2年及以上": "1-3年", "2年以上": "1-3年",
    "3年及以上": "3-5年", "3年以上": "3-5年",
    "1-3年": "1-3年", "3-5年": "3-5年",
    "5年及以上": "5-10年", "5年以上": "5-10年",
    "5-10年": "5-10年", "10年以上": "10年以上",
}


def norm_exp(text: str | None) -> str:
    """将各平台经验文本归一化为标准值"""
    if not text:
        return "不限"
    for k, v in EXP_MAP.items():
        if k in text:
            return v
    return text


# ============================================================
# 薪资解析（综合版，支持所有格式）
# ============================================================
def parse_salary(text: str | None) -> tuple[int | None, int | None, int | None]:
    """解析薪资文本，返回 (min, max, months)

    支持的格式：
    - "9-11k·14薪" → (9000, 11000, 14)
    - "15k-25k"     → (15000, 25000, 12)
    - "1.5-2.5万/月" → (15000, 25000, 12)
    - "200-250元/天" → (4400, 5500, 12)  # 按22天折算
    - "20-40万/年"   → (16666, 33333, 12)
    - "薪资面议"      → (None, None, None)
    - "薪" 开头等无效字 → (None, None, None)
    - "17-20K"       → (17000, 20000, 12)
    """
    if not text:
        return None, None, None

    # 面议/无效标记
    if any(w in text for w in ["面议", "面谈", "薪资面议"]):
        return None, None, None
    if text.strip() in ("薪",):
        return None, None, None

    text = text.strip()
    months = 12

    # 提取年薪月数（必须在年薪判断之前）
    m = re.search(r"[\xb7\s]*(\d+)\s*薪", text)
    if m:
        months = int(m.group(1))
        text = re.sub(r"[\xb7\s]*\d+\s*薪", "", text).strip()

    # 判断是否为年薪（必须在月数提取之后）
    is_yearly = False
    if "年" in text and "月" not in text:
        is_yearly = True

    values = []

    # 1. X-Yk / X-YK 共享单位（核心修复）
    m = re.match(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)\s*[kK]", text)
    if m:
        values.append(float(m.group(1)) * 1000)
        values.append(float(m.group(2)) * 1000)

    # 2. X-Y万 共享单位
    if not values:
        m = re.match(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)\s*万", text)
        if m:
            values.append(float(m.group(1)) * 10000)
            values.append(float(m.group(2)) * 10000)

    # 3. X-Y千 共享单位
    if not values:
        m = re.match(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)\s*千", text)
        if m:
            values.append(float(m.group(1)) * 1000)
            values.append(float(m.group(2)) * 1000)

    # 4. X-Y元/天 日薪格式
    if not values:
        m = re.match(r"(\d+)\s*[-~]\s*(\d+)\s*元/天", text)
        if m:
            values.append(int(m.group(1)) * 22)
            values.append(int(m.group(2)) * 22)

    # 5. 单值带K匹配
    if not values:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[kK]", text):
            values.append(float(m.group(1)) * 1000)

    # 6. 单值带万匹配
    if not values:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*万", text):
            v = float(m.group(1)) * 10000
            if not any(abs(v - n) < 100 for n in values):
                values.append(v)

    # 7. 单值带千匹配
    if not values:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*千", text):
            v = float(m.group(1)) * 1000
            if not any(abs(v - n) < 100 for n in values):
                values.append(v)

    # 8. 纯数字备用: "15000-25000元/月"
    if not values:
        for m in re.finditer(r"(\d{4,})", text):
            v = float(m.group(1))
            if 1000 <= v <= 500000:
                values.append(v)

    if len(values) < 2:
        return None, None, None

    lo = min(values)
    hi = max(values)
    if is_yearly:
        lo = lo / 12
        hi = hi / 12
    return int(lo), int(hi), months


# ============================================================
# 城市解析 — 分离 "城市·区县"
# ============================================================
def parse_city(text: str | None) -> tuple[str, str]:
    """从 ''北京·海淀'' 或 ''上海-浦东'' 中分离城市和区县"""
    if not text:
        return "", ""
    parts = re.split(r"[\xb7\-﹣—]", text.strip(), maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip().rstrip("市"), parts[1].strip()
    return text.strip().rstrip("市"), ""


# ============================================================
# 配置加载
# ============================================================
def load_yaml(path: str) -> dict:
    """加载 YAML 配置文件"""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_mysql_config(config_path: str | None = None) -> dict:
    """从 config/mysql.yaml 加载 MySQL 连接配置"""
    if config_path is None:
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config", "mysql.yaml"
        )
    return load_yaml(config_path)


def get_mysql_connection(config: dict | None = None):
    """获取 MySQL 连接"""
    if config is None:
        config = load_mysql_config()
    return pymysql.connect(
        host=config["host"],
        port=config["port"],
        user=config["user"],
        password=config["password"],
        database=config["database"],
        charset=config.get("charset", "utf8mb4"),
        autocommit=False,
    )
