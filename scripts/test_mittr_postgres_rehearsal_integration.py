import json
import os
import re
import signal
import unittest
from unittest.mock import patch

from scripts import mittr_postgres_rehearsal as tool


RUN_LABEL = 'io.mittr.postgres-rehearsal-test-run'


class FixtureDocker(tool.Docker):
    def run(self, args, **kwargs):
        run_id = os.environ.get('MITTR_PG_TEST_RUN_ID')
        if run_id:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', run_id):
                raise tool.SafetyError('Invalid fixture run identity')
            if args[0] == 'create':
                args = ['create', '--label', RUN_LABEL + '=' + run_id, *args[1:]]
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
        self.source = tool.Target(self.docker, image, [])
        self.app = tool.Target(self.docker, image, [])
        self.addCleanup(self.cleanup_fixture)
        self.source.create()
        self.app.create()
        self.before = [self.docker.inspect(target.identity) for target in (self.source, self.app)]
        self.source.sql(tool.FIXTURE_SQL)
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


if __name__ == '__main__':
    unittest.main()
