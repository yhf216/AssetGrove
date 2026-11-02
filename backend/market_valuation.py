"""Historical PE channels; descriptive statistics, never backtested signals."""
from bisect import bisect_right
from datetime import date
import math
from .market_reference import INDICES,REPOSITORY

SCHEMA='''CREATE TABLE IF NOT EXISTS valuation_bars(symbol TEXT,date TEXT,close REAL,pe REAL,source TEXT,PRIMARY KEY(symbol,date));'''


def install(db):
    db.executescript(SCHEMA)
    for operation in ['INSERT','UPDATE','DELETE']:
        db.execute(f'CREATE TRIGGER IF NOT EXISTS valuation_revision_{operation.lower()} AFTER {operation} ON valuation_bars BEGIN UPDATE market_revision SET value=value+1 WHERE id=1; END')


def save(db,symbol,records):
    if symbol not in INDICES:raise ValueError('无效估值指数')
    db.executemany('''INSERT INTO valuation_bars VALUES(?,?,?,?,?) ON CONFLICT(symbol,date) DO UPDATE SET close=excluded.close,pe=excluded.pe,source=excluded.source
        WHERE valuation_bars.close IS NOT excluded.close OR valuation_bars.pe IS NOT excluded.pe OR valuation_bars.source IS NOT excluded.source''',
        [(symbol,r['date'],r['close'],r['pe'],r['source']) for r in records])


def sample(values,period):
    if period=='d':return values
    groups={}
    for row in values:
        day=date.fromisoformat(row['date'])
        key=day.isocalendar()[:2] if period=='w' else row['date'][:7]
        groups[key]=row
    return list(groups.values())


def percentile(values,value):return bisect_right(values,value)/len(values)*100 if values else None


def describe(values):
    numbers=sorted(values);size=len(numbers)
    if not size:return None
    mean=sum(numbers)/size
    std=math.sqrt(sum((v-mean)**2 for v in numbers)/(size-1)) if size>1 else 0
    def quantile(q):
        position=(size-1)*q;lo=int(position);hi=min(size-1,lo+1)
        return numbers[lo]+(numbers[hi]-numbers[lo])*(position-lo)
    return {'count':size,'mean':mean,'std':std,'lower2':max(0,mean-2*std),'lower1':max(0,mean-std),'upper1':mean+std,'upper2':mean+2*std,
            'p10':quantile(.1),'p25':quantile(.25),'p50':quantile(.5),'p75':quantile(.75),'p90':quantile(.9)}


def history(db,prefs):
    symbol=prefs.get('valuation_symbol','sh000300')
    values=[dict(r) for r in db.execute('SELECT date,close,pe,source FROM valuation_bars WHERE symbol=? ORDER BY date',(symbol,))]
    years=prefs.get('valuation_years',10)
    if values and years:
        end=date.fromisoformat(values[-1]['date'])
        try:first=end.replace(year=end.year-years).isoformat()
        except ValueError:first=end.replace(year=end.year-years,day=28).isoformat()
        values=[r for r in values if r['date']>=first]
    stats=describe([r['pe'] for r in values])
    if stats:stats.update(latest=values[-1]['pe'],percentile=percentile(sorted(r['pe'] for r in values),values[-1]['pe']))
    coverage=None
    all_bounds=db.execute('SELECT MIN(date),MAX(date),COUNT(*) FROM valuation_bars WHERE symbol=?',(symbol,)).fetchone()
    if all_bounds[0]:
        first,last=date.fromisoformat(all_bounds[0]),date.fromisoformat(all_bounds[1])
        try:enough=first.replace(year=first.year+10)<=last
        except ValueError:enough=first.replace(year=first.year+10,day=28)<=last
        coverage={'first':all_bounds[0],'last':all_bounds[1],'count':all_bounds[2],'ten_years':enough}
    return {'symbol':symbol,'name':INDICES[symbol][0],'series':sample(values,prefs.get('indicator_period','w')),'statistics':stats,'coverage':coverage,'source_url':REPOSITORY,'period':prefs.get('indicator_period','w'),'years':years}
