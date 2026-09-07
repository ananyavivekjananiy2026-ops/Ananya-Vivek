from flask import Flask, render_template, request, jsonify
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from datetime import datetime
import re

app = Flask(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (NewsCheck Hackathon/1.0)"
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


def verify_claim(claim):

    words = re.findall(
        r"[A-Za-z0-9%'-]+",
        claim
    )

    keywords = [
        word
        for word in words
        if len(word) > 4
    ][:10]

    query = " ".join(keywords)

    return {
        "status": "Needs verification",
        "query": query,
        "sources": []
    }


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

        verified = [
            verify_claim(claim)
            for claim in claims
        ]

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

        claim_points = (
            10
            if len(claims) <= 2
            else 8
        )

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
                f"author information and "
                f"{len(claims)} candidate claims.",

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
                    "label": "Needs verification"
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
                    "query": verification["query"]
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
                "Verification queries prepared",
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
