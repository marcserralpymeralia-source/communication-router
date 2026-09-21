from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "development")

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.security import hash_password, verify_password  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import BackgroundJob, Conversation, Customer, Email, EmailSettings, JobAttempt, KnowledgeEntry, Order, OrderLine, ProductEmbedding, TenantSchemaMigration  # noqa: E402
from app.jobs.service import enqueue_job  # noqa: E402
from app.master.database import MasterBase  # noqa: E402
from app.master.migrations import CURRENT_MASTER_SCHEMA_CHECKSUM, CURRENT_MASTER_SCHEMA_NAME, CURRENT_MASTER_SCHEMA_VERSION, master_migration_report, upgrade_master_schema  # noqa: E402
from app.master.models import CompanyMembership, EmailSyncState, MasterCompany, MasterSchemaMigration, MasterTenantDatabase, MasterUser  # noqa: E402
from app.master.provisioning import _ensure_master_user  # noqa: E402
from app.migrations.inspection import discover_sqlite_files, inspect_database_url, inventory_records, simulate_sqlite_reference  # noqa: E402
from app.migrations.helpers import connection_scope, ensure_postgresql_foreign_key, table_exists  # noqa: E402
from app.migrations.registry import CURRENT_TENANT_SCHEMA_CHECKSUM, CURRENT_TENANT_SCHEMA_NAME, CURRENT_TENANT_SCHEMA_VERSION, MASTER_EMAIL_SYNC_STATE_COLUMNS, TENANT_COMPAT_COLUMNS, _apply_master_email_listener_state, _apply_master_email_sync_state_repair, _apply_tenant_email_favorites, _apply_tenant_knowledge_entries, _apply_tenant_order_archiving, _apply_tenant_product_embeddings, _apply_tenant_routing_destinations, _apply_tenant_routing_knowledge_enrichment  # noqa: E402
from app.tenancy.migrations import tenant_migration_report, upgrade_tenant_schema  # noqa: E402
from app.workers.jobs_worker import run_worker_cycle  # noqa: E402
from app.migrations.runner import MigrationSpec, run_migration_plan  # noqa: E402


class SchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.master_path = base / "master.sqlite"
        self.tenant_path = base / "tenant.sqlite"
        self.simulation_root = base / "simulations"
        self.master_engine = create_engine(f"sqlite:///{self.master_path.as_posix()}", connect_args={"check_same_thread": False})
        self.tenant_engine = create_engine(f"sqlite:///{self.tenant_path.as_posix()}", connect_args={"check_same_thread": False})
        self.atomic_engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False, "autocommit": False},
        )
        self.MasterSession = sessionmaker(bind=self.master_engine, autoflush=False, autocommit=False)
        self.TenantSession = sessionmaker(bind=self.tenant_engine, autoflush=False, autocommit=False)
        self.AtomicSession = sessionmaker(bind=self.atomic_engine, autoflush=False, autocommit=False)

    def tearDown(self):
        self.master_engine.dispose()
        self.tenant_engine.dispose()
        self.atomic_engine.dispose()
        self.tempdir.cleanup()

    def _create_tables_without_ledger(self, engine, base_metadata):  # noqa: ANN001
        tables = [table for name, table in base_metadata.tables.items() if name != "schema_migrations"]
        base_metadata.create_all(engine, tables=tables)

    def _seed_master_catalog(self, tenant_url: str) -> None:
        self._create_tables_without_ledger(self.master_engine, MasterBase.metadata)
        db = self.MasterSession()
        db.add_all(
            [
                MasterCompany(id=1, name="Demo", slug="demo", active=True),
                MasterUser(id=1, email="admin@anchi.local", full_name="Admin Demo", password_hash=hash_password("admin123"), is_active=True),
                CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=True),
                MasterTenantDatabase(company_id=1, database_key="demo", database_url=tenant_url, is_active=True, health_status="ok"),
            ]
        )
        db.commit()
        db.close()

    def test_existing_master_user_keeps_password_during_provisioning(self) -> None:
        MasterBase.metadata.create_all(self.master_engine)
        db = self.MasterSession()
        try:
            user = _ensure_master_user(
                db,
                "admin@example.com",
                "Administrador Inicial",
                "initial-password",
            )
            db.commit()
            original_hash = user.password_hash

            same_user = _ensure_master_user(
                db,
                "admin@example.com",
                "Administrador Actualizado",
                "different-password",
            )
            db.commit()

            self.assertEqual(same_user.id, user.id)
            self.assertEqual(same_user.full_name, "Administrador Actualizado")
            self.assertEqual(same_user.password_hash, original_hash)
            self.assertTrue(verify_password("initial-password", same_user.password_hash))
            self.assertFalse(verify_password("different-password", same_user.password_hash))
        finally:
            db.close()

    def _seed_current_tenant_without_ledger(self, *, with_jobs: bool = False) -> None:
        self._create_tables_without_ledger(self.tenant_engine, Base.metadata)
        db = self.TenantSession()
        db.add(Customer(company_id=1, code="C001", fiscal_name="Cliente Uno", primary_email="uno@example.com"))
        if with_jobs:
            job = BackgroundJob(company_id=1, job_type="process_email", dedupe_key="dedupe-1", status="queued", payload_json='{"email_id": 1}', progress=0)
            db.add(job)
            db.flush()
            db.add(JobAttempt(company_id=1, job_id=job.id, attempt_number=1, worker_id="worker-a", status="running"))
        db.commit()
        db.close()

    def _seed_legacy_tenant_schema(self) -> None:
        with self.tenant_engine.begin() as conn:
            conn.execute(text("CREATE TABLE customers (id INTEGER PRIMARY KEY, company_id INTEGER, code VARCHAR(50), fiscal_name VARCHAR(255))"))
            conn.execute(text("CREATE TABLE background_jobs (id INTEGER PRIMARY KEY, company_id INTEGER, job_type VARCHAR(80), dedupe_key VARCHAR(255), status VARCHAR(40), payload_json TEXT)"))
            conn.execute(text("INSERT INTO customers (id, company_id, code, fiscal_name) VALUES (1, 1, 'C001', 'Legacy Client')"))
            conn.execute(text("INSERT INTO background_jobs (id, company_id, job_type, dedupe_key, status, payload_json) VALUES (1, 1, 'process_email', 'dup-key', 'queued', '{}')"))

    def _seed_unknown_schema(self) -> None:
        with self.tenant_engine.begin() as conn:
            conn.execute(text("CREATE TABLE misc (id INTEGER PRIMARY KEY, label TEXT)"))
            conn.execute(text("INSERT INTO misc (id, label) VALUES (1, 'orphan')"))

    def _seed_legacy_routing_enrichment_schema(self, *, related_column: bool = False) -> None:
        related_sql = ", related_department_id INTEGER" if related_column else ""
        with self.tenant_engine.begin() as conn:
            conn.execute(text("CREATE TABLE departments (id INTEGER PRIMARY KEY, company_id INTEGER, name VARCHAR(150))"))
            conn.execute(
                text(
                    "CREATE TABLE department_knowledge ("
                    "id INTEGER PRIMARY KEY, department_id INTEGER, title VARCHAR(200), content TEXT, "
                    f"knowledge_type VARCHAR(30){related_sql}, active BOOLEAN)"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE routing_decisions ("
                    "id INTEGER PRIMARY KEY, company_id INTEGER, communication_id INTEGER, department_id INTEGER, "
                    "category VARCHAR(100), confidence FLOAT, reason TEXT)"
                )
            )
            conn.execute(text("INSERT INTO departments (id, company_id, name) VALUES (1, 7, 'Comercial'), (2, 7, 'Logística')"))
            related_value = ", 2" if related_column else ""
            conn.execute(
                text(
                    "INSERT INTO department_knowledge "
                    "(id, department_id, title, content, knowledge_type, active"
                    f"{', related_department_id' if related_column else ''}) "
                    f"VALUES (1, 1, 'Presupuestos', 'Precios y condiciones', 'responsibility', 1{related_value})"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO routing_decisions "
                    "(id, company_id, communication_id, department_id, category, confidence, reason) "
                    "VALUES (1, 7, 10, 1, 'commercial', 0.8, 'legacy')"
                )
            )

    def test_routing_enrichment_adds_legacy_indexes_and_preserves_data(self):
        self._seed_legacy_routing_enrichment_schema(related_column=True)

        actions = _apply_tenant_routing_knowledge_enrichment(self.tenant_engine, dry_run=False)

        indexes = {
            index["name"]: tuple(index["column_names"])
            for index in inspect(self.tenant_engine).get_indexes("department_knowledge")
        }
        indexes.update(
            {
                index["name"]: tuple(index["column_names"])
                for index in inspect(self.tenant_engine).get_indexes("routing_decisions")
            }
        )
        self.assertEqual(indexes["ix_department_knowledge_related_department_id"], ("related_department_id",))
        self.assertEqual(indexes["ix_department_knowledge_priority"], ("priority",))
        self.assertEqual(indexes["ix_routing_decisions_runner_up_department_id"], ("runner_up_department_id",))
        self.assertIn("CREATE INDEX", "\n".join(actions))

        with self.tenant_engine.connect() as conn:
            row = conn.execute(
                text("SELECT department_id, related_department_id, priority FROM department_knowledge WHERE id = 1")
            ).one()
            decision = conn.execute(text("SELECT category FROM routing_decisions WHERE id = 1")).scalar_one()
        self.assertEqual(tuple(row), (1, 2, "NORMAL"))
        self.assertEqual(decision, "commercial")

    def test_routing_enrichment_is_idempotent_and_does_not_add_undeclared_sqlite_constraints(self):
        self._seed_legacy_routing_enrichment_schema()

        first_actions = _apply_tenant_routing_knowledge_enrichment(self.tenant_engine, dry_run=False)
        second_actions = _apply_tenant_routing_knowledge_enrichment(self.tenant_engine, dry_run=False)

        self.assertTrue(first_actions)
        self.assertEqual(second_actions, [])
        self.assertEqual(inspect(self.tenant_engine).get_check_constraints("department_knowledge"), [])
        self.assertEqual(inspect(self.tenant_engine).get_foreign_keys("department_knowledge"), [])
        with self.tenant_engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT COUNT(*) FROM department_knowledge")).scalar_one(), 1)

    def test_sqlite_legacy_migration_uses_additive_columns_and_keeps_priority_default(self):
        self._seed_legacy_routing_enrichment_schema()

        _apply_tenant_routing_knowledge_enrichment(self.tenant_engine, dry_run=False)

        with self.tenant_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO department_knowledge "
                    "(id, department_id, title, content, knowledge_type, active) "
                    "VALUES (2, 2, 'Entregas', 'Incidencias', 'responsibility', 1)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO department_knowledge "
                    "(id, department_id, title, content, knowledge_type, active, priority) "
                    "VALUES (3, 2, 'Nota opcional', 'Sin prioridad', 'guideline', 1, NULL)"
                )
            )
        with self.tenant_engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT priority FROM department_knowledge WHERE id = 2")).scalar_one(), "NORMAL")
            self.assertIsNone(conn.execute(text("SELECT priority FROM department_knowledge WHERE id = 3")).scalar_one())
            self.assertEqual(conn.execute(text("SELECT COUNT(*) FROM department_knowledge")).scalar_one(), 3)

    def test_routing_destinations_migration_creates_fresh_table_and_legacy_columns(self):
        with self.tenant_engine.begin() as conn:
            conn.execute(text("CREATE TABLE routing_evaluation_cases (id INTEGER PRIMARY KEY, title TEXT)"))
            conn.execute(text("CREATE TABLE routing_evaluation_results (id INTEGER PRIMARY KEY, status TEXT)"))

        first_actions = _apply_tenant_routing_destinations(self.tenant_engine, dry_run=False)
        second_actions = _apply_tenant_routing_destinations(self.tenant_engine, dry_run=False)

        self.assertTrue(any("routing_decision_destinations" in action for action in first_actions))
        self.assertEqual(second_actions, [])
        tables = set(inspect(self.tenant_engine).get_table_names())
        self.assertIn("routing_decision_destinations", tables)
        columns = {
            column["name"]
            for column in inspect(self.tenant_engine).get_columns("routing_evaluation_cases")
        }
        self.assertTrue({"expected_destinations_json", "expected_raci_json"}.issubset(columns))
        result_columns = {
            column["name"]
            for column in inspect(self.tenant_engine).get_columns("routing_evaluation_results")
        }
        self.assertTrue(
            {
                "expected_destinations_json",
                "expected_raci_json",
                "predicted_destinations_json",
                "additional_destinations_correct",
                "raci_match",
            }.issubset(result_columns)
        )
        self.assertEqual(len(inspect(self.tenant_engine).get_check_constraints("routing_decision_destinations")), 2)
        self.assertEqual(len(inspect(self.tenant_engine).get_foreign_keys("routing_decision_destinations")), 3)

    def test_postgresql_foreign_key_ddl_and_orphan_guard_are_safe(self):
        class _Result:
            def __init__(self, value):
                self.value = value

            def scalar_one(self):
                return self.value

        class _Connection:
            def __init__(self, engine):
                self.engine = engine

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, statement):
                sql = str(statement)
                self.engine.executed.append(sql)
                if sql.startswith("SELECT COUNT(*)"):
                    return _Result(self.engine.orphan_count)
                return _Result(None)

        class _Engine:
            dialect = type("Dialect", (), {"name": "postgresql"})()

            def __init__(self, orphan_count):
                self.orphan_count = orphan_count
                self.executed = []

            def connect(self):
                return _Connection(self)

            def begin(self):
                return _Connection(self)

        inspector = SimpleNamespace(
            get_table_names=lambda: ["departments", "department_knowledge"],
            get_columns=lambda _table: [{"name": "related_department_id"}],
            get_foreign_keys=lambda _table: [],
        )
        engine = _Engine(orphan_count=0)
        with patch("app.migrations.helpers.inspect", return_value=inspector):
            actions = ensure_postgresql_foreign_key(
                engine,
                "department_knowledge",
                ("related_department_id",),
                "departments",
                ("id",),
                "fk_department_knowledge_related_department",
                ondelete="RESTRICT",
            )
        self.assertEqual(len(actions), 1)
        self.assertIn("ON DELETE RESTRICT", actions[0])
        self.assertIn("ALTER TABLE department_knowledge ADD CONSTRAINT", engine.executed[-1])

        orphan_engine = _Engine(orphan_count=1)
        with patch("app.migrations.helpers.inspect", return_value=inspector):
            with self.assertRaisesRegex(RuntimeError, r"valor\(es\) huérfano\(s\)"):
                ensure_postgresql_foreign_key(
                    orphan_engine,
                    "department_knowledge",
                    ("related_department_id",),
                    "departments",
                    ("id",),
                    "fk_department_knowledge_related_department",
                    ondelete="RESTRICT",
                )
        self.assertEqual(len([sql for sql in orphan_engine.executed if sql.startswith("ALTER TABLE")]), 0)

    def _seed_legacy_messages_schema(self) -> None:
        with self.tenant_engine.begin() as conn:
            conn.execute(text("CREATE TABLE input_channels (id INTEGER PRIMARY KEY, company_id INTEGER, key VARCHAR(80), name VARCHAR(150), channel_type VARCHAR(50), is_active BOOLEAN, is_default BOOLEAN, supports_text BOOLEAN, supports_attachments BOOLEAN, supports_documents BOOLEAN)"))
            conn.execute(text("CREATE TABLE emails (id INTEGER PRIMARY KEY, company_id INTEGER, external_id VARCHAR(255), sender VARCHAR(255), subject VARCHAR(500), body TEXT, extracted_text TEXT, received_at DATETIME)"))
            conn.execute(text("CREATE TABLE inbound_messages (id INTEGER PRIMARY KEY, company_id INTEGER, channel_id INTEGER, source_external_id VARCHAR(255), source_thread_id VARCHAR(255), provider VARCHAR(50), sender VARCHAR(255), subject VARCHAR(500), original_content TEXT, received_at DATETIME, customer_id INTEGER, order_id INTEGER, status VARCHAR(80), processing_step VARCHAR(80))"))
            conn.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY, company_id INTEGER, email_id INTEGER, customer_detected_name VARCHAR(255), status VARCHAR(80), score FLOAT, created_at DATETIME)"))
            conn.execute(text("INSERT INTO input_channels (id, company_id, key, name, channel_type, is_active, is_default, supports_text, supports_attachments, supports_documents) VALUES (1, 1, 'email', 'Email', 'message', 1, 1, 1, 1, 1)"))
            conn.execute(text("INSERT INTO emails (id, company_id, external_id, sender, subject, body, extracted_text, received_at) VALUES (1, 1, 'mail-legacy', 'cliente@example.com', 'Pedido legado', '10 cajas', '10 cajas', CURRENT_TIMESTAMP)"))
            conn.execute(text("INSERT INTO inbound_messages (id, company_id, channel_id, source_external_id, source_thread_id, provider, sender, subject, original_content, received_at, customer_id, order_id, status, processing_step) VALUES (1, 1, 1, 'mail-legacy', 'thread-legacy', 'imap', 'cliente@example.com', 'Pedido legado', '10 cajas', CURRENT_TIMESTAMP, NULL, NULL, 'received', 'received')"))
            conn.execute(text("INSERT INTO orders (id, company_id, email_id, customer_detected_name, status, score, created_at) VALUES (1, 1, 1, 'Cliente legado', 'pedido_pendiente_revision', 88, CURRENT_TIMESTAMP)"))

    def _seed_checksum_mismatch(self) -> None:
        self._create_tables_without_ledger(self.tenant_engine, Base.metadata)
        with self.tenant_engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE schema_migrations (
                        id INTEGER PRIMARY KEY,
                        company_id INTEGER UNIQUE,
                        version VARCHAR(80),
                        name VARCHAR(180),
                        checksum VARCHAR(120),
                        execution_ms INTEGER DEFAULT 0,
                        application_version VARCHAR(80),
                        status VARCHAR(30),
                        applied_at DATETIME,
                        last_checked_at DATETIME,
                        last_error TEXT,
                        notes TEXT,
                        created_at DATETIME,
                        updated_at DATETIME
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO schema_migrations
                        (id, company_id, version, name, checksum, execution_ms, application_version, status, applied_at, last_checked_at, last_error, notes, created_at, updated_at)
                    VALUES
                        (1, 1, :version, :name, :checksum, 0, '1.2.3', 'current', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """
                ),
                {
                    "version": CURRENT_TENANT_SCHEMA_VERSION,
                    "name": CURRENT_TENANT_SCHEMA_NAME,
                    "checksum": "wrong-checksum",
                },
            )

    def test_inventory_detects_master_and_tenant_sources(self):
        self._seed_current_tenant_without_ledger()
        self._seed_master_catalog(f"sqlite:///{self.tenant_path.as_posix()}")

        orphan_path = Path(self.tempdir.name) / "orphan.sqlite"
        with create_engine(
            f"sqlite:///{orphan_path.as_posix()}",
            connect_args={"check_same_thread": False},
        ).begin() as conn:
            conn.execute(text("CREATE TABLE misc (id INTEGER PRIMARY KEY)"))

        db = self.MasterSession()
        try:
            items = inventory_records(db, Path(self.tempdir.name))
        finally:
            db.close()

        logical_names = {item["logical_name"] for item in items}
        self.assertIn("master", logical_names)
        self.assertIn("tenant:demo", logical_names)
        self.assertTrue(any(item["source"] == "filesystem" for item in items))

    def test_inspection_classifies_current_legacy_unknown_and_checksum_mismatch(self):
        self._seed_current_tenant_without_ledger()
        current = inspect_database_url(f"sqlite:///{self.tenant_path.as_posix()}", logical_name="tenant-current")
        self.assertEqual(current["classification"], "current-without-ledger")
        self.assertTrue(current["baseline_safe"])

        self.tenant_engine.dispose()
        self.tenant_path.unlink()
        self.tenant_engine = create_engine(f"sqlite:///{self.tenant_path.as_posix()}", connect_args={"check_same_thread": False})
        self.TenantSession = sessionmaker(bind=self.tenant_engine, autoflush=False, autocommit=False)
        self._seed_legacy_tenant_schema()
        legacy = inspect_database_url(f"sqlite:///{self.tenant_path.as_posix()}", logical_name="tenant-legacy")
        self.assertEqual(legacy["classification"], "legacy-recognized")
        self.assertTrue(legacy["baseline_safe"])

        self.tenant_engine.dispose()
        self.tenant_path.unlink()
        self.tenant_engine = create_engine(f"sqlite:///{self.tenant_path.as_posix()}", connect_args={"check_same_thread": False})
        self.TenantSession = sessionmaker(bind=self.tenant_engine, autoflush=False, autocommit=False)
        self._seed_unknown_schema()
        unknown = inspect_database_url(f"sqlite:///{self.tenant_path.as_posix()}", logical_name="tenant-unknown")
        self.assertEqual(unknown["classification"], "unknown-schema")
        self.assertFalse(unknown["baseline_safe"])

        self.tenant_engine.dispose()
        self.tenant_path.unlink()
        self.tenant_engine = create_engine(f"sqlite:///{self.tenant_path.as_posix()}", connect_args={"check_same_thread": False})
        self.TenantSession = sessionmaker(bind=self.tenant_engine, autoflush=False, autocommit=False)
        self._seed_checksum_mismatch()
        mismatch = inspect_database_url(f"sqlite:///{self.tenant_path.as_posix()}", logical_name="tenant-mismatch")
        self.assertEqual(mismatch["classification"], "checksum-mismatch")
        self.assertFalse(mismatch["readiness"] == "ready")

    def test_dry_run_does_not_write_tenant_copy(self):
        self._seed_current_tenant_without_ledger(with_jobs=True)
        self.assertTrue(self.tenant_path.exists())
        before_tables = set(inspect_database_url(f"sqlite:///{self.tenant_path.as_posix()}")["tables"])
        before_jobs = self._count_table("background_jobs")
        before_attempts = self._count_table("job_attempts")

        result = upgrade_tenant_schema(self.tenant_engine, company_id=1, application_version="1.2.3", dry_run=True)

        self.assertTrue(result["dry_run"])
        after_tables = set(inspect_database_url(f"sqlite:///{self.tenant_path.as_posix()}")["tables"])
        self.assertEqual(before_tables, after_tables)
        self.assertEqual(before_jobs, self._count_table("background_jobs"))
        self.assertEqual(before_attempts, self._count_table("job_attempts"))
        self.assertFalse(table_exists(self.tenant_engine, "schema_migrations"))

    def test_upgrade_copy_preserves_counts_and_second_run_is_noop(self):
        self._seed_current_tenant_without_ledger(with_jobs=True)
        before_counts = self._snapshot_counts(self.tenant_engine, ["customers", "background_jobs", "job_attempts"])

        first = upgrade_tenant_schema(self.tenant_engine, company_id=1, application_version="1.2.3")
        self.assertEqual(first["current_version"], CURRENT_TENANT_SCHEMA_VERSION)
        self.assertTrue(first["is_current"])

        after_first = self._snapshot_counts(self.tenant_engine, ["customers", "background_jobs", "job_attempts"])
        self.assertEqual(before_counts, after_first)

        second = upgrade_tenant_schema(self.tenant_engine, company_id=1, application_version="1.2.3")
        self.assertTrue(second["is_current"])
        self.assertEqual(second["applied_versions"], [])
        self.assertEqual(after_first, self._snapshot_counts(self.tenant_engine, ["customers", "background_jobs", "job_attempts"]))

        db = self.TenantSession()
        try:
            report = tenant_migration_report(db, 1)
        finally:
            db.close()
        self.assertTrue(report["is_current"])
        self.assertEqual(report["version"], CURRENT_TENANT_SCHEMA_VERSION)

    def _seed_transactional_ledger(self, version: str = "2026.09.08.3") -> None:
        TenantSchemaMigration.__table__.create(bind=self.atomic_engine, checkfirst=True)
        db = self.AtomicSession()
        db.add(
            TenantSchemaMigration(
                company_id=1,
                version=version,
                name=f"tenant migration {version}",
                checksum="previous-checksum",
                status="current",
            )
        )
        db.commit()
        db.close()

    @staticmethod
    def _transactional_specs(*, fail_17: bool = False, fail_18: bool = False) -> list[MigrationSpec]:
        def baseline(_bind, _dry_run):  # noqa: ANN001
            return []

        def migration_17(bind, dry_run):  # noqa: ANN001
            if dry_run:
                return ["CREATE TABLE atomic_migration_17"]
            with connection_scope(bind) as conn:
                conn.execute(text("CREATE TABLE atomic_migration_17 (id INTEGER PRIMARY KEY)"))
                conn.execute(text("CREATE INDEX atomic_migration_17_idx ON atomic_migration_17 (id)"))
            if fail_17:
                raise RuntimeError("failure in 2026.09.17.1")
            return ["CREATE TABLE atomic_migration_17"]

        def migration_18(bind, dry_run):  # noqa: ANN001
            if dry_run:
                return ["CREATE TABLE atomic_migration_18"]
            with connection_scope(bind) as conn:
                conn.execute(text("CREATE TABLE atomic_migration_18 (id INTEGER PRIMARY KEY)"))
                conn.execute(text("CREATE INDEX atomic_migration_18_idx ON atomic_migration_18 (id)"))
            if fail_18:
                raise RuntimeError("failure in 2026.09.18.1")
            return ["CREATE TABLE atomic_migration_18"]

        return [
            MigrationSpec("2026.09.08.3", "tenant worker heartbeats", "baseline", baseline),
            MigrationSpec("2026.09.17.1", "tenant routing knowledge enrichment", "17", migration_17),
            MigrationSpec("2026.09.18.1", "tenant normalized routing destinations", "18", migration_18),
        ]

    def _transactional_ledger_version(self) -> str:
        db = self.AtomicSession()
        try:
            return db.scalar(select(TenantSchemaMigration.version).order_by(TenantSchemaMigration.id.desc()))
        finally:
            db.close()

    def test_each_migration_commits_schema_and_ledger_together(self):
        self._seed_transactional_ledger()

        result = run_migration_plan(
            self.atomic_engine,
            self.AtomicSession(),
            TenantSchemaMigration,
            self._transactional_specs(),
            company_id=1,
        )

        self.assertEqual(result["applied_versions"], ["2026.09.17.1", "2026.09.18.1"])
        self.assertEqual(self._transactional_ledger_version(), "2026.09.18.1")
        self.assertTrue(table_exists(self.atomic_engine, "atomic_migration_17"))
        self.assertTrue(table_exists(self.atomic_engine, "atomic_migration_18"))

    def test_failed_17_rolls_back_all_17_schema_and_keeps_previous_ledger(self):
        self._seed_transactional_ledger()

        with self.assertRaisesRegex(RuntimeError, "2026.09.17.1"):
            run_migration_plan(
                self.atomic_engine,
                self.AtomicSession(),
                TenantSchemaMigration,
                self._transactional_specs(fail_17=True),
                company_id=1,
            )

        self.assertFalse(table_exists(self.atomic_engine, "atomic_migration_17"))
        self.assertEqual(self._transactional_ledger_version(), "2026.09.08.3")

    def test_failed_18_keeps_committed_17_and_does_not_leave_partial_18(self):
        self._seed_transactional_ledger()

        with self.assertRaisesRegex(RuntimeError, "2026.09.18.1"):
            run_migration_plan(
                self.atomic_engine,
                self.AtomicSession(),
                TenantSchemaMigration,
                self._transactional_specs(fail_18=True),
                company_id=1,
            )

        self.assertTrue(table_exists(self.atomic_engine, "atomic_migration_17"))
        self.assertFalse(table_exists(self.atomic_engine, "atomic_migration_18"))
        self.assertEqual(self._transactional_ledger_version(), "2026.09.17.1")

    def test_retry_after_failed_migration_continues_from_last_committed_version(self):
        self._seed_transactional_ledger()

        with self.assertRaises(RuntimeError):
            run_migration_plan(
                self.atomic_engine,
                self.AtomicSession(),
                TenantSchemaMigration,
                self._transactional_specs(fail_18=True),
                company_id=1,
            )

        result = run_migration_plan(
            self.atomic_engine,
            self.AtomicSession(),
            TenantSchemaMigration,
            self._transactional_specs(),
            company_id=1,
        )

        self.assertEqual(result["applied_versions"], ["2026.09.18.1"])
        self.assertEqual(self._transactional_ledger_version(), "2026.09.18.1")
        with self.atomic_engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT COUNT(*) FROM atomic_migration_17")).scalar_one(), 0)
            self.assertEqual(conn.execute(text("SELECT COUNT(*) FROM atomic_migration_18")).scalar_one(), 0)

    def test_upgrade_master_copy_preserves_state(self):
        self._create_tables_without_ledger(self.master_engine, MasterBase.metadata)
        db = self.MasterSession()
        db.add_all(
            [
                MasterCompany(id=1, name="Demo", slug="demo", active=True),
                MasterUser(id=1, email="admin@anchi.local", full_name="Admin Demo", password_hash=hash_password("admin123"), is_active=True),
                CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=True),
                MasterTenantDatabase(company_id=1, database_key="demo", database_url=f"sqlite:///{self.tenant_path.as_posix()}", is_active=True, health_status="ok"),
            ]
        )
        db.commit()
        db.close()

        before_users = self._count_master("users")
        before_companies = self._count_master("companies")
        result = upgrade_master_schema(self.master_engine, application_version="1.2.3")
        self.assertTrue(result["is_current"])
        self.assertEqual(before_users, self._count_master("users"))
        self.assertEqual(before_companies, self._count_master("companies"))
        inspection = inspect_database_url(f"sqlite:///{self.master_path.as_posix()}", logical_name="master-copy")
        self.assertEqual(inspection["classification"], "versioned-current")
        self.assertTrue(inspection["baseline_safe"])

    def test_tenant_compat_boolean_defaults_are_postgresql_safe(self):
        unsafe = []
        for table_name, columns in TENANT_COMPAT_COLUMNS.items():
            for column_name, definition in columns.items():
                if "BOOLEAN DEFAULT 0" in definition or "BOOLEAN DEFAULT 1" in definition:
                    unsafe.append(f"{table_name}.{column_name}={definition}")
        self.assertEqual(unsafe, [])

    def test_email_settings_registry_columns_are_present_in_model(self):
        model_columns = set(EmailSettings.__table__.columns.keys())
        registry_columns = set(TENANT_COMPAT_COLUMNS["email_settings"].keys())
        self.assertIn("smtp_enabled", registry_columns)
        self.assertIn("initial_history_mode", registry_columns)
        self.assertIn("initial_history_limit", registry_columns)
        self.assertTrue(registry_columns.issubset(model_columns))

    def test_product_embeddings_migration_creates_model_columns(self):
        self._create_tables_without_ledger(self.tenant_engine, Base.metadata)
        ProductEmbedding.__table__.drop(bind=self.tenant_engine)

        actions = _apply_tenant_product_embeddings(self.tenant_engine, dry_run=False)

        self.assertIn("CREATE TABLE product_embeddings (...)", actions)
        model_columns = set(ProductEmbedding.__table__.columns.keys())
        db_columns = {column["name"] for column in inspect(self.tenant_engine).get_columns("product_embeddings")}
        self.assertEqual(model_columns, db_columns)

    def test_knowledge_entries_migration_creates_model_columns(self):
        self._create_tables_without_ledger(self.tenant_engine, Base.metadata)
        KnowledgeEntry.__table__.drop(bind=self.tenant_engine)

        actions = _apply_tenant_knowledge_entries(self.tenant_engine, dry_run=False)

        self.assertIn("CREATE TABLE knowledge_entries (...)", actions)
        model_columns = set(KnowledgeEntry.__table__.columns.keys())
        db_columns = {column["name"] for column in inspect(self.tenant_engine).get_columns("knowledge_entries")}
        self.assertEqual(model_columns, db_columns)

    def test_order_archiving_migration_adds_column_to_existing_orders(self):
        with self.tenant_engine.begin() as conn:
            conn.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY, company_id INTEGER)"))

        actions = _apply_tenant_order_archiving(self.tenant_engine, dry_run=False)

        self.assertEqual(actions, ["ALTER TABLE orders ADD COLUMN archived BOOLEAN DEFAULT false"])
        columns = {column["name"] for column in inspect(self.tenant_engine).get_columns("orders")}
        self.assertIn("archived", columns)

    def test_email_favorites_migration_adds_column_to_existing_emails(self):
        with self.tenant_engine.begin() as conn:
            conn.execute(text("CREATE TABLE emails (id INTEGER PRIMARY KEY, company_id INTEGER)"))

        actions = _apply_tenant_email_favorites(self.tenant_engine, dry_run=False)

        self.assertEqual(actions, ["ALTER TABLE emails ADD COLUMN is_favorite BOOLEAN DEFAULT false"])
        columns = {column["name"] for column in inspect(self.tenant_engine).get_columns("emails")}
        self.assertIn("is_favorite", columns)

    def test_master_email_sync_state_registry_contains_listener_columns(self):
        with self.master_engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE email_sync_state (
                        id INTEGER PRIMARY KEY,
                        company_id INTEGER,
                        channel_key VARCHAR(80),
                        enabled BOOLEAN DEFAULT true,
                        frequency_seconds INTEGER DEFAULT 60,
                        status VARCHAR(50) DEFAULT 'idle'
                    )
                    """
                )
            )
        actions = _apply_master_email_listener_state(self.master_engine, dry_run=False)
        self.assertTrue(actions)
        columns = {column["name"] for column in inspect(self.master_engine).get_columns("email_sync_state")}
        for column in (
            "listener_status",
            "listener_owner",
            "listener_last_started_at",
            "listener_last_heartbeat_at",
            "listener_last_error_at",
            "listener_last_error_message",
        ):
            self.assertIn(column, columns)

    def test_master_email_sync_repair_fixes_partial_checkpoint_schema(self):
        MasterSchemaMigration.__table__.create(bind=self.master_engine, checkfirst=True)
        with self.master_engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE email_sync_state (
                        id INTEGER PRIMARY KEY,
                        company_id INTEGER,
                        channel_key VARCHAR(80),
                        enabled BOOLEAN DEFAULT true,
                        frequency_seconds INTEGER DEFAULT 60,
                        status VARCHAR(50) DEFAULT 'idle'
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO schema_migrations
                    (version, name, checksum, execution_ms, status, applied_at, last_checked_at, created_at, updated_at)
                    VALUES
                    ('2026.07.16.1', 'master email sync checkpoints', 'legacy-checksum', 0, 'current', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """
                )
            )

        result = upgrade_master_schema(self.master_engine, application_version="test-repair")

        columns = {column["name"] for column in inspect(self.master_engine).get_columns("email_sync_state")}
        self.assertTrue(set(MASTER_EMAIL_SYNC_STATE_COLUMNS).issubset(columns))
        self.assertIn("2026.08.19.2", result["applied_versions"])
        self.assertTrue(result["is_current"])

    def test_master_email_sync_repair_is_safe_to_run_twice(self):
        with self.master_engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE email_sync_state (
                        id INTEGER PRIMARY KEY,
                        company_id INTEGER,
                        channel_key VARCHAR(80),
                        enabled BOOLEAN DEFAULT true,
                        frequency_seconds INTEGER DEFAULT 60,
                        status VARCHAR(50) DEFAULT 'idle'
                    )
                    """
                )
            )

        first_actions = _apply_master_email_sync_state_repair(self.master_engine, dry_run=False)
        second_actions = _apply_master_email_sync_state_repair(self.master_engine, dry_run=False)

        self.assertTrue(first_actions)
        self.assertEqual(second_actions, [])

    def test_master_email_sync_repair_noops_when_schema_is_current(self):
        MasterBase.metadata.create_all(self.master_engine)

        actions = _apply_master_email_sync_state_repair(self.master_engine, dry_run=False)

        self.assertEqual(actions, [])
        columns = {column["name"] for column in inspect(self.master_engine).get_columns("email_sync_state")}
        self.assertTrue(set(EmailSyncState.__table__.columns.keys()).issubset(columns))

    def test_upgrade_tenant_repairs_missing_email_settings_columns_on_current_schema(self):
        with self.tenant_engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE email_settings (
                        id INTEGER PRIMARY KEY,
                        company_id INTEGER UNIQUE,
                        provider VARCHAR(50) DEFAULT 'imap',
                        connection_method VARCHAR(50) DEFAULT 'password',
                        imap_host VARCHAR(255),
                        imap_port INTEGER DEFAULT 993,
                        imap_use_ssl BOOLEAN DEFAULT true,
                        imap_security VARCHAR(30) DEFAULT 'ssl_tls',
                        imap_username VARCHAR(255),
                        imap_password_encrypted TEXT,
                        test_read_limit INTEGER DEFAULT 10,
                        oauth_scopes TEXT,
                        mailbox VARCHAR(255),
                        inbox_folder VARCHAR(100) DEFAULT 'INBOX',
                        processed_folder VARCHAR(100),
                        error_folder VARCHAR(100),
                        no_order_folder VARCHAR(100),
                        doubtful_folder VARCHAR(100),
                        read_limit INTEGER DEFAULT 25,
                        auto_sync_enabled BOOLEAN DEFAULT false,
                        read_unread_only BOOLEAN DEFAULT true,
                        read_from_date VARCHAR(50),
                        initial_history_mode VARCHAR(30) DEFAULT 'new',
                        initial_history_limit INTEGER DEFAULT 50,
                        mark_as_read_after_import BOOLEAN DEFAULT false,
                        move_after_processing BOOLEAN DEFAULT false,
                        post_process_action VARCHAR(50) DEFAULT 'mark_read',
                        polling_frequency_minutes INTEGER DEFAULT 1,
                        smtp_provider VARCHAR(50) DEFAULT 'smtp',
                        smtp_host VARCHAR(255),
                        smtp_port INTEGER DEFAULT 587,
                        smtp_security VARCHAR(30) DEFAULT 'starttls',
                        smtp_username VARCHAR(255),
                        smtp_password_encrypted TEXT,
                        from_email VARCHAR(255),
                        from_name VARCHAR(255),
                        reply_to VARCHAR(255),
                        default_cc TEXT,
                        default_bcc TEXT,
                        save_internal_copy BOOLEAN DEFAULT true,
                        preserve_thread_headers BOOLEAN DEFAULT true,
                        auto_process_on_fetch BOOLEAN DEFAULT false,
                        process_only_with_attachments BOOLEAN DEFAULT false,
                        process_only_with_pdf BOOLEAN DEFAULT false,
                        process_without_attachments BOOLEAN DEFAULT true,
                        process_read_emails BOOLEAN DEFAULT false,
                        avoid_duplicates_by_message_id BOOLEAN DEFAULT true,
                        allow_reprocess BOOLEAN DEFAULT false,
                        auto_create_order_if_detected BOOLEAN DEFAULT true,
                        always_human_review BOOLEAN DEFAULT true,
                        mark_doubtful_below_threshold BOOLEAN DEFAULT true,
                        mark_no_order_if_detected BOOLEAN DEFAULT true,
                        action_order_detected VARCHAR(80) DEFAULT 'move_processed',
                        action_no_order VARCHAR(80) DEFAULT 'move_no_order',
                        action_doubtful VARCHAR(80) DEFAULT 'move_doubtful',
                        action_error VARCHAR(80) DEFAULT 'move_error',
                        minimum_score_auto_order INTEGER DEFAULT 90,
                        visible_states TEXT DEFAULT 'pending,processing,pedido,no_pedido,dudoso,error_processing,pending_reprocess,responded,closed',
                        default_filter VARCHAR(80) DEFAULT 'all',
                        default_date_range VARCHAR(80) DEFAULT 'today',
                        default_page_size INTEGER DEFAULT 25,
                        default_sort VARCHAR(80) DEFAULT 'date_desc',
                        show_summary_cards BOOLEAN DEFAULT true,
                        show_score_column BOOLEAN DEFAULT true,
                        show_customer_column BOOLEAN DEFAULT true,
                        show_attachments_column BOOLEAN DEFAULT true,
                        show_order_column BOOLEAN DEFAULT true,
                        show_reply_button BOOLEAN DEFAULT true,
                        show_process_button BOOLEAN DEFAULT true,
                        signature_text TEXT DEFAULT 'Equipo de pedidos',
                        signature_html TEXT,
                        use_signature BOOLEAN DEFAULT true,
                        include_logo_in_signature BOOLEAN DEFAULT false,
                        legal_footer TEXT,
                        last_imap_test_at DATETIME,
                        last_imap_test_ok BOOLEAN,
                        last_imap_test_message TEXT,
                        last_sync_at DATETIME,
                        last_sync_ok BOOLEAN,
                        last_sync_message TEXT,
                        last_sync_error TEXT,
                        last_sync_new INTEGER DEFAULT 0,
                        last_sync_duplicates INTEGER DEFAULT 0,
                        last_smtp_test_at DATETIME,
                        last_smtp_test_ok BOOLEAN,
                        last_smtp_test_message TEXT,
                        updated_by INTEGER,
                        created_at DATETIME,
                        updated_at DATETIME
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE schema_migrations (
                        id INTEGER PRIMARY KEY,
                        company_id INTEGER UNIQUE,
                        version VARCHAR(80),
                        name VARCHAR(180),
                        checksum VARCHAR(120),
                        execution_ms INTEGER DEFAULT 0,
                        application_version VARCHAR(80),
                        status VARCHAR(30),
                        applied_at DATETIME,
                        last_checked_at DATETIME,
                        last_error TEXT,
                        notes TEXT,
                        created_at DATETIME,
                        updated_at DATETIME
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO schema_migrations
                        (id, company_id, version, name, checksum, execution_ms, application_version, status, applied_at, last_checked_at, last_error, notes, created_at, updated_at)
                    VALUES
                        (1, 3, :version, :name, :checksum, 0, '1.2.3', 'current', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """
                ),
                {
                    "version": CURRENT_TENANT_SCHEMA_VERSION,
                    "name": CURRENT_TENANT_SCHEMA_NAME,
                    "checksum": CURRENT_TENANT_SCHEMA_CHECKSUM,
                },
            )

        result = upgrade_tenant_schema(self.tenant_engine, company_id=3, application_version="1.2.3")
        self.assertTrue(result["is_current"])
        columns = {column["name"] for column in inspect(self.tenant_engine).get_columns("email_settings")}
        self.assertIn("smtp_enabled", columns)
        self.assertIn("initial_history_mode", columns)
        self.assertIn("initial_history_limit", columns)

    def test_upgrade_messages_schema_creates_conversations_and_links_legacy_data(self):
        self._seed_legacy_messages_schema()
        result = upgrade_tenant_schema(self.tenant_engine, company_id=1, application_version="1.2.3")
        self.assertTrue(result["is_current"])

        db = self.TenantSession()
        try:
            conversation_count = db.scalar(select(func.count()).select_from(Conversation)) or 0
            email_conversation_id = db.execute(text("SELECT conversation_id FROM emails WHERE id = 1")).scalar_one()
            order_conversation_id = db.execute(text("SELECT conversation_id FROM orders WHERE id = 1")).scalar_one()
            inbound = db.execute(text("SELECT conversation_id FROM inbound_messages WHERE id = 1")).scalar_one()
        finally:
            db.close()

        self.assertEqual(conversation_count, 1)
        self.assertIsNotNone(email_conversation_id)
        self.assertIsNotNone(order_conversation_id)
        self.assertIsNotNone(inbound)

    def test_duplicate_jobs_are_blocked_by_inspection(self):
        with self.tenant_engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE background_jobs (
                        id INTEGER PRIMARY KEY,
                        company_id INTEGER,
                        job_type VARCHAR(80),
                        dedupe_key VARCHAR(255),
                        status VARCHAR(40),
                        payload_json TEXT,
                        progress INTEGER DEFAULT 0,
                        attempt_count INTEGER DEFAULT 0,
                        retry_count INTEGER DEFAULT 0,
                        max_retries INTEGER DEFAULT 3,
                        queued_at DATETIME,
                        created_at DATETIME,
                        updated_at DATETIME
                    )
                    """
                )
            )
            conn.execute(text("INSERT INTO background_jobs (id, company_id, job_type, dedupe_key, status, payload_json, progress, attempt_count, retry_count, max_retries, queued_at, created_at, updated_at) VALUES (1, 1, 'process_email', 'dup', 'queued', '{}', 0, 0, 0, 3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
            conn.execute(text("INSERT INTO background_jobs (id, company_id, job_type, dedupe_key, status, payload_json, progress, attempt_count, retry_count, max_retries, queued_at, created_at, updated_at) VALUES (2, 1, 'process_email', 'dup', 'queued', '{}', 0, 0, 0, 3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        report = inspect_database_url(f"sqlite:///{self.tenant_path.as_posix()}", logical_name="tenant-duplicates")
        self.assertEqual(report["classification"], "blocked-by-data")
        self.assertIn("background_jobs duplicated dedupe_key", report["blockers"])

    def test_worker_skips_incompatible_schema_without_processing_jobs(self):
        self._create_tables_without_ledger(self.master_engine, MasterBase.metadata)
        db = self.MasterSession()
        db.add_all(
            [
                MasterCompany(id=1, name="Demo", slug="demo", active=True),
                MasterUser(id=1, email="admin@anchi.local", full_name="Admin Demo", password_hash=hash_password("admin123"), is_active=True),
                CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=True),
                MasterTenantDatabase(company_id=1, database_key="demo", database_url=f"sqlite:///{self.tenant_path.as_posix()}", is_active=True, health_status="ok"),
            ]
        )
        db.commit()
        db.close()

        self._seed_current_tenant_without_ledger()
        tenant_db = self.TenantSession()
        tenant_db.add(BackgroundJob(company_id=1, job_type="process_email", dedupe_key="worker-skip", status="queued", payload_json='{"email_id": 1}', progress=0))
        tenant_db.commit()
        tenant_db.close()

        calls = {"count": 0}

        def fake_process(*args, **kwargs):  # noqa: ANN001
            calls["count"] += 1
            raise AssertionError("The worker should not process jobs when the schema is not current.")

        with patch("app.workers.jobs_worker.MasterSessionLocal", new=self.MasterSession), patch("app.agent.platform.UnifiedOrderPipelineService.process_inbound_message", new=fake_process):
            summary = run_worker_cycle()

        self.assertEqual(summary["blocked"], 1)
        self.assertEqual(summary["processed"], 0)
        self.assertEqual(calls["count"], 0)
        tenant_db = self.TenantSession()
        try:
            queued = tenant_db.scalar(select(func.count()).select_from(BackgroundJob))
        finally:
            tenant_db.close()
        self.assertEqual(queued, 1)

    def test_simulation_runs_on_copy_without_touching_original(self):
        self._seed_current_tenant_without_ledger(with_jobs=True)
        source_before = inspect_database_url(f"sqlite:///{self.tenant_path.as_posix()}", logical_name="source")
        self.assertEqual(source_before["classification"], "current-without-ledger")

        result = simulate_sqlite_reference(self.tenant_path, self.simulation_root, label="tenant", kind_hint="tenant", company_id=1, application_version="1.2.3")
        self.assertTrue(Path(result["copy_path"]).exists())
        self.assertTrue(result["dry_run"]["dry_run"])
        self.assertIsNotNone(result["baseline"])
        self.assertIsNotNone(result["upgrade"])
        self.assertIsNotNone(result["second_run"])

        source_after = inspect_database_url(f"sqlite:///{self.tenant_path.as_posix()}", logical_name="source")
        self.assertEqual(source_after["classification"], "current-without-ledger")
        self.assertFalse(table_exists(self.tenant_engine, "schema_migrations"))

    def _snapshot_counts(self, engine, tables: list[str]) -> dict[str, int]:  # noqa: ANN001
        counts: dict[str, int] = {}
        with engine.connect() as conn:
            for table in tables:
                if table in Base.metadata.tables or table in MasterBase.metadata.tables:
                    try:
                        counts[table] = int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
                    except Exception:  # noqa: BLE001
                        counts[table] = -1
                else:
                    counts[table] = -1
        return counts

    def _count_table(self, table_name: str) -> int:
        with self.tenant_engine.connect() as conn:
            try:
                return int(conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one())
            except Exception:
                return -1

    def _count_master(self, table_name: str) -> int:
        with self.master_engine.connect() as conn:
            try:
                return int(conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one())
            except Exception:
                return -1


if __name__ == "__main__":
    unittest.main()
