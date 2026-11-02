"""Build an explicit, checked source archive; never publish or send invitations."""
from pathlib import Path
import hashlib
import json
import re
import zipfile

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / 'output' / 'github'


def source_files():
    paths = [ROOT / name for name in (
        '.gitignore', '.dockerignore', '.env.example', 'README.md',
        'Dockerfile', 'compose.yaml', 'docs/协作开发.md',
        'scripts/prepare_github_source.py',
        '启动演示.cmd', '启动正式服务.cmd', '启动AI联调.cmd',
    )]
    for folder in ('app', 'tests', 'deploy'):
        for path in (ROOT / folder).rglob('*'):
            if not path.is_file() or '__pycache__' in path.parts:
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel in ('app/mail_smoke.py', 'deploy/README.md',
                       'deploy/finish_deployment.py', 'deploy/verify_connections.py'):
                continue
            if path.suffix.lower() not in {'.py', '.sql', '.js', '.cjs', '.css', '.html', '.svg', '.png', '.webmanifest', '.sh', '.service', '.timer'} and path.name != 'Caddyfile':
                raise RuntimeError(f'Unexpected source file requires review: {rel}')
            paths.append(path)
    return sorted(set(paths))


def main():
    paths = source_files()
    # Compare against local secret values without printing or copying them.
    secret_values = []
    env = ROOT / '.env'
    if env.exists():
        for line in env.read_text('utf-8-sig').splitlines():
            if line.lstrip().startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            value = value.strip().strip('\"').strip("'")
            if re.search(r'KEY|PASSWORD|SECRET|TOKEN', key, re.I) and len(value) >= 8:
                secret_values.append(value.encode())
    for path in paths:
        content = path.read_bytes()
        if any(value in content for value in secret_values):
            raise RuntimeError(f'Local secret found in {path.relative_to(ROOT)}')
        if path.suffix.lower() != '.png' and re.search(rb'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----|(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{24,}', content):
            raise RuntimeError(f'Credential pattern found in {path.relative_to(ROOT)}')
    DEST.mkdir(parents=True, exist_ok=True)
    archive = DEST / 'cmee-work-source.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for path in paths:
            z.write(path, path.relative_to(ROOT).as_posix())
    manifest = dict(files=[p.relative_to(ROOT).as_posix() for p in paths],
                    count=len(paths), bytes=archive.stat().st_size,
                    sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                    local_secret_scan='passed', published=False,
                    planned_repo='r1uda/cmee-work', collaborator='HwangXP')
    (DEST / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps({k: v for k, v in manifest.items() if k != 'files'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
