"""
Static (no API call) tests for listing_copy.py constants and templates.
Run: python -m pytest tests/test_listing_copy_static.py -v
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_html_template_has_no_redirect_block():
    """_HTML_TEMPLATE_INSTRUCTIONS must not contain the off-platform redirect div."""
    from tools.listing_copy import _HTML_TEMPLATE_INSTRUCTIONS
    assert "Save 10% buying direct" not in _HTML_TEMPLATE_INSTRUCTIONS
    assert "coming-soon.com" not in _HTML_TEMPLATE_INSTRUCTIONS


def test_system_prompt_has_no_redirect_rule():
    """SYSTEM_PROMPT must not instruct Claude to include redirect/discount messages."""
    from tools.listing_copy import SYSTEM_PROMPT
    assert "redirect" not in SYSTEM_PROMPT.lower()
    assert "10% off" not in SYSTEM_PROMPT
    assert "discount code" not in SYSTEM_PROMPT.lower()


def test_product_lines_exclude_site_url_and_discount_code(monkeypatch):
    """generate_listing_copy must not send site_url or discount_code to Claude."""
    import anthropic
    captured = []

    class FakeContent:
        text = '[{"seo_title":"Test","bullets":"a|b|c|d|e","description":"<p>ok</p>","meta_desc":"155 char meta desc placeholder that is exactly right length for testing purposes ok","keywords":"k","alt_text":"a","redirect_msg":"","google_hl":"h|h|h","google_desc":"d|d","meta_text":"t","meta_hl":"h","demand_note":"note"}]'

    class FakeMessage:
        content = [FakeContent()]

    class FakeMessages:
        def create(self, **kwargs):
            captured.append(kwargs)
            return FakeMessage()

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: FakeClient())

    from tools.listing_copy import generate_listing_copy
    generate_listing_copy([{
        "title": "Test Product",
        "category": "Pharmacy",
        "cost": "10.00",
        "sell_price": "19.99",
    }])

    assert len(captured) == 1
    user_msg = captured[0]["messages"][0]["content"]
    assert "site_url" not in user_msg.lower()
    assert "discount_code" not in user_msg.lower()
    assert "SITE URL" not in user_msg
    assert "DISCOUNT CODE" not in user_msg
