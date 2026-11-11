import json

import pytest

from worker.workflows.tech_stock_digest import (
    Headline,
    build_attachment_filename,
    build_email_html,
    build_email_text,
    build_subject,
    count_words,
    extract_json_object,
    parse_stream_cards,
)

CARD = """
<div data-testid="stream-card">
  <a href="https://finance.yahoo.com/technology/article/nvidia-beats-earnings-123.html">
    <h3>Nvidia beats earnings as AI demand surges</h3>
  </a>
  <a href="/quote/NVDA/">NVDA</a>
</div>
<div data-testid="stream-card">
  <a href="https://finance.yahoo.com/news/personal-finance-tips-for-2026.html">
    <h3>10 personal finance tips for 2026</h3>
  </a>
</div>
<div data-testid="stream-card">
  <a href="/markets/stocks/articles/rocket-lab-reports-214909928.html?param=1#frag">
    <h3>Rocket Lab reports bullish quarter</h3>
  </a>
  <a href="/quote/RKLB/">RKLB</a>
  <a href="/quote/RKLB/">RKLB</a>
</div>
<div data-testid="stream-card">
  <h3>No link card</h3>
</div>
"""


def test_parse_stream_cards_extracts_titles_urls_tickers():
    headlines = parse_stream_cards(CARD)
    assert len(headlines) == 3
    assert headlines[0].title == "Nvidia beats earnings as AI demand surges"
    assert headlines[0].url == "https://finance.yahoo.com/technology/article/nvidia-beats-earnings-123.html"
    assert headlines[0].tickers == ["NVDA"]


def test_parse_stream_cards_normalizes_relative_url_and_dedupes_tickers():
    headlines = parse_stream_cards(CARD)
    assert headlines[2].url == "https://finance.yahoo.com/markets/stocks/articles/rocket-lab-reports-214909928.html"
    assert headlines[2].tickers == ["RKLB"]


def test_parse_stream_cards_dedupes_urls_across_blocks():
    duplicated = CARD + CARD
    assert len(parse_stream_cards(duplicated)) == 3


def test_parse_stream_cards_skips_cards_without_title_or_url():
    assert parse_stream_cards("<p>nothing here</p>") == []


def test_extract_json_object_plain():
    payload = {"a": 1}
    assert extract_json_object(json.dumps(payload)) == payload


def test_extract_json_object_fenced_and_surrounded():
    text = "Sure!\n```json\n{\"a\": {\"b\": 2}}\n```\nHope that helps."
    assert extract_json_object(text) == {"a": {"b": 2}}


def test_extract_json_object_invalid_raises():
    with pytest.raises(json.JSONDecodeError):
        extract_json_object("not json at all")


def test_build_email_html_renders_links_lists_and_bold():
    markdown = (
        "# Tech stock digest\n\n"
        "Market rallied.\n\n"
        "## Headlines\n\n"
        "- **[Nvidia beats earnings](https://finance.yahoo.com/technology/article/nvidia-123.html)** - AI demand surges $NVDA.\n"
    )
    html = build_email_html(markdown)
    assert '<a href="https://finance.yahoo.com/technology/article/nvidia-123.html">Nvidia beats earnings</a>' in html
    assert "<li>" in html and "<ul>" in html
    assert "<strong>" in html
    assert "<h1>Tech stock digest</h1>" in html
    assert "**" not in html


def test_build_email_text_keeps_urls_visible():
    markdown = "- [Nvidia beats earnings](https://example.com/x) - AI demand surges."
    text = build_email_text(markdown)
    assert "https://example.com/x" in text
    assert "[" not in text and "]" not in text


def test_build_subject_and_attachment_filename_format():
    assert build_subject().startswith("Tech stock digest - ")
    assert build_attachment_filename().startswith("tech-stock-digest-")
    assert build_attachment_filename().endswith(".mp3")


def test_count_words():
    assert count_words("one two  three\nfour") == 4


def test_headline_model_defaults():
    headline = Headline(title="t", url="https://finance.yahoo.com/x")
    assert headline.tickers == []
