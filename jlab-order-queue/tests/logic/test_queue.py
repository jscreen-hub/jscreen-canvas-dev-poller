"""Tests for the pure queue decision logic.

No Canvas SDK, no Django, no mocks -- these exercise `recompute_patient_queue`
and `business_date` directly, which is where the spec's actual business
rules live. Scenario numbers in test names/comments refer to the 16 required
scenarios in TEST_PLAN.md.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from jlab_order_queue.logic.queue import (
    CANCELLED,
    DEFERRED,
    NEW,
    READY,
    RELEASED,
    ROUTINE,
    SIGNED,
    STAT,
    URGENT,
    QueuedOrder,
    business_date,
    is_bypass_priority,
    recompute_patient_queue,
)

UTC = timezone.utc
# A real near-term Thursday, so the weekend test (#13) crosses an actual
# Fri/Sat/Sun without hardcoding unrelated historical dates.
TODAY = date(2026, 10, 1)


def qo(
    command_id: str,
    received_at: datetime,
    queue_state: str,
    eligible_date: date,
    priority: str = ROUTINE,
    patient_id: str = "pat-1",
) -> QueuedOrder:
    return QueuedOrder(
        command_id=command_id,
        patient_id=patient_id,
        received_at=received_at,
        queue_state=queue_state,
        eligible_date=eligible_date,
        priority=priority,
    )


def at(hour: int, minute: int = 0, day_offset: int = 0) -> datetime:
    return datetime(2026, 9, 29, hour, minute, tzinfo=UTC) + timedelta(days=day_offset)


def result_for(results, command_id):
    return next(r for r in results if r.command_id == command_id)


# -- Scenario 1: first routine order today -> READY -------------------------


def test_first_routine_order_today_is_ready():
    entries = [qo("cmd-1", at(9), NEW, TODAY)]

    results = recompute_patient_queue(entries, TODAY)

    r = result_for(results, "cmd-1")
    assert r.queue_state == READY
    assert r.eligible_date == TODAY
    assert r.became_available is True
    assert r.blocking_order_id is None


# -- Scenario 2: second routine order same patient same day -> deferred -----


def test_second_same_day_order_is_deferred_to_tomorrow():
    entries = [
        qo("cmd-1", at(9), READY, TODAY),  # already released today
        qo("cmd-2", at(10), NEW, TODAY),  # arriving now
    ]

    results = recompute_patient_queue(entries, TODAY)

    first = result_for(results, "cmd-1")
    second = result_for(results, "cmd-2")
    assert first.queue_state == READY  # untouched
    assert second.queue_state == DEFERRED
    assert second.eligible_date == TODAY + timedelta(days=1)
    assert second.became_available is False
    assert second.blocking_order_id == "cmd-1"
    assert "already released" in second.defer_reason


# -- Scenario 3: three routine orders -> three sequential dates -------------


def test_three_same_day_orders_get_three_sequential_dates():
    entries = [
        qo("cmd-1", at(8), NEW, TODAY),
        qo("cmd-2", at(9), NEW, TODAY),
        qo("cmd-3", at(10), NEW, TODAY),
    ]

    results = recompute_patient_queue(entries, TODAY)

    assert result_for(results, "cmd-1").eligible_date == TODAY
    assert result_for(results, "cmd-1").queue_state == READY
    assert result_for(results, "cmd-2").eligible_date == TODAY + timedelta(days=1)
    assert result_for(results, "cmd-2").queue_state == DEFERRED
    assert result_for(results, "cmd-3").eligible_date == TODAY + timedelta(days=2)
    assert result_for(results, "cmd-3").queue_state == DEFERRED
    # FIFO: oldest received wins the earliest date regardless of dict/list order.
    # Each deferred entry names whichever order is immediately ahead of it.
    assert result_for(results, "cmd-2").blocking_order_id == "cmd-1"
    assert result_for(results, "cmd-3").blocking_order_id == "cmd-2"


# -- Scenario 4: two different patients -> both READY today -----------------


def test_two_different_patients_are_independent():
    patient_a = [qo("cmd-a1", at(9), NEW, TODAY, patient_id="pat-a")]
    patient_b = [qo("cmd-b1", at(9), NEW, TODAY, patient_id="pat-b")]

    results_a = recompute_patient_queue(patient_a, TODAY)
    results_b = recompute_patient_queue(patient_b, TODAY)

    assert result_for(results_a, "cmd-a1").queue_state == READY
    assert result_for(results_b, "cmd-b1").queue_state == READY


# -- Scenarios 5 & 6: STAT / URGENT bypass -----------------------------------


def test_stat_order_bypasses_the_queue_even_with_a_routine_order_today():
    entries = [
        qo("cmd-1", at(9), READY, TODAY),
        qo("cmd-2", at(10), NEW, TODAY, priority=STAT),
    ]

    results = recompute_patient_queue(entries, TODAY)

    stat = result_for(results, "cmd-2")
    assert stat.queue_state == READY
    assert stat.eligible_date == TODAY
    assert stat.blocking_order_id is None


def test_urgent_order_bypasses_the_queue():
    entries = [
        qo("cmd-1", at(9), READY, TODAY),
        qo("cmd-2", at(10), NEW, TODAY, priority=URGENT),
    ]

    results = recompute_patient_queue(entries, TODAY)

    assert result_for(results, "cmd-2").queue_state == READY


def test_bypass_order_does_not_consume_the_routine_slot():
    """Spec: STAT/URGENT defaults to NOT consuming the patient's routine
    daily slot -- a routine order the same day must still be able to use it."""
    entries = [
        qo("cmd-stat", at(8), NEW, TODAY, priority=STAT),
        qo("cmd-routine", at(9), NEW, TODAY, priority=ROUTINE),
    ]

    results = recompute_patient_queue(entries, TODAY)

    assert result_for(results, "cmd-stat").queue_state == READY
    assert result_for(results, "cmd-routine").queue_state == READY
    assert result_for(results, "cmd-routine").eligible_date == TODAY


def test_is_bypass_priority_is_case_insensitive():
    assert is_bypass_priority("stat") is True
    assert is_bypass_priority("Urgent") is True
    assert is_bypass_priority("routine") is False
    assert is_bypass_priority("") is False


# -- Scenario 8: deferred order is cancelled, frees the slot -----------------


def test_cancelling_a_deferred_order_pulls_the_next_one_forward():
    # cmd-2 was deferred to tomorrow behind cmd-1; cmd-1 gets cancelled.
    entries = [
        qo("cmd-1", at(9), CANCELLED, TODAY),
        qo("cmd-2", at(10), DEFERRED, TODAY + timedelta(days=1)),
    ]

    results = recompute_patient_queue(entries, TODAY)

    cancelled = result_for(results, "cmd-1")
    promoted = result_for(results, "cmd-2")
    assert cancelled.queue_state == CANCELLED
    assert cancelled.eligible_date == TODAY  # never altered, per spec
    assert promoted.queue_state == RELEASED  # was deferred, now reaching its day
    assert promoted.eligible_date == TODAY
    assert promoted.became_available is True


# -- Scenario 9: a READY/RELEASED order is signed ----------------------------


def test_signed_order_keeps_its_day_and_is_never_reassigned():
    entries = [
        qo("cmd-1", at(9), SIGNED, TODAY),
        qo("cmd-2", at(10), NEW, TODAY),
    ]

    results = recompute_patient_queue(entries, TODAY)

    signed = result_for(results, "cmd-1")
    other = result_for(results, "cmd-2")
    assert signed.queue_state == SIGNED
    assert signed.eligible_date == TODAY
    assert signed.became_available is False
    # a signed order from today still occupies today's slot
    assert other.queue_state == DEFERRED
    assert other.eligible_date == TODAY + timedelta(days=1)


# -- Scenario 10: a deferred order reaches its eligible date -----------------


def test_deferred_order_is_released_once_its_eligible_date_arrives():
    tomorrow = TODAY + timedelta(days=1)
    entries = [qo("cmd-2", at(10), DEFERRED, tomorrow)]

    # "today" has rolled forward to what was tomorrow
    results = recompute_patient_queue(entries, tomorrow)

    r = result_for(results, "cmd-2")
    assert r.queue_state == RELEASED
    assert r.eligible_date == tomorrow
    assert r.became_available is True


def test_recompute_is_idempotent_for_an_already_released_order():
    """Running the recompute again on an unchanged RELEASED row must not
    re-fire `became_available` -- required for safe reprocessing/duplicate
    cron ticks (Step 8)."""
    released_day = TODAY
    entries = [qo("cmd-2", at(10), RELEASED, released_day)]

    results = recompute_patient_queue(entries, released_day)

    r = result_for(results, "cmd-2")
    assert r.queue_state == RELEASED
    assert r.became_available is False


# -- Scenario 11: a new order arrives while an older deferred order exists --


def test_new_order_queues_behind_an_existing_deferred_order_by_received_time():
    """Even if the new order's event is *processed* after the older one was
    already deferred, FIFO is decided by `received_at`, not processing order."""
    entries = [
        qo("cmd-locked", at(7), READY, TODAY),  # already holds today's slot
        qo("cmd-old", at(9), DEFERRED, TODAY + timedelta(days=1)),
        qo("cmd-new", at(14), NEW, TODAY),  # arrives later the same day
    ]

    results = recompute_patient_queue(entries, TODAY)

    old = result_for(results, "cmd-old")
    new = result_for(results, "cmd-new")
    assert old.eligible_date == TODAY + timedelta(days=1)
    assert old.blocking_order_id == "cmd-locked"
    assert new.eligible_date == TODAY + timedelta(days=2)
    assert new.blocking_order_id == "cmd-old"


def test_an_out_of_order_received_at_still_wins_the_earlier_slot():
    """A late-processed event for an order actually received earlier must
    still get the earlier eligible date than one received later -- proves
    FIFO is keyed on `received_at`, not on list/processing order."""
    entries = [
        qo("cmd-later", at(9), NEW, TODAY),  # processed first, received later
        qo("cmd-earlier", at(7), NEW, TODAY),  # processed second, received earlier
    ]

    results = recompute_patient_queue(entries, TODAY)

    assert result_for(results, "cmd-earlier").eligible_date == TODAY
    assert result_for(results, "cmd-earlier").queue_state == READY
    assert result_for(results, "cmd-later").eligible_date == TODAY + timedelta(days=1)


# -- Scenario 12: date rollover / timezone ------------------------------------


def test_business_date_uses_instance_timezone_not_utc():
    eastern = ZoneInfo("America/New_York")
    # 2026-10-01 02:30 UTC is still 2026-09-30 22:30 in New York.
    moment = datetime(2026, 10, 1, 2, 30, tzinfo=UTC)
    assert business_date(moment, eastern) == date(2026, 9, 30)


def test_business_date_rolls_over_at_local_midnight():
    eastern = ZoneInfo("America/New_York")
    just_before_midnight = datetime(2026, 10, 1, 3, 59, tzinfo=UTC)  # 23:59 EDT 9/30
    just_after_midnight = datetime(2026, 10, 1, 4, 1, tzinfo=UTC)  # 00:01 EDT 10/1
    assert business_date(just_before_midnight, eastern) == date(2026, 9, 30)
    assert business_date(just_after_midnight, eastern) == date(2026, 10, 1)


def test_date_rollover_makes_a_deferred_order_eligible_with_no_other_event():
    """A DEFERRED order's eligible_date simply becoming <= the new local
    business date (because the calendar turned over) is, by itself, enough
    for the next recompute pass to release it -- this is exactly what the
    CronTask's periodic sweep is for, since nothing else need happen."""
    entries = [qo("cmd-2", at(10), DEFERRED, TODAY)]

    results = recompute_patient_queue(entries, TODAY + timedelta(days=1))

    r = result_for(results, "cmd-2")
    assert r.queue_state == RELEASED
    assert r.eligible_date == TODAY + timedelta(days=1)


# -- Scenario 13: weekend behavior --------------------------------------------


def test_weekend_dates_are_assigned_like_any_other_calendar_day():
    """2026-10-01 is a Thursday; three same-day routine orders should land
    on Thu/Fri/Sat with no weekend-skipping -- the spec says 'calendar day',
    not 'business day', and nothing here assumes a weekend is special."""
    thursday = date(2026, 10, 1)
    entries = [
        qo("cmd-1", at(8), NEW, thursday),
        qo("cmd-2", at(9), NEW, thursday),
        qo("cmd-3", at(10), NEW, thursday),
    ]

    results = recompute_patient_queue(entries, thursday)

    assert result_for(results, "cmd-1").eligible_date == date(2026, 10, 1)  # Thu
    assert result_for(results, "cmd-2").eligible_date == date(2026, 10, 2)  # Fri
    assert result_for(results, "cmd-3").eligible_date == date(2026, 10, 3)  # Sat


# -- Recompute is idempotent and reprocessing-safe ---------------------------


def test_recompute_called_twice_with_the_same_input_is_identical():
    entries = [
        qo("cmd-1", at(8), NEW, TODAY),
        qo("cmd-2", at(9), NEW, TODAY),
    ]

    first_pass = recompute_patient_queue(entries, TODAY)
    second_pass = recompute_patient_queue(entries, TODAY)

    assert first_pass == second_pass


def test_cancelled_orders_are_excluded_entirely():
    entries = [qo("cmd-1", at(9), CANCELLED, TODAY)]

    results = recompute_patient_queue(entries, TODAY)

    r = result_for(results, "cmd-1")
    assert r.queue_state == CANCELLED
    assert r.became_available is False
