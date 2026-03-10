# -*- coding: utf-8 -*-

# Kiro Gateway
# https://github.com/jwadow/kiro-gateway
# Copyright (C) 2025 Jwadow
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
Pydantic models for Anthropic Messages API.

Defines data schemas for requests and responses compatible with
Anthropic's Messages API specification.

Reference: https://docs.anthropic.com/en/api/messages
"""

import time
from typing import Any, Dict, List, Literal, Optional, Union
from loguru import logger
from pydantic import BaseModel, Field, model_validator


# ==================================================================================================
# Content Block Models
# ==================================================================================================


class TextContentBlock(BaseModel):
    """
    Text content block in Anthropic format.

    Used in both requests and responses for text content.
    """

    type: Literal["text"] = "text"
    text: str


class ThinkingContentBlock(BaseModel):
    """
    Thinking content block in Anthropic format.

    Represents the model's reasoning/thinking process.
    Used when extended thinking is enabled.

    Attributes:
        type: Always "thinking"
        thinking: The thinking/reasoning content
        signature: Cryptographic signature for verification (placeholder in our case)
    """

    type: Literal["thinking"] = "thinking"
    thinking: str
    signature: str = ""


class ToolUseContentBlock(BaseModel):
    """
    Tool use content block in Anthropic format.

    Represents a tool call made by the assistant.
    """

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: Dict[str, Any]


class GenericContentBlock(BaseModel):
    """
    Fallback content block for unknown/unsupported types.

    Catches content block types not explicitly modeled (e.g. tool_reference,
    server_tool_use) so Pydantic validation doesn't reject the entire request.
    The converter layer handles extracting useful data from these blocks.
    """

    type: str
    model_config = {"extra": "allow"}


class ToolResultContentBlock(BaseModel):
    """
    Tool result content block in Anthropic format.

    Represents the result of a tool call, sent by the user.
    Tool results can contain text, images, or a mix of both.
    """

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: Optional[
        Union[
            str,
            List[Union["TextContentBlock", "ImageContentBlock", GenericContentBlock]],
        ]
    ] = None
    is_error: Optional[bool] = None


# ==================================================================================================
# Image Content Block Models
# ==================================================================================================


class Base64ImageSource(BaseModel):
    """
    Base64-encoded image source in Anthropic format.

    Attributes:
        type: Always "base64"
        media_type: MIME type (e.g., "image/jpeg", "image/png", "image/gif", "image/webp")
        data: Base64-encoded image data
    """

    type: Literal["base64"] = "base64"
    media_type: str
    data: str


class URLImageSource(BaseModel):
    """
    URL-based image source in Anthropic format.

    Note: URL images require fetching and converting to base64 for Kiro API.
    Currently logged as warning and skipped.

    Attributes:
        type: Always "url"
        url: HTTP(S) URL to the image
    """

    type: Literal["url"] = "url"
    url: str


class ImageContentBlock(BaseModel):
    """
    Image content block in Anthropic format.

    Represents an image in a message. Supports both base64-encoded
    images and URL references.

    Attributes:
        type: Always "image"
        source: Image source (base64 or URL)
    """

    type: Literal["image"] = "image"
    source: Union[Base64ImageSource, URLImageSource]


# Union type for all content blocks (including images and thinking)
# GenericContentBlock MUST be last — Pydantic tries unions in order,
# and we want specific types to match first.
ContentBlock = Union[
    TextContentBlock,
    ThinkingContentBlock,
    ImageContentBlock,
    ToolUseContentBlock,
    ToolResultContentBlock,
    GenericContentBlock,
]

# Known content block types that we can parse
KNOWN_CONTENT_TYPES = {"text", "thinking", "image", "tool_use", "tool_result"}

# Known content types inside tool_result.content
KNOWN_TOOL_RESULT_CONTENT_TYPES = {"text", "image"}


def _sanitize_tool_result_content(content: Any) -> Any:
    """
    Sanitize the content field inside a tool_result block.

    Removes unknown content types (e.g., tool_reference) from the nested
    content list. These are client-side metadata that the conversion
    pipeline would ignore anyway.

    Args:
        content: The content field of a tool_result block (str, list, or None)

    Returns:
        Sanitized content with unknown types removed
    """
    if not isinstance(content, list):
        return content

    sanitized = []
    for item in content:
        if isinstance(item, dict):
            item_type = item.get("type", "")
            if item_type in KNOWN_TOOL_RESULT_CONTENT_TYPES:
                sanitized.append(item)
            else:
                logger.debug(
                    f"Stripped unknown type '{item_type}' from tool_result.content"
                )
        else:
            sanitized.append(item)

    return sanitized if sanitized else ""


def _sanitize_content_blocks(content: Any) -> Any:
    """
    Sanitize message content by removing unknown content block types.

    Some clients (e.g., Claude Code) send non-standard content types like
    'tool_reference' that are not part of the Anthropic API spec. These blocks
    carry client-side metadata and would be ignored by the conversion pipeline,
    but they cause Pydantic validation failures (422) before reaching conversion.

    This function strips unknown types so the request can pass validation,
    matching the behavior the conversion pipeline would have anyway.

    Args:
        content: Raw message content (str, list of dicts, or other)

    Returns:
        Sanitized content with unknown block types removed
    """
    if not isinstance(content, list):
        return content

    sanitized = []
    for block in content:
        if not isinstance(block, dict):
            sanitized.append(block)
            continue

        block_type = block.get("type", "")

        if block_type in KNOWN_CONTENT_TYPES:
            # For tool_result blocks, also sanitize nested content
            if block_type == "tool_result" and "content" in block:
                block = {**block, "content": _sanitize_tool_result_content(block["content"])}
            sanitized.append(block)
        else:
            logger.debug(
                f"Stripped unknown content block type '{block_type}' from message"
            )

    return sanitized if sanitized else ""


# ==================================================================================================
# Message Models
# ==================================================================================================


class AnthropicMessage(BaseModel):
    """
    Message in Anthropic format.

    Attributes:
        role: Message role (user or assistant)
        content: Message content (string or list of content blocks)
    """

    role: Literal["user", "assistant"]
    content: Union[str, List[ContentBlock]]

    model_config = {"extra": "allow"}

    @model_validator(mode="before")
    @classmethod
    def sanitize_unknown_content_types(cls, data: Any) -> Any:
        """
        Pre-validation sanitizer that removes unknown content block types.

        Clients like Claude Code may send non-standard types (e.g., tool_reference)
        that are not part of the Anthropic API spec. These would be ignored by the
        conversion pipeline but cause 422 validation errors at the Pydantic layer.

        This validator strips them before validation, matching the conversion
        pipeline's existing behavior of ignoring unknown types.

        Args:
            data: Raw input data before Pydantic validation

        Returns:
            Sanitized data with unknown content types removed
        """
        if isinstance(data, dict) and "content" in data:
            data["content"] = _sanitize_content_blocks(data["content"])
        return data


# ==================================================================================================
# Tool Models
# ==================================================================================================


class AnthropicTool(BaseModel):
    """
    Tool definition in Anthropic format.

    Attributes:
        name: Tool name (must match pattern ^[a-zA-Z0-9_-]{1,64}$)
        description: Tool description (optional but recommended)
        input_schema: JSON Schema for tool parameters
    """

    name: str
    description: Optional[str] = None
    input_schema: Dict[str, Any]


class ToolChoiceAuto(BaseModel):
    """Auto tool choice - model decides whether to use tools."""

    type: Literal["auto"] = "auto"


class ToolChoiceAny(BaseModel):
    """Any tool choice - model must use at least one tool."""

    type: Literal["any"] = "any"


class ToolChoiceTool(BaseModel):
    """Specific tool choice - model must use the specified tool."""

    type: Literal["tool"] = "tool"
    name: str


ToolChoice = Union[ToolChoiceAuto, ToolChoiceAny, ToolChoiceTool]


# ==================================================================================================
# Request Models
# ==================================================================================================


class SystemContentBlock(BaseModel):
    """
    System content block for prompt caching.

    Anthropic API supports system as a list of content blocks
    with optional cache_control for prompt caching.
    """

    type: Literal["text"] = "text"
    text: str
    cache_control: Optional[Dict[str, Any]] = None

    model_config = {"extra": "allow"}


# System can be a string or list of content blocks (for prompt caching)
SystemPrompt = Union[str, List[SystemContentBlock], List[Dict[str, Any]]]


class AnthropicMessagesRequest(BaseModel):
    """
    Request to Anthropic Messages API (/v1/messages).

    Attributes:
        model: Model ID (e.g., "claude-sonnet-4-5")
        messages: List of conversation messages
        max_tokens: Maximum tokens in response (required)
        system: System prompt (optional, string or list of content blocks for caching)
        stream: Whether to stream the response
        tools: List of available tools
        tool_choice: Tool selection strategy
        temperature: Sampling temperature (0-1)
        top_p: Top-p sampling
        top_k: Top-k sampling
        stop_sequences: Custom stop sequences
        metadata: Request metadata
    """

    model: str
    messages: List[AnthropicMessage] = Field(min_length=1)
    max_tokens: int

    # Optional parameters - system can be string or list of content blocks
    system: Optional[SystemPrompt] = None
    stream: bool = False

    # Tools
    tools: Optional[List[AnthropicTool]] = None
    tool_choice: Optional[Union[ToolChoice, Dict[str, Any]]] = None

    # Sampling parameters
    temperature: Optional[float] = Field(default=None, ge=0, le=1)
    top_p: Optional[float] = Field(default=None, ge=0, le=1)
    top_k: Optional[int] = Field(default=None, ge=0)

    # Other parameters
    stop_sequences: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None

    model_config = {"extra": "allow"}


# ==================================================================================================
# Response Models
# ==================================================================================================


class AnthropicUsage(BaseModel):
    """
    Token usage information in Anthropic format.

    Attributes:
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
    """

    input_tokens: int
    output_tokens: int


class AnthropicMessagesResponse(BaseModel):
    """
    Response from Anthropic Messages API (non-streaming).

    Attributes:
        id: Unique message ID
        type: Always "message"
        role: Always "assistant"
        content: List of content blocks (may include thinking, text, tool_use)
        model: Model used
        stop_reason: Why generation stopped
        stop_sequence: Stop sequence that triggered stop (if any)
        usage: Token usage information
    """

    id: str
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    content: List[Union[ThinkingContentBlock, TextContentBlock, ToolUseContentBlock]]
    model: str
    stop_reason: Optional[
        Literal["end_turn", "max_tokens", "stop_sequence", "tool_use"]
    ] = None
    stop_sequence: Optional[str] = None
    usage: AnthropicUsage


# ==================================================================================================
# Streaming Event Models
# ==================================================================================================


class MessageStartEvent(BaseModel):
    """
    Event sent at the start of a message stream.

    Contains the initial message object with empty content.
    """

    type: Literal["message_start"] = "message_start"
    message: Dict[str, Any]


class ContentBlockStartEvent(BaseModel):
    """
    Event sent at the start of a content block.

    Attributes:
        index: Index of the content block
        content_block: Initial content block (with empty text for text blocks)
    """

    type: Literal["content_block_start"] = "content_block_start"
    index: int
    content_block: Dict[str, Any]


class TextDelta(BaseModel):
    """Delta for text content."""

    type: Literal["text_delta"] = "text_delta"
    text: str


class ThinkingDelta(BaseModel):
    """Delta for thinking content."""

    type: Literal["thinking_delta"] = "thinking_delta"
    thinking: str


class InputJsonDelta(BaseModel):
    """Delta for tool input JSON."""

    type: Literal["input_json_delta"] = "input_json_delta"
    partial_json: str


class ContentBlockDeltaEvent(BaseModel):
    """
    Event sent when content block is updated.

    Attributes:
        index: Index of the content block being updated
        delta: The delta update (text_delta, thinking_delta, or input_json_delta)
    """

    type: Literal["content_block_delta"] = "content_block_delta"
    index: int
    delta: Union[TextDelta, ThinkingDelta, InputJsonDelta, Dict[str, Any]]


class ContentBlockStopEvent(BaseModel):
    """
    Event sent when a content block is complete.
    """

    type: Literal["content_block_stop"] = "content_block_stop"
    index: int


class MessageDeltaUsage(BaseModel):
    """Usage information in message_delta event."""

    output_tokens: int


class MessageDeltaEvent(BaseModel):
    """
    Event sent near the end of the stream with final message data.

    Attributes:
        delta: Contains stop_reason and stop_sequence
        usage: Output token count
    """

    type: Literal["message_delta"] = "message_delta"
    delta: Dict[str, Any]
    usage: MessageDeltaUsage


class MessageStopEvent(BaseModel):
    """
    Event sent at the end of the message stream.
    """

    type: Literal["message_stop"] = "message_stop"


class PingEvent(BaseModel):
    """
    Ping event sent periodically to keep connection alive.
    """

    type: Literal["ping"] = "ping"


class ErrorEvent(BaseModel):
    """
    Error event sent when an error occurs during streaming.
    """

    type: Literal["error"] = "error"
    error: Dict[str, Any]


# Union of all streaming events
StreamingEvent = Union[
    MessageStartEvent,
    ContentBlockStartEvent,
    ContentBlockDeltaEvent,
    ContentBlockStopEvent,
    MessageDeltaEvent,
    MessageStopEvent,
    PingEvent,
    ErrorEvent,
]


# ==================================================================================================
# Error Models
# ==================================================================================================


class AnthropicErrorDetail(BaseModel):
    """
    Error detail in Anthropic format.
    """

    type: str
    message: str


class AnthropicErrorResponse(BaseModel):
    """
    Error response in Anthropic format.
    """

    type: Literal["error"] = "error"
    error: AnthropicErrorDetail
