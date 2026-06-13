from pathlib import Path
import json
import csv
import calendar
from datetime import date, timedelta
from dataclasses import dataclass

@dataclass
class Debt:
    name: str
    balance: float
    apr: float
    minimum: float
    due_day: int

@dataclass
class Bill:
    name: str
    amount: float
    due_day: int

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
    rows = list(csv.DictReader(path.open(newline="")))
    newest = max(rows, key=lambda row: datetime.strptime(row["date"].strip(), "%Y-%m-%d"))
    return _to_float(newest["newbalance"])

def next_payday(today: date, anchor: date, interval_days: int) -> date:
    delta = (today - anchor).days
    n = delta // interval_days + 1
    return anchor + timedelta(days=n * interval_days)

def next_due_date(today: date, due_day: int) -> date:
    def clamp(year: int, month: int, day: int) -> date:
        last = calendar.monthrange(year, month)[1]
        return date(year, month, min(day, last))

    this_month = clamp(today.year, today.month, due_day)
    if this_month >= today:
        return this_month
    if today.month == 12:
        return clamp(today.year + 1, 1, due_day)
    return clamp(today.year, today.month + 1, due_day)

def _target_debt(active: list[Debt], strategy: str) -> Debt:
    if strategy == "avalanche":
        return max(active, key=lambda d: d.apr)
    if strategy == "snowball":
        return min(active, key=lambda d: d.balance)
    raise ValueError(f"Unknown strategy: {strategy!r}")

def simulate_payoff(debts: list[Debt], monthly_payment: float, strategy: str, max_months: int = 1200) -> dict:
    active = [d for d in debts]
    total_interest, months = 0.0, 0
    if monthly_payment < sum(d.minimum for d in active):
        return {"strategy": strategy, "months": None, "total_interest": None,
                "feasible": False, "note": "Monthly payment below sum of minimums."}
    while active and months < max_months:
        months += 1
        for d in active:
            interest = d.balance * d.apr / 12.0
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
               strategy: str, extra_payment: float = float("inf")) -> dict:
    obligations = []
    for d in debts:
        if d.balance <= 0:
            continue
        due = next_due_date(today, d.due_day)
        if today <= due < payday:
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

    recommended_extra = min(extra_payment, max(safe_today, 0.0))
    extra_affordable = (extra_payment == float("inf")) or (safe_today >= extra_payment)

    extra = []
    remaining = recommended_extra
    pool = [d for d in debts if d.balance > 0]
    while remaining > 0.005 and any(d.balance > 0 for d in pool):
        t = _target_debt([d for d in pool if d.balance > 0], strategy)
        pay = min(remaining, t.balance)
        extra.append({"name": t.name, "amount": round(pay, 2)})
        t.balance -= pay
        remaining -= pay

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
        "recommended_extra": round(recommended_extra, 2),
        "extra_payment": None if extra_payment == float("inf") else round(extra_payment, 2),
        "extra_affordable": extra_affordable,
        "payday": payday,
        "paycheck_amount": round(paycheck_amount, 2),
        "projected_after_payday": round(projected_after_payday, 2),
        "strategy": strategy,
    }
