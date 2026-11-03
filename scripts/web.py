"""web — the polite HTTP client for public reference APIs (Wikipedia, Wikidata, Open Library, OpenAlex):
an identifying User-Agent, a short pause per call, Retry-After honoured, a host that keeps refusing
skipped for the rest of the pass. Used by the validation pass and by the show registry."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import settings  # noqa: F401  (importing it loads the project .env: PODNOTE_CONTACT)

# Wikimedia asks automated clients for contact details (and limits anonymous ones to 10 requests a minute)
CONTACT = os.environ.get("PODNOTE_CONTACT", "").strip()
AGENT = f"podnote-verify/1.0 ({CONTACT}) python-urllib/3" if CONTACT else "podnote-verify/1.0 python-urllib/3"
THROTTLED: set[str] = set()                               # hosts that kept answering 429 in this pass


def http_json(url: str, timeout: int = 15):
    """GET JSON politely: a short pause per call to other hosts (a pass stays far below Wikimedia's
    limit for a User-Agent with contact details); after a rate limit (Wikimedia answers 429 with
    Retry-After) wait exactly as asked when it is a minute or less, otherwise skip that host for the pass;
    after a network error 2 s; three tries. A lookup
    that still fails raises, and callers treat it as no evidence. A host still rate-limiting after
    three tries is skipped for the rest of the pass, so a throttled source cannot stall the queue."""
    host = urllib.parse.urlsplit(url).netloc
    if host in THROTTLED:
        raise urllib.error.URLError(f"{host} is rate-limiting this pass")
    request = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": "application/json"})
    for attempt in (1, 2, 3):
        if host.endswith("openlibrary.org"):
            time.sleep(0.35)                              # Open Library: 3 requests/s for identified clients
        elif not host.endswith(("wikipedia.org", "wikidata.org")):
            time.sleep(0.2)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if attempt == 3 and error.code in (429, 503):
                THROTTLED.add(host)
            if attempt == 3 or error.code not in (429, 503):
                raise
            wait = str((error.headers or {}).get("Retry-After", "5")).strip()
            if wait.isdigit():
                seconds = int(wait)
            else:                                         # an HTTP date
                try:
                    from email.utils import parsedate_to_datetime
                    seconds = max(0, int(parsedate_to_datetime(wait).timestamp() - time.time()))
                except (TypeError, ValueError):
                    seconds = 5
            if seconds > 60:                              # longer than a pass can wait: never ask again early
                THROTTLED.add(host)
                raise
            time.sleep(seconds)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt == 3:
                raise
            time.sleep(2)
