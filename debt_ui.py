"""
debt_ui.py  --  Friendly local web UI for the debt planner (v2: editable)
=========================================================================

WHAT'S NEW
----------
- Add a bill or a debt straight from the page (no editing JSON by hand), so
  this is usable by anyone in the household.
- A Settings panel sets your strategy, buffer, and -- the important one -- a
  DEFINED "extra payment". The recommendation now tells you to send exactly
  that amount to the target debt, capped by what's safe, instead of "throw
  every spare dollar at it."
- These forms WRITE to bills.json / debts.json / config.json (your own local
  files). Writes are atomic (temp file + replace) so a crash can't corrupt them.

  python debt_ui.py            -> serves http://127.0.0.1:8765/
  python debt_ui.py --tailscale -> binds to your Tailscale IP for phone/laptop

SECURITY
--------
Binds to localhost by default. --tailscale binds to your tailnet IP so only
your own signed-in devices can reach it (and not the rest of your LAN). It has
no password, so do NOT bind it to 0.0.0.0.

RATE LIMIT
----------
The bank balance is cached in memory and only re-fetched by the "Refresh"
button. Editing bills/debts/settings recomputes locally and never re-hits the
bank.

Standard library only.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import debt_planner_v2 as planner
import simplefin

HOST = "100.89.183.102"
PORT = 8765

HERE = Path(__file__).parent
DEBTS, BILLS, CONFIG, CSV = (HERE / n for n in
                             ("debts.json", "bills.json", "config.json", "bank_sample.csv"))

_cache: dict = {"balance": None, "fetched_at": None, "source": None}
m = planner.money


# ----------------------------------------------------------------------------
# SAFE FILE WRITES + VALIDATION
# ----------------------------------------------------------------------------

def _write_json(path: Path, data) -> None:
    """Atomic write: dump to a temp file, then replace -- never a half-written file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)            # atomic on the same filesystem


def _name(v) -> str:
    s = str(v or "").strip()
    if not s:
        raise ValueError("Name is required.")
    return s


def _num(v, label: str, minv: float | None = None) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.")
    if minv is not None and x < minv:
        raise ValueError(f"{label} must be {minv} or more.")
    return round(x, 2)


def _day(v) -> int:
    try:
        d = int(v)
    except (TypeError, ValueError):
        raise ValueError("Due day must be a whole number.")
    if not 1 <= d <= 31:
        raise ValueError("Due day must be between 1 and 31.")
    return d


def add_bill(body: dict) -> None:
    bill = {"name": _name(body.get("name")),
            "amount": _num(body.get("amount"), "Amount", minv=0),
            "due_day": _day(body.get("due_day"))}
    bills = json.loads(BILLS.read_text()) if BILLS.exists() else []
    bills.append(bill)
    _write_json(BILLS, bills)


def add_debt(body: dict) -> None:
    # APR is entered as a human percentage (22.49) and stored as a decimal (0.2249).
    apr_percent = _num(body.get("apr"), "APR", minv=0)
    debt = {"name": _name(body.get("name")),
            "balance": _num(body.get("balance"), "Balance", minv=0),
            "apr": round(apr_percent / 100.0, 6),
            "minimum": _num(body.get("minimum"), "Minimum payment", minv=0),
            "due_day": _day(body.get("due_day"))}
    debts = json.loads(DEBTS.read_text()) if DEBTS.exists() else []
    debts.append(debt)
    _write_json(DEBTS, debts)


def save_settings(body: dict) -> None:
    strategy = str(body.get("strategy", "")).strip()
    if strategy not in ("avalanche", "snowball"):
        raise ValueError("Strategy must be avalanche or snowball.")
    config = json.loads(CONFIG.read_text())
    config["strategy"] = strategy
    config["buffer"] = _num(body.get("buffer"), "Buffer", minv=0)
    config["extra_payment"] = _num(body.get("extra_payment"), "Extra payment", minv=0)
    _write_json(CONFIG, config)


def _delete_at(path: Path, body: dict, kind: str) -> None:
    """
    Remove one item by its position in the list. We also verify the name still
    matches what the page showed -- so if the list changed between the page
    rendering and the click, we refuse rather than delete the wrong row.
    """
    items = json.loads(path.read_text()) if path.exists() else []
    try:
        idx = int(body.get("index"))
    except (TypeError, ValueError):
        raise ValueError("Invalid item.")
    if not 0 <= idx < len(items):
        raise ValueError(f"That {kind} no longer exists -- refresh and try again.")
    expected = str(body.get("name", "")).strip()
    if expected and items[idx].get("name", "") != expected:
        raise ValueError("The list changed -- refresh and try again.")
    items.pop(idx)
    _write_json(path, items)


def delete_bill(body: dict) -> None:
    _delete_at(BILLS, body, "bill")


def delete_debt(body: dict) -> None:
    _delete_at(DEBTS, body, "debt")


# ----------------------------------------------------------------------------
# READ + ASSEMBLE THE PLAN
# ----------------------------------------------------------------------------

def get_balance(force: bool = False) -> tuple[float, str, str]:
    if not force and _cache["balance"] is not None:
        return _cache["balance"], _cache["fetched_at"], _cache["source"]
    config = planner.load_config(CONFIG)
    source = config.get("balance_source", "csv")
    if source == "simplefin":
        access = simplefin.get_access_url()
        if not access:
            raise RuntimeError("No SimpleFIN access URL. Run: python simplefin.py claim")
        balance = simplefin.load_balance_from_simplefin(access, config.get("simplefin_account_id"))
    else:
        balance = planner.load_balance_from_csv(CSV)
    _cache.update(balance=balance,
                  fetched_at=datetime.now().strftime("%b %d, %I:%M %p"),
                  source=source)
    return balance, _cache["fetched_at"], source


def build_payload(refresh: bool) -> dict:
    config = planner.load_config(CONFIG)
    debts = planner.load_debts(DEBTS)
    bills = planner.load_bills(BILLS)
    balance, fetched_at, source = get_balance(force=refresh)

    today = planner.resolve_today(config)
    anchor = datetime.strptime(config["paycheck_anchor"], "%Y-%m-%d").date()
    payday = planner.next_payday(today, anchor, config["paycheck_interval_days"])
    strategy = config["strategy"]
    extra_payment = config.get("extra_payment", float("inf"))

    plan = planner.plan_today(debts, bills, balance, config["buffer"], today, payday,
                              config["paycheck_amount"], strategy, extra_payment)

    def project(name: str) -> dict:
        r = planner.simulate_payoff(debts, config["monthly_payment"], name)
        return {"feasible": r["feasible"], "months": r["months"],
                "interest_str": m(r["total_interest"]) if r["feasible"] else None,
                "note": r["note"]}

    return {
        "today": today.strftime("%A, %B %d, %Y"),
        "balance_str": m(balance),
        "source": source,
        "fetched_at": fetched_at,
        "next_payday": payday.strftime("%b %d"),
        "paycheck_str": m(config["paycheck_amount"]),
        "reserved": [{"due": o["due"].strftime("%b %d"), "name": o["name"],
                      "amount_str": m(o["amount"]), "kind": o["kind"],
                      "due_today": o in plan["due_today"]} for o in plan["obligations"]],
        "reserved_total_str": m(plan["reserved"]),
        "safe_today": plan["safe_today"],
        "safe_today_str": m(plan["safe_today"]),
        "extra": [{"name": e["name"], "amount_str": m(e["amount"])} for e in plan["extra"]],
        "extra_affordable": plan["extra_affordable"],
        "extra_payment_str": m(plan["extra_payment"]) if plan["extra_payment"] is not None else None,
        "projected_after_payday_str": m(plan["projected_after_payday"]),
        "strategy": strategy,
        "monthly_str": m(config["monthly_payment"]),
        "projection": {"avalanche": project("avalanche"), "snowball": project("snowball")},
        # current values for the forms
        "settings": {"strategy": strategy, "buffer": config["buffer"],
                     "extra_payment": config.get("extra_payment", 0)},
        "all_bills": [{"name": b.name, "amount_str": m(b.amount), "due_day": b.due_day}
                      for b in bills],
        "all_debts": [{"name": d.name, "balance_str": m(d.balance),
                       "apr_str": f"{d.apr * 100:.2f}%", "minimum_str": m(d.minimum),
                       "due_day": d.due_day} for d in debts],
    }


# ----------------------------------------------------------------------------
# CHAT (the agent layer)
# The model interprets the question and calls run_scenario; the deterministic
# planner computes the numbers; the model narrates. The model never touches
# money or the bank -- run_scenario is read-only and uses the cached balance.
# ----------------------------------------------------------------------------

LLM_TIMEOUT = 90

RUN_SCENARIO_TOOL = {
    "type": "function",
    "function": {
        "name": "run_scenario",
        "description": ("Recompute the debt plan under hypothetical settings. Any field "
                        "you omit keeps the user's current value. Call this for ANY 'what "
                        "if' about the safety buffer, the extra payment, the payoff "
                        "strategy, or the monthly amount toward debt."),
        "parameters": {
            "type": "object",
            "properties": {
                "buffer": {"type": "number", "description": "hypothetical safety buffer, dollars"},
                "extra_payment": {"type": "number", "description": "hypothetical extra payment per pay period, dollars"},
                "monthly_payment": {"type": "number", "description": "hypothetical total monthly amount toward debt (drives the payoff projection)"},
                "strategy": {"type": "string", "enum": ["avalanche", "snowball"]},
            },
        },
    },
}


def run_scenario(args: dict) -> dict:
    """Recompute the plan with optional overrides. Read-only; uses cached balance."""
    config = planner.load_config(CONFIG)
    debts = planner.load_debts(DEBTS)
    bills = planner.load_bills(BILLS)
    balance, _, _ = get_balance(force=False)        # cached -- chat never hits the bank

    today = planner.resolve_today(config)
    anchor = datetime.strptime(config["paycheck_anchor"], "%Y-%m-%d").date()
    payday = planner.next_payday(today, anchor, config["paycheck_interval_days"])

    def num(key, default):
        try:
            return float(args[key])
        except (KeyError, TypeError, ValueError):
            return default

    buffer = num("buffer", config["buffer"])
    extra = num("extra_payment", config.get("extra_payment", float("inf")))
    monthly = num("monthly_payment", config["monthly_payment"])
    strategy = args.get("strategy") if args.get("strategy") in ("avalanche", "snowball") else config["strategy"]

    plan = planner.plan_today(debts, bills, balance, buffer, today, payday,
                              config["paycheck_amount"], strategy, extra)
    av = planner.simulate_payoff(debts, monthly, "avalanche")
    sn = planner.simulate_payoff(debts, monthly, "snowball")
    return {
        "inputs": {"buffer": buffer,
                   "extra_payment": None if extra == float("inf") else extra,
                   "strategy": strategy, "monthly_payment": monthly},
        "safe_to_spend_today": plan["safe_today"],
        "recommended_today": [{"name": e["name"], "amount": e["amount"]} for e in plan["extra"]],
        "extra_fully_affordable_today": plan["extra_affordable"],
        "holding_today": plan["safe_today"] <= 0,
        "next_payday": payday.strftime("%b %d"),
        "projected_spendable_after_payday": plan["projected_after_payday"],
        "avalanche_months": av["months"], "avalanche_interest": av["total_interest"],
        "snowball_months": sn["months"], "snowball_interest": sn["total_interest"],
    }


def _ollama_chat(messages: list, model: str) -> dict:
    payload = {"model": model, "messages": messages,
               "tools": [RUN_SCENARIO_TOOL], "stream": False}
    req = urllib.request.Request("http://localhost:11434/api/chat",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
        return json.loads(resp.read())


def _baseline_context() -> str:
    """A compact snapshot of the real current numbers, for the system prompt."""
    config = planner.load_config(CONFIG)
    balance, _, _ = get_balance(force=False)
    base = run_scenario({})
    rec = ", ".join(f"${e['amount']:.0f} to {e['name']}" for e in base["recommended_today"]) \
        or "nothing (holding today)"
    return (f"Today's checking balance ${balance:,.2f}. Buffer ${config['buffer']:,.2f}. "
            f"Extra payment setting ${config.get('extra_payment', 0):,.2f}. "
            f"Strategy {config['strategy']}. Monthly toward debt ${config['monthly_payment']:,.2f}. "
            f"Safe to spend today ${base['safe_to_spend_today']:,.2f}. "
            f"Today's recommendation: send {rec}. Next paycheck {base['next_payday']}. "
            f"Payoff: avalanche {base['avalanche_months']} mo "
            f"(${(base['avalanche_interest'] or 0):,.2f} interest), "
            f"snowball {base['snowball_months']} mo "
            f"(${(base['snowball_interest'] or 0):,.2f} interest).")


def _facts_lines(result: dict) -> list:
    """Ground-truth fact lines straight from run_scenario -- shown next to the
    model's prose so the wording can never quietly replace the real numbers."""
    inp = result["inputs"]
    extra = "off" if inp["extra_payment"] is None else m(inp["extra_payment"])
    lines = [["Scenario", f"buffer {m(inp['buffer'])} · extra {extra} · "
                          f"{inp['strategy']} · {m(inp['monthly_payment'])}/mo"],
             ["Safe to spend today", m(result["safe_to_spend_today"])]]
    rec = ", ".join(f"{m(e['amount'])} to {e['name']}" for e in result["recommended_today"]) \
        or "hold — nothing safe today"
    lines.append(["Recommended", rec])
    if result["avalanche_months"] is not None:
        lines.append(["Avalanche payoff",
                      f"{result['avalanche_months']} mo · {m(result['avalanche_interest'])} interest paid"])
    if result["snowball_months"] is not None:
        lines.append(["Snowball payoff",
                      f"{result['snowball_months']} mo · {m(result['snowball_interest'])} interest paid"])
    return lines


def chat_reply(messages: list) -> dict:
    config = planner.load_config(CONFIG)
    model = config.get("llm_model", "llama3.2")
    system = (
        "You are a friendly, plain-spoken assistant inside a personal debt-payoff app. "
        "Help the user understand today's plan and explore hypotheticals about their "
        "safety buffer, extra payment, payoff strategy, and monthly payment. "
        "NEVER invent or guess numbers, and never do arithmetic yourself. For ANY "
        "hypothetical or 'what if', call the run_scenario tool and base your answer ONLY "
        "on the numbers it returns. Note that the interest figures are interest you will "
        "PAY, not interest saved. Keep answers short and concrete. You explain this "
        "tool's math; you are not a licensed financial advisor and you do not give "
        "regulated investment advice.\n\nCURRENT SITUATION: " + _baseline_context()
    )
    convo = [{"role": "system", "content": system}]
    convo += [{"role": msg.get("role", "user"), "content": msg.get("content", "")} for msg in messages]

    last_facts = None
    try:
        for _ in range(4):                               # cap tool-call rounds
            resp = _ollama_chat(convo, model)
            reply_msg = resp.get("message", {})
            calls = reply_msg.get("tool_calls")
            if not calls:
                text = (reply_msg.get("content") or "").strip() or "(no answer)"
                return {"reply": text, "facts": last_facts}
            convo.append(reply_msg)
            for tc in calls:
                args = tc.get("function", {}).get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                result = run_scenario(args)
                last_facts = _facts_lines(result)        # remember the latest real numbers
                convo.append({"role": "tool", "content": json.dumps(result)})
        return {"reply": "I couldn't settle that one -- try rephrasing the what-if.",
                "facts": last_facts}
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        raise RuntimeError("Couldn't reach the local model. Make sure Ollama is running "
                           "(ollama serve) and the configured model is installed.")


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode())
            return
        if parsed.path == "/api/plan":
            refresh = parse_qs(parsed.query).get("refresh", ["0"])[0] == "1"
            try:
                self._send(200, "application/json", json.dumps(build_payload(refresh)).encode())
            except Exception as e:
                self._send(500, "application/json", json.dumps({"error": str(e)}).encode())
            return
        self._send(404, "text/plain", b"not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, "application/json", json.dumps({"error": "Bad JSON."}).encode())
        if parsed.path == "/api/chat":
            try:
                return self._send(200, "application/json",
                                  json.dumps(chat_reply(body.get("messages", []))).encode())
            except Exception as e:
                return self._send(200, "application/json", json.dumps({"error": str(e)}).encode())
        routes = {"/api/bill": add_bill, "/api/debt": add_debt,
                  "/api/settings": save_settings,
                  "/api/bill/delete": delete_bill, "/api/debt/delete": delete_debt}
        fn = routes.get(parsed.path)
        if not fn:
            return self._send(404, "application/json", json.dumps({"error": "not found"}).encode())
        try:
            fn(body)
            self._send(200, "application/json", json.dumps({"ok": True}).encode())
        except ValueError as e:                          # validation -> friendly 400
            self._send(400, "application/json", json.dumps({"error": str(e)}).encode())
        except Exception as e:
            self._send(500, "application/json", json.dumps({"error": str(e)}).encode())

    def log_message(self, *args) -> None:
        pass

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ----------------------------------------------------------------------------
# PAGE
# ----------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Debt plan</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#05080f; --surface:#10151e; --surface2:#0b0f17; --ink:#7fd8ff; --bright:#c8efff;
    --muted:#5f7790; --line:#1e2a3b; --accent:#00d3ff;
    --go:#34e2b0; --go-bg:rgba(52,226,176,.10); --hold:#ffb454; --hold-bg:rgba(255,180,84,.10);
    --glow:0 0 14px rgba(0,211,255,.45);
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font-family:"IBM Plex Sans",system-ui,sans-serif;line-height:1.6;}
  .wrap{max-width:760px;margin:0 auto;padding:40px 24px 90px;}
  h1{font-family:"Rajdhani",sans-serif;font-weight:700;font-size:34px;margin:0 0 2px;letter-spacing:.02em;color:var(--bright);text-shadow:var(--glow);text-transform:uppercase;}
  h2{font-family:"Rajdhani",sans-serif;font-weight:600;font-size:20px;margin:0 0 14px;letter-spacing:.03em;color:var(--bright);text-transform:uppercase;}
  .sub{color:var(--muted);font-size:14px;margin-bottom:26px;}
  .card{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:22px 24px;margin-bottom:18px;box-shadow:inset 0 0 30px rgba(0,140,200,.04);}
  .balance{font-family:"Rajdhani",sans-serif;font-size:52px;font-weight:700;line-height:1.1;letter-spacing:.01em;color:var(--bright);text-shadow:var(--glow);}
  .row{display:flex;justify-content:space-between;align-items:baseline;gap:16px;}
  .label{color:var(--muted);font-size:13px;text-transform:uppercase;letter-spacing:.08em;}
  .meta{color:var(--muted);font-size:13px;margin-top:6px;}
  .reserved-line{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--line);font-size:15px;}
  .reserved-line:last-child{border-bottom:0}
  .reserved-line .when{color:var(--muted);width:64px;display:inline-block;}
  .tag{font-size:11px;color:var(--hold);background:var(--hold-bg);border-radius:6px;padding:1px 7px;margin-left:8px;}
  .verdict{border-radius:12px;padding:16px 18px;font-size:16px;margin-top:4px;}
  .verdict.go{background:var(--go-bg);color:var(--go);border:1px solid rgba(52,226,176,.30);}
  .verdict.hold{background:var(--hold-bg);color:var(--hold);border:1px solid rgba(255,180,84,.30);}
  .verdict b{font-weight:600}
  .note{font-size:12px;color:var(--muted);margin-top:10px;}
  .seg{display:inline-flex;border:1px solid var(--line);border-radius:10px;overflow:hidden;}
  .seg button{border:0;background:var(--surface2);padding:9px 16px;font:inherit;cursor:pointer;color:var(--muted);}
  .seg button.on{background:var(--accent);color:#04121a;font-weight:600;box-shadow:inset 0 0 14px rgba(0,211,255,.35);}
  .proj{display:grid;grid-template-columns:1fr 1fr;gap:14px;}
  .proj .box{border:1px solid var(--line);border-radius:10px;padding:14px;}
  .proj .box.on{border-color:var(--accent);background:var(--go-bg);box-shadow:var(--glow);}
  .proj .name{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);}
  .proj .big{font-family:"Rajdhani",sans-serif;font-size:26px;font-weight:600;margin:4px 0;color:var(--bright);}
  .err{background:rgba(255,80,80,.12);color:#ff9a9a;border:1px solid rgba(255,80,80,.30);border-radius:10px;padding:14px;}
  .field{display:flex;flex-direction:column;gap:4px;margin-bottom:12px;min-width:0;}
  .field label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;}
  .field input{width:100%;min-width:0;font:inherit;padding:9px 11px;border:1px solid var(--line);border-radius:9px;background:var(--surface2);color:var(--bright);}
  .field input::placeholder{color:var(--muted);}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px;}
  .btn{border:1px solid var(--line);background:var(--surface2);border-radius:9px;padding:9px 16px;font:inherit;cursor:pointer;color:var(--ink);}
  .btn:hover{border-color:var(--accent);color:var(--accent);box-shadow:var(--glow);}
  .btn.primary{background:var(--accent);color:#04121a;border-color:var(--accent);font-weight:600;}
  .btn.primary:hover{color:#04121a;box-shadow:var(--glow);}
  .msg{font-size:13px;margin-top:10px;min-height:18px;}
  .msg.ok{color:var(--go);} .msg.bad{color:#ff9a9a;}
  .listrow{display:flex;justify-content:space-between;font-size:14px;padding:6px 0;border-bottom:1px solid var(--line);gap:10px;}
  .listrow:last-child{border-bottom:0}
  .listrow .r{color:var(--muted);}
  .del{border:0;background:transparent;color:var(--muted);cursor:pointer;font-size:13px;padding:0 5px;margin-left:8px;border-radius:6px;line-height:1;}
  .del:hover{color:#ff9a9a;background:rgba(255,80,80,.12);}
  .chat-msgs{display:flex;flex-direction:column;gap:8px;max-height:340px;overflow-y:auto;margin:6px 0 12px;}
  .cmsg{padding:9px 13px;border-radius:13px;font-size:14px;max-width:88%;white-space:pre-wrap;line-height:1.5;}
  .cu{align-self:flex-end;background:rgba(0,211,255,.14);border:1px solid rgba(0,211,255,.35);color:var(--bright);border-bottom-right-radius:4px;}
  .cb{align-self:flex-start;background:#141b26;border:1px solid var(--line);color:var(--ink);border-bottom-left-radius:4px;max-width:94%;}
  .cb.think{color:var(--muted);font-style:italic;}
  .facts{margin-top:9px;border:1px solid var(--line);border-radius:10px;padding:9px 11px;background:var(--surface2);}
  .facts-h{font-size:10px;text-transform:uppercase;letter-spacing:.12em;color:var(--accent);margin-bottom:6px;}
  .fact{display:flex;justify-content:space-between;gap:14px;padding:2px 0;font-size:12.5px;}
  .fact span:first-child{color:var(--muted);white-space:nowrap;}
  .fact span:last-child{color:var(--bright);text-align:right;}
  .chat-in{display:flex;gap:8px;}
  .chat-in input{flex:1;min-width:0;font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:var(--surface2);color:var(--bright);}
  .chat-in input::placeholder{color:var(--muted);}
  .chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px;}
  .chip{border:1px solid var(--line);background:var(--surface2);border-radius:20px;padding:6px 12px;font-size:13px;cursor:pointer;color:var(--muted);}
  .chip:hover{border-color:var(--accent);color:var(--accent);}
  @media (max-width:520px){
    .wrap{padding:28px 16px 70px;}
    .grid2{grid-template-columns:1fr;}
    .balance{font-size:42px;}
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>Your debt plan</h1>
  <div class="sub" id="today">Loading…</div>
  <div id="chatcard"></div>
  <div id="app"></div>
</div>
<script>
async function load(refresh){
  const app = document.getElementById("app");
  try{
    const r = await fetch("/api/plan" + (refresh ? "?refresh=1" : ""));
    const d = await r.json();
    if (d.error){ app.innerHTML = '<div class="card err">'+d.error+'</div>'; return; }
    render(d);
  }catch(e){ app.innerHTML = '<div class="card err">Could not reach the server.</div>'; }
}

async function post(url, body, msgId){
  const el = document.getElementById(msgId);
  el.className = "msg"; el.textContent = "Saving…";
  try{
    const r = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    const d = await r.json();
    if (d.error){ el.className = "msg bad"; el.textContent = d.error; return false; }
    el.className = "msg ok"; el.textContent = "Saved.";
    await load(false);
    return true;
  }catch(e){ el.className = "msg bad"; el.textContent = "Could not reach the server."; return false; }
}

function v(id){ return document.getElementById(id).value; }

function addBill(){ post("/api/bill", {name:v("b_name"), amount:v("b_amt"), due_day:v("b_day")}, "b_msg")
  .then(ok => { if(ok){ ["b_name","b_amt","b_day"].forEach(i=>document.getElementById(i).value=""); }}); }
function addDebt(){ post("/api/debt", {name:v("d_name"), balance:v("d_bal"), apr:v("d_apr"), minimum:v("d_min"), due_day:v("d_day")}, "d_msg")
  .then(ok => { if(ok){ ["d_name","d_bal","d_apr","d_min","d_day"].forEach(i=>document.getElementById(i).value=""); }}); }
let curStrategy = null;
function setStrategy(s){ curStrategy = s;
  document.getElementById("s_av").className = (s==="avalanche"?"on":"");
  document.getElementById("s_sn").className = (s==="snowball"?"on":""); }
function saveSettings(){ post("/api/settings", {strategy:curStrategy, buffer:v("s_buf"), extra_payment:v("s_extra")}, "s_msg"); }

let LAST = null;
function deleteBill(i){ const b = LAST.all_bills[i]; if(!confirm('Delete bill "'+b.name+'"?')) return; post("/api/bill/delete", {index:i, name:b.name}, "b_msg"); }
function deleteDebt(i){ const x = LAST.all_debts[i]; if(!confirm('Delete debt "'+x.name+'"?')) return; post("/api/debt/delete", {index:i, name:x.name}, "d_msg"); }

// ---- chat (the agent layer) ----
let CHAT = [];
let CHAT_BUSY = false;
function escapeHtml(s){ return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
function renderChat(){
  const chips = CHAT.length ? '' :
    '<div class="chips">'
    + '<button class="chip" onclick="sendChat(this.textContent)">Why are we holding today?</button>'
    + '<button class="chip" onclick="sendChat(this.textContent)">What if I pay $500 extra?</button>'
    + '<button class="chip" onclick="sendChat(this.textContent)">What if our buffer were $1,000?</button>'
    + '</div>';
  const msgs = CHAT.map(mm => {
    let html = '<div class="cmsg '+(mm.role==="user"?"cu":"cb")+'">'+escapeHtml(mm.content);
    if(mm.facts && mm.facts.length){
      html += '<div class="facts"><div class="facts-h">from the planner</div>'
        + mm.facts.map(f=>'<div class="fact"><span>'+escapeHtml(f[0])+'</span><span>'+escapeHtml(f[1])+'</span></div>').join('')
        + '</div>';
    }
    return html + '</div>';
  }).join('')
    + (CHAT_BUSY ? '<div class="cmsg cb think">thinking…</div>' : '');
  document.getElementById("chatcard").innerHTML =
    '<div class="card"><h2>Ask about your plan</h2>'
    + chips
    + '<div class="chat-msgs">'+msgs+'</div>'
    + '<div class="chat-in"><input id="chat_in" placeholder="e.g. what if our buffer were $1,000?" '
      + (CHAT_BUSY?'disabled ':'') + 'onkeydown="if(event.key===\'Enter\')sendChat()">'
      + '<button class="btn primary" '+(CHAT_BUSY?'disabled ':'')+'onclick="sendChat()">Send</button></div>'
    + '<div class="note">The assistant interprets your question and runs the planner to answer — it never moves money.</div>'
    + '</div>';
  const box = document.querySelector(".chat-msgs"); if(box) box.scrollTop = box.scrollHeight;
  const inp = document.getElementById("chat_in"); if(inp && !CHAT_BUSY) inp.focus();
}
async function sendChat(text){
  if(CHAT_BUSY) return;
  const inp = document.getElementById("chat_in");
  text = (text || (inp ? inp.value : "") || "").trim();
  if(!text) return;
  CHAT.push({role:"user", content:text});
  CHAT_BUSY = true; renderChat();
  try{
    const r = await fetch("/api/chat", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({messages:CHAT})});
    const d = await r.json();
    CHAT.push({role:"assistant", content: d.error ? ("⚠ "+d.error) : d.reply, facts: d.error ? null : d.facts});
  }catch(e){ CHAT.push({role:"assistant", content:"⚠ Could not reach the server."}); }
  CHAT_BUSY = false; renderChat();
}

function render(d){
  LAST = d;
  document.getElementById("today").textContent = d.today;
  curStrategy = d.settings.strategy;
  const go = d.safe_today > 0;

  let verdict;
  if (!go){
    verdict = '<div class="verdict hold"><b>Hold today.</b> No safe surplus after the buffer and '
      + 'upcoming bills. Funds free up after your paycheck on '+d.next_payday
      + ' (projected spendable ~'+d.projected_after_payday_str+').</div>';
  } else if (d.extra.length){
    const lines = d.extra.map(e => 'Send <b>'+e.amount_str+'</b> to <b>'+e.name+'</b>').join('<br>');
    verdict = '<div class="verdict go">'+lines+'</div>';
    if (!d.extra_affordable){
      verdict += '<div class="note">Your set extra payment is '+d.extra_payment_str
        + ', but only '+d.safe_today_str+' is safe today. The rest frees up after '+d.next_payday+'.</div>';
    }
  } else {
    verdict = '<div class="verdict go">Surplus is available ('+d.safe_today_str
      + '), but your extra payment is set to $0.00 — set it in Settings to get a target.</div>';
  }
  const dueToday = d.reserved.filter(o=>o.due_today)
    .map(o=>'<div class="verdict hold" style="margin-top:10px">Pay today: <b>'+o.name+'</b> '+o.amount_str+' (due today)</div>').join('');

  const reserved = d.reserved.length
    ? d.reserved.map(o=>'<div class="reserved-line"><span><span class="when">'+o.due+'</span>'
        + o.name + (o.due_today?'<span class="tag">due today</span>':'') + '</span><span>'+o.amount_str+'</span></div>').join('')
    : '<div class="meta">Nothing due before your next paycheck.</div>';

  const projBox = (key,label,o) => '<div class="box '+(d.strategy===key?'on':'')+'">'
    + '<div class="name">'+label+'</div>'
    + (o.feasible ? '<div class="big">'+o.months+' mo</div><div class="meta">'+o.interest_str+' interest</div>'
                  : '<div class="meta">'+(o.note||'n/a')+'</div>') + '</div>';

  const billRows = d.all_bills.length
    ? d.all_bills.map((b,i)=>'<div class="listrow"><span>'+b.name+'</span><span class="r">'+b.amount_str+' · day '+b.due_day
        +' <button class="del" title="Delete" onclick="deleteBill('+i+')">✕</button></span></div>').join('')
    : '<div class="meta">No bills yet.</div>';
  const debtRows = d.all_debts.length
    ? d.all_debts.map((x,i)=>'<div class="listrow"><span>'+x.name+'</span><span class="r">'+x.balance_str+' · '+x.apr_str+' · min '+x.minimum_str+' · day '+x.due_day
        +' <button class="del" title="Delete" onclick="deleteDebt('+i+')">✕</button></span></div>').join('')
    : '<div class="meta">No debts yet.</div>';

  document.getElementById("app").innerHTML =
    '<div class="card">'
      + '<div class="label">Checking balance</div>'
      + '<div class="balance">'+d.balance_str+'</div>'
      + '<div class="meta">'+(d.source==="simplefin"?"Live from your bank":"From CSV")+' · as of '+(d.fetched_at||'—')
        + ' · next pay '+d.next_payday+' ('+d.paycheck_str+')</div>'
      + '<div style="margin-top:14px"><button class="btn" onclick="load(true)">↻ Refresh balance</button>'
        + '<span class="note" style="margin-left:10px">Refresh hits the bank; everything else doesn\'t.</span></div>'
    + '</div>'

    + '<div class="card">'
      + '<div class="label" style="margin-bottom:10px">Today\'s recommendation</div>'
      + verdict + dueToday
    + '</div>'

    + '<div class="card">'
      + '<div class="row"><div class="label">Reserved before next paycheck</div><div>'+d.reserved_total_str+'</div></div>'
      + '<div style="margin-top:10px">'+reserved+'</div>'
      + '<div class="meta" style="margin-top:12px">Safe to spend today: <b style="color:'+(go?'var(--go)':'var(--hold)')+'">'+d.safe_today_str+'</b></div>'
    + '</div>'

    + '<div class="card">'
      + '<div class="label" style="margin-bottom:12px">Payoff projection (at '+d.monthly_str+'/mo)</div>'
      + '<div class="proj">'+projBox("avalanche","Avalanche",d.projection.avalanche)+projBox("snowball","Snowball",d.projection.snowball)+'</div>'
    + '</div>'

    + '<div class="card">'
      + '<h2>Settings</h2>'
      + '<div class="field"><label>Strategy</label><div class="seg">'
        + '<button id="s_av" class="'+(d.settings.strategy==="avalanche"?"on":"")+'" onclick="setStrategy(\'avalanche\')">Avalanche</button>'
        + '<button id="s_sn" class="'+(d.settings.strategy==="snowball"?"on":"")+'" onclick="setStrategy(\'snowball\')">Snowball</button>'
      + '</div></div>'
      + '<div class="grid2">'
        + '<div class="field"><label>Safety buffer ($)</label><input id="s_buf" type="number" value="'+d.settings.buffer+'"></div>'
        + '<div class="field"><label>Extra payment ($)</label><input id="s_extra" type="number" value="'+d.settings.extra_payment+'"></div>'
      + '</div>'
      + '<button class="btn primary" onclick="saveSettings()">Save settings</button>'
      + '<div class="note">The extra payment is the amount the planner tells you to send to your target debt each pay period (capped by what\'s safe).</div>'
      + '<div id="s_msg" class="msg"></div>'
    + '</div>'

    + '<div class="card">'
      + '<h2>Add a bill</h2>'
      + '<div class="field"><label>Name</label><input id="b_name" placeholder="e.g. Electric"></div>'
      + '<div class="grid2">'
        + '<div class="field"><label>Amount ($)</label><input id="b_amt" type="number"></div>'
        + '<div class="field"><label>Due day (1–31)</label><input id="b_day" type="number"></div>'
      + '</div>'
      + '<button class="btn primary" onclick="addBill()">Add bill</button>'
      + '<div id="b_msg" class="msg"></div>'
      + '<div style="margin-top:14px"><div class="label" style="margin-bottom:6px">Your bills</div>'+billRows+'</div>'
    + '</div>'

    + '<div class="card">'
      + '<h2>Add a debt</h2>'
      + '<div class="field"><label>Debt name</label><input id="d_name" placeholder="e.g. Discover Card"></div>'
      + '<div class="grid2">'
        + '<div class="field"><label>Balance ($)</label><input id="d_bal" type="number"></div>'
        + '<div class="field"><label>APR (%)</label><input id="d_apr" type="number" placeholder="22.49"></div>'
        + '<div class="field"><label>Minimum payment ($)</label><input id="d_min" type="number"></div>'
        + '<div class="field"><label>Due day (1–31)</label><input id="d_day" type="number"></div>'
      + '</div>'
      + '<button class="btn primary" onclick="addDebt()">Add debt</button>'
      + '<div id="d_msg" class="msg"></div>'
      + '<div style="margin-top:14px"><div class="label" style="margin-bottom:6px">Your debts</div>'+debtRows+'</div>'
    + '</div>';
}
load(false);
renderChat();
</script>
</body>
</html>
"""


def detect_tailscale_ip() -> str | None:
    """Best-effort: ask the Tailscale CLI for this device's tailnet IP."""
    for cmd in (["tailscale", "ip", "-4"],
                [r"C:\Program Files\Tailscale\tailscale.exe", "ip", "-4"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            ip = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
            if ip.startswith("100."):
                return ip
        except Exception:
            continue
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Debt planner web UI.")
    parser.add_argument("--host", default=None, help="bind address (default 127.0.0.1)")
    parser.add_argument("--tailscale", action="store_true",
                        help="bind to this device's Tailscale IP for phone/laptop access")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    host = args.host or HOST
    if args.tailscale:
        ip = detect_tailscale_ip()
        if not ip:
            raise SystemExit("Could not find a Tailscale IP. Is Tailscale running and signed in?")
        host = ip

    httpd = http.server.ThreadingHTTPServer((host, args.port), Handler)
    url = f"http://{host}:{args.port}/"
    print(f"Debt planner UI running at {url}")
    if host in ("127.0.0.1", "localhost"):
        print("Bound to localhost only.")
    else:
        print(f"Bound to {host} -- reachable from your other devices on that network.")
    print("Press Ctrl+C to stop.")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        httpd.shutdown()


if __name__ == "__main__":
    main()