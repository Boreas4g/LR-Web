from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    select,
    text,
    update,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import Settings

DEADLOCK_SQLSTATE = "40P01"
DEADLOCK_RETRY_DELAYS_S = (0.05, 0.15, 0.3)


def is_deadlock(error: OperationalError) -> bool:
    """True when *error* wraps a PostgreSQL deadlock (SQLSTATE 40P01)."""
    return getattr(error.orig, "sqlstate", None) == DEADLOCK_SQLSTATE


class Base(DeclarativeBase):
    pass


class UserModel(Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)


class RobotModel(Base):
    __tablename__ = "robots"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    location: Mapped[str] = mapped_column(String(255), nullable=False)
    current_task: Mapped[str] = mapped_column(Text, nullable=False)
    map_version: Mapped[str] = mapped_column(String(120), nullable=False)
    map_id: Mapped[str | None] = mapped_column(String(80))
    pose: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    localization_status: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown")
    localization_scan_mode: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    health: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(80), nullable=False)


class TaskModel(Base):
    __tablename__ = "dispatch_tasks"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    robot_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    target: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    progress: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(80), nullable=False)
    map_id: Mapped[str | None] = mapped_column(String(80))
    annotation_id: Mapped[str | None] = mapped_column(String(80))
    shelf_id: Mapped[str | None] = mapped_column(String(80))
    route_annotation_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    loop_count: Mapped[int | None] = mapped_column(Integer)
    book_id: Mapped[str | None] = mapped_column(String(80))
    book_title: Mapped[str | None] = mapped_column(String(160))
    workflow_mode: Mapped[str | None] = mapped_column(String(32))
    delivery_annotation_id: Mapped[str | None] = mapped_column(String(80))
    delivery_label: Mapped[str | None] = mapped_column(String(160))
    workflow_id: Mapped[str | None] = mapped_column(String(80))
    workflow_step: Mapped[int | None] = mapped_column(Integer)
    workflow_step_count: Mapped[int | None] = mapped_column(Integer)
    workflow_step_name: Mapped[str | None] = mapped_column(String(255))
    workflow_message: Mapped[str | None] = mapped_column(Text)
    workflow_details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failure_reason: Mapped[str | None] = mapped_column(Text)


class AlertModel(Base):
    __tablename__ = "alerts"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    time: Mapped[str] = mapped_column(String(80), nullable=False)
    acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False)


class MapVersionModel(Base):
    __tablename__ = "map_versions"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[str] = mapped_column(String(80), nullable=False)
    yaml_file: Mapped[str] = mapped_column(String(255), nullable=False)
    pgm_file: Mapped[str] = mapped_column(String(255), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution: Mapped[float] = mapped_column(nullable=False)
    origin: Mapped[list[float]] = mapped_column(JSON, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    negate: Mapped[bool] = mapped_column(Boolean, nullable=False)
    occupied_threshold: Mapped[float] = mapped_column(nullable=False)
    free_threshold: Mapped[float] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), nullable=False)


class MapAnnotationModel(Base):
    __tablename__ = "map_annotations"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    map_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    pixel_x: Mapped[int] = mapped_column(Integer, nullable=False)
    pixel_y: Mapped[int] = mapped_column(Integer, nullable=False)
    map_x: Mapped[float] = mapped_column(nullable=False)
    map_y: Mapped[float] = mapped_column(nullable=False)
    yaw: Mapped[float] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), nullable=False)


class CommandModel(Base):
    __tablename__ = "robot_commands"

    command_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    robot_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    command: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(80))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class BookModel(Base):
    __tablename__ = "books"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    template_id: Mapped[str] = mapped_column(String(80), nullable=False)
    height_mm: Mapped[float] = mapped_column(Float, nullable=False)
    annotation_id: Mapped[str | None] = mapped_column(String(80))
    preferred_slot: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    updated_at: Mapped[str] = mapped_column(String(80), nullable=False, default="")


class StressTestTemplateModel(Base):
    __tablename__ = "stress_test_templates"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    updated_at: Mapped[str] = mapped_column(String(80), nullable=False, default="")


class AuditModel(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    username: Mapped[str] = mapped_column(String(80), nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    robot_id: Mapped[str] = mapped_column(String(80), nullable=False)
    command_id: Mapped[str | None] = mapped_column(String(80))


class Database:
    def __init__(self, settings: Settings) -> None:
        if not settings.sqlalchemy_database_url:
            raise ValueError("LR_DATABASE_URL is required for PostgreSQL storage")
        self.engine = create_engine(settings.sqlalchemy_database_url, pool_pre_ping=True)
        self._session_factory = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)
        # Existing development databases predate map/location linkage columns.
        with self.engine.begin() as connection:
            for statement in (
                "ALTER TABLE dispatch_tasks ADD COLUMN IF NOT EXISTS map_id VARCHAR(80)",
                "ALTER TABLE dispatch_tasks ADD COLUMN IF NOT EXISTS annotation_id VARCHAR(80)",
                "ALTER TABLE dispatch_tasks ADD COLUMN IF NOT EXISTS shelf_id VARCHAR(80)",
                (
                    "ALTER TABLE dispatch_tasks ADD COLUMN IF NOT EXISTS "
                    "route_annotation_ids JSONB NOT NULL DEFAULT '[]'::jsonb"
                ),
                "ALTER TABLE dispatch_tasks ADD COLUMN IF NOT EXISTS loop_count INTEGER",
                "ALTER TABLE dispatch_tasks ADD COLUMN IF NOT EXISTS book_id VARCHAR(80)",
                "ALTER TABLE dispatch_tasks ADD COLUMN IF NOT EXISTS book_title VARCHAR(160)",
                "ALTER TABLE dispatch_tasks ADD COLUMN IF NOT EXISTS workflow_mode VARCHAR(32)",
                "ALTER TABLE dispatch_tasks ADD COLUMN IF NOT EXISTS delivery_annotation_id VARCHAR(80)",
                "ALTER TABLE dispatch_tasks ADD COLUMN IF NOT EXISTS delivery_label VARCHAR(160)",
                "ALTER TABLE dispatch_tasks ADD COLUMN IF NOT EXISTS workflow_id VARCHAR(80)",
                "ALTER TABLE dispatch_tasks ADD COLUMN IF NOT EXISTS workflow_step INTEGER",
                "ALTER TABLE dispatch_tasks ADD COLUMN IF NOT EXISTS workflow_step_count INTEGER",
                "ALTER TABLE dispatch_tasks ADD COLUMN IF NOT EXISTS workflow_step_name VARCHAR(255)",
                "ALTER TABLE dispatch_tasks ADD COLUMN IF NOT EXISTS workflow_message TEXT",
                (
                    "ALTER TABLE dispatch_tasks ADD COLUMN IF NOT EXISTS "
                    "workflow_details JSONB NOT NULL DEFAULT '{}'::jsonb"
                ),
                (
                    "ALTER TABLE dispatch_tasks ADD COLUMN IF NOT EXISTS "
                    "requires_confirmation BOOLEAN NOT NULL DEFAULT FALSE"
                ),
                "ALTER TABLE dispatch_tasks ADD COLUMN IF NOT EXISTS failure_reason TEXT",
                "ALTER TABLE robots ADD COLUMN IF NOT EXISTS map_id VARCHAR(80)",
                "ALTER TABLE robots ADD COLUMN IF NOT EXISTS pose JSONB",
                (
                    "ALTER TABLE robots ADD COLUMN IF NOT EXISTS "
                    "localization_status VARCHAR(40) NOT NULL DEFAULT 'unknown'"
                ),
                (
                    "ALTER TABLE robots ADD COLUMN IF NOT EXISTS "
                    "localization_scan_mode VARCHAR(40) NOT NULL DEFAULT 'unknown'"
                ),
                "ALTER TABLE robots ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ",
                "ALTER TABLE books ADD COLUMN IF NOT EXISTS annotation_id VARCHAR(80)",
                "ALTER TABLE books ADD COLUMN IF NOT EXISTS preferred_slot INTEGER",
            ):
                connection.execute(text(statement))

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def has_users(self) -> bool:
        with self.session() as session:
            return session.scalar(select(UserModel.username).limit(1)) is not None

    def audit(self, username: str, action: str, robot_id: str, command_id: str | None = None) -> None:
        with self.session() as session:
            session.add(
                AuditModel(
                    created_at=datetime.now(UTC),
                    username=username,
                    action=action,
                    robot_id=robot_id,
                    command_id=command_id,
                )
            )

    def update_robot_telemetry(self, robot: RobotModel) -> None:
        """Persist high-frequency pose data without rebuilding every business table.

        Runs on the paho MQTT thread; a deadlock must be retried here instead
        of propagating, because an escaping exception historically killed the
        paho loop thread and froze all robot state. The transaction is a
        single-row update, so a deadlock victim can simply be re-executed.
        """
        for attempt, delay in enumerate((*DEADLOCK_RETRY_DELAYS_S, None)):
            try:
                with self.session() as session:
                    session.execute(
                        update(RobotModel)
                        .where(RobotModel.id == robot.id)
                        .values(
                            status=robot.status,
                            location=robot.location,
                            current_task=robot.current_task,
                            map_version=robot.map_version,
                            map_id=robot.map_id,
                            pose=robot.pose,
                            localization_status=robot.localization_status,
                            localization_scan_mode=robot.localization_scan_mode,
                            last_seen_at=robot.last_seen_at,
                            health=robot.health,
                            updated_at=robot.updated_at,
                        )
                    )
                return
            except OperationalError as error:
                if delay is None or not is_deadlock(error):
                    raise
                time.sleep(delay)
