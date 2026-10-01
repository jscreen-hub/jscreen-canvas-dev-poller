"""Persistence for the JLab routine-lab-order queue.

A `CustomModel` (not an AttributeHub or Command Metadata) -- this is
structured, relational data with a stable schema that needs compound
filtering and sorting (all DEFERRED orders due today; a patient's whole
queue history), which is exactly the case the Canvas custom-data design
guide (docs.canvasmedical.com/sdk/custom-data-design-considerations/)
points at a CustomModel for. See DISCOVERY.md §6.

Per docs.canvasmedical.com/sdk/custom-data-custom-models/, this file must
live under a top-level `models/` directory for migrations to be generated
at all, and the proxy (`ModelExtension`) classes below give this plugin its
own private, unnamespaced `related_name`s on the shared SDK models
(docs.canvasmedical.com/sdk/custom-data-extending-sdk-models/).
"""

from __future__ import annotations

# `# type: ignore[var-annotated]` appears on every field assignment below.
# Plain mypy (without the full django-stubs mypy plugin, which needs an
# importable Django settings module this plugin project does not have --
# the Canvas SDK's own test harness configures Django programmatically
# rather than via one) cannot infer a type for a bare Django field
# descriptor assignment. This is the standard, low-risk way to silence that
# specific noise in a Django codebase that isn't running the stubs plugin;
# it does not suppress any other category of type error.
from django.db.models import (
    DO_NOTHING,
    DateField,
    DateTimeField,
    ForeignKey,
    Index,
    IntegerField,
    OneToOneField,
    TextField,
)

from canvas_sdk.v1.data import ModelExtension, Patient, Staff
from canvas_sdk.v1.data.base import CustomModel
from canvas_sdk.v1.data.command import Command


class CustomCommand(Command, ModelExtension):
    """Proxy giving this plugin its own handle on the shared Command table."""


class CustomPatient(Patient, ModelExtension):
    """Proxy giving this plugin its own handle on the shared Patient table."""


class CustomStaff(Staff, ModelExtension):
    """Proxy giving this plugin its own handle on the shared Staff table."""


class JLabOrderQueueEntry(CustomModel):
    """One row per routine JLab Lab Order command tracked by the queue.

    Keyed on the Command (`OneToOneField`, `primary_key=True`) rather than
    on `LabOrder` -- the Command id is stable from the moment the order is
    staged (`LAB_ORDER_COMMAND__POST_ORIGINATE`), which is before a
    committed `LabOrder` row necessarily exists. This also gives natural,
    database-enforced idempotency: processing the same command's event
    twice can never create a second row for it.
    """

    command = OneToOneField(  # type: ignore[var-annotated]
        CustomCommand,
        to_field="dbid",
        on_delete=DO_NOTHING,
        related_name="jlab_order_queue_entry",
        primary_key=True,
    )
    patient = ForeignKey(  # type: ignore[var-annotated]
        CustomPatient,
        to_field="dbid",
        on_delete=DO_NOTHING,
        related_name="jlab_order_queue_entries",
    )
    # Copied from Command.created at creation time for fast sorting/filtering
    # without a join, and because it must never change thereafter (spec:
    # "never alter ... original received timestamp") -- Command.created
    # itself is the source of truth and is never written by this plugin.
    received_at = DateTimeField()  # type: ignore[var-annotated]
    # Always "ROUTINE" today -- see logic/queue.py module docstring on
    # STAT/URGENT and DISCOVERY.md Open Question 1. Stored (rather than
    # hardcoded at read time) so the schema does not need to change if a
    # priority source is added later.
    priority = TextField(default="ROUTINE")  # type: ignore[var-annotated]
    # READY / DEFERRED / RELEASED / CANCELLED / SIGNED -- see logic/queue.py.
    queue_state = TextField(default="DEFERRED")  # type: ignore[var-annotated]
    eligible_date = DateField()  # type: ignore[var-annotated]
    released_at = DateTimeField(default=None)  # type: ignore[var-annotated]
    signed_at = DateTimeField(default=None)  # type: ignore[var-annotated]
    defer_reason = TextField(default="")  # type: ignore[var-annotated]
    # Which other queue entry (by its dbid, i.e. its own PK -- see `command`
    # above) this one is waiting behind. A plain scalar column, not a real
    # `ForeignKey("self", ...)`: Canvas's plugin-runner DDL generator cannot
    # resolve a self-referencing FK on a CustomModel at migration time --
    # confirmed on jlab-dev, where installing with that field as a real FK
    # failed with `ValueError: Related model 'self' cannot be resolved` in
    # `plugin_runner/ddl.py` (a different, later failure than anything the
    # local sandbox-load check or plain Django ORM usage catches -- this one
    # only surfaces from the real migration step on an actual instance). No
    # relational integrity/cascade is needed here anyway -- it's informative
    # only (the "Reason Deferred" UI column is driven by `defer_reason`,
    # which already carries a human-readable explanation).
    blocking_order_id = IntegerField(default=None)  # type: ignore[var-annotated]
    ordering_provider = ForeignKey(  # type: ignore[var-annotated]
        CustomStaff,
        to_field="dbid",
        on_delete=DO_NOTHING,
        related_name="jlab_order_queue_entries",
    )
    last_processed_at = DateTimeField(auto_now=True)  # type: ignore[var-annotated]

    class Meta:
        indexes = [
            # The CronTask's core query: due DEFERRED orders.
            Index(fields=["queue_state", "eligible_date"]),
            # The UI's and the real-time handler's core query: one
            # patient's whole queue, newest first.
            Index(fields=["patient", "-received_at"]),
        ]
