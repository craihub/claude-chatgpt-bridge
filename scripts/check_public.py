"""Scan only public source/history/artifacts. Never reads runtime credentials."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    'private-key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'provider-token': re.compile(rb'\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{24,}|github_pat_[A-Za-z0-9_]{30,})\b'),
    'aws-access-id': re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'jwt': re.compile(rb'\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b'),
    'personal-home-path': re.compile(rb'/(?:home|Users)/[A-Za-z0-9_.-]+/|[A-Za-z]:\\Users\\[^\\]+\\'),
    'issued-registration': re.compile(rb'\boaiapp_[A-Za-z0-9_-]{12,}\b'),
    'fixed-host-id': re.compile(rb'urn:uuid:[0-9a-fA-F-]{36}'),
}
EMAIL = re.compile(rb'\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b')
GENERATED = {'PKG-INFO', 'setup.cfg', 'dependency_links.txt', 'entry_points.txt',
             'requires.txt', 'SOURCES.txt', 'top_level.txt', 'METADATA', 'WHEEL', 'RECORD', 'LICENSE'}


def findings(label, data):
    result = [{'file': label, 'rule': rule} for rule, pattern in PATTERNS.items() if pattern.search(data)]
    if any(not m.group(1).endswith((b'.invalid', b'.example')) and
           m.group(1) not in (b'example.com', b'example.org', b'example.net', b'users.noreply.github.com')
           for m in EMAIL.finditer(data)):
        result.append({'file': label, 'rule': 'personal-email'})
    return result


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args], stderr=subprocess.DEVNULL)


def public_files():
    paths = [line.strip() for line in (ROOT / 'PUBLIC_FILES.txt').read_text().splitlines()
             if line.strip() and not line.startswith('#')]
    if len(paths) != len(set(paths)) or any(Path(p).is_absolute() or '..' in Path(p).parts for p in paths):
        raise ValueError('Invalid public-file manifest.')
    return set(paths)


def scan(artifacts=None):
    allowed = public_files()
    errors, objects, artifact_files = [], 0, 0
    for name in sorted(allowed):
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            errors.append({'file': name, 'rule': 'missing-or-symlink'})
            continue
        errors.extend(findings(name, path.read_bytes()))
    if (ROOT / '.git').exists():
        tracked = set(git('ls-files', '-z').decode().strip('\0').split('\0')) - {''}
        for name in sorted(tracked - allowed):
            errors.append({'file': name, 'rule': 'not-allowlisted'})
        # Scan blobs plus commit/tag messages and identities, even unreachable objects.
        inventory = git('cat-file', '--batch-all-objects', '--batch-check=%(objectname) %(objecttype)').decode()
        for row in inventory.splitlines():
            oid, kind = row.split()
            objects += 1
            if kind in ('blob', 'commit', 'tag'):
                errors.extend(findings('git-object:' + oid[:12], git('cat-file', kind, oid)))
        for name in git('ls-files', '--others', '--exclude-standard', '-z').decode().split('\0'):
            if name and name not in allowed:
                errors.append({'file': name, 'rule': 'unreviewed-untracked-file'})
    if artifacts:
        for archive in sorted(Path(artifacts).iterdir()):
            entries = []
            if archive.suffix == '.whl':
                with zipfile.ZipFile(archive) as z:
                    entries = [(n, z.read(n)) for n in z.namelist() if not n.endswith('/')]
            elif archive.name.endswith('.tar.gz'):
                with tarfile.open(archive) as t:
                    for member in t.getmembers():
                        if member.uname or member.gname or member.uid or member.gid:
                            errors.append({'file': archive.name, 'rule': 'personal-archive-owner'})
                        if member.issym() or member.islnk():
                            errors.append({'file': archive.name, 'rule': 'archive-link'})
                        if member.isfile():
                            entries.append((member.name, t.extractfile(member).read()))
            else:
                errors.append({'file': archive.name, 'rule': 'unexpected-artifact'})
            for name, data in entries:
                artifact_files += 1
                errors.extend(findings(archive.name + ':' + name, data))
                path = Path(name)
                if path.is_absolute() or '..' in path.parts:
                    errors.append({'file': name, 'rule': 'unsafe-archive-path'})
                relative = '/'.join(path.parts[1:])
                source_name = 'src/' + name
                generated = path.name in GENERATED and (
                    path.name in ('PKG-INFO', 'setup.cfg') or
                    any(p.endswith(('.dist-info', '.egg-info')) for p in path.parts))
                if name not in allowed and relative not in allowed and source_name not in allowed and not generated:
                    errors.append({'file': name, 'rule': 'unexpected-archive-member'})
    return {'public_files': len(allowed), 'git_objects': objects,
            'artifact_files': artifact_files, 'findings': errors}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', type=Path)
    args = parser.parse_args()
    result = scan(args.artifacts)
    print(json.dumps(result, indent=2))
    raise SystemExit(bool(result['findings']))
