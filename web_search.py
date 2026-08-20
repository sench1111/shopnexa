"""Reliable, dependency-light web search for ShopNexa Work AI.

Provider order:
1. Tavily when TAVILY_API_KEY is configured.
2. DuckDuckGo HTML as a no-key fallback.

This module returns public search results only. It never sends ShopNexa
credentials, session cookies, CSRF tokens, password hashes or API keys in
the query.
"""
import html
import json
import os
import re
from urllib.parse import quote_plus, unquote
from urllib.request import Request, urlopen


def _clean_html(value):
    value = html.unescape(value or "")
    value = re.sub(r"<script.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _ddg(query, limit=5):
    url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; ShopNexa-Work-AI/2.0)",
            "Accept-Language": "en-US,en;q=0.8",
        },
    )
    with urlopen(req, timeout=int(os.getenv("WEB_SEARCH_TIMEOUT", "12"))) as response:
        body = response.read().decode("utf-8", "ignore")

    results = []
    # DDG sometimes changes wrapper markup, so parse each result link and
    # then find its nearest useful snippet rather than relying on one giant
    # wrapper regex.
    link_matches = list(re.finditer(
        r'<a[^>]+class=["\'][^"\']*result__a[^"\']*["\'][^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        body, flags=re.I | re.S
    ))
    for match in link_matches[:limit]:
        href = html.unescape(match.group(1))
        title = _clean_html(match.group(2))
        if href.startswith("//"):
            href = "https:" + href

        # DDG can wrap destination URLs in redirect parameters.
        if "uddg=" in href:
            m = re.search(r"[?&]uddg=([^&]+)", href)
            if m:
                href = unquote(m.group(1))

        tail = body[match.end():match.end() + 3000]
        sm = re.search(
            r'class=["\'][^"\']*result__snippet[^"\']*["\'][^>]*>(.*?)</(?:a|div)>',
            tail, flags=re.I | re.S
        )
        snippet = _clean_html(sm.group(1)) if sm else ""
        results.append({"title": title, "url": href, "snippet": snippet})

    # De-duplicate by URL while preserving ranking.
    unique = []
    seen = set()
    for result in results:
        if result["url"] and result["url"] not in seen:
            seen.add(result["url"])
            unique.append(result)
    return unique[:limit]


def _tavily(query, limit=5):
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        return []
    payload = json.dumps({
        "api_key": key,
        "query": query,
        "max_results": limit,
        "search_depth": "advanced",
        "include_answer": False,
    }).encode("utf-8")
    req = Request(
        "https://api.tavily.com/search",
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "ShopNexa-Work-AI/2.0"},
    )
    with urlopen(req, timeout=int(os.getenv("WEB_SEARCH_TIMEOUT", "15"))) as response:
        data = json.loads(response.read().decode("utf-8"))
    return [
        {
            "title": str(item.get("title", "")),
            "url": str(item.get("url", "")),
            "snippet": str(item.get("content", "")),
        }
        for item in data.get("results", [])[:limit]
    ]


def search_web(query, limit=5):
    query = (query or "").strip()
    if not query:
        return []

    try:
        results = _tavily(query, limit)
        if results:
            return results
    except Exception:
        pass

    try:
        return _ddg(query, limit)
    except Exception:
        return []
