"""Load and clean the Tracxn unicorn CSV into plain dicts ready for the database.

Known quirks of the source file (see README "Data quality"):
- Encoded as Windows-1252, not UTF-8; contains non-breaking spaces.
- The rupee sign was lost upstream and appears as "?" (e.g. "?569Cr").
- `location` values carry trailing whitespace.
- `top_investors_1/2` sometimes hold sector names instead of investors (shifted columns).
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


def normalize_city(city: str | None) -> str | None:
    if not city:
        return None
    return CITY_ALIASES.get(city.strip().lower(), city.strip().title())


def _row_to_company(row: dict) -> dict:
    r = {k: clean_text(v) for k, v in row.items()}
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
        "valuation": fix_rupee(r["valuation"]) or None,
        "annual_revenue": fix_rupee(r["annual_revenue"]) or None,
        "total_funding": r["total_funding_till_date"] or None,
        "employee_count": r["employee_count"] or None,
        "latest_funding_round": r["latest_funding_round"] or None,
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
