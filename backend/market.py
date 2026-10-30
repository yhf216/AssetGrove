"""Independent public market cache and resumable breadth jobs. No portfolio access."""
from collections import defaultdict, OrderedDict
from copy import deepcopy
from bisect import bisect_left
from contextlib import contextmanager
from datetime import date, timedelta
import json
import math
from pathlib import Path
import sqlite3
import threading
import time

from .providers import fetch
from .storage import now, rows
from .market_cache import install_cache, calculate_trin
from .market_history import quarter_ranges, repair_priority
from .market_network import MarketBudget, MarketLimit

INDICES = [
    {'symbol':'sh000001','name':'上证指数'}, {'symbol':'sz399102','name':'创业板综指'},
    {'symbol':'sh000688','name':'科创50'}, {'symbol':'sh000300','name':'沪深300'},
    {'symbol':'sz399006','name':'创业板指'}, {'symbol':'sz399001','name':'深证成指'},
    {'symbol':'sh000905','name':'中证500'}, {'symbol':'sh000852','name':'中证1000'},
    {'symbol':'sh000016','name':'上证50'}, {'symbol':'sz399106','name':'深证综指'},
]
SYMBOLS = {r['symbol'] for r in INDICES}
DEFAULT_PREFS = {'symbols':['sh000001'],'period':'d','chart_type':'candlestick','ma_enabled':False,
                 'ma_periods':[10,20,50,250],'volume_enabled':True,'trin_enabled':True,'normalize':True}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS index_bars(symbol TEXT,date TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,source TEXT,PRIMARY KEY(symbol,date));
CREATE TABLE IF NOT EXISTS index_info(symbol TEXT PRIMARY KEY,source TEXT,first_date TEXT,last_date TEXT,updated_at TEXT);
CREATE TABLE IF NOT EXISTS universe(symbol TEXT PRIMARY KEY,name TEXT,ipo TEXT,out_date TEXT);
CREATE TABLE IF NOT EXISTS breadth(symbol TEXT,date TEXT,change REAL,volume REAL,trading INTEGER,PRIMARY KEY(symbol,date));
CREATE INDEX IF NOT EXISTS breadth_date ON breadth(date);
CREATE TABLE IF NOT EXISTS fetch_ranges(symbol TEXT,start TEXT,end TEXT,PRIMARY KEY(symbol,start,end));
CREATE TABLE IF NOT EXISTS calendar(date TEXT PRIMARY KEY,trading INTEGER);
CREATE TABLE IF NOT EXISTS trin(date TEXT PRIMARY KEY,value REAL,advances INTEGER,declines INTEGER,up_volume REAL,down_volume REAL,coverage REAL,source TEXT,provisional INTEGER,reason TEXT);
'''


def finite(value, positive=False):
    number=float(value)
    if not math.isfinite(number) or (positive and number<=0):raise ValueError('无效行情数值')
    return number


def trin_value(stocks):
    up=down=0;uv=dv=0.0;excluded=0
    for stock in stocks:
        try:
            if not stock.get('trading',1):excluded+=1;continue
            change=finite(stock['change']);volume=finite(stock['volume'])
            if volume<=0 or change==0:excluded+=1;continue
            if change>0:up+=1;uv+=volume
            else:down+=1;dv+=volume
        except (ValueError,TypeError,KeyError):excluded+=1
    value=(up/down)/(uv/dv) if up and down and uv>0 and dv>0 else None
    return {'value':value,'advances':up,'declines':down,'up_volume':uv,'down_volume':dv,'excluded':excluded}


def period_key(day, period):
    parsed=date.fromisoformat(day)
    return parsed.isocalendar()[:2] if period=='w' else day[:7] if period=='m' else day


def aggregate_bars(bars, period, ma_periods):
    sums={n:0.0 for n in ma_periods}
    daily=[]
    for i,bar in enumerate(bars):
        ma={}
        for n in ma_periods:
            sums[n]+=bar['close']
            if i>=n:sums[n]-=bars[i-n]['close']
            ma[str(n)]=sums[n]/n if i+1>=n else None
        daily.append({**bar,'ma':ma})
    if period=='d':return daily
    groups={}
    for bar in daily:
        key=period_key(bar['date'],period)
        if key not in groups:groups[key]=dict(bar)
        else:
            group=groups[key]
            group.update(date=bar['date'],close=bar['close'],high=max(group['high'],bar['high']),low=min(group['low'],bar['low']),volume=group['volume']+bar['volume'],ma=bar['ma'])
    return list(groups.values())


class MarketStore:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        self.calculation_lock=threading.Lock()
        self.history_cache=OrderedDict();self.cached_revision=-1
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)
            install_cache(db)

    @contextmanager
    def connect(self):
        with self.lock:
            db=sqlite3.connect(self.path,timeout=15);db.row_factory=sqlite3.Row
            try:yield db;db.commit()
            except BaseException:db.rollback();raise
            finally:db.close()

    def setting(self,key,value=None,write=False,default=None):
        with self.connect() as db:
            if write:
                db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(value,ensure_ascii=False)))
                return value
            row=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
            return json.loads(row[0]) if row else default

    def history_gaps(self,start,end,symbols=None):
        """Verify actual daily rows; old completion markers can contain empty responses."""
        if symbols is not None and not symbols:return {},[]
        with self.connect() as db:
            days=[r['date'] for r in rows(db,'SELECT date FROM calendar WHERE trading=1 AND date BETWEEN ? AND ? ORDER BY date',(start,end))]
            clause=' AND b.symbol IN ('+','.join('?' for _ in symbols)+')' if symbols else ''
            universe=rows(db,'SELECT * FROM universe ORDER BY symbol')
            if symbols is not None:universe=[r for r in universe if r['symbol'] in symbols]
            counts={r['symbol']:r['n'] for r in rows(db,"SELECT b.symbol,COUNT(*) n FROM breadth b JOIN calendar c ON c.date=b.date AND c.trading=1 JOIN universe u ON u.symbol=b.symbol WHERE b.date BETWEEN ? AND ? AND b.date>=u.ipo AND (u.out_date='' OR b.date<u.out_date) AND (b.trading=0 OR (b.change IS NOT NULL AND b.volume>=0))"+clause+' GROUP BY b.symbol',(start,end,*(symbols or [])))}
        expected={r['symbol']:max(0,bisect_left(days,r['out_date'])-bisect_left(days,r['ipo'])) if r['out_date'] else len(days)-bisect_left(days,r['ipo']) for r in universe}
        expected={s:n for s,n in expected.items() if n>0}
        missing=sorted((s for s,n in expected.items() if counts.get(s,0)<n),key=lambda s:(counts.get(s,0),s))
        return expected,missing

    def missing_ranges(self,symbol,start,end):
        with self.connect() as db:
            stock=db.execute('SELECT * FROM universe WHERE symbol=?',(symbol,)).fetchone()
            if not stock:return []
            days=[r[0] for r in db.execute("SELECT date FROM calendar WHERE trading=1 AND date BETWEEN ? AND ? AND date>=? AND (?='' OR date<?) ORDER BY date",(start,end,stock['ipo'],stock['out_date'],stock['out_date']))]
            present={r[0] for r in db.execute('SELECT date FROM breadth WHERE symbol=? AND date BETWEEN ? AND ? AND (trading=0 OR (change IS NOT NULL AND volume>=0))',(symbol,start,end))}
        if not days:return []
        if not present:return [(days[0],days[-1])]
        result=[];first=last=None
        for day in days:
            if day in present:
                if first:result.append((first,last));first=last=None
            else:
                if first is None:first=day
                last=day
        if first:result.append((first,last))
        return result

    def chart_revision(self):
        with self.connect() as db:return db.execute('SELECT value FROM market_revision WHERE id=1').fetchone()[0]

    def enqueue_calculations(self,start,end,changed_inputs=False):
        with self.connect() as db:
            clause='' if changed_inputs else ' AND t.value IS NULL'
            db.execute("INSERT OR IGNORE INTO indicator_dirty(indicator,date) SELECT 'trin',b.date FROM breadth b LEFT JOIN trin t ON t.date=b.date WHERE b.date BETWEEN ? AND ?"+clause+' GROUP BY b.date',(start,end))

    def replace_universe(self,records):
        values=[(r['code'],r['code_name'],r.get('ipoDate') or '1990-01-01',r.get('outDate') or '') for r in records]
        with self.connect() as db:
            old={r['symbol']:(r['ipo'],r['out_date']) for r in rows(db,'SELECT * FROM universe')}
            new={r[0]:(r[2],r[3]) for r in values}
            db.execute('DELETE FROM universe')
            db.executemany('INSERT INTO universe VALUES(?,?,?,?)',values)
            for symbol in old.keys() | new.keys():
                previous,current=old.get(symbol),new.get(symbol)
                if previous==current:continue
                boundaries=sorted({boundary for item in [previous,current] if item for boundary in [item[0],item[1] or '9999-12-31']})
                for first,last in zip(boundaries,boundaries[1:]):
                    active=lambda item:bool(item and item[0]<=first and (not item[1] or item[1]>first))
                    if active(previous)!=active(current):
                        db.execute("INSERT OR IGNORE INTO indicator_dirty(indicator,date) SELECT 'trin',date FROM breadth WHERE date>=? AND date<? GROUP BY date",(first,last))

    def calendar_gaps(self,start,end):
        with self.connect() as db:present={r[0] for r in db.execute('SELECT date FROM calendar WHERE date BETWEEN ? AND ?',(start,end))}
        result=[];first=last=None
        for i in range((date.fromisoformat(end)-date.fromisoformat(start)).days+1):
            day=str(date.fromisoformat(start)+timedelta(days=i))
            if day in present:
                if first:result.append((first,last));first=last=None
            else:
                if first is None:first=day
                last=day
        if first:result.append((first,last))
        return result

    def index_ranges(self,symbol,target,refresh=False):
        with self.connect() as db:
            info=db.execute('SELECT * FROM index_info WHERE symbol=?',(symbol,)).fetchone()
            if not info:return [('1990-01-01',target)]
            goal=info['last_date'] if refresh and info['last_date']==now().date().isoformat() else target
            days=[r[0] for r in db.execute('SELECT date FROM calendar WHERE trading=1 AND date BETWEEN ? AND ? ORDER BY date',(info['first_date'],goal))]
            present={r[0] for r in db.execute('SELECT date FROM index_bars WHERE symbol=? AND date BETWEEN ? AND ?',(symbol,info['first_date'],goal))}
        result=[];first=last=None
        for day in days:
            if day in present:
                if first:result.append((first,last));first=last=None
            else:
                if first is None:first=day
                last=day
        if first:result.append((first,last))
        if info['last_date']<goal:result.append((str(date.fromisoformat(info['last_date'])+timedelta(days=1)),goal))
        elif info['updated_at'][:10]<=goal and info['updated_at'][11:16]<'18:00':result.append((goal,goal))
        merged=[]
        for first,last in sorted(result):
            if merged and first<=str(date.fromisoformat(merged[-1][1])+timedelta(days=1)):
                merged[-1]=(merged[-1][0],max(last,merged[-1][1]))
            else:merged.append((first,last))
        return merged

    def calculate_pending(self,cancel=None,budget=2,limit=128,start='1990-01-01',end=None,progress=None):
        if not self.calculation_lock.acquire(blocking=False):return 0
        began=time.monotonic();processed=0;end=end or now().date().isoformat()
        try:
            with self.connect() as db:
                if db.execute('SELECT COUNT(*) FROM universe').fetchone()[0]<3000:return 0
                db.execute("DELETE FROM indicator_dirty WHERE indicator='trin' AND date IN (SELECT date FROM calendar WHERE trading=0)")
                days=[r[0] for r in db.execute("SELECT q.date FROM indicator_dirty q JOIN calendar c ON c.date=q.date AND c.trading=1 WHERE q.indicator='trin' AND q.date BETWEEN ? AND ? AND q.not_before<=? ORDER BY q.date DESC LIMIT ?",(start,end,now().timestamp(),limit))]
            for day in days:
                if cancel is not None and cancel.is_set():break
                if processed and time.monotonic()-began>=budget:break
                with self.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    if not db.execute("SELECT 1 FROM indicator_dirty WHERE indicator='trin' AND date=?",(day,)).fetchone():continue
                    calculate_trin(db,day)
                processed+=1
                if progress:progress(processed,len(days))
            return processed
        finally:self.calculation_lock.release()

    def preferences(self,body=None):
        with self.connect() as db:
            old=db.execute("SELECT value FROM meta WHERE key='preferences'").fetchone()
            prefs={**DEFAULT_PREFS,**(json.loads(old[0]) if old else {})}
            if body is not None:
                if set(body)-set(DEFAULT_PREFS):raise ValueError('未知市场设置')
                prefs.update(body)
                if not isinstance(prefs['symbols'],list) or not 1<=len(prefs['symbols'])<=len(INDICES) or len(set(prefs['symbols']))!=len(prefs['symbols']) or not set(prefs['symbols'])<=SYMBOLS:raise ValueError('请选择有效指数')
                if prefs['period'] not in {'d','w','m'} or prefs['chart_type'] not in {'line','candlestick'}:raise ValueError('无效图表周期')
                values=prefs['ma_periods']
                if not isinstance(values,list) or not 1<=len(values)<=8 or any(type(n) is not int or not 1<=n<=1000 for n in values) or len(values)!=len(set(values)):raise ValueError('均线周期须为 1–1000 天，最多 8 条且不能重复')
                for key in ['ma_enabled','volume_enabled','trin_enabled','normalize']:
                    if type(prefs[key]) is not bool:raise ValueError('无效图表选项')
                db.execute("INSERT INTO meta VALUES('preferences',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(prefs,ensure_ascii=False),))
            return prefs

    def save_index(self,symbol,result):
        values=[]
        for r in result['bars']:
            day=date.fromisoformat(str(r['date'])[:10]).isoformat()
            o,h,l,c=[finite(r[k],True) for k in ['open','high','low','close']]
            if day>now().date().isoformat() or h<max(o,c,l) or l>min(o,c,h):raise ValueError('指数 OHLC 校验失败')
            volume=finite(r.get('volume',0))
            if volume<0:raise ValueError('指数成交量为负')
            # 统一为股；新浪与东财指数日线的源单位由适配器明确提供。
            if result['volume_unit']=='手':volume*=100
            values.append((symbol,day,o,h,l,c,volume,result['source']))
        if not values:raise ValueError('未收到指数历史')
        with self.connect() as db:
            db.executemany('''INSERT INTO index_bars VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(symbol,date) DO UPDATE SET
                open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume,source=excluded.source
                WHERE index_bars.open IS NOT excluded.open OR index_bars.high IS NOT excluded.high OR index_bars.low IS NOT excluded.low
                OR index_bars.close IS NOT excluded.close OR index_bars.volume IS NOT excluded.volume''',values)
            bounds=db.execute('SELECT MIN(date),MAX(date) FROM index_bars WHERE symbol=?',(symbol,)).fetchone()
            db.execute('INSERT OR REPLACE INTO index_info VALUES(?,?,?,?,?)',(symbol,result['source'],*bounds,now().isoformat()))

    def save_snapshot(self,result):
        values=trin_value(result['stocks'])
        day=date.fromisoformat(result['date']).isoformat()
        with self.connect() as db:
            existing=db.execute('SELECT provisional FROM trin WHERE date=?',(day,)).fetchone()
            if existing and not existing[0] and result['provisional']:return
            db.execute('INSERT OR REPLACE INTO trin VALUES(?,?,?,?,?,?,?,?,?,?)',(day,values['value'],values['advances'],values['declines'],values['up_volume'],values['down_volume'],1,result['source'],int(result['provisional']),'' if values['value'] is not None else '上涨或下跌组为空'))

    def history(self):
        prefs=self.preferences()
        with self.connect() as db:
            revision=db.execute('SELECT value FROM market_revision WHERE id=1').fetchone()[0]
            if revision!=self.cached_revision:
                self.history_cache.clear();self.cached_revision=revision
            key=json.dumps(prefs,sort_keys=True)
            if key in self.history_cache:
                self.history_cache.move_to_end(key)
                return deepcopy(self.history_cache[key])
            daily={s:rows(db,'SELECT date,open,high,low,close,volume FROM index_bars WHERE symbol=? ORDER BY date',(s,)) for s in prefs['symbols']}
            series={s:aggregate_bars(bars,prefs['period'],prefs['ma_periods'] if prefs['ma_enabled'] else []) for s,bars in daily.items()}
            trin=rows(db,'SELECT * FROM trin ORDER BY date')
            if prefs['period']!='d':
                groups=defaultdict(list);expected=defaultdict(set)
                for bars in daily.values():
                    for bar in bars:expected[period_key(bar['date'],prefs['period'])].add(bar['date'])
                for row in trin:groups[period_key(row['date'],prefs['period'])].append(row)
                result=[]
                for key,group in groups.items():
                    observed={r['date'] for r in group if r['value'] is not None}
                    complete=expected[key]<=observed and all(r['value'] is not None for r in group)
                    day=max(expected[key] | {r['date'] for r in group})
                    result.append({**group[-1],'date':day,'value':sum(r['value'] for r in group)/len(group) if complete else None,'provisional':any(r['provisional'] for r in group),'observations':len(group),'reason':'' if complete else '所选周期的历史日值未齐'})
                trin=result
            result={'preferences':prefs,'series':series,'trin':trin,'coverage':rows(db,'SELECT * FROM index_info ORDER BY symbol'),'revision':revision}
            self.history_cache[key]=result
            while len(self.history_cache)>3 or (len(self.history_cache)>1 and sum(sum(len(v) for v in item['series'].values())+len(item['trin']) for item in self.history_cache.values())>100000):
                self.history_cache.popitem(last=False)
            return deepcopy(result)

    def reconstruct(self,start,end,cancel=None,progress=None):
        with self.connect() as db:
            if db.execute('SELECT COUNT(*) FROM universe').fetchone()[0]<3000:raise ValueError('历史证券目录不完整，未生成 TRIN')
            days=[r['date'] for r in rows(db,'SELECT date FROM calendar WHERE trading=1 AND date BETWEEN ? AND ? ORDER BY date',(start,end))]
        for day in days:
            if cancel is not None and cancel.is_set():return
            if progress:progress(day)
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                calculate_trin(db,day)


class MarketService:
    def __init__(self,store):
        self.store=store;self.lock=threading.Lock();self.cancel=threading.Event()
        self.progress={'running':False,'phase':'','done':0,'total':0,'errors':[],'revision':0}
        self.calculation={'running':False,'done':0,'total':0,'error':''}
        self.calculation_cancel=threading.Event()
        if not self.store.setting('history_enabled',default=True):self.calculation_cancel.set()
        self.thread=None;self.request_interval=1.5;self.last_request=0
        self.index_refresh=False;self.index_target=None
        self.network=MarketBudget(str(self.store.path))
        from .market_jobs import MarketScheduler
        self.scheduler=MarketScheduler(self)

    def shutdown(self):
        self.scheduler.stop.set()
        self.cancel.set()
        self.calculation_cancel.set()
        deadline=time.monotonic()+2
        for thread in [self.thread,self.scheduler.calculation_thread]:
            if thread and thread.is_alive():thread.join(timeout=max(0,deadline-time.monotonic()))

    def pause(self):
        self.store.setting('history_enabled',False,write=True)
        self.cancel.set()
        self.calculation_cancel.set()

    def background(self,enabled):
        if type(enabled) is not bool:raise ValueError('无效的后台回补设置')
        self.store.setting('history_enabled',enabled,write=True)
        if enabled:
            self.store.setting('history_next_at',0,write=True)
            self.calculation_cancel.clear()
        else:
            self.calculation_cancel.set()
            if self.progress.get('kind')=='history':self.cancel.set()

    def request(self,action,payload,timeout=60):
        if self.cancel.wait(max(0,self.request_interval-(time.monotonic()-self.last_request))):
            return {'ok':False,'cancelled':True,'error':'同步已停止'}
        self.last_request=time.monotonic()
        result=fetch(action,{**payload,'_market_cache':str(self.store.path),'_market_scope':'history' if action in {'market_universe','market_universe_ak','market_calendar','market_calendar_ak','market_breadth_batch','market_breadth_stock_ak'} else 'daily'},timeout=timeout,cancel=self.cancel)
        if result.get('limited'):raise MarketLimit(result['error'],result['retry_at'],result.get('source',''))
        return result

    def request_with_backup(self,primary,backup,payload,timeout=180):
        try:result=self.request(primary,payload,timeout=timeout)
        except MarketLimit as exc:
            if not exc.source:raise
            result={'ok':False}
        if not result['ok'] and not self.cancel.is_set():result=self.request(backup,payload,timeout=timeout)
        return result

    def state(self):
        with self.lock:
            progress={**self.progress,'errors':list(self.progress['errors'])}
            calculation=dict(self.calculation)
        progress['job_revision']=progress['revision']
        progress['revision']=self.store.chart_revision()
        with self.store.connect() as db:
            stats=db.execute('SELECT COUNT(*),MIN(date),MAX(date) FROM trin WHERE value IS NOT NULL').fetchone()
            latest=db.execute('SELECT * FROM trin WHERE value IS NOT NULL ORDER BY date DESC LIMIT 1').fetchone()
        return {'catalog':INDICES,'preferences':self.store.preferences(),'progress':progress,'calculation':calculation,'latest_trin':dict(latest) if latest else None,'trin_coverage':{'count':stats[0],'first':stats[1],'last':stats[2]},'cache_path':str(self.store.path),
                'network':{**self.network.status(),'message':self.store.setting('market_network_message',default=''),'retry_at':self.store.setting('market_network_retry',default=0)},
                'background':{'enabled':self.store.setting('history_enabled',default=True),'plan':self.store.setting('history_plan'),'next_at':self.store.setting('history_next_at',default=0),'last_daily':self.store.setting('daily_success'),'sync_time':'18:00','error':self.store.setting('background_error',default='')}}

    def update(self,**values):
        with self.lock:self.progress.update(values)

    def changed(self):
        with self.lock:self.progress['revision']+=1

    def start(self,kind='indices',start=None,end=None,budget=None,managed=False,daily_target=None):
        with self.lock:
            if self.progress['running']:return False
            self.progress.update(running=True,kind=kind,phase='准备同步',done=0,total=0,errors=[])
            self.cancel.clear()
        if kind=='indices':
            from .market_jobs import last_closed_day
            self.index_refresh=daily_target is None
            daily_target=daily_target or last_closed_day(self.store)
            self.index_target=daily_target
            self.store.setting('daily_attempt',daily_target,write=True)
        elif not managed:
            self.store.setting('history_plan',{'start':start,'end':end},write=True)
            self.store.setting('history_enabled',True,write=True)
            self.calculation_cancel.clear()
        if kind=='history' and budget is None:budget=75
        def work():
            try:
                if kind=='history':
                    result=self.backfill(start,end,budget=budget)
                    if not self.cancel.is_set():self.scheduler.completed(start,end,result)
                else:
                    self.sync_indices()
                    if not self.cancel.is_set() and not self.progress['errors']:
                        self.store.setting('daily_success',daily_target,write=True)
                        self.store.setting('daily_retry_at',0,write=True)
            except MarketLimit as exc:
                self.store.setting('history_next_at',exc.retry_at,write=True)
                self.store.setting('market_network_retry',exc.retry_at,write=True)
                self.store.setting('market_network_message',str(exc),write=True)
                if kind=='indices':self.store.setting('daily_retry_at',exc.retry_at,write=True)
                self.update(phase=str(exc))
            except Exception as exc:
                if not self.cancel.is_set():
                    with self.lock:self.progress['errors'].append(str(exc)[:350])
                    self.store.setting('history_next_at',now().timestamp()+900,write=True)
            finally:
                self.update(running=False,phase='已停止' if self.cancel.is_set() else '同步结束')
                self.changed()
        self.thread=threading.Thread(target=work,daemon=True)
        self.thread.start()
        return True

    def sync_indices(self):
        from .market_jobs import last_closed_day
        target=self.index_target or last_closed_day(self.store)
        self.update(total=len(INDICES)+1)
        for i,index in enumerate(INDICES):
            if self.cancel.is_set():return
            symbol=index['symbol'];self.update(phase='同步 '+index['name'],done=i)
            with self.store.connect() as db:
                info=db.execute('SELECT * FROM index_info WHERE symbol=?',(symbol,)).fetchone()
            actions=['market_index_em','market_index_sina'] if info else ['market_index_sina','market_index_em']
            for start,end in self.store.index_ranges(symbol,target,self.index_refresh):
                errors=[]
                for action in actions:
                    if self.cancel.is_set():return
                    try:
                        result=self.request(action,{'symbol':symbol,'start':start,'end':end},timeout=60)
                        if not result['ok']:raise ValueError(result.get('error','数据源无响应'))
                        self.store.save_index(symbol,result['data']);self.changed();break
                    except MarketLimit as exc:
                        if not exc.source:raise
                        errors.append(str(exc))
                    except Exception as exc:errors.append(str(exc)[:150])
                else:
                    with self.lock:self.progress['errors'].append(index['name']+'：'+'；'.join(errors))
        if self.cancel.is_set():return
        with self.store.connect() as db:
            cached=db.execute('SELECT date FROM trin WHERE date>=? AND value IS NOT NULL AND provisional=0 ORDER BY date DESC LIMIT 1',(target,)).fetchone()
        if cached and (not self.index_refresh or cached['date']>=now().date().isoformat()):
            self.update(done=len(INDICES)+1);return
        self.update(phase='同步沪深市场 TRIN',done=len(INDICES))
        result=self.request('market_snapshot',{},timeout=600)
        if not result['ok']:raise ValueError(result.get('error','市场快照失败'))
        self.store.save_snapshot(result['data']);self.changed();self.update(done=len(INDICES)+1)

    def save_breadth(self,records,allowed,start,end):
        values=[]
        for r in records:
            if r['code'] not in allowed or not start<=r['date']<=end:continue
            try:
                change=finite(r['pctChg']) if r.get('pctChg') not in {None,''} else None
                volume=finite(r['volume']) if r.get('volume') not in {None,''} else 0
                trading=int(r.get('tradestatus') or 0)
                if volume<0 or (trading and change is None):continue
                values.append((r['code'],r['date'],change,volume,trading))
            except (ValueError,TypeError):continue
        with self.store.connect() as db:db.executemany('''INSERT INTO breadth VALUES(?,?,?,?,?) ON CONFLICT(symbol,date) DO UPDATE SET
            change=excluded.change,volume=excluded.volume,trading=excluded.trading
            WHERE breadth.change IS NOT excluded.change OR breadth.volume IS NOT excluded.volume OR breadth.trading IS NOT excluded.trading''',values)

    def backfill(self,start,end,budget=None):
        start=date.fromisoformat(start).isoformat();end=date.fromisoformat(end).isoformat()
        if start>end or start<'1990-01-01' or end>now().date().isoformat():raise ValueError('历史回补日期应在 1990 年至今天之间')
        began=time.monotonic()
        with self.store.connect() as db:count=db.execute('SELECT COUNT(*) FROM universe').fetchone()[0]
        # Complete cached inputs take the offline path even if source metadata is old.
        if count>=3000 and not self.store.calendar_gaps(start,end):
            expected,missing=self.store.history_gaps(start,end)
            if not missing:
                self.update(total=len(expected),done=len(expected),phase='从缓存补算 TRIN')
                self.store.enqueue_calculations(start,end)
                with self.store.connect() as db:db.execute('UPDATE indicator_dirty SET not_before=0 WHERE date BETWEEN ? AND ?',(start,end))
                self.store.calculate_pending(cancel=self.cancel,start=start,end=end)
                return {'complete':True,'wait':30}
        universe_date=self.store.setting('universe_date')
        if universe_date is None and count>=3000:
            with self.store.connect() as db:cached=db.execute('SELECT MAX(updated_at) FROM index_info').fetchone()[0]
            universe_date=(cached or '')[:10]
            self.store.setting('universe_date',universe_date,write=True)
        if count<3000 or end>(universe_date or ''):
            self.update(phase='读取历史上市与退市证券目录')
            universe=self.request_with_backup('market_universe','market_universe_ak',{},timeout=180)
            if not universe['ok'] or len(universe['data'])<3000:raise ValueError(universe.get('error','历史证券目录不完整'))
            self.store.replace_universe(universe['data'])
            self.store.setting('universe_date',now().date().isoformat(),write=True)
        for first,last in self.store.calendar_gaps(start,end):
            if self.cancel.is_set():return
            self.update(phase='补齐交易日历 '+first+' — '+last)
            payload={'start':first,'end':last}
            calendar=self.request_with_backup('market_calendar','market_calendar_ak',payload,timeout=60)
            if not calendar['ok'] or not calendar['data']:raise ValueError(calendar.get('error','交易日历为空，等待重试'))
            trading={r['calendar_date']:int(r['is_trading_day']) for r in calendar['data']}
            days=[str(date.fromisoformat(first)+timedelta(days=i)) for i in range((date.fromisoformat(last)-date.fromisoformat(first)).days+1)]
            with self.store.connect() as db:db.executemany('INSERT INTO calendar VALUES(?,?) ON CONFLICT(date) DO UPDATE SET trading=excluded.trading WHERE calendar.trading IS NOT excluded.trading',[(d,trading.get(d,0)) for d in days])
        if self.cancel.is_set():return
        expected,missing=self.store.history_gaps(start,end)
        missing=set(missing)
        self.update(total=len(expected),done=len(expected)-len(missing))
        failures=self.store.setting('history_failures',default={})
        stock_failures=self.store.setting('history_stock_failures',default={})
        # Chunk keys survive changes to the selected overall range. Old failure
        # records still gate the first retry, but no longer stall unrelated years.
        old_key=lambda code:code+':'+start+':'+end
        retry_key=lambda code,first,last:code+':'+first+':'+last
        def failure_for(code,first,last):
            exact=failures.get(retry_key(code,first,last))
            if exact is not None:return exact
            inherited=[v for k,v in failures.items() if k.startswith(code+':') and k.split(':')[1]<=last and k.split(':')[2]>=first]
            return max(inherited,key=lambda v:v.get('next',0),default={})
        plans=defaultdict(list)
        for symbol in sorted(missing):
            if stock_failures.get(symbol,{}).get('next',0)>now().timestamp():continue
            for gap_start,gap_end in self.store.missing_ranges(symbol,start,end):
                for first,last in quarter_ranges(gap_start,gap_end):
                    state=failure_for(symbol,first,last)
                    if state.get('next',0)<=now().timestamp():plans[(first,last)].append(symbol)
        exhausted=False
        deferred=None
        for (first,last),symbols in sorted(plans.items(),key=repair_priority(self.store,start,end),reverse=True):
            for offset in range(0,len(symbols),8):
                if self.cancel.is_set():return
                batch=[s for s in symbols[offset:offset+8] if failure_for(s,first,last).get('next',0)<=now().timestamp() and stock_failures.get(s,{}).get('next',0)<=now().timestamp()]
                if not batch:continue
                self.update(phase=f'补齐历史缺口 {len(expected)-len(missing)} / {len(expected)}')
                primary_cooling=False
                try:response=self.request('market_breadth_batch',{'symbols':batch,'start':first,'end':last,'request_interval':5.0},timeout=180)
                except MarketLimit as exc:
                    if not exc.source:raise
                    primary_cooling=True
                    response={'ok':False,'error':str(exc)}
                if self.cancel.is_set():return
                data=response['data'] if response['ok'] else {'records':[],'completed':[],'errors':[{'symbol':s,'error':response.get('error','历史查询失败')} for s in batch]}
                self.save_breadth(data['records'],batch,first,last)
                if data.get('limited'):
                    if data.get('source'):primary_cooling=True
                    else:deferred=MarketLimit(data['error'],data['retry_at'])
                _,gap_missing=self.store.history_gaps(first,last,symbols=batch)
                retrying=[] if deferred else [s for s in batch if s in gap_missing and (primary_cooling or failure_for(s,first,last).get('attempts',0)>0)]
                for code in retrying:
                    if self.cancel.is_set():return
                    # A partially successful primary query must not redownload
                    # the days it just saved when switching to the backup.
                    for gap_start,gap_end in self.store.missing_ranges(code,first,last):
                        try:fallback=self.request('market_breadth_stock_ak',{'symbol':code,'start':gap_start,'end':gap_end},timeout=60)
                        except MarketLimit as exc:deferred=exc;break
                        if fallback['ok']:self.save_breadth(fallback['data']['records'],[code],gap_start,gap_end)
                    if deferred:break
                if retrying:_,gap_missing=self.store.history_gaps(first,last,symbols=batch)
                _,remaining=self.store.history_gaps(start,end,symbols=batch)
                missing.difference_update(batch);missing.update(remaining)
                errors={r['symbol']:r['error'] for r in data['errors']}
                for code in batch:
                    key=retry_key(code,first,last)
                    if code not in gap_missing:
                        with self.store.connect() as db:db.execute('INSERT OR IGNORE INTO fetch_ranges VALUES(?,?,?)',(code,first,last))
                        failures.pop(key,None)
                        if code in stock_failures:stock_failures[code]['next']=0
                        if code not in missing:failures.pop(old_key(code),None)
                    elif not deferred:
                        attempts=failure_for(code,first,last).get('attempts',0)+1
                        failures[key]={'attempts':attempts,'next':now().timestamp()+min(86400,900*2**min(attempts-1,7))}
                        previous=stock_failures.get(code,{})
                        today=now().date().isoformat()
                        daily=(previous.get('daily',0) if previous.get('date')==today else 0)+1
                        until=failures[key]['next']
                        if daily>=3:until=max(until,(now().replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)).timestamp())
                        stock_failures[code]={'date':today,'daily':daily,'next':until}
                        with self.lock:self.progress['errors'].append(code+'：'+errors.get(code,'历史行数不足，已保存有效数据并延后重试'))
                self.store.setting('history_failures',failures,write=True)
                self.store.setting('history_stock_failures',stock_failures,write=True)
                self.update(done=len(expected)-len(missing))
                if deferred:exhausted=True;break
                if budget is not None and time.monotonic()-began>=budget:exhausted=True;break
            if exhausted:break
        if self.cancel.is_set():return
        self.update(phase='从缓存补算 TRIN')
        self.store.enqueue_calculations(start,end)
        if not missing:
            with self.store.connect() as db:db.execute('UPDATE indicator_dirty SET not_before=0 WHERE date BETWEEN ? AND ?',(start,end))
        self.store.calculate_pending(cancel=self.cancel,start=start,end=end)
        if deferred:raise deferred
        wait=60
        if missing:
            retries=[v.get('next',0) for k,v in failures.items() if k.split(':')[0] in missing]+[v.get('next',0) for k,v in stock_failures.items() if k in missing]
            if not plans and retries:wait=max(60,min(retries)-now().timestamp())
        return {'complete':not missing,'wait':wait}
