#!/usr/bin/env python3
"""
Íslenski skoðunarþermómetrinn — daily fetcher
Fetches opinion pieces from Icelandic media, filters for EU-topic relevance,
classifies sentiment, and appends new articles to the cumulative feed.json.
"""

import os, json, hashlib, datetime, time, re, http.client
import xml.etree.ElementTree as ET
from urllib.request import urlopen, Request
from urllib.error import URLError

# ── CONFIG ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── OPINION SOURCES ──────────────────────────────────────────────────────────
OPINION_SOURCES = [
    {
        "id": "mbl",
        "label": "Morgunblaðið",
        "type": "opinion",
        "rss_candidates": ["https://www.mbl.is/feeds/mogginn/leidarar/"],
        "page_url": "https://www.mbl.is/vidskipti/pistlar/",
    },
    {
        "id": "blog",
        "label": "Blog.is",
        "type": "opinion",
        "rss_candidates": ["https://www.mbl.is/feeds/blog/"],
        "page_url": "https://www.blog.is/forsida/",
    },
    {
        "id": "visir_skodun",
        "label": "Vísir — Skoðun",
        "type": "opinion",
        "rss_candidates": ["https://www.visir.is/rss/skodanir"],
        "page_url": "https://www.visir.is/f/skodanir",
    },
    {
        "id": "heimildin",
        "label": "Heimildin",
        "type": "opinion",
        "rss_candidates": [
            "https://heimildin.is/feed/",
            "https://heimildin.is/rss/",
        ],
        "page_url": "https://heimildin.is/umraeda/",
    },
    {
        "id": "dv_eyjan",
        "label": "DV — Eyjan",
        "type": "opinion",
        "rss_candidates": ["https://www.dv.is/eyjan/feed/"],
        "page_url": "https://www.dv.is/eyjan/",
    },
]

# ── NEWS SOURCES ──────────────────────────────────────────────────────────────
NEWS_SOURCES = [
    {
        "id": "mbl_innlent",
        "label": "Morgunblaðið",
        "type": "news",
        "rss_candidates": ["https://www.mbl.is/feeds/innlent/"],
        "page_url": "https://www.mbl.is/frettir/innlent/",
    },
    {
        "id": "mbl_vidskipti",
        "label": "Morgunblaðið — Viðskipti",
        "type": "news",
        "rss_candidates": ["https://www.mbl.is/feeds/vidskipti/"],
        "page_url": "https://www.mbl.is/vidskipti/",
    },
    {
        "id": "visir_news",
        "label": "Vísir",
        "type": "news",
        "rss_candidates": ["https://www.visir.is/rss/allt"],
        "exclude_rss_sources": ["https://www.visir.is/rss/skodanir"],
        "page_url": "https://www.visir.is/f/frettir",
    },
    {
        "id": "heimildin_news",
        "label": "Heimildin",
        "type": "news",
        "rss_candidates": [
            "https://heimildin.is/feed/",
            "https://heimildin.is/rss/",
        ],
        "page_url": "https://heimildin.is/",
    },
    {
        "id": "ruv",
        "label": "RÚV",
        "type": "news",
        "rss_candidates": ["https://www.ruv.is/rss/frettir"],
        "page_url": "https://www.ruv.is/frettir",
    },
    {
        "id": "dv",
        "label": "DV",
        "type": "news",
        "rss_candidates": ["https://www.dv.is/feed/"],
        "exclude_url_pattern": "/eyjan/",  # Eyjan goes to opinion
        "page_url": "https://www.dv.is/",
    },
]

SOURCES = OPINION_SOURCES + NEWS_SOURCES

MAX_ARTICLES_PER_SOURCE = 60
LOOKBACK_HOURS = 365
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "feed.json")

# ── RSS / HTML FETCHING ───────────────────────────────────────────────────────

DATE_FORMATS = [
    "%a, %d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S %Z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
]

def parse_date(s):
    for fmt in DATE_FORMATS:
        try:
            return datetime.datetime.strptime(s.strip(), fmt)
        except ValueError:
            pass
    return None

def http_get(url, timeout=15):
    try:
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; SentimentBot/1.0; +https://github.com)",
            "Accept": "application/rss+xml, application/xml, text/xml, text/html, */*",
        })
        with urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace"), r.headers.get("Content-Type", "")
    except Exception as e:
        print(f"    ↳ fetch failed: {e}")
        return None, None

def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    entities = {
        "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
        "&apos;": "'", "&hellip;": "…", "&mdash;": "—", "&ndash;": "–",
        "&bdquo;": "„", "&ldquo;": "\u201c", "&rdquo;": "\u201d",
        "&laquo;": "«", "&raquo;": "»", "&nbsp;": " ",
    }
    for ent, char in entities.items():
        s = s.replace(ent, char)
    s = re.sub(r"&[a-zA-Z]{2,8};", " ", s)
    # Decode numeric entities: &#123; (decimal) and &#xF0; (hex)
    s = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"&#([0-9]+);", lambda m: chr(int(m.group(1))), s)
    return re.sub(r"\s+", " ", s).strip()

def extract_rss_items(xml_text, source, cutoff):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"    ↳ XML parse error: {e}")
        return []

    items = (root.findall(".//item") or
             root.findall(".//{http://www.w3.org/2005/Atom}entry"))
    def find_el(item, tag, atom_tag):
        el = item.find(tag)
        if el is not None:
            return el
        return item.find(atom_tag)

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    results = []
    for item in items:
        t = find_el(item, "title", "{http://www.w3.org/2005/Atom}title")
        title = strip_html(t.text or "") if t is not None else ""
        if not title:
            continue

        l = find_el(item, "link", "{http://www.w3.org/2005/Atom}link")
        url = (l.text or l.get("href", "")).strip() if l is not None else ""

        p = find_el(item, "pubDate", "{http://www.w3.org/2005/Atom}published")
        published = parse_date((p.text or "").strip()) if p is not None else None

        if published:
            aware_cutoff = cutoff.replace(tzinfo=datetime.timezone.utc) if not cutoff.tzinfo else cutoff
            if published < aware_cutoff:
                continue

        d = find_el(item, "description", "{http://www.w3.org/2005/Atom}summary")
        if d is None:
            d = item.find("{http://purl.org/rss/1.0/modules/content/}encoded")
        summary = strip_html(d.text or "")[:500] if d is not None else ""

        # Category filter — if the source requires a specific category, skip non-matches
        cat_filter = source.get("category_filter")
        if cat_filter:
            cats = [strip_html(c.text or "").lower() for c in item.findall("category")]
            if not any(cat_filter.lower() in c for c in cats):
                continue

        # URL pattern exclusion (e.g. exclude /eyjan/ from DV general feed)
        exclude = source.get("exclude_url_pattern")
        if exclude and exclude in url:
            continue

        aid = f"{source['id']}-{hashlib.md5(url.encode()).hexdigest()[:8]}"
        results.append({
            "id": aid,
            "source": source["id"] + ".is",
            "source_label": source["label"],
            "title": title,
            "url": url or source["page_url"],
            "published": published.isoformat() if published else now_utc.isoformat(),
            "first_seen": datetime.date.today().isoformat(),
            "summary": summary,
        })
    return results[:MAX_ARTICLES_PER_SOURCE]

def fetch_article_text(url, max_chars=3000):
    """Fetch full article body text from a URL, stripping boilerplate."""
    body, _ = http_get(url, timeout=15)
    if not body:
        return None
    # Remove script/style blocks entirely
    body = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", " ", body, flags=re.DOTALL | re.IGNORECASE)
    # Extract text from paragraph tags preferentially
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", body, re.DOTALL | re.IGNORECASE)
    if paragraphs:
        text = " ".join(strip_html(p) for p in paragraphs)
    else:
        text = strip_html(body)
    text = re.sub(r"\s+", " ", text).strip()
    # Reject navigation/boilerplate: real article text has long sentences
    # If no single space-separated chunk is longer than 60 chars, it's a menu
    chunks = [c.strip() for c in re.split(r"[.!?]", text) if len(c.strip()) > 60]
    if len(chunks) < 2:
        return None
    # Trim to max_chars at a word boundary
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "…"
    return text if len(text) > 100 else None

def fetch_source(source, cutoff):
    print(f"  📡 {source['label']}")
    for rss_url in source.get("rss_candidates", []):
        print(f"    trying RSS: {rss_url}")
        body, _ = http_get(rss_url)
        if body and ("<item" in body or "<entry" in body):
            items = extract_rss_items(body, source, cutoff)
            print(f"    ✓ {len(items)} articles — fetching full text...")
            for item in items:
                full_text = fetch_article_text(item["url"])
                if full_text:
                    item["summary"] = full_text
                time.sleep(0.5)
            return items
    print(f"    ✗ all RSS candidates failed")
    return []

# ── CLAUDE API CALLS ──────────────────────────────────────────────────────────

def claude_api(prompt, max_tokens=2000):
    if not ANTHROPIC_API_KEY:
        return None
    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    try:
        conn = http.client.HTTPSConnection("api.anthropic.com", timeout=40)
        conn.request("POST", "/v1/messages", body=payload, headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        })
        r = conn.getresponse()
        body = r.read().decode()
        conn.close()
        if r.status != 200:
            print(f"    ⚠ API {r.status}: {body[:200]}")
            return None
        data = json.loads(body)
        return data["content"][0]["text"].strip()
    except Exception as e:
        print(f"    ⚠ API call failed: {e}")
        return None

def extract_json(text):
    """Find and parse the first complete JSON array or object, ignoring trailing text."""
    for start_char, end_char in [('[', ']'), ('{', '}')]:
        idx = text.find(start_char)
        if idx == -1:
            continue
        depth, in_str, i = 0, False, idx
        while i < len(text):
            ch = text[i]
            if in_str:
                if ch == '\\':
                    i += 1
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == start_char:
                    depth += 1
                elif ch == end_char:
                    depth -= 1
                    if depth == 0:
                        return json.loads(text[idx:i+1])
            i += 1
    return None

# ── STEP 1: EU RELEVANCE FILTER ───────────────────────────────────────────────

EU_CONTEXT = """
Í ágúst 2026 fara fram kosningar á Íslandi þar sem ESB-aðild og samningaviðræður
við Evrópusambandið eru í brennidepli. Umræðan snýst um hvort Ísland eigi að sækja
um aðild að ESB, endurskoðun EES-samningsins, fullveldi, fiskveiðar, gjaldmiðil og
tengsl við Evrópu almennt.
"""

def filter_eu_relevant(articles):
    if not articles:
        return []
    if not ANTHROPIC_API_KEY:
        # Without API key, keep all articles (can't filter)
        for a in articles:
            a["eu_relevant"] = True
        return articles

    numbered = "\n".join(
        f"{i+1}. {a['title']}\n   {a['summary']}"
        for i, a in enumerate(articles)
    )
    is_opinion = not any(a.get("type") == "news" for a in articles)
    type_req = (
        "1. FORM: Greinin er skoðunargrein, pistill, leiðari eða greinagerð með skýra afstöðu höfundar."
        if is_opinion else
        "1. FORM: Greinin er fréttamiðlun eða greiningargrein — ekki skoðunargrein."
    )

    prompt = f"""Þú ert greiningarsérfræðingur á íslenskum fjölmiðlum. Svaraðu AÐEINS með JSON — ekkert annað.

SAMHENGI: {EU_CONTEXT}

Greinar þurfa að uppfylla BÁÐAR kröfur:

{type_req}

2. EFNI: MEGINEFNI greinarinnar fjallar um þjóðaratkvæðið í ágúst, ESB-aðild Íslands,
   eða bein pólitísk eða efnahagsleg áhrif ESB-ferils á Ísland.

   EKKI viðeigandi (merktu relevant: false):
   - Greinar þar sem ESB er aðeins nefnt í einni setningu í öðru samhengi
   - Efnahagslegar greinar um vexti, húsnæði, laun — nema þær tengi þetta beint ESB
   - Almennar stjórnmálagreinar sem fjalla ekki sérstaklega um þjóðaratkvæðið
   - Fréttir af erlendum ESB-málum sem varða ekki Ísland

{numbered}

JSON fylki — engin texti fyrir eða eftir:
[
  {{"index": 1, "relevant": true/false, "reason": "ein setning"}},
  ...
]"""
    text = claude_api(prompt, max_tokens=800)
    if not text:
        for a in articles:
            a["eu_relevant"] = True
        return articles

    try:
        results = extract_json(text)
        rmap = {r["index"]: r for r in results}
        for i, a in enumerate(articles):
            r = rmap.get(i + 1, {})
            a["eu_relevant"] = bool(r.get("relevant", True))
        kept = [a for a in articles if a["eu_relevant"]]
        dropped = len(articles) - len(kept)
        if dropped:
            print(f"    → {dropped} article(s) filtered out (not EU-relevant)")
        return kept
    except Exception as e:
        print(f"    ⚠ Relevance parse failed: {e}")
        for a in articles:
            a["eu_relevant"] = True
        return articles

# ── STEP 2: SENTIMENT ANALYSIS ────────────────────────────────────────────────

def analyze_sentiment(articles):
    if not articles:
        return articles
    if not ANTHROPIC_API_KEY:
        for a in articles:
            a.update({"sentiment": "neutral", "sentiment_score": 0.0, "sentiment_reason": "API key vantar."})
        return articles

    numbered = "\n".join(
        f"{i+1}. [{a['source_label']}] {a['title']}\n   {a['summary']}"
        for i, a in enumerate(articles)
    )
    prompt = f"""Greindu eftirfarandi pistla/greinar um ESB-málið á Íslandi ítarlega.

{numbered}

Svaraðu EINUNGIS með JSON fylki — engin önnur texti:
[
  {{
    "index": 1,
    "sentiment": "positive" | "negative" | "neutral",
    "sentiment_score": <-1.0 til 1.0>,
    "sentiment_reason": "<ein setning: hvert er meginviðhorf höfundar?>",
    "main_argument": "<ein setning: hvert er aðalrökstuðningur greinarinnar?>",
    "directed_at": "<hverjum er greinin beint að, t.d. ríkisstjórnin, ESB, almenningur, stjórnarandstaðan>",
    "conclusion": "<ein setning: hvaða niðurstöðu eða áskorun kemst höfundur að?>"
  }},
  ...
]

- "positive": jákvæður tónn gagnvart ESB-aðild / samningum
- "negative": neikvæður tónn, gagnrýni, andstaða
- "neutral": hlutlæg greining, blandaðar skoðanir
- Öll svör skulu vera á íslensku
"""
    text = claude_api(prompt, max_tokens=1200)
    if not text:
        print(f"    ⚠ Sentiment API call failed — retrying one by one...")
        return _analyze_one_by_one(articles)

    try:
        results = extract_json(text)
        rmap = {r["index"]: r for r in results}
        for i, a in enumerate(articles):
            r = rmap.get(i + 1, {})
            a["sentiment"] = r.get("sentiment", "neutral")
            a["sentiment_score"] = float(r.get("sentiment_score", 0.0))
            a["sentiment_reason"] = r.get("sentiment_reason", "")
            a["main_argument"] = r.get("main_argument", "")
            a["directed_at"] = r.get("directed_at", "")
            a["conclusion"] = r.get("conclusion", "")
    except Exception as e:
        print(f"    ⚠ Sentiment parse failed: {e} — retrying one by one...")
        return _analyze_one_by_one(articles)
    return articles


def _analyze_one_by_one(articles):
    """Fallback: analyze each article individually when batch JSON parsing fails."""
    for a in articles:
        prompt = f"""Greindu þennan pistil um ESB-málið á Íslandi ítarlega.

Titill: {a['title']}
Efni: {a['summary']}

Svaraðu EINUNGIS með JSON hlut:
{{
  "sentiment": "positive" | "negative" | "neutral",
  "sentiment_score": <-1.0 til 1.0>,
  "sentiment_reason": "<ein setning: hvert er meginviðhorf höfundar?>",
  "main_argument": "<ein setning: hvert er aðalrökstuðningur greinarinnar?>",
  "directed_at": "<hverjum er greinin beint að>",
  "conclusion": "<ein setning: hvaða niðurstöðu kemst höfundur að?>"
}}
"""
        text = claude_api(prompt, max_tokens=400)
        if not text:
            a.setdefault("sentiment", "neutral")
            a.setdefault("sentiment_score", 0.0)
            a.setdefault("sentiment_reason", "Greining mistókst.")
            a.setdefault("main_argument", "")
            a.setdefault("directed_at", "")
            a.setdefault("conclusion", "")
            continue
        try:
            r = extract_json(text)
            a["sentiment"] = r.get("sentiment", "neutral")
            a["sentiment_score"] = float(r.get("sentiment_score", 0.0))
            a["sentiment_reason"] = r.get("sentiment_reason", "")
            a["main_argument"] = r.get("main_argument", "")
            a["directed_at"] = r.get("directed_at", "")
            a["conclusion"] = r.get("conclusion", "")
            print(f"      ✓ {a['title'][:50]}")
        except Exception as e:
            print(f"      ⚠ Failed for '{a['title'][:40]}': {e}")
            a.setdefault("sentiment", "neutral")
            a.setdefault("sentiment_score", 0.0)
            a.setdefault("sentiment_reason", "Greining mistókst.")
            a.setdefault("main_argument", "")
            a.setdefault("directed_at", "")
            a.setdefault("conclusion", "")
        time.sleep(1)
    return articles

# ── FRAMING ANALYSIS ──────────────────────────────────────────────────────────

def analyze_framing(articles):
    """Analyze news articles for framing — what angle/picture is being constructed."""
    if not articles:
        return articles
    if not ANTHROPIC_API_KEY:
        for a in articles:
            a.update({"framing": "", "framing_description": "", "voices_centered": "", "conclusion": "", "framing_direction": "neutral", "framing_score": 0.0})
        return articles

    numbered = "\n".join(
        f"{i+1}. [{a['source_label']}] {a['title']}\n   {a['summary']}"
        for i, a in enumerate(articles)
    )

    prompt = f"""Þú ert sérfræðingur í framsetningu og fjölmiðlagreiningu.

SAMHENGI: Ísland er að undirbúa þjóðaratkvæðagreiðslu um ESB-aðild í ágúst 2026.

Greindu hvernig hver fréttargrein FRAMSETIR þetta efni — ekki hvort hún er jákvæð eða neikvæð,
heldur HVAÐA HORN og MYND höfundur er að byggja upp í huga lesandans.

{numbered}

Svaraðu EINUNGIS með JSON fylki — engin önnur texti:
[
  {{
    "index": 1,
    "framing": "<stutt merki, t.d. 'Lýðræðisleg hætta', 'Efnahagsleg tækifæri', 'Pólitískt leikfang', 'Hlutlæg upplýsingagjöf', 'Fullveldisógn', 'Söguleg þýðing'>",
    "framing_description": "<ein setning: hvaða horn/sjónarhorn er greinin sett fram frá?>",
    "voices_centered": "<hvers rödd/sjónarmiðar eru í fyrirrúmi í greininni?>",
    "conclusion": "<ein setning: hvaða mynd er lesandinn skilinn eftir með?>",
    "framing_direction": "positive" | "negative" | "neutral",
    "framing_score": <-1.0 til 1.0 — hversu sterkt er framsetningin í þessa átt>
  }},
  ...
]

- "positive": framsetningin hlynnar þjóðaratkvæðinu / ESB-ferli
- "negative": framsetningin gagnrýnir eða tortryggir þjóðaratkvæðið / ESB-ferli  
- "neutral": hlutlæg framsetning án greinilegrar áhrifatilhneigingar
- Öll svör skulu vera á íslensku nema framing_direction og framing_score
"""

    text = claude_api(prompt, max_tokens=1500)
    if not text:
        print(f"    ⚠ Framing API call failed — retrying one by one...")
        return _frame_one_by_one(articles)

    try:
        results = extract_json(text)
        rmap = {r["index"]: r for r in results}
        for i, a in enumerate(articles):
            r = rmap.get(i + 1, {})
            a["framing"]             = r.get("framing", "")
            a["framing_description"] = r.get("framing_description", "")
            a["voices_centered"]     = r.get("voices_centered", "")
            a["conclusion"]          = r.get("conclusion", "")
            a["framing_direction"]   = r.get("framing_direction", "neutral")
            a["framing_score"]       = float(r.get("framing_score", 0.0))
    except Exception as e:
        print(f"    ⚠ Framing parse failed: {e} — retrying one by one...")
        return _frame_one_by_one(articles)
    return articles


def _frame_one_by_one(articles):
    for a in articles:
        prompt = f"""Greindu hvernig þessi fréttargrein um ESB/þjóðaratkvæðagreiðsluna framsetir efnið.

Titill: {a['title']}
Efni: {a['summary']}

Svaraðu EINUNGIS með JSON hlut:
{{
  "framing": "<stutt merki fyrir framsetninguna>",
  "framing_description": "<ein setning: hvaða horn er greinin sett fram frá?>",
  "voices_centered": "<hvers rödd er í fyrirrúmi?>",
  "conclusion": "<ein setning: hvaða mynd er lesandinn skilinn eftir með?>",
  "framing_direction": "positive" | "negative" | "neutral",
  "framing_score": <-1.0 til 1.0>
}}
"""
        text = claude_api(prompt, max_tokens=400)
        defaults = {"framing": "", "framing_description": "", "voices_centered": "", "conclusion": "", "framing_direction": "neutral", "framing_score": 0.0}
        if not text:
            for k, v in defaults.items():
                a.setdefault(k, v)
            continue
        try:
            r = extract_json(text)
            a["framing"]             = r.get("framing", "")
            a["framing_description"] = r.get("framing_description", "")
            a["voices_centered"]     = r.get("voices_centered", "")
            a["conclusion"]          = r.get("conclusion", "")
            a["framing_direction"]   = r.get("framing_direction", "neutral")
            a["framing_score"]       = float(r.get("framing_score", 0.0))
            print(f"      ✓ {a['title'][:50]}")
        except Exception as e:
            print(f"      ⚠ Failed for '{a['title'][:40]}': {e}")
            for k, v in defaults.items():
                a.setdefault(k, v)
        time.sleep(1)
    return articles

def load_existing():
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"updated": "", "articles": []}

def main():
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=LOOKBACK_HOURS)
    today = datetime.date.today().isoformat()
    print(f"🗓  {today} — fetching articles since {cutoff.strftime('%Y-%m-%d %H:%M')} UTC\n")

    existing = load_existing()
    existing_urls = {a["url"] for a in existing.get("articles", [])}
    existing_ids  = {a["id"]  for a in existing.get("articles", [])}

    opinion_candidates = []
    news_candidates    = []
    seen_this_run      = set()

    for source in SOURCES:
        items = fetch_source(source, cutoff)
        new = [a for a in items
               if a["url"] not in existing_urls
               and a["id"] not in existing_ids
               and a["url"] not in seen_this_run]
        print(f"    {len(new)} new (of {len(items)} fetched)\n")
        if source.get("type") == "news":
            news_candidates.extend(new)
        else:
            opinion_candidates.extend(new)
        seen_this_run.update(a["url"] for a in new)
        time.sleep(1)

    analyzed = []

    # ── OPINION PIPELINE ──────────────────────────────────────────────────────
    if opinion_candidates:
        print(f"🔍 Checking EU relevance for {len(opinion_candidates)} opinion candidates...")
        BATCH = 10
        eu_opinion = []
        for i in range(0, len(opinion_candidates), BATCH):
            batch = opinion_candidates[i:i+BATCH]
            eu_opinion.extend(filter_eu_relevant(batch))
            if i + BATCH < len(opinion_candidates):
                time.sleep(2)
        for a in eu_opinion:
            a.pop("eu_relevant", None)
            a["type"] = "opinion"

        print(f"\n🧠 Sentiment analysis for {len(eu_opinion)} EU-relevant opinion articles...")
        for i in range(0, len(eu_opinion), 8):
            batch = eu_opinion[i:i+8]
            analyzed.extend(analyze_sentiment(batch))
            if i + 8 < len(eu_opinion):
                time.sleep(2)

    # ── NEWS PIPELINE ─────────────────────────────────────────────────────────
    if news_candidates:
        print(f"\n🔍 Checking EU relevance for {len(news_candidates)} news candidates...")
        BATCH = 10
        eu_news = []
        for i in range(0, len(news_candidates), BATCH):
            batch = news_candidates[i:i+BATCH]
            eu_news.extend(filter_eu_relevant(batch))
            if i + BATCH < len(news_candidates):
                time.sleep(2)
        for a in eu_news:
            a.pop("eu_relevant", None)
            a["type"] = "news"

        print(f"\n📰 Framing analysis for {len(eu_news)} EU-relevant news articles...")
        for i in range(0, len(eu_news), 8):
            batch = eu_news[i:i+8]
            analyzed.extend(analyze_framing(batch))
            if i + 8 < len(eu_news):
                time.sleep(2)

    all_articles = existing.get("articles", []) + analyzed
    all_articles.sort(key=lambda a: a.get("published", ""), reverse=True)

    output = {
        "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "articles": all_articles,
    }
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    n_opinion = len([a for a in analyzed if a.get("type") == "opinion"])
    n_news    = len([a for a in analyzed if a.get("type") == "news"])
    print(f"\n✅ Archive: {len(all_articles)} total (+{n_opinion} opinion, +{n_news} news today)")

if __name__ == "__main__":
    main()
