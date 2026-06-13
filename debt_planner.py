"""
debt_planner_v2.py  --  Cash-flow-aware daily debt planner (v2)
===============================================================

WHAT CHANGED FROM v1
--------------------
v1 thought in monthly lumps: it asked "can my balance cover the whole
monthly payment right now?" That fails when you're paid biweekly and bills
land throughout the month -- you never hold a whole month's debt money at once.

v2 is meant to be run DAILY. Each day it asks a cash-flow question instead:

    "Given today's balance, my safety buffer, and everything due BEFORE my
     next paycheck, how much can I safely send to a debt right now -- and
     which debt?"

It is still 100% READ-ONLY. It moves no money. It only recommends.

THREE NEW INGREDIENTS
---------------------
1. A paycheck schedule (an anchor payday + a 14-day interval) so it knows
   when your next inflow arrives.
2. A due_day on every debt, plus a separate bills.json for recurring
   non-debt expenses (rent, utilities, phone...) with their own due days.
3. A daily "safe to spend" number:

       safe_today = balance - buffer - (everything due before next payday)

   Anything above that line is genuinely free to throw at debt today.
   Anything below it must stay put so you can cover what's coming.

THE STATE INSIGHT
-----------------
Because this re-reads your real balance from the CSV every run, the bank
balance itself is the memory. If you acted on yesterday's recommendation,
today's balance is already lower and today's recommendation adjusts on its
own. No separate "what did I pay" file required.

HOW TO RUN (Windows / PowerShell, Python 3.12)
----------------------------------------------
  python debt_planner_v2.py

First run writes example files: debts.json, bills.json, config.json,
bank_sample.csv. Replace them with your real data and run again. For LIVE
daily use, set "today": null in config.json so it uses the real current date.
Standard library only -- no pip installs.
"""

from __future__ import annotations

import calendar
import csv
import json
import urllib.request
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path


# ----------------------------------------------------------------------------
# 1. DATA MODEL
# ----------------------------------------------------------------------------

@dataclass
class Debt:
    name: str
    balance: float
    apr: float          # annual rate as decimal, e.g. 0.2249 == 22.49%
    minimum: float
    due_day: int        # NEW in v2: day of month the minimum is due (1-31)

    @property
    def monthly_rate(self) -> float:
        return self.apr / 12.0


@dataclass
class Bill:
    name: str
    amount: float
    due_day: int        # day of month this recurring bill is due


# ----------------------------------------------------------------------------
# 2. LOADING (the "Ledger")
# ----------------------------------------------------------------------------

def load_debts(path: Path) -> list[Debt]:
    return [Debt(**e) for e in json.loads(path.read_text())]


def load_bills(path: Path) -> list[Bill]:
    return [Bill(**e) for e in json.loads(path.read_text())]


def load_config(path: Path) -> dict:
    return json.loads(path.read_text())


def _to_float(value: str) -> float:
    s = value.strip().replace("$", "").replace(",", "")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    return float(s)


def load_balance_from_csv(path: Path) -> float:
    """Return the newest row's newbalance from a Statewide FCU-style export."""
    rows = list(csv.DictReader(path.open(newline="")))
    if not rows:
        raise ValueError(f"{path} has no data rows.")
    fmts = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y")

    def parse(row):
        for fmt in fmts:
            try:
                return datetime.strptime(row["date"].strip(), fmt)
            except (ValueError, KeyError):
                continue
        return None

    newest = max(rows, key=parse) if all(parse(r) for r in rows) else rows[-1]
    return _to_float(newest["newbalance"])


# ----------------------------------------------------------------------------
# 3. CALENDAR MATH -- the genuinely new logic in v2
# ----------------------------------------------------------------------------

def next_payday(today: date, anchor: date, interval_days: int) -> date:
    """
    Paydays are anchor + n*interval for every integer n. Return the first one
    STRICTLY AFTER today. Python's // floors toward negative infinity, so this
    formula works whether the anchor is in the past or the future.
    """
    delta = (today - anchor).days
    n = delta // interval_days + 1
    return anchor + timedelta(days=n * interval_days)


def next_due_date(today: date, due_day: int) -> date:
    """
    Find the next calendar date matching due_day, on or after today.
    Clamps to the last day of the month (so due_day=31 becomes Feb 28/29 etc).
    """
    def clamp(year: int, month: int, day: int) -> date:
        last = calendar.monthrange(year, month)[1]
        return date(year, month, min(day, last))

    this_month = clamp(today.year, today.month, due_day)
    if this_month >= today:
        return this_month
    if today.month == 12:
        return clamp(today.year + 1, 1, due_day)
    return clamp(today.year, today.month + 1, due_day)


# ----------------------------------------------------------------------------
# 4. THE PLANNER
# ----------------------------------------------------------------------------

def _target_debt(active: list[Debt], strategy: str) -> Debt:
    """avalanche -> highest APR; snowball -> smallest balance. The only diff."""
    if strategy == "avalanche":
        return max(active, key=lambda d: d.apr)
    if strategy == "snowball":
        return min(active, key=lambda d: d.balance)
    raise ValueError(f"Unknown strategy: {strategy!r}")


def simulate_payoff(debts: list[Debt], monthly_payment: float, strategy: str,
                    max_months: int = 1200) -> dict:
    """
    Long-term projection (unchanged from v1): IF you sustain roughly
    monthly_payment per month, how long until debt-free and how much interest?
    This is informational -- the daily section below is what's actionable.
    """
    active = [replace(d) for d in debts]
    total_interest, months = 0.0, 0
    if monthly_payment < sum(d.minimum for d in active):
        return {"strategy": strategy, "months": None, "total_interest": None,
                "feasible": False, "note": "Monthly payment below sum of minimums."}
    while active and months < max_months:
        months += 1
        for d in active:
            interest = d.balance * d.monthly_rate
            d.balance += interest
            total_interest += interest
        pool = monthly_payment
        for d in active:
            pay = min(d.minimum, d.balance)
            d.balance -= pay
            pool -= pay
        while pool > 0 and any(d.balance > 0 for d in active):
            t = _target_debt([d for d in active if d.balance > 0], strategy)
            pay = min(pool, t.balance)
            t.balance -= pay
            pool -= pay
        active = [d for d in active if d.balance > 0.005]
    return {"strategy": strategy, "months": months,
            "total_interest": round(total_interest, 2),
            "feasible": active == [],
            "note": "" if not active else f"Not cleared within {max_months} months."}


def plan_today(debts: list[Debt], bills: list[Bill], balance: float, buffer: float,
               today: date, payday: date, paycheck_amount: float,
               strategy: str) -> dict:
    """
    THE DAILY DECISION.

    1. Find every obligation (debt minimum or bill) due between today and the
       next payday. That money is spoken for -- reserve it.
    2. safe_today = balance - buffer - reserved obligations.
    3. If safe_today > 0, that surplus can go to debt right now, poured onto
       the avalanche/snowball target (cascading to the next debt if the target
       gets cleared).
    4. Flag anything due *today* so you don't miss it.
    5. If safe_today <= 0, recommend holding and report when funds free up.
    """
    obligations = []
    for d in debts:
        if d.balance <= 0:
            continue
        due = next_due_date(today, d.due_day)
        if today <= due < payday:                      # due before next inflow
            obligations.append({"name": d.name, "amount": min(d.minimum, d.balance),
                                "due": due, "kind": "minimum"})
    for b in bills:
        due = next_due_date(today, b.due_day)
        if today <= due < payday:
            obligations.append({"name": b.name, "amount": b.amount,
                                "due": due, "kind": "bill"})

    obligations.sort(key=lambda o: o["due"])
    reserved = sum(o["amount"] for o in obligations)
    safe_today = balance - buffer - reserved
    due_today = [o for o in obligations if o["due"] == today]

    # Cascade any safe surplus onto debt targets (same logic as the simulation).
    extra = []
    remaining = max(safe_today, 0.0)
    pool = [replace(d) for d in debts if d.balance > 0]
    while remaining > 0.005 and any(d.balance > 0 for d in pool):
        t = _target_debt([d for d in pool if d.balance > 0], strategy)
        pay = min(remaining, t.balance)
        extra.append({"name": t.name, "amount": round(pay, 2)})
        t.balance -= pay
        remaining -= pay

    # What spendable cash looks like right after the next paycheck lands.
    projected_after_payday = balance - reserved + paycheck_amount - buffer

    return {
        "today": today,
        "balance": round(balance, 2),
        "buffer": round(buffer, 2),
        "obligations": obligations,
        "reserved": round(reserved, 2),
        "safe_today": round(safe_today, 2),
        "due_today": due_today,
        "extra": extra,
        "payday": payday,
        "paycheck_amount": round(paycheck_amount, 2),
        "projected_after_payday": round(projected_after_payday, 2),
        "strategy": strategy,
    }


# ----------------------------------------------------------------------------
# 5. NARRATOR (optional local LLM -- explains, never computes)
# ----------------------------------------------------------------------------

def narrate(summary: str, model: str) -> str | None:
    prompt = ("You are a blunt, friendly financial assistant. In 3-4 sentences, "
              "explain today's recommendation in plain English. Do not invent "
              "numbers; only use what is given:\n\n" + summary)
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
    try:
        req = urllib.request.Request("http://localhost:11434/api/generate",
                                     data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())["response"].strip()
    except Exception:
        return None


# ----------------------------------------------------------------------------
# 6. ORCHESTRATION
# ----------------------------------------------------------------------------

def money(x: float) -> str:
    return f"${x:,.2f}"


def resolve_today(config: dict) -> date:
    """Use config['today'] if set (handy for testing); otherwise the real date."""
    raw = config.get("today")
    return datetime.strptime(raw, "%Y-%m-%d").date() if raw else date.today()


def main() -> None:
    here = Path(__file__).parent
    paths = {
        "debts": here / "debts.json",
        "bills": here / "bills.json",
        "config": here / "config.json",
        "csv": here / "bank_sample.csv",
    }
    created = _write_examples_if_missing(paths)
    if created:
        print("Created example files:", ", ".join(created))
        print("Edit them with your real data, then run again.\n")

    debts = load_debts(paths["debts"])
    bills = load_bills(paths["bills"])
    config = load_config(paths["config"])
    balance = load_balance_from_csv(paths["csv"])

    today = resolve_today(config)
    anchor = datetime.strptime(config["paycheck_anchor"], "%Y-%m-%d").date()
    payday = next_payday(today, anchor, config["paycheck_interval_days"])
    strategy = config["strategy"]

    plan = plan_today(debts, bills, balance, config["buffer"], today, payday,
                      config["paycheck_amount"], strategy)

    print("=" * 64)
    print(f"  DAILY DEBT PLAN for {today:%A, %B %d, %Y}  (read-only)")
    print("=" * 64)
    print(f"Current balance:   {money(plan['balance'])}")
    print(f"Safety buffer:     {money(plan['buffer'])}")
    print(f"Next paycheck:     {payday:%b %d} ({money(plan['paycheck_amount'])})\n")

    print("Reserved before next paycheck")
    print("-" * 64)
    if plan["obligations"]:
        for o in plan["obligations"]:
            tag = "  <-- DUE TODAY" if o in plan["due_today"] else ""
            print(f"  {o['due']:%b %d}  {o['name']:<14} {money(o['amount'])} "
                  f"({o['kind']}){tag}")
    else:
        print("  nothing due before the next paycheck")
    print(f"  {'reserved total':<20} {money(plan['reserved'])}")
    print(f"\nSafe to spend today: {money(plan['balance'])} - {money(plan['buffer'])} "
          f"buffer - {money(plan['reserved'])} reserved = {money(plan['safe_today'])}\n")

    print(f"Recommendation ({strategy})")
    print("-" * 64)
    if plan["due_today"]:
        for o in plan["due_today"]:
            print(f"  PAY TODAY: {o['name']} {money(o['amount'])} (due today)")
    if plan["extra"]:
        for e in plan["extra"]:
            print(f"  SEND EXTRA: up to {money(e['amount'])} to {e['name']} "
                  f"(your {strategy} target)")
    elif plan["safe_today"] <= 0:
        print(f"  HOLD -- no safe surplus today. Funds free up after your")
        print(f"  next paycheck on {payday:%b %d}; projected spendable then "
              f"~ {money(plan['projected_after_payday'])}.")
    print()

    # Informational long-term projection.
    avalanche = simulate_payoff(debts, config["monthly_payment"], "avalanche")
    snowball = simulate_payoff(debts, config["monthly_payment"], "snowball")
    print(f"Long-term projection (assumes ~{money(config['monthly_payment'])}/mo)")
    print("-" * 64)
    for r in (avalanche, snowball):
        if r["feasible"]:
            print(f"  {r['strategy']:<10} debt-free in {r['months']:>3} months "
                  f"| interest {money(r['total_interest'])}")
        else:
            print(f"  {r['strategy']:<10} {r['note']}")
    print()

    summary = _build_summary(plan, avalanche, snowball)
    story = narrate(summary, config.get("llm_model", "llama3.2"))
    print("Plain-English summary")
    print("-" * 64)
    print(story if story else summary)


def _build_summary(plan, avalanche, snowball) -> str:
    parts = [f"Today is {plan['today']:%B %d}.",
             f"Balance {money(plan['balance'])}, buffer {money(plan['buffer'])}, "
             f"{money(plan['reserved'])} reserved for bills/minimums due before "
             f"the next paycheck on {plan['payday']:%B %d}.",
             f"Safe to spend today: {money(plan['safe_today'])}."]
    if plan["due_today"]:
        parts.append("Due today: " +
                     ", ".join(f"{o['name']} {money(o['amount'])}" for o in plan["due_today"]) + ".")
    if plan["extra"]:
        parts.append("Recommended extra: " +
                     ", ".join(f"{money(e['amount'])} to {e['name']}" for e in plan["extra"]) + ".")
    elif plan["safe_today"] <= 0:
        parts.append(f"No safe surplus today; hold until {plan['payday']:%B %d} "
                     f"(projected spendable then ~{money(plan['projected_after_payday'])}).")
    return " ".join(parts)


# ----------------------------------------------------------------------------
# 7. EXAMPLE DATA
# ----------------------------------------------------------------------------

def _write_examples_if_missing(paths: dict) -> list[str]:
    created = []

    if not paths["debts"].exists():
        paths["debts"].write_text(json.dumps([
            {"name": "Store Card",    "balance": 900.0,   "apr": 0.2699, "minimum": 35.0,  "due_day": 15},
            {"name": "Visa",          "balance": 4200.0,  "apr": 0.2249, "minimum": 105.0, "due_day": 22},
            {"name": "Personal Loan", "balance": 3000.0,  "apr": 0.1199, "minimum": 150.0, "due_day": 5},
            {"name": "Car Loan",      "balance": 11800.0, "apr": 0.0649, "minimum": 312.0, "due_day": 18},
        ], indent=2))
        created.append(paths["debts"].name)

    if not paths["bills"].exists():
        paths["bills"].write_text(json.dumps([
            {"name": "Rent",      "amount": 950.0, "due_day": 1},
            {"name": "Utilities", "amount": 180.0, "due_day": 12},
            {"name": "Phone",     "amount": 75.0,  "due_day": 20},
            {"name": "Internet",  "amount": 60.0,  "due_day": 24},
        ], indent=2))
        created.append(paths["bills"].name)

    if not paths["config"].exists():
        paths["config"].write_text(json.dumps({
            "monthly_payment": 1200.0,
            "buffer": 500.0,
            "strategy": "avalanche",
            "llm_model": "llama3.2",
            "paycheck_amount": 1850.0,
            "paycheck_anchor": "2025-06-06",
            "paycheck_interval_days": 14,
            "today": "2025-06-12"
        }, indent=2))
        created.append(paths["config"].name)

    if not paths["csv"].exists():
        rows = [
            ["amount", "date", "description", "newbalance", "type"],
            ["-45.20", "2025-06-08", "Grocery Store",  "1475.20", "debit"],
            ["-62.10", "2025-06-09", "Gas Station",    "1413.10", "debit"],
            ["-63.10", "2025-06-10", "Pharmacy",       "1350.00", "debit"],
        ]
        with paths["csv"].open("w", newline="") as f:
            csv.writer(f).writerows(rows)
        created.append(paths["csv"].name)

    return created


if __name__ == "__main__":
    main()