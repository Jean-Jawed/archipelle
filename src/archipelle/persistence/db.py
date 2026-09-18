"""Accès SQLite : une connexion par thread, mode WAL, migrations versionnées (CDC §7bis).

Les migrations sont des fichiers ``NNN_nom.sql`` rangés dans ``migrations/<base>/``.
Le numéro appliqué est conservé dans ``PRAGMA user_version``.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from archipelle.core import resources

_log = logging.getLogger(__name__)
_MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


class MigrationError(RuntimeError):
    pass


class Database:
    def __init__(self, path: Path, migrations: str) -> None:
        self.path = path
        self.migrations = migrations
        self._local = threading.local()
        self._lock = threading.Lock()
        self._all: list[sqlite3.Connection] = []

    # --- connexions -----------------------------------------------------------------

    def connection(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open()
            self._local.conn = conn
        return conn

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit : les transactions sont ouvertes explicitement par transaction().
        # check_same_thread=False uniquement pour permettre close_all() depuis un autre
        # thread ; chaque connexion reste utilisée par un seul thread.
        conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        with self._lock:
            self._all.append(conn)
        return conn

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        """Transaction d'écriture (``BEGIN IMMEDIATE``), validée ou annulée en bloc."""
        conn = self.connection()
        if conn.in_transaction:
            # Transaction imbriquée : on s'appuie sur la transaction englobante.
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def close_current_thread(self) -> None:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            self._local.conn = None
            with self._lock:
                if conn in self._all:
                    self._all.remove(conn)
            conn.close()

    def close_all(self) -> None:
        with self._lock:
            connections = list(self._all)
            self._all.clear()
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                _log.exception("Fermeture d'une connexion SQLite impossible")
        self._local = threading.local()

    # --- migrations -----------------------------------------------------------------

    def migrate(self) -> int:
        """Applique les migrations manquantes et renvoie la version atteinte."""
        conn = self.connection()
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        steps = _list_migrations(self.migrations)
        latest = steps[-1][0] if steps else 0
        if current > latest:
            raise MigrationError(
                f"Base {self.path.name} en version {current}, "
                f"plus récente que l'application ({latest})"
            )
        for number, script_path in steps:
            if number <= current:
                continue
            script = script_path.read_text(encoding="utf-8")
            _log.info("Migration %s → %03d (%s)", self.path.name, number, script_path.name)
            try:
                conn.executescript(
                    f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;"
                )
            except sqlite3.Error as exc:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise MigrationError(f"Échec de la migration {script_path.name} : {exc}") from exc
            current = number
        return current


def _list_migrations(name: str) -> list[tuple[int, Path]]:
    directory = resources.package_path("persistence", "migrations", name)
    steps: list[tuple[int, Path]] = []
    for path in directory.iterdir():
        match = _MIGRATION_NAME.match(path.name)
        if match:
            steps.append((int(match.group(1)), path))
    steps.sort()
    numbers = [number for number, _ in steps]
    if numbers != list(range(1, len(numbers) + 1)):
        raise MigrationError(f"Numérotation des migrations {name} incohérente : {numbers}")
    return steps
