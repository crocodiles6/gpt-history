#!/usr/bin/env python3
"""Check Git publication candidates without printing matched private values.

This is a targeted publication guard, not a general-purpose secret scanner.
Review prose and images manually as well.
"""
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

PRIVATE_DIRS = {'.validation', '.private', '.codex', '.claude', '.chatgpt-source',
                '.local-edits', '.obsidian', 'AI-Chat-Backup', 'sample-output',
                'exports', 'backups', 'sessions', 'archived_sessions', 'attachments'}
PRIVATE_FILES = ('*.jsonl', '*.jsonl.zst', '*.sqlite*', '*.db', '*.db-*',
                 '*-export-manifest.json', 'export-report.json', 'inventory.json',
                 'auth.json', 'cookies*.json', '*.pem', '*.key', '*.p12', '*.pfx', '.export.lock')
PATTERNS = {
    'personal-home-path': re.compile(r'/(?:Users|home)/[^/\s"\'`<>]+/'),
    'conversation-uuid': re.compile(r'\b[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\b', re.I),
    'openai-key': re.compile(r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b'),
    'github-token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
    'aws-access-key': re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'jwt': re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b'),
    'email-address': re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'),
}


def publication_files(root):
    result = subprocess.run(['git', '-C', str(root), 'ls-files', '--cached', '--others',
                             '--exclude-standard', '-z'], check=True, capture_output=True)
    return sorted(set(result.stdout.decode().split('\0')) - {''})


def scan_file(root, name):
    path = root / name
    issues = []
    if path.is_symlink():
        return [(name, 0, 'symlink-needs-review')]
    if not path.exists():
        return [(name, 0, 'tracked-file-missing-stage-its-deletion')]
    parts = Path(name).parts
    if (PRIVATE_DIRS.intersection(parts)
            or any(fnmatch.fnmatch(path.name, p) for p in PRIVATE_FILES)
            or (path.name.startswith('.env') and path.name != '.env.example')):
        issues.append((name, 0, 'private-artifact'))
    try:
        data = path.read_bytes()
    except OSError:
        return issues + [(name, 0, 'unreadable-file')]
    try:
        content = data.decode('utf-8')
    except UnicodeDecodeError:
        # Only reviewed README artwork is accepted as a binary candidate.
        if name not in ('docs/assets/overview.png', 'docs/assets/obsidian-redacted.png'):
            issues.append((name, 0, 'unreviewed-binary'))
        return issues
    for number, line in enumerate(content.splitlines(), 1):
        for label, pattern in PATTERNS.items():
            if pattern.search(line):
                issues.append((name, number, label))
    return issues


def main():
    root = Path(__file__).resolve().parents[1]
    try:
        files = publication_files(root)
        issues = [issue for name in files for issue in scan_file(root, name)]
    except (OSError, subprocess.CalledProcessError) as error:
        print('Publication check failed: ' + type(error).__name__, file=sys.stderr)
        return 2
    for name, line, rule in issues:
        print('{}:{}: {}'.format(name, line, rule))
    print('{} Git candidate files; {} findings.'.format(len(files), len(issues)))
    print('Scope: current working-tree candidates. Review image pixels and prose manually.')
    return 1 if issues else 0


if __name__ == '__main__':
    sys.exit(main())
