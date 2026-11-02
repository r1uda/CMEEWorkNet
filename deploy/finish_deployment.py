"""Remove this deployment's temporary copies and public key, preserving cloud access."""
from pathlib import Path

authorized = Path('/home/admin/.ssh/authorized_keys')
key = 'AAAAC3NzaC1lZDI1NTE5AAAAIMlzu6z3Mh/zFLSNsNuKDVfciLqmNTZm2a/jkZHj9b6q'
lines = authorized.read_text().splitlines()
kept = [line for line in lines if key not in line.split()]
assert len(lines) - len(kept) == 1, 'Expected exactly one temporary deployment key'
assert any(line.strip() for line in kept), 'Preserve existing cloud login key'
authorized.write_text('\n'.join(kept) + '\n')
authorized.chmod(0o600)
stage = Path('/home/admin/cmee-upload')
for name in ('code.tar.gz', 'workbench.sqlite3', 'production.env', 'member_contacts_pending.json',
             'quill.apk', 'verified-cloud-backup.sqlite3', 'install.sh', 'configure-web.sh',
             'verify_connections.py', 'finish_deployment.py'):
    path = stage / name
    if path.is_file():
        path.unlink()
print('temporary_key_revoked; upload_copies_removed; cloud_login_key_preserved')
