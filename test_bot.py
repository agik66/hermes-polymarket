#!/usr/bin/env python3
"""Self-check for the paper bot's pure logic. Run: python3 test_bot.py"""
import os, tempfile
from datetime import datetime, timezone
import bot
from bot import (one_hit_penalty, score_wallet, score_trade, position_size,
                 paper_pnl, fill_price, daily_stop_hit, DEFAULT_RULES, clamp)

R = dict(DEFAULT_RULES)

# one-hit-wonder penalty
assert one_hit_penalty([100, 100, 100, 100]) == 0.25
assert one_hit_penalty([1000, 1, 1]) > 0.99
assert one_hit_penalty([-50, -20]) == 1.0  # no positive pnl = fully penalized
assert one_hit_penalty([]) == 1.0

# wallet scoring: diversified consistent wallet beats one-hit wonder (no win_rate:
# public feeds can't produce an unbiased one, see AUDIT.md P2)
g1, c1, cp1 = score_wallet(0.25, 40, 0.15, 500, 60, R)
g2, c2, cp2 = score_wallet(0.25, 40, 0.95, 500, 60, R)
assert g1 > g2, (g1, g2)
assert 0 <= g1 <= 100 and 0 <= c1 <= 1 and 0 <= cp1 <= 1

# trade scoring hard fails
_, _, fail = score_trade(80, 0.0, 0.10, 50000, 48, 500, R)
assert fail and "spread" in fail
_, _, fail = score_trade(80, 0.0, 0.01, 100, 48, 500, R)
assert fail and "liquidity" in fail
_, _, fail = score_trade(80, 0.20, 0.01, 50000, 48, 500, R)
assert fail and "moved" in fail
_, _, fail = score_trade(80, -0.20, 0.01, 50000, 48, 500, R)  # collapsed price = also no copy
assert fail and "moved" in fail
_, _, fail = score_trade(80, 0.0, 0.01, 50000, 48, 500, R, mid=0.005)  # lottery ticket
assert fail and "band" in fail
_, _, fail = score_trade(80, 0.0, 0.01, 50000, 48, 500, R, mid=0.97)  # near-certainty
assert fail and "band" in fail
_, _, fail = score_trade(80, 0.0, 0.01, 50000, 0.2, 500, R)
assert fail and "soon" in fail

# good setup scores above gate; worse wallet scores lower
s_hi, bd, fail = score_trade(90, 0.005, 0.01, 60000, 72, 1000, R)
assert fail is None and s_hi >= R["min_copy_score"], s_hi
s_lo, _, _ = score_trade(30, 0.005, 0.01, 60000, 72, 1000, R)
assert s_lo < s_hi

# bankroll sizing: 1-2% of equity, monotone in score, capped by cash, floor $1
assert R["size_min"] <= position_size(60, R, 100, 100) <= position_size(95, R, 100, 100)
assert position_size(95, R, 100, 100) <= R["risk_max"] * 100 + 0.01
assert position_size(95, R, 100, 1.5) == 1.5      # cash cap, no leverage
assert position_size(95, R, 100, 0.5) == 0.0      # below min stake -> no trade
assert position_size(95, R, 1000, 1000) > position_size(95, R, 100, 100)  # compounds

# paper pnl math: $10 at 0.50 -> 0.60 = +$2
assert abs(paper_pnl(0.50, 0.60, 10) - 2.0) < 1e-9
assert abs(paper_pnl(0.50, 0.0, 10) + 10.0) < 1e-9  # total loss capped at stake

# realistic fills: BUY crosses to ask, SELL to bid, +1c on thin books
assert abs(fill_price(0.5, 0.02, "BUY", 50000) - 0.51) < 1e-9
assert abs(fill_price(0.5, 0.02, "SELL", 50000) - 0.49) < 1e-9
assert abs(fill_price(0.5, 0.02, "BUY", 1000) - 0.52) < 1e-9   # thin book penalty
assert abs(fill_price(0.5, None, "BUY", 50000) - 0.51) < 1e-9  # unknown spread -> 0.02
assert fill_price(0.995, 0.02, "BUY", 50000) <= 0.999          # clamped
# round trip at constant mid must lose the spread (no free lunch)
assert fill_price(0.5, 0.02, "SELL", 50000) < fill_price(0.5, 0.02, "BUY", 50000)

# resolution-based review + auto-tuning is OFF
tmp = tempfile.mktemp(suffix=".db")
bot.DB = tmp
c = bot.db()
rules, rv = bot.get_rules(c)
assert rv == 1 and rules["min_copy_score"] == DEFAULT_RULES["min_copy_score"]
rowsin = [  # (condition, outcome_index, decision) -> c1 resolves outcome 0, c3 voids
    ("c1", 0, "paper_copy"),   # copied the winner -> good_copy
    ("c1", 1, "paper_copy"),   # copied the loser  -> bad_copy
    ("c1", 1, "skip"),         # skipped the loser -> skipped_loser, good
    ("c2", 0, "skip"),         # market not closed -> stays pending
    ("c3", 0, "paper_copy"),   # voided market (0.5/0.5) -> unpriced, not a win
]
for cid, oi, dec in rowsin:
    c.execute("""INSERT INTO observed_trades(wallet,condition_id,asset,question,outcome_index,
                 side,wallet_price,detected_price,size_usd,ts,created)
                 VALUES('0xw',?,?,'q',?,'BUY',0.5,0.5,200,1,1)""", (cid, cid + str(oi) + dec, oi))
    oid = c.execute("SELECT last_insert_rowid() i").fetchone()["i"]
    c.execute("INSERT INTO decisions(observed_id,wallet,decision,score,created) VALUES(?,'0xw',?,70,1)",
              (oid, dec))
c.commit()
MKT = {"c1": dict(liquidity=50000, spread=0.01, closed=True, hours_to_res=0, prices=[1.0, 0.0]),
       "c2": dict(liquidity=50000, spread=0.01, closed=False, hours_to_res=48, prices=[0.6, 0.4]),
       "c3": dict(liquidity=50000, spread=0.01, closed=True, hours_to_res=0, prices=[0.5, 0.5])}
bot.market_info = lambda cid: MKT[cid]
bot.step_review(c, rules, rv)
revs = {r["kind"]: r for r in c.execute(
    "SELECT r.*, d.decision FROM reviews r JOIN decisions d ON d.id=r.decision_id").fetchall()}
assert set(revs) == {"good_copy", "bad_copy", "skipped_loser", "unpriced"}, set(revs)
assert revs["good_copy"]["was_good"] == 1 and revs["good_copy"]["price_now"] == 1.0
assert revs["bad_copy"]["was_good"] == 0 and revs["bad_copy"]["price_now"] == 0.0
assert revs["skipped_loser"]["was_good"] == 1
assert revs["unpriced"]["was_good"] is None  # void resolution is not a win
# open market decision stayed pending; no rule ever changed
assert c.execute("SELECT COUNT(*) n FROM reviews").fetchone()["n"] == 4
assert bot.get_rules(c)[1] == 1
assert c.execute("SELECT COUNT(*) n FROM rule_changes").fetchone()["n"] == 0

# daily stop kill-switch: -5% on the day blocks new copies
day0 = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
c.execute("INSERT INTO portfolio_snapshots(at,total_pnl,realized,unrealized,open_count) VALUES(?,0,0,0,0)",
          (day0 - 100,))
c.commit()
assert not daily_stop_hit(c, rules)  # flat day
c.execute("""INSERT INTO paper_trades(wallet,condition_id,asset,question,side,entry,cur,size,shares,
             real,status,opened,closed) VALUES('0xw','c9','a9','q','BUY',0.5,0.0,10,20,-10,'resolved',?,?)""",
          (day0 + 1, day0 + 2))
c.commit()
assert daily_stop_hit(c, rules)      # -10 on 100 bankroll = -10% day
os.unlink(tmp)

# learning v2: learns from REALIZED resolution results, with guardrails
def fresh_db():
    t = tempfile.mktemp(suffix=".db")
    bot.DB = t
    cc = bot.db()
    return t, cc, *bot.get_rules(cc)

def seed_copies(cc, n, r, score, t0=1000):
    """n resolved paper copies with per-$1 return r at decision score `score`."""
    for i in range(n):
        cur = cc.execute("""INSERT INTO decisions(observed_id,wallet,decision,score,spread,liquidity,created)
                            VALUES(?,'0xw','paper_copy',?,0.01,50000,?)""", (t0 + i, score, t0 + i))
        cc.execute("""INSERT INTO paper_trades(decision_id,wallet,condition_id,asset,question,side,entry,
                      cur,size,shares,real,status,opened,closed) VALUES(?,'0xw','c','a','q','BUY',
                      0.5,0.0,10,20,?,'resolved',?,?)""", (cur.lastrowid, r * 10, t0 + i, t0 + i))
    cc.commit()

# consistent near-gate losses -> gate +2 and risk_max down; second call blocked by cooldown
tmp, c, rules, rv = fresh_db()
seed_copies(c, 24, -0.3, score=rules["min_copy_score"] + 2)
applied = {k: (b, a) for k, b, a, *_ in bot.learn_rules(c, rules, rv)}
assert applied["min_copy_score"] == (60.0, 62.0), applied
assert applied["risk_max"] == (0.02, 0.0175), applied
rules2, rv2 = bot.get_rules(c)
assert rv2 == 2 and rules2["min_copy_score"] == 62.0
assert bot.learn_rules(c, rules2, rv2) == []  # 7-day cooldown: no ratcheting
ch = c.execute("SELECT * FROM rule_changes").fetchall()
assert len(ch) == 2 and all(x["reason"] and x["evidence"] for x in ch)
# evidence recency: cooldown expired but SAME stale evidence -> still no change
c.execute("UPDATE rule_changes SET created=?", (bot.now() - 8 * 86400,))
c.commit()
assert bot.learn_rules(c, *bot.get_rules(c)) == []
# fresh losing evidence (resolved after the last change) -> fires again
seed_copies(c, 24, -0.3, score=64, t0=bot.now())
applied = {k: (b, a) for k, b, a, *_ in bot.learn_rules(c, *bot.get_rules(c))}
assert applied["min_copy_score"] == (62.0, 64.0), applied
os.unlink(tmp)

# consistent profits well above the gate -> gate widens, sizing up (bounded)
tmp, c, rules, rv = fresh_db()
seed_copies(c, 24, 0.3, score=80)
applied = {k: (b, a) for k, b, a, *_ in bot.learn_rules(c, rules, rv)}
assert applied["min_copy_score"] == (60.0, 58.0), applied
assert applied["risk_max"] == (0.02, 0.0225), applied
os.unlink(tmp)

# symmetric noise (mean ~0, halves disagree) -> learns NOTHING
tmp, c, rules, rv = fresh_db()
for i in range(12):
    seed_copies(c, 1, 0.3 if i % 2 == 0 else -0.3, score=61, t0=2000 + i)
seed_copies(c, 12, 0.0, score=61, t0=5000)
assert bot.learn_rules(c, rules, rv) == []
assert bot.get_rules(c)[1] == 1
os.unlink(tmp)

# too few samples -> no change even with a clear direction
tmp, c, rules, rv = fresh_db()
seed_copies(c, 10, -0.5, score=61)
assert bot.learn_rules(c, rules, rv) == []
os.unlink(tmp)

# backtest pure functions
import backtest as bt
s = bt.stats([1.0, -1.0, 0.5])
assert s["n"] == 3 and abs(s["mean"] - 0.5 / 3) < 1e-3 and s["win_rate"] == round(2 / 3, 3)
assert bt.stats([])["n"] == 0
assert bt.stats([-1.0, 1.0])["max_drawdown"] == 1.0  # peak 0 -> trough -1
assert bt.spearman([1, 2, 3], [10, 20, 30]) == 1.0
assert bt.spearman([1, 2, 3], [30, 20, 10]) == -1.0
assert bt.ret(dict(price=0.5, oi=0), [1.0, 0.0], 0.0) == 1.0    # 0.5 -> 1.0 doubles
assert bt.ret(dict(price=0.5, oi=1), [1.0, 0.0], 0.0) == -1.0   # total loss
assert bt.ret(dict(price=0.985, oi=0), [1.0, 0.0], 0.01) is None  # entry >= 0.99
assert bt.ret(dict(price=0.5, oi=2), [1.0, 0.0], 0.0) is None     # bad outcome index
cands = bt.candidates([
    dict(side="BUY", conditionId="c", price=0.5, size=400, timestamp=2, outcomeIndex=0, eventSlug="mlb-x"),
    dict(side="BUY", conditionId="c", price=0.6, size=400, timestamp=1, outcomeIndex=0, eventSlug="mlb-x"),
    dict(side="BUY", conditionId="d", price=0.99, size=400, timestamp=3, outcomeIndex=0),  # band
    dict(side="BUY", conditionId="e", price=0.5, size=10, timestamp=4, outcomeIndex=0),    # too small
    dict(side="SELL", conditionId="f", price=0.5, size=400, timestamp=5, outcomeIndex=0)])
assert len(cands) == 1 and cands[0]["price"] == 0.6  # first BUY chronologically, filters applied

# learning-signal simulation: resolution learning cuts a losing wallet, "none" never does
fin = {"m%d" % i: [0.0, 1.0] for i in range(15)}  # outcome 0 always loses
tw = [dict(cid="m%d" % i, oi=0, price=0.5, ts=i * 200000, cat="x") for i in range(15)]
sim = bt.simulate_learning({"w": {"oos": tw, "is": []}}, fin, ["w"], 0.0)
assert sim["none"]["n"] == 15
assert sim["resolution"]["n"] == 8  # dropped after MIN_N=8 known losses
assert sim["resolution"]["total"] > sim["none"]["total"]  # learning limited the damage

# safety: source must contain no signing/private-key/order-placement code
here = os.path.dirname(os.path.abspath(__file__))
for fname in ("bot.py", "backtest.py"):
    path = os.path.join(here, fname)
    if not os.path.exists(path): continue
    src = open(path).read()
    for banned in ("private_key", "privatekey", "sign_transaction", "signtransaction",
                   "web3", "eth_account", "post_order", "create_order", "apikey"):
        assert banned not in src.lower(), "banned token in %s: %s" % (fname, banned)
    assert "data=" not in src, "POST-capable urlopen in " + fname  # GETs only

print("ALL TESTS PASSED")
