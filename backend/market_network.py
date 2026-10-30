"""Durable budgets shared by disposable public-market workers."""
from contextlib import contextmanager
from datetime import timedelta
import sqlite3
import time

from .storage import now

INTERVAL = 30.0
DAILY_LIMIT = 60
AUTOMATIC_DAILY_LIMIT = 40
HOURLY_LIMIT = 20
HISTORY_DAILY_LIMIT = 10

SCHEMA = '''
CREATE TABLE IF NOT EXISTS market_requests(at REAL NOT NULL,scope TEXT NOT NULL,source TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS market_requests_at ON market_requests(at);
CREATE TABLE IF NOT EXISTS market_circuits(source TEXT PRIMARY KEY,failures INTEGER NOT NULL DEFAULT 0,until REAL NOT NULL DEFAULT 0);
'''


class MarketLimit(RuntimeError):
    def __init__(self,message,retry_at,source=''):
        super().__init__(message);self.retry_at=retry_at;self.source=source


class MarketBudget:
    def __init__(self,path,scope='history',manual=False):
        if manual:scope='manual:'+scope
        self.path=path;self.scope=scope
        with self.connect() as db:db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=15)
        try:yield db;db.commit()
        except BaseException:db.rollback();raise
        finally:db.close()

    def reserve(self,source):
        while True:
            moment=now();stamp=moment.timestamp()
            midnight=moment.replace(hour=0,minute=0,second=0,microsecond=0)
            tomorrow=(midnight+timedelta(days=1)).timestamp()
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('DELETE FROM market_requests WHERE at<?',(stamp-172800,))
                circuit=db.execute('SELECT until FROM market_circuits WHERE source=?',(source,)).fetchone()
                if circuit and circuit[0]>stamp:raise MarketLimit('数据源冷却中，稍后自动继续',circuit[0],source)
                daily,automatic,history=db.execute("SELECT COUNT(*),COALESCE(SUM(scope NOT LIKE 'manual:%'),0),COALESCE(SUM(scope='history'),0) FROM market_requests WHERE at>=?",(midnight.timestamp(),)).fetchone()
                if daily>=DAILY_LIMIT or (not self.scope.startswith('manual:') and automatic>=AUTOMATIC_DAILY_LIMIT) or (self.scope=='history' and history>=HISTORY_DAILY_LIMIT):
                    raise MarketLimit('已达今日市场请求预算，明日自动继续',tomorrow)
                count,first,last=db.execute('SELECT COUNT(*),MIN(at),MAX(at) FROM market_requests WHERE at>?',(stamp-3600,)).fetchone()
                if count>=HOURLY_LIMIT:raise MarketLimit('已达每小时市场请求预算，稍后自动继续',first+3600.01)
                delay=max(0,(last or 0)+INTERVAL-stamp)
                if not delay:
                    db.execute('INSERT INTO market_requests VALUES(?,?,?)',(stamp,self.scope,source))
                    return
            time.sleep(min(INTERVAL,delay))

    def outcome(self,source,error=None,blocked=False,retry_after=0):
        with self.connect() as db:
            if error is None:
                db.execute('INSERT INTO market_circuits VALUES(?,0,0) ON CONFLICT(source) DO UPDATE SET failures=0',(source,))
                return
            previous=db.execute('SELECT failures,until FROM market_circuits WHERE source=?',(source,)).fetchone()
            failures=(previous[0] if previous else 0)+1
            until=max(previous[1] if previous else 0,now().timestamp()+max(86400 if blocked else 1800 if failures>=3 else 0,retry_after))
            # The first two ordinary errors do not close a source.
            if not blocked and failures<3 and not retry_after:until=previous[1] if previous else 0
            db.execute('INSERT OR REPLACE INTO market_circuits VALUES(?,?,?)',(source,failures,until))

    def status(self):
        moment=now();midnight=moment.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        with self.connect() as db:
            used,automatic,history=db.execute("SELECT COUNT(*),COALESCE(SUM(scope NOT LIKE 'manual:%'),0),COALESCE(SUM(scope='history'),0) FROM market_requests WHERE at>=?",(midnight,)).fetchone()
        return {'used':used,'limit':DAILY_LIMIT,'automatic_used':automatic,'automatic_limit':AUTOMATIC_DAILY_LIMIT,'history_used':history,'history_limit':HISTORY_DAILY_LIMIT,'hourly_limit':HOURLY_LIMIT,'interval':INTERVAL}
