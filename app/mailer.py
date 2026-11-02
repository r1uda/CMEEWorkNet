import datetime as dt
from email.message import EmailMessage
import logging
import html
import re
import os
import smtplib
import ssl

from .core import SLOTS, TZ, now, row, rows, setting, slot_end, uid
from . import swap_mail, escalation_mail, public_swap_mail, workflow_mail

log = logging.getLogger(__name__)


def configured():
    return bool(os.getenv('SMTP_HOST') and os.getenv('MAIL_FROM')
                and (not os.getenv('SMTP_USER') or os.getenv('SMTP_PASSWORD')))


class UncertainDelivery(Exception):
    pass


def send_email(address, subject, body):
    if not configured():
        raise RuntimeError('邮件服务未配置')
    if not address:
        raise RuntimeError('成员邮箱未配置')
    msg = EmailMessage()
    msg['From'], msg['To'], msg['Subject'] = os.environ['MAIL_FROM'], address, subject
    msg.set_content(body)
    escaped = html.escape(body)
    linked = re.sub(r'https?://[^\s<>]+', lambda m: '<a href="' + m[0] + '">' + m[0] + '</a>', escaped)
    msg.add_alternative('<html><body><div style="white-space:pre-wrap;line-height:1.7">' + linked + '</div></body></html>', subtype="html")
    host, port = os.environ['SMTP_HOST'], int(os.getenv('SMTP_PORT','465'))
    mode = os.getenv('SMTP_SECURITY','ssl')
    if mode not in ('ssl','starttls'):
        raise RuntimeError('SMTP_SECURITY 必须为 ssl 或 starttls')
    client = smtplib.SMTP_SSL(host,port,timeout=12,context=ssl.create_default_context()) if mode=='ssl' else smtplib.SMTP(host,port,timeout=12)
    try:
        if mode=='starttls':
            client.starttls(context=ssl.create_default_context())
        if os.getenv('SMTP_USER'):
            client.login(os.environ['SMTP_USER'],os.getenv('SMTP_PASSWORD',''))
        try:
            refused = client.send_message(msg)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
        except (smtplib.SMTPResponseException,smtplib.SMTPRecipientsRefused):
            raise
        except (OSError,smtplib.SMTPServerDisconnected) as exc:
            raise UncertainDelivery('发信连接中断，邮件可能已被接受；请核查后再决定是否重发') from exc
    finally:
        # QUIT failing after a successful send must not turn a known acceptance into a retry.
        try:
            client.quit()
        except Exception:
            client.close()


def queue_due(store, at=None):
    at = at or dt.datetime.now(TZ)
    with store.write() as db:
        activation = setting(db,'activation_date')
        if not activation:
            return 0
        shifts = rows(db,"""SELECT s.* FROM shifts s WHERE active=1 AND date>=? AND date<=?
            AND NOT EXISTS (SELECT 1 FROM registrations r WHERE r.shift_id=s.id AND state='submitted')
            AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.kind='reminder' AND o.shift_id=s.id AND o.status IN ('sent','sending','uncertain'))""",(activation,at.date().isoformat()))
        count = 0
        for shift in shifts:
            if not notifications_enabled(db, shift['member_id']):
                continue
            if slot_end(shift)+dt.timedelta(minutes=10)>at:
                continue
            slot = SLOTS[shift['slot']]
            url = os.getenv('APP_URL','http://127.0.0.1:8765').rstrip('/') + '/#register?shift=' + shift['id']
            before = db.total_changes
            db.execute("""INSERT INTO outbox(id,dedupe_key,shift_id,shift_version,recipient_id,subject,body,status,next_attempt_at,created_at)
                VALUES (?,?,?,?,?,?,?,'pending',?,?)
                ON CONFLICT(dedupe_key) DO UPDATE SET status='pending',error=NULL,next_attempt_at=excluded.next_attempt_at
                WHERE outbox.status='canceled'""",(uid(),f"reminder:{shift['id']}:{shift['version']}",shift['id'],shift['version'],shift['member_id'],
                f"值班登记提醒 · {shift['date']} {slot['name']}",f"你负责的 {shift['date']} {slot['name']}（{slot['start']}—{slot['end']}）尚未提交登记。\n即使没有事务，也请正常提交。\n登记入口：{url}\n请使用本人账号登录。",at.isoformat(timespec='seconds'),now()))
            count += db.total_changes-before
        return count


def notifications_enabled(db, member_id):
    # Login codes are explicitly requested and bypass this background-mail preference.
    return setting(db, 'mail_notifications:' + member_id) is not False


def eligible(db,item,at):
    if item['kind']=='public_swap':
        return public_swap_mail.eligible(db,item,at)
    if item['kind']=='workflow':
        return workflow_mail.eligible(db,item,at)
    if item['kind']=='swap_request':
        return swap_mail.eligible(db,item,at)
    if not notifications_enabled(db, item['recipient_id']):
        return False
    if item['kind']=='reminder_escalation':
        return escalation_mail.eligible(db,item,at)
    if item['kind']!='reminder': return False
    shift = row(db,'SELECT * FROM shifts WHERE id=?',(item['shift_id'],))
    activation = setting(db,'activation_date')
    submitted = row(db,"SELECT id FROM registrations WHERE shift_id=? AND state='submitted'",(item['shift_id'],))
    other = row(db,"SELECT id FROM outbox WHERE kind='reminder' AND shift_id=? AND id!=? AND status IN ('sent','sending','uncertain')",(item['shift_id'],item['id']))
    return bool(shift and shift['active'] and activation and shift['date']>=activation
                and shift['version']==item['shift_version'] and shift['member_id']==item['recipient_id']
                and slot_end(shift)+dt.timedelta(minutes=10)<=at and not submitted and not other)


def process_outbox(store, sender=send_email, at=None, limit=20):
    fixed_at = at
    at = fixed_at or dt.datetime.now(TZ)
    # Serialize the last validation, SMTP acceptance and status update with schedule writes.
    # This can briefly block writes; it cannot retract mail already accepted by SMTP.
    with store.read() as db:
        ids = [r['id'] for r in rows(db,"SELECT id FROM outbox WHERE status IN ('pending','failed') AND attempts<4 AND next_attempt_at<=? ORDER BY created_at LIMIT ?",(at.isoformat(timespec='seconds'),limit))]
    for item_id in ids:
        with store.write() as db:
            at = fixed_at or dt.datetime.now(TZ)
            item = row(db,'SELECT * FROM outbox WHERE id=?',(item_id,))
            if not item or item['status'] not in ('pending','failed') or item['attempts']>=4:
                continue
            if not eligible(db,item,at):
                db.execute("UPDATE outbox SET status='canceled',error='发送前检查：班次、责任人、提交或启用范围已改变' WHERE id=?",(item_id,))
                continue
            recipient = row(db,'SELECT email FROM members WHERE id=? AND active=1',(item['recipient_id'],))
            if not recipient or not recipient['email']:
                db.execute("UPDATE outbox SET error='收件成员邮箱尚未绑定',next_attempt_at=? WHERE id=?",
                           ((at+dt.timedelta(minutes=5)).isoformat(timespec='seconds'),item_id))
                continue
            # Durably claim before network activity. A crash leaves 'sending', which becomes
            # 'uncertain' at the next exclusive worker startup and is never blindly retried.
            db.execute("UPDATE outbox SET status='sending',attempts=attempts+1 WHERE id=?",(item_id,))
            db.commit()
            db.execute('BEGIN IMMEDIATE')
            # A schedule write can slip between claim and reacquiring the lock: validate again.
            at = fixed_at or dt.datetime.now(TZ)
            if not eligible(db,item,at):
                db.execute("UPDATE outbox SET status='canceled',error='领取任务后班次已改变' WHERE id=?",(item_id,))
                continue
            recipient = row(db,'SELECT email FROM members WHERE id=? AND active=1',(item['recipient_id'],))
            if not recipient or not recipient['email']:
                db.execute("UPDATE outbox SET status='pending',attempts=attempts-1,error='收件成员邮箱尚未绑定',next_attempt_at=? WHERE id=?",
                           ((at+dt.timedelta(minutes=5)).isoformat(timespec='seconds'),item_id))
                continue
            try:
                if item['kind']=='reminder_escalation':
                    item['body']=escalation_mail.refresh_body(db,item,at)
                    if not item['body']:
                        db.execute("UPDATE outbox SET status='canceled',error='汇总班次已全部处理' WHERE id=?",(item_id,))
                        continue
                sender(recipient['email'] if recipient else None,item['subject'],item['body'])
                db.execute("UPDATE outbox SET status='sent',sent_at=?,error=NULL WHERE id=?",(now(),item_id))
            except UncertainDelivery as exc:
                db.execute("UPDATE outbox SET status='uncertain',error=? WHERE id=?",(str(exc),item_id))
            except Exception as exc:
                retry_at = (at+dt.timedelta(minutes=2**(item['attempts']+1))).isoformat(timespec='seconds')
                # Do not persist server responses that could include credentials.
                db.execute("UPDATE outbox SET status='failed',next_attempt_at=?,error=? WHERE id=?",(retry_at,type(exc).__name__+': 发送失败，请检查服务配置或收件地址',item_id))


def recover_interrupted(store):
    # Only at exclusive worker startup, never periodically while another sender is active.
    with store.write() as db:
        db.execute("UPDATE outbox SET status='uncertain',error='发信进程上次在发送中停止，需人工核查，未自动重发' WHERE status='sending'")


def tick(store):
    queue_due(store)
    escalation_mail.queue_due(store)
    if configured():
        process_outbox(store)
