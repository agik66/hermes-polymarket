#!/usr/bin/env python3
"""Self-check for the paper bot's pure logic. Run: python3 test_bot.py"""
import json, os, sqlite3, tempfile
import bot
from bot import (one_hit_penalty, score_wallet, score_trade, position_size,
                 paper_pnl, DEFAULT_RULES, clamp)

R = dict(DEFAULT_RULES)

# one-hit-wonder penalty
assert one_hit_penalty([100, 100, 100, 100]) == 0.25
assert one_hit_penalty([1000, 1, 1]) > 0.99
assert one_hit_penalty([-50, -20]) == 1.0  # no positive pnl = fully penalized
assert one_hit_penalty([]) == 1.0

# wallet scoring: diversified consistent wallet beats one-hit wonder
g1, c1, cp1 = score_wallet(0.25, 0.7, 40, 0.15, 500, 60, R)
g2, c2, cp2 = score_wallet(0.25, 0.7, 40, 0.95, 500, 60, R)
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

# bankroll sizing: fraction of equity, monotone in score, capped by cash, floor $5
assert R["size_min"] <= position_size(60, R, 100, 100) <= position_size(95, R, 100, 100)
assert position_size(95, R, 100, 100) <= R["risk_max"] * 100 + 0.01
assert position_size(95, R, 100, 7.5) == 7.5      # cash cap, no leverage
assert position_size(95, R, 100, 3) == 0.0        # below min stake -> no trade
assert position_size(95, R, 1000, 1000) > position_size(95, R, 100, 100)  # compounds

# paper pnl math: $10 at 0.50 -> 0.60 = +$2
assert abs(paper_pnl(0.50, 0.60, 10) - 2.0) < 1e-9
assert abs(paper_pnl(0.50, 0.0, 10) + 10.0) < 1e-9  # total loss capped at stake

# rule auto-update + versioning on synthetic evidence
tmp = tempfile.mktemp(suffix=".db")
bot.DB = tmp
c = bot.db()
rules, rv = bot.get_rules(c)
assert rv == 1 and rules["max_spread"] == DEFAULT_RULES["max_spread"]
# 3 losing high-spread copies (spread > 0.6*max_spread), reviewed
for i in range(3):
    c.execute("INSERT INTO observed_trades(wallet,condition_id,asset,question,side,wallet_price,detected_price,size_usd,ts,created) VALUES('0xw','c','a%d','q','BUY',0.5,0.5,200,1,1)" % i)
    oid = c.execute("SELECT last_insert_rowid() i").fetchone()["i"]
    c.execute("INSERT INTO decisions(observed_id,wallet,decision,score,spread,liquidity,drift,created) VALUES(?, '0xw','paper_copy',70,?,?,0.0,1)",
              (oid, 0.025, 50000))
    did = c.execute("SELECT last_insert_rowid() i").fetchone()["i"]
    c.execute("INSERT INTO reviews(decision_id,at,price_now,drift,was_good,kind,lesson) VALUES(?,2,0.45,-0.05,0,'bad_copy','x')", (did,))
c.commit()
bot.midpoint = lambda a: None  # no network in tests
bot.step_review(c, rules, rv)
rules2, rv2 = bot.get_rules(c)
assert rv2 == 2, rv2
assert rules2["max_spread"] < rules["max_spread"]
ch = c.execute("SELECT * FROM rule_changes").fetchall()
assert len(ch) >= 1 and ch[0]["old_version"] == 1 and ch[0]["new_version"] == 2
assert ch[0]["reason"] and ch[0]["evidence"]
os.unlink(tmp)

# safety: source must contain no signing/private-key/order-placement code
src = open(os.path.join(os.path.dirname(__file__), "bot.py")).read().lower()
for banned in ("private_key", "privatekey", "sign_transaction", "signtransaction",
               "web3", "eth_account", "post_order", "create_order", "apikey"):
    assert banned not in src, "banned token in source: " + banned
import urllib.request as _u  # bot only ever GETs: no data= kwarg used
assert "urlopen(req" in open(os.path.join(os.path.dirname(__file__), "bot.py")).read()
assert "data=" not in open(os.path.join(os.path.dirname(__file__), "bot.py")).read()

print("ALL TESTS PASSED")
