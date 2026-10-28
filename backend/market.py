"""Independent public market cache and resumable breadth jobs. No portfolio access."""
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, timedelta
import json
import math
from pathlib import Path
import sqlite3
import threading

from .providers import fetch
from .storage import now, rows

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
        with self.connect() as db:db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        with self.lock:
            db=sqlite3.connect(self.path,timeout=15);db.row_factory=sqlite3.Row
            try:yield db;db.commit()
            except BaseException:db.rollback();raise
            finally:db.close()

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
            db.executemany('INSERT OR REPLACE INTO index_bars VALUES(?,?,?,?,?,?,?,?)',values)
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
            return {'preferences':prefs,'series':series,'trin':trin,'coverage':rows(db,'SELECT * FROM index_info ORDER BY symbol')}

    def reconstruct(self,start,end,cancel=None,progress=None):
        with self.connect() as db:
            universe=rows(db,'SELECT * FROM universe')
            if len(universe)<3000:raise ValueError('历史证券目录不完整，未生成 TRIN')
            days=[r['date'] for r in rows(db,'SELECT date FROM calendar WHERE trading=1 AND date BETWEEN ? AND ? ORDER BY date',(start,end))]
        for day in days:
            if cancel is not None and cancel.is_set():return
            if progress:progress(day)
            with self.connect() as db:
                expected={s['symbol'] for s in universe if s['ipo']<=day and (not s['out_date'] or s['out_date']>=day)}
                observed=[r for r in rows(db,'SELECT symbol,change,volume,trading FROM breadth WHERE date=?',(day,)) if r['symbol'] in expected]
                actual={r['symbol'] for r in observed}
                coverage=len(actual & expected)/len(expected) if expected else 0
                values=trin_value(observed)
                valid=expected and expected<=actual and values['value'] is not None
                if not valid and db.execute('SELECT 1 FROM trin WHERE date=? AND value IS NOT NULL',(day,)).fetchone():
                    continue  # 未完成的回补不能覆盖已经核实的市场快照。
                db.execute('INSERT OR REPLACE INTO trin VALUES(?,?,?,?,?,?,?,?,?,?)',(day,values['value'] if valid else None,values['advances'],values['declines'],values['up_volume'],values['down_volume'],coverage,'历史全市场 / BaoStock、AKShare',0,'' if valid else '历史样本未齐，未发布数值'))


class MarketService:
    def __init__(self,store):
        self.store=store;self.lock=threading.Lock();self.cancel=threading.Event()
        self.progress={'running':False,'phase':'','done':0,'total':0,'errors':[],'revision':0}
        self.thread=None

    def shutdown(self):
        self.cancel.set()
        if self.thread and self.thread.is_alive():self.thread.join(timeout=2)

    def state(self):
        with self.lock:
            progress={**self.progress,'errors':list(self.progress['errors'])}
        with self.store.connect() as db:
            stats=db.execute('SELECT COUNT(*),MIN(date),MAX(date) FROM trin WHERE value IS NOT NULL').fetchone()
            latest=db.execute('SELECT * FROM trin WHERE value IS NOT NULL ORDER BY date DESC LIMIT 1').fetchone()
        return {'catalog':INDICES,'preferences':self.store.preferences(),'progress':progress,'latest_trin':dict(latest) if latest else None,'trin_coverage':{'count':stats[0],'first':stats[1],'last':stats[2]},'cache_path':str(self.store.path)}

    def update(self,**values):
        with self.lock:self.progress.update(values)

    def changed(self):
        with self.lock:self.progress['revision']+=1

    def start(self,kind='indices',start=None,end=None):
        with self.lock:
            if self.progress['running']:return False
            self.progress.update(running=True,phase='准备同步',done=0,total=0,errors=[])
            self.cancel.clear()
        def work():
            try:
                if kind=='history':self.backfill(start,end)
                else:self.sync_indices()
            except Exception as exc:
                if not self.cancel.is_set():
                    with self.lock:self.progress['errors'].append(str(exc)[:350])
            finally:
                self.update(running=False,phase='已停止' if self.cancel.is_set() else '同步结束')
                self.changed()
        self.thread=threading.Thread(target=work,daemon=True)
        self.thread.start()
        return True

    def sync_indices(self):
        self.update(total=len(INDICES)+1)
        for i,index in enumerate(INDICES):
            if self.cancel.is_set():return
            symbol=index['symbol'];self.update(phase='同步 '+index['name'],done=i)
            with self.store.connect() as db:
                info=db.execute('SELECT * FROM index_info WHERE symbol=?',(symbol,)).fetchone()
            start=(date.fromisoformat(info['last_date'])-timedelta(days=7)).isoformat() if info else '1990-01-01'
            actions=['market_index_em','market_index_sina'] if info else ['market_index_sina','market_index_em']
            errors=[]
            for action in actions:
                if self.cancel.is_set():return
                result=fetch(action,{'symbol':symbol,'start':start},timeout=60,cancel=self.cancel)
                try:
                    if not result['ok']:raise ValueError(result.get('error','数据源无响应'))
                    self.store.save_index(symbol,result['data']);self.changed();break
                except Exception as exc:errors.append(str(exc)[:150])
            else:
                with self.lock:self.progress['errors'].append(index['name']+'：'+'；'.join(errors))
        if self.cancel.is_set():return
        self.update(phase='同步沪深市场 TRIN',done=len(INDICES))
        result=fetch('market_snapshot',{},timeout=180,cancel=self.cancel)
        if not result['ok']:raise ValueError(result.get('error','市场快照失败'))
        self.store.save_snapshot(result['data']);self.changed();self.update(done=len(INDICES)+1)

    def backfill(self,start,end):
        start=date.fromisoformat(start).isoformat();end=date.fromisoformat(end).isoformat()
        if start>end or start<'1990-01-01' or end>now().date().isoformat():raise ValueError('历史回补日期应在 1990 年至今天之间')
        self.update(phase='读取历史上市与退市证券目录')
        universe=fetch('market_universe',{},timeout=60,cancel=self.cancel)
        if not universe['ok'] and not self.cancel.is_set():
            self.update(phase='切换历史证券目录备用源')
            universe=fetch('market_universe_ak',{},timeout=150,cancel=self.cancel)
        if not universe['ok']:raise ValueError(universe.get('error','历史证券目录失败'))
        if len(universe['data'])<3000:raise ValueError('历史证券目录不完整')
        calendar=fetch('market_calendar',{'start':start,'end':end},timeout=60,cancel=self.cancel)
        if not calendar['ok'] and not self.cancel.is_set():calendar=fetch('market_calendar_ak',{'start':start,'end':end},timeout=60,cancel=self.cancel)
        if not calendar['ok']:raise ValueError(calendar.get('error','交易日历失败'))
        with self.store.connect() as db:
            db.execute('DELETE FROM universe')
            db.executemany('INSERT INTO universe VALUES(?,?,?,?)',[(r['code'],r['code_name'],r.get('ipoDate') or '1990-01-01',r.get('outDate') or '') for r in universe['data']])
            db.executemany('INSERT OR REPLACE INTO calendar VALUES(?,?)',[(r['calendar_date'],int(r['is_trading_day'])) for r in calendar['data']])
            eligible=[r['symbol'] for r in rows(db,'SELECT * FROM universe') if r['ipo']<=end and (not r['out_date'] or r['out_date']>=start)]
            missing=[symbol for symbol in eligible if not db.execute('SELECT 1 FROM fetch_ranges WHERE symbol=? AND start<=? AND end>=?',(symbol,start,end)).fetchone()]
        self.update(total=len(eligible),done=len(eligible)-len(missing))
        completed_count=len(eligible)-len(missing)
        for offset in range(0,len(missing),12):
            if self.cancel.is_set():break
            batch=missing[offset:offset+12];self.update(phase=f'回补证券 {offset+1}–{min(offset+12,len(missing))} / {len(missing)}')
            response=fetch('market_breadth_batch',{'symbols':batch,'start':start,'end':end},timeout=120,cancel=self.cancel)
            if not response['ok'] and not self.cancel.is_set():
                partial={'records':[],'completed':[],'errors':[]}
                for code in batch:
                    if self.cancel.is_set():break
                    fallback=fetch('market_breadth_stock_ak',{'symbol':code,'start':start,'end':end},timeout=40,cancel=self.cancel)
                    if fallback['ok']:
                        partial['records'].extend(fallback['data']['records']);partial['completed'].extend(fallback['data']['completed'])
                    else:partial['errors'].append({'symbol':code,'error':fallback.get('error','备用源失败')})
                response={'ok':True,'data':partial}
            if not response['ok']:raise ValueError(response.get('error','历史分批查询失败；已完成批次保留'))
            data=response['data']
            with self.store.connect() as db:
                values=[]
                for r in data['records']:
                    change=finite(r['pctChg']) if r.get('pctChg') else None
                    volume=finite(r['volume']) if r.get('volume') else 0
                    values.append((r['code'],r['date'],change,volume,int(r.get('tradestatus') or 0)))
                db.executemany('INSERT OR REPLACE INTO breadth VALUES(?,?,?,?,?)',values)
                db.executemany('INSERT OR IGNORE INTO fetch_ranges VALUES(?,?,?)',[(code,start,end) for code in data['completed']])
            completed_count+=len(data['completed'])
            self.update(done=completed_count)
            if data['errors']:
                with self.lock:self.progress['errors'].extend(r['symbol']+'：'+r['error'] for r in data['errors'])
        self.update(phase='汇总历史 TRIN')
        self.store.reconstruct(start,end,cancel=self.cancel,progress=lambda day:self.update(phase='汇总历史 TRIN '+day));self.changed()
