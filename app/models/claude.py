from typing import Optional, List, Union, Literal, Dict, Any
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator
from enum import Enum

THINK_MODEL_SUFFIX = "-think"


class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


class ImageType(str, Enum):
    JPEG = "image/jpeg"
    PNG = "image/png"
    GIF = "image/gif"
    WEBP = "image/webp"


# Image sources
class Base64ImageSource(BaseModel):
    type: Literal["base64"] = "base64"
    media_type: ImageType = Field(..., description="MIME type of the image")
    data: str = Field(..., description="Base64 encoded image data")


class URLImageSource(BaseModel):
    type: Literal["url"] = "url"
    url: str = Field(..., description="URL of the image")


class FileImageSource(BaseModel):
    type: Literal["file"] = "file"
    file_uuid: str = Field(..., description="UUID of the uploaded file")


# Web search result
class WebSearchResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["web_search_result"]
    title: str
    url: str
    encrypted_content: str
    page_age: Optional[str] = None


# Cache control
class CacheControl(BaseModel):
    type: Literal["ephemeral"]


# Content types
class TextContent(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["text"]
    text: str
    cache_control: Optional[CacheControl] = None


class ImageContent(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["image"]
    source: Base64ImageSource | URLImageSource | FileImageSource
    cache_control: Optional[CacheControl] = None


class ThinkingContent(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["thinking"]
    thinking: str


# redacted_thinking 块：API 可能返回被审查的思考内容
class RedactedThinkingContent(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["redacted_thinking"]
    data: str


class ToolUseContent(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["tool_use"]
    id: str
    name: str
    input: Dict[str, Any]
    cache_control: Optional[CacheControl] = None


class ToolResultContent(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["tool_result"]
    tool_use_id: str
    content: str | List[TextContent | ImageContent]
    is_error: Optional[bool] = False
    cache_control: Optional[CacheControl] = None


class ServerToolUseContent(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["server_tool_use"]
    id: str
    name: str
    input: Dict[str, Any]
    cache_control: Optional[CacheControl] = None


class WebSearchToolResultContent(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["web_search_tool_result"]
    tool_use_id: str
    content: List[WebSearchResult]
    cache_control: Optional[CacheControl] = None


ContentBlock = Union[
    TextContent,
    ImageContent,
    ThinkingContent,
    RedactedThinkingContent,
    ToolUseContent,
    ToolResultContent,
    ServerToolUseContent,
    WebSearchToolResultContent,
]


class InputMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: Role
    content: Union[str, List[ContentBlock]]


class ThinkingOptions(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["enabled", "disabled", "adaptive"] = "disabled"
    budget_tokens: Optional[int] = None


class ToolChoice(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["auto", "any", "tool", "none"] = "auto"
    name: Optional[str] = None
    disable_parallel_tool_use: Optional[bool] = None


class CustomToolSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    description: Optional[str] = None
    input_schema: Optional[Any] = None


class Tool(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Optional[str] = None
    name: Optional[str] = None
    input_schema: Optional[Any] = None
    description: Optional[str] = None
    custom: Optional[CustomToolSpec] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_input_schema(cls, data: Any) -> Any:
        """Accept alternate schema field shapes and normalize to `input_schema`."""
        if not isinstance(data, dict):
            return data

        if data.get("input_schema") is not None:
            return data

        normalized = data.copy()

        # MCP tools often expose camelCase `inputSchema`; Claude.ai web expects snake_case.
        if normalized.get("inputSchema") is not None:
            normalized["input_schema"] = normalized["inputSchema"]
            return normalized

        custom = normalized.get("custom")
        if isinstance(custom, dict):
            if custom.get("input_schema") is not None:
                normalized["input_schema"] = custom["input_schema"]
            elif custom.get("inputSchema") is not None:
                normalized["input_schema"] = custom["inputSchema"]

        return normalized


class OutputConfig(BaseModel):
    """Output configuration (effort, format, etc). effort and structured outputs are now GA."""

    model_config = ConfigDict(extra="allow")
    effort: Optional[Literal["low", "medium", "high", "max"]] = None


class OutputFormat(BaseModel):
    """Output format for structured outputs (deprecated, use output_config.format instead)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True, serialize_by_alias=True)
    type: Literal["json_schema"]
    schema_: Optional[Dict[str, Any]] = Field(default=None, alias="schema")


class ServerToolUsage(BaseModel):
    model_config = ConfigDict(extra="allow")
    web_search_requests: Optional[int] = None


class Usage(BaseModel):
    model_config = ConfigDict(extra="allow")
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: Optional[int] = 0
    cache_read_input_tokens: Optional[int] = 0
    server_tool_use: Optional[ServerToolUsage] = None


class MessagesAPIRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    _force_web_thinking: bool = PrivateAttr(default=False)
    model: str = Field(default="claude-opus-4-20250514")
    messages: List[InputMessage]
    max_tokens: int = Field(default=8192, ge=1)
    system: Optional[str | List[TextContent]] = None
    temperature: Optional[float] = Field(default=None, ge=0, le=1)
    top_p: Optional[float] = Field(default=None, ge=0, le=1)
    top_k: Optional[int] = Field(default=None, ge=0)
    stop_sequences: Optional[List[str]] = None
    stream: Optional[bool] = False
    metadata: Optional[Dict[str, Any]] = None
    thinking: Optional[ThinkingOptions] = None
    tool_choice: Optional[ToolChoice] = None
    tools: Optional[List[Tool]] = None
    output_config: Optional[OutputConfig] = None
    output_format: Optional[OutputFormat] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_think_model_suffix(cls, data: Any) -> Any:
        """Normalize `<model>-think` aliases into the base model plus web thinking."""
        if not isinstance(data, dict):
            return data

        model = data.get("model")
        if not isinstance(model, str) or not model.endswith(THINK_MODEL_SUFFIX):
            return data

        base_model = model[: -len(THINK_MODEL_SUFFIX)]
        if not base_model:
            return data

        normalized = data.copy()
        normalized["model"] = base_model

        thinking = normalized.get("thinking")
        if isinstance(thinking, dict):
            normalized_thinking = thinking.copy()
            if normalized_thinking.get("type") in (None, "disabled"):
                normalized_thinking["type"] = "enabled"
            normalized["thinking"] = normalized_thinking
        elif thinking is None:
            normalized["thinking"] = {"type": "enabled"}

        normalized["_clove_force_web_thinking"] = True
        return normalized

    @property
    def force_web_thinking(self) -> bool:
        """Whether the request explicitly asked for Claude Web extended thinking."""
        return self._force_web_thinking

    @model_validator(mode="after")
    def validate_thinking_tokens(self) -> "MessagesAPIRequest":
        """Apply internal flags and ensure max_tokens > thinking.budget_tokens."""
        if self.model_extra:
            self._force_web_thinking = bool(
                self.model_extra.pop("_clove_force_web_thinking", False)
            )

        if (
            self.thinking
            and self.thinking.type == "enabled"
            and self.thinking.budget_tokens is not None
            and self.max_tokens <= self.thinking.budget_tokens
        ):
            self.max_tokens = self.thinking.budget_tokens + 1
        return self


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    type: Literal["message"]
    role: Literal["assistant"]
    content: List[ContentBlock]
    model: str
    stop_reason: Optional[
        Literal[
            "end_turn",
            "max_tokens",
            "stop_sequence",
            "tool_use",
            "pause_turn",
            "refusal",
        ]
    ] = None
    stop_sequence: Optional[str] = None
    usage: Optional[Usage] = None
