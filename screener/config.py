"""集中配置。

设计参考了 sngyai/Sequoia-X 的 core/config.py（Pydantic-settings），
但本项目刻意保持零第三方依赖，因此改为普通常量模块。
"""
from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
STATIC_DIR = BASE_DIR / "static"

for _d in (DATA_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- 网络
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
HTTP_TIMEOUT = 12
HTTP_RETRIES = 3
HTTP_BACKOFF = 0.7

# ---------------------------------------------------------------- 缓存 TTL（秒）
SNAPSHOT_TTL = 45
KLINE_TTL = 6 * 3600
INDUSTRY_TTL = 120
INDEX_TTL = 30

# ---------------------------------------------------------------- 东方财富字段
# f1 小数位 f2 最新价 f3 涨跌幅 f4 涨跌额 f5 成交量(手) f6 成交额 f7 振幅
# f8 换手率 f9 市盈率(动) f10 量比 f12 代码 f13 市场 f14 名称 f15 最高 f16 最低
# f17 今开 f18 昨收 f20 总市值 f21 流通市值 f22 涨速 f23 市净率 f24 60日涨跌幅
# f25 年初至今 f62 主力净流入 f100 行业 f115 市盈率(TTM)
SNAPSHOT_FIELDS = (
    "f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,"
    "f20,f21,f22,f23,f24,f25,f62,f100,f115"
)

# 沪深 A 股（不含北交所）
FS_ALL_A = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
FS_INDUSTRY = "m:90+t:2"

# 主要指数
INDEX_LIST = [
    ("1.000001", "上证指数"),
    ("0.399001", "深证成指"),
    ("0.399006", "创业板指"),
    ("1.000300", "沪深300"),
    ("0.399905", "中证500"),
    ("1.000688", "科创50"),
]

# ---------------------------------------------------------------- 业务参数
# 策略需要 K 线，为避免打爆接口，最多对多少个候选拉 K 线
MAX_KLINE_FETCH = 160
KLINE_WORKERS = 12
KLINE_BARS = 130

# 默认筛选条件（前端可覆盖）
DEFAULT_FILTERS = {
    "min_amount_yi": 1.0,      # 成交额下限（亿元）
    "min_price": 2.0,          # 股价下限，过滤面值退市风险股
    "max_price": 0,            # 0 表示不限
    "pct_min": -9.9,           # 涨跌幅区间
    "pct_max": 9.9,
    "turnover_min": 0.3,       # 换手率下限 %
    "turnover_max": 25.0,
    "pe_min": 0.0,
    "pe_max": 0.0,             # 0 表示不限
    "pb_max": 0.0,             # 0 表示不限
    "mv_min_yi": 30.0,         # 总市值下限（亿元）
    "exclude_st": True,
    "exclude_new": True,       # 排除上市不足 60 个交易日的次新（用 K 线长度判断）
    "industry": "",
}
