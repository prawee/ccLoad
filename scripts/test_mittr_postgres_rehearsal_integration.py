import contextlib
import io
import json
import os
import re
import signal
import selectors
import subprocess
import sys
import time
from collections import Counter
import unittest
from unittest.mock import patch

from scripts import mittr_postgres_rehearsal as tool


RUN_LABEL = 'io.mittr.postgres-rehearsal-test-run'


WAL_BYTE_LIMIT = 65536
WAL_RECORD_LIMIT = 256
WAL_TIMEOUT = 5
LSN = re.compile(r'[0-9A-F]{1,8}/[0-9A-F]{1,8}')
# Values are constants, never arbitrary words copied out of a WAL description.
WAL_TYPES = {
    'Heap': {'INSERT', 'DELETE', 'UPDATE', 'HOT_UPDATE', 'TRUNCATE', 'CONFIRM', 'LOCK', 'INPLACE'},
    'Heap2': {'PRUNE_ON_ACCESS', 'PRUNE_VACUUM_SCAN', 'PRUNE_VACUUM_CLEANUP', 'PRUNE',
              'VACUUM', 'FREEZE_PAGE', 'VISIBLE', 'MULTI_INSERT', 'LOCK_UPDATED', 'NEW_CID', 'REWRITE'},
    'Btree': {'INSERT_LEAF', 'INSERT_UPPER', 'INSERT_META', 'SPLIT_L', 'SPLIT_R',
              'DEDUP', 'VACUUM', 'DELETE', 'MARK_PAGE_HALFDEAD', 'UNLINK_PAGE',
              'UNLINK_PAGE_META', 'NEWROOT', 'REUSE_PAGE', 'META_CLEANUP'},
    'Transaction': {'COMMIT', 'ABORT', 'PREPARE', 'COMMIT_PREPARED', 'ABORT_PREPARED',
                    'ASSIGNMENT', 'INVALIDATION'},
    'XLOG': {'CHECKPOINT_SHUTDOWN', 'CHECKPOINT_ONLINE', 'NOOP', 'NEXTOID', 'SWITCH',
             'BACKUP_END', 'PARAMETER_CHANGE', 'RESTORE_POINT', 'FPW_CHANGE', 'FPI',
             'FPI_FOR_HINT', 'END_OF_RECOVERY', 'OVERWRITE_CONTRECORD'},
    'Standby': {'LOCK', 'RUNNING_XACTS', 'INVALIDATIONS'},
    'Sequence': {'LOG'},
    'Storage': {'CREATE', 'TRUNCATE'},
}


def bounded_docker(args):
    """Capture in memory only; cap combined stdout/stderr, including error output."""
    output = bytearray()
    status = 'ok'
    with subprocess.Popen(['docker', *args], stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + WAL_TIMEOUT
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(max(0, remaining)):
                        status = 'timeout'
                        break
                    chunk = os.read(process.stdout.fileno(), min(4096, WAL_BYTE_LIMIT - len(output)))
                    if not chunk:
                        process.wait(timeout=max(0.001, deadline - time.monotonic()))
                        status = 'ok' if process.returncode == 0 else 'unavailable-or-active-eof'
                        break
                    output.extend(chunk)
                    if len(output) >= WAL_BYTE_LIMIT:
                        status = 'byte-limit'
                        break
        except subprocess.TimeoutExpired:
            status = 'timeout'
        finally:
            # This kills only the client. Exec commands have an independent
            # in-container timeout; fixture rm -f -v remains the lifecycle backstop.
            if process.poll() is None:
                process.kill()
            process.wait()
    return bytes(output), status


def lsn_value(value):
    high, low = value.split('/')
    return (int(high, 16) << 32) + int(low, 16)


def wal_summary(raw, start, end):
    counts = Counter()
    omitted = False
    previous = lsn_value(start) - 1
    decoded = 0
    for line in raw.decode('ascii', errors='replace').splitlines(keepends=True):
        if not line.endswith('\n'):
            omitted = True
            continue
        match = re.match(
            r'^rmgr:\s+([A-Za-z0-9]+)\s+len \(rec/tot\):\s+\d+/\s*\d+, '
            r'tx:\s+\d+, lsn: (' + LSN.pattern + r'), prev ' + LSN.pattern +
            r', desc: ([A-Z][A-Z0-9_]*)(?:\s|$)', line)
        if not match:
            omitted = True
            continue
        position = lsn_value(match[2])
        if not lsn_value(start) <= position < lsn_value(end) or position <= previous:
            # Do not attribute any counts to this interval if the reader disagrees.
            return Counter(), True, decoded, False
        previous = position
        decoded += 1
        if match[3] in WAL_TYPES.get(match[1], set()):
            counts[(match[1], match[3])] += 1
        else:
            omitted = True
    return counts, omitted, decoded, True


class FixtureWAL:
    """Only the generated source Target is accepted; no CLI/path/SQL input."""
    def __init__(self, source):
        self.source = source
        self.baseline = None

    def owned(self):
        source = self.source
        if (not isinstance(source, tool.Target) or not source.attempted or
                not re.fullmatch(r'[a-f0-9]{64}', source.identity or '') or
                not re.fullmatch(r'[a-f0-9]{32}', source.token) or
                source.identity in source.protected or
                source.name not in (tool.PREFIX + source.token, 'mittr-pg-fixture-source-' + source.token)):
            return False
        raw, status = bounded_docker(['inspect', '--type=container', '--format',
                                     '{{json .Id}}\n{{json .Name}}\n{{json .Config.Labels}}', source.identity])
        if status != 'ok':
            return False
        try:
            identity, name, labels = map(json.loads, raw.decode().splitlines())
            return (identity == source.identity and name == '/' + source.name and
                    (labels or {}).get(tool.LABEL) == source.token)
        except (ValueError, TypeError, AttributeError):
            return False

    def read_position(self, sql):
        # Internal, validated SELECTs only; never filesystem SQL reads or WAL writes.
        command = (tool.PRELUDE + "export PGOPTIONS='-c default_transaction_read_only=on "
                   "-c statement_timeout=3000 -c lock_timeout=1000'; "
                   'exec psql -X -qAt -v ON_ERROR_STOP=1 -c "' + sql + '"')
        if not self.owned():
            return ''
        raw, status = bounded_docker(['exec', self.source.identity, 'timeout', '-s', 'KILL', '4',
                                     'sh', '-c', command])
        return raw.decode('ascii', errors='replace').strip() if status == 'ok' else ''

    def position(self):
        value = self.read_position('SELECT pg_current_wal_flush_lsn()::text;')
        return value if LSN.fullmatch(value) else None

    def start(self):
        try:
            if self.owned():
                self.baseline = self.position()
        except Exception:
            pass  # Diagnostics must never create a fixture failure.

    def report(self):
        if not self.owned():
            return 'WAL evidence: unavailable (ownership not verified)'
        if not self.baseline or not LSN.fullmatch(self.baseline):
            return 'WAL evidence: unavailable (baseline missing)'
        end = self.position()
        if end is None or not LSN.fullmatch(end):
            return 'WAL evidence: baseline=' + self.baseline + ' unavailable (end LSN missing)'
        header = 'WAL evidence: baseline=' + self.baseline + ' end=' + end
        if end == self.baseline:
            return header + ' records=0 status=unchanged'
        if tuple(int(part, 16) for part in end.split('/')) < tuple(int(part, 16) for part in self.baseline.split('/')):
            return header + ' unavailable (LSN order)'
        # Without STARTSEG pg_waldump probes an arbitrary directory entry for the
        # segment size, which can be an all-zero preallocated future segment.
        # +1 selects the segment containing the start byte at an exact boundary;
        # pg_walfile_name(end) intentionally selects the preceding segment there.
        segments = self.read_position("SELECT pg_walfile_name('" + self.baseline +
                                      "'::pg_lsn + 1), pg_walfile_name('" + end + "'::pg_lsn);")
        if not re.fullmatch(r'[0-9A-F]{24}\|[0-9A-F]{24}', segments):
            return header + ' unavailable (segment names missing)'
        # PGDATA is the generated container's own environment, not an SQL/user path.
        command = ('exec pg_waldump --path="$PGDATA/pg_wal" --start=' + self.baseline +
                   ' --end=' + end + ' --limit=' + str(WAL_RECORD_LIMIT) +
                   ' ' + segments.replace('|', ' '))
        start_segment, end_segment = segments.split('|')
        if (start_segment[:8] == '00000000' or start_segment[:8] != end_segment[:8] or
                start_segment[8:] > end_segment[8:]):
            return header + ' unavailable (segment order or timeline)'
        if not self.owned():
            return header + ' unavailable (ownership not verified)'
        # BusyBox timeout in postgres:18-alpine supports -s KILL SECS PROG ARGS.
        # exec replaces the shell; timeout therefore bounds pg_waldump itself,
        # independently of killing the host Docker CLI on timeout/output cap.
        raw, status = bounded_docker(['exec', self.source.identity, 'timeout', '-s', 'KILL', '4',
                                     'sh', '-c', command])
        counts, omitted, decoded, valid = wal_summary(raw, self.baseline, end)
        if not valid:
            status = 'invalid-record-lsn'
        elif status == 'ok' and (omitted or not decoded):
            status = 'unparsed-output'
        truncated = decoded >= WAL_RECORD_LIMIT or status == 'byte-limit'
        incomplete = status != 'ok' or omitted or truncated
        total = sum(counts.values())
        metadata = ' '.join(manager + '/' + kind + '=' + str(count)
                            for (manager, kind), count in sorted(counts.items()))
        return (header + ' records=' + str(total) + ' status=' + status +
                ' record-limit=' + str(WAL_RECORD_LIMIT) +
                ' truncated=' + str(truncated).lower() +
                ' incomplete=' + str(incomplete).lower() +
                ' unparsed=' + str(omitted).lower() + (' ' + metadata if metadata else ''))


def report_failed_fixture(case):
    # unittest records setUp/test/tearDown errors before running LIFO cleanups.
    result = case._outcome.result
    failed = any(test is case or getattr(test, 'test_case', None) is case
                 for test, _ in (*result.failures, *result.errors))
    if failed:
        try:
            evidence = getattr(case, 'wal', None)
            print(evidence.report() if evidence else 'WAL evidence: unavailable (source missing)', file=sys.stderr)
        except Exception:
            print('WAL evidence: unavailable (diagnostic failed)', file=sys.stderr)


class FixtureDocker(tool.Docker):
    def run(self, args, **kwargs):
        run_id = os.environ.get('MITTR_PG_TEST_RUN_ID')
        if run_id:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', run_id):
                raise tool.SafetyError('Invalid fixture run identity')
            if args[0] == 'create':
                args = ['create', '--label', RUN_LABEL + '=' + run_id, *args[1:]]
        return super().run(args, **kwargs)


class QuietSourceDocker(FixtureDocker):
    def run(self, args, **kwargs):
        if args[0] == 'create':
            args = [*args, 'postgres', '-c', 'autovacuum=off',
                    '-c', 'checkpoint_timeout=1d', '-c', 'wal_level=minimal',
                    '-c', 'max_wal_senders=0']
        return super().run(args, **kwargs)


RELATIONAL_SQL = """
CREATE SCHEMA relational_source;
CREATE SCHEMA relational_detail;
CREATE TABLE relational_source.parents (
    id bigserial PRIMARY KEY,
    name text NOT NULL UNIQUE,
    balance numeric(24,8) NOT NULL CHECK (balance >= 0),
    created_at timestamptz NOT NULL,
    metadata jsonb,
    payload bytea,
    optional text
);
CREATE TABLE relational_detail.children (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    parent_id bigint NOT NULL,
    code text NOT NULL UNIQUE,
    quantity integer NOT NULL CHECK (quantity > 0),
    CONSTRAINT child_parent_fk FOREIGN KEY (parent_id) REFERENCES relational_source.parents(id)
);
CREATE TABLE relational_detail.empty_rows (id integer PRIMARY KEY);
CREATE INDEX child_parent_idx ON relational_detail.children(parent_id);
CREATE VIEW relational_detail.joined AS
    SELECT c.id, p.name, c.code FROM relational_detail.children c
    JOIN relational_source.parents p ON p.id=c.parent_id;
CREATE FUNCTION relational_detail.validate_code() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.code = '' THEN RAISE EXCEPTION 'empty code'; END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER child_code BEFORE INSERT OR UPDATE ON relational_detail.children
    FOR EACH ROW EXECUTE FUNCTION relational_detail.validate_code();
INSERT INTO relational_source.parents(name,balance,created_at,metadata,payload,optional) VALUES
    ('ภาษาไทย 雪',1234567890123456.12345678,'2025-01-02 03:04:05.123456+07',
     '{"nested":[null,true,"雪"],"precise":1.1234567890123456789}',decode('000aff00','hex'),NULL),
    ('second',0.00000001,'2000-02-29 23:59:59.999999-05',NULL,decode('','hex'),'');
INSERT INTO relational_detail.children(parent_id,code,quantity) VALUES
    (1,'ลูกหนึ่ง',1),(1,'雪-two',2),(2,'third',3);
SELECT nextval('relational_source.parents_id_seq');
SELECT nextval('relational_detail.children_id_seq');
"""


@unittest.skipUnless(os.environ.get('MITTR_PG_DOCKER_INTEGRATION') == '1', 'Opt-in Docker fixtures only')
class DockerRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.docker = FixtureDocker()
        image = self.docker.run(['image', 'inspect', '--format', '{{.Id}}', 'postgres:18-alpine']).decode().strip()
        self.source = tool.Target(QuietSourceDocker(), image, [])
        self.app = tool.Target(self.docker, image, [])
        self.addCleanup(self.cleanup_fixture)
        self.addCleanup(report_failed_fixture, self)
        self.wal = FixtureWAL(self.source)
        self.source.create()
        self.wal.start()  # Also available if later fixture setup fails.
        self.app.create()
        self.before = [self.docker.inspect(target.identity) for target in (self.source, self.app)]
        self.source.sql(tool.FIXTURE_SQL)
        self.source.sql('VACUUM (FREEZE, ANALYZE);')
        self.wal.start()  # Before fingerprint reads: retain evidence of read-induced maintenance.
        self.original = self.source.fingerprint()
        self.leftovers = []
        self.targets = []
        self.dump_paths = []

    def assert_removed(self, targets):
        for target in targets:
            self.assertTrue(target.name.startswith((tool.PREFIX, 'mittr-pg-fixture-source-', 'mittr-pg-fixture-app-')))
            for command in (['ps', '-aq'], ['volume', 'ls', '-q'], ['network', 'ls', '-q']):
                remaining = self.docker.run([*command, '--filter', 'label=' + tool.LABEL + '=' + target.token])
                self.assertEqual(remaining.strip(), b'')

    def cleanup_fixture(self):
        targets = [*getattr(self, 'targets', []), self.app, self.source]
        try:
            for target in targets:
                remaining = self.docker.run(['ps', '-aq', '--filter', 'label=' + tool.LABEL + '=' + target.token])
                if remaining.strip():
                    tool.Target.cleanup(target)
        finally:
            self.assert_removed(targets)
            for path in getattr(self, 'dump_paths', []):
                self.assertFalse(path.parent.exists())

    def rehearsal(self, target_class=tool.Target):
        source_name = 'mittr-pg-fixture-source-' + self.source.token
        app_name = 'mittr-pg-fixture-app-' + self.app.token
        for target, name in ((self.source, source_name), (self.app, app_name)):
            if target.name != name:
                self.docker.run(['rename', target.identity, name])
                target.name = name
        self.before = [self.docker.inspect(target.identity) for target in (self.source, self.app)]
        targets = self.targets
        paths = self.dump_paths
        class TrackedTarget(target_class):
            def create(inner):
                targets.append(inner)
                super().create()

            def restore(inner, path, expected, database='rehearsal'):
                paths.append(path)
                return super().restore(path, expected, database)
        with patch.object(tool, 'Target', TrackedTarget):
            return tool.rehearse(source_name, app_name, self.docker)

    def assert_unchanged(self):
        self.assertEqual(self.original, self.source.fingerprint())
        self.assertEqual(self.before, [self.docker.inspect(target.identity) for target in (self.source, self.app)])
        self.assertEqual(self.source.sql('SELECT 1;').strip(), b'1')
        self.assertEqual(self.app.sql('SELECT 1;').strip(), b'1')
        self.assert_removed(self.targets)
        for path in self.dump_paths:
            self.assertFalse(path.parent.exists())

    def relational_source(self):
        self.source.sql(RELATIONAL_SQL)
        self.source.sql('VACUUM (FREEZE, ANALYZE);')
        self.original = tool.Source(self.docker, self.source.identity).fingerprint()
        self.assertEqual(self.original, self.source.fingerprint())
        self.assert_relational(self.source)

    def assert_relational(self, target):
        schema, contents = target.fingerprint()
        self.assertEqual((schema, contents), self.original)
        for namespace, table, count in (('relational_source', 'parents', 2), ('relational_detail', 'children', 3)):
            self.assertEqual(contents[('relation', namespace, table)][0], count)
        self.assertIn(('inventory', 'relational_detail', 'empty_rows', 'r'), contents)
        self.assertNotIn(('relation', 'relational_detail', 'empty_rows'), contents)
        for namespace, sequence, value in (('relational_source', 'parents_id_seq', '3'), ('relational_detail', 'children_id_seq', '4')):
            expected = tool.fingerprints(json.dumps(['sequence', namespace, sequence, value, True]))
            self.assertEqual(contents[('sequence', namespace, sequence)], expected[('sequence', namespace, sequence)])
        fk = target.sql("SELECT count(*) FROM pg_constraint WHERE conname='child_parent_fk' AND contype='f' AND conrelid='relational_detail.children'::regclass AND confrelid='relational_source.parents'::regclass AND convalidated;")
        self.assertEqual(fk.strip(), b'1')
        self.assertEqual(target.sql('SELECT count(*) FROM relational_detail.joined;').strip(), b'3')
        return schema, contents

    def test_nonempty_relational_restore_schema_data_and_constraints(self):
        self.relational_source()
        checked = []
        case = self
        class VerifiedTarget(tool.Target):
            def restore(inner, path, expected, database='rehearsal'):
                super().restore(path, expected, database)
                if database != 'rehearsal':
                    return
                checked.append(case.assert_relational(inner))
                for sql in (
                    "INSERT INTO relational_detail.children(id,parent_id,code,quantity) OVERRIDING SYSTEM VALUE VALUES (900,999,'orphan',1);",
                    "DELETE FROM relational_source.parents WHERE id=1;",
                    "UPDATE relational_source.parents SET name=(SELECT name FROM relational_source.parents WHERE id=1) WHERE id=2;",
                    "UPDATE relational_detail.children SET quantity=0 WHERE id=1;",
                    "UPDATE relational_detail.children SET code='' WHERE id=1;",
                ):
                    with case.assertRaises(tool.SafetyError):
                        inner.sql(sql)
                case.assert_relational(inner)
                case.assertEqual(inner.sql("BEGIN; SELECT nextval('relational_source.parents_id_seq'), nextval('relational_detail.children_id_seq'); ROLLBACK;").strip(), b'4|5')
                inner.sql("SELECT setval('relational_source.parents_id_seq',3,true), setval('relational_detail.children_id_seq',4,true);")
                case.assert_relational(inner)
        report = self.rehearsal(VerifiedTarget)
        self.assertEqual(len(checked), 1)
        self.assertIn('4 tables, 9 rows', report)
        self.assertNotIn('recovery remains unproven', report)
        self.assert_unchanged()

    def test_dump_checksum_tamper_rejected_before_pg_restore(self):
        self.relational_source()
        restored = []
        tampered = []
        case = self
        class TamperedTarget(tool.Target):
            def execute(inner, command, input=None, database='rehearsal'):
                if command.startswith('pg_restore'):
                    restored.append(database)
                return super().execute(command, input, database)

            def restore(inner, path, expected, database='rehearsal'):
                case.assertEqual(tool.checksum(path), expected)
                with path.open('ab') as stream:
                    stream.write(b'synthetic checksum corruption')
                case.assertNotEqual(tool.checksum(path), expected)
                tampered.append(path)
                return super().restore(path, expected, database)
        with self.assertRaisesRegex(tool.SafetyError, '^Dump checksum mismatch$'):
            self.rehearsal(TamperedTarget)
        self.assertEqual(len(tampered), 1)
        self.assertEqual(restored, [])
        self.assert_unchanged()

    def corrupt_restored(self, sql, schema_changed, child_count):
        self.relational_source()
        checked = []
        case = self
        class CorruptedTarget(tool.Target):
            def restore(inner, path, expected, database='rehearsal'):
                super().restore(path, expected, database)
                case.assertEqual(database, 'rehearsal')
                before = case.assert_relational(inner)
                inner.sql(sql)
                after = inner.fingerprint()
                case.assertEqual(before[0] != after[0], schema_changed)
                case.assertEqual(before[1] == after[1], schema_changed)
                case.assertEqual(after[1][('relation', 'relational_detail', 'children')][0], child_count)
                checked.append(after)
        with self.assertRaisesRegex(tool.SafetyError, '^Restore completeness mismatch \\(data suppressed\\)$'):
            self.rehearsal(CorruptedTarget)
        self.assertEqual(len(checked), 1)
        self.assert_unchanged()

    def test_deleted_restored_child_detected(self):
        self.corrupt_restored('DELETE FROM relational_detail.children WHERE id=1;', False, 2)

    def test_changed_restored_child_detected_with_same_count(self):
        self.corrupt_restored('UPDATE relational_detail.children SET quantity=99 WHERE id=1;', False, 3)

    def test_dropped_restored_fk_detected_with_unchanged_data(self):
        self.corrupt_restored('ALTER TABLE relational_detail.children DROP CONSTRAINT child_parent_fk;', True, 3)

    def active_wal_change(self):
        self.wal.start()
        self.assertIsNotNone(self.wal.baseline)
        before = self.source.sql('SELECT pg_walfile_name(pg_current_wal_insert_lsn() + 1);').strip()
        self.source.sql("BEGIN; SET LOCAL synchronous_commit=on; "
                        "UPDATE rehearsal_fixture.rows SET value='{}' WHERE id=1; "
                        "INSERT INTO rehearsal_fixture.rows(value) VALUES ('{}'); COMMIT;")
        return before

    def assert_active_wal_report(self, report, before):
        after = self.source.sql('SELECT pg_walfile_name(pg_current_wal_insert_lsn() + 1);').strip()
        self.assertEqual(before, after)
        positions = re.search(r'baseline=(' + LSN.pattern + r') end=(' + LSN.pattern + r')', report)
        self.assertIsNotNone(positions)
        segments = self.source.sql("SELECT pg_walfile_name('" + positions[1] + "'::pg_lsn + 1), "
                                   "pg_walfile_name('" + positions[2] + "'::pg_lsn);").strip().split(b'|')
        self.assertEqual(segments, [before, before])
        self.assertRegex(report, r' records=[1-9][0-9]* status=ok ')
        self.assertRegex(report, r'Heap/(?:HOT_UPDATE|UPDATE)=[1-9][0-9]*')
        self.assertRegex(report, r'Heap/INSERT=[1-9][0-9]*')
        self.assertRegex(report, r'Transaction/COMMIT=[1-9][0-9]*')
        self.assertIn('truncated=false', report)
        self.assertIn('incomplete=false', report)
        self.assertIn('unparsed=false', report)
        self.assertNotIn('unavailable', report)
        self.assertNotIn('XLOG/CHECKPOINT', report)
        self.assertNotIn('XLOG/SWITCH', report)

    def test_active_segment_decodes_committed_records(self):
        before = self.active_wal_change()
        self.assert_active_wal_report(self.wal.report(), before)

    def test_failure_hook_decodes_active_records_before_cleanup(self):
        outer = self
        events = []
        class DeliberateFailure(unittest.TestCase):
            def setUp(inner):
                inner.wal = outer.wal
                def cleanup():
                    # The actual hook must have decoded while the fixture still exists.
                    outer.assert_active_wal_report(output.getvalue(), events[0])
                    events.append('cleanup')
                inner.addCleanup(cleanup)
                inner.addCleanup(report_failed_fixture, inner)
            def runTest(inner):
                events.append(outer.active_wal_change())
                inner.fail('deliberate synthetic failure')
        result = unittest.TestResult()
        with contextlib.redirect_stderr(io.StringIO()) as output:
            DeliberateFailure().run(result)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.errors, [])
        self.assertIn('deliberate synthetic failure', result.failures[0][1])
        self.assertEqual(events[1:], ['cleanup'])
        report = output.getvalue()
        self.assertEqual(report.count('WAL evidence:'), 1)
        self.assert_active_wal_report(report, events[0])

    def test_injected_wal_read_failure_redacts_and_cleans_real_fixtures(self):
        # Real lifecycle/rehearsal; only the diagnostic WAL read is replaced.
        self.rehearsal()
        self.assert_unchanged()
        self.active_wal_change()
        fingerprint = self.source.fingerprint()
        before = [self.docker.inspect(target.identity) for target in (self.source, self.app)]
        original_bounded = bounded_docker
        injected = []
        payload = 'malicious-password SELECT private_rows ::error::injected'
        outer = self
        def failing_read(args):
            if args[0] == 'exec' and 'exec pg_waldump ' in args[-1]:
                injected.append(args)
                return (('rmgr: Heap len (rec/tot): 24/24, tx: 1, lsn: ' + self.wal.baseline +
                         ', prev 0/0, desc: INSERT ' + payload + '\n' +
                         'pg_waldump: error: unexpected pageaddr ' + payload + '\n').encode(),
                        'unavailable-or-active-eof')
            return original_bounded(args)
        class DeliberateFailure(unittest.TestCase):
            def setUp(inner):
                inner.wal = outer.wal
                inner.addCleanup(outer.cleanup_fixture)
                def unchanged_before_removal():
                    outer.assertEqual(fingerprint, outer.source.fingerprint())
                    outer.assertEqual(before, [outer.docker.inspect(target.identity)
                                              for target in (outer.source, outer.app)])
                inner.addCleanup(unchanged_before_removal)
                inner.addCleanup(report_failed_fixture, inner)
            def runTest(inner):
                inner.fail('original controlled fixture failure')
        result = unittest.TestResult()
        with patch(__name__ + '.bounded_docker', side_effect=failing_read), \
                contextlib.redirect_stderr(io.StringIO()) as output:
            DeliberateFailure().run(result)
        self.assertEqual(len(injected), 1)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.errors, [])
        self.assertIn('original controlled fixture failure', result.failures[0][1])
        self.assertIn('incomplete=true', output.getvalue())
        self.assertIn('status=unavailable-or-active-eof', output.getvalue())
        captured = output.getvalue() + ''.join(traceback for _, traceback in result.failures + result.errors)
        for secret in (payload, 'malicious-password', 'private_rows', 'SELECT', '::error::', 'unexpected pageaddr'):
            self.assertNotIn(secret, captured)
        self.assert_removed([self.source, self.app, *self.targets])

    def test_internal_timeout_terminates_exec_child(self):
        # Verify Alpine's actual timeout behavior rather than inferring it from
        # killing a host-side Docker CLI. No PostgreSQL process is signalled.
        started = time.monotonic()
        raw, status = bounded_docker(['exec', self.source.identity, 'timeout', '-s', 'KILL', '1',
                                     'sh', '-c', 'exec sleep 30'])
        self.assertEqual(status, 'unavailable-or-active-eof')
        self.assertLess(time.monotonic() - started, WAL_TIMEOUT)
        processes = self.docker.run(['exec', self.source.identity, 'ps', '-o', 'pid,args'])
        self.assertNotIn(b'sleep 30', processes)
        self.assert_unchanged()

    def test_quiet_source_across_background_interval(self):
        case = self
        class SlowTarget(tool.Target):
            def create(inner):
                case.source.sql('SELECT pg_sleep(65);')
                super().create()
        report = self.rehearsal(SlowTarget)
        self.assertIn('synthetic roundtrip passed', report)
        self.assert_unchanged()

    def test_source_mutation_during_restore_rejected(self):
        case = self
        class MutatingTarget(tool.Target):
            def restore(inner, path, expected, database='rehearsal'):
                super().restore(path, expected, database)
                case.source.sql("UPDATE rehearsal_fixture.rows SET value='{}' WHERE id=1;")
        with self.assertRaisesRegex(tool.SafetyError, '^Source drift detected; restore comparison inconclusive$'):
            self.rehearsal(MutatingTarget)
        self.assertNotEqual(self.original, self.source.fingerprint())
        self.source.sql('UPDATE rehearsal_fixture.rows SET value=$${"nested":[null,true,1.25,"text"]}$$ WHERE id=1;')
        self.assert_unchanged()

    def test_repeat_success(self):
        for _ in range(2):
            report = self.rehearsal()
            self.assertIn('1 tables, 4 rows', report)
            self.assertIn('synthetic roundtrip passed', report)
            self.assert_unchanged()

    def test_real_restore_failure_cleanup(self):
        seen = []
        class FailingTarget(tool.Target):
            def create(inner):
                super().create()
                seen.append(inner)

            def restore(inner, path, expected, database='rehearsal'):
                inner.sql('CREATE TABLE rehearsal_fixture_conflict (id int);')
                inner.sql('CREATE SCHEMA rehearsal_fixture;')
                super().restore(path, expected, database)
        with self.assertRaisesRegex(tool.SafetyError, 'Docker operation failed'):
            self.rehearsal(FailingTarget)
        self.assert_unchanged()
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.docker.run(['ps', '-aq', '--filter', 'label=' + tool.LABEL + '=' + seen[0].token]).strip(), b'')

    def test_cleanup_failure_reports_and_recover_owned(self):
        leftovers = self.leftovers
        class CleanupFailure(tool.Target):
            def cleanup(inner):
                leftovers.append(inner)
                raise tool.SafetyError('Injected cleanup failure')
        with self.assertRaisesRegex(tool.SafetyError, 'Cleanup failed; owned resource may remain:') as caught:
            self.rehearsal(CleanupFailure)
        target = leftovers.pop()
        self.assertIn(target.identity, str(caught.exception))
        self.assertEqual(self.docker.inspect(target.identity)['labels'][tool.LABEL], target.token)
        tool.Target.cleanup(target)
        self.assert_unchanged()
        self.assertEqual(self.docker.run(['ps', '-aq', '--filter', 'label=' + tool.LABEL + '=' + target.token]).strip(), b'')

    def test_sigterm_during_target_restore_cleans(self):
        seen = []
        class InterruptedTarget(tool.Target):
            def restore(inner, path, expected, database='rehearsal'):
                seen.append(inner)
                os.kill(os.getpid(), signal.SIGTERM)
        previous = signal.signal(signal.SIGTERM, tool.terminate)
        try:
            with self.assertRaisesRegex(tool.SafetyError, 'KeyboardInterrupt'):
                self.rehearsal(InterruptedTarget)
        finally:
            signal.signal(signal.SIGTERM, previous)
        self.assert_unchanged()
        self.assertEqual(self.docker.run(['ps', '-aq', '--filter', 'label=' + tool.LABEL + '=' + seen[0].token]).strip(), b'')


class RecyclingSourceDocker(FixtureDocker):
    """Separate pressure fixture; never change the quiet rehearsal sources."""
    def run(self, args, **kwargs):
        if args[0] == 'create':
            args = ['create', '--memory=512m', '--cpus=1', '--pids-limit=128',
                    '-e', 'POSTGRES_INITDB_ARGS=--wal-segsize=1', *args[1:],
                    'postgres', '-c', 'autovacuum=off', '-c', 'checkpoint_timeout=30s',
                    '-c', 'checkpoint_completion_target=0',
                    # 2MB is valid but can recycle the COPY's baseline before
                    # the reader starts. 16MB keeps setup below that threshold.
                    '-c', 'max_wal_size=16MB',
                    '-c', 'min_wal_size=2MB', '-c', 'wal_level=minimal',
                    '-c', 'max_wal_senders=0']
        return super().run(args, **kwargs)


@unittest.skipUnless(os.environ.get('MITTR_PG_DOCKER_INTEGRATION') == '1', 'Opt-in Docker fixtures only')
class AutomaticRecyclingTests(unittest.TestCase):
    def test_postgres_recycles_during_real_diagnostic_read(self):
        docker = RecyclingSourceDocker()
        image = docker.run(['image', 'inspect', '--format', '{{.Id}}', 'postgres:18-alpine']).decode().strip()
        source = tool.Target(docker, image, [])
        self.addCleanup(self.remove_fixture, source)
        source.create()
        wal = FixtureWAL(source)
        self.assertTrue(wal.owned())
        self.assertEqual(source.sql("SELECT current_setting('wal_segment_size'), "
                                    "current_setting('checkpoint_timeout'), current_setting('max_wal_size'), "
                                    "current_setting('min_wal_size'), current_setting('wal_recycle');").strip(),
                         b'1MB|30s|16MB|2MB|on')
        source.sql('CREATE TABLE recycle_rows (payload text);')
        wal.start()
        # COPY produces large MULTI_INSERT descriptions: the anonymous pipe
        # blocks the helper well before its 256-record limit, even if scheduled
        # ahead of the shell. No raw WAL/output file or fake reader is involved.
        sentinel = b'synthetic-private-recycle-marker!'
        source.execute('psql -X -qAt -v ON_ERROR_STOP=1 -c "SET synchronous_commit=on;" '
                       '-c "COPY recycle_rows FROM STDIN;"',
                       (sentinel + b'\n') * 26000)
        original_bounded = bounded_docker
        evidence = {}

        def scheduled_read(args):
            if args[0] != 'exec' or not args[-1].startswith('exec pg_waldump '):
                return original_bounded(args)
            self.assertTrue(wal.owned())
            before = self.segments(source)
            command = args[-1]
            # The PID is emitted by the same shell that execs pg_waldump.
            # The consuming shell stops that exact child after a decoded line,
            # then forwards the unmodified line and remaining real output.
            pipeline = ("{ timeout -s KILL 110 sh -c 'echo $$; " + command +
                        "'; status=$?; printf '\\nREADER_EXIT=%s\\n' \"$status\"; } 2>&1 | "
                        "{ IFS= read -r pid; printf '%s\\n' \"$pid\"; "
                        "while IFS= read -r line; do case \"$line\" in rmgr:*) "
                        "kill -STOP \"$pid\" || exit 1; printf '%s\\n' \"$line\"; break;; "
                        "*) printf '%s\\n' \"$line\";; esac; done; cat; }")
            output = bytearray()
            with subprocess.Popen(['docker', 'exec', source.identity, 'timeout', '-s', 'KILL', '115',
                                   'sh', '-c', pipeline], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        deadline = time.monotonic() + 100
                        while b'\nrmgr:' not in output or not output.endswith(b'\n'):
                            self.assertTrue(selector.select(5), 'reader did not produce a decoded line')
                            chunk = os.read(process.stdout.fileno(), 4096)
                            self.assertTrue(chunk, 'reader exited before pause')
                            output.extend(chunk)
                            self.assertLess(len(output), WAL_BYTE_LIMIT)
                        pid, raw = bytes(output).split(b'\n', 1)
                        self.assertTrue(re.fullmatch(rb'[1-9][0-9]*', pid), 'reader PID unavailable (output suppressed)')
                        pid = pid.decode()
                        # COPY may emit MULTI_INSERT+INIT, deliberately not
                        # allowlisted by the summary parser. Validate only the
                        # actual reader's metadata/interval here, not row text.
                        positions = re.findall(rb'(?m)^rmgr: .*?, lsn: (' + LSN.pattern.encode() + rb'), prev ', raw)
                        self.assertTrue(positions, 'reader has not decoded a WAL record')
                        decoded = len(positions)
                        self.assertTrue(all(lsn_value(wal.baseline) <= lsn_value(p.decode()) <
                                            lsn_value(wal.position()) for p in positions))
                        self.assert_stopped(source, pid)
                        opened = self.open_segments(source, pid)
                        self.assertEqual(len(opened), 1)
                        current = opened[0]
                        # A later interval segment is not yet open/read. Reusing
                        # its inode must not be mistaken for a stable snapshot.
                        end_name = command.split()[-1]
                        self.assertLess(current, end_name)
                        self.assertIn(end_name, before)
                        read_started = time.monotonic()
                        recycled = None
                        for batch in range(64):
                            self.assertLess(time.monotonic(), deadline, 'automatic recycling deadline')
                            source.sql("SET statement_timeout='3s'; SET synchronous_commit=on; "
                                       "INSERT INTO recycle_rows SELECT repeat('new-generation-', 128) "
                                       "FROM generate_series(1,2048);")
                            after = self.segments(source)
                            reused = [name for name, identity in after.items()
                                      if identity == before[end_name] and name > end_name]
                            if end_name not in after and reused:
                                recycled = reused[0]
                                break
                            time.sleep(0.1)
                        self.assertIsNotNone(recycled, 'PostgreSQL did not recycle the unopened interval segment')
                        self.assert_stopped(source, pid)
                        self.assertEqual(self.open_segments(source, pid), opened)
                        recycled_at = time.monotonic()
                        evidence.update(old=end_name, new=recycled, identity=before[end_name],
                                        batches=batch + 1, decoded_before_pause=decoded,
                                        read_started=read_started, recycled_at=recycled_at)
                        docker.run(['exec', source.identity, 'kill', '-CONT', pid])
                        output = bytearray(raw)
                        while True:
                            remaining = deadline - time.monotonic()
                            self.assertGreater(remaining, 0, 'resumed reader deadline')
                            self.assertTrue(selector.select(remaining), 'resumed reader timeout')
                            chunk = os.read(process.stdout.fileno(), min(4096, WAL_BYTE_LIMIT - len(output)))
                            if not chunk:
                                break
                            output.extend(chunk)
                            self.assertLess(len(output), WAL_BYTE_LIMIT, 'real reader exceeded output bound')
                        process.wait(timeout=5)
                        self.assertEqual(process.returncode, 0, 'reader forwarding shell failed')
                        self.assertTrue(b'READER_EXIT=1\n' in output, 'reader did not reject recycled interval')
                        output = output[:output.rfind(b'\nREADER_EXIT=')]
                        evidence['resumed_at'] = time.monotonic()
                        evidence['bytes'] = len(output)
                        # Require actual missing-file/page validation, not a cap
                        # or watchdog failure masquerading as recycling evidence.
                        self.assertTrue(b'could not find file' in output or b'pageaddr' in output,
                                        'reader failed for a reason other than recycled WAL')
                        return bytes(output), 'unavailable-or-active-eof'
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()

        with patch(__name__ + '.bounded_docker', side_effect=scheduled_read):
            report = wal.report()
        self.assertTrue(evidence, 'actual pg_waldump path was not executed')
        self.assertLess(evidence['read_started'], evidence['recycled_at'])
        self.assertLess(evidence['recycled_at'], evidence['resumed_at'])
        self.assertIn('status=unavailable-or-active-eof', report)
        self.assertIn('incomplete=true', report)
        self.assertIn('truncated=false', report)
        self.assertNotIn(sentinel.decode(), report)
        self.assertNotIn('new-generation', report)
        self.assertNotIn('could not find file', report)
        self.assertNotIn('pageaddr', report)
        self.assertEqual(source.sql('SELECT 1;').strip(), b'1')
        # Sanitized evidence only, useful when checking repeated real runs.
        print('Automatic recycling: ' + json.dumps(evidence, sort_keys=True), file=sys.stderr)

    def remove_fixture(self, source):
        if source.attempted:
            source.cleanup()
        for command in (['ps', '-aq'], ['volume', 'ls', '-q'], ['network', 'ls', '-q']):
            self.assertEqual(source.docker.run([*command, '--filter', 'label=' + tool.LABEL + '=' + source.token]).strip(), b'')

    def segments(self, source):
        self.assertTrue(FixtureWAL(source).owned())
        raw = source.docker.run(['exec', source.identity, 'sh', '-c',
                                 'stat -c "%n %d:%i" "$PGDATA"/pg_wal/????????????????????????'])
        result = {}
        for line in raw.decode().splitlines():
            path, identity = line.split()
            name = path.rsplit('/', 1)[-1]
            self.assertRegex(name, r'^[0-9A-F]{24}$')
            self.assertRegex(identity, r'^[0-9]+:[0-9]+$')
            result[name] = identity
        return result

    def assert_stopped(self, source, pid):
        raw = source.docker.run(['exec', source.identity, 'cat', '/proc/' + pid + '/status'])
        self.assertRegex(raw, rb'(?m)^Name:\s+pg_waldump$')
        self.assertRegex(raw, rb'(?m)^State:\s+T ')

    def open_segments(self, source, pid):
        raw = source.docker.run(['exec', source.identity, 'sh', '-c',
                                 'for fd in /proc/' + pid + '/fd/*; do readlink "$fd"; done'])
        return sorted(path.removesuffix(' (deleted)').rsplit('/', 1)[-1]
                      for path in raw.decode().splitlines()
                      if re.search(r'/pg_wal/[0-9A-F]{24}(?: \(deleted\))?$', path))


if __name__ == '__main__':
    unittest.main()
