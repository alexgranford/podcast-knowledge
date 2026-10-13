#!/usr/bin/env python3
"""podverify — one external validation pass over a finished podnote note.

    podverify.py <note.md>          verify one note in place; prints "Verified: <n> changes, <c> claims"

Checks everything checkable against outside sources, never against the model's own knowledge:
- names of people and organisations (Participants, Summary, Key themes): respelled only;
- every work under References: what it is (Wikidata "instance of"), its proper title and author,
  and for books a link to the Open Library work page (its editions carry the shop links);
- every quote: against the transcript (replaced by the words as said when close, removed otherwise);
- up to six checkable claims (statistics, dated events, who-did-what), with evidence and links from
  Wikipedia full-text search, OpenAlex (research papers and reports) and Brave Search.

Sources decide, the model only reads. One model session per note: an inventory call (work types,
clean titles, the claims worth checking), a names-and-works verdict call and a claims verdict call,
each shown only the evidence gathered. Python guards every edit: a name only to a spelling variant,
a title only to a variant of the same title, a work removed only when Wikidata or Wikipedia says the
same title is not a work. Web text is data, never instructions. A failed source never fails the
pass; an item without evidence stays as it is and is never marked.

The note gets: References grouped by type (Books as a table with Open Library links, Films & TV,
Papers & reports, Laws, Other works, Quotes as callouts), a "Claims checked" section with source
footnotes and a collapsed verification log; what the pass did goes to the job's verify-record.json.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import settings  # noqa: E402,F401  (importing it loads the project .env)
import podnote  # noqa: E402

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
OPEN_LIBRARY = "https://openlibrary.org/search.json"
OPENALEX = "https://api.openalex.org/works"
# Wikimedia asks automated clients for contact details (and limits anonymous ones to 10 requests a minute)
CONTACT = os.environ.get("PODNOTE_CONTACT", "").strip()
AGENT = f"podnote-verify/1.0 ({CONTACT}) python-urllib/3" if CONTACT else "podnote-verify/1.0 python-urllib/3"
MAX_NAMES, MAX_CLAIMS = 30, 6
NOT_NAMES = {"the", "a", "an", "in", "on", "at", "of", "and", "but", "or", "if", "while", "as", "this", "that",
             "these", "those", "it", "its", "he", "she", "they", "we", "his", "her", "their", "our", "for", "with",
             "from", "by", "to", "after", "before", "during", "since", "when", "where", "what", "why", "how",
             "both", "unlike", "despite", "although", "however", "many", "some", "most", "all", "each", "every",
             "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
             "november", "december", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
             "so", "key", "themes", "summary", "participants", "references", "works", "quotes", "claims"}

# Evidence that an entry IS a work: never removed against it.
NAMES = r"[A-Z][\w.'’-]*(?:\s+(?:[A-Z][\w.'’-]*|von|van|de|del|da|di|la|le))*"   # "Friedrich von Hayek"
WORK_EVIDENCE = re.compile(r"\b(poem|poetry|epic|novel|novella|book|memoir|biography|autobiography|film|movie|"
                           r"documentary|television series|tv series|sitcom|series|play|opera|musical|song|album|"
                           r"single|essay|report|study|paper|article|journal|magazine|painting|sculpture|"
                           r"short story|anthology|treatise|scripture|speech|lecture|literary work|written work|"
                           r"legislation|statute|law|act|code|regulation|directive|treaty|newspaper|periodical|pamphlet|manifesto|"
                           r"publication|newsletter)\b", re.I)
# What a Wikipedia description or Wikidata type calls a named idea: theory, principle, ideology...
IDEA_EVIDENCE = re.compile(r"\b(theory|theories|theorem|principle|eponymous|adage|effect|paradox|fallacy|bias|"
                           r"hypothesis|conjecture|concept|model|framework|thought experiment|ideology|ideologies|"
                           r"doctrine|philosophy|philosophical|school of thought|movement|dilemma|heuristic|razor|"
                           r"maxim|axiom|syndrome|phenomenon|observation|law of|theses|thesis|tenets)\b", re.I)
# Pages that exist but are not ideas: honours, events, occasions ("Victoria Cross — Highest award…").
NOT_AN_IDEA = re.compile(r"\b(honou?rific|honou?r|award|prize|medal|title (given|bestowed|awarded)|holiday|festival|"
                         r"war|battle|massacre|attack|incident|election|ceremony|campaign|operation|protests?|riots?|"
                         r"case|trial|scandal|disaster)\b", re.I)
# What a matching page must call the thing before a "work" may be removed.
PERIODICAL = re.compile(r"\b(newspaper|magazine|periodical|tabloid|broadsheet|news website)\b", re.I)
NOT_A_WORK = re.compile(r"\b(company|corporation|conglomerate|firm|business|brand|organi[sz]ation|agency|institution|"
                        r"university|club|political party|platform|website|web site|app|software|social network|"
                        r"search engine|language model|chatbot|town|city|village|country|region|county|river|"
                        r"building|hotel|restaurant|airline|bank|stock exchange|government|ministry|parliament|"
                        r"legislature|prediction market|exchange|human|person)\b", re.I)
GROUPS = (   # (heading, pattern over the inventory type and Wikidata labels), first match wins
    ("Books", r"\b(book|novel|novella|memoir|biography|autobiography|anthology|non-fiction|nonfiction|"
              r"literary work|written work|book series|novel series)\b"),
    ("Films & TV", r"\b(film|movie|documentary|television|tv|miniseries|sitcom|web series)\b"),
    ("Papers & reports", r"\b(paper|report|study|article|scholarly|white paper|working paper|journal|survey|poll)\b"),
    ("Laws", r"\b(law|act|statute|legislation|treaty|bill|constitution|regulation|directive)\b"),
)
VERDICTS = {"disputed": "⚠️ Disputed", "supported": "✅ Supported"}

INVENTORY_PROMPT = """Below are the summary, key themes and raw reference list of notes on one podcast episode, made from automatic transcription.

1. For each WORK entry, give its type (book, film, tv, documentary, paper, report, study, article, law, poem, scripture, play, song, other) and a clean title and author as the entry itself suggests (remove filler words; do not add anything that is not in the entry; leave author empty if the entry names none). If the entry is not a work at all — a concept, theory, problem, incident, event, project, organisation, product, platform, statistic or claim — give type "not_a_work".
2. List up to {max_claims} CLAIMS from the themes worth checking against outside sources: statistics, dated events, and statements of who did what about public figures or institutions. Never opinions, predictions, arguments, quotations or sayings. Write each claim as one plain factual sentence WITHOUT saying who said it, give who made it separately, and give a short web search query (4 to 8 key words: names, numbers, places, the institution or study) that would find a source for it.

Reply with JSON only:
{{"works": [{{"id": "W1", "type": "book", "title": "...", "author": "..."}}], "claims": [{{"id": "C1", "claim": "...", "who": "...", "query": "..."}}]}}

SUMMARY AND THEMES
{text}

WORKS
{works}"""

NAMES_WORKS_PROMPT = """You are checking names and works in podcast notes made from automatic transcription. For each item you are given evidence from the episode description, Wikipedia, Wikidata and book catalogues. Use ONLY that evidence, never your own knowledge.

For each NAME: "keep", or "correct" with the exact spelling the evidence shows, only when the name is MISSPELLED (a transcription error) and the evidence clearly refers to the same person or organisation. Never expand, formalise or retitle a correctly spelled name ("President Macron" stays).

For each WORK: "keep"; "correct" with the title and author exactly as the evidence gives them, when the evidence shows the same work spelled or titled slightly differently; "move" when the entry is really a quotation, not a title; "remove" only when the evidence positively shows the same title is not a work (a company, platform, place, institution, event, policy, statistic). Missing evidence is never a reason to remove.

Reply with JSON only:
{{"names": [{{"id": "N1", "action": "keep|correct", "correct": "..."}}], "works": [{{"id": "W1", "action": "keep|correct|move|remove", "title": "...", "author": "..."}}]}}

Episode description:
{description}

NAMES
{names}

WORKS
{works}"""

CLAIMS_PROMPT = """Check each claim from a podcast against the numbered evidence snippets given for it. Use ONLY the evidence, never your own knowledge.

Evidence counts only if it is about the same subject, people and period as the claim; evidence about another year, country, person or event does not count. Verdicts: "supported" (relevant evidence confirms it; spoken figures are approximate, so a figure within rounding or about a tenth of the source's counts as confirmed — "about 20%" against "19%" is supported), "disputed" (relevant evidence plainly contradicts it: the opposite fact, or a materially different figure for the same thing and period), "not_checkable" (no relevant evidence). Cite the evidence ids you relied on in "evidence". One short line of reason, without evidence ids.

Reply with JSON only:
{{"claims": [{{"id": "C1", "verdict": "supported|disputed|not_checkable", "reason": "...", "evidence": ["E1"]}}]}}

{claims}"""

BOOK_PROMPT = """A podcast cited a book. Name it exactly as these web search results identify it.

Cited as: {cited}
What was said about it: {context}

Search results (title — address):
{results}

Reply with JSON only: {{"title": "…", "author": "…"}} — the book's title (with its subtitle when the results give one) and its author or authors, spelled as the results spell them. When none of the results is clearly this book, reply {{"title": "", "author": ""}}."""

DISPUTE_PROMPT = """Does this evidence contradict the claim? Contradict means: the evidence states something about the same thing and the same period that cannot be true at the same time as the claim. Evidence that is about another year or event, that is merely incomplete, or that does not mention what the claim says is NOT a contradiction. Use ONLY the evidence.

Claim (said in a podcast published {date}): {claim}

Evidence:
{evidence}

Reply with JSON only: {{"contradicts": true|false, "quote": "the exact sentence of the evidence that contradicts the claim, copied word for word, or empty"}}"""


# ---------------------------------------------------------------- sources

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


def wikipedia(query: str, limit: int = 3) -> list[str]:
    """'Title — short description' for the top Wikipedia search hits; [] on any failure."""
    try:
        data = http_json(WIKI_API + "?" + urllib.parse.urlencode({
            "action": "query", "generator": "search", "gsrsearch": query, "gsrlimit": limit,
            "prop": "description", "format": "json", "formatversion": 2}))
    except Exception:
        return []
    pages = sorted(data.get("query", {}).get("pages", []), key=lambda page: page.get("index", 99))
    return [f"{page['title']} — {page.get('description', '')}".strip(" —") for page in pages]


def wikidata_types(title: str) -> list[str]:
    """English labels of the Wikidata 'instance of' (P31) values for a Wikipedia page title."""
    try:
        data = http_json(WIKI_API + "?" + urllib.parse.urlencode({
            "action": "query", "titles": title, "prop": "pageprops", "redirects": 1,
            "format": "json", "formatversion": 2}))
        item = (data.get("query", {}).get("pages", [{}])[0].get("pageprops") or {}).get("wikibase_item")
        if not item:
            return []
        claims = http_json(WIKIDATA_API + "?" + urllib.parse.urlencode({
            "action": "wbgetentities", "ids": item, "props": "claims", "format": "json"}))
        ids = [c["mainsnak"]["datavalue"]["value"]["id"]
               for c in claims["entities"][item]["claims"].get("P31", []) if c["mainsnak"].get("datavalue")][:6]
        if not ids:
            return []
        labels = http_json(WIKIDATA_API + "?" + urllib.parse.urlencode({
            "action": "wbgetentities", "ids": "|".join(ids), "props": "labels", "languages": "en", "format": "json"}))
        return [e.get("labels", {}).get("en", {}).get("value", "") for e in labels["entities"].values() if e]
    except Exception:
        return []


BRAVE_SEARCH = "https://api.search.brave.com/res/v1/web/search"
_LAST_BRAVE = [0.0]


def brave_key() -> str:
    """The skill's own Brave Search API key, from the environment (Hermes never passes its own
    BRAVE_SEARCH_API_KEY to skill scripts)."""
    return os.environ.get("PODNOTE_BRAVE_API_KEY", "").strip()


def web(query: str, limit: int = 4) -> list[dict]:
    """[{title, url, snippet}] for the top general web results from Brave Search (its own index; `site:`
    and quoted phrases honoured); [] without a key or on failure."""
    key = brave_key()
    if not key:
        return []
    request = urllib.request.Request(BRAVE_SEARCH + "?" + urllib.parse.urlencode({"q": query, "count": max(1, min(limit, 20))}),
                                     headers={"Accept": "application/json", "X-Subscription-Token": key, "User-Agent": AGENT})
    for attempt in (1, 2):
        time.sleep(max(0.0, 1.1 - (time.time() - _LAST_BRAVE[0])))     # at most one request a second
        _LAST_BRAVE[0] = time.time()
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.load(response)
            break
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt == 2:
                return []
        except Exception:
            return []
    plain = lambda text: html.unescape(re.sub(r"<[^>]+>", "", text or ""))
    return [{"title": plain(hit.get("title")), "url": hit.get("url", ""), "snippet": plain(hit.get("description"))[:240]}
            for hit in (data.get("web") or {}).get("results", [])[:limit] if hit.get("url")]


_SEARCHED: dict[str, list[str]] = {}


def book_search(query: str, limit: int = 5) -> list[str]:
    """'title — address' of the top general web results (Brave Search, as for claims), used to name
    books; [] without a key or on any failure. Each query is asked once per pass."""
    if query not in _SEARCHED:
        _SEARCHED[query] = [f"{hit['title']} — {hit['url']}" for hit in web(query, limit) if hit["title"]]
    return _SEARCHED[query]


SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into", "nor", "of", "on", "or", "the",
               "to", "with"}


def tidy_book(title: str, author: str) -> tuple[str, str]:
    """Shop and catalogue spellings made plain: no series tag ("(Aubrey & Maturin, #1)"), no ISBN, no
    edition ("…, 3rd Edition"), no blurb for a subtitle (one quoting praise or "bestselling"), no
    SHOUTING, authors as "First Last" rather than "Last, First" (also "Last, First, Last, First")."""
    title = re.sub(r"\s*\((?:[^()]*#\s*\d+[^()]*|[^()]*\bbook\s+\d+)\)\s*$", "", title).strip()
    title = re.sub(r"\s*[:(–-]?\s*(?:ISBN[:\s]*)?97[89][\d-]{10,14}\)?\s*$", "", title).strip()
    title = re.sub(r"(?i)\s*(?:[,:–-]\s*|\(\s*)(?:\d+(?:st|nd|rd|th)|first|second|third|fourth|fifth|sixth|revised|"
                   r"updated|expanded|new|anniversary|illustrated)(?:\s+[\w-]+)?\s+edition\s*\)?\s*$", "", title).strip()
    main, colon, subtitle = title.partition(": ")
    if colon and main and re.search(r"[“”\"]|\bbest-?selling\b|\bbestseller\b|\bgripping\b", subtitle, re.I):
        title = main                                       # "…: A gripping memoir, a “taut thriller” (a reviewer)"
    parts = [part.strip() for part in author.split(",")]
    if (len(parts) >= 4 and len(parts) % 2 == 0 and not re.search(r";|&|\band\b", author)
            and all(len(surname.split()) == 1 for surname in parts[0::2])):
        author = "; ".join(f"{parts[i]}, {parts[i + 1]}" for i in range(0, len(parts), 2))

    def calm(text: str) -> str:
        if not text.isupper():
            return text
        out = []
        for i, word in enumerate(text.lower().split()):
            after_colon = i and out[-1].endswith(":")
            out.append(word if i and word in SMALL_WORDS and not after_colon else word[:1].upper() + word[1:])
        return " ".join(out)
    names = []
    for name in re.split(r"\s*(?:;|&|\band\b)\s*", calm(author)):
        last_first = re.fullmatch(r"([^,]+),\s*([^,]+)", name.strip())
        if last_first and 1 in (len(last_first.group(1).split()), len(last_first.group(2).split())):
            names.append(f"{last_first.group(2)} {last_first.group(1)}")    # "Pratchett, Terry", "Le Carré, John"
        else:
            names.append(name.strip())                                  # "Terry Pratchett, Neil Gaiman": two people
    return calm(title), ", ".join(n for n in names if n)


def resolve_book(title: str, author: str, context: str, model) -> tuple[str, str] | None:
    """The book a podcast meant, as the web names it: a general search (title and author as heard, then
    the title alone) read by the model, which may only answer from those results. Accepted when the
    answer shares a word with what was heard or said, or names the author heard. None: unresolved."""
    results = book_search(f"{title} {author} book".strip()) or (book_search(f"{title} book") if author else [])
    if not results:
        return None
    answer = model.ask(BOOK_PROMPT.format(cited=f"{title}" + (f" (by {author})" if author else ""),
                                          context=context or "—", results="\n".join(results)), 300)
    found, by = tidy_book(str(answer.get("title") or "").strip(), str(answer.get("author") or "").strip())
    if not found or not by:
        return None
    words = lambda text: {w for w in re.findall(r"[a-z0-9]+", text.lower().replace("-", "")) if len(w) > 3}
    heard = words(f"{title} {author} {context}")
    if not (words(found) & heard or words(by) & heard):
        return None                                       # an answer unrelated to what was heard
    return found, by


def book_record(title: str, author: str) -> dict:
    """{title, author, year, work} for the Open Library work of this title and author; {} if none.
    Open Library keeps subtitles apart and drops articles ("World" for "The World: A Family
    History"), so it is searched by the bare main title and matched on title plus subtitle or on the
    bare main title; authors also match on their alternative names ("Homer" for "Ὅμηρος")."""
    main = title.split(":")[0].strip() or title
    query = {"title": bare_title_words(main) or main, "limit": 10,
             "fields": "key,title,subtitle,author_name,author_alternative_name,first_publish_year,edition_count"}
    if author:
        query["author"] = author
    try:
        docs = http_json(OPEN_LIBRARY + "?" + urllib.parse.urlencode(query)).get("docs", [])
    except Exception:
        return {}
    matching = []
    for doc in docs:
        full = doc.get("title", "") + (f": {doc['subtitle']}" if doc.get("subtitle") else "")
        matches = (same_title(title, full) or (not doc.get("subtitle") and same_title(title, doc.get("title", "")))
                   or bare_title_words(main) == bare_title_words(doc.get("title", "")))
        if matches and re.fullmatch(r"/works/OL\d+W", doc.get("key", "")) \
                and author_agrees(author, doc.get("author_name", []) + doc.get("author_alternative_name", [])):
            matching.append((full, doc))
    if not matching:
        return {}
    # Open Library often holds one book as several work records; the main one has the most editions
    full, doc = max(matching, key=lambda pair: pair[1].get("edition_count") or 0)
    return {"title": full, "author": ", ".join(doc.get("author_name", [])[:2]),
            "year": doc.get("first_publish_year"), "work": doc["key"], "edition": latest_english_edition(doc["key"])}


SELF_PUBLISHED = re.compile(r"independently published|createspace|lulu\b|kindle direct|amazon digital", re.I)
MONTHS = {m: n for n, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def publication_date(text: str) -> tuple[int, int, int]:
    """(year, month, day) from Open Library's free-text publish_date ("May 16, 2017", "2016-01-28",
    "2016"); missing parts are 0, an unreadable date is (0, 0, 0)."""
    text = (text or "").lower()
    year = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", text)
    if not year:
        return 0, 0, 0
    iso = re.search(r"\b(\d{4})-(\d{1,2})(?:-(\d{1,2}))?\b", text)
    if iso:
        return int(iso.group(1)), int(iso.group(2)), int(iso.group(3) or 0)
    month = next((n for name, n in MONTHS.items() if re.search(rf"\b{name}", text)), 0)
    day = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b,?\s+(?=\d{4})", text)
    return int(year.group(1)), month, int(day.group(1)) if day and month else 0


def latest_english_edition(work_key: str) -> str:
    """The key (/books/OL…M) of the most recently published English edition of a work; "" when the
    work lists none. English: not marked as another language, and its ISBN (if any) in the
    English-language groups 0/1 (UK, US, Canada, Australia…), so not an Indian reprint of a classic.
    Left out: e-books and audio (the page's shop links follow the shown edition's ISBN, and an e-book
    ISBN has no print page) and self-published print-on-demand (the newest "edition" of a classic is
    usually one)."""
    try:
        entries = http_json(f"https://openlibrary.org{work_key}/editions.json?limit=1000").get("entries", [])
    except Exception:
        return ""
    best, best_date = "", None
    for edition in entries:
        fmt = (edition.get("physical_format") or "").lower()
        if re.search(r"e-?book|electronic|kindle|audio|\bcd\b|mp3|cassette", fmt):
            continue
        languages = [l.get("key", "") for l in edition.get("languages", []) if isinstance(l, dict)]
        isbns = [i.replace("-", "") for i in (edition.get("isbn_13") or []) + (edition.get("isbn_10") or [])]
        english_isbn = any(i[:4] in ("9780", "9781") or (len(i) == 10 and i[0] in "01") for i in isbns)
        if (languages and "/languages/eng" not in languages) or (isbns and not english_isbn) \
                or (not isbns and not languages) or SELF_PUBLISHED.search(" ".join(edition.get("publishers") or [])):
            continue
        date = publication_date(edition.get("publish_date", ""))
        if re.fullmatch(r"/books/OL\d+M", edition.get("key", "")) and (best_date is None or date > best_date):
            best, best_date = edition["key"], date
    return best


def wikipedia_passages(query: str, limit: int = 3) -> list[dict]:
    """Wikipedia full-text search: article titles with the matching passage."""
    try:
        data = http_json(WIKI_API + "?" + urllib.parse.urlencode({
            "action": "query", "list": "search", "srsearch": query, "srlimit": limit, "format": "json"}))
    except Exception:
        return []
    return [{"title": hit["title"], "snippet": re.sub(r"<[^>]+>", "", hit.get("snippet", "")),
             "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(hit["title"].replace(" ", "_"))}
            for hit in data.get("query", {}).get("search", [])]


def studies(query: str, limit: int = 3) -> list[dict]:
    """OpenAlex: research papers and reports matching the query, newest relevant first."""
    try:
        data = http_json(OPENALEX + "?" + urllib.parse.urlencode({
            "search": query, "per-page": limit, "select": "title,publication_year,doi,id,primary_location"}))
    except Exception:
        return []
    hits = []
    for work in data.get("results", []):
        venue = ((work.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
        hits.append({"title": work.get("title") or "", "url": work.get("doi") or work.get("id") or "",
                     "snippet": f"{work.get('publication_year') or ''} {venue}".strip()})
    return hits


def relevant(query: str, hits: list[dict]) -> list[dict]:
    """Hits that share the query's key words (search engines answer long or odd queries with adverts)."""
    keys = {w for w in re.findall(r"[a-z0-9%.,-]+", query.lower()) if len(w) >= 4 or any(c.isdigit() for c in w)}
    need = 2 if len(keys) > 2 else 1
    return [hit for hit in hits
            if sum(k in f"{hit.get('title', '')} {hit.get('snippet', '')}".lower() for k in keys) >= need]


def author_agrees(author: str, catalogue: list[str]) -> bool:
    """No author given -> any; otherwise a catalogue author must share the given author's surname."""
    if not author.strip():
        return True
    surname = author.replace(",", " ").split()[-1].lower()
    return any(surname in name.lower() for name in catalogue)


def readable(name: str) -> bool:
    return bool(name) and all(ord(c) < 0x250 for c in name if c.isalpha())


def bare_title_words(title: str) -> str:
    """A title for comparison: lower case, no punctuation, no leading article."""
    words = re.sub(r"[^\w\s]", " ", title.lower()).split()
    return " ".join(words[1:] if words[:1] in (["the"], ["a"], ["an"]) and len(words) > 1 else words)


SCRIPTURE_TITLE = re.compile(r"(?i)^(the )?(holy )?(bible|old testament|new testament|hebrew bible|torah|talmud|"
                             r"mishnah|qur'?an|koran|gospels?\b.*|psalms|book of \w+|ecclesiastes|genesis|exodus|"
                             r"leviticus|numbers|deuteronomy|proverbs|isaiah|jeremiah|revelation|bhagavad gita)$")
NO_SHOP_EVIDENCE = re.compile(r"\b(religious text|sacred text|holy book|scripture|biblical|book of the (hebrew )?bible|"
                              r"forgery|forged|fabricat\w*|hoax|propaganda|antisemiti\w*|hate (speech|text|literature)|"
                              r"conspiracy theor\w*)\b", re.I)


def no_shop_link(work: dict) -> bool:
    """No shop link for scripture or for a work Wikipedia/Wikidata call a forgery, hoax, propaganda or
    hate text (The Donation of Constantine): it stays in References, without "Find"."""
    if SCRIPTURE_TITLE.match(work["title"].strip()):
        return True
    top = (work.get("hits") or [""])[0]
    name, _, description = top.partition(" — ")
    return (bool(name) and same_title(work["title"], name) and bool(NO_SHOP_EVIDENCE.search(description))) \
        or any(NO_SHOP_EVIDENCE.search(t) for t in work.get("types") or [])


def book_link(title: str, author: str, work: str = "", edition: str = "") -> tuple[str, str]:
    """(label, url): the Open Library work page, opened on its latest English edition when one is
    known (the page carries that edition's shop links: Amazon, Bookshop.org, Better World Books); an
    Amazon UK book search when Open Library has no record of the book."""
    if work:
        return "Open Library", f"https://openlibrary.org{work}" + (f"?edition=key:{edition}" if edition else "")
    return "Amazon UK search", "https://www.amazon.co.uk/s?" + urllib.parse.urlencode(
        {"k": f"{title} {author}".strip(), "i": "stripbooks"})


# ---------------------------------------------------------------- guards

def same_title(a: str, b: str, subtitle: bool = True) -> bool:
    """Two spellings of one title: punctuation or a few letters apart, or the same title with a
    subtitle after a colon or dash (unless subtitle=False) — never a different title that merely
    starts with the same words ("Swan Song" is not "Swan song now glad whooping")."""
    x, y = (" ".join(re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split()) for s in (a, b))
    if not x or not y:
        return False
    if difflib.SequenceMatcher(None, x, y).ratio() >= 0.85:
        return True
    if not subtitle:
        return False
    (short_raw, long_raw) = sorted((a.strip(), b.strip()), key=len)
    head = long_raw[:len(short_raw)]
    rest = long_raw[len(short_raw):].lstrip()
    return (len(short_raw) >= 6 and same_title(short_raw, head, subtitle=False)
            and rest[:1] in {":", "—", "–", "-"})


def episode_date(note_text: str) -> str:
    found = re.search(r"(?m)^published: ['\"]?(\d{4}-\d{2}-\d{2})", note_text) \
        or re.search(r"(?m)^\*\*.+?\*\* · (\d{4}-\d{2}-\d{2}) ·", note_text)    # else the note's header line
    return found.group(1) if found else ""


def same_period(claim: str, texts: list[str], date: str) -> bool:
    """Evidence for a claim that names no year must not be about another year: when the cited
    evidence (or the verdict's reason) names years and none is the episode's year or the one
    before, it is about another period ("Q2 2018 growth was 4.2%" says nothing of this Q2)."""
    if not date or re.search(r"\b(19|20)\d{2}\b", claim):
        return True
    years = {int(y) for y in re.findall(r"\b((?:19|20)\d{2})\b", " ".join(texts))}
    episode_year = int(date[:4])
    return not years or bool(years & {episode_year, episode_year - 1})


def confirmed_contradiction(model, claim: str, sources: list[str], date: str) -> bool:
    """A second, narrow question for each disputed claim: the model must copy the contradicting
    sentence, and that sentence must really be in the evidence."""
    if not sources:
        return False
    answer = model.ask(DISPUTE_PROMPT.format(date=date or "recently", claim=claim,
                                             evidence="\n".join(f"- {s}" for s in sources)), 400)
    quote = " ".join(str(answer.get("quote") or "").lower().split()).strip(" .\"'“”")
    haystack = " ".join(re.sub(r"<[^>]+>", "", html.unescape(" ".join(sources))).lower().split())
    return answer.get("contradicts") is True and len(quote.split()) >= 4 and quote in haystack


def same_work(a: dict, b: dict) -> bool:
    """One work cited twice: the same title, or the same author and nearly the same main title (the
    part before a colon): "On the Equality of All Things: Lessons on Physics" and "On the Equality of
    Things" by Carlo Rovelli."""
    if same_title(a["title"], b["title"]):
        return True
    if not (a["author"] and b["author"] and author_agrees(a["author"], [b["author"]])):
        return False
    short, long = sorted((bare_title_words(a["title"].split(":")[0]), bare_title_words(b["title"].split(":")[0])), key=len)
    # the start of the longer title against the shorter one: a subtitle may follow without a colon
    return bool(short) and difflib.SequenceMatcher(None, short, long[:len(short) + 6]).ratio() >= 0.85


def spelling_variant(wrong: str, right: str) -> bool:
    """A correction must look like a transcription fix of the same name, not a different phrasing:
    letters change inside words, no word is swapped for another ("Mustafa Kemal Pasha" is not to
    become "Mustafa Kemal Atatürk", nor "Ayatollah Khomeini" "Ruhollah Khomeini")."""
    a, b = (re.sub(r"[^\w\s]", "", s.lower()).split() for s in (wrong, right))
    return (len(a) == len(b)
            and difflib.SequenceMatcher(None, wrong.lower(), right.lower()).ratio() >= 0.8
            and all(difflib.SequenceMatcher(None, x, y).ratio() >= 0.7 for x, y in zip(a, b)))


def proven_not_a_work(title: str, evidence: list[str], types: list[str]) -> bool:
    """True only when a Wikipedia hit for this very title, or its Wikidata types, call it something
    that is not a work. Missing, unrelated or vague evidence never removes."""
    if types and not any(WORK_EVIDENCE.search(t) for t in types) and any(NOT_A_WORK.search(t) for t in types):
        return True
    for hit in evidence:
        name, _, description = hit.partition(" — ")
        if same_title(title, name, subtitle=False) and NOT_A_WORK.search(description) \
                and not WORK_EVIDENCE.search(description):
            return True
    return False


def group_for(kind: str, types: list[str]) -> str:
    text = " ".join([kind] + types)
    for heading, pattern in GROUPS:
        if re.search(pattern, text, re.I):
            return heading
    return "Other works"


# ---------------------------------------------------------------- note parsing

def front_people(front: str) -> tuple[str, list[str]]:
    """The show and the people taking part, from the note's properties."""
    fields = podnote.read_front(front if front.startswith("---") else f"---\n{front.strip()}\n---\n")
    people = [str(n) for key in ("hosts", "guests") for n in (fields.get(key) or []) if isinstance(fields.get(key), list)]
    return str(fields.get("show") or ""), people


def cited_by_not_written_by(title: str, author: str, people: list[str]) -> str | None:
    """A participant put where the author belongs ("Harry Potter (Hugo Marsh)" — the host who cited it).
    Only when the catalogue has no such book by them and the work has its own Wikipedia article: the
    author that article names, or "" (no author) when it names someone else or no one and not them.
    None — leave it: no evidence either way, and a guest's own book stays theirs."""
    if not author or not any(author_agrees(author, [person]) for person in people) or book_record(title, author):
        return None
    hits = wikipedia(title, 3)
    if not hits or not same_title(title, hits[0].partition(" — ")[0]):
        return None
    named = author_from_description(hits[0])
    if named:
        return named if not author_agrees(author, [named]) else None
    return "" if author.split()[-1].lower() not in hits[0].lower() else None


def split_note(text: str) -> tuple[str, str]:
    if text.startswith("---\n") and "\n---\n" in text[4:]:
        end = text.index("\n---\n", 4) + 5
        return text[:end], text[end:]
    return "", text


def section(body: str, title: str) -> str:
    match = re.search(rf"(?ms)^## {re.escape(title)}\n(.*?)(?=^## |^\[\^|\Z)", body)
    return match.group(1) if match else ""


def summary_text(body: str) -> str:
    """The summary callout (or an old-style ## Summary section) as plain text."""
    callout = re.search(r"(?ms)^> \[!abstract\] Summary\n((?:>.*\n?)*)", body)
    if callout:
        return "\n".join(line[1:].lstrip() for line in callout.group(1).splitlines())
    return section(body, "Summary")


def candidate_names(body: str) -> list[str]:
    text = summary_text(body) + "\n" + section(body, "Key themes")
    counts: dict[str, int] = {}
    for match in re.finditer(r"\b[A-Z][\w'’.-]+(?:\s+(?:[A-Z][\w'’.-]+|bin|al|de|van|von|der|ben|ibn)){1,3}\b", text):
        words = match.group(0).split()
        while words and words[0].lower() in NOT_NAMES:
            words = words[1:]
        while words and words[-1].lower() in NOT_NAMES:
            words = words[:-1]
        if len(words) >= 2:
            name = " ".join(words).rstrip(".'’s")
            counts[name] = counts.get(name, 0) + 1
    return sorted(counts, key=lambda n: -counts[n])[:MAX_NAMES]


def reference_entries(body: str) -> tuple[list[str], list[str], list[str]]:
    """(works, ideas, quotes) from a podnote References section (plain bullets)."""
    refs = section(body, "References")
    head, _, quotes_part = refs.partition("### Quotes")
    works_part, _, ideas_part = head.partition("### Ideas")
    bullets = lambda part: [line[2:].strip() for line in part.splitlines() if line.startswith("- ")]
    return bullets(works_part), bullets(ideas_part), bullets(quotes_part)


def idea_link(title: str, hits: list[str], types: list[str], tagged: bool = False) -> tuple[str, str] | None:
    """(Wikipedia title, URL) when the top Wikipedia hit is this very idea; None otherwise. An entry
    the notes tagged as an idea needs only an article of that title that is not about a work, an
    organisation, place or person, or a list of meanings ("Peelian principles — Theses describing an
    ethical police force"). An entry filed as a work moves to Ideas only when the description or
    Wikidata type calls it a theory, principle, ideology... ("Broken windows theory — Criminological
    theory")."""
    if not hits:
        return None
    name, _, description = hits[0].partition(" — ")
    if not (same_title(title, name) or bare_title_words(title) == bare_title_words(name)):
        return None
    if WORK_EVIDENCE.search(description) or re.search(r"(?i)referred to by the same|disambiguation", description):
        return None
    called_an_idea = IDEA_EVIDENCE.search(description) or any(IDEA_EVIDENCE.search(t) for t in types)
    if not called_an_idea and not (tagged and not NOT_A_WORK.search(description) and not NOT_AN_IDEA.search(description)):
        return None
    return name, "https://en.wikipedia.org/wiki/" + urllib.parse.quote(name.replace(" ", "_"))


def work_title_author(entry: str) -> tuple[str, str, str]:
    head, context = (re.split(r"\s[-–—]\s", entry, maxsplit=1) + [""])[:2]
    match = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", head)
    title, author = (match.group(1), match.group(2)) if match else (head, "")
    if re.fullmatch(r"\s*\d{4}s?\s*", author):           # "(2023)" is a year, not an author
        title, author = head, ""
    by = re.match(rf"^(.*?)\s+by\s+({NAMES})\s*$", title) if not author else None
    if by and len(by.group(2).split()) >= 2:             # "A Study in Scarlet by Arthur Conan Doyle", not "Stand by Me"
        title, author = by.group(1), by.group(2)
    return title.strip(" *_\"'"), author.strip(), context.strip()


def author_from_description(hit: str) -> str:
    """The author(s) a Wikipedia short description names for a work: "1990 novel by Terry
    Pratchett and Neil Gaiman" → "Terry Pratchett, Neil Gaiman"; "" unless it describes a work by someone
    ("Swan song — Final gesture or effort", "Leviathan — Biblical sea monster")."""
    description = hit.partition(" — ")[2]
    found = re.search(rf"\bby\s+({NAMES}(?:\s*(?:,|and|&)\s*{NAMES})*)", description)
    if not found or not WORK_EVIDENCE.search(description[:found.start()]):
        return ""
    return ", ".join(n.strip() for n in re.split(r"\s*(?:,|\band\b|&)\s*", found.group(1)) if n.strip())


def bare_title(title: str) -> str:
    """A model-proposed title without trailing brackets: the author is kept apart, so
    "Brotherhood of Italy (Meloni)" must not become the title and then gain "(Meloni)" again.
    A trailing year in brackets stays."""
    year = re.search(r"\s*\(\d{4}s?\)\s*$", title)
    stripped = re.sub(r"(\s*\([^)]*\))+\s*$", "", title).strip()
    return stripped + (f" {year.group(0).strip()}" if year else "")


def named_author(proposed: str, entry: str, title: str) -> bool:
    """An author the entry itself names ("memoir by Harry", "Jeffrey Archer cites its reprints"),
    and not a word of the title ("ISM manufacturing" is not by ISM)."""
    surname = proposed.split()[-1] if proposed.split() else ""
    return bool(surname) and not re.search(rf"\b{re.escape(surname)}\b", title, re.I) \
        and bool(re.search(rf"\b{re.escape(surname)}\b", entry, re.I))


def description_evidence(name: str, description: str) -> list[str]:
    found = []
    for candidate in re.findall(r"[A-Z][\w'’.-]+(?:\s+[A-Z][\w'’.-]+){0,3}", description):
        if candidate != name and difflib.SequenceMatcher(None, candidate.lower(), name.lower()).ratio() >= 0.75:
            found.append(candidate)
    return found[:3]


# ---------------------------------------------------------------- quotes (mechanical)

STUTTER = re.compile(r"(?i)\b((?:[\w'’]+[ ,]+){1,4}?)\1")


def destutter(tokens: list[str]) -> list[str]:
    out: list[str] = []
    for token in tokens:
        out.append(token)
        for size in (1, 2, 3):
            if len(out) >= 2 * size and [w.lower() for w in out[-size:]] == [w.lower() for w in out[-2 * size:-size]]:
                del out[-size:]
                break
    return out


def destutter_quote(quote: str) -> str:
    match = re.match(r'^([“"])(.+?)([”"])', quote)
    if not match:
        return quote
    words, previous = match.group(2), None
    while words != previous:
        previous, words = words, STUTTER.sub(r"\1", words)
    return match.group(1) + words + match.group(3) + quote[match.end():]


def check_quote(quote: str, transcript: str) -> tuple[str, str | None]:
    """('keep'|'snap'|'remove', the transcript's words when snapping). A quote keeps only words that were
    said: a passage matching >= 0.8 of its words in order is replaced by the transcript's own words
    (repetitions included); anything less is removed. A short quote (under 4 words) must appear as said."""
    match = re.match(r'^[“"](.+?)[”"]', quote)
    if not match or not transcript:
        return "keep", None
    said = re.findall(r"[\w'’]+", match.group(1))
    heard = re.findall(r"[\w'’]+", transcript)
    lower_said, lower_heard = [w.lower() for w in said], [w.lower() for w in heard]
    if len(said) < 4:
        return ("keep", None) if f" {' '.join(lower_said)} " in f" {' '.join(lower_heard)} " else ("remove", None)
    opening = set(lower_said[:3])
    best, best_span = 0.0, (0, 0)
    for i, word in enumerate(lower_heard):
        if word not in opening:
            continue
        for length in {len(said) - 2, len(said), len(said) + 2, len(said) + 4}:
            window = lower_heard[i:i + max(length, 1)]
            ratio = difflib.SequenceMatcher(None, lower_said, window, autojunk=False).ratio()
            if ratio > best:
                best, best_span = ratio, (i, i + len(window))
    if best < 0.8:
        return "remove", None
    exact = " ".join(heard[best_span[0]:best_span[1]])
    return ("keep", None) if exact.lower() == " ".join(lower_said) else ("snap", exact)


def job_dir(note_text: str) -> Path | None:
    match = re.search(r"^apple_episode_id: ['\"]?(\d+)", note_text, re.M)
    root = podnote.CACHE / "jobs" / f"podcast-{match.group(1)}" if match else None
    return root if root is not None and root.is_dir() else None


def job_files(note_text: str) -> tuple[str, str]:
    """(transcript text, cleaned episode description) from the note's job cache, '' when absent."""
    match = re.search(r"^apple_episode_id: ['\"]?(\d+)", note_text, re.M)
    if not match:
        return "", ""
    root = podnote.CACHE / "jobs" / f"podcast-{match.group(1)}"
    transcript = description = ""
    if (root / "asr/transcript-v1.json").is_file():
        chunks = json.loads((root / "asr/transcript-v1.json").read_text(encoding="utf-8"))["chunks"]
        transcript = " ".join(str(c.get("text") or "") for c in chunks)
    if (root / "source/resolved-episode-v1.json").is_file():
        meta = json.loads((root / "source/resolved-episode-v1.json").read_text(encoding="utf-8"))
        description = podnote.clean_description(meta.get("episode_description", ""))
    return transcript, description


# ---------------------------------------------------------------- model

class VerifyError(RuntimeError):
    """The pass could not be completed; nothing is written and nothing is recorded as verified."""


class Model:
    """One local-model session for the whole pass. ask() returns parsed JSON, or {} on any failure."""

    def __init__(self):
        self.session = self.config = None
        self.unreadable: list[str] = []                    # raw answers that did not parse, for the record

    def __enter__(self):
        try:
            from model_arbiter import local_model_session
            self.session = local_model_session("podverify", "podverify", state_root=podnote.CACHE / "model-arbiter",
                                               model=podnote.MODEL, operation="podverify")
            self.config = self.session.__enter__()
        except Exception as error:
            self.session = self.config = None
            raise VerifyError(f"no verification model: {type(error).__name__}: {error}") from error
        return self

    def __exit__(self, *exc):
        if self.session is not None:
            try:
                self.session.__exit__(None, None, None)
            except Exception:
                pass
        return False

    def ask(self, prompt: str, limit: int = 2500) -> dict:
        if self.config is None:
            return {}
        try:
            import omlx_client
            text, finish = podnote.ask(self.config["base_url"], omlx_client.headers(self.config), prompt, limit)
        except Exception as error:                      # the model went away mid-pass: the pass fails
            raise VerifyError(f"verification model call failed: {error}") from error
        parsed = parse_answer(text) if finish == "stop" else {}
        if not parsed:
            self.unreadable.append(f"finish={finish}: {text[:1500]}")
        elif not whole_json(text):                       # salvaged item by item: some may be missing
            self.unreadable.append(f"partly read: {text[:3000]}")
        return parsed


def whole_json(text: str) -> bool:
    match = re.search(r"\{.*\}", text, re.S)
    try:
        return bool(match) and isinstance(json.loads(match.group(0)), dict)
    except json.JSONDecodeError:
        return False


def parse_answer(text: str) -> dict:
    """The model's JSON answer. When the whole object does not parse (typically a stray quote inside
    a reason), the list items that do parse on their own are kept: {"key": [items...]}."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        pass
    body, answer = match.group(0), {}
    starts = [m.start() for m in re.finditer(r'\{\s*"id"\s*:', body)]
    for n, at in enumerate(starts):                       # each item on its own, even with a broken bracket
        chunk = body[at:starts[n + 1] if n + 1 < len(starts) else len(body)]
        item = None
        whole = re.match(r"\{[^{}]*\}", chunk)
        if whole:
            try:
                item = json.loads(whole.group(0))
            except json.JSONDecodeError:
                item = None
        if item is None:                                  # fields one by one ("evidence": ["E4" } ...)
            item = {}
            for key, value in re.findall(r'"(\w+)"\s*:\s*"((?:[^"\\]|\\.)*)"', chunk):
                try:
                    item.setdefault(key, json.loads(f'"{value}"'))
                except json.JSONDecodeError:
                    item.setdefault(key, value)
            listed = re.search(r'"evidence"\s*:\s*\[([^\]{}]*)', chunk)
            if listed:
                item["evidence"] = re.findall(r'"(E\d+)"', listed.group(1))
            rest = re.sub(r'"\w+"\s*:\s*"(?:[^"\\]|\\.)*"|"E\d+"|"\w+"\s*:', "", chunk)
            if "id" not in item or '"' in rest:          # a stray quote: a value may be cut short, so drop it
                continue
        owners = [m.group(1) for m in re.finditer(r'"(\w+)"\s*:\s*\[', body[:at]) if m.group(1) != "evidence"]
        if owners:
            answer.setdefault(owners[-1], []).append(item)
    return answer


# ---------------------------------------------------------------- the pass

def verify(path: Path, model=None) -> tuple[int, int]:
    """Verify one note in place. Returns (changes, claims checked)."""
    text = path.read_text(encoding="utf-8")
    front, body = split_note(text)
    if already_verified(text, front):
        return -1, 0                                      # already verified; regenerate the note to redo it
    transcript, description = job_files(text)
    works_raw, ideas_raw, quotes_raw = reference_entries(body)
    changes: list[str] = []
    model = model or Model()
    with model:
        # A. inventory: work types, clean titles, claims worth checking
        listing = "\n".join(f"W{i}: {entry}" for i, entry in enumerate(works_raw, 1)) or "none"
        inventory = model.ask(INVENTORY_PROMPT.format(
            max_claims=MAX_CLAIMS, text=(summary_text(body) + "\n\n" + section(body, "Key themes"))[:12000],
            works=listing))
        kinds = {w.get("id"): w for w in inventory.get("works", []) or [] if isinstance(w, dict)}

        # B. evidence
        names = {f"N{i}": (name, wikipedia(name, 2) + [f"description: {d}" for d in description_evidence(name, description)])
                 for i, name in enumerate(candidate_names(body), 1)}
        works = {}
        show_name, people = front_people(front)
        publisher = str(podnote.read_front(text).get("publisher") or "")
        for i, entry in enumerate(works_raw, 1):
            key = f"W{i}"
            title, author, context = work_title_author(entry)
            clean = kinds.get(key, {})
            if author and f" by {author}" in entry:            # a byline only with evidence for it:
                twin = any(same_title(title, work_title_author(other)[0]) for other in works_raw if other != entry)
                named = author_agrees(author, [clean.get("author") or ""]) and bool(clean.get("author"))
                if not (twin or named or book_record(title, author)):
                    title, author = f"{title} by {author}", ""  # "Death by Black Hole" is a title, not a byline
            if clean.get("title") and same_title(title, bare_title(clean["title"])):
                title = bare_title(clean["title"])
                proposed = (clean.get("author") or "").strip()
                if not author and proposed and named_author(proposed, works_raw[i - 1], title):
                    author = proposed                     # only an author the entry itself names ("by Brett McGurk")
            if re.fullmatch(r"(?i)\s*(unspecified|unknown|none|n/?a|not given|various|-+|—)\s*", author or ""):
                author = ""
            if show_name and same_title(title, show_name):  # the show itself is not something it cites
                changes.append(f"Removed from Works: {title} (the show itself)")
                continue
            written_by = cited_by_not_written_by(title, author, people)
            if written_by is not None:
                context = context or author                # who cited it stays as the reason
                author = written_by
            hits = wikipedia(f"{title} {author}".strip(), 3)
            top = hits[0].partition(" — ")[0] if hits else ""
            types = wikidata_types(top) if top and same_title(title, top) else []
            kind = (clean.get("type") or "").lower()
            if not author and top and same_title(title, top):
                author = author_from_description(hits[0])  # "Good Omens": Pratchett and Gaiman
            book, named = {}, None
            if group_for(kind, types) == "Books" or kind == "book":
                named = resolve_book(title, author, context, model)   # the web knows books best: normalise first
                if named and (named[0], named[1]) != (title, author):
                    title, author = named
                book = book_record(title, author) if author else {}
            evidence = hits + [f"Wikidata instance of: {', '.join(types)}"] * bool(types)
            if book:
                evidence.append(f"Book catalogue: {book.get('title')} by {book.get('author')} ({book.get('year')})")
            works[key] = {"title": title, "author": author, "context": context, "kind": kind, "types": types,
                          "hits": hits, "book": book, "evidence": evidence, "named": bool(named)}

        # C. names and works verdicts
        verdict = {}
        if names or works:
            verdict = model.ask(NAMES_WORKS_PROMPT.format(
                description=description or "none",
                names="\n".join(f"{k}: {n}\n  evidence: {' | '.join(ev) or 'none'}" for k, (n, ev) in names.items()) or "none",
                works="\n".join(f"{k}: {w['title']} ({w['author'] or 'no author given'}) [{w['kind'] or 'type unknown'}]\n"
                                f"  evidence: {' | '.join(w['evidence']) or 'none'}" for k, w in works.items()) or "none"))
        for item in verdict.get("names", []) or []:
            key, correct = item.get("id"), (item.get("correct") or "").strip()
            if key in names and item.get("action") == "correct" and correct and correct != names[key][0] \
                    and spelling_variant(names[key][0], correct) \
                    and any(correct.lower() in str(e).lower() for e in names[key][1]):   # the evidence spells it so
                wrong = names[key][0]
                body, n = re.subn(rf"\b{re.escape(wrong)}\b", correct, body)
                front = re.sub(rf"(?m)^((?:hosts|guests):.*)$",
                               lambda m: re.sub(rf"\b{re.escape(wrong)}\b", correct, m.group(1)), front)
                front = re.sub(rf"(?m)^(\s*-\s+){re.escape(wrong)}\s*$", rf"\g<1>{correct}", front)
                if n:
                    changes.append(f"{wrong} → {correct} (name)")

        by_key = {item.get("id"): item for item in verdict.get("works", []) or [] if isinstance(item, dict)}
        grouped: dict[str, list[dict]] = {}
        moved_quotes, ideas = [], []
        for entry in ideas_raw:                           # named ideas: checked against Wikipedia
            title, origin, context = work_title_author(entry)
            hits = wikipedia(title, 3)
            top = hits[0].partition(" — ")[0] if hits else ""
            types = wikidata_types(top) if top and same_title(title, top) else []
            link = idea_link(title, hits, types, tagged=True)
            if re.fullmatch(r"(?i)\s*(unspecified|unknown|none|n/?a|not (given|specified|named)|originator not given|various|-+|—)\s*", origin):
                origin = ""
            if link:
                if link[0] != title:
                    changes.append(f"{title} → {link[0]} (idea)")
                ideas.append({"title": link[0], "url": link[1], "origin": origin, "context": context})
            elif proven_not_a_work(title, hits, types):
                changes.append(f"Removed from Ideas: {title} (not an idea per Wikipedia/Wikidata)")
            else:                                         # no evidence either way: left as it is
                ideas.append({"title": title, "url": "", "origin": origin, "context": context})
        for key, work in works.items():
            item, raw_title = by_key.get(key, {}), work_title_author(works_raw[int(key[1:]) - 1])[0]
            idea = idea_link(work["title"], work["hits"], work["types"]) if not work["book"] else None
            if idea:                                      # "Broken window theory" filed as a work
                ideas.append({"title": idea[0], "url": idea[1], "origin": work["author"], "context": work["context"]})
                changes.append(f"Moved to Ideas: {raw_title} ({idea[0]})")
                continue
            no_work_evidence = (bool(work["hits"]) and not work["book"]          # a failed lookup is not evidence
                                and not any(WORK_EVIDENCE.search(x) for x in work["types"] + work["hits"]))
            if (item.get("action") == "remove" and proven_not_a_work(work["title"], work["hits"], work["types"])) \
                    or (work["kind"] == "not_a_work" and no_work_evidence and item.get("action") != "correct"):
                changes.append(f"Removed from Works: {raw_title} (not a work per Wikipedia/Wikidata)")
                continue
            own = next((h.partition(" — ")[2] for h in work["hits"] if same_title(work["title"], h.partition(" — ")[0])), None)
            if publisher and same_title(work["title"], publisher):
                changes.append(f"Removed from Works: {raw_title} (the show's publisher)")
                continue
            if item.get("action") == "remove" and own is not None and PERIODICAL.search(own):
                changes.append(f"Removed from Works: {raw_title} (a newspaper or magazine named, no piece of it cited)")
                continue
            if item.get("action") == "remove" and not work["book"] and own is None \
                    and re.fullmatch(r"[^.!?]{2,}\s\w+[^.!?]*\.", work["title"]) and len(work["title"].split()) >= 6:
                changes.append(f"Removed from Works: {raw_title[:60]} (a description, not a title)")
                continue
            if item.get("action") == "remove" and work["kind"] == "not_a_work" and work["hits"] and not work["book"] \
                    and not (own and WORK_EVIDENCE.search(own)) and not any(WORK_EVIDENCE.search(t) for t in work["types"]):
                changes.append(f"Removed from Works: {raw_title} (not a work by both checks; no record of such a work)")
                continue
            if item.get("action") == "move" and len(work["title"].split()) >= 4:
                moved_quotes.append(f"\"{work['title']}\" — {work['context'] or work['author'] or 'cited in the episode'}")
                changes.append(f"Moved to Quotes: {raw_title}")
                continue
            corrected = bare_title((item.get("title") or "").strip())
            if item.get("action") == "correct" and corrected and same_title(work["title"], corrected):
                new_author = (item.get("author") or "").strip()
                keep_author = not new_author or not work["author"] or not spelling_variant(work["author"], new_author)
                work["title"], work["author"] = corrected, work["author"] if keep_author else new_author
            if work["book"] and same_title(work["title"], work["book"].get("title", "")) and not work.get("named"):
                work["title"] = work["book"]["title"]            # a title the web named keeps the web's spelling
                if not work["author"] and readable(work["book"].get("author", "")):
                    work["author"] = work["book"]["author"]
            raw_title_only, raw_author, _ = work_title_author(works_raw[int(key[1:]) - 1])
            if re.fullmatch(r"(?i)\s*(unspecified|unknown|none|n/?a|not given|various|-+|—)\s*", raw_author or ""):
                raw_author = ""
            if (work["title"], work["author"]) != (raw_title_only, raw_author):
                changes.append(f"{raw_title} → {label(work)} (work)")
            heading = group_for(work["kind"], work["types"])
            if heading in {w_heading for w_heading, _ in GROUPS} or heading == "Other works":
                if any(same_work(work, other) for other in grouped.get(heading, [])):
                    continue                              # two spellings of one work
                grouped.setdefault(heading, []).append(work)

        # quotes: mechanical checks against the transcript
        new_quotes = []
        for quote in quotes_raw + moved_quotes:
            action, exact = check_quote(quote, transcript)
            if action == "remove":
                changes.append(f"Removed quote not found in the transcript: {quote[:60]}")
                continue
            if action == "snap":
                quote = re.sub(r'^[“"](.+?)[”"]', f'"{exact}"', quote, count=1)
                changes.append(f"Quote matched to the transcript wording: \"{exact[:50]}\"")
            new_quotes.append(quote)                       # the words as said, repetitions included

        # D. claims
        claims = [c for c in inventory.get("claims", []) or [] if isinstance(c, dict) and c.get("claim")][:MAX_CLAIMS]
        evidence_ids, claim_blocks = {}, []
        for n, claim in enumerate(claims, 1):
            claim["id"] = f"C{n}"
            query = (claim.get("query") or claim["claim"]).strip()
            hits = relevant(query, wikipedia_passages(query, 3) + studies(query, 3) + web(query, 6))[:6]
            lines, claim["own"] = [], []
            for hit in hits:
                eid = f"E{len(evidence_ids) + 1}"
                evidence_ids[eid] = hit
                claim["own"].append(eid)
                lines.append(f"  {eid}: {hit['title']} — {hit['snippet']}")
            claim_blocks.append(f"C{n}: {claim['claim']} (said by {claim.get('who') or 'a speaker'})\n" + ("\n".join(lines) or "  no evidence"))
        checked = model.ask(CLAIMS_PROMPT.format(claims="\n\n".join(claim_blocks))) if claims else {}
        results = {item.get("id"): item for item in checked.get("claims", []) or [] if isinstance(item, dict)}
        rows = []
        for claim in claims:
            item = results.get(claim["id"], {})
            verdict_name = item.get("verdict") if item.get("verdict") in {"supported", "disputed"} else "not_checkable"
            cited = [eid for eid in item.get("evidence", []) or [] if eid in claim["own"]]   # its own evidence only
            if verdict_name != "not_checkable" and not cited:
                verdict_name = "not_checkable"                # no cited evidence, no verdict
            if verdict_name == "not_checkable":
                cited = []                                    # sources are shown only behind a verdict
            reason = re.sub(r"\s*\(?\bE\d+\b\)?,?", "", (item.get("reason") or "")).strip(" ,;")
            sources = [f"{evidence_ids[e]['title']} — {evidence_ids[e]['snippet']}" for e in cited]
            if verdict_name == "disputed" and not (same_period(claim["claim"], sources + [reason], episode_date(text))
                                                   and confirmed_contradiction(model, claim["claim"], sources, episode_date(text))):
                verdict_name, cited = "not_checkable", []      # a dispute needs same-period evidence and a quoted contradiction
            rows.append((verdict_name, claim, reason, cited))

    record = job_dir(text)
    if record is not None:
        try:
            podnote.atomic_write(record / "verify-record.json", json.dumps({
                "inventory": inventory, "names_works": verdict, "claims": checked,
                "works_evidence": {k: {x: w[x] for x in ("title", "author", "kind", "types", "hits", "book")} for k, w in works.items()},
                "claims_evidence": evidence_ids, "unreadable_answers": getattr(model, "unreadable", [])},
                ensure_ascii=False, indent=2, default=str))
        except OSError:
            pass

    # E. render
    # Only claims with a verdict are shown; unchecked claims are left out of the note entirely.
    rows = sorted((row for row in rows if row[0] != "not_checkable"), key=lambda row: row[0] != "disputed")
    footnotes, claims_md = [], ""
    if rows:
        table = ["| Claim | Who | Verdict | Sources |", "| --- | --- | --- | --- |"]
        for verdict_name, claim, reason, cited in rows:
            marks = []
            for eid in cited:
                hit = evidence_ids[eid]
                snippet = cell(html.unescape(hit.get("snippet", "")))[:160]
                footnotes.append(f"[^{len(footnotes) + 1}]: [{cell(hit['title'])}]({hit['url']})" + (f" — {snippet}" if snippet else ""))
                marks.append(f"[^{len(footnotes)}]")
            shown = VERDICTS[verdict_name] + (f" — {cell(reason)}" if reason else "")
            who = claim.get("who") or ""
            who = "—" if re.fullmatch(r"(?i)\s*(not specified|unspecified|unknown|none|n/?a|a speaker|research)?\s*", who) else who
            table.append(f"| {cell(claim['claim'])} | {cell(who)} | {shown} | {' '.join(marks) or '—'} |")
        claims_md = "## Claims checked\n\n" + "\n".join(table)

    unique_ideas = []
    for idea in ideas:
        if not any(same_title(idea["title"], other["title"]) for other in unique_ideas):
            unique_ideas.append(idea)
    refs_md = render_references(grouped, new_quotes, unique_ideas)
    body = strip_generated(body)
    parts = [body.rstrip("\n")]
    if claims_md:
        parts.append(claims_md)
    if refs_md:
        parts.append(refs_md)
    log = changes + (["Wikipedia did not answer; works were left as they were."]
                     if works and not any(w["hits"] for w in works.values()) else []) \
        + (["The model's claims answer could not be read; claims were left unchecked."] if claims and not results else [])
    if log:
        parts.append("> [!info]- Verification log\n" + "\n".join(f"> - {line}" for line in log))
    if footnotes:
        parts.append("\n".join(footnotes))
    body = "\n\n".join(parts) + "\n"

    disputed = sum(1 for row in rows if row[0] == "disputed")
    if front + body != text:                             # the note says when it changed; the check stays outside
        front = re.sub(r'(?m)^updated: .*$', f'updated: "{podnote.utc_now()}"', front)
        issues = podnote.validate(front + body)
        if issues:
            raise VerifyError("verified note fails the metadata check: " + ", ".join(issues))
        if path.read_text(encoding="utf-8") != text:     # the note was re-made meanwhile: keep the newer one
            raise VerifyError("the note changed during verification; not written")
        podnote.atomic_write(path, front + body)
    if record is not None:
        merge_record(record / "verify-record.json", verified_at=podnote.utc_now(), changes=len(changes),
                     claims_checked=len(rows), claims_disputed=disputed,
                     note_sha256=hashlib.sha256((front + body).encode()).hexdigest())
    return len(changes), len(rows)


def already_verified(text: str, front: str) -> bool:
    """Has this very note been verified? The job record holds the fingerprint of the note it verified."""
    record = job_dir(text)
    path = record / "verify-record.json" if record else None
    if not path or not path.is_file():
        return False
    return json.loads(path.read_text(encoding="utf-8")).get("note_sha256") == hashlib.sha256(text.encode()).hexdigest()


def merge_record(path: Path, **fields) -> None:
    current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    current.update(fields)
    podnote.atomic_write(path, json.dumps(current, ensure_ascii=False, indent=2, default=str))


def label(work: dict) -> str:
    return work["title"] + (f" ({work['author']})" if work["author"] else "")


def cell(text: str) -> str:
    return " ".join(str(text).replace("|", "/").split())


def strip_generated(body: str) -> str:
    """Remove what this pass writes (and the old QA pass wrote), so a rerun starts clean."""
    body = re.sub(r"(?ms)^## (Claims checked|References|QA|Verification)\n.*?(?=^## |\Z)", "", body)
    body = re.sub(r"(?ms)^> \[!info\]- Verification log\n(?:>.*\n?)*", "", body)
    body = re.sub(r"(?m)^\[\^\d+\]: .*\n?", "", body)
    return re.sub(r"\n{3,}", "\n\n", body).rstrip("\n") + "\n"


def render_references(grouped: dict[str, list[dict]], quotes: list[str], ideas: list[dict] | None = None) -> str:
    blocks = []
    books = grouped.get("Books", [])
    if books:
        rows = ["| Title | Author | Cited for | Find |", "| --- | --- | --- | --- |"]
        for work in books:
            book = work["book"] or {}
            label, url = book_link(work["title"], work["author"], book.get("work", ""), book.get("edition", ""))
            find = "—" if no_shop_link(work) else f"[{label}]({url})"
            rows.append(f"| *{cell(work['title'])}* | {cell(work['author'] or '—')} | "
                        f"{cell(work['context'] or '—')} | {find} |")
        blocks.append("### Books\n" + "\n".join(rows))
    for heading in ("Films & TV", "Papers & reports", "Laws", "Other works"):
        items = grouped.get(heading, [])
        if items:
            blocks.append(f"### {heading}\n" + "\n".join(
                f"- *{w['title']}*" + (f" ({w['author']})" if w["author"] else "") + (f" — {w['context']}" if w["context"] else "")
                for w in items))
    if ideas:                                             # linked to the Wikipedia article when there is one
        blocks.append("### Ideas\n" + "\n".join(
            "- " + (f"[{i['title']}]({i['url']})" if i["url"] else i["title"]) + (f" ({i['origin']})" if i["origin"] else "")
            + (f" — {i['context']}" if i["context"] else "") for i in ideas))
    if quotes:
        callouts = []
        for quote in quotes:
            pieces = re.split(r"\s[-–—]\s", quote, maxsplit=1)
            text, who = pieces[0], pieces[1] if len(pieces) > 1 else ""
            callouts.append(f"> [!quote] {cell(who).rstrip('.') or 'Quote'}\n> {text.strip()}")
        blocks.append("### Quotes\n" + "\n\n".join(callouts))
    return "## References\n\n" + "\n\n".join(blocks) if blocks else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("note")
    args = parser.parse_args()
    try:
        changes, claims = verify(Path(args.note))
    except VerifyError as error:
        print(f"STOPPED: {error}")
        return 1
    print("Verified: already verified, skipped" if changes < 0 else f"Verified: {changes} changes, {claims} claims")
    return 0


if __name__ == "__main__":
    sys.exit(main())
