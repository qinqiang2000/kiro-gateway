# -*- coding: utf-8 -*-

"""Gateway-side web_search agentic loop for the Quick backend (Path B).

Amazon Quick's Bedrock backend does not run Anthropic's native ``web_search``
server tool, so when the model asks to search, the gateway executes the search
itself (:mod:`quick.websearch`), feeds the results back as a Converse
``toolResult``, and re-invokes the model — looping until the model stops calling
``web_search``. This mirrors kiro-gateway's Path B.

Only ``web_search`` is intercepted. Any other tool the model calls is left as a
normal ``tool_use`` for the client to handle (the loop returns immediately so the
client's own tool round-trip proceeds). ``web_fetch`` is never intercepted — it
already works as a pass-through.
"""

import json
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from loguru import logger

from quick.client import converse_stream
from quick.streaming import ConverseAggregator, EventStreamDecoder
from quick.websearch import search_web

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from quick.pool import Account

# Safety bound: never loop the model↔search cycle more than this many times.
MAX_SEARCH_ROUNDS = 5
# Max results fed back per search.
SEARCH_MAX_RESULTS = 6


def _format_results_text(query: str, results: List[Dict[str, str]]) -> str:
    """Render search results as the text handed back to the model."""
    if not results:
        return f'No web search results found for "{query}".'
    lines = [f'Web search results for "{query}":', ""]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        if r.get("url"):
            lines.append(f"   URL: {r['url']}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
        lines.append("")
    return "\n".join(lines)


async def _run_one_round(
    converse_input: Dict, message_id: str, model: str, account: Optional["Account"] = None
) -> ConverseAggregator:
    """Run a single Converse stream call and return the filled aggregator."""
    aggregator = ConverseAggregator(message_id, model)
    decoder = EventStreamDecoder()
    async for chunk in converse_stream(converse_input, account):
        for event in decoder.feed(chunk):
            aggregator.add(event)
    return aggregator


def _extract_websearch_calls(message: Dict) -> List[Dict]:
    """Return the web_search tool_use blocks in an assistant message (may be empty)."""
    return [
        b for b in message.get("content", [])
        if b.get("type") == "tool_use" and b.get("name") == "web_search"
    ]


def _anthropic_content_to_converse(content: List[Dict]) -> List[Dict]:
    """Convert an assistant message's content blocks to Converse blocks (for replay)."""
    out: List[Dict] = []
    for b in content:
        t = b.get("type")
        if t == "text" and b.get("text"):
            out.append({"text": b["text"]})
        elif t == "tool_use":
            out.append({"toolUse": {
                "toolUseId": b.get("id", ""),
                "name": b.get("name", ""),
                "input": b.get("input", {}),
            }})
        # thinking blocks are not replayed back to Converse (no stable signature contract here)
    return out or [{"text": ""}]


async def run_websearch_loop(
    converse_input: Dict, message_id: str, model: str, account: Optional["Account"] = None
) -> Tuple[Optional[Dict], Optional[str]]:
    """Drive the model↔web_search loop and return (final_anthropic_message, error).

    The model may call ``web_search`` zero or more times; each call is executed by
    the gateway and fed back, until the model produces a non-search answer (or a
    non-web_search tool_use, which is returned as-is for the client to handle). Every
    round runs on the same pool account, so one answer is metered against one account.

    Returns:
        (message, None) on success, or (None, error_string) if the backend errored.
    """
    messages: List[Dict] = converse_input.get("messages", [])
    for round_idx in range(MAX_SEARCH_ROUNDS):
        aggregator = await _run_one_round(converse_input, message_id, model, account)
        if aggregator.error:
            return None, aggregator.error
        message = aggregator.result()

        ws_calls = _extract_websearch_calls(message)
        if not ws_calls:
            # No gateway-handled search this round → this is the final answer
            # (or a client-side tool_use we pass straight back).
            return message, None

        # Replay the assistant turn (with its tool_use blocks) into the transcript.
        messages.append({"role": "assistant",
                         "content": _anthropic_content_to_converse(message["content"])})
        # Execute each web_search and build one user turn of toolResults.
        tool_results: List[Dict] = []
        for call in ws_calls:
            query = (call.get("input") or {}).get("query", "")
            results = await search_web(query, max_results=SEARCH_MAX_RESULTS) if query else []
            tool_results.append({"toolResult": {
                "toolUseId": call.get("id", ""),
                "content": [{"text": _format_results_text(query, results)}],
                "status": "success",
            }})
        messages.append({"role": "user", "content": tool_results})
        converse_input["messages"] = messages
        logger.info("web_search loop round {} executed {} search(es)", round_idx + 1, len(ws_calls))

    # Hit the round cap: run one final round with web_search forced off so the model
    # answers with whatever it has instead of looping forever.
    logger.warning("web_search loop hit MAX_SEARCH_ROUNDS={}, forcing a final answer", MAX_SEARCH_ROUNDS)
    converse_input.pop("toolConfig", None)
    aggregator = await _run_one_round(converse_input, message_id, model, account)
    if aggregator.error:
        return None, aggregator.error
    return aggregator.result(), None
