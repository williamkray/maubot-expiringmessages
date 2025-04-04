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
    _redaction_semaphore: asyncio.Semaphore = None
    _last_redaction_time: float = 0
    _min_redaction_interval: float = 0.1  # Minimum 100ms between redactions

    async def can_use_command(self, evt: MessageEvent) -> tuple[bool, str]:
        """
        Check if both the user and bot have permission to redact messages in this room.
        Returns a tuple of (has_permission, error_message).
        """
        try:
            # Get the room's power levels
            levels = await self.client.get_state_event(
                evt.room_id, EventType.ROOM_POWER_LEVELS
            )
            
            # Get the user's power level
            user_level = levels.get_user_level(evt.sender)
            
            # Get the bot's power level
            bot_level = levels.get_user_level(self.client.mxid)
            
            # Get the power level required for redaction
            redact_level = getattr(levels, 'redact', 50)  # Default to 50 if not set
            
            # Debug logging
            self.log.debug(f"Permission check for room {evt.room_id}:")
            self.log.debug(f"  User {evt.sender} has level {user_level}")
            self.log.debug(f"  Bot {self.client.mxid} has level {bot_level}")
            self.log.debug(f"  Required redaction level is {redact_level}")
            
            # Check if user has permission
            if user_level < redact_level:
                return False, f"You need a power level of {redact_level} or higher to set message expiration."
            
            # Check if bot has permission
            if bot_level < redact_level:
                return False, f"I need a power level of {redact_level} or higher to redact messages."
            
            return True, ""
        except Exception as e:
            self.log.error(f"Error checking command permissions: {e}")
            return False, "Failed to check permissions. Please try again later."

    async def _redact_with_backoff(self, room_id: str, event_id: str, max_retries: int = 5) -> bool:
        """
        Attempt to redact an event with exponential backoff.
        Returns True if successful, False if failed after all retries.
        """
        base_delay = 1  # Start with 1 second delay
        max_delay = 32  # Maximum delay of 32 seconds
        
        for attempt in range(max_retries):
            try:
                # Ensure minimum time between redactions
                now = asyncio.get_event_loop().time()
                time_since_last = now - self._last_redaction_time
                if time_since_last < self._min_redaction_interval:
                    await asyncio.sleep(self._min_redaction_interval - time_since_last)
                
                async with self._redaction_semaphore:
                    await self.client.redact(room_id, event_id, reason="Message expired")
                    self._last_redaction_time = asyncio.get_event_loop().time()
                    return True
            except Exception as e:
                if "Too Many Requests" in str(e):
                    delay = min(base_delay * (2 ** attempt), max_delay)  # Exponential backoff, capped at max_delay
                    self.log.warning(f"Rate limited while redacting {event_id}. Retrying in {delay} seconds...")
                    await asyncio.sleep(delay)
                else:
                    self.log.error(f"Failed to redact event {event_id}: {e}")
                    return False
        
        self.log.error(f"Failed to redact event {event_id} after {max_retries} attempts")
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
            
            # Process events in smaller batches to avoid overwhelming the server
            batch_size = 10
            for i in range(0, len(events_to_expire), batch_size):
                batch = events_to_expire[i:i + batch_size]
                
                for event in batch:
                    room_id = event['room_id']
                    expiration_ms = event['expiry_msec']
                    cutoff = now_ms - expiration_ms
                    
                    try:
                        event_content = await self.client.get_event(room_id, event['event_id'])
                        if event_content.timestamp < cutoff:
                            if await self._redact_with_backoff(room_id, event['event_id']):
                                # Delete the event from our database after successful redaction
                                await self.database.execute(
                                    "DELETE FROM events WHERE event_id = $1",
                                    event['event_id']
                                )
                                self.log.info(f"Redacted event {event['event_id']} in room {room_id}")
                            else:
                                self.log.error(f"Failed to redact event {event['event_id']} after all retries")
                    except Exception as e:
                        self.log.error(f"Failed to process event {event['event_id']}: {e}")
                
                # Add a small delay between batches
                if i + batch_size < len(events_to_expire):
                    await asyncio.sleep(0.5)
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
        self._redaction_semaphore = asyncio.Semaphore(1)  # Only one redaction at a time
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
        has_permission, error_msg = await self.can_use_command(evt)
        if not has_permission:
            await evt.respond(error_msg)
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

        has_permission, error_msg = await self.can_use_command(evt)
        if not has_permission:
            await evt.respond(error_msg)
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

