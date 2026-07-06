import json
import sqlite3
from pathlib import Path
from typing import Iterable

from scraper import Weapon


class Database:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row

    def initialize(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS weapons (
                identity TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                tier TEXT,
                weapon_type TEXT,
                range_role TEXT NOT NULL DEFAULT 'Polyvalente',
                image_url TEXT,
                url TEXT NOT NULL,
                rank INTEGER,
                attachments TEXT NOT NULL DEFAULT '[]',
                build TEXT NOT NULL DEFAULT '{}',
                build_signature TEXT,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._ensure_column("weapons", "build", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column("weapons", "build_signature", "TEXT")
        self._ensure_column("weapons", "range_role", "TEXT NOT NULL DEFAULT 'Polyvalente'")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS published_weapons (
                identity TEXT PRIMARY KEY,
                published_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS current_top5 (
                position INTEGER PRIMARY KEY,
                identity TEXT NOT NULL UNIQUE,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS current_meta_picks (
                range_role TEXT PRIMARY KEY,
                identity TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS published_builds (
                build_signature TEXT PRIMARY KEY,
                identity TEXT NOT NULL,
                published_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS latest_builds (
                identity TEXT PRIMARY KEY,
                build_signature TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.connection.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        rows = self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        if any(row["name"] == column for row in rows):
            return
        self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def upsert_weapons(self, weapons: Iterable[Weapon]) -> None:
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO weapons (
                    identity, name, tier, weapon_type, range_role, image_url, url, rank, attachments,
                    build, build_signature, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(identity) DO UPDATE SET
                    name = excluded.name,
                    tier = excluded.tier,
                    weapon_type = excluded.weapon_type,
                    range_role = excluded.range_role,
                    image_url = excluded.image_url,
                    url = excluded.url,
                    rank = excluded.rank,
                    attachments = excluded.attachments,
                    build = excluded.build,
                    build_signature = excluded.build_signature,
                    last_seen_at = CURRENT_TIMESTAMP
                """,
                [
                    (
                        weapon.identity,
                        weapon.name,
                        weapon.tier,
                        weapon.weapon_type,
                        weapon.range_role,
                        weapon.image_url,
                        weapon.url,
                        weapon.rank,
                        json.dumps(weapon.attachments, ensure_ascii=False),
                        json.dumps(weapon.build, ensure_ascii=False),
                        weapon.build_signature,
                    )
                    for weapon in weapons
                ],
            )

    def was_published(self, identity: str) -> bool:
        cursor = self.connection.execute(
            "SELECT 1 FROM published_weapons WHERE identity = ? LIMIT 1",
            (identity,),
        )
        return cursor.fetchone() is not None

    def mark_published(self, weapon: Weapon) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO published_weapons (identity, published_at)
                VALUES (?, CURRENT_TIMESTAMP)
                """,
                (weapon.identity,),
            )

    def was_build_published(self, build_signature: str) -> bool:
        cursor = self.connection.execute(
            "SELECT 1 FROM published_builds WHERE build_signature = ? LIMIT 1",
            (build_signature,),
        )
        return cursor.fetchone() is not None

    def mark_build_published(self, weapon: Weapon) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO published_builds (build_signature, identity, published_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                """,
                (weapon.build_signature, weapon.identity),
            )

    def get_latest_build_signature(self, identity: str) -> str | None:
        row = self.connection.execute(
            """
            SELECT build_signature
            FROM latest_builds
            WHERE identity = ?
            LIMIT 1
            """,
            (identity,),
        ).fetchone()
        return row["build_signature"] if row else None

    def replace_latest_builds(self, weapons: Iterable[Weapon]) -> None:
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO latest_builds (identity, build_signature, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(identity) DO UPDATE SET
                    build_signature = excluded.build_signature,
                    updated_at = CURRENT_TIMESTAMP
                """,
                [(weapon.identity, weapon.build_signature) for weapon in weapons],
            )

    def get_current_top5_identities(self) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT identity
            FROM current_top5
            ORDER BY position ASC
            """
        ).fetchall()
        return [row["identity"] for row in rows]

    def replace_current_top5(self, weapons: Iterable[Weapon]) -> None:
        weapon_list = list(weapons)
        with self.connection:
            self.connection.execute("DELETE FROM current_top5")
            self.connection.executemany(
                """
                INSERT INTO current_top5 (position, identity, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                """,
                [
                    (index, weapon.identity)
                    for index, weapon in enumerate(weapon_list[:5], start=1)
                ],
            )

    def get_current_meta_picks(self) -> dict[str, str]:
        rows = self.connection.execute(
            """
            SELECT range_role, identity
            FROM current_meta_picks
            """
        ).fetchall()
        return {row["range_role"]: row["identity"] for row in rows}

    def replace_current_meta_picks(self, weapons: Iterable[Weapon]) -> None:
        weapon_list = list(weapons)
        with self.connection:
            self.connection.execute("DELETE FROM current_meta_picks")
            self.connection.executemany(
                """
                INSERT INTO current_meta_picks (range_role, identity, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                """,
                [(weapon.range_role, weapon.identity) for weapon in weapon_list],
            )

    def get_weapons(self, limit: int | None = None) -> list[Weapon]:
        query = """
            SELECT name, tier, weapon_type, range_role, image_url, url, rank, attachments, build
            FROM weapons
            ORDER BY rank ASC, name ASC
        """
        params: tuple[int, ...] = ()

        if limit is not None:
            query += " LIMIT ?"
            params = (limit,)

        rows = self.connection.execute(query, params).fetchall()
        weapons: list[Weapon] = []

        for row in rows:
            try:
                attachments = json.loads(row["attachments"] or "[]")
            except json.JSONDecodeError:
                attachments = []
            try:
                build = json.loads(row["build"] or "{}")
            except json.JSONDecodeError:
                build = {}

            weapons.append(
                Weapon(
                    name=row["name"],
                    tier=row["tier"] or "",
                    weapon_type=row["weapon_type"] or "",
                    range_role=row["range_role"] or "Polyvalente",
                    image_url=row["image_url"] or "",
                    url=row["url"],
                    rank=row["rank"] or len(weapons) + 1,
                    attachments=attachments if isinstance(attachments, list) else [],
                    build=build if isinstance(build, dict) else {},
                )
            )

        return weapons

    def close(self) -> None:
        self.connection.close()
