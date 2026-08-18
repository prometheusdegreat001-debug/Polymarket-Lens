"""Shared HTTP helper - retry + exponential backoff for rate-limited APIs."""

import time
import requests
from config.loader import REQUEST_TIMEOUT_SECONDS, MAX_RETRIES, BACKOFF_BASE_SECONDS


class ApiError(Exception):
    pass


def get_json(url: str, params: dict = None, headers: dict = None):
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                time.sleep(BACKOFF_BASE_SECONDS ** (attempt + 1))
                continue
            if resp.status_code in (400, 401, 403, 422):
                # PERMANENT failures - bad request, missing/invalid
                # credentials, or forbidden. Retrying with the exact same
                # request will never succeed (an invalid API key doesn't
                # become valid by waiting), so this fails fast instead of
                # burning MAX_RETRIES worth of exponential backoff sleeps
                # (real wall-clock time during a live scan cycle) on a
                # request that was never going to work.
                raise ApiError(f"{resp.status_code} from {url}: {resp.text[:300]}")
            last_exc = ApiError(f"{resp.status_code} from {url}: {resp.text[:300]}")
            time.sleep(BACKOFF_BASE_SECONDS ** (attempt + 1))
        except requests.RequestException as e:
            last_exc = ApiError(f"Request failed for {url}: {e}")
            time.sleep(BACKOFF_BASE_SECONDS ** (attempt + 1))
    raise last_exc or ApiError(f"Failed to fetch {url} after {MAX_RETRIES} retries")
