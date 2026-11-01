"""The HTML-to-text step uses MIT markdownify (html2text, GPL, was removed in 0.7.1)."""

import importlib.util

from mailforge.mail.sanitize import sanitize_html


def test_html2text_not_required():
    assert importlib.util.find_spec("markdownify") is not None


def test_links_lists_and_dropped_parts():
    out = sanitize_html(
        '<p>Hi <b>you</b> <a href="https://x.example/a">link</a> <a href="#top">in</a></p>'
        "<ul><li>a</li><li>b</li></ul><script>x()</script><img src=a>"
    )
    assert "**you**" in out
    assert "[link](https://x.example/a)" in out
    assert "#top" not in out and "x()" not in out
    assert "* a" in out and "* b" in out
