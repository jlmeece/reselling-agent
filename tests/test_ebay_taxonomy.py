"""
Mocked tests for tools/ebay_taxonomy.py and its wiring into ebay_export.py.
HTTP is faked at ebay_taxonomy._http_json — no live eBay calls (tests/conftest.py also
blocks the seam and redirects the cache file for every test).
Run: python -m pytest tests/test_ebay_taxonomy.py -v
"""
import base64
import csv
import io
import json
import os
import sys
import urllib.error
import urllib.parse

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import ebay_export as ee
from tools import ebay_taxonomy as et


# ── Fake eBay ────────────────────────────────────────────────────────────────

class FakeEbay:
    """Routes the two eBay endpoints; records every request."""

    def __init__(self, aspects_response=None, token_status=200, expires_in=7200):
        self.calls = []                     # (kind, request)
        self.token_status = token_status
        self.expires_in = expires_in
        self.tokens_issued = 0
        self.aspects_response = aspects_response          # (status, body) or callable(cat_id, n)
        self.aspects_calls = 0

    def __call__(self, req):
        url = req.full_url
        if url == et.TOKEN_URL:
            self.calls.append(("token", req))
            if self.token_status != 200:
                return self.token_status, {"error": "invalid_client"}
            self.tokens_issued += 1
            return 200, {"access_token": f"tok{self.tokens_issued}", "expires_in": self.expires_in}
        assert url.startswith(et.ASPECTS_URL)
        self.calls.append(("aspects", req))
        self.aspects_calls += 1
        cat_id = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["category_id"][0]
        resp = self.aspects_response
        return resp(cat_id, self.aspects_calls) if callable(resp) else resp

    def count(self, kind):
        return sum(1 for k, _ in self.calls if k == kind)


def aspect(name, required=True, mode="FREE_TEXT", values=(), cardinality="SINGLE",
           max_length=None, style="aspectRequired"):
    constraint = {"aspectMode": mode, "itemToAspectCardinality": cardinality}
    if max_length:
        constraint["aspectMaxLength"] = max_length
    if style == "aspectRequired":
        constraint["aspectRequired"] = required
    elif required:
        constraint["requirement"] = "REQUIRED"
    return {
        "localizedAspectName": name,
        "aspectConstraint": constraint,
        "aspectValues": [{"localizedValue": v} for v in values],
    }


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("EBAY_APP_ID", "my-app")
    monkeypatch.setenv("EBAY_CERT_ID", "my-cert")


@pytest.fixture
def clock(monkeypatch):
    t = {"now": 1_000_000.0}
    monkeypatch.setattr(et, "_now", lambda: t["now"])
    return t


@pytest.fixture
def ebay(monkeypatch, creds):
    fake = FakeEbay(aspects_response=(200, {"aspects": [aspect("Brand"), aspect("Model")]}))
    monkeypatch.setattr(et, "_http_json", fake)
    return fake


# ── OAuth ────────────────────────────────────────────────────────────────────

def test_token_request_uses_basic_auth_and_client_credentials(ebay):
    assert et._get_oauth_token() == "tok1"
    kind, req = ebay.calls[0]
    assert kind == "token" and req.get_method() == "POST"
    expected = base64.b64encode(b"my-app:my-cert").decode()
    assert req.get_header("Authorization") == f"Basic {expected}"
    body = urllib.parse.parse_qs(req.data.decode())
    assert body["grant_type"] == ["client_credentials"]
    assert body["scope"] == [et.OAUTH_SCOPE]


def test_token_is_cached_until_near_expiry(ebay, clock):
    assert et._get_oauth_token() == "tok1"
    clock["now"] += 7000            # under 7200 - 60s skew
    assert et._get_oauth_token() == "tok1"
    assert ebay.count("token") == 1


def test_token_refreshes_after_expiry(ebay, clock):
    assert et._get_oauth_token() == "tok1"
    clock["now"] += 7200            # past the skewed expiry
    assert et._get_oauth_token() == "tok2"
    assert ebay.count("token") == 2


def test_token_missing_creds_returns_none_without_http(monkeypatch, ebay):
    monkeypatch.delenv("EBAY_CERT_ID")
    assert et._get_oauth_token() is None
    assert ebay.calls == []


def test_token_http_error_returns_none_then_cools_down(monkeypatch, creds, clock):
    fake = FakeEbay(token_status=401)
    monkeypatch.setattr(et, "_http_json", fake)
    assert et._get_oauth_token() is None
    assert et._get_oauth_token() is None            # within cooldown: no second request
    assert fake.count("token") == 1
    clock["now"] += et.FAILURE_COOLDOWN_SECONDS + 1
    assert et._get_oauth_token() is None
    assert fake.count("token") == 2


def test_token_network_error_and_bad_json_return_none(monkeypatch, creds):
    def boom(req):
        raise urllib.error.URLError("down")
    monkeypatch.setattr(et, "_http_json", boom)
    assert et._get_oauth_token() is None

    et.reset_state()
    monkeypatch.setattr(et, "_http_json", lambda req: (200, {"unexpected": True}))   # no access_token
    assert et._get_oauth_token() is None


# ── Aspects: filtering / normalization ───────────────────────────────────────

def test_only_required_aspects_are_returned(monkeypatch, creds):
    monkeypatch.setattr(et, "_http_json", FakeEbay(aspects_response=(200, {"aspects": [
        aspect("Brand", required=True),
        aspect("Color", required=False),
        aspect("Country of Origin", required=True, style="requirement"),   # `requirement: REQUIRED` spelling
        aspect("Features", required=False, style="requirement"),
    ]})))
    names = [a["name"] for a in et.get_item_aspects("111")]
    assert names == ["Brand", "Country of Origin"]


def test_aspect_normalization(monkeypatch, creds):
    monkeypatch.setattr(et, "_http_json", FakeEbay(aspects_response=(200, {"aspects": [
        aspect("Type", mode="SELECTION_ONLY", values=["Bar", "Coin"], cardinality="MULTI", max_length=65),
        aspect("Brand", mode="FREE_TEXT", values=["ignored"]),
    ]})))
    type_, brand = et.get_item_aspects("111")
    assert type_ == {"name": "Type", "mode": "SELECTION_ONLY", "values": ["Bar", "Coin"],
                     "multi": True, "max_length": 65}
    assert brand["mode"] == "FREE_TEXT" and brand["values"] == [] and not brand["multi"]


def test_stored_values_are_capped(monkeypatch, creds):
    many = [f"v{i}" for i in range(et.MAX_STORED_VALUES + 50)]
    monkeypatch.setattr(et, "_http_json", FakeEbay(aspects_response=(200, {"aspects": [
        aspect("Brand", mode="SELECTION_ONLY", values=many)]})))
    assert len(et.get_item_aspects("111")[0]["values"]) == et.MAX_STORED_VALUES


def test_no_required_aspects_is_empty_list_not_none(monkeypatch, creds):
    monkeypatch.setattr(et, "_http_json", FakeEbay(aspects_response=(200, {"aspects": [
        aspect("Color", required=False)]})))
    assert et.get_item_aspects("111") == []


def test_request_carries_category_and_bearer_token(ebay):
    et.get_item_aspects("183904")
    kind, req = ebay.calls[-1]
    assert kind == "aspects"
    assert req.full_url == f"{et.ASPECTS_URL}?category_id=183904"
    assert req.get_header("Authorization") == "Bearer tok1"


# ── Aspects: caching ─────────────────────────────────────────────────────────

def test_second_lookup_is_served_from_cache(ebay):
    first = et.get_item_aspects("111")
    assert et.get_item_aspects("111") == first
    assert ebay.aspects_calls == 1


def test_cache_persists_to_file_and_reloads_across_processes(ebay):
    first = et.get_item_aspects("111")
    on_disk = json.loads(et.CACHE_PATH.read_text(encoding="utf-8"))
    assert on_disk["111"]["aspects"] == first

    et.reset_state()                    # "new process": memory gone, file remains
    assert et.get_item_aspects("111") == first
    assert ebay.aspects_calls == 1


def test_cache_expires_after_ttl(ebay, clock):
    et.get_item_aspects("111")
    clock["now"] += et.CACHE_TTL_SECONDS - 10
    et.get_item_aspects("111")
    assert ebay.aspects_calls == 1
    clock["now"] += 20
    et.get_item_aspects("111")
    assert ebay.aspects_calls == 2


def test_refresh_bypasses_cache(ebay):
    et.get_item_aspects("111")
    et.get_item_aspects("111", refresh=True)
    assert ebay.aspects_calls == 2


def test_corrupt_cache_file_is_ignored(ebay):
    et.CACHE_PATH.write_text("{not json", encoding="utf-8")
    assert [a["name"] for a in et.get_item_aspects("111")] == ["Brand", "Model"]


# ── Aspects: failure handling ────────────────────────────────────────────────

def test_bad_category_is_none_and_negatively_cached(monkeypatch, creds, clock):
    fake = FakeEbay(aspects_response=(400, {"errors": [{"message": "not a leaf category"}]}))
    monkeypatch.setattr(et, "_http_json", fake)
    assert et.get_item_aspects("999") is None
    assert et.get_item_aspects("999") is None
    assert fake.aspects_calls == 1                       # negative cache hit
    clock["now"] += et.NEGATIVE_TTL_SECONDS + 1
    et.get_item_aspects("999")
    assert fake.aspects_calls == 2


def test_server_error_returns_none_after_one_retry_and_is_not_cached(monkeypatch, creds, clock):
    fake = FakeEbay(aspects_response=(503, {}))
    monkeypatch.setattr(et, "_http_json", fake)
    assert et.get_item_aspects("111") is None
    assert fake.aspects_calls == 2                       # original + one retry
    assert not et.CACHE_PATH.exists()                    # transient: nothing persisted
    assert et.get_item_aspects("111") is None            # inside cooldown: no hammering
    assert fake.aspects_calls == 2
    clock["now"] += et.FAILURE_COOLDOWN_SECONDS + 1
    fake.aspects_response = (200, {"aspects": [aspect("Brand")]})
    assert [a["name"] for a in et.get_item_aspects("111")] == ["Brand"]


def test_401_reauthenticates_once_then_succeeds(monkeypatch, creds):
    fake = FakeEbay(aspects_response=lambda cat, n: (401, {}) if n == 1
                    else (200, {"aspects": [aspect("Brand")]}))
    monkeypatch.setattr(et, "_http_json", fake)
    assert [a["name"] for a in et.get_item_aspects("111")] == ["Brand"]
    assert fake.tokens_issued == 2


def test_persistent_401_gives_up(monkeypatch, creds):
    fake = FakeEbay(aspects_response=(401, {}))
    monkeypatch.setattr(et, "_http_json", fake)
    assert et.get_item_aspects("111") is None
    assert fake.tokens_issued == 2 and fake.aspects_calls == 2


def test_no_token_means_none_and_no_aspect_request(monkeypatch, creds):
    fake = FakeEbay(token_status=401)
    monkeypatch.setattr(et, "_http_json", fake)
    assert et.get_item_aspects("111") is None
    assert fake.aspects_calls == 0


def test_never_raises(monkeypatch, creds):
    def boom(req):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(et, "_http_json", boom)
    assert et.get_item_aspects("111") is None
    assert et.get_item_aspects("") is None
    assert et.get_item_aspects(None) is None


def test_unwritable_cache_does_not_break_lookup(ebay, monkeypatch):
    monkeypatch.setattr(et, "CACHE_PATH", et.CACHE_PATH / "sub" / "x.json")   # parent is not a dir
    et.CACHE_PATH.parent.parent.write_text("i am a file", encoding="utf-8")
    assert [a["name"] for a in et.get_item_aspects("111")] == ["Brand", "Model"]


# ── Export wiring ────────────────────────────────────────────────────────────

def _row(category="Small Appliances", title="Ninja Blender Black", notes=""):
    row = [""] * 50
    row[ee._COL["status"]] = "READY"
    row[ee._COL["title"]] = title
    row[ee._COL["category"]] = category
    row[ee._COL["ebay_price"]] = "99.99"
    row[ee._COL["seo_title"]] = title
    row[ee._COL["notes"]] = notes
    return row


CONFIG = {
    "business": {},
    "categories": {
        "Small Appliances": {
            "ebay_category_id": "14070",
            "ebay_required_specifics": ["C:Brand", "C:Type", "C:Model", "C:Color"],
            "default_dimensions": {"length": 14, "width": 12, "height": 14},
        },
    },
}


def _export(config=CONFIG, **row_kwargs):
    text = ee.generate_ebay_csv([(4, _row(**row_kwargs))], config)
    reader = csv.DictReader(io.StringIO(text))
    return reader.fieldnames, list(reader)


def _serve(monkeypatch, aspects):
    monkeypatch.setattr(ee.ebay_taxonomy, "get_item_aspects", lambda cat_id, **kw: aspects)


def test_export_falls_back_to_yaml_when_taxonomy_unavailable():
    # conftest blocks HTTP, so this is the genuine offline path
    columns, rows = _export()
    r = rows[0]
    assert r["C:Brand"] == "Ninja" and r["C:Type"] == "Blender" and r["C:Color"] == "Black"
    assert r["C:Model"] == "Does Not Apply"
    assert r["C:Item Length"] == ""             # not in the yaml required list → untouched
    assert columns == ee._EBAY_COLUMNS


def test_export_uses_taxonomy_required_list_instead_of_yaml(monkeypatch):
    monkeypatch.setattr(ee, "YAML_FLOOR", False)
    _serve(monkeypatch, [et._normalize(aspect("Brand")), et._normalize(aspect("Item Length"))])
    columns, rows = _export()
    r = rows[0]
    assert r["C:Item Length"] == "14 in"        # dimension logic applied to a Taxonomy-only requirement
    assert r["C:Brand"] == "Ninja"
    assert r["C:Model"] == ""                   # yaml's Model requirement no longer forces a value
    assert r["C:Type"] == "Blender"             # ...but values the infer logic finds are still emitted
    assert columns == ee._EBAY_COLUMNS          # both required aspects are existing columns


def test_yaml_floor_is_on_by_default_and_unions_with_taxonomy(monkeypatch):
    assert ee.YAML_FLOOR is True
    _serve(monkeypatch, [et._normalize(aspect("Brand")), et._normalize(aspect("Item Length"))])
    _, rows = _export()
    r = rows[0]
    assert r["C:Item Length"] == "14 in"            # Taxonomy requirement
    assert r["C:Model"] == "Does Not Apply"         # yaml requirement kept as a floor
    assert r["C:Color"] == "Black"


def test_yaml_floor_does_not_duplicate_keys(monkeypatch):
    _serve(monkeypatch, [et._normalize(aspect("brand")), et._normalize(aspect("Color"))])
    required, _ = ee._required_specifics("14070", CONFIG["categories"]["Small Appliances"])
    assert sorted(k.casefold() for k in required) == sorted(set(k.casefold() for k in required))
    assert {"C:Brand", "C:Color", "C:Type", "C:Model"} <= set(required)


def test_required_specifics_falls_back_to_yaml_when_unavailable():
    required, aspects = ee._required_specifics("14070", CONFIG["categories"]["Small Appliances"])
    assert required == ["C:Brand", "C:Type", "C:Model", "C:Color"] and aspects == {}


def test_does_not_apply_becomes_other_on_selection_only_aspect_without_it(monkeypatch):
    _serve(monkeypatch, [et._normalize(aspect("Country of Origin", mode="SELECTION_ONLY",
                                              values=["China", "Other"])),
                         et._normalize(aspect("Department", mode="SELECTION_ONLY",
                                              values=["Men", "Women"]))])
    _, rows = _export()
    assert rows[0]["C:Country of Origin"] == "Other"
    assert rows[0]["C:Department"] == "Does Not Apply"     # nothing valid to use; logged for manual fix


def test_taxonomy_only_aspect_gets_its_own_column_and_is_never_blank(monkeypatch):
    _serve(monkeypatch, [et._normalize(aspect("Brand")), et._normalize(aspect("Country of Origin")),
                         et._normalize(aspect("Capacity"))])
    columns, rows = _export()
    assert "C:Country of Origin" in columns and "C:Capacity" in columns
    split = columns.index("ShippingProfileName")
    assert columns.index("C:Country of Origin") < split           # before the profile columns
    assert rows[0]["C:Country of Origin"] == "Does Not Apply"
    assert rows[0]["C:Capacity"] == "Does Not Apply"


def test_category_aspect_defaults_beat_does_not_apply(monkeypatch):
    cfg = json.loads(json.dumps(CONFIG))
    cfg["categories"]["Small Appliances"]["ebay_aspect_defaults"] = {"C:Country of Origin": "China"}
    _serve(monkeypatch, [et._normalize(aspect("Country of Origin"))])
    _, rows = _export(cfg)
    assert rows[0]["C:Country of Origin"] == "China"


def test_taxonomy_names_map_onto_existing_columns_case_insensitively(monkeypatch):
    _serve(monkeypatch, [et._normalize(aspect("brand")), et._normalize(aspect("COLOR"))])
    columns, rows = _export()
    assert columns == ee._EBAY_COLUMNS               # no duplicate "C:brand" / "C:COLOR" columns
    assert rows[0]["C:Brand"] == "Ninja" and rows[0]["C:Color"] == "Black"


def test_selection_only_values_are_normalized_to_allowed_list(monkeypatch):
    _serve(monkeypatch, [
        et._normalize(aspect("Type", mode="SELECTION_ONLY", values=["Blenders", "Other"])),
        et._normalize(aspect("Color", mode="SELECTION_ONLY", values=["Black", "White"])),
        et._normalize(aspect("Model", mode="SELECTION_ONLY", values=["X1"])),
    ])
    _, rows = _export(notes="Costco specs: Model: bLaCk")
    r = rows[0]
    assert r["C:Type"] == "Other"                    # "Blender" not allowed → "Other"
    assert r["C:Color"] == "Black"                   # already canonical
    assert r["C:Model"] == "Does Not Apply"          # no "Other" allowed → Does Not Apply


def test_selection_only_case_insensitive_match_uses_canonical_spelling(monkeypatch):
    _serve(monkeypatch, [et._normalize(aspect("Color", mode="SELECTION_ONLY", values=["BLACK"]))])
    _, rows = _export()
    assert rows[0]["C:Color"] == "BLACK"


def test_values_are_truncated_to_max_length(monkeypatch):
    _serve(monkeypatch, [et._normalize(aspect("Model", max_length=5))])
    _, rows = _export(notes="Costco specs: Model: ABCDEFGHIJ")
    assert rows[0]["C:Model"] == "ABCDE"


def test_empty_taxonomy_answer_means_nothing_required_not_fallback(monkeypatch):
    monkeypatch.setattr(ee, "YAML_FLOOR", False)
    _serve(monkeypatch, [])
    _, rows = _export()
    assert rows[0]["C:Model"] == ""                  # yaml's Model requirement NOT applied


def test_export_end_to_end_through_http_seam(monkeypatch, creds):
    fake = FakeEbay(aspects_response=(200, {"aspects": [aspect("Brand"), aspect("Country of Origin")]}))
    monkeypatch.setattr(et, "_http_json", fake)
    columns, rows = _export()
    assert "C:Country of Origin" in columns
    assert rows[0]["C:Country of Origin"] == "Does Not Apply" and rows[0]["C:Brand"] == "Ninja"
    _export()                                        # second export: served from cache
    assert fake.aspects_calls == 1


def test_export_rows_share_columns_and_missing_cells_are_blank(monkeypatch):
    def per_cat(cat_id, **kw):
        return [et._normalize(aspect("Brand"))] + ([et._normalize(aspect("Capacity"))] if cat_id == "14070" else [])
    monkeypatch.setattr(ee.ebay_taxonomy, "get_item_aspects", per_cat)
    cfg = json.loads(json.dumps(CONFIG))
    cfg["categories"]["Other"] = {"ebay_category_id": "555"}
    text = ee.generate_ebay_csv([(4, _row()), (5, _row(category="Other", title="Widget"))], cfg)
    a, b = list(csv.DictReader(io.StringIO(text)))
    assert a["C:Capacity"] == "Does Not Apply" and b["C:Capacity"] == ""
