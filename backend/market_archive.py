"""Pinned, unadjusted public snapshot. Import missing breadth rows only."""
import csv
import hashlib
import io
import math
from pathlib import Path
import re
import sqlite3
import tarfile
import tempfile
import shutil
import time
import json
from datetime import date
import duckdb

URL='https://api.github.com/repos/noblechang/a-share-data/releases/assets/588722223'
SOURCE='https://github.com/noblechang/a-share-data/releases/tag/FREE_DATA_v0'
SIZE=927546368
SHA256='9e5d9d7386f695f5b255b4d44adb435aec410c5df4276f8db44be111deed0d35'
DOWNLOAD_SECONDS=900
STOCK=re.compile(r'(sh\.(60|68)|sz\.(00|30))\d{4}')
FILL='''INSERT INTO breadth VALUES(?,?,?,?,?) ON CONFLICT(symbol,date) DO UPDATE SET
    change=excluded.change,volume=excluded.volume,trading=excluded.trading
    WHERE breadth.volume IS NULL OR breadth.volume<0 OR (breadth.trading<>0 AND breadth.change IS NULL)'''


def progress(cache,values):
    with sqlite3.connect(cache,timeout=30) as db:
        db.execute("INSERT INTO meta VALUES('static_history',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps({'source':SOURCE,'size':SIZE,**values},ensure_ascii=False),))


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


def parquet_records(archive,member,engine,folder):
    """Decode one bounded member at a time; never extract archive paths."""
    with tempfile.NamedTemporaryFile(dir=folder,suffix='.parquet',delete=False) as stream:
        local=Path(stream.name)
        with archive.extractfile(member) as source:shutil.copyfileobj(source,stream,65536)
    try:
        result=engine.execute('SELECT * FROM read_parquet(?)',[str(local)])
        fields=[column[0] for column in result.description]
        while values:=result.fetchmany(2048):
            for row in values:yield dict(zip(fields,row))
    finally:
        engine.execute('SELECT 1')
        local.unlink(missing_ok=True)


def import_snapshot(path,cache,today):
    """Decode verified raw Parquet/CSV, preserving valid existing rows."""
    with sqlite3.connect(cache,timeout=30) as db:
        allowed={r[0] for r in db.execute('SELECT symbol FROM universe')}
    if len(allowed)<3000:raise ValueError('请先同步沪深证券目录，再导入个股静态历史')
    imported=valid=files=0;reported=time.monotonic()
    progress(cache,{'phase':'import','downloaded':SIZE,'completed':False,'pending':False,'message':'正在导入本地快照'})
    folder=Path(cache).parent/'public-archive';folder.mkdir(exist_ok=True)
    config={'threads':1,'memory_limit':'256MB','autoinstall_known_extensions':False,'autoload_known_extensions':False}
    with tempfile.TemporaryDirectory(dir=folder,prefix='decode-') as temporary,duckdb.connect(config=config) as engine,tarfile.open(path,mode='r:') as archive,sqlite3.connect(cache,timeout=30) as db:
        for member in archive:
            if not member.isfile() or not member.name.lower().endswith(('.csv','.parquet')) or member.size>32*1024*1024:continue
            parts=Path(member.name).parts
            if any(p.startswith('._') or 'qfq' in p.lower() or 'hfq' in p.lower() or 'factor' in p.lower() for p in parts):continue
            def consume(reader,fields):
                nonlocal imported,valid,files
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
                    valid+=len(batch);db.commit();files+=1
                elif 'bars_daily' in parts:raise ValueError('个股快照字段不兼容：'+Path(member.name).name)
            if member.name.lower().endswith('.parquet'):
                reader=parquet_records(archive,member,engine,temporary)
                try:
                    first=next(reader,None)
                    if first is not None:
                        from itertools import chain
                        consume(chain([first],reader),set(first))
                finally:reader.close()
            else:
                with io.TextIOWrapper(archive.extractfile(member),encoding='utf-8-sig',newline='') as stream:
                    reader=csv.DictReader(stream);consume(reader,set(reader.fieldnames or []))
            if time.monotonic()-reported>=5:
                db.commit();progress(cache,{'phase':'import','downloaded':SIZE,'completed':False,'pending':False,'files':files,'imported':imported,'message':'正在导入本地快照'})
                reported=time.monotonic()
    if not valid:raise ValueError('静态文件没有兼容的未复权涨跌幅与成交量记录，未推算或填造数据')
    return {'valid':valid,'imported':imported,'files':files,'source':SOURCE,'sha256':SHA256,'completed':True,'pending':False,'downloaded':SIZE,'size':SIZE}


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
        if part.exists() and part.stat().st_size>=SIZE:
            if part.stat().st_size==SIZE:
                with part.open('rb') as stream:complete=hashlib.file_digest(stream,'sha256').hexdigest()==SHA256
                if complete:part.replace(path);return import_snapshot(path,cache,today)
            part.unlink()
        offset=part.stat().st_size if part.exists() else 0
        headers={'User-Agent':'AssetGrove','Accept':'application/octet-stream','Accept-Encoding':'identity'}
        if offset:headers['Range']=f'bytes={offset}-'
        began=time.monotonic()
        def pending(message):return {'pending':True,'completed':False,'downloaded':part.stat().st_size if part.exists() else 0,'size':SIZE,'source':SOURCE,'message':message}
        progress(cache,{'phase':'download','pending':True,'completed':False,'downloaded':offset,'message':'正在下载，可断点续传'})
        try:
            with requests.get(URL,headers=headers,timeout=(20,60),stream=True) as response:
                response.raise_for_status()
                if 'application/json' in response.headers.get('Content-Type',''):raise ValueError('静态源没有返回数据文件')
                if offset:
                    expected=f'bytes {offset}-{SIZE-1}/{SIZE}'
                    if response.status_code!=206 or response.headers.get('Content-Range')!=expected:raise ValueError('静态源未返回正确的续传区间，已保留进度')
                elif response.status_code==206 and response.headers.get('Content-Range')!=f'bytes 0-{SIZE-1}/{SIZE}':raise ValueError('静态源返回不完整区间')
                count=offset;reported=time.monotonic()
                with part.open('ab' if offset else 'wb') as stream:
                    for chunk in response.iter_content(65536):
                        count+=len(chunk)
                        if count>SIZE:raise ValueError('静态文件超过已核验的大小')
                        stream.write(chunk)
                        if time.monotonic()-reported>=5:
                            stream.flush();progress(cache,{'phase':'download','pending':True,'completed':False,'downloaded':count,'message':'正在下载，可断点续传'});reported=time.monotonic()
                        if count<SIZE and time.monotonic()-began>=DOWNLOAD_SECONDS:
                            stream.flush();return pending('本轮下载已暂停，下次从当前进度继续')
                if count<SIZE:return pending('下载中断，进度已保留，可继续下载')
                with part.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
                if digest!=SHA256:
                    part.unlink();raise ValueError('静态文件 SHA-256 不匹配，未导入')
            part.replace(path)
        except (requests.ConnectionError,requests.Timeout,requests.exceptions.ChunkedEncodingError):
            return pending('网络连接中断，进度已保留，可继续下载')
    return import_snapshot(path,cache,today)
