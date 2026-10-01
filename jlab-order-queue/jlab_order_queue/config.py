"""Small shared constants and helpers used across handlers/apps/api.

Nothing here is Canvas-specific state -- just names that need to agree
across files (the Application identifier used for notification-badge
broadcasts must exactly match the manifest's `applications` class path) and
the one bit of environment parsing every handler needs.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

# Must match CANVAS_MANIFEST.json's `applications[].class` for the
# global-scope app exactly -- `ApplicationNotificationBadge` addresses an
# application by this string. See /sdk/effect-application-notification-badge/.
GLOBAL_APP_IDENTIFIER = "jlab_order_queue.apps.queue_app:GlobalQueueApp"
PATIENT_APP_IDENTIFIER = "jlab_order_queue.apps.queue_app:PatientQueueApp"

# Defensive fallback only -- every BaseHandler/CronTask is documented to
# expose `self.environment["INSTALLATION_TIME_ZONE"]` as a real IANA zone
# name (/sdk/handlers/). This is used solely if that key is ever missing or
# unparseable, so "fail safely" (Step 8) never means "crash on a bad clock".
FALLBACK_TIMEZONE = "UTC"


def instance_timezone(environment: dict[str, str]) -> ZoneInfo:
    """The instance's configured local timezone, per `self.environment`.

    Falls back to UTC (logging is the caller's responsibility) rather than
    raising, so a missing/bad environment value degrades to "treat every
    business date as UTC" instead of taking the whole handler down.
    """
    name = environment.get("INSTALLATION_TIME_ZONE") or FALLBACK_TIMEZONE
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(FALLBACK_TIMEZONE)
