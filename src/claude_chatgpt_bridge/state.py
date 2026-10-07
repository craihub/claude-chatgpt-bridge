"""Private runtime storage, kept out of source checkouts."""
import os
from pathlib import Path
import secrets
import stat

from .platforms import app_directory, check_windows_private, is_link, protect_windows


def default_directory():
    override = os.environ.get('CLAUDE_CHATGPT_STATE_DIR')
    if override:
        return Path(override).expanduser()
    return app_directory()


def ensure_private_directory(directory):
    path = Path(directory).expanduser().absolute()
    # Prevent accidental commits even if somebody chooses the project as state.
    # Empty .git directories can also be sandbox guards, not real checkouts.
    def checkout(parent):
        marker = parent / '.git'
        return (marker.is_dir() and (marker / 'HEAD').is_file()) or marker.is_file()
    if any(checkout(p) for p in (path, *path.parents)):
        raise ValueError('Runtime state must be outside a Git checkout.')
    # macOS exposes /tmp and /var through OS-owned aliases; resolve ancestors,
    # while still rejecting a linked state directory or linked state files.
    if is_link(path):
        raise ValueError('Runtime state must not use symbolic links.')
    path = path.parent.resolve() / path.name
    if any(checkout(p) for p in (path, *path.parents)):
        raise ValueError('Runtime state must be outside a Git checkout.')
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == 'nt':
        if created:
            protect_windows(path)
        check_windows_private(path)
        for child in path.iterdir():
            if is_link(child):
                raise ValueError('Runtime state must not contain links or reparse points.')
            check_windows_private(child)
        return path
    info = path.stat()
    if info.st_uid != os.getuid():
        raise ValueError('Runtime state must belong to the current user.')
    # Refuse unsafe existing directories instead of changing unrelated paths.
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError('Runtime state needs private permissions (chmod 700).')
    for child in path.iterdir():
        if is_link(child):
            raise ValueError('Runtime state must not contain symbolic links.')
        info = child.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError('Runtime files must belong to you and be private (chmod 600).')
    return path


def initialize(directory):
    path = ensure_private_directory(directory)
    key_path = path / 'bridge.key'
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except FileExistsError:
        if len(key_path.read_text().strip()) < 32:
            raise ValueError('Existing bridge credential is invalid.')
    else:
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_hex(32) + '\n')
    return path
