"""Public volunteering is consent to a proposal, never an immediate schedule edit.

Public email recipients are controlled by the explicit public-swap mail mode.
"""
import datetime as dt

from . import public_swap_mail, workflow_mail

from .core import TZ, audit, can, js, member_by_id, now, permit, require, row, rows, slot_end, text_field, uid


def available(db, shift_id, owner, version):
    shift = row(db, 'SELECT * FROM shifts WHERE id=?', (shift_id,))
    member = row(db, 'SELECT * FROM members WHERE id=? AND active=1', (owner,))
    return bool(member and can(member, 'can_register') and shift and shift['active']
                and shift['member_id'] == owner and shift['version'] == version
                and slot_end(shift) > dt.datetime.now(TZ)
                and not row(db, 'SELECT id FROM registrations WHERE shift_id=?', (shift_id,)))


def drop_mail_audience(db, public_id):
    """The per-post mail audience is only needed while the post is open."""
    db.execute('DELETE FROM settings WHERE key=?', ('public_swap_mail_audience:' + str(public_id),))


def expire(db):
    for post in rows(db, "SELECT * FROM public_swaps WHERE state='open'"):
        if not available(db, post['shift_id'], post['requester_id'], post['shift_version']):
            db.execute("UPDATE public_swaps SET state='stale',decided_at=? WHERE id=?", (now(), post['id']))
            drop_mail_audience(db, post['id'])


def listing(db, member):
    posts = rows(db, '''SELECT p.*,m.name AS requester_name,s.date,s.slot FROM public_swaps p
        JOIN members m ON m.id=p.requester_id JOIN shifts s ON s.id=p.shift_id
        ORDER BY p.created_at DESC,p.id DESC''')
    for post in posts:
        if post['state'] == 'open' and not available(db, post['shift_id'], post['requester_id'], post['shift_version']):
            post['state'] = 'stale'
        post['offers'] = rows(db, '''SELECT o.*,m.name AS member_name,s.date AS target_date,s.slot AS target_slot
            FROM public_swap_offers o JOIN members m ON m.id=o.member_id
            LEFT JOIN shifts s ON s.id=o.target_shift_id WHERE public_id=? ORDER BY o.created_at,o.id''', (post['id'],))
        for offer in post['offers']:
            person = row(db, 'SELECT * FROM members WHERE id=? AND active=1', (offer['member_id'],))
            if offer['state'] == 'active' and (not person or not can(person, 'can_register') or
                    (offer['target_shift_id'] and not available(db, offer['target_shift_id'], offer['member_id'], offer['target_version']))):
                offer['state'] = 'stale'
    return posts


def notify(db, sender, recipients, text):
    recipients = set(recipients) - {sender}
    if recipients:
        ident = uid()
        db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)', (ident, sender, text, None, None, now()))
        for recipient in recipients:
            db.execute('INSERT INTO message_recipients(message_id,member_id) VALUES (?,?)', (ident, recipient))


def create(db, member, body):
    permit(member, 'can_register')
    expire(db)
    shift_id, version = body.get('shift_id'), body.get('version')
    require(available(db, shift_id, member['id'], version), '请选择自己尚未结束、没有登记的有效班次', 409)
    require(not row(db, "SELECT id FROM swaps WHERE shift_id=? AND state='pending'", (shift_id,)), '该班次已有待确认的个人换班请求', 409)
    require(not row(db, "SELECT id FROM public_swaps WHERE shift_id=? AND state='open'", (shift_id,)), '该班次已经在公开求换班', 409)
    audience = body.get('mail_audience')
    if audience is not None:
        permit(member, 'can_manage_members')
        require(isinstance(audience, list) and 1 <= len(audience) <= 10 and all(isinstance(x, str) for x in audience) and len(set(audience)) == len(audience), '邮件范围无效')
        for recipient in audience:
            member_by_id(db, recipient)
    ident = uid()
    if audience is not None:
        db.execute('INSERT INTO settings VALUES (?,?)', ('public_swap_mail_audience:' + ident, js(audience)))
    db.execute('INSERT INTO public_swaps(id,requester_id,shift_id,shift_version,reason,created_at) VALUES (?,?,?,?,?,?)',
               (ident, member['id'], shift_id, version, text_field(body.get('reason', ''), 1000), now()))
    mailed = public_swap_mail.queue(db, ident, 'published')
    audit(db, member['id'], 'public_swap.create', dict(id=ident, shift_id=shift_id))
    return dict(id=ident, message=f'公开求换班已发布，{mailed} 封邮件通知已排队' if mailed else '已发布公开求换班，当前仅站内通知')


def open_post(db, ident):
    post = row(db, 'SELECT * FROM public_swaps WHERE id=?', (ident,))
    require(post and post['state'] == 'open', '公开换班已结束或不存在', 409)
    require(available(db, post['shift_id'], post['requester_id'], post['shift_version']), '原班次已改变或结束，不能继续换班', 409)
    return post


def offer(db, member, body):
    permit(member, 'can_register')
    post = open_post(db, body.get('id'))
    require(post['requester_id'] != member['id'], '不能回复自己发布的求换班', 403)
    target = body.get('target_shift_id') or None
    kind = body.get('reply_kind', 'exchange' if target else 'cover')
    require(kind in ('exchange', 'cover', 'undecided'), '请选择你想怎么换班')
    require((kind == 'exchange') == bool(target), '用自己的班交换时，请先选好班次；其他回复不要选择班次')
    version = body.get('target_version') if target else None
    message = text_field(body.get('message', ''), 1000)
    if target:
        require(available(db, target, member['id'], version), '对调班次已改变或不可用', 409)
    previous = row(db, "SELECT * FROM public_swap_offers WHERE public_id=? AND member_id=? AND state='active'", (post['id'], member['id']))
    replace_id = body.get('replace_offer_id')
    if previous:
        require(replace_id == previous['id'], '你已经回复过了，请刷新后点击“修改回复”', 409)
        require((kind, target, version, message) !=
                (previous['reply_kind'], previous['target_shift_id'], previous['target_version'], previous['message']),
                '回复没有变化，不需要重复发送', 409)
        db.execute("UPDATE public_swap_offers SET state='withdrawn' WHERE id=?", (previous['id'],))
    else:
        require(not replace_id, '该回复已变更，请刷新后再试', 409)
    ident = uid()
    db.execute('INSERT INTO public_swap_offers(id,public_id,member_id,target_shift_id,target_version,message,created_at,reply_kind) VALUES (?,?,?,?,?,?,?,?)',
               (ident, post['id'], member['id'], target, version, message, now(), kind))
    public_swap_mail.queue(db, post['id'], 'offer', ident)
    audit(db, member['id'], 'public_swap.offer', dict(id=ident, public_id=post['id']))
    return dict(id=ident)


def withdraw(db, member, body):
    proposal = row(db, 'SELECT * FROM public_swap_offers WHERE id=?', (body.get('id'),))
    require(proposal and proposal['member_id'] == member['id'], '只能撤回自己的回复', 403)
    post = row(db, 'SELECT * FROM public_swaps WHERE id=?', (proposal['public_id'],))
    require(post, '公开换班不存在', 404)
    require(post['state'] == 'open' and proposal['state'] == 'active', '该回复已结束，不能撤回', 409)
    db.execute("UPDATE public_swap_offers SET state='withdrawn' WHERE id=?", (proposal['id'],))
    workflow_mail.queue(db,'public',post['id'],'withdrawn',proposal['id'],[post['requester_id']],
        '公开换班 · 回复已撤回',member['name']+' 已撤回回复，原排班不变。',actor_id=member['id'])
    audit(db, member['id'], 'public_swap.withdraw', dict(id=proposal['id']))
    return dict(ok=True)


def cancel(db, member, body):
    post = row(db, 'SELECT * FROM public_swaps WHERE id=?', (body.get('id'),))
    require(post and post['requester_id'] == member['id'], '只能取消自己发布的换班', 403)
    require(post['state'] == 'open', '公开换班已结束', 409)
    db.execute("UPDATE public_swaps SET state='canceled',decided_at=? WHERE id=?", (now(), post['id']))
    workflow_mail.queue(db,'public',post['id'],'canceled','canceled',
        [m['id'] for m in rows(db,'SELECT id FROM members WHERE active=1 AND id!=?',(member['id'],))],
        '公开换班 · 公告已取消',member['name']+' 已取消公开求换班，原排班不变。',actor_id=member['id'])
    drop_mail_audience(db, post['id'])
    audit(db, member['id'], 'public_swap.cancel', dict(id=post['id']))
    return dict(ok=True)


def select(db, member, body):
    # Store.write holds BEGIN IMMEDIATE: selecting a winner and changing both
    # assignments form one transaction, including all version/conflict checks.
    from . import service
    proposal = row(db, 'SELECT * FROM public_swap_offers WHERE id=?', (body.get('offer_id'),))
    require(proposal, '回复不存在', 404)
    post = open_post(db, proposal['public_id'])
    require(post['requester_id'] == member['id'], '只有发起人可以同意换班', 403)
    require(proposal['state'] == 'active', '该回复已撤回或处理', 409)
    require(proposal['reply_kind'] != 'undecided', '对方尚未确定班次，请等待对方补选班次或明确选择仅代班', 409)
    recipient = member_by_id(db, proposal['member_id'])
    result = service.swap_create(db, member, dict(shift_id=post['shift_id'], version=post['shift_version'],
        recipient_id=recipient['id'], target_shift_id=proposal['target_shift_id'], target_version=proposal['target_version'],
        reason=post['reason']), public_selection=True)
    result = service.swap_decide(db, recipient, dict(id=result['id'], decision='accept')) | dict(swap_id=result['id'])
    require(result['state'] == 'accepted', '班次已不可用，请刷新后重新选择', 409)
    db.execute("UPDATE public_swaps SET state='matched',decided_at=?,swap_id=? WHERE id=?", (now(), result['swap_id'], post['id']))
    db.execute("UPDATE public_swap_offers SET state='not_selected' WHERE public_id=? AND state='active'", (post['id'],))
    db.execute("UPDATE public_swap_offers SET state='selected' WHERE id=?", (proposal['id'],))
    for item in rows(db, "SELECT id,state FROM public_swap_offers WHERE public_id=? AND state IN ('selected','not_selected')", (post['id'],)):
        public_swap_mail.queue(db, post['id'], item['state'], item['id'])
    audit(db, member['id'], 'public_swap.select', dict(id=post['id'], offer_id=proposal['id']))
    drop_mail_audience(db, post['id'])
    return result
