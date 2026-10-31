"""Persistent dirty dates and derived market values; never accesses a portfolio."""
from .storage import now
import math

HISTORY_SOURCE='历史全市场 / BaoStock、AKShare'


def save_breadth(db,records,allowed,start,end):
    values=[];allowed=set(allowed)
    for row in records:
        if row.get('code') not in allowed or not start<=row.get('date','')<=end:continue
        try:
            change=float(row['pctChg']) if row.get('pctChg') not in {None,''} else None
            volume=float(row['volume']) if row.get('volume') not in {None,''} else 0
            trading=int(row.get('tradestatus') or 0)
            if not math.isfinite(volume) or volume<0 or (change is not None and not math.isfinite(change)) or (trading and change is None):continue
            values.append((row['code'],row['date'],change,volume,trading))
        except (ValueError,TypeError):continue
    db.executemany('''INSERT INTO breadth VALUES(?,?,?,?,?) ON CONFLICT(symbol,date) DO UPDATE SET
        change=excluded.change,volume=excluded.volume,trading=excluded.trading
        WHERE breadth.change IS NOT excluded.change OR breadth.volume IS NOT excluded.volume OR breadth.trading IS NOT excluded.trading''',values)

CACHE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS market_revision(id INTEGER PRIMARY KEY CHECK(id=1),value INTEGER NOT NULL);
INSERT OR IGNORE INTO market_revision VALUES(1,0);
CREATE TABLE IF NOT EXISTS indicator_dirty(indicator TEXT,date TEXT,not_before REAL NOT NULL DEFAULT 0,PRIMARY KEY(indicator,date));
CREATE INDEX IF NOT EXISTS indicator_due ON indicator_dirty(indicator,not_before,date);
CREATE TRIGGER IF NOT EXISTS breadth_calculate_insert AFTER INSERT ON breadth BEGIN
  INSERT INTO indicator_dirty(indicator,date) VALUES('trin',NEW.date) ON CONFLICT(indicator,date) DO UPDATE SET not_before=0;
END;
CREATE TRIGGER IF NOT EXISTS breadth_calculate_update AFTER UPDATE ON breadth
WHEN OLD.change IS NOT NEW.change OR OLD.volume IS NOT NEW.volume OR OLD.trading IS NOT NEW.trading BEGIN
  INSERT INTO indicator_dirty(indicator,date) VALUES('trin',NEW.date) ON CONFLICT(indicator,date) DO UPDATE SET not_before=0;
END;
CREATE TRIGGER IF NOT EXISTS breadth_calculate_delete AFTER DELETE ON breadth BEGIN
  INSERT INTO indicator_dirty(indicator,date) VALUES('trin',OLD.date) ON CONFLICT(indicator,date) DO UPDATE SET not_before=0;
END;
CREATE TRIGGER IF NOT EXISTS calendar_calculate_update AFTER UPDATE ON calendar WHEN OLD.trading IS NOT NEW.trading BEGIN
  INSERT INTO indicator_dirty(indicator,date) SELECT 'trin',NEW.date WHERE EXISTS(SELECT 1 FROM breadth WHERE date=NEW.date) ON CONFLICT(indicator,date) DO UPDATE SET not_before=0;
END;
'''


def install_cache(db):
    db.executescript(CACHE_SCHEMA)
    for table in ['index_bars','index_info','trin']:
        for operation in ['INSERT','UPDATE','DELETE']:
            db.execute(f'CREATE TRIGGER IF NOT EXISTS {table}_revision_{operation.lower()} AFTER {operation} ON {table} BEGIN UPDATE market_revision SET value=value+1 WHERE id=1; END')
    version=db.execute("SELECT value FROM meta WHERE key='calculation_cache_version'").fetchone()
    if not version or version[0]!='2':
        for name in ['breadth_calculate_insert','breadth_calculate_update','breadth_calculate_delete','calendar_calculate_update']:
            db.execute('DROP TRIGGER IF EXISTS '+name)
        db.executescript(CACHE_SCHEMA)
        db.execute("INSERT OR IGNORE INTO indicator_dirty(indicator,date) SELECT 'trin',b.date FROM breadth b LEFT JOIN trin t ON t.date=b.date WHERE t.value IS NULL GROUP BY b.date")
        db.execute("INSERT INTO meta VALUES('calculation_cache_version','2') ON CONFLICT(key) DO UPDATE SET value=excluded.value")


def calculate_trin(db,day):
    expected=db.execute("SELECT COUNT(*) FROM universe WHERE ipo<=? AND (out_date='' OR out_date>?)",(day,day)).fetchone()[0]
    data=db.execute('''SELECT COUNT(*) observed,
        COALESCE(SUM(CASE WHEN b.trading<>0 AND b.change>0 AND b.volume>0 THEN 1 ELSE 0 END),0) advances,
        COALESCE(SUM(CASE WHEN b.trading<>0 AND b.change<0 AND b.volume>0 THEN 1 ELSE 0 END),0) declines,
        COALESCE(SUM(CASE WHEN b.trading<>0 AND b.change>0 AND b.volume>0 THEN b.volume ELSE 0 END),0) up_volume,
        COALESCE(SUM(CASE WHEN b.trading<>0 AND b.change<0 AND b.volume>0 THEN b.volume ELSE 0 END),0) down_volume
        FROM breadth b JOIN universe u ON u.symbol=b.symbol
        WHERE b.date=? AND u.ipo<=b.date AND (u.out_date='' OR u.out_date>b.date)
        AND (b.trading=0 OR (b.change IS NOT NULL AND b.volume>=0))''',(day,)).fetchone()
    observed,up,down,uv,dv=data
    complete=expected>0 and observed==expected
    value=(up/down)/(uv/dv) if complete and up and down and uv>0 and dv>0 else None
    coverage=observed/expected if expected else 0
    old=db.execute('SELECT value,source FROM trin WHERE date=?',(day,)).fetchone()
    # A partial cache cannot replace a verified snapshot.
    if value is not None or not old or old[0] is None or old[1]==HISTORY_SOURCE:
        reason='' if value is not None else '上涨或下跌组为空' if complete else '历史样本未齐，未发布数值'
        db.execute('''INSERT INTO trin VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET
            value=excluded.value,advances=excluded.advances,declines=excluded.declines,
            up_volume=excluded.up_volume,down_volume=excluded.down_volume,coverage=excluded.coverage,
            source=excluded.source,provisional=excluded.provisional,reason=excluded.reason
            WHERE trin.value IS NOT excluded.value OR trin.advances IS NOT excluded.advances OR trin.declines IS NOT excluded.declines
            OR trin.up_volume IS NOT excluded.up_volume OR trin.down_volume IS NOT excluded.down_volume
            OR trin.coverage IS NOT excluded.coverage OR trin.source IS NOT excluded.source
            OR trin.provisional IS NOT excluded.provisional OR trin.reason IS NOT excluded.reason''',
            (day,value,up,down,uv,dv,coverage,HISTORY_SOURCE,0,reason))
    if complete:
        db.execute("DELETE FROM indicator_dirty WHERE indicator='trin' AND date=?",(day,))
    else:
        db.execute("INSERT INTO indicator_dirty VALUES('trin',?,?) ON CONFLICT(indicator,date) DO UPDATE SET not_before=excluded.not_before",(day,now().timestamp()+60))
    return complete
