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

SOURCES = [
    {
        # Morgunblaðið editorials — confirmed RSS from mbl.is/feeds/
        "id": "mbl",
        "label": "Morgunblaðið",
        "rss_candidates": [
            "https://www.mbl.is/feeds/mogginn/leidarar/",
        ],
        "page_url": "https://www.mbl.is/vidskipti/pistlar/",
    },
    {
        # blog.is — hosted and served by mbl.is, RSS confirmed
        "id": "blog",
        "label": "Blog.is",
        "rss_candidates": [
            "https://www.mbl.is/feeds/blog/",
        ],
        "page_url": "https://www.blog.is/forsida/",
    },
    {
        "id": "visir",
        "label": "Vísir",
        "rss_candidates": [
            "https://www.visir.is/rss/allt",
        ],
        "page_url": "https://www.visir.is/f/skodanir",
    },
    {
        "id": "heimildin",
        "label": "Heimildin",
        "rss_candidates": [
            "https://heimildin.is/feed/",
            "https://heimildin.is/rss/",
            "https://heimildin.is/umraeda/feed/",
        ],
        "page_url": "https://heimildin.is/umraeda/",
    },
]

MAX_ARTICLES_PER_SOURCE = 6
LOOKBACK_HOURS = 36
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

def fetch_source(source, cutoff):
    print(f"  📡 {source['label']}")
    for rss_url in source.get("rss_candidates", []):
        print(f"    trying RSS: {rss_url}")
        body, _ = http_get(rss_url)
        if body and ("<item" in body or "<entry" in body):
            items = extract_rss_items(body, source, cutoff)
            print(f"    ✓ {len(items)} articles")
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
    m = re.search(r"(\[.*\]|\{.*\})", text, re.DOTALL)
    return json.loads(m.group()) if m else None

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
    prompt = f"""Þú ert greiningarsérfræðingur á íslenskum fjölmiðlum.

SAMHENGI: {EU_CONTEXT}

Hér eru {len(articles)} greinar/pistlar. Þín verkefni: Greindu hvort hver grein snúist um
þetta efni — beint eða óbeint. Greinar geta vísað til ESB-málsins í myndmáli, 
samlíkingum eða í víðara samhengi án þess að nefna „ESB" beint.

{numbered}

Svaraðu EINUNGIS með JSON fylki — engin önnur texti:
[
  {{"index": 1, "relevant": true/false, "reason": "stuttur skýring á íslensku"}},
  ...
]
"""
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
    prompt = f"""Greindu tilfinningalegan tón hvers pistils/greinar um ESB-málið á Íslandi.

{numbered}

Svaraðu EINUNGIS með JSON fylki:
[
  {{
    "index": 1,
    "sentiment": "positive" | "negative" | "neutral",
    "sentiment_score": <-1.0 til 1.0>,
    "sentiment_reason": "<ein setning á íslensku>"
  }},
  ...
]

- "positive": jákvæður tónn gagnvart ESB-aðild / samningum
- "negative": neikvæður tónn, gagnrýni, andstaða
- "neutral": hlutlæg greining, blandaðar skoðanir
"""
    text = claude_api(prompt, max_tokens=1200)
    if not text:
        for a in articles:
            a.setdefault("sentiment", "neutral")
            a.setdefault("sentiment_score", 0.0)
            a.setdefault("sentiment_reason", "Greining mistókst.")
        return articles

    try:
        results = extract_json(text)
        rmap = {r["index"]: r for r in results}
        for i, a in enumerate(articles):
            r = rmap.get(i + 1, {})
            a["sentiment"] = r.get("sentiment", "neutral")
            a["sentiment_score"] = float(r.get("sentiment_score", 0.0))
            a["sentiment_reason"] = r.get("sentiment_reason", "")
    except Exception as e:
        print(f"    ⚠ Sentiment parse failed: {e}")
        for a in articles:
            a.setdefault("sentiment", "neutral")
            a.setdefault("sentiment_score", 0.0)
            a.setdefault("sentiment_reason", "Greining mistókst.")
    return articles

# ── MAIN ──────────────────────────────────────────────────────────────────────

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
    existing_ids = {a["id"] for a in existing.get("articles", [])}

    fresh_candidates = []
    for source in SOURCES:
        items = fetch_source(source, cutoff)
        new = [a for a in items if a["url"] not in existing_urls and a["id"] not in existing_ids]
        print(f"    {len(new)} new (of {len(items)} fetched)\n")
        fresh_candidates.extend(new)
        time.sleep(1)

    print(f"🔍 Checking EU relevance for {len(fresh_candidates)} candidates...")
    BATCH = 10
    eu_relevant = []
    for i in range(0, len(fresh_candidates), BATCH):
        batch = fresh_candidates[i:i+BATCH]
        eu_relevant.extend(filter_eu_relevant(batch))
        if i + BATCH < len(fresh_candidates):
            time.sleep(2)
    # Clean up helper field
    for a in eu_relevant:
        a.pop("eu_relevant", None)

    print(f"\n🧠 Sentiment analysis for {len(eu_relevant)} EU-relevant articles...")
    analyzed = []
    for i in range(0, len(eu_relevant), 8):
        batch = eu_relevant[i:i+8]
        analyzed.extend(analyze_sentiment(batch))
        if i + 8 < len(eu_relevant):
            time.sleep(2)

    all_articles = existing.get("articles", []) + analyzed
    # Sort by published descending
    all_articles.sort(key=lambda a: a.get("published", ""), reverse=True)

    output = {
        "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "articles": all_articles,
    }
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Archive now has {len(all_articles)} articles (+{len(analyzed)} new today)")

if __name__ == "__main__":
    main()
