"""Durable, slow background history work, independent of any open UI page."""
from datetime import date, timedelta
import threading

from .storage import now
from .sync_schedule import automatic_slot,TIMES


def last_closed_day(store,moment=None):
    moment=moment or now()
    day=moment.date() if moment.strftime('%H:%M')>=TIMES[-1] else moment.date()-timedelta(days=1)
    with store.connect() as db:
        calendar={r['date']:r['trading'] for r in db.execute('SELECT date,trading FROM calendar WHERE date BETWEEN ? AND ?',(str(day-timedelta(days=20)),str(day)))}
    while day.weekday()>=5 or calendar.get(str(day))==0:day-=timedelta(days=1)
    return day.isoformat()


class MarketScheduler:
    def __init__(self,service):
        self.service=service;self.store=service.store;self.stop=threading.Event();self.thread=None;self.calculation_thread=None

    def start(self):
        self.thread=threading.Thread(target=self.loop,daemon=True)
        self.thread.start()
        self.calculation_thread=threading.Thread(target=self.calculation_loop,daemon=True)
        self.calculation_thread.start()

    def calculation_loop(self):
        if self.stop.wait(1):return
        while not self.stop.is_set():
            self.calculate()
            if self.stop.wait(5):return

    def calculate(self):
        if self.stop.is_set() or not self.store.setting('history_enabled',default=True):return 0
        with self.service.lock:self.service.calculation.update(running=True,done=0,total=0,error='')
        try:
            def progress(done,total):
                with self.service.lock:self.service.calculation.update(done=done,total=total)
            return self.store.calculate_pending(cancel=self.service.calculation_cancel,progress=progress)
        except Exception as exc:
            with self.service.lock:self.service.calculation['error']=str(exc)[:250]
            return 0
        finally:
            with self.service.lock:self.service.calculation['running']=False

    def loop(self):
        while not self.stop.wait(20):
            try:self.tick()
            except Exception as exc:
                self.service.update(phase='后台任务等待重试')
                self.store.setting('history_next_at',now().timestamp()+900,write=True)
                self.store.setting('background_error',str(exc)[:250],write=True)

    def tick(self,moment=None):
        moment=moment or now()
        if self.service.state()['progress']['running']:return
        today=moment.date().isoformat()
        with self.store.connect() as db:trading=db.execute('SELECT trading FROM calendar WHERE date=?',(today,)).fetchone()
        slot=automatic_slot(moment,trading[0] if trading else None)
        if not slot:return
        if self.store.setting('market_auto_attempt')!=slot:
            self.service.start('indices',daily_target=today,automatic=slot)
            return
        if not self.store.setting('history_enabled',default=True):return
        # A single small history batch after the closing update. No automatic
        # retry loops, including after restarts, pause/resume or old retry timers.
        if not slot.endswith(TIMES[-1]) or self.store.setting('history_auto_attempt')==today:return
        if self.store.setting('market_auto_completed')!=slot:return
        self.store.setting('history_auto_attempt',today,write=True)
        target=last_closed_day(self.store,moment)
        plan=self.store.setting('history_plan')
        if not plan:
            with self.store.connect() as db:
                bounds=db.execute('SELECT MIN(date),MAX(date) FROM breadth').fetchone()
            end=min(bounds[1],target) if bounds[1] else target
            start=bounds[0] if bounds[0] and bounds[0]<=end else str(date.fromisoformat(end)-timedelta(days=120))
            plan={'start':start,'end':end}
            self.store.setting('history_plan',plan,write=True)
        self.service.start('history',plan['start'],plan['end'],budget=0,managed=True)

    def completed(self,start,end,result):
        if not result:return
        if result['complete']:
            target=last_closed_day(self.store)
            oldest=min(start,self.store.setting('oldest_complete',default=start))
            self.store.setting('oldest_complete',oldest,write=True)
            with self.store.connect() as db:
                bounds=db.execute('SELECT MIN(date),MAX(date) FROM trin WHERE value IS NOT NULL').fetchone()
            if bounds[1] and bounds[1]<target:
                plan={'start':str(date.fromisoformat(bounds[1])+timedelta(days=1)),'end':target}
            elif min(oldest,bounds[0] or oldest)>'1990-12-19':
                older_end=date.fromisoformat(min(oldest,bounds[0] or oldest))-timedelta(days=1)
                plan={'start':max('1990-12-19',str(older_end-timedelta(days=364))),'end':str(older_end)}
            else:
                self.store.setting('history_next_at',now().timestamp()+86400,write=True)
                return
            self.store.setting('history_plan',plan,write=True)
        self.store.setting('history_next_at',now().timestamp()+result.get('wait',30),write=True)
