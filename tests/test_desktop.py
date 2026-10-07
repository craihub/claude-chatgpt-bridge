import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from claude_chatgpt_bridge import auth, cli, desktop, platforms, service, state
import test_recovery as recovery


def synthetic_verification(directory, model='chatgpt.synthetic-model'):
    auth.atomic_json(directory / 'chatgpt-auth.json', {'active': 'synthetic-client', 'profiles': {
        'synthetic-client': {'client_id': 'synthetic-client', 'access_token': 'synthetic-token',
                             'scopes': ['chatgpt.tokens.use.direct']}}})
    config = {'inferenceProvider': 'gateway', 'synthetic': True}
    auth.atomic_json(directory / 'desktop-import.json', config)
    runtime = desktop.runtime_fingerprint()
    auth.atomic_json(directory / 'desktop-setup.json', {'model': model, 'port': 12345, 'runtime': runtime,
                                                       'phase': 'awaiting_desktop_import'})
    check = {'model': model, 'marker': 'BRIDGE_SETUP_synthetic_marker', 'created_at': time.time(),
             'account': hashlib.sha256(b'synthetic-client').hexdigest(),
             'config': hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
             'runtime': runtime, 'completed': False}
    auth.atomic_json(directory / 'desktop-check.json', check)
    return check


class PlanTests(unittest.TestCase):
    def test_native_directory_layouts(self):
        for system, suffix in [('linux', '.local/state/claude-chatgpt-bridge'),
                               ('macos', 'Library/Application Support/ClaudeChatGPTBridge/state'),
                               ('windows', 'AppData/Local/ClaudeChatGPTBridge/state')]:
            self.assertEqual(platforms.app_directory(system=system, home=Path('synthetic-home'), environ={}),
                             Path('synthetic-home') / suffix)

    def test_service_definitions_contain_no_credentials_or_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'space % $ directory'
            args = service.command(directory, 12345, python='/synthetic path/python')
            linux = service.plan(directory, 12345, system='linux', home=tmp, python=args[0])
            self.assertIn(b'%% $$', linux['content'])
            self.assertIn(b'UMask=0077', linux['content'])
            mac = service.plan(directory, 12345, system='macos', home=tmp, python=args[0])
            self.assertEqual(plistlib.loads(mac['content'])['ProgramArguments'], args)
            windows = service.plan(directory, 12345, system='windows', home=tmp, python=args[0],
                                   user_sid='S-1-5-21-123-234-345-1001')
            identity = service.task_identity(windows['content'])
            self.assertEqual(identity['LogonType'], ['InteractiveToken'])
            self.assertEqual(identity['RunLevel'], ['LeastPrivilege'])
            self.assertEqual(identity['Command'], [args[0]])
            self.assertNotIn('access_token', windows['content'].decode('utf-16'))
            self.assertNotIn('bridge.key', json.dumps(identity))

    def test_config_uses_desktop_key_and_account_models(self):
        models = [{'slug': 'synthetic-model', 'display_name': 'Synthetic'},
                  {'slug': 'other-model', 'display_name': 'Other'}]
        config = desktop.make_config(models, 'synthetic-local-credential', 12345, 'chatgpt.other-model')
        self.assertEqual(config['inferenceModels'][0]['name'], 'chatgpt.other-model')
        self.assertEqual(config['inferenceGatewayBaseUrl'], 'http://127.0.0.1:12345')
        self.assertEqual(config['inferenceCredentialKind'], 'static')
        self.assertIs(config['toolSearchEnabled'], True)
        disabled = desktop.make_config(models, 'synthetic-local-credential', 12345,
                                       'chatgpt.other-model', tool_search=False)
        self.assertIs(disabled['toolSearchEnabled'], False)
        self.assertNotIn('inferenceCustomHeaders', config)
        self.assertNotIn('inferenceBedrockProfile', config)
        self.assertNotIn('anthropicFamilyTier', json.dumps(config))
        with self.assertRaises(ValueError):
            desktop.make_config(models, 'synthetic', 12345, 'chatgpt.unavailable')

    def test_service_conflict_never_overwrites_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = state.initialize(Path(tmp) / 'state')
            plan = service.plan(directory, 12345, system='linux', home=Path(tmp))
            path = Path(plan['path']); path.parent.mkdir(parents=True)
            path.write_bytes(b'Unrelated service')
            with patch.object(service, 'plan', return_value=plan), patch.object(service, 'run') as commands:
                with self.assertRaises(RuntimeError):
                    service.install(directory, 12345)
                commands.assert_not_called()
            self.assertEqual(path.read_bytes(), b'Unrelated service')

    def test_owned_service_reuse_and_guarded_undo(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = state.initialize(Path(tmp) / 'state')
            plan = service.plan(directory, 12345, system='linux', home=Path(tmp))
            result = subprocess.CompletedProcess([], 0, 'active', '')
            with patch.object(service, 'plan', return_value=plan), patch.object(service, 'run', return_value=result) as commands:
                service.install(directory, 12345)
                self.assertTrue((directory / 'service.json').exists())
                commands.reset_mock()
                self.assertTrue(service.install(directory, 12345)['reused'])
                self.assertEqual(commands.call_count, 1)
                Path(plan['path']).write_bytes(b'Edited outside installer')
                with self.assertRaises(RuntimeError):
                    service.uninstall(directory)
                self.assertTrue(Path(plan['path']).exists())

    def test_backup_is_exact_and_never_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = state.initialize(Path(tmp) / 'state')
            source = Path(tmp) / 'previous.json'
            raw = b'{ "inferenceProvider" : "bedrock", "custom": true }\n'
            source.write_bytes(raw)
            destination = Path(desktop.backup_previous(directory, source))
            self.assertEqual(destination.read_bytes(), raw)
            source.write_bytes(b'{}')
            with self.assertRaises(ValueError):
                desktop.backup_previous(directory, source)
            self.assertEqual(destination.read_bytes(), raw)

    def test_windows_upgrade_stops_old_task_before_replacing(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = state.initialize(Path(tmp) / 'state')
            plan = service.plan(directory, 12345, system='windows', home=Path(tmp),
                                user_sid='S-1-5-21-123-234-345-1001')
            Path(plan['path']).write_bytes(plan['content'])
            auth.atomic_json(directory / 'service.json', {k: v for k, v in plan.items() if k != 'content'})
            result = subprocess.CompletedProcess([], 0, plan['content'].decode('utf-16'), '')
            with patch.object(service, 'plan', return_value=plan), \
                 patch.object(service, 'windows_task_info', return_value={'running': True, 'xml': plan['content']}), \
                 patch.object(service, 'status', return_value={'installed': True, 'running': True}), \
                 patch.object(service, 'run', return_value=result) as commands:
                service.install(directory, 12345, restart=True)
                actions = [c.args[0][1] for c in commands.call_args_list]
                self.assertLess(actions.index('/End'), actions.index('/Create'))
                self.assertLess(actions.index('/Create'), actions.index('/Run'))

    def test_undo_waits_for_desktop_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = state.initialize(Path(tmp) / 'state')
            auth.atomic_json(directory / 'desktop-setup.json', {'phase': 'awaiting_desktop_import'})
            with patch.object(service, 'uninstall', return_value={'removed': True}) as remove:
                self.assertEqual(desktop.undo(directory)['phase'], 'awaiting_desktop_restore')
                remove.assert_not_called()
                self.assertEqual(desktop.undo(directory, True)['phase'], 'undone')
                remove.assert_called_once()


class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_doctor_rejects_expired_account_changed_and_config_changed_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = state.initialize(Path(tmp) / 'state')
            check = synthetic_verification(directory)
            check['completed'] = True
            auth.atomic_json(directory / 'desktop-check.json', check)
            with patch.object(desktop, 'probe', AsyncMock(return_value=True)), \
                 patch.object(service, 'status', return_value={'installed': True, 'running': True}):
                self.assertTrue((await desktop.doctor(directory))['desktop_verified'])
                for field, value in [('created_at', time.time() - 8 * 86400),
                                     ('account', 'another-account'), ('config', 'another-config'),
                                     ('runtime', None), ('runtime', 'previous-runtime')]:
                    auth.atomic_json(directory / 'desktop-check.json', {**check, field: value})
                    result = await desktop.doctor(directory)
                    self.assertFalse(result['desktop_verified'])
                    self.assertNotIn('verification_prompt', result)
                auth.atomic_json(directory / 'desktop-check.json', check)
                with patch.object(desktop, 'runtime_fingerprint', return_value='updated-code'):
                    self.assertFalse((await desktop.doctor(directory))['desktop_verified'])

    async def test_changed_runtime_cannot_complete_an_old_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = state.initialize(Path(tmp) / 'state')
            check = synthetic_verification(directory)
            with patch.object(desktop, 'runtime_fingerprint', return_value='updated-code'):
                desktop.observe_completion(directory,
                    {'messages': [{'role': 'user', 'content': check['marker']}]},
                    check['model'], [{'type': 'text', 'text': check['marker']}])
            self.assertFalse(json.loads((directory / 'desktop-check.json').read_text())['completed'])

    async def test_no_login_returns_pending_without_service_or_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'state'
            with patch.object(desktop, 'refresh_models', AsyncMock()) as models, patch.object(service, 'install') as install:
                result = await desktop.setup(directory, no_login=True)
                self.assertEqual(result['phase'], 'awaiting_login')
                models.assert_not_called(); install.assert_not_called()
                self.assertFalse((directory / 'desktop-import.json').exists())

    async def test_setup_resumes_without_reauthorizing_or_replacing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = state.initialize(Path(tmp) / 'state')
            auth.atomic_json(directory / 'chatgpt-auth.json', {'active': 'synthetic-client', 'profiles': {
                'synthetic-client': {'client_id': 'synthetic-client', 'access_token': 'synthetic-token',
                                     'scopes': ['chatgpt.tokens.use.direct']}}})
            async def refresh(path):
                auth.atomic_json(path / 'models.json', [{'slug': 'synthetic-model', 'display_name': 'Synthetic'}])
            with patch.object(desktop, 'refresh_models', refresh), \
                 patch.object(desktop, 'login', AsyncMock()) as login, \
                 patch.object(desktop, 'wait_ready', AsyncMock()), \
                 patch.object(desktop, 'probe', AsyncMock(return_value=True)), \
                 patch.object(service, 'install', return_value={'installed': True}) as install, \
                 patch.object(service, 'status', return_value={'installed': True, 'running': True}):
                first = await desktop.setup(directory)
                self.assertFalse(first['desktop_verified'])
                original = (directory / 'desktop.key').read_bytes()
                record = desktop.read_record(directory)
                second = await desktop.setup(directory)
                self.assertEqual((directory / 'desktop.key').read_bytes(), original)
                self.assertEqual(desktop.read_record(directory)['port'], record['port'])
                self.assertEqual(first['verification_prompt'], second['verification_prompt'])
                login.assert_not_called()
                self.assertTrue(install.call_args_list[0].kwargs['restart'])
                self.assertFalse(install.call_args_list[1].kwargs['restart'])
                self.assertNotIn('synthetic-token', json.dumps(first))
                self.assertNotIn(original.decode().strip(), json.dumps(first))

                check_path = directory / 'desktop-check.json'
                check = json.loads(check_path.read_text())
                check['completed'] = True
                auth.atomic_json(check_path, check)
                self.assertTrue((await desktop.setup(directory))['desktop_verified'])
                self.assertFalse(install.call_args.kwargs['restart'])

                with patch.object(desktop, 'runtime_fingerprint', return_value='updated-code'):
                    updated = await desktop.setup(directory)
                    self.assertTrue(install.call_args.kwargs['restart'])
                    self.assertFalse(updated['desktop_verified'])
                    self.assertNotEqual(updated['verification_prompt'], first['verification_prompt'])
                    self.assertFalse(json.loads(check_path.read_text())['completed'])
                    self.assertEqual(json.loads(check_path.read_text())['runtime'], 'updated-code')

                disabled = await desktop.setup(directory, tool_search=False)
                config_path = directory / 'desktop-import.json'
                self.assertIs(json.loads(config_path.read_text())['toolSearchEnabled'], False)
                self.assertFalse(disabled['desktop_verified'])
                await desktop.setup(directory)
                self.assertIs(json.loads(config_path.read_text())['toolSearchEnabled'], False)
                await desktop.setup(directory, tool_search=True)
                self.assertIs(json.loads(config_path.read_text())['toolSearchEnabled'], True)


class DesktopProtocolTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = recovery.RecoveryTests.asyncSetUp
    asyncTearDown = recovery.RecoveryTests.asyncTearDown
    send = recovery.RecoveryTests.send

    def desktop_auth(self):
        self.bridge.desktop_key = 'synthetic-desktop-key-' + 'x' * 40
        return {'Authorization': 'Bearer ' + self.bridge.desktop_key}

    async def test_desktop_bearer_and_api_key_auth_only_allow_inference(self):
        headers = self.desktop_auth()
        for candidate in (headers, {'x-api-key': self.bridge.desktop_key}):
            reply = await self.client.get('/v1/models', headers=candidate)
            self.assertEqual(reply.status, 200)
            health = await self.client.get('/health', headers=candidate)
            self.assertEqual(health.status, 401)
        reply = await self.client.get('/v1/models', headers={**headers, 'x-api-key': 'different'})
        self.assertEqual(reply.status, 401)
        reply = await self.client.get('/v1/models', headers={**headers, 'Origin': 'https://example.invalid'})
        self.assertEqual(reply.status, 403)

    async def test_real_completed_desktop_marker_required(self):
        marker = synthetic_verification(self.directory, recovery.PAYLOAD['model'])['marker']
        def completion():
            event = recovery.complete()
            event['response']['output'][0]['content'][0]['text'] = marker
            return event
        payload = {**recovery.PAYLOAD, 'stream': False,
                   'messages': [{'role': 'user', 'content': 'Reply exactly: ' + marker}]}
        # A CLI/admin-key check cannot mark desktop verified.
        self.actions.append([recovery.created(), completion()])
        await self.send(payload)
        self.assertFalse(json.loads((self.directory / 'desktop-check.json').read_text())['completed'])
        self.actions.append([recovery.created(), completion()])
        reply = await self.client.post('/v1/messages', json=payload, headers=self.desktop_auth())
        self.assertEqual(reply.status, 200)
        await reply.read()
        self.assertTrue(json.loads((self.directory / 'desktop-check.json').read_text())['completed'])

    async def test_desktop_key_never_authorizes_native_forwarding(self):
        self.bridge.allow_claude = True
        reply = await self.client.post('/v1/messages', json={**recovery.PAYLOAD, 'model': 'claude-test'},
                                       headers=self.desktop_auth())
        self.assertEqual(reply.status, 401)
        self.assertEqual(self.requests, [])


class LockTests(unittest.IsolatedAsyncioTestCase):
    async def test_lock_contention_is_nonblocking_and_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'private.lock'
            from filelock import FileLock
            outer = FileLock(str(path), mode=0o600)
            outer.acquire()
            entered = asyncio.Event()
            async def contender():
                async with auth.file_lock(path):
                    entered.set()
            task = asyncio.create_task(contender())
            await asyncio.sleep(.02)
            self.assertFalse(entered.is_set())
            outer.release()
            await asyncio.wait_for(task, 2)
            self.assertTrue(entered.is_set())


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('bridge_bootstrap', Path(__file__).parents[1] / 'install.py')
        self.bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bootstrap)

    def test_desktop_tool_search_option_reaches_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            for option, value in (('--no-tool-search', False), ('--tool-search', True)):
                parsed = cli.parser().parse_args(['desktop', 'setup', option])
                self.assertIs(parsed.tool_search, value)
                with patch.object(self.bootstrap, 'runtime_path', return_value=Path(tmp)), \
                     patch.object(self.bootstrap, 'install', return_value=Path(tmp) / 'python'), \
                     patch.object(self.bootstrap.subprocess, 'call', return_value=2) as call, \
                     patch.object(self.bootstrap.sys, 'argv', ['install.py', 'continue', option]):
                    with self.assertRaises(SystemExit) as stopped:
                        self.bootstrap.main()
                    self.assertEqual(stopped.exception.code, 2)
                    self.assertEqual(call.call_args.args[0][-3:], ['desktop', 'setup', option])

    @unittest.skipIf(os.name == 'nt', 'POSIX permissions and links')
    def test_unsafe_runtime_is_refused_without_installing(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / 'runtime'
            runtime.mkdir(mode=0o755)
            runtime.chmod(0o755)
            with self.assertRaises(RuntimeError):
                self.bootstrap.install(runtime)
            runtime.chmod(0o700)
            (runtime / 'venv').symlink_to(Path(tmp) / 'outside')
            with self.assertRaises(RuntimeError):
                self.bootstrap.install(runtime)
            self.assertFalse((Path(tmp) / 'outside').exists())

    def test_unchanged_source_reuses_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, runtime = Path(tmp) / 'source', Path(tmp) / 'runtime'
            root.mkdir()
            (root / 'PUBLIC_FILES.txt').write_text('example.txt\n')
            (root / 'example.txt').write_text('synthetic source')
            def create(env_dir):
                python = env_dir / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
                python.parent.mkdir(parents=True)
                python.write_bytes(b'')
                (env_dir / 'pyvenv.cfg').write_text('synthetic')
            with patch.object(self.bootstrap, 'ROOT', root.resolve()), \
                 patch.object(self.bootstrap.venv.EnvBuilder, 'create', side_effect=create) as venv_create, \
                 patch.object(self.bootstrap.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as pip:
                first = self.bootstrap.install(runtime)
                self.assertEqual(self.bootstrap.install(runtime), first)
                self.assertEqual(venv_create.call_count, 1)
                self.assertEqual(pip.call_count, 1)
                self.assertNotIn('PYTHONPATH', pip.call_args.kwargs['env'])
                self.assertEqual(len(list(runtime.glob('source-*'))), 1)


@unittest.skipUnless(os.name == 'nt', 'Native Windows ACL validation')
class WindowsStorageTests(unittest.TestCase):
    def test_private_directory_and_reject_public_acl(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = state.initialize(Path(tmp) / 'state')
            platforms.check_windows_private(directory)
            platforms.check_windows_private(directory / 'bridge.key')
            import win32security as security
            from unittest.mock import Mock
            foreign = Mock()
            foreign.GetSecurityDescriptorOwner.return_value = security.CreateWellKnownSid(security.WinWorldSid, None)
            with patch.object(security, 'GetNamedSecurityInfo', return_value=foreign):
                with self.assertRaises(ValueError):
                    platforms.check_windows_private(directory)
            foreign.GetSecurityDescriptorOwner.return_value = security.CreateWellKnownSid(security.WinBuiltinAdministratorsSid, None)
            with patch.object(security, 'GetNamedSecurityInfo', return_value=foreign), \
                 patch.object(platforms, 'windows_token_sid', return_value=platforms.windows_identity()):
                with self.assertRaises(ValueError):
                    platforms.check_windows_private(directory)
            descriptor = security.GetNamedSecurityInfo(str(directory), security.SE_FILE_OBJECT,
                                                      security.DACL_SECURITY_INFORMATION)
            acl = descriptor.GetSecurityDescriptorDacl()
            acl.AddAccessAllowedAce(security.ACL_REVISION, 0x120089,
                                   security.CreateWellKnownSid(security.WinWorldSid, None))
            security.SetNamedSecurityInfo(str(directory), security.SE_FILE_OBJECT,
                                          security.DACL_SECURITY_INFORMATION, None, None, acl, None)
            with self.assertRaises(ValueError):
                state.ensure_private_directory(directory)
