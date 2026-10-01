"""Client for the `lab_billing_lookup` plugin's signed lab-order feed.

The poller used to read FHIR `ServiceRequest`. It no longer does, because the
FHIR resource cannot supply the identifier downstream lab vendors ask for:
`ServiceRequest.id` and the Canvas `LabOrder.id` are separate identifier spaces
with no reliable join. Measured on this instance, 62% of committed
ServiceRequests matched more than one LabOrder on test codes alone (one matched
30), and the dates differ too -- a sample order was authored 09-04 in FHIR and
ordered 09-09 in Canvas.

Reading `LabOrder` through the plugin removes the guesswork entirely: the vendor's
id, the requisition number, the CPT codes and the ICD-10 diagnoses all come from
the same row.

"Signed" is Canvas's own commit flag (`LabOrder.objects.committed()` -- a
committer is set and the order is not entered-in-error), which is stricter than
the FHIR `status` heuristic this poller used to apply.
"""

from __future__ import annotations

from typing import Any

import httpx


class LabOrderSource:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        http: httpx.Client | None = None,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._api_key = api_key or ""
        self._http = http if http is not None else httpx.Client(timeout=120.0)

    @property
    def configured(self) -> bool:
        return bool(self._base_url and self._api_key)

    def signed_orders(self) -> list[dict[str, Any]]:
        """Every signed lab order, already in the poller's record shape.

        Raises on any failure. Unlike the billing poller's optional enrichment,
        this IS the data source -- a silent empty list here would look exactly
        like "no new orders" and quietly stall the feed.
        """
        if not self.configured:
            raise RuntimeError(
                "Lab order source is not configured: set BILLING_LOOKUP_URL and "
                "BILLING_LOOKUP_API_KEY"
            )
        response = self._http.get(
            f"{self._base_url}/lab-orders",
            headers={"Authorization": self._api_key, "Accept": "application/json"},
        )
        response.raise_for_status()
        body = response.json()
        orders = body.get("orders")
        if not isinstance(orders, list):
            raise RuntimeError(f"unexpected response from lab-orders: {body!r:.200}")
        return orders

    def close(self) -> None:
        self._http.close()
