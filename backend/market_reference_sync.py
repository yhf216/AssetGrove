"""Update valuation and bond inputs using the market worker and shared budget."""
from datetime import date, timedelta

from .market_erp import save_component
from .market_network import MarketLimit
from .market_reference import CSI_SOURCE, CSI_START, ENIU_SOURCE, INDICES, index_source_url, save_quotes
from .market_valuation import preferred_csi_source, save as save_valuation
from .storage import now


def fetch(service, action, payload):
    try:
        return service.request(action,payload,timeout=120)
    except MarketLimit as exc:
        if not exc.source:raise
        return {'ok':False,'error':str(exc)}


def due(service, stamp, target, today, fresh):
    return service.manual or (not fresh and (stamp.get('checked'),stamp.get('target'))!=(today,target))


def save_stamp(store, key, today, target, url, bounds):
    first,last,count=bounds
    store.setting('reference_'+key,{'checked':today,'target':target,'first':first,'asof':last,'count':count,'url':url},write=True)


def valuation(service,symbol,target,today,repair=False):
    store=service.store
    source=CSI_SOURCE if symbol in CSI_START else ENIU_SOURCE
    stamp=store.setting('reference_'+symbol,default={})
    with store.connect() as db:
        active_first,active_last,active_count=db.execute('SELECT MIN(date),MAX(date),COUNT(*) FROM valuation_bars WHERE symbol=? AND source=?',(symbol,source)).fetchone()
        cached=db.execute('SELECT 1 FROM valuation_bars WHERE symbol=? LIMIT 1',(symbol,)).fetchone()
    incomplete=stamp.get('url')==index_source_url(symbol,source) and active_count<stamp.get('count',0)
    if not due(service,stamp,target,today,bool(active_last and active_last>=target and not incomplete)):return
    start=CSI_START.get(symbol,'1990-01-01')
    if active_first and not (repair or incomplete):
        start=max(start,str(date.fromisoformat(active_last)-timedelta(days=14)))
    service.update(phase='同步 PE-TTM：'+INDICES[symbol][0])
    response=fetch(service,'market_valuation_daily',{'symbol':symbol,'start':start,'end':target})
    url=index_source_url(symbol,source)
    if not response['ok'] and not cached and symbol in CSI_START and not service.cancel.is_set():
        backup=fetch(service,'market_reference_index',{'symbol':symbol})
        if backup['ok']:
            response={'ok':True,'data':backup['data']['records']}
            url=backup['data'].get('url')
    if service.cancel.is_set():return
    if response['ok']:
        records=response['data']
        with store.connect() as db:
            save_valuation(db,symbol,records)
            save_quotes(db,symbol,records,today)
            if symbol=='sh000300':save_component(db,'pe',records,today)
    with store.connect() as db:
        active=preferred_csi_source(db,'valuation_bars','AND symbol=?',(symbol,)) or source
        bounds=db.execute('SELECT MIN(date),MAX(date),COUNT(*) FROM valuation_bars WHERE symbol=? AND source=?',(symbol,active)).fetchone()
    if response['ok']:save_stamp(store,symbol,today,target,url,bounds)
    else:store.setting('reference_'+symbol,{**stamp,'checked':today,'target':target},write=True)
    error=response.get('error','估值暂不可用') if not response['ok'] else (f'估值数据暂更新至 {bounds[1]}' if bounds[1] and bounds[1]<target else '')
    store.setting('valuation_error_'+symbol,error,write=True)


def bond(service,target,today,need_erp):
    store=service.store
    stamp=store.setting('reference_bond',default={})
    end=date.fromisoformat(target)
    with store.connect() as db:
        first,last=db.execute('SELECT MIN(date),MAX(date) FROM erp_bond').fetchone()
        pe_source=preferred_csi_source(db,'erp_pe')
        cursor=stamp.get('repair_before') or target
        gap=db.execute('''SELECT MAX(p.date),MAX(CASE WHEN p.date<=? THEN p.date END) FROM erp_pe p
            LEFT JOIN erp_bond b ON p.date=b.date
            WHERE b.date IS NULL AND p.date<=? AND (? IS NULL OR p.source=?)''',(cursor,target,pe_source,pe_source)).fetchone() if need_erp else (None,None)
    try:history_start=end.replace(year=end.year-10).isoformat()
    except ValueError:history_start=end.replace(year=end.year-10,day=28).isoformat()
    has_history=bool(first and first<=history_start)
    if not due(service,stamp,target,today,bool(last and last>=target and has_history and not gap[0])):return

    service.update(phase='同步中国10年期国债收益率')
    # Fill the latest sessions first, then make one bounded pass into old gaps.
    repair_day=gap[0] if gap[0] and gap[0]>str(end-timedelta(days=30)) else gap[1] or gap[0]
    if last and last>=target and repair_day:
        end=date.fromisoformat(repair_day)
        start=end-timedelta(days=363)
    elif last and last>=target and first and not has_history:
        end=date.fromisoformat(first)-timedelta(days=1)
        start=end-timedelta(days=363)
    else:
        start=max(end-timedelta(days=363),date.fromisoformat(last)-timedelta(days=7)) if last else end-timedelta(days=363)
    params={'start':start.isoformat(),'end':end.isoformat()}
    response=fetch(service,'market_erp_bond',params)
    if not response['ok'] and not service.cancel.is_set():
        response=fetch(service,'market_erp_bond_em',params)
    if service.cancel.is_set():return
    if response['ok']:
        with store.connect() as db:save_component(db,'bond',response['data'],today)
    if not has_history and not store.setting('bond_archive_loaded',default=False) and not service.cancel.is_set():
        backup=fetch(service,'market_reference_bond',{})
        if backup['ok']:
            with store.connect() as db:save_component(db,'bond',backup['data']['records'],today)
            store.setting('bond_archive_loaded',True,write=True)
    if service.cancel.is_set():return
    with store.connect() as db:bounds=db.execute('SELECT MIN(date),MAX(date),COUNT(*) FROM erp_bond').fetchone()
    store.setting('reference_bond',{'checked':today,'target':target,'first':bounds[0],'asof':bounds[1],'count':bounds[2],
        'repair_before':str(start-timedelta(days=1)) if response['ok'] and repair_day and last and last>=target else cursor},write=True)
    error=response.get('error','国债收益率暂不可用') if not response['ok'] else (f'国债收益率暂更新至 {bounds[1]}' if bounds[1] and bounds[1]<target else '')
    store.setting('bond_error',error,write=True)


def sync(service,valuation_only=False,repair=False):
    from .market_jobs import last_closed_day
    if service.cancel.is_set():return
    store=service.store;target=last_closed_day(store);today=now().date().isoformat()
    selected=store.preferences()['valuation_symbol']
    need_erp=not valuation_only and store.setting('erp_requested',default=False)
    need_bond=not valuation_only and (need_erp or store.setting('bond_requested',default=False))
    valuation(service,selected,target,today,repair)
    if need_erp and selected!='sh000300' and not service.cancel.is_set():
        valuation(service,'sh000300',target,today)
    if need_bond and not service.cancel.is_set():bond(service,target,today,need_erp)
    if not service.cancel.is_set():service.scheduler.calculation_wakeup.set()
