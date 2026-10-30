"""网络请求运行在可终止的子进程，避免供应商阻塞桌面账本。"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import multiprocessing as mp
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from decimal import Decimal

from .storage import TZ, now, rows
from .ledger import cents, decimal, dps_for, uid, market_prefix, security_name
from .network import configure_direct_network
from .bao_transport import BaoTransportError, safe_baostock_transport
from .market_network import MarketLimit

def clean_date(value):
    value = str(value)
    if value in {'', 'None', 'nan', 'NaT', '--'}:
        return ''
    try:
        return datetime.fromisoformat(value[:10]).date().isoformat()
    except ValueError:
        return ''

def _worker(pipe, action, payload):
    socket.setdefaulttimeout(12)
    try:
        pipe.send({'progress': '初始化网络组件'})
        configure_direct_network()
        pipe.send({'progress': '查询供应商'})
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                if action.startswith('bao_'):
                    for attempt in range(2):
                        try:
                            with safe_baostock_transport():
                                result = _fetch(action, payload)
                            break
                        except BaoTransportError:
                            if attempt == 1:
                                raise
                            pipe.send({'progress': '重新连接 BaoStock'})
                            time.sleep(0.4)
                else:
                    result = _fetch(action, payload)
            except Exception as primary_error:
                fallbacks = {'ak_quote':['ak_quote_sina','ak_quote_xq'], 'ak_etf_history':['ak_etf_history_sina'], 'ak_etf_dividends':['ak_etf_dividends_sina']}.get(action)
                if not fallbacks:
                    raise
                errors = [f'主源：{str(primary_error)[:100]}']
                for fallback in fallbacks:
                    try:
                        result = _fetch(fallback,payload)
                        break
                    except Exception as exc:
                        errors.append(f'{fallback}：{str(exc)[:100]}')
                else:
                    raise RuntimeError('；'.join(errors)) from primary_error
                pipe.send({'progress':'备用源查询成功'})
        pipe.send({'ok': True, 'data': result})
    except MarketLimit as exc:
        pipe.send({'ok':False,'limited':True,'retry_at':exc.retry_at,'source':exc.source,'error':str(exc)})
    except BaseException as exc:
        pipe.send({'ok': False, 'error': f'{type(exc).__name__}: {str(exc)[:240]}'})
    finally:
        pipe.close()

def fetch(action, payload, timeout=45, cancel=None):
    if getattr(sys, 'frozen', False):
        return _fetch_packaged(action, payload, timeout, cancel)
    ctx = mp.get_context('spawn')
    reader, writer = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_worker, args=(writer,action,payload), daemon=True)
    started = time.monotonic()
    process.start()
    writer.close()
    try:
        stage = '启动数据进程'
        deadline = started + timeout
        while True:
            if cancel is not None and cancel.is_set():
                return {'ok':False,'cancelled':True,'error':'同步已停止'}
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                return {'ok': False, 'error': f'请求超过 {timeout} 秒（{stage}），已停止；不会使用旧数据触发提醒', 'elapsed_seconds': round(time.monotonic()-started,2)}
            if not reader.poll(min(.1,remaining) if cancel is not None else remaining):
                continue
            result = reader.recv()
            if 'progress' in result:
                stage = result['progress']
                continue
            result['elapsed_seconds'] = round(time.monotonic()-started,2)
            return result
    except EOFError:
        return {'ok': False, 'error': '数据进程提前退出'}
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=3)
        reader.close()


class StdioPipe:
    """UTF-8 transport for a packaged worker, independent of spawn's handle setup."""
    def __init__(self):
        self.output = sys.stdout.buffer

    def send(self, value):
        self.output.write((json.dumps(value, ensure_ascii=False, default=str)+'\n').encode('utf-8'))
        self.output.flush()

    def close(self):
        self.output.flush()


def packaged_worker():
    request = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    _worker(StdioPipe(), request['action'], request['payload'])


def _stop_provider(process):
    if process.poll() is not None:return
    if sys.platform=='win32':
        # PyInstaller onefile 有引导进程与工作子进程，停止时一起回收。
        try:
            subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),timeout=5,check=False)
        except (OSError,subprocess.TimeoutExpired):pass
    else:
        import signal
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
    if process.poll() is None:process.kill()


def _fetch_packaged(action, payload, timeout, cancel=None):
    started = time.monotonic()
    process = subprocess.Popen([sys.executable, '--provider-worker'], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),start_new_session=sys.platform!='win32')
    timed_out = cancelled = False
    request=json.dumps({'action':action,'payload':payload}).encode('utf-8')
    while True:
        remaining=timeout-(time.monotonic()-started)
        cancelled=cancel is not None and cancel.is_set()
        if cancelled or remaining<=0:
            timed_out=not cancelled
            _stop_provider(process)
            output,errors=process.communicate(timeout=5)
            break
        try:
            output,errors=process.communicate(request,timeout=min(.1,remaining) if cancel is not None else remaining)
            break
        except subprocess.TimeoutExpired:
            request=None
    stage, result = '启动数据进程', None
    for line in output.decode('utf-8', errors='replace').splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if 'progress' in message:
            stage = message['progress']
        elif 'ok' in message:
            result = message
    if cancelled:
        result={'ok':False,'cancelled':True,'error':'同步已停止'}
    elif timed_out:
        result = {'ok':False,'error':f'请求超过 {timeout} 秒（{stage}），已停止；不会使用旧数据触发提醒'}
    elif result is None:
        result = {'ok':False,'error':'数据进程未返回结果：'+errors.decode('utf-8', errors='replace')[-240:]}
    result['elapsed_seconds'] = round(time.monotonic()-started,2)
    return result

def _fetch(action, payload):
    if action.startswith('market_'):
        from .market_sources import query
        return query(action,payload)
    symbol = payload.get('symbol', '600036')
    code = market_prefix(symbol) + '.' + symbol
    if action.startswith('bao_'):
        import baostock as bs
        login = bs.login()
        if login.error_code != '0':
            raise RuntimeError(login.error_msg)
        def consume(result):
            if result.error_code != '0':
                raise RuntimeError(result.error_msg)
            result_rows = []
            while result.next():
                result_rows.append(dict(zip(result.fields, result.get_row_data())))
            return result_rows
        try:
            if action == 'bao_catalog':
                return consume(bs.query_stock_basic())
            if action == 'bao_history':
                return consume(bs.query_history_k_data_plus(code, 'date,code,close', start_date=payload.get('start', (now().date()-timedelta(days=365)).isoformat()), end_date=now().date().isoformat(), frequency='d', adjustflag='3'))
            if action == 'bao_minute':
                return consume(bs.query_history_k_data_plus(code, 'date,time,code,close', start_date=now().date().isoformat(), end_date=now().date().isoformat(), frequency='5', adjustflag='3'))
            if action == 'bao_dividends':
                result = []
                for year in range(now().year-5, now().year+1):
                    result.extend(consume(bs.query_dividend_data(code, str(year), yearType='operate')))
                return result
            if action == 'bao_calendar':
                return consume(bs.query_trade_dates(start_date=(now().date()-timedelta(days=7)).isoformat(), end_date=(now().date()+timedelta(days=7)).isoformat()))
        finally:
            # A failed logout must not replace a useful query result or its real error.
            if sys.exc_info()[0] is None:
                with contextlib.suppress(Exception):
                    bs.logout()
    import akshare as ak
    if action == 'ak_catalog':
        return ak.stock_info_a_code_name().to_dict('records')
    if action == 'ak_etf_catalog':
        return ak.fund_etf_category_sina(symbol='ETF基金').to_dict('records')
    if action == 'ak_calendar':
        return ak.tool_trade_date_hist_sina().astype(str).to_dict('records')
    if action == 'ak_etf_history':
        frame = ak.fund_etf_hist_em(symbol=symbol, start_date=(now().date()-timedelta(days=365)).strftime('%Y%m%d'), end_date=now().strftime('%Y%m%d'), adjust='')
        if frame.empty:
            raise ValueError('东方财富 ETF 历史行情为空')
        return frame.astype(object).where(frame.notna(),None).to_dict('records')
    if action == 'ak_etf_history_sina':
        frame = ak.fund_etf_hist_sina(symbol=market_prefix(symbol)+symbol)
        cutoff = (now().date()-timedelta(days=365)).isoformat()
        return [{'日期':clean_date(r['date']),'收盘':str(r['close']),'_source':'AKShare / 新浪财经 ETF'} for r in frame.to_dict('records') if clean_date(r['date']) >= cutoff]
    if action == 'ak_etf_dividends':
        frame = ak.fund_open_fund_info_em(symbol=symbol,indicator='分红送配详情')
        try:
            splits = ak.fund_open_fund_info_em(symbol=symbol,indicator='拆分详情')
            has_splits = not splits.empty
        except Exception:
            has_splits = True  # 折算资料缺失时保留已查到的现金分红，暂停多年预测。
        return {'events':frame.astype(object).where(frame.notna(),None).to_dict('records'), 'has_splits':has_splits}
    if action == 'ak_etf_dividends_sina':
        import requests
        from akshare.utils import demjson
        text = requests.get(f'https://finance.sina.com.cn/realstock/company/{market_prefix(symbol)+symbol}/hfq.js',timeout=12).text
        if not text.lstrip().startswith('var ') or '=' not in text:
            raise ValueError('新浪未返回有效的 ETF 分红数据')
        raw = demjson.decode(text.split('=',1)[1].strip().rstrip(';'))
        if not isinstance(raw,dict) or not isinstance(raw.get('data'),list) or not raw['data']:
            raise ValueError('新浪 ETF 分红覆盖范围无法确认')
        cumulative, events = Decimal(0), []
        for r in sorted(raw['data'],key=lambda r:str(r['d'] if isinstance(r,dict) and 'd' in r else r.get('date','') if isinstance(r,dict) else r[0])):
            day = clean_date(r.get('date',r.get('d')) if isinstance(r,dict) else r[0])
            value = decimal(r.get('u') if isinstance(r,dict) else r[3])
            if value < cumulative:
                raise ValueError('ETF 累计分红下降，无法安全换算单次派息')
            delta, cumulative = value-cumulative, value
            if day and day!='1900-01-01' and delta>0:
                events.append({'除息日':day,'每份分红':str(delta)+'元'})
        return {'events':events,'has_splits':True,'source':'新浪财经 ETF / 累计分红差额'}
    if action == 'ak_quote_sina':
        import requests
        response = requests.get('https://hq.sinajs.cn/list='+market_prefix(symbol)+symbol,headers={'Referer':'https://finance.sina.com.cn/'},timeout=12)
        response.encoding = 'gb18030'
        values = response.text.split('="',1)[-1].split('"',1)[0].split(',')
        if len(values)<32:
            raise ValueError('新浪未返回有效报价')
        price = decimal(values[3],positive=True)
        stamp = datetime.fromisoformat(values[30]+'T'+values[31]).replace(tzinfo=TZ)
        return {'symbol':symbol,'name':values[0],'price':str(price),'as_of':stamp.isoformat(),
                'timestamp_verified':stamp<=now()+timedelta(minutes=1),'source':'新浪财经'}
    if action == 'ak_quote_xq':
        frame = ak.stock_individual_spot_xq(symbol=market_prefix(symbol).upper()+symbol,timeout=12)
        values = dict(zip(frame['item'],frame['value']))
        price = decimal(values.get('现价',0),positive=True)
        stamp = datetime.fromisoformat(str(values['时间'])).astimezone(TZ) if values.get('时间') else None
        return {'symbol':symbol,'name':str(values.get('名称') or symbol),'price':str(price),
                'source':'AKShare / 雪球','as_of':stamp.isoformat() if stamp else '',
                'timestamp_verified':bool(stamp and stamp<=now()+timedelta(minutes=1))}
    if action == 'ak_quote':
        # 同一快照读取价格、精度和源时间；ETF 常用三位小数，不能按 A 股固定精度解析。
        import requests
        secid = ('1.' if market_prefix(symbol) == 'sh' else '0.') + symbol
        raw = requests.get('https://push2.eastmoney.com/api/qt/stock/get', params={'secid':secid,'fields':'f43,f58,f59,f86'}, timeout=12).json()['data']
        price = decimal(raw['f43'],positive=True) / (Decimal(10) ** int(raw['f59']))
        stamp = datetime.fromtimestamp(int(raw['f86']),TZ)
        return {'symbol':symbol,'price':str(price),'source':'AKShare / 东方财富','name':raw.get('f58',symbol),
                'as_of':stamp.isoformat(),'timestamp_verified':stamp<=now()+timedelta(minutes=1)}
    if action == 'ak_dividends':
        frame = ak.stock_fhps_detail_em(symbol=symbol)
        return frame.astype(object).where(frame.notna(),None).to_dict('records')
    raise ValueError('不支持的数据请求')

def status(db,key,value):
    db.execute('INSERT INTO sync_status VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(value)))

def save_dividend(db,event):
    # 两个供应商以证券与除息日合并；未知派息日保持为空。
    key = event['symbol'] + ':' + (event['ex_date'] or event.get('period','unknown'))
    eid = hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]
    old = db.execute('SELECT * FROM dividends WHERE id=?',(eid,)).fetchone()
    if old:
        event['pay_date'] = event['pay_date'] or old['pay_date']
        event['record_date'] = event['record_date'] or old['record_date']
        event['report_year'] = event.get('report_year') or old['report_year']
    db.execute('INSERT INTO dividends VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET report_year=excluded.report_year,record_date=excluded.record_date,ex_date=excluded.ex_date,pay_date=excluded.pay_date,cash_ps=excluded.cash_ps,status=excluded.status,source=excluded.source,updated_at=excluded.updated_at',
               (eid,event['symbol'],event.get('report_year'),event['record_date'],event['ex_date'],event['pay_date'],event['cash_ps'],event['status'],event['source'],now().isoformat()))

def apply_result(db,action,symbol,result):
    status(db,action+':'+symbol, ('成功 · ' if result['ok'] else '失败 · ') + now().strftime('%m-%d %H:%M') + ('' if result['ok'] else ' · '+result.get('error','未知错误')))
    if not result['ok']:
        return
    data = result['data']
    if action == 'ak_quote':
        db.execute('INSERT INTO quotes VALUES(?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET price=excluded.price,as_of=excluded.as_of,received_at=excluded.received_at,source=excluded.source,timestamp_verified=excluded.timestamp_verified', (symbol,data['price'],data['as_of'],now().isoformat(),data['source'],int(data['timestamp_verified'])))
        if data.get('name'):
            db.execute('UPDATE securities SET name=? WHERE symbol=?',(data['name'],symbol))
    elif action in {'bao_history', 'ak_etf_history'}:
        source = 'BaoStock' if action == 'bao_history' else 'AKShare / 东方财富 ETF'
        if action == 'ak_etf_history':
            source = data[0].get('_source',source) if data else source
            data = [{'date':clean_date(r.get('日期')), 'close':str(r['收盘'])} for r in data if r.get('收盘') is not None]
        for r in data:
            if r.get('close'):
                db.execute('INSERT OR REPLACE INTO daily_prices VALUES(?,?,?,?)',(symbol,r['date'],r['close'],source))
        if data and not db.execute('SELECT 1 FROM quotes WHERE symbol=?',(symbol,)).fetchone():
            r = data[-1]
            db.execute('INSERT INTO quotes VALUES(?,?,?,?,?,?)',(symbol,r['close'],r['date']+'T15:00:00+08:00',now().isoformat(),source+' / 历史收盘',0))
    elif action == 'bao_calendar':
        for r in data:
            status(db,'trading_day:'+r['calendar_date'],r['is_trading_day'])
    elif action == 'ak_calendar':
        days = {clean_date(r.get('trade_date')) for r in data}
        days.discard('')
        if days:
            end = min(now().date().isoformat(),max(days))
            for offset in range(46):
                day = (now().date()-timedelta(days=offset)).isoformat()
                if day <= end:
                    status(db,'trading_day:'+day,'1' if day in days else '0')
    elif action == 'ak_etf_dividends':
        for r in data['events']:
            ex = clean_date(r.get('除息日'))
            raw = r.get('每10份分红', r.get('每份分红'))
            numbers = re.findall(r'(\d+(?:\.\d+)?)\s*元', str(raw))
            if not ex or not numbers:
                raise ValueError('ETF 分红字段不完整，未更新覆盖范围')
            cash = decimal(numbers[-1],positive=True) / (10 if '每10份分红' in r else 1)
            save_dividend(db,{'symbol':symbol,'report_year':int(ex[:4]),'record_date':clean_date(r.get('权益登记日')),
                             'ex_date':ex,'pay_date':clean_date(r.get('分红发放日')),'cash_ps':str(cash),
                             'status':'implemented','source':data.get('source','AKShare / 天天基金 ETF')})
        # 该接口返回成立以来全部分红。ETF 以除息年度汇总，不冒充上市公司财年分红。
        status(db,'dividend_coverage:'+symbol,1990)
        if data['has_splits']:
            status(db,'corporate_actions:'+symbol,'存在基金份额折算，多年分红尚未统一份额基准')
    elif action == 'bao_dividends':
        for r in data:
            if not r.get('dividCashPsBeforeTax') or decimal(r['dividCashPsBeforeTax']) <= 0:
                continue
            ex = clean_date(r.get('dividOperateDate'))
            if not ex:
                continue
            save_dividend(db,{'symbol':symbol,'record_date':clean_date(r.get('dividRegistDate')),'ex_date':ex,
                            'pay_date':clean_date(r.get('dividPayDate')),'cash_ps':r['dividCashPsBeforeTax'],
                            'status':'implemented','source':'BaoStock'})
    elif action == 'ak_dividends':
        earliest = now().year
        corporate_actions = False
        for r in data:
            report = clean_date(r.get('报告期'))
            if not report:
                continue
            earliest = min(earliest,int(report[:4]))
            if int(report[:4]) >= now().year-5 and decimal(r.get('送转股份-送转总比例') or 0) > 0:
                corporate_actions = True
            raw = r.get('现金分红-现金分红比例')
            if raw is None or decimal(raw) <= 0:
                continue
            ex = clean_date(r.get('除权除息日'))
            save_dividend(db,{'symbol':symbol,'report_year':int(report[:4]),'period':report,
                            'record_date':clean_date(r.get('股权登记日')),'ex_date':ex,'pay_date':'',
                            'cash_ps':str(decimal(raw)/10),'status':'implemented' if '实施' in str(r.get('方案进度')) else 'proposed','source':'AKShare / 东方财富'})
        # 首次实际分红年份不能证明此前每年均无分红，覆盖范围仅从可见年份起算。
        status(db,'dividend_coverage:'+symbol,earliest)
        if corporate_actions:
            status(db,'corporate_actions:'+symbol,'历史存在送转股；多年每股股息尚未统一股本基准，均值暂不可用')

def usable_rule_quote(db, quote, moment, opening=False):
    if not quote or not quote['timestamp_verified'] or not quote['as_of']:
        return False
    try:
        stamp = datetime.fromisoformat(quote['as_of'])
        age = (moment - stamp).total_seconds()
    except (ValueError, TypeError):
        return False
    if age < 0:
        return False
    day = moment.date().isoformat()
    trading = db.execute('SELECT value FROM sync_status WHERE key=?', ('trading_day:' + day,)).fetchone()
    hm = moment.strftime('%H:%M')
    intraday = trading and trading[0] == '1' and ('09:30' <= hm <= '11:30' or '13:00' <= hm <= '15:00')
    if intraday:
        return age <= 900
    if not opening:
        return False
    if not trading:
        if moment.weekday() < 5:
            return False
        coverage = db.execute("SELECT MAX(substr(key,13)) FROM sync_status WHERE key LIKE 'trading_day:%' AND substr(key,13)<=?",(day,)).fetchone()[0]
        friday = (moment.date()-timedelta(days=moment.weekday()-4)).isoformat()
        if not coverage or coverage < friday:
            return False
    # 午休只接受当天上午的收盘报价；开盘前/休市时只接受最近完整交易日的收盘报价。
    if trading and trading[0] == '1' and '11:30' < hm < '13:00':
        return stamp.date() == moment.date() and stamp.strftime('%H:%M') >= '11:15'
    if moment.weekday() < 5 and '09:30' <= hm < '15:00' and not trading:
        return False
    cutoff = day if hm > '15:00' or (trading and trading[0] == '0') or moment.weekday() >= 5 else (moment.date()-timedelta(days=1)).isoformat()
    latest = db.execute("SELECT substr(key,13) day FROM sync_status WHERE key LIKE 'trading_day:%' AND value='1' AND substr(key,13)<=? ORDER BY key DESC LIMIT 1", (cutoff,)).fetchone()
    return bool(latest and stamp.date().isoformat() == latest[0] and stamp.strftime('%H:%M') >= '14:45' and age <= 40*86400)


def evaluate_rules(db, emit=lambda value: None, opening_ids=None):
    moment = now()
    for r in rows(db,'SELECT * FROM rules WHERE enabled=1'):
        opening = opening_ids is not None and r['id'] in opening_ids
        q = db.execute('SELECT * FROM quotes WHERE symbol=?',(r['symbol'],)).fetchone()
        if not usable_rule_quote(db,q,moment,opening):
            continue
        checks = []
        if r['target_price'] is not None and r['mode'] != 'yield':
            checks.append(decimal(q['price']) <= decimal(r['target_price']))
        if r['target_yield'] is not None and r['mode'] != 'price':
            dps = dps_for(db,r['symbol'],r['basis'])
            if dps is None:
                continue
            checks.append(dps/decimal(q['price'])*100 >= decimal(r['target_yield']))
        hit = bool(checks) and (all(checks) if r['mode']=='all' else any(checks))
        if hit and (opening or not r['triggered']):
            label = {'ttm':'近12个月','last_year':'1年','avg3':'3年平均','avg5':'5年平均'}.get(r['basis'],r['basis'])
            dps = dps_for(db,r['symbol'],r['basis'])
            yield_text = f" · {label}股息率 {dps/decimal(q['price'])*100:.2f}%" if dps is not None else ''
            message = {'event':'notification','title':security_name(db,r['symbol'])+' 达到关注条件','body':f"{r['symbol']} · 参考价 ¥{q['price']}{yield_text}\n报价 {q['as_of'][:19].replace('T',' ')}"}
            db.execute('INSERT INTO notifications VALUES(?,?,?,?,?,0)',(uid(),moment.isoformat(),message['title'],message['body'],r['id']))
            emit(message)
        db.execute('UPDATE rules SET triggered=? WHERE id=?',(int(hit),r['id']))
        if opening:
            opening_ids.discard(r['id'])
