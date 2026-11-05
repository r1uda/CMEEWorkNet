import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3

ROOT = Path(__file__).resolve().parent.parent
CURRENT_SCHEMA_VERSION = 11
TZ = dt.timezone(dt.timedelta(hours=8))
NAMES = ['薛铖昊', '张梓涵', '李宥达', '温子轩', '李欣', '游晨箫', '孙梓添', '潘雨桐', '海本卓', '黄晋宜']
CAPS = ['can_register', 'can_manage_schedule', 'can_export', 'can_send_announcements', 'can_manage_members']
SLOTS = [dict(name='一二节', start='08:00', end='10:00'), dict(name='三四节', start='10:00', end='12:00'),
         dict(name='五六节', start='14:00', end='16:00'), dict(name='七八节', start='16:00', end='18:00')]


def load_env():
    path = ROOT / '.env'
    if path.exists():
        for line in path.read_text('utf-8-sig').splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def now():
    return dt.datetime.now(TZ).isoformat(timespec='seconds')


def uid():
    return secrets.token_hex(12)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def js(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class Problem(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


def require(condition, message, status=400):
    if not condition:
        raise Problem(message, status)


def valid_date(value):
    require(isinstance(value, str), '日期格式错误')
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        raise Problem('日期格式错误，请使用 YYYY-MM-DD')
    require(parsed.isoformat() == value and 2000 <= parsed.year <= 2100, '日期超出范围')
    return value


def text_field(value, maximum=10000):
    require(isinstance(value, str) and len(value) <= maximum, '文本格式错误或超过长度限制')
    return value.strip()


def slot_end(shift):
    return dt.datetime.fromisoformat(f"{shift['date']}T{SLOTS[shift['slot']]['end']}:00+08:00")


def sql_statements(script):
    """Split a .sql file into complete statements.

    Splits on real statement boundaries, not on lines: two statements on one line
    must become two, because db.execute() rejects a string holding more than one.
    Quotes, [identifiers], -- line comments and /* block comments */ are skipped,
    and each candidate is judged with sqlite3.complete_statement(text + ';'), which keeps
    a CREATE TRIGGER body whole until its final END; and never splits inside it.
    """
    statements = []
    buffer = []
    quote = None
    in_line_comment = in_block_comment = False
    index, length = 0, len(script)
    while index < length:
        char = script[index]
        following = script[index + 1] if index + 1 < length else ''
        if in_line_comment:
            buffer.append(char)
            if char == '\n':
                in_line_comment = False
            index += 1
            continue
        if in_block_comment:
            buffer.append(char)
            if char == '*' and following == '/':
                buffer.append(following)
                index += 2
                in_block_comment = False
                continue
            index += 1
            continue
        if quote:
            buffer.append(char)
            if char == quote:
                if following == quote:
                    buffer.append(following)
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char == '-' and following == '-':
            buffer.extend((char, following))
            index += 2
            in_line_comment = True
            continue
        if char == '/' and following == '*':
            buffer.extend((char, following))
            index += 2
            in_block_comment = True
            continue
        if char in ("'", '"', '`'):
            quote = char
            buffer.append(char)
            index += 1
            continue
        if char == '[':
            quote = ']'
            buffer.append(char)
            index += 1
            continue
        if char == ';':
            text = ''.join(buffer)
            # Judge the candidate WITH its terminator: inside a CREATE TRIGGER body the
            # text is not yet complete, so every inner semicolon keeps accumulating and
            # the trigger ends exactly at its END;. No keyword matching, so a table
            # called trigger_log or a comment mentioning trigger changes nothing.
            if not sqlite3.complete_statement(text + ';'):
                buffer.append(char)
                index += 1
                continue
            statement = text.strip()
            if statement:
                statements.append(statement)
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    tail = ''.join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


def apply_migrations(db, folder):
    """Run pending migrations inside the caller's transaction.

    executescript() would implicitly commit the surrounding transaction, so a failure
    halfway left partial DDL behind. Executing statement by statement keeps the
    migration and its schema_version row atomic: either both land or neither does.
    """
    current = db.execute('SELECT MAX(version) FROM schema_version').fetchone()[0]
    require(current <= CURRENT_SCHEMA_VERSION, '数据库版本高于当前应用，请使用对应版本的代码')
    for migration in sorted(Path(folder).glob('*.sql')):
        version = int(migration.name.split('_', 1)[0])
        if db.execute('SELECT 1 FROM schema_version WHERE version=?', (version,)).fetchone():
            continue
        for statement in sql_statements(migration.read_text('utf-8')):
            db.execute(statement)
        db.execute('INSERT INTO schema_version VALUES (?)', (version,))


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=25)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=25000')
        return db

    @contextlib.contextmanager
    def read(self):
        db = self.connect()
        try:
            yield db
        finally:
            db.close()

    @contextlib.contextmanager
    def write(self):
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def init(self):
        with self.read() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript((ROOT / 'app/schema.sql').read_text('utf-8'))
            db.commit()
        with self.write() as db:
            apply_migrations(db, ROOT / 'app/migrations')
            for i, name in enumerate(NAMES):
                db.execute('INSERT OR IGNORE INTO members(id,name) VALUES (?,?)', (f'm{i+1:02}', name))
            for key, value in [('activation_date', None), ('share_records', False), ('share_schedule', False), ('ai_consent', False), ('escalation_recipient', None)]:
                db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', (key, js(value)))


def rows(db, sql, params=()):
    return [dict(r) for r in db.execute(sql, params).fetchall()]


def row(db, sql, params=()):
    r = db.execute(sql, params).fetchone()
    return dict(r) if r else None


def setting(db, key):
    result = row(db, 'SELECT value FROM settings WHERE key=?', (key,))
    return json.loads(result['value']) if result else None


AUDIT_DETAIL_LIMIT = 4000


def audit(db, actor, action, detail):
    # A few endpoints pass whole request bodies here; keep the table small and readable.
    text = js(detail)
    if len(text) > AUDIT_DETAIL_LIMIT:
        text = js(dict(truncated=True, size=len(text), preview=text[:AUDIT_DETAIL_LIMIT - 200]))
    db.execute('INSERT INTO audit VALUES (?,?,?,?,?)', (uid(), actor, action, text, now()))


def can(member, capability):
    return capability in json.loads(member['caps'])


def permit(member, capability):
    require(can(member, capability), '没有此操作权限', 403)


def member_by_id(db, member_id):
    result = row(db, 'SELECT * FROM members WHERE id=? AND active=1', (member_id,))
    require(result, '成员不存在或已停用', 404)
    return result


def can_read_record(db, member, record):
    return record['author_id'] == member['id'] or (record['state'] == 'submitted' and (setting(db, 'share_records') or can(member,'can_manage_members')))


def record_view(db, record):
    record = dict(record)
    record['tasks'] = rows(db, 'SELECT t.*,m.name AS assignee_name FROM tasks t JOIN members m ON m.id=t.assignee_id LEFT JOIN task_positions p ON p.task_id=t.id WHERE registration_id=? AND t.active=1 ORDER BY COALESCE(p.position,t.rowid),t.rowid', (record['id'],))
    for task in record['tasks']:
        task['progress'] = rows(db, 'SELECT p.*,m.name AS author_name FROM progress p JOIN members m ON m.id=p.author_id WHERE task_id=? ORDER BY p.created_at', (task['id'],))
    record['work_mode'] = 'worked' if record['tasks'] else 'no_work'
    return record


def schedule_baseline(db):
    return digest(js(rows(db, 'SELECT id,date,slot,member_id,version,active FROM shifts ORDER BY id')))
