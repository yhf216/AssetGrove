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
from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .ledger import (build_state, cents, confirm_dividend, decimal, delete_account, delete_transaction, iso_date,
                     replay, run_cashflows, save_account, save_transaction, security_name, symbol_code, uid,
                     is_etf, preference, set_preference, security_snapshot)
from .providers import apply_result, evaluate_rules, fetch, status
from .storage import Database, audit, now, rows
from .network import configure_direct_network
from .settings import read_settings, write_settings

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
    def __init__(self,database):
        self.db = database
        self.busy = threading.Lock()
        self.stop = threading.Event()
        self.last_quotes = 0.0
        self.last_full = ''
        self.queue_lock = threading.Lock()
        self.queued_symbols = set()
        self.queued_all = False
        self.queued_full = False

    def sync(self, full=False, symbols=None):
        if not self.busy.acquire(blocking=False):
            with self.queue_lock:
                self.queued_symbols.update(symbols or [])
                self.queued_all |= symbols is None
                self.queued_full |= full
            return False
        def work():
            try:
                with self.db.connect() as db:
                    status(db,'running','1')
                    tracked = symbols if symbols is not None else [r['symbol'] for r in rows(db,"SELECT symbol FROM watchlist UNION SELECT symbol FROM rules WHERE enabled=1 UNION SELECT DISTINCT symbol FROM transactions WHERE symbol<>''")]
                actions = [('bao_calendar',tracked[0] if tracked else '600036')]
                for symbol in tracked:
                    actions.append(('ak_quote',symbol))
                    if full:
                        actions.extend((action,symbol) for action in (['ak_etf_history','ak_etf_dividends'] if is_etf(symbol) else ['bao_history','bao_dividends','ak_dividends']))
                for action,symbol in actions:
                    if self.stop.is_set():
                        break
                    result = fetch(action,{'symbol':symbol})
                    if action == 'bao_calendar' and not result['ok']:
                        with self.db.connect() as db:
                            apply_result(db,action,symbol,result)
                        action, result = 'ak_calendar', fetch('ak_calendar', {'symbol':symbol})
                    with self.db.connect() as db:
                        apply_result(db,action,symbol,result)
                        evaluate_rules(db,emit)
                self.last_quotes = time.monotonic()
                if full and symbols is None:
                    self.last_full = now().date().isoformat()
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

    def loop(self):
        while not self.stop.wait(15):
            try:
                with self.db.connect() as db:
                    status(db,'scheduler_last_tick',now().isoformat())
                    run_cashflows(db)
                    build_state(db)  # 派息日生成待确认到账，不自动增加现金。
                    tracked = db.execute('SELECT 1 FROM watchlist UNION SELECT 1 FROM rules WHERE enabled=1 UNION SELECT 1 FROM transactions WHERE symbol<>\'\' LIMIT 1').fetchone()
                    evaluate_rules(db, emit)
                if tracked:
                    moment = now()
                    market = moment.weekday() < 5 and ('09:30'<=moment.strftime('%H:%M')<='11:30' or '13:00'<=moment.strftime('%H:%M')<='15:00')
                    if self.last_full != moment.date().isoformat():
                        self.sync(full=True)
                    elif market and time.monotonic()-self.last_quotes >= 600:
                        self.sync()
            except Exception as exc:
                with self.db.connect() as db:
                    status(db,'scheduler_error',str(exc)[:300])

def create_app(database=None, token=None, background=True):
    database = database or Database()
    token = token or secrets.token_urlsafe(32)
    service = Service(database)

    @asynccontextmanager
    async def lifespan(app):
        if background:
            threading.Thread(target=service.loop,daemon=True).start()
        yield
        service.stop.set()

    app = FastAPI(title='AssetGrove Local API',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.database, app.state.service = database,service
    app.add_middleware(CORSMiddleware,allow_origins=['http://127.0.0.1:1420','http://localhost:1420','http://tauri.localhost','https://tauri.localhost','tauri://localhost'],allow_methods=['GET','POST','PUT','DELETE'],allow_headers=['Authorization','Content-Type'])

    @app.middleware('http')
    async def limit_body(request: Request, call_next):
        if int(request.headers.get('content-length','0')) > 20_000_000:
            return JSONResponse({'detail':'导入文件不得超过 20 MB'},status_code=413)
        return await call_next(request)

    def auth(authorization: str = Header(default='')):
        if not secrets.compare_digest(authorization,'Bearer '+token):
            raise HTTPException(401,'本地连接令牌无效')

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
        if background:
            service.sync(True,[symbol])
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
        if body.get('kind') in {'buy','sell','opening_position'} and background:
            service.sync(True,[symbol_code(body.get('symbol',''))])
        return {'id':tid}

    @app.put('/api/transactions/{tx_id}',dependencies=[Depends(auth)])
    def edit_transaction(tx_id:str,body:dict):
        with database.connect() as db:
            tid = save_transaction(db,body,tx_id)
        if body.get('kind') in {'buy','sell','opening_position'} and background:
            service.sync(True,[symbol_code(body.get('symbol',''))])
        return {'id':tid}

    @app.delete('/api/transactions/{tx_id}',dependencies=[Depends(auth)])
    def remove_transaction(tx_id:str):
        with database.connect() as db:
            old = db.execute('SELECT symbol FROM transactions WHERE id=?',(tx_id,)).fetchone()
            delete_transaction(db,tx_id)
        if old and old['symbol'] and background:
            service.sync(True,[old['symbol']])
        return {'ok':True}

    @app.post('/api/watchlist',dependencies=[Depends(auth)])
    def add_watch(body:dict):
        symbol = symbol_code(body.get('symbol',''))
        with database.connect() as db:
            db.execute('INSERT INTO securities VALUES(?,?) ON CONFLICT(symbol) DO UPDATE SET name=excluded.name',(symbol,security_name(db,symbol,body.get('name',''))))
            db.execute('INSERT OR IGNORE INTO watchlist VALUES(?)',(symbol,))
        if background:
            service.sync(True,[symbol])
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

    @app.post('/api/rules',dependencies=[Depends(auth)])
    def rule(body:dict):
        symbol = symbol_code(body.get('symbol',''))
        price, yield_value = body.get('target_price'),body.get('target_yield')
        price = str(decimal(price,True)) if price not in (None,'') else None
        yield_value = str(decimal(yield_value,True)) if yield_value not in (None,'') else None
        basis,mode = body.get('basis','avg3'),body.get('mode','price')
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
            rid = uid()
            db.execute('INSERT INTO rules VALUES(?,?,?,?,?,?,1,0)',(rid,symbol,price,yield_value,basis,mode))
            evaluate_rules(db, emit)
        if background:
            service.sync(True,[symbol])
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
            return {'format':'assetgrove','version':1,'exported_at':now().isoformat(),'tables':{table:rows(db,'SELECT * FROM '+table) for table in BACKUP_TABLES}}

    @app.post('/api/import',dependencies=[Depends(auth)])
    def import_backup(body:dict):
        if body.get('format')!='assetgrove' or body.get('version')!=1 or set(body.get('tables',{}))!=set(BACKUP_TABLES):
            raise ValueError('不是受支持的 AssetGrove v1 备份文件')
        import sqlite3
        try:
            with database.connect() as db:
                before = {table:rows(db,'SELECT * FROM '+table) for table in BACKUP_TABLES}
                for table in reversed(BACKUP_TABLES):
                    db.execute('DELETE FROM '+table)
                for table in BACKUP_TABLES:
                    columns = [r['name'] for r in db.execute('PRAGMA table_info('+table+')')]
                    for record in body['tables'][table]:
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
                    if t['quantity']<0 or t['fees']<0:
                        raise ValueError('备份含无效交易')
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
    config = uvicorn.Config(create_app(token=token),log_level='warning')
    server = uvicorn.Server(config)
    if args.desktop:
        def watch_desktop():
            # 桌面正常退出发送 shutdown；桌面崩溃或管道关闭时也停止服务。
            for line in sys.stdin:
                if line.strip() == 'shutdown':
                    break
            server.should_exit = True
        threading.Thread(target=watch_desktop,daemon=True).start()
    server.run(sockets=[sock])

if __name__ == '__main__':
    main()
