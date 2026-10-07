"""Build reviewed source, remove machine-specific tar metadata, then scan."""
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

from check_public import ROOT, public_files, scan


def main():
    report = scan()
    if report['findings']:
        raise SystemExit('Source scan failed. Run scripts/check_public.py for rule names.')
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='bridge-release-') as tmp:
        source = Path(tmp) / 'source'
        source.mkdir()
        # Never give the build backend private or unreviewed local files.
        for name in public_files():
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes())
        # Windows console launchers contain ZIP files, whose timestamps must
        # be 1980 or later. A fixed second day also tolerates timezone offsets.
        env = dict(os.environ, SOURCE_DATE_EPOCH='315619200')
        subprocess.run([sys.executable, '-m', 'build', '--outdir', str(output), str(source)],
                       env=env, check=True)
    for archive in output.glob('*.tar.gz'):
        entries = []
        with tarfile.open(archive) as original:
            for member in original.getmembers():
                if member.issym() or member.islnk():
                    raise SystemExit('Unexpected link in release archive.')
                data = original.extractfile(member).read() if member.isfile() else None
                member.uid = member.gid = 0
                member.uname = member.gname = ''
                member.mtime = 0
                member.pax_headers = {}
                entries.append((member, data))
        # Tar headers otherwise expose the build user's local login name.
        with tempfile.TemporaryDirectory(prefix='bridge-archive-') as tmp:
            clean = Path(tmp) / archive.name
            with tarfile.open(clean, 'w:gz', format=tarfile.PAX_FORMAT) as dest:
                for member, data in entries:
                    dest.addfile(member, io.BytesIO(data) if data is not None else None)
            archive.write_bytes(clean.read_bytes())
    result = scan(output)
    if result['findings']:
        raise SystemExit('Artifact scan failed. Run scripts/check_public.py --artifacts dist.')
    print(f"Release verified: {result['artifact_files']} archive files scanned.")


if __name__ == '__main__':
    main()
