"""Deliver an export payload to its two destinations.

Both deliveries are best-effort and isolated: a failure in one (or an exception
raised by the transport / filesystem) is logged and swallowed so the handler
never crashes and never loses the other output. The external API is the
authoritative destination; the NDJSON file is a local audit/fallback record and
may be ephemeral across plugin deploys/restarts.
"""

import json
from typing import Any

from canvas_sdk.utils import Http
from logger import log


def post_to_api(url: str | None, token: str | None, payload: dict[str, Any]) -> bool:
    """POST the payload as JSON to the configured external API.

    Args:
        url: destination endpoint (from the ORDER_EXPORT_API_URL secret).
        token: bearer token (from the ORDER_EXPORT_API_TOKEN secret); optional.
        payload: JSON-serializable order payload.

    Returns:
        True if the request succeeded (2xx), False otherwise.
    """
    if not url:
        log.warning("[lab-order-export] ORDER_EXPORT_API_URL not set; skipping API POST")
        return False

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = Http().post(url, json=payload, headers=headers)
    except Exception as exc:  # noqa: BLE001 - never let a transport error crash the handler
        log.error(f"[lab-order-export] API POST raised an exception: {exc}")
        return False

    if response.ok:
        log.info("[lab-order-export] order exported to API successfully")
        return True

    log.error(
        f"[lab-order-export] API POST failed with status {response.status_code}"
    )
    return False


def append_to_file(path: str | None, payload: dict[str, Any]) -> bool:
    """Append the payload as one NDJSON line to the local audit file.

    Args:
        path: destination file path (from the ORDER_EXPORT_FILE_PATH secret).
        payload: JSON-serializable order payload.

    Returns:
        True if the line was written, False otherwise.
    """
    if not path:
        log.warning(
            "[lab-order-export] ORDER_EXPORT_FILE_PATH not set; skipping file append"
        )
        return False

    try:
        line = json.dumps(payload)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 - file access is best-effort/ephemeral
        log.error(f"[lab-order-export] file append failed: {exc}")
        return False

    log.info("[lab-order-export] order appended to audit file")
    return True
