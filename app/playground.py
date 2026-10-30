"""Guided Playground: deterministic demonstrations on synthetic scenarios, with evidence and structured feedback.
Outputs are simulations, never credit or business decisions. Runs are private to the person who made them."""
import json
import random
import time

from . import audit
from .constants import Accuracy, Permission, parse
from .auth import Ctx
from .db import tx
from .policy import Denied, entitled, require_grant, require_participant

DISCLAIMER = "Simulated output on synthetic data. Not a credit or business decision."


# ---- engines: each takes validated inputs and returns {headline, verdict, evidence[], metrics{}} ----------------

def _sme_credit(i: dict) -> dict:
    dscr = i["ebitda"] / i["annual_debt_service"] if i["annual_debt_service"] else 99.0
    leverage = i["total_debt"] / i["ebitda"] if i["ebitda"] > 0 else 99.0
    current = i["current_assets"] / i["current_liabilities"] if i["current_liabilities"] else 99.0
    rules = [
        ("Debt service coverage (EBITDA ÷ annual debt service)", dscr, ">=", 1.25, f"{dscr:.2f}×"),
        ("Leverage (total debt ÷ EBITDA)", leverage, "<=", 4.0, f"{leverage:.2f}×"),
        ("Current ratio (current assets ÷ current liabilities)", current, ">=", 1.1, f"{current:.2f}"),
        ("Years trading", i["years_trading"], ">=", 2, f"{i['years_trading']:g} years"),
    ]
    evidence, fails = [], 0
    for label, value, op, limit, shown in rules:
        ok = value >= limit if op == ">=" else value <= limit
        fails += not ok
        evidence.append({"check": label, "value": shown, "policy": f"{op} {limit:g}", "result": "pass" if ok else "fail"})
    verdict = "Within sandbox policy" if fails == 0 else ("Refer for analyst review" if fails == 1 else "Outside sandbox policy")
    return {"headline": verdict, "verdict": "ok" if fails == 0 else ("warn" if fails == 1 else "bad"), "evidence": evidence,
            "metrics": {"DSCR": f"{dscr:.2f}×", "Leverage": f"{leverage:.2f}×", "Current ratio": f"{current:.2f}"}}


def _portfolio(seed: int = 7, n: int = 400):
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        score = max(300, min(900, int(rng.gauss(650, 80))))
        dti = round(max(0.05, min(0.9, rng.gauss(0.38, 0.12))), 2)
        # default probability falls with score and rises with debt-to-income
        pd = max(0.004, min(0.45, 0.32 - (score - 300) * 0.00048 + max(0, dti - 0.35) * 0.4))
        out.append((score, dti, pd))
    return out


def _strategy(i: dict) -> dict:
    book = _portfolio()

    def run(cutoff, max_dti):
        approved = [b for b in book if b[0] >= cutoff and b[1] <= max_dti]
        rate = len(approved) / len(book)
        loss = sum(b[2] for b in approved) / len(approved) if approved else 0.0
        return rate, loss, len(approved)

    base_rate, base_loss, base_n = run(620, 0.45)
    rate, loss, n = run(i["score_cutoff"], i["max_dti"])
    evidence = [
        {"check": "Approval rate", "value": f"{rate:.1%}", "policy": f"baseline {base_rate:.1%}", "result": "pass" if rate >= base_rate * 0.9 else "fail"},
        {"check": "Expected default rate of approved", "value": f"{loss:.2%}", "policy": f"baseline {base_loss:.2%}", "result": "pass" if loss <= base_loss * 1.1 else "fail"},
        {"check": "Applications approved", "value": f"{n} of {len(book)}", "policy": f"baseline {base_n}", "result": "info"},
    ]
    worse = sum(e["result"] == "fail" for e in evidence)
    head = ("Higher approvals at similar risk" if rate > base_rate and loss <= base_loss * 1.1 else
            "Lower risk, fewer approvals" if loss < base_loss and rate < base_rate else
            "Trade-off needs review" if worse else "Close to the current policy")
    return {"headline": head, "verdict": "ok" if not worse else "warn", "evidence": evidence,
            "metrics": {"Approval rate": f"{rate:.1%}", "Expected default": f"{loss:.2%}", "Δ approvals": f"{(rate - base_rate) * 100:+.1f} pts"}}


MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _seasonality(i: dict) -> dict:
    this, last = i["revenue_this_year"], i["revenue_last_year"]
    evidence, flags_qoq, flags_yoy = [], 0, 0
    for q in range(1, 4):
        cur, prev = sum(this[q * 3:q * 3 + 3]), sum(this[q * 3 - 3:q * 3])
        same = sum(last[q * 3:q * 3 + 3])
        qoq = (cur - prev) / prev if prev else 0
        yoy = (cur - same) / same if same else 0
        qoq_flag, yoy_flag = qoq < -0.15, yoy < -0.15
        flags_qoq += qoq_flag
        flags_yoy += yoy_flag
        evidence.append({"check": f"Q{q + 1} vs Q{q}", "value": f"{qoq:+.0%} quarter on quarter", "policy": f"{yoy:+.0%} vs same quarter last year (flag below −15%)",
                         "result": "fail" if yoy_flag else ("warn" if qoq_flag else "pass")})
    if flags_qoq and not flags_yoy:
        head, verdict = "Seasonal pattern, not decline", "ok"
    elif flags_yoy:
        head, verdict = "Decline against the same season last year", "bad"
    else:
        head, verdict = "Stable", "ok"
    return {"headline": head, "verdict": verdict, "evidence": evidence,
            "metrics": {"Quarter-on-quarter flags": str(flags_qoq), "Same-season flags": str(flags_yoy),
                        "Annual change": f"{(sum(this) - sum(last)) / sum(last):+.1%}"},
            "series": {"this": this, "last": last}}


def _riva(i: dict) -> dict:
    lines = i["lines"]
    evidence = []
    total = sum(v for _, v in lines if v is not None)
    blanks = [name for name, v in lines if v is None]
    evidence.append({"check": "Mandatory lines filled", "value": f"{len(lines) - len(blanks)} of {len(lines)}",
                     "policy": "all lines", "result": "fail" if blanks else "pass"})
    evidence.append({"check": "Subtotal equals sum of lines", "value": f"{i['reported_total']:,.0f} reported",
                     "policy": f"{total:,.0f} computed", "result": "pass" if abs(total - i["reported_total"]) < 1 else "fail"})
    negatives = [name for name, v in lines if v is not None and v < 0]
    evidence.append({"check": "No negative exposures", "value": ", ".join(negatives) or "none", "policy": "≥ 0", "result": "fail" if negatives else "pass"})
    fails = sum(e["result"] == "fail" for e in evidence)
    return {"headline": "Ready to submit in the sandbox" if not fails else f"{fails} validation issue(s) to fix",
            "verdict": "ok" if not fails else "bad", "evidence": evidence,
            "metrics": {"Lines": str(len(lines)), "Blank lines": str(len(blanks)), "Issues": str(fails)}}


# ---- scenarios: synthetic, editable inputs per offering ------------------------------------------------------

def _num(name, label, value, lo, hi, step=1, unit=""):
    return {"name": name, "label": label, "value": value, "min": lo, "max": hi, "step": step, "unit": unit}


SCENARIOS = {
    "sme-credit-decisioning": {
        "engine": _sme_credit, "title": "Evaluate a synthetic SME applicant",
        "story": "Agri-input distributor applying for a working-capital line. All figures are synthetic (₹ lakh).",
        "fields": [_num("ebitda", "EBITDA", 84, -50, 1000, 1, "₹ lakh"), _num("annual_debt_service", "Annual debt service", 61, 0, 1000, 1, "₹ lakh"),
                   _num("total_debt", "Total debt", 290, 0, 5000, 1, "₹ lakh"), _num("current_assets", "Current assets", 210, 0, 5000, 1, "₹ lakh"),
                   _num("current_liabilities", "Current liabilities", 176, 0, 5000, 1, "₹ lakh"), _num("years_trading", "Years trading", 6, 0, 80, 1, "years")],
    },
    "credit-strategy-simulator": {
        "engine": _strategy, "title": "Simulate a credit policy change",
        "story": "400 synthetic applications. The current policy approves score ≥ 620 with debt-to-income ≤ 0.45.",
        "fields": [_num("score_cutoff", "Minimum score", 640, 300, 900, 5), _num("max_dti", "Maximum debt-to-income", 0.40, 0.05, 0.9, 0.01)],
    },
    "financial-analysis-intelligence": {
        "engine": _seasonality, "title": "Seasonal revenue or real decline?",
        "story": "Monthly revenue for a synthetic agri-input SME (₹ lakh). Compare quarter-on-quarter with the same season last year.",
        "fields": [{"name": "revenue_this_year", "label": "Revenue this year, Jan–Dec", "series": [42, 40, 44, 30, 27, 26, 33, 38, 41, 52, 55, 58], "unit": "₹ lakh"},
                   {"name": "revenue_last_year", "label": "Revenue last year, Jan–Dec", "series": [39, 38, 41, 28, 25, 25, 31, 35, 38, 48, 51, 54], "unit": "₹ lakh"}],
    },
    "riva": {
        "engine": _riva, "title": "Validate a synthetic regulatory return",
        "story": "Exposure return with five lines. Leave a line blank to see validation catch it.",
        "fields": [_num("line_retail", "Retail exposures", 1240, -100000, 100000, 1, "₹ lakh"), _num("line_sme", "SME exposures", 860, -100000, 100000, 1, "₹ lakh"),
                   _num("line_corporate", "Corporate exposures", 2310, -100000, 100000, 1, "₹ lakh"), _num("line_interbank", "Interbank exposures", 415, -100000, 100000, 1, "₹ lakh"),
                   _num("line_other", "Other exposures", 95, -100000, 100000, 1, "₹ lakh"), _num("reported_total", "Reported subtotal", 4920, 0, 1000000, 1, "₹ lakh")],
    },
}


def available(conn, ctx: Ctx) -> list[dict]:
    """Employees see every scenario; clients only those of offerings their active organisation is entitled to."""
    require_participant(ctx)
    names = {r["slug"]: r for r in conn.execute("SELECT * FROM offerings")}
    out = []
    for slug, sc in SCENARIOS.items():
        if ctx.is_employee or entitled(conn, ctx.org_id, slug):
            out.append({"slug": slug, "name": names[slug]["name"], "category": names[slug]["category"], "title": sc["title"], "story": sc["story"]})
    return out


def scenario(conn, ctx: Ctx, slug: str) -> dict:
    if slug not in {s["slug"] for s in available(conn, ctx)}:
        raise Denied(404, "This sandbox is not available to your workspace.")
    return SCENARIOS[slug]


def _parse(sc: dict, form: dict) -> dict:
    out = {}
    for f in sc["fields"]:
        raw = form.get(f["name"])
        if "series" in f:
            parts = [p.strip() for p in str(raw if raw is not None else ",".join(map(str, f["series"]))).split(",")]
            try:
                vals = [float(p) for p in parts]
            except ValueError:
                raise Denied(422, f"{f['label']}: enter 12 numbers separated by commas.")
            if len(vals) != 12 or min(vals) < 0 or max(vals) > 1e6:
                raise Denied(422, f"{f['label']}: enter 12 non-negative numbers.")
            out[f["name"]] = vals
            continue
        if raw in (None, "") and f["name"].startswith("line_"):
            out[f["name"]] = None
            continue
        try:
            v = float(raw if raw not in (None, "") else f["value"])
        except ValueError:
            raise Denied(422, f"{f['label']} must be a number.")
        if not f["min"] <= v <= f["max"]:
            raise Denied(422, f"{f['label']} must be between {f['min']:g} and {f['max']:g}.")
        out[f["name"]] = v
    if any(k.startswith("line_") for k in out):
        out = {"lines": [(f["label"], out[f["name"]]) for f in sc["fields"] if f["name"].startswith("line_")], "reported_total": out["reported_total"]}
    return out


def run(conn, ctx: Ctx, slug: str, form: dict) -> int:
    sc = scenario(conn, ctx, slug)
    inputs = _parse(sc, form)
    result = sc["engine"](inputs) | {"disclaimer": DISCLAIMER}
    with tx(conn):
        rid = conn.execute(
            "INSERT INTO playground_runs (user_id, org_id, offering, inputs, result, created_at) VALUES (?,?,?,?,?,?)",
            (ctx.user_id, ctx.org_id, slug, json.dumps(inputs), json.dumps(result), time.time()),
        ).lastrowid
        audit.record(conn, ctx.user_id, "playground.run", f"run:{rid}", {"offering": slug})
    return rid


def get_run(conn, ctx: Ctx, run_id: int):
    """Runs are private to their author in the workspace they were made in."""
    require_participant(ctx)
    r = conn.execute("SELECT * FROM playground_runs WHERE id=? AND user_id=? AND org_id=?", (run_id, ctx.user_id, ctx.org_id)).fetchone()
    if not r:
        raise Denied(404, "Run not found.")
    return {**dict(r), "inputs": json.loads(r["inputs"]), "result": json.loads(r["result"])}


def my_runs(conn, ctx: Ctx, limit: int = 10):
    require_participant(ctx)
    return conn.execute("SELECT id, offering, created_at, json_extract(result, '$.headline') AS headline, json_extract(result, '$.verdict') AS verdict,"
                        " rating FROM playground_runs WHERE user_id=? AND org_id=? ORDER BY id DESC LIMIT ?", (ctx.user_id, ctx.org_id, limit)).fetchall()


ACCURACY = list(Accuracy)


def feedback(conn, ctx: Ctx, run_id: int, rating: str, raw_accuracy: str, comment: str) -> None:
    get_run(conn, ctx, run_id)
    accuracy = parse(Accuracy, raw_accuracy)
    if rating not in {"1", "2", "3", "4", "5"} or accuracy is None:
        raise Denied(422, "Choose a rating from 1 to 5 and how the result compared with your expectation.")
    with tx(conn):
        conn.execute("UPDATE playground_runs SET rating=?, accuracy=?, comment=? WHERE id=?", (int(rating), accuracy, comment.strip()[:500], run_id))
        audit.record(conn, ctx.user_id, "playground.feedback", f"run:{run_id}", {"rating": int(rating)})


def feedback_summary(conn, ctx: Ctx):
    """Aggregated for product owners: counts and averages per offering, no names, organisations or comments' authors."""
    require_grant(ctx, Permission.PRODUCT_OWNER)
    rows = conn.execute("SELECT offering, COUNT(*) runs, COUNT(rating) rated, AVG(rating) avg_rating,"
                        f" SUM(accuracy='{Accuracy.NOT_EXPECTED}') unexpected FROM playground_runs GROUP BY offering ORDER BY runs DESC").fetchall()
    comments = conn.execute("SELECT offering, comment, rating FROM playground_runs WHERE comment <> '' ORDER BY id DESC LIMIT 10").fetchall()
    return rows, comments


def explain(conn, ctx: Ctx, run_id: int) -> dict:
    """Optional AI-assisted explanation of one run, using only that run's synthetic inputs and computed evidence."""
    r = get_run(conn, ctx, run_id)
    from .aira import _rate_limit, MODELS, GROQ_URL
    import os

    import httpx

    _rate_limit(ctx.user_id)
    key = os.environ.get("GROQ_API_KEY")
    fallback = {"text": "AI explanation is unavailable. The evidence table shows each check, its value and the policy it was compared with.", "model": None}
    if not key:
        return fallback
    prompt = (f"Synthetic sandbox run for {r['offering']}. Inputs: {json.dumps(r['inputs'])}. Computed result: {json.dumps(r['result'])}. "
              "Each evidence item has: check, value, policy (what the value was compared with, including any threshold), and result "
              "(pass, warn = flagged only on the quarter-on-quarter view, fail = flagged on the decisive comparison). "
              "Explain in at most 90 words, for a relationship manager, which checks drove the headline and what would change it. "
              "Say it is a simulation, not a decision. Do not invent numbers that are not in the result.")
    for model in MODELS:
        try:
            resp = httpx.post(GROQ_URL, headers={"Authorization": f"Bearer {key}"}, timeout=20,
                              json={"model": model, "temperature": 0.2, "reasoning_effort": "low", "messages": [{"role": "user", "content": prompt}]})
            if resp.status_code == 200:
                return {"text": resp.json()["choices"][0]["message"]["content"].strip(), "model": model}
        except httpx.HTTPError:
            continue
    return fallback
