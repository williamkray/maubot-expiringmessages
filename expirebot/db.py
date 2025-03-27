from mautrix.util.async_db import UpgradeTable, Scheme, Connection

upgrade_table = UpgradeTable()

@upgrade_table.register(description="Table initialization")
async def upgrade_v1(conn: Connection) -> None:
    # Create room_expiry_times table with compatible types
    await conn.execute(
            """CREATE TABLE room_expiry_times (
                room_id TEXT PRIMARY KEY,
                expiry_msec INTEGER NOT NULL
            )
            """
            )
    
    # Create events table with compatible types and foreign key
    await conn.execute(
            """CREATE TABLE events (
                event_id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                FOREIGN KEY(room_id) REFERENCES room_expiry_times(room_id) ON DELETE CASCADE
            )"""
            )
    
    # Create index on room_id for faster lookups
    await conn.execute(
            """CREATE INDEX idx_events_room_id ON events(room_id)"""
            )

@upgrade_table.register(description="Add index for faster room lookups")
async def upgrade_v2(conn: Connection) -> None:
    # Check if index exists first (SQLite doesn't support IF NOT EXISTS for indexes)
    try:
        await conn.execute("CREATE INDEX idx_events_room_id ON events(room_id)")
    except Exception:
        # Index might already exist, that's fine
        pass
