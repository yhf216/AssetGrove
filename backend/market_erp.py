"""CSI 300 trailing earnings yield minus Chinese ten-year government yield."""
import math
from datetime import date

SCHEMA='''
CREATE TABLE IF NOT EXISTS erp_pe(date TEXT PRIMARY KEY,pe REAL NOT NULL,source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS erp_bond(date TEXT PRIMARY KEY,yield10 REAL NOT NULL,source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS erp(date TEXT PRIMARY KEY,value REAL NOT NULL,pe REAL NOT NULL,bond_yield REAL NOT NULL,pe_source TEXT NOT NULL,bond_source TEXT NOT NULL);
'''


def install_erp(db):
    db.executescript(SCHEMA)
    for table in ['erp_pe','erp_bond']:
        for operation,reference in [('INSERT','NEW'),('UPDATE','NEW'),('DELETE','OLD')]:
            changed=' WHEN OLD.pe IS NOT NEW.pe OR OLD.source IS NOT NEW.source' if operation=='UPDATE' and table=='erp_pe' else ' WHEN OLD.yield10 IS NOT NEW.yield10 OR OLD.source IS NOT NEW.source' if operation=='UPDATE' else ''
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_dirty_{operation.lower()} AFTER {operation} ON {table}{changed} BEGIN
                INSERT INTO indicator_dirty(indicator,date) VALUES('erp',{reference}.date) ON CONFLICT(indicator,date) DO UPDATE SET not_before=0;
                END''')


def save_component(db,kind,records,today):
    if kind not in {'pe','bond'}:raise ValueError('无效的 ERP 数据类型')
    table,column=('erp_pe','pe') if kind=='pe' else ('erp_bond','yield10')
    values=[]
    for row in records:
        try:
            day=date.fromisoformat(str(row['date'])[:10]).isoformat()
            value=float(row[column])
            if day>today or not math.isfinite(value) or (kind=='pe' and value<=0) or (kind=='bond' and not -10<=value<=30):continue
            values.append((day,value,str(row['source'])))
        except (ValueError,KeyError,TypeError):continue
    if not values:raise ValueError('ERP 数据为空或无有效数值')
    db.executemany(f'''INSERT INTO {table} VALUES(?,?,?) ON CONFLICT(date) DO UPDATE SET
        {column}=excluded.{column},source=excluded.source
        WHERE {table}.{column} IS NOT excluded.{column} OR {table}.source IS NOT excluded.source''',values)


def calculate_pending(db,limit=128):
    days=[r[0] for r in db.execute("SELECT date FROM indicator_dirty WHERE indicator='erp' ORDER BY date DESC LIMIT ?",(limit,))]
    for day in days:
        raw=db.execute('SELECT p.pe,b.yield10,p.source,b.source FROM erp_pe p JOIN erp_bond b ON b.date=p.date WHERE p.date=?',(day,)).fetchone()
        if raw:
            pe,yield10,pe_source,bond_source=raw
            value=100/pe-yield10
            db.execute('''INSERT INTO erp VALUES(?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET
                value=excluded.value,pe=excluded.pe,bond_yield=excluded.bond_yield,pe_source=excluded.pe_source,bond_source=excluded.bond_source
                WHERE erp.value IS NOT excluded.value OR erp.pe IS NOT excluded.pe OR erp.bond_yield IS NOT excluded.bond_yield
                OR erp.pe_source IS NOT excluded.pe_source OR erp.bond_source IS NOT excluded.bond_source''',(day,value,pe,yield10,pe_source,bond_source))
        else:db.execute('DELETE FROM erp WHERE date=?',(day,))
        db.execute("DELETE FROM indicator_dirty WHERE indicator='erp' AND date=?",(day,))
    return len(days)
