import argparse
import base64
import datetime as dt
import hashlib
import hmac
from http.cookies import SimpleCookie
import ipaddress
import json
import logging
import mimetypes
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import sys
import threading
import time
import traceback
from urllib.parse import parse_qs, urlsplit
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server
from socketserver import ThreadingMixIn

from . import ai, auth, mailer, service
from .core import CAPS, NAMES, ROOT, SLOTS, TZ, Problem, Store, audit, digest, js, load_env, member_by_id, now, permit, record_view, require, row, rows, setting, uid
from .xlsx import schedule_template, workbook, RECORD_HEADERS

log = logging.getLogger('workbench')

QUILL_APK = ROOT/'data/quill/quill-0.1.5-android-arm64.apk'


class FileDownload:
    """Stream large APKs without retaining the entire package in memory."""
    def __init__(self, path):
        self.file = path.open('rb')
        self.size = os.fstat(self.file.fileno()).st_size

    def __len__(self):
        return self.size

    def __iter__(self):
        try:
            while chunk := self.file.read(256*1024):
                yield chunk
        finally:
            self.close()

    def close(self):
        self.file.close()


class App:
    def __init__(self,store,demo=False,demo_ai=False):
        self.store,self.demo = store,demo
        require(not demo_ai or demo,'本机 AI 联调必须使用独立演示数据库')
        self.demo_ai = demo_ai
        self.rate_lock = threading.Lock()
        self.rates = {}

    @staticmethod
    def client_ip(environ):
        """X-Forwarded-For is trusted only when the peer really is a local reverse proxy."""
        peer = environ.get('REMOTE_ADDR','') or 'unknown'
        forwarded = (environ.get('HTTP_X_FORWARDED_FOR') or '').split(',')[0].strip()
        return forwarded if forwarded and peer in ('127.0.0.1','::1') else peer

    def rate_limit(self,environ,bucket='auth',limit=60):
        key = (bucket,self.client_ip(environ))
        minute = int(time.time()//60)
        with self.rate_lock:
            self.rates = {k:v for k,v in self.rates.items() if v[0]>=minute-1}
            previous,count = self.rates.get(key,(minute,0))
            count = count+1 if previous==minute else 1
            self.rates[key] = (minute,count)
            require(count<=limit,'请求过于频繁，请稍后再试',429)

    def __call__(self,environ,start_response):
        try:
            status,headers,content = self.dispatch(environ)
        except Problem as exc:
            status,headers,content = exc.status,[],js(dict(error=exc.message)).encode()
        except sqlite3.IntegrityError:
            status,headers,content = 409,[],js(dict(error='操作冲突：记录已存在、请求待处理或邮箱已被绑定，请刷新核对')).encode()
        except (ValueError,TypeError,KeyError,OverflowError):
            status,headers,content = 400,[],js(dict(error='请求格式不正确')).encode()
        except Exception:
            log.exception('Unhandled request error')
            status,headers,content = 500,[],js(dict(error='服务暂时出错，请稍后重试')).encode()
        if not any(k.lower()=='content-type' for k,v in headers):
            headers.append(('Content-Type','application/json; charset=utf-8'))
        headers.extend([('Content-Length',str(len(content))),('X-Content-Type-Options','nosniff'),('Referrer-Policy','same-origin'),
            ('X-Frame-Options','DENY'),('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
            ('Permissions-Policy','camera=(), microphone=(), geolocation=()'),('Strict-Transport-Security','max-age=31536000')])
        if not any(k.lower()=='cache-control' for k,v in headers):
            headers.append(('Cache-Control','no-store'))
        reasons = {200:'OK',201:'Created',400:'Bad Request',401:'Unauthorized',403:'Forbidden',404:'Not Found',405:'Method Not Allowed',409:'Conflict',413:'Content Too Large',429:'Too Many Requests',500:'Internal Server Error',502:'Bad Gateway',503:'Service Unavailable'}
        start_response(f'{status} {reasons.get(status,"Error")}',headers)
        return content if isinstance(content,FileDownload) else [content]

    def cookie(self,token,max_age=90*86400):
        secure = '; Secure' if os.getenv('COOKIE_SECURE','false').lower()=='true' else ''
        return ('Set-Cookie',f'wb_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={max_age}'+secure)

    def dispatch(self,e):
        if self.demo_ai:
            require(ipaddress.ip_address(e.get('REMOTE_ADDR','0.0.0.0')).is_loopback,'本机 AI 联调仅限本机访问',403)
        method,path = e['REQUEST_METHOD'],e.get('PATH_INFO','/')
        query = {k:v[-1] for k,v in parse_qs(e.get('QUERY_STRING','')).items()}
        if method=='GET' and path=='/downloads/quill.apk':
            require(QUILL_APK.is_file(),'安装包暂不可用，请稍后再试',404)
            return 200,[('Content-Type','application/vnd.android.package-archive'),
                ('Content-Disposition','attachment; filename="quill-0.1.5-android-arm64.apk"')],FileDownload(QUILL_APK)
        if method=='GET' and not path.startswith('/api/'):
            files = {'/':'index.html','/app.js':'app.js','/style.css':'style.css','/manifest.webmanifest':'manifest.webmanifest','/sw.js':'sw.js','/icon.svg':'icon.svg','/college-logo.png':'college-logo.png'}
            files.update({f'/quill/{name}.png':f'quill/{name}.png' for name in ('icon','home','excel','birthdays','widgets')})
            require(path in files,'页面不存在',404)
            file = ROOT/'app/static'/files[path]
            require(file.is_file(),'页面尚未准备完成',503)
            mime = {'.js':'text/javascript','.webmanifest':'application/manifest+json','.svg':'image/svg+xml'}.get(file.suffix,mimetypes.guess_type(str(file))[0] or 'text/plain')
            data = file.read_bytes()
            etag = '"%s"' % hashlib.sha1(data).hexdigest()[:20]
            cache = [('ETag',etag),('Cache-Control','no-cache')]
            if e.get('HTTP_IF_NONE_MATCH')==etag:
                return 304,cache,b''
            return 200,[('Content-Type',mime+'; charset=utf-8')]+cache,data
        require(path.startswith('/api/'),'接口不存在',404)
        body = {}
        if method=='POST':
            require(e.get('CONTENT_TYPE','').split(';')[0]=='application/json','请使用 JSON 请求')
            require(e.get('HTTP_ORIGIN')==os.getenv('APP_URL','http://127.0.0.1:8765').rstrip('/'),'请求来源不正确',403)
            size = int(e.get('CONTENT_LENGTH') or 0)
            require(0<size<=12*1024*1024,'请求体为空或过大',413)
            try:
                body = json.loads(e['wsgi.input'].read(size))
            except (ValueError,UnicodeDecodeError):
                raise Problem('JSON 格式无效')
            require(isinstance(body,dict),'请求必须为 JSON 对象')
        else:
            require(method=='GET','不支持此请求方法',405)
        route = path.removeprefix('/api/')
        if route=='health' and method=='GET':
            return 200,[],js(dict(ok=True)).encode()
        if route=='auth/code' and method=='POST':
            self.rate_limit(e,'auth',60)
            require(not self.demo,'演示模式不发送邮件')
            return 200,[],js(auth.request_code(self.store,body)).encode()
        if route=='auth/verify' and method=='POST':
            self.rate_limit(e,'auth',60)
            require(not self.demo,'演示模式请使用演示成员入口')
            result = auth.verify_code(self.store,body)
            return 200,[self.cookie(result.pop('token'))],js(result).encode()
        if route=='auth/demo' and method=='POST':
            require(self.demo and ipaddress.ip_address(e.get('REMOTE_ADDR','0.0.0.0')).is_loopback,'此入口仅在本机演示模式开放',403)
            with self.store.write() as db:
                result = auth.create_session(db,member_by_id(db,body.get('id','m01')))
            return 200,[self.cookie(result.pop('token'))],js(result).encode()
        cookies = SimpleCookie()
        try:
            cookies.load(e.get('HTTP_COOKIE',''))
        except Exception:
            pass
        token = cookies['wb_session'].value if 'wb_session' in cookies else None
        with self.store.read() as db:
            member = auth.session_member(db,token,touch=False)
        if member and member['last_seen'][:10]!=now()[:10]:
            with self.store.write() as db:
                auth.touch_session(db,token)
        if route=='bootstrap' and method=='GET':
            with self.store.read() as db:
                setup = bool(db.execute('SELECT 1 FROM members WHERE email IS NOT NULL LIMIT 1').fetchone())
                cfg = dict(demo=self.demo,demo_ai=self.demo_ai,initialized=setup,mail_configured=mailer.configured() and not self.demo,ai_configured=ai.configured() and (not self.demo or self.demo_ai),logged_in=bool(member),csrf=member['csrf'] if member else None,slots=SLOTS)
                if self.demo: cfg['demo_members'] = rows(db,'SELECT id,name FROM members')
                cfg['quill_download_ready'] = QUILL_APK.is_file()
            return 200,[self.cookie(token)] if member else [],js(cfg).encode()
        require(member,'请先使用已绑定邮箱登录',401)
        if method=='POST':
            require(hmac.compare_digest(e.get('HTTP_X_CSRF_TOKEN',''),member['csrf']),'会话校验失败，请刷新页面',403)
            self.rate_limit(e,'write',120)
        if route=='auth/logout' and method=='POST':
            with self.store.write() as db:
                db.execute('DELETE FROM sessions WHERE token_hash=?',(digest(token),))
            return 200,[self.cookie('',0)],b'{"ok":true}'
        if route=='state' and method=='GET':
            with self.store.read() as db:
                result = service.state(db,member,query)
            return 200,[],js(result).encode()
        if route=='workload' and method=='GET':
            with self.store.read() as db:
                result = service.workload(db,member,query)
            return 200,[],js(result).encode()
        if route=='attendance' and method=='GET':
            with self.store.read() as db:
                result=service.attendance(db,member,query)
            return 200,[],js(result).encode()
        if route=='records/template' and method=='GET':
            permit(member,'can_manage_members')
            return 200,[('Content-Type','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),('Content-Disposition','attachment; filename="record-import-template.xlsx"')],workbook(RECORD_HEADERS,[])
        if route=='records/history' and method=='GET':
            with self.store.read() as db:
                record = row(db,'SELECT * FROM registrations WHERE id=?',(query.get('id'),))
                require(record and (record['author_id']==member['id'] or (record['state']=='submitted' and 'can_manage_members' in json.loads(member['caps']))),'无权查看修改历史',403)
                history = rows(db,'SELECT h.*,m.name AS actor_name FROM record_history h LEFT JOIN members m ON m.id=h.actor_id WHERE registration_id=? ORDER BY h.created_at DESC,h.rowid DESC',(record['id'],))
            return 200,[],js(history).encode()
        if route=='export' and method=='GET':
            with self.store.read() as db:
                output = service.export_records(db,member,query)
            return 200,[('Content-Type','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),('Content-Disposition','attachment; filename="records.xlsx"')],output
        if route=='schedule/template' and method=='GET':
            permit(member,'can_manage_schedule')
            return 200,[('Content-Type','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),('Content-Disposition','attachment; filename="schedule-template.xlsx"')],schedule_template()
        if route=='ai/ask' and method=='POST':
            require(not self.demo or self.demo_ai,'演示模式未连接外部模型，请使用普通操作',503)
            return 200,[],js(ai.ask(self.store,member,body)).encode()
        if route=='ai/confirm' and method=='POST':
            with self.store.write() as db:
                fresh = auth.session_member(db,token)
                require(fresh,'会话已失效，请重新登录',401)
                result = ai.confirm(db,fresh,body)
            return 200,[],js(result).encode()
        if route=='mail/run' and method=='POST':
            permit(member,'can_manage_schedule')
            queued = mailer.queue_due(self.store)
            return 200,[],js(dict(queued=queued)).encode()
        if route=='audit' and method=='GET':
            permit(member,'can_manage_members')
            with self.store.read() as db:
                output = rows(db,'SELECT a.*,m.name AS actor_name FROM audit a LEFT JOIN members m ON m.id=a.actor_id ORDER BY a.rowid DESC LIMIT 300')
            return 200,[],js(output).encode()
        if route in service.ACTIONS and method=='POST':
            with self.store.write() as db:
                # Re-read after obtaining the write lock: permission changes cannot race writes.
                fresh = auth.session_member(db,token)
                require(fresh,'会话已失效，请重新登录',401)
                result = service.ACTIONS[route](db,fresh,body)
                if self.demo and route=='swaps/create':
                    result['message']='换班请求已发起，等待对方确认。当前为演示模式，不发送真实邮件。'
            return 200,[],js(result).encode()
        raise Problem('接口不存在',404)


class ThreadingServer(ThreadingMixIn,WSGIServer):
    daemon_threads = True


class QuietHandler(WSGIRequestHandler):
    def log_message(self,format,*args):
        # Never log request body, login codes, cookies, or query parameters.
        log.info('%s %s',self.command,self.path.split('?')[0])


def seed_demo(store):
    with store.write() as db:
        if row(db,"SELECT id FROM shifts LIMIT 1"):
            return
        db.execute('UPDATE members SET caps=? WHERE id=?',(js(CAPS),'m01'))
        db.execute("UPDATE settings SET value='true' WHERE key IN ('share_schedule','share_records')")
        today = dt.datetime.now(TZ).date()
        monday = today-dt.timedelta(days=today.weekday())
        for delta in range(-7,14):
            date = monday+dt.timedelta(days=delta)
            if date.weekday()>4: continue
            for slot in range(4):
                member_id = f'm{(delta*4+slot)%10+1:02}'
                if date==today and slot in (0,2): member_id='m01'
                shift_id = uid()
                db.execute('INSERT INTO shifts(id,date,slot,original_member_id,member_id) VALUES (?,?,?,?,?)',(shift_id,date.isoformat(),slot,member_id,member_id))
                if date<today and (delta+slot)%3:
                    user = member_by_id(db,member_id)
                    service.save_record(db,user,dict(shift_id=shift_id,shift_version=1,state='submitted',tasks=[dict(text=['整理资料归档，更新文件目录','核对活动报名信息','协助处理日常事务','整理会议记录与交接清单'][slot],status='done' if slot%2 else 'pending')],notes='演示数据，仅用于体验'))
        sender = member_by_id(db,'m02')
        service.message_send(db,sender,dict(recipients=['m01'],body='本周的资料已整理到共享文件夹，请值班同学核对后确认收到。（演示通知）'))


def main():
    load_env()
    parser = argparse.ArgumentParser(description='事务组工作台')
    parser.add_argument('--demo',action='store_true',help='仅本机演示，独立数据库，不连接外部邮件或模型')
    parser.add_argument('--demo-ai',action='store_true',help='配合 --demo 显式启用真实 AI；仅本机，邮件仍停用')
    parser.add_argument('--port',type=int,default=int(os.getenv('PORT','8765')))
    parser.add_argument('--host',default=os.getenv('HOST','127.0.0.1'))
    parser.add_argument('--no-worker',action='store_true',help='由独立 worker 负责调度')
    args = parser.parse_args()
    require(not args.demo_ai or args.demo,'--demo-ai 必须同时指定 --demo')
    require(not args.demo_ai or ai.configured(),'请先在 .env 填写 AI_API_KEY 和模型接口配置')
    os.environ.setdefault('APP_URL',f'http://{args.host}:{args.port}')
    require(not args.demo or ipaddress.ip_address(args.host).is_loopback,'演示模式只能绑定本机地址')
    if not args.demo:
        url = os.getenv('APP_URL','http://127.0.0.1:8765')
        require(not url.startswith('https://') or os.getenv('COOKIE_SECURE','false').lower()=='true','HTTPS 部署必须开启 COOKIE_SECURE')
    else:
        os.environ['APP_URL']=f'http://{args.host}:{args.port}'
        os.environ['COOKIE_SECURE']='false'
    store = Store(ROOT/'data/demo.sqlite3' if args.demo else ROOT/os.getenv('DATABASE_PATH','data/workbench.sqlite3'))
    store.init()
    if args.demo: seed_demo(store)
    stop = threading.Event()
    if not args.no_worker and not args.demo:
        from .worker import start_worker
        start_worker(store,stop)
    server = make_server(args.host,args.port,App(store,args.demo,args.demo_ai),server_class=ThreadingServer,handler_class=QuietHandler)
    print(f'CMEE事务组工作台: http://{args.host}:{args.port}'+(' [本机 AI 联调：真实模型，邮件停用]' if args.demo_ai else ' [独立演示模式]' if args.demo else ''),flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(message)s')
    main()
