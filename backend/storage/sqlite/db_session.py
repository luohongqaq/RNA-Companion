"""Async SQLAlchemy lifecycle and short transactions for the SQLite adapter.

Reuse the engine/factory; create a fresh db_session for every operation. The
single writer controls ordering; write transactions use BEGIN IMMEDIATE.
"""

import sqlite3
import importlib.util
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import URL, event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def _migration_statements(script: str) -> Iterator[str]:
    """把一整份 .sql 迁移文件拆成一条条完整 SQL，交给 SQLAlchemy 逐条执行"""
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending.strip()
            pending = ""
    if pending.strip() and any(line.strip() and not line.lstrip().startswith("--")
                               for line in pending.splitlines()):
        raise ValueError("migration has an incomplete SQL statement")


class SQLiteDatabase:
    """存储层的数据库资源管理器，负责：
        - 创建连接引擎和 Session 工厂；
        - 配置 SQLite；
        - 提供查询 Session 和写事务；
        - 初始化、升级数据库；
        - 关闭数据库资源。
    """
    def __init__(self, path: Path, *, echo: bool = False, busy_timeout_ms: int = 5000) -> None:
        """准备数据库访问工具"""
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms cannot be negative")
        self.path = Path(path).resolve()
        self._closed = False
        # 创建SQL引擎
        self.engine = create_async_engine(
            URL.create("sqlite+aiosqlite", database=str(self.path)), echo=echo,
            connect_args={"timeout": busy_timeout_ms / 1000},
        )
        # 创建Session 工厂
        self.db_session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False, autoflush=False,
        )

        @event.listens_for(self.engine.sync_engine, "connect")
        def configure_connection(dbapi_connection, _connection_record):
            # 每当连接池建立一个新的物理数据库连接时，都会立刻触发这个事件。
            dbapi_connection.isolation_level = None #底层驱动将永远不会自动开启事务,与后续begin_transaction配套
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}") #等待锁的时间,处理额外竞争的兜底
                cursor.execute("PRAGMA foreign_keys=ON") #启用外键约束，阻止无效关联
                cursor.execute("PRAGMA journal_mode=WAL") #启用wal模式
                cursor.execute("PRAGMA synchronous=FULL") #提交时执行较强的磁盘同步，降低断电导致已确认数据丢失的风险
            finally:
                cursor.close()

        @event.listens_for(self.engine.sync_engine, "begin")
        def begin_transaction(connection):
            # 重建sessions这类被多表引用的父表时，只在初始化迁移连接关闭外键执行；
            # 提交前做foreign_key_check，普通读写事务始终重新启用外键。
            migration = connection.get_execution_options().get("sqlite_schema_migration", False)
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF" if migration else "PRAGMA foreign_keys=ON")
            mode = connection.get_execution_options().get("sqlite_begin_mode", "DEFERRED")
            if mode not in ("DEFERRED", "IMMEDIATE"):
                raise ValueError("unsupported SQLite transaction mode")
            connection.exec_driver_sql(f"BEGIN {mode}")

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("database has been closed")

    @asynccontextmanager
    async def db_session(self) -> AsyncIterator[AsyncSession]:
        """Fresh session for reads. Pending changes are rolled back on close, never auto-committed."""
        self._check_open()
        async with self.db_session_factory() as db_session:
            yield db_session

    @asynccontextmanager
    async def transaction(self, *, _schema_migration: bool = False) -> AsyncIterator[AsyncSession]:
        self._check_open()
        async with self.engine.connect() as connection: #正常运行完自动commit
            await connection.execution_options(
                sqlite_begin_mode="IMMEDIATE", sqlite_schema_migration=_schema_migration,
            )
            async with self.db_session_factory(bind=connection) as db_session:
                async with db_session.begin():
                    yield db_session

    async def initialize(self) -> None:
        """
        空库直接应用基线，已有库按版本号执行尚未登记的迁移。
        """
        self._check_open()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.transaction(_schema_migration=True) as db_session:
            await db_session.execute(text("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""))
            applied = set((await db_session.execute(text("SELECT version FROM schema_migrations"))).scalars())
            migration_root = Path(__file__).with_name("migrations")
            connection = await db_session.connection()
            if not applied and not (await connection.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name <> 'schema_migrations' "
                "AND name NOT GLOB 'sqlite_*' LIMIT 1"
            )).scalar():
                baseline = migration_root / "baseline.sql"
                for statement in _migration_statements(baseline.read_text(encoding="utf-8")):
                    await connection.exec_driver_sql(statement)
                applied = set((await db_session.execute(text("SELECT version FROM schema_migrations"))).scalars())

            migrations = (
                (int(path.stem.split("_", 1)[0]), path)
                for directory in (migration_root / "legacy", migration_root)
                for path in directory.iterdir()
                if path.suffix in (".sql", ".py") and path.stem.split("_", 1)[0].isdecimal()
            )
            for version, path in sorted(migrations):
                if version in applied:
                    continue
                if path.suffix == ".sql":
                    for statement in _migration_statements(path.read_text(encoding="utf-8")):
                        await connection.exec_driver_sql(statement)
                else:
                    # JSON 改名同时涉及规范 hash 与版本引用，需要在同一事务中用 Python 转换。
                    spec = importlib.util.spec_from_file_location(f"sqlite_migration_{version}", path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    await module.migrate(connection)
                await db_session.execute(text("INSERT INTO schema_migrations(version) VALUES (:version)"),
                                         {"version": version})
                applied.add(version)
            violations = (await connection.exec_driver_sql("PRAGMA foreign_key_check")).all()
            if violations:
                raise ValueError("数据库迁移后外键关系不完整，已回滚迁移")

    async def close(self) -> None:
        """只关连接、不做任何提交。关闭时连接上如果还挂着未提交的事务，会被直接回滚"""
        if not self._closed:
            self._closed = True
            await self.engine.dispose()
