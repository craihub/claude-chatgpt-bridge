#!/usr/bin/env python3
"""Agent-run installer. Python 3.11+ is the only bootstrap prerequisite."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import venv

ROOT = Path(__file__).resolve().parent


def is_link(path):
    return path.is_symlink() or (os.name == 'nt' and path.exists()
        and bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT))


def python_environment():
    return {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}


def runtime_path():
    home = Path.home()
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA') or home / 'AppData/Local') / 'ClaudeChatGPTBridge/runtime'
    if sys.platform == 'darwin':
        return home / 'Library/Application Support/ClaudeChatGPTBridge/runtime'
    if sys.platform.startswith('linux'):
        return Path(os.environ.get('XDG_DATA_HOME') or home / '.local/share') / 'claude-chatgpt-bridge-runtime'
    raise RuntimeError('This installer targets Linux, Windows and macOS.')


def install(runtime):
    # This directory contains code, never OAuth credentials. The installed
    # package creates protected state (including a Windows ACL) before login.
    if is_link(runtime):
        raise RuntimeError('The runtime directory must not be a symbolic link.')
    runtime = runtime.parent.resolve() / runtime.name
    if any((p / '.git/HEAD').exists() or (p / '.git').is_file() for p in (runtime, *runtime.parents)):
        raise RuntimeError('Install the runtime outside Git checkouts.')
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != 'nt':
        info = runtime.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeError('The dedicated runtime must belong to you with private permissions (chmod 700).')
    env_dir = runtime / 'venv'
    if is_link(env_dir):
        raise RuntimeError('The runtime venv must not be a link or reparse point.')
    if env_dir.exists() and not (env_dir / 'pyvenv.cfg').exists():
        raise RuntimeError('The target venv path already contains unrelated files.')
    python = env_dir / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    paths = [line.strip() for line in (ROOT / 'PUBLIC_FILES.txt').read_text().splitlines()
             if line.strip() and not line.startswith('#')]
    if len(paths) != len(set(paths)):
        raise RuntimeError('Invalid public source manifest.')
    digest = hashlib.sha256()
    for name in paths:
        path = ROOT / name
        if (Path(name).is_absolute() or '..' in Path(name).parts or is_link(path)
                or not path.is_file() or not path.resolve().is_relative_to(ROOT)):
            raise RuntimeError('Invalid public source manifest.')
        digest.update(name.encode() + b'\0' + path.read_bytes())
    revision = digest.hexdigest()
    receipt = runtime / 'bootstrap.json'
    if is_link(receipt):
        raise RuntimeError('The runtime receipt must not be a link or reparse point.')
    old = json.loads(receipt.read_text()) if receipt.exists() else {}
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(env_dir)
    if old.get('source_sha256') != revision:
        # A fresh snapshot avoids following a stale or edited destination tree.
        source = Path(tempfile.mkdtemp(prefix='source-' + revision[:16] + '-', dir=runtime))
        for name in paths:
            destination = source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)
        result = subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
                                 '--upgrade', str(source)], env=python_environment())
        if result.returncode:
            raise RuntimeError('Dependency installation failed. Resolve the Python/package error and rerun.')
        fd, temporary = tempfile.mkstemp(prefix='bootstrap-', suffix='.json', dir=runtime)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(json.dumps({'source_sha256': revision}) + '\n')
            os.replace(temporary, receipt)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return python


def main():
    if sys.version_info < (3, 11):
        raise SystemExit('Python 3.11+ is required. The installation agent should install it from an official source.')
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default='setup', choices=('setup', 'continue', 'doctor', 'undo'))
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--model')
    parser.add_argument('--no-login', action='store_true')
    parser.add_argument('--previous-config', type=Path)
    parser.add_argument('--tool-search', action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument('--desktop-restored', action='store_true')
    args = parser.parse_args()
    try:
        runtime = runtime_path()
        if args.action in ('setup', 'continue'):
            python = install(runtime)
        else:
            python = runtime / ('venv/Scripts/python.exe' if os.name == 'nt' else 'venv/bin/python')
            if not python.exists():
                raise RuntimeError('Runtime is not installed. Run setup first.')
        command = [str(python), '-m', 'claude_chatgpt_bridge']
        if args.state_dir:
            command += ['--state-dir', str(args.state_dir.expanduser().absolute())]
        command += ['desktop', 'setup' if args.action == 'continue' else args.action]
        if args.action in ('setup', 'continue'):
            if args.model:
                command += ['--model', args.model]
            if args.no_login:
                command += ['--no-login']
            if args.previous_config:
                command += ['--previous-config', str(args.previous_config.expanduser().absolute())]
            if args.tool_search is not None:
                command.append('--tool-search' if args.tool_search else '--no-tool-search')
        if args.action == 'undo' and args.desktop_restored:
            command.append('--desktop-restored')
        raise SystemExit(subprocess.call(command, cwd=runtime, env=python_environment()))
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error) if isinstance(error, RuntimeError) else 'Installer could not access a required file. Check the dedicated runtime directory.', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
