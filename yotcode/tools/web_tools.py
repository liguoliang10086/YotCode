"""WebFetch / WebSearch 工具。

WebFetch 直接抓取 URL，HTML 会被转成可读文本；WebSearch 根据 settings 里的
webSearch.provider 选择 Brave 或 Tavily（需要对应 API Key）。
"""
from __future__ import annotations

import html as html_lib
import json
import os
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from ..utils import truncate_middle
from .base import Tool, ToolContext, ToolResult

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36 YotCode/0.1"
)
MAX_BYTES = 3 * 1024 * 1024


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "canvas", "template", "iframe"}
    BLOCK = {
        "p", "div", "section", "article", "header", "footer", "main", "aside",
        "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "br", "hr", "pre",
        "table", "blockquote", "ul", "ol", "dl", "dt", "dd", "form", "nav",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False
        self._current_link: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag in self.SKIP:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag == "a" and attrs_dict.get("href"):
            self._current_link = attrs_dict["href"]
            self._link_text = []
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._current_link:
            text = "".join(self._link_text).strip()
            if text and len(self.links) < 60:
                self.links.append((text[:120], self._current_link))
            self._current_link = None
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data.strip() + " "
            return
        if self._current_link is not None:
            self._link_text.append(data)
        self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t\u00a0]+", " ", raw)
        raw = re.sub(r"\n\s*\n\s*\n+", "\n\n", raw)
        lines = [line.strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_text(html: str, base_url: str = "") -> tuple[str, str, list[tuple[str, str]]]:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    text = parser.text()
    if base_url:
        links = [(label, urljoin(base_url, href)) for label, href in parser.links]
    else:
        links = parser.links
    return parser.title.strip(), text, links


def _relevant(text: str, prompt: str, max_chars: int = 16_000) -> str:
    if not prompt or len(text) <= max_chars:
        return text[:max_chars]
    words = {
        w.lower() for w in re.findall(r"[A-Za-z0-9_\u4e00-\u9fff]{3,}", prompt)
    }
    if not words:
        return text[:max_chars]
    chunks = re.split(r"\n\s*\n", text)
    scored: list[tuple[int, int, str]] = []
    for index, chunk in enumerate(chunks):
        lowered = chunk.lower()
        score = sum(lowered.count(word) for word in words)
        if score:
            scored.append((score, index, chunk))
    scored.sort(key=lambda item: (-item[0], item[1]))
    picked: list[tuple[int, str]] = []
    total = 0
    for score, index, chunk in scored:
        if total + len(chunk) > max_chars and picked:
            break
        picked.append((index, chunk))
        total += len(chunk)
    if not picked:
        return text[:max_chars]
    picked.sort(key=lambda item: item[0])
    return "\n\n".join(chunk for _, chunk in picked)[:max_chars]


class WebFetchTool(Tool):
    name = "WebFetch"
    read_only = True
    description = (
        "Fetches a URL and returns its content converted to readable text (HTML is "
        "stripped of markup). Use it to read documentation or pages the user links. "
        "The URL must be fully formed (https://...). Include a short prompt describing "
        "what information you need so the excerpt can focus on it."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Fully formed URL, including https://."},
            "prompt": {"type": "string", "description": "What to extract from the page."},
        },
        "required": ["url"],
    }

    def summary(self, data: dict[str, Any]) -> str:
        return str(data.get("url") or "")

    async def run(self, data: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = str(data.get("url") or "").strip()
        if not url:
            return self.error("url is required")
        if not re.match(r"^https?://", url, re.I):
            if url.startswith("//"):
                url = "https:" + url
            else:
                url = "https://" + url
        cache: dict[str, Any] = ctx.state.setdefault("web_cache", {})
        if url in cache:
            payload = cache[url]
        else:
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(30.0, connect=15.0),
                    follow_redirects=True,
                    headers={"user-agent": USER_AGENT, "accept-language": "en,zh-CN;q=0.8"},
                ) as client:
                    async with client.stream("GET", url) as response:
                        if response.status_code >= 400:
                            return self.error(
                                f"Request failed with HTTP {response.status_code} for {url}"
                            )
                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            chunks.append(chunk)
                            size += len(chunk)
                            if size > MAX_BYTES:
                                break
                        raw = b"".join(chunks)
                        content_type = response.headers.get("content-type", "")
                        payload = {
                            "content_type": content_type,
                            "final_url": str(response.url),
                            "raw": raw,
                        }
                        cache[url] = payload
            except httpx.HTTPError as exc:
                return self.error(f"Could not fetch {url}: {exc}")

        content_type = payload["content_type"]
        raw: bytes = payload["raw"]
        final_url = payload["final_url"]
        if any(k in content_type for k in ("text/", "json", "xml", "javascript")):
            body = raw.decode("utf-8", errors="replace")
        else:
            return self.error(
                f"Unsupported content type '{content_type or 'unknown'}' at {url}. "
                "WebFetch only handles text/HTML/JSON."
            )

        if "html" in content_type or body.lstrip().lower().startswith(("<!doctype", "<html")):
            title, text, links = html_to_text(body, final_url)
            excerpt = _relevant(text, str(data.get("prompt") or ""))
            parts = [f"# {title or final_url}", f"URL: {final_url}"]
            if final_url != url:
                parts.append(f"(redirected from {url})")
            parts.append("")
            parts.append(excerpt)
            if links:
                parts.append("\n## Links")
                parts.extend(f"- [{label}]({href})" for label, href in links[:30])
            out = "\n".join(parts)
        else:
            out = f"URL: {final_url}\n\n{body}"

        out, truncated = truncate_middle(out, 40_000)
        if truncated:
            out += "\n\n... (content truncated)"
        return self.ok(out, url=final_url, bytes=len(raw))


class WebSearchTool(Tool):
    name = "WebSearch"
    read_only = True
    description = (
        "Search the web and return titles, URLs and snippets. Use for current "
        "information beyond your knowledge cutoff. After searching, use WebFetch to "
        "read the most relevant result. Results come from DuckDuckGo by default; set "
        "BRAVE_API_KEY or TAVILY_API_KEY for better quality."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "allowed_domains": {
                "type": "array", "items": {"type": "string"},
                "description": "Only include results from these domains.",
            },
            "blocked_domains": {
                "type": "array", "items": {"type": "string"},
                "description": "Exclude results from these domains.",
            },
            "max_results": {"type": "integer", "description": "1-10, default 8."},
        },
        "required": ["query"],
    }

    def summary(self, data: dict[str, Any]) -> str:
        return str(data.get("query") or "")

    async def run(self, data: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(data.get("query") or "").strip()
        if not query:
            return self.error("query is required")
        limit = max(1, min(int(data.get("max_results") or 8), 10))
        allowed = [str(d).lower() for d in (data.get("allowed_domains") or [])]
        blocked = [str(d).lower() for d in (data.get("blocked_domains") or [])]

        provider = ctx.config.web_search_provider
        env = {**os.environ, **(ctx.config.env or {})}
        results: list[dict[str, str]] = []
        errors: list[str] = []
        tavily = env.get("TAVILY_API_KEY")
        brave = env.get("BRAVE_API_KEY")

        try:
            if provider == "tavily" or (not provider and tavily):
                results = await self._tavily(query, limit, tavily or "")
            elif provider == "brave" or (not provider and brave):
                results = await self._brave(query, limit, brave or "")
            else:
                results = await self._duckduckgo(query, limit)
        except Exception as exc:
            errors.append(str(exc))

        if not results and not errors:
            errors.append("no results returned")

        def domain_ok(url: str) -> bool:
            host = (urlparse(url).hostname or "").lower()
            if blocked and any(host == d or host.endswith("." + d) for d in blocked):
                return False
            if allowed and not any(host == d or host.endswith("." + d) for d in allowed):
                return False
            return True

        results = [r for r in results if domain_ok(r.get("url", ""))]
        if not results:
            return self.error(
                "Web search returned no results"
                + (f" ({'; '.join(errors)})" if errors else "")
                + ". Try WebFetch on a known URL instead."
            )
        lines = [f"Search results for: {query}", ""]
        for index, result in enumerate(results[:limit], 1):
            lines.append(f"{index}. {result.get('title', 'Untitled')}")
            lines.append(f"   {result.get('url', '')}")
            snippet = re.sub(r"\s+", " ", result.get("snippet") or "").strip()
            if snippet:
                lines.append(f"   {snippet[:400]}")
            lines.append("")
        return self.ok("\n".join(lines), count=len(results))

    async def _tavily(self, query: str, limit: int, key: str) -> list[dict[str, str]]:
        if not key:
            raise RuntimeError("TAVILY_API_KEY is not set")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.tavily.com/search",
                json={"api_key": key, "query": query, "max_results": limit,
                      "search_depth": "advanced", "include_answer": False},
            )
            response.raise_for_status()
            payload = response.json()
        return [
            {"title": item.get("title", ""), "url": item.get("url", ""),
             "snippet": item.get("content", "")}
            for item in payload.get("results") or []
        ]

    async def _brave(self, query: str, limit: int, key: str) -> list[dict[str, str]]:
        if not key:
            raise RuntimeError("BRAVE_API_KEY is not set")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": limit},
                headers={"X-Subscription-Token": key, "Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
        out = []
        for item in (payload.get("web") or {}).get("results") or []:
            out.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
            })
        return out

    async def _duckduckgo(self, query: str, limit: int) -> list[dict[str, str]]:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=12.0),
            follow_redirects=True,
            headers={"user-agent": USER_AGENT},
        ) as client:
            response = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query, "kl": "wt-wt"},
            )
            if response.status_code >= 400:
                raise RuntimeError(f"DuckDuckGo HTTP {response.status_code}")
            page = response.text

        pattern = re.compile(
            r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
            re.S,
        )
        snippet_re = re.compile(
            r'class="result__snippet"[^>]*>(?P<snippet>.*?)</a>', re.S
        )
        snippets = [
            _clean_html(m.group("snippet")) for m in snippet_re.finditer(page)
        ]
        results: list[dict[str, str]] = []
        for index, match in enumerate(pattern.finditer(page)):
            href = html_lib.unescape(match.group("href"))
            if href.startswith("//"):
                href = "https:" + href
            if "duckduckgo.com/l/" in href:
                query_params = parse_qs(urlparse(href).query)
                href = (query_params.get("uddg") or [href])[0]
            title = _clean_html(match.group("title"))
            if not title or not href.startswith("http"):
                continue
            results.append({
                "title": title,
                "url": href,
                "snippet": snippets[index] if index < len(snippets) else "",
            })
            if len(results) >= limit:
                break
        return results


def _clean_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw)
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()
