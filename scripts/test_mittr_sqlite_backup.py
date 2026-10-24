import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import mittr_sqlite_backup as tool


class FakeCompose:
    def __init__(self, running=True):
        self.running = running
        self.calls = []
        self.failures = {}

    def invoke(self, name):
        self.calls.append(name)
        failures = self.failures.get(name, [])
        if failures:
            error = failures.pop(0)
            if error:
                raise error

    def validate(self, data):
        self.invoke("validate")
        return "ccload.db", self.running

    def stop(self):
        self.invoke("stop")

    def start(self):
        self.invoke("start")

    def ownership(self, stage):
        self.invoke("ownership")


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.docker_guard = patch.object(subprocess, "run", side_effect=AssertionError("Real subprocess forbidden"))
        self.docker_guard.start()
        self.addCleanup(self.docker_guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / "data"
        self.data.mkdir()
        self.destination = self.root / "backups"
        self.destination.mkdir()
        with contextlib.closing(sqlite3.connect(self.data / "ccload.db")) as db:
            with db:
                db.execute("CREATE TABLE fixture (value TEXT)")
                db.execute("INSERT INTO fixture VALUES ('original')")
        (self.data / "credentials").mkdir()
        (self.data / "credentials" / "token").write_bytes(b"fixture-secret")
        (self.data / "empty-directory").mkdir()
        self.compose = FakeCompose()

    def snapshot(self):
        return tool.backup(self.data, self.destination, self.compose)

    def manifest(self, archive):
        return json.loads((archive / "manifest.json").read_text())

    def write_manifest(self, archive, manifest):
        (archive / "manifest.json").write_text(json.dumps(manifest))

    def test_backup_complete_private_and_verifiable(self):
        archive = self.snapshot()
        self.assertEqual(tool.verify(archive), "ccload.db")
        self.assertEqual(self.compose.calls, ["validate", "stop", "start"])
        self.assertEqual(self.manifest(archive)["files"]["credentials/token"], tool.digest(self.data / "credentials/token"))
        for path in [archive.parent, archive, *archive.rglob("*")]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
        self.assertFalse((archive / ".env.mittr.local").exists())

    def test_checksum_tamper(self):
        archive = self.snapshot()
        (archive / "data/credentials/token").write_bytes(b"tamper")
        with self.assertRaisesRegex(tool.SafetyError, "checksum"):
            tool.verify(archive)

    def test_missing_and_unlisted_files(self):
        archive = self.snapshot()
        (archive / "data/extra").write_text("extra")
        with self.assertRaisesRegex(tool.SafetyError, "inventory"):
            tool.verify(archive)
        (archive / "data/extra").unlink()
        (archive / "data/credentials/token").unlink()
        with self.assertRaisesRegex(tool.SafetyError, "inventory"):
            tool.verify(archive)

    def test_unlisted_directory_and_top_level_file(self):
        archive = self.snapshot()
        (archive / "extra").mkdir()
        with self.assertRaises(tool.SafetyError):
            tool.verify(archive)
        (archive / "extra").rmdir()
        (archive / "extra").write_text("extra")
        with self.assertRaises(tool.SafetyError):
            tool.verify(archive)

    def test_invalid_sqlite_with_valid_checksum(self):
        archive = self.snapshot()
        path = archive / "data/ccload.db"
        path.write_bytes(b"SQLite format 3\x00" + b"broken" * 1000)
        manifest = self.manifest(archive)
        manifest["files"]["ccload.db"] = tool.digest(path)
        self.write_manifest(archive, manifest)
        with self.assertRaisesRegex(tool.SafetyError, "integrity"):
            tool.verify(archive)

    def test_empty_and_non_sqlite_rejected(self):
        for content in (b"", b"not sqlite"):
            with self.subTest(content=content):
                (self.data / "ccload.db").write_bytes(content)
                with self.assertRaisesRegex(tool.SafetyError, "invalid SQLite"):
                    self.snapshot()
                self.assertEqual(self.compose.calls[-1], "start")

    def test_symlinks_and_hardlinks_rejected(self):
        link = self.data / "link"
        link.symlink_to(self.data / "ccload.db")
        with self.assertRaises(tool.SafetyError):
            self.snapshot()
        link.unlink()
        os.link(self.data / "ccload.db", link)
        with self.assertRaises(tool.SafetyError):
            self.snapshot()

    def test_symlink_ancestor_and_archive_symlink(self):
        alias = self.root / "alias"
        alias.symlink_to(self.destination, target_is_directory=True)
        with self.assertRaises(tool.SafetyError):
            tool.backup(self.data, alias, self.compose)
        archive = self.snapshot()
        (archive / "data/link").symlink_to(self.data / "ccload.db")
        with self.assertRaises(tool.SafetyError):
            tool.verify(archive)

    def test_nesting_rejected_before_lifecycle(self):
        child = self.data / "backups"
        child.mkdir()
        for destination in (child, self.root, self.data):
            with self.subTest(destination=destination):
                with self.assertRaises(tool.SafetyError):
                    tool.backup(self.data, destination, self.compose)
        self.assertEqual(self.compose.calls, [])
        with self.assertRaises(tool.SafetyError):
            tool.restore(self.data, self.data / "backup", self.compose, True)

    def test_manifest_path_traversal(self):
        archive = self.snapshot()
        for name in ("../outside", "/absolute", "a/../b", "a//b", "a\\b"):
            manifest = self.manifest(archive)
            manifest["sqlite"] = name
            self.write_manifest(archive, manifest)
            with self.subTest(name=name), self.assertRaises(tool.SafetyError):
                tool.verify(archive)

    def test_duplicate_target_protection(self):
        source, target = self.root / "source", self.root / "target"
        source.mkdir()
        target.mkdir()
        with self.assertRaisesRegex(tool.SafetyError, "exists"):
            tool.publish(source, target)
        self.assertTrue(source.exists())
        first, second = self.snapshot(), self.snapshot()
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists())

    def test_lock_overlap(self):
        with tool.lock(self.data):
            with self.assertRaisesRegex(tool.SafetyError, "lock"):
                self.snapshot()
            with self.assertRaisesRegex(tool.SafetyError, "lock"):
                tool.restore(self.data, self.destination / "missing", self.compose, True)
        self.assertEqual(self.compose.calls, [])

    def test_stop_failure_attempts_restart_without_snapshot(self):
        self.compose.failures["stop"] = [tool.SafetyError("stop failed")]
        with self.assertRaisesRegex(tool.SafetyError, "stop failed"):
            self.snapshot()
        self.assertEqual(self.compose.calls, ["validate", "stop", "start"])
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_backup_and_restart_failures_both_reported(self):
        self.compose.failures["start"] = [tool.SafetyError("start failed")]
        with patch.object(tool, "copy_private", side_effect=OSError("fixture")):
            with self.assertRaisesRegex(tool.SafetyError, "Backup: OSError.*Restart original app: start failed"):
                self.snapshot()

    def test_interrupt_restarts_original(self):
        for error in (KeyboardInterrupt(), SystemExit()):
            with self.subTest(error=error):
                with patch.object(tool, "copy_private", side_effect=error):
                    with self.assertRaises(tool.SafetyError):
                        self.snapshot()
                self.assertEqual(self.compose.calls[-1], "start")

    def test_successful_exact_restore_and_preserved_original(self):
        archive = self.snapshot()
        expected = self.manifest(archive)["files"]
        (self.data / "credentials/token").write_bytes(b"new credentials")
        (self.data / "new-file").write_text("new")
        self.compose.calls.clear()
        original = tool.restore(self.data, archive, self.compose, True)
        self.assertEqual(self.compose.calls, ["validate", "stop", "ownership", "start"])
        actual, dirs = tool.inventory(self.data)
        self.assertEqual({name: tool.digest(path) for name, path in actual.items()}, expected)
        self.assertEqual(dirs, self.manifest(archive)["directories"])
        self.assertEqual((original / "new-file").read_text(), "new")
        self.assertEqual((original / "credentials/token").read_bytes(), b"new credentials")

    def test_initially_stopped_stays_stopped(self):
        self.compose.running = False
        archive = self.snapshot()
        tool.restore(self.data, archive, self.compose, True)
        self.assertEqual(self.compose.calls, ["validate", "validate", "ownership"])

    def test_restore_requires_confirmation_before_docker(self):
        with self.assertRaisesRegex(tool.SafetyError, "confirm"):
            tool.restore(self.data, self.destination, self.compose)
        self.assertEqual(self.compose.calls, [])

    def test_verify_before_restore_stop(self):
        archive = self.snapshot()
        self.compose.calls.clear()
        (archive / "data/ccload.db").write_bytes(b"invalid")
        with self.assertRaises(tool.SafetyError):
            tool.restore(self.data, archive, self.compose, True)
        self.assertEqual(self.compose.calls, [])

    def test_restore_start_failure_rolls_back_and_preserves_failed(self):
        archive = self.snapshot()
        (self.data / "current").write_text("keep")
        self.compose.calls.clear()
        self.compose.failures["start"] = [tool.SafetyError("health failed"), None]
        with self.assertRaisesRegex(tool.SafetyError, "health failed"):
            tool.restore(self.data, archive, self.compose, True)
        self.assertEqual((self.data / "current").read_text(), "keep")
        self.assertEqual(self.compose.calls, ["validate", "stop", "ownership", "start", "stop", "start"])
        failed = list(self.root.glob(".data.mittr-stage-*/failed-restored"))
        self.assertEqual(len(failed), 1)
        self.assertTrue((failed[0] / "ccload.db").exists())

    def test_restore_ownership_and_stop_failures_restart_original(self):
        archive = self.snapshot()
        for step in ("stop", "ownership"):
            with self.subTest(step=step):
                self.compose.calls.clear()
                self.compose.failures[step] = [tool.SafetyError(step + " failed")]
                before = tool.digest(self.data / "ccload.db")
                with self.assertRaisesRegex(tool.SafetyError, step + " failed"):
                    tool.restore(self.data, archive, self.compose, True)
                self.assertEqual(tool.digest(self.data / "ccload.db"), before)
                self.assertEqual(self.compose.calls[-1], "start")

    def test_restore_second_rename_failure_recovers_original(self):
        archive = self.snapshot()
        real_publish = tool.publish
        def fail_stage(source, target):
            if source.name == "data" and source.parent.name == "snapshot":
                raise OSError("fixture rename failure")
            real_publish(source, target)
        with patch.object(tool, "publish", side_effect=fail_stage):
            with self.assertRaises(tool.SafetyError):
                tool.restore(self.data, archive, self.compose, True)
        self.assertTrue((self.data / "ccload.db").is_file())
        self.assertEqual(self.compose.calls[-1], "start")

    def test_restore_rollback_stop_failure_preserves_both(self):
        archive = self.snapshot()
        (self.data / "current").write_text("keep")
        self.compose.failures["start"] = [tool.SafetyError("health")]
        self.compose.failures["stop"] = [None, tool.SafetyError("cannot stop")]
        with self.assertRaisesRegex(tool.SafetyError, "rollback unsafe"):
            tool.restore(self.data, archive, self.compose, True)
        originals = list(self.root.glob(".data.mittr-stage-*/original/current"))
        self.assertEqual(len(originals), 1)
        self.assertEqual(originals[0].read_text(), "keep")
        self.assertTrue((self.data / "ccload.db").exists())

    def test_unsafe_data_directory(self):
        for path in (Path.cwd(), Path.home(), Path("/")):
            with self.subTest(path=path), self.assertRaises(tool.SafetyError):
                tool.data_path(path)

    def test_verify_cli_no_compose(self):
        archive = self.snapshot()
        with patch.object(tool, "Compose", side_effect=AssertionError("Docker forbidden")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tool.main(["verify", "--backup", str(archive)]), 0)

    def test_sigterm_handler_uses_recovery_path(self):
        def interrupt(*args):
            signal.raise_signal(signal.SIGTERM)
        with patch.object(tool, "Compose", return_value=self.compose), patch.object(tool, "copy_private", side_effect=interrupt), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tool.main(["backup", "--data", str(self.data), "--destination", str(self.destination)]), 1)
        self.assertEqual(self.compose.calls[-1], "start")

    def compose_fixture(self, running=True):
        args = argparse.Namespace(env_file=str(self.root / "fixture.env"), compose_file=str(self.root / "fixture.yml"), project="fixture", service="app")
        environment = {"SQLITE_PATH": "/app/data/ccload.db"}
        service = {"image": "fixture-image", "environment": environment, "volumes": [{"type": "bind", "source": str(self.data), "target": "/app/data"}]}
        container = {"Mounts": [{"Type": "bind", "Source": str(self.data), "Destination": "/app/data", "RW": True}], "Config": {"Env": ["SQLITE_PATH=/app/data/ccload.db"]}, "State": {"Running": running}}
        calls = []
        def runner(command, **kwargs):
            calls.append((command, kwargs))
            if "config" in command:
                output = json.dumps({"services": {"app": service}})
            elif "inspect" in command:
                output = json.dumps([container])
            elif "--all" in command:
                output = "fixture-id"
            else:
                output = ""
            return subprocess.CompletedProcess(command, 0, output, "")
        return tool.Compose(args, runner), service, container, calls

    def test_compose_validates_actual_bind_and_scrubs_environment(self):
        with patch.dict(os.environ, {"CCLOAD_PASS": "fixture-secret", "MITTR_PORT": "9000", "TZ": "UTC"}):
            compose, service, container, calls = self.compose_fixture()
        self.assertEqual(compose.validate(self.data), ("ccload.db", True))
        compose.stop()
        compose.start()
        compose.ownership(self.data)
        for command, kwargs in calls:
            self.assertNotIn("CCLOAD_PASS", kwargs["env"])
            self.assertNotIn("MITTR_PORT", kwargs["env"])
            self.assertNotIn("TZ", kwargs["env"])
        self.assertIn("--wait", calls[-2][0])
        self.assertIn("--force-recreate", calls[-2][0])
        self.assertIn("1001:1001", calls[-1][0])

    def test_incompatible_compose_rejected(self):
        for field in ("mount", "sqlite", "remote", "actual-mount", "actual-sqlite", "paused"):
            compose, service, container, calls = self.compose_fixture()
            if field == "mount":
                service["volumes"][0]["type"] = "volume"
            elif field == "sqlite":
                service["environment"]["SQLITE_PATH"] = "/outside.db"
            elif field == "remote":
                service["environment"]["CCLOAD_MYSQL"] = "fixture"
            elif field == "actual-mount":
                container["Mounts"][0]["Source"] = str(self.root / "other")
            elif field == "actual-sqlite":
                container["Config"]["Env"] = ["SQLITE_PATH=/app/data/other.db"]
            else:
                container["State"]["Paused"] = True
            with self.subTest(field=field), self.assertRaises(tool.SafetyError):
                compose.validate(self.data)
            self.assertFalse(any("stop" in command for command, kwargs in calls))

    def test_wal_snapshot_is_verified_without_modification(self):
        with contextlib.closing(sqlite3.connect(self.data / "ccload.db")) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("INSERT INTO fixture VALUES ('wal')")
            db.commit()
            self.compose.running = False
            archive = self.snapshot()
            before = self.manifest(archive)["files"]
            self.assertIn("ccload.db-wal", before)
            tool.verify(archive)
            actual, _ = tool.inventory(archive / "data")
            self.assertEqual({name: tool.digest(path) for name, path in actual.items()}, before)

    def test_staging_tamper_rejected_before_stop(self):
        archive = self.snapshot()
        self.compose.calls.clear()
        real_copy = tool.copy_private
        def tamper(source, target):
            real_copy(source, target)
            if target.name == "snapshot":
                (target / "data/credentials/token").write_bytes(b"tamper")
        with patch.object(tool, "copy_private", side_effect=tamper):
            with self.assertRaisesRegex(tool.SafetyError, "checksum"):
                tool.restore(self.data, archive, self.compose, True)
        self.assertEqual(self.compose.calls, ["validate"])

    def test_restore_interrupt_rolls_back(self):
        archive = self.snapshot()
        (self.data / "current").write_text("keep")
        self.compose.failures["start"] = [KeyboardInterrupt(), None]
        with self.assertRaises(tool.SafetyError):
            tool.restore(self.data, archive, self.compose, True)
        self.assertEqual((self.data / "current").read_text(), "keep")
        self.assertEqual(self.compose.calls[-1], "start")

    def test_parent_traversal_and_sqlite_traversal_rejected(self):
        with self.assertRaises(tool.SafetyError):
            tool.safe_path(self.data / ".." / "data")
        compose, service, container, calls = self.compose_fixture()
        service["environment"]["SQLITE_PATH"] = "/app/data/a/../ccload.db"
        with self.assertRaises(tool.SafetyError):
            compose.validate(self.data)

    def test_malformed_manifest_rejected(self):
        archive = self.snapshot()
        for value in ([], None, {"version": 1, "files": {}, "directories": "bad", "sqlite": "ccload.db"}):
            self.write_manifest(archive, value)
            with self.subTest(value=value), self.assertRaises(tool.SafetyError):
                tool.verify(archive)

    def test_docker_output_never_exposes_secrets(self):
        compose, service, container, calls = self.compose_fixture()
        compose.runner = lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "fixture-secret", "fixture-secret")
        with self.assertRaises(tool.SafetyError) as error:
            compose.start()
        self.assertNotIn("fixture-secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
