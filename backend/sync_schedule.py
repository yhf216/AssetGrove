"""Two durable automatic update windows, in the application's Shanghai timezone."""
from .storage import now

TIMES = ('12:00','16:00')


def automatic_slot(moment=None,trading=None):
    moment=moment or now()
    if moment.weekday()>=5 or trading==0:return None
    hm=moment.strftime('%H:%M')
    slot=TIMES[-1] if hm>=TIMES[-1] else TIMES[0] if TIMES[0]<=hm<'13:00' else None
    return f'{moment.date()} {slot}' if slot else None
