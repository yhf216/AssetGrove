"""Durable budgets shared by disposable public-market workers."""
from contextlib import contextmanager
from datetime import timedelta
import sqlite3
import time
import math
import errno
import json
from pathlib import Path
from email.utils import parsedate_to_datetime

from .storage import now
from .source_policy import source_key,policy

INTERVAL = 5.0
DAILY_LIMIT = 180
AUTOMATIC_DAILY_LIMIT = 140
HOURLY_LIMIT = 80
HISTORY_DAILY_LIMIT = 120

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
        with self.connect() as db:
            db.executescript(SCHEMA)
            # Preserve counts and cooldowns when old hostnames join one provider.
            for (old,) in db.execute('SELECT DISTINCT source FROM market_requests UNION SELECT source FROM market_circuits').fetchall():
                key=source_key(old)
                if key==old:continue
                db.execute('UPDATE market_requests SET source=? WHERE source=?',(key,old))
                previous=db.execute('SELECT failures,until FROM market_circuits WHERE source=?',(old,)).fetchone()
                if previous:
                    db.execute('''INSERT INTO market_circuits VALUES(?,?,?) ON CONFLICT(source) DO UPDATE SET
                        failures=MAX(market_circuits.failures,excluded.failures),until=MAX(market_circuits.until,excluded.until)''',(key,*previous))
                    db.execute('DELETE FROM market_circuits WHERE source=?',(old,))
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
                migrated=db.execute("SELECT 1 FROM meta WHERE key='source_policy_version'").fetchone()
                if not migrated:
                    values={k:json.loads(v) for k,v in db.execute("SELECT key,value FROM meta WHERE key IN ('market_network_message','market_network_retry','history_next_at')")}
                    if values.get('market_network_message') in {'已达今日市场请求预算，明日自动继续','已达每小时市场请求预算，稍后自动继续'}:
                        retry=values.get('market_network_retry',0)
                        keys=['market_network_retry','market_network_message']
                        if values.get('history_next_at')==retry:keys.append('history_next_at')
                        for key in keys:db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps('' if key=='market_network_message' else 0)))
                    db.execute("INSERT INTO meta VALUES('source_policy_version','1')")

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=15)
        try:yield db;db.commit()
        except BaseException:db.rollback();raise
        finally:db.close()

    def reserve(self,source):
        source=source_key(source);limits=policy(source)
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
                used=db.execute('SELECT COUNT(*) FROM market_requests WHERE source=? AND at>=?',(source,midnight.timestamp())).fetchone()[0]
                if used>=limits.daily:raise MarketLimit(limits.name+'今日同步已暂停，明日继续',tomorrow,source)
                source_count,source_first,source_last=db.execute('SELECT COUNT(*),MIN(at),MAX(at) FROM market_requests WHERE source=? AND at>?',(source,stamp-3600)).fetchone()
                if source_count>=limits.hourly:raise MarketLimit(limits.name+'同步已暂缓，稍后继续',source_first+3600.01,source)
                delay=max(0,(last or 0)+INTERVAL-stamp)
                delay=max(delay,(source_last or 0)+limits.interval-stamp)
                if not delay:
                    db.execute('INSERT INTO market_requests VALUES(?,?,?)',(stamp,self.scope,source))
                    return
            time.sleep(min(INTERVAL,delay))

    def outcome(self,source,error=None,blocked=False,retry_after=0):
        source=source_key(source)
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

    def inspect_response(self,source,status,headers):
        """Use provider feedback without making a separate quota query."""
        source=source_key(source);stamp=now().timestamp()
        retry=headers.get('Retry-After','0')
        try:retry=float(retry)
        except (ValueError,TypeError):
            try:retry=max(0,parsedate_to_datetime(retry).timestamp()-stamp)
            except (ValueError,TypeError,OverflowError):retry=0
        if not math.isfinite(retry):retry=0
        reset=0
        if source=='github_api' and headers.get('X-RateLimit-Remaining')=='0':
            try:reset=float(headers.get('X-RateLimit-Reset',0))
            except (TypeError,ValueError):reset=0
            if not math.isfinite(reset):reset=0
        if status in {403,429}:
            if source=='github_api':
                with self.connect() as db:old=db.execute('SELECT failures FROM market_circuits WHERE source=?',(source,)).fetchone()
                delay=max(60*2**min(old[0] if old else 0,6),retry,reset-stamp)
                self.outcome(source,'HTTP '+str(status),retry_after=delay)
            else:self.outcome(source,'HTTP '+str(status),blocked=True,retry_after=retry)
            with self.connect() as db:until=db.execute('SELECT until FROM market_circuits WHERE source=?',(source,)).fetchone()[0]
            raise MarketLimit(policy(source).name+'暂时限制访问，稍后自动继续',until,source)
        self.outcome(source,None if status<500 else 'HTTP '+str(status))
        if reset>stamp or retry>0:
            with self.connect() as db:
                db.execute('''INSERT INTO market_circuits VALUES(?,0,?) ON CONFLICT(source) DO UPDATE SET
                    until=MAX(market_circuits.until,excluded.until)''',(source,max(reset,stamp+retry)))

    def check_baostock(self,result):
        if str(getattr(result,'error_code',''))=='10001011':
            self.outcome('baostock','访问受限',blocked=True)
            with self.connect() as db:until=db.execute("SELECT until FROM market_circuits WHERE source='baostock'").fetchone()[0]
            raise MarketLimit('BaoStock 暂时限制访问，已暂停同步',until,'baostock')

    @contextmanager
    def baostock_session(self):
        """An OS lock spans login through socket close, including killed workers."""
        import os
        handle=(Path(self.path).parent/'.baostock-session.lock').open('a+b')
        locked=False
        try:
            handle.seek(0,2)
            if handle.tell()==0:handle.write(b'0');handle.flush()
            handle.seek(0)
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                locked=True
            except OSError as exc:
                if exc.errno not in {errno.EACCES,errno.EAGAIN,errno.EDEADLK}:raise
                raise MarketLimit('BaoStock 正在同步，稍后继续',now().timestamp()+30,'baostock') from exc
            yield
        finally:
            if locked:
                handle.seek(0)
                if os.name=='nt':msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)
            handle.close()

    def status(self):
        moment=now();midnight=moment.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        with self.connect() as db:
            used,automatic,history=db.execute("SELECT COUNT(*),COALESCE(SUM(scope NOT LIKE 'manual:%'),0),COALESCE(SUM(scope='history'),0) FROM market_requests WHERE at>=?",(midnight,)).fetchone()
        return {'used':used,'limit':DAILY_LIMIT,'automatic_used':automatic,'automatic_limit':AUTOMATIC_DAILY_LIMIT,'history_used':history,'history_limit':HISTORY_DAILY_LIMIT,'hourly_limit':HOURLY_LIMIT,'interval':INTERVAL}

    def next_history_at(self,moment=None):
        """Read-only scheduling check; workers still reserve every wire request."""
        moment=moment or now();stamp=moment.timestamp()
        midnight=moment.replace(hour=0,minute=0,second=0,microsecond=0)
        with self.connect() as db:
            daily,automatic,history=db.execute("SELECT COUNT(*),COALESCE(SUM(scope NOT LIKE 'manual:%'),0),COALESCE(SUM(scope='history'),0) FROM market_requests WHERE at>=?",(midnight.timestamp(),)).fetchone()
            count,first,last=db.execute('SELECT COUNT(*),MIN(at),MAX(at) FROM market_requests WHERE at>?',(stamp-3600,)).fetchone()
        if daily>=DAILY_LIMIT or automatic>=AUTOMATIC_DAILY_LIMIT or history>=HISTORY_DAILY_LIMIT:
            return (midnight+timedelta(days=1)).timestamp()
        if count>=HOURLY_LIMIT:return first+3600.01
        return max(stamp,(last or 0)+INTERVAL)
