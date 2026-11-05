"""Persistent closing-data checks, independent of old-history job markers."""
from datetime import timedelta
from .storage import now

CLOSE_TIME='15:10'


def targets(store,moment=None,limit=7):
    moment=moment or now()
    end=moment.date() if moment.strftime('%H:%M')>=CLOSE_TIME else moment.date()-timedelta(days=1)
    with store.connect() as db:
        calendar=dict(db.execute('SELECT date,trading FROM calendar WHERE date BETWEEN ? AND ?',(str(end-timedelta(days=30)),str(end))))
    result=[]
    for offset in range(31):
        day=end-timedelta(days=offset);key=str(day)
        if day.weekday()<5 and calendar.get(key,1):result.append(key)
        if len(result)>=limit:break
    return result


def status(store,day=None,moment=None):
    day=day or targets(store,moment,1)[0]
    prefs=store.preferences();need_breadth=store.trin_requested()
    with store.connect() as db:
        calendar=db.execute('SELECT trading FROM calendar WHERE date=?',(day,)).fetchone()
        if calendar and not calendar[0]:return {'date':day,'complete':True,'trading':False,'missing_indices':[],'missing_stocks':0}
        missing=[symbol for symbol in prefs['symbols'] if not db.execute('SELECT 1 FROM index_bars WHERE symbol=? AND date=?',(symbol,day)).fetchone()]
        expected=db.execute("SELECT COUNT(*) FROM universe WHERE ipo<=? AND (out_date='' OR out_date>?)",(day,day)).fetchone()[0] if need_breadth else 0
        observed=db.execute("SELECT COUNT(*) FROM breadth b JOIN universe u ON u.symbol=b.symbol WHERE b.date=? AND u.ipo<=? AND (u.out_date='' OR u.out_date>?) AND (b.trading=0 OR (b.change IS NOT NULL AND b.volume>=0))",(day,day,day)).fetchone()[0] if need_breadth else 0
        derived=db.execute('SELECT coverage,provisional FROM trin WHERE date=?',(day,)).fetchone() if need_breadth else None
    complete=not missing and (not need_breadth or (expected>=3000 and observed==expected and derived and derived[0]>=1 and not derived[1]))
    retry=store.setting('closing_retries',default={}).get(day,{})
    return {'date':day,'complete':bool(complete),'trading':True,'missing_indices':missing,'missing_stocks':max(0,expected-observed),
            'needs_universe':need_breadth and expected<3000,'error':retry.get('error',''),'next_at':retry.get('next_at',0)}


def record(service,day,error='',retry_at=None,progressed=False):
    current=status(service.store,day)
    retries=service.store.setting('closing_retries',default={})
    if current['complete']:
        retries.pop(day,None)
        if day>=service.store.setting('closing_success',default=''):service.store.setting('closing_success',day,write=True)
        if day>=service.store.setting('daily_success',default=''):service.store.setting('daily_success',day,write=True)
    else:
        count=retries.get(day,{}).get('attempts',0)+(0 if progressed else 1)
        wait=5 if progressed else min(600,60*2**min(count-1,4))
        retries[day]={'attempts':count,'next_at':max(now().timestamp()+wait,retry_at or 0),'error':error or '上游数据尚未齐全，将继续补齐'}
    service.store.setting('closing_retries',dict(sorted(retries.items(),reverse=True)[:20]),write=True)
    return status(service.store,day)
