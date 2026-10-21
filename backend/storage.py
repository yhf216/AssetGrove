from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from .settings import data_directory, legacy_directory, read_settings, write_settings

TZ = ZoneInfo('Asia/Shanghai')

def now():
    return datetime.now(TZ)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, rate TEXT NOT NULL DEFAULT '0', maturity TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS transactions(id TEXT PRIMARY KEY, kind TEXT NOT NULL, account_id TEXT NOT NULL REFERENCES accounts(id), to_account_id TEXT REFERENCES accounts(id), date TEXT NOT NULL, symbol TEXT NOT NULL DEFAULT '', quantity INTEGER NOT NULL DEFAULT 0, price TEXT NOT NULL DEFAULT '0', fees INTEGER NOT NULL DEFAULT 0, amount INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '', dividend_id TEXT UNIQUE, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS tx_date ON transactions(date, created_at);
CREATE TABLE IF NOT EXISTS securities(symbol TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS watchlist(symbol TEXT PRIMARY KEY REFERENCES securities(symbol));
CREATE TABLE IF NOT EXISTS quotes(symbol TEXT PRIMARY KEY, price TEXT NOT NULL, as_of TEXT NOT NULL, received_at TEXT NOT NULL, source TEXT NOT NULL, timestamp_verified INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS daily_prices(symbol TEXT NOT NULL, date TEXT NOT NULL, close TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY(symbol,date));
CREATE TABLE IF NOT EXISTS dividends(id TEXT PRIMARY KEY, symbol TEXT NOT NULL, report_year INTEGER, record_date TEXT NOT NULL DEFAULT '', ex_date TEXT NOT NULL DEFAULT '', pay_date TEXT NOT NULL DEFAULT '', cash_ps TEXT NOT NULL, status TEXT NOT NULL, source TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entitlements(id TEXT PRIMARY KEY, dividend_id TEXT NOT NULL REFERENCES dividends(id), account_id TEXT NOT NULL REFERENCES accounts(id), quantity INTEGER NOT NULL, expected_amount INTEGER NOT NULL, state TEXT NOT NULL, UNIQUE(dividend_id, account_id));
CREATE TABLE IF NOT EXISTS rules(id TEXT PRIMARY KEY, symbol TEXT NOT NULL, target_price TEXT, target_yield TEXT, basis TEXT NOT NULL, mode TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, triggered INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS notifications(id TEXT PRIMARY KEY, date TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, rule_id TEXT, read INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS cashflows(id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id), amount INTEGER NOT NULL, day INTEGER NOT NULL, start_date TEXT NOT NULL, note TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS flow_runs(flow_id TEXT NOT NULL REFERENCES cashflows(id), date TEXT NOT NULL, transaction_id TEXT, PRIMARY KEY(flow_id,date));
CREATE TABLE IF NOT EXISTS sync_status(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, action TEXT NOT NULL, entity TEXT NOT NULL, before_json TEXT, after_json TEXT);
CREATE TABLE IF NOT EXISTS security_catalog(symbol TEXT PRIMARY KEY, name TEXT NOT NULL);
'''

class Database:
    def __init__(self, path=None):
        self.path = Path(path) if path else data_directory() / 'assetgrove.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        legacy = legacy_directory() / 'assetgrove.sqlite3'
        if (not path and not os.getenv('ASSETGROVE_DATA_DIR') and not self.path.exists()
                and not read_settings().get('data_directory') and legacy.exists() and legacy.resolve() != self.path.resolve()):
            with sqlite3.connect(legacy.as_uri()+'?mode=ro', uri=True) as source, sqlite3.connect(self.path) as target:
                source.backup(target)
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.execute("INSERT INTO meta VALUES('schema_version','2') ON CONFLICT(key) DO UPDATE SET value='2'")
            db.execute("INSERT OR IGNORE INTO meta VALUES('tracking_start',?)", (now().date().isoformat(),))
            for catalog in sorted((Path(__file__).parent/'data').glob('*.json')):
                data = json.loads(catalog.read_text(encoding='utf-8'))
                key = 'catalog_date:' + catalog.stem
                catalog_date = db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
                if not catalog_date or catalog_date[0] != data['as_of']:
                    db.executemany('INSERT INTO security_catalog VALUES(?,?) ON CONFLICT(symbol) DO UPDATE SET name=excluded.name',data['securities'].items())
                    db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,data['as_of']))

    def move_to(self, folder):
        target_dir = Path(str(folder).strip()).expanduser()
        if not target_dir.is_absolute():
            raise ValueError('请选择完整的文件夹路径')
        target_dir = target_dir.resolve()
        target = target_dir / 'assetgrove.sqlite3'
        with self.lock:
            if target == self.path.resolve():
                return
            if target.exists():
                raise ValueError('目标文件夹已有账本，请选择其他文件夹，避免覆盖')
            target_dir.mkdir(parents=True, exist_ok=True)
            try:
                with sqlite3.connect(self.path) as source, sqlite3.connect(target) as destination:
                    source.backup(destination)
                settings = read_settings()
                settings['data_directory'] = str(target_dir)
                write_settings(settings)
            except Exception:
                target.unlink(missing_ok=True)
                raise
            self.path = target

    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=15)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

def rows(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args)]

def audit(db, action, entity, before=None, after=None):
    db.execute('INSERT INTO audit(at,action,entity,before_json,after_json) VALUES(?,?,?,?,?)',
               (now().isoformat(), action, entity, json.dumps(before, ensure_ascii=False) if before is not None else None,
                json.dumps(after, ensure_ascii=False) if after is not None else None))
