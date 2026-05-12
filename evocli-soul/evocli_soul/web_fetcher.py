"""
web_fetcher.py — URL 内容抓取工具 (Aider /web 命令 + Continue.dev @url 提供者)

研究来源:
- Aider (scrape.py): playwright + BeautifulSoup + pypandoc
- Continue.dev (@url): @mozilla/readability + html2text
- EvoCLI 采用: httpx (已有依赖) + readability-lxml + html2text
  更轻量，不需要 playwright 浏览器，适合文档/GitHub/API 参考

功能:
- fetch_url(url): 获取 URL 内容，清理 HTML，转换为 Markdown
- 自动处理 token 预算（截断过长内容）
- 支持纯文本页面直接返回

需要: pip install "evocli-soul[code]" (包含 html2text, readability-lxml)
"""
from __future__ import annotations

import importlib.util
import logging

log = logging.getLogger("evocli.web_fetcher")

_READABILITY_AVAILABLE = importlib.util.find_spec("readability") is not None
_HTML2TEXT_AVAILABLE   = importlib.util.find_spec("html2text")   is not None

# Token budget: 默认最多 4k tokens (约 16k chars) of web content
DEFAULT_MAX_CHARS = 16_000


async def fetch_url(url: str, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    """
    Fetch a URL and return clean Markdown content for LLM context.

    Research (Aider /web pattern):
    1. Fetch HTML with httpx (lightweight, already a dependency)
    2. Extract main content with readability-lxml (strips navigation/ads/footers)
    3. Convert to Markdown with html2text (LLM-friendly format)
    4. Truncate to token budget

    Returns: {"ok": bool, "url": str, "title": str, "content": str, "chars": int}
    """
    import httpx

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; EvoCLI/1.0)"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        raw_html = resp.text

        # Pure text/markdown — return directly
        if "text/plain" in content_type or "markdown" in content_type:
            truncated = raw_html[:max_chars]
            return {"ok": True, "url": url, "title": url, "content": truncated,
                    "chars": len(truncated), "source": "plain_text"}

        # Extract main content using readability (Continue.dev pattern)
        title, markdown = _html_to_markdown(raw_html, url)

        if len(markdown) > max_chars:
            markdown = markdown[:max_chars] + f"\n\n...[content truncated at {max_chars} chars]"

        log.info("Fetched %s: %d chars", url, len(markdown))
        return {"ok": True, "url": url, "title": title, "content": markdown,
                "chars": len(markdown)}

    except Exception as e:
        log.warning("web_fetcher: failed to fetch %s: %s", url, e)
        return {"ok": False, "url": url, "title": "", "content": f"Error: {e}", "chars": 0}


def _html_to_markdown(html: str, url: str) -> tuple[str, str]:
    """
    Convert HTML to clean Markdown.
    Strategy (Continue.dev @url provider pattern):
    1. readability-lxml: extract main article content (strips nav/ads/footers)
    2. html2text: convert cleaned HTML to Markdown
    """
    title = url

    # Stage 1: readability — extract main content
    if _READABILITY_AVAILABLE:
        try:
            from readability import Document
            doc     = Document(html)
            title   = doc.title() or url
            clean_html = doc.summary()
        except Exception as e:
            log.debug("readability failed: %s", e)
            clean_html = _simple_strip(html)
    else:
        clean_html = _simple_strip(html)

    # Stage 2: html2text — convert to Markdown
    if _HTML2TEXT_AVAILABLE:
        try:
            import html2text
            h = html2text.HTML2Text()
            h.ignore_links      = False
            h.ignore_images     = True    # Save tokens
            h.ignore_tables     = False
            h.body_width        = 0       # Don't wrap lines
            h.unicode_snob      = True
            markdown = h.handle(clean_html)
        except Exception as e:
            log.debug("html2text failed: %s", e)
            markdown = _strip_tags(clean_html)
    else:
        markdown = _strip_tags(clean_html)

    return title, markdown.strip()


def _simple_strip(html: str) -> str:
    """Simple removal of script/style tags (fallback when readability unavailable)."""
    import re
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    return html


def _strip_tags(html: str) -> str:
    """Strip all HTML tags (last resort fallback)."""
    import re
    return re.sub(r"<[^>]+>", "", html)
