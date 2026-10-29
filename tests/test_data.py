from pathlib import Path

from app.chatbot import Filters, normalize_filters
from app.data import SECTOR_GROUPS, load_companies

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


def test_sector_groups_cover_fintech():
    fintech = [c["name"] for c in load_companies(CSV) if c["sector_group"] == "FinTech"]
    assert {"Razorpay", "PhonePe", "Groww", "CRED"} <= set(fintech)
    assert len(SECTOR_GROUPS) == 12


def test_normalize_filters_maps_user_wording():
    f = normalize_filters(Filters(sector_group="fintech", city="Bangalore", stage="ipo", country="USA"))
    assert f == {"sector_group": "FinTech", "city": "Bengaluru", "stage": "Public", "country": "United States"}
    assert normalize_filters(Filters(sector_group="made-up", city=None)) == {}
