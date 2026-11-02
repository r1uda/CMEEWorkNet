"""Deployment probe. No messages sent and no credentials printed."""
import json
import os
import smtplib
import sqlite3
import ssl
import urllib.request
from app.core import load_env

load_env()
db = sqlite3.connect('file:/opt/cmee/data/workbench.sqlite3?mode=ro', uri=True)
print('integrity', db.execute('pragma integrity_check').fetchone()[0])
for table in ('members', 'shifts', 'registrations', 'tasks'):
    print(table, db.execute('select count(*) from ' + table).fetchone()[0])
print('bound_emails', db.execute("select count(*) from members where email is not null and email != ''").fetchone()[0])
with smtplib.SMTP_SSL(os.environ['SMTP_HOST'], int(os.getenv('SMTP_PORT', '465')), timeout=15, context=ssl.create_default_context()) as client:
    client.login(os.environ['SMTP_USER'], os.environ['SMTP_PASSWORD'])
    print('smtp_auth', 'ok')
payload = dict(model=os.environ['AI_MODEL'], messages=[dict(role='user', content='Reply with OK only.')], max_tokens=8)
request = urllib.request.Request(os.environ['AI_BASE_URL'].rstrip('/') + '/chat/completions', data=json.dumps(payload).encode(), headers={'Content-Type':'application/json', 'Authorization':'Bearer ' + os.environ['AI_API_KEY']})
with urllib.request.urlopen(request, timeout=35) as response:
    result = json.load(response)
    print('ai_response', bool(result.get('choices')))
