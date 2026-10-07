import re
import time
from collections import Counter

import requests

ENGINE_VERSION = "v5-cached"
API_ENDPOINT = "https://api.dhaconnects.com/api/plots/public/dha-lahore-phase-8/listings/"
CACHE_SECONDS = 120        # reuse fetched listings for 2 minutes
WIDE_TABLE_MAX_ROWS = 15   # above this, full lists use the compact 3-column table

_cache = {"at": 0.0, "db": None}


# --------------------------------------------------------------------------- helpers

def parse_price_to_numeric(val):
    """Converts price strings or numbers into raw numeric float values in PKR."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)

    val_str = str(val).lower().replace(",", "").strip()
    try:
        if "crore" in val_str or "cr" in val_str:
            return float(re.findall(r"[\d.]+", val_str)[0]) * 10_000_000
        if "lakh" in val_str or "lac" in val_str:
            return float(re.findall(r"[\d.]+", val_str)[0]) * 100_000
        nums = re.findall(r"[\d.]+", val_str)
        if nums:
            return float(nums[0])
    except Exception:
        pass
    return None


def format_pkr(amount):
    """Formats numeric amounts into Pakistani Rupee notation (Crore / Lac)."""
    if not isinstance(amount, (int, float)) or amount <= 0:
        return "N/A"
    if amount >= 10_000_000:
        return f"{amount / 10_000_000:.2f} Crore PKR"
    if amount >= 100_000:
        return f"{amount / 100_000:.2f} Lac PKR"
    return f"{amount:,.0f} PKR"


def extract_field(item, possible_keys, default="N/A"):
    for key in possible_keys:
        val = item.get(key)
        if val is not None and str(val).strip() != "":
            return val
    return default


def norm(x):
    """Lowercase and remove all spaces/dashes so '1 Kanal' matches '1Kanal'."""
    return re.sub(r"[\s\-_]+", "", str(x).lower())


def clean_block(val):
    """'Block X' / 'x' / 'CCA 3' / 'CCA3' / 'CCA-3'  ->  'X' / 'X' / 'CCA-3'"""
    b = str(val).strip().upper()
    b = re.sub(r"^BLOCK[\s\-]*", "", b).strip()
    b = re.sub(r"^CCA[\s\-]*(\d)", r"CCA-\1", b)
    return b or "Unspecified"


def build_size(raw):
    """Combine area + areaUnit into e.g. '1 Kanal' / '23.37 Marla'."""
    area = raw.get("area") or raw.get("size") or raw.get("plot_size")
    unit = raw.get("areaUnit") or raw.get("area_unit") or raw.get("unit") or ""
    if area is None or str(area).strip() == "":
        return "Unspecified"
    try:
        area = f"{float(str(area).replace(',', '')):g}"
    except ValueError:
        area = str(area).strip()
    return f"{area} {str(unit).strip().title()}".strip()


def block_match(query, item_block):
    q = re.sub(r"^block", "", norm(query))
    b = norm(item_block)
    if b == q:
        return True
    # "CCA" with no number means every CCA sector
    return q == "cca" and b.startswith("cca")


def size_match(query, item_size):
    """Match sizes without letting '1kanal' match '21kanal' or '5marla' match '15marla'."""
    q, v = norm(query), norm(item_size)
    return re.search(r"(?<![\d.])" + re.escape(q), v) is not None


# --------------------------------------------------------------------------- data loading

def _normalize(raw_items):
    db = []
    for idx, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            continue
        p_raw = extract_field(raw, ["price", "demand", "amount", "total_price"], None)
        p_numeric = parse_price_to_numeric(p_raw)
        db.append({
            "id": extract_field(raw, ["id", "pk", "listing_id"], idx + 1),
            "title": str(extract_field(raw, ["title", "name", "description"], f"Plot #{idx + 1}")).strip(),
            "plot": str(extract_field(raw, ["plotnum", "plot_number", "plot_no", "plot"], "")).strip(),
            "block": clean_block(extract_field(raw, ["sector", "block", "phase_block", "block_name"], "Unspecified")),
            "size": build_size(raw),
            "category": str(extract_field(raw, ["landUse", "land_use", "property_type", "type"], "Residential")).strip().title(),
            "type": str(extract_field(raw, ["subType", "sub_type", "type"], "Plot")).strip(),
            "price_pkr": p_numeric,
            "price": format_pkr(p_numeric) if p_numeric else str(p_raw),
        })
    return db


def load_listings():
    """Fetch and normalise listings, cached for CACHE_SECONDS."""
    now = time.monotonic()
    if _cache["db"] is not None and now - _cache["at"] < CACHE_SECONDS:
        return _cache["db"]
    try:
        response = requests.get(API_ENDPOINT, timeout=12)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        if _cache["db"] is not None:
            return _cache["db"]  # serve slightly older data rather than fail
        raise

    items = []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("results") or payload.get("listings") or payload.get("data") or []

    db = _normalize(items if isinstance(items, list) else [])
    _cache.update(at=now, db=db)
    return db


# --------------------------------------------------------------------------- search

def _matches(item, block, size, min_p, max_p, category, keyword):
    if block and not block_match(block, item["block"]):
        return False
    if size and not size_match(size, item["size"]):
        return False
    if category:
        c = norm(category)
        if c not in norm(item["category"]) and c not in norm(item["type"]):
            return False
    price = item["price_pkr"]
    if min_p and (price is None or price < min_p):
        return False
    if max_p and (price is None or price > max_p):
        return False
    if keyword and norm(keyword) not in norm(item["title"] + item["plot"] + item["type"]):
        return False
    return True


def _price_stats(prices):
    if not prices:
        return "N/A", "N/A", "N/A"
    return format_pkr(min(prices)), format_pkr(max(prices)), format_pkr(sum(prices) / len(prices))


def _public(item):
    """Listing fields the model needs (no raw numeric price)."""
    return {k: item[k] for k in ("id", "title", "plot", "block", "size", "category", "price")}


def query_dha_data_engine(block=None, size=None, min_price=None, max_price=None,
                          category=None, search_keyword=None, sort_order=None,
                          limit=8, count_only=False, breakdown=False):
    """Fetch listings, apply filters, return compact JSON for the model."""
    try:
        db = load_listings()
    except Exception as e:
        return {"error": f"Listings could not be loaded: {e}"}
    if not db:
        return {"total_db_listings": 0, "message": "No listings are available right now."}

    try:
        min_p = parse_price_to_numeric(min_price)
        max_p = parse_price_to_numeric(max_price)
        filtered = [i for i in db if _matches(i, block, size, min_p, max_p, category, search_keyword)]

        if sort_order in ("highest_price", "lowest_price"):
            priced = [i for i in filtered if i["price_pkr"]]
            unpriced = [i for i in filtered if not i["price_pkr"]]
            priced.sort(key=lambda i: i["price_pkr"], reverse=(sort_order == "highest_price"))
            filtered = priced + unpriced

        try:
            limit = max(1, min(int(limit or 8), 1000))
        except (TypeError, ValueError):
            limit = 8

        m_min, m_max, m_avg = _price_stats([i["price_pkr"] for i in filtered if i["price_pkr"]])
        stats = {
            "total_matches_for_query": len(filtered),
            "matched_price_min": m_min,
            "matched_price_max": m_max,
            "matched_price_avg": m_avg,
        }
        if count_only:
            return {"total_db_listings": len(db), "search_query_stats": stats}

        g_min, g_max, g_avg = _price_stats([i["price_pkr"] for i in db if i["price_pkr"]])
        overview = {
            "total_db_listings": len(db),
            "cheapest_listing_overall": g_min,
            "most_expensive_overall": g_max,
            "average_price_overall": g_avg,
        }

        if limit <= 8:
            out = {"global_overview": overview, "search_query_stats": stats,
                   "top_matching_listings_sample": [_public(i) for i in filtered[:limit]]}
            if breakdown:
                # Counted over the matching listings, so filters are respected
                out["listings_per_block_breakdown"] = dict(Counter(i["block"] for i in filtered))
                out["listings_per_size_breakdown"] = dict(Counter(i["size"] for i in filtered))
                out["category_breakdown"] = dict(Counter(i["category"] for i in filtered))
            return out

        # Full-list mode: the server renders the table itself (see render_full_list)
        return {
            "full_list": True,
            "global_overview": overview,
            "search_query_stats": stats,
            "listings": [_public(i) for i in filtered[:limit]],
            "listings_not_shown": max(0, len(filtered) - limit),
        }
    except Exception as e:
        return {"error": f"Listing search failed: {e}"}


# --------------------------------------------------------------------------- full-list rendering

def _cell(x):
    return str(x).replace("|", "/").replace("\n", " ").strip() or "-"


def render_full_list(result):
    """Build the full Markdown table in Python so long lists never hit the model's token cap."""
    rows = result.get("listings", [])
    total = result.get("search_query_stats", {}).get("total_matches_for_query", len(rows))

    if not rows:
        overall = result.get("global_overview", {})
        n = overall.get("total_db_listings")
        tail = f" There are {n} listings on the market overall." if n else ""
        return ("No listings currently match those exact criteria." + tail +
                " Try a wider budget, a different size, or a nearby block.")

    if total == len(rows):
        intro = f"Here are all {total} currently available listings that match:"
    else:
        intro = f"Here are {len(rows)} of the {total} currently available listings that match:"
    lines = [intro, ""]

    if len(rows) <= WIDE_TABLE_MAX_ROWS:
        lines += ["| ID | Title / Plot Details | Block | Size | Price (PKR) |",
                  "|---|---|---|---|---|"]
        for r in rows:
            details = r["title"] + (f" (Plot {r['plot']})" if r["plot"] else "")
            lines.append(f"| {_cell(r['id'])} | {_cell(details)} | {_cell(r['block'])} "
                         f"| {_cell(r['size'])} | {_cell(r['price'])} |")
    else:
        lines += ["| ID | Plot | Price (PKR) |", "|---|---|---|"]
        for r in rows:
            plot = f"{r['size']}, Block {r['block']}" + (f", Plot {r['plot']}" if r["plot"] else "")
            lines.append(f"| {_cell(r['id'])} | {_cell(plot)} | {_cell(r['price'])} |")

    return "\n".join(lines)


# --------------------------------------------------------------------------- tool schema

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_dha_data_engine",
            "description": "Search live DHA Lahore Phase 8 listings: counts, price stats, filtered plots, breakdowns.",
            "parameters": {
                "type": "object",
                "properties": {
                    "block": {"type": ["string", "null"], "description": "Block name, e.g. 'A', 'W', 'Ivy Green', 'CCA-1'."},
                    "size": {"type": ["string", "null"], "description": "Plot size, e.g. '1 Kanal', '10 Marla', '5 Marla'."},
                    "min_price": {"type": ["number", "null"], "description": "Minimum budget in PKR."},
                    "max_price": {"type": ["number", "null"], "description": "Maximum budget in PKR."},
                    "category": {"type": ["string", "null"], "description": "'Commercial' or 'Residential'."},
                    "search_keyword": {"type": ["string", "null"], "description": "Plot number or feature, e.g. 'corner', 'park facing'."},
                    "limit": {"type": ["integer", "null"], "description": "Listings to return. Default 8. Use 1000 when the user asks for all / every / the full list."},
                    "count_only": {"type": ["boolean", "null"], "description": "True when the user only asks how many. Returns just the numbers."},
                    "breakdown": {"type": ["boolean", "null"], "description": "True only when the user asks for a breakdown per block, size or category."},
                    "sort_order": {"type": ["string", "null"], "enum": ["highest_price", "lowest_price"], "description": "'highest_price' for most expensive, 'lowest_price' for cheapest."},
                },
                "required": [],
            },
        },
    }
]

AVAILABLE_TOOLS = {"query_dha_data_engine": query_dha_data_engine}
