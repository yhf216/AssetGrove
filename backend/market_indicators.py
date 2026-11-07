"""Price-only indicators, calculated from cached daily bars without network I/O."""
from bisect import bisect_right
from collections import deque
import math


def price_indicators(bars, trading_days=()):
    """WVF(22) and SMA50/200 trend; restart windows across known data gaps."""
    expected=sorted(trading_days)
    window=deque();highs=deque();sums={50:0.,200:0.}
    wvf=[];trend=[];previous=None;count=0
    for bar in bars:
        day=bar['date'];close=bar['close'];low=bar['low']
        gap=previous is not None and bisect_right(expected,day)-bisect_right(expected,previous)>1
        valid=all(isinstance(v,(int,float)) and math.isfinite(v) and v>0 for v in [close,low])
        if gap or not valid:
            window.clear();highs.clear();sums={50:0.,200:0.};count=0
        fear=None;bias=None;ma50=None;ma200=None;state=None
        if valid:
            count+=1;window.append(close)
            for n in sums:
                sums[n]+=close
                if len(window)>n:sums[n]-=window[-n-1]
            while highs and highs[-1][1]<=close:highs.pop()
            highs.append((count,close))
            while highs and highs[0][0]<=count-22:highs.popleft()
            if count>=22:fear=(highs[0][1]-low)/highs[0][1]*100
            if count>=50:ma50=sums[50]/50
            if count>=200:
                ma200=sums[200]/200;bias=(close/ma200-1)*100
                above=close>=ma200;golden=ma50>=ma200
                state='震荡阶段' if math.isclose(close,ma200,rel_tol=1e-9) or math.isclose(ma50,ma200,rel_tol=1e-9) else '牛市趋势' if above and golden else '熊市趋势' if not above and not golden else '修复阶段' if above else '回调阶段'
            if len(window)>200:window.popleft()
        wvf.append({'date':day,'value':fear})
        trend.append({'date':day,'value':bias,'ma50':ma50,'ma200':ma200,'close':close if valid else None,'state':state})
        previous=day
    return {'wvf':wvf,'trend':trend}
