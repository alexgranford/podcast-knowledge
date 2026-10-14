"""The prompts podnote sends to the text model: part notes, themes, summary and tags.

Kept apart from the code so a prompt edit is a reviewable change on its own. Every model output cache
records a fingerprint of the prompts it was made with, so editing one sets the matching cached
answers aside on the next run."""

SECTION_PROMPT = """You are taking reading notes on part {n} of {total} of a podcast transcript (automatic transcription, no speaker labels).

Show: {show}
Regular hosts: {hosts}
Episode: {episode}
Episode description (names here are spelled correctly; it usually says who the guests are): {description}

Write notes on THIS PART ONLY, in Markdown, in exactly this form:

**Topic:** one line saying what this part is about.
- One bullet for each distinct point the speakers make in this part, as many as the text holds (typically one for every 150 to 250 words); never pad, and never merge two points into one. Each bullet states one complete point directly: an argument with its reasoning, a disagreement, a caveat, or a concrete example. Never open a bullet with "It is argued that". Keep the direction of each claim exactly as spoken (who did what, for or against).

Who said what. The transcript has no speaker labels, so credit points with care:
- Credit a point to a person only when the text shows it is theirs: they are introduced, they speak about their own book, work, role or past, or they answer a question just put to them.
- Whoever is addressed by name ("Maya, ...", "as you said, Jason") is the listener of that line, not its speaker.
- When a show has several hosts, name one only if this passage itself identifies them (by name, or by their own company, book or past). Otherwise state the point without a name.
- Use people's names from the hosts line and the description rather than "the host" or "the guest".
- In an interview or debate the host mostly asks and challenges and the guest makes the longer case, but hosts argue too: use this only to orient, never as proof.
- If you cannot tell who said something, state the point without a name. Never guess.
**References:** works and quotes a reader could look up later, one bullet each, starting with its type in square brackets:
- [work] Title (author or source, if given) — who cited it and why. Works are books, films, TV series, plays, poems, songs, papers, studies, reports, articles, speeches and laws; never people, companies, products, apps, platforms or AI models, and never a claim or a statistic. Give the title cleanly, without filler words from the speech.
- [idea] Name (originator, if given) — who invoked it and how. Ideas are named theories, principles, laws of nature or economics, effects, paradoxes, fallacies, biases, thought experiments, models, ideologies and doctrines that a speaker draws on (for example broken windows theory, Occam's razor, Goodhart's law, the trolley problem, Thatcherism); use the name an encyclopedia would use.
- [quote] "words exactly as spoken" — who said them, and whom they were quoting, if anyone. Only memorable lines of at least a few words, including sayings, proverbs and scripture; copy the words from the transcript.
Write "- none" if the part cites nothing.

Rules: use only what is in the text below. Skip adverts, sponsor reads, discount codes, "subscribe/like/comment" appeals and credits entirely; if the whole part is promotional, reply with the single word SKIP.{advertisers} Never mention sponsors, adverts, or that you left anything out. No introduction, no closing remarks.

Transcript part {n}:
{text}"""

THEMES_PROMPT = """Below are sequential reading notes covering one podcast episode ({minutes} minutes, {parts} parts). Using ONLY these notes, write the body of a knowledge note in Markdown.

Group the material into themes: subjects the conversation actually spent time on. A theme draws together everything said about its subject, wherever in the episode it came up. Do not give each part its own theme, and do not blend unrelated segments into one. Aim for about {target} themes, fewer only if the episode is narrow, and order them by how much of the episode they occupy. Every subject that a part's **Topic** line names must appear in some theme.

Under a single heading `## Key themes`, write each theme as:

**Theme title** (a short phrase naming the subject, not a sentence)
A short paragraph (2 to 4 sentences, no label) on the essence of the theme: what it comes down to, where the speakers agree or clash, and what is left open.
- Then one bullet per point, opening with a short bold lead-in that names the point, then a dash, then the claim with its reasoning, evidence or example: `- **The 0.1% argument** — Ortiz argues that…`. Merge points that say the same thing; keep figures and names exactly. As many bullets as the theme needs, usually 3 to 6.

Write tightly in your own words; do not copy whole sentences from the notes. Put coined terms, slogans and named concepts the speakers use in `backticks`, sparingly.

Rules: add nothing that is not in the notes. Keep every attribution exactly as the notes give it: never move a point from one person to another, and never add a name to a point the notes leave unattributed. Use people's names exactly as they appear in the notes or the hosts line; never add or complete a surname from memory. Never mention sponsors, adverts or offers. No preface, no closing remarks, no other headings.

Show: {show}
Regular hosts: {hosts}
Episode: {episode}

Notes:
{notes}"""

SUMMARY_PROMPT = """Below are the key themes of one podcast episode. Write the opening of a knowledge note, in exactly this form and nothing else:

**Participants:** the hosts taking part (given below) first, then every guest the episode description or the themes name, each name in bold followed by their role in brackets, for example **Maya Lindqvist** (host), **Ruth Calder** (guest, historian). List only people who take part in the conversation; never someone who is only discussed. This line always starts with **Participants:**.

Then the quick summary: one plain sentence of 15 to 30 words saying what is discussed — the subject and the angle taken, as concrete as the themes allow. No label.

Then one clear paragraph of 60 to 110 words: where the discussion goes and where it lands — its main arguments and conclusions. Plain prose, no label.

Open both with the substance itself, never with "This episode", "This discussion" or "In this episode", and never address or mention the reader or listener.

Rules: add nothing that is not in the themes or the participants line. Keep attributions exactly as the themes give them. No other headings, no bullets, no closing remarks.

Show: {show}
Hosts taking part: {hosts}
Episode: {episode}
Episode description (names here are spelled correctly; it usually says who takes part): {description}

Themes:
{themes}"""

TAGS_PROMPT = """Choose the subject tags for a knowledge note on one podcast episode: the subjects someone would search for to find this note. First its main subject, then its substantial secondary themes, up to five in all, each taken exactly from the vocabulary below and each a different subject: where the vocabulary has two words for one thing, take one.

Reply with JSON only: {{"tags": ["…"], "missing": ["…"]}} — "tags" from the vocabulary, most important first; "missing" for up to three substantial subjects the vocabulary has no tag for (lowercase, hyphenated).

Vocabulary: {vocabulary}

Episode: {episode}

{summary}

{themes}"""
