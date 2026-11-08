"""Terminal compatibility handlers for the retired General-to-Party relay."""

from chatter_db import mark_event


def maybe_queue_group_general_reaction(*args, **kwargs):
    """Suppress every producer, including inherited configurations."""
    return False


def process_group_general_reaction_event(db, client, config, event):
    """Consume pending legacy relay events without generation."""
    mark_event(db, event["id"], "skipped")
    return False
