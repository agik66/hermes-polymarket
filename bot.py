#!/usr/bin/env python3
"""Hermes Polymarket copy-trading research bot. PAPER TRADING ONLY.

No private keys, no signing, no real orders. Read-only public APIs:
  data-api.polymarket.com  (leaderboard, trades, positions)
  gamma-api.polymarket.com (market metadata, liquidity, resolution)
  clob.polymarket.com      (midpoints, order books)

Commands: scan | monitor | pnl | review | report | export | cycle | loop
State: hermes.db (sqlite). Dashboard data: docs/data.json.
"""
import json, sqlite3, sys, time, urllib.request, urllib.parse, traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

DB = __file__.rsplit("/", 1)[0] + "/hermes.db"
DOCS = __file__.rsplit("/", 1)[0] + "/docs"
DATA_API = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

DEFAULT_RULES = {
    "min_global_score": 55.0,   # wallet quality gate for tracking
    "track_top_n": 15,          # max tracked wallets
    "min_copy_score": 60.0,     # trade score gate for paper_copy
    "max_spread": 0.03,
    "min_liquidity": 5000.0,
    "max_drift": 0.05,          # max adverse price move since wallet entry
    "min_wallet_trade_usd": 100.0,
    "min_hours_to_resolution": 1.0,
    "max_days_to_resolution": 30.0,
    "size_min": 5.0,
    "bankroll_start": 100.0, "target": 1000.0,   # paper bankroll: grow $100 -> $1000
    "risk_min": 0.05, "risk_max": 0.15,          # fraction of equity per trade by confidence
    "min_entry_price": 0.05, "max_entry_price": 0.92,  # no lottery tickets, no near-certainties
    "w_roi": 0.35, "w_consistency": 0.35, "w_copyability": 0.30,
}

def now(): return int(time.time())
def iso(ts=None): return datetime.fromtimestamp(ts or now(), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
def clamp(x, lo=0.0, hi=1.0): return max(lo, min(hi, x))

def http(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (hermes-paper-bot)"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            time.sleep(1 + i)
    raise RuntimeError("API failure %s: %s" % (url.split("?")[0], last))

# ---------------------------------------------------------------- db
SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets(
  address TEXT PRIMARY KEY, label TEXT, source_rank INT, status TEXT DEFAULT 'ignore',
  pnl30d REAL, vol30d REAL, roi30d REAL, consistency REAL, copyability REAL,
  one_hit_penalty REAL, global_score REAL, best_category TEXT, avg_trade_size REAL,
  trade_count_30d INT, resolved_count INT, win_rate REAL, status_reason TEXT,
  last_scanned INT, created INT);
CREATE TABLE IF NOT EXISTS observed_trades(
  id INTEGER PRIMARY KEY, wallet TEXT, condition_id TEXT, asset TEXT, question TEXT,
  category TEXT, outcome TEXT, outcome_index INT, side TEXT, wallet_price REAL,
  detected_price REAL, size_usd REAL, ts INT, created INT,
  UNIQUE(wallet, asset, ts, side));
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY, observed_id INT, wallet TEXT, decision TEXT, score REAL,
  confidence REAL, reasons TEXT, risks TEXT, breakdown TEXT, size REAL,
  spread REAL, liquidity REAL, drift REAL, hours_to_res REAL, created INT);
CREATE TABLE IF NOT EXISTS paper_trades(
  id INTEGER PRIMARY KEY, decision_id INT, wallet TEXT, condition_id TEXT, asset TEXT,
  question TEXT, outcome TEXT, outcome_index INT, side TEXT, entry REAL, cur REAL,
  size REAL, shares REAL, unreal REAL DEFAULT 0, real REAL, status TEXT DEFAULT 'open',
  reason TEXT, opened INT, closed INT);
CREATE TABLE IF NOT EXISTS pnl_snapshots(
  id INTEGER PRIMARY KEY, paper_id INT, price REAL, pnl REAL, at INT);
CREATE TABLE IF NOT EXISTS portfolio_snapshots(
  id INTEGER PRIMARY KEY, at INT, total_pnl REAL, realized REAL, unrealized REAL,
  open_count INT, blind_pnl REAL);
CREATE TABLE IF NOT EXISTS reviews(
  id INTEGER PRIMARY KEY, decision_id INT UNIQUE, at INT, price_now REAL, drift REAL,
  was_good INT, kind TEXT, lesson TEXT);
CREATE TABLE IF NOT EXISTS rulesets(
  id INTEGER PRIMARY KEY, version INT, active INT, json TEXT, created INT);
CREATE TABLE IF NOT EXISTS rule_changes(
  id INTEGER PRIMARY KEY, old_version INT, new_version INT, key TEXT, before REAL,
  after REAL, reason TEXT, evidence TEXT, created INT);
CREATE TABLE IF NOT EXISTS reports(
  id INTEGER PRIMARY KEY, date TEXT UNIQUE, json TEXT, created INT);
CREATE TABLE IF NOT EXISTS scans(
  id INTEGER PRIMARY KEY, at INT, source TEXT, wallet_count INT, note TEXT);
CREATE TABLE IF NOT EXISTS errors(
  id INTEGER PRIMARY KEY, at INT, step TEXT, error TEXT);
"""

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c

def get_rules(c):
    r = c.execute("SELECT * FROM rulesets WHERE active=1 ORDER BY version DESC LIMIT 1").fetchone()
    if not r:
        c.execute("INSERT INTO rulesets(version,active,json,created) VALUES(1,1,?,?)",
                  (json.dumps(DEFAULT_RULES), now()))
        c.commit()
        return dict(DEFAULT_RULES), 1
    merged = dict(DEFAULT_RULES)
    merged.update(json.loads(r["json"]))  # new default keys appear without resetting learned values
    return merged, r["version"]

# ---------------------------------------------------------------- scoring (pure, testable)
def one_hit_penalty(position_pnls):
    """Share of total positive pnl coming from the single best position. 0..1"""
    pos = [p for p in position_pnls if p > 0]
    if not pos: return 1.0
    return max(pos) / sum(pos)

def score_wallet(roi, win_rate, resolved_count, penalty, avg_size, trade_count, rules):
    roi_n = clamp(roi / 0.3)
    consistency = clamp(win_rate * (1 - 0.7 * penalty) * clamp(resolved_count / 10))
    # copyability: enough activity, human-copyable sizes, not one-hit
    freq = clamp(trade_count / 30)                     # ~1 trade/day is plenty
    size_n = 1.0 if 50 <= avg_size <= 20000 else (0.5 if avg_size < 50 else 0.3)
    copyability = clamp(0.5 * freq + 0.5 * size_n) * (1 - 0.5 * penalty)
    g = 100 * (rules["w_roi"] * roi_n + rules["w_consistency"] * consistency
               + rules["w_copyability"] * copyability)
    return round(g, 1), round(consistency, 3), round(copyability, 3)

def score_trade(wallet_score, drift, spread, liquidity, hours_to_res, size_usd, rules, mid=0.5):
    """Returns (score 0-100, breakdown, hard_fail_reason|None)."""
    if not rules["min_entry_price"] <= mid <= rules["max_entry_price"]:
        return 0, {}, "entry price %.3f outside copyable band %.2f-%.2f" % (
            mid, rules["min_entry_price"], rules["max_entry_price"])
    if abs(drift) > rules["max_drift"]:
        return 0, {}, "price moved %+.3f since wallet entry (max %.3f either way)" % (drift, rules["max_drift"])
    if spread is None or spread > rules["max_spread"]:
        return 0, {}, "spread %.3f > max %.3f" % (spread or 9, rules["max_spread"])
    if liquidity < rules["min_liquidity"]:
        return 0, {}, "liquidity %.0f < min %.0f" % (liquidity, rules["min_liquidity"])
    if size_usd < rules["min_wallet_trade_usd"]:
        return 0, {}, "wallet trade $%.0f below conviction min $%.0f" % (size_usd, rules["min_wallet_trade_usd"])
    if hours_to_res < rules["min_hours_to_resolution"]:
        return 0, {}, "resolves in %.1fh, too soon" % hours_to_res
    if hours_to_res > rules["max_days_to_resolution"] * 24:
        return 0, {}, "resolves in %.0fd, capital locked too long" % (hours_to_res / 24)
    b = {
        "wallet": clamp(wallet_score / 100),
        "entry_timing": 1 - clamp(drift / rules["max_drift"]) if drift > 0 else 1.0,
        "spread": 1 - clamp(spread / rules["max_spread"]),
        "liquidity": clamp(liquidity / (4 * rules["min_liquidity"])),
        "conviction": clamp(size_usd / (5 * rules["min_wallet_trade_usd"])),
        "time_fit": 1.0 if hours_to_res <= 14 * 24 else 0.6,
    }
    w = {"wallet": .35, "entry_timing": .20, "spread": .13, "liquidity": .12,
         "conviction": .12, "time_fit": .08}
    return round(100 * sum(b[k] * w[k] for k in w), 1), {k: round(v, 3) for k, v in b.items()}, None

def position_size(score, rules, equity, cash):
    """Confidence-scaled fraction of equity, capped by available cash (no leverage)."""
    conf = clamp((score - rules["min_copy_score"]) / max(1, 100 - rules["min_copy_score"]))
    size = equity * (rules["risk_min"] + (rules["risk_max"] - rules["risk_min"]) * conf)
    size = min(size, cash)
    return round(size, 2) if size >= rules["size_min"] else 0.0

def bankroll(c, rules):
    """(equity, cash) of the paper bankroll."""
    t = c.execute("""SELECT COALESCE(SUM(COALESCE(real,0)),0) r,
                            COALESCE(SUM(CASE WHEN status='open' THEN unreal ELSE 0 END),0) u,
                            COALESCE(SUM(CASE WHEN status='open' THEN size ELSE 0 END),0) o
                     FROM paper_trades""").fetchone()
    start = rules["bankroll_start"]
    return round(start + t["r"] + t["u"], 2), round(start + t["r"] - t["o"], 2)

def paper_pnl(entry, cur, size):
    shares = size / entry if entry > 0 else 0
    return round((cur - entry) * shares, 4)

# ---------------------------------------------------------------- steps
def step_scan(c, rules, depth=500, profile_n=50):
    """Leaderboard scan + wallet profiling. Profiles top `profile_n` of `depth` scanned
    (ponytail: deep-profiling all 500 = 1000+ API calls; top 50 by pnl covers copy candidates)."""
    rows = []
    for off in range(0, depth, 50):
        page = http("%s/v1/leaderboard?window=1m&limit=50&offset=%d" % (DATA_API, off))
        rows += page
        if len(page) < 50: break
    c.execute("INSERT INTO scans(at,source,wallet_count,note) VALUES(?,?,?,?)",
              (now(), "data-api/v1/leaderboard window=1m", len(rows),
               "profiled top %d of %d" % (profile_n, len(rows))))
    for i, r in enumerate(rows):
        c.execute("""INSERT INTO wallets(address,label,source_rank,pnl30d,vol30d,roi30d,created)
                     VALUES(?,?,?,?,?,?,?) ON CONFLICT(address) DO UPDATE SET
                     source_rank=excluded.source_rank, pnl30d=excluded.pnl30d,
                     vol30d=excluded.vol30d, roi30d=excluded.roi30d, label=excluded.label""",
                  (r["proxyWallet"], r.get("userName") or r["proxyWallet"][:10],
                   i + 1, r.get("pnl", 0), r.get("vol", 0),
                   (r.get("pnl", 0) / r["vol"]) if r.get("vol") else 0, now()))
    c.commit()

    def profile(addr):
        # positions drop redeemed winners, so wins come from REDEEM activity instead
        pos = http("%s/positions?user=%s&limit=500" % (DATA_API, addr))
        red = http("%s/activity?user=%s&type=REDEEM&limit=500" % (DATA_API, addr))
        tr = http("%s/trades?user=%s&limit=200" % (DATA_API, addr))
        cutoff = now() - 30 * 86400
        tr30 = [t for t in tr if t.get("timestamp", 0) >= cutoff]
        red30 = [x for x in red if x.get("timestamp", 0) >= cutoff]
        win_usdc = [x.get("usdcSize", 0) for x in red30 if x.get("usdcSize", 0) > 0]
        # ponytail: both feeds cap at 500 rows -> win_rate skews high for whales;
        # ranking still discriminates via penalty/roi/activity, and the paper-trade
        # review loop downgrades wallets that don't perform when copied.
        losers = [p for p in pos if p.get("curPrice", 0.5) <= 0.02 and not p.get("redeemable")]
        wins, resolved = len(win_usdc), len(win_usdc) + len(losers)
        sizes = [t["size"] * t["price"] for t in tr30 if t.get("size") and t.get("price")]
        cats = {}
        for t in tr30:
            k = (t.get("eventSlug") or "other").split("-")[0]
            cats[k] = cats.get(k, 0) + 1
        return dict(address=addr, penalty=round(one_hit_penalty(win_usdc), 3),
                    resolved=resolved, win_rate=round(wins / resolved, 3) if resolved else 0,
                    avg_size=round(sum(sizes) / len(sizes), 2) if sizes else 0,
                    trade_count=len(tr30), best_cat=max(cats, key=cats.get) if cats else "")

    top = rows[:profile_n]
    with ThreadPoolExecutor(8) as ex:
        profiles = list(ex.map(lambda r: profile(r["proxyWallet"]), top))
    scored = []
    for r, p in zip(top, profiles):
        roi = (r.get("pnl", 0) / r["vol"]) if r.get("vol") else 0
        g, cons, copy_ = score_wallet(roi, p["win_rate"], p["resolved"], p["penalty"],
                                      p["avg_size"], p["trade_count"], rules)
        scored.append((r["proxyWallet"], g, cons, copy_, p))
        c.execute("""UPDATE wallets SET consistency=?, copyability=?, one_hit_penalty=?,
                     global_score=?, best_category=?, avg_trade_size=?, trade_count_30d=?,
                     resolved_count=?, win_rate=?, last_scanned=? WHERE address=?""",
                  (cons, copy_, p["penalty"], g, p["best_cat"], p["avg_size"],
                   p["trade_count"], p["resolved"], p["win_rate"], now(), r["proxyWallet"]))
    # statuses: top N above gate = track
    scored.sort(key=lambda x: -x[1])
    tracked = 0
    for addr, g, cons, copy_, p in scored:
        if g >= rules["min_global_score"] and p["resolved"] >= 5 and tracked < rules["track_top_n"]:
            st, why = "track", "score %.0f, win rate %.0f%% on %d resolved, penalty %.2f" % (
                g, p["win_rate"] * 100, p["resolved"], p["penalty"])
            tracked += 1
        elif g >= 40:
            st, why = "watch", "score %.0f below gate %.0f or <5 resolved" % (g, rules["min_global_score"])
        else:
            st, why = "ignore", "score %.0f: penalty %.2f, win rate %.0f%%" % (g, p["penalty"], p["win_rate"] * 100)
        c.execute("UPDATE wallets SET status=?, status_reason=? WHERE address=?", (st, why, addr))
    c.commit()
    print("scan: %d wallets, %d profiled, %d tracked" % (len(rows), len(profiles), tracked))

def market_info(condition_id):
    m = http("%s/markets?condition_ids=%s" % (GAMMA, condition_id))
    if not m: return None
    m = m[0]
    end = m.get("endDate")
    hours = 0
    if end:
        try:
            hours = (datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp() - now()) / 3600
        except ValueError: pass
    return dict(liquidity=float(m.get("liquidity") or 0), spread=m.get("spread"),
                closed=m.get("closed"), hours_to_res=hours,
                prices=json.loads(m.get("outcomePrices") or "[]"))

def midpoint(asset):
    try:
        return float(http("%s/midpoint?token_id=%s" % (CLOB, asset), tries=1)["mid"])
    except Exception:
        return None

def step_monitor(c, rules, rv):
    """Detect new trades of tracked wallets, score, decide, open paper trades."""
    tracked = c.execute("SELECT * FROM wallets WHERE status='track'").fetchall()
    n_new = n_copy = 0
    for w in tracked:
        addr = w["address"]
        last = c.execute("SELECT MAX(ts) m FROM observed_trades WHERE wallet=?", (addr,)).fetchone()["m"] or 0
        trades = http("%s/trades?user=%s&limit=25" % (DATA_API, addr))
        for t in sorted(trades, key=lambda x: x.get("timestamp", 0)):
            ts = t.get("timestamp", 0)
            if ts <= last or ts < now() - 24 * 3600: continue
            side, asset = t.get("side"), t.get("asset")
            # SELL by tracked wallet = exit signal: close our matching open paper trade
            if side == "SELL":
                open_pt = c.execute("SELECT * FROM paper_trades WHERE wallet=? AND asset=? AND status='open'",
                                    (addr, asset)).fetchone()
                if open_pt:
                    mid = midpoint(asset) or open_pt["cur"] or open_pt["entry"]
                    pnl = paper_pnl(open_pt["entry"], mid, open_pt["size"])
                    c.execute("UPDATE paper_trades SET status='closed', cur=?, real=?, closed=?, reason=reason||' | closed: wallet exited' WHERE id=?",
                              (mid, pnl, now(), open_pt["id"]))
                continue
            if side != "BUY": continue
            size_usd = (t.get("size") or 0) * (t.get("price") or 0)
            mid = midpoint(asset)
            info = market_info(t["conditionId"]) if t.get("conditionId") else None
            detected = mid if mid is not None else t.get("price", 0)
            cur = c.execute("""INSERT OR IGNORE INTO observed_trades
                (wallet,condition_id,asset,question,category,outcome,outcome_index,side,
                 wallet_price,detected_price,size_usd,ts,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (addr, t.get("conditionId"), asset, t.get("title"), (t.get("eventSlug") or "").split("-")[0],
                 t.get("outcome"), t.get("outcomeIndex"), side, t.get("price"), detected, size_usd, ts, now()))
            if cur.rowcount == 0: continue
            oid = cur.lastrowid
            n_new += 1
            if info is None or mid is None or info["closed"]:
                dec, score, reasons, bd, sz = "skip", 0, ["market closed or no live price"], {}, 0
            else:
                drift = mid - t["price"]  # positive = we'd pay more than the wallet did
                dup = c.execute("SELECT 1 FROM paper_trades WHERE asset=? AND status='open'", (asset,)).fetchone()
                score, bd, fail = score_trade(w["global_score"] or 0, drift, info["spread"],
                                              info["liquidity"], info["hours_to_res"], size_usd, rules, mid=mid)
                if dup:
                    fail = "already holding an open paper position in this market"
                if fail:
                    dec, reasons, sz = "skip", [fail], 0
                elif score >= rules["min_copy_score"]:
                    eq, cash = bankroll(c, rules)
                    sz = position_size(score, rules, eq, cash)
                    dec = "paper_copy" if sz > 0 else "skip"
                    if sz <= 0:
                        reasons = ["insufficient paper cash ($%.2f) for min stake" % cash]
                    else:
                        reasons = ["wallet %s score %.0f" % (w["label"][:16], w["global_score"] or 0),
                               "copy score %.0f >= gate %.0f" % (score, rules["min_copy_score"]),
                               "spread %.3f, liq $%.0fk, drift %+.3f" % (info["spread"], info["liquidity"] / 1000, drift)]
                else:
                    dec, sz = "watchlist", 0
                    reasons = ["score %.0f below gate %.0f" % (score, rules["min_copy_score"])]
            risks = ["paper only", "liquidity may thin out", "wallet edge may be category-specific"]
            cur = c.execute("""INSERT INTO decisions(observed_id,wallet,decision,score,confidence,
                reasons,risks,breakdown,size,spread,liquidity,drift,hours_to_res,created)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (oid, addr, dec, score, score / 100, json.dumps(reasons), json.dumps(risks),
                 json.dumps(bd), sz, info and info["spread"], info and info["liquidity"],
                 mid - t["price"] if mid else None, info and info["hours_to_res"], now()))
            if dec == "paper_copy":
                n_copy += 1
                shares = sz / mid
                c.execute("""INSERT INTO paper_trades(decision_id,wallet,condition_id,asset,question,
                    outcome,outcome_index,side,entry,cur,size,shares,status,reason,opened)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?)""",
                    (cur.lastrowid, addr, t.get("conditionId"), asset, t.get("title"), t.get("outcome"),
                     t.get("outcomeIndex"), "BUY", mid, mid, sz, shares, "; ".join(reasons), now()))
        c.commit()
    print("monitor: %d new trades, %d paper copies (rules v%d)" % (n_new, n_copy, rv))

def step_pnl(c):
    """Hourly PnL update + resolution check for open paper trades."""
    open_pts = c.execute("SELECT * FROM paper_trades WHERE status='open'").fetchall()
    for pt in open_pts:
        info = market_info(pt["condition_id"])
        mid = midpoint(pt["asset"])
        if info and info["closed"]:
            final = None
            if info["prices"] and pt["outcome_index"] is not None and pt["outcome_index"] < len(info["prices"]):
                final = float(info["prices"][pt["outcome_index"]])
            final = final if final is not None else (mid or pt["cur"] or pt["entry"])
            pnl = paper_pnl(pt["entry"], final, pt["size"])
            c.execute("UPDATE paper_trades SET status='resolved', cur=?, real=?, unreal=0, closed=? WHERE id=?",
                      (final, pnl, now(), pt["id"]))
            c.execute("INSERT INTO pnl_snapshots(paper_id,price,pnl,at) VALUES(?,?,?,?)",
                      (pt["id"], final, pnl, now()))
        elif mid is not None:
            pnl = paper_pnl(pt["entry"], mid, pt["size"])
            c.execute("UPDATE paper_trades SET cur=?, unreal=? WHERE id=?", (mid, pnl, pt["id"]))
            c.execute("INSERT INTO pnl_snapshots(paper_id,price,pnl,at) VALUES(?,?,?,?)",
                      (pt["id"], mid, pnl, now()))
    # blind-copy benchmark: every observed BUY of tracked wallets, $10 at detected price
    blind = 0.0
    for ot in c.execute("""SELECT ot.* FROM observed_trades ot WHERE ot.side='BUY'""").fetchall():
        pt = c.execute("SELECT cur FROM paper_trades WHERE asset=? AND status IN ('open','closed','resolved') ORDER BY id DESC LIMIT 1",
                       (ot["asset"],)).fetchone()
        rv = c.execute("SELECT price_now FROM reviews r JOIN decisions d ON d.id=r.decision_id WHERE d.observed_id=? ORDER BY r.at DESC LIMIT 1",
                       (ot["id"],)).fetchone()
        cur_price = (pt and pt["cur"]) or (rv and rv["price_now"])
        if cur_price and ot["detected_price"]:
            blind += paper_pnl(ot["detected_price"], cur_price, 10.0)
    tot = c.execute("""SELECT COALESCE(SUM(CASE WHEN status='open' THEN unreal ELSE 0 END),0) u,
                              COALESCE(SUM(COALESCE(real,0)),0) r,
                              SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) o FROM paper_trades""").fetchone()
    c.execute("INSERT INTO portfolio_snapshots(at,total_pnl,realized,unrealized,open_count,blind_pnl) VALUES(?,?,?,?,?,?)",
              (now(), round(tot["u"] + tot["r"], 4), round(tot["r"], 4), round(tot["u"], 4), tot["o"] or 0, round(blind, 4)))
    c.commit()
    print("pnl: %d open updated, total %.2f (blind bench %.2f)" % (len(open_pts), tot["u"] + tot["r"], blind))

def step_review(c, rules, rv):
    """Judge decisions >=1h old; extract lessons; auto-update rules with evidence."""
    pend = c.execute("""SELECT d.*, ot.asset, ot.detected_price FROM decisions d
        JOIN observed_trades ot ON ot.id=d.observed_id
        LEFT JOIN reviews r ON r.decision_id=d.id
        WHERE r.id IS NULL AND d.created <= ?""", (now() - 3600,)).fetchall()
    for d in pend:
        price = midpoint(d["asset"])
        if price is None:
            info = market_info(c.execute("SELECT condition_id FROM observed_trades WHERE id=?",
                                         (d["observed_id"],)).fetchone()["condition_id"])
            oi = c.execute("SELECT outcome_index FROM observed_trades WHERE id=?", (d["observed_id"],)).fetchone()["outcome_index"]
            if info and info["prices"] and oi is not None and oi < len(info["prices"]):
                price = float(info["prices"][oi])
        if price is None: continue
        drift = price - (d["detected_price"] or price)
        if d["decision"] == "paper_copy":
            good, kind = (1 if drift > 0 else 0), ("good_copy" if drift > 0 else "bad_copy")
            lesson = "copied at %.3f, now %.3f" % (d["detected_price"], price)
        elif drift > 0.03:
            good, kind, lesson = 0, "missed_winner", "skipped/watched, price rose %.3f->%.3f" % (d["detected_price"], price)
        elif drift < -0.03:
            good, kind, lesson = 1, "avoided_loser", "stayed out, price fell %.3f->%.3f" % (d["detected_price"], price)
        else:
            good, kind, lesson = 1, "neutral", "price flat since decision"
        c.execute("INSERT OR IGNORE INTO reviews(decision_id,at,price_now,drift,was_good,kind,lesson) VALUES(?,?,?,?,?,?,?)",
                  (d["id"], now(), price, round(drift, 4), good, kind, lesson))
    c.commit()

    # ---- learning: evidence-driven threshold updates (the bot's "learning" core)
    changes = []
    rows = c.execute("""SELECT d.*, r.drift rdrift, r.kind FROM decisions d
                        JOIN reviews r ON r.decision_id=d.id""").fetchall()
    copies = [x for x in rows if x["decision"] == "paper_copy"]
    def avg(xs): return sum(xs) / len(xs)
    hs = [x["rdrift"] for x in copies if (x["spread"] or 0) > 0.6 * rules["max_spread"]]
    if len(hs) >= 3 and avg(hs) < 0:
        changes.append(("max_spread", rules["max_spread"], round(rules["max_spread"] * 0.85, 4),
                        "high-spread copies losing", "%d trades, avg drift %.3f" % (len(hs), avg(hs))))
    ll = [x["rdrift"] for x in copies if (x["liquidity"] or 0) < 2 * rules["min_liquidity"]]
    if len(ll) >= 3 and avg(ll) < 0:
        changes.append(("min_liquidity", rules["min_liquidity"], round(rules["min_liquidity"] * 1.3, 0),
                        "low-liquidity copies losing", "%d trades, avg drift %.3f" % (len(ll), avg(ll))))
    lt = [x["rdrift"] for x in copies if (x["drift"] or 0) > 0.6 * rules["max_drift"]]
    if len(lt) >= 3 and avg(lt) < 0:
        changes.append(("max_drift", rules["max_drift"], round(rules["max_drift"] * 0.85, 4),
                        "late entries losing", "%d trades, avg drift %.3f" % (len(lt), avg(lt))))
    missed = [x for x in rows if x["kind"] == "missed_winner" and x["score"] and x["score"] >= rules["min_copy_score"] - 10]
    if len(missed) >= 3 and rules["min_copy_score"] > 45:
        changes.append(("min_copy_score", rules["min_copy_score"], round(rules["min_copy_score"] - 2, 1),
                        "missing winners just under the gate", "%d near-miss winners" % len(missed)))
    if len(copies) >= 5 and avg([x["rdrift"] for x in copies]) < -0.01 and rules["min_copy_score"] < 80:
        changes.append(("min_copy_score", rules["min_copy_score"], round(rules["min_copy_score"] + 3, 1),
                        "copies losing overall, tighten gate", "%d copies, avg drift %.3f" % (len(copies), avg([x["rdrift"] for x in copies]))))
    if changes:
        merged = dict(rules)
        seen = set()
        applied = []
        for k, before, after, reason, ev in changes:
            if k in seen: continue  # one change per key per run
            seen.add(k); merged[k] = after; applied.append((k, before, after, reason, ev))
        nv = rv + 1
        c.execute("UPDATE rulesets SET active=0 WHERE active=1")
        c.execute("INSERT INTO rulesets(version,active,json,created) VALUES(?,1,?,?)", (nv, json.dumps(merged), now()))
        for k, before, after, reason, ev in applied:
            c.execute("INSERT INTO rule_changes(old_version,new_version,key,before,after,reason,evidence,created) VALUES(?,?,?,?,?,?,?,?)",
                      (rv, nv, k, before, after, reason, ev, now()))
        c.commit()
        print("review: rules v%d -> v%d: %s" % (rv, nv, ", ".join(k for k, *_ in applied)))
    # wallet downgrades on bad paper performance
    for w in c.execute("SELECT wallet, AVG(COALESCE(real,unreal)) p, COUNT(*) n FROM paper_trades GROUP BY wallet").fetchall():
        if w["n"] >= 3 and w["p"] < -0.5:
            c.execute("UPDATE wallets SET status='watch', status_reason='downgraded: avg paper pnl %.2f over %d copies' WHERE address=? AND status='track'",
                      (w["p"], w["n"], w["wallet"]))
    c.commit()
    print("review: %d decisions reviewed, %d rule changes" % (len(pend), len(changes)))

def step_report(c):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day0 = int(datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    pts = c.execute("SELECT * FROM paper_trades").fetchall()
    resolved = [p for p in pts if p["status"] in ("resolved", "closed") and p["real"] is not None]
    wins = [p for p in resolved if p["real"] > 0]
    snap = c.execute("SELECT * FROM portfolio_snapshots ORDER BY at DESC LIMIT 1").fetchone()
    decs = c.execute("SELECT decision, COUNT(*) n FROM decisions WHERE created>=? GROUP BY decision", (day0,)).fetchall()
    dmap = {d["decision"]: d["n"] for d in decs}
    best = max(pts, key=lambda p: (p["real"] if p["real"] is not None else p["unreal"] or 0), default=None)
    worst = min(pts, key=lambda p: (p["real"] if p["real"] is not None else p["unreal"] or 0), default=None)
    rc = c.execute("SELECT * FROM rule_changes WHERE created>=?", (day0,)).fetchall()
    lessons = c.execute("SELECT lesson, kind FROM reviews ORDER BY at DESC LIMIT 5").fetchall()
    rep = {
        "date": today,
        "total_paper_pnl": snap and snap["total_pnl"] or 0,
        "blind_benchmark_pnl": snap and snap["blind_pnl"] or 0,
        "beat_blind_copy": bool(snap and snap["total_pnl"] >= (snap["blind_pnl"] or 0) * min(1, len(pts) * 10 / max(1, len(pts) * 10))),
        "win_rate": round(len(wins) / len(resolved), 3) if resolved else None,
        "open_positions": snap and snap["open_count"] or 0,
        "signals": dmap,
        "best_trade": best and {"q": best["question"], "pnl": best["real"] if best["real"] is not None else best["unreal"]},
        "worst_trade": worst and {"q": worst["question"], "pnl": worst["real"] if worst["real"] is not None else worst["unreal"]},
        "rule_changes": [{"key": r["key"], "before": r["before"], "after": r["after"], "reason": r["reason"]} for r in rc],
        "top_lesson": lessons[0]["lesson"] if lessons else "not enough data yet",
        "watch_tomorrow": "review open positions and near-gate watchlist signals",
    }
    c.execute("INSERT OR REPLACE INTO reports(date,json,created) VALUES(?,?,?)", (today, json.dumps(rep), now()))
    c.commit()
    print("report: %s pnl=%.2f" % (today, rep["total_paper_pnl"]))

def step_export(c):
    """Write docs/data.json for the static dashboard."""
    def rows(q, *a): return [dict(r) for r in c.execute(q, *a).fetchall()]
    rules, rv = get_rules(c)
    out = {
        "generated_at": iso(), "generated_ts": now(),
        "safety": "PAPER TRADING ONLY — no real orders, no private keys. Not financial advice.",
        "rules": rules, "rules_version": rv,
        "wallets": rows("SELECT * FROM wallets WHERE global_score IS NOT NULL ORDER BY global_score DESC LIMIT 100"),
        "leaderboard_size": dict(c.execute("SELECT wallet_count FROM scans ORDER BY at DESC LIMIT 1").fetchone() or {"wallet_count": 0}),
        "signals": rows("""SELECT d.*, ot.question, ot.wallet_price, ot.detected_price, ot.size_usd, ot.outcome, ot.ts
                           FROM decisions d JOIN observed_trades ot ON ot.id=d.observed_id
                           ORDER BY d.created DESC LIMIT 200"""),
        "paper_trades": rows("SELECT * FROM paper_trades ORDER BY opened DESC"),
        "portfolio": rows("SELECT * FROM portfolio_snapshots ORDER BY at"),
        "reviews": rows("""SELECT r.*, d.decision, d.score, ot.question FROM reviews r
                           JOIN decisions d ON d.id=r.decision_id
                           JOIN observed_trades ot ON ot.id=d.observed_id ORDER BY r.at DESC LIMIT 200"""),
        "rule_changes": rows("SELECT * FROM rule_changes ORDER BY created DESC"),
        "rulesets": rows("SELECT version, active, json, created FROM rulesets ORDER BY version DESC"),
        "reports": rows("SELECT * FROM reports ORDER BY date DESC LIMIT 14"),
        "scans": rows("SELECT * FROM scans ORDER BY at DESC LIMIT 10"),
        "errors": rows("SELECT * FROM errors ORDER BY at DESC LIMIT 20"),
    }
    with open(DOCS + "/data.json", "w") as f:
        json.dump(out, f, default=str)
    print("export: docs/data.json written")

def cycle():
    c = db()
    steps = [("scan", lambda: step_scan(c, get_rules(c)[0])),
             ("monitor", lambda: step_monitor(c, *get_rules(c))),
             ("pnl", lambda: step_pnl(c)),
             ("review", lambda: step_review(c, *get_rules(c))),
             ("report", lambda: step_report(c)),
             ("export", lambda: step_export(c))]
    # scan is heavy; only rescan if last scan >6h old
    last = c.execute("SELECT MAX(at) m FROM scans").fetchone()["m"] or 0
    if now() - last < 6 * 3600:
        steps = steps[1:]
    for name, fn in steps:
        try:
            fn()
        except Exception as e:
            c.execute("INSERT INTO errors(at,step,error) VALUES(?,?,?)", (now(), name, str(e)[:500]))
            c.commit()
            print("ERROR %s: %s" % (name, e), file=sys.stderr)
            traceback.print_exc()

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "cycle"
    c = db()
    if cmd == "scan": step_scan(c, get_rules(c)[0])
    elif cmd == "monitor": step_monitor(c, *get_rules(c))
    elif cmd == "pnl": step_pnl(c)
    elif cmd == "review": step_review(c, *get_rules(c))
    elif cmd == "report": step_report(c)
    elif cmd == "export": step_export(c)
    elif cmd == "cycle": cycle()
    elif cmd == "loop":
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else 900
        while True:
            cycle()
            time.sleep(interval)
    else:
        print(__doc__)
