from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

from database import Repositories
from database.mongo import MongoConfig, MongoRuntime


def rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    cur = conn.execute(f'SELECT * FROM "{table}"')
    return [dict(row) for row in cur.fetchall()]


def find_guild_id(conn: sqlite3.Connection) -> int | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT value FROM default_setting WHERE name = 'guild_id' LIMIT 1").fetchone()
    return int(row["value"]) if row else None


def mongo_config_from_env() -> MongoConfig:
    load_dotenv()
    return MongoConfig(
        uri=os.getenv("MONGO_URI"),
        db_name=os.getenv("MONGO_DB_NAME", "devilblox"),
        host=os.getenv("MONGO_HOST", "127.0.0.1"),
        port=int(os.getenv("MONGO_PORT", "27017")),
        user=os.getenv("MONGO_USER") or None,
        password=os.getenv("MONGO_PASSWORD") or None,
        auth_db=os.getenv("MONGO_AUTH_DB", "admin"),
        use_ssh=os.getenv("MONGO_USE_SSH", "").lower() in {"1", "true", "yes", "on"},
        ssh_host=os.getenv("SSH_HOST") or None,
        ssh_port=int(os.getenv("SSH_PORT", "22")),
        ssh_user=os.getenv("SSH_USER") or None,
        ssh_key=os.getenv("SSH_KEY") or None,
    )


async def migrate(sqlite_path: Path, guild_id: int | None):
    if not sqlite_path.is_file():
        raise FileNotFoundError(sqlite_path)

    conn = sqlite3.connect(sqlite_path)
    try:
        target_guild_id = guild_id or find_guild_id(conn)
        if target_guild_id is None:
            raise RuntimeError("guild_id not found. Pass --guild-id explicitly.")

        runtime = MongoRuntime(mongo_config_from_env())
        db = await runtime.connect()
        repos = Repositories(db)
        await repos.ensure_indexes()

        default_settings = {row["name"]: row["value"] for row in rows(conn, "default_setting")}
        await repos.settings.apply_legacy_settings(target_guild_id, default_settings)

        for row in rows(conn, "user_data"):
            await repos.users.import_legacy_user(target_guild_id, row)

        for row in rows(conn, "seller_data"):
            await repos.sellers.import_legacy_seller(target_guild_id, row)

        for row in rows(conn, "middleman_data"):
            await repos.middlemen.import_legacy_middleman(target_guild_id, row)

        for row in rows(conn, "coupon_data"):
            await repos.coupons.import_legacy_coupon(target_guild_id, row)

        for row in rows(conn, "user_coupon_data"):
            await repos.coupons.user_coupons.update_one(
                {
                    "guild_id": target_guild_id,
                    "user_id": int(row["user_id"]),
                    "code": row["coupon_code"],
                },
                {
                    "$set": {
                        "guild_id": target_guild_id,
                        "user_id": int(row["user_id"]),
                        "code": row["coupon_code"],
                        "acquired_date": row["acquired_date"],
                        "deadline": row["deadline"],
                    }
                },
                upsert=True,
            )

        for row in rows(conn, "purchase_list"):
            await repos.tickets.create(
                target_guild_id,
                "purchase",
                int(row["user_id"]),
                int(row["channel_id"]),
                migrated=True,
            )

        for row in rows(conn, "ticket_list"):
            await repos.tickets.create(
                target_guild_id,
                "support",
                int(row["user_id"]),
                int(row["channel_id"]),
                migrated=True,
            )

        for row in rows(conn, "middleman_service"):
            await repos.tickets.create(
                target_guild_id,
                "middleman",
                int(row["user_id"]),
                int(row["channel_id"]),
                counterparty_id=int(row["counterparty_id"]),
                middleman_id=int(row["middleman_id"]),
                migrated=True,
            )

        await runtime.close()
        print(f"Migration complete: guild_id={target_guild_id}")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Migrate DEVILROBLOX SQLite data into MongoDB.")
    parser.add_argument("--sqlite", default="DEVILROBLOX_extracted/UserData.db", help="Path to UserData.db")
    parser.add_argument("--guild-id", type=int, default=None, help="Override guild id")
    args = parser.parse_args()
    asyncio.run(migrate(Path(args.sqlite), args.guild_id))


if __name__ == "__main__":
    main()
