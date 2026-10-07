"""Per-user background services. No administrator privileges or embedded keys."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from .auth import atomic_json
from .platforms import is_link, platform_name, windows_identity


def run(command, *, check=True):
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if check and result.returncode:
        # OS output may contain user paths and environment details.
        raise RuntimeError('Background service command failed. Run desktop doctor for status; '
                           'no desktop routing was changed.')
    return result


def service_id(directory):
    return 'claude-chatgpt-' + hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:12]


def command(directory, port, python=None):
    executable = Path(python or sys.executable).absolute()
    if os.name == 'nt' and executable.with_name('pythonw.exe').exists():
        executable = executable.with_name('pythonw.exe')
    return [str(executable), '-m', 'claude_chatgpt_bridge', '--state-dir', str(directory),
            '--port', str(port), 'serve']


def plan(directory, port, *, system=None, home=None, python=None, user_sid=None):
    system, home = system or platform_name(), Path(home) if home else Path.home()
    name = service_id(directory)
    args = command(directory, port, python)
    if system == 'linux':
        def quote(value):
            return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'
        content = ('[Unit]\nDescription=Local Claude ChatGPT Bridge\nAfter=network.target\n\n[Service]\n'
                   'Type=simple\nExecStart=' + ' '.join(map(quote, args)) + '\n'
                   'Restart=on-failure\nRestartSec=10\nUMask=0077\n'
                   'StandardOutput=null\nStandardError=null\n\n[Install]\nWantedBy=default.target\n').encode()
        path = home / '.config/systemd/user' / (name + '.service')
    elif system == 'macos':
        name = 'local.' + name
        content = plistlib.dumps({'Label': name, 'ProgramArguments': args,
            'RunAtLoad': True, 'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 10,
            'Umask': 0o077, 'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'})
        path = home / 'Library/LaunchAgents' / (name + '.plist')
    elif system == 'windows':
        if user_sid is None:
            import win32security
            user_sid = win32security.ConvertSidToStringSid(windows_identity())
        ns = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
        ET.register_namespace('', ns)
        def child(parent, tag, value=None, **attributes):
            node = ET.SubElement(parent, '{' + ns + '}' + tag, attributes)
            if value is not None:
                node.text = value
            return node
        root = ET.Element('{' + ns + '}Task', {'version': '1.2'})
        trigger = child(child(root, 'Triggers'), 'LogonTrigger')
        child(trigger, 'Enabled', 'true'); child(trigger, 'UserId', user_sid)
        principal = child(child(root, 'Principals'), 'Principal', id='CurrentUser')
        child(principal, 'UserId', user_sid); child(principal, 'LogonType', 'InteractiveToken')
        child(principal, 'RunLevel', 'LeastPrivilege')
        settings = child(root, 'Settings')
        for key, value in {'MultipleInstancesPolicy': 'IgnoreNew', 'DisallowStartIfOnBatteries': 'false',
                           'StopIfGoingOnBatteries': 'false', 'ExecutionTimeLimit': 'PT0S',
                           'StartWhenAvailable': 'true'}.items():
            child(settings, key, value)
        restart = child(settings, 'RestartOnFailure')
        child(restart, 'Interval', 'PT1M'); child(restart, 'Count', '3')
        action = child(child(root, 'Actions', Context='CurrentUser'), 'Exec')
        child(action, 'Command', args[0]); child(action, 'Arguments', subprocess.list2cmdline(args[1:]))
        child(action, 'WorkingDirectory', str(directory))
        content = ET.tostring(root, encoding='utf-16', xml_declaration=True)
        path = directory / 'service-task.xml'
    else:
        raise ValueError('Unsupported background service platform.')
    return {'system': system, 'name': name, 'path': str(path), 'content': content,
            'sha256': hashlib.sha256(content).hexdigest()}


def task_identity(data):
    root = ET.fromstring(data)
    tags = ('Command', 'Arguments', 'WorkingDirectory', 'UserId', 'LogonType', 'RunLevel')
    return {tag: [n.text for n in root.iter() if n.tag.split('}')[-1] == tag] for tag in tags}


def windows_task_info(name):
    """COM avoids localized status strings and schtasks XML output encodings."""
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    try:
        scheduler = win32com.client.Dispatch('Schedule.Service')
        scheduler.Connect()
        task = scheduler.GetFolder('\\').GetTask(name)
        return {'running': task.State == 4, 'xml': task.Xml}
    except pythoncom.com_error as error:
        codes = [error.hresult]
        if error.excepinfo and error.excepinfo[5] is not None:
            codes.append(error.excepinfo[5])
        if any(code & 0xffffffff == 0x80070002 for code in codes):
            return None
        raise RuntimeError('Could not inspect the scheduled task. No task was changed.') from None
    finally:
        pythoncom.CoUninitialize()


def check_owned(record):
    path = Path(record['path'])
    if is_link(path) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
        raise RuntimeError('Service definition changed outside this installer. Preserve it and resolve the conflict before continuing.')
    if record['system'] == 'windows':
        actual = windows_task_info(record['name'])
        if actual and task_identity(actual['xml']) != task_identity(path.read_bytes()):
            raise RuntimeError('A different scheduled task uses this name. No task was changed.')


def install(directory, port, *, restart=False):
    candidate = plan(directory, port)
    path, receipt = Path(candidate['path']), directory / 'service.json'
    previous = json.loads(receipt.read_text()) if receipt.exists() else None
    if previous:
        check_owned(previous)
        if previous['name'] != candidate['name'] or previous['path'] != candidate['path']:
            raise RuntimeError('Service location changed. Undo the old installation before reinstalling.')
    elif path.exists() or is_link(path):
        raise RuntimeError('A service definition already exists without an installer receipt. Nothing was overwritten.')
    elif candidate['system'] == 'windows' and run(['schtasks', '/Query', '/TN', candidate['name']], check=False).returncode == 0:
        raise RuntimeError('A scheduled task with this name already exists. Nothing was overwritten.')
    if previous and previous['sha256'] == candidate['sha256'] and status(directory)['running'] and not restart:
        return {'installed': True, 'running': True, 'reused': True}
    path.parent.mkdir(parents=True, exist_ok=True)
    # Only generated service files, never credentials, are written here.
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(candidate['content'])
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    atomic_json(receipt, {k: v for k, v in candidate.items() if k != 'content'})
    system, name = candidate['system'], candidate['name']
    if system == 'linux':
        run(['systemctl', '--user', 'daemon-reload'])
        run(['systemctl', '--user', 'enable', name + '.service'])
        run(['systemctl', '--user', 'restart', name + '.service'])
    elif system == 'macos':
        domain = 'gui/' + str(os.getuid())
        if previous:
            run(['launchctl', 'bootout', domain + '/' + name], check=False)
        run(['launchctl', 'bootstrap', domain, str(path)])
    else:
        args = ['schtasks', '/Create', '/TN', name, '/XML', str(path)]
        if previous:
            # Re-registering an active task does not stop its old Python process.
            run(['schtasks', '/End', '/TN', name], check=False)
            args.append('/F')
        run(args)
        run(['schtasks', '/Run', '/TN', name])
    return {'installed': True, 'running': status(directory)['running']}


def status(directory):
    receipt = directory / 'service.json'
    if not receipt.exists():
        return {'installed': False, 'running': False}
    record = json.loads(receipt.read_text())
    check_owned(record)
    system, name = record['system'], record['name']
    if system == 'linux':
        active = run(['systemctl', '--user', 'is-active', name + '.service'], check=False).returncode == 0
    elif system == 'macos':
        result = run(['launchctl', 'print', 'gui/' + str(os.getuid()) + '/' + name], check=False)
        active = result.returncode == 0 and 'state = running' in result.stdout
    else:
        actual = windows_task_info(name)
        if actual is None:
            return {'installed': False, 'running': False, 'system': system}
        active = actual['running']
    return {'installed': True, 'running': active, 'system': system}


def uninstall(directory):
    receipt = directory / 'service.json'
    if not receipt.exists():
        return {'removed': False}
    record = json.loads(receipt.read_text()); check_owned(record)
    system, name = record['system'], record['name']
    if system == 'linux':
        run(['systemctl', '--user', 'disable', '--now', name + '.service'])
    elif system == 'macos':
        run(['launchctl', 'bootout', 'gui/' + str(os.getuid()) + '/' + name], check=False)
    else:
        if run(['schtasks', '/Query', '/TN', name], check=False).returncode == 0:
            run(['schtasks', '/End', '/TN', name], check=False)
            run(['schtasks', '/Delete', '/TN', name, '/F'])
    Path(record['path']).unlink()
    if system == 'linux':
        run(['systemctl', '--user', 'daemon-reload'])
    receipt.unlink()
    return {'removed': True}
