"""Conservative application caps; only BaoStock/GitHub publish applicable quotas."""
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Policy:
    name:str
    interval:float
    hourly:int
    daily:int


# BaoStock: https://www.baostock.com/blacklist (50,000/day; no concurrent sessions).
# GitHub API: https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
# All values below are application guardrails, not promised provider allowances.
POLICIES={
    'baostock':Policy('BaoStock',10,60,150),
    'eastmoney':Policy('东方财富',30,10,30),
    'sina':Policy('新浪财经',30,20,40),
    'tencent':Policy('腾讯财经',30,10,20),
    'xueqiu':Policy('雪球',60,6,12),
    'legulegu':Policy('乐咕乐股',60,6,12),
    'chinabond':Policy('中债',60,6,12),
    'exchange':Policy('交易所网站',60,6,12),
    'github_api':Policy('GitHub API',10,30,60),
    'github_pages':Policy('估值静态数据',60,3,6),
    'github_downloads':Policy('公开历史快照',30,4,8),
}
DEFAULT=Policy('该数据源',60,6,12)


def source_key(value):
    value=str(value).strip().lower()
    host=(urlsplit(value).hostname or '') if '://' in value else value
    if host in POLICIES:return host
    def domain(suffix):return host==suffix or host.endswith('.'+suffix)
    for suffix,key in [('eastmoney.com','eastmoney'),('sinajs.cn','sina'),('sina.com.cn','sina'),('sina.com','sina'),
                       ('gtimg.cn','tencent'),('qq.com','tencent'),('xueqiu.com','xueqiu'),('legulegu.com','legulegu'),
                       ('chinabond.com.cn','chinabond'),('sse.com.cn','exchange'),('szse.cn','exchange'),
                       ('api.github.com','github_api'),('github.io','github_pages'),('githubusercontent.com','github_downloads')]:
        if domain(suffix):return key
    return host


def policy(value):return POLICIES.get(source_key(value),DEFAULT)
