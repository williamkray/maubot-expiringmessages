## Expirebot
## A plugin for Matrix that allows users to set message expiration times for rooms.

import asyncio
import datetime
import re
from typing import Type

from maubot import Plugin
from maubot.handlers import command, event
from mautrix.types import (MessageEvent, EventType, MessageType)

from .db import upgrade_table

def parse_duration(duration_str: str) -> int:
    """
    Parse a string such as "24h", "3d", "15m", or composite durations like "1d2h" into seconds.
    Raises ValueError if the format is invalid.
    """
    # Match optional days, hours, minutes, seconds (all parts optional but at least one must be present)
    pattern = re.compile(
        r"^((?P<days>\d+?)d)?\s*((?P<hours>\d+?)h)?\s*((?P<minutes>\d+?)m)?\s*((?P<seconds>\d+?)s)?$"
    )
    match = pattern.fullmatch(duration_str.strip())
    if not match or not any(match.group(g) for g in ("days", "hours", "minutes", "seconds")):
        raise ValueError(f"Invalid duration format: {duration_str}")

    parts = {name: int(val) for name, val in match.groupdict(default="0").items()}
    td = datetime.timedelta(
        days=parts["days"],
        hours=parts["hours"],
        minutes=parts["minutes"],
        seconds=parts["seconds"],
    )
    return int(td.total_seconds() * 1000) # return expiration time in ms

class ExpiringMessages(Plugin):

    _expirer_task: asyncio.Task = None

    async def can_use_command(self, evt: MessageEvent) -> bool:
        """Check if the user has permission to redact messages in this room."""
        try:
            # Get the room's power levels
            levels = await self.client.get_state_event(
                evt.room_id, EventType.ROOM_POWER_LEVELS
            )
            
            # Get the user's power level
            user_level = levels.get_user_level(evt.sender)
            
            # Get the power level required for redaction
            redact_level = levels.get_event_level(EventType.ROOM_REDACTION)
            
            # If no specific redaction level is set, it defaults to 50
            if redact_level is None:
                redact_level = 50
            
            # User must have at least the redaction power level
            return user_level >= redact_level
        except Exception as e:
            self.log.error(f"Error checking command permissions: {e}")
            return False


    async def _process_expirations(self):
        now_ms = int(datetime.datetime.now(datetime.UTC).timestamp() * 1000)
        try:
            # Use a more compatible query that works with both SQLite and PostgreSQL
            events_query = """
                SELECT e.event_id, e.room_id, r.expiry_msec as expiry_msec
                FROM events e
                INNER JOIN room_expiry_times r ON r.room_id = e.room_id
            """
            events_to_expire = await self.database.fetch(events_query)
            
            for event in events_to_expire:
                room_id = event['room_id']
                expiration_ms = event['expiry_msec']
                cutoff = now_ms - expiration_ms
                
                try:
                    event_content = await self.client.get_event(room_id, event['event_id'])
                    if event_content.timestamp < cutoff:
                        await self.client.redact(room_id, event['event_id'], reason="Message expired")
                        # Delete the event from our database after successful redaction
                        await self.database.execute(
                            "DELETE FROM events WHERE event_id = $1",
                            event['event_id']
                        )
                        self.log.info(f"Redacted event {event['event_id']} in room {room_id}")
                except Exception as e:
                    self.log.error(f"Failed to redact event {event['event_id']}: {e}")
        except Exception as e:
            self.log.error(f"Database error in _process_expirations: {e}")

    async def _expirer_loop(self):
        """
        Periodic task that scans recent room history for messages that have become expired and redacts them.
        This loop runs every 60 seconds.
        """
        while True:
            try:
                await self._process_expirations()
            except Exception as e:
                self.log.error("Error in expiration loop: %s", e)
            await asyncio.sleep(60)

    async def start(self) -> None:
        await super().start()
        # Database migrations will ensure RoomExpiration table exists.
        self._expirer_task = asyncio.create_task(self._expirer_loop())
        self.log.info("ExpirePlugin started!")

    async def stop(self) -> None:
        if self._expirer_task:
            self._expirer_task.cancel()
        await super().stop()

    @command.new("expire", help="Configure message expiration for rooms")
    async def cmd_expire(self, evt: MessageEvent) -> None:
        """Base command for message expiration settings"""
        await evt.respond("Available subcommands:\n"
                         "  !expire set <time> - Set expiration time (e.g. !expire set 24h)\n"
                         "  !expire unset - Disable message expiration\n"
                         "  !expire show - Show current expiration settings")

    @cmd_expire.subcommand("set", help="Set message expiration for this room")
    @command.argument("time_arg", "expiry time, e.g. 24h or 1d2h30m")
    async def cmd_expire_set(self, evt: MessageEvent, time_arg: str) -> None:
        room_id = evt.room_id

        ## room participants must have appropriate PL to set message expiration
        if not await self.can_use_command(evt):
            await evt.reply(
                "Only users with PL of 50 or higher can set message expiration."
            )
            return None

        # Handle setting expiration time
        try:
            mseconds = parse_duration(time_arg)
        except ValueError as e:
            await evt.respond(f"Error parsing duration: {e}")
            return
        
        try:
            # Use REPLACE INTO for SQLite compatibility
            query = """
                INSERT INTO room_expiry_times(room_id, expiry_msec)
                VALUES ($1, $2)
                ON CONFLICT(room_id) DO UPDATE SET expiry_msec=$2
            """
            await self.database.execute(query, room_id, mseconds)
            await evt.respond(f"Message expiration for this room set to {time_arg}")
        except Exception as e:
            self.log.error(f"Database error in cmd_expire_set: {e}")
            await evt.respond("Failed to update room expiration settings. Please try again later.")

    @cmd_expire.subcommand("unset", help="Disable message expiration for this room")
    async def cmd_expire_unset(self, evt: MessageEvent) -> None:
        room_id = evt.room_id

        if not await self.can_use_command(evt):
            await evt.reply(
                "Only users with PL of 50 or higher can set message expiration."
            )
            return None

        try:
            # Delete the room rule - events will be automatically deleted due to ON DELETE CASCADE
            await self.database.execute(
                "DELETE FROM room_expiry_times WHERE room_id = $1",
                room_id
            )
            await evt.respond("Message expiration for this room has been disabled. All tracked messages will be preserved.")
        except Exception as e:
            self.log.error(f"Database error in cmd_expire_unset: {e}")
            await evt.respond("Failed to disable room expiration. Please try again later.")

    @cmd_expire.subcommand("show", help="Show current message expiration settings for this room")
    async def cmd_expire_show(self, evt: MessageEvent) -> None:
        room_id = evt.room_id
        try:
            query = """
                SELECT expiry_msec FROM room_expiry_times
                WHERE room_id = $1
            """
            result = await self.database.fetchrow(query, room_id)
            
            if result:
                # Convert milliseconds back to human readable format
                msec = result['expiry_msec']
                days = msec // (24 * 60 * 60 * 1000)
                msec %= (24 * 60 * 60 * 1000)
                hours = msec // (60 * 60 * 1000)
                msec %= (60 * 60 * 1000)
                minutes = msec // (60 * 1000)
                msec %= (60 * 1000)
                seconds = msec // 1000
                
                parts = []
                if days > 0:
                    parts.append(f"{days}d")
                if hours > 0:
                    parts.append(f"{hours}h")
                if minutes > 0:
                    parts.append(f"{minutes}m")
                if seconds > 0:
                    parts.append(f"{seconds}s")
                
                duration = "".join(parts)
                await evt.respond(f"Messages in this room expire after {duration}")
            else:
                await evt.respond("No message expiration is set for this room")
        except Exception as e:
            self.log.error(f"Database error in cmd_expire_show: {e}")
            await evt.respond("Failed to fetch room expiration settings. Please try again later.")

    @event.on(EventType.ROOM_MESSAGE)
    async def track_expiring_message(self, evt: MessageEvent) -> None:
        if evt.content.msgtype in {MessageType.TEXT, MessageType.NOTICE, MessageType.EMOTE, 
                                   MessageType.FILE, MessageType.IMAGE, MessageType.VIDEO,
                                   MessageType.LOCATION}:
            try:
                room_rules = await self.database.fetch("SELECT room_id, expiry_msec FROM room_expiry_times")
                
                # Check if this room has an expiration rule
                room_rule = next((rule for rule in room_rules if rule['room_id'] == evt.room_id), None)
                if room_rule:
                    query = """
                        INSERT INTO events(event_id, room_id)
                        VALUES ($1, $2)
                    """
                    await self.database.execute(query, evt.event_id, evt.room_id)
            except Exception as e:
                self.log.error(f"Database error in track_expiring_message: {e}")
                # Don't respond to the user since this is an event handler

    @event.on(EventType.STICKER)
    async def track_expiring_sticker(self, evt) -> None:
        try:
            room_rules = await self.database.fetch("SELECT room_id, expiry_msec FROM room_expiry_times")
            
            # Check if this room has an expiration rule
            room_rule = next((rule for rule in room_rules if rule['room_id'] == evt.room_id), None)
            if room_rule:
                query = """
                    INSERT INTO events(event_id, room_id)
                    VALUES ($1, $2)
                """
                await self.database.execute(query, evt.event_id, evt.room_id)
        except Exception as e:
            self.log.error(f"Database error in track_expiring_sticker: {e}")
            # Don't respond to the user since this is an event handler

    @classmethod
    def get_db_upgrade_table(cls) -> None:
        return upgrade_table

