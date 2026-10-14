"""The prompts the validation pass (podverify) sends to the text model.

Kept apart from the code so a prompt edit is a reviewable change on its own. Every model output cache
records a fingerprint of the prompts it was made with, so editing one sets the matching cached
answers aside on the next run."""

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
