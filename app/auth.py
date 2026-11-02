import datetime as dt
import hmac
import re
import secrets

from . import mailer
from .core import TZ, audit, digest, now, require, row, text_field


def create_session(db,member):
    token, csrf = secrets.token_urlsafe(40),secrets.token_urlsafe(24)
    expires = (dt.datetime.now(TZ)+dt.timedelta(days=90)).isoformat(timespec='seconds')
    db.execute('INSERT INTO sessions VALUES (?,?,?,?,?)',(digest(token),member['id'],expires,now(),csrf))
    audit(db,member['id'],'auth.login',{})
    return dict(token=token,csrf=csrf)


def session_member(db,token):
    if not token:
        return None
    result = row(db,'SELECT m.*,s.csrf,s.last_seen FROM sessions s JOIN members m ON m.id=s.member_id WHERE s.token_hash=? AND s.expires_at>? AND m.active=1',(digest(token),now()))
    if result and result['last_seen'][:10]!=now()[:10]:
        db.execute('UPDATE sessions SET last_seen=?,expires_at=? WHERE token_hash=?',(now(),(dt.datetime.now(TZ)+dt.timedelta(days=90)).isoformat(timespec='seconds'),digest(token)))
    return result


def request_code(store,body):
    require(mailer.configured(),'邮件服务尚未配置，请联系管理员',503)
    email = text_field(body.get('email',''),254).lower()
    require('@' in email,'请输入有效邮箱')
    code = f'{secrets.randbelow(1000000):06d}'
    with store.write() as db:
        member = row(db,'SELECT id FROM members WHERE email=? AND active=1',(email,))
        previous = row(db,'SELECT sent_at FROM auth_codes WHERE email=?',(email,))
        require(not previous or dt.datetime.fromisoformat(previous['sent_at'])+dt.timedelta(seconds=60)<=dt.datetime.now(TZ),'请等待 60 秒后再申请验证码',429)
        if not member:
            return dict(message='如果邮箱已绑定，将收到验证码；请同时检查垃圾邮件。')
        db.execute('INSERT OR REPLACE INTO auth_codes VALUES (?,?,?,?,0,0)',(email,digest(email+':'+code),(dt.datetime.now(TZ)+dt.timedelta(minutes=10)).isoformat(timespec='seconds'),now()))
    try:
        mailer.send_email(email,'事务组工作台 · 登录验证码',f'【CMEE事务组】\n\n登录验证码（仅以下一行的 6 位数字）：\n\n{code}\n\n有效期：十分钟。请完整输入这六位数字。\n请勿转发；不是本人操作可忽略。')
    except Exception:
        with store.write() as db:
            db.execute('UPDATE auth_codes SET consumed=1 WHERE email=? AND code_hash=?',(email,digest(email+':'+code)))
        require(False,'验证码发送失败，请联系管理员检查邮件服务',503)
    return dict(message='如果邮箱已绑定，将收到验证码；请同时检查垃圾邮件。')


def verify_code(store,body):
    email,code = text_field(body.get('email',''),254).lower(),text_field(body.get('code',''),20)
    require(bool(re.fullmatch(r'[0-9]{6}',code)),'请输入完整的 6 位数字验证码，不要添加其他数字')
    # Failed attempts must commit even though the endpoint will return an error.
    with store.write() as db:
        item = row(db,'SELECT * FROM auth_codes WHERE email=?',(email,))
        valid = item and not item['consumed'] and item['expires_at']>now() and item['attempts']<5
        if valid:
            db.execute('UPDATE auth_codes SET attempts=attempts+1 WHERE email=?',(email,))
            valid = hmac.compare_digest(item['code_hash'],digest(email+':'+code))
        if valid:
            member = row(db,'SELECT * FROM members WHERE email=? AND active=1',(email,))
            if member:
                db.execute('UPDATE auth_codes SET consumed=1 WHERE email=?',(email,))
                return create_session(db,member)
    require(False,'验证码错误、已使用或已过期',401)
