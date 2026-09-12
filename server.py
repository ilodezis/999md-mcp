"""Read-only MCP server for 999.md, the Moldovan classifieds board.

Talks to the same public GraphQL endpoint the website uses (https://999.md/graphql):
no account, no API key, no cookies. Only read queries are sent.
"""

import asyncio
import io
import logging
import os
import re
import time
from typing import Annotated, Any, Literal

import httpx
import PIL.Image
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.utilities.types import Image
from pydantic import BaseModel, Field

GRAPHQL_URL = "https://999.md/graphql"
LANG = os.environ.get("SITE_LANG", "ru")  # "ru" or "ro": language of translated titles and options
SITE = f"https://999.md/{LANG}"
PHOTO_URL = "https://i.simpalsmedia.com/999.md/BoardImages/900x900/"  # despite the name: up to 1280 px, uncropped
PREVIEW_SIDE = 768  # long side of compact photos: ~550 tokens each vs ~1500 full, rooms and printed text stay readable
MIN_INTERVAL = 0.2  # seconds between request starts: at most 5 requests per second

# Feature ids that mean the same thing in every category.
F_OFFER, F_PRICE, F_MAP, F_REGION, F_CITY, F_SECTOR, F_STREET, F_HOUSE = 1, 2, 3, 7, 8, 9, 10, 11
F_TITLE, F_BODY, F_PHOTOS, F_VIDEOS, F_CONTACTS, F_UPLOADED_VIDEOS = 12, 13, 14, 15, 16, 2562
SEPARATE = {F_OFFER, F_PRICE, F_MAP, F_REGION, F_CITY, F_SECTOR, F_STREET, F_HOUSE,
            F_TITLE, F_BODY, F_PHOTOS, F_VIDEOS, F_CONTACTS, F_UPLOADED_VIDEOS}

UNITS = {"EUR": "EUR", "USD": "USD", "MDL": "MDL", "METER_SQUARE": "m²", "CENTIMETER": "cm",
         "METER": "m", "KILOMETER": "km", "ARE": "ar", "HECTARE": "ha", "KILOGRAM": "kg",
         "LITER": "l", "CENTIMETER_CUBE": "cm³", "HORSEPOWER": "hp", "MONTH": "months"}
PRICE_MODES = {"PM_FROM": "from", "PM_FROM_TO": "from-to", "PM_PIECEWORK": "piecework"}
SORTS = {"newest": "SORT_ADS_DATE_DESC", "oldest": "SORT_ADS_DATE_ASC",
         "price_asc": "SORT_ADS_PRICE_ASC", "price_desc": "SORT_ADS_PRICE_DESC",
         "relevance": "SORT_ADS_RELEVANCE"}
FILTER_KINDS = {"FILTER_TYPE_OPTIONS": "options", "FILTER_TYPE_COLOR": "options",
                "FILTER_TYPE_RANGE": "range", "FILTER_TYPE_EXISTS": "flag",
                "FILTER_TYPE_FEATURES_AND": "flag", "FILTER_TYPE_FEATURES_OR": "flag"}

DATE = 'format: "2006-01-02 15:04", timezone: "Europe/Chisinau"'
LIST_AD = f"""
fragment ListAd on Advert {{
  id title
  reseted(input: {{{DATE}}})
  offer: feature(id: {F_OFFER}) {{ value }}
  price: feature(id: {F_PRICE}) {{ value }}
  region: feature(id: {F_REGION}) {{ value }}
  city: feature(id: {F_CITY}) {{ value }}
  sector: feature(id: {F_SECTOR}) {{ value }}
  photos: feature(id: {F_PHOTOS}) {{ value }}
  subCategory {{ id title {{ translated }} }}
  owner {{ login }}
}}"""
SEARCH_Q = "query($input: Ads_SearchInput) { searchAds(input: $input) { count ads { ...ListAd } } }" + LIST_AD
SELLER_Q = ("query($input: Ads_ProfileInput, $login: String!) {"
            " profileAds(input: $input) { count ads { ...ListAd } }"
            ' getAccountByLogin(input: $login) { id login createdDate(input: {format: "2006-01-02"})'
            " verification { isVerified } business { plan } } }" + LIST_AD)
AD_Q = f"""query($input: AdvertInput!) {{ advert(input: $input) {{
  id title state isExpired
  posted(input: {{{DATE}}}) reseted(input: {{{DATE}}}) expire(input: {{{DATE}}})
  subCategory {{ id url title {{ translated }} }}
  owner {{ id login createdDate(input: {{format: "2006-01-02"}}) verification {{ isVerified }} business {{ plan }} }}
  groups(placement: VIEW_ONE_DEFAULT) {{ controls {{ title feature {{ id type value }} }} }}
}} }}"""
PHOTOS_Q = f"query($input: AdvertInput!) {{ advert(input: $input) {{ id photos: feature(id: {F_PHOTOS}) {{ value }} }} }}"
CATEGORY_Q = """query($input: GetCategoryRequestInput!) { category(input: $input) {
  id url type title { translated }
  parent { id type parent { id type parent { id type } } } } }"""
FILTERS_Q = """query($input: GetCategoryRequestInput!) { category(input: $input) {
  filters { type title { translated } units
    features { id type title { translated } parentId childId
      options { id title { translated } } } } } }"""
OPTIONS_Q = """query($input: FeatureRequestInput!) { feature(input: $input) {
  id title { translated } childId options { id title { translated } hasChildren } } }"""
COUNTERS_Q = """query($input: Ads_CategoriesWithCountersInput) { categoriesWithCounters(input: $input) {
  categoriesWithCounters { id title url count subcategoriesWithCounters { id title url count } } } }"""
PRICE_Q = """query($input: Ads_PricePredictionInput!) { pricePrediction(input: $input) {
  medianPrice averagePrice minPrice maxPrice count coincidence currency message { translated } } }"""

mcp = FastMCP("999md", instructions=(
    "999.md is the main classifieds board of Moldova (flats, cars, phones, jobs, services). "
    "Typical flow: categories to find a subcategory -> get_filters for its filter ids "
    "-> search -> get_ad for full details and the seller's phone. "
    "The photos tool shows the pictures themselves but is expensive in context: use it only for a few "
    "shortlisted ads when their look matters, never for a whole search page."))
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per request otherwise

_next_start = 0.0


async def gql(query: str, variables: dict) -> dict:
    """POST one GraphQL query; spaces request starts MIN_INTERVAL apart even under concurrency."""
    global _next_start
    now = time.monotonic()
    wait, _next_start = _next_start - now, max(now, _next_start) + MIN_INTERVAL
    if wait > 0:
        await asyncio.sleep(wait)
    try:
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "Mozilla/5.0", "lang": LANG}) as c:
            r = await c.post(GRAPHQL_URL, json={"query": query, "variables": variables})
    except httpx.HTTPError as e:
        raise ToolError(f"999.md is unreachable ({type(e).__name__}); try again in a minute") from e
    if r.status_code != 200:
        raise ToolError(f"999.md answered HTTP {r.status_code}; try again later")
    body = r.json()
    if body.get("errors"):
        raise ToolError("999.md rejected the query: " + "; ".join(e.get("message", "?") for e in body["errors"]))
    return body["data"]


# ---------- parsing and formatting (pure) ----------

def parse_ad_id(ad: str) -> str:
    """Accept an ad id or any 999.md ad link."""
    m = re.search(r"\d{5,}", str(ad))
    if not m:
        raise ToolError(f"'{ad}' is not a 999.md ad id or link (expected e.g. 105361034 or https://999.md/ru/105361034)")
    return m.group()


def category_input(category: str | int) -> dict:
    """Accept a category id, a path like 'real-estate/apartments-and-rooms', or a 999.md list link."""
    s = str(category).strip()
    m = re.search(r"999\.md/(?:ru|ro|en)/list/([^?#]+)", s)
    s = (m.group(1) if m else s).strip("/")
    return {"id": int(s)} if s.isdigit() else {"url": s}


def unit(u: str | None) -> str:
    u = (u or "").removeprefix("UNIT_")
    return UNITS.get(u, u.lower())


def fmt_price(v: dict | None) -> str | None:
    if not v or not v.get("value"):
        return None
    s = f"{v['value']} {unit(v.get('unit'))}"
    if v.get("mode") in PRICE_MODES:
        s = f"{PRICE_MODES[v['mode']]} {s}"
    return s + (", negotiable" if v.get("bargain") else "")


def fmt_value(v: Any) -> Any:
    """Turn a FeatureValue.value into something short and readable."""
    if isinstance(v, dict):
        if "translated" in v:
            return v["translated"]
        if "unit" in v and "value" in v:
            return f"{v['value']} {unit(v['unit'])}"
    return v


def tr(feature: dict | None) -> str | None:
    return fmt_value(feature["value"]) if feature and feature.get("value") is not None else None


def location(ad: dict) -> str | None:
    parts = [tr(ad.get("sector")), tr(ad.get("city"))]
    parts = [p for p in parts if p] or [tr(ad.get("region"))]
    return ", ".join(p for p in parts if p) or None


def list_ad(ad: dict) -> dict:
    photos = (ad.get("photos") or {}).get("value") or []
    return {
        "id": ad["id"],
        "title": ad["title"],
        "price": fmt_price((ad.get("price") or {}).get("value")),
        "offer": tr(ad.get("offer")),
        "location": location(ad),
        "date": ad.get("reseted"),
        "category": ((ad.get("subCategory") or {}).get("title") or {}).get("translated"),
        "seller": (ad.get("owner") or {}).get("login"),
        "photo": PHOTO_URL + photos[0] if photos else None,
        "url": f"{SITE}/{ad['id']}",
    }


def page(items: list, total: int, offset: int) -> dict:
    return {"total": total, "offset": offset, "count": len(items),
            "has_more": offset + len(items) < total,
            "next_offset": offset + len(items) if offset + len(items) < total else None,
            "ads": items}


class Filter(BaseModel):
    """One filter condition. Several filters are combined with AND."""
    feature_id: int = Field(description="Feature id from get_filters (e.g. 241 = number of rooms for flats)")
    option_ids: list[int] | None = Field(None, description="For 'options' filters: match any of these option ids")
    min: float | None = Field(None, description="For 'range' filters: lower bound")
    max: float | None = Field(None, description="For 'range' filters: upper bound")
    unit: str | None = Field(None, description="Unit for 'range' filters: EUR, USD, MDL, METER_SQUARE, ...")


def build_filters(filters: list[Filter], price_min: float | None = None, price_max: float | None = None,
                  currency: str = "EUR") -> list[dict]:
    """One group per feature: the API ORs features inside a group and ANDs the groups.
    filterId is ignored by the API, so 0 is sent."""
    filters = list(filters)
    if price_min is not None or price_max is not None:
        filters.append(Filter(feature_id=F_PRICE, min=price_min, max=price_max, unit=currency))
    out = []
    for f in filters:
        feat: dict[str, Any] = {"featureId": f.feature_id}
        if f.option_ids:
            feat["optionIds"] = f.option_ids
        if f.min is not None or f.max is not None:
            feat["range"] = {k: v for k, v in (("min", f.min), ("max", f.max)) if v is not None}
        if f.unit:
            feat["unit"] = "UNIT_" + f.unit.upper().removeprefix("UNIT_")
        out.append({"filterId": 0, "features": [feat]})
    return out


def ad_details(a: dict) -> dict:
    by_id, specs, amenities = {}, {}, []
    for control in (c for g in a.get("groups") or [] for c in g["controls"]):
        f = control["feature"]
        by_id[f["id"]] = f
        if f["id"] in SEPARATE or f["value"] in (None, "", "-", False):
            continue
        if f["value"] is True:
            amenities.append(control["title"])
        else:
            specs[control["title"]] = fmt_value(f["value"])
    val = lambda fid: (by_id.get(fid) or {}).get("value")  # noqa: E731
    point = val(F_MAP) or {}
    v = val(F_VIDEOS)
    videos = (v.get("videos") or []) if isinstance(v, dict) else (v or [])
    owner = a.get("owner") or {}
    return {
        "id": a["id"],
        "url": f"{SITE}/{a['id']}",
        "title": a["title"],
        "state": a["state"].removeprefix("AD_STATE_").lower() + (" (expired)" if a.get("isExpired") else ""),
        "offer": tr(by_id.get(F_OFFER)),
        "price": fmt_price(val(F_PRICE)),
        "posted": a.get("posted"), "republished": a.get("reseted"), "expires": a.get("expire"),
        "category": {"id": a["subCategory"]["id"], "title": a["subCategory"]["title"]["translated"],
                     "url": a["subCategory"]["url"]} if a.get("subCategory") else None,
        "location": {k: v for k, v in {
            "region": tr(by_id.get(F_REGION)), "city": tr(by_id.get(F_CITY)), "sector": tr(by_id.get(F_SECTOR)),
            "street": val(F_STREET), "house": val(F_HOUSE) if val(F_HOUSE) != "-" else None,
            "lat": point.get("lat"), "lon": point.get("lon")}.items() if v},
        "description": tr(by_id.get(F_BODY)),
        "characteristics": specs,
        "amenities": amenities,
        "phones": ["+" + p for p in (val(F_CONTACTS) or {}).get("phone_numbers") or []],
        "seller": {"login": owner.get("login"), "id": owner.get("id"), "since": owner.get("createdDate"),
                   "verified": (owner.get("verification") or {}).get("isVerified"),
                   "business": (owner.get("business") or {}).get("plan")} if owner else None,
        "photos": [PHOTO_URL + p for p in val(F_PHOTOS) or []],
        "videos": [v.get("video_url", v) if isinstance(v, dict) else v for v in videos + (val(F_UPLOADED_VIDEOS) or [])],
    }


def shrink(data: bytes, side: int = PREVIEW_SIDE) -> bytes:
    """Fit a photo into side x side keeping its proportions (the CDN's own small sizes crop to 4:3)."""
    img = PIL.Image.open(io.BytesIO(data))
    img.thumbnail((side, side))
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=80)
    return out.getvalue()


def flatten_filters(filters: list[dict], max_options: int) -> list[dict]:
    out = []
    for f in filters:
        for ft in f.get("features") or []:
            opts = ft.get("options") or []
            item: dict[str, Any] = {
                "feature_id": ft["id"],
                "title": (ft.get("title") or f.get("title") or {}).get("translated"),
                "kind": FILTER_KINDS.get(f["type"], f["type"]),
            }
            if f.get("units"):  # same spelling search accepts in Filter.unit
                item["units"] = [u.removeprefix("UNIT_") for u in f["units"]]
            if opts:
                item["options"] = [{"id": o["id"], "title": o["title"]["translated"]} for o in opts[:max_options]]
                if len(opts) > max_options:
                    item["options_total"] = len(opts)
            if ft.get("parentId"):
                item["depends_on_feature"] = ft["parentId"]
                if not opts:
                    item["note"] = f"options depend on feature {ft['parentId']}: use get_options"
            out.append(item)
    return out


# ---------- category helpers ----------

async def resolve_category(category: str | int) -> dict:
    c = (await gql(CATEGORY_Q, {"input": category_input(category)}))["category"]
    if not c or not c["id"]:
        raise ToolError(f"999.md has no category '{category}'; use categories to find a valid id or url")
    top, p = (c["id"] if c["type"] == "CATEGORY" else None), c.get("parent")
    while p and top is None:
        top, p = (p["id"] if p["type"] == "CATEGORY" else None), p.get("parent")
    return {"id": c["id"], "url": c["url"], "type": c["type"], "title": c["title"]["translated"], "top_id": top}


# ---------- tools ----------

@mcp.tool(annotations=READ_ONLY)
async def search(
    query: Annotated[str | None, Field(description="Free-text search, e.g. 'iphone 15 pro' or 'велосипед'")] = None,
    category: Annotated[str | int | None, Field(description="Category or subcategory: id (1404), path "
                        "('real-estate/apartments-and-rooms') or a 999.md list link")] = None,
    filters: Annotated[list[Filter] | None, Field(description="Filter conditions (ANDed); ids come from get_filters")] = None,
    price_min: Annotated[float | None, Field(description="Shortcut for a price filter")] = None,
    price_max: Annotated[float | None, Field(description="Shortcut for a price filter")] = None,
    currency: Annotated[Literal["EUR", "USD", "MDL"], Field(description="Currency of price_min/price_max; "
                        "ads in other currencies are converted by 999.md")] = "EUR",
    sort: Annotated[Literal["newest", "oldest", "price_asc", "price_desc", "relevance"] | None,
                    Field(description="Default: 999.md's own order (newest republished first)")] = None,
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """Search 999.md ads by text, category and filters. Returns a page of short ad cards
    (price, location, date, seller login, first photo, link) plus total count and paging info."""
    if not (query or category or filters):
        raise ToolError("Give at least a query, a category or filters")
    inp: dict[str, Any] = {"pagination": {"skip": offset, "limit": limit}}
    if query:
        inp["query"] = query
    if category is not None:
        c = await resolve_category(category)
        inp["categoryId" if c["type"] == "CATEGORY" else "subCategoryId"] = c["id"]
    if flt := build_filters(filters or [], price_min, price_max, currency):
        inp["filters"] = flt
    if sort:
        inp["sort"] = SORTS[sort]
    r = (await gql(SEARCH_Q, {"input": inp}))["searchAds"]
    return page([list_ad(a) for a in r["ads"]], r["count"], offset)


@mcp.tool(annotations=READ_ONLY)
async def get_ad(
    ad: Annotated[str, Field(description="Ad id (105361034) or a 999.md ad link")],
) -> dict[str, Any]:
    """Full ad: description, all characteristics, amenities, exact location with coordinates,
    seller's phone numbers and profile, photo and video links, dates.
    Photo links are text only; to actually look at the photos use the photos tool."""
    a = (await gql(AD_Q, {"input": {"id": parse_ad_id(ad)}}))["advert"]
    if not a:
        raise ToolError(f"Ad {ad} not found (deleted or wrong id)")
    return ad_details(a)


@mcp.tool(annotations=READ_ONLY)
async def photos(
    ad: Annotated[str, Field(description="Ad id (105361034) or a 999.md ad link")],
    offset: Annotated[int, Field(ge=0, description="Skip this many photos, to page through a long gallery")] = 0,
    limit: Annotated[int, Field(ge=1, le=10, description="Photos to return; keep the default unless the user wants more")] = 4,
    full_resolution: Annotated[bool, Field(description="Original size (up to 1280 px, ~1500 tokens per photo). "
                               "Only when the user explicitly asks for full quality or a tiny detail is unreadable")] = False,
) -> list[str | Image]:
    """EXPENSIVE: returns the ad's photos as images you can see, ~550 tokens each (compact, long side 768 px)
    and ~1500 each with full_resolution. They stay in the conversation context for the rest of the chat.
    Call only when the look matters and the text of get_ad cannot answer: renovation and condition
    of a flat, body damage on a car, what exactly is being sold. Pick 1-3 finalists first;
    never call it for every ad of a search page. Page with offset instead of raising limit."""
    a = (await gql(PHOTOS_Q, {"input": {"id": parse_ad_id(ad)}}))["advert"]
    if not a:
        raise ToolError(f"Ad {ad} not found (deleted or wrong id)")
    names = (a.get("photos") or {}).get("value") or []
    batch = names[offset:offset + limit]
    if not batch:
        return [f"Ad {a['id']} has {len(names)} photos, nothing at offset {offset}"]
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as c:
        responses = await asyncio.gather(*(c.get(PHOTO_URL + n) for n in batch), return_exceptions=True)
    end = offset + len(batch)
    out: list[str | Image] = [f"Ad {a['id']}: photos {offset + 1}-{end} of {len(names)}"
                              + (f", next offset {end}" if end < len(names) else "")]
    for i, r in enumerate(responses, offset + 1):
        if isinstance(r, httpx.Response) and r.status_code == 200:
            out.append(Image(data=r.content if full_resolution else shrink(r.content), format="jpeg"))
        else:
            out.append(f"photo {i} failed to load")
    return out


@mcp.tool(annotations=READ_ONLY)
async def get_filters(
    category: Annotated[str | int, Field(description="Subcategory id, path or 999.md list link")],
    max_options: Annotated[int, Field(ge=1, le=500, description="Cut long option lists")] = 60,
) -> dict[str, Any]:
    """Filters available in a subcategory, with the feature ids and option ids that search accepts.
    kind='options' -> pass option_ids; 'range' -> min/max (+unit); 'flag' -> feature_id alone.
    Features with depends_on_feature (city after region, model after brand) need get_options."""
    c = await resolve_category(category)
    f = (await gql(FILTERS_Q, {"input": {"id": c["id"]}}))["category"]["filters"]
    return {"category": {k: c[k] for k in ("id", "url", "title")}, "filters": flatten_filters(f, max_options)}


@mcp.tool(annotations=READ_ONLY)
async def get_options(
    feature_id: Annotated[int, Field(description="Feature whose options you need (e.g. 8 = city, 9 = sector, 590 = phone model)")],
    parent_option_id: Annotated[int | None, Field(description="Selected option of the parent feature "
                                "(e.g. region 12900 = Chișinău mun. for cities, city 13859 = Chișinău for sectors)")] = None,
    query: Annotated[str | None, Field(description="Only options whose title contains this text")] = None,
) -> dict[str, Any]:
    """Options of one feature, typically a dependent one: cities of a region, sectors of a city,
    models of a brand. Use the returned ids in search filters."""
    inp: dict[str, Any] = {"id": feature_id}
    if parent_option_id is not None:
        inp["parentId"] = parent_option_id
    if query:
        inp["query"] = query
    f = (await gql(OPTIONS_Q, {"input": inp}))["feature"]
    return {"feature_id": f["id"], "title": (f.get("title") or {}).get("translated"),
            "child_feature": f.get("childId") or None,
            "options": [{"id": o["id"], "title": o["title"]["translated"], **({"has_children": True} if o.get("hasChildren") else {})}
                        for o in f["options"]]}


@mcp.tool(annotations=READ_ONLY)
async def categories(
    category: Annotated[str | int | None, Field(description="Top-level category id or path to list its subcategories")] = None,
    query: Annotated[str | None, Field(description="Show only subcategories that have ads matching this text")] = None,
) -> dict[str, Any]:
    """999.md category tree with live ad counts. No arguments: top-level categories.
    With category: its subcategories. With query: where ads matching the text live, biggest first."""
    tree = (await gql(COUNTERS_Q, {"input": {"query": query} if query else {}}))["categoriesWithCounters"]["categoriesWithCounters"]
    row = lambda c, parent=None: {"id": c["id"], "title": c["title"], "url": c["url"], "count": c["count"],  # noqa: E731
                                  **({"parent": parent} if parent else {})}
    if query and category is None:
        subs = [row(s, c["title"]) for c in tree for s in c["subcategoriesWithCounters"] or [] if s["count"]]
        return {"query": query, "subcategories": sorted(subs, key=lambda s: -s["count"])[:40]}
    if category is None:
        return {"categories": [row(c) for c in tree]}
    want = category_input(category)
    for c in tree:
        if c["id"] == want.get("id") or c["url"] == want.get("url"):
            return {"category": row(c), "subcategories": [row(s) for s in c["subcategoriesWithCounters"] or []]}
    raise ToolError(f"'{category}' is not a top-level category; call categories() without arguments to see them")


@mcp.tool(annotations=READ_ONLY)
async def seller_ads(
    login: Annotated[str, Field(description="Seller login, as in get_ad seller.login")],
    query: Annotated[str | None, Field(description="Free-text search inside this seller's ads")] = None,
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """A seller's profile (registered since, verified, business plan) and their active ads.
    Handy to tell a private person from an agency or reseller."""
    inp: dict[str, Any] = {"login": login, "pagination": {"skip": offset, "limit": limit}}
    if query:
        inp["query"] = query
    r = await gql(SELLER_Q, {"input": inp, "login": login})
    acc = r.get("getAccountByLogin")
    if not acc:
        raise ToolError(f"No 999.md seller with login '{login}'")
    ads = r["profileAds"]
    return {"seller": {"login": acc["login"], "id": acc["id"], "since": acc["createdDate"],
                       "verified": (acc.get("verification") or {}).get("isVerified"),
                       "business": (acc.get("business") or {}).get("plan")},
            **page([list_ad(a) for a in ads["ads"]], ads["count"], offset)}


class OptionPick(BaseModel):
    feature_id: int = Field(description="Feature id from get_filters")
    option_id: int = Field(description="One option id of that feature")


@mcp.tool(annotations=READ_ONLY)
async def price_stats(
    category: Annotated[str | int, Field(description="Subcategory id, path or 999.md list link")],
    options: Annotated[list[OptionPick] | None, Field(description="Narrow the sample, e.g. offer type 'rent monthly', "
                       "2 rooms, region Chișinău, or brand + model")] = None,
    currency: Literal["EUR", "USD", "MDL"] = "EUR",
) -> dict[str, Any]:
    """999.md's own price estimate over matching ads: median, average, min, max and sample size.
    Prefer the median: min/max and the average are skewed by mistyped prices."""
    c = await resolve_category(category)
    if c["type"] == "CATEGORY" or not c["top_id"]:
        raise ToolError("Price stats need a subcategory (e.g. 1404 for flats), not a top-level category")
    p = (await gql(PRICE_Q, {"input": {
        "category": c["top_id"], "subCategory": c["id"], "currency": f"CURRENCY_ADS_{currency}",
        "featureIds": [{"featureId": o.feature_id, "optionId": o.option_id} for o in options or []]}}))["pricePrediction"]
    return {"category": c["title"], "currency": currency, "median": p["medianPrice"],
            "average": round(p["averagePrice"], 2), "min": p["minPrice"], "max": p["maxPrice"],
            "sample_size": p["count"], "confidence": p["coincidence"].removeprefix("COINCIDENCE_LEVEL_").lower(),
            "note": p["message"]["translated"]}


def main() -> None:
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
