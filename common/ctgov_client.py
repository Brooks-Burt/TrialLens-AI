"""
common/ctgov_client.py

The only code that talks to the CT.gov API v2. Used by pipeline/validate.py
(live, count-only checks of candidate terms) and pipeline/retrieve.py
(field-limited live search).

Parameter names verified against the official OpenAPI spec
(https://clinicaltrials.gov/api/oas/v2, API version 2.0.5) on 2026-10-29.

A thin, stateless wrapper with no domain logic and no Claude calls, so
retries, backoff and error handling are defined once.
"""

from __future__ import annotations

import re
import time

import requests

API_BASE = "https://clinicaltrials.gov/api/v2/studies"
REQUEST_TIMEOUT = 30
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2


class CTGovError(RuntimeError):
    """
    The registry could not be reached or rejected the request.

    A distinct type so callers can tell "the API failed" apart from "the API
    answered zero". v1 validation treated both as zero hits, which silently
    dropped real drugs on a timeout.
    """


# query.* values are parsed as Essie expressions, where ( ) [ ] " are syntax.
# A stray one -- or a real drug name like "[177Lu]Lu-PSMA-617" -- is an HTTP
# 400. Replacing them with spaces keeps normal (non-phrase) matching: tested
# 2026-10-29, "177Lu Lu-PSMA-617" -> 47 hits, quoted phrase -> 18, raw -> 400.
_ESSIE_SYNTAX = re.compile(r'[()\[\]"]')
MAX_TERM_CHARS = 200


def clean_term(term: str) -> str:
    """
    Make one drug/condition term safe to send. Used by every caller that
    builds a query.* value, so validation counts and retrieval searches see
    exactly the same string.

    Raises ValueError on absurdly long terms: a 5,000-character condition
    made CT.gov time out three times (~95 s) instead of failing fast.
    """
    cleaned = " ".join(_ESSIE_SYNTAX.sub(" ", term or "").split())
    if len(cleaned) > MAX_TERM_CHARS:
        raise ValueError(f"Search term is {len(cleaned)} characters; limit is {MAX_TERM_CHARS}: {cleaned[:60]}...")
    return cleaned


def _clean(params: dict) -> dict:
    # CT.gov answers an *empty* parameter (e.g. `filter.advanced=`) with HTTP
    # 500, not by ignoring it. Drop None/empty values before sending.
    return {k: v for k, v in params.items() if v not in (None, "", [])}


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
        params = _clean(params)
        last_error = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                response = requests.get(self.base_url, params=params, timeout=self.timeout)
                if 400 <= response.status_code < 500:
                    # Our request is malformed (e.g. a bad field name). Retrying
                    # cannot fix it, and CT.gov's body says exactly what is wrong.
                    raise CTGovError(f"CT.gov rejected the request ({response.status_code}): {response.text[:300]}")
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.retry_attempts:
                    wait = self.retry_backoff_seconds * attempt
                    print(f"  request failed ({exc}); retrying in {wait}s...")
                    time.sleep(wait)
        raise CTGovError(f"Failed to fetch page after {self.retry_attempts} attempts: {last_error}")

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

        # pageSize=0 is NOT "no results" -- CT.gov treats it as "use the
        # default of 10". 1 is the smallest real page.
        params = {"pageSize": 1, "countTotal": "true", "format": "json", "fields": "NCTId"}
        if intervention:
            params["query.intr"] = clean_term(intervention)
        if condition:
            params["query.cond"] = clean_term(condition)

        data = self._get(params)
        return data.get("totalCount", 0)

    def search(self, params: dict, fields: list[str], max_results: int) -> tuple[list[dict], int]:
        """
        One page of field-limited study records plus the registry's exact
        total match count. Returns (studies, total_count).

        `params` holds query.* / filter.* / sort keys as CT.gov names them.
        Only the first page is fetched: max_results is a deliberate cap (v3
        sends a bounded set to the summarizer), and CT.gov allows up to 1,000
        per page, so one page always covers it.
        """
        page_params = {
            **params,
            "format": "json",
            "fields": ",".join(fields),
            "countTotal": "true",
            "pageSize": max(1, min(max_results, 1000)),
        }
        data = self._get(page_params)
        return data.get("studies", []), data.get("totalCount", 0)
