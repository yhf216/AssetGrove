"""Public market data queries, executed in isolated provider workers."""
import re
import time
from datetime import timedelta
from decimal import Decimal
from contextlib import contextmanager

from .storage import now
from .bao_transport import BaoTransportError, safe_baostock_transport


def _records(frame):
    return frame.astype(object).where(frame.notna(),None).to_dict('records')


@contextmanager
def paced_http():
    """Throttle paginated SDK requests inside this disposable market worker only."""
    import requests
    original=requests.sessions.Session.request
    last=0.0
    def request(session,*args,**kwargs):
        nonlocal last
        time.sleep(max(0,1.0-(time.monotonic()-last)))
        last=time.monotonic()
        return original(session,*args,**kwargs)
    requests.sessions.Session.request=request
    try:yield
    finally:requests.sessions.Session.request=original


def query(action,payload):
    with paced_http():return _query(action,payload)


def _query(action, payload):
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
        import akshare as ak
        errors=[]
        for fetch,source,multiplier in [(ak.stock_zh_a_spot_em,'AKShare / 东方财富',100),(ak.stock_zh_a_spot,'AKShare / 新浪财经',1)]:
            try:
                frame=fetch()
                records=[]
                for r in _records(frame):
                    code=str(r['代码']).lower().replace('sh','').replace('sz','')
                    if not re.fullmatch(r'(60|68|00|30)\d{4}',code):
                        continue
                    change=r.get('涨跌幅')
                    price=r.get('最新价')
                    volume=r.get('成交量')
                    valid = price is not None and volume is not None and change is not None and float(price)>0
                    records.append({'symbol':code,'change':str(change) if valid else None,'volume':str(Decimal(str(volume))*multiplier) if valid else None,'time':str(r.get('时间戳',''))})
                if len(records)<3000:
                    raise ValueError('全市场有效行数不足，未发布 TRIN')
                # 全市场快照没有日期字段；读取沪深锚点源日期，避免周末把昨日行情记作今日。
                import requests
                response=requests.get('https://hq.sinajs.cn/list=sh000001,sz399001',headers={'Referer':'https://finance.sina.com.cn/'},timeout=12)
                response.encoding='gb18030'
                dates=[]
                for line in response.text.splitlines():
                    values=line.split('="',1)[-1].split('"',1)[0].split(',')
                    if len(values)>=32:dates.append(values[30])
                if len(dates)!=2 or len(set(dates))!=1 or dates[0]>now().date().isoformat():
                    raise ValueError('无法核实沪深快照源日期')
                return {'source':source,'date':dates[0],'received_at':now().isoformat(),'provisional':dates[0]==now().date().isoformat() and now().strftime('%H:%M')<'15:10','stocks':records}
            except Exception as exc:
                errors.append(str(exc)[:150])
        raise ValueError('；'.join(errors))
    if action in {'market_universe','market_breadth_batch','market_calendar'}:
        import baostock as bs
        def consume(result):
            if result.error_code!='0':
                raise ValueError(result.error_msg)
            values=[]
            while result.next():
                values.append(dict(zip(result.fields,result.get_row_data())))
            if result.error_code!='0':raise ValueError(result.error_msg or '历史分页查询失败')
            return values
        with safe_baostock_transport():
            login=bs.login()
            if login.error_code!='0':
                raise ValueError(login.error_msg)
            try:
                if action=='market_universe':
                    return [r for r in consume(bs.query_stock_basic()) if r.get('type')=='1' and re.fullmatch(r'(sh\.(60|68)|sz\.(00|30))\d{4}',r['code'])]
                if action=='market_calendar':
                    return consume(bs.query_trade_dates(start_date=payload['start'],end_date=payload['end']))
                records, completed, errors = [], [], []
                for index,code in enumerate(payload['symbols']):
                    if index:time.sleep(max(0,float(payload.get('request_interval',1.0))))
                    try:
                        values=consume(bs.query_history_k_data_plus(code,'date,code,close,preclose,volume,pctChg,tradestatus',start_date=payload['start'],end_date=payload['end'],frequency='d',adjustflag='3'))
                        if not values:raise ValueError('历史响应为空，保留缺口等待重试')
                        records.extend(values);completed.append(code)
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
