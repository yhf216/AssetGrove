"""网络请求运行在可终止的子进程，避免供应商阻塞桌面账本。"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import multiprocessing as mp
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from decimal import Decimal

from .storage import TZ, now, rows
from .ledger import cents, decimal, dps_for, uid
from .network import configure_direct_network
from .bao_transport import BaoTransportError, safe_baostock_transport

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
                if action != 'ak_quote':
                    raise
                try:
                    result = _fetch('ak_quote_xq',payload)
                except Exception as fallback_error:
                    raise RuntimeError(f'东方财富：{str(primary_error)[:110]}；雪球备用：{str(fallback_error)[:110]}') from fallback_error
                result['primary_error'] = str(primary_error)[:160]
        pipe.send({'ok': True, 'data': result})
    except BaseException as exc:
        pipe.send({'ok': False, 'error': f'{type(exc).__name__}: {str(exc)[:240]}'})
    finally:
        pipe.close()

def fetch(action, payload, timeout=45):
    if getattr(sys, 'frozen', False):
        return _fetch_packaged(action, payload, timeout)
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
            remaining = deadline-time.monotonic()
            if remaining <= 0 or not reader.poll(remaining):
                return {'ok': False, 'error': f'请求超过 {timeout} 秒（{stage}），已停止；不会使用旧数据触发提醒', 'elapsed_seconds': round(time.monotonic()-started,2)}
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


def _fetch_packaged(action, payload, timeout):
    started = time.monotonic()
    process = subprocess.Popen([sys.executable, '--provider-worker'], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    timed_out = False
    try:
        output, errors = process.communicate(json.dumps({'action':action,'payload':payload}).encode('utf-8'), timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        output, errors = process.communicate(timeout=5)
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
    if timed_out:
        result = {'ok':False,'error':f'请求超过 {timeout} 秒（{stage}），已停止；不会使用旧数据触发提醒'}
    elif result is None:
        result = {'ok':False,'error':'数据进程未返回结果：'+errors.decode('utf-8', errors='replace')[-240:]}
    result['elapsed_seconds'] = round(time.monotonic()-started,2)
    return result

def _fetch(action, payload):
    symbol = payload.get('symbol', '600036')
    code = ('sh.' if symbol.startswith('6') else 'sz.') + symbol
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
    if action == 'ak_quote_xq':
        frame = ak.stock_individual_spot_xq(symbol=('SH' if symbol.startswith('6') else 'SZ')+symbol,timeout=12)
        values = dict(zip(frame['item'],frame['value']))
        price = decimal(values.get('现价',0),positive=True)
        stamp = datetime.fromisoformat(str(values['时间'])).astimezone(TZ) if values.get('时间') else None
        return {'symbol':symbol,'name':str(values.get('名称') or symbol),'price':str(price),
                'source':'AKShare / 雪球','as_of':stamp.isoformat() if stamp else '',
                'timestamp_verified':bool(stamp and stamp<=now()+timedelta(minutes=1))}
    if action == 'ak_quote':
        frame = ak.stock_bid_ask_em(symbol=symbol)
        values = dict(zip(frame['item'],frame['value']))
        price = decimal(values.get('最新', values.get('最新价', 0)), positive=True)
        result = {'symbol':symbol,'price':str(price),'source':'AKShare / 东方财富','as_of':'','timestamp_verified':False}
        # AKShare 报价表不包含源时间；额外读取同一上游 f86，不能把本机接收时间伪装成报价时间。
        try:
            import requests
            secid = ('1.' if symbol.startswith('6') else '0.') + symbol
            raw = requests.get('https://push2.eastmoney.com/api/qt/stock/get', params={'secid':secid,'fields':'f43,f58,f59,f86'}).json()['data']
            upstream_price = decimal(raw['f43']) / (Decimal(10) ** int(raw['f59']))
            stamp = datetime.fromtimestamp(int(raw['f86']),TZ)
            if upstream_price == price and stamp <= now()+timedelta(minutes=1):
                result.update(as_of=stamp.isoformat(),timestamp_verified=True,name=raw.get('f58',symbol))
        except Exception:
            pass
        return result
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
    elif action == 'bao_history':
        for r in data:
            if r.get('close'):
                db.execute('INSERT OR REPLACE INTO daily_prices VALUES(?,?,?,?)',(symbol,r['date'],r['close'],'BaoStock'))
        if data and not db.execute('SELECT 1 FROM quotes WHERE symbol=?',(symbol,)).fetchone():
            r = data[-1]
            db.execute('INSERT INTO quotes VALUES(?,?,?,?,?,?)',(symbol,r['close'],r['date']+'T15:00:00+08:00',now().isoformat(),'BaoStock / 历史收盘',0))
    elif action == 'bao_calendar':
        for r in data:
            status(db,'trading_day:'+r['calendar_date'],r['is_trading_day'])
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

def evaluate_rules(db, emit=lambda value: None):
    moment = now()
    trading = db.execute('SELECT value FROM sync_status WHERE key=?',('trading_day:'+moment.date().isoformat(),)).fetchone()
    hm = moment.strftime('%H:%M')
    if not trading or trading[0] != '1' or not ('09:30' <= hm <= '11:30' or '13:00' <= hm <= '15:00'):
        return
    for r in rows(db,'SELECT * FROM rules WHERE enabled=1'):
        q = db.execute('SELECT * FROM quotes WHERE symbol=?',(r['symbol'],)).fetchone()
        if not q or not q['timestamp_verified'] or not q['as_of']:
            continue
        age = (moment - datetime.fromisoformat(q['as_of'])).total_seconds()
        if not 0 <= age <= 900:
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
        if hit and not r['triggered']:
            message = {'event':'notification','title':r['symbol']+' 达到关注条件','body':f"最新价 ¥{q['price']} · {r['basis']} · 报价时间 {q['as_of']}"}
            db.execute('INSERT INTO notifications VALUES(?,?,?,?,?,0)',(uid(),moment.isoformat(),message['title'],message['body'],r['id']))
            emit(message)
        db.execute('UPDATE rules SET triggered=? WHERE id=?',(int(hit),r['id']))
