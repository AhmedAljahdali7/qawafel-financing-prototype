"""Generate 12 months of FICTIONAL invoice and bank data for a made-up wholesaler.

Company: "Al Waha Foods Trading" (fictional), a Jeddah food and beverage wholesaler
selling to supermarkets, restaurants and cafes.

Everything here is invented. Run with: python generate_data.py
Outputs go to ./sample_data (invoices.xlsx, invoices.csv, bank_transactions.csv)
"""
import os
import random
from datetime import date, timedelta

import numpy as np
import pandas as pd

SEED = 2026
random.seed(SEED)
rng = np.random.default_rng(SEED)

START = date(2025, 10, 1)   # 12 months: Oct 2025 to Sep 2026
END = date(2026, 9, 30)
OPENING_BALANCE = 2_500_000.0   # SAR, balance at the start of the statement
AS_OF = date(2026, 10, 15)  # "today" for the analysis (open invoices are measured against it)

# Customers: name, segment, payment terms (days), behaviour, relative size
# behaviour: avg extra days paid after due date, and probability of a long delay
CUSTOMERS = [
    # name, segment, terms, avg_late_days, p_very_late, weight
    ("Hyper Basket Markets", "Supermarket", 60, 6, 0.04, 14.0),
    ("Red Sea Fresh Mart", "Supermarket", 45, 4, 0.03, 9.0),
    ("Najd Family Stores", "Supermarket", 45, 12, 0.10, 7.0),
    ("Corniche Cafe Group", "Cafe chain", 30, 3, 0.02, 6.0),
    ("Al Baraka Catering", "Catering", 30, 10, 0.08, 5.5),
    ("Palm Grill Restaurants", "Restaurant", 30, 5, 0.03, 5.0),
    ("Tahlia Bites", "Restaurant", 30, 8, 0.06, 3.5),
    ("Alsafa Minimarket", "Minimarket", 30, 20, 0.18, 3.0),
    ("Jeddah Hotel Supplies", "Hospitality", 60, 7, 0.04, 6.5),
    ("Madinah Road Grocers", "Minimarket", 30, 15, 0.12, 2.5),
    ("Blue Wave Cafe", "Cafe", 15, 4, 0.02, 1.8),
    ("Al Khalij Sweets", "Bakery", 15, 6, 0.05, 1.5),
    ("Harbor Light Cafeteria", "Cafe", 15, 9, 0.07, 1.4),
    ("Rawabi Mini Mart", "Minimarket", 30, 25, 0.22, 2.2),
    ("Sunrise Bakery", "Bakery", 15, 3, 0.02, 1.6),
    ("Al Hamra Events", "Catering", 30, 18, 0.15, 2.0),
    ("Zahra Superstore", "Supermarket", 45, 9, 0.06, 4.5),
    ("Oasis Juice Bar", "Cafe", 15, 5, 0.03, 1.2),
    ("Taif Road Mart", "Minimarket", 30, 14, 0.11, 2.4),
    ("Marina Dining Co", "Restaurant", 30, 6, 0.04, 3.2),
]

# Monthly seasonality (index 1.0 = average). Ramadan is roughly 18 Feb to 19 Mar 2026,
# so demand builds in Jan and Feb and peaks in March. Summer is slightly softer.
SEASON = {
    (2025, 10): 0.95, (2025, 11): 0.98, (2025, 12): 1.00,
    (2026, 1): 1.05, (2026, 2): 1.25, (2026, 3): 1.40,
    (2026, 4): 1.10, (2026, 5): 0.95, (2026, 6): 0.90,
    (2026, 7): 0.88, (2026, 8): 0.92, (2026, 9): 1.02,
}


def business_days(start, end):
    d = start
    while d <= end:
        if d.weekday() not in (4, 5):  # Friday and Saturday weekend in KSA
            yield d
        d += timedelta(days=1)


def main():
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_data")
    os.makedirs(out_dir, exist_ok=True)

    names = [c[0] for c in CUSTOMERS]
    weights = np.array([c[5] for c in CUSTOMERS], dtype=float)
    weights = weights / weights.sum()
    cust_by_name = {c[0]: c for c in CUSTOMERS}

    rows = []
    inv_no = 1000
    days = list(business_days(START, END))
    for d in days:
        season = SEASON[(d.year, d.month)]
        n_today = rng.poisson(2.6 * season)
        for _ in range(n_today):
            cname = rng.choice(names, p=weights)
            _, segment, terms, avg_late, p_vl, _w = cust_by_name[cname]
            inv_no += 1
            base = {"Supermarket": 9500, "Hospitality": 8000, "Catering": 5200,
                    "Restaurant": 3600, "Cafe chain": 4200, "Minimarket": 2600,
                    "Cafe": 1500, "Bakery": 1700}.get(segment, 3000)
            amount = float(np.round(rng.lognormal(mean=np.log(base), sigma=0.45), 2))
            amount = max(amount, 150.0)
            vat = round(amount * 0.15, 2)
            due = d + timedelta(days=terms)

            # payment behaviour
            if rng.random() < p_vl:
                late = int(rng.integers(35, 100))
            else:
                late = int(max(-5, rng.normal(avg_late * 0.6 - 3, 5)))
            pay_date = due + timedelta(days=late)
            status = "Paid" if pay_date <= AS_OF else "Open"

            rows.append({
                "invoice_no": f"INV-{inv_no}",
                "invoice_date": d,
                "customer": cname,
                "segment": segment,
                "payment_terms_days": terms,
                "due_date": due,
                "net_amount_sar": amount,
                "vat_sar": vat,
                "total_sar": round(amount + vat, 2),
                "status": status,
                "paid_date": pay_date if status == "Paid" else None,
                "doc_type": "Invoice",
            })

    df = pd.DataFrame(rows)

    # Credit notes: about 2.5% of invoices get partly or fully reversed later
    cn_rows = []
    sample = df.sample(frac=0.025, random_state=SEED)
    for _, r in sample.iterrows():
        inv_no += 1
        frac = random.choice([0.2, 0.35, 0.5, 1.0])
        cn_date = r["invoice_date"] + timedelta(days=int(rng.integers(5, 40)))
        if cn_date > END:
            continue
        net = round(-r["net_amount_sar"] * frac, 2)
        vat = round(net * 0.15, 2)
        cn_rows.append({
            "invoice_no": f"CN-{inv_no}",
            "invoice_date": cn_date,
            "customer": r["customer"],
            "segment": r["segment"],
            "payment_terms_days": r["payment_terms_days"],
            "due_date": cn_date,
            "net_amount_sar": net,
            "vat_sar": vat,
            "total_sar": round(net + vat, 2),
            "status": "Credit",
            "paid_date": None,
            "doc_type": "Credit note",
        })
    df = pd.concat([df, pd.DataFrame(cn_rows)], ignore_index=True)

    # ---- Deliberate data problems (real uploads are never clean) ----
    inv_idx = df.index[df["doc_type"] == "Invoice"].to_numpy()
    # 1) duplicate invoice rows (exported twice)
    dup = df.loc[rng.choice(inv_idx, size=7, replace=False)].copy()
    df = pd.concat([df, dup], ignore_index=True)
    # 2) missing VAT on a few invoices
    miss = rng.choice(df.index[df["doc_type"] == "Invoice"], size=6, replace=False)
    df.loc[miss, "vat_sar"] = np.nan
    df.loc[miss, "total_sar"] = df.loc[miss, "net_amount_sar"]
    # 3) due date before invoice date (typo)
    bad = rng.choice(df.index[df["doc_type"] == "Invoice"], size=3, replace=False)
    df.loc[bad, "due_date"] = df.loc[bad, "invoice_date"] - pd.Timedelta(days=30)
    # 4) customer name spelled differently
    var = rng.choice(df.index[df["customer"] == "Hyper Basket Markets"], size=6, replace=False)
    df.loc[var, "customer"] = "HyperBasket Market"
    var2 = rng.choice(df.index[df["customer"] == "Corniche Cafe Group"], size=4, replace=False)
    df.loc[var2, "customer"] = "Corniche Café Group"
    # 5) paid date earlier than invoice date
    pd_bad = rng.choice(df.index[(df["status"] == "Paid")], size=2, replace=False)
    df.loc[pd_bad, "paid_date"] = df.loc[pd_bad, "invoice_date"] - pd.Timedelta(days=3)

    df = df.sort_values(["invoice_date", "invoice_no"]).reset_index(drop=True)

    # ---- Bank transactions (what the PDF statements would contain after extraction) ----
    bank = []
    paid = df[(df["status"] == "Paid") & (df["doc_type"] == "Invoice")].drop_duplicates("invoice_no")
    # Customers often pay several invoices in one transfer; model ~25% as batched by customer+week
    paid = paid.copy()
    paid["paid_date"] = pd.to_datetime(paid["paid_date"])
    paid["wk"] = paid["paid_date"].dt.to_period("W-THU")
    batched_keys = set()
    for (cust, wk), g in paid.groupby(["customer", "wk"]):
        if len(g) > 1 and rng.random() < 0.6:
            batched_keys.add((cust, wk))
    tx_no = 0
    for (cust, wk), g in paid.groupby(["customer", "wk"]):
        if (cust, wk) in batched_keys:
            tx_no += 1
            total = round(g["total_sar"].sum(), 2)
            bank.append({
                "tx_id": f"TX-{tx_no:05d}", "date": g["paid_date"].max().date(),
                "description": f"TRANSFER FROM {cust.upper()} REF {'/'.join(g['invoice_no'].str[4:])}",
                "credit_sar": total, "debit_sar": 0.0,
            })
        else:
            for _, r in g.iterrows():
                if rng.random() < 0.09:  # marked paid in the ledger, but no matching bank credit (cash, other account, or error)
                    continue
                tx_no += 1
                ref = r["invoice_no"] if rng.random() < 0.7 else ""
                desc = f"TRANSFER FROM {cust.upper()}" + (f" REF {ref}" if ref else "")
                amt = r["total_sar"]
                if rng.random() < 0.03:  # short pay
                    amt = round(amt * 0.97, 2)
                bank.append({
                    "tx_id": f"TX-{tx_no:05d}", "date": r["paid_date"].date(),
                    "description": desc, "credit_sar": amt, "debit_sar": 0.0,
                })
    # Transfers from customers that match no invoice (advance payments, other entities)
    for cust_name in ["NAJD FAMILY STORES", "ALSAFA MINIMARKET", "RED SEA FRESH MART", "UNKNOWN SENDER 4471",
                      "TAIF ROAD MART", "AL HAMRA EVENTS", "ZAHRA SUPERSTORE", "PALM GRILL RESTAURANTS"]:
        tx_no += 1
        bank.append({"tx_id": f"TX-{tx_no:05d}", "date": START + timedelta(days=int(rng.integers(10, 340))),
                     "description": f"TRANSFER FROM {cust_name}",
                     "credit_sar": float(np.round(rng.uniform(4000, 22000), 2)), "debit_sar": 0.0})
    # Non-customer entries: supplier payments, rent, salaries, loan instalments
    d = START
    while d <= END:
        if d.day == 1:
            tx_no += 1
            bank.append({"tx_id": f"TX-{tx_no:05d}", "date": d, "description": "WAREHOUSE RENT",
                         "credit_sar": 0.0, "debit_sar": 38000.0})
        if d.day == 25:
            tx_no += 1
            bank.append({"tx_id": f"TX-{tx_no:05d}", "date": d, "description": "PAYROLL",
                         "credit_sar": 0.0, "debit_sar": 64000.0})
            tx_no += 1
            bank.append({"tx_id": f"TX-{tx_no:05d}", "date": d, "description": "EXISTING LOAN INSTALMENT",
                         "credit_sar": 0.0, "debit_sar": 21000.0})
        d += timedelta(days=1)
    for i in range(260):
        tx_no += 1
        dd = START + timedelta(days=int(rng.integers(0, (END - START).days)))
        bank.append({"tx_id": f"TX-{tx_no:05d}", "date": dd,
                     "description": random.choice(["SUPPLIER PAYMENT AL SAFWA TRADING",
                                                   "SUPPLIER PAYMENT GULF DAIRY",
                                                   "FUEL AND LOGISTICS", "CASH DEPOSIT"]),
                     "credit_sar": float(np.round(rng.uniform(2000, 9000), 2)) if random.random() < 0.08 else 0.0,
                     "debit_sar": float(np.round(rng.lognormal(np.log(18000), 0.6), 2)) if random.random() >= 0.08 else 0.0})
    bank_df = pd.DataFrame(bank)
    # A cash deposit is money IN: always a credit, never a debit.
    cash = bank_df["description"] == "CASH DEPOSIT"
    bank_df.loc[cash, "credit_sar"] = (bank_df.loc[cash, "credit_sar"] + bank_df.loc[cash, "debit_sar"]).round(2)
    bank_df.loc[cash, "debit_sar"] = 0.0
    bank_df = bank_df.sort_values(["date", "tx_id"]).reset_index(drop=True)
    # Running balance, starting from a stated opening balance (a real statement always has one).
    bank_df["balance_sar"] = (OPENING_BALANCE + (bank_df["credit_sar"] - bank_df["debit_sar"]).cumsum()).round(2)

    out = df.drop(columns=[c for c in ["wk"] if c in df.columns])
    out.to_excel(os.path.join(out_dir, "invoices.xlsx"), index=False)
    out.to_csv(os.path.join(out_dir, "invoices.csv"), index=False)
    bank_df.to_csv(os.path.join(out_dir, "bank_transactions.csv"), index=False)
    print(f"invoices: {len(out)} rows | bank transactions: {len(bank_df)} rows")


if __name__ == "__main__":
    main()
