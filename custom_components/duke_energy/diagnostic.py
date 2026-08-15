"""Temporary privacy-safe Duke API response diagnostic."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from aiodukeenergy import DukeEnergy

_DATE_KEY = re.compile(
    r"(?ix)(billing|cycle|meter.*read|next.*read|previous.*read|"
    r"period.*start|period.*end|start.*period|end.*period)"
)
_DATE_VALUE = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+\-Z]+)?|\d{1,2}/\d{1,2}/\d{4})$"
)
_SAFE_KEY = re.compile(r"^[A-Za-z_][A-Za-z_-]{0,63}$")


class SanitizedDiagnosticDukeEnergy(DukeEnergy):
    """Capture response shapes without retaining private values."""

    def __init__(self, auth: Any) -> None:
        """Initialize the diagnostic client."""
        super().__init__(auth)
        self._diagnostic: dict[str, set[str]] = {
            "account-details-v2": set(),
            "usage-graph-billingcycle": set(),
        }

    async def _get_json(
        self, url: Any, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Fetch JSON without upstream raw-payload debug logging."""
        response = await self._auth.request("GET", url, params=params or {})
        response.raise_for_status()
        result = await response.json()
        if str(url).rstrip("/").endswith("account-details-v2"):
            self._capture("account-details-v2", result)
        return result

    async def _post_json(
        self, url: Any, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Post JSON without upstream raw-payload debug logging."""
        response = await self._auth.request("POST", url, json=body or {})
        response.raise_for_status()
        result = await response.json()
        if (
            str(url).rstrip("/").endswith("account/usage/graph")
            and body is not None
            and body.get("periodType") == "BILLINGCYCLE"
        ):
            self._capture("usage-graph-billingcycle", result)
        return result

    def sanitized_diagnostic(self) -> dict[str, list[str]]:
        """Return the collected sanitized response schemas."""
        return {
            endpoint: sorted(records) for endpoint, records in self._diagnostic.items()
        }

    def _capture(self, endpoint: str, value: Any) -> None:
        """Collect only key paths, primitive types, and allowlisted dates."""
        self._walk(endpoint, "$", None, value)

    def _walk(
        self,
        endpoint: str,
        path: str,
        key: str | None,
        value: Any,
    ) -> None:
        """Walk a response while discarding all non-allowlisted values."""
        records = self._diagnostic[endpoint]
        if isinstance(value, Mapping):
            records.add(f"{path}: object")
            for raw_key, child in value.items():
                key_text = str(raw_key)
                safe_key = (
                    key_text if _SAFE_KEY.fullmatch(key_text) else "<dynamic-key>"
                )
                self._walk(endpoint, f"{path}.{safe_key}", key_text, child)
            return
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            records.add(f"{path}: array")
            for child in value:
                self._walk(endpoint, f"{path}[]", key, child)
            return

        primitive_type = self._primitive_type(value)
        record = f"{path}: {primitive_type}"
        if (
            isinstance(value, str)
            and key is not None
            and _DATE_KEY.search(key)
            and _DATE_VALUE.fullmatch(value)
        ):
            record = f"{record} = {value}"
        records.add(record)

    @staticmethod
    def _primitive_type(value: Any) -> str:
        """Return a JSON-oriented primitive type name."""
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "number"
        if isinstance(value, str):
            return "string"
        return "unknown"
