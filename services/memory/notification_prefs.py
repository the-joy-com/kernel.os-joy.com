"""Notification preferences: which channels the symbiot lets the kernel reach them on.

Every channel is on by default —
a symbiot who has never touched this is reachable everywhere,
which is what "double up first delivery" wants.
So the store holds only the exceptions:
a row exists for a channel the symbiot has taken a position on,
carrying whether it's enabled.
Absence means the default (enabled),
which is why disabled_channels asks only for the ones explicitly switched off.

This is the durable half of the /notifications command (see the route in main.py).
The dispatcher reads disabled_channels before every fan-out (services/loop/notify.py)
so a globally disabled channel is never fired,
no matter who asked for it or how;
the route reads preferences to show the symbiot the full picture,
and writes through set_channel when they flip one.
The table is keyed on (symbiot_id, channel),
so the write is a plain upsert
and a symbiot only ever has one standing position per channel.
"""


def disabled_channels(conn, symbiot_id: int) -> set[str]:
    """The channel slugs this symbiot has globally switched off — the set the dispatcher skips.

    Only the explicitly-disabled rows:
    a channel with no row, or a row left enabled, is reachable and simply isn't here.
    Returned as a set because the dispatcher only ever asks "is this one off?", never order.
    """
    rows = conn.execute(
        "SELECT channel FROM notification_preference WHERE symbiot_id = %s AND NOT enabled",
        (symbiot_id,),
    ).fetchall()
    return {row[0] for row in rows}


def preferences(conn, symbiot_id: int, channels: tuple[str, ...]) -> dict[str, bool]:
    """Every channel that exists, mapped to whether this symbiot has it enabled — the /notifications view.

    Built against the full channel set the caller passes (notify.ALL_CHANNELS),
    so a channel the symbiot has never touched shows as enabled (its default)
    rather than being absent —
    the shell renders one toggle per real channel, on unless the symbiot turned it off.
    """
    disabled = disabled_channels(conn, symbiot_id)
    return {channel: channel not in disabled for channel in channels}


def set_channel(conn, symbiot_id: int, channel: str, enabled: bool) -> None:
    """Record the symbiot's standing position on one channel — the write behind /notifications.

    A plain upsert on (symbiot_id, channel):
    flipping a channel again overwrites the one row in place rather than piling up positions,
    so the newest choice is the only one that stands.
    Setting a channel back to enabled keeps the row (now carrying true),
    which reads identically to having no row at all —
    either way disabled_channels won't return it and the dispatcher will fire it.
    """
    conn.execute(
        "INSERT INTO notification_preference (symbiot_id, channel, enabled) VALUES (%s, %s, %s) "
        "ON CONFLICT (symbiot_id, channel) DO UPDATE SET enabled = EXCLUDED.enabled",
        (symbiot_id, channel, enabled),
    )
