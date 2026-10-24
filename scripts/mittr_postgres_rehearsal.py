import argparse
from collections import defaultdict
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time


PREFIX = "mittr-pg-rehearsal-"
LABEL = "io.mittr.postgres-rehearsal"
READ_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=120000 -c lock_timeout=5000 -c row_security=off"
PRELUDE = 'unset PGHOSTADDR PGSERVICE PGSERVICEFILE; export PGPASSWORD="$POSTGRES_PASSWORD" PGUSER="$POSTGRES_USER" PGDATABASE="$POSTGRES_DB" PGHOST=/var/run/postgresql PGPORT=5432 PGCONNECT_TIMEOUT=5; '
STATUS_SQL = "SELECT json_build_array(pg_is_in_recovery(), current_setting('transaction_read_only'), pg_current_wal_lsn()::text);"
DATA_SQL = r"""
SELECT json_build_array('unsupported', count(*)) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='f' AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema';
SELECT format('SELECT json_build_array(''relation'', %L, %L, to_jsonb(t)) FROM ONLY %I.%I t;', n.nspname, c.relname, n.nspname, c.relname) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','m') AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' ORDER BY n.nspname,c.relname
\gexec
SELECT json_build_array('inventory', n.nspname, c.relname, c.relkind) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p','m','v','S') AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema';
SELECT format('SELECT json_build_array(''sequence'', %L, %L, last_value::text, is_called) FROM %I.%I;', n.nspname, c.relname, n.nspname, c.relname) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='S' AND n.nspname !~ '^pg_' ORDER BY n.nspname,c.relname
\gexec
SELECT json_build_array('largeobject', loid, pageno, encode(data,'hex')) FROM pg_largeobject;
"""
FIXTURE_SQL = """
CREATE SCHEMA rehearsal_fixture;
CREATE TABLE rehearsal_fixture.rows (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, value jsonb, payload bytea);
INSERT INTO rehearsal_fixture.rows(value,payload) VALUES ('{"nested":[null,true,1.25,"text"]}',decode('000aff','hex')), ('{"empty":[]}',decode('ff','hex'));
SELECT nextval('rehearsal_fixture.rows_id_seq');
CREATE VIEW rehearsal_fixture.visible AS SELECT id,value FROM rehearsal_fixture.rows;
CREATE MATERIALIZED VIEW rehearsal_fixture.cached AS SELECT * FROM rehearsal_fixture.rows;
"""


class SafetyError(Exception):
    pass


def canonical(value):
    if isinstance(value, dict):
        return ['object', sorted((key, canonical(item)) for key, item in value.items())]
    if isinstance(value, list):
        return ['array', [canonical(item) for item in value]]
    if isinstance(value, Decimal):
        return ['number', str(value)]
    return [type(value).__name__, value]


def fingerprints(output):
    groups = defaultdict(list)
    counts = defaultdict(int)
    for line in output.splitlines():
        row = json.loads(line, parse_float=Decimal)
        if row[0] == 'unsupported':
            if row[1]:
                raise SafetyError('Foreign tables are unsupported')
            continue
        key = tuple(row[:4]) if row[0] == 'inventory' else tuple(row[:3]) if row[0] in ('relation', 'sequence') else (row[0],)
        encoded = json.dumps(canonical(row), ensure_ascii=True, separators=(',', ':')).encode()
        groups[key].append(hashlib.sha256(encoded).hexdigest())
        counts[key] += 1
    return {key: (counts[key], hashlib.sha256(''.join(sorted(rows)).encode()).hexdigest()) for key, rows in groups.items()}


def normalize_schema(data):
    return b'\n'.join(line for line in data.splitlines() if not re.fullmatch(rb'\\(?:un)?restrict [A-Za-z0-9]+', line))


def checksum(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


class Docker:
    def __init__(self, runner=None):
        self.runner = runner or subprocess.run

    def run(self, args, input=None, stdout=None, env=None):
        try:
            feed = {'stdin': input} if hasattr(input, 'read') else {'input': input}
            result = self.runner(['docker', *args], **feed, stdout=stdout or subprocess.PIPE,
                                 stderr=subprocess.PIPE, timeout=180, check=False, env=env)
        except (OSError, subprocess.TimeoutExpired):
            raise SafetyError('Docker execution failed or timed out (details suppressed)') from None
        if result.returncode:
            raise SafetyError('Docker operation failed (details suppressed)')
        return result.stdout

    def inspect(self, container):
        template = '{{json .Id}}\n{{json .Image}}\n{{json .State}}\n{{json .RestartCount}}\n{{json .Config.Labels}}\n{{json .Name}}'
        rows = self.run(['inspect', '--type=container', '--format', template, container]).decode().splitlines()
        identity, image, state, restarts, labels, name = map(json.loads, rows)
        if not state['Running'] or state.get('Paused') or state.get('Restarting') or state.get('Health', {}).get('Status', 'healthy') != 'healthy':
            raise SafetyError('Container must be running, stable and healthy if healthchecked')
        return {'id': identity, 'image': image, 'started': state['StartedAt'], 'restarts': restarts,
                'labels': labels or {}, 'name': name.lstrip('/')}


class Source:
    def __init__(self, docker, identity):
        self.docker = docker
        self.identity = identity

    def read(self, operation, stdout=None):
        commands = {
            'status': 'psql -X -qAt -v ON_ERROR_STOP=1',
            'data': 'psql -X -qAt -v ON_ERROR_STOP=1',
            'schema': 'pg_dump --schema-only --no-owner --no-acl',
            'dump': 'pg_dump --format=custom --no-owner --no-acl',
            'config': 'test -n "$POSTGRES_DB" && test -n "$POSTGRES_USER" && printf ready',
        }
        if operation not in commands:
            raise SafetyError('Source operation not allowlisted')
        command = PRELUDE + 'export PGOPTIONS=' + "'" + READ_OPTIONS + "'; exec " + commands[operation] if operation != 'config' else commands[operation]
        return self.docker.run(['exec', '-i', self.identity, 'sh', '-c', command],
                               input=DATA_SQL.encode() if operation == 'data' else STATUS_SQL.encode() if operation == 'status' else None, stdout=stdout)

    def fingerprint(self):
        return (hashlib.sha256(normalize_schema(self.read('schema'))).hexdigest(), fingerprints(self.read('data').decode()))

    def position(self):
        recovery, readonly, lsn = json.loads(self.read('status'))
        if recovery or readonly != 'on':
            raise SafetyError('Source must be a primary with forced read-only transactions')
        return lsn


class Target:
    def __init__(self, docker, image, protected):
        self.docker = docker
        self.image = image
        self.protected = set(protected)
        self.token = secrets.token_hex(16)
        self.name = PREFIX + self.token
        self.identity = None
        self.attempted = False

    def create(self):
        self.attempted = True
        environment = dict(os.environ, POSTGRES_PASSWORD=secrets.token_urlsafe(32))
        identity = self.docker.run(['create', '--pull=never', '--name', self.name, '--label', LABEL + '=' + self.token,
                                    '--network=none', '--tmpfs', '/var/lib/postgresql:rw,noexec,nosuid,size=1g',
                                    '-e', 'POSTGRES_PASSWORD', '-e', 'POSTGRES_USER=mittr_rehearsal',
                                    '-e', 'POSTGRES_DB=rehearsal', self.image], env=environment).decode().strip()
        if not re.fullmatch('[a-f0-9]{64}', identity) or identity in self.protected:
            raise SafetyError('Invalid temporary container identity')
        self.identity = identity
        self.docker.run(['start', self.identity])
        deadline = time.monotonic() + 60
        while True:
            try:
                self.sql('SELECT 1;')
                break
            except SafetyError:
                if time.monotonic() >= deadline:
                    raise SafetyError('Temporary PostgreSQL readiness timeout') from None
                time.sleep(0.5)

    def execute(self, command, input=None, database='rehearsal'):
        if self.identity is None or self.identity in self.protected or database not in ('rehearsal', 'fixture', 'roundtrip'):
            raise SafetyError('Unsafe target routing')
        return self.docker.run(['exec', '-i', self.identity, 'sh', '-c', PRELUDE + 'export PGDATABASE=' + database + '; exec ' + command], input=input)

    def sql(self, sql, database='rehearsal'):
        return self.execute('psql -X -qAt -v ON_ERROR_STOP=1', sql.encode(), database)

    def fingerprint(self, database='rehearsal'):
        schema = self.execute('pg_dump --schema-only --no-owner --no-acl', database=database)
        return hashlib.sha256(normalize_schema(schema)).hexdigest(), fingerprints(self.sql(DATA_SQL, database).decode())

    def restore(self, path, expected, database='rehearsal'):
        if checksum(path) != expected:
            raise SafetyError('Dump checksum mismatch')
        with path.open('rb') as stream:
            self.execute('pg_restore --exit-on-error --no-owner --no-acl --dbname="$PGDATABASE"', stream, database)

    def cleanup(self):
        if not self.attempted:
            return
        reference = self.identity or self.name
        raw = self.docker.run(['inspect', '--type=container', '--format', '{{json .Id}}\n{{json .Config.Labels}}\n{{json .Name}}', reference])
        identity, labels, name = map(json.loads, raw.decode().splitlines())
        if identity in self.protected or (self.identity and identity != self.identity) or name != '/' + self.name or (labels or {}).get(LABEL) != self.token:
            raise SafetyError('Refusing cleanup of foreign container')
        self.docker.run(['rm', '-f', '-v', identity])


def private_dump(path, action):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        action(stream)
    if path.stat().st_size == 0:
        raise SafetyError('Empty dump')
    return checksum(path)


def compare(left, right):
    if left != right:
        raise SafetyError('Restore completeness mismatch (data suppressed)')


def rehearse(source_name, protected_name, docker=None):
    if source_name == protected_name or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name) or name.startswith(PREFIX) for name in (source_name, protected_name)):
        raise SafetyError('Invalid, identical or reserved container names')
    docker = docker or Docker()
    before = [docker.inspect(name) for name in (source_name, protected_name)]
    if before[0]['id'] == before[1]['id'] or any(item['name'].startswith(PREFIX) for item in before):
        raise SafetyError('Protected containers overlap or use reserved namespace')
    source = Source(docker, before[0]['id'])
    target = Target(docker, before[0]['image'], [item['id'] for item in before])
    errors = []
    report = None
    try:
        source.read('config')
        start = source.position()
        original = source.fingerprint()
        with tempfile.TemporaryDirectory(prefix=PREFIX) as directory:
            dump = Path(directory) / 'database.dump'
            digest = private_dump(dump, lambda stream: source.read('dump', stream))
            after_dump = source.fingerprint()
            if start != source.position() or original != after_dump:
                raise SafetyError('Source drift detected; retry during a quiet window')
            target.create()
            target.restore(dump, digest)
            restored = target.fingerprint()
            final = source.fingerprint()
            if start != source.position() or original != final:
                raise SafetyError('Source drift detected; restore comparison inconclusive')
            compare(original, restored)
            target.sql('CREATE DATABASE fixture;')
            target.sql('CREATE DATABASE roundtrip;')
            target.sql(FIXTURE_SQL, 'fixture')
            fixture = target.fingerprint('fixture')
            fixture_dump = Path(directory) / 'fixture.dump'
            def dump_fixture(stream):
                stream.write(target.execute('pg_dump --format=custom --no-owner --no-acl', database='fixture'))
            fixture_digest = private_dump(fixture_dump, dump_fixture)
            target.restore(fixture_dump, fixture_digest, 'roundtrip')
            compare(fixture, target.fingerprint('roundtrip'))
            counts = original[1]
            tables = sum(1 for key in counts if key[0] == 'inventory' and key[3] in ('r', 'p'))
            rows = sum(value[0] for key, value in counts.items() if key[0] == 'relation')
            report = 'Rehearsal passed: schema, contents and sequences match; {} tables, {} rows. Target-only synthetic roundtrip passed.'.format(tables, rows)
            if not tables or not rows:
                report += ' Source has no tables or no rows; real application-data recovery remains unproven.'
    except BaseException as error:
        errors.append(str(error) if isinstance(error, SafetyError) else type(error).__name__ + ' (details suppressed)')
    finally:
        previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
        interrupt = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            try:
                target.cleanup()
            except BaseException:
                errors.append('Cleanup failed; owned resource may remain: ' + target.name + ' ID=' + (target.identity or 'unknown'))
            try:
                after = [docker.inspect(name) for name in (source_name, protected_name)]
                if before != after:
                    raise SafetyError('Protected container state changed')
            except BaseException:
                errors.append('Protected container identity/start/restart/health verification failed')
        finally:
            signal.signal(signal.SIGTERM, previous)
            signal.signal(signal.SIGINT, interrupt)
    if errors:
        raise SafetyError('; '.join(errors))
    return report + ' Protected containers unchanged; temporary resources removed.'


def terminate(signum, frame):
    raise KeyboardInterrupt()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only PostgreSQL backup/restore rehearsal; Python 3.9+ and local Docker required')
    parser.add_argument('--run', action='store_true', help='Explicitly authorize read-only source access and disposable target writes')
    parser.add_argument('--source-container', default='mittr-postgres-postgres-1')
    parser.add_argument('--protected-container', default='mittr-load-app-1')
    args = parser.parse_args(argv)
    if not args.run:
        parser.print_help()
        return 0
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        print(rehearse(args.source_container, args.protected_container))
        return 0
    except BaseException as error:
        print(str(error) if isinstance(error, SafetyError) else type(error).__name__ + ' (details suppressed)', file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    sys.exit(main())
