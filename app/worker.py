"""A single persistent reminder worker per SQLite database."""
import logging
import os
from pathlib import Path
import threading

from .core import ROOT, Store, load_env
from .mailer import recover_interrupted, tick

log = logging.getLogger(__name__)


def acquire_lock(store):
    handle = open(store.path+'.worker.lock','a+b')
    if os.fstat(handle.fileno()).st_size==0:
        handle.write(b'0'); handle.flush()
    handle.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError('已有催填 worker 正在运行；每个数据库只启动一个 worker')
    return handle


def loop(store,stop,lock):
    try:
        recover_interrupted(store)
        while not stop.is_set():
            try:
                tick(store)
            except Exception:
                log.exception('Reminder tick failed')
            stop.wait(30)
    finally:
        lock.close()


def start_worker(store,stop):
    lock = acquire_lock(store)
    thread = threading.Thread(target=loop,args=(store,stop,lock),daemon=True,name='reminders')
    thread.start()
    return thread


def main():
    load_env()
    store = Store(ROOT/os.getenv('DATABASE_PATH','data/workbench.sqlite3'))
    store.init()
    stop = threading.Event()
    lock = acquire_lock(store)
    try:
        loop(store,stop,lock)
    except KeyboardInterrupt:
        stop.set()


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    main()
