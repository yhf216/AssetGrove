"""Read public InvestmentQuant datasets without executing third-party code."""
import csv
from datetime import date
from io import BytesIO,StringIO
import math

BASE='https://ron-wang.github.io/InvestmentQuant/'
REPOSITORY='https://github.com/Ron-Wang/InvestmentQuant'
INDICES={'sh000300':('沪深300','analysis-hs300-erp/000300_CSI_quote_history.csv'),
         'sz399986':('中证银行','analysis-bank-value/399986_CSI_quote_history.csv')}
BOND_PATH='analysis-hs300-erp/国债及其他债券收益率曲线.xlsx'
PE_SOURCE='InvestmentQuant / CSI PE-TTM'
BOND_SOURCE='InvestmentQuant / 中债10年国债'


def bootstrap(store,today):
    """Consume explicitly downloaded local snapshots once, without networking."""
    import hashlib,json
    from .market_erp import save_component
    from .market_valuation import save
    folder=store.path.parent/'reference-seed';manifest=folder/'manifest.json'
    if not manifest.is_file():return
    try:
        if manifest.stat().st_size>16384:raise ValueError('本地参考数据清单过大')
        files=json.loads(manifest.read_text(encoding='utf-8'))
        for key,name in [('sh000300','sh000300.csv'),('sz399986','sz399986.csv'),('bond','bond.xlsx')]:
            entry=files.get(key)
            if not entry or store.setting('reference_seed_'+key)==entry['sha256']:continue
            path=folder/name
            if path.stat().st_size>4*1024*1024:raise ValueError('本地参考数据过大')
            body=path.read_bytes()
            if hashlib.sha256(body).hexdigest()!=entry['sha256']:raise ValueError('本地参考数据校验失败')
            records=parse_bond(body,today) if key=='bond' else parse_index(body,key,today)
            with store.connect() as db:
                if key=='bond':save_component(db,'bond',records,today)
                else:
                    save(db,key,records)
                    if key=='sh000300':save_component(db,'pe',records,today)
            store.setting('reference_'+key,{'checked':today,'asof':max(r['date'] for r in records),'sha256':entry['sha256'],'url':BASE+(BOND_PATH if key=='bond' else INDICES[key][1])},write=True)
            store.setting('reference_seed_'+key,entry['sha256'],write=True)
        store.calculate_pending(limit=10000)
    except Exception as exc:
        store.setting('erp_error','本地参考数据未导入：'+str(exc)[:160],write=True)


def parse_index(data,symbol,today):
    if symbol not in INDICES:raise ValueError('不支持此指数的估值数据')
    reader=csv.DictReader(StringIO(data.decode('utf-8-sig')))
    required={'code','date','close','pe_ttm','OpenPrice','HighPrice','LowPrice'}
    if not required<=set(reader.fieldnames or []):raise ValueError('指数静态数据缺少 PE-TTM 或行情列')
    records={}
    for row in reader:
        try:
            day=date.fromisoformat(row['date']).isoformat()
            if row['code'].zfill(6)!=symbol[2:] or day>today:continue
            pe,o,h,l,c=[float(row[key]) for key in ['pe_ttm','OpenPrice','HighPrice','LowPrice','close']]
            if any(not math.isfinite(v) or v<=0 for v in [pe,o,h,l,c]) or h<max(o,l,c) or l>min(o,h,c):continue
            records[day]={'symbol':symbol,'date':day,'pe':pe,'open':o,'high':h,'low':l,'close':c,'source':PE_SOURCE}
        except (ValueError,TypeError,KeyError):continue
    values=sorted(records.values(),key=lambda r:r['date'])
    if not values:raise ValueError('没有有效的指数估值历史')
    return values


def parse_bond(data,today):
    import pandas as pd
    frame=pd.read_excel(BytesIO(data))
    if not {'曲线名称','日期','10年'}<=set(frame.columns):raise ValueError('国债文件缺少日期或十年期收益率')
    values={}
    for row in frame.to_dict('records'):
        if row.get('曲线名称')!='中债国债收益率曲线':continue
        try:
            day=pd.Timestamp(row['日期']).date().isoformat();value=float(row['10年'])
            if day>today or not math.isfinite(value) or not -10<=value<=30:continue
            values[day]={'date':day,'yield10':value,'source':BOND_SOURCE}
        except (ValueError,TypeError):continue
    if not values:raise ValueError('没有有效的中国十年期国债数据')
    return sorted(values.values(),key=lambda r:r['date'])


def query(kind,payload,today):
    import hashlib
    import requests
    from urllib.parse import quote
    path=INDICES[payload['symbol']][1] if kind=='index' else BOND_PATH
    headers={'User-Agent':'AssetGrove','Accept':'text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*'}
    if payload.get('etag'):headers['If-None-Match']=payload['etag']
    response=requests.get(BASE+quote(path),headers=headers,timeout=30,stream=True)
    try:
        if response.status_code==304:return {'unchanged':True,'etag':response.headers.get('ETag') or payload.get('etag'),'url':BASE+quote(path)}
        response.raise_for_status();body=bytearray()
        for chunk in response.iter_content(65536):
            body.extend(chunk)
            if len(body)>4*1024*1024:raise ValueError('静态数据超过大小上限')
        records=parse_index(bytes(body),payload['symbol'],today) if kind=='index' else parse_bond(bytes(body),today)
        if kind=='index':
            first,last=date.fromisoformat(records[0]['date']),date.fromisoformat(records[-1]['date'])
            try:enough=first.replace(year=first.year+10)<=last
            except ValueError:enough=first.replace(year=first.year+10,day=28)<=last
            if len(records)<500 or not enough:raise ValueError('静态估值历史不足十年，保留已有缓存')
        return {'records':records,'etag':response.headers.get('ETag'),'sha256':hashlib.sha256(body).hexdigest(),'url':BASE+quote(path)}
    finally:response.close()
