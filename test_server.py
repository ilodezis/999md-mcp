"""Offline tests for parsing/formatting, then live tests against 999.md through the MCP layer
(the site's API contract is the point). Skip the live part with OFFLINE=1.
"""

import asyncio
import io
import os

import PIL.Image
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import server as m

live = pytest.mark.skipif(os.environ.get("OFFLINE") == "1", reason="OFFLINE=1")
FLATS, RENT_MONTHLY, ROOMS, TWO_ROOMS, REGION, CHISINAU_MUN, CITY, CHISINAU = 1404, 912, 241, 894, 7, 12900, 8, 13859


def call(tool: str, **args) -> dict:
    """Call a tool the way a client does: JSON arguments in, structured result out."""
    async def go():
        async with Client(m.mcp) as c:
            return await c.call_tool(tool, args)
    return asyncio.run(go()).structured_content


# ---------- offline ----------

def test_parse_ad_id():
    assert m.parse_ad_id("105361034") == "105361034"
    assert m.parse_ad_id("https://999.md/ru/105361034?utm=x") == "105361034"
    with pytest.raises(ToolError):
        m.parse_ad_id("iphone")


def test_category_input():
    assert m.category_input(1404) == {"id": 1404}
    assert m.category_input("real-estate/apartments-and-rooms/") == {"url": "real-estate/apartments-and-rooms"}
    assert m.category_input("https://999.md/ro/list/transport/cars?o_16_1=776") == {"url": "transport/cars"}


def test_build_filters_one_group_per_feature_and_price_shortcut():
    f = m.build_filters([m.Filter(feature_id=ROOMS, option_ids=[893, 894]), m.Filter(feature_id=188),
                         m.Filter(feature_id=244, min=50, unit="METER_SQUARE")], price_max=600, currency="EUR")
    assert f == [
        {"filterId": 0, "features": [{"featureId": ROOMS, "optionIds": [893, 894]}]},
        {"filterId": 0, "features": [{"featureId": 188}]},
        {"filterId": 0, "features": [{"featureId": 244, "range": {"min": 50}, "unit": "UNIT_METER_SQUARE"}]},
        {"filterId": 0, "features": [{"featureId": m.F_PRICE, "range": {"max": 600}, "unit": "UNIT_EUR"}]},
    ]


def test_formatting():
    assert m.fmt_price({"value": 1600, "unit": "UNIT_EUR", "mode": "PM_FIXED", "bargain": False}) == "1600 EUR"
    assert m.fmt_price({"value": 50, "unit": "UNIT_MDL", "mode": "PM_FROM", "bargain": True}) == "from 50 MDL, negotiable"
    assert m.fmt_price({"value": 0, "unit": "UNIT_EUR"}) is None
    assert m.fmt_value({"unit": "UNIT_METER_SQUARE", "value": 88}) == "88 m²"
    assert m.fmt_value({"translated": "Центр", "value": 15664}) == "Центр"
    ad = {"region": {"value": {"translated": "Кишинёв мун."}}, "city": {"value": {"translated": "Кишинёв"}},
          "sector": {"value": {"translated": "Центр"}}}
    assert m.location(ad) == "Центр, Кишинёв"
    assert m.location({"region": {"value": {"translated": "Бельцы мун."}}}) == "Бельцы мун."


def test_ad_details_splits_amenities_and_skips_separate_fields():
    groups = [{"controls": [
        {"title": "Цена", "feature": {"id": m.F_PRICE, "type": "FEATURE_PRICE", "value": {"value": 500, "unit": "UNIT_EUR"}}},
        {"title": "Общая площадь", "feature": {"id": 244, "type": "FEATURE_INT_UNIT", "value": {"value": 50, "unit": "UNIT_METER_SQUARE"}}},
        {"title": "Лифт", "feature": {"id": 186, "type": "FEATURE_BOOLEAN", "value": True}},
        {"title": "Дом", "feature": {"id": m.F_HOUSE, "type": "FEATURE_TEXT", "value": "-"}},
        {"title": "Контакты", "feature": {"id": m.F_CONTACTS, "type": "FEATURE_CONTACTS", "value": {"phone_numbers": ["37360000000"]}}},
    ]}]
    d = m.ad_details({"id": "1", "title": "t", "state": "AD_STATE_PUBLIC", "groups": groups})
    assert d["price"] == "500 EUR"
    assert d["characteristics"] == {"Общая площадь": "50 m²"}
    assert d["amenities"] == ["Лифт"]
    assert d["phones"] == ["+37360000000"]
    assert d["location"] == {}
    assert d["state"] == "public"


def test_flatten_filters_units_match_search_input():
    f = m.flatten_filters([{"type": "FILTER_TYPE_RANGE", "title": {"translated": "Площадь"}, "units": ["UNIT_METER_SQUARE"],
                            "features": [{"id": 244, "title": None, "options": [], "parentId": 0}]}], 10)
    assert f == [{"feature_id": 244, "title": "Площадь", "kind": "range", "units": ["METER_SQUARE"]}]


def test_shrink_keeps_whole_frame():
    buf = io.BytesIO()
    PIL.Image.new("RGB", (900, 1200)).save(buf, "JPEG")
    assert PIL.Image.open(io.BytesIO(m.shrink(buf.getvalue()))).size == (576, 768)


def test_throttle_spaces_concurrent_requests(monkeypatch):
    starts = []

    class FakeClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, json):
            starts.append(asyncio.get_running_loop().time())
            return type("R", (), {"status_code": 200, "json": lambda self: {"data": {}}})()

    monkeypatch.setattr(m.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(m, "_next_start", 0.0)

    async def burst():
        await asyncio.gather(*(m.gql("q", {}) for _ in range(4)))
    asyncio.run(burst())
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(g >= m.MIN_INTERVAL * 0.9 for g in gaps), gaps


def test_tools_are_registered_read_only():
    async def go():
        async with Client(m.mcp) as c:
            return await c.list_tools()
    tools = asyncio.run(go())
    assert {t.name for t in tools} == {"search", "get_ad", "photos", "get_filters",
                                       "get_options", "categories", "seller_ads", "price_stats"}
    assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in tools)


# ---------- live ----------

@live
def test_search_rent_two_rooms_chisinau_within_budget():
    base = dict(category="real-estate/apartments-and-rooms", limit=5,
                filters=[{"feature_id": 1, "option_ids": [RENT_MONTHLY]}, {"feature_id": REGION, "option_ids": [CHISINAU_MUN]},
                         {"feature_id": ROOMS, "option_ids": [TWO_ROOMS]}])
    wide = call("search", **base)
    narrow = call("search", **base, price_min=300, price_max=600, sort="price_asc")
    assert 0 < narrow["total"] < wide["total"]
    assert narrow["has_more"] and narrow["next_offset"] == 5
    ad = narrow["ads"][0]
    assert ad["url"].startswith("https://999.md/") and ad["price"] and ad["offer"]


@live
def test_get_ad_has_details_and_phone():
    ad_id = call("search", query="iphone", category=40, limit=1)["ads"][0]["id"]
    d = call("get_ad", ad=f"https://999.md/ru/{ad_id}")
    assert d["id"] == ad_id and d["title"] and d["characteristics"]
    assert d["seller"]["login"]
    assert all(p.startswith("+") for p in d["phones"])


@live
def test_photos_compact_and_full():
    ad_id = next(a["id"] for a in call("search", category=FLATS, limit=10)["ads"] if a["photo"])

    async def go():
        async with Client(m.mcp) as c:
            return [(await c.call_tool("photos", {"ad": ad_id, "limit": 2, "full_resolution": full})).content
                    for full in (False, True)]
    compact, full = asyncio.run(go())
    assert compact[0].type == "text" and f"Ad {ad_id}: photos 1-" in compact[0].text
    assert [b.type for b in compact[1:]] == ["image"] * (len(compact) - 1) and compact[1].mime_type == "image/jpeg"
    assert len(compact[1].data) < len(full[1].data)


@live
def test_filters_and_dependent_options():
    f = call("get_filters", category=FLATS)
    rooms = next(x for x in f["filters"] if x["feature_id"] == ROOMS)
    assert rooms["kind"] == "options" and any(o["id"] == TWO_ROOMS for o in rooms["options"])
    city = next(x for x in f["filters"] if x["feature_id"] == CITY)
    assert city["depends_on_feature"] == REGION
    cities = call("get_options", feature_id=CITY, parent_option_id=CHISINAU_MUN)
    assert any(o["id"] == CHISINAU for o in cities["options"])
    sectors = call("get_options", feature_id=9, parent_option_id=CHISINAU, query="Бот")
    assert [o["title"] for o in sectors["options"]] == ["Ботаника"]


@live
def test_categories_three_modes_and_unknown_category():
    top = call("categories")["categories"]
    assert any(c["url"] == "real-estate" for c in top)
    subs = call("categories", category="real-estate")["subcategories"]
    assert any(s["id"] == FLATS for s in subs)
    hits = call("categories", query="велосипед")["subcategories"]
    assert hits and hits == sorted(hits, key=lambda s: -s["count"])
    with pytest.raises(ToolError, match="no category"):
        call("get_filters", category="no/such-category")


@live
def test_seller_ads():
    ad = call("search", category=FLATS, limit=1)["ads"][0]
    r = call("seller_ads", login=ad["seller"], limit=3)
    assert r["seller"]["login"] == ad["seller"] and r["total"] >= 1


@live
def test_price_stats_rent():
    p = call("price_stats", category=FLATS, options=[{"feature_id": 1, "option_id": RENT_MONTHLY},
                                                           {"feature_id": ROOMS, "option_id": TWO_ROOMS},
                                                           {"feature_id": REGION, "option_id": CHISINAU_MUN}])
    assert p["sample_size"] > 50 and 100 < p["median"] < 5000
