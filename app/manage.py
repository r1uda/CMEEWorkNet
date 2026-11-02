import argparse
import contextlib
import datetime as dt
import os
from pathlib import Path
import re
import sqlite3

from .core import CAPS, CURRENT_SCHEMA_VERSION, ROOT, Store, TZ, audit, js, load_env, now, require, row, rows


def main():
    load_env()
    parser = argparse.ArgumentParser(description='成员初始化与数据库维护（在服务器上执行）')
    sub = parser.add_subparsers(dest='command',required=True)
    sub.add_parser('members',help='列出固定成员与已配置权限')
    init = sub.add_parser('init',help='首次绑定真实管理员邮箱；仅未初始化数据库可执行')
    init.add_argument('--admin',required=True,help='固定成员完整姓名')
    init.add_argument('--email',required=True,help='本人真实邮箱')
    sub.add_parser('backup',help='通过 SQLite 在线备份 API 生成一致性备份')
    sub.add_parser('mail-status',help='列出最近邮件任务 ID 和状态，供人工核查使用')
    restore = sub.add_parser('restore',help='停止服务后恢复备份；恢复前自动备份当前数据库')
    restore.add_argument('--source',required=True)
    restore.add_argument('--confirm-restoration',action='store_true',required=True)
    mail = sub.add_parser('resolve-mail',help='核实发送不确定结果后，标记已接受或允许重试')
    mail.add_argument('--id',required=True)
    mail.add_argument('--result',choices=['accepted','retry'],required=True)
    mail.add_argument('--reason',required=True)
    args = parser.parse_args()
    store = Store(ROOT/os.getenv('DATABASE_PATH','data/workbench.sqlite3'))
    store.init()
    if args.command=='members':
        with store.read() as db:
            for m in rows(db,'SELECT id,name,email,caps FROM members'):
                print(m['id'],m['name'],m['email'] or '未绑定',m['caps'])
    elif args.command=='init':
        email = args.email.strip().lower()
        require(re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email),'邮箱格式错误')
        with store.write() as db:
            require(not row(db,'SELECT id FROM members WHERE email IS NOT NULL'),'已完成初始化，请通过管理页面配置成员')
            member = row(db,'SELECT id FROM members WHERE name=?',(args.admin,))
            require(member,'姓名不在固定成员名单中')
            db.execute('UPDATE members SET email=?,caps=? WHERE id=?',(email,js(CAPS),member['id']))
            audit(db,member['id'],'setup.initial_admin',dict(name=args.admin))
        print('管理员已绑定；配置 SMTP 后即可通过本人邮箱验证登录。尚未开启任何历史催填。')
    elif args.command=='mail-status':
        with store.read() as db:
            for item in rows(db,'SELECT id,status,attempts,subject,error FROM outbox ORDER BY created_at DESC LIMIT 100'):
                print(js(item))
    elif args.command=='backup':
        print(backup(store))
    elif args.command=='restore':
        from .worker import acquire_lock
        lock = acquire_lock(store)
        try:
            source = Path(args.source).resolve()
            require(source.is_file() and source!=Path(store.path).resolve(),'备份路径无效')
            with contextlib.closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as db:
                require(db.execute('PRAGMA integrity_check').fetchone()[0]=='ok','备份完整性检查失败')
                require(1<=db.execute('SELECT MAX(version) FROM schema_version').fetchone()[0]<=CURRENT_SCHEMA_VERSION,'备份数据库版本不兼容')
                previous = backup(store)
                with store.read() as destination:
                    db.backup(destination)
            store.init()
            with store.write() as db:
                db.execute('DELETE FROM sessions')
                db.execute('DELETE FROM auth_codes')
                db.execute("UPDATE outbox SET status='uncertain',error='恢复后等待人工核查；备份后可能已经投递' WHERE status IN ('pending','failed','sending')")
                audit(db,None,'database.restore',dict(previous_backup=str(previous)))
            print('恢复完成。原数据库已备份到',previous,'；会话已撤销，待发邮件须核查。')
        finally:
            lock.close()
    elif args.command=='resolve-mail':
        with store.write() as db:
            item = row(db,'SELECT * FROM outbox WHERE id=?',(args.id,))
            require(item and (item['status']=='uncertain' or (item['status']=='failed' and args.result=='retry')),'只能核查 uncertain 邮件或重新排队 failed 邮件')
            if args.result=='accepted':
                db.execute("UPDATE outbox SET status='sent',sent_at=?,error=NULL WHERE id=?",(now(),args.id))
            else:
                db.execute("UPDATE outbox SET status='pending',attempts=0,next_attempt_at=?,error=NULL WHERE id=?",(now(),args.id))
            audit(db,None,'mail.resolve',dict(id=args.id,result=args.result,reason=args.reason))
        print('已记录人工核查结果；重试仍会重新检查班次与责任人。')


def backup(store):
    target = ROOT/'backups'/('workbench-'+dt.datetime.now(TZ).strftime('%Y%m%d-%H%M%S-%f')+'.sqlite3')
    target.parent.mkdir(exist_ok=True)
    with store.read() as source, contextlib.closing(sqlite3.connect(target)) as destination:
        source.backup(destination)
    if os.name!='nt': target.chmod(0o600)
    return target


if __name__=='__main__':
    main()
