"""Web fetch tool — retrieve a URL and return readable text. Stdlib only."""
from __future__ import annotations

import gzip
import html
import http.client
import io
import json
import re
import urllib.error
import urllib.request
import zlib
from html.parser import HTMLParser

from ..core.logging import get_logger
from ..core.models import ToolResult
from .base import BaseTool

_MAX_BYTES = 2_000_000  # hard download cap, before text extraction
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "li", "tr", "br", "hr", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "ul", "ol",
}


class _TextExtractor(HTMLParser):
    """Collects visible text, one entry per block-level boundary."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip_depth:
            self.parts.append(data)


def _html_to_text(markup: str) -> tuple[str, str]:
    """(readable_text, title). Blank-line-separates blocks, collapses runs."""
    extractor = _TextExtractor()
    try:
        extractor.feed(markup)
    except Exception:
        pass  # keep whatever was extracted before the parse hiccup
    text = "".join(extractor.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), html.unescape(extractor.title.strip())


class WebFetchTool(BaseTool):
    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return (
            "Fetch a public http(s) URL and return its readable content. "
            "HTML is stripped to text; JSON and plain text are returned as-is. "
            "Use for documentation, references, or APIs the task needs."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The http(s) URL to fetch",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Truncate the extracted text to this many characters (default 20000)",
                    "default": 20000,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Request timeout in seconds (default 20)",
                    "default": 20,
                },
            },
            "required": ["url"],
        }

    def __init__(self):
        self.logger = get_logger(__name__)

    def _error(self, message: str) -> ToolResult:
        return ToolResult(tool_call_id="", name=self.name, content=message, is_error=True)

    def execute(self, arguments: dict) -> ToolResult:
        url = str(arguments.get("url", "")).strip()
        max_chars = int(arguments.get("max_chars", 20000) or 20000)
        timeout = int(arguments.get("timeout", 20) or 20)

        if not url:
            return self._error("Error: no url provided")
        if not url.lower().startswith(("http://", "https://")):
            return self._error(f"Error: only http(s) URLs are supported, got: {url}")

        req = urllib.request.Request(url, headers={
            "User-Agent": "tether/1.0 (+terminal coding agent)",
            "Accept": "text/html,application/json,text/plain,*/*",
            "Accept-Encoding": "gzip",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(_MAX_BYTES + 1)
                truncated_dl = len(raw) > _MAX_BYTES
                raw = raw[:_MAX_BYTES]
                if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                    try:
                        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                    except (OSError, EOFError, zlib.error):  # truncated at _MAX_BYTES / corrupt
                        pass
                content_type = (resp.headers.get("Content-Type") or "").lower()
                final_url = resp.geturl()
        except urllib.error.HTTPError as e:
            return self._error(f"HTTP {e.code} {e.reason} fetching {url}")
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
            # ValueError/InvalidURL: malformed URL (spaces, bad IPv6 literal)
            reason = getattr(e, "reason", e)
            return self._error(f"Could not fetch {url}: {reason}")

        charset = "utf-8"
        m = re.search(r"charset=([\w\-]+)", content_type)
        if m:
            charset = m.group(1)
        try:
            body = raw.decode(charset, errors="replace")
        except LookupError:
            body = raw.decode("utf-8", errors="replace")

        title = ""
        if "html" in content_type or (
            not content_type and body.lstrip()[:1] == "<"
        ):
            text, title = _html_to_text(body)
        elif "json" in content_type:
            try:  # pretty-print compact JSON so the model can actually read it
                text = json.dumps(json.loads(body), indent=2)[: max_chars * 2]
            except (json.JSONDecodeError, ValueError):
                text = body
        else:
            text = body

        notes = []
        if truncated_dl:
            notes.append(f"download capped at {_MAX_BYTES} bytes")
        if len(text) > max_chars:
            notes.append(f"text truncated to {max_chars} of {len(text)} chars")
            text = text[:max_chars]

        header = f"URL: {final_url}"
        if title:
            header += f"\nTitle: {title}"
        if notes:
            header += f"\n({'; '.join(notes)})"
        return ToolResult(
            tool_call_id="",
            name=self.name,
            content=f"{header}\n\n{text}" if text else f"{header}\n\n(no readable text)",
        )
