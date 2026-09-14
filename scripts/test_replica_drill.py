#!/usr/bin/env python3
"""Guard disposable runtime lifecycle operations and reject misleading concurrency evidence."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("replica_drill", Path(__file__).with_name("replica-drill.py"))
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)


class ReplicaDrillTest(unittest.TestCase):
    def fixture(self, running=True):
        fixture = drill.ReplicaFixture.__new__(drill.ReplicaFixture)
        fixture.image = "sha256:" + "a" * 64
        fixture.prefix = "inventory-drill-owned"
        fixture.network = fixture.prefix + "-network"
        fixture.passwords = {"customer": "fixture-password"}
        name, database = fixture.prefix + "-replica-a", fixture.prefix + "-db"
        fixture.replicas = {name: database}
        fixture.containers = [database, name]
        item = {
            "Id": "original-container", "Image": fixture.image,
            "Config": {"Env": ["SPRING_PROFILES_ACTIVE=local", "DB_USERNAME=inventory_app",
                                "SPRING_FLYWAY_ENABLED=false",
                                f"DB_URL=jdbc:postgresql://{database}:5432/inventory?socketTimeout=5&connectTimeout=3"]},
            "NetworkSettings": {"Networks": {fixture.network: {}},
                                "Ports": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "49123"}]}},
            "State": {"Running": running, "Status": "running" if running else "exited"},
        }
        fixture.owned = Mock(return_value=item)
        return fixture, name, item

    def test_stop_rejects_database_even_if_owned_by_this_fixture(self):
        fixture, name, _ = self.fixture()
        with patch.object(drill, "docker") as docker:
            with self.assertRaisesRegex(RuntimeError, "untracked runtime"):
                fixture.stop_replica(fixture.replicas[name])
            fixture.owned.assert_not_called()
            docker.assert_not_called()

    def test_restart_rejects_untracked_foreign_container_before_inspection(self):
        fixture, _, _ = self.fixture()
        with patch.object(drill, "docker") as docker:
            with self.assertRaisesRegex(RuntimeError, "untracked runtime"):
                fixture.restart_replica("existing-compose-app")
            fixture.owned.assert_not_called()
            docker.assert_not_called()

    def test_stop_requires_unchanged_image_identity(self):
        fixture, name, item = self.fixture()
        item["Image"] = "sha256:" + "b" * 64
        with patch.object(drill, "docker") as docker:
            with self.assertRaisesRegex(RuntimeError, "image identity"):
                fixture.stop_replica(name)
            docker.assert_not_called()

    def test_owner_credentials_and_migration_profiles_cannot_be_restarted(self):
        for original, replacement in (("DB_USERNAME=inventory_app", "DB_USERNAME=inventory"),
                                      ("SPRING_PROFILES_ACTIVE=local", "SPRING_PROFILES_ACTIVE=migrate"),
                                      ("SPRING_FLYWAY_ENABLED=false", "SPRING_FLYWAY_ENABLED=true")):
            with self.subTest(replacement=replacement):
                fixture, name, item = self.fixture(running=False)
                item["Config"]["Env"].remove(original)
                item["Config"]["Env"].append(replacement)
                with patch.object(drill, "docker") as docker:
                    with self.assertRaisesRegex(RuntimeError, "restricted local runtime"):
                        fixture.restart_replica(name)
                    docker.assert_not_called()

    def test_restart_rejects_runtime_attached_to_another_network(self):
        fixture, name, item = self.fixture(running=False)
        item["NetworkSettings"]["Networks"]["user-network"] = {}
        with patch.object(drill, "docker") as docker:
            with self.assertRaisesRegex(RuntimeError, "unexpected network"):
                fixture.restart_replica(name)
            docker.assert_not_called()

    def test_api_rejects_public_or_multiple_port_bindings(self):
        for bindings in ([{"HostIp": "0.0.0.0", "HostPort": "49123"}],
                         [{"HostIp": "127.0.0.1", "HostPort": "49123"}] * 2):
            fixture, name, item = self.fixture()
            item["NetworkSettings"]["Ports"]["8080/tcp"] = bindings
            with self.subTest(bindings=bindings), self.assertRaisesRegex(RuntimeError, "loopback-only"):
                fixture.api_for_replica(name)

    def test_restart_requires_peer_to_be_stopped_first(self):
        fixture, name, _ = self.fixture()
        with patch.object(drill, "docker") as docker:
            with self.assertRaisesRegex(RuntimeError, "lifecycle state"):
                fixture.restart_replica(name)
            docker.assert_not_called()

    def test_replacement_container_cannot_count_as_a_successful_restart(self):
        fixture, name, before = self.fixture(running=False)
        after = deepcopy(before)
        after["State"]["Running"] = True
        after["Id"] = "replacement-container"
        fixture.owned.side_effect = [before, after]
        with patch.object(drill, "docker") as docker:
            with self.assertRaisesRegex(RuntimeError, "replaced the tracked container"):
                fixture.restart_replica(name)
            docker.assert_called_once_with("container", "start", name)

    def test_successful_restart_rediscovers_the_actual_loopback_port(self):
        fixture, name, before = self.fixture(running=False)
        after = deepcopy(before)
        after["State"]["Running"] = True
        after["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostPort"] = "49999"
        fixture.owned.side_effect = [before, after, after]
        with patch.object(drill, "docker"), patch.object(drill.recovery.Api, "wait_health") as wait:
            api = fixture.restart_replica(name)
            self.assertEqual(api.port, 49999)
            wait.assert_called_once_with()

    def test_batch_bounds_reject_unbalanced_unbounded_or_non_integer_requests(self):
        for count in (0, 1, 3, 18, True, 2.0):
            with self.subTest(count=count), self.assertRaisesRegex(RuntimeError, "even integer"):
                drill.concurrent_batch([object(), object()], Mock(), count=count)
        with self.assertRaisesRegex(RuntimeError, "exactly two"):
            drill.concurrent_batch([object()], Mock())

    def test_requests_reach_both_replicas_concurrently(self):
        seen = []
        lock = threading.Lock()
        rendezvous = threading.Barrier(8, timeout=5)
        apis = [object(), object()]

        def operation(api, index):
            rendezvous.wait()
            with lock:
                seen.append((api, index))
            return 200, {"index": index}

        samples = drill.concurrent_batch(apis, operation)
        self.assertEqual(len(seen), 8)
        self.assertEqual(sum(api is apis[0] for api, _ in seen), 4)
        self.assertEqual(sum(api is apis[1] for api, _ in seen), 4)
        self.assertEqual(drill.summarize_batch(samples)["requests_by_replica"], {"0": 4, "1": 4})

    def test_same_id_with_different_committed_fields_is_not_a_valid_retry(self):
        samples = [{"status": 201, "payload": {"id": "same", "quantity": 5}},
                   {"status": 200, "payload": {"id": "same", "quantity": 4}}]
        with self.assertRaisesRegex(RuntimeError, "identical reservation fields"):
            drill.require_same_reservation(samples, {201: 1, 200: 1})

    def test_lock_timeouts_or_duplicate_key_conflicts_do_not_prove_no_overselling(self):
        for detail in ("Idempotency-Key is busy", "Operation conflicts with an existing record or inventory constraint"):
            samples = [{"status": 201, "payload": {"id": "created"}},
                       {"status": 409, "payload": {"detail": detail}}]
            with self.subTest(detail=detail), self.assertRaisesRegex(RuntimeError, "non-stock conflict"):
                drill.require_stock_conflicts(samples, successes=1)


if __name__ == "__main__":
    unittest.main()
