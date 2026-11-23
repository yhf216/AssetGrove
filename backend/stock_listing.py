"""Normalize listing dates and exchange-confirmed delistings."""
from datetime import date
import re


def listing_date(value):
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return ''


def valid_stock_code(code):
    return bool(re.fullmatch(r'(sh\.(60|68)|sz\.(00|30))\d{4}',code))


def exchange_delistings(exchange):
    import akshare as ak

    if exchange=='sh':
        # AKShare's label says “暂停上市”, but this endpoint selects COMPANY_STATUS=3
        # and the exchange field is DELIST_DATE.
        frame=ak.stock_info_sh_delist()
        code_key,name_key,end_key='公司代码','公司简称','暂停上市日期'
    elif exchange=='sz':
        frame=ak.stock_info_sz_delist(symbol='终止上市公司')
        code_key,name_key,end_key='证券代码','证券简称','终止上市日期'
    else:
        raise ValueError('无效交易所')
    records=[]
    for row in frame.to_dict('records'):
        code=exchange+'.'+str(row[code_key]).zfill(6)
        end=listing_date(row[end_key])
        if valid_stock_code(code) and end:
            records.append({'code':code,'code_name':str(row[name_key]),'ipoDate':listing_date(row['上市日期']),'outDate':end})
    if not records:raise ValueError('交易所终止上市名单为空')
    return records
