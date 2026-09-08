from __future__ import annotations

from hashlib import sha256
import tempfile
from pathlib import Path
import os
import sqlite3
import unittest
from unittest.mock import patch

from qingtian_core.models import (
    ConflictError,
    NotFoundError,
    RunState,
    SessionState,
    StorageContractError,
    TaskState,
    TransitionError,
)
from qingtian_core.store import QingtianStore


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="qingtian-store-test-")
        self.store = QingtianStore(Path(self.temp.name) / "qingtian.db")
        self.store.initialize()
        self.task = self.store.create_task(
            project_id="project-a",
            title="Example",
            objective="Exercise the state contract",
            scope=["docs"],
            acceptance=["receipt exists"],
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def activate(self, task: dict[str, object]) -> dict[str, object]:
        ready = self.store.transition_task(
            str(task["task_id"]), TaskState.READY, expected_revision=int(task["revision"])
        )
        return self.store.transition_task(
            str(task["task_id"]), TaskState.RUNNING, expected_revision=ready["revision"]
        )

    def test_task_cas_and_legal_transitions(self) -> None:
        if os.name == "posix":
            self.assertEqual(Path(self.store.database).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.task["schema_version"], 1)
        ready = self.store.transition_task(
            self.task["task_id"], TaskState.READY, expected_revision=1
        )
        self.assertEqual(ready["revision"], 2)
        with self.assertRaises(ConflictError):
            self.store.transition_task(
                self.task["task_id"], TaskState.RUNNING, expected_revision=1
            )
        with self.assertRaises(TransitionError):
            self.store.transition_task(
                self.task["task_id"], TaskState.DONE, expected_revision=2
            )

    def test_session_run_idempotency_and_unknown_reconciliation(self) -> None:
        self.task = self.activate(self.task)
        session = self.store.create_session(
            task_id=self.task["task_id"], host_alias="test-host"
        )
        session = self.store.transition_session(session["session_id"], SessionState.ACTIVE)
        self.assertEqual(session["state"], "ACTIVE")
        first = self.store.create_run(
            task_id=self.task["task_id"],
            session_id=session["session_id"],
            executor="test",
            idempotency_key="same-key",
            request={"value": 1},
        )
        replay = self.store.create_run(
            task_id=self.task["task_id"],
            session_id=session["session_id"],
            executor="test",
            idempotency_key="same-key",
            request={"value": 1},
        )
        self.assertEqual(first["run_id"], replay["run_id"])
        with self.assertRaises(ConflictError):
            self.store.create_run(
                task_id=self.task["task_id"],
                session_id=session["session_id"],
                executor="test",
                idempotency_key="same-key",
                request={"value": 2},
            )
        running = self.store.transition_run(first["run_id"], RunState.RUNNING)
        unknown = self.store.transition_run(
            running["run_id"], RunState.UNKNOWN, external_operation_id="external-123"
        )
        resolved = self.store.transition_run(unknown["run_id"], RunState.SUCCEEDED)
        self.assertEqual(resolved["external_operation_id"], "external-123")
        self.assertEqual(resolved["schema_version"], 1)

    def test_idempotency_key_is_scoped_to_task(self) -> None:
        self.task = self.activate(self.task)
        first_session = self.store.create_session(
            task_id=self.task["task_id"], host_alias="first-host"
        )
        first_session = self.store.transition_session(
            first_session["session_id"], SessionState.ACTIVE
        )
        second_task = self.store.create_task(
            project_id="project-a",
            title="Second",
            objective="Use an independent idempotency scope",
            scope=["tests"],
            acceptance=["run exists"],
        )
        second_task = self.activate(second_task)
        second_session = self.store.create_session(
            task_id=second_task["task_id"], host_alias="second-host"
        )
        second_session = self.store.transition_session(
            second_session["session_id"], SessionState.ACTIVE
        )
        first_run = self.store.create_run(
            task_id=self.task["task_id"],
            session_id=first_session["session_id"],
            executor="test",
            idempotency_key="shared-client-key",
            request={"value": 1},
        )
        second_run = self.store.create_run(
            task_id=second_task["task_id"],
            session_id=second_session["session_id"],
            executor="test",
            idempotency_key="shared-client-key",
            request={"value": 1},
        )
        self.assertNotEqual(first_run["run_id"], second_run["run_id"])

    def test_cross_aggregate_state_invariants_and_operation_identity(self) -> None:
        with self.assertRaises(TransitionError):
            self.store.create_session(task_id=self.task["task_id"], host_alias="too-early")
        self.task = self.activate(self.task)
        session = self.store.create_session(
            task_id=self.task["task_id"], host_alias="state-host"
        )
        session = self.store.transition_session(session["session_id"], SessionState.ACTIVE)
        run = self.store.create_run(
            task_id=self.task["task_id"],
            session_id=session["session_id"],
            executor="state-test",
            request={"value": 1},
        )
        run = self.store.transition_run(run["run_id"], RunState.RUNNING)
        run = self.store.transition_run(
            run["run_id"], RunState.UNKNOWN, external_operation_id="operation-1"
        )
        with self.assertRaises(TransitionError):
            self.store.transition_task(
                self.task["task_id"],
                TaskState.REVIEW_PENDING,
                expected_revision=self.task["revision"],
            )
        with self.assertRaises(ConflictError):
            self.store.transition_run(
                run["run_id"], RunState.SUCCEEDED, external_operation_id="operation-2"
            )
        self.store.transition_run(run["run_id"], RunState.SUCCEEDED)
        session = self.store.transition_session(session["session_id"], SessionState.QUIESCING)
        session = self.store.transition_session(session["session_id"], SessionState.CHECKPOINTED)
        review = self.store.transition_task(
            self.task["task_id"],
            TaskState.REVIEW_PENDING,
            expected_revision=self.task["revision"],
        )
        self.store.transition_task(
            self.task["task_id"], TaskState.DONE, expected_revision=review["revision"]
        )
        with self.assertRaises(TransitionError):
            self.store.create_run(
                task_id=self.task["task_id"],
                session_id=session["session_id"],
                executor="too-late",
            )

    def test_in_flight_run_blocks_session_handoff(self) -> None:
        self.task = self.activate(self.task)
        old_session = self.store.create_session(
            task_id=self.task["task_id"], host_alias="old-host"
        )
        old_session = self.store.transition_session(
            old_session["session_id"], SessionState.ACTIVE
        )
        run = self.store.create_run(
            task_id=self.task["task_id"],
            session_id=old_session["session_id"],
            executor="external-writer",
        )
        run = self.store.transition_run(run["run_id"], RunState.RUNNING)
        self.store.transition_session(old_session["session_id"], SessionState.QUIESCING)
        with self.assertRaisesRegex(TransitionError, "in flight"):
            self.store.transition_session(old_session["session_id"], SessionState.CHECKPOINTED)

        checkpoint = self.store.create_checkpoint(
            task_id=self.task["task_id"],
            snapshot={"source_session_id": old_session["session_id"]},
        )
        with self.assertRaisesRegex(TransitionError, "prior session"):
            self.store.create_session(
                task_id=self.task["task_id"],
                host_alias="new-host",
                source_checkpoint_id=checkpoint["checkpoint_id"],
            )

        unknown = self.store.transition_run(
            run["run_id"], RunState.UNKNOWN, external_operation_id="external-still-running"
        )
        old_session = self.store.transition_session(
            old_session["session_id"], SessionState.CHECKPOINTED
        )
        recovered = self.store.create_session(
            task_id=self.task["task_id"],
            host_alias="new-host",
            source_checkpoint_id=checkpoint["checkpoint_id"],
        )
        recovered = self.store.transition_session(
            recovered["session_id"], SessionState.ACTIVE
        )
        with self.assertRaisesRegex(TransitionError, "UNKNOWN"):
            self.store.create_run(
                task_id=self.task["task_id"],
                session_id=recovered["session_id"],
                executor="must-not-replay",
            )
        self.store.transition_run(unknown["run_id"], RunState.SUCCEEDED)
        resumed = self.store.create_run(
            task_id=self.task["task_id"],
            session_id=recovered["session_id"],
            executor="safe-after-reconcile",
        )
        self.assertEqual(resumed["state"], RunState.PLANNED.value)

    def test_handoff_cannot_bypass_checkpoint_by_omitting_source(self) -> None:
        self.task = self.activate(self.task)
        old_session = self.store.create_session(
            task_id=self.task["task_id"], host_alias="old-host"
        )
        old_session = self.store.transition_session(
            old_session["session_id"], SessionState.ACTIVE
        )
        with self.assertRaisesRegex(TransitionError, "after the first"):
            self.store.create_session(
                task_id=self.task["task_id"],
                host_alias="new-host",
                source_checkpoint_id=None,
            )
        checkpoint = self.store.create_checkpoint(
            task_id=self.task["task_id"], snapshot={"safe": True}
        )
        self.store.transition_session(old_session["session_id"], SessionState.QUIESCING)
        self.store.transition_session(old_session["session_id"], SessionState.CHECKPOINTED)

        # Simulate a CREATED row written by a pre-fix runtime. Activation must
        # independently enforce the recovery contract rather than trusting creation.
        dirty_session_id = "session_legacy_without_checkpoint"
        self.store.connection.execute(
            """INSERT INTO sessions(
                session_id, task_id, host_alias, context_revision, state,
                source_checkpoint_id, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, NULL, ?, ?)""",
            (
                dirty_session_id,
                self.task["task_id"],
                "legacy-host",
                self.task["revision"],
                SessionState.CREATED.value,
                checkpoint["created_at"],
                checkpoint["created_at"],
            ),
        )
        with self.assertRaisesRegex(TransitionError, "after the first"):
            self.store.transition_session(dirty_session_id, SessionState.ACTIVE)

    def test_checkpoint_integrity_is_rechecked_on_read_recovery_and_export(self) -> None:
        self.task = self.activate(self.task)
        old_session = self.store.create_session(
            task_id=self.task["task_id"], host_alias="old-host"
        )
        old_session = self.store.transition_session(
            old_session["session_id"], SessionState.ACTIVE
        )
        checkpoint = self.store.create_checkpoint(
            task_id=self.task["task_id"], snapshot={"generation": 1}
        )
        self.store.transition_session(old_session["session_id"], SessionState.QUIESCING)
        self.store.transition_session(old_session["session_id"], SessionState.CHECKPOINTED)
        recovered = self.store.create_session(
            task_id=self.task["task_id"],
            host_alias="recovery-host",
            source_checkpoint_id=checkpoint["checkpoint_id"],
        )
        self.store.connection.execute(
            "UPDATE checkpoints SET snapshot_json = ? WHERE checkpoint_id = ?",
            ('{"generation":2}', checkpoint["checkpoint_id"]),
        )
        with self.assertRaisesRegex(StorageContractError, "integrity"):
            self.store.get_checkpoint(checkpoint["checkpoint_id"])
        with self.assertRaisesRegex(StorageContractError, "integrity"):
            self.store.transition_session(recovered["session_id"], SessionState.ACTIVE)
        with self.assertRaisesRegex(StorageContractError, "integrity"):
            self.store.export_task(self.task["task_id"])

    def test_checkpoint_superseded_after_session_creation_blocks_activation(self) -> None:
        self.task = self.activate(self.task)
        source = self.store.create_checkpoint(
            task_id=self.task["task_id"], snapshot={"generation": 1}
        )
        recovered = self.store.create_session(
            task_id=self.task["task_id"],
            host_alias="recovery-host",
            source_checkpoint_id=source["checkpoint_id"],
        )
        self.store.create_checkpoint(
            task_id=self.task["task_id"],
            snapshot={"generation": 2},
            supersedes=source["checkpoint_id"],
        )
        with self.assertRaisesRegex(ConflictError, "superseded"):
            self.store.transition_session(recovered["session_id"], SessionState.ACTIVE)

    def test_stale_session_context_cannot_reactivate(self) -> None:
        self.task = self.activate(self.task)
        stale_session = self.store.create_session(
            task_id=self.task["task_id"], host_alias="stale-host"
        )
        blocked = self.store.transition_task(
            self.task["task_id"], TaskState.BLOCKED, expected_revision=self.task["revision"]
        )
        ready = self.store.transition_task(
            self.task["task_id"], TaskState.READY, expected_revision=blocked["revision"]
        )
        self.task = self.store.transition_task(
            self.task["task_id"], TaskState.RUNNING, expected_revision=ready["revision"]
        )
        with self.assertRaisesRegex(ConflictError, "stale"):
            self.store.transition_session(stale_session["session_id"], SessionState.ACTIVE)
        closed = self.store.transition_session(
            stale_session["session_id"], SessionState.CLOSED
        )
        self.assertEqual(closed["state"], SessionState.CLOSED.value)
        review = self.store.transition_task(
            self.task["task_id"],
            TaskState.REVIEW_PENDING,
            expected_revision=self.task["revision"],
        )
        done = self.store.transition_task(
            self.task["task_id"], TaskState.DONE, expected_revision=review["revision"]
        )
        self.assertEqual(done["state"], TaskState.DONE.value)

    def test_stale_active_session_cannot_create_or_start_runs(self) -> None:
        self.task = self.activate(self.task)
        session = self.store.create_session(
            task_id=self.task["task_id"], host_alias="active-stale-host"
        )
        session = self.store.transition_session(session["session_id"], SessionState.ACTIVE)
        planned = self.store.create_run(
            task_id=self.task["task_id"],
            session_id=session["session_id"],
            executor="planned-before-block",
        )
        blocked = self.store.transition_task(
            self.task["task_id"], TaskState.BLOCKED, expected_revision=self.task["revision"]
        )
        ready = self.store.transition_task(
            self.task["task_id"], TaskState.READY, expected_revision=blocked["revision"]
        )
        self.task = self.store.transition_task(
            self.task["task_id"], TaskState.RUNNING, expected_revision=ready["revision"]
        )
        with self.assertRaisesRegex(ConflictError, "stale"):
            self.store.create_run(
                task_id=self.task["task_id"],
                session_id=session["session_id"],
                executor="new-after-resume",
            )
        with self.assertRaisesRegex(ConflictError, "stale"):
            self.store.transition_run(planned["run_id"], RunState.RUNNING)

    def test_stale_or_superseded_checkpoint_is_not_a_recovery_source(self) -> None:
        self.task = self.activate(self.task)
        first = self.store.create_checkpoint(
            task_id=self.task["task_id"], snapshot={"generation": 1}
        )
        latest = self.store.create_checkpoint(
            task_id=self.task["task_id"],
            snapshot={"generation": 2},
            supersedes=first["checkpoint_id"],
        )
        with self.assertRaisesRegex(ConflictError, "superseded"):
            self.store.create_session(
                task_id=self.task["task_id"],
                host_alias="old-checkpoint",
                source_checkpoint_id=first["checkpoint_id"],
            )
        created = self.store.create_session(
            task_id=self.task["task_id"],
            host_alias="latest-checkpoint",
            source_checkpoint_id=latest["checkpoint_id"],
        )
        self.assertEqual(created["source_checkpoint_id"], latest["checkpoint_id"])

        self.store.transition_session(created["session_id"], SessionState.ACTIVE)
        self.store.transition_session(created["session_id"], SessionState.QUIESCING)
        self.store.transition_session(created["session_id"], SessionState.CHECKPOINTED)
        blocked = self.store.transition_task(
            self.task["task_id"], TaskState.BLOCKED, expected_revision=self.task["revision"]
        )
        ready = self.store.transition_task(
            self.task["task_id"], TaskState.READY, expected_revision=blocked["revision"]
        )
        self.task = self.store.transition_task(
            self.task["task_id"], TaskState.RUNNING, expected_revision=ready["revision"]
        )
        with self.assertRaisesRegex(ConflictError, "different task revision"):
            self.store.create_session(
                task_id=self.task["task_id"],
                host_alias="stale-checkpoint",
                source_checkpoint_id=latest["checkpoint_id"],
            )

    def test_initialize_rejects_an_uncontracted_database_without_mutating_it(self) -> None:
        self.store.close()
        database = Path(self.temp.name) / "legacy.db"
        connection = sqlite3.connect(database)
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata(key, value) VALUES('schema_version', '1');
            """
        )
        connection.close()
        if os.name == "posix":
            database.chmod(0o640)
        before_hash = sha256(database.read_bytes()).hexdigest()
        before_mode = database.stat().st_mode & 0o777
        connection = sqlite3.connect(database)
        before_journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
        connection.close()

        with self.assertRaisesRegex(StorageContractError, "schema contract"):
            QingtianStore(database)

        self.assertEqual(sha256(database.read_bytes()).hexdigest(), before_hash)
        self.assertEqual(database.stat().st_mode & 0o777, before_mode)
        connection = sqlite3.connect(database)
        after_journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
        connection.close()
        self.assertEqual(after_journal, before_journal)
        self.assertFalse(Path(f"{database}-wal").exists())
        self.assertFalse(Path(f"{database}-shm").exists())
        # A failed constructor must not retain a connection to the rejected file.
        renamed = database.with_name("legacy-renamed.db")
        database.rename(renamed)
        self.assertTrue(renamed.is_file())

    @unittest.skipUnless(os.name == "posix", "link boundary checks require POSIX")
    def test_database_rejects_direct_symlink_hardlink_and_nonregular_paths(self) -> None:
        target = Path(self.temp.name) / "link-target.db"
        target.write_bytes(b"not-a-database")
        symbolic = Path(self.temp.name) / "symbolic.db"
        symbolic.symlink_to(target)
        with self.assertRaisesRegex(StorageContractError, "symbolic link"):
            QingtianStore(symbolic)

        hardlink = Path(self.temp.name) / "hardlink.db"
        os.link(target, hardlink)
        with self.assertRaisesRegex(StorageContractError, "hard link"):
            QingtianStore(hardlink)
        with self.assertRaisesRegex(StorageContractError, "hard link"):
            QingtianStore(target)

        directory = Path(self.temp.name) / "database-directory"
        directory.mkdir()
        with self.assertRaisesRegex(StorageContractError, "regular file"):
            QingtianStore(directory)

        real_parent = Path(self.temp.name) / "real-parent"
        real_parent.mkdir()
        linked_parent = Path(self.temp.name) / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaisesRegex(StorageContractError, "ancestors"):
            QingtianStore(linked_parent / "new-child" / "state.db")
        self.assertFalse((real_parent / "state.db").exists())
        self.assertFalse((real_parent / "new-child").exists())

    def test_new_database_securely_creates_missing_parent_directories(self) -> None:
        nested = Path(self.temp.name) / "new" / "nested" / "state.db"
        with QingtianStore(nested) as store:
            store.initialize()
        self.assertTrue(nested.is_file())
        if os.name == "posix":
            self.assertEqual(nested.parent.stat().st_mode & 0o777, 0o700)

    @unittest.skipUnless(os.name == "posix", "directory inode race requires POSIX")
    def test_sqlite_connection_is_bound_to_pinned_inode_during_parent_swap(self) -> None:
        race_root = Path(self.temp.name) / "parent-race"
        expected_parent = race_root / "A"
        decoy_parent = race_root / "B"
        held_parent = race_root / "held"
        expected_parent.mkdir(parents=True)
        decoy_parent.mkdir()
        expected_database = expected_parent / "state.db"
        decoy_database = decoy_parent / "state.db"
        for path, project_id in (
            (expected_database, "expected-project"),
            (decoy_database, "decoy-project"),
        ):
            with QingtianStore(path) as store:
                store.initialize()
                store.create_task(
                    project_id=project_id,
                    title="Inode binding fixture",
                    objective="Detect a deterministic parent replacement",
                    scope=["synthetic"],
                    acceptance=["wrong inode is rejected"],
                )
        expected_before = sha256(expected_database.read_bytes()).hexdigest()
        decoy_before = sha256(decoy_database.read_bytes()).hexdigest()
        real_connect = sqlite3.connect
        connect_calls = 0

        def swapped_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
            nonlocal connect_calls
            connect_calls += 1
            if connect_calls == 1:
                return real_connect(*args, **kwargs)
            expected_parent.rename(held_parent)
            decoy_parent.rename(expected_parent)
            try:
                connection = real_connect(*args, **kwargs)
            finally:
                expected_parent.rename(decoy_parent)
                held_parent.rename(expected_parent)
            return connection

        with patch("qingtian_core.store.sqlite3.connect", side_effect=swapped_connect):
            with self.assertRaisesRegex(StorageContractError, "pinned database inode"):
                QingtianStore(expected_database)

        self.assertEqual(connect_calls, 2)
        self.assertEqual(sha256(expected_database.read_bytes()).hexdigest(), expected_before)
        self.assertEqual(sha256(decoy_database.read_bytes()).hexdigest(), decoy_before)

    @unittest.skipUnless(os.name == "posix", "directory inode race requires POSIX")
    def test_new_database_race_rolls_back_only_the_created_inode(self) -> None:
        race_root = Path(self.temp.name) / "new-parent-race"
        expected_parent = race_root / "A"
        decoy_parent = race_root / "B"
        held_parent = race_root / "held"
        expected_parent.mkdir(parents=True)
        decoy_parent.mkdir()
        expected_database = expected_parent / "state.db"
        decoy_database = decoy_parent / "state.db"
        with QingtianStore(decoy_database) as store:
            store.initialize()
            store.create_task(
                project_id="decoy-project",
                title="Decoy fixture",
                objective="Remain unchanged during a failed create",
                scope=["synthetic"],
                acceptance=["content hash is stable"],
            )
        decoy_before = sha256(decoy_database.read_bytes()).hexdigest()
        real_connect = sqlite3.connect

        def swapped_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
            expected_parent.rename(held_parent)
            decoy_parent.rename(expected_parent)
            try:
                connection = real_connect(*args, **kwargs)
            finally:
                expected_parent.rename(decoy_parent)
                held_parent.rename(expected_parent)
            return connection

        with patch("qingtian_core.store.sqlite3.connect", side_effect=swapped_connect):
            with self.assertRaisesRegex(StorageContractError, "pinned database inode"):
                QingtianStore(expected_database)

        self.assertFalse(expected_database.exists())
        self.assertEqual(sha256(decoy_database.read_bytes()).hexdigest(), decoy_before)

    @unittest.skipUnless(os.name == "posix", "inode replacement test requires POSIX")
    def test_failed_create_never_unlinks_a_replacement_inode(self) -> None:
        parent = Path(self.temp.name) / "replacement-race"
        parent.mkdir()
        database = parent / "state.db"
        replacement = parent / "replacement.db"
        replacement.write_bytes(b"replacement-must-survive")

        def replace_then_fail(*_: object, **__: object) -> sqlite3.Connection:
            replacement.replace(database)
            raise sqlite3.OperationalError("synthetic connect failure")

        with patch("qingtian_core.store.sqlite3.connect", side_effect=replace_then_fail):
            with self.assertRaises(sqlite3.OperationalError):
                QingtianStore(database)

        self.assertEqual(database.read_bytes(), b"replacement-must-survive")

    def test_evidence_subject_identity_and_existence_are_enforced(self) -> None:
        with self.assertRaisesRegex(ValueError, "task_id"):
            self.store.add_evidence(
                subject_type="task", subject_id="session_wrong_type", result="observed"
            )
        with self.assertRaisesRegex(NotFoundError, "task not found"):
            self.store.add_evidence(
                subject_type="task", subject_id="task_missing", result="observed"
            )
        for subject_type, subject_id in (
            ("release", "release_example_001"),
            ("artifact", "artifact_example_001"),
        ):
            evidence = self.store.add_evidence(
                subject_type=subject_type,
                subject_id=subject_id,
                result="observed",
            )
            self.assertEqual(evidence["subject_id"], subject_id)
        with self.assertRaisesRegex(ValueError, "release_id"):
            self.store.add_evidence(
                subject_type="release", subject_id="unsafe", result="observed"
            )

        self.task = self.activate(self.task)
        session = self.store.create_session(
            task_id=self.task["task_id"], host_alias="evidence-host"
        )
        run_session = self.store.transition_session(
            session["session_id"], SessionState.ACTIVE
        )
        run = self.store.create_run(
            task_id=self.task["task_id"],
            session_id=run_session["session_id"],
            executor="evidence-test",
        )
        for subject_type, subject_id in (
            ("task", self.task["task_id"]),
            ("session", session["session_id"]),
            ("run", run["run_id"]),
        ):
            evidence = self.store.add_evidence(
                subject_type=subject_type,
                subject_id=subject_id,
                result="passed",
            )
            self.assertEqual(evidence["subject_type"], subject_type)

    def test_caller_supplied_values_cannot_create_schema_invalid_objects(self) -> None:
        with self.assertRaisesRegex(ValueError, "task_id"):
            self.store.create_task(
                task_id="bad id",
                project_id="project-a",
                title="Bad",
                objective="Reject an invalid id",
                scope=["test"],
                acceptance=["rejected"],
            )
        self.task = self.activate(self.task)
        with self.assertRaisesRegex(ValueError, "session_id"):
            self.store.create_session(
                task_id=self.task["task_id"], host_alias="host", session_id="bad id"
            )
        session = self.store.create_session(
            task_id=self.task["task_id"], host_alias="host"
        )
        session = self.store.transition_session(session["session_id"], SessionState.ACTIVE)
        with self.assertRaisesRegex(ValueError, "run_id"):
            self.store.create_run(
                task_id=self.task["task_id"],
                session_id=session["session_id"],
                executor="test",
                run_id="bad id",
            )
        with self.assertRaisesRegex(ValueError, "evidence_id"):
            self.store.add_evidence(
                subject_type="task",
                subject_id=self.task["task_id"],
                result="observed",
                evidence_id="bad id",
            )
        with self.assertRaisesRegex(ValueError, "metadata"):
            self.store.add_evidence(
                subject_type="task",
                subject_id=self.task["task_id"],
                result="observed",
                metadata=[],  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "checkpoint_id"):
            self.store.create_checkpoint(
                task_id=self.task["task_id"], snapshot={}, checkpoint_id="bad id"
            )
        with self.assertRaisesRegex(ValueError, "snapshot"):
            self.store.create_checkpoint(
                task_id=self.task["task_id"], snapshot=[]  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "knowledge_id"):
            self.store.add_knowledge(
                project_id="project-a",
                scope="project",
                classification="public",
                kind="source",
                title="Bad",
                content="Reject invalid id",
                source="synthetic",
                evidence_label="SOURCE",
                knowledge_id="bad id",
            )
        with self.assertRaisesRegex(ValueError, "valid_until"):
            self.store.add_knowledge(
                project_id="project-a",
                scope="project",
                classification="public",
                kind="source",
                title="Bad date",
                content="Reject invalid date",
                source="synthetic",
                evidence_label="SOURCE",
                valid_until="tomorrow",
            )

    def test_checkpoint_export_is_hash_bound(self) -> None:
        evidence = self.store.add_evidence(
            subject_type="task",
            subject_id=self.task["task_id"],
            result="passed",
            metadata={"command": "synthetic"},
        )
        checkpoint = self.store.create_checkpoint(
            task_id=self.task["task_id"],
            snapshot={"evidence_refs": [evidence["evidence_id"]], "next_step": "review"},
        )
        export = self.store.export_task(self.task["task_id"])
        self.assertEqual(export["checkpoints"][0]["content_hash"], checkpoint["content_hash"])
        self.assertEqual(export["evidence"][0]["schema_version"], 1)
        self.assertEqual(len(export["content_hash"]), 64)

    def test_task_export_includes_session_evidence(self) -> None:
        self.task = self.activate(self.task)
        session = self.store.create_session(
            task_id=self.task["task_id"], host_alias="evidence-host"
        )
        session_evidence = self.store.add_evidence(
            subject_type="session",
            subject_id=session["session_id"],
            result="observed",
            metadata={"source": "synthetic"},
        )
        export = self.store.export_task(self.task["task_id"])
        self.assertEqual(
            [item["evidence_id"] for item in export["evidence"]],
            [session_evidence["evidence_id"]],
        )

    def test_knowledge_search_enforces_project_scope_and_classification(self) -> None:
        public = self.store.add_knowledge(
            project_id="project-a",
            scope="project",
            classification="public",
            kind="decision",
            title="Release decision",
            content="Use immutable release receipts.",
            source="synthetic",
            evidence_label="SOURCE",
        )
        self.store.add_knowledge(
            project_id="project-a",
            scope="private-team",
            classification="restricted",
            kind="observation",
            title="Restricted release note",
            content="The private detail must not cross the ACL.",
            source="synthetic",
            evidence_label="OBSERVED",
        )
        self.store.add_knowledge(
            project_id="project-b",
            scope="project",
            classification="public",
            kind="decision",
            title="Other project release",
            content="Must not leak across projects.",
            source="synthetic",
            evidence_label="SOURCE",
        )
        results = self.store.search_knowledge(
            project_id="project-a",
            query="release",
            allowed_scopes=["project"],
            max_classification="public",
        )
        self.assertEqual([item["knowledge_id"] for item in results], [public["knowledge_id"]])
        self.assertEqual(results[0]["schema_version"], 1)
        self.assertEqual(results[0]["review_state"], "unreviewed")
        self.store.forget_knowledge(public["knowledge_id"])
        self.assertEqual(
            self.store.search_knowledge(
                project_id="project-a",
                query="release",
                allowed_scopes=["project"],
                max_classification="public",
            ),
            [],
        )

    def test_fts_index_is_backfilled_when_a_database_is_reopened(self) -> None:
        record = self.store.add_knowledge(
            project_id="project-a",
            scope="project",
            classification="public",
            kind="source",
            title="Portable backfill marker",
            content="Reopening restores derived search state.",
            source="synthetic",
            evidence_label="SOURCE",
        )
        if not self.store.fts_enabled:
            self.skipTest("SQLite build does not include FTS5")
        self.store.connection.execute("DELETE FROM knowledge_fts")
        database = self.store.database
        self.store.close()
        self.store = QingtianStore(database)
        self.store.initialize()
        results = self.store.search_knowledge(
            project_id="project-a",
            query="backfill",
            allowed_scopes=["project"],
            max_classification="public",
        )
        self.assertEqual([item["knowledge_id"] for item in results], [record["knowledge_id"]])


if __name__ == "__main__":
    unittest.main()
