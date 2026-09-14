"""Seed universe of tradeable Indian equities.

Prices are approximate reference levels used to *seed* the simulator; they are not live
quotes. Every listed scrip trades on NSE; the BSE listing mirrors it with a small basis.
``lot_size`` is 1 for the cash segment. ``band_pct`` is the daily price band (circuit filter).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class InstrumentSeed:
    symbol: str
    name: str
    sector: str
    ref_price: Decimal
    annual_vol: float  # annualised volatility used by the simulator (0.25 = 25%)
    band_pct: int = 10  # price band / circuit limit in percent
    tick_size: Decimal = Decimal("0.05")
    lot_size: int = 1
    isin: str | None = None


def _s(symbol, name, sector, price, vol, band=10):
    return InstrumentSeed(symbol, name, sector, Decimal(str(price)), vol, band)


# Broadly the NIFTY 50 universe plus a few liquid large/mid caps. Reference prices are
# rounded levels from 2026 and only anchor the simulator's starting point.
UNIVERSE: tuple[InstrumentSeed, ...] = (
    _s("RELIANCE", "Reliance Industries", "Energy", 1450, 0.22),
    _s("TCS", "Tata Consultancy Services", "IT", 3400, 0.20),
    _s("HDFCBANK", "HDFC Bank", "Banks", 980, 0.20),
    _s("ICICIBANK", "ICICI Bank", "Banks", 1400, 0.21),
    _s("INFY", "Infosys", "IT", 1500, 0.24),
    _s("SBIN", "State Bank of India", "Banks", 800, 0.26),
    _s("BHARTIARTL", "Bharti Airtel", "Telecom", 1900, 0.22),
    _s("ITC", "ITC", "FMCG", 420, 0.18),
    _s("LT", "Larsen & Toubro", "Infrastructure", 3600, 0.24),
    _s("KOTAKBANK", "Kotak Mahindra Bank", "Banks", 2100, 0.22),
    _s("HINDUNILVR", "Hindustan Unilever", "FMCG", 2400, 0.17),
    _s("AXISBANK", "Axis Bank", "Banks", 1150, 0.25),
    _s("BAJFINANCE", "Bajaj Finance", "NBFC", 950, 0.28),
    _s("MARUTI", "Maruti Suzuki", "Auto", 12500, 0.22),
    _s("SUNPHARMA", "Sun Pharmaceutical", "Pharma", 1700, 0.22),
    _s("TITAN", "Titan Company", "Consumer", 3500, 0.24),
    _s("ASIANPAINT", "Asian Paints", "Consumer", 2400, 0.21),
    _s("ULTRACEMCO", "UltraTech Cement", "Cement", 11500, 0.22),
    _s("NTPC", "NTPC", "Power", 340, 0.23),
    _s("POWERGRID", "Power Grid Corporation", "Power", 290, 0.20),
    _s("TATASTEEL", "Tata Steel", "Metals", 160, 0.32),
    _s("JSWSTEEL", "JSW Steel", "Metals", 1050, 0.30),
    _s("M&M", "Mahindra & Mahindra", "Auto", 3300, 0.25),
    _s("BAJAJFINSV", "Bajaj Finserv", "NBFC", 2000, 0.25),
    _s("NESTLEIND", "Nestle India", "FMCG", 1200, 0.18),
    _s("HCLTECH", "HCL Technologies", "IT", 1500, 0.23),
    _s("WIPRO", "Wipro", "IT", 250, 0.25),
    _s("TECHM", "Tech Mahindra", "IT", 1550, 0.26),
    _s("ADANIENT", "Adani Enterprises", "Conglomerate", 2400, 0.40),
    _s("ADANIPORTS", "Adani Ports & SEZ", "Infrastructure", 1400, 0.32),
    _s("ONGC", "Oil & Natural Gas Corporation", "Energy", 250, 0.26),
    _s("COALINDIA", "Coal India", "Mining", 400, 0.24),
    _s("BPCL", "Bharat Petroleum", "Energy", 330, 0.28),
    _s("GRASIM", "Grasim Industries", "Cement", 2800, 0.23),
    _s("HINDALCO", "Hindalco Industries", "Metals", 750, 0.30),
    _s("DRREDDY", "Dr. Reddy's Laboratories", "Pharma", 1250, 0.22),
    _s("CIPLA", "Cipla", "Pharma", 1500, 0.21),
    _s("APOLLOHOSP", "Apollo Hospitals", "Healthcare", 7500, 0.24),
    _s("DIVISLAB", "Divi's Laboratories", "Pharma", 6500, 0.24),
    _s("EICHERMOT", "Eicher Motors", "Auto", 6500, 0.23),
    _s("HEROMOTOCO", "Hero MotoCorp", "Auto", 5000, 0.24),
    _s("BAJAJ-AUTO", "Bajaj Auto", "Auto", 8500, 0.22),
    _s("TATACONSUM", "Tata Consumer Products", "FMCG", 1100, 0.22),
    _s("BRITANNIA", "Britannia Industries", "FMCG", 5800, 0.18),
    _s("SBILIFE", "SBI Life Insurance", "Insurance", 1900, 0.22),
    _s("HDFCLIFE", "HDFC Life Insurance", "Insurance", 780, 0.22),
    _s("INDUSINDBK", "IndusInd Bank", "Banks", 800, 0.35),
    _s("SHRIRAMFIN", "Shriram Finance", "NBFC", 700, 0.28),
    _s("TRENT", "Trent", "Retail", 5000, 0.35),
    _s("BEL", "Bharat Electronics", "Defence", 400, 0.30),
    _s("JIOFIN", "Jio Financial Services", "NBFC", 300, 0.30),
    _s("ETERNAL", "Eternal (Zomato)", "Internet", 300, 0.38, 20),
    _s("INDIGO", "InterGlobe Aviation", "Aviation", 5500, 0.26),
    _s("ZYDUSLIFE", "Zydus Lifesciences", "Pharma", 950, 0.24),
    _s("IRCTC", "IRCTC", "Travel", 750, 0.28, 20),
    _s("YESBANK", "Yes Bank", "Banks", 20, 0.40, 20),
    _s("IDEA", "Vodafone Idea", "Telecom", 8, 0.55, 20),
    _s("SUZLON", "Suzlon Energy", "Renewables", 60, 0.45, 20),
)

BY_SYMBOL: dict[str, InstrumentSeed] = {i.symbol: i for i in UNIVERSE}
