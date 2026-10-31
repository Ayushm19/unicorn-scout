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
    # Shifted columns: employee counts must not pose as money, and move back to employee_count.
    assert by_name["Policybazaar"]["valuation"] is None
    assert by_name["Policybazaar"]["employee_count"].startswith("22,635")
    # Shifted money: a valuation sitting in annual_revenue (dated on its funding round) and funding in valuation.
    games = by_name["Games24x7"]
    assert games["valuation"].startswith("₹18,800Cr") and games["total_funding"] == "$99.1M"
    assert games["annual_revenue"] is None
    assert by_name["Open"]["valuation"].startswith("₹7,300Cr") and by_name["Open"]["total_funding"] == "$186M"
    assert by_name["Nykaa"]["valuation"] is None and by_name["Nykaa"]["total_funding"] == "$103M"
    assert by_name["Nykaa"]["annual_revenue"].startswith("₹6,420Cr")  # real revenue, dated Mar 31, stays
    assert by_name["FirstCry"]["total_funding"] == "$513M" and by_name["FirstCry"]["annual_revenue"] is None
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


def test_count_sentence_states_scope_and_count():
    from app.chatbot import _count_sentence
    assert _count_sentence(57, 0, scoped=False, widened=False) == "57 of the 118 unicorns match."
    assert _count_sentence(8, 14, scoped=True, widened=False) == "Of the 14 companies in my previous answer, 8 match."
    assert _count_sentence(1, 1, scoped=True, widened=False) == "Of the company in my previous answer, 1 matches."
    assert "searched all 118: 16 match" in _count_sentence(16, 5, scoped=False, widened=True)
    from app.chatbot import _ranked_sentence
    companies = sort_and_limit(load_companies(CSV), {"sort_by": "valuation", "limit": 3})
    assert _ranked_sentence(companies, "valuation", limited=True) == (
        "Top 3 by valuation: **Flipkart** (≈ $36.0B), **Blinkit** (≈ $13.0B), **PhonePe** (≈ $12.2B).")
    oldest = sort_and_limit(load_companies(CSV), {"sort_by": "oldest", "limit": 2})
    assert _ranked_sentence(oldest, "oldest", limited=True).startswith("2 oldest: **Five Star Business Finance** (1984)")


def test_filters_take_several_values():
    f = normalize_filters(Filters(sector="cybersecurity or api management", city=["Bangalore", "Pune"],
                                  sector_group=["fintech", "made-up"], stage="ipo"))
    assert f == {"sector": ["cybersecurity", "api management"], "city": ["Bengaluru", "Pune"],
                 "sector_group": "FinTech", "stage": "Public"}
    # Real sector names contain "and" and commas; only " or " splits.
    assert normalize_filters(Filters(sector="Speech and Voice Recognition")) == {"sector": "Speech and Voice Recognition"}


def test_ranked_sentence_marks_more_only_when_more_follow():
    from app.chatbot import _ranked_sentence
    top5 = sort_and_limit(load_companies(CSV), {"sort_by": "valuation", "limit": 5})
    assert not _ranked_sentence(top5, "valuation", limited=True).endswith("…")
    assert _ranked_sentence(top5, "valuation", limited=True, prev=28).startswith(
        "Of the 28 companies in my previous answer, top 5 by valuation: **Flipkart**")
    everyone = sort_and_limit(load_companies(CSV), {"sort_by": "valuation"})
    assert _ranked_sentence(everyone, "valuation", limited=False).endswith(", …")
