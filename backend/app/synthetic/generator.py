"""Synthetic bank-statement ground truth.

Produces realistic Indian retail-banking transactions (INR, UPI/NEFT/POS/ACH
narrations with messy merchant strings) with exact running balances, category
labels and seeded anomalies. Rendering to PDF lives in `render.py`.

All money is handled as integer paise internally to keep reconciliation exact.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from app.schemas.taxonomy import HEAD_OF

# (category, merchant (normalized), narration templates, (min_rupees, max_rupees))
# `{ref}` -> random reference number, `{n}` -> short random code.
MERCHANTS: list[tuple[str, str, list[str], tuple[int, int]]] = [
    ("Groceries", "BigBasket", ["UPI/{ref}/BIGBASKET/bigbasket@hdfc", "POS {n} BIGBASKET BLR"], (300, 3500)),
    ("Groceries", "DMart", ["POS {n} AVENUE SUPERMARTS", "UPI/{ref}/DMART/dmart.pay@icici"], (400, 4500)),
    ("Groceries", "Zepto", ["UPI/{ref}/ZEPTO MARKETPLACE/zepto@ybl"], (150, 1200)),
    ("Dining", "Swiggy", ["UPI/{ref}/SWIGGY/swiggy.stores@axb", "POS {n} SWIGGY*ORDER"], (180, 1400)),
    ("Dining", "Zomato", ["UPI/{ref}/ZOMATO LTD/zomato@hdfcbank"], (200, 1600)),
    ("Dining", "Third Wave Coffee", ["POS {n} THIRD WAVE COFFEE ROA"], (180, 650)),
    ("Transport/Fuel", "Uber", ["UPI/{ref}/UBER INDIA/uber@axisbank", "POS {n} UBER *TRIP"], (90, 800)),
    ("Transport/Fuel", "Indian Oil", ["POS {n} IOCL COCO FUEL STN", "UPI/{ref}/INDIAN OIL CORP/iocl@sbi"], (500, 3000)),
    ("Transport/Fuel", "Namma Metro", ["UPI/{ref}/BMRCL/bmrcl@ybl"], (50, 500)),
    ("Shopping", "Amazon", ["POS {n} AMZN MKTP IN*{n}", "UPI/{ref}/AMAZON PAY/amazon@apl"], (250, 6000)),
    ("Shopping", "Flipkart", ["UPI/{ref}/FLIPKART INTERNET/fkrt@ybl"], (300, 5000)),
    ("Shopping", "Myntra", ["POS {n} MYNTRA DESIGNS PVT"], (500, 4000)),
    ("Health", "Apollo Pharmacy", ["POS {n} APOLLO PHARMACY 0{n}", "UPI/{ref}/APOLLO PHARMACIES/apollo@icici"], (120, 2500)),
    ("Utilities", "BESCOM", ["BILLPAY/BESCOM/{ref}"], (800, 3200)),
    ("Utilities", "Airtel", ["BILLPAY/AIRTEL POSTPAID/{ref}"], (499, 1199)),
    ("Utilities", "ACT Fibernet", ["ACH D- ACT FIBERNET-{ref}"], (799, 1499)),
]

SUBSCRIPTIONS = [
    ("Netflix", "POS {n} NETFLIX.COM", 649),
    ("Spotify", "UPI/{ref}/SPOTIFY INDIA/spotify@hdfcbank", 119),
    ("YouTube Premium", "POS {n} GOOGLE *YOUTUBEPREMIUM", 149),
    ("Hotstar", "POS {n} DISNEY HOTSTAR", 299),
]
NEW_RECURRING_POOL = [
    ("Cult.fit", "ACH D- CULTFIT HEALTHIFYME-{ref}", 1499),
    ("Audible", "POS {n} AUDIBLE*IN", 199),
    ("ChatGPT Plus", "POS {n} OPENAI *CHATGPT SUBSCR", 1999),
]

FIRST_NAMES = ["Aarav", "Diya", "Kabir", "Meera", "Rohan", "Ananya", "Vikram", "Isha"]
LAST_NAMES = ["Sharma", "Iyer", "Reddy", "Nair", "Gupta", "Menon", "Rao", "Kulkarni"]
LANDLORDS = ["R K PROPERTIES", "SUNITA DEVI", "PRESTIGE RENTALS"]
PEOPLE = ["RAMESH PLUMBER", "SURESH ELECTRICIAN", "PRIYA S", "AMIT K", "MOM", "LATHA MAID"]


@dataclass
class Txn:
    date: str  # ISO yyyy-mm-dd
    description: str
    merchant: str
    category: str
    head: str
    debit: int = 0  # paise
    credit: int = 0  # paise
    balance: int = 0  # paise
    value_date: str | None = None
    ref: str = ""
    page: int | None = None
    idx: int = 0


@dataclass
class Statement:
    statement_id: str
    layout: str
    bank: str
    holder: str
    account_masked: str
    ifsc: str
    branch: str
    period_start: str
    period_end: str
    opening_balance: int
    closing_balance: int = 0
    transactions: list[Txn] = field(default_factory=list)
    anomalies: list[dict] = field(default_factory=list)
    source_type: str = "digital"

    def to_ground_truth(self) -> dict:
        def rs(p: int) -> float:
            return round(p / 100, 2)

        d = asdict(self)
        d["currency"] = "INR"
        d["opening_balance"] = rs(self.opening_balance)
        d["closing_balance"] = rs(self.closing_balance)
        d["total_debits"] = rs(sum(t.debit for t in self.transactions))
        d["total_credits"] = rs(sum(t.credit for t in self.transactions))
        d["n_transactions"] = len(self.transactions)
        for t in d["transactions"]:
            t["debit"] = rs(t["debit"]) if t["debit"] else None
            t["credit"] = rs(t["credit"]) if t["credit"] else None
            t["balance"] = rs(t["balance"])
        return d


class _Builder:
    def __init__(self, rng: random.Random):
        self.rng = rng
        self.events: list[Txn] = []

    def ref(self) -> str:
        return str(self.rng.randint(10**11, 10**12 - 1))

    def code(self) -> str:
        return "".join(self.rng.choices("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789", k=4))

    def fmt(self, tpl: str) -> str:
        return tpl.replace("{ref}", self.ref()).replace("{n}", self.code())

    def add(self, d: date, desc: str, merchant: str, category: str, *, debit_rs: float = 0,
            credit_rs: float = 0) -> Txn:
        t = Txn(
            date=d.isoformat(), description=desc, merchant=merchant, category=category,
            head=HEAD_OF[category], debit=round(debit_rs * 100), credit=round(credit_rs * 100),
        )
        self.events.append(t)
        return t

    def amount(self, lo: int, hi: int) -> float:
        # Skewed toward the low end, with paise.
        x = lo + (hi - lo) * (self.rng.random() ** 1.8)
        return round(x + self.rng.choice([0, 0, 0.5, self.rng.random()]), 2)


def _month_starts(start: date, months: int) -> list[date]:
    out, d = [], start
    for _ in range(months):
        out.append(d)
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def generate_statement(
    statement_id: str, layout: str, bank: str, seed: int, *, months: int = 2,
    start: date = date(2025, 1, 1), anomalies: bool = True,
) -> Statement:
    rng = random.Random(seed)
    b = _Builder(rng)
    month_starts = _month_starts(start, months)
    end = (month_starts[-1].replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

    holder = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}".upper()
    salary = rng.choice([65000, 82000, 95000, 120000, 145000])
    employer = rng.choice(["ACME SOFTWARE PVT LTD", "NIMBUS ANALYTICS LLP", "ORBIT TECH SERVICES"])
    rent = rng.choice([18000, 22000, 28000, 35000])
    landlord = rng.choice(LANDLORDS)
    emi = rng.choice([0, 8450, 12999, 15620])
    subs = rng.sample(SUBSCRIPTIONS, k=rng.randint(1, 3))
    insurance_month = rng.randrange(months)

    for mi, ms in enumerate(month_starts):
        def day(n: int) -> date:
            return ms + timedelta(days=min(n, 27) - 1)  # noqa: B023 - called within the iteration

        b.add(day(1), f"NEFT CR-{employer[:10].replace(' ', '')}0001-{employer}-SALARY {ms:%b %Y}".upper(),
              employer.title(), "Salary", credit_rs=salary)
        b.add(day(rng.randint(2, 5)), f"IMPS/P2A/{b.ref()}/{landlord}/RENT", landlord.title(), "Housing",
              debit_rs=rent)
        if emi:
            b.add(day(5), f"ACH D- HDB FINANCIAL SERV-{b.ref()}", "HDB Financial", "EMI/Loans", debit_rs=emi)
        for name, tpl, amt in subs:
            b.add(day(rng.randint(3, 20)), b.fmt(tpl), name, "Subscriptions", debit_rs=amt)
        if mi == insurance_month:
            b.add(day(rng.randint(8, 22)), f"BILLPAY/LIC OF INDIA/{b.ref()}", "LIC", "Insurance",
                  debit_rs=rng.choice([4500, 12500, 18200]))
        # Discretionary spend.
        for _ in range(rng.randint(14, 26)):
            cat, merch, tpls, (lo, hi) = rng.choice(MERCHANTS)
            b.add(day(rng.randint(1, 28)), b.fmt(rng.choice(tpls)), merch, cat, debit_rs=b.amount(lo, hi))
        if rng.random() < 0.8:
            b.add(day(rng.randint(6, 25)), f"ATW-{rng.randint(400000, 499999)}-ATM CASH WDL-{b.code()}",
                  "ATM", "Cash Withdrawal", debit_rs=rng.choice([2000, 3000, 5000, 10000]))
        if rng.random() < 0.7:
            who = rng.choice(PEOPLE)
            b.add(day(rng.randint(2, 27)), f"UPI/{b.ref()}/{who}/{who.split()[0].lower()}@oksbi",
                  who.title(), "Transfers Out", debit_rs=rng.choice([500, 1200, 2500, 4000]))
        if rng.random() < 0.4:
            b.add(day(rng.randint(2, 27)), f"UPI/{b.ref()}/{rng.choice(PEOPLE)}/REFUND SPLIT",
                  "Friend", "Transfers In", credit_rs=rng.choice([350, 800, 1500]))
        if rng.random() < 0.3:
            b.add(day(rng.randint(10, 27)), f"REFUND/AMZN MKTP IN/{b.ref()}", "Amazon", "Refunds",
                  credit_rs=b.amount(300, 2500))
        if rng.random() < 0.25:
            b.add(day(rng.randint(10, 27)), f"NEFT CR-FREELANCE-{rng.choice(['UPWORK', 'TOPTAL'])} ESCROW",
                  "Freelance", "Business/Freelance", credit_rs=rng.choice([15000, 22000, 40000]))
        b.add(day(28), "SMS ALERT CHGS INCL GST", "Bank", "Fees/Charges", debit_rs=17.70)
        if mi == months - 1 or rng.random() < 0.3:
            b.add(day(28), "CREDIT INTEREST CAPITALISED", "Bank", "Interest",
                  credit_rs=round(rng.uniform(80, 900), 2))

    stmt_anoms: list[dict] = []
    if anomalies:
        _seed_anomalies(b, rng, month_starts, stmt_anoms)

    # Order: by date; within a day credits first (salary lands before spending).
    b.events.sort(key=lambda t: (t.date, 0 if t.credit else 1))
    opening = round(rng.uniform(8000, 60000) * 100)
    bal = opening
    for i, t in enumerate(b.events):
        bal += t.credit - t.debit
        t.balance = bal
        t.idx = i
        t.ref = t.ref or b.ref()[:10]
        vd = date.fromisoformat(t.date) + timedelta(days=1 if rng.random() < 0.08 else 0)
        t.value_date = vd.isoformat()

    for a in stmt_anoms:  # resolve object refs -> indices
        a["txn_idx"] = sorted(t.idx for t in a.pop("_txns"))

    return Statement(
        statement_id=statement_id, layout=layout, bank=bank, holder=holder,
        account_masked=f"XXXXXXXX{rng.randint(1000, 9999)}",
        ifsc=f"{bank[:4].upper()}0{rng.randint(100000, 999999)}",
        branch=rng.choice(["Indiranagar", "Koramangala", "HSR Layout", "Whitefield", "Jayanagar"]),
        period_start=month_starts[0].isoformat(), period_end=end.isoformat(),
        opening_balance=opening, closing_balance=bal, transactions=b.events, anomalies=stmt_anoms,
    )


def _seed_anomalies(b: _Builder, rng: random.Random, month_starts: list[date], out: list[dict]) -> None:
    last = month_starts[-1]
    spend = [t for t in b.events if t.debit and t.category in ("Shopping", "Dining", "Groceries")]

    # 1. Duplicate charge: same narration + amount, same or next day.
    orig = rng.choice(spend)
    d = date.fromisoformat(orig.date) + timedelta(days=rng.choice([0, 1]))
    dup = b.add(d, orig.description, orig.merchant, orig.category, debit_rs=orig.debit / 100)
    out.append({"type": "duplicate_charge", "_txns": [orig, dup]})

    # 2. Unusually large debit vs own category median.
    cat_txns = [t for t in spend if t.category == "Shopping"] or spend
    med = sorted(t.debit for t in cat_txns)[len(cat_txns) // 2] / 100
    big = b.add(last + timedelta(days=rng.randint(5, 24)), b.fmt("POS {n} CROMA RETAIL ELECTRONICS"),
                "Croma", "Shopping", debit_rs=round(max(med * rng.uniform(8, 14), 25000), 2))
    out.append({"type": "large_debit", "_txns": [big]})

    # 3. New recurring payment: appears only in the last month.
    name, tpl, amt = rng.choice(NEW_RECURRING_POOL)
    nr = b.add(last + timedelta(days=rng.randint(2, 20)), b.fmt(tpl), name, "Subscriptions", debit_rs=amt)
    out.append({"type": "new_recurring", "_txns": [nr]})

    # 4. Fee spike: cluster of charges in one month.
    fees = []
    for desc, amt in [("CHQ RETURN CHGS INCL GST", 590.0), ("MIN BAL NON MAINT CHGS", 354.0),
                      ("ECS RETURN CHGS INCL GST", 472.0)]:
        fees.append(b.add(last + timedelta(days=rng.randint(10, 26)), desc, "Bank", "Fees/Charges",
                          debit_rs=amt))
    out.append({"type": "fee_spike", "_txns": fees})
