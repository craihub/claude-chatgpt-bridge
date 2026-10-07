"""Platform paths and Windows ACLs. Never relax existing storage permissions."""
import os
from pathlib import Path
import stat
import sys


def platform_name():
    if sys.platform == 'win32':
        return 'windows'
    if sys.platform == 'darwin':
        return 'macos'
    if sys.platform.startswith('linux'):
        return 'linux'
    raise RuntimeError('Supported targets are Linux, macOS and Windows.')


def app_directory(kind='state', *, system=None, home=None, environ=None):
    system = system or platform_name()
    home = Path(home) if home else Path.home()
    env = os.environ if environ is None else environ
    if system == 'windows':
        base = Path(env.get('LOCALAPPDATA') or home / 'AppData/Local')
        return base / 'ClaudeChatGPTBridge' / kind
    if system == 'macos':
        return home / 'Library/Application Support/ClaudeChatGPTBridge' / kind
    variable, fallback = ('XDG_STATE_HOME', '.local/state') if kind == 'state' else ('XDG_DATA_HOME', '.local/share')
    return Path(env.get(variable) or home / fallback) / ('claude-chatgpt-bridge' if kind == 'state' else 'claude-chatgpt-bridge-runtime')


def is_link(path):
    if path.is_symlink():
        return True
    if os.name == 'nt' and path.exists():
        return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    return False


def windows_identity():
    import win32api
    import win32con
    import win32security
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()


def protect_windows(path):
    """Apply an owner + SYSTEM protected DACL to a newly created directory."""
    import win32security as security
    user = windows_identity()
    system = security.CreateWellKnownSid(security.WinLocalSystemSid, None)
    acl = security.ACL()
    flags = security.OBJECT_INHERIT_ACE | security.CONTAINER_INHERIT_ACE
    for sid in (user, system):
        acl.AddAccessAllowedAceEx(security.ACL_REVISION, flags, 0x1F01FF, sid)
    security.SetNamedSecurityInfo(str(path), security.SE_FILE_OBJECT,
        security.DACL_SECURITY_INFORMATION | security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, acl, None)


def check_windows_private(path):
    import win32security as security
    descriptor = security.GetNamedSecurityInfo(str(path), security.SE_FILE_OBJECT,
        security.OWNER_SECURITY_INFORMATION | security.DACL_SECURITY_INFORMATION)
    user = windows_identity()
    if descriptor.GetSecurityDescriptorOwner() != user:
        raise ValueError('Runtime storage must belong to the current user.')
    acl = descriptor.GetSecurityDescriptorDacl()
    if acl is None:
        raise ValueError('Runtime storage must have a private Windows ACL.')
    allowed = {security.ConvertSidToStringSid(user),
               security.ConvertSidToStringSid(security.CreateWellKnownSid(security.WinLocalSystemSid, None)),
               security.ConvertSidToStringSid(security.CreateWellKnownSid(security.WinBuiltinAdministratorsSid, None))}
    user_allowed = False
    for index in range(acl.GetAceCount()):
        ace = acl.GetAce(index)
        if ace[0][0] == security.ACCESS_ALLOWED_ACE_TYPE:
            if security.ConvertSidToStringSid(ace[2]) not in allowed:
                raise ValueError('Runtime storage grants access to another Windows account.')
            user_allowed |= ace[2] == user
        elif ace[0][0] != security.ACCESS_DENIED_ACE_TYPE:
            raise ValueError('Runtime storage uses an unsupported Windows ACL.')
    if not user_allowed:
        raise ValueError('Runtime storage does not grant this user access.')
