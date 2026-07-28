"""
common/ctgov_client.py

Single shared client for talking to the CT.gov API v2. Used by:
  - ingest/pull_trials.py    -- bulk, paged pulls of full study records
  - enrich/query_planner.py  -- live, count-only validation of candidate terms

This lives outside both ingest/ and enrich/ on purpose. The project's rule
is that domain modules don't import each other -- data/ is the only
interface between them. A thin, stateless API wrapper with no domain
logic (no query_config.json awareness, no JSON-to-disk writing, no
Claude calls) isn't domain logic, so both modules importing *this* is
not the same as ingest and enrich importing each other.
"""

from __future__ import annotations

import time

import requests

API_BASE = "https://clinicaltrials.gov/api/v2/studies"
REQUEST_TIMEOUT = 30
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2


class CTGovClient:
    def __init__(
        self,
        base_url: str = API_BASE,
        timeout: int = REQUEST_TIMEOUT,
        retry_attempts: int = RETRY_ATTEMPTS,
        retry_backoff_seconds: int = RETRY_BACKOFF_SECONDS,
    ):
        self.base_url = base_url
        self.timeout = timeout
        self.retry_attempts = retry_attempts
        self.retry_backoff_seconds = retry_backoff_seconds

    def _get(self, params: dict) -> dict:
        last_error = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                response = requests.get(self.base_url, params=params, timeout=self.timeout)
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.retry_attempts:
                    wait = self.retry_backoff_seconds * attempt
                    print(f"  request failed ({exc}); retrying in {wait}s...")
                    time.sleep(wait)
        raise RuntimeError(f"Failed to fetch page after {self.retry_attempts} attempts: {last_error}")

    def fetch_page(self, params: dict) -> dict:
        """Full page of study records, for ingest's bulk pulls."""
        page_params = {"format": "json", **params}
        return self._get(page_params)

    def count(self, intervention: str | None = None, condition: str | None = None) -> int:
        """
        Live, count-only lookup -- no study bodies returned.

        pageSize=1 + countTotal=true asks CT.gov for a total match count
        without paging through results. This is the cheapest possible
        call shape, which matters because query_planner calls it once per
        candidate term, on every query, with no caching by design: term
        existence can change over time, and validation is meant to
        reflect the registry as it is right now, not a stale local copy.
        """
        if not intervention and not condition:
            raise ValueError("count() requires intervention and/or condition")

        params = {"pageSize": 1, "countTotal": "true", "format": "json"}
        if intervention:
            params["query.intr"] = intervention
        if condition:
            params["query.cond"] = condition

        data = self._get(params)
        return data.get("totalCount", 0)
