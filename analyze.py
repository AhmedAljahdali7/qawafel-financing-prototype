"""Financing-readiness analysis for one applicant (fictional data).

Reads sample_data/invoices.csv (or .xlsx) and sample_data/bank_transactions.csv,
cleans them, reconciles invoices against bank credits, computes the numbers an
analyst needs for a receivables-financing decision, and writes:
  - output/dashboard.html, sales.html, cash.html  (self-contained, no internet needed)
  - output/metrics.json    (same numbers, machine readable)

Usage: python analyze.py [--invoices PATH] [--bank PATH] [--as-of YYYY-MM-DD]
"""
import argparse
import difflib
import json
import os
import re
import unicodedata
from datetime import date

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- Policy assumptions (all editable, all explained in the README) ----
ADVANCE_LOW, ADVANCE_HIGH = 0.70, 0.80   # share of eligible receivables we would advance
MAX_DAYS_PAST_DUE = 60                   # invoices older than this past due are not eligible
MAX_DEBTOR_SHARE = 0.25                  # one debtor may not exceed 25% of the eligible pool
WATCH_LATE_RATE = 0.30                   # debtors paying late more than 30% of the time: excluded
LATE_GRACE = 7                           # paying within 7 days of the due date is not counted as late
MIN_RECON_RATE = 0.85                    # below this, bank data does not support the invoices


def canon(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def load(path_inv, path_bank):
    if path_inv.endswith(".xlsx"):
        inv = pd.read_excel(path_inv)
    else:
        inv = pd.read_csv(path_inv)
    bank = pd.read_csv(path_bank)
    for c in ["invoice_date", "due_date", "paid_date"]:
        inv[c] = pd.to_datetime(inv[c], errors="coerce")
    bank["date"] = pd.to_datetime(bank["date"], errors="coerce")
    return inv, bank


def clean(inv):
    """Return cleaned invoices and a list of data quality findings."""
    flags = []
    n0 = len(inv)

    # 1. exact duplicate document numbers
    dup_mask = inv.duplicated("invoice_no", keep="first")
    flags.append(("Duplicate invoice rows removed", int(dup_mask.sum()),
                  "Same invoice number appears twice, likely exported twice. Kept first."))
    inv = inv[~dup_mask].copy()

    # 2. customer name variants -> canonical (fuzzy match to frequent names)
    counts = inv["customer"].value_counts()
    main_names = [n for n, c in counts.items() if c >= 15]
    main_canon = {canon(n): n for n in main_names}
    mapping, changed = {}, 0
    for n in counts.index:
        if n in main_names:
            mapping[n] = n
            continue
        m = difflib.get_close_matches(canon(n), list(main_canon), n=1, cutoff=0.8)
        mapping[n] = main_canon[m[0]] if m else n
        if m:
            changed += int(counts[n])
    inv["customer_raw"] = inv["customer"]
    inv["customer"] = inv["customer"].map(mapping)
    flags.append(("Customer name variants merged", changed,
                  "e.g. 'HyperBasket Market' merged into 'Hyper Basket Markets'. Would otherwise split one debtor into two."))

    # 3. missing VAT
    miss_vat = inv["vat_sar"].isna() & (inv["doc_type"] == "Invoice")
    flags.append(("Invoices with missing VAT", int(miss_vat.sum()),
                  "Total equals net. Filled using 15% so totals are comparable. Worth asking the customer."))
    inv.loc[miss_vat, "vat_sar"] = (inv.loc[miss_vat, "net_amount_sar"] * 0.15).round(2)
    inv.loc[miss_vat, "total_sar"] = (inv.loc[miss_vat, "net_amount_sar"] * 1.15).round(2)

    # 4. impossible dates
    bad_due = (inv["due_date"] < inv["invoice_date"]) & (inv["doc_type"] == "Invoice")
    flags.append(("Due date before invoice date", int(bad_due.sum()),
                  "Due date rebuilt as invoice date + the customer's usual terms."))
    terms = inv["payment_terms_days"].fillna(30).astype(int)
    inv.loc[bad_due, "due_date"] = inv.loc[bad_due, "invoice_date"] + pd.to_timedelta(terms[bad_due], unit="D")

    bad_paid = inv["paid_date"].notna() & (inv["paid_date"] < inv["invoice_date"])
    flags.append(("Paid before invoiced", int(bad_paid.sum()),
                  "Payment date impossible. Excluded from payment-speed stats, kept in totals."))
    inv["paid_date_ok"] = inv["paid_date"].where(~bad_paid)

    # 5. credit notes with positive amounts would be a sign flip error (none expected here)
    cn_pos = (inv["doc_type"] == "Credit note") & (inv["net_amount_sar"] > 0)
    flags.append(("Credit notes with positive sign", int(cn_pos.sum()), "Sign flipped to negative."))
    inv.loc[cn_pos, ["net_amount_sar", "vat_sar", "total_sar"]] *= -1

    flags.insert(0, ("Rows received", n0, "Before any cleaning."))
    return inv, flags


def reconcile(inv, bank):
    """Match paid invoices to bank credits. Returns per-invoice match info and stats."""
    cred = bank[(bank["credit_sar"] > 0) & bank["description"].str.startswith("TRANSFER FROM")].copy()
    cred["refs"] = cred["description"].apply(lambda s: set(re.findall(r"\b(\d{4})\b", s)))
    cred["used"] = False

    paid = inv[(inv["doc_type"] == "Invoice") & (inv["status"] == "Paid")].copy()
    paid["num"] = paid["invoice_no"].str[4:]
    paid["match"] = "Unmatched"
    paid["diff"] = 0.0

    # pass 1: reference in description
    ref_index = {}
    for i, r in cred.iterrows():
        for ref in r["refs"]:
            ref_index.setdefault(ref, []).append(i)
    for i, r in paid.iterrows():
        txs = ref_index.get(r["num"])
        if txs:
            tx = cred.loc[txs[0]]
            if len(tx["refs"]) == 1:
                paid.at[i, "match"] = "Reference"
                paid.at[i, "diff"] = round(tx["credit_sar"] - r["total_sar"], 2)
                cred.at[txs[0], "used"] = True
            else:
                paid.at[i, "match"] = "Batch reference"
                cred.at[txs[0], "used"] = True

    # pass 2: customer + exact amount + date within 5 days, no reference
    for i, r in paid[paid["match"] == "Unmatched"].iterrows():
        cands = cred[(~cred["used"]) & (cred["description"].apply(canon).str.contains(canon(r["customer_raw"])[:12]))
                     & ((cred["credit_sar"] - r["total_sar"]).abs() < 0.02)
                     & ((cred["date"] - r["paid_date"]).abs() <= pd.Timedelta(days=5))]
        if len(cands):
            j = cands.index[0]
            cred.at[j, "used"] = True
            paid.at[i, "match"] = "Amount and date"

    # pass 3: short-paid (within 5% under) with customer and date
    for i, r in paid[paid["match"] == "Unmatched"].iterrows():
        cands = cred[(~cred["used"]) & (cred["description"].apply(canon).str.contains(canon(r["customer_raw"])[:12]))
                     & ((cred["credit_sar"] / r["total_sar"]).between(0.94, 0.999))
                     & ((cred["date"] - r["paid_date"]).abs() <= pd.Timedelta(days=5))]
        if len(cands):
            j = cands.index[0]
            cred.at[j, "used"] = True
            paid.at[i, "match"] = "Short-paid"
            paid.at[i, "diff"] = round(cred.at[j, "credit_sar"] - r["total_sar"], 2)

    matched_val = paid.loc[paid["match"] != "Unmatched", "total_sar"].sum()
    total_val = paid["total_sar"].sum()
    unexplained = cred[~cred["used"]]
    stats = {
        "paid_invoices": int(len(paid)),
        "matched_invoices": int((paid["match"] != "Unmatched").sum()),
        "match_rate_count": float((paid["match"] != "Unmatched").mean()),
        "match_rate_value": float(matched_val / total_val) if total_val else 0.0,
        "by_method": paid["match"].value_counts().to_dict(),
        "short_paid_count": int((paid["diff"] < -0.5).sum()),
        "short_paid_value": float(-paid.loc[paid["diff"] < -0.5, "diff"].sum()),
        "unexplained_credits": int(len(unexplained)),
        "unexplained_value": float(unexplained["credit_sar"].sum()),
        "customer_credits_total_incl_vat": float(cred["credit_sar"].sum()),  # cash from customers, VAT included
        "paid_invoices_total_incl_vat": float(total_val),               # compare like with like: VAT included on both sides
        "unmatched_paid_value": float(total_val - matched_val),
    }
    return paid, stats


def bank_checks(bank):
    """Does the statement add up? Opening balance + credits - debits must equal every running balance."""
    b = bank.sort_values(["date", "tx_id"]).reset_index(drop=True)
    net = b["credit_sar"] - b["debit_sar"]
    out = {"credits_total": float(b["credit_sar"].sum()), "debits_total": float(b["debit_sar"].sum())}
    if "balance_sar" in b.columns and b["balance_sar"].notna().all():
        opening = float(b["balance_sar"].iloc[0] - net.iloc[0])
        expected = opening + net.cumsum()
        bad = int(((expected - b["balance_sar"]).abs() > 0.01).sum())
        out.update({"opening_balance": opening, "closing_balance": float(b["balance_sar"].iloc[-1]),
                    "lowest_balance": float(b["balance_sar"].min()), "balance_rows_wrong": bad,
                    "balance_check_ok": bad == 0})
    else:
        out["balance_check_ok"] = None  # statement has no running balance column
    cash = b[b["description"].str.startswith("CASH DEPOSIT")]
    out["cash_deposits_value"] = float(cash["credit_sar"].sum())
    out["cash_deposits_count"] = int(len(cash))
    out["cash_share_of_credits"] = float(cash["credit_sar"].sum() / out["credits_total"]) if out["credits_total"] else 0.0
    return out


def analyze(inv, bank, as_of):
    inv, flags = clean(inv)
    paid_rec, recon = reconcile(inv, bank)

    docs = inv.copy()
    docs["month"] = docs["invoice_date"].dt.to_period("M").astype(str)
    monthly = docs.groupby("month")["net_amount_sar"].sum().round(0)  # credit notes net off automatically
    gross = docs[docs["doc_type"] == "Invoice"]["net_amount_sar"].sum()
    credits = -docs[docs["doc_type"] == "Credit note"]["net_amount_sar"].sum()
    net_rev = gross - credits

    invs = docs[docs["doc_type"] == "Invoice"].copy()
    paid = invs[(invs["status"] == "Paid") & invs["paid_date_ok"].notna()].copy()
    paid["days_to_pay"] = (paid["paid_date_ok"] - paid["invoice_date"]).dt.days
    paid["days_late"] = (paid["paid_date_ok"] - paid["due_date"]).dt.days
    dso_paid = float(paid["days_to_pay"].mean())
    late_rate = float((paid["days_late"] > LATE_GRACE).mean())
    very_late_rate = float((paid["days_late"] > 30).mean())
    avg_days_late = float(paid.loc[paid["days_late"] > LATE_GRACE, "days_late"].mean())

    # open book and ageing as of the cut-off date
    openinv = invs[invs["status"] == "Open"].copy()
    openinv["days_past_due"] = (pd.Timestamp(as_of) - openinv["due_date"]).dt.days
    bins = [-10_000, 0, 30, 60, 90, 10_000]
    labels = ["Not yet due", "1-30 past due", "31-60 past due", "61-90 past due", "90+ past due"]
    openinv["bucket"] = pd.cut(openinv["days_past_due"], bins=bins, labels=labels)
    aging = openinv.groupby("bucket", observed=False)["total_sar"].sum().round(0)

    # customer table
    rows = []
    for c, g in invs.groupby("customer"):
        p = paid[paid["customer"] == c]
        o = openinv[openinv["customer"] == c]
        rows.append({
            "customer": c,
            "segment": g["segment"].iloc[0],
            "terms": int(g["payment_terms_days"].mode().iloc[0]),
            "revenue": float(g["net_amount_sar"].sum()),
            "invoices": int(len(g)),
            "avg_days_late": float(p.loc[p["days_late"] > LATE_GRACE, "days_late"].mean()) if (p["days_late"] > LATE_GRACE).any() else 0.0,
            "late_rate": float((p["days_late"] > LATE_GRACE).mean()) if len(p) else 0.0,
            "very_late_rate": float((p["days_late"] > 30).mean()) if len(p) else 0.0,
            "open_total": float(o["total_sar"].sum()),
            "open_overdue_60": float(o.loc[o["days_past_due"] > MAX_DAYS_PAST_DUE, "total_sar"].sum()),
        })
    cust = pd.DataFrame(rows).sort_values("revenue", ascending=False).reset_index(drop=True)
    cust["share"] = cust["revenue"] / cust["revenue"].sum()
    top5 = float(cust["share"].head(5).sum())
    hhi = float((cust["share"] ** 2).sum() * 10_000)

    def tier(r):
        if r["late_rate"] > WATCH_LATE_RATE and r["very_late_rate"] > 0.10:
            return "Watch"
        if r["late_rate"] > WATCH_LATE_RATE:
            return "Caution"
        return "Good"
    cust["tier"] = cust.apply(tier, axis=1)

    # eligible receivables and suggested limit
    oi = openinv.merge(cust[["customer", "tier"]], on="customer")
    oi["eligible"] = (oi["days_past_due"] <= MAX_DAYS_PAST_DUE) & (oi["tier"] != "Watch")
    pool = oi.loc[oi["eligible"]].groupby("customer")["total_sar"].sum()
    pool_total = float(pool.sum())
    cap = MAX_DEBTOR_SHARE * pool_total
    capped = pool.clip(upper=cap)
    # iterate once more since capping shrinks the pool
    cap2 = MAX_DEBTOR_SHARE * float(capped.sum())
    capped = capped.clip(upper=cap2)
    eligible_total = float(capped.sum())
    limit_low, limit_high = eligible_total * ADVANCE_LOW, eligible_total * ADVANCE_HIGH
    excluded = {
        "total_open": float(oi["total_sar"].sum()),
        "overdue_over_60": float(oi.loc[oi["days_past_due"] > MAX_DAYS_PAST_DUE, "total_sar"].sum()),
        "watch_debtors": float(oi.loc[(oi["tier"] == "Watch") & (oi["days_past_due"] <= MAX_DAYS_PAST_DUE), "total_sar"].sum()),
        "concentration_trim": float(pool_total - eligible_total),
    }

    # decision logic: simple, explainable rules
    checks = [
        ("Bank data supports invoices", recon["match_rate_value"] >= MIN_RECON_RATE,
         f"{recon['match_rate_value']*100:.0f}% of paid invoice value traced to bank credits (need {MIN_RECON_RATE*100:.0f}%)"),
        ("No single debtor dominates", cust["share"].iloc[0] <= MAX_DEBTOR_SHARE,
         f"Largest debtor is {cust['share'].iloc[0]*100:.0f}% of sales (limit {MAX_DEBTOR_SHARE*100:.0f}%)"),
        ("Customers mostly pay close to terms", very_late_rate <= 0.10,
         f"{very_late_rate*100:.1f}% of invoices paid more than 30 days late (limit 10%)"),
        ("Few credit notes and disputes", credits / gross <= 0.05,
         f"Credit notes are {credits/gross*100:.1f}% of sales (limit 5%)"),
        ("Overdue book is small", excluded["overdue_over_60"] <= 0.10 * excluded["total_open"],
         f"{excluded['overdue_over_60']/excluded['total_open']*100:.0f}% of open invoices are >60 days past due (limit 10%)"
         if excluded["total_open"] else "No open invoices"),
    ]
    n_fail = sum(1 for _, ok, _ in checks if not ok)
    recommendation = "Proceed" if n_fail == 0 else ("Proceed with conditions" if n_fail <= 2 else "Do not proceed yet")

    # sales trend (first 3 months vs last 3 months, excluding Ramadan noise noted in the README)
    m = monthly.tolist()
    trend = (np.mean(m[-3:]) / np.mean(m[:3]) - 1) if len(m) >= 6 else 0.0
    peak_month = monthly.idxmax()

    metrics = {
        "as_of": str(as_of),
        "period": [monthly.index.min(), monthly.index.max()],
        "gross_sales": float(gross), "credit_notes": float(credits), "net_sales": float(net_rev),  # all excluding VAT
        "credit_note_rate": float(credits / gross),
        "avg_monthly_net_sales": float(monthly.mean()),  # net of credit notes, excluding VAT
        "trend_last3_vs_first3": float(trend), "peak_month": peak_month,
        "monthly": {k: float(v) for k, v in monthly.items()},
        "dso_paid_days": dso_paid, "late_rate": late_rate, "very_late_rate": very_late_rate,
        "avg_days_late_when_late": avg_days_late,
        "top5_share": top5, "hhi": hhi, "customers": int(len(cust)),
        "aging": {str(k): float(v) for k, v in aging.items()},
        "open_total": excluded["total_open"],
        "reconciliation": recon,
        "bank": bank_checks(bank),
        "eligible_receivables": eligible_total, "pool_before_cap": pool_total, "excluded": excluded,
        "limit_low": limit_low, "limit_high": limit_high,
        "checks": [{"name": n, "ok": bool(ok), "detail": d} for n, ok, d in checks],
        "recommendation": recommendation,
        "flags": [{"issue": a, "count": b, "note": c} for a, b, c in flags],
    }
    metrics["insights"] = insights(inv, bank, paid, metrics)
    return metrics, cust, paid_rec


def insights(inv, bank, paid, metrics):
    invs = inv[inv["doc_type"] == "Invoice"]
    days = (invs["invoice_date"].max() - invs["invoice_date"].min()).days + 1
    daily_sales_incl_vat = float(invs["total_sar"].sum()) / days
    seg_share = (invs.groupby("segment")["net_amount_sar"].sum() / invs["net_amount_sar"].sum()).sort_values(ascending=False)
    seg_days = paid.groupby("segment")["days_to_pay"].mean().reindex(seg_share.index)
    late = paid[paid["days_late"] > LATE_GRACE]

    b = bank.assign(month=bank["date"].dt.to_period("M").astype(str))
    receipts = b[b["description"].str.startswith("TRANSFER FROM")].groupby("month")["credit_sar"].sum()
    # average over the analysed 12 months only: the statement runs a few days past the period end,
    # and that partial month would pull the average down and overstate the runway
    out_by_month = b.groupby("month")["debit_sar"].sum()
    out_by_month = out_by_month[(out_by_month.index >= metrics["period"][0]) & (out_by_month.index <= metrics["period"][1])]
    avg_outflow = float(out_by_month.mean())

    return {
        "ten_days_faster_cash": 10 * daily_sales_incl_vat,
        "late_value": float(late["total_sar"].sum()),
        "late_count": int(len(late)),
        "months_runway": metrics["bank"]["closing_balance"] / avg_outflow,
        "avg_monthly_outflow": avg_outflow,
        "segment_share": {k: float(v) for k, v in seg_share.items()},
        "segment_days": {k: float(v) for k, v in seg_days.items()},
        "receipts_monthly": {k: float(v) for k, v in receipts.items()},
    }


# ------------------------------- HTML dashboard -------------------------------
PAGES = [
    ("dashboard.html", "Overview"),
    ("sales.html", "Sales & customers"),
    ("cash.html", "Cash & collections"),
]

RECCLS = {"Proceed": "d-proceed", "Proceed with conditions": "d-conditions", "Do not proceed yet": "d-stop"}

CSS = """
:root{--bg:#f4f6f4;--card:#fff;--ink:#17201c;--mute:#5c6b64;--line:#dfe5e1;--accent:#1d6f5c;--nav:#0f3d33}
@media (prefers-color-scheme:dark){:root{--bg:#101714;--card:#18221e;--ink:#e8efeb;--mute:#9bb0a6;--line:#2a3a33;--nav:#0a211b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header.top{background:var(--nav);color:#e9f3ef}
header.top .inner{max-width:1120px;margin:0 auto;padding:0 20px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px 20px;min-height:58px}
.brand{font-weight:700;letter-spacing:.2px}.brand span{font-weight:400;opacity:.7;margin-left:8px;font-size:13px}
nav a{color:#cfe3da;text-decoration:none;padding:8px 12px;border-radius:8px;font-size:14px;display:inline-block}
nav a:hover{background:rgba(255,255,255,.08)}nav a.on{background:#fff;color:var(--nav);font-weight:600}
.wrap{max-width:1120px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:24px;margin:0 0 2px}h2{font-size:16px;margin:0 0 12px}
.sub{color:var(--mute);margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:16px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));margin-bottom:16px}
.two{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));margin-bottom:16px}
.two>.card{margin-bottom:0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px}
.kpi b{display:block;font-size:22px;line-height:1.2}.kpi span{color:var(--mute);font-size:12px}
.decision{border-left:6px solid var(--accent)}
.decision .big{font-size:26px;font-weight:700}
.d-proceed{border-left-color:#1d7a46}.d-proceed .big{color:#1d7a46}
.d-conditions{border-left-color:#b7791f}.d-conditions .big{color:#b7791f}
.d-stop{border-left-color:#b42318}.d-stop .big{color:#b42318}
.limit{font-size:20px;font-weight:700}
ul.checks{list-style:none;margin:10px 0 0;padding:0}
ul.checks li{padding:8px 0;border-top:1px solid var(--line)}
ul.checks li b{display:inline-block;width:68px;font-size:12px}
ul.checks li.ok b{color:#1d7a46}ul.checks li.bad b{color:#b42318}
ul.checks li span{display:block;margin-left:68px;color:var(--mute);font-size:13px}
ul.headline{margin:0;padding-left:20px}ul.headline li{margin:8px 0}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:7px 8px;border-top:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--mute);font-weight:600;border-top:none}
td.n,th.n{text-align:right;white-space:nowrap}
.scroll{overflow-x:auto}
.pill{padding:2px 8px;border-radius:99px;font-size:12px;font-weight:600}
.t-good{background:#d8f1e3;color:#14532d}.t-caution{background:#fcefc7;color:#7a4b00}.t-watch{background:#fbd9d4;color:#8a1c12}
.note{color:var(--mute);font-size:13px;margin:10px 0 0}
footer{color:var(--mute);font-size:12px;margin-top:24px;text-align:center}
@media (max-width:640px){h1{font-size:20px}.kpi b{font-size:19px}}
"""


def sar(x):
    return f"{x:,.0f}"


def bar_chart(labels, values, w=640, h=230, color="#1d6f5c", fmt=lambda v: f"{v/1000:,.0f}k", highlight=None):
    left, bottom, top = 10, 46, 18
    n = len(values)
    bw = (w - left * 2) / n
    mx = max(values) if max(values) > 0 else 1
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" style="width:100%;height:auto">']
    for i, (lab, v) in enumerate(zip(labels, values)):
        bh = (h - bottom - top) * v / mx
        x = left + i * bw + bw * 0.15
        y = h - bottom - bh
        col = highlight.get(lab, color) if highlight else color
        out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw*0.7:.1f}" height="{bh:.1f}" fill="{col}" rx="3"/>')
        out.append(f'<text x="{x+bw*0.35:.1f}" y="{y-4:.1f}" font-size="10" text-anchor="middle" fill="currentColor">{fmt(v)}</text>')
        out.append(f'<text x="{x+bw*0.35:.1f}" y="{h-bottom+14}" font-size="10" text-anchor="middle" fill="currentColor" opacity=".75">{lab}</text>')
    out.append("</svg>")
    return "".join(out)


def hbar_chart(labels, values, w=640, color="#1d6f5c"):
    rh = 26
    h = rh * len(values) + 8
    mx = max(values) if max(values) > 0 else 1
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" style="width:100%;height:auto">']
    for i, (lab, v) in enumerate(zip(labels, values)):
        y = i * rh + 4
        bwid = (w - 250) * v / mx
        out.append(f'<text x="0" y="{y+16}" font-size="12" fill="currentColor">{lab}</text>')
        out.append(f'<rect x="190" y="{y+3}" width="{bwid:.1f}" height="16" fill="{color}" rx="3"/>')
        out.append(f'<text x="{190+bwid+6:.1f}" y="{y+16}" font-size="12" fill="currentColor">{v*100:.1f}%</text>')
    out.append("</svg>")
    return "".join(out)


def kpi(value, label):
    return f'<div class="kpi"><b>{value}</b><span>{label}</span></div>'


def shell(title, active, body, m):
    nav = "".join(
        '<a href="%s"%s>%s</a>' % (f, ' class="on"' if f == active else "", label)
        for f, label in PAGES)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} | Al Waha Foods (fictional)</title><style>{CSS}</style></head><body>
<header class="top"><div class="inner"><div class="brand">Al Waha Foods Trading<span>Financing readiness</span></div><nav>{nav}</nav></div></header>
<main class="wrap">
{body}
<footer>Fictional data. Period {m['period'][0]} to {m['period'][1]}. Open invoices as of {m['as_of']}. Generated by analyze.py.</footer>
</main></body></html>"""


def headlines(m):
    ins = m["insights"]
    top_seg, top_share = next(iter(ins["segment_share"].items()))
    return [
        f"Collecting just 10 days faster would release about <b>SAR {sar(ins['ten_days_faster_cash'])}</b> of cash.",
        f"Closing bank balance covers <b>{ins['months_runway']:.1f} months</b> of average outflows (SAR {sar(ins['avg_monthly_outflow'])} a month).",
        f"<b>SAR {sar(ins['late_value'])}</b> of invoices ({ins['late_count']} invoices) were paid more than {LATE_GRACE} days late.",
        f"<b>{top_seg}</b> is the largest segment at {top_share*100:.0f}% of net sales.",
        f"Customer transfers cover <b>{m['reconciliation']['match_rate_value']*100:.0f}%</b> of paid invoice value, with <b>SAR {sar(m['reconciliation']['unexplained_value'])}</b> of transfers still unexplained.",
    ]


def page_overview(m, cust):
    rec = m["recommendation"]
    checks_html = "".join(
        f'<li class="{"ok" if c["ok"] else "bad"}"><b>{"PASS" if c["ok"] else "REVIEW"}</b> {c["name"]}'
        f'<span>{c["detail"]}</span></li>' for c in m["checks"])
    ex = m["excluded"]
    limit_rows = f"""<table>
<tr><td>Open invoices</td><td class="n">{sar(ex['total_open'])}</td></tr>
<tr><td>less: more than {MAX_DAYS_PAST_DUE} days past due</td><td class="n">-{sar(ex['overdue_over_60'])}</td></tr>
<tr><td>less: debtors on Watch (not overdue)</td><td class="n">-{sar(ex['watch_debtors'])}</td></tr>
<tr><td>less: trim so no debtor is above {MAX_DEBTOR_SHARE*100:.0f}% of pool</td><td class="n">-{sar(ex['concentration_trim'])}</td></tr>
<tr><td><b>Eligible receivables</b></td><td class="n"><b>{sar(m['eligible_receivables'])}</b></td></tr>
<tr><td>Advance at {ADVANCE_LOW*100:.0f}% to {ADVANCE_HIGH*100:.0f}%</td><td class="n"><b>{sar(m['limit_low'])} to {sar(m['limit_high'])}</b></td></tr></table>"""
    heads = "".join(f"<li>{h}</li>" for h in headlines(m))
    kpis = "".join([
        kpi(f"SAR {m['net_sales']/1e6:.2f}M", "Net sales, 12 months"),
        kpi(f"{m['dso_paid_days']:.0f} days", "Average invoice-to-payment time"),
        kpi(f"{m['late_rate']*100:.0f}%", f"Invoices paid over {LATE_GRACE} days late"),
        kpi(f"{m['top5_share']*100:.0f}%", "Sales from top 5 customers"),
        kpi(f"{m['reconciliation']['match_rate_value']*100:.0f}%", "Paid value traced to bank"),
        kpi(f"SAR {sar(m['open_total'])}", "Open invoices today"),
    ])
    body = f"""
<h1>Overview</h1>
<p class="sub">Suggested decision, facility size and the headline numbers behind them.</p>
<section class="card decision {RECCLS[rec]}">
  <h2>Suggested decision</h2>
  <div class="big">{rec}</div>
  <p>Suggested starting facility: <span class="limit">SAR {sar(m['limit_low'])} to {sar(m['limit_high'])}</span></p>
  <ul class="checks">{checks_html}</ul>
  <p class="note">This is decision support, not a decision. An analyst should confirm the REVIEW items and the data flags before an offer goes to the bank.</p>
</section>
<div class="grid">{kpis}</div>
<div class="two">
  <section class="card"><h2>What stands out</h2><ul class="headline">{heads}</ul></section>
  <section class="card"><h2>How the limit is worked out</h2>{limit_rows}</section>
</div>""" + controls_section(m)
    return shell("Overview", "dashboard.html", body, m)


def page_sales(m, cust):
    monthly = m["monthly"]
    mlabels = [pd.Period(x).strftime("%b %y") for x in monthly]
    monthly_svg = bar_chart(mlabels, list(monthly.values()), highlight={"Mar 26": "#c9892b", "Feb 26": "#c9892b"})
    ins = m["insights"]
    seg_svg = hbar_chart(list(ins["segment_share"].keys()), list(ins["segment_share"].values()), color="#7a5ea8")
    top = cust.head(8)
    conc_svg = hbar_chart(list(top["customer"]), list(top["share"]), color="#3a6ea5")
    tier_cls = {"Good": "t-good", "Caution": "t-caution", "Watch": "t-watch"}
    cust_rows = "".join(
        f'<tr><td>{r.customer}</td><td>{r.segment}</td><td class="n">{r.terms}</td>'
        f'<td class="n">{sar(r.revenue)}</td><td class="n">{r.share*100:.1f}%</td>'
        f'<td class="n">{r.late_rate*100:.0f}%</td><td class="n">{r.avg_days_late:.0f}</td>'
        f'<td class="n">{sar(r.open_total)}</td><td><span class="pill {tier_cls[r.tier]}">{r.tier}</span></td></tr>'
        for r in cust.itertuples())
    body = f"""
<h1>Sales &amp; customers</h1>
<p class="sub">How sales are built up over the year, who buys, and how concentrated the book is.</p>
<section class="card"><h2>Monthly net sales (SAR)</h2>{monthly_svg}
<p class="note">Orange bars are the Ramadan build-up (Feb) and peak (Mar 2026). The last 3 months are {m['trend_last3_vs_first3']*100:+.0f}% versus the first 3. Only 12 months are available, so this cannot yet be separated from normal seasonality. Ask for a second year before reading it as a trend.</p></section>
<div class="two">
  <section class="card"><h2>Net sales mix by segment</h2>{seg_svg}
  <p class="note">Share of net sales over the 12 months.</p></section>
  <section class="card"><h2>Customer concentration (share of sales)</h2>{conc_svg}
  <p class="note">{m['customers']} active customers. Concentration index (HHI) {m['hhi']:.0f}; below 1,500 is generally considered unconcentrated.</p></section>
</div>
<section class="card"><h2>Customers</h2><div class="scroll"><table>
<tr><th>Customer</th><th>Segment</th><th class="n">Terms</th><th class="n">Sales</th><th class="n">Share</th><th class="n">Paid late</th><th class="n">Avg days late</th><th class="n">Open (SAR)</th><th>Tier</th></tr>
{cust_rows}</table></div>
<p class="note">Tier (late = more than {LATE_GRACE} days after due): Watch = pays late over {WATCH_LATE_RATE*100:.0f}% of the time and over 10% more than 30 days late. Caution = late over {WATCH_LATE_RATE*100:.0f}%. Good = everyone else.</p></section>"""
    return shell("Sales & customers", "sales.html", body, m)


def page_cash(m, cust):
    ins = m["insights"]
    bk = m["bank"]
    rec_months = list(ins["receipts_monthly"].keys())
    receipts_svg = bar_chart([pd.Period(x).strftime("%b %y") for x in rec_months],
                             list(ins["receipts_monthly"].values()), color="#3a6ea5")
    aging_cols = {"Not yet due": "#1d6f5c", "1-30 past due": "#7aa84a", "31-60 past due": "#d6a419",
                  "61-90 past due": "#d9772b", "90+ past due": "#b42318"}
    aging_svg = bar_chart(list(m["aging"].keys()), list(m["aging"].values()), highlight=aging_cols)
    seg_rows = "".join(
        f'<tr><td>{seg}</td><td class="n">{share*100:.0f}%</td><td class="n">{ins["segment_days"][seg]:.0f}</td></tr>'
        for seg, share in ins["segment_share"].items())
    kpis = "".join([
        kpi(f"{ins['months_runway']:.1f} mo", "Cash runway at average outflows"),
        kpi(f"SAR {ins['ten_days_faster_cash']/1e6:.2f}M", "Cash freed by 10 days faster collection"),
        kpi(f"SAR {ins['late_value']/1e6:.2f}M", f"Value paid late ({ins['late_count']} invoices)"),
        kpi(f"{bk['cash_share_of_credits']*100:.0f}%", "Credits that are cash deposits"),
    ])
    body = f"""
<h1>Cash &amp; collections</h1>
<p class="sub">How quickly cash comes in, what it is worth to speed it up, and what is overdue.</p>
<div class="grid">{kpis}</div>
<div class="two">
  <section class="card"><h2>Customer receipts by month (SAR)</h2>{receipts_svg}
  <p class="note">Money received from customer bank transfers only; cash deposits are excluded. The first and last months are partial: payments only start arriving after the first invoices fall due, and the statement stops in mid-October.</p></section>
  <section class="card"><h2>Open invoices by age (SAR)</h2>{aging_svg}
  <p class="note">Only invoices up to {MAX_DAYS_PAST_DUE} days past due, from debtors not on Watch, count toward the facility.</p></section>
</div>
<section class="card"><h2>Payment speed by segment</h2><div class="scroll"><table>
<tr><th>Segment</th><th class="n">Share of sales</th><th class="n">Avg days to pay</th></tr>{seg_rows}</table></div></section>
<section class="card"><h2>Cash deposits</h2>
<p>Cash deposits are not customer transfers, so they cannot be traced to an invoice: {bk['cash_deposits_count']} deposits worth SAR {sar(bk['cash_deposits_value'])} ({bk['cash_share_of_credits']*100:.0f}% of all credits). The customer should explain where this cash comes from (for example counter sales or cash customers).</p></section>"""
    return shell("Cash & collections", "cash.html", body, m)


def controls_section(m):
    r = m["reconciliation"]
    bk = m["bank"]
    if bk["balance_check_ok"] is None:
        bank_line = "The statement has no running balance column, so it could not be checked for completeness."
    elif bk["balance_check_ok"]:
        bank_line = (f"<b>Balance check passed.</b> Opening balance SAR {sar(bk['opening_balance'])} plus credits SAR {sar(bk['credits_total'])} minus debits SAR {sar(bk['debits_total'])} "
                     f"gives closing balance SAR {sar(bk['closing_balance'])}, and every running balance agrees. Lowest balance in the year: SAR {sar(bk['lowest_balance'])}.")
    else:
        bank_line = (f"<b>Balance check FAILED.</b> {bk['balance_rows_wrong']} rows do not follow from the previous balance. "
                     "A page may be missing or a row was edited. Ask for the statement again before using it.")
    method_rows = "".join(f'<tr><td>{k}</td><td class="n">{v}</td></tr>' for k, v in r["by_method"].items())
    flags_html = "".join(
        f'<tr><td>{f["issue"]}</td><td class="n">{f["count"]}</td><td>{f["note"]}</td></tr>' for f in m["flags"])
    body = f"""
<h1 style="margin-top:32px;font-size:20px">Reconciliation &amp; data quality</h1>
<p class="sub">Whether the invoices and the bank statement agree, and what the cleaning step changed.</p>
<div class="two">
  <section class="card"><h2>Invoice to bank reconciliation</h2>
  <p>{r['matched_invoices']} of {r['paid_invoices']} paid invoices ({r['match_rate_count']*100:.0f}%) were traced to a bank credit, covering {r['match_rate_value']*100:.0f}% of paid value.</p>
  <table><tr><th>Method</th><th class="n">Invoices</th></tr>{method_rows}</table>
  <p>{r['short_paid_count']} payments arrived short by a total of SAR {sar(r['short_paid_value'])} (disputes or bank charges).
  SAR {sar(r['unmatched_paid_value'])} of invoices marked paid could not be found in the statements, and {r['unexplained_credits']} customer transfers worth SAR {sar(r['unexplained_value'])} do not map to any invoice. These are the first things to ask the customer about.</p></section>
  <section class="card"><h2>Bank statement checks</h2><p>{bank_line}</p></section>
</div>
<section class="card"><h2>Data quality findings (what the cleaning step fixed or flagged)</h2><div class="scroll"><table>
<tr><th>Check</th><th class="n">Count</th><th>What we did</th></tr>{flags_html}</table></div></section>"""
    return body


def build_pages(m, cust):
    return {
        "dashboard.html": page_overview(m, cust),
        "sales.html": page_sales(m, cust),
        "cash.html": page_cash(m, cust),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--invoices", default=os.path.join(HERE, "sample_data", "invoices.csv"))
    ap.add_argument("--bank", default=os.path.join(HERE, "sample_data", "bank_transactions.csv"))
    ap.add_argument("--as-of", default="2026-10-15")
    ap.add_argument("--out", default=os.path.join(HERE, "output"))
    a = ap.parse_args()

    inv, bank = load(a.invoices, a.bank)
    metrics, cust, _ = analyze(inv, bank, date.fromisoformat(a.as_of))
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "metrics.json"), "w") as f:
        json.dump(metrics, f, indent=2, default=str)
    for name, html in build_pages(metrics, cust).items():
        with open(os.path.join(a.out, name), "w", encoding="utf-8") as f:
            f.write(html)
    print(f"Recommendation: {metrics['recommendation']}")
    print(f"Limit: SAR {metrics['limit_low']:,.0f} to {metrics['limit_high']:,.0f}")
    for c in metrics["checks"]:
        print(("PASS   " if c["ok"] else "REVIEW ") + c["name"] + " | " + c["detail"])
    print("Wrote", ", ".join(os.path.join(a.out, p) for p, _ in PAGES))


if __name__ == "__main__":
    main()
