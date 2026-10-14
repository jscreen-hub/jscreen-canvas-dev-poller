"""Pure business logic for the JLab routine-lab-order queue.

No Canvas SDK or Django imports on purpose: every rule in the spec --
one routine JLab lab order per patient per local calendar day, FIFO,
STAT/URGENT bypass, idempotent reprocessing, cancellation/date-rollover
recalculation, and (the latest rule) never letting the *next* order
actually become available until one calendar day after the *previous* one
was actually **signed** -- lives here as plain Python over plain
dataclasses, so it is fully unit-testable without a database, an event bus,
or a mocked SDK.

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

    `signed_date` is the local calendar date the order was actually signed,
    already converted via `business_date`/the instance timezone by the
    caller (this module stays timezone-agnostic, same as `today`) -- `None`
    unless `queue_state == SIGNED`. It is what gates the *next* order in
    this patient's FIFO chain from actually becoming available (see
    `recompute_patient_queue`), independent of the calendar-slot
    `eligible_date` projection below.
    """

    command_id: str
    patient_id: str
    received_at: datetime
    queue_state: str
    eligible_date: date
    priority: str = ROUTINE
    blocking_order_id: str | None = None
    defer_reason: str = ""
    signed_date: date | None = None


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


def _signing_gate_date(predecessor: "QueuedOrder | None") -> date | None:
    """The earliest date `predecessor` allows the *next* order to become
    available, or `None` if `predecessor` hasn't been signed yet (in which
    case the next order cannot become available at any date yet).

    `predecessor is None` (nothing ahead of this order in the FIFO chain)
    imposes no signing gate at all -- represented by returning `date.min`
    (handled by the caller) rather than `None`, so "no predecessor" and
    "predecessor not yet signed" stay distinguishable.
    """
    if predecessor is None:
        return date.min
    if predecessor.queue_state != SIGNED or predecessor.signed_date is None:
        return None
    return predecessor.signed_date + timedelta(days=1)


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

    Two gates both have to pass before a pending order can actually become
    available, and `eligible_date` always reflects the first (the original
    calendar-slot projection), regardless of whether the second currently
    blocks it:

      1. **Calendar slot** (the original rule): entries already LOCKED
         (READY/RELEASED/SIGNED) or CANCELLED keep their current
         eligible_date/state untouched -- a day already spent or a row no
         longer in play is never reassigned. Every other (DEFERRED, or a
         brand-new order being evaluated for the first time) entry is
         sorted oldest-received-first -- FIFO -- and each is assigned the
         earliest date >= today not already spent by a LOCKED entry for
         this same patient. This is purely a projection ("if signed
         promptly, this is the earliest day it could run") and is computed
         exactly as before, independent of anyone's actual signing.
      2. **Signing gate** (the newer rule): on top of its calendar slot
         having arrived, an entry also cannot become available until one
         calendar day after the order immediately ahead of it in the FIFO
         chain was *actually signed* -- an available-but-unsigned
         predecessor does not start this clock, so the next order stays
         DEFERRED (Sign button still blocked) even once its calendar slot
         has passed, until the physician actually signs the one ahead of
         it. This is what guarantees "signed on different days" -- an
         order signed today can never let the next one become available
         before tomorrow, however far in the past its calendar slot is.

    An entry whose calendar slot has arrived AND whose signing gate has
    passed is promoted to READY (if it was never available before) or
    RELEASED (if it was previously DEFERRED) -- `became_available` marks
    exactly this transition, which is what the caller uses to decide
    whether to flip the Canvas sign block off and stamp `released_at`/fire
    a notification-badge update. STAT/URGENT entries never occupy a slot or
    a chain position and are always available; see `is_bypass_priority`.

    Cancelled orders free their day and their chain position immediately
    (excluded from both `taken` and the FIFO chain used for the signing
    gate) -- this is also how "recalculate later queued orders" (Step 4.5)
    and cancellation (Step 2.8) are satisfied: the next call to this
    function with the cancelled row removed/marked CANCELLED pulls every
    later DEFERRED row forward by however many days (and chain positions)
    opened up.
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
            # Frees its day and chain position -- excluded from `taken` and
            # from the FIFO chain -- but still reported so the caller can
            # confirm it (e.g. a defensive re-check after a delete) rather
            # than it silently vanishing from the results.
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

    # The full FIFO chain (locked + still-pending, excluding bypass/
    # cancelled), used only to find each entry's immediately-preceding
    # order for the signing gate -- a distinct concept from which calendar
    # date that predecessor happens to occupy, so it stays correct even
    # across a gap opened up by an earlier cancellation.
    chain = sorted(
        locked_or_terminal + pending, key=lambda e: (e.received_at, e.command_id)
    )
    predecessor_by_id: dict[str, QueuedOrder | None] = {}
    previous: QueuedOrder | None = None
    for e in chain:
        predecessor_by_id[e.command_id] = previous
        previous = e

    # FIFO: oldest `received_at` first. Ties broken by command_id so the
    # ordering is deterministic (and therefore idempotent) even if two
    # orders share a timestamp.
    pending.sort(key=lambda e: (e.received_at, e.command_id))

    for entry in pending:
        assigned = _first_available_date(today, taken)
        taken.add(assigned)
        was_deferred = entry.queue_state == DEFERRED

        predecessor = predecessor_by_id.get(entry.command_id)
        gate_date = _signing_gate_date(predecessor)
        signing_gate_passed = gate_date is not None and gate_date <= today
        calendar_slot_arrived = assigned <= today

        if calendar_slot_arrived and signing_gate_passed:
            new_state = RELEASED if was_deferred else READY
            became_available = True
            reason = ""
            this_blocking_id = None
        else:
            new_state = DEFERRED
            became_available = False
            this_blocking_id = predecessor.command_id if predecessor else None
            reason_parts = []
            if not calendar_slot_arrived:
                reason_parts.append(
                    "Another routine JLab lab order for this patient was "
                    "already released today; this order becomes eligible "
                    f"on {assigned.isoformat()}."
                )
            if gate_date is None:
                reason_parts.append(
                    "Another routine JLab lab order for this patient must "
                    "be signed before this one can become available."
                )
            elif not signing_gate_passed and predecessor is not None:
                reason_parts.append(
                    "Another routine JLab lab order for this patient was "
                    f"signed on {predecessor.signed_date.isoformat()}; this "  # type: ignore[union-attr]
                    f"order cannot become available until {gate_date.isoformat()}."
                )
            reason = " ".join(reason_parts)
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
