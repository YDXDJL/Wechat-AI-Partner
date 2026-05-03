"""Built-in tools: Read, Write, Edit, Bash — Claude Code compatible."""

import logging
import datetime
import html
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Working directory for tool operations
_WORK_DIR = os.getcwd()
_HTTP_TIMEOUT = 20
_USER_AGENT = "Mozilla/5.0 (compatible; MicroAgent/1.0)"


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip = False
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip = True
        elif tag in ("p", "br", "div", "section", "article", "h1", "h2", "h3", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = False
        elif tag in ("p", "div", "section", "article", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        text = data.strip()
        if text:
            self.parts.append(text)

    def text(self) -> str:
        raw = " ".join(self.parts)
        lines = []
        for line in raw.splitlines():
            cleaned = " ".join(line.split())
            if cleaned:
                lines.append(cleaned)
        return "\n".join(lines)


class _DuckDuckGoParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._capture_title = False
        self._capture_snippet = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        class_name = attrs_dict.get("class", "")
        if tag == "a" and "result__a" in class_name:
            self._current = {"title": "", "url": self._clean_url(attrs_dict.get("href", "")), "snippet": ""}
            self._capture_title = True
        elif tag in ("a", "div") and "result__snippet" in class_name and self._current is not None:
            self._capture_snippet = True

    def handle_endtag(self, tag):
        if tag == "a" and self._capture_title:
            self._capture_title = False
            if self._current and self._current.get("title") and self._current.get("url"):
                self.results.append(self._current)
            self._current = None
        elif self._capture_snippet and tag in ("a", "div"):
            self._capture_snippet = False

    def handle_data(self, data):
        if self._current is None:
            return
        text = html.unescape(data).strip()
        if not text:
            return
        if self._capture_title:
            self._current["title"] += (" " if self._current["title"] else "") + text
        elif self._capture_snippet:
            self._current["snippet"] += (" " if self._current["snippet"] else "") + text

    def _clean_url(self, url: str) -> str:
        if not url:
            return ""
        if url.startswith("//"):
            url = "https:" + url
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if "uddg" in query:
            return urllib.parse.unquote(query["uddg"][0])
        return url


def set_work_dir(path: str):
    global _WORK_DIR
    _WORK_DIR = os.path.abspath(path)


def _resolve_path(file_path: str) -> str:
    """Resolve a file path relative to work directory."""
    if os.path.isabs(file_path):
        return file_path
    return os.path.join(_WORK_DIR, file_path)


BUILTIN_TOOLS = [
    {
        "name": "Read",
        "description": "Read a file from the local filesystem. Supports text files, images (PNG/JPG), PDFs, and Jupyter notebooks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the file to read",
                },
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (0-based). Only for text files.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of lines to read. Only for text files.",
                },
                "pages": {
                    "type": "string",
                    "description": "Page range for PDF files (e.g. '1-5', '3'). Max 20 pages per request.",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "Write",
        "description": "Write content to a file. Creates new files or completely overwrites existing files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the file to write",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
            },
            "required": ["file_path", "content"],
        },
    },
    {
        "name": "Edit",
        "description": "Perform exact string replacement in a file. The old_string must be unique in the file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the file to edit",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact text to replace",
                },
                "new_string": {
                    "type": "string",
                    "description": "The text to replace it with",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default: false)",
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    {
        "name": "Bash",
        "description": "Execute a shell command and return its output.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in milliseconds (max 600000, default 120000)",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "CurrentTime",
        "description": "Get the current date and time for a timezone. Use this when the user asks what time/date it is now.",
        "input_schema": {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone name, default Asia/Shanghai.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "WebSearch",
        "description": "Search the web for current or real-world information. Returns titles, URLs, and snippets.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of results to return, 1-8. Default 5.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "WebFetch",
        "description": "Fetch a web page by URL and return readable text. Use after WebSearch when details are needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "HTTP or HTTPS URL to fetch.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return. Default 8000, max 20000.",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "Weather",
        "description": "Get current weather for a city or location. Use this for questions about today's weather.",
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City or location name, for example Guangzhou or 北京.",
                },
            },
            "required": ["location"],
        },
    },
]


def execute_builtin_tool(name: str, arguments: dict) -> str:
    """Execute a built-in tool and return the result as text."""
    try:
        if name == "Read":
            return _read_file(arguments)
        elif name == "Write":
            return _write_file(arguments)
        elif name == "Edit":
            return _edit_file(arguments)
        elif name == "Bash":
            return _run_bash(arguments)
        elif name == "CurrentTime":
            return _current_time(arguments)
        elif name == "WebSearch":
            return _web_search(arguments)
        elif name == "WebFetch":
            return _web_fetch(arguments)
        elif name == "Weather":
            return _weather(arguments)
        else:
            return f"Error: Unknown built-in tool '{name}'"
    except Exception as e:
        return f"Error: {e}"


def _read_file(args: dict) -> str:
    file_path = _resolve_path(args["file_path"])

    # Check if it's an image
    ext = os.path.splitext(file_path)[1].lower()
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"):
        return f"[Image file: {file_path} — cannot display in terminal, {os.path.getsize(file_path)} bytes]"

    # Check if it's a PDF
    if ext == ".pdf":
        pages = args.get("pages", "")
        return _read_pdf(file_path, pages)

    # Read as text
    if not os.path.exists(file_path):
        return f"Error: File not found: {file_path}"

    offset = args.get("offset", 0)
    limit = args.get("limit", 2000)

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    total = len(lines)
    start = min(offset, total)
    end = min(start + limit, total)
    selected = lines[start:end]

    # Format with line numbers (cat -n style)
    numbered = []
    for i, line in enumerate(selected, start=start + 1):
        numbered.append(f"{i}\t{line.rstrip()}")

    result = "\n".join(numbered)
    if end < total:
        result += f"\n\n... ({total - end} more lines)"
    return result


def _read_pdf(file_path: str, pages: str = "") -> str:
    """Read PDF file content. Tries PyPDF2, falls back to basic info."""
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(file_path)
        total_pages = len(reader.pages)

        if pages:
            # Parse page range like "1-5" or "3"
            page_indices = _parse_page_range(pages, total_pages)
        else:
            page_indices = list(range(min(20, total_pages)))

        texts = []
        for i in page_indices:
            text = reader.pages[i].extract_text()
            texts.append(f"--- Page {i + 1} ---\n{text}")

        return "\n\n".join(texts)
    except ImportError:
        return f"[PDF file: {file_path} — install PyPDF2 to read PDFs]"
    except Exception as e:
        return f"Error reading PDF: {e}"


def _parse_page_range(pages_str: str, total: int) -> list:
    """Parse '1-5' or '3' into 0-based page indices."""
    indices = []
    for part in pages_str.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            s = max(0, int(start) - 1)
            e = min(total, int(end))
            indices.extend(range(s, e))
        else:
            idx = int(part) - 1
            if 0 <= idx < total:
                indices.append(idx)
    return indices[:20]  # max 20 pages


def _write_file(args: dict) -> str:
    file_path = _resolve_path(args["file_path"])
    content = args["content"]

    # Ensure parent directory exists
    parent = os.path.dirname(file_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

    return f"File written: {file_path} ({len(content)} chars)"


def _edit_file(args: dict) -> str:
    file_path = _resolve_path(args["file_path"])
    old_string = args["old_string"]
    new_string = args["new_string"]
    replace_all = args.get("replace_all", False)

    if not os.path.exists(file_path):
        return f"Error: File not found: {file_path}"

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    if old_string not in content:
        return f"Error: old_string not found in {file_path}"

    if replace_all:
        count = content.count(old_string)
        content = content.replace(old_string, new_string)
    else:
        # Check uniqueness
        count = content.count(old_string)
        if count > 1:
            return f"Error: old_string is not unique in {file_path} ({count} occurrences). Provide more context to make it unique."
        content = content.replace(old_string, new_string, 1)

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

    return f"File edited: {file_path}"


def _run_bash(args: dict) -> str:
    command = args["command"]
    timeout_ms = args.get("timeout", 120000)
    timeout_s = min(timeout_ms / 1000, 600)

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=_WORK_DIR,
            encoding="utf-8",
            errors="replace",
        )

        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            if output:
                output += "\n"
            output += result.stderr

        if result.returncode != 0:
            output += f"\n(exit code: {result.returncode})"

        if not output.strip():
            output = "(no output)"

        # Truncate very long output
        if len(output) > 50000:
            output = output[:25000] + "\n\n... (truncated) ...\n\n" + output[-25000:]

        return output
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout_s}s"
    except Exception as e:
        return f"Error running command: {e}"


def _http_get(url: str) -> tuple[str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        content_type = resp.headers.get("content-type", "")
        charset = resp.headers.get_content_charset() or "utf-8"
        body = resp.read(2_000_000)
    return body.decode(charset, errors="replace"), content_type


def _current_time(args: dict) -> str:
    timezone = args.get("timezone") or "Asia/Shanghai"
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
        timezone = "Asia/Shanghai"
    now = datetime.datetime.now(tz)
    return (
        f"timezone: {timezone}\n"
        f"date: {now:%Y-%m-%d}\n"
        f"time: {now:%H:%M:%S}\n"
        f"weekday: {now.strftime('%A')}\n"
        f"iso: {now.isoformat()}"
    )


def _web_search(args: dict) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "Error: query is required"
    max_results = max(1, min(int(args.get("max_results") or 5), 8))
    url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    try:
        body, _ = _http_get(url)
    except urllib.error.URLError as e:
        return f"Error searching web: {e}"

    parser = _DuckDuckGoParser()
    parser.feed(body)
    results = parser.results[:max_results]
    if not results:
        return "No search results found."

    lines = [f"Search results for: {query}"]
    for idx, item in enumerate(results, 1):
        lines.append(f"{idx}. {item['title']}\nURL: {item['url']}")
        if item.get("snippet"):
            lines.append(f"Snippet: {item['snippet']}")
    return "\n\n".join(lines)


def _web_fetch(args: dict) -> str:
    url = (args.get("url") or "").strip()
    if not url:
        return "Error: url is required"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "Error: only http and https URLs are supported"
    max_chars = max(1000, min(int(args.get("max_chars") or 8000), 20000))
    try:
        body, content_type = _http_get(url)
    except urllib.error.URLError as e:
        return f"Error fetching URL: {e}"

    if "html" in content_type.lower() or "<html" in body[:500].lower():
        extractor = _TextExtractor()
        extractor.feed(body)
        text = extractor.text()
    else:
        text = body
    text = html.unescape(text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n... (truncated)"
    return f"URL: {url}\nContent-Type: {content_type}\n\n{text}"


def _weather(args: dict) -> str:
    location = (args.get("location") or "").strip()
    if not location:
        return "Error: location is required"
    url = "https://wttr.in/" + urllib.parse.quote(location) + "?format=j1"
    try:
        body, _ = _http_get(url)
        data = json.loads(body)
    except Exception as e:
        return f"Error fetching weather: {e}"

    current = (data.get("current_condition") or [{}])[0]
    nearest = (data.get("nearest_area") or [{}])[0]
    area = ", ".join(
        part[0].get("value", "")
        for part in (nearest.get("areaName"), nearest.get("region"), nearest.get("country"))
        if part
    )
    desc = ", ".join(item.get("value", "") for item in current.get("weatherDesc", []))
    return (
        f"location: {area or location}\n"
        f"observed_at: {current.get('localObsDateTime', '')}\n"
        f"weather: {desc}\n"
        f"temperature_c: {current.get('temp_C', '')}\n"
        f"feels_like_c: {current.get('FeelsLikeC', '')}\n"
        f"humidity: {current.get('humidity', '')}%\n"
        f"precip_mm: {current.get('precipMM', '')}\n"
        f"wind: {current.get('windspeedKmph', '')} km/h {current.get('winddir16Point', '')}\n"
        f"uv_index: {current.get('uvIndex', '')}"
    )
