"""Private runtime storage, kept out of source checkouts."""
import os
from pathlib import Path
import secrets
import stat


def default_directory():
    override = os.environ.get('CLAUDE_CHATGPT_STATE_DIR')
    if override:
        return Path(override).expanduser()
    base = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state')
    return base / 'claude-chatgpt-bridge'


def ensure_private_directory(directory):
    path = Path(directory).expanduser().absolute()
    # Prevent accidental commits even if somebody chooses the project as state.
    # Empty .git directories can also be sandbox guards, not real checkouts.
    def checkout(parent):
        marker = parent / '.git'
        return (marker.is_dir() and (marker / 'HEAD').is_file()) or marker.is_file()
    if any(checkout(p) for p in (path, *path.parents)):
        raise ValueError('Runtime state must be outside a Git checkout.')
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError('Runtime state must not use symbolic links.')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if info.st_uid != os.getuid():
        raise ValueError('Runtime state must belong to the current user.')
    # Refuse unsafe existing directories instead of changing unrelated paths.
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError('Runtime state needs private permissions (chmod 700).')
    for child in path.iterdir():
        if child.is_symlink():
            raise ValueError('Runtime state must not contain symbolic links.')
        info = child.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError('Runtime files must belong to you and be private (chmod 600).')
    return path


def initialize(directory):
    path = ensure_private_directory(directory)
    key_path = path / 'bridge.key'
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if len(key_path.read_text().strip()) < 32:
            raise ValueError('Existing bridge credential is invalid.')
    else:
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_hex(32) + '\n')
    return path
