"""Tech stock digest workflow.

Reads Yahoo Finance (front page attempt + tech topic stream), filters and
summarizes tech-industry stock news with a Mistral model, synthesizes a short
podcast-style audio with Voxtral TTS, and emails the digest with the audio
attached through the Gmail connector.
"""

from __future__ import annotations

import base64
import html as html_module
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pydantic

import mistralai.workflows as workflows
from mistralai.workflows import Depends
from mistralai.workflows.client import get_mistral_client
from mistralai.workflows.plugins.mistralai.connectors import (
    ToolCallClient,
    connector,
    uses_connectors,
)

gmail_connector = connector("gmail")

YAHOO_FINANCE_PAGES = (
    "https://finance.yahoo.com/",
    "https://finance.yahoo.com/topic/tech/",
)

DIGEST_MODEL = "mistral-medium-latest"
TTS_MODEL = "voxtral-mini-tts-3-0"
DEFAULT_VOICE = "gb_jane_neutral"

PODCAST_MIN_WORDS = 170
PODCAST_MAX_WORDS = 310

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

HREF_PATTERN = re.compile(r'href="([^"]+)"')
TITLE_PATTERN = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S)
TICKER_PATTERN = re.compile(r'href="/quote/([A-Za-z0-9.\-]+)/"')
STREAM_CARD_SPLIT = '<div data-testid="stream-card"'


class Headline(pydantic.BaseModel):
    title: str
    url: str
    tickers: list[str] = pydantic.Field(default_factory=list)


class DigestItem(pydantic.BaseModel):
    title: str
    url: str
    tickers: list[str] = pydantic.Field(default_factory=list)
    summary: str


class DigestContent(pydantic.BaseModel):
    items: list[DigestItem]
    digest_markdown: str
    podcast_script: str


class DigestInput(pydantic.BaseModel):
    recipient_email: str | None = None
    max_headlines: int = 10
    podcast_voice: str = DEFAULT_VOICE
    dry_run: bool = False


class DigestOutput(pydantic.BaseModel):
    subject: str
    digest_markdown: str
    podcast_script: str
    audio_bytes: int
    email_sent: bool
    headlines_considered: int
    tech_headlines_kept: int
    message: str | None = None


def parse_stream_cards(page_html: str, base_url: str = "https://finance.yahoo.com") -> list[Headline]:
    """Extract (title, url, tickers) from Yahoo Finance stream cards."""
    headlines: list[Headline] = []
    seen_urls: set[str] = set()
    for block in page_html.split(STREAM_CARD_SPLIT)[1:]:
        title_match = TITLE_PATTERN.search(block)
        if not title_match:
            continue
        title = html_module.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))).strip()
        if not title:
            continue
        url = None
        for href in HREF_PATTERN.findall(block):
            if "/article" not in href and "/news/" not in href:
                continue
            url = _normalize_url(href, base_url)
            break
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        tickers = []
        for ticker in TICKER_PATTERN.findall(block):
            if ticker.upper() not in tickers:
                tickers.append(ticker.upper())
        headlines.append(Headline(title=title, url=url, tickers=tickers[:5]))
    return headlines


def _normalize_url(href: str, base_url: str) -> str | None:
    href = html_module.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = base_url + href
    if not href.startswith("https://"):
        return None
    return href.split("?")[0].split("#")[0]


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def count_words(text: str) -> int:
    return len(re.findall(r"\S+", text))


def build_email_html(digest_markdown: str) -> str:
    html_body = digest_markdown
    html_body = re.sub(r"^### (.*)$", r"<h3>\1</h3>", html_body, flags=re.M)
    html_body = re.sub(r"^## (.*)$", r"<h2>\1</h2>", html_body, flags=re.M)
    html_body = re.sub(r"^# (.*)$", r"<h1>\1</h1>", html_body, flags=re.M)
    html_body = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", html_body)
    html_body = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        r'<a href="\2">\1</a>',
        html_body,
    )
    html_body = re.sub(r"^- (.*)$", r"<li>\1</li>", html_body, flags=re.M)
    html_body = re.sub(r"(<li>.*?</li>\n?)+", lambda m: "<ul>" + m.group(0) + "</ul>", html_body)
    html_body = html_body.replace("\n\n", "<br><br>").replace("\n", "<br>")
    return (
        '<div style="font-family: Arial, sans-serif; max-width: 640px; margin: 0 auto;">'
        + html_body
        + "</div>"
    )


def build_email_text(digest_markdown: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", digest_markdown)
    text = re.sub(r"^#+ ", "", text, flags=re.M)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    return text


def build_subject() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"Tech stock digest - {today}"


def build_attachment_filename() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"tech-stock-digest-{today}.mp3"


@workflows.activity(start_to_close_timeout=timedelta(seconds=90))
async def fetch_yahoo_finance_headlines(max_items: int = 30) -> list[Headline]:
    headlines: list[Headline] = []
    seen_urls: set[str] = set()
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=30,
        follow_redirects=True,
    ) as client:
        for page_url in YAHOO_FINANCE_PAGES:
            try:
                response = await client.get(page_url)
                response.raise_for_status()
            except httpx.HTTPError:
                continue
            for headline in parse_stream_cards(response.text, base_url=page_url):
                if headline.url in seen_urls:
                    continue
                seen_urls.add(headline.url)
                headlines.append(headline)
    return headlines[:max_items]


@workflows.activity(start_to_close_timeout=timedelta(seconds=180))
async def summarize_tech_news(headlines: list[Headline], max_headlines: int = 10) -> DigestContent:
    client = get_mistral_client(use_executor_credentials=False)
    lines = [
        f"{index}. {headline.title} | {headline.url} | tickers: {', '.join(headline.tickers) or 'none'}"
        for index, headline in enumerate(headlines, start=1)
    ]
    prompt = f"""You are a financial news editor for a tech-investor newsletter.

Below are headlines scraped from the Yahoo Finance front page and its tech topic page.

Task:
1. Keep ONLY news about technology-industry public companies and their stocks: semiconductors, software, cloud, AI, hardware, consumer electronics, telecom equipment, and similar tech businesses. Drop personal-finance blog posts, politics, sports, entertainment, and any non-tech-industry topics.
2. Keep at most {max_headlines} items, most market-relevant first. Drop everything else.
3. For each kept item write a 1-2 sentence factual summary grounded in the headline.
4. Write `digest_markdown`: a short intro line about the tech market mood, then a "## Headlines" section with one markdown bullet per item: `[Title](url) - summary`, and tickers mentioned as $TICKER.
5. Write `podcast_script`: a podcast-style spoken intro presenting the headlines, {PODCAST_MIN_WORDS}-{PODCAST_MAX_WORDS} words (about 1-2 minutes at natural speaking pace). Conversational tone, no markdown, no emoji, mention company names and tickers naturally, and end with a one-line sign-off.

Headlines:
{chr(10).join(lines)}

Respond with a single JSON object: {{"items": [{{"title", "url", "tickers", "summary"}}], "digest_markdown", "podcast_script"}}."""
    response = await client.chat.complete_async(
        model=DIGEST_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content or ""
    payload = extract_json_object(content)
    return DigestContent.model_validate(payload)


@workflows.activity(start_to_close_timeout=timedelta(seconds=180))
async def generate_podcast_audio(script: str, voice: str = DEFAULT_VOICE) -> str:
    client = get_mistral_client(use_executor_credentials=False)
    response = await client.audio.speech.complete(
        model=TTS_MODEL,
        input=script,
        voice_id=voice,
        response_format="mp3",
    )
    audio = base64.b64decode(response.audio_data)
    if len(audio) < 1000:
        raise workflows.ActivityError(f"TTS returned suspiciously small audio ({len(audio)} bytes)")
    return base64.b64encode(audio).decode()


@workflows.activity(start_to_close_timeout=timedelta(seconds=120))
async def send_digest_email(
    subject: str,
    digest_markdown: str,
    audio_base64: str,
    recipient_email: str,
    gmail: ToolCallClient = Depends(gmail_connector),
) -> dict:
    result = await gmail.call_tool(
        tool_name="send_email",
        arguments={
            "to": [recipient_email],
            "subject": subject,
            "body": build_email_text(digest_markdown),
            "htmlBody": build_email_html(digest_markdown),
            "attachments": [
                {
                    "content": audio_base64,
                    "filename": build_attachment_filename(),
                    "mimeType": "audio/mpeg",
                }
            ],
        },
    )
    return {"sent": True, "result": result}


@workflows.workflow.define(
    name="tech-stock-digest",
    workflow_display_name="Tech Stock Digest",
    workflow_description=(
        "Reads Yahoo Finance, summarizes the latest tech-industry stock news, and emails "
        "a digest with a 1-2 minute podcast audio attached. One-shot: triggered manually."
    ),
    on_behalf_of=True,
)
@uses_connectors(gmail_connector)
class TechStockDigestWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: DigestInput) -> DigestOutput:
        subject = build_subject()
        headlines = await fetch_yahoo_finance_headlines(max_items=max(input.max_headlines * 3, 30))
        if not headlines:
            return DigestOutput(
                subject=subject,
                digest_markdown="",
                podcast_script="",
                audio_bytes=0,
                email_sent=False,
                headlines_considered=0,
                tech_headlines_kept=0,
                message="No headlines could be fetched from Yahoo Finance.",
            )
        digest = await summarize_tech_news(headlines, max_headlines=input.max_headlines)
        audio_base64 = await generate_podcast_audio(digest.podcast_script, voice=input.podcast_voice)
        audio_bytes = len(base64.b64decode(audio_base64))
        if input.dry_run:
            return DigestOutput(
                subject=subject,
                digest_markdown=digest.digest_markdown,
                podcast_script=digest.podcast_script,
                audio_bytes=audio_bytes,
                email_sent=False,
                headlines_considered=len(headlines),
                tech_headlines_kept=len(digest.items),
                message="Dry run: digest and audio generated, no email sent.",
            )
        if not input.recipient_email:
            raise workflows.WorkflowError(
                "recipient_email is required when dry_run is false."
            )
        await send_digest_email(
            subject=subject,
            digest_markdown=digest.digest_markdown,
            audio_base64=audio_base64,
            recipient_email=input.recipient_email,
        )
        return DigestOutput(
            subject=subject,
            digest_markdown=digest.digest_markdown,
            podcast_script=digest.podcast_script,
            audio_bytes=audio_bytes,
            email_sent=True,
            headlines_considered=len(headlines),
            tech_headlines_kept=len(digest.items),
            message=f"Digest sent to {input.recipient_email}.",
        )
