from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from linkmanager.config import LinkSpec
from linkmanager import core
from linkmanager.core import LinkService
from linkmanager.models import record_needs_attention


class ProcessInterrupted(BaseException):
    """Simulate a terminated process, bypassing normal exception rollback."""


class LinkServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="linkmanager-core-")
        self.root = Path(self._temporary.name)
        self.state_dir = self.root / "state"
        self.tree = self.root / "tree"
        self.tree.mkdir()
        self.service = LinkService(self.state_dir)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def make_spec(
        self,
        name: str = "item",
        *,
        kind: str = "junction",
        source_exists: bool = True,
        target_exists: bool = False,
    ) -> LinkSpec:
        source = self.tree / f"{name}-source"
        target = self.tree / f"{name}-target"
        if source_exists:
            source.mkdir(parents=True, exist_ok=True)
            (source / "payload.txt").write_text("source data", encoding="utf-8")
        if target_exists:
            target.mkdir(parents=True, exist_ok=True)
            (target / "existing.txt").write_text("existing data", encoding="utf-8")
        return LinkSpec("test-project", str(source), str(target), kind)

    def preview_one(self, spec: LinkSpec, *, backup_existing: bool = False) -> dict:
        plan = self.service.preview([spec], backup_existing=backup_existing)
        self.assertEqual(len(plan), 1)
        return plan[0]

    def execute_one(self, spec: LinkSpec, *, backup_existing: bool = False) -> dict:
        plan = [self.preview_one(spec, backup_existing=backup_existing)]
        self.assertTrue(plan[0]["valid"], plan[0].get("error"))
        return self.service.execute(plan)

    def assert_link_permission_skip(self, result: dict) -> None:
        if result.get("success"):
            return
        message = str(result.get("message", "")) + " " + str(result.get("results", ""))
        permission_markers = (
            "1314",
            "privilege",
            "privileges",
            "permission",
            "access is denied",
            "access denied",
            "not have sufficient",
            "requires elevation",
        )
        if os.name == "nt" and any(marker in message.lower() for marker in permission_markers):
            self.skipTest(f"Windows does not permit creating this link: {message}")
        self.fail(f"Link operation failed unexpectedly: {result}")

    def test_creates_junction_and_skips_same_target(self) -> None:
        spec = self.make_spec()
        result = self.execute_one(spec)
        self.assertTrue(result["success"], result)
        self.assertTrue(Path(spec.target).is_dir())
        self.assertEqual((Path(spec.target) / "payload.txt").read_text(encoding="utf-8"), "source data")

        second_plan = self.preview_one(spec)
        self.assertTrue(second_plan["valid"], second_plan.get("error"))
        self.assertEqual(second_plan["action"], "无需更改")
        second = self.service.execute([second_plan])
        self.assertTrue(second["success"], second)
        self.assertEqual((Path(spec.source) / "payload.txt").read_text(encoding="utf-8"), "source data")

    def test_ordinary_existing_target_is_rejected_by_default(self) -> None:
        spec = self.make_spec(target_exists=True)
        plan = self.preview_one(spec)
        self.assertFalse(plan["valid"])
        self.assertTrue(plan.get("error"))
        self.assertEqual((Path(spec.target) / "existing.txt").read_text(encoding="utf-8"), "existing data")
        self.assertFalse(Path(spec.target).is_symlink())

    def test_backup_existing_target_and_undo_restore_its_data(self) -> None:
        spec = self.make_spec(target_exists=True)
        result = self.execute_one(spec, backup_existing=True)
        self.assertTrue(result["success"], result)
        self.assertTrue(Path(spec.target).is_dir())
        self.assertEqual((Path(spec.target) / "payload.txt").read_text(encoding="utf-8"), "source data")

        records = self.service.history()
        self.assertTrue(records)
        undone = self.service.undo(records[-1].get("id", records[-1].get("record_id")))
        self.assertTrue(undone["success"], undone)
        self.assertTrue((Path(spec.target) / "existing.txt").is_file())
        self.assertEqual((Path(spec.target) / "existing.txt").read_text(encoding="utf-8"), "existing data")

    def preview_delete(self, specs: list[LinkSpec]) -> list[dict]:
        return self.service.preview(specs, delete_existing=True)

    def execute_delete_one(self, spec: LinkSpec) -> dict:
        plan = self.preview_delete([spec])
        self.assertEqual(len(plan), 1)
        self.assertTrue(plan[0]["valid"], plan[0].get("error"))
        return self.service.execute(plan)

    def test_delete_existing_directory_and_create_junction_without_backup(self) -> None:
        spec = self.make_spec("delete-dir", target_exists=True)
        target = Path(spec.target)
        (target / "nested").mkdir()
        (target / "nested" / "old.txt").write_text("remove me", encoding="utf-8")

        plan = self.preview_delete([spec])
        self.assertTrue(plan[0]["valid"], plan[0].get("error"))
        result = self.service.execute(plan)

        self.assertTrue(result["success"], result)
        self.assertTrue(target.is_dir())
        self.assertEqual((target / "payload.txt").read_text(encoding="utf-8"), "source data")
        self.assertFalse((target / "nested").exists())
        self.assertFalse(any(path.name.startswith(target.name + ".linkmanager-backup-")
                             for path in self.tree.iterdir()))

    @unittest.skipUnless(os.name == "nt", "Junction behavior is Windows-specific")
    def test_delete_directory_does_not_follow_nested_junction_outside_target(self) -> None:
        spec = self.make_spec("delete-nested-junction", target_exists=True)
        target = Path(spec.target)
        outside = self.root / "outside-data"
        outside.mkdir()
        (outside / "keep.txt").write_text("external data", encoding="utf-8")
        nested_link = target / "pointer-outside"
        core._junction(outside, nested_link)

        try:
            result = self.execute_delete_one(spec)
        finally:
            # Remove the link entry itself if an assertion or operation failed.
            if os.path.lexists(nested_link):
                nested_link.rmdir()

        self.assertTrue(result["success"], result)
        self.assertEqual((outside / "keep.txt").read_text(encoding="utf-8"), "external data")
        self.assertEqual((Path(spec.target) / "payload.txt").read_text(encoding="utf-8"), "source data")

    def test_delete_existing_correct_junction_forces_rebuild(self) -> None:
        spec = self.make_spec("delete-rebuild")
        first = self.execute_one(spec)
        self.assertTrue(first["success"], first)
        old_id = core._fingerprint(Path(spec.target))["ino"]

        plan = self.preview_delete([spec])
        self.assertTrue(plan[0]["valid"], plan[0].get("error"))
        self.assertNotEqual(plan[0]["action"], "无需更改")
        rebuilt = self.service.execute(plan)

        self.assertTrue(rebuilt["success"], rebuilt)
        self.assertNotEqual(core._fingerprint(Path(spec.target))["ino"], old_id)
        self.assertEqual((Path(spec.target) / "payload.txt").read_text(encoding="utf-8"), "source data")

    def test_delete_batch_missing_source_prevents_all_target_deletions(self) -> None:
        valid = self.make_spec("delete-batch-valid", target_exists=True)
        missing = self.make_spec("delete-batch-missing", source_exists=False, target_exists=True)
        before = (Path(valid.target) / "existing.txt").read_text(encoding="utf-8")

        plan = self.preview_delete([valid, missing])
        self.assertEqual(len(plan), 2)
        self.assertFalse(plan[1]["valid"])
        result = self.service.execute(plan)

        self.assertFalse(result["success"])
        self.assertEqual((Path(valid.target) / "existing.txt").read_text(encoding="utf-8"), before)
        self.assertEqual((Path(missing.target) / "existing.txt").read_text(encoding="utf-8"), "existing data")

    def test_delete_preview_rejects_nested_file_change(self) -> None:
        spec = self.make_spec("delete-changed-tree", target_exists=True)
        target = Path(spec.target)
        nested = target / "deep" / "data.txt"
        nested.parent.mkdir()
        nested.write_text("before", encoding="utf-8")
        plan = self.preview_delete([spec])
        self.assertTrue(plan[0]["valid"], plan[0].get("error"))

        nested.write_text("changed after preview", encoding="utf-8")
        result = self.service.execute(plan)

        self.assertFalse(result["success"])
        self.assertEqual(nested.read_text(encoding="utf-8"), "changed after preview")

    def test_delete_create_failure_reports_irrecoverable_data_and_cannot_undo(self) -> None:
        spec = self.make_spec("delete-create-fails", target_exists=True)
        plan = self.preview_delete([spec])
        self.assertTrue(plan[0]["valid"], plan[0].get("error"))

        with patch.object(core, "_create", side_effect=OSError("injected create failure")):
            result = self.service.execute(plan)

        self.assertFalse(result["success"], result)
        self.assertFalse(Path(spec.target).exists())
        self.assertTrue(any(word in result["message"] for word in ("永久", "无法恢复", "不可恢复")), result)
        records = self.service.history()
        self.assertTrue(records)
        undone = self.service.undo(records[0]["id"])
        self.assertFalse(undone["success"], undone)

    def test_delete_existing_file_and_replace_with_junction(self) -> None:
        source = self.tree / "delete-file-source"
        source.mkdir()
        (source / "payload.txt").write_text("source data", encoding="utf-8")
        target = self.tree / "delete-file-target"
        target.write_text("old file", encoding="utf-8")
        spec = LinkSpec("test-project", str(source), str(target), "junction")

        result = self.execute_delete_one(spec)

        self.assertTrue(result["success"], result)
        self.assertTrue(target.is_dir())
        self.assertEqual((target / "payload.txt").read_text(encoding="utf-8"), "source data")

    def test_delete_mode_removes_readonly_regular_file(self) -> None:
        source = self.tree / "readonly-source.txt"
        target = self.tree / "readonly-target.txt"
        source.write_text("source data", encoding="utf-8")
        target.write_text("old readonly data", encoding="utf-8")
        target.chmod(stat.S_IREAD)
        spec = LinkSpec("test-project", str(source), str(target), "hardlink")

        try:
            result = self.execute_delete_one(spec)
        finally:
            # Restore write permission for TemporaryDirectory cleanup on Windows.
            if target.exists():
                target.chmod(stat.S_IREAD | stat.S_IWRITE)

        self.assertTrue(result["success"], result)
        self.assertTrue(os.path.samefile(source, target))

    def test_backup_and_delete_modes_are_mutually_exclusive(self) -> None:
        spec = self.make_spec("delete-conflict", target_exists=True)

        plan = self.service.preview([spec], backup_existing=True, delete_existing=True)

        self.assertEqual(len(plan), 1)
        self.assertFalse(plan[0]["valid"])
        self.assertTrue(plan[0].get("error"))
        self.assertEqual((Path(spec.target) / "existing.txt").read_text(encoding="utf-8"), "existing data")

    def test_delete_mode_keeps_root_and_protected_target_rejections(self) -> None:
        protected_target = self.tree / "protected-delete-target"
        protected_target.mkdir()
        (protected_target / "keep.txt").write_text("keep", encoding="utf-8")
        source = self.tree / "protected-delete-source"
        source.mkdir()
        spec = LinkSpec("protected-delete", str(source), str(protected_target), "junction")
        protected_service = LinkService(self.state_dir, protected_paths=[protected_target])

        plan = protected_service.preview([spec], delete_existing=True)

        self.assertFalse(plan[0]["valid"])
        self.assertEqual((protected_target / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_missing_source_preflight_prevents_partial_batch_changes(self) -> None:
        valid = self.make_spec("valid")
        missing = self.make_spec("missing", source_exists=False)
        plan = self.service.preview([valid, missing])
        self.assertEqual(len(plan), 2)
        self.assertFalse(plan[1]["valid"])

        result = self.service.execute(plan)
        self.assertFalse(result["success"])
        self.assertFalse(Path(valid.target).exists())
        self.assertFalse(Path(missing.target).exists())

    def test_rejects_duplicate_targets_and_nested_targets(self) -> None:
        first = self.make_spec("first")
        duplicate = LinkSpec(first.project, str(self.tree / "other-source"), first.target, "junction")
        (self.tree / "other-source").mkdir()
        duplicate_plan = self.service.preview([first, duplicate])
        self.assertTrue(any(not item["valid"] for item in duplicate_plan))

        outer = self.make_spec("outer")
        nested_target = Path(outer.target) / "child-link"
        nested = LinkSpec("test-project", str(Path(outer.source)), str(nested_target), "junction")
        nested_plan = self.service.preview([outer, nested])
        self.assertTrue(any(not item["valid"] for item in nested_plan))

    def test_rejects_indirect_cycle_with_targets_inside_other_sources(self) -> None:
        source_a = self.tree / "A" / "source"
        source_b = self.tree / "B" / "source"
        source_a.mkdir(parents=True)
        source_b.mkdir(parents=True)
        specs = [
            LinkSpec("cycle", str(source_a), str(source_b / "link-a"), "junction"),
            LinkSpec("cycle", str(source_b), str(source_a / "link-b"), "junction"),
        ]

        plan = self.service.preview(specs)

        self.assertEqual(len(plan), 2)
        self.assertTrue(any(not item["valid"] for item in plan), plan)
        self.assertFalse((source_a / "link-b").exists())
        self.assertFalse((source_b / "link-a").exists())

    def test_rejects_cycles_and_root_protected_paths(self) -> None:
        left = self.tree / "left"
        right = self.tree / "right"
        left.mkdir()
        right.mkdir()
        cycle = [
            LinkSpec("cycle", str(left), str(right), "junction"),
            LinkSpec("cycle", str(right), str(left), "junction"),
        ]
        cycle_plan = self.service.preview(cycle, backup_existing=True)
        self.assertTrue(any(not item["valid"] for item in cycle_plan))

        protected_service = LinkService(self.state_dir, protected_paths=[self.root])
        protected = LinkSpec("protected", str(left), str(self.tree / "protected-target"), "junction")
        protected_plan = protected_service.preview([protected], delete_existing=True)
        self.assertFalse(protected_plan[0]["valid"])

    def test_rejects_project_path_as_link_target(self) -> None:
        project_path = self.tree / "project-data"
        project_path.mkdir()
        (project_path / "keep.txt").write_text("keep", encoding="utf-8")
        source = self.tree / "elsewhere"
        source.mkdir()
        spec = LinkSpec("protected-project", str(source), str(project_path), "junction")
        project_service = LinkService(self.state_dir, protected_paths=[project_path])
        plan = project_service.preview([spec], backup_existing=True)
        self.assertFalse(plan[0]["valid"])
        self.assertEqual((project_path / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_rejects_preview_after_external_target_change(self) -> None:
        spec = self.make_spec()
        plan = self.preview_one(spec)
        Path(spec.target).mkdir()
        (Path(spec.target) / "external.txt").write_text("external", encoding="utf-8")

        result = self.service.execute([plan])
        self.assertFalse(result["success"])
        self.assertEqual((Path(spec.target) / "external.txt").read_text(encoding="utf-8"), "external")

    def test_unlink_removes_link_without_touching_source(self) -> None:
        spec = self.make_spec()
        result = self.execute_one(spec)
        self.assertTrue(result["success"], result)

        unlink_result = self.service.unlink([spec])
        self.assertTrue(unlink_result["success"], unlink_result)
        self.assertFalse(Path(spec.target).exists())
        self.assertEqual((Path(spec.source) / "payload.txt").read_text(encoding="utf-8"), "source data")

    def test_hardlink_creation_and_removal(self) -> None:
        source = self.tree / "hard-source.txt"
        target = self.tree / "hard-target.txt"
        source.write_text("hard linked bytes", encoding="utf-8")
        spec = LinkSpec("test-project", str(source), str(target), "hardlink")
        result = self.execute_one(spec)
        self.assertTrue(result["success"], result)
        self.assertTrue(os.path.samefile(source, target))

        unlinked = self.service.unlink([spec])
        self.assertTrue(unlinked["success"], unlinked)
        self.assertFalse(target.exists())
        self.assertEqual(source.read_text(encoding="utf-8"), "hard linked bytes")

    def test_hardlink_unlink_refuses_when_source_disappears_after_preflight(self) -> None:
        source = self.tree / "unlink-race-source.txt"
        target = self.tree / "unlink-race-target.txt"
        source.write_text("keep target name", encoding="utf-8")
        spec = LinkSpec("test-project", str(source), str(target), "hardlink")
        created = self.execute_one(spec)
        self.assertTrue(created["success"], created)
        self.assertTrue(os.path.samefile(source, target))

        record = self.service._record

        def delete_source_after_unlink_record(action: str) -> dict:
            result = record(action)
            if action == "unlink":
                source.unlink()
            return result

        with patch.object(self.service, "_record", side_effect=delete_source_after_unlink_record):
            removed = self.service.unlink([spec])

        self.assertFalse(removed["success"], removed)
        self.assertFalse(source.exists())
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_text(encoding="utf-8"), "keep target name")

    def test_batch_hardlinks_from_same_source_can_be_undone(self) -> None:
        source = self.tree / "shared-hard-source.txt"
        first_target = self.tree / "shared-hard-target-1.txt"
        second_target = self.tree / "shared-hard-target-2.txt"
        source.write_text("shared bytes", encoding="utf-8")
        specs = [
            LinkSpec("test-project", str(source), str(first_target), "hardlink"),
            LinkSpec("test-project", str(source), str(second_target), "hardlink"),
        ]

        plan = self.service.preview(specs)
        self.assertEqual(len(plan), 2)
        self.assertTrue(all(item["valid"] for item in plan), plan)
        result = self.service.execute(plan)
        self.assertTrue(result["success"], result)
        self.assertTrue(os.path.samefile(source, first_target))
        self.assertTrue(os.path.samefile(source, second_target))

        record = next(record for record in self.service.history() if record["action"] == "create")
        undone = self.service.undo(record["id"])
        self.assertTrue(undone["success"], undone)
        self.assertFalse(first_target.exists())
        self.assertFalse(second_target.exists())
        self.assertEqual(source.read_text(encoding="utf-8"), "shared bytes")

    def test_undo_refuses_backup_modified_after_creation(self) -> None:
        spec = self.make_spec("modified-backup", target_exists=True)
        result = self.execute_one(spec, backup_existing=True)
        self.assertTrue(result["success"], result)

        record = next(record for record in self.service.history() if record["action"] == "create")
        backup = Path(record["operations"][0]["backup"])
        self.assertTrue(backup.is_dir())
        (backup / "existing.txt").write_text("changed outside service", encoding="utf-8")

        undone = self.service.undo(record["id"])
        self.assertFalse(undone["success"])
        self.assertTrue(Path(spec.target).is_dir())
        self.assertEqual((Path(spec.target) / "payload.txt").read_text(encoding="utf-8"), "source data")
        self.assertEqual((backup / "existing.txt").read_text(encoding="utf-8"), "changed outside service")

    def test_undo_retries_backup_restore_after_first_rename_failure(self) -> None:
        spec = self.make_spec("retry-backup", target_exists=True)
        result = self.execute_one(spec, backup_existing=True)
        self.assertTrue(result["success"], result)

        record = next(record for record in self.service.history() if record["action"] == "create")
        backup = Path(record["operations"][0]["backup"])
        target = Path(spec.target)
        original_rename = Path.rename
        injected = {"done": False}

        def fail_backup_restore_once(path: Path, destination: str | Path) -> Path:
            if path == backup and Path(destination) == target and not injected["done"]:
                injected["done"] = True
                raise PermissionError("injected one-time backup restore failure")
            return original_rename(path, destination)

        with patch.object(Path, "rename", autospec=True, side_effect=fail_backup_restore_once):
            first_undo = self.service.undo(record["id"])

        self.assertTrue(injected["done"])
        self.assertFalse(first_undo["success"], first_undo)
        self.assertFalse(os.path.lexists(target))
        self.assertTrue((backup / "existing.txt").is_file())
        self.assertEqual((backup / "existing.txt").read_text(encoding="utf-8"), "existing data")

        second_undo = self.service.undo(record["id"])
        self.assertTrue(second_undo["success"], second_undo)
        self.assertTrue((target / "existing.txt").is_file())
        self.assertEqual((target / "existing.txt").read_text(encoding="utf-8"), "existing data")
        self.assertFalse(backup.exists())

    def test_symlink_creation_and_removal_or_skip_for_permission(self) -> None:
        for kind in ("dir_symlink", "file_symlink"):
            with self.subTest(kind=kind):
                if kind == "dir_symlink":
                    source = self.tree / "symlink-dir-source"
                    target = self.tree / "symlink-dir-target"
                    source.mkdir(exist_ok=True)
                    (source / "payload.txt").write_text("dir", encoding="utf-8")
                else:
                    source = self.tree / "symlink-file-source.txt"
                    target = self.tree / "symlink-file-target.txt"
                    source.write_text("file", encoding="utf-8")
                spec = LinkSpec("test-project", str(source), str(target), kind)
                result = self.execute_one(spec)
                if not result.get("success"):
                    self.assertTrue(source.exists(), f"failed symlink creation changed the source: {result}")
                    self.assertFalse(os.path.lexists(target), f"failed symlink creation left a target behind: {result}")
                self.assert_link_permission_skip(result)
                self.assertTrue(target.is_symlink())
                self.assertEqual(os.path.realpath(target), os.path.realpath(source))
                removed = self.service.unlink([spec])
                self.assertTrue(removed["success"], removed)
                self.assertTrue(source.exists())
                self.assertFalse(target.exists())

    def test_inspect_reports_target_and_source_state(self) -> None:
        spec = self.make_spec()
        state = self.service.inspect(spec)
        self.assertIn("status", state)
        self.assertEqual(state["target_kind"], "missing")
        self.assertFalse(state["actual_target"])
        self.assertTrue(state["source_exists"])
        self.assertFalse(state["target_exists"])
        self.assertIn("detail", state)

    def test_source_containment_batch_succeeds_and_undoes_in_both_orders(self) -> None:
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                base = self.tree / str(reverse)
                source_a, source_b = base / "A", base / "B"
                source_a.mkdir(parents=True)
                source_b.mkdir()
                (source_b / "data.txt").write_text("keep B", encoding="utf-8")
                specs = [LinkSpec("batch", str(source_b), str(source_a / "nested"), "junction"),
                         LinkSpec("batch", str(source_a), str(base / "C"), "junction")]
                if reverse:
                    specs.reverse()
                result = self.service.execute(self.service.preview(specs))
                self.assertTrue(result['success'], result)
                self.assertEqual((base / "C" / "nested" / "data.txt").read_text(), "keep B")
                record = self.service.history()[0]
                undone = self.service.undo(record['id'])
                self.assertTrue(undone['success'], undone)
                self.assertFalse(os.path.lexists(base / "C"))
                self.assertFalse(os.path.lexists(source_a / "nested"))
                self.assertEqual((source_b / "data.txt").read_text(), "keep B")

    @unittest.skipUnless(os.name == 'nt', 'Windows shared read-only attributes')
    def test_readonly_hardlink_rebuild_preflight_keeps_source_attributes(self) -> None:
        source, target = self.tree / "readonly-shared-source", self.tree / "readonly-shared-target"
        source.write_text("shared bytes", encoding="utf-8")
        os.link(source, target)
        source.chmod(stat.S_IREAD)
        before = core._fingerprint(source)
        try:
            spec = LinkSpec("readonly", str(source), str(target), "hardlink")
            plan = self.service.preview([spec], delete_existing=True)
            self.assertFalse(plan[0]['valid'], plan)
            self.assertFalse(self.service.execute(plan)['success'])
            self.assertTrue(os.path.samefile(source, target))
            self.assertEqual(core._fingerprint(source), before)
            self.assertEqual(target.read_text(), "shared bytes")
        finally:
            source.chmod(stat.S_IREAD | stat.S_IWRITE)

    @unittest.skipUnless(os.name == 'nt', 'Windows shared read-only attributes')
    def test_nested_readonly_hardlink_preflight_keeps_external_data_and_target_tree(self) -> None:
        spec = self.make_spec("nested-readonly", target_exists=True)
        external = self.tree / "external-shared-file"
        external.write_text("external data", encoding="utf-8")
        nested = Path(spec.target) / "nested" / "external-name"
        nested.parent.mkdir()
        os.link(external, nested)
        external.chmod(stat.S_IREAD)
        before = core._fingerprint(external)
        try:
            plan = self.service.preview([spec], delete_existing=True)
            self.assertFalse(plan[0]['valid'], plan)
            self.assertFalse(self.service.execute(plan)['success'])
            self.assertEqual(core._fingerprint(external), before)
            self.assertTrue(os.path.samefile(external, nested))
            self.assertEqual((Path(spec.target) / "existing.txt").read_text(), "existing data")
        finally:
            external.chmod(stat.S_IREAD | stat.S_IWRITE)

    def test_batch_rejects_source_edit_between_operations(self) -> None:
        first, second = self.make_spec("race-first"), self.make_spec("race-second")
        create = core._create
        def create_and_edit(source, target, kind):
            create(source, target, kind)
            if target == Path(first.target):
                (Path(second.source) / "external.txt").write_text("outside change")
        with patch.object(core, '_create', side_effect=create_and_edit):
            result = self.service.execute(self.service.preview([first, second]))
        self.assertFalse(result['success'], result)
        self.assertFalse(os.path.lexists(first.target))
        self.assertFalse(os.path.lexists(second.target))
        self.assertEqual((Path(second.source) / "external.txt").read_text(), "outside change")

    def interrupt_phase(self, phase: str):
        store = self.service._store
        def interrupted(record):
            store(record)
            if record['action'] == 'create' and any(op['phase'] == phase for op in record['operations']):
                raise ProcessInterrupted(phase)
        return patch.object(self.service, '_store', side_effect=interrupted)

    def test_recover_durable_creation_phases_with_new_service_instance(self) -> None:
        scenarios = [('prepared', False), ('backup_pending', True), ('backed_up', True),
                     ('create_pending', True), ('created', True)]
        for phase, backup in scenarios:
            with self.subTest(phase=phase):
                spec = self.make_spec('recover-' + phase, target_exists=backup)
                with self.interrupt_phase(phase):
                    with self.assertRaises(ProcessInterrupted):
                        self.service.execute(self.service.preview([spec], backup_existing=backup))
                record_id = self.service.history()[0]['id']
                fresh = LinkService(self.state_dir)
                self.assertTrue(fresh.preview_recovery(record_id)['success'])
                restored = fresh.recover(record_id)
                self.assertTrue(restored['success'], restored)
                if backup:
                    self.assertEqual((Path(spec.target) / 'existing.txt').read_text(), 'existing data')
                else:
                    self.assertFalse(os.path.lexists(spec.target))
                original = next(r for r in fresh.history() if r['id'] == record_id)
                self.assertTrue(original['recovered'])
                self.assertFalse(record_needs_attention(original))
                self.assertFalse(fresh.recover(record_id)['success'])

    def test_recover_old_link_removal_and_writable_hardlink_rebuild_phases(self) -> None:
        for kind, phases in [('junction', ('remove_pending', 'old_removed')),
                             ('hardlink', ('delete_pending', 'target_deleted'))]:
            for phase in phases:
                with self.subTest(kind=kind, phase=phase):
                    if kind == 'junction':
                        spec = self.make_spec('old-' + phase)
                        old_source = self.tree / ('original-' + phase)
                        old_source.mkdir()
                        (old_source / 'old.txt').write_text('old link source')
                        core._create(old_source, Path(spec.target), kind)
                    else:
                        source = self.tree / ('hard-old-' + phase)
                        source.write_text('hard bytes')
                        target = self.tree / ('hard-target-' + phase)
                        os.link(source, target)
                        spec = LinkSpec('hard recovery', str(source), str(target), kind)
                        old_source = source
                    with self.interrupt_phase(phase):
                        with self.assertRaises(ProcessInterrupted):
                            self.service.execute(self.service.preview([spec], delete_existing=(kind == 'hardlink')))
                    record_id = self.service.history()[0]['id']
                    self.assertTrue(self.service.preview_recovery(record_id)['success'])
                    result = self.service.recover(record_id)
                    self.assertTrue(result['success'], result)
                    if kind == 'junction':
                        self.assertEqual(core._readlink(Path(spec.target)), old_source)
                    else:
                        self.assertTrue(os.path.samefile(spec.target, old_source))
                        self.assertEqual(old_source.read_text(), 'hard bytes')

    def test_recovery_retry_resolves_interrupted_attempt_at_each_revert_phase(self) -> None:
        for phase in ('remove_created_pending', 'created_removed', 'restore_pending', 'restored', 'rolled_back'):
            with self.subTest(phase=phase):
                spec = self.make_spec('revert-' + phase, target_exists=True)
                with self.interrupt_phase('created'):
                    with self.assertRaises(ProcessInterrupted):
                        self.service.execute(self.service.preview([spec], backup_existing=True))
                record_id = self.service.history()[0]['id']
                with self.interrupt_phase(phase):
                    with self.assertRaises(ProcessInterrupted):
                        self.service.recover(record_id)
                pending_attempt = next(r for r in self.service.history() if r['action'] == 'recover' and r.get('recovery_of') == record_id)
                self.assertEqual(pending_attempt['state'], 'pending')
                result = self.service.recover(record_id)
                self.assertTrue(result['success'], result)
                self.assertEqual((Path(spec.target) / 'existing.txt').read_text(), 'existing data')
                old_attempt = next(r for r in self.service.history() if r['id'] == pending_attempt['id'])
                self.assertFalse(record_needs_attention(old_attempt), old_attempt)
                self.assertTrue(old_attempt['recovered'])

    def test_recovery_refuses_entry_created_without_durable_identity(self) -> None:
        spec = self.make_spec('unrecorded-created', target_exists=True)
        create = core._create
        def interrupted_create(source, target, kind):
            create(source, target, kind)
            raise ProcessInterrupted('created before checkpoint')
        with patch.object(core, '_create', side_effect=interrupted_create):
            with self.assertRaises(ProcessInterrupted):
                self.service.execute(self.service.preview([spec], backup_existing=True))
        record = self.service.history()[0]
        self.assertEqual(record['operations'][0]['phase'], 'create_pending')
        before = core._fingerprint(Path(spec.target))
        self.assertFalse(self.service.preview_recovery(record['id'])['success'])
        self.assertFalse(self.service.recover(record['id'])['success'])
        self.assertEqual(core._fingerprint(Path(spec.target)), before)
        self.assertEqual((Path(record['operations'][0]['backup']) / 'existing.txt').read_text(), 'existing data')

    def test_recovery_refuses_unknown_phase_and_irreversible_record(self) -> None:
        for flag in ('phase', 'irreversible'):
            with self.subTest(flag=flag):
                spec = self.make_spec('unknown-' + flag)
                with self.interrupt_phase('created'):
                    with self.assertRaises(ProcessInterrupted):
                        self.service.execute(self.service.preview([spec]))
                record = self.service.history()[0]
                if flag == 'phase':
                    record['operations'][0]['phase'] = 'unrecognized'
                else:
                    record['irreversible'] = True
                self.service._store(record)
                before = core._fingerprint(Path(spec.target))
                self.assertFalse(self.service.preview_recovery(record['id'])['success'])
                self.assertFalse(self.service.recover(record['id'])['success'])
                self.assertEqual(core._fingerprint(Path(spec.target)), before)

    def test_recovery_refuses_changed_backup_or_replaced_parent(self) -> None:
        spec = self.make_spec('recovery-backup-change', target_exists=True)
        with self.interrupt_phase('created'):
            with self.assertRaises(ProcessInterrupted):
                self.service.execute(self.service.preview([spec], backup_existing=True))
        record = self.service.history()[0]
        backup = Path(record['operations'][0]['backup'])
        (backup / 'existing.txt').write_text('external backup edit')
        self.assertFalse(self.service.recover(record['id'])['success'])
        self.assertEqual((backup / 'existing.txt').read_text(), 'external backup edit')
        self.assertEqual((Path(spec.target) / 'payload.txt').read_text(), 'source data')

        source = self.tree / 'new-parent-source'
        source.mkdir()
        parent = self.tree / 'new-parent'
        target = parent / 'entry'
        parent_spec = LinkSpec('parent', str(source), str(target), 'junction')
        with self.interrupt_phase('create_pending'):
            with self.assertRaises(ProcessInterrupted):
                self.service.execute(self.service.preview([parent_spec]))
        parent_record = self.service.history()[0]
        parent.rename(self.tree / 'saved-parent')
        parent.mkdir()
        (parent / 'outside.txt').write_text('external directory')
        self.assertFalse(self.service.recover(parent_record['id'])['success'])
        self.assertEqual((parent / 'outside.txt').read_text(), 'external directory')

    def test_recovery_refuses_unrecorded_mkdir_after_crash(self) -> None:
        spec = self.make_spec('pending-parent')
        target = self.tree / 'unrecorded-parent' / 'entry'
        spec = LinkSpec(spec.project, spec.source, str(target), spec.kind)
        mkdir = Path.mkdir
        def interrupted_mkdir(path, *args, **kwargs):
            result = mkdir(path, *args, **kwargs)
            if path == target.parent:
                raise ProcessInterrupted('mkdir before checkpoint')
            return result
        with patch.object(Path, 'mkdir', autospec=True, side_effect=interrupted_mkdir):
            with self.assertRaises(ProcessInterrupted):
                self.service.execute(self.service.preview([spec]))
        record = self.service.history()[0]
        self.assertTrue(target.parent.is_dir())
        self.assertFalse(self.service.recover(record['id'])['success'])
        self.assertTrue(target.parent.is_dir())

    def test_transient_history_write_failure_rolls_back_verified_link(self) -> None:
        spec = self.make_spec('transient-write', target_exists=True)
        store = self.service._store
        failed = False
        def fail_once(record):
            nonlocal failed
            if not failed and any(op['phase'] == 'created' for op in record['operations']):
                failed = True
                raise OSError('injected journal write failure')
            store(record)
        with patch.object(self.service, '_store', side_effect=fail_once):
            result = self.service.execute(self.service.preview([spec], backup_existing=True))
        self.assertFalse(result['success'], result)
        self.assertTrue(failed)
        self.assertEqual((Path(spec.target) / 'existing.txt').read_text(), 'existing data')
        self.assertFalse(record_needs_attention(self.service.history()[0]))

    def test_persistent_history_write_failure_preserves_unverified_pending_target(self) -> None:
        spec = self.make_spec('persistent-write')
        store = self.service._store
        failed = False
        def fail_after_create(record):
            nonlocal failed
            failed = failed or any(op['phase'] == 'created' for op in record['operations'])
            if failed:
                raise OSError('injected persistent journal failure')
            store(record)
        with patch.object(self.service, '_store', side_effect=fail_after_create):
            result = self.service.execute(self.service.preview([spec]))
        self.assertFalse(result['success'], result)
        self.assertTrue(Path(spec.target).is_dir())
        durable = self.service.history()[0]
        self.assertEqual(durable['operations'][0]['phase'], 'create_pending')
        self.assertTrue(record_needs_attention(durable))
        self.assertFalse(self.service.recover(durable['id'])['success'])
        self.assertTrue(Path(spec.target).is_dir())

    def test_final_history_write_failure_can_recover_verified_link(self) -> None:
        spec = self.make_spec('final-write')
        store = self.service._store
        def fail_completion(record):
            if record.get('state') == 'completed':
                raise OSError('injected final history failure')
            store(record)
        with patch.object(self.service, '_store', side_effect=fail_completion):
            result = self.service.execute(self.service.preview([spec]))
        self.assertFalse(result['success'], result)
        durable = self.service.history()[0]
        self.assertEqual(durable['state'], 'pending')
        self.assertEqual(durable['operations'][0]['phase'], 'created')
        self.assertTrue(self.service.recover(durable['id'])['success'])
        self.assertFalse(os.path.lexists(spec.target))

    def test_prepared_recovery_does_not_consume_an_unverified_second_entry(self) -> None:
        spec = self.make_spec('prepared-backup', target_exists=True)
        with self.interrupt_phase('prepared'):
            with self.assertRaises(ProcessInterrupted):
                self.service.execute(self.service.preview([spec], backup_existing=True))
        record = self.service.history()[0]
        backup = Path(record['operations'][0]['backup'])
        original = self.tree / 'external-original'
        Path(spec.target).rename(original)
        backup.mkdir()
        (backup / 'outside.txt').write_text('unverified entry')
        self.assertFalse(self.service.preview_recovery(record['id'])['success'])
        self.assertFalse(self.service.recover(record['id'])['success'])
        self.assertFalse(os.path.lexists(spec.target))
        self.assertEqual((backup / 'outside.txt').read_text(), 'unverified entry')
        self.assertEqual((original / 'existing.txt').read_text(), 'existing data')

    def test_recovery_refuses_changed_old_hardlink_source_identity(self) -> None:
        source, target = self.tree / 'old-hard-source', self.tree / 'old-hard-target'
        source.write_text('original bytes')
        os.link(source, target)
        spec = LinkSpec('old hard source', str(source), str(target), 'hardlink')
        with self.interrupt_phase('target_deleted'):
            with self.assertRaises(ProcessInterrupted):
                self.service.execute(self.service.preview([spec], delete_existing=True))
        record = self.service.history()[0]
        source.rename(self.tree / 'preserved-old-source')
        source.write_text('external replacement')
        self.assertFalse(self.service.recover(record['id'])['success'])
        self.assertFalse(target.exists())
        self.assertEqual(source.read_text(), 'external replacement')
        self.assertEqual((self.tree / 'preserved-old-source').read_text(), 'original bytes')

    def test_recovery_resolves_interrupted_undo_attempt(self) -> None:
        spec = self.make_spec('interrupted-undo', target_exists=True)
        result = self.execute_one(spec, backup_existing=True)
        self.assertTrue(result['success'], result)
        record_id = self.service.history()[0]['id']
        with self.interrupt_phase('created_removed'):
            with self.assertRaises(ProcessInterrupted):
                self.service.undo(record_id)
        old_attempt = next(r for r in self.service.history() if r['action'] == 'undo' and r.get('undo_of') == record_id)
        self.assertTrue(record_needs_attention(old_attempt))
        retried = self.service.undo(record_id)
        self.assertTrue(retried['success'], retried)
        old_attempt = next(r for r in self.service.history() if r['id'] == old_attempt['id'])
        self.assertFalse(record_needs_attention(old_attempt))
        self.assertEqual((Path(spec.target) / 'existing.txt').read_text(), 'existing data')

    def test_undo_preflight_keeps_link_when_created_parent_identity_changed(self) -> None:
        source = self.tree / 'undo-parent-source'
        source.mkdir()
        parent = self.tree / 'undo-parent'
        spec = LinkSpec('parent', str(source), str(parent / 'entry'), 'junction')
        result = self.execute_one(spec)
        self.assertTrue(result['success'], result)
        record = self.service.history()[0]
        saved = self.tree / 'renamed-original-parent'
        parent.rename(saved)
        parent.mkdir()
        core._create(source, parent / 'entry', 'junction')
        undone = self.service.undo(record['id'])
        self.assertFalse(undone['success'], undone)
        self.assertTrue(os.path.lexists(parent / 'entry'))
        self.assertTrue(os.path.lexists(saved / 'entry'))

    def test_undo_preserves_new_unrelated_parent_contents(self) -> None:
        source = self.tree / 'unrelated-parent-source'
        source.mkdir()
        parent = self.tree / 'unrelated-parent'
        spec = LinkSpec('parent', str(source), str(parent / 'entry'), 'junction')
        self.assertTrue(self.execute_one(spec)['success'])
        record_id = self.service.history()[0]['id']
        (parent / 'outside.txt').write_text('unrelated contents')
        undone = self.service.undo(record_id)
        self.assertTrue(undone['success'], undone)
        self.assertFalse(os.path.lexists(parent / 'entry'))
        self.assertEqual((parent / 'outside.txt').read_text(), 'unrelated contents')

    def test_batch_shares_only_verified_parents_created_by_same_transaction(self) -> None:
        first, second = self.make_spec('shared-parent-1'), self.make_spec('shared-parent-2')
        parent = self.tree / 'shared-new-parent'
        specs = [LinkSpec(first.project, first.source, str(parent / 'first'), first.kind),
                 LinkSpec(second.project, second.source, str(parent / 'second'), second.kind)]
        result = self.service.execute(self.service.preview(specs))
        self.assertTrue(result['success'], result)
        record_id = self.service.history()[0]['id']
        self.assertTrue(self.service.undo(record_id)['success'])
        self.assertFalse(parent.exists())

    def test_execute_rejects_missing_parent_created_externally_during_batch(self) -> None:
        first, second = self.make_spec('external-parent-1'), self.make_spec('external-parent-2')
        parent = self.tree / 'external-new-parent'
        second = LinkSpec(second.project, second.source, str(parent / 'second'), second.kind)
        create = core._create
        def create_and_add_parent(source, target, kind):
            create(source, target, kind)
            if target == Path(first.target):
                parent.mkdir()
                (parent / 'external.txt').write_text('keep external directory')
        with patch.object(core, '_create', side_effect=create_and_add_parent):
            result = self.service.execute(self.service.preview([first, second]))
        self.assertFalse(result['success'], result)
        self.assertFalse(os.path.lexists(first.target))
        self.assertFalse(os.path.lexists(second.target))
        self.assertEqual((parent / 'external.txt').read_text(), 'keep external directory')


if __name__ == "__main__":
    unittest.main()
