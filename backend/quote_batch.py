"""Read named quotes in bounded batches instead of one HTTP call per security."""
from datetime import datetime,timedelta
from decimal import Decimal
import re

from .storage import now,TZ

BATCH_SIZE=500


def read_quotes(symbols):
    import requests
    codes=list(dict.fromkeys(symbols))
    if any(not re.fullmatch(r'(sh|sz)\d{6}',code) for code in codes):raise ValueError('无效的批量行情代码')
    result={}
    for offset in range(0,len(codes),BATCH_SIZE):
        batch=codes[offset:offset+BATCH_SIZE]
        response=requests.get('https://hq.sinajs.cn/list='+','.join(batch),headers={'Referer':'https://finance.sina.com.cn/'},timeout=12)
        response.raise_for_status();response.encoding='gb18030'
        for code,body in re.findall(r'var hq_str_((?:sh|sz)\d{6})="([^"]*)";',response.text):
            if code not in batch:continue
            values=body.split(',')
            if len(values)<32:continue
            try:
                price,previous,volume=[Decimal(values[i]) for i in (3,2,8)]
                if not all(value.is_finite() for value in (price,previous,volume)) or previous<=0 or price<0 or volume<0:continue
                stamp=datetime.fromisoformat(values[30]+'T'+values[31]).replace(tzinfo=TZ)
                result[code]={'symbol':code[2:],'code':code,'name':values[0],'price':str(price),'previous':str(previous),'volume':str(volume),
                              'change':str((price/previous-1)*100) if price>0 else '0','date':stamp.date().isoformat(),'as_of':stamp.isoformat(),
                              'timestamp_verified':stamp<=now()+timedelta(minutes=1),'source':'新浪财经'}
            except (ValueError,ArithmeticError):continue
    return result
