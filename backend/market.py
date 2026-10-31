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
from .market_cache import install_cache, calculate_trin, save_breadth
from .market_history import repair_ranges, repair_priority, history_work
from .market_network import MarketBudget, MarketLimit
from .sync_schedule import TIMES
from .market_erp import save_component, calculate_pending as calculate_erp

INDICES = [
    {'symbol':'sh000001','name':'上证指数'}, {'symbol':'sz399102','name':'创业板综指'},
    {'symbol':'sh000688','name':'科创50'}, {'symbol':'sh000300','name':'沪深300'},
    {'symbol':'sz399006','name':'创业板指'}, {'symbol':'sz399001','name':'深证成指'},
    {'symbol':'sh000905','name':'中证500'}, {'symbol':'sh000852','name':'中证1000'},
    {'symbol':'sh000016','name':'上证50'}, {'symbol':'sz399106','name':'深证综指'},
]
SYMBOLS = {r['symbol'] for r in INDICES}
DEFAULT_PREFS = {'symbols':['sh000001'],'period':'d','chart_type':'candlestick','ma_enabled':False,
                 'ma_periods':[10,20,50,250],'volume_enabled':True,'trin_enabled':False,'erp_enabled':False,'normalize':True}
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
            if not db.execute("SELECT 1 FROM meta WHERE key='indicator_display_defaults'").fetchone():
                old=db.execute("SELECT value FROM meta WHERE key='preferences'").fetchone()
                if old:
                    prefs={**json.loads(old[0]),'trin_enabled':False,'erp_enabled':False}
                    db.execute("UPDATE meta SET value=? WHERE key='preferences'",(json.dumps(prefs,ensure_ascii=False),))
                db.execute("INSERT INTO meta VALUES('indicator_display_defaults','1')")

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
        elif info['updated_at'][:10]<=goal and info['updated_at'][11:16]<TIMES[-1]:result.append((goal,goal))
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
                processed=calculate_erp(db,limit)
                if db.execute('SELECT COUNT(*) FROM universe').fetchone()[0]<3000:return processed
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
                if progress:progress(processed,max(processed,len(days)))
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
                for key in ['ma_enabled','volume_enabled','trin_enabled','erp_enabled','normalize']:
                    if type(prefs[key]) is not bool:raise ValueError('无效图表选项')
                db.execute("INSERT INTO meta VALUES('preferences',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(prefs,ensure_ascii=False),))
                if prefs['erp_enabled']:db.execute("INSERT INTO meta VALUES('erp_requested','true') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
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
            trin=rows(db,'SELECT t.* FROM trin t LEFT JOIN calendar c ON c.date=t.date WHERE c.trading IS NULL OR c.trading=1 ORDER BY t.date')
            pe_count,months=db.execute('SELECT COUNT(*),COUNT(DISTINCT SUBSTR(date,1,7)) FROM erp_pe').fetchone()
            erp_frequency='monthly' if pe_count and pe_count<=months*1.1 else 'daily'
            erp=rows(db,'SELECT p.date,e.value,p.pe,e.bond_yield,p.source pe_source,e.bond_source FROM erp_pe p LEFT JOIN erp e ON e.date=p.date ORDER BY p.date') if pe_count else rows(db,'SELECT * FROM erp ORDER BY date')
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
                groups=defaultdict(list)
                for row in erp:groups[period_key(row['date'],prefs['period'])].append(row)
                erp=[{**group[-1],'date':max(expected[key] | {r['date'] for r in group}),'observed_date':group[-1]['date'],
                      'value':group[-1]['value'] if erp_frequency=='monthly' else sum(r['value'] for r in group)/len(group) if expected[key]<={r['date'] for r in group if r['value'] is not None} else None,
                      'observations':len(group)} for key,group in groups.items()]
            result={'preferences':prefs,'series':series,'trin':trin,'erp':erp,'erp_frequency':erp_frequency,'coverage':rows(db,'SELECT * FROM index_info ORDER BY symbol'),'revision':revision}
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
        self.thread=None;self.request_interval=1.5;self.last_request=0
        self.index_refresh=False;self.index_target=None
        self.manual=False
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

    def background(self,enabled):
        if type(enabled) is not bool:raise ValueError('无效的后台回补设置')
        self.store.setting('history_enabled',enabled,write=True)
        if enabled:
            self.store.setting('history_next_at',0,write=True)
        elif self.progress.get('kind') in {'history','erp'}:self.cancel.set()

    def request(self,action,payload,timeout=60):
        if self.cancel.wait(max(0,self.request_interval-(time.monotonic()-self.last_request))):
            return {'ok':False,'cancelled':True,'error':'同步已停止'}
        self.last_request=time.monotonic()
        scope='history' if self.progress.get('kind') in {'history','erp'} or action in {'market_breadth_batch','market_breadth_stock_ak'} else 'daily'
        result=fetch(action,{**payload,'_market_cache':str(self.store.path),'_market_scope':scope,'_market_manual':self.manual},timeout=timeout,cancel=self.cancel)
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
            pending=db.execute('SELECT COUNT(*) FROM trin t LEFT JOIN calendar c ON c.date=t.date WHERE t.value IS NULL AND (c.trading IS NULL OR c.trading=1)').fetchone()[0]
            gap=db.execute('SELECT t.date,t.coverage,t.reason FROM trin t LEFT JOIN calendar c ON c.date=t.date WHERE t.value IS NULL AND (c.trading IS NULL OR c.trading=1) ORDER BY t.date DESC LIMIT 1').fetchone()
            erp_stats=db.execute('SELECT COUNT(*),MIN(date),MAX(date) FROM erp').fetchone()
            latest_erp=db.execute('SELECT * FROM erp ORDER BY date DESC LIMIT 1').fetchone()
        return {'catalog':INDICES,'preferences':self.store.preferences(),'progress':progress,'calculation':calculation,'latest_trin':dict(latest) if latest else None,'trin_coverage':{'count':stats[0],'first':stats[1],'last':stats[2]},'cache_path':str(self.store.path),
                'history_pending':{'count':pending,'latest':dict(gap) if gap else None},
                'erp_coverage':{'count':erp_stats[0],'first':erp_stats[1],'last':erp_stats[2]},'latest_erp':dict(latest_erp) if latest_erp else None,'erp_error':self.store.setting('erp_error',default=''),
                'network':{**self.network.status(),'message':self.store.setting('market_network_message',default=''),'retry_at':self.store.setting('market_network_retry',default=0)},
                'background':{'enabled':self.store.setting('history_enabled',default=True),'plan':self.store.setting('history_plan'),'next_at':self.store.setting('history_next_at',default=0),'last_daily':self.store.setting('daily_success'),'sync_time':' / '.join(TIMES),'error':self.store.setting('background_error',default='')}}

    def update(self,**values):
        with self.lock:self.progress.update(values)

    def changed(self):
        with self.lock:self.progress['revision']+=1

    def start(self,kind='indices',start=None,end=None,budget=None,managed=False,daily_target=None,automatic=None):
        with self.lock:
            if self.progress['running']:return False
            self.progress.update(running=True,kind=kind,phase='准备同步',done=0,total=0,errors=[])
            self.cancel.clear()
            self.manual=not managed and automatic is None
        if automatic:self.store.setting('market_auto_attempt',automatic,write=True)
        if kind=='indices':
            from .market_jobs import last_closed_day
            self.index_refresh=True
            moment=now()
            daily_target=daily_target or (moment.date().isoformat() if moment.weekday()<5 and moment.strftime('%H:%M')>='09:30' else last_closed_day(self.store))
            self.index_target=daily_target
            self.store.setting('daily_attempt',daily_target,write=True)
        elif kind=='history' and not managed:
            self.store.setting('history_plan',{'start':start,'end':end},write=True)
            self.store.setting('history_enabled',True,write=True)
            self.calculation_cancel.clear()
        if kind=='history' and budget is None:budget=75
        def work():
            try:
                if kind=='history':
                    result=self.backfill(start,end,budget=budget)
                    if not self.cancel.is_set():self.scheduler.completed(start,end,result)
                elif kind=='erp':self.sync_erp(history=True)
                else:
                    self.sync_indices()
                    self.sync_erp()
                    if not self.cancel.is_set() and not self.progress['errors']:
                        self.store.setting('daily_success',daily_target,write=True)
                        self.store.setting('daily_retry_at',0,write=True)
            except MarketLimit as exc:
                self.store.setting('history_next_at',exc.retry_at,write=True)
                self.store.setting('market_network_retry',exc.retry_at,write=True)
                self.store.setting('market_network_message',str(exc),write=True)
                self.update(phase=str(exc))
            except Exception as exc:
                if not self.cancel.is_set():
                    with self.lock:self.progress['errors'].append(str(exc)[:350])
                    self.store.setting('history_next_at',now().timestamp()+900,write=True)
            finally:
                if automatic:self.store.setting('market_auto_completed',automatic,write=True)
                self.update(running=False,phase='已停止' if self.cancel.is_set() else '同步结束')
                self.changed()
        self.thread=threading.Thread(target=work,daemon=True)
        self.thread.start()
        return True

    def sync_erp(self,history=False):
        if not self.store.setting('erp_requested',default=False) or self.cancel.is_set():return
        from .market_jobs import last_closed_day
        target=last_closed_day(self.store);errors=[];attempted=False
        with self.store.connect() as db:
            pe_first,pe_last=db.execute('SELECT MIN(date),MAX(date) FROM erp_pe').fetchone()
            bond_first,bond_last=db.execute('SELECT MIN(date),MAX(date) FROM erp_bond').fetchone()
            bond_gap=db.execute('''SELECT MIN(p.date),MAX(p.date) FROM erp_pe p WHERE p.date BETWEEN ? AND ?
                AND NOT EXISTS(SELECT 1 FROM erp_bond b WHERE b.date=p.date)''',(bond_first or target,min(target,bond_last or target))).fetchone()
        if (not pe_last or pe_last<target) and (self.manual or self.store.setting('erp_pe_attempt')!=target):
            self.store.setting('erp_pe_attempt',target,write=True)
            attempted=True
            self.update(phase='读取沪深300滚动市盈率')
            try:result=self.request('market_erp_pe',{},timeout=150)
            except MarketLimit as exc:
                if not exc.source:raise
                result={'ok':False,'error':str(exc)}
            if result['ok']:
                with self.store.connect() as db:
                    save_component(db,'pe',result['data'],now().date().isoformat())
                    pe_first,pe_last=db.execute('SELECT MIN(date),MAX(date) FROM erp_pe').fetchone()
            else:errors.append(result.get('error','指数估值数据暂不可用'))
        first=last=None
        if not bond_last:first=str(date.fromisoformat(target)-timedelta(days=364));last=target
        elif bond_gap[0]:first=bond_gap[0];last=min(bond_gap[1],str(date.fromisoformat(first)+timedelta(days=364)))
        elif bond_last<target:first=str(date.fromisoformat(bond_last)+timedelta(days=1));last=min(target,str(date.fromisoformat(first)+timedelta(days=364)))
        elif history and pe_first and bond_first>pe_first:
            last=str(date.fromisoformat(bond_first)-timedelta(days=1));first=max(pe_first,str(date.fromisoformat(last)-timedelta(days=364)))
        if first and (self.manual or self.store.setting('erp_bond_attempt')!=[first,last,target]):
            self.store.setting('erp_bond_attempt',[first,last,target],write=True)
            attempted=True
            self.update(phase='读取中国10年期国债历史收益率')
            try:result=self.request_with_backup('market_erp_bond','market_erp_bond_em',{'start':first,'end':last},timeout=150)
            except MarketLimit as exc:
                if not exc.source:raise
                result={'ok':False,'error':str(exc)}
            if result['ok']:
                with self.store.connect() as db:save_component(db,'bond',result['data'],now().date().isoformat())
            else:errors.append(result.get('error','国债收益率数据暂不可用'))
        self.store.calculate_pending(cancel=self.cancel)
        if attempted:self.store.setting('erp_error','；'.join(errors),write=True)

    def sync_indices(self):
        from .market_jobs import last_closed_day
        target=self.index_target or last_closed_day(self.store)
        selected=set(self.store.preferences()['symbols'])
        indices=[index for index in INDICES if index['symbol'] in selected]
        self.update(total=len(indices)+1)
        for i,index in enumerate(indices):
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
                        result=self.request(action,{'symbol':symbol,'start':start,'end':end},timeout=120)
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
            self.update(done=len(indices)+1);return
        self.update(phase='同步沪深市场 TRIN',done=len(indices))
        if self.store.setting('universe_date')!=now().date().isoformat():
            universe=self.request_with_backup('market_universe','market_universe_ak',{},timeout=240)
            if not universe['ok'] or len(universe['data'])<3000:raise ValueError('证券目录未齐，保留已缓存的 TRIN')
            self.store.replace_universe(universe['data'])
            self.store.setting('universe_date',now().date().isoformat(),write=True)
        with self.store.connect() as db:
            symbols=[r[0].replace('.','') for r in db.execute("SELECT symbol FROM universe WHERE ipo<=? AND (out_date='' OR out_date>?) ORDER BY symbol",(target,target))]
        result=self.request('market_snapshot',{'symbols':symbols},timeout=900)
        if not result['ok']:raise ValueError(result.get('error','市场快照失败'))
        self.store.save_snapshot(result['data']);self.changed();self.update(done=len(indices)+1)

    def save_breadth(self,records,allowed,start,end):
        with self.store.connect() as db:save_breadth(db,records,allowed,start,end)

    def backfill(self,start,end,budget=None):
        self.update(kind='history')
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
            universe=self.request_with_backup('market_universe','market_universe_ak',{},timeout=300)
            if not universe['ok'] or len(universe['data'])<3000:raise ValueError(universe.get('error','历史证券目录不完整'))
            self.store.replace_universe(universe['data'])
            self.store.setting('universe_date',now().date().isoformat(),write=True)
        for first,last in self.store.calendar_gaps(start,end):
            if self.cancel.is_set():return
            self.update(phase='补齐交易日历 '+first+' — '+last)
            payload={'start':first,'end':last}
            calendar=self.request_with_backup('market_calendar','market_calendar_ak',payload,timeout=180)
            if not calendar['ok'] or not calendar['data']:raise ValueError(calendar.get('error','交易日历为空，等待重试'))
            trading={r['calendar_date']:int(r['is_trading_day']) for r in calendar['data']}
            days=[str(date.fromisoformat(first)+timedelta(days=i)) for i in range((date.fromisoformat(last)-date.fromisoformat(first)).days+1)]
            with self.store.connect() as db:db.executemany('INSERT INTO calendar VALUES(?,?) ON CONFLICT(date) DO UPDATE SET trading=excluded.trading WHERE calendar.trading IS NOT excluded.trading',[(d,trading.get(d,0)) for d in days])
        if self.cancel.is_set():return
        expected,missing=self.store.history_gaps(start,end)
        missing=set(missing)
        self.update(total=len(expected),done=len(expected)-len(missing))
        # Publish usable cached inputs before deciding whether any network work
        # is necessary. Verified daily snapshots need no duplicate raw download.
        self.store.enqueue_calculations(start,end)
        self.store.calculate_pending(cancel=self.cancel,start=start,end=end)
        work_start,work_end,work_days,sparse=history_work(self.store,start,end)
        if not work_days:return {'complete':True,'wait':30}
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
            for gap_start,gap_end in self.store.missing_ranges(symbol,work_start,work_end):
                for first,last in repair_ranges(gap_start,gap_end):
                    state=failure_for(symbol,first,last)
                    if state.get('next',0)<=now().timestamp():plans[(first,last)].append(symbol)
        # Sparse whole-market history is cheaper by day; almost-complete years
        # are cheaper by stock. Both paths use the exact same request budget.
        if sparse and len(work_days)<sum(map(len,plans.values())) and self.store.setting('history_daily_disabled_until',default=0)<=now().timestamp():
            return self.backfill_days(start,end,work_days,budget,began)
        exhausted=False
        deferred=None
        for (first,last),symbols in sorted(plans.items(),key=repair_priority(self.store,start,end),reverse=True):
            for offset in range(0,len(symbols),8):
                if self.cancel.is_set():return
                batch=[s for s in symbols[offset:offset+8] if failure_for(s,first,last).get('next',0)<=now().timestamp() and stock_failures.get(s,{}).get('next',0)<=now().timestamp()]
                if not batch:continue
                self.update(phase=f'补齐历史缺口 {len(expected)-len(missing)} / {len(expected)}')
                primary_cooling=False
                try:response=self.request('market_breadth_batch',{'symbols':batch,'start':first,'end':last,'request_interval':30.0},timeout=900)
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
                # Publish repaired days after each saved batch, even if a later
                # batch times out or exhausts today's network budget.
                self.store.calculate_pending(cancel=self.cancel,start=first,end=last)
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

    def backfill_days(self,start,end,days,budget,began):
        failures=self.store.setting('history_day_failures',default={})
        pending=[day for day in reversed(days) if failures.get(day,{}).get('next',0)<=now().timestamp()]
        with self.store.connect() as db:allowed=[r[0] for r in db.execute('SELECT symbol FROM universe')]
        for offset in range(0,len(pending),8):
            if self.cancel.is_set():return
            batch=pending[offset:offset+8]
            self.update(phase=f'按交易日补齐全市场历史 {batch[-1]} — {batch[0]}')
            try:response=self.request('market_breadth_days',{'dates':batch},timeout=900)
            except MarketLimit:raise
            if self.cancel.is_set():return
            if not response['ok']:
                # Unsupported SDK/server or an unavailable endpoint must not
                # repeatedly consume the budget; retain the stock route.
                self.store.setting('history_daily_disabled_until',now().timestamp()+86400,write=True)
                with self.lock:self.progress['errors'].append(response.get('error','全市场日线查询失败')[:250])
                return {'complete':False,'wait':900}
            data=response['data']
            self.save_breadth(data['records'],allowed,min(batch),max(batch))
            self.store.calculate_pending(cancel=self.cancel,start=min(batch),end=max(batch))
            _,_,remaining,_=history_work(self.store,min(batch),max(batch))
            gaps=set(remaining)
            errors={r['date']:r['error'] for r in data.get('errors',[])}
            for day in batch:
                if day not in gaps:failures.pop(day,None)
                elif not data.get('limited'):
                    attempts=failures.get(day,{}).get('attempts',0)+1
                    failures[day]={'attempts':attempts,'next':now().timestamp()+min(86400,900*2**min(attempts-1,7))}
                    with self.lock:self.progress['errors'].append(day+'：'+errors.get(day,'全市场历史样本未齐，稍后重试'))
            self.store.setting('history_day_failures',failures,write=True)
            if data.get('limited'):raise MarketLimit(data['error'],data['retry_at'],data.get('source',''))
            if errors:
                self.store.setting('history_daily_disabled_until',now().timestamp()+86400,write=True)
                break
            if budget is not None and time.monotonic()-began>=budget:break
        _,_,remaining,_=history_work(self.store,start,end)
        wait=max(60,min((value['next'] for day,value in failures.items() if day in remaining),default=now().timestamp())-now().timestamp()) if not pending else 60
        return {'complete':not remaining,'wait':wait}
