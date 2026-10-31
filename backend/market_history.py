"""Small, resumable history ranges; repair populated dates before empty years."""
from bisect import bisect_left
from datetime import date,timedelta
from .market_cache import HISTORY_SOURCE


def quarter_ranges(first,last):
    current=date.fromisoformat(first);end=date.fromisoformat(last)
    while current<=end:
        month=((current.month-1)//3+1)*3
        next_quarter=date(current.year+1,1,1) if month==12 else date(current.year,month+1,1)
        stop=min(end,next_quarter-timedelta(days=1))
        yield current.isoformat(),stop.isoformat()
        current=stop+timedelta(days=1)


def repair_ranges(first,last):
    """One yearly response repairs many dates without decade-sized queries."""
    current=date.fromisoformat(first);end=date.fromisoformat(last)
    while current<=end:
        stop=min(end,current+timedelta(days=365))
        yield current.isoformat(),stop.isoformat()
        current=stop+timedelta(days=1)


def history_work(store,start,end):
    """Repair almost-complete history before wholly missing recent sessions."""
    with store.connect() as db:
        days=[r[0] for r in db.execute('SELECT date FROM calendar WHERE trading=1 AND date BETWEEN ? AND ? ORDER BY date',(start,end))]
        universe=list(db.execute('SELECT ipo,out_date FROM universe'))
        counts=dict(db.execute("SELECT b.date,COUNT(*) FROM breadth b JOIN universe u ON u.symbol=b.symbol WHERE b.date BETWEEN ? AND ? AND b.date>=u.ipo AND (u.out_date='' OR b.date<u.out_date) AND (b.trading=0 OR (b.change IS NOT NULL AND b.volume>=0)) GROUP BY b.date",(start,end)))
        published={r[0] for r in db.execute('SELECT date FROM trin WHERE date BETWEEN ? AND ? AND value IS NOT NULL AND provisional=0 AND source<>?',(start,end,HISTORY_SOURCE))}
    changes=[0]*(len(days)+1)
    for ipo,out in universe:
        changes[bisect_left(days,ipo)]+=1
        changes[bisect_left(days,out) if out else len(days)]-=1
    missing=[];nearly=[];active=0
    for i,day in enumerate(days):
        active+=changes[i]
        if day in published or not active or counts.get(day,0)>=active:continue
        missing.append(day)
        if counts.get(day,0)/active>=.9:nearly.append(day)
    if not missing:return start,end,[],False
    first,last=(nearly[0],nearly[-1]) if nearly else (missing[0],missing[-1])
    return first,last,[day for day in missing if first<=day<=last],not nearly


def repair_priority(store,start,end):
    with store.connect() as db:
        days=[r[0] for r in db.execute('SELECT date FROM calendar WHERE trading=1 AND date BETWEEN ? AND ? ORDER BY date',(start,end))]
        universe=list(db.execute('SELECT ipo,out_date FROM universe'))
        counts=dict(db.execute("SELECT b.date,COUNT(*) FROM breadth b JOIN universe u ON u.symbol=b.symbol WHERE b.date BETWEEN ? AND ? AND b.date>=u.ipo AND (u.out_date='' OR b.date<u.out_date) AND (b.trading=0 OR (b.change IS NOT NULL AND b.volume>=0)) GROUP BY b.date",(start,end)))
    changes=[0]*(len(days)+1)
    for ipo,out in universe:
        changes[bisect_left(days,ipo)]+=1
        changes[bisect_left(days,out) if out else len(days)]-=1
    coverage=[];active=0
    for i,day in enumerate(days):
        active+=changes[i]
        coverage.append(min(1,counts.get(day,0)/active) if active else 0)
    prefix=[0]
    for value in coverage:prefix.append(prefix[-1]+value)
    def score(item):
        first,last=item[0];a=bisect_left(days,first);b=bisect_left(days,last+'~')
        return (round((prefix[b]-prefix[a])/max(1,b-a),6),last,first)
    return score
