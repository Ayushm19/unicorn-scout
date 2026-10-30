"""Load and clean the Tracxn unicorn CSV into plain dicts ready for the database.

Known quirks of the source file (see README "Data quality"):
- Encoded as Windows-1252, not UTF-8; contains non-breaking spaces.
- The rupee sign was lost upstream and appears as "?" (e.g. "?569Cr").
- `location` values carry trailing whitespace.
- `top_investors_1/2` sometimes hold sector names instead of investors (shifted columns).
- `total_funding_till_date` / `valuation` sometimes hold employee counts (same shift); dropped.
- 56 fine-grained `primary_sector` values and no "FinTech"-style grouping, so we add one.
"""
import csv
import re
import unicodedata
from pathlib import Path

SECTOR_GROUPS = {
    "FinTech": ["Alternative Lending", "Banking Tech", "Consumer and SME Loans", "Cryptocurrencies",
                "Internet First Insurance Platforms", "Investment Tech", "Payments"],
    "E-Commerce": ["Auto E-Commerce & Content", "B2B E-Commerce", "Consumer Electronics E-Commerce",
                   "E-Commerce Enablers", "Horizontal E-Commerce", "Social Commerce", "Beauty Tech", "Fashion Tech"],
    "Consumer Brands": ["Beauty & Personal Care Products", "Brands Tech", "Mom & Baby Care"],
    "Food & Grocery": ["Food Tech", "Online Grocery"],
    "Logistics": ["Logistics Tech"],
    "Mobility & Travel": ["Electric Vehicles", "Road Transport Tech", "Online Travel"],
    "EdTech": ["Continued Learning", "K-12 EdTech", "Test Preparation Tech"],
    "HealthTech": ["Healthcare Booking Platforms", "Healthcare IT", "Healthcare Payer Tech",
                   "Infectious Diseases", "Employee Health IT"],
    "Enterprise SaaS": ["API Management", "Customer Service Software", "Cybersecurity", "Finance & Accounting Tech",
                        "HRTech", "Legal Tech", "SME Business Management Software", "Sales Force Automation",
                        "Software Testing Tools"],
    "AI & Analytics": ["Generative AI", "Business Intelligence", "Chatbots", "Speech and Voice Recognition"],
    "Media, Gaming & AdTech": ["AdTech and Marketing", "Mobile Gaming", "Mobile Utility Tools", "PC & Console Gaming",
                               "Sports Tech", "Tech for Traditional Advertising", "Vernacular Platforms"],
    "Real Estate & Home": ["Construction Tech", "Home Improvements", "Residential Real Estate Tech", "Local Services"],
}
_SECTOR_TO_GROUP = {s: g for g, sectors in SECTOR_GROUPS.items() for s in sectors}

# User spellings -> dataset spellings. Applied to filters the LLM extracts.
CITY_ALIASES = {
    "bangalore": "Bengaluru", "bengaluru": "Bengaluru", "blr": "Bengaluru",
    "gurgaon": "Gurugram", "gurugram": "Gurugram",
    "bombay": "Mumbai", "new delhi": "Delhi", "ncr": "Delhi",
    "madras": "Chennai", "poona": "Pune",
}


def clean_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"\s+", " ", value).strip()


def fix_rupee(value: str) -> str:
    return re.sub(r"\?(?=\d)", "₹", value)


def money(value: str) -> str | None:
    """Keep a money field only if it is money. Shifted columns put employee counts here ("289 as on ...")."""
    value = fix_rupee(value)
    return value if re.match(r"^[$₹]", value) or re.search(r"\b(USD|INR|SGD)\b", value) else None


# Fixed FX rates are enough to order companies; use dated rates if exact USD figures are ever shown.
_TO_USD = {"$": 1.0, "₹": 1 / 85, "SGD": 0.75}
_UNITS = {"B": 1e9, "M": 1e6, "Cr": 1e7, "L": 1e5}


def money_usd(value: str | None) -> float | None:
    """Approximate USD amount of a money field, for sorting ("$36B as on ...", "₹2,786Cr", "1.33B SGD").
    A range ("$1.67B - $3.12B") counts as its lower bound."""
    m = re.match(r"^([$₹])?([\d,.]+)\s*(B|M|Cr|L)?\s*(SGD)?", value or "")
    if not m or not (m.group(1) or m.group(4)):
        return None
    return float(m.group(2).replace(",", "")) * _UNITS.get(m.group(3), 1) * _TO_USD[m.group(1) or m.group(4)]


def normalize_city(city: str | None) -> str | None:
    if not city:
        return None
    return CITY_ALIASES.get(city.strip().lower(), city.strip().title())


_HEADCOUNT = re.compile(r"^\d[\d,]* as on ")  # "952 as on Feb 28, 2025"


def _unshift_money(rev: str | None, val: str | None, fund: str | None) -> tuple:
    """Undo the scrape's column shift in the money fields. Returns (revenue, valuation, funding).

    When a company has no revenue figure, its valuation lands in annual_revenue and its funding in valuation.
    Tell-tales: a "revenue" dated on a funding-round day (real revenue is dated at a year end, Mar 31 or Dec 31)
    that is at least $500M, e.g. Rebel Foods "₹13,800Cr as on Apr 21, 2025" next to a round on Apr 22, 2025.
    And an undated "valuation" with no funding figure is the funding: valuations here carry an "as on" date and
    funding never does (Nykaa "$103M", Mamaearth "$89.4M", BYJU'S "$4.45B").
    """
    date = re.search(r"as on (\w{3} \d{2})", rev or "")
    if date and date.group(1) not in ("Mar 31", "Dec 31") and (money_usd(rev) or 0) >= 5e8:
        return None, rev, fund or val
    if rev and not date and not val and not fund:  # shifted twice: FirstCry's "$513M" is its funding
        return None, None, rev
    if val and not fund and " as on " not in val and " - " not in val:
        return rev, None, val
    return rev, val, fund


def _row_to_company(row: dict) -> dict:
    r = {k: clean_text(v) for k, v in row.items()}
    revenue, valuation, funding = _unshift_money(money(r["annual_revenue"]), money(r["valuation"]),
                                                 money(r["total_funding_till_date"]))
    # Shifted headcounts ("952 as on ...") land in valuation, total_funding or latest_funding_round.
    employees = r["employee_count"] or next(
        (r[k] for k in ("valuation", "total_funding_till_date", "latest_funding_round") if _HEADCOUNT.match(r[k])),
        None)
    latest_round = None if _HEADCOUNT.match(r["latest_funding_round"]) else r["latest_funding_round"] or None
    sector = r["primary_sector"]
    # Shifted columns put sector names where investors belong; drop those.
    investors = []
    for name in (r["round_led_by"], r["top_investors_1"], r["top_investors_2"]):
        if name and name not in _SECTOR_TO_GROUP and name not in investors:
            investors.append(name)
    return {
        "name": r["Company"],
        "rank": int(r["Rank"]),
        "sector": sector,
        "sector_group": _SECTOR_TO_GROUP.get(sector, "Other"),
        "city": r["location"],
        "country": r["country"],
        "stage": r["stage"],
        "founded_year": int(r["founded_year"]) if r["founded_year"].isdigit() else None,
        "description": r["company_background"],
        "unicorn_info": r["company_info"],
        "investors": investors,
        "valuation": valuation,
        "annual_revenue": revenue,
        "total_funding": funding,
        "employee_count": employees,
        "latest_funding_round": latest_round,
        "time_to_unicorn": r["time_to_unicorn"] or None,
        "profile_url": r["company_link"] or None,
    }


def load_companies(path: str | Path) -> list[dict]:
    with open(path, encoding="cp1252", newline="") as f:
        return [_row_to_company(row) for row in csv.DictReader(f)]


def embedding_text(c: dict) -> str:
    """The text we embed per company: what it does plus where it sits."""
    return (f"{c['name']}. Sector: {c['sector']} ({c['sector_group']}). "
            f"Based in {c['city']}, {c['country']}. {c['description']}")
