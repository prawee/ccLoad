import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile


class SafetyError(Exception):
    pass


def safe_path(value):
    if ".." in Path(value).parts:
        raise SafetyError("Parent traversal paths are not supported")
    path = Path(os.path.abspath(value))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise SafetyError("Symlink paths are not supported")
    return path


def separate(left, right):
    if left == right or left in right.parents or right in left.parents:
        raise SafetyError("Source and destination must not overlap")


def data_path(value):
    path = safe_path(value)
    if path in (Path.cwd(), *Path.cwd().parents, Path.home(), *Path.home().parents) or len(path.parts) < 3:
        raise SafetyError("Refusing unsafe data directory")
    if not path.is_dir():
        raise SafetyError("Data directory must already exist")
    return path


def inventory(root):
    if root.is_symlink() or not root.is_dir():
        raise SafetyError("Expected an ordinary directory")
    files, directories = {}, []
    def walk_error(error):
        raise error
    for current, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
        for name in sorted(dirs + names):
            path = Path(current) / name
            mode = path.lstat().st_mode
            relative = path.relative_to(root).as_posix()
            if stat.S_ISDIR(mode):
                directories.append(relative)
            elif stat.S_ISREG(mode) and path.stat().st_nlink == 1:
                files[relative] = path
            else:
                raise SafetyError("Only ordinary directories and non-linked files are allowed")
    return files, sorted(directories)


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def copy_private(source, target):
    files, directories = inventory(source)
    target.mkdir(mode=0o700)
    for name in sorted(directories, key=lambda item: (item.count("/"), item)):
        (target / name).mkdir(mode=0o700)
    for name, path in files.items():
        fd = os.open(target / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as dst, path.open("rb") as src:
            shutil.copyfileobj(src, dst)


def sqlite_check(root, primary):
    files, _ = inventory(root)
    if primary not in files:
        raise SafetyError("Configured SQLite database is missing")
    databases = []
    for name, path in files.items():
        with path.open("rb") as stream:
            header = stream.read(16)
        if name == primary or path.suffix.lower() in (".db", ".sqlite", ".sqlite3") or header == b"SQLite format 3\x00":
            if header != b"SQLite format 3\x00":
                raise SafetyError("Empty or invalid SQLite database")
            databases.append(name)
    with tempfile.TemporaryDirectory(prefix="mittr-integrity-") as temporary:
        copied = Path(temporary) / "data"
        copy_private(root, copied)
        for name in databases:
            try:
                with contextlib.closing(sqlite3.connect((copied / name).as_uri() + "?mode=ro", uri=True)) as db:
                    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                        raise SafetyError("SQLite integrity check failed")
            except sqlite3.Error:
                raise SafetyError("SQLite integrity check failed") from None


def relative_name(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise SafetyError("Invalid manifest path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise SafetyError("Invalid manifest path")
    return value


def verify(archive):
    archive = safe_path(archive)
    files, directories = inventory(archive)
    if set(files) - {"manifest.json"} != {name for name in files if name.startswith("data/")}:
        raise SafetyError("Unexpected backup files")
    if "manifest.json" not in files or not (archive / "data").is_dir():
        raise SafetyError("Not a mittr backup")
    try:
        manifest = json.loads(files["manifest.json"].read_text())
        if not isinstance(manifest, dict) or manifest["version"] != 1 or not isinstance(manifest["files"], dict) or not isinstance(manifest["directories"], list):
            raise SafetyError("Unsupported manifest")
        primary = relative_name(manifest["sqlite"])
        expected = {relative_name(name): value for name, value in manifest["files"].items()}
        expected_dirs = sorted(relative_name(name) for name in manifest["directories"])
    except (KeyError, TypeError, ValueError):
        raise SafetyError("Invalid backup manifest") from None
    actual, actual_dirs = inventory(archive / "data")
    if set(actual) != set(expected) or actual_dirs != expected_dirs or directories != sorted(["data"] + ["data/" + name for name in expected_dirs]):
        raise SafetyError("Backup inventory mismatch")
    for name, path in actual.items():
        if digest(path) != expected[name]:
            raise SafetyError("Backup checksum mismatch")
    sqlite_check(archive / "data", primary)
    return primary


@contextlib.contextmanager
def lock(data):
    path = safe_path(data.parent / ("." + data.name + ".mittr-sqlite.lock"))
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
            raise SafetyError("Unsafe lock file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SafetyError("Another backup/restore owns the data lock") from None
        yield
    finally:
        os.close(fd)


class Compose:
    def __init__(self, args, runner=None):
        self.runner = runner or subprocess.run
        self.env = dict(os.environ)
        for key in ("CCLOAD_PASS", "MITTR_PORT", "TZ", "COMPOSE_FILE", "COMPOSE_PROFILES"):
            self.env.pop(key, None)
        self.base = ["docker", "compose", "--env-file", str(safe_path(args.env_file)), "-p", args.project, "-f", str(safe_path(args.compose_file))]
        self.service = args.service
        self.image = None

    def run(self, command):
        try:
            result = self.runner(command, env=self.env, capture_output=True, text=True, check=False)
        except OSError:
            raise SafetyError("Docker command could not execute (output suppressed)") from None
        if result.returncode:
            raise SafetyError("Docker command failed, exit {} (output suppressed)".format(result.returncode))
        return result.stdout

    def call(self, *args):
        return self.run(self.base + list(args))

    def validate(self, data):
        try:
            service = json.loads(self.call("config", "--format", "json"))["services"][self.service]
            environment = service.get("environment", {})
            if environment.get("CCLOAD_MYSQL") or environment.get("CCLOAD_POSTGRES") or str(environment.get("CCLOAD_ENABLE_SQLITE_REPLICA", "0")).lower() not in ("0", "false", ""):
                raise SafetyError("Only standalone SQLite is supported")
            mounts = service.get("volumes", [])
            if len(mounts) != 1 or mounts[0].get("type") != "bind" or mounts[0].get("target") != "/app/data" or mounts[0].get("read_only") or safe_path(mounts[0]["source"]) != data:
                raise SafetyError("Compose must bind only the specified data directory to /app/data")
            sqlite_path = environment["SQLITE_PATH"]
            if not sqlite_path.startswith("/app/data/"):
                raise SafetyError("SQLite must be inside /app/data")
            primary = relative_name(sqlite_path[len("/app/data/"):])
            if service.get("user") not in (None, "1001:1001"):
                raise SafetyError("Only the image's UID/GID 1001 is supported")
            self.image = service["image"]
            ids = self.call("ps", "--all", "--quiet", self.service).split()
            if len(ids) > 1:
                raise SafetyError("Only one app container is supported")
            running = False
            if ids:
                container = json.loads(self.run(["docker", "inspect", ids[0]]))[0]
                binds = container["Mounts"]
                if len(binds) != 1 or binds[0].get("Type") != "bind" or binds[0].get("Destination") != "/app/data" or safe_path(binds[0]["Source"]) != data or not binds[0].get("RW"):
                    raise SafetyError("Existing container mount differs from specified data")
                actual_env = dict(value.split("=", 1) for value in container["Config"]["Env"])
                for key in ("SQLITE_PATH", "CCLOAD_MYSQL", "CCLOAD_POSTGRES", "CCLOAD_ENABLE_SQLITE_REPLICA"):
                    if actual_env.get(key, "") != str(environment.get(key, "") or ""):
                        raise SafetyError("Existing container SQLite configuration differs from Compose")
                running = container["State"]["Running"]
                if container["State"].get("Paused") or container["State"].get("Restarting"):
                    raise SafetyError("Container must be stable and not paused")
            return primary, running
        except (KeyError, TypeError, ValueError, IndexError):
            raise SafetyError("Unsupported Compose/container configuration (details suppressed)") from None

    def stop(self):
        self.call("stop", self.service)
        if self.call("ps", "--status", "running", "--quiet", self.service).strip():
            raise SafetyError("App is still running after stop")

    def start(self):
        self.call("up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "120", self.service)

    def ownership(self, stage):
        self.run(["docker", "run", "--rm", "--pull=never", "--network=none", "--user", "0:0", "--mount", "type=bind,source={},target=/restore".format(stage), "--entrypoint", "chown", self.image, "-R", "1001:1001", "/restore"])


def failure_label(error):
    if isinstance(error, SafetyError):
        return str(error)
    return type(error).__name__ + " (details suppressed)"


def recover(action, errors, label):
    try:
        action()
    except BaseException as error:
        errors.append(label + ": " + failure_label(error))


def unique_directory(parent, prefix):
    return Path(tempfile.mkdtemp(prefix=prefix, dir=parent))


def backup(data, destination, compose):
    data = data_path(data)
    destination = safe_path(destination)
    separate(data, destination)
    if not destination.is_dir():
        raise SafetyError("Backup destination must already exist outside data")
    with lock(data):
        primary, running = compose.validate(data)
        errors = []
        result = None
        try:
            if running:
                compose.stop()
            holder = unique_directory(destination, "mittr-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ-"))
            partial = holder / ".partial"
            partial.mkdir(mode=0o700)
            copy_private(data, partial / "data")
            sqlite_check(partial / "data", primary)
            files, directories = inventory(partial / "data")
            manifest = {"version": 1, "sqlite": primary, "files": {name: digest(path) for name, path in files.items()}, "directories": directories}
            fd = os.open(partial / "manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(manifest, stream, sort_keys=True, indent=2)
            verify(partial)
            result = holder / "snapshot"
            publish(partial, result)
        except BaseException as error:
            errors.append("Backup: " + failure_label(error))
        finally:
            if running:
                recover(compose.start, errors, "Restart original app")
        if errors:
            location = "; snapshot: " + str(result) if result is not None and result.exists() else ""
            raise SafetyError("; ".join(errors) + location)
        return result


def publish(source, target):
    if target.exists() or target.is_symlink():
        raise SafetyError("Target already exists")
    source.rename(target)


def restore(data, archive, compose, confirmed=False):
    if not confirmed:
        raise SafetyError("Restore requires --confirm-restore")
    data = data_path(data)
    archive = safe_path(archive)
    separate(data, archive)
    with lock(data):
        primary = verify(archive)
        configured, running = compose.validate(data)
        if configured != primary:
            raise SafetyError("Backup and configured SQLite paths differ")
        inventory(data)
        stage_holder = unique_directory(data.parent, "." + data.name + ".mittr-stage-")
        snapshot = stage_holder / "snapshot"
        copy_private(archive, snapshot)
        verify(snapshot)
        stage = snapshot / "data"
        original = stage_holder / "original"
        failed = stage_holder / "failed-restored"
        errors = []
        swapped = False
        try:
            if running:
                compose.stop()
            compose.ownership(stage)
            publish(data, original)
            publish(stage, data)
            swapped = True
            if running:
                compose.start()
        except BaseException as error:
            errors.append("Restore: " + failure_label(error))
            if swapped and running:
                recover(compose.stop, errors, "Stop failed restored app")
                if len(errors) > 1:
                    raise SafetyError("; ".join(errors) + "; rollback unsafe, original preserved at " + str(original)) from None
            if original.exists():
                try:
                    if data.exists():
                        publish(data, failed)
                    publish(original, data)
                except BaseException as rollback_error:
                    errors.append("Rollback: " + failure_label(rollback_error))
                    raise SafetyError("; ".join(errors) + "; original preserved at " + str(original)) from None
            if running:
                recover(compose.start, errors, "Restart original app")
        if errors:
            raise SafetyError("; ".join(errors) + "; recovery files: " + str(stage_holder))
        return original


def parser():
    result = argparse.ArgumentParser(description="Offline mittr SQLite data backup/restore (POSIX, Python 3.9+)")
    commands = result.add_subparsers(dest="command", required=True)
    check = commands.add_parser("verify", help="Verify manifest, complete inventory and SQLite without Docker")
    check.add_argument("--backup", required=True)
    for name in ("backup", "restore"):
        command = commands.add_parser(name)
        command.add_argument("--data", default="./data")
        command.add_argument("--env-file", default=".env.mittr.local")
        command.add_argument("--compose-file", default="docker-compose.mittr.yml")
        command.add_argument("--project", default="mittr-load")
        command.add_argument("--service", default="app")
        if name == "backup":
            command.add_argument("--destination", required=True)
        else:
            command.add_argument("--backup", required=True)
            command.add_argument("--confirm-restore", action="store_true")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    previous = signal.getsignal(signal.SIGTERM)
    def terminate(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, terminate)
    try:
        if args.command == "verify":
            verify(args.backup)
            print("Backup verified")
        else:
            data = data_path(args.data)
            compose = Compose(args)
            if args.command == "backup":
                print(backup(data, args.destination, compose))
            else:
                print("Original data preserved at:", restore(data, args.backup, compose, args.confirm_restore))
        return 0
    except (SafetyError, OSError, sqlite3.Error, KeyboardInterrupt) as error:
        print(failure_label(error), file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())
