"""Pure business logic for the JLab routine-lab-order queue.

No Canvas SDK or Django imports on purpose: every rule in the spec --
one routine JLab lab order per patient per local calendar day, FIFO,
STAT/URGENT bypass, idempotent reprocessing, cancellation/date-rollover
recalculation -- lives here as plain Python over plain dataclasses, so it
is fully unit-testable without a database, an event bus, or a mocked SDK.

`handlers/lab_order_queue.py` and `handlers/release_task.py` are thin
adapters: they translate Canvas events and `JLabOrderQueueEntry` rows into
the dataclasses below, call into this module, and translate the results
back into model writes and Command Validation effects. They contain no
decision logic of their own.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# Deliberately NOT using `from __future__ import annotations` in this file.
# Canvas's plugin sandbox executes each module via `exec()` into a scope
# that is never registered in `sys.modules` under the module's own name.
# Stdlib `dataclasses` is fine with that for ordinary live-object type
# annotations, but PEP 563 (postponed/string annotations, which
# `from __future__ import annotations` turns on everywhere in the file)
# makes `@dataclass` resolve field annotations by looking up
# `sys.modules[cls.__module__]` -- which is `None` in the sandbox, crashing
# with `AttributeError: 'NoneType' object has no attribute '__dict__'` at
# class-definition time. Confirmed by running this plugin's own handlers
# through `plugin_runner.sandbox_plugin_handlers` (the same loader
# `canvas validate`/`canvas install` use) locally; every other file in this
# plugin keeps the future import since none of them define a `@dataclass`.
# `X | Y` union syntax below works at runtime without it on Python 3.12+
# regardless, so nothing is lost by leaving it out here.

# -- queue states -------------------------------------------------------

READY = "READY"
DEFERRED = "DEFERRED"
RELEASED = "RELEASED"
CANCELLED = "CANCELLED"
SIGNED = "SIGNED"

# Not a persisted state -- a sentinel for "just originated, no decision made
# yet" used only when building the `QueuedOrder` passed in for a brand-new
# order. Distinguishing this from DEFERRED is what lets recompute tell "this
# is this order's very first day" (-> READY) from "this order was deferred
# before and is only now reaching its day" (-> RELEASED), per the spec's own
# distinction between those two states.
NEW = "NEW"

# States that still occupy a calendar-day slot and participate in FIFO
# recomputation (i.e. not yet resolved one way or the other).
PENDING_STATES = (DEFERRED, NEW)
# States that have already consumed a calendar-day slot for the patient --
# that slot is "spent" and not available to a newer order, regardless of
# whether the order has gone on to be signed.
LOCKED_STATES = (READY, RELEASED, SIGNED)
# Terminal states that never re-enter the queue and never block anything.
TERMINAL_STATES = (CANCELLED, SIGNED)

# -- priority -------------------------------------------------------------

# Lab Order commands carry no native priority field in the installed Canvas
# SDK (see DISCOVERY.md, Open Question 1). Per the confirmed decision, every
# JLab lab order is treated as ROUTINE today. The STAT/URGENT constants and
# the `is_bypass_priority` check are kept as live-but-currently-inert code:
# if this instance ever gains a way to carry a priority value (e.g. a future
# Command Metadata field), routing it through `priority` on `NewOrder` makes
# the bypass take effect with no change to this module.
STAT = "STAT"
URGENT = "URGENT"
ROUTINE = "ROUTINE"
BYPASS_PRIORITIES = (STAT, URGENT)


def is_bypass_priority(priority: str) -> bool:
    return priority.upper() in BYPASS_PRIORITIES


# -- inputs/outputs ---------------------------------------------------------


@dataclass(frozen=True)
class QueuedOrder:
    """One queue row, in the shape the pure logic needs.

    `command_id` is the Canvas Command id (stable from staging onward --
    see DISCOVERY.md §"Canvas model involved" for why this, not LabOrder, is
    the identity the queue is keyed on).
    """

    command_id: str
    patient_id: str
    received_at: datetime
    queue_state: str
    eligible_date: date
    priority: str = ROUTINE
    blocking_order_id: str | None = None
    defer_reason: str = ""


@dataclass(frozen=True)
class RecomputeResult:
    """What a recompute pass decided for one queued order."""

    command_id: str
    queue_state: str
    eligible_date: date
    blocking_order_id: str | None
    defer_reason: str
    became_available: bool  # True iff this call is what newly unblocked it


def business_date(now: datetime, tz: ZoneInfo) -> date:
    """The local calendar date `now` falls on, in the instance's timezone.

    `now` must be timezone-aware (UTC from Canvas event timestamps). This is
    the one and only place "what day is it" is decided, so every rule in
    this module agrees on the same notion of "today" -- deliberately using
    the Canvas instance's configured zone rather than UTC, per the spec.
    """
    return now.astimezone(tz).date()


def _first_available_date(start: date, taken: set[date]) -> date:
    """The earliest date >= start not already in `taken`."""
    candidate = start
    while candidate in taken:
        candidate += timedelta(days=1)
    return candidate


def recompute_patient_queue(
    entries: list[QueuedOrder], today: date
) -> list[RecomputeResult]:
    """Re-derive queue_state/eligible_date for every entry of one patient.

    This is the single source of truth for the one-per-calendar-day FIFO
    rule, and it is the only function that decides READY/DEFERRED/RELEASED.
    It is pure and idempotent: calling it twice with the same input (plus
    the same `today`) produces the same output, which is what makes
    reprocessing and duplicate events safe (Step 8's "idempotent" and
    Step 2.8's "reprocessing" requirements).

    Algorithm:
      1. Entries already LOCKED (READY/RELEASED/SIGNED) or CANCELLED keep
         their current eligible_date/state untouched -- a day already spent
         or a row no longer in play is never reassigned.
      2. Every other (DEFERRED, or a brand-new order being evaluated for the
         first time) entry is sorted oldest-received-first -- FIFO -- and
         each is assigned the earliest date >= today not already spent by a
         LOCKED entry for this same patient.
      3. An entry whose assigned date == today is promoted to READY (if it
         was never available before) or RELEASED (if it was previously
         DEFERRED and is only now reaching its day) -- "became_available"
         marks exactly this transition, which is what the caller uses to
         decide whether to flip the Canvas sign block off and stamp
         `released_at`/fire a notification-badge update.
      4. STAT/URGENT entries never occupy a slot and are always available;
         see `is_bypass_priority`.

    Cancelled orders free their day immediately (they are simply excluded
    from the "taken" set) -- this is also how "recalculate later queued
    orders" (Step 4.5) and cancellation (Step 2.8) are satisfied: the next
    call to this function with the cancelled row removed/marked CANCELLED
    pulls every later DEFERRED row forward by however many days opened up.
    """
    results: list[RecomputeResult] = []

    taken: set[date] = set()
    locked_or_terminal: list[QueuedOrder] = []
    pending: list[QueuedOrder] = []

    for entry in entries:
        if is_bypass_priority(entry.priority):
            # STAT/URGENT never consumes a routine slot and is always
            # available -- bypass the queue entirely (Step 6 of the spec).
            results.append(
                RecomputeResult(
                    command_id=entry.command_id,
                    queue_state=(
                        entry.queue_state
                        if entry.queue_state in TERMINAL_STATES
                        else READY
                    ),
                    eligible_date=(
                        entry.eligible_date
                        if entry.queue_state in TERMINAL_STATES
                        else today
                    ),
                    blocking_order_id=None,
                    defer_reason="",
                    became_available=entry.queue_state in (DEFERRED, NEW),
                )
            )
            continue
        if entry.queue_state == CANCELLED:
            # Frees its day -- excluded from `taken` -- but still reported so
            # the caller can confirm it (e.g. a defensive re-check after a
            # delete) rather than it silently vanishing from the results.
            results.append(
                RecomputeResult(
                    command_id=entry.command_id,
                    queue_state=CANCELLED,
                    eligible_date=entry.eligible_date,
                    blocking_order_id=None,
                    defer_reason=entry.defer_reason,
                    became_available=False,
                )
            )
            continue
        if entry.queue_state in LOCKED_STATES:
            locked_or_terminal.append(entry)
            taken.add(entry.eligible_date)
            results.append(
                RecomputeResult(
                    command_id=entry.command_id,
                    queue_state=entry.queue_state,
                    eligible_date=entry.eligible_date,
                    blocking_order_id=entry.blocking_order_id,
                    defer_reason=entry.defer_reason,
                    became_available=False,
                )
            )
            continue
        pending.append(entry)

    # FIFO: oldest `received_at` first. Ties broken by command_id so the
    # ordering is deterministic (and therefore idempotent) even if two
    # orders share a timestamp.
    pending.sort(key=lambda e: (e.received_at, e.command_id))

    # Tracks who holds each date, so a deferred entry can name exactly which
    # order it is queued behind -- the locked entry on the previous day, or
    # (once we start assigning them below) an earlier pending entry in this
    # same FIFO chain.
    occupant_by_date: dict[date, str] = {
        e.eligible_date: e.command_id for e in locked_or_terminal
    }

    for entry in pending:
        assigned = _first_available_date(today, taken)
        taken.add(assigned)
        was_deferred = entry.queue_state == DEFERRED
        if assigned <= today:
            new_state = RELEASED if was_deferred else READY
            became_available = True
            reason = ""
            this_blocking_id = None
        else:
            new_state = DEFERRED
            became_available = False
            this_blocking_id = occupant_by_date.get(assigned - timedelta(days=1))
            reason = (
                "Another routine JLab lab order for this patient was "
                f"already released today; this order becomes eligible on "
                f"{assigned.isoformat()}."
            )
        occupant_by_date[assigned] = entry.command_id
        results.append(
            RecomputeResult(
                command_id=entry.command_id,
                queue_state=new_state,
                eligible_date=assigned,
                blocking_order_id=this_blocking_id,
                defer_reason=reason,
                became_available=became_available,
            )
        )

    return results
