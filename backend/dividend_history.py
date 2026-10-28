"""Read-only dividend reconstruction; never posts cash or changes confirmations."""
from collections import defaultdict
from datetime import date, timedelta

from .ledger import DIVIDEND_KINDS, cents, decimal, money, replay
from .storage import now, rows


def dividend_receipts(db, transactions):
    actual = [dict(t) for t in transactions if t['kind'] in DIVIDEND_KINDS]
    result = [{**t, 'amount':money(t['amount']), 'inferred':False} for t in actual]
    linked = {t['dividend_id'] for t in actual if t['dividend_id']}
    groups, snapshots = defaultdict(list), {}
    for event in rows(db, "SELECT * FROM dividends WHERE status='implemented' AND record_date<>'' AND pay_date<>'' AND pay_date<=? ORDER BY pay_date,record_date,id", (now().date().isoformat(),)):
        day = event['record_date']
        if day not in snapshots:
            snapshots[day] = replay(db, day)[1]
        for holding in snapshots[day]:
            if holding['symbol'] != event['symbol']:
                continue
            eid = event['id']+':'+holding['account_id']
            if eid in linked:
                continue
            key = (holding['account_id'],event['symbol'],event['pay_date'])
            groups[key].append({'id':'calculated:'+eid,'kind':'dividend_record','account_id':holding['account_id'],
                                'symbol':event['symbol'],'date':event['pay_date'],'record_date':day,
                                'quantity':holding['quantity'],'cash_ps':event['cash_ps'],
                                'amount':money(cents(decimal(event['cash_ps'])*holding['quantity'])),
                                'inferred':True,'source':event['source'],'note':'按股权登记日持股回溯（税前）'})
    manual = [t for t in actual if not t['dividend_id']]
    matched = set()
    for tx in manual:
        key = (tx['account_id'],tx['symbol'],tx['date'])
        if key in groups:
            matched.add(key)
            continue
        candidates = [k for k in groups if k[:2] == key[:2] and 0 <= (date.fromisoformat(tx['date'])-date.fromisoformat(k[2])).days <= 7]
        if len(candidates) == 1:
            matched.add(candidates[0])
    for key, values in groups.items():
        if key not in matched:
            result.extend(values)
    return sorted(result,key=lambda t:(t['date'],t['id']),reverse=True)
