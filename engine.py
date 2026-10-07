import re
import requests
from collections import Counter

API_ENDPOINT = "https://api.dhaconnects.com/api/plots/public/dha-lahore-phase-8/listings/"


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
        elif "lakh" in val_str or "lac" in val_str or "lacs" in val_str:
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
    elif amount >= 100_000:
        return f"{amount / 100_000:.2f} Lac PKR"
    return f"{amount:,.0f} PKR"


def extract_field(item, possible_keys, default="N/A"):
    for key in possible_keys:
        val = item.get(key)
        if val is not None and str(val).strip() != "":
            return val
    return default


def query_dha_data_engine(block=None, size=None, min_price=None, max_price=None,
                          category=None, search_keyword=None, sort_order=None, limit=8):
    """Fetch listings, compute global stats, apply filters, return compact JSON."""
    try:
        response = requests.get(API_ENDPOINT, timeout=12)
        response.raise_for_status()
        raw_payload = response.json()

        raw_items = []
        if isinstance(raw_payload, list):
            raw_items = raw_payload
        elif isinstance(raw_payload, dict):
            raw_items = raw_payload.get("results", raw_payload.get("listings", raw_payload.get("data", [])))

        if not isinstance(raw_items, list) or len(raw_items) == 0:
            return {"total_listings_in_db": 0, "message": "No listings available in API database."}

        normalized_db = []
        block_counter, size_counter, category_counter = Counter(), Counter(), Counter()
        all_numeric_prices = []

        for idx, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                continue

            b_name = str(extract_field(raw, ["block", "phase_block", "block_name", "sector"], "Unspecified")).strip().upper()
            s_val = str(extract_field(raw, ["size", "marla_size", "area", "plot_size"], "Unspecified")).strip()
            cat_val = str(extract_field(raw, ["type", "category", "property_type", "purpose"], "Residential")).strip()
            title_val = str(extract_field(raw, ["title", "name", "plot_number", "plot_no", "description"], f"Plot #{idx+1}")).strip()

            p_raw = extract_field(raw, ["price", "demand", "amount", "total_price"], None)
            p_numeric = parse_price_to_numeric(p_raw)

            block_counter[b_name] += 1
            size_counter[s_val] += 1
            category_counter[cat_val] += 1
            if p_numeric:
                all_numeric_prices.append(p_numeric)

            normalized_db.append({
                "id": extract_field(raw, ["id", "pk", "listing_id"], idx + 1),
                "title": title_val,
                "block": b_name,
                "size": s_val,
                "category": cat_val,
                "price_pkr": p_numeric,
                "price_formatted": format_pkr(p_numeric) if p_numeric else str(p_raw),
            })

        filtered = []
        for item in normalized_db:
            if block and str(block).strip().upper() not in item["block"]:
                continue
            if size and str(size).strip().lower() not in item["size"].lower():
                continue
            if category and str(category).strip().lower() not in item["category"].lower():
                continue
            if min_price and (item["price_pkr"] is None or item["price_pkr"] < min_price):
                continue
            if max_price and (item["price_pkr"] is None or item["price_pkr"] > max_price):
                continue
            if search_keyword and str(search_keyword).strip().lower() not in item["title"].lower():
                continue
            filtered.append(item)

        if sort_order in ("highest_price", "lowest_price"):
            priced = [i for i in filtered if i["price_pkr"]]
            unpriced = [i for i in filtered if not i["price_pkr"]]
            priced.sort(key=lambda i: i["price_pkr"], reverse=(sort_order == "highest_price"))
            filtered = priced + unpriced

        fp = [i["price_pkr"] for i in filtered if i["price_pkr"]]

        try:
            limit = max(1, min(int(limit or 8), 1000))
        except (TypeError, ValueError):
            limit = 8

        stats = {
            "total_matches_for_query": len(filtered),
            "matched_price_min": format_pkr(min(fp)) if fp else "N/A",
            "matched_price_max": format_pkr(max(fp)) if fp else "N/A",
            "matched_price_avg": format_pkr(sum(fp) / len(fp)) if fp else "N/A",
        }
        overview = {
            "total_db_listings": len(normalized_db),
            "cheapest_listing_overall": format_pkr(min(all_numeric_prices)) if all_numeric_prices else "N/A",
            "most_expensive_overall": format_pkr(max(all_numeric_prices)) if all_numeric_prices else "N/A",
            "average_price_overall": format_pkr(sum(all_numeric_prices) / len(all_numeric_prices)) if all_numeric_prices else "N/A",
        }

        if limit <= 8:
            return {
                "global_overview": overview,
                "listings_per_block_breakdown": dict(block_counter),
                "listings_per_size_breakdown": dict(size_counter),
                "category_breakdown": dict(category_counter),
                "search_query_stats": stats,
                "top_matching_listings_sample": filtered[:limit],
            }

        # Full-list mode: compact one-line rows to save tokens
        rows = [f"{i['id']} | {i['title']} | {i['block']} | {i['size']} | {i['price_formatted']}" for i in filtered[:limit]]
        return {
            "global_overview": overview,
            "search_query_stats": stats,
            "listings_format": "id | title | block | size | price",
            "listings": rows,
            "listings_not_shown": max(0, len(filtered) - limit),
        }
    except Exception as e:
        return {"error": f"API engine execution failed: {str(e)}"}


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_dha_data_engine",
            "description": "Comprehensive engine for fetching DHA Phase 8 listings, statistical analytics, block totals, price ranges, and property filtering.",
            "parameters": {
                "type": "object",
                "properties": {
                    "block": {"type": ["string", "null"], "description": "Block name, e.g., 'A', 'B', 'W', 'Ivy Green', 'CCA-1'."},
                    "size": {"type": ["string", "null"], "description": "Plot size, e.g., '1 Kanal', '10 Marla', '5 Marla', '4 Marla'."},
                    "min_price": {"type": ["number", "null"], "description": "Minimum budget in PKR."},
                    "max_price": {"type": ["number", "null"], "description": "Maximum budget in PKR."},
                    "category": {"type": ["string", "null"], "description": "Type of property, e.g., 'Commercial', 'Residential'."},
                    "search_keyword": {"type": ["string", "null"], "description": "Specific search term like plot number, street name, or feature keyword."},
                    "limit": {"type": ["integer", "null"], "description": "How many listings to return. Default 8. Use 1000 when the user asks for all / every / the full list of listings."},
                    "sort_order": {"type": ["string", "null"], "enum": ["highest_price", "lowest_price", None], "description": "Use 'highest_price' for most expensive / top listings and 'lowest_price' for cheapest listings. The first items of top_matching_listings_sample are then the actual extremes."},
                },
                "required": [],
            },
        },
    }
]

AVAILABLE_TOOLS = {"query_dha_data_engine": query_dha_data_engine}
