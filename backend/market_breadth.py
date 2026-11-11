"""Net advances from verified shared market summaries, without network access."""
from collections import defaultdict
from datetime import date
import math


def net_advances(records,expected_days=(),period='d'):
    def key(day):
        value=date.fromisoformat(day)
        return value.isocalendar()[:2] if period=='w' else day[:7] if period=='m' else day
    daily=[]
    for row in records:
        up=row['advances'];down=row['declines'];coverage=row['coverage']
        valid=all(isinstance(n,(int,float)) and math.isfinite(n) for n in [up,down,coverage]) and up>=0 and down>=0 and coverage>=1
        daily.append({**row,'value':up-down if valid else None,'reason':'' if valid else '历史样本未齐，未发布数值'})
    if period=='d':return daily
    expected=defaultdict(set);groups=defaultdict(list)
    for day in expected_days:expected[key(day)].add(day)
    for row in daily:groups[key(row['date'])].append(row)
    result=[]
    for bucket,group in sorted(groups.items()):
        observed={r['date'] for r in group if r['value'] is not None}
        complete=expected[bucket]<=observed and all(r['value'] is not None for r in group)
        day=max(expected[bucket] | {r['date'] for r in group})
        result.append({**group[-1],'date':day,'value':sum(r['value'] for r in group)/len(group) if complete else None,
                       'provisional':any(r['provisional'] for r in group),'observations':len(group),
                       'reason':'' if complete else '所选周期的历史日值未齐'})
    return result
