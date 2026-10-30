from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import date,timedelta
from decimal import Decimal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .ledger import (build_state, cents, confirm_dividend, decimal, delete_account, delete_transaction, iso_date,
                     replay, run_cashflows, save_account, save_transaction, security_name, symbol_code, uid,
                     is_etf, preference, set_preference, security_snapshot)
from .providers import apply_result, evaluate_rules, fetch, status
from .storage import Database, audit, now, rows
from .network import configure_direct_network
from .settings import read_settings, write_settings, app_directory
from .market import MarketStore, MarketService
from .market_network import MarketBudget,MarketLimit
from .sync_schedule import automatic_slot
from .quote_batch import BATCH_SIZE

BACKUP_TABLES = ['meta','accounts','securities','transactions','watchlist','quotes','daily_prices','dividends','entitlements','rules','notifications','cashflows','flow_runs']

def emit(message):
    text = json.dumps(message,ensure_ascii=False) + '\n'
    binary = getattr(sys.stdout,'buffer',None)
    if binary is not None:
        binary.write(text.encode('utf-8'))
        binary.flush()
    else:
        sys.stdout.write(text)
        sys.stdout.flush()

class Service:
    def __init__(self,database,network_path=None):
        self.db = database
        self.network_path=str(network_path or database.path.parent/'market.sqlite3')
        self.network=MarketBudget(self.network_path,'daily')
        self.busy = threading.Lock()
        self.stop = threading.Event()
        self.last_quotes = 0.0
        self.last_full = ''
        self.queue_lock = threading.Lock()
        self.queued_symbols = set()
        self.queued_all = False
        self.queued_full = False
        self.opening_rules = set()

    def opened(self):
        with self.db.connect() as db:
            self.opening_rules = {r['id'] for r in rows(db,'SELECT id FROM rules WHERE enabled=1')}
            evaluate_rules(db,emit,self.opening_rules)

    def sync(self, full=False, symbols=None, manual=True, automatic=None):
        if not self.busy.acquire(blocking=False):
            if not manual:return False
            with self.queue_lock:
                self.queued_symbols.update(symbols or [])
                self.queued_all |= symbols is None
                self.queued_full |= full
            return False
        if automatic:
            with self.db.connect() as db:status(db,'automatic_slot',automatic)
        def work():
            try:
                with self.db.connect() as db:
                    status(db,'running','1')
                    tracked = symbols if symbols is not None else [r['symbol'] for r in rows(db,"SELECT symbol FROM watchlist UNION SELECT symbol FROM rules WHERE enabled=1 UNION SELECT DISTINCT symbol FROM transactions WHERE symbol<>''")]
                    today=now().date().isoformat()
                    cached={r['key']:r['value'] for r in rows(db,"SELECT key,value FROM sync_status WHERE key LIKE 'cache:%'")}
                    latest={r['symbol']:r['day'] for r in rows(db,'SELECT symbol,MAX(date) day FROM daily_prices GROUP BY symbol')}
                    price_dates={}
                    for record in rows(db,'SELECT symbol,date FROM daily_prices'):price_dates.setdefault(record['symbol'],set()).add(record['date'])
                    calendar={r['key'][12:]:int(r['value']) for r in rows(db,"SELECT key,value FROM sync_status WHERE key LIKE 'trading_day:%'")}
                def request(action,payload,timeout=90):
                    response=fetch(action,{**payload,'_market_cache':self.network_path,'_market_manual':manual},timeout=timeout,cancel=self.stop)
                    if response.get('limited'):raise MarketLimit(response['error'],response['retry_at'],response.get('source',''))
                    return response
                for offset in range(0,len(tracked),BATCH_SIZE):
                    if self.stop.is_set():return
                    batch=tracked[offset:offset+BATCH_SIZE]
                    result=request('quote_batch',{'symbols':[('sh' if s.startswith(('5','6')) else 'sz')+s for s in batch]})
                    with self.db.connect() as db:
                        if result['ok']:
                            for quote in result['data']:
                                if Decimal(quote['price'])<=0:continue
                                apply_result(db,'ak_quote',quote['symbol'],{'ok':True,'data':quote})
                                if quote['timestamp_verified'] and quote['as_of'][11:16]>='15:00':
                                    db.execute('INSERT OR REPLACE INTO daily_prices VALUES(?,?,?,?)',(quote['symbol'],quote['as_of'][:10],quote['price'],quote['source']))
                        else:status(db,'quote_batch','失败 · '+result.get('error','批量报价失败'))
                        evaluate_rules(db,emit,self.opening_rules)
                actions=[]
                history_starts={}
                if cached.get('cache:calendar','')<str(now().date()-timedelta(days=5)):
                    actions.append(('bao_calendar',tracked[0] if tracked else '600036'))
                for symbol in tracked if full else []:
                    history='ak_etf_history' if is_etf(symbol) else 'bao_history'
                    last=date.fromisoformat(latest[symbol]) if symbol in latest else None
                    day=last+timedelta(days=1) if last else now().date()-timedelta(days=365)
                    gap=last is None
                    known_holes=[value for value,trading in calendar.items() if trading and value<today and value not in price_dates.get(symbol,set()) and value>=min(price_dates.get(symbol,{today}))]
                    if known_holes:gap=True;history_starts[symbol]=min(known_holes)
                    while not gap and day<now().date():
                        gap=calendar.get(day.isoformat(),int(day.weekday()<5))==1
                        day+=timedelta(days=1)
                    if gap and cached.get('cache:'+history+':'+symbol)!=today:actions.append((history,symbol))
                    dividend='ak_etf_dividends' if is_etf(symbol) else 'ak_dividends'
                    if cached.get('cache:'+dividend+':'+symbol)!=today:actions.append((dividend,symbol))
                for action,symbol in actions:
                    if self.stop.is_set():
                        break
                    payload={'symbol':symbol}
                    if action in {'bao_history','ak_etf_history'} and latest.get(symbol):payload['start']=history_starts.get(symbol,str(date.fromisoformat(latest[symbol])+timedelta(days=1)))
                    result = request(action,payload,timeout=180)
                    if action == 'bao_calendar' and not result['ok']:
                        with self.db.connect() as db:
                            apply_result(db,action,symbol,result)
                        action, result = 'ak_calendar', request('ak_calendar', {'symbol':symbol})
                    with self.db.connect() as db:
                        apply_result(db,action,symbol,result)
                        if result['ok']:status(db,'cache:calendar' if action in {'bao_calendar','ak_calendar'} else 'cache:'+action+':'+symbol,today)
                        evaluate_rules(db,emit,self.opening_rules)
                self.last_quotes = time.monotonic()
                if full and symbols is None:
                    self.last_full = now().date().isoformat()
            except MarketLimit as exc:
                with self.db.connect() as db:status(db,'error',str(exc))
            except Exception as exc:
                with self.db.connect() as db:
                    status(db,'error',str(exc)[:300])
            finally:
                with self.db.connect() as db:
                    status(db,'running','0')
                    status(db,'last_attempt',now().isoformat())
                self.busy.release()
                with self.queue_lock:
                    queued_symbols, queued_all, queued_full = list(self.queued_symbols), self.queued_all, self.queued_full
                    self.queued_symbols.clear()
                    self.queued_all = self.queued_full = False
                if not self.stop.is_set() and (queued_symbols or queued_all):
                    self.sync(queued_full, None if queued_all else queued_symbols)
        threading.Thread(target=work,daemon=True).start()
        return True

    def tick(self,moment=None):
        moment=moment or now()
        with self.db.connect() as db:
            status(db,'scheduler_last_tick',moment.isoformat())
            run_cashflows(db);build_state(db)
            tracked=db.execute("SELECT 1 FROM watchlist UNION SELECT 1 FROM rules WHERE enabled=1 UNION SELECT 1 FROM transactions WHERE symbol<>'' LIMIT 1").fetchone()
            evaluate_rules(db,emit,self.opening_rules)
            trading=db.execute('SELECT value FROM sync_status WHERE key=?',('trading_day:'+moment.date().isoformat(),)).fetchone()
            previous=db.execute("SELECT value FROM sync_status WHERE key='automatic_slot'").fetchone()
        slot=automatic_slot(moment,int(trading[0]) if trading else None)
        if tracked and slot and (not previous or previous[0]!=slot):self.sync(full=True,manual=False,automatic=slot)

    def loop(self):
        while not self.stop.wait(15):
            try:self.tick()
            except Exception as exc:
                with self.db.connect() as db:
                    status(db,'scheduler_error',str(exc)[:300])

def create_app(database=None, token=None, background=True):
    injected_database = database is not None
    database = database or Database()
    token = token or secrets.token_urlsafe(32)
    market_base = database.path.parent if injected_database else Path(os.getenv('ASSETGROVE_DATA_DIR') or app_directory()/'data')
    market = MarketService(MarketStore(market_base/'market.sqlite3'))
    service = Service(database,market.store.path)

    @asynccontextmanager
    async def lifespan(app):
        if background:
            threading.Thread(target=service.loop,daemon=True).start()
            market.scheduler.start()
        yield
        service.stop.set()
        market.shutdown()

    app = FastAPI(title='AssetGrove Local API',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.database, app.state.service = database,service
    app.state.market = market
    app.add_middleware(CORSMiddleware,allow_origins=['http://127.0.0.1:1420','http://localhost:1420','http://tauri.localhost','https://tauri.localhost','tauri://localhost'],allow_methods=['GET','POST','PUT','DELETE'],allow_headers=['Authorization','Content-Type'])

    @app.middleware('http')
    async def limit_body(request: Request, call_next):
        if int(request.headers.get('content-length','0')) > 20_000_000:
            return JSONResponse({'detail':'导入文件不得超过 20 MB'},status_code=413)
        return await call_next(request)

    def auth(authorization: str = Header(default='')):
        if not secrets.compare_digest(authorization,'Bearer '+token):
            raise HTTPException(401,'本地连接令牌无效')

    @app.get('/api/market/state',dependencies=[Depends(auth)])
    def market_state():
        return market.state()

    @app.get('/api/market/history',dependencies=[Depends(auth)])
    def market_history():
        return market.store.history()

    @app.post('/api/market/preferences',dependencies=[Depends(auth)])
    def market_preferences(body:dict):
        return market.store.preferences(body)

    @app.post('/api/market/sync',dependencies=[Depends(auth)])
    def market_sync(body:dict):
        kind=body.get('kind','indices')
        if kind not in {'indices','history'}:raise ValueError('无效同步任务')
        if kind=='history':
            start=iso_date(body.get('start'));end=iso_date(body.get('end'))
            from .market_jobs import last_closed_day
            end=min(end,last_closed_day(market.store))
            if start>end:raise ValueError('开始日期不能晚于结束日期')
        else:start=end=None
        return {'started':market.start(kind,start,end)}

    @app.post('/api/market/cancel',dependencies=[Depends(auth)])
    def market_cancel():
        market.pause()
        return {'ok':True}

    @app.post('/api/market/background',dependencies=[Depends(auth)])
    def market_background(body:dict):
        market.background(body.get('enabled'))
        return {'ok':True}

    @app.exception_handler(ValueError)
    async def invalid(request,exc):
        return JSONResponse({'detail':str(exc)},status_code=400)

    @app.exception_handler(OSError)
    async def file_error(request,exc):
        return JSONResponse({'detail':'无法访问数据文件夹，请检查路径、写入权限和磁盘空间'},status_code=400)

    @app.get('/api/state',dependencies=[Depends(auth)])
    def state():
        with database.connect() as db:
            result = build_state(db)
            result['data_path'] = str(database.path)
            result['network_mode'] = 'direct'
            settings = read_settings()
            result['settings'] = {'font_size':settings.get('font_size',16), 'theme':settings.get('theme','light')}
            return result

    @app.post('/api/settings',dependencies=[Depends(auth)])
    def settings(body:dict):
        with database.lock:
            settings = read_settings()
            if 'font_size' in body:
                value = int(body['font_size'])
                if not 14 <= value <= 24:
                    raise ValueError('字体大小应为 14–24')
                settings['font_size'] = value
            if 'theme' in body:
                if body['theme'] not in {'light','dark','system'}:
                    raise ValueError('无效的外观设置')
                settings['theme'] = body['theme']
            with database.connect() as db:
                for key, allowed in {'account_sort': {'manual','type','amount'}, 'dividend_basis': {'last_year','ttm','avg3','avg5'}}.items():
                    if key in body:
                        if body[key] not in allowed:
                            raise ValueError('无效的排序或股息口径')
                        set_preference(db, key, body[key])
            write_settings(settings)
        return {'ok':True}

    @app.post('/api/data-directory',dependencies=[Depends(auth)])
    def change_data_directory(body:dict):
        if not service.busy.acquire(blocking=False):
            raise ValueError('请等待数据同步完成后再更换文件夹')
        try:
            database.move_to(body.get('path',''))
        finally:
            service.busy.release()
        return {'ok':True,'data_path':str(database.path)}

    @app.post('/api/accounts',dependencies=[Depends(auth)])
    def accounts(body:dict):
        with database.connect() as db:
            return {'id':save_account(db,body)}

    @app.put('/api/accounts/{account_id}',dependencies=[Depends(auth)])
    def edit_account(account_id:str, body:dict):
        with database.connect() as db:
            return {'id':save_account(db,body,account_id)}

    @app.post('/api/accounts/order',dependencies=[Depends(auth)])
    def reorder_accounts(body:dict):
        ids = body.get('ids')
        with database.connect() as db:
            existing = {r['id'] for r in rows(db, 'SELECT id FROM accounts')}
            if not isinstance(ids, list) or not all(isinstance(i,str) for i in ids) or len(ids) != len(existing) or set(ids) != existing:
                raise ValueError('账户列表已变化，请刷新后重新排序')
            set_preference(db,'account_order',json.dumps(ids))
            set_preference(db,'account_sort','manual')
        return {'ok':True}

    @app.post('/api/securities/prepare',dependencies=[Depends(auth)])
    def prepare_security(body:dict):
        symbol = symbol_code(body.get('symbol',''))
        with database.connect() as db:
            db.execute('INSERT INTO securities VALUES(?,?) ON CONFLICT(symbol) DO NOTHING', (symbol, security_name(db,symbol,body.get('name',''))))
            result = security_snapshot(db,symbol)
        return result

    @app.get('/api/security/{symbol}',dependencies=[Depends(auth)])
    def security_details(symbol:str):
        with database.connect() as db:
            return security_snapshot(db, symbol_code(symbol))

    @app.get('/api/securities/search',dependencies=[Depends(auth)])
    def search_securities(q:str=''):
        q = q.strip()[:30]
        with database.connect() as db:
            return rows(db,'''SELECT symbol,MAX(name) name FROM (
                SELECT symbol,name FROM securities UNION ALL SELECT symbol,name FROM security_catalog
                ) WHERE symbol LIKE ? OR name LIKE ? GROUP BY symbol
                ORDER BY CASE WHEN symbol=? OR name=? THEN 0 ELSE 1 END,symbol LIMIT 20''',(q+'%','%'+q+'%',q,q))

    @app.delete('/api/accounts/{account_id}',dependencies=[Depends(auth)])
    def remove_account(account_id:str):
        with database.connect() as db:
            delete_account(db,account_id)
        return {'ok':True}

    @app.post('/api/transactions',dependencies=[Depends(auth)])
    def transactions(body:dict):
        with database.connect() as db:
            tid = save_transaction(db,body)
        return {'id':tid}

    @app.put('/api/transactions/{tx_id}',dependencies=[Depends(auth)])
    def edit_transaction(tx_id:str,body:dict):
        with database.connect() as db:
            tid = save_transaction(db,body,tx_id)
        return {'id':tid}

    @app.delete('/api/transactions/{tx_id}',dependencies=[Depends(auth)])
    def remove_transaction(tx_id:str):
        with database.connect() as db:
            delete_transaction(db,tx_id)
        return {'ok':True}

    @app.post('/api/watchlist',dependencies=[Depends(auth)])
    def add_watch(body:dict):
        symbol = symbol_code(body.get('symbol',''))
        with database.connect() as db:
            db.execute('INSERT INTO securities VALUES(?,?) ON CONFLICT(symbol) DO UPDATE SET name=excluded.name',(symbol,security_name(db,symbol,body.get('name',''))))
            db.execute('INSERT OR IGNORE INTO watchlist VALUES(?)',(symbol,))
        return {'ok':True}

    @app.delete('/api/watchlist/{symbol}',dependencies=[Depends(auth)])
    def remove_watch(symbol:str):
        with database.connect() as db:
            db.execute('DELETE FROM watchlist WHERE symbol=?',(symbol,))
        return {'ok':True}

    @app.post('/api/sync',dependencies=[Depends(auth)])
    def sync():
        return {'started':service.sync(full=True)}

    @app.post('/api/entitlements/{eid}/confirm',dependencies=[Depends(auth)])
    def confirm(eid:str,body:dict):
        with database.connect() as db:
            confirm_dividend(db,eid,body)
        return {'ok':True}

    def save_rule(body:dict, rid=None):
        symbol = symbol_code(body.get('symbol',''))
        price, yield_value = body.get('target_price'),body.get('target_yield')
        price = str(decimal(price,True)) if price not in (None,'') else None
        yield_value = str(decimal(yield_value,True)) if yield_value not in (None,'') else None
        basis,mode = body.get('basis','ttm'),body.get('mode','price')
        if mode not in {'price','yield','all'} or basis not in {'ttm','last_year','avg3','avg5'}:
            raise ValueError('请设置价格或股息率条件，并选择有效口径')
        if (mode in {'price','all'} and price is None) or (mode in {'yield','all'} and yield_value is None):
            raise ValueError('请填写所选条件的目标值')
        if mode == 'price':
            yield_value = None
        elif mode == 'yield':
            price = None
        with database.connect() as db:
            db.execute('INSERT INTO securities VALUES(?,?) ON CONFLICT(symbol) DO NOTHING', (symbol,security_name(db,symbol,body.get('name',''))))
            before = db.execute('SELECT * FROM rules WHERE id=?',(rid,)).fetchone() if rid else None
            if rid and not before:
                raise ValueError('关注条件不存在，请刷新后重试')
            if rid:
                db.execute('UPDATE rules SET symbol=?,target_price=?,target_yield=?,basis=?,mode=?,triggered=0 WHERE id=?',(symbol,price,yield_value,basis,mode,rid))
            else:
                rid = uid()
                db.execute('INSERT INTO rules VALUES(?,?,?,?,?,?,1,0)',(rid,symbol,price,yield_value,basis,mode))
            audit(db,'update' if before else 'create','rule:'+rid,dict(before) if before else None,body)
            evaluate_rules(db, emit, {rid})
        return {'ok':True,'id':rid}

    @app.post('/api/rules',dependencies=[Depends(auth)])
    def rule(body:dict):
        return save_rule(body)

    @app.put('/api/rules/{rid}',dependencies=[Depends(auth)])
    def edit_rule(rid:str,body:dict):
        return save_rule(body,rid)

    @app.post('/api/rules/order',dependencies=[Depends(auth)])
    def order_rules(body:dict):
        ids = body.get('ids')
        with database.connect() as db:
            existing = {r['id'] for r in rows(db,'SELECT id FROM rules')}
            if not isinstance(ids,list) or not all(isinstance(i,str) for i in ids) or len(ids)!=len(existing) or set(ids)!=existing:
                raise ValueError('关注列表已变化，请刷新后重新排序')
            set_preference(db,'rule_order',json.dumps(ids))
        return {'ok':True}

    @app.delete('/api/rules/{rid}',dependencies=[Depends(auth)])
    def remove_rule(rid:str):
        with database.connect() as db:
            db.execute('DELETE FROM rules WHERE id=?',(rid,))
        return {'ok':True}

    @app.post('/api/cashflows',dependencies=[Depends(auth)])
    def cashflow(body:dict):
        amount = cents(body.get('amount',0))
        day = int(body.get('day',1))
        start = iso_date(body.get('start_date',now().date().isoformat()),True)
        if amount <= 0 or not 1<=day<=31:
            raise ValueError('现金流增量须大于零，每月日期须在 1–31 之间')
        if date.fromisoformat(start).year < now().year-1:
            raise ValueError('周期现金流最早从上一年开始，历史记录可逐笔录入')
        with database.connect() as db:
            account = db.execute('SELECT * FROM accounts WHERE id=?',(body.get('account_id'),)).fetchone()
            if not account or account['kind'] not in {'cash','broker'}:
                raise ValueError('请选择现金或证券现金账户')
            db.execute('INSERT INTO cashflows VALUES(?,?,?,?,?,?,1)',(uid(),body['account_id'],amount,day,start,str(body.get('note','现金流增量'))[:200]))
            run_cashflows(db)
        return {'ok':True}

    @app.delete('/api/cashflows/{fid}',dependencies=[Depends(auth)])
    def remove_flow(fid:str):
        with database.connect() as db:
            db.execute('UPDATE cashflows SET enabled=0 WHERE id=?',(fid,))
        return {'ok':True}

    @app.get('/api/history/{symbol}',dependencies=[Depends(auth)])
    def history(symbol:str):
        with database.connect() as db:
            return rows(db,'SELECT date,close FROM daily_prices WHERE symbol=? ORDER BY date',(symbol_code(symbol),))

    @app.get('/api/export',dependencies=[Depends(auth)])
    def export():
        with database.connect() as db:
            return {'format':'assetgrove','version':2,'exported_at':now().isoformat(),'tables':{table:rows(db,'SELECT * FROM '+table) for table in BACKUP_TABLES}}

    @app.post('/api/import',dependencies=[Depends(auth)])
    def import_backup(body:dict):
        if body.get('format')!='assetgrove' or body.get('version') not in {1,2} or set(body.get('tables',{}))!=set(BACKUP_TABLES):
            raise ValueError('不是受支持的 AssetGrove 备份文件')
        import sqlite3
        try:
            with database.connect() as db:
                before = {table:rows(db,'SELECT * FROM '+table) for table in BACKUP_TABLES}
                for table in reversed(BACKUP_TABLES):
                    db.execute('DELETE FROM '+table)
                for table in BACKUP_TABLES:
                    columns = [r['name'] for r in db.execute('PRAGMA table_info('+table+')')]
                    for record in body['tables'][table]:
                        if table=='transactions' and body['version']==1 and 'cash_settlement' not in record:
                            record = {**record,'cash_settlement':0}
                        if set(record)!=set(columns):
                            raise ValueError('备份字段不匹配：'+table)
                        db.execute('INSERT INTO '+table+' VALUES('+','.join('?' for _ in columns)+')',[record[c] for c in columns])
                tracking = db.execute("SELECT value FROM meta WHERE key='tracking_start'").fetchone()
                if not tracking:
                    raise ValueError('备份缺少起始日期')
                iso_date(tracking[0])
                for a in rows(db,'SELECT * FROM accounts'):
                    if a['kind'] not in {'cash','broker','fund','bond','deposit'}:
                        raise ValueError('备份含无效资产类型')
                for t in rows(db,'SELECT * FROM transactions'):
                    iso_date(t['date'])
                    decimal(t['price'])
                    if t['quantity']<0 or t['fees']<0 or t['cash_settlement'] not in (0,1) or (t['cash_settlement'] and t['kind'] not in {'buy','sell'}):
                        raise ValueError('备份含无效交易')
                set_preference(db,'schema_version','3')
                replay(db,validate=True)
                build_state(db)
                audit(db,'restore','backup',before=before,after={'exported_at':body.get('exported_at')})
        except sqlite3.Error as exc:
            raise ValueError('备份数据关联不完整，原数据已保留：'+str(exc))
        service.last_full = ''
        return {'ok':True}

    return app

def main():
    multiprocessing.freeze_support()
    configure_direct_network()
    parser = argparse.ArgumentParser()
    parser.add_argument('--dev',action='store_true')
    parser.add_argument('--desktop',action='store_true')
    args = parser.parse_args()
    token = 'assetgrove-local-development' if args.dev else secrets.token_urlsafe(32)
    sock = socket.socket()
    sock.bind(('127.0.0.1',18765 if args.dev else 0))
    port = sock.getsockname()[1]
    emit({'event':'connection','port':port,'token':token})
    import uvicorn
    app = create_app(token=token)
    config = uvicorn.Config(app,log_level='warning')
    server = uvicorn.Server(config)
    if args.desktop:
        def watch_desktop():
            # 桌面正常退出发送 shutdown；桌面崩溃或管道关闭时也停止服务。
            for line in sys.stdin:
                if line.strip() == 'shutdown':
                    break
                if line.strip() == 'opened':
                    try:
                        app.state.service.opened()
                    except Exception as exc:
                        with app.state.database.connect() as db:
                            status(db,'opening_alert_error',str(exc)[:300])
            server.should_exit = True
        threading.Thread(target=watch_desktop,daemon=True).start()
    server.run(sockets=[sock])

if __name__ == '__main__':
    main()
