from flask import Flask, render_template, request, jsonify
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import os
import re

app = Flask(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (NewsCheck Hackathon/1.0)"
}

NEWSAPI_KEY = os.environ.get("newsapi") or os.environ.get("NEWSAPI_KEY", "")
NEWSAPI_URL = "https://newsapi.org/v2/everything"
NEWSAPI_TIMEOUT = 8
MAX_SOURCES_PER_CLAIM = 4

STOPWORDS = {
    "about", "after", "again", "against", "along", "among", "around",
    "because", "before", "being", "below", "between", "could", "during",
    "every", "first", "found", "their", "there", "these", "thing", "things",
    "those", "through", "under", "until", "where", "which", "while", "whose",
    "would", "should", "might", "other", "another", "still", "since", "years",
    "people", "today", "years", "according", "reported", "report", "says",
    "said", "including", "however", "already", "really", "something",
}

TRUSTED_DOMAINS = {
    "reuters.com": 20,
    "bbc.com": 18,
    "bbc.co.uk": 18,
    "apnews.com": 18,
    "thehindu.com": 16,
    "ndtv.com": 14,
    "who.int": 20,
    "un.org": 20,
    "gov.in": 20,
}


def root_domain(host):
    host = host.lower().split(":")[0]

    if host.startswith("www."):
        host = host[4:]

    parts = host.split(".")

    if len(parts) >= 2:
        return ".".join(parts[-2:])

    return host


def fetch_article(url):

    response = requests.get(
        url,
        headers=HEADERS,
        timeout=12
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    # Remove unnecessary HTML
    for tag in soup([
        "script",
        "style",
        "noscript",
        "svg",
        "nav",
        "footer"
    ]):
        tag.decompose()

    # -------------------------
    # TITLE
    # -------------------------

    title = ""

    og_title = soup.find(
        "meta",
        attrs={"property": "og:title"}
    )

    if og_title and og_title.get("content"):
        title = og_title["content"].strip()

    if not title and soup.title:
        title = soup.title.get_text(
            " ",
            strip=True
        )

    # -------------------------
    # AUTHOR
    # -------------------------

    author = ""

    meta_author = soup.find(
        "meta",
        attrs={"name": "author"}
    )

    if meta_author:
        author = meta_author.get(
            "content",
            ""
        ).strip()

    # -------------------------
    # ARTICLE TEXT
    # -------------------------

    paragraphs = [
        p.get_text(" ", strip=True)
        for p in soup.find_all("p")
    ]

    paragraphs = [
        p for p in paragraphs
        if len(p) > 40
    ]

    text = "\n".join(paragraphs)

    return {
        "title": title[:300] or "Untitled article",
        "author": author or "Not detected",
        "text": text[:30000],
        "html_size": len(response.text)
    }


def extract_claims(title, text):

    sentences = re.split(
        r'(?<=[.!?])\s+',
        (title + ". " + text).strip()
    )

    candidates = []

    trigger_words = [
        "will",
        "has",
        "have",
        "is",
        "are",
        "announced",
        "according",
        "claims",
        "reported",
        "discovered",
        "government",
        "study",
        "research",
        "million",
        "percent",
        "%",
        "first",
        "new",
        "confirmed"
    ]

    for sentence in sentences:

        sentence = re.sub(
            r"\s+",
            " ",
            sentence
        ).strip()

        if (
            55 <= len(sentence) <= 320
            and any(
                word in sentence.lower()
                for word in trigger_words
            )
        ):

            if sentence not in candidates:
                candidates.append(sentence)

        if len(candidates) >= 8:
            break

    # Fallback
    if not candidates:

        for sentence in sentences:

            sentence = re.sub(
                r"\s+",
                " ",
                sentence
            ).strip()

            if 55 <= len(sentence) <= 320:
                candidates.append(sentence)

            if len(candidates) >= 5:
                break

    return candidates[:8]


def language_signal(text):

    sensational_words = [
        "shocking",
        "breaking",
        "you won't believe",
        "miracle",
        "secret",
        "urgent",
        "exposed",
        "100%",
        "guaranteed",
        "!!!",
        "must see",
        "world's biggest",
        "destroyed",
        "scandal"
    ]

    lower_text = text.lower()

    hits = sum(
        lower_text.count(word)
        for word in sensational_words
    )

    score = max(
        4,
        20 - min(16, hits * 4)
    )

    if score >= 16:
        label = "Good"

    elif score >= 10:
        label = "Moderate"

    else:
        label = "Sensational"

    return score, label, hits


def source_score(host):

    domain = root_domain(host)

    if domain in TRUSTED_DOMAINS:
        return TRUSTED_DOMAINS[domain], "Good"

    suspicious_words = [
        "blogspot",
        "wordpress",
        "click",
        "viral",
        "news24",
        "truth",
        "daily"
    ]

    if any(
        word in domain
        for word in suspicious_words
    ):
        return 7, "Poor"

    return 12, "Moderate"


def build_keywords(claim):

    words = re.findall(r"[A-Za-z][A-Za-z0-9'-]+|\d[\d,.]*%?", claim)

    proper_nouns = []
    numbers = []
    common = []

    for index, word in enumerate(words):

        cleaned = word.strip("'-")
        lowered = cleaned.lower()

        if not cleaned or lowered in STOPWORDS:
            continue

        if cleaned[0].isdigit():
            numbers.append(cleaned)

        # Capitalised words that are not simply the first word of the sentence
        elif cleaned[0].isupper() and index > 0:
            proper_nouns.append(cleaned)

        elif len(cleaned) > 4:
            common.append(cleaned)

    ordered = []

    for word in proper_nouns + numbers + common:
        if word.lower() not in (w.lower() for w in ordered):
            ordered.append(word)

    return ordered


def search_newsapi(keywords, exclude_domain):

    query = " ".join(keywords)

    params = {
        "q": query[:500],
        "searchIn": "title,description",
        "language": "en",
        "sortBy": "relevancy",
        "pageSize": 10,
    }

    if exclude_domain:
        params["excludeDomains"] = exclude_domain

    response = requests.get(
        NEWSAPI_URL,
        params=params,
        headers={"X-Api-Key": NEWSAPI_KEY},
        timeout=NEWSAPI_TIMEOUT
    )

    payload = response.json()

    if payload.get("status") != "ok":
        raise RuntimeError(payload.get("message") or f"NewsAPI HTTP {response.status_code}")

    return payload.get("articles", [])


def summarise_sources(articles):

    sources = []
    seen_domains = set()

    for article in articles:

        url = article.get("url") or ""
        domain = root_domain(urlparse(url).netloc) if url else ""

        if not url or domain in seen_domains:
            continue

        seen_domains.add(domain)

        published = (article.get("publishedAt") or "")[:10]

        sources.append({
            "title": (article.get("title") or "Untitled").strip()[:160],
            "outlet": ((article.get("source") or {}).get("name") or domain).strip(),
            "domain": domain,
            "url": url,
            "published": published,
            "trusted": domain in TRUSTED_DOMAINS,
        })

        if len(sources) >= MAX_SOURCES_PER_CLAIM:
            break

    return sources


def classify_sources(sources):

    trusted_count = sum(1 for source in sources if source["trusted"])

    if len(sources) >= 3 or trusted_count >= 1 and len(sources) >= 2:
        return "Supported"

    if sources:
        return "Partially Supported"

    return "Not Found"


def verify_claim(claim, exclude_domain=None):

    keywords = build_keywords(claim)
    query = " ".join(keywords[:6])

    result = {
        "status": "Needs verification",
        "query": query,
        "sources": [],
        "note": "",
    }

    if not NEWSAPI_KEY:
        result["note"] = "NewsAPI key not configured."
        return result

    if len(keywords) < 2:
        result["status"] = "Not Found"
        result["note"] = "Claim too vague to search."
        return result

    try:

        articles = search_newsapi(keywords[:5], exclude_domain)

        # Broad claims often match nothing when every keyword is required,
        # so fall back to the three strongest keywords before giving up.
        if not articles and len(keywords) > 3:
            result["query"] = " ".join(keywords[:3])
            articles = search_newsapi(keywords[:3], exclude_domain)

        sources = summarise_sources(articles)

        result["sources"] = sources
        result["status"] = classify_sources(sources)

        if not sources:
            result["note"] = "No independent coverage found in the last 30 days."

    except (requests.exceptions.RequestException, RuntimeError, ValueError) as error:

        result["status"] = "Unavailable"
        result["note"] = f"Verification service error: {error}"

    return result


def claim_verification_score(verified):

    if not verified:
        return 10, "No claims"

    checked = [v for v in verified if v["status"] not in ("Unavailable", "Needs verification")]

    if not checked:
        return 10, "Unavailable"

    weights = {
        "Supported": 1.0,
        "Partially Supported": 0.6,
        "Not Found": 0.15,
    }

    ratio = sum(weights.get(v["status"], 0) for v in checked) / len(checked)
    points = round(4 + ratio * 16)

    if ratio >= 0.7:
        label = "Corroborated"
    elif ratio >= 0.4:
        label = "Partially corroborated"
    else:
        label = "Weak corroboration"

    return points, label


@app.route("/")
def index():

    return render_template(
        "index.html"
    )


@app.post("/analyze")
def analyze():

    data = request.get_json(
        silent=True
    ) or {}

    url = (
        data.get("url")
        or ""
    ).strip()

    # -------------------------
    # URL VALIDATION
    # -------------------------

    if not re.match(
        r"^https?://",
        url
    ):

        return jsonify({
            "error":
            "Please enter a complete URL beginning with http:// or https://"
        }), 400

    try:

        parsed = urlparse(url)

        article = fetch_article(url)

        domain = root_domain(
            parsed.netloc
        )

        # -------------------------
        # DOMAIN
        # -------------------------

        domain_points, domain_label = source_score(
            parsed.netloc
        )

        # -------------------------
        # LANGUAGE
        # -------------------------

        language_points, language_label, sensational_hits = language_signal(
            article["text"]
        )

        # -------------------------
        # CLAIMS
        # -------------------------

        claims = extract_claims(
            article["title"],
            article["text"]
        )

        if claims:
            with ThreadPoolExecutor(max_workers=min(8, len(claims))) as pool:
                verified = list(pool.map(
                    lambda claim: verify_claim(claim, exclude_domain=domain),
                    claims
                ))
        else:
            verified = []

        supported_count = sum(
            1 for v in verified if v["status"] == "Supported"
        )

        # -------------------------
        # AUTHOR
        # -------------------------

        author_points = (
            12
            if article["author"] != "Not detected"
            else 7
        )

        # -------------------------
        # TRANSPARENCY
        # -------------------------

        transparency_points = (
            14
            if any(
                word in article["text"].lower()
                for word in [
                    "according to",
                    "source:",
                    "study",
                    "report"
                ]
            )
            else 8
        )

        # -------------------------
        # CLAIM SCORE
        # -------------------------

        claim_points, claim_label = claim_verification_score(verified)

        # -------------------------
        # FINAL SCORE
        # -------------------------

        total = min(
            100,
            domain_points
            + language_points
            + author_points
            + transparency_points
            + claim_points
        )

        if total >= 75:

            label = "Likely Reliable"

        elif total >= 50:

            label = "Needs Review"

        else:

            label = "Likely Unreliable"

        return jsonify({

            "ok": True,

            "url": url,

            "domain": domain,

            "title": article["title"],

            "author": article["author"],

            "summary":
                f"NewsCheck analyzed the source, "
                f"article language, transparency, "
                f"author information and cross-checked "
                f"{len(claims)} candidate claims against "
                f"independent news coverage "
                f"({supported_count} supported).",

            "score": total,

            "label": label,

            "signals": [

                {
                    "name": "Domain Reputation",
                    "score": domain_points,
                    "max": 20,
                    "label": domain_label
                },

                {
                    "name": "Website Credibility",
                    "score": transparency_points,
                    "max": 20,
                    "label":
                        "Good"
                        if transparency_points >= 15
                        else "Moderate"
                },

                {
                    "name": "Article Language",
                    "score": language_points,
                    "max": 20,
                    "label": language_label
                },

                {
                    "name": "Claim Verification",
                    "score": claim_points,
                    "max": 20,
                    "label": claim_label
                },

                {
                    "name": "Source Transparency",
                    "score": transparency_points,
                    "max": 20,
                    "label":
                        "Good"
                        if transparency_points >= 15
                        else "Moderate"
                },

                {
                    "name": "Author Information",
                    "score": author_points,
                    "max": 20,
                    "label":
                        "Found"
                        if author_points >= 10
                        else "Missing"
                }
            ],

            "claims": [

                {
                    "id": i + 1,
                    "text": claim,
                    "status": verification["status"],
                    "query": verification["query"],
                    "sources": verification["sources"],
                    "note": verification["note"]
                }

                for i, (
                    claim,
                    verification
                ) in enumerate(
                    zip(
                        claims,
                        verified
                    )
                )
            ],

            "timeline": [

                "URL submitted",
                "Domain investigated",
                "Website & author analysis",
                "Article extracted & parsed",
                f"{len(claims)} claims identified",
                f"Claims cross-checked via NewsAPI ({supported_count} supported)",
                "Credibility report generated"

            ],

            "timestamp":
                datetime.now().strftime(
                    "%I:%M %p"
                )
        })

    except requests.exceptions.RequestException as error:

        return jsonify({
            "error":
                f"Could not fetch that article: {error}"
        }), 502

    except Exception as error:

        return jsonify({
            "error":
                f"Analysis failed: {error}"
        }), 500


if __name__ == "__main__":

    app.run(
        debug=True,
        port=5000
    )
