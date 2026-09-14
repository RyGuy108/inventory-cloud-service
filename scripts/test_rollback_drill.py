#!/usr/bin/env python3
"""Check that the failure fixture preserves existing application code and migration history."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile


spec = importlib.util.spec_from_file_location("rollback_drill", Path(__file__).with_name("rollback-drill.py"))
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)


class RollbackFixtureTest(unittest.TestCase):
    def test_candidate_adds_only_one_sql_resource_without_changing_existing_bytes(self):
        entries = {
            "META-INF/MANIFEST.MF": b"Main-Class: example.Launcher\n",
            "BOOT-INF/classes/com/example/Business.class": bytes(range(256)),
            "BOOT-INF/lib/example.jar": b"unchanged-nested-library",
            "BOOT-INF/classes/db/migration/V1__tables.sql": b"CREATE TABLE reservations (id int);",
            "BOOT-INF/classes/db/migration/V2__retry_key.sql": b"ALTER TABLE reservations ADD COLUMN retry_key text;",
        }
        with tempfile.TemporaryDirectory() as temporary:
            source, candidate = Path(temporary) / "source.jar", Path(temporary) / "candidate.jar"
            with zipfile.ZipFile(source, "w") as archive:
                for name, data in entries.items():
                    archive.writestr(name, data)
            version, resource = drill.make_candidate_jar(source, candidate)
            self.assertEqual(version, "3")
            with zipfile.ZipFile(candidate) as archive:
                for name, data in entries.items():
                    self.assertEqual(archive.read(name), data)
                self.assertEqual(set(archive.namelist()), set(entries) | {resource})
                self.assertIn(b"ADD COLUMN rollback_drill_note", archive.read(resource))
                self.assertNotIn(b"NOT NULL", archive.read(resource))

    def test_non_application_archive_cannot_be_used_as_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, candidate = Path(temporary) / "source.jar", Path(temporary) / "candidate.jar"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("arbitrary.txt", "Not a migrated application")
            with self.assertRaisesRegex(RuntimeError, "versioned database migrations"):
                drill.make_candidate_jar(source, candidate)
            self.assertFalse(candidate.exists())

    def test_reusing_prior_fixture_candidate_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, candidate = Path(temporary) / "source.jar", Path(temporary) / "candidate.jar"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("BOOT-INF/classes/db/migration/V3__rollback_drill_add_optional_note.sql", "existing")
            with self.assertRaisesRegex(RuntimeError, "prior drill candidate"):
                drill.make_candidate_jar(source, candidate)

    def test_changed_original_checksum_is_not_reported_as_schema_compatibility(self):
        before = [{"version": "1", "checksum": 123, "success": True}]
        after = [{"version": "1", "checksum": 456, "success": True},
                 {"version": "2", "checksum": 789, "success": True}]
        with self.assertRaisesRegex(RuntimeError, "pre-existing migration history"):
            drill.validate_history(before, after, "2")

    def test_failed_or_missing_candidate_migration_blocks_compatibility_claim(self):
        before = [{"version": "1", "checksum": 123, "success": True}]
        for after in (before, before + [{"version": "2", "checksum": 789, "success": False}]):
            with self.subTest(after=after), self.assertRaises(RuntimeError):
                drill.validate_history(before, after, "2")

    def test_cleanup_never_removes_the_verified_source_image(self):
        fixture = SimpleNamespace(image="sha256:" + "a" * 64, run_id="owned")
        with patch.object(drill, "docker") as docker:
            with self.assertRaisesRegex(RuntimeError, "verified application image"):
                drill.remove_candidate(fixture, fixture.image)
            docker.assert_not_called()

    def test_cleanup_refuses_a_foreign_candidate_image(self):
        fixture = SimpleNamespace(image="sha256:" + "a" * 64, run_id="owned")
        candidate = "sha256:" + "b" * 64
        inspection = [{"Id": candidate, "Config": {"Labels": {
            drill.recovery.OWNER_LABEL: "someone-else", drill.recovery.PURPOSE_LABEL: drill.recovery.PURPOSE,
            drill.CANDIDATE_LABEL: "true",
        }}}]
        with patch.object(drill, "docker", return_value=json.dumps(inspection).encode()) as docker:
            with self.assertRaisesRegex(RuntimeError, "ownership labels"):
                drill.remove_candidate(fixture, candidate)
            self.assertEqual(docker.call_args_list, [unittest.mock.call("image", "inspect", candidate)])

    def test_temporary_tag_cleanup_cannot_remove_the_sources_last_reference(self):
        fixture = SimpleNamespace(image="sha256:" + "a" * 64, prefix="inventory-drill-owned")
        tag = fixture.prefix + "-rollback-source:fixture"
        inspection = [{"Id": fixture.image, "RepoTags": [tag]}]
        with patch.object(drill, "docker", return_value=json.dumps(inspection).encode()) as docker:
            with self.assertRaisesRegex(RuntimeError, "last tag"):
                drill.remove_base_tag(fixture, tag)
            self.assertEqual(docker.call_args_list, [unittest.mock.call("image", "inspect", tag)])


if __name__ == "__main__":
    unittest.main()
