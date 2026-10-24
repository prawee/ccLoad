import argparse
import contextlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

from scripts import mittr_sqlite_backup as tool


IMAGE = "mittr-load:1.0.0"
PREFIX = "mittr-sqlite-integration-"


def docker(*args):
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=45).stdout


def protected_state():
    return docker("ps", "-a", "--filter", "label=com.docker.compose.project=mittr-load", "--format", "{{.ID}} {{.State}}")


class FixtureCompose(tool.Compose):
    def __init__(self, args, root):
        super().__init__(args)
        self.root = root
        self.project = args.project
        self.commands = []
        self.owned = []

    def run(self, command):
        if command[:len(self.base)] == self.base:
            if not self.project.startswith(PREFIX):
                raise AssertionError("Unsafe fixture project")
        elif command[:2] == ["docker", "inspect"]:
            ids = self.call("ps", "--all", "--quiet", "app").split()
            if len(command) != 3 or command[2] not in ids:
                raise AssertionError("Inspect must target the fixture")
        elif command[:2] == ["docker", "run"]:
            mount = command[command.index("--mount") + 1]
            source = mount.split(",")[1].removeprefix("source=")
            if self.root not in Path(source).parents or "--network=none" not in command or "--pull=never" not in command:
                raise AssertionError("Unsafe ownership helper")
        else:
            raise AssertionError("Unexpected Docker command")
        self.commands.append(command)
        return super().run(command)

    def start(self):
        self.call("up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "8", "app")

    def ownership(self, stage):
        self.owned.append(stage)
        super().ownership(stage)


@unittest.skipUnless(os.environ.get("MITTR_DOCKER_INTEGRATION") == "1", "Set MITTR_DOCKER_INTEGRATION=1 for real isolated Docker tests")
class DockerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        docker("image", "inspect", IMAGE, "--format", "{{.Id}}")
        docker("compose", "version")
        cls.before = protected_state()
        cls.projects = []
        print("\nProtected mittr-load before: " + cls.before.strip(), flush=True)

    @classmethod
    def tearDownClass(cls):
        after = protected_state()
        if after != cls.before:
            raise AssertionError("Protected container IDs/state changed")
        print("Protected mittr-load after: " + after.strip(), flush=True)
        for project in cls.projects:
            for resource, args in (("containers", ("ps", "-aq")), ("volumes", ("volume", "ls", "-q")), ("networks", ("network", "ls", "-q"))):
                remaining = docker(*args, "--filter", "label=com.docker.compose.project=" + project).strip()
                if remaining:
                    raise AssertionError("Leaked fixture " + resource + ": " + remaining)
        print("Cleanup verified: containers=0 volumes=0 networks=0 for {} generated projects".format(len(cls.projects)), flush=True)

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix=PREFIX)).resolve()
        self.project = PREFIX + uuid.uuid4().hex[:12]
        self.projects.append(self.project)
        self.data = self.root / "data"
        self.data.mkdir(mode=0o700)
        self.destination = self.root / "backups"
        self.destination.mkdir(mode=0o700)
        env = self.root / "fixture.env"
        env.write_text("CCLOAD_PASS=synthetic-integration-password-only\n")
        env.chmod(0o600)
        compose_file = self.root / "compose.json"
        compose_file.write_text(json.dumps({"services": {"app": {
            "image": IMAGE,
            "pull_policy": "never",
            "user": "1001:1001",
            "network_mode": "none",
            "env_file": [str(env)],
            "environment": {"SQLITE_PATH": "/app/data/ccload.db", "PORT": "8080", "GIN_MODE": "release", "CCLOAD_CONTAINER": "1", "CCLOAD_ANTHROPIC_CLI_VERSION_SYNC": "false"},
            "entrypoint": ["/bin/sh", "-c", "test ! -f /app/data/fail-start && exec ./ccload"],
            "volumes": [{"type": "bind", "source": str(self.data), "target": "/app/data"}],
            "healthcheck": {"test": ["CMD", "wget", "-q", "-O", "/dev/null", "--tries=1", "http://127.0.0.1:8080/health"], "interval": "1s", "timeout": "1s", "retries": 2}
        }}}))
        args = argparse.Namespace(env_file=str(env), compose_file=str(compose_file), project=self.project, service="app")
        self.compose = FixtureCompose(args, self.root)
        self.addCleanup(self.cleanup_fixture)
        self.compose.image = IMAGE
        self.compose.ownership(self.data)
        self.compose.start()
        self.assertHealthy()
        self.compose.stop()
        with contextlib.closing(sqlite3.connect(self.data / "ccload.db")) as db:
            with db:
                db.execute("CREATE TABLE integration_fixture (value TEXT)")
                db.execute("INSERT INTO integration_fixture VALUES ('snapshot-value')")
        credentials = self.data / "credentials"
        credentials.mkdir(mode=0o700)
        (credentials / "token").write_text("synthetic-token-original")
        (credentials / "token").chmod(0o600)
        (self.data / "empty-directory").mkdir(mode=0o700)
        self.compose.ownership(self.data)
        self.compose.start()
        self.compose.commands.clear()

    def cleanup_fixture(self):
        self.compose.call("down", "--volumes", "--remove-orphans", "--timeout", "2")
        docker("run", "--rm", "--pull=never", "--network=none", "--user", "0:0", "--label", "com.docker.compose.project=" + self.project, "--mount", "type=bind,source={},target=/restore".format(self.root), "--entrypoint", "chmod", IMAGE, "-R", "a+rwX", "/restore")
        shutil.rmtree(self.root)
        self.assertFalse(self.root.exists())

    def assertHealthy(self):
        ids = self.compose.call("ps", "--all", "--quiet", "app").split()
        self.assertEqual(len(ids), 1)
        container = json.loads(self.compose.run(["docker", "inspect", ids[0]]))[0]
        self.assertTrue(container["State"]["Running"])
        self.assertEqual(container["State"]["Health"]["Status"], "healthy")
        self.assertEqual(container["Config"]["User"], "1001:1001")
        self.assertEqual(container["HostConfig"]["NetworkMode"], "none")
        self.assertFalse(container["HostConfig"].get("PortBindings"))
        self.assertEqual(len(container["Mounts"]), 1)
        self.assertEqual(container["Mounts"][0]["Source"], str(self.data))
        self.compose.call("exec", "-T", "app", "wget", "-q", "-O", "/dev/null", "--tries=1", "http://127.0.0.1:8080/health")
        return ids[0]

    def value(self, root):
        with contextlib.closing(sqlite3.connect((root / "ccload.db").as_uri() + "?mode=ro", uri=True)) as db:
            return db.execute("SELECT value FROM integration_fixture").fetchone()[0]

    def change_live(self):
        self.compose.stop()
        with contextlib.closing(sqlite3.connect(self.data / "ccload.db")) as db:
            with db:
                db.execute("UPDATE integration_fixture SET value='live-value'")
        (self.data / "credentials/token").write_text("synthetic-token-live")
        (self.data / "live-only").write_text("preserve-original")
        self.compose.start()

    def snapshot(self):
        return tool.backup(self.data, self.destination, self.compose)

    def test_running_round_trip_and_preserved_original(self):
        before = self.assertHealthy()
        archive = self.snapshot()
        self.assertNotEqual(before, self.assertHealthy())
        self.assertEqual(tool.verify(archive), "ccload.db")
        manifest_before = tool.digest(archive / "manifest.json")
        for path in [archive.parent, archive, *archive.rglob("*")]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
        self.assertFalse((archive / "data/fixture.env").exists())
        self.change_live()
        original = tool.restore(self.data, archive, self.compose, True)
        self.assertHealthy()
        self.assertEqual(self.value(self.data), "snapshot-value")
        self.assertEqual((self.data / "credentials/token").read_text(), "synthetic-token-original")
        self.assertTrue((self.data / "empty-directory").is_dir())
        self.assertFalse((self.data / "live-only").exists())
        self.assertEqual(self.value(original), "live-value")
        self.assertEqual((original / "credentials/token").read_text(), "synthetic-token-live")
        self.assertEqual((original / "live-only").read_text(), "preserve-original")
        self.assertEqual(tool.digest(archive / "manifest.json"), manifest_before)
        self.assertEqual(tool.verify(archive), "ccload.db")
        self.assertEqual(self.compose.owned[-1].parent.name, "snapshot")
        self.assertEqual(self.compose.call("exec", "-T", "app", "stat", "-c", "%u:%g", "/app/data/ccload.db").strip(), "1001:1001")

    def test_stopped_backup_and_restore_remain_stopped(self):
        self.compose.stop()
        self.compose.commands.clear()
        archive = self.snapshot()
        original = tool.restore(self.data, archive, self.compose, True)
        self.assertEqual(self.value(original), "snapshot-value")
        self.assertEqual(self.compose.validate(self.data), ("ccload.db", False))
        self.assertFalse(any("up" in command or "stop" in command for command in self.compose.commands))
        self.assertEqual(tool.verify(archive), "ccload.db")

    def test_snapshot_copy_failure_restarts_real_app(self):
        before = self.assertHealthy()
        real_copy = tool.copy_private
        def fail_snapshot(source, target):
            if source == self.data and target.parent.name == ".partial":
                raise OSError("synthetic snapshot failure")
            return real_copy(source, target)
        with patch.object(tool, "copy_private", side_effect=fail_snapshot):
            with self.assertRaisesRegex(tool.SafetyError, "Backup: OSError"):
                self.snapshot()
        self.assertNotEqual(before, self.assertHealthy())
        self.assertEqual(self.value(self.data), "snapshot-value")
        self.assertEqual((self.data / "credentials/token").read_text(), "synthetic-token-original")
        self.assertEqual(list(self.destination.glob("*/snapshot")), [])
        self.assertEqual(len(list(self.destination.glob("*/.partial"))), 1)

    def test_failed_restored_start_rolls_back_real_app(self):
        self.compose.stop()
        (self.data / "fail-start").write_text("synthetic startup failure")
        archive = self.snapshot()
        self.assertEqual(tool.verify(archive), "ccload.db")
        (self.data / "fail-start").unlink()
        self.compose.start()
        self.change_live()
        before = self.assertHealthy()
        with self.assertRaisesRegex(tool.SafetyError, "Restore: Docker command failed.*recovery files:"):
            tool.restore(self.data, archive, self.compose, True)
        self.assertNotEqual(before, self.assertHealthy())
        self.assertEqual(self.value(self.data), "live-value")
        self.assertEqual((self.data / "credentials/token").read_text(), "synthetic-token-live")
        self.assertEqual((self.data / "live-only").read_text(), "preserve-original")
        self.assertFalse((self.data / "fail-start").exists())
        failed = list(self.root.glob(".data.mittr-stage-*/failed-restored"))
        self.assertEqual(len(failed), 1)
        self.assertEqual(self.value(failed[0]), "snapshot-value")
        self.assertTrue((failed[0] / "fail-start").is_file())
        self.assertEqual(tool.verify(archive), "ccload.db")

    def test_publish_refuses_overwrite_and_snapshots_are_unique(self):
        first = self.snapshot()
        first_files, _ = tool.inventory(first)
        expected = {name: tool.digest(path) for name, path in first_files.items()}
        second = self.snapshot()
        self.assertNotEqual(first, second)
        before = self.assertHealthy()
        self.compose.commands.clear()
        with self.assertRaisesRegex(tool.SafetyError, "Target already exists"):
            tool.publish(second, first)
        self.assertEqual(self.compose.commands, [])
        actual, _ = tool.inventory(first)
        self.assertEqual({name: tool.digest(path) for name, path in actual.items()}, expected)
        self.assertEqual(tool.verify(first), "ccload.db")
        self.assertEqual(tool.verify(second), "ccload.db")
        self.assertEqual(before, self.assertHealthy())


if __name__ == "__main__":
    unittest.main()
