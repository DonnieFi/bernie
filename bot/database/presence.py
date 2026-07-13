"""database.presence — presence + last_home_signal (family-bot-8lx.6).

Path ownership:
- **Poll path (canonical):** ``presence_service`` → ``apply_presence_tick`` —
  one transaction for the whole family tick; may set ``last_home_signal`` on
  each row via ``set_last_home_signal`` field on each update dict.
- **Manual/API path:** ``update_presence`` / ``set_last_home_signal`` for
  single-person mutations (slash/tools/home API). When marking someone home
  via ``update_presence``, pass ``touch_home_signal=True`` (or set the signal
  explicitly) so first-insert semantics match the poll path.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone as dt_timezone

from database.conn import _db_conn, _db_read

log = logging.getLogger("database.presence")


async def get_last_home_signal(person_id: str) -> float:
    async with _db_read() as db:
        cur = await db.execute("SELECT last_home_signal FROM presence_current WHERE person_id=?", (person_id,))
        row = await cur.fetchone()
        return row[0] if row and row[0] is not None else 0.0


async def get_last_home_signals(person_ids: list[str]) -> dict[str, float]:
    """Batch read last_home_signal for many people (family-bot-ah5.4)."""
    if not person_ids:
        return {}
    ids = list(dict.fromkeys(person_ids))
    placeholders = ",".join("?" * len(ids))
    out = {pid: 0.0 for pid in ids}
    async with _db_read() as db:
        async with db.execute(
            f"SELECT person_id, last_home_signal FROM presence_current WHERE person_id IN ({placeholders})",
            ids,
        ) as cur:
            async for row in cur:
                pid, ts = row[0], row[1]
                out[str(pid)] = float(ts) if ts is not None else 0.0
    return out


async def set_last_home_signal(person_id: str, ts: float):
    async with _db_conn() as db:
        await db.execute(
            "UPDATE presence_current SET last_home_signal=? WHERE person_id=?",
            (ts, person_id)
        )
        await db.commit()


async def update_presence(
    person_id: str,
    is_home: bool,
    device_mac: str = None,
    *,
    touch_home_signal: bool | None = None,
    home_signal_ts: float | None = None,
):
    """Single-person presence write (API/slash). Poll path uses apply_presence_tick.

    When *is_home* and *touch_home_signal* is True (default when home), also set
    ``last_home_signal`` so grace-period logic matches the poll path.
    """
    now = datetime.now(dt_timezone.utc).isoformat()
    if touch_home_signal is None:
        touch_home_signal = bool(is_home)
    signal_ts = home_signal_ts if home_signal_ts is not None else time.time()
    async with _db_conn() as db:
        cur = await db.execute("SELECT is_home FROM presence_current WHERE person_id=?", (person_id,))
        row = await cur.fetchone()

        changed = False
        if row is None:
            changed = True
            await db.execute(
                """INSERT INTO presence_current
                   (person_id, is_home, last_seen, last_arrived, last_departed, last_home_signal)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    person_id, int(is_home), now,
                    now if is_home else None,
                    None if is_home else now,
                    float(signal_ts) if (is_home and touch_home_signal) else None,
                ),
            )
        else:
            current_is_home = bool(row[0])
            if current_is_home != is_home:
                changed = True
                if is_home:
                    await db.execute(
                        "UPDATE presence_current SET is_home=?, last_seen=?, last_arrived=? WHERE person_id=?",
                        (int(is_home), now, now, person_id)
                    )
                else:
                    await db.execute(
                        "UPDATE presence_current SET is_home=?, last_departed=? WHERE person_id=?",
                        (int(is_home), now, person_id)
                    )
            elif is_home:
                await db.execute(
                    "UPDATE presence_current SET last_seen=? WHERE person_id=?",
                    (now, person_id)
                )
            if is_home and touch_home_signal:
                await db.execute(
                    "UPDATE presence_current SET last_home_signal=? WHERE person_id=?",
                    (float(signal_ts), person_id),
                )

        if changed:
            await db.execute(
                "INSERT INTO presence_log (person_id, event, device_mac) VALUES (?, ?, ?)",
                (person_id, "arrived" if is_home else "departed", device_mac)
            )

        await db.commit()
        return changed


async def apply_presence_tick(
    updates: list[dict],
) -> list[tuple[str, bool]]:
    """Canonical poll-path writer — one transaction for the whole family tick (ah5.4).

    Each *updates* item: person_id, is_home, device_mac?, set_last_home_signal? (float ts).
    Returns [(person_id, changed), ...]. Single-person API mutations use update_presence.
    """
    if not updates:
        return []
    now = datetime.now(dt_timezone.utc).isoformat()
    results: list[tuple[str, bool]] = []
    async with _db_conn() as db:
        for u in updates:
            person_id = u["person_id"]
            is_home = bool(u["is_home"])
            device_mac = u.get("device_mac")
            signal_ts = u.get("set_last_home_signal")
            if signal_ts is not None:
                await db.execute(
                    "UPDATE presence_current SET last_home_signal=? WHERE person_id=?",
                    (float(signal_ts), person_id),
                )
            cur = await db.execute(
                "SELECT is_home FROM presence_current WHERE person_id=?", (person_id,)
            )
            row = await cur.fetchone()
            changed = False
            if row is None:
                changed = True
                await db.execute(
                    """INSERT INTO presence_current
                       (person_id, is_home, last_seen, last_arrived, last_departed, last_home_signal)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        person_id, int(is_home), now,
                        now if is_home else None,
                        None if is_home else now,
                        float(signal_ts) if signal_ts is not None else None,
                    ),
                )
            else:
                current_is_home = bool(row[0])
                if current_is_home != is_home:
                    changed = True
                    if is_home:
                        await db.execute(
                            "UPDATE presence_current SET is_home=?, last_seen=?, last_arrived=? WHERE person_id=?",
                            (int(is_home), now, now, person_id),
                        )
                    else:
                        await db.execute(
                            "UPDATE presence_current SET is_home=?, last_departed=? WHERE person_id=?",
                            (int(is_home), now, person_id),
                        )
                elif is_home:
                    await db.execute(
                        "UPDATE presence_current SET last_seen=? WHERE person_id=?",
                        (now, person_id),
                    )
            if changed:
                await db.execute(
                    "INSERT INTO presence_log (person_id, event, device_mac) VALUES (?, ?, ?)",
                    (person_id, "arrived" if is_home else "departed", device_mac),
                )
            results.append((person_id, changed))
        await db.commit()
    return results


async def get_presence():
    async with _db_read() as db:
        cur = await db.execute("SELECT person_id, is_home, last_seen FROM presence_current")
        rows = await cur.fetchall()
        return {r[0]: {"is_home": bool(r[1]), "last_seen": r[2]} for r in rows}
