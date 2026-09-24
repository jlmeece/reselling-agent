"""Image extraction order for Costco product pages (post Sep-2026 redesign)."""
from tools.costco_scraper import _extract_image_urls

GDX = "https://gdx-assets.costco.com/adobe/assets/urn:aaid:aem:{}/as/x_{}.avif?width=727"


class FakeEl:
    def __init__(self, src=None, data_src=None, content=None):
        self._a = {"src": src, "data-src": data_src, "content": content}

    def get_attribute(self, name):
        return self._a.get(name)


class FakePage:
    """Maps a substring of the selector to the elements it 'matches'."""

    def __init__(self, by_selector=None, og=None):
        self.by_selector = by_selector or {}
        self.og = og

    def query_selector_all(self, sel):
        for key, els in self.by_selector.items():
            if key in sel:
                return els
        return []

    def query_selector(self, sel):
        return FakeEl(content=self.og) if self.og else None


def test_alt_text_gallery_is_preferred_and_capped_at_five():
    els = [FakeEl(src=GDX.format(i, i)) for i in range(9)]
    page = FakePage({"Enlarge Product Preview": els, "gdx-assets": [FakeEl(src="https://x/other.jpg")]})
    urls = _extract_image_urls(page)
    assert urls == [GDX.format(i, i) for i in range(5)]


def test_dedupes_and_skips_logo_icon_svg():
    els = [FakeEl(src=GDX.format(1, 1)), FakeEl(src=GDX.format(1, 1)),
           FakeEl(src="https://x/logo.png"), FakeEl(src="https://x/a.svg"),
           FakeEl(src="https://x/icon-cart.png"), FakeEl(src=GDX.format(2, 2))]
    urls = _extract_image_urls(FakePage({"Enlarge Product Preview": els}))
    assert urls == [GDX.format(1, 1), GDX.format(2, 2)]


def test_data_src_used_when_src_missing():
    urls = _extract_image_urls(FakePage({"Enlarge Product Preview": [FakeEl(data_src=GDX.format(3, 3))]}))
    assert urls == [GDX.format(3, 3)]


def test_cdn_hostname_fallback_when_no_alt_match():
    urls = _extract_image_urls(FakePage({"gdx-assets": [FakeEl(src=GDX.format(4, 4))]}))
    assert urls == [GDX.format(4, 4)]


def test_legacy_container_fallback():
    urls = _extract_image_urls(FakePage({"product-image": [FakeEl(src="https://old/p.jpg")]}))
    assert urls == ["https://old/p.jpg"]


def test_og_image_final_fallback():
    assert _extract_image_urls(FakePage(og="https://og/img.jpg")) == ["https://og/img.jpg"]


def test_nothing_found_returns_empty_list():
    assert _extract_image_urls(FakePage()) == []
