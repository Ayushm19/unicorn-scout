from pathlib import Path

from app.chatbot import Filters, normalize_filters, sort_and_limit
from app.data import SECTOR_GROUPS, load_companies, money_usd

CSV = Path(__file__).parent.parent / "data" / "tracxn.csv"


def test_load_and_clean_dataset():
    companies = load_companies(CSV)
    assert len(companies) == 118
    by_name = {c["name"]: c for c in companies}
    razorpay = by_name["Razorpay"]
    assert razorpay["sector_group"] == "FinTech" and razorpay["city"] == "Bengaluru"
    assert all(c["sector_group"] != "Other" for c in companies), "every sector must map to a group"
    assert by_name["Perfios"]["valuation"].startswith("₹")
    # Shifted columns: sector names must not leak into investors.
    assert "Alternative Lending" not in by_name["Perfios"]["investors"]
    assert all(c["city"] == c["city"].strip() for c in companies)
    # Shifted columns: employee counts must not pose as money.
    assert by_name["Open"]["total_funding"] is None and by_name["Policybazaar"]["valuation"] is None
    assert by_name["LivSpace"]["valuation"].startswith("1.33B SGD")
    assert by_name["Razorpay"]["total_funding"].startswith("$")


def test_sector_groups_cover_fintech():
    fintech = [c["name"] for c in load_companies(CSV) if c["sector_group"] == "FinTech"]
    assert {"Razorpay", "PhonePe", "Groww", "CRED"} <= set(fintech)
    assert len(SECTOR_GROUPS) == 12


def test_normalize_filters_maps_user_wording():
    f = normalize_filters(Filters(sector_group="fintech", city="Bangalore", stage="ipo", country="USA"))
    assert f == {"sector_group": "FinTech", "city": "Bengaluru", "stage": "Public", "country": "United States"}
    assert normalize_filters(Filters(sector_group="made-up", city=None)) == {}


def test_normalize_filters_keeps_exclusion_sort_and_sane_limits():
    f = normalize_filters(Filters(exclude_country="india", exclude_city="Bangalore", sort_by="valuation", limit=5))
    assert f == {"exclude_country": ["India"], "exclude_city": ["Bengaluru"], "sort_by": "valuation", "limit": 5}
    assert "limit" not in normalize_filters(Filters(limit=0)) and "limit" not in normalize_filters(Filters(limit=500))


def test_money_usd_parses_every_format_in_the_dataset():
    assert money_usd("$36B as on May 25, 2024") == 36e9
    assert money_usd("$566M") == 566e6
    assert money_usd("$1.67B - $3.12B") == 1.67e9  # range -> lower bound
    assert round(money_usd("₹8,500Cr")) == round(8500e7 / 85)
    assert money_usd("1.33B SGD as on Jun 23, 2022") == 1.33e9 * 0.75
    assert money_usd(None) is None and money_usd("289 as on Mar 2024") is None
    # Every valuation in the data either parses or is missing; nothing is silently mis-read as 0.
    assert all(money_usd(c["valuation"]) for c in load_companies(CSV) if c["valuation"])


def test_top_5_by_valuation_and_outside_india():
    companies = load_companies(CSV)
    top = sort_and_limit(companies, {"sort_by": "valuation", "limit": 5})
    assert len(top) == 5 and top[0]["name"] == "Flipkart"
    assert [money_usd(c["valuation"]) for c in top] == sorted((money_usd(c["valuation"]) for c in top), reverse=True)
    assert sort_and_limit(companies, {"sort_by": "oldest", "limit": 1})[0]["founded_year"] == min(
        c["founded_year"] for c in companies)
    abroad = [c["name"] for c in companies if c["country"] != "India"]
    assert len(abroad) == 16 and {"Hasura", "Chargebee"} <= set(abroad)
    assert normalize_filters(Filters(exclude_city=["Bangalore", "Gurgaon"]))["exclude_city"] == ["Bengaluru", "Gurugram"]
