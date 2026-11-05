"""Public market data queries, executed in isolated provider workers."""
import re
import time
from datetime import timedelta
from decimal import Decimal
from contextlib import contextmanager,nullcontext

from .storage import now
from .bao_transport import BaoTransportError, safe_baostock_transport
from .market_network import MarketBudget, MarketLimit
from .source_policy import source_key


def _records(frame):
    return frame.astype(object).where(frame.notna(),None).to_dict('records')


@contextmanager
def paced_http(budget=None):
    """Throttle paginated SDK requests inside this disposable market worker only."""
    import requests
    original=requests.sessions.Session.request
    original_send=requests.sessions.Session.send
    last=0.0
    def request(session,*args,**kwargs):
        nonlocal last
        time.sleep(max(0,1.0-(time.monotonic()-last)))
        last=time.monotonic()
        return original(session,*args,**kwargs)
    def send(session,prepared,**kwargs):
        from urllib.parse import urlsplit
        host=(urlsplit(prepared.url).hostname or '').lower()
        source=source_key(host)
        budget.reserve(source)
        try:
            response=original_send(session,prepared,**kwargs)
        except MarketLimit:raise
        except Exception as exc:
            budget.outcome(source,exc);raise
        wire=next((r for r in getattr(response,'history',[]) if getattr(getattr(r,'request',None),'url',None)==prepared.url),response)
        try:budget.inspect_response(source,wire.status_code,wire.headers)
        except MarketLimit:
            response.close();raise
        return response
    if budget:requests.sessions.Session.send=send
    else:requests.sessions.Session.request=request
    try:yield
    finally:
        requests.sessions.Session.request=original
        requests.sessions.Session.send=original_send


def query(action,payload):
    budget=MarketBudget(payload['_market_cache'],payload.get('_market_scope','history'),payload.get('_market_manual',False)) if payload.get('_market_cache') else None
    with paced_http(budget):return _query(action,payload,budget)


def _query(action, payload, budget=None):
    def checkpoint(values,first,last,allowed=None):
        if not payload.get('_market_cache'):return
        import sqlite3
        from contextlib import closing
        from .market_cache import save_breadth
        # Commit each completed source query before requesting the next one.
        # Cancellation or a killed worker must not lose already downloaded bars.
        with closing(sqlite3.connect(payload['_market_cache'],timeout=15)) as db,db:
            if allowed is None:allowed=[r[0] for r in db.execute('SELECT symbol FROM universe')]
            save_breadth(db,values,allowed,first,last)
    if action in {'market_reference_index','market_reference_bond'}:
        from .market_reference import query as reference_query
        return reference_query('index' if action=='market_reference_index' else 'bond',payload,now().date().isoformat())
    if action=='market_static_history':
        from .market_archive import query as archive_query
        return archive_query(payload,now().date().isoformat())
    if action=='market_erp_pe':
        import akshare as ak
        frame=ak.stock_index_pe_lg(symbol='沪深300')
        return [{'date':str(r['日期'])[:10],'pe':r['滚动市盈率'],'source':'乐咕乐股 / 沪深300整体法滚动市盈率'} for r in _records(frame)]
    if action=='market_erp_bond':
        import akshare as ak
        frame=ak.bond_china_yield(start_date=payload['start'].replace('-',''),end_date=payload['end'].replace('-',''))
        records=[{'date':str(r['日期'])[:10],'yield10':r['10年'],'source':'中债 / 中国10年期国债收益率'} for r in _records(frame) if str(r.get('曲线名称','')) in {'中债国债收益率曲线','国债收益率曲线'}]
        if not records:raise ValueError('未取得明确的中国10年期国债曲线')
        return records
    if action=='market_erp_bond_em':
        import requests
        response=requests.get('https://datacenter.eastmoney.com/api/data/v1/get',params={
            'reportName':'RPTA_WEB_TREASURYYIELD','columns':'SOLAR_DATE,EMM00166466','sortColumns':'SOLAR_DATE','sortTypes':'-1','pageSize':1000,'pageNumber':1,
            'filter':f"(SOLAR_DATE>='{payload['start']}')(SOLAR_DATE<='{payload['end']}')"},timeout=12)
        response.raise_for_status();result=response.json().get('result')
        if not result or int(result.get('pages',1))>1:raise ValueError('国债区间未完整返回，保留缓存等待重试')
        return [{'date':str(r['SOLAR_DATE'])[:10],'yield10':r['EMM00166466'],'source':'东方财富 / 中国10年期国债收益率'} for r in result['data'] if payload['start']<=str(r['SOLAR_DATE'])[:10]<=payload['end']]
    if action=='market_universe_ak':
        import akshare as ak
        result={}
        sources=[(ak.stock_info_sh_name_code(symbol='主板A股'),'证券代码','证券简称','上市日期',None,'sh'),
                 (ak.stock_info_sh_name_code(symbol='科创板'),'证券代码','证券简称','上市日期',None,'sh'),
                 (ak.stock_info_sz_name_code(symbol='A股列表'),'A股代码','A股简称','A股上市日期',None,'sz'),
                 (ak.stock_info_sh_delist(),'公司代码','公司简称','上市日期','暂停上市日期','sh'),
                 (ak.stock_info_sz_delist(),'证券代码','证券简称','上市日期','终止上市日期','sz')]
        for frame,code_key,name_key,ipo_key,out_key,exchange in sources:
            for r in _records(frame):
                code=str(r[code_key]).zfill(6)
                if not re.fullmatch(r'(60|68|00|30)\d{4}',code):continue
                ipo=str(r[ipo_key])[:10]
                if len(ipo)!=10 or ipo in {'None','NaT'}:raise ValueError('上市日期缺失，无法证明历史样本覆盖')
                result[exchange+'.'+code]={'code':exchange+'.'+code,'code_name':str(r[name_key]),'ipoDate':ipo,'outDate':str(r[out_key])[:10] if out_key and r.get(out_key) else ''}
        return list(result.values())
    if action=='market_calendar_ak':
        import akshare as ak
        days=[str(r['trade_date'])[:10] for r in ak.tool_trade_date_hist_sina().to_dict('records')]
        return [{'calendar_date':day,'is_trading_day':'1'} for day in days if payload['start']<=day<=payload['end']]
    if action=='market_breadth_stock_ak':
        import akshare as ak
        code=payload['symbol']
        frame=ak.stock_zh_a_hist(symbol=code.split('.')[-1],start_date=payload['start'].replace('-',''),end_date=payload['end'].replace('-',''),adjust='',timeout=12)
        records=[{'code':code,'date':str(r['日期'])[:10],'pctChg':str(r['涨跌幅']),'volume':str(Decimal(str(r['成交量']))*100),'tradestatus':'1'} for r in _records(frame)]
        return {'records':records,'completed':[code],'errors':[]}
    if action in {'market_index_sina','market_index_em'}:
        import akshare as ak
        symbol = payload['symbol']
        if not re.fullmatch(r'(sh|sz)\d{6}',symbol):
            raise ValueError('无效的指数代码')
        if action == 'market_index_sina':
            frame = ak.stock_zh_index_daily(symbol=symbol)
            source = 'AKShare / 新浪财经'
        else:
            frame = ak.stock_zh_index_daily_em(symbol=symbol,start_date=payload.get('start','19900101').replace('-',''),end_date=payload.get('end',now().date().isoformat()).replace('-',''))
            source = 'AKShare / 东方财富'
        if frame.empty:
            raise ValueError('指数数据为空')
        start = payload.get('start','1990-01-01')
        end = payload.get('end',now().date().isoformat())
        bars = [{**r,'date':str(r['date'])[:10]} for r in _records(frame) if start <= str(r['date'])[:10] <= end]
        return {'source':source,'volume_unit':'手' if action == 'market_index_em' else '股','bars':bars}
    if action == 'market_snapshot':
        from .quote_batch import read_quotes
        symbols=payload.get('symbols',[])
        if len(set(symbols))<3000:raise ValueError('证券目录不完整，保留已缓存的 TRIN')
        if payload.get('target'):
            quotes=read_quotes(['sh000001','sz399001'])
            if any(not quotes.get(code) or quotes[code]['date']!=payload['target'] for code in ['sh000001','sz399001']):raise ValueError('快照尚未提供待补齐日期的数据')
            quotes.update(read_quotes(symbols))
        else:quotes=read_quotes(['sh000001','sz399001',*symbols])
        anchors=[quotes.get(code) for code in ['sh000001','sz399001']]
        if not all(anchors) or anchors[0]['date']!=anchors[1]['date']:raise ValueError('无法核实沪深快照日期')
        day=anchors[0]['date']
        if payload.get('target') and day!=payload['target']:raise ValueError('快照日期与待补齐日期不一致')
        records=[]
        for code in symbols:
            quote=quotes.get(code)
            if not quote or not quote['timestamp_verified'] or quote['date']!=day:raise ValueError('批量快照样本未齐，保留已缓存的 TRIN')
            records.append({'symbol':code,'change':quote['change'],'volume':quote['volume'],'trading':int(Decimal(quote['price'])>0)})
        return {'source':'新浪财经 / 批量行情','date':day,'received_at':now().isoformat(),'provisional':day==now().date().isoformat() and now().strftime('%H:%M')<'15:10','stocks':records}
    if action in {'market_universe','market_breadth_batch','market_breadth_days','market_calendar','market_index_bao'}:
        import baostock as bs
        def consume(result):
            if budget:budget.check_baostock(result)
            if result.error_code!='0':
                raise ValueError(result.error_msg)
            values=[]
            while result.next():
                values.append(dict(zip(result.fields,result.get_row_data())))
            if budget:budget.check_baostock(result)
            if result.error_code!='0':raise ValueError(result.error_msg or '历史分页查询失败')
            return values
        transport_options={'timeout':60} if action=='market_breadth_days' else {}
        transport=safe_baostock_transport(before_request=lambda:budget.reserve('baostock'),outcome=lambda error:budget.outcome('baostock',error),**transport_options) if budget else safe_baostock_transport(**transport_options)
        with (budget.baostock_session() if budget else nullcontext()),transport:
            login=bs.login()
            if budget:budget.check_baostock(login)
            if login.error_code!='0':
                raise ValueError(login.error_msg)
            try:
                if action=='market_index_bao':
                    symbol=payload['symbol']
                    values=consume(bs.query_history_k_data_plus(symbol[:2]+'.'+symbol[2:],'date,code,open,high,low,close,volume',start_date=payload['start'],end_date=payload['end'],frequency='d',adjustflag='3'))
                    if not values:raise ValueError('指数日线尚未发布或响应为空')
                    return {'source':'BaoStock / 指数日线','volume_unit':'股','bars':[{'date':r['date'],**{key:float(r[key]) for key in ['open','high','low','close','volume']}} for r in values]}
                if action=='market_universe':
                    return [r for r in consume(bs.query_stock_basic()) if r.get('type')=='1' and re.fullmatch(r'(sh\.(60|68)|sz\.(00|30))\d{4}',r['code'])]
                if action=='market_calendar':
                    return consume(bs.query_trade_dates(start_date=payload['start'],end_date=payload['end']))
                if action=='market_breadth_days':
                    records,completed,errors=[],[],[]
                    for day in payload['dates']:
                        try:
                            values=consume(bs.query_daily_history_k_AStock(date=day))
                            # Reject wrong-session responses and unrelated markets.
                            values=[r for r in values if r.get('date')==day and re.fullmatch(r'(sh\.(60|68)|sz\.(00|30))\d{4}',r.get('code',''))]
                            if not values:raise ValueError('全市场日线响应为空，保留缺口')
                            checkpoint(values,day,day)
                            records.extend(values);completed.append(day)
                        except MarketLimit as exc:
                            return {'records':records,'completed':completed,'errors':errors,'limited':True,'retry_at':exc.retry_at,'source':exc.source,'error':str(exc)}
                        except Exception as exc:
                            errors.append({'date':day,'error':str(exc)[:180]})
                            break
                    return {'records':records,'completed':completed,'errors':errors}
                records, completed, errors = [], [], []
                for index,code in enumerate(payload['symbols']):
                    if index:time.sleep(max(0,float(payload.get('request_interval',1.0))))
                    try:
                        values=consume(bs.query_history_k_data_plus(code,'date,code,close,preclose,volume,pctChg,tradestatus',start_date=payload['start'],end_date=payload['end'],frequency='d',adjustflag='3'))
                        if not values:raise ValueError('历史响应为空，保留缺口等待重试')
                        checkpoint(values,payload['start'],payload['end'],[code])
                        records.extend(values);completed.append(code)
                    except MarketLimit as exc:
                        return {'records':records,'completed':completed,'errors':errors,'limited':True,'retry_at':exc.retry_at,'source':exc.source,'error':str(exc)}
                    except BaoTransportError as exc:
                        errors.append({'symbol':code,'error':str(exc)[:160]})
                        errors.extend({'symbol':s,'error':'BaoStock 连接已中断，已保留缺口，稍后重新连接'} for s in payload['symbols'][index+1:])
                        break
                    except Exception as exc:
                        errors.append({'symbol':code,'error':str(exc)[:160]})
                return {'records':records,'completed':completed,'errors':errors}
            finally:
                try:bs.logout()
                except Exception:pass
    raise ValueError('未知市场数据请求')
