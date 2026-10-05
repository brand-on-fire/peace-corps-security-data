#!/usr/bin/env python3
"""Audit the public-only bundle; publish only docs/data changes from GitHub CI.

The ephemeral GITHUB_TOKEN is passed to git through process environment only.
It is never printed, written to git config, or saved to a credential file.
"""
import argparse
import base64
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

MAX_TREE_BYTES = 500_000_000
MAX_REMOTE_KIB = 750_000
ALLOWED_TOP = {'.github', '.gitignore', 'README.md', 'NOTICE.md', 'scripts', 'config', 'docs', '.git'}
PATTERNS = (rb'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----',
            rb'gh[pousr]_[A-Za-z0-9]{30,}', rb'github_pat_[A-Za-z0-9_]{30,}',
            rb'/Users/', rb'creds (?:run|view|set|edit)', rb'CLOUDFLARE_API_TOKEN')


def command(*args, env=None):
    return subprocess.run(args, env=env, check=True, capture_output=True, text=True).stdout.strip()


def audit(root, remote=True):
    files, total = [], 0
    for path in root.rglob('*'):
        rel = path.relative_to(root)
        if rel.parts[0] == '.git':
            continue
        if path.is_symlink():
            raise ValueError('Symlinks are not permitted in public bundle')
        if rel.parts[0] not in ALLOWED_TOP:
            raise ValueError('Unapproved public bundle path: '+str(rel))
        if not path.is_file():
            continue
        if any(x in path.name.casefold() for x in ('.env', '.dev.vars', '.pem', '.key', 'credential', 'secret')):
            raise ValueError('Credential-like filename rejected: '+str(rel))
        raw = path.read_bytes()
        if len(raw) > 25_000_000:
            raise ValueError('Public file exceeds 25MB; no partial publication permitted: '+str(rel))
        # The scanner itself contains patterns, not credential values.
        if rel != Path('scripts/public_data_publish.py') and any(re.search(p, raw) for p in PATTERNS):
            raise ValueError('Public-content scan failed: '+str(rel))
        total += len(raw)
        files.append(str(rel))
    if total > MAX_TREE_BYTES:
        raise ValueError('500MB public-tree ceiling reached; stop rather than delete data or buy storage')
    policy = json.loads((root/'config/collection-policy.json').read_bytes())
    for source in policy['sources']:
        if source['mode'] not in ('official','discovery') or source['cadenceMinutes'] < 15:
            raise ValueError('Collection policy is outside reviewed scope')
    remote_size = None
    if remote:
        repository = os.environ.get('GITHUB_REPOSITORY', '')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ValueError('Missing exact GitHub repository context')
        headers = {'Accept':'application/vnd.github+json','User-Agent':'PublicPolicyDataPublisher/1.0'}
        token = os.environ.get('GITHUB_TOKEN')
        if token:
            headers['Authorization'] = 'Bearer '+token
        request = urllib.request.Request('https://api.github.com/repos/'+repository, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            metadata = json.load(response)
        if metadata.get('private') is not False or metadata.get('visibility') != 'public':
            raise ValueError('Repository must remain public; no private-runner quota is permitted')
        remote_size = metadata['size']
        if remote_size >= MAX_REMOTE_KIB:
            raise ValueError('750MB repository-history ceiling reached; stop without deleting history or buying storage')
    return dict(files=len(files), treeBytes=total, remoteRepositoryKiB=remote_size, passed=True)


def publish(root):
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('Publishing only runs inside its audited GitHub Actions workflow')
    audit(root)
    changed = command('git','status','--porcelain','--untracked-files=all').splitlines()
    if any(not line[3:].startswith('docs/data/') for line in changed):
        raise ValueError('Collector modified something outside public docs/data; publication refused')
    if not changed:
        print(json.dumps(dict(published=False, reason='No changed data')))
        return
    command('git','add','--','docs/data')
    command('git','-c','user.name=github-actions[bot]', '-c',
            'user.email=41898282+github-actions[bot]@users.noreply.github.com',
            'commit','-m','Refresh public source observations and health')
    token = os.environ.get('GITHUB_TOKEN')
    if not token:
        raise ValueError('Missing ephemeral repository token')
    environment = dict(os.environ)
    environment.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
                       GIT_CONFIG_VALUE_0='AUTHORIZATION: basic '+base64.b64encode(('x-access-token:'+token).encode()).decode())
    # Fixed GitHub origin from checkout, no force push and no recursive triggers.
    command('git','push','origin','HEAD:'+os.environ['GITHUB_REF_NAME'], env=environment)
    print(json.dumps(dict(published=True, commit=command('git','rev-parse','HEAD'))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--audit', action='store_true')
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--local-audit', action='store_true')
    args = parser.parse_args()
    try:
        if args.publish:
            publish(Path.cwd())
        else:
            print(json.dumps(audit(Path.cwd(), remote=not args.local_audit)))
    except Exception as error:
        # Never print subprocess commands/environment or authenticated HTTP objects.
        print('Public-data operation refused: '+type(error).__name__+': '+str(error))
        raise SystemExit(1)
