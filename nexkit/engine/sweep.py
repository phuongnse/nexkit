"""The sweep: which of the items that may start do, and in what order, as
docs/engine.md#the-sweep says. A pure function of its arguments.

It trusts its items. Blockers, waves and holds are read from each item's state, and `step`
refuses a start that comes late or should not happen.
"""

from __future__ import annotations

from .machine import EngineError


def sweep(items, policies, running, limited):
    """`(starts, waits)` for `items`, listed oldest first, with `running` items already running.

    The order is the item's first `priority` label, then its milestone with none last, then its
    wave within the milestone, then its place in `items`. Starts stay within `maxParallel`,
    counting `running`, except an `awaited` item, which starts beyond it. An item on an account
    in `limited` never starts and takes no place."""
    seen = set()
    for item in items:
        if item["item"] in seen:
            raise EngineError(f"request.items names {item['item']} twice")
        seen.add(item["item"])
    priority = policies.get("priority") or []
    limit = policies.get("maxParallel")

    def key(pair):
        position, item = pair
        labels = set(item.get("labels") or [])
        rank = next((n for n, label in enumerate(priority) if label in labels), len(priority))
        milestone = item.get("milestone")
        wave = item.get("wave", 0) if milestone is not None else 0
        return (rank, milestone is None, milestone or 0, wave, position)

    starts, waits = [], []
    for _, item in sorted(enumerate(items), key=key):
        full = limit is not None and running >= limit
        if item.get("account") is not None and item["account"] in limited:
            waits.append({"item": item["item"], "for": "usageLimit", "running": running})
        elif full and not item.get("awaited", False):
            waits.append({"item": item["item"], "for": "place", "running": running})
        else:
            starts.append({"item": item["item"], "beyondLimit": full})
            running += 1
    return starts, waits
