# Matrix Expiring Messages Bot

A Matrix bot that allows users to set message expiration times for rooms. Messages will be automatically redacted after
the specified duration.

## A NOTE ABOUT SECURITY

Security is complicated. Matrix is complicated. Put these together and you're bound to have some confusing
conversations. Using this plugin does not make you "secure". It does not protect you from malicious actors or software.

Please understand your threat model and how this plugin fits into that.

This bot assumes that all messages are sent by trusted contacts, and that the matrix homeservers are not
intentionally behaving badly. This is not always the case. This bot uses a message's timestamp to determine when
to delete it, however if a user sends a message with a spoofed timestamp, it may not be redacted at all, or
redacted on a different schedule than expected.

Also, the distributed nature of Matrix federation means that redacting a message on one server does not necessarily
guarantee that the redaction will reach the other participating servers in the room in the expected time frame, or at
all.

In addition, even if the message is effectively redacted, there is no guarantee that the participating servers
expunge that redacted data in a reasonable time. Synapse default is to retain the original message for 7 days (I
believe), but this can be modified or changed to never expunge the redacted event at all.

Beyond all of that, there is always the possibility that the people you are messaging may screenshot the
conversation, or otherwise transcribe it to some other location before it is expired.

Keep all of these factors in mind when using this plugin. The most ideal situation is using this plugin in a room that
is comprised of only members of a server you control, and have the ability to validate that the server is acting in
good faith. Anything beyond that and you are on your own.

## Commands

### Set Message Expiration
```
!expire set <duration>
```
Sets the message expiration time for the current room. The duration can be specified using:
- Days: `d` (e.g., `1d`)
- Hours: `h` (e.g., `24h`)
- Minutes: `m` (e.g., `30m`)
- Seconds: `s` (e.g., `60s`)

You can combine these units (e.g., `1d2h30m`). Expiration times are based on the timestamp of the message, so best
practices would be using an expiration time that gives participants enough time to read the message before it gets
deleted.

Please note that messages sent before expiration has been configured will not be tracked and expired.

### Disable Message Expiration
```
!expire unset
```
Disables message expiration for the current room. All tracked messages will be preserved. Messages previously marked for
expiration will remain, and messages sent will no longer be tracked for deletion.

### Show Current Settings
```
!expire show
```
Displays the current message expiration settings for the room.

## Permissions

Only users with permission to redact messages in a room can set message expiration times. This is determined by the
room's power levels, with redaction permission typically requiring a power level of 50 or higher.

## Supported Message Types

The bot will track and expire the following message types:
- Text messages
- Notices
- Emotes
- Files
- Images
- Videos 
- Stickers
- Location (untested)
