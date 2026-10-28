from __future__ import annotations

import calendar
import json
import re
import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .storage import audit, now, rows

KINDS = {'cash', 'broker', 'fund', 'bond', 'deposit'}
TX_KINDS = {'opening', 'balance', 'income', 'adjustment', 'transfer', 'opening_position', 'buy', 'sell', 'dividend', 'dividend_record'}
DIVIDEND_KINDS = {'dividend', 'dividend_record'}

def uid():
    return uuid.uuid4().hex

def decimal(value, positive=False):
    try:
        value = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('请输入有效数字')
    if not value.is_finite() or abs(value) > Decimal('1000000000000'):
        raise ValueError('金额或价格超出支持范围')
    if positive and value <= 0:
        raise ValueError('数量、价格或金额必须大于零')
    return value

def cents(value):
    return int((decimal(value) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))

def money(value):
    return float(Decimal(value) / 100)

def iso_date(value, allow_future=False):
    try:
        parsed = date.fromisoformat(str(value))
    except (ValueError, TypeError):
        raise ValueError('日期格式应为 YYYY-MM-DD')
    if parsed.year < 1990 or (not allow_future and parsed > now().date()):
        raise ValueError('账本日期必须在 1990 年至今天之间')
    return parsed.isoformat()

def symbol_code(value):
    value = str(value).lower().strip().replace('sh.', '').replace('sz.', '')
    if not re.fullmatch(r'(60\d{4}|68\d{4}|00\d{4}|30\d{4}|5[1268]\d{4}|159\d{3})', value):
        raise ValueError('请输入沪深 A 股或 ETF 六位代码，例如 600036、518880、159915')
    return value


def is_etf(symbol):
    return str(symbol).startswith(('15', '51', '52', '56', '58'))


def market_prefix(symbol):
    return 'sh' if str(symbol).startswith(('5', '6')) else 'sz'


def preference(db, key, default):
    value = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return value[0] if value else default


def set_preference(db, key, value):
    db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))


def security_name(db, symbol, provided=''):
    name = str(provided).strip()[:80]
    if name and name != symbol:
        return name
    result = db.execute('SELECT name FROM security_catalog WHERE symbol=? UNION ALL SELECT name FROM securities WHERE symbol=? LIMIT 1',(symbol,symbol)).fetchone()
    return result[0] if result else symbol

def cash_effect(tx):
    kind = tx['kind']
    if kind in {'buy', 'sell'}:
        if not tx.get('cash_settlement', 0):
            return 0
        gross = cents(decimal(tx['price']) * tx['quantity'])
        return -gross - tx['fees'] if kind == 'buy' else gross - tx['fees']
    if kind in {'opening_position', 'dividend_record'}:
        return 0
    if kind == 'transfer':
        return -tx['amount']
    return tx['amount']

def replay(db, until=None, validate=False, balances=None):
    accounts = {a['id']: {**a, 'balance_cents': 0} for a in rows(db, 'SELECT * FROM accounts')}
    holdings = {}
    transactions = rows(db, 'SELECT * FROM transactions ORDER BY date,created_at,rowid')
    for tx in transactions:
        if until and tx['date'] > until:
            continue
        aid = tx['account_id']
        if tx['kind'] in {'opening','balance'}:
            accounts[aid]['balance_cents'] = tx['amount']
        else:
            accounts[aid]['balance_cents'] += cash_effect(tx)
        if tx['kind'] == 'transfer':
            accounts[tx['to_account_id']]['balance_cents'] += tx['amount']
        if balances is not None:
            balances[tx['id']] = money(accounts[aid]['balance_cents'])
        if tx['kind'] in {'opening_position', 'buy', 'sell'}:
            key = (aid, tx['symbol'])
            h = holdings.setdefault(key, {'account_id': aid, 'symbol': tx['symbol'], 'quantity': 0, 'cost_cents': 0, 'reference_cost_cents': 0})
            if tx['kind'] == 'sell':
                if tx['quantity'] > h['quantity']:
                    raise ValueError('卖出数量超过该日期的持仓，或修改导致后续交易持仓不足')
                h['cost_cents'] -= int((Decimal(h['cost_cents']) * tx['quantity'] / h['quantity']).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
                h['reference_cost_cents'] -= int((Decimal(h['reference_cost_cents']) * tx['quantity'] / h['quantity']).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
                h['quantity'] -= tx['quantity']
            else:
                h['quantity'] += tx['quantity']
                h['cost_cents'] += cents(decimal(tx['price']) * tx['quantity']) + tx['fees']
                h['reference_cost_cents'] += cents(decimal(tx['price']) * tx['quantity'])
        if validate:
            for a in accounts.values():
                if a['balance_cents'] < 0:
                    raise ValueError(f"{a['name']} 在 {tx['date']} 余额不足，请先录入期初余额或转入资金")
    return list(accounts.values()), [h for h in holdings.values() if h['quantity'] > 0]

def save_account(db, body, account_id=None):
    name = str(body.get('name', '')).strip()[:80]
    if not name or body.get('kind') not in KINDS:
        raise ValueError('请填写账户名称并选择资产类型')
    aid = account_id or uid()
    rate = decimal(body.get('rate', 0))
    if rate < 0 or rate > 100:
        raise ValueError('年利率应在 0–100% 之间')
    maturity = body.get('maturity', '')
    if maturity:
        maturity = iso_date(maturity, True)
    if body['kind'] not in {'bond', 'deposit'}:
        rate, maturity = Decimal(0), ''
    if account_id:
        old = db.execute('SELECT * FROM accounts WHERE id=?', (aid,)).fetchone()
        if not old:
            raise ValueError('账户不存在')
        if body['kind'] != 'broker' and db.execute("SELECT 1 FROM transactions WHERE account_id=? AND symbol<>'' LIMIT 1", (aid,)).fetchone():
            raise ValueError('该账户有证券交易或分红记录，须保留证券账户性质')
        if body['kind'] not in {'cash', 'broker'} and (db.execute('SELECT 1 FROM cashflows WHERE account_id=? AND enabled=1', (aid,)).fetchone() or db.execute("SELECT 1 FROM transactions WHERE kind='transfer' AND (account_id=? OR to_account_id=?)", (aid, aid)).fetchone()):
            raise ValueError('该账户关联转账或启用的现金流计划，须保留现金或证券账户性质')
        db.execute('UPDATE accounts SET name=?,kind=?,rate=?,maturity=? WHERE id=?', (name, body['kind'], str(rate), maturity, aid))
        audit(db, 'update', 'account:' + aid, dict(old), body)
        return aid
    db.execute('INSERT INTO accounts VALUES(?,?,?,?,?)', (aid, name, body['kind'], str(rate), maturity))
    amount = cents(body.get('balance', 0))
    if amount < 0:
        raise ValueError('期初余额不能为负数')
    save_transaction(db, {'kind': 'opening', 'account_id': aid, 'date': body.get('date', now().date().isoformat()), 'amount': money(amount), 'note': '期初余额'})
    audit(db, 'create', 'account:' + aid, after=body)
    return aid

def save_transaction(db, body, tx_id=None, internal=False):
    kind = body.get('kind')
    if kind in DIVIDEND_KINDS and not internal:
        kind = 'dividend' if body.get('credit_cash', kind == 'dividend') else 'dividend_record'
    if kind not in TX_KINDS:
        raise ValueError('无效的记录类型')
    aid = str(body.get('account_id', ''))
    account = db.execute('SELECT * FROM accounts WHERE id=?', (aid,)).fetchone()
    if not account:
        raise ValueError('请选择有效账户')
    tx_date = iso_date(body.get('date', now().date().isoformat()))
    quantity = int(body.get('quantity', 0))
    if decimal(body.get('quantity', 0)) != quantity or quantity < 0 or quantity > 1_000_000_000:
        raise ValueError('股数必须为有效整数')
    price = decimal(body.get('price', 0))
    fees = cents(body.get('fees', 0))
    amount = cents(body.get('amount', 0))
    if fees < 0:
        raise ValueError('交易费用不能为负数')
    symbol = ''
    to_id = None
    if kind in {'buy', 'sell', 'opening_position'}:
        if account['kind'] != 'broker':
            raise ValueError('股票交易必须选择证券账户')
        symbol = symbol_code(body.get('symbol', ''))
        if quantity <= 0 or price <= 0:
            raise ValueError('请填写大于零的股数和价格')
        name = security_name(db,symbol,body.get('name',''))
        db.execute('INSERT INTO securities VALUES(?,?) ON CONFLICT(symbol) DO UPDATE SET name=CASE WHEN excluded.name=excluded.symbol THEN securities.name ELSE excluded.name END', (symbol, name))
        amount = 0
    if kind in DIVIDEND_KINDS:
        if account['kind'] != 'broker' or amount < 0 or (amount == 0 and not internal):
            raise ValueError('请选择证券账户，并填写大于零的分红金额')
        symbol = symbol_code(body.get('symbol', ''))
        name = security_name(db,symbol,body.get('name',''))
        db.execute('INSERT INTO securities VALUES(?,?) ON CONFLICT(symbol) DO NOTHING', (symbol, name))
        quantity, price, fees = 0, Decimal(0), 0
    if kind == 'transfer':
        to_id = body.get('to_account_id')
        target = db.execute('SELECT * FROM accounts WHERE id=?', (to_id,)).fetchone()
        if not target or to_id == aid or amount <= 0:
            raise ValueError('请选择另一个转入账户，转账金额须大于零')
        if account['kind'] not in {'cash', 'broker'} or target['kind'] not in {'cash', 'broker'}:
            raise ValueError('首版转账支持现金与证券现金账户；其他资产请使用变动记录')
    if kind in {'income', 'opening', 'balance'} and amount < 0:
        raise ValueError('余额与现金流增量不能为负数')
    before = None
    if tx_id:
        old = db.execute('SELECT * FROM transactions WHERE id=?', (tx_id,)).fetchone()
        if not old:
            raise ValueError('记录不存在')
        before = dict(old)
        if old['kind'] == 'dividend' and old['dividend_id']:
            raise ValueError('已确认分红请先撤销到账确认，再重新确认')
        created = old['created_at']
    else:
        tx_id, created = uid(), now().isoformat()
    settlement = (before['cash_settlement'] if before else 1) if kind in {'buy', 'sell'} else 0
    values = (tx_id, kind, aid, to_id, tx_date, symbol, quantity, str(price), fees, amount,
              str(body.get('note', ''))[:500], body.get('dividend_id') if internal else None, created, settlement)
    if before:
        db.execute('UPDATE transactions SET kind=?,account_id=?,to_account_id=?,date=?,symbol=?,quantity=?,price=?,fees=?,amount=?,note=?,dividend_id=?,created_at=?,cash_settlement=? WHERE id=?',values[1:]+(tx_id,))
    else:
        db.execute('INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', values)
    replay(db, validate=True)
    reconcile(db, strict=True)
    audit(db, 'update' if before else 'create', 'transaction:' + tx_id, before, body)
    return tx_id

def delete_transaction(db, tx_id):
    old = db.execute('SELECT * FROM transactions WHERE id=?', (tx_id,)).fetchone()
    if not old:
        raise ValueError('记录不存在')
    db.execute('DELETE FROM transactions WHERE id=?', (tx_id,))
    if old['dividend_id']:
        db.execute("UPDATE entitlements SET state='pending' WHERE id=?", (old['dividend_id'],))
    replay(db, validate=True)
    reconcile(db, strict=True)
    audit(db, 'delete', 'transaction:' + tx_id, dict(old))


def delete_account(db, account_id):
    account = db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
    if not account:
        raise ValueError('账户不存在')
    related = rows(db, 'SELECT * FROM transactions WHERE account_id=? OR to_account_id=?', (account_id, account_id))
    db.execute('DELETE FROM flow_runs WHERE flow_id IN (SELECT id FROM cashflows WHERE account_id=?)', (account_id,))
    db.execute('DELETE FROM cashflows WHERE account_id=?', (account_id,))
    db.execute('DELETE FROM transactions WHERE account_id=? OR to_account_id=?', (account_id, account_id))
    db.execute('DELETE FROM entitlements WHERE account_id=?', (account_id,))
    db.execute('DELETE FROM accounts WHERE id=?', (account_id,))
    replay(db, validate=True)
    reconcile(db, strict=True)
    audit(db, 'delete', 'account:'+account_id, {'account':dict(account), 'transactions':related})

def reconcile(db, strict=False):
    tracking = db.execute("SELECT value FROM meta WHERE key='tracking_start'").fetchone()[0]
    today = now().date().isoformat()
    valid_ids = set()
    for event in rows(db, "SELECT * FROM dividends WHERE status='implemented' AND record_date<>'' AND pay_date<>'' AND pay_date>=? AND record_date<=?", (tracking, today)):
        _, holdings = replay(db, event['record_date'])
        for h in holdings:
            if h['symbol'] != event['symbol']:
                continue
            eid = event['id'] + ':' + h['account_id']
            valid_ids.add(eid)
            expected = cents(decimal(event['cash_ps']) * h['quantity'])
            old = db.execute('SELECT * FROM entitlements WHERE id=?', (eid,)).fetchone()
            if old and old['state'] == 'confirmed':
                if strict and (old['quantity'] != h['quantity'] or old['expected_amount'] != expected):
                    raise ValueError('此修改影响已确认分红，请先撤销该笔到账确认')
                continue
            state = 'pending' if event['pay_date'] <= today else 'scheduled'
            db.execute('INSERT INTO entitlements VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET quantity=excluded.quantity,expected_amount=excluded.expected_amount,state=excluded.state',
                       (eid, event['id'], h['account_id'], h['quantity'], expected, state))
    for old in rows(db, 'SELECT * FROM entitlements'):
        if old['id'] not in valid_ids:
            if old['state'] == 'confirmed':
                if strict:
                    raise ValueError('此修改影响已确认分红，请先撤销该笔到账确认')
            else:
                db.execute('DELETE FROM entitlements WHERE id=?', (old['id'],))

def confirm_dividend(db, entitlement_id, body):
    reconcile(db)
    e = db.execute("SELECT e.*,d.symbol,d.pay_date FROM entitlements e JOIN dividends d ON d.id=e.dividend_id WHERE e.id=?", (entitlement_id,)).fetchone()
    if not e or e['state'] != 'pending':
        raise ValueError('该分红尚未到派息日、已经确认或不存在')
    amount = cents(body.get('amount', money(e['expected_amount'])))
    if amount < 0 or amount > e['expected_amount']:
        raise ValueError('实际到账金额应介于零与预计税前金额之间')
    received = iso_date(body.get('date', now().date().isoformat()))
    if received < e['pay_date']:
        raise ValueError('到账日期不能早于派息日')
    save_transaction(db, {'kind': 'dividend', 'account_id': e['account_id'], 'date': received,
                         'symbol':e['symbol'],
                         'amount': money(amount), 'dividend_id': entitlement_id,
                         'note': e['symbol'] + ' 分红到账（手动确认）'}, internal=True)
    db.execute("UPDATE entitlements SET state='confirmed' WHERE id=?", (entitlement_id,))

def dps_for(db, symbol, basis='ttm'):
    today = now().date()
    events = rows(db, "SELECT * FROM dividends WHERE symbol=? AND status='implemented'", (symbol,))
    if basis == 'ttm':
        dates = [e for e in events if e['ex_date'] and (today - timedelta(days=365)).isoformat() < e['ex_date'] <= today.isoformat()]
        covered = db.execute('SELECT 1 FROM sync_status WHERE key=?', ('dividend_coverage:' + symbol,)).fetchone()
        return sum((decimal(e['cash_ps']) for e in dates), Decimal(0)) if events or covered else None
    years = {'last_year': 1, 'avg3': 3, 'avg5': 5}.get(basis)
    if not years:
        raise ValueError('未知股息口径')
    if events and db.execute('SELECT 1 FROM sync_status WHERE key=?', ('corporate_actions:' + symbol,)).fetchone():
        return None
    # 没有完整财年覆盖证明时不把数据缺失当作零。
    coverage = db.execute('SELECT value FROM sync_status WHERE key=?', ('dividend_coverage:' + symbol,)).fetchone()
    if not coverage:
        return None
    start_year = int(coverage[0])
    if start_year > today.year - years:
        return None
    selected = [e for e in events if e['report_year'] is not None and today.year-years <= e['report_year'] < today.year]
    return sum((decimal(e['cash_ps']) for e in selected), Decimal(0)) / years

def security_snapshot(db, symbol):
    quote = db.execute('SELECT * FROM quotes WHERE symbol=?', (symbol,)).fetchone()
    quote = dict(quote) if quote else None
    dps = {basis: dps_for(db, symbol, basis) for basis in ['ttm', 'last_year', 'avg3', 'avg5']}
    return {'symbol': symbol, 'name': security_name(db, symbol), 'quote': quote,
            'instrument': 'etf' if is_etf(symbol) else 'stock',
            'dps': {k: float(v) if v is not None else None for k, v in dps.items()},
            'yields': {k: float(v / decimal(quote['price']) * 100) if quote and v is not None else None for k, v in dps.items()}}


def build_state(db):
    reconcile(db)
    balances = {}
    accounts, holdings = replay(db, balances=balances)
    dividend_basis = preference(db, 'dividend_basis', 'avg3')
    names = {s['symbol']: s['name'] for s in rows(db, 'SELECT * FROM securities')}
    quotes = {q['symbol']: q for q in rows(db, 'SELECT * FROM quotes')}
    stock_value, estimate, estimated_count, missing_dividends = 0, 0, 0, 0
    for h in holdings:
        q = quotes.get(h['symbol'])
        h['name'] = names.get(h['symbol'], h['symbol'])
        h['cost'] = money(h.pop('cost_cents'))
        h['average_cost'] = h['cost'] / h['quantity']
        h['quote'] = q
        h['market_value'] = float(decimal(q['price']) * h['quantity']) if q else None
        h['reference_value'] = h['market_value'] if q else money(h.pop('reference_cost_cents'))
        stock_value += cents(h['reference_value'])
        estimated_count += int(q is None)
        dps = dps_for(db, h['symbol'], dividend_basis)
        h['annual_dividend_estimate'] = float(dps * h['quantity']) if dps is not None else None
        missing_dividends += int(dps is None)
        estimate += cents(h['annual_dividend_estimate'] or 0)
        ttm = dps_for(db, h['symbol'], 'ttm')
        h['yield_ttm'] = float(ttm / decimal(q['price']) * 100) if q and ttm is not None else None
        h['cost_yield_ttm'] = float(ttm / decimal(h['average_cost']) * 100) if h['average_cost'] > 0 and ttm is not None else None
        h['dividend_yield'] = float(dps / decimal(q['price']) * 100) if q and dps is not None else None
        h['cost_dividend_yield'] = float(dps / decimal(h['average_cost']) * 100) if h['average_cost'] > 0 and dps is not None else None
        h['instrument'] = 'etf' if is_etf(h['symbol']) else 'stock'
    broker_cash = sum(a['balance_cents'] for a in accounts if a['kind'] == 'broker')
    allocation = {k: sum(a['balance_cents'] for a in accounts if a['kind'] == k) for k in KINDS}
    allocation['cash'] += allocation.pop('broker')
    etf_value = sum(cents(h['reference_value']) for h in holdings if h['instrument'] == 'etf')
    allocation['fund'] += etf_value
    allocation['stock'] = stock_value - etf_value
    total = sum(allocation.values())
    for a in accounts:
        a['balance'] = money(a.pop('balance_cents'))
        a['stock_value'] = sum(h['reference_value'] for h in holdings if h['account_id'] == a['id'])
    order = json.loads(preference(db, 'account_order', '[]'))
    ranks = {aid: i for i, aid in enumerate(order)}
    sort_mode = preference(db, 'account_sort', 'manual')
    accounts.sort(key=lambda a: (ranks.get(a['id'], len(ranks)), a['name'], a['id']))
    if sort_mode == 'type':
        accounts.sort(key=lambda a: ['cash', 'broker', 'fund', 'bond', 'deposit'].index(a['kind']))
    elif sort_mode == 'amount':
        accounts.sort(key=lambda a: -(a['balance'] + a['stock_value']))
    txs = rows(db, 'SELECT * FROM transactions ORDER BY date DESC,created_at DESC,rowid DESC')
    year = now().year
    dividend_txs = [t for t in txs if t['kind'] in DIVIDEND_KINDS]
    from .dividend_history import dividend_receipts
    receipts = dividend_receipts(db, txs)
    confirmed_received = sum(t['amount'] for t in dividend_txs if t['date'].startswith(str(year)))
    received = sum(cents(t['amount']) for t in receipts if t['date'].startswith(str(year)))
    inferred_received = sum(cents(t['amount']) for t in receipts if t['inferred'] and t['date'].startswith(str(year)))
    dividend_years = sorted({str(year)} | {t['date'][:4] for t in receipts}, reverse=True)
    by_year = {y:[money(sum(cents(t['amount']) for t in receipts if t['date'].startswith(f'{y}-{m:02}'))) for m in range(1,13)] for y in dividend_years}
    monthly = by_year[str(year)]
    timeline = []
    tracking_start = db.execute("SELECT value FROM meta WHERE key='tracking_start'").fetchone()[0]
    cash_dates = [t['date'] for t in txs if t['kind'] not in {'buy','sell','opening_position','dividend_record'}]
    start = min([tracking_start]+cash_dates)
    price_dates = {r['date'] for r in rows(db, 'SELECT DISTINCT date FROM daily_prices WHERE date>=?', (start,))}
    dates = sorted({t['date'] for t in txs if t['date'] >= start} | price_dates | {start, now().date().isoformat()})
    for day in dates[-366:]:
        acc, hs = replay(db, day)
        value = sum(a['balance_cents'] for a in acc)
        approximate = False
        for h in hs:
            quote = db.execute('SELECT close FROM daily_prices WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 1', (h['symbol'], day)).fetchone()
            if day == now().date().isoformat() and h['symbol'] in quotes:
                value += cents(decimal(quotes[h['symbol']]['price']) * h['quantity'])
            elif quote:
                value += cents(decimal(quote[0]) * h['quantity'])
            else:
                value += h['reference_cost_cents']
                approximate = True
        timeline.append({'date': day, 'value': money(value), 'estimated': approximate})
    watch = []
    symbols = sorted({w['symbol'] for w in rows(db, 'SELECT * FROM watchlist UNION SELECT symbol FROM rules')} | {h['symbol'] for h in holdings})
    for symbol in symbols:
        q = quotes.get(symbol)
        yields, dps = {}, {}
        for basis in ['ttm', 'last_year', 'avg3', 'avg5']:
            v = dps_for(db, symbol, basis)
            dps[basis] = float(v) if v is not None else None
            yields[basis] = float(v / decimal(q['price']) * 100) if q and v is not None else None
        watch.append({'symbol': symbol, 'name': names.get(symbol, symbol), 'quote': q, 'yields': yields, 'dps': dps, 'instrument':'etf' if is_etf(symbol) else 'stock',
                      'held': any(h['symbol'] == symbol for h in holdings)})
    for t in txs:
        t['amount'] = money(t['amount'])
        t['fees'] = money(t['fees'])
        t['balance_after'] = balances[t['id']]
    entitlements = rows(db, 'SELECT e.*,d.symbol,d.pay_date,d.cash_ps,a.name account_name FROM entitlements e JOIN dividends d ON d.id=e.dividend_id JOIN accounts a ON a.id=e.account_id ORDER BY d.pay_date')
    for e in entitlements:
        e['expected_amount'] = money(e['expected_amount'])
    rules = rows(db, 'SELECT * FROM rules ORDER BY rowid')
    rule_order = {rid: i for i, rid in enumerate(json.loads(preference(db, 'rule_order', '[]')))}
    rules.sort(key=lambda r: rule_order.get(r['id'], len(rule_order)))
    return {'today': now().date().isoformat(), 'tracking_start': tracking_start, 'timeline_start':start, 'accounts': accounts, 'holdings': holdings,
            'preferences': {'account_sort': sort_mode, 'dividend_basis': dividend_basis},
            'securities':rows(db,'SELECT * FROM securities ORDER BY name'),
            'dividend_receipts':receipts,
            'transactions': txs, 'watchlist': watch, 'dividends': rows(db, 'SELECT * FROM dividends ORDER BY pay_date DESC,ex_date DESC'),
            'entitlements': entitlements, 'rules': rules, 'cashflows': rows(db, 'SELECT * FROM cashflows'),
            'notifications': rows(db, 'SELECT * FROM notifications ORDER BY date DESC LIMIT 100'),
            'sync': {r['key']: r['value'] for r in rows(db, 'SELECT * FROM sync_status')},
            'summary': {'total': money(total), 'stock_value': money(stock_value), 'broker_value': money(broker_cash+stock_value),
                        'broker_cash': money(broker_cash), 'received': money(received), 'annual_estimate': money(estimate),
                        'confirmed_received':money(confirmed_received), 'inferred_received':money(inferred_received),
                        'estimated_positions': estimated_count, 'missing_dividend_positions': missing_dividends,
                        'allocation': {k: money(v) for k,v in allocation.items()}},
            'timeline': timeline, 'monthly_dividends': monthly, 'dividend_years':dividend_years, 'monthly_dividends_by_year':by_year}

def run_cashflows(db):
    today = now().date()
    for flow in rows(db, 'SELECT * FROM cashflows WHERE enabled=1'):
        start = date.fromisoformat(flow['start_date'])
        year, month = start.year, start.month
        while (year, month) <= (today.year, today.month):
            due = date(year, month, min(flow['day'], calendar.monthrange(year,month)[1]))
            if start <= due <= today and not db.execute('SELECT 1 FROM flow_runs WHERE flow_id=? AND date=?', (flow['id'], due.isoformat())).fetchone():
                tx_id = save_transaction(db, {'kind': 'income', 'account_id': flow['account_id'], 'date': due.isoformat(), 'amount': money(flow['amount']), 'note': flow['note']})
                db.execute('INSERT INTO flow_runs VALUES(?,?,?)', (flow['id'],due.isoformat(),tx_id))
            month += 1
            if month == 13:
                year, month = year+1, 1
