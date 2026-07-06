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
                image_url TEXT,
                url TEXT NOT NULL,
                rank INTEGER,
                attachments TEXT NOT NULL DEFAULT '[]',
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS published_weapons (
                identity TEXT PRIMARY KEY,
                published_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.connection.commit()

    def upsert_weapons(self, weapons: Iterable[Weapon]) -> None:
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO weapons (
                    identity, name, tier, weapon_type, image_url, url, rank, attachments, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(identity) DO UPDATE SET
                    name = excluded.name,
                    tier = excluded.tier,
                    weapon_type = excluded.weapon_type,
                    image_url = excluded.image_url,
                    url = excluded.url,
                    rank = excluded.rank,
                    attachments = excluded.attachments,
                    last_seen_at = CURRENT_TIMESTAMP
                """,
                [
                    (
                        weapon.identity,
                        weapon.name,
                        weapon.tier,
                        weapon.weapon_type,
                        weapon.image_url,
                        weapon.url,
                        weapon.rank,
                        json.dumps(weapon.attachments, ensure_ascii=False),
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

    def get_weapons(self, limit: int | None = None) -> list[Weapon]:
        query = """
            SELECT name, tier, weapon_type, image_url, url, rank, attachments
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

            weapons.append(
                Weapon(
                    name=row["name"],
                    tier=row["tier"] or "",
                    weapon_type=row["weapon_type"] or "",
                    image_url=row["image_url"] or "",
                    url=row["url"],
                    rank=row["rank"] or len(weapons) + 1,
                    attachments=attachments if isinstance(attachments, list) else [],
                )
            )

        return weapons

    def close(self) -> None:
        self.connection.close()
