import contextlib
import io
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import mittr_postgres_rehearsal as tool
from scripts.test_mittr_postgres_rehearsal_integration import FixtureDocker, RUN_LABEL
from scripts import test_mittr_postgres_rehearsal_integration as integration


class RehearsalTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(subprocess, 'run', side_effect=AssertionError('Real subprocess forbidden'))
        guard.start()
        self.addCleanup(guard.stop)

    def test_default_is_help_without_docker(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tool.main([]), 0)

    def test_source_allowlist_and_readonly(self):
        docker = Mock()
        source = tool.Source(docker, 'source-id')
        for operation in ('status', 'data', 'schema', 'dump', 'config'):
            source.read(operation)
            args = docker.run.call_args.args[0]
            self.assertEqual(args[:4], ['exec', '-i', 'source-id', 'sh'])
            self.assertNotIn('pg_restore', args[-1])
            if operation != 'config':
                self.assertIn(tool.READ_OPTIONS, args[-1])
                self.assertIn('PGPASSWORD="$POSTGRES_PASSWORD"', args[-1])
        for operation in ('restore', 'stop', 'rm', 'SELECT 1', 'dump; DROP TABLE x'):
            with self.assertRaises(tool.SafetyError):
                source.read(operation)
        self.assertEqual(docker.run.call_count, 5)

    def test_normalized_restrict_only(self):
        self.assertEqual(tool.normalize_schema(b'\\restrict abc123\nCREATE TABLE x();\n\\unrestrict abc123\n'), b'CREATE TABLE x();')
        self.assertNotEqual(tool.normalize_schema(b'CREATE TABLE x();'), tool.normalize_schema(b'CREATE TABLE y();'))

    def test_fingerprints_order_duplicates_precision_and_sequences(self):
        rows = ['["relation","public","t",{"x":1.1234567890123456789}]', '["relation","public","t",{"x":2}]']
        self.assertEqual(tool.fingerprints('\n'.join(rows)), tool.fingerprints('\n'.join(reversed(rows))))
        self.assertNotEqual(tool.fingerprints('\n'.join(rows)), tool.fingerprints('\n'.join(rows + rows)))
        self.assertNotEqual(tool.fingerprints(rows[0]), tool.fingerprints(rows[0].replace('789', '788')))
        self.assertNotEqual(tool.fingerprints('["sequence","public","s","1",true]'), tool.fingerprints('["sequence","public","s","2",true]'))

    def test_foreign_tables_refused(self):
        with self.assertRaises(tool.SafetyError):
            tool.fingerprints('["unsupported",1]')

    def test_checksum_private_copy_and_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'dump'
            digest = tool.private_dump(path, lambda stream: stream.write(b'archive'))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            target = tool.Target(Mock(), 'image', ['source'])
            target.identity = 'target'
            target.restore(path, digest)
            self.assertIn('pg_restore --exit-on-error', target.docker.run.call_args.args[0][-1])
            path.write_bytes(b'tampered')
            with self.assertRaisesRegex(tool.SafetyError, 'checksum'):
                target.restore(path, digest)
            self.assertEqual(target.docker.run.call_count, 1)

    def test_copy_failure_closes_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'dump'
            with self.assertRaises(OSError):
                tool.private_dump(path, Mock(side_effect=OSError('secret')))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_target_routing_refuses_protected(self):
        target = tool.Target(Mock(), 'image', ['source', 'app'])
        for identity in (None, 'source', 'app'):
            target.identity = identity
            with self.assertRaises(tool.SafetyError):
                target.sql('CREATE DATABASE fixture;')
        target.docker.run.assert_not_called()

    def test_owned_cleanup_and_foreign_refusal(self):
        docker = Mock()
        target = tool.Target(docker, 'image', ['source', 'app'])
        target.identity = 'owned'
        target.attempted = True
        def inspect(identity='owned', labels=None, name=None):
            return '\n'.join(json.dumps(item) for item in (identity, labels or {tool.LABEL: target.token}, name or '/' + target.name)).encode()
        for raw in (inspect(identity='source'), inspect(labels={tool.LABEL: 'foreign'}), inspect(name='/foreign'), inspect(identity='other')):
            docker.run.return_value = raw
            with self.assertRaises(tool.SafetyError):
                target.cleanup()
            self.assertEqual(docker.run.call_args.args[0][0], 'inspect')
        docker.run.return_value = inspect()
        target.cleanup()
        self.assertEqual(docker.run.call_args.args[0], ['rm', '-f', '-v', 'owned'])

    def test_create_is_isolated_and_password_not_in_arguments(self):
        docker = Mock()
        docker.run.return_value = b'a' * 64
        target = tool.Target(docker, 'sha256:image', ['source', 'app'])
        target.create()
        call = docker.run.call_args_list[0]
        args = call.args[0]
        password = call.kwargs['env']['POSTGRES_PASSWORD']
        self.assertNotIn(password, ' '.join(args))
        self.assertIn('--network=none', args)
        self.assertIn('--tmpfs', args)
        self.assertNotIn('--mount', args)
        self.assertNotIn('-p', args)
        self.assertIn('sha256:image', args)

    def test_fixture_run_label_only_on_create(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, b'created', b''))
        docker = FixtureDocker(runner)
        with patch.dict('os.environ', {'MITTR_PG_TEST_RUN_ID': '123-2-rehearsal'}):
            docker.run(['create', '--name', 'fixture', 'image'])
            self.assertEqual(runner.call_args.args[0], [
                'docker', 'create', '--label', RUN_LABEL + '=123-2-rehearsal',
                '--name', 'fixture', 'image'])
            docker.run(['ps', '-aq'])
            self.assertEqual(runner.call_args.args[0], ['docker', 'ps', '-aq'])
        with patch.dict('os.environ', {'MITTR_PG_TEST_RUN_ID': ''}):
            docker.run(['create', 'image'])
            self.assertEqual(runner.call_args.args[0], ['docker', 'create', 'image'])
        runner.reset_mock()
        with patch.dict('os.environ', {'MITTR_PG_TEST_RUN_ID': 'invalid run/id'}):
            with self.assertRaisesRegex(tool.SafetyError, 'Invalid fixture run identity'):
                docker.run(['create', 'image'])
        runner.assert_not_called()

    def workflow(self):
        path = Path(__file__).resolve().parents[1] / '.github/workflows/mittr-postgres-rehearsal.yml'
        return path.read_text()

    def test_ci_workflow_safety_contract(self):
        workflow = self.workflow()
        for required in (
            '  pull_request:', '  push:', '  workflow_dispatch:',
            'permissions:\n  contents: read', 'runs-on: ubuntu-latest',
            'persist-credentials: false', 'if: ${{ always() }}',
            'timeout-minutes: 15', 'timeout-minutes: 10',
            'MITTR_PG_DOCKER_INTEGRATION: \'1\'', 'docker pull postgres:18-alpine',
            'python3 -B -m unittest scripts.test_mittr_postgres_rehearsal -v',
            'python3 -B -m unittest scripts.test_mittr_postgres_rehearsal_integration -v',
            "'--filter', 'label=" + RUN_LABEL + "=' + run_id",
            "'--filter', 'label=" + tool.LABEL + "'",
        ):
            self.assertIn(required, workflow)
        for forbidden in ('secrets.', 'pull_request_target:', 'prune', 'continue-on-error:',
                          '.env.mittr.local', 'mittr-postgres-postgres-1', 'mittr-load-app-1'):
            self.assertNotIn(forbidden, workflow)

    def test_ci_job_env_does_not_use_runner_context(self):
        job = self.workflow().split('  rehearsal:\n', 1)[1].split('    steps:\n', 1)[0]
        self.assertNotRegex(job, r'\$\{\{[^}]*\brunner\s*\.')
        self.assertNotIn('TMPDIR:', job)

    def test_ci_preparation_exports_step_tmpdir_for_subsequent_tests(self):
        workflow = self.workflow()
        preparation = workflow.split('      - name: Prepare private test directory\n', 1)[1].split(
            '      - name:', 1)[0]
        self.assertIn(
            '        env:\n          TMPDIR: ${{ runner.temp }}/mittr-postgres-rehearsal-'
            '${{ github.run_id }}-${{ github.run_attempt }}\n', preparation)
        self.assertIn('mkdir -m 700 "$TMPDIR"', preparation)
        self.assertIn('printf \'TMPDIR=%s\\n\' "$TMPDIR" >> "$GITHUB_ENV"', preparation)
        self.assertLess(preparation.index('mkdir -m 700'), preparation.index('>> "$GITHUB_ENV"'))
        self.assertLess(workflow.index('- name: Prepare private test directory'),
                        workflow.index('- name: Run unit tests'))

    def test_ci_cleanup_has_independent_step_tmpdir_even_if_preparation_fails(self):
        cleanup = self.workflow().split(
            '      - name: Clean up owned test fixtures and dumps\n', 1)[1]
        self.assertIn('        if: ${{ always() }}\n', cleanup)
        self.assertIn(
            '        env:\n          TMPDIR: ${{ runner.temp }}/mittr-postgres-rehearsal-'
            '${{ github.run_id }}-${{ github.run_attempt }}\n', cleanup)
        self.assertNotIn('${{ env.TMPDIR }}', cleanup)
        self.assertNotIn('${{ steps.', cleanup)

    def workflow_script(self, step):
        block = self.workflow().split('      - name: ' + step, 1)[1]
        script = block.split("python3 -B - <<'PY'\n", 1)[1].split('          PY', 1)[0]
        return '\n'.join(line[10:] for line in script.splitlines())

    def cleanup_environment(self):
        return {
            'MITTR_PG_TEST_RUN_ID': '123-2-rehearsal', 'GITHUB_RUN_ID': '123',
            'GITHUB_RUN_ATTEMPT': '2', 'RUNNER_TEMP': '/runner-temp',
            'TMPDIR': '/runner-temp/mittr-postgres-rehearsal-123-2',
        }

    def test_ci_simulation_dispatch_contract(self):
        workflow = self.workflow()
        self.assertIn('simulate_failure:\n        description:', workflow)
        self.assertIn('type: boolean\n        required: false\n        default: false', workflow)
        self.assertIn("if: ${{ github.event_name == 'workflow_dispatch' && inputs.simulate_failure }}", workflow)
        self.assertIn('SIMULATE_FAILURE: ${{ inputs.simulate_failure }}', workflow)
        self.assertLess(workflow.index('- name: Run isolated Docker integration tests'),
                        workflow.index('- name: Simulate failure with owned fixture'))
        self.assertLess(workflow.index('- name: Simulate failure with owned fixture'),
                        workflow.index('- name: Clean up owned test fixtures and dumps'))
        script = self.workflow_script('Simulate failure with owned fixture')
        self.assertNotIn('${{', script)
        self.assertNotIn('password', script.lower())

    def test_ci_simulation_leaves_running_owned_isolated_fixture_and_fails(self):
        identity = 'a' * 64
        script = self.workflow_script('Simulate failure with owned fixture')
        with patch.dict('os.environ', dict(self.cleanup_environment(), SIMULATE_FAILURE='true')), \
                patch.object(subprocess, 'run') as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            run.return_value = subprocess.CompletedProcess([], 0, identity + '\n', '')
            with self.assertRaises(SystemExit) as caught:
                exec(compile(script, '<workflow-simulation>', 'exec'), {})
            self.assertEqual(caught.exception.code, 1)
            self.assertEqual(run.call_args_list[0].args[0], [
                'docker', 'create', '--network=none',
                '--label', RUN_LABEL + '=123-2-rehearsal',
                '--label', tool.LABEL + '=simulated-failure',
                '--tmpfs', '/var/lib/postgresql',
                '--entrypoint', 'sleep', 'postgres:18-alpine', '300'])
            self.assertEqual(run.call_args_list[1].args[0], ['docker', 'start', identity])
            self.assertEqual(run.call_count, 2)
            self.assertIn('simulated failure: owned running container ' + identity, output.getvalue())

    def test_ci_simulation_rejects_non_true_input_without_docker(self):
        script = self.workflow_script('Simulate failure with owned fixture')
        for value in ('false', '', 'True', 'true; exit 0'):
            with self.subTest(value=value), patch.dict('os.environ', {'SIMULATE_FAILURE': value}), \
                    patch.object(subprocess, 'run') as run:
                with self.assertRaises(SystemExit):
                    exec(compile(script, '<workflow-simulation>', 'exec'), {})
                run.assert_not_called()

    def test_ci_simulation_rejects_invalid_run_id_without_docker(self):
        script = self.workflow_script('Simulate failure with owned fixture')
        with patch.dict('os.environ', {'SIMULATE_FAILURE': 'true', 'MITTR_PG_TEST_RUN_ID': 'bad/run'}), \
                patch.object(subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'Invalid fixture run identity'):
                exec(compile(script, '<workflow-simulation>', 'exec'), {})
            run.assert_not_called()

    def test_ci_simulation_redacts_invalid_container_identity(self):
        script = self.workflow_script('Simulate failure with owned fixture')
        with patch.dict('os.environ', dict(self.cleanup_environment(), SIMULATE_FAILURE='true')), \
                patch.object(subprocess, 'run') as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            run.return_value = subprocess.CompletedProcess([], 0, 'secret\n::error::unsafe', '')
            with self.assertRaisesRegex(RuntimeError, '^Invalid fixture container identity$'):
                exec(compile(script, '<workflow-simulation>', 'exec'), {})
            self.assertEqual(run.call_count, 1)
            self.assertEqual(output.getvalue(), '')

    def execute_cleanup(self, runner, exists=(True, False), environment=None, symlink=False):
        with patch.dict('os.environ', environment or self.cleanup_environment()), \
                patch.object(subprocess, 'run', side_effect=runner) as run, \
                patch.object(Path, 'exists', side_effect=exists), \
                patch.object(Path, 'is_symlink', return_value=symlink), \
                patch('shutil.rmtree') as remove, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            exec(compile(self.workflow_script('Clean up owned test fixtures and dumps'),
                         '<workflow-cleanup>', 'exec'), {})
            return run, remove, output.getvalue()

    def cleanup_runner(self, args, **kwargs):
        output = ''
        if args[:3] == ['docker', 'ps', '-aq']:
            self.ps_calls += 1
            output = 'owned-id\n' if self.ps_calls == 1 else ''
        elif args[:2] == ['docker', 'inspect']:
            output = json.dumps([{'Mounts': [
                {'Type': 'volume', 'Name': 'owned-volume'},
                {'Type': 'tmpfs', 'Destination': '/var/lib/postgresql'}]}])
        elif args == ['docker', 'volume', 'ls', '-q']:
            output = 'unrelated-volume\n'
        return subprocess.CompletedProcess(args, 0, output, '')

    def test_ci_cleanup_removes_only_owned_and_verifies_containers_volumes_and_dump(self):
        self.ps_calls = 0
        run, remove, output = self.execute_cleanup(self.cleanup_runner)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0], [
            'docker', 'ps', '-aq', '--filter', 'label=' + RUN_LABEL + '=123-2-rehearsal',
            '--filter', 'label=' + tool.LABEL])
        self.assertEqual(commands[1], ['docker', 'inspect', 'owned-id'])
        self.assertEqual(commands[2], ['docker', 'rm', '-f', '-v', 'owned-id'])
        self.assertEqual(commands[3], commands[0])
        self.assertEqual(commands[4], ['docker', 'volume', 'ls', '-q'])
        self.assertEqual(commands[5], ['docker', 'volume', 'ls', '-q', *commands[0][3:]])
        remove.assert_called_once_with(Path(self.cleanup_environment()['TMPDIR']))
        self.assertIn('Owned containers before cleanup: 1', output)
        self.assertIn('0 owned containers, 0 owned volumes; checked 1 captured volume names', output)
        self.assertIn('dedicated test directory absent', output)

    def test_ci_cleanup_removes_dump_even_on_docker_failure(self):
        script = self.workflow_script('Clean up owned test fixtures and dumps')
        with patch.dict('os.environ', self.cleanup_environment()), \
                patch.object(subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'docker')), \
                patch.object(Path, 'exists', side_effect=[True, False]), \
                patch.object(Path, 'is_symlink', return_value=False), \
                patch('shutil.rmtree') as remove, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(subprocess.CalledProcessError):
                exec(compile(script, '<workflow-cleanup>', 'exec'), {})
            remove.assert_called_once_with(Path(self.cleanup_environment()['TMPDIR']))

    def test_ci_cleanup_refuses_leftover_containers_and_volumes(self):
        for resource in ('container', 'anonymous-volume', 'labeled-volume'):
            self.ps_calls = 0
            def runner(args, **kwargs):
                result = self.cleanup_runner(args, **kwargs)
                if resource == 'container' and self.ps_calls == 2:
                    result.stdout = 'leftover'
                if resource == 'anonymous-volume' and args == ['docker', 'volume', 'ls', '-q']:
                    result.stdout = 'owned-volume\nunrelated-volume'
                if resource == 'labeled-volume' and args[:4] == ['docker', 'volume', 'ls', '-q'] and len(args) > 4:
                    result.stdout = 'labeled-volume'
                return result
            with self.subTest(resource=resource), self.assertRaisesRegex(RuntimeError, 'remain after cleanup'):
                self.execute_cleanup(runner)

    def test_ci_cleanup_empty_resources_and_missing_directory(self):
        def runner(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, '', '')
        run, remove, output = self.execute_cleanup(runner, exists=(False, False))
        self.assertFalse(any(call.args[0][1] in ('inspect', 'rm') for call in run.call_args_list))
        remove.assert_not_called()
        self.assertIn('Owned containers before cleanup: 0', output)
        self.assertIn('checked 0 captured volume names', output)

    def test_ci_cleanup_refuses_unsafe_directory(self):
        def runner(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, '', '')
        for directory, symlink in (('/outside', False), (self.cleanup_environment()['TMPDIR'], True)):
            environment = dict(self.cleanup_environment(), TMPDIR=directory)
            with self.subTest(directory=directory, symlink=symlink), \
                    self.assertRaisesRegex(RuntimeError, 'Refusing cleanup outside dedicated test directory'):
                self.execute_cleanup(runner, environment=environment, symlink=symlink)

    def test_ci_cleanup_refuses_remaining_directory(self):
        def runner(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, '', '')
        with self.assertRaisesRegex(RuntimeError, 'Dedicated test directory remains'):
            self.execute_cleanup(runner, exists=(True, True))

    def test_errors_and_timeouts_redacted(self):
        for failure in (OSError('secret'), subprocess.TimeoutExpired('secret', 180)):
            docker = tool.Docker(Mock(side_effect=failure))
            with self.assertRaises(tool.SafetyError) as caught:
                docker.run(['inspect', 'source'])
            self.assertNotIn('secret', str(caught.exception))
        runner = Mock(return_value=subprocess.CompletedProcess([], 1, b'secret', b'secret'))
        with self.assertRaises(tool.SafetyError):
            tool.Docker(runner).run(['inspect', 'source'])
        self.assertEqual(runner.call_args.kwargs['timeout'], 180)

    def test_recovery_and_readwrite_refused(self):
        source = tool.Source(Mock(), 'source')
        for value in ([True, 'on', '0/1'], [False, 'off', '0/1']):
            source.docker.run.return_value = json.dumps(value).encode()
            with self.assertRaises(tool.SafetyError):
                source.position()

    def test_names_refused_before_docker(self):
        docker = Mock()
        for source, app in [('same', 'same'), (tool.PREFIX + 'x', 'app'), ('--all', 'app'), ('source;echo', 'app')]:
            with self.assertRaises(tool.SafetyError):
                tool.rehearse(source, app, docker)
        docker.inspect.assert_not_called()

    def run_fake(self, failure=None, cleanup=None, drift=False, mismatch=False, changed=False):
        docker = Mock()
        source_state = {'id': 'source', 'image': 'image', 'name': 'source'}
        app_state = {'id': 'app', 'image': 'appimage', 'name': 'app'}
        docker.inspect.side_effect = [source_state, app_state, dict(source_state, id='changed') if changed else source_state, app_state]
        source = Mock()
        source.position.side_effect = ['0/1', '0/2' if drift else '0/1', '0/1']
        source.fingerprint.return_value = ('schema', {})
        source.read.return_value = b''
        target = Mock(name='target')
        target.name = tool.PREFIX + 'safe'
        target.identity = 'target-id'
        target.fingerprint.return_value = ('bad' if mismatch else 'schema', {})
        target.execute.return_value = b'fixture'
        target.restore.side_effect = failure
        target.cleanup.side_effect = cleanup
        with patch.object(tool, 'Source', return_value=source), patch.object(tool, 'Target', return_value=target), patch.object(tool, 'private_dump', return_value='checksum'):
            result = tool.rehearse('source', 'app', docker)
        return result, target

    def test_empty_caveat_and_synthetic_roundtrip(self):
        report, target = self.run_fake()
        self.assertIn('real application-data recovery remains unproven', report)
        self.assertEqual(target.restore.call_count, 2)
        target.cleanup.assert_called_once()
        self.assertIn(tool.FIXTURE_SQL, [call.args[0] for call in target.sql.call_args_list])

    def test_failure_cleanup_and_state_check(self):
        for failure in (tool.SafetyError('restore failure'), OSError('secret'), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                with self.assertRaises(tool.SafetyError) as caught:
                    self.run_fake(failure=failure, cleanup=OSError('secret'))
                self.assertIn('Cleanup failed', str(caught.exception))
                self.assertIn('target-id', str(caught.exception))
                self.assertNotIn('secret', str(caught.exception))
                self.assertIn('restore failure' if isinstance(failure, tool.SafetyError) else type(failure).__name__, str(caught.exception))

    def test_drift_mismatch_and_changed_state_fail(self):
        for kwargs, message in (({'drift': True}, 'drift'), ({'mismatch': True}, 'mismatch'), ({'changed': True}, 'verification failed')):
            with self.assertRaisesRegex(tool.SafetyError, message):
                self.run_fake(**kwargs)

    def test_sigterm_and_handler_restoration(self):
        previous = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(KeyboardInterrupt):
            tool.terminate(signal.SIGTERM, None)
        with patch.object(tool, 'rehearse', side_effect=KeyboardInterrupt()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tool.main(['--run']), 1)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)


class WALDiagnosticsTests(unittest.TestCase):
    def target(self):
        target = tool.Target(Mock(), 'image', [])
        target.identity = 'a' * 64
        target.attempted = True
        return target

    def test_allowlisted_metadata_only(self):
        raw = (b'rmgr: Heap2 len (rec/tot): 1/2, tx: 0, lsn: 0/10, prev 0/0, desc: PRUNE_ON_ACCESS secret SQL password\n'
               b'rmgr: Heap2 len (rec/tot): 1/2, tx: 0, lsn: 0/18, prev 0/10, desc: PRUNE_ON_ACCESS other rows\n'
               b'rmgr: Evil        len x desc: PASSWORD secret\n'
               b'rmgr: Heap2       len x desc: SECRET credentials\n'
               b'::error::secret Docker error SELECT password\n')
        counts, omitted, decoded, valid = integration.wal_summary(raw, '0/10', '0/20')
        self.assertEqual(dict(counts), {('Heap2', 'PRUNE_ON_ACCESS'): 2})
        self.assertTrue(omitted)
        wal = integration.FixtureWAL(self.target())
        wal.baseline = '0/10'
        with patch.object(wal, 'owned', return_value=True), patch.object(wal, 'position', return_value='0/20'), \
                patch.object(wal, 'read_position', return_value='000000010000000000000001|000000010000000000000001'), \
                patch.object(integration, 'bounded_docker', return_value=(raw, 'unavailable-or-active-eof')) as run:
            report = wal.report()
        self.assertIn('Heap2/PRUNE_ON_ACCESS=2', report)
        self.assertIn('unavailable-or-active-eof', report)
        for secret in ('secret', 'password', 'credentials', 'SELECT', '::error::', 'Evil'):
            self.assertNotIn(secret, report)
        command = run.call_args.args[0]
        self.assertEqual(command[:8], ['exec', wal.source.identity, 'timeout', '-s', 'KILL', '4', 'sh', '-c'])
        self.assertIn('--start=0/10 --end=0/20 --limit=256', command[-1])
        self.assertIn('--path="$PGDATA/pg_wal"', command[-1])
        self.assertTrue(command[-1].endswith('000000010000000000000001 000000010000000000000001'))

    def test_explicit_segments_avoid_zero_preallocated_header(self):
        wal = integration.FixtureWAL(self.target())
        wal.baseline = '0/1000000'
        segments = '000000010000000000000001|000000010000000000000002'
        observed = []
        def docker(args):
            command = args[-1]
            observed.append(command)
            if 'psql' in command:
                return segments.encode(), 'ok'
            if command.endswith(segments.replace('|', ' ')):
                return b'rmgr: Transaction len (rec/tot): 24/24, tx: 1, lsn: 0/1000028, prev 0/FFFFF0, desc: COMMIT suppressed\n', 'ok'
            return (b'pg_waldump: error: invalid WAL segment size in WAL file '
                    b'"000000010000000000000003" (0 bytes)\n', 'unavailable-or-active-eof')
        with patch.object(wal, 'owned', return_value=True), patch.object(wal, 'position', return_value='0/2000000'), \
                patch.object(integration, 'bounded_docker', side_effect=docker):
            report = wal.report()
        self.assertIn('records=1 status=ok', report)
        self.assertIn('Transaction/COMMIT=1', report)
        self.assertIn("pg_walfile_name('0/1000000'::pg_lsn + 1)", observed[0])
        self.assertIn("pg_walfile_name('0/2000000'::pg_lsn)", observed[0])
        self.assertIn('default_transaction_read_only=on', observed[0])
        for command in observed:
            for forbidden in ('checkpoint', 'switch_wal', 'pg_log_standby_snapshot', 'cp ', '--follow', '--save-fullpage'):
                self.assertNotIn(forbidden, command.lower())
        self.assertNotIn('suppressed', report)

    def test_invalid_segments_and_reversed_lsn_do_not_decode(self):
        wal = integration.FixtureWAL(self.target())
        wal.baseline = '0/10'
        for segments in ('', 'secret', 'A' * 24 + '|$(secret)', 'A' * 24 + '|' + 'a' * 24):
            with patch.object(wal, 'owned', return_value=True), patch.object(wal, 'position', return_value='0/20'), \
                    patch.object(wal, 'read_position', return_value=segments), \
                    patch.object(integration, 'bounded_docker') as run:
                self.assertIn('segment names missing', wal.report())
                run.assert_not_called()
        for segments in ('000000000000000000000001|000000000000000000000001',
                         '000000010000000000000001|000000020000000000000001',
                         '000000010000000000000002|000000010000000000000001'):
            with patch.object(wal, 'owned', return_value=True), patch.object(wal, 'position', return_value='0/20'), \
                    patch.object(wal, 'read_position', return_value=segments), \
                    patch.object(integration, 'bounded_docker') as run:
                self.assertIn('segment order or timeline', wal.report())
                run.assert_not_called()
        with patch.object(wal, 'owned', return_value=True), patch.object(wal, 'position', return_value='0/F'), \
                patch.object(integration, 'bounded_docker') as run:
            self.assertIn('LSN order', wal.report())
            run.assert_not_called()

    def test_ownership_mismatch_and_arbitrary_target_refused(self):
        wal = integration.FixtureWAL(self.target())
        with patch.object(integration, 'bounded_docker', return_value=(b'foreign secret', 'ok')) as run:
            self.assertIn('ownership not verified', wal.report())
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][0], 'inspect')
        for attribute, value in (('identity', 'arbitrary'), ('name', 'production'), ('attempted', False),
                                 ('token', 'invalid'), ('protected', {'a' * 64})):
            target = self.target()
            setattr(target, attribute, value)
            with patch.object(integration, 'bounded_docker') as run:
                self.assertFalse(integration.FixtureWAL(target).owned())
                run.assert_not_called()

    def test_missing_baseline_unchanged_and_malformed_lsn(self):
        wal = integration.FixtureWAL(self.target())
        with patch.object(wal, 'owned', return_value=True), patch.object(wal, 'position', return_value='0/10'), \
                patch.object(integration, 'bounded_docker') as run:
            self.assertIn('baseline missing', wal.report())
            wal.start()
            self.assertIn('records=0 status=unchanged', wal.report())
            wal.baseline = '0/10; secret'
            self.assertIn('baseline missing', wal.report())
            run.assert_not_called()
        with patch.object(wal, 'owned', return_value=True), \
                patch.object(integration, 'bounded_docker', return_value=(b'secret\n0/10', 'ok')):
            self.assertIsNone(wal.position())

    def test_readonly_position_and_timeout_safe_start(self):
        wal = integration.FixtureWAL(self.target())
        with patch.object(wal, 'owned', return_value=True), \
                patch.object(integration, 'bounded_docker', return_value=(b'0/10\n', 'ok')) as run:
            self.assertEqual(wal.position(), '0/10')
            command = run.call_args.args[0][-1]
            self.assertIn('default_transaction_read_only=on', command)
            self.assertIn('SELECT pg_current_wal_flush_lsn()::text;', command)
            self.assertNotIn('checkpoint', command.lower())
            self.assertNotIn('switch_wal', command)
        with patch.object(wal, 'owned', side_effect=RuntimeError('secret')):
            wal.start()
        self.assertIsNone(wal.baseline)

    def run_case(self, mode, diagnostic_error=False):
        events = []
        class Probe(unittest.TestCase):
            def setUp(inner):
                inner.addCleanup(lambda: events.append('cleanup'))
                inner.addCleanup(integration.report_failed_fixture, inner)
                if mode == 'partial':
                    raise RuntimeError('original setup')
                inner.wal = Mock()
                def report():
                    events.append('diagnostic')
                    if diagnostic_error:
                        raise RuntimeError('diagnostic secret')
                    return 'WAL evidence: safe'
                inner.wal.report.side_effect = report
            def tearDown(inner):
                if mode == 'teardown':
                    raise RuntimeError('original teardown')
            def runTest(inner):
                if mode == 'teardown':
                    return
                if mode == 'expected':
                    with inner.assertRaises(ValueError):
                        raise ValueError('expected')
                elif mode == 'subtest':
                    with inner.subTest(value=1):
                        inner.fail('original subtest')
                elif mode == 'error':
                    raise ValueError('original error')
                else:
                    inner.fail('original failure')
        result = unittest.TestResult()
        with contextlib.redirect_stderr(io.StringIO()) as output:
            Probe().run(result)
        return result, events, output.getvalue()

    def test_real_unittest_failures_errors_subtests_before_cleanup(self):
        for mode in ('failure', 'error', 'subtest', 'teardown'):
            result, events, output = self.run_case(mode)
            self.assertEqual(events, ['diagnostic', 'cleanup'])
            self.assertEqual(len(result.failures) + len(result.errors), 1)
            self.assertIn('original', (result.failures + result.errors)[0][1])
            self.assertEqual(output, 'WAL evidence: safe\n')

    def test_expected_errors_do_not_emit(self):
        result, events, output = self.run_case('expected')
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(events, ['cleanup'])
        self.assertEqual(output, '')

    def test_partial_setup_and_diagnostic_error_preserve_original_cleanup(self):
        for mode, broken in (('partial', False), ('failure', True)):
            result, events, output = self.run_case(mode, broken)
            self.assertEqual(events[-1], 'cleanup')
            self.assertEqual(len(result.failures) + len(result.errors), 1)
            self.assertIn('original', (result.failures + result.errors)[0][1])
            self.assertIn('unavailable', output)
            captured = output + ''.join(traceback for _, traceback in result.failures + result.errors)
            self.assertNotIn('secret', captured)

    def test_exact_ownership_identity_name_label_and_pre_read_recheck(self):
        target = self.target()
        wal = integration.FixtureWAL(target)
        for identity, name, labels in (
                (target.identity, '/' + target.name, {tool.LABEL: target.token}),
                ('b' * 64, '/' + target.name, {tool.LABEL: target.token}),
                (target.identity, '/foreign', {tool.LABEL: target.token}),
                (target.identity, '/' + target.name, {tool.LABEL: 'foreign'})):
            raw = '\n'.join(json.dumps(value) for value in (identity, name, labels)).encode()
            with patch.object(integration, 'bounded_docker', return_value=(raw, 'ok')):
                self.assertEqual(wal.owned(), identity == target.identity and
                                 name == '/' + target.name and labels[tool.LABEL] == target.token)
        wal.baseline = '0/10'
        with patch.object(wal, 'owned', side_effect=[True, False]), \
                patch.object(wal, 'position', return_value='0/20'), \
                patch.object(wal, 'read_position', return_value='000000010000000000000001|000000010000000000000001'), \
                patch.object(integration, 'bounded_docker') as run:
            self.assertIn('ownership not verified', wal.report())
            run.assert_not_called()

    def record(self, position='0/10'):
        return ('rmgr: Heap len (rec/tot): 24/24, tx: 1, lsn: ' + position +
                ', prev 0/0, desc: INSERT malicious-password SELECT ::error::\n').encode()

    def diagnostic_report(self, raw, status='ok'):
        wal = integration.FixtureWAL(self.target())
        wal.baseline = '0/10'
        with patch.object(wal, 'owned', return_value=True), \
                patch.object(wal, 'position', return_value='0/20'), \
                patch.object(wal, 'read_position', return_value='000000010000000000000001|000000010000000000000001'), \
                patch.object(integration, 'bounded_docker', return_value=(raw, status)):
            return wal.report()

    def test_selection_read_races_and_partial_nonzero_are_incomplete(self):
        # Deterministic stand-ins for pg_waldump errors, not actual WAL recycling.
        for error in (b'could not open file: No such file or directory',
                      b'unexpected pageaddr 0/3000000 in WAL segment'):
            for partial in (b'', self.record()):
                report = self.diagnostic_report(partial + error + b' malicious-password SELECT ::error::',
                                                'unavailable-or-active-eof')
                self.assertIn('status=unavailable-or-active-eof', report)
                self.assertIn('incomplete=true', report)
                self.assertIn('truncated=false', report)
                for payload in ('malicious-password', 'SELECT', '::error::', 'pageaddr', 'could not open'):
                    self.assertNotIn(payload, report)

    def test_zero_exit_malformed_out_of_range_duplicate_and_backwards_lsn(self):
        for raw in (b'', b'malicious-password SELECT ::error::',
                    b'rmgr: Heap len x desc: INSERT\n', self.record()[:-1]):
            report = self.diagnostic_report(raw)
            # A line without its final newline is not a completed decoded record.
            self.assertIn('incomplete=true', report)
            self.assertNotIn('status=ok', report)
        for raw in (self.record('0/F'), self.record('0/20'), self.record('1/10'),
                    self.record() * 2, self.record('0/18') + self.record(),
                    self.record('0/20').replace(b'rmgr: Heap', b'rmgr: Unknown')):
            report = self.diagnostic_report(raw)
            self.assertIn('status=invalid-record-lsn', report)
            self.assertIn('records=0', report)
            self.assertIn('incomplete=true', report)
            self.assertNotIn('Heap/INSERT', report)

    def test_truncation_is_distinct_from_failed_read(self):
        for status in ('timeout', 'byte-limit'):
            report = self.diagnostic_report(self.record(), status)
            self.assertIn('status=' + status, report)
            self.assertIn('incomplete=true', report)
            self.assertIn('truncated=' + str(status == 'byte-limit').lower(), report)
        records = b''.join(self.record('0/' + format(value, 'X')) for value in range(16, 272))
        counts, omitted, decoded, valid = integration.wal_summary(records, '0/10', '0/200')
        self.assertEqual(decoded, integration.WAL_RECORD_LIMIT)
        self.assertTrue(valid)
        self.assertFalse(omitted)
        wal = integration.FixtureWAL(self.target())
        wal.baseline = '0/10'
        with patch.object(wal, 'owned', return_value=True), \
                patch.object(wal, 'position', return_value='0/200'), \
                patch.object(wal, 'read_position', return_value='000000010000000000000001|000000010000000000000001'), \
                patch.object(integration, 'bounded_docker', return_value=(records, 'ok')):
            report = wal.report()
        self.assertIn('truncated=true', report)
        self.assertIn('incomplete=true', report)

    def test_eof_wait_timeout_reaps_client_without_exposing_output(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('secret', 5), None]
        selector = Mock()
        selector.select.return_value = [(None, None)]
        with patch.object(subprocess, 'Popen') as popen, \
                patch.object(integration.selectors, 'DefaultSelector') as select, \
                patch.object(integration.os, 'read', return_value=b''):
            popen.return_value.__enter__.return_value = process
            select.return_value.__enter__.return_value = selector
            raw, status = integration.bounded_docker(['inspect', 'owned'])
        self.assertEqual((raw, status), (b'', 'timeout'))
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

    def test_capture_byte_cap_and_timeout_kill_process(self):
        for timeout in (False, True):
            process = Mock()
            process.poll.return_value = None
            selector = Mock()
            selector.select.return_value = [] if timeout else [(None, None)]
            with patch.object(subprocess, 'Popen') as popen, \
                    patch.object(integration.selectors, 'DefaultSelector') as select, \
                    patch.object(integration.os, 'read', return_value=b'x' * 4096):
                popen.return_value.__enter__.return_value = process
                select.return_value.__enter__.return_value = selector
                raw, status = integration.bounded_docker(['exec', 'owned'])
            self.assertEqual(status, 'timeout' if timeout else 'byte-limit')
            self.assertLessEqual(len(raw), integration.WAL_BYTE_LIMIT)
            process.kill.assert_called_once()
            self.assertEqual(popen.call_args.kwargs['stderr'], subprocess.STDOUT)
            self.assertEqual(popen.call_args.kwargs['stdin'], subprocess.DEVNULL)


if __name__ == '__main__':
    unittest.main()
