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

    def __init__(self, aspects_response=None, token_status=200, expires_in=7200,
                 suggestions_response=None):
        self.suggestions_response = suggestions_response if suggestions_response is not None else (200, {})
        self.suggestion_calls = 0
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
        if url.startswith(et.SUGGESTIONS_URL):
            self.calls.append(("suggestions", req))
            self.suggestion_calls += 1
            resp = self.suggestions_response
            return resp(self.suggestion_calls) if callable(resp) else resp
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


# ── Category suggestions ─────────────────────────────────────────────────────

def suggestion(cat_id, name, level, ancestors=()):
    """A categorySuggestions[] entry; `ancestors` = [(id, name, level), ...] in eBay's leaf-first order."""
    return {
        "category": {"categoryId": str(cat_id), "categoryName": name},
        "categoryTreeNodeLevel": level,
        "categoryTreeNodeAncestors": [
            {"categoryId": str(i), "categoryName": n, "categoryTreeNodeLevel": lv} for i, n, lv in ancestors],
        "relevancy": "1.0",
    }


def suggest_response(*entries):
    return 200, {"categoryTreeId": "0", "categorySuggestions": list(entries)}


@pytest.fixture
def sugg_ebay(monkeypatch, creds):
    fake = FakeEbay(aspects_response=(200, {"aspects": [aspect("Brand")]}),
                    suggestions_response=suggest_response(
                        suggestion(111, "Air Fryers", 4, [(30, "Kitchen", 2), (20, "Home", 1)])))
    monkeypatch.setattr(et, "_http_json", fake)
    return fake


def test_suggestions_request_url_and_bearer(sugg_ebay):
    et.get_category_suggestions("Ninja Air Fryer 5.5 qt & more")
    kind, req = sugg_ebay.calls[-1]
    assert kind == "suggestions"
    assert req.full_url == f"{et.SUGGESTIONS_URL}?q=Ninja+Air+Fryer+5.5+qt+%26+more"
    assert req.get_header("Authorization") == "Bearer tok1"


def test_suggestions_are_normalized_with_root_first_path(sugg_ebay):
    (top,) = et.get_category_suggestions("Ninja Air Fryer")
    assert top == {"id": "111", "name": "Air Fryers", "level": 4,
                   "path": "Home > Kitchen > Air Fryers"}


def test_suggestions_keep_ebays_relevance_order_not_depth(monkeypatch, creds):
    # Regression: sorting deepest-first put commercial deep fryers / watch dials ahead of the
    # real answer. eBay ranks by relevance; every suggestion is already a leaf.
    monkeypatch.setattr(et, "_http_json", FakeEbay(suggestions_response=suggest_response(
        suggestion(1, "Fryers", 4),
        suggestion(2, "Commercial Electric Fryers", 6),
        suggestion(3, "Toaster Ovens", 4))))
    got = et.get_category_suggestions("air fryer")
    assert [x["id"] for x in got] == ["1", "2", "3"]
    assert got[1]["level"] == 6                         # level is still reported


def test_no_match_is_empty_list_not_none(monkeypatch, creds):
    monkeypatch.setattr(et, "_http_json", FakeEbay(suggestions_response=(200, {"categoryTreeId": "0"})))
    assert et.get_category_suggestions("zzzz") == []


def test_malformed_suggestion_entries_are_skipped(monkeypatch, creds):
    monkeypatch.setattr(et, "_http_json", FakeEbay(suggestions_response=suggest_response(
        {"category": {}}, "junk", suggestion(7, "Ok", 3))))
    assert [x["id"] for x in et.get_category_suggestions("t")] == ["7"]


def test_title_is_trimmed_and_capped(sugg_ebay):
    et.get_category_suggestions("  " + "word " * 100)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(sugg_ebay.calls[-1][1].full_url).query)["q"][0]
    assert len(q) <= et.MAX_QUERY_CHARS and q == q.strip()


def test_blank_title_is_none_without_http(sugg_ebay):
    assert et.get_category_suggestions("   ") is None and et.get_category_suggestions(None) is None
    assert sugg_ebay.calls == []


def test_suggestions_cached_per_title_case_insensitively(sugg_ebay):
    first = et.get_category_suggestions("Ninja Air Fryer")
    assert et.get_category_suggestions("  ninja  AIR fryer ") == first
    assert sugg_ebay.suggestion_calls == 1
    et.get_category_suggestions("Different Title")
    assert sugg_ebay.suggestion_calls == 2


def test_suggestion_cache_persists_and_does_not_collide_with_category_keys(sugg_ebay):
    first = et.get_category_suggestions("Ninja Air Fryer")
    et.get_item_aspects("111")
    on_disk = json.loads(et.CACHE_PATH.read_text(encoding="utf-8"))
    assert on_disk["q:ninja air fryer"]["suggestions"] == first
    assert "aspects" in on_disk["111"] and "suggestions" not in on_disk["111"]
    et.reset_state()
    assert et.get_category_suggestions("Ninja Air Fryer") == first
    assert sugg_ebay.suggestion_calls == 1


def test_suggestion_cache_expires_and_empty_result_expires_sooner(monkeypatch, creds, clock):
    fake = FakeEbay(suggestions_response=suggest_response(suggestion(1, "A", 3)))
    monkeypatch.setattr(et, "_http_json", fake)
    et.get_category_suggestions("has match")
    clock["now"] += et.NEGATIVE_TTL_SECONDS + 10           # past 1 day, well inside 30 days
    et.get_category_suggestions("has match")
    assert fake.suggestion_calls == 1
    clock["now"] += et.CACHE_TTL_SECONDS
    et.get_category_suggestions("has match")
    assert fake.suggestion_calls == 2

    fake.suggestions_response = (200, {})
    et.get_category_suggestions("no match")                 # cached as []
    et.get_category_suggestions("no match")
    assert fake.suggestion_calls == 3
    clock["now"] += et.NEGATIVE_TTL_SECONDS + 1
    et.get_category_suggestions("no match")
    assert fake.suggestion_calls == 4


def test_refresh_bypasses_suggestion_cache(sugg_ebay):
    et.get_category_suggestions("t")
    et.get_category_suggestions("t", refresh=True)
    assert sugg_ebay.suggestion_calls == 2


def test_suggestions_failure_is_none_uncached_with_cooldown(monkeypatch, creds, clock):
    fake = FakeEbay(suggestions_response=(500, {}))
    monkeypatch.setattr(et, "_http_json", fake)
    assert et.get_category_suggestions("t") is None
    assert fake.suggestion_calls == 2                      # original + one retry
    assert not et.CACHE_PATH.exists()
    assert et.get_category_suggestions("t") is None        # cooldown: no hammering
    assert fake.suggestion_calls == 2
    clock["now"] += et.FAILURE_COOLDOWN_SECONDS + 1
    fake.suggestions_response = suggest_response(suggestion(9, "Z", 3))
    assert [x["id"] for x in et.get_category_suggestions("t")] == ["9"]


def test_suggestions_401_reauths_once(monkeypatch, creds):
    fake = FakeEbay(suggestions_response=lambda n: (401, {}) if n == 1 else suggest_response(suggestion(9, "Z", 3)))
    monkeypatch.setattr(et, "_http_json", fake)
    assert [x["id"] for x in et.get_category_suggestions("t")] == ["9"]
    assert fake.tokens_issued == 2


def test_suggestions_no_creds_or_crash_never_raise(monkeypatch):
    monkeypatch.delenv("EBAY_APP_ID", raising=False)
    monkeypatch.delenv("EBAY_CERT_ID", raising=False)
    assert et.get_category_suggestions("t") is None

    def boom(req):
        raise RuntimeError("kaboom")
    monkeypatch.setenv("EBAY_APP_ID", "a")
    monkeypatch.setenv("EBAY_CERT_ID", "b")
    et.reset_state()
    monkeypatch.setattr(et, "_http_json", boom)
    assert et.get_category_suggestions("t") is None


# ── --check drift helpers ────────────────────────────────────────────────────

def test_drift_status():
    top = [{"id": "111"}]
    assert et._drift_status("111", True, top) == "OK"
    assert et._drift_status("222", True, top) == "DIFFERS"
    assert et._drift_status("222", False, top) == "STALE"
    assert et._drift_status("222", False, []) == "STALE (no suggestion)"
    assert et._drift_status("222", True, None) == "NO SUGGESTION"


def test_representative_title_prefers_keyword_match_then_keyword_then_first():
    known = {"Jewelry": ["Gold Ring 14kt", "Gold Bracelet 14kt", "Silver Chain"]}
    assert et._representative_title("Jewelry", "bracelet", known) == "Gold Bracelet 14kt"
    assert et._representative_title("Jewelry", "earring", known) == "earring"      # no match -> keyword
    assert et._representative_title("Jewelry", "default", known) == "Gold Ring 14kt"
    assert et._representative_title("Toys", "default", known) == "Toys"


def test_load_known_titles_from_knowledge_dir(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"title": "T1", "category": "Jewelry"}), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps({"title": "T2", "category": "Jewelry"}), encoding="utf-8")
    (tmp_path / "c.json").write_text("{broken", encoding="utf-8")
    (tmp_path / "d.json").write_text(json.dumps({"title": "no category"}), encoding="utf-8")
    assert et._load_known_titles(tmp_path) == {"Jewelry": ["T1", "T2"]}


def test_check_category_suggestions_reports_stale_and_ok(monkeypatch, creds, capsys):
    def aspects_resp(cat_id, n):
        return (200, {"aspects": [aspect("Brand")]}) if cat_id == "111" else (400, {"errors": [{"message": "x"}]})
    fake = FakeEbay(aspects_response=aspects_resp,
                    suggestions_response=suggest_response(suggestion(111, "Air Fryers", 4)))
    monkeypatch.setattr(et, "_http_json", fake)
    cats = {"Small Appliances": {"ebay_category_id": "111", "ebay_category_map": {"air fryer": "111", "coffee": "999"}}}
    not_ok = et.check_category_suggestions(cats, {"Small Appliances": ["Ninja Air Fryer"]})
    out = capsys.readouterr().out
    assert out.count("synthetic") == 1                  # only the 'coffee' row lacks a real product title
    assert not_ok == 1
    assert "yaml 111 (valid leaf)  ->  OK" in out
    assert "yaml 999 (NOT a valid leaf)  ->  STALE" in out
    assert "suggested: 111 Air Fryers" in out


# ── Export: suggestions resolve the category ─────────────────────────────────

def _spy_aspects(monkeypatch, valid):
    """Fake get_item_aspects: aspects for ids in `valid`, None otherwise; records ids asked."""
    asked = []

    def fake(cat_id, **kw):
        asked.append(cat_id)
        return [et._normalize(aspect("Brand"))] if cat_id in valid else None
    monkeypatch.setattr(ee.ebay_taxonomy, "get_item_aspects", fake)
    return asked


def _suggest(monkeypatch, result):
    monkeypatch.setattr(ee.ebay_taxonomy, "get_category_suggestions", lambda title, **kw: result)


def _sg(cat_id, name="Cat", level=4):
    return {"id": str(cat_id), "name": name, "level": level, "path": f"Root > {name}"}


def test_suggestion_overrides_yaml_id_and_feeds_aspects(monkeypatch):
    asked = _spy_aspects(monkeypatch, valid={"777"})
    _suggest(monkeypatch, [_sg(777, "Air Fryers")])
    _, rows = _export()
    assert rows[0]["Category"] == "777"                 # yaml said 14070
    assert "777" in asked and asked[-1] == "777"        # specifics come from the written category
    assert "14070" not in asked


def test_export_falls_back_to_yaml_id_when_suggestions_unavailable(monkeypatch):
    _spy_aspects(monkeypatch, valid={"14070"})
    _suggest(monkeypatch, None)
    _, rows = _export()
    assert rows[0]["Category"] == "14070"


def test_export_falls_back_to_yaml_id_when_no_suggestions(monkeypatch):
    _spy_aspects(monkeypatch, valid={"14070"})
    _suggest(monkeypatch, [])
    _, rows = _export()
    assert rows[0]["Category"] == "14070"


def test_invalid_top_suggestion_is_skipped_for_next_valid_one(monkeypatch):
    _spy_aspects(monkeypatch, valid={"222"})
    _suggest(monkeypatch, [_sg(111), _sg(222)])          # 111 is not a valid leaf
    _, rows = _export()
    assert rows[0]["Category"] == "222"


def test_all_suggestions_invalid_falls_back_to_yaml_id(monkeypatch):
    _spy_aspects(monkeypatch, valid=set())
    _suggest(monkeypatch, [_sg(111), _sg(222), _sg(333)])
    _, rows = _export()
    assert rows[0]["Category"] == "14070"


def test_only_top_candidates_are_tried(monkeypatch):
    asked = _spy_aspects(monkeypatch, valid={"5"})
    _suggest(monkeypatch, [_sg(1), _sg(2), _sg(3), _sg(4), _sg(5)])
    _, rows = _export()
    assert rows[0]["Category"] == "14070" and "4" not in asked and "5" not in asked


def test_suggestions_primary_off_uses_yaml_only(monkeypatch):
    monkeypatch.setattr(ee, "SUGGESTIONS_PRIMARY", False)
    _spy_aspects(monkeypatch, valid={"777", "14070"})
    _suggest(monkeypatch, [_sg(777)])
    _, rows = _export()
    assert rows[0]["Category"] == "14070"


def test_migration_still_applies_to_yaml_fallback_id(monkeypatch):
    _spy_aspects(monkeypatch, valid=set())
    _suggest(monkeypatch, None)
    cfg = {"business": {}, "categories": {"Pharmacy": {"ebay_category_id": "11896"}}}
    text = ee.generate_ebay_csv([(4, _row(category="Pharmacy", title="Vitamin D"))], cfg)
    assert list(csv.DictReader(io.StringIO(text)))[0]["Category"] == "183904"


def test_override_is_logged_once_per_pair(monkeypatch):
    from loguru import logger
    ee._logged_suggestions.clear()
    _spy_aspects(monkeypatch, valid={"777"})
    _suggest(monkeypatch, [_sg(777, "Air Fryers")])
    msgs = []
    sink = logger.add(lambda m: msgs.append(str(m)), level="INFO")
    try:
        ee._suggested_category_id("Ninja Air Fryer", "14070")
        ee._suggested_category_id("Ninja Air Fryer", "14070")
    finally:
        logger.remove(sink)
    assert sum("eBay suggests 777" in m for m in msgs) == 1


def test_suggestion_matching_yaml_id_is_silent(monkeypatch):
    ee._logged_suggestions.clear()
    _spy_aspects(monkeypatch, valid={"14070"})
    _suggest(monkeypatch, [_sg(14070)])
    assert ee._suggested_category_id("t", "14070") == "14070" and not ee._logged_suggestions


def test_export_end_to_end_through_http_seam(monkeypatch, creds):
    fake = FakeEbay(aspects_response=(200, {"aspects": [aspect("Brand")]}),
                    suggestions_response=suggest_response(suggestion(777, "Air Fryers", 4)))
    monkeypatch.setattr(et, "_http_json", fake)
    _, rows = _export()
    assert rows[0]["Category"] == "777"
    aspect_urls = [r.full_url for k, r in fake.calls if k == "aspects"]
    assert aspect_urls and all(u.endswith("category_id=777") for u in aspect_urls)
    _export()                                            # second run served from cache
    assert fake.suggestion_calls == 1 and fake.aspects_calls == 1


def test_serper_lookup_is_gone():
    assert not hasattr(ee, "_serper_category_lookup")
