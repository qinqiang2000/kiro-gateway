# -*- coding: utf-8 -*-

"""Web search execution for the Quick gateway (Path B, key-less).

Amazon Quick's Bedrock backend does not expose Anthropic's native server-side
``web_search`` tool, so the gateway executes the search itself and feeds the
results back into the conversation (mirroring kiro-gateway's Path B). The source
is DuckDuckGo's HTML endpoint — no API key, no extra dependency (uses httpx),
verified reachable from the deploy box.

``web_fetch`` needs no such handling: it already works end-to-end as a pass-through.
"""

import html
import re
from typing import Dict, List
from urllib.parse import unquote

import httpx
from loguru import logger

DUCKDUCKGO_HTML_URL = "https://duckduckgo.com/html/"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"

# DuckDuckGo HTML layout: the class attribute precedes href on the result anchor,
# e.g. <a ... class="result__a" href="//duckduckgo.com/l/?uddg=<encoded>">Title</a>.
_RESULT_A = re.compile(r'class="result__a"[^>]*?href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_SNIPPET = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S)


def _clean(text: str) -> str:
    """Strip HTML tags + unescape entities from a DuckDuckGo fragment."""
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _unwrap_ddg_url(href: str) -> str:
    """DuckDuckGo wraps result links as /l/?uddg=<encoded>. Unwrap to the real URL."""
    m = re.search(r"[?&]uddg=([^&]+)", href)
    if m:
        return unquote(m.group(1))
    if href.startswith("//"):
        return "https:" + href
    return href


async def search_web(query: str, max_results: int = 6) -> List[Dict[str, str]]:
    """Run one DuckDuckGo search and return a list of {title, url, snippet}.

    Args:
        query: The search query.
        max_results: Maximum results to return.

    Returns:
        A list of result dicts (possibly empty). Never raises for a normal empty
        result set; network/parse errors are logged and yield an empty list so the
        caller can still return a graceful "no results" to the model.
    """
    params = {"q": query}
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(
                DUCKDUCKGO_HTML_URL, params=params, headers={"User-Agent": _UA}
            )
        if resp.status_code != 200:
            logger.warning("DuckDuckGo search HTTP {} for query {!r}", resp.status_code, query)
            return []
        body = resp.text
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.warning("DuckDuckGo search failed for {!r}: {}", query, exc)
        return []

    anchors = _RESULT_A.findall(body)
    snippets = _SNIPPET.findall(body)
    results: List[Dict[str, str]] = []
    for i, (href, title_html) in enumerate(anchors[:max_results]):
        snippet = _clean(snippets[i]) if i < len(snippets) else ""
        results.append(
            {
                "title": _clean(title_html),
                "url": _unwrap_ddg_url(href),
                "snippet": snippet,
            }
        )
    logger.info("web_search {!r} -> {} results", query, len(results))
    return results
