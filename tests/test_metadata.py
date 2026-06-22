"""Tests for page metadata extraction (OpenGraph + JSON-LD + canonical + title)."""

from master_fetch.metadata import extract_metadata


def test_empty_html_returns_empty():
    assert extract_metadata("", "https://x.com") == {}
    assert extract_metadata(None, "https://x.com") == {}


def test_opengraph_tags_extracted():
    html = (
        '<html><head>'
        '<meta property="og:title" content="Hound Release Notes">'
        '<meta property="og:description" content="A web research MCP server.">'
        '<meta property="og:site_name" content="Hound">'
        '<meta property="og:type" content="article">'
        '<meta property="og:image" content="https://x.com/img.png">'
        '</head><body></body></html>'
    )
    m = extract_metadata(html, "https://x.com/p")
    assert m["title"] == "Hound Release Notes"
    assert m["description"] == "A web research MCP server."
    assert m["site_name"] == "Hound"
    assert m["type"] == "article"
    assert m["image"] == "https://x.com/img.png"


def test_title_fallback_from_title_tag():
    html = '<html><head><title>Plain Title</title></head><body></body></html>'
    m = extract_metadata(html, "https://x.com")
    assert m["title"] == "Plain Title"


def test_canonical_absolute():
    html = '<html><head><link rel="canonical" href="/p/123"></head><body></body></html>'
    m = extract_metadata(html, "https://x.com/blog")
    assert m["canonical"] == "https://x.com/p/123"


def test_html_lang():
    html = '<html lang="en-US"><head><title>x</title></head><body></body></html>'
    m = extract_metadata(html, "https://x.com")
    assert m["lang"] == "en-US"


def test_json_ld_published_author():
    html = (
        '<html><head>'
        '<script type="application/ld+json">'
        '{"@type":"Article","headline":"LD Title","datePublished":"2026-06-01T00:00:00Z",'
        '"author":{"@type":"Person","name":"Bishesh"},"description":"LD desc"}'
        '</script>'
        '</head><body></body></html>'
    )
    m = extract_metadata(html, "https://x.com")
    assert m["published_time"] == "2026-06-01T00:00:00Z"
    assert m["author"] == "Bishesh"
    assert m["title"] == "LD Title"
    assert m["description"] == "LD desc"


def test_json_ld_author_list():
    html = (
        '<script type="application/ld+json">'
        '{"@type":"Article","author":[{"@type":"Person","name":"A"},{"@type":"Person","name":"B"}]}'
        '</script>'
    )
    m = extract_metadata(html, "https://x.com")
    assert m["author"] == "A"  # first author


def test_meta_reverse_attribute_order():
    # content before property (some sites do this)
    html = '<meta content="Rev Title" property="og:title">'
    m = extract_metadata(html, "https://x.com")
    assert m["title"] == "Rev Title"


def test_opengraph_beats_title_tag():
    html = (
        '<html><head>'
        '<meta property="og:title" content="OG Title">'
        '<title>Tag Title</title>'
        '</head><body></body></html>'
    )
    m = extract_metadata(html, "https://x.com")
    assert m["title"] == "OG Title"


def test_long_values_truncated():
    html = f'<meta property="og:description" content="{"x" * 2000}">'
    m = extract_metadata(html, "https://x.com")
    assert len(m["description"]) == 500


# ─── extract_image_urls ─────────────────────────────────────────────────

from master_fetch.metadata import extract_image_urls


def test_image_urls_absolute_and_deduped():
    html = (
        '<img src="/a.png"><img src="https://x.com/a.png">'
        '<img src="https://cdn.y.com/b.jpg"><img src="data:image/png;base64,xxx">'
    )
    urls = extract_image_urls(html, "https://x.com/page")
    assert urls == ["https://x.com/a.png", "https://cdn.y.com/b.jpg"]


def test_image_urls_capped():
    html = "".join(f'<img src="/img{i}.png">' for i in range(30))
    urls = extract_image_urls(html, "https://x.com/p", max_n=5)
    assert len(urls) == 5
    assert urls[0] == "https://x.com/img0.png"


def test_image_urls_empty_html():
    assert extract_image_urls("", "https://x.com") == []
    assert extract_image_urls(None, "https://x.com") == []
