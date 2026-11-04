"""Pinned, unadjusted public snapshot. Import missing breadth rows only."""
import csv
import hashlib
import io
import math
from pathlib import Path
import re
import sqlite3
import tarfile
from datetime import date

URL='https://github.com/noblechang/a-share-data/releases/download/FREE_DATA_v0/a-share-raw-v0.tar'
SOURCE='https://github.com/noblechang/a-share-data/releases/tag/FREE_DATA_v0'
SIZE=927546368
SHA256='9e5d9d7386f695f5b255b4d44adb435aec410c5df4276f8db44be111deed0d35'
STOCK=re.compile(r'(sh\.(60|68)|sz\.(00|30))\d{4}')
FILL='''INSERT INTO breadth VALUES(?,?,?,?,?) ON CONFLICT(symbol,date) DO UPDATE SET
    change=excluded.change,volume=excluded.volume,trading=excluded.trading
    WHERE breadth.volume IS NULL OR breadth.volume<0 OR (breadth.trading<>0 AND breadth.change IS NULL)'''


def day(value):
    value=str(value or '')
    if re.fullmatch(r'\d{8}',value):value=value[:4]+'-'+value[4:6]+'-'+value[6:]
    return date.fromisoformat(value).isoformat()


def symbol(value):
    value=str(value or '').strip().lower()
    if re.fullmatch(r'\d{6}\.(sh|sz)',value):value=value[-2:]+'.'+value[:6]
    elif re.fullmatch(r'(sh|sz)\d{6}',value):value=value[:2]+'.'+value[2:]
    elif re.fullmatch(r'\d{6}',value):value=('sh.' if value.startswith(('60','68')) else 'sz.')+value
    return value if STOCK.fullmatch(value) else ''


def breadth_rows(reader,today):
    """Require provider percentage change; never infer it from adjusted closes."""
    for row in reader:
        try:
            code=symbol(row.get('code',row.get('symbol','')))
            if not code:continue
            if str(row.get('adjustflag','3'))!='3':continue
            observed=day(row.get('date',row.get('trade_date')))
            if not '2010-01-04'<=observed<=min(today,'2026-09-24'):continue
            trading=int(row.get('tradestatus',row.get('trade_status','1')))
            change=row.get('pctChg',row.get('pct_chg'))
            change=float(change) if change not in {None,''} else None
            volume=float(row.get('volume') or 0)
            if trading not in {0,1} or not math.isfinite(volume) or volume<0 or (change is not None and not math.isfinite(change)) or (trading and change is None):continue
            yield code,observed,change,volume,trading
        except (ValueError,TypeError):continue


def import_snapshot(path,cache,today):
    """No extraction, no external code, bounded CSV members, existing rows win."""
    with sqlite3.connect(cache,timeout=30) as db:
        allowed={r[0] for r in db.execute('SELECT symbol FROM universe')}
    if len(allowed)<3000:raise ValueError('请先同步沪深证券目录，再导入个股静态历史')
    imported=valid=0
    with tarfile.open(path,mode='r:') as archive,sqlite3.connect(cache,timeout=30) as db:
        for member in archive:
            if not member.isfile() or not member.name.lower().endswith('.csv') or member.size>32*1024*1024:continue
            parts=Path(member.name).parts
            if any('qfq' in p.lower() or 'hfq' in p.lower() or 'factor' in p.lower() for p in parts):continue
            with io.TextIOWrapper(archive.extractfile(member),encoding='utf-8-sig',newline='') as stream:
                reader=csv.DictReader(stream);fields=set(reader.fieldnames or [])
                if {'calendar_date','is_trading_day'}<=fields:
                    values=[]
                    for row in reader:
                        try:
                            observed=day(row['calendar_date']);trading=int(row['is_trading_day'])
                            if '2010-01-04'<=observed<=min(today,'2026-09-24') and trading in {0,1}:values.append((observed,trading))
                        except (ValueError,TypeError):continue
                    db.executemany('INSERT OR IGNORE INTO calendar VALUES(?,?)',values);db.commit()
                elif 'volume' in fields and fields & {'pctChg','pct_chg'} and fields & {'code','symbol'}:
                    batch=[]
                    for row in breadth_rows(reader,today):
                        if row[0] not in allowed:continue
                        batch.append(row)
                        if len(batch)>=10000:
                            imported+=db.executemany(FILL,batch).rowcount
                            valid+=len(batch);db.commit();batch=[]
                    imported+=db.executemany(FILL,batch).rowcount
                    valid+=len(batch);db.commit()
    if not valid:raise ValueError('静态文件没有兼容的未复权涨跌幅与成交量记录，未推算或填造数据')
    return {'valid':valid,'imported':imported,'source':SOURCE,'sha256':SHA256}


def query(payload,today):
    import requests
    cache=Path(payload['_market_cache'])
    folder=cache.parent/'public-archive';folder.mkdir(exist_ok=True)
    path=folder/'a-share-raw-v0.tar'
    def checked():
        if not path.is_file() or path.stat().st_size!=SIZE:return False
        with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()==SHA256
    if not checked():
        part=folder/'a-share-raw-v0.tar.part'
        try:
            with requests.get(URL,headers={'User-Agent':'AssetGrove'},timeout=(20,60),stream=True) as response:
                response.raise_for_status();count=0;digest=hashlib.sha256()
                with part.open('wb') as stream:
                    for chunk in response.iter_content(1024*1024):
                        count+=len(chunk)
                        if count>SIZE:raise ValueError('静态文件超过已核验的大小')
                        digest.update(chunk);stream.write(chunk)
                if count!=SIZE or digest.hexdigest()!=SHA256:raise ValueError('静态文件不完整或 SHA-256 不匹配，未导入')
            part.replace(path)
        except BaseException:
            part.unlink(missing_ok=True);raise
    return import_snapshot(path,cache,today)
