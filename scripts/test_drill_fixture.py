#!/usr/bin/env python3
"""Offline guards for migration failure, credential probes, and owned cleanup."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("recovery_drill", Path(__file__).with_name("recovery-drill.py"))
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)


class FixtureFailureTests(unittest.TestCase):
    def setUp(self):
        self.fixture = object.__new__(drill.Fixture)
        self.fixture.prefix = "inventory-drill-owned"
        self.fixture.run_id = "owned"
        self.fixture.network = self.fixture.prefix + "-network"
        self.fixture.network_created = False
        self.database = self.fixture.prefix + "-db"
        self.fixture.containers = [self.database]
        self.fixture.image = "sha256:fixture-test"
        self.fixture.db_password = "owner-secret-not-for-output"
        self.fixture.initial_runtime_password = "initial-secret-not-for-output"
        self.fixture.prepared_databases = {self.database}
        self.fixture.runtime_passwords = {self.database: "old-secret-not-for-output"}
        self.fixture.migration_runs = []

    def test_failed_migration_blocks_runtime_and_keeps_unconfirmed_secret_out(self):
        with patch.object(self.fixture, "owned", return_value={"State": {"Status": "exited", "ExitCode": 1}}), \
             patch.object(self.fixture, "run_container", return_value=self.fixture.prefix + "-migration"), \
             patch.object(drill, "docker", return_value=b"1\n"):
            with self.assertRaisesRegex(RuntimeError, "runtime startup blocked") as error:
                self.fixture.migrate_database("rotation", self.database, "new-secret-not-for-output")
            self.assertNotIn("new-secret", str(error.exception))
            self.assertNotIn(self.database, self.fixture.prepared_databases)
            self.assertEqual("old-secret-not-for-output", self.fixture.runtime_passwords[self.database])
            with self.assertRaisesRegex(RuntimeError, "confirmed successful one-shot migration"):
                self.fixture.start_app("runtime", self.database)

    def test_unconfirmed_container_state_does_not_accept_zero_wait_output(self):
        with patch.object(self.fixture, "owned", return_value={"State": {"Status": "running", "ExitCode": 0}}), \
             patch.object(self.fixture, "run_container", return_value=self.fixture.prefix + "-migration"), \
             patch.object(drill, "docker", return_value=b"0\n"):
            with self.assertRaises(RuntimeError):
                self.fixture.migrate_database("rotation", self.database, "new-secret-not-for-output")
            self.assertNotIn(self.database, self.fixture.prepared_databases)

    def test_timeout_blocks_runtime_and_leaves_container_tracked_for_cleanup(self):
        job = self.fixture.prefix + "-migration"
        def create(*args, **kwargs):
            self.fixture.containers.append(job)
            return job
        with patch.object(self.fixture, "owned", return_value={}), \
             patch.object(self.fixture, "run_container", side_effect=create), \
             patch.object(drill, "docker", side_effect=subprocess.TimeoutExpired(["docker", "wait", job], 90)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.fixture.migrate_database("rotation", self.database)
            self.assertNotIn(self.database, self.fixture.prepared_databases)
            self.assertIn(job, self.fixture.containers)

    def test_network_failure_is_not_counted_as_old_password_rejection(self):
        response = subprocess.CompletedProcess([], 2, b"", b"could not connect to server: Connection refused")
        with patch.object(self.fixture, "owned", return_value={}), patch.object(drill.subprocess, "run", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "not specifically rejected"):
                self.fixture.check_runtime_login(self.database, "old-secret", accepted=False)

    def test_trusted_login_is_not_counted_as_password_rejection(self):
        response = subprocess.CompletedProcess([], 0, b"inventory_app|t|t|t|t\n", b"")
        with patch.object(self.fixture, "owned", return_value={}), patch.object(drill.subprocess, "run", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "not specifically rejected"):
                self.fixture.check_runtime_login(self.database, "old-secret", accepted=False)

    def test_elevated_runtime_login_fails_privilege_probe(self):
        response = subprocess.CompletedProcess([], 0, b"inventory_app|f|t|t|t\n", b"")
        with patch.object(self.fixture, "owned", return_value={}), patch.object(drill.subprocess, "run", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "restricted privilege"):
                self.fixture.check_runtime_login(self.database, "new-secret")

    def test_password_is_passed_through_environment_and_never_result(self):
        response = subprocess.CompletedProcess([], 2, b"", b'FATAL: password authentication failed for user "inventory_app"')
        with patch.object(self.fixture, "owned", return_value={}), \
             patch.object(drill.subprocess, "run", return_value=response) as command:
            result = self.fixture.check_runtime_login(self.database, "old-secret", accepted=False)
            self.assertTrue(result["expected_authentication_rejection"])
            self.assertNotIn("old-secret", repr(command.call_args.args))
            self.assertEqual("old-secret", command.call_args.kwargs["env"]["PGPASSWORD"])
            self.assertNotIn("old-secret", repr(result))

    def test_untracked_name_is_rejected_before_inspection(self):
        with patch.object(drill, "docker") as command:
            with self.assertRaisesRegex(RuntimeError, "not created by this fixture"):
                self.fixture.owned("container", self.fixture.prefix + "-foreign")
            command.assert_not_called()

    def test_cleanup_does_not_delete_mismatched_ownership(self):
        def command(*args, **kwargs):
            if args[:2] == ("container", "ls"):
                return (self.database + "\n").encode()
            if args[:2] == ("container", "inspect"):
                return b'[{"Config":{"Labels":{"inventory.drill.run":"foreign"}}}]'
            self.fail("Cleanup attempted a destructive operation despite foreign ownership")
        with patch.object(drill, "docker", side_effect=command):
            errors = self.fixture.cleanup()
            self.assertEqual(1, len(errors))
            self.assertIn("ownership labels do not match", errors[0])

    def test_uncertain_network_creation_is_still_tracked_for_cleanup(self):
        with patch.object(drill, "docker", side_effect=subprocess.TimeoutExpired(["docker", "network", "create"], 90)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.fixture.create_network()
        self.assertTrue(self.fixture.network_created)
        self.fixture.containers = []
        def command(*args, **kwargs):
            if args[:2] == ("network", "ls"):
                return (self.fixture.network + "\n").encode()
            if args[:2] == ("network", "inspect"):
                return b'[{"Labels":{"inventory.drill.run":"owned","inventory.drill.purpose":"disposable-local-verification"}}]'
            if args[:2] == ("network", "rm"):
                self.assertEqual(self.fixture.network, args[2])
                return b""
            self.fail("Unexpected resource operation")
        with patch.object(drill, "docker", side_effect=command) as invoke:
            self.assertEqual([], self.fixture.cleanup())
            self.assertIn(unittest.mock.call("network", "rm", self.fixture.network), invoke.call_args_list)


if __name__ == "__main__":
    unittest.main()
