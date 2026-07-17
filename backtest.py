#!/usr/bin/env python3
"""Out-of-sample backtest: does copying Polymarket leaderboard wallets have edge?

Method
  1. Universe: current 30d-leaderboard top N wallets (via data-api).
  2. Pull each wallet's trade history back HIST_DAYS (paginated, read-only GETs).
  3. Copy candidates: BUY, >= $100, price 0.05-0.92, first BUY per (wallet, market).
  4. Resolve each market via gamma outcomePrices (batched, cached in sqlite).
     Only clearly resolved markets count (a final price >= 0.95 on some outcome).
  5. Walk-forward split:
       in-sample  (IS):  trades older than OOS_START_DAYS -> ONLY rank/select wallets
       out-sample (OOS): trades in [now-OOS_START_DAYS, now-OOS_END_DAYS] -> evaluation
     The last OOS_END_DAYS are dropped entirely: gamma flips `closed` days late,
     so recent trades would only count when de-facto decided = unresolved bias.
  6. Costs: entry = wallet's own fill + COST cents (stands in for spread crossing
     + copy latency drift). Scenarios 0/1/2c. Return per $1 = (final-entry)/entry.

Known biases -- all INFLATE the result, so a negative verdict is robust,
a positive one is an upper bound:
  * Survivorship: today's leaderboard omits wallets that blew up earlier.
  * Selection look-ahead: leaderboard ranks on the last 30d, which overlaps OOS.
    (No historical leaderboard snapshots exist in the public API.)
  * IS resolution look-ahead: an IS trade whose market resolved only recently still
    counts toward selection; a selector standing at the split date couldn't know it.
    (Gamma has no resolution timestamps to filter on.)
  * OOS drops still-unresolved markets -> skews toward fast-resolving ones.
  * Assumes full fill at wallet price + cost; real books may be thinner.
  * Hyperactive wallets (>3000 trades in the window) are undersampled: pagination
    caps at 3000 rows. Those are HFT/market-maker style and uncopyable anyway;
    wallets whose history never reaches the IS period are reported, not hidden.

PAPER/RESEARCH ONLY. Read-only GETs, no keys, no orders.
"""
import json, sqlite3, sys, time
from concurrent.futures import ThreadPoolExecutor
from bot import http, now, DATA_API, GAMMA, DB

HIST_DAYS = 90       # how far back to pull wallet trades
OOS_START_DAYS = 28  # OOS = [now-28d, now-7d]
OOS_END_DAYS = 7     # last 7d dropped: resolution-flag lag
N_WALLETS = 50      # leaderboard wallets to test
TOP_K = 5           # portfolio size selected on IS
MIN_IS_TRADES = 10  # wallet needs this many resolved IS trades to be selectable
COSTS = [0.0, 0.01, 0.02]  # entry penalty scenarios (spread + latency)
MIN_USD, PMIN, PMAX = 100.0, 0.05, 0.92  # same copy filters as the live bot

def fetch_wallet_trades(addr, cutoff):
    out, offset = [], 0
    while offset < 3000:
        try:
            page = http("%s/trades?user=%s&limit=500&offset=%d" % (DATA_API, addr, offset))
        except Exception:
            break
        if not isinstance(page, list) or not page: break
        out += page
        if len(page) < 500 or min(t.get("timestamp", 0) for t in page) < cutoff: break
        offset += 500
    return [t for t in out if t.get("timestamp", 0) >= cutoff]

def candidates(trades):
    """First qualifying BUY per market, chronological."""
    seen, out = set(), []
    for t in sorted(trades, key=lambda x: x.get("timestamp", 0)):
        cid, px = t.get("conditionId"), t.get("price") or 0
        usd = (t.get("size") or 0) * px
        if t.get("side") != "BUY" or not cid or cid in seen: continue
        if usd < MIN_USD or not PMIN <= px <= PMAX: continue
        if t.get("outcomeIndex") is None: continue
        seen.add(cid)
        out.append(dict(cid=cid, oi=t["outcomeIndex"], price=px, ts=t["timestamp"],
                        cat=(t.get("eventSlug") or "other").split("-")[0]))
    return out

def resolutions(cids):
    """{condition_id: [final outcome prices]} for clearly resolved markets, sqlite-cached."""
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS bt_markets(
        condition_id TEXT PRIMARY KEY, closed INT, prices TEXT, fetched INT)""")
    cached = {r[0]: (r[1], r[2]) for r in
              c.execute("SELECT condition_id, closed, prices FROM bt_markets").fetchall()}
    todo = [x for x in cids if x not in cached or not cached[x][0]]  # refetch unresolved
    batches = [todo[i:i + 100] for i in range(0, len(todo), 100)]
    fails = [0]
    def grab(batch):
        # gamma omits closed markets unless closed=true; we only need resolved ones
        try:
            return batch, http(GAMMA + "/markets?closed=true&" + "&".join("condition_ids=" + b for b in batch))
        except Exception:
            fails[0] += 1
            return batch, None
    with ThreadPoolExecutor(3) as ex:
        for batch, res in ex.map(grab, batches):
            if res is None: continue  # leave uncached -> retried next run
            got = set()
            for m in res:
                cid = m.get("conditionId")
                if not cid: continue
                prices = json.loads(m.get("outcomePrices") or "[]")
                c.execute("INSERT OR REPLACE INTO bt_markets VALUES(?,1,?,?)",
                          (cid, json.dumps(prices), now()))
                cached[cid] = (1, json.dumps(prices))
                got.add(cid)
            for cid in batch:
                if cid not in got and not (cached.get(cid) or (0,))[0]:
                    c.execute("INSERT OR REPLACE INTO bt_markets VALUES(?,0,'[]',?)", (cid, now()))
                    cached[cid] = (0, "[]")
    # second pass: markets gamma doesn't list as closed yet, but whose live price
    # is at an extreme = de-facto decided (same 0.995/0.005 rule as the live bot)
    open_todo = [x for x in cids if not (cached.get(x) or (0,))[0]]
    open_batches = [open_todo[i:i + 100] for i in range(0, len(open_todo), 100)]
    def grab_open(batch):
        try:
            return http(GAMMA + "/markets?" + "&".join("condition_ids=" + b for b in batch))
        except Exception:
            fails[0] += 1
            return []
    with ThreadPoolExecutor(3) as ex:
        for res in ex.map(grab_open, open_batches):
            for m in res or []:
                cid = m.get("conditionId")
                if not cid: continue
                prices = json.loads(m.get("outcomePrices") or "[]")
                c.execute("INSERT OR REPLACE INTO bt_markets VALUES(?,0,?,?)",
                          (cid, json.dumps(prices), now()))
                cached[cid] = (0, json.dumps(prices))
    c.commit(); c.close()
    if fails[0]:
        print("WARNING: %d resolution batches failed (likely rate limit) - rerun to fill" % fails[0])
    out = {}
    for cid, (closed, pj) in cached.items():
        prices = [float(p) for p in json.loads(pj or "[]")]
        if not prices: continue
        if closed and max(prices) >= 0.95:          # officially resolved
            out[cid] = prices
        elif not closed and max(prices) >= 0.995:   # de-facto decided
            out[cid] = prices
    return out

def stats(rets):
    """Flat $1 per trade: n, win rate, mean, t-stat, total, max drawdown."""
    n = len(rets)
    if n == 0: return dict(n=0)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1) if n > 1 else 0.0
    sd = var ** 0.5
    t = mean / (sd / n ** 0.5) if sd > 0 and n > 1 else 0.0
    cum = peak = dd = 0.0
    for r in rets:
        cum += r; peak = max(peak, cum); dd = max(dd, peak - cum)
    return dict(n=n, win_rate=round(sum(r > 0 for r in rets) / n, 3), mean=round(mean, 4),
                sd=round(sd, 4), t_stat=round(t, 2), total=round(sum(rets), 2),
                max_drawdown=round(dd, 2))

def spearman(a, b):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for rank, i in enumerate(order): r[i] = rank
        return r
    ra, rb = ranks(a), ranks(b)
    n = len(a)
    if n < 3: return None
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra) ** 0.5
    vb = sum((y - mb) ** 2 for y in rb) ** 0.5
    return round(cov / (va * vb), 3) if va and vb else None

def simulate_learning(per_wallet, finals, top, cost):
    """Learning-signal comparison on the wallet-selection dimension over OOS:
      none       — keep the IS-selected set for the whole window
      resolution — drop a wallet once >= MIN_N of its copies RESOLVED with negative
                   mean return; resolution knowledge arrives RES_LAG after the trade
      noise      — identical rule, but labels are coin flips: the old 1h-drift signal
                   was empirically ~50/50 (AUDIT.md P0). Seeded, averaged over N_RUNS.
    The 1h-drift series itself is not reconstructable offline (no public minute-level
    price history), hence the coin-flip emulation of its information content.
    Limitations: this tests the wallet-selection lever, not the live loop's threshold
    keys (conclusion transfers by analogy); RES_LAG=2d is FASTER than real resolution
    feedback, which flatters the resolution learner — it still underperforms.
    """
    import random
    MIN_N, RES_LAG, N_RUNS = 8, 2 * 86400, 20
    trades = sorted(((a, t) for a in top for t in per_wallet[a]["oos"]), key=lambda x: x[1]["ts"])
    def run(label=None):
        active, hist, rets, seen = set(top), {a: [] for a in top}, [], set()
        for a, t in trades:
            if t["cid"] in seen: continue
            if label is not None:
                known = [l for tk, l in hist[a] if tk <= t["ts"]]
                if a in active and len(known) >= MIN_N and sum(known) < 0:
                    active.discard(a)
            if a not in active: continue
            r = ret(t, finals[t["cid"]], cost)
            if r is None: continue
            seen.add(t["cid"]); rets.append(r)
            if label is not None: hist[a].append((t["ts"] + RES_LAG, label(r)))
        return rets
    out = {"none": stats(run()), "resolution": stats(run(lambda r: r))}
    noise = []
    for i in range(N_RUNS):
        rng = random.Random(1000 + i)
        noise.append(stats(run(lambda r: rng.choice((1.0, -1.0)))))
    ns = [s for s in noise if s["n"]]
    out["noise_1h_drift_emulation"] = {
        "runs": N_RUNS, "avg_n": round(sum(s.get("n", 0) for s in noise) / len(noise), 1),
        "mean_of_means": round(sum(s["mean"] for s in ns) / len(ns), 4) if ns else None,
        "mean_total": round(sum(s["total"] for s in ns) / len(ns), 2) if ns else None}
    return out

def ret(trade, finals, cost):
    entry = trade["price"] + cost
    if entry >= 0.99 or trade["oi"] >= len(finals): return None
    return (finals[trade["oi"]] - entry) / entry

def main():
    t_split = now() - OOS_START_DAYS * 86400
    t_end = now() - OOS_END_DAYS * 86400
    cutoff = now() - HIST_DAYS * 86400
    lb = []
    for off in range(0, N_WALLETS, 50):
        lb += http("%s/v1/leaderboard?window=1m&limit=50&offset=%d" % (DATA_API, off))
    lb = lb[:N_WALLETS]
    print("backtest: %d wallets, history %dd, OOS window = [-%dd, -%dd]" % (
        len(lb), HIST_DAYS, OOS_START_DAYS, OOS_END_DAYS))

    with ThreadPoolExecutor(8) as ex:
        histories = list(ex.map(lambda w: fetch_wallet_trades(w["proxyWallet"], cutoff), lb))
    shallow = [w.get("userName") or w["proxyWallet"][:10] for w, h in zip(lb, histories)
               if h and min(t.get("timestamp", 0) for t in h) > t_split]
    if shallow:
        print("NOTE: %d hyperactive wallets never reach the IS period (pagination cap), excluded by min-IS-trades rule: %s"
              % (len(shallow), ", ".join(shallow)))
    cands = {w["proxyWallet"]: candidates(h) for w, h in zip(lb, histories)}
    all_cids = {t["cid"] for ts in cands.values() for t in ts}
    finals = resolutions(list(all_cids))
    print("markets: %d touched, %d clearly resolved" % (len(all_cids), len(finals)))

    # per-wallet IS/OOS resolved trades (returns computed on demand per cost)
    per_wallet = {}
    for addr, ts in cands.items():
        w = {"is": [], "oos": []}
        for t in ts:
            if t["cid"] not in finals or t["ts"] >= t_end: continue
            w["is" if t["ts"] < t_split else "oos"].append(t)
        per_wallet[addr] = w

    def wallet_rets(trades, cost):
        rs = (ret(t, finals[t["cid"]], cost) for t in trades)
        return [r for r in rs if r is not None]

    sel_cost = COSTS[-1]  # select on the most conservative cost
    eligible = []
    for a, w in per_wallet.items():
        rs = wallet_rets(w["is"], sel_cost)
        if len(rs) >= MIN_IS_TRADES: eligible.append((a, sum(rs) / len(rs)))
    eligible.sort(key=lambda x: -x[1])
    top = [a for a, _ in eligible[:TOP_K]]
    # control must not overlap the selection
    bottom = [a for a, _ in eligible[-TOP_K:]] if len(eligible) >= 2 * TOP_K else []
    labels = {w["proxyWallet"]: (w.get("userName") or w["proxyWallet"][:10]) for w in lb}
    print("eligible wallets (>=%d resolved IS trades): %d" % (MIN_IS_TRADES, len(eligible)))
    print("selected on IS mean@%dc: %s" % (100 * sel_cost, ", ".join(labels[a] for a in top)))

    def pooled(addrs, side, cost):
        """Chronological, one return per market across the whole group: two wallets
        buying the same market is one correlated bet, not two independent ones."""
        seen, out = set(), []
        for t in sorted((t for a in addrs for t in per_wallet[a][side]), key=lambda x: x["ts"]):
            if t["cid"] in seen: continue
            seen.add(t["cid"])
            r = ret(t, finals[t["cid"]], cost)
            if r is not None: out.append(r)
        return out

    results = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "params": dict(
        hist_days=HIST_DAYS, oos_window_days=[OOS_START_DAYS, OOS_END_DAYS],
        n_wallets=N_WALLETS, top_k=TOP_K,
        min_is_trades=MIN_IS_TRADES, min_usd=MIN_USD, band=[PMIN, PMAX]),
        "selected": [labels[a] for a in top], "scenarios": {}}
    for cost in COSTS:
        sc = {
            "selected_top%d_OOS" % TOP_K: stats(pooled(top, "oos", cost)),
            "all_wallets_OOS_blind": stats(pooled(list(per_wallet), "oos", cost)),
            "bottom%d_OOS_control" % TOP_K: stats(pooled(bottom, "oos", cost)),
            "selected_top%d_IS_for_reference" % TOP_K: stats(pooled(top, "is", cost)),
        }
        results["scenarios"]["cost_%dc" % round(100 * cost)] = sc
        print("\n--- cost +%dc ---" % round(100 * cost))
        for k, v in sc.items(): print("  %-28s %s" % (k, v))

    # does IS performance predict OOS performance at all?
    pairs = []
    for w in per_wallet.values():
        ri, ro = wallet_rets(w["is"], sel_cost), wallet_rets(w["oos"], sel_cost)
        if len(ri) >= MIN_IS_TRADES and len(ro) >= 5:
            pairs.append((sum(ri) / len(ri), sum(ro) / len(ro)))
    rho = spearman([p[0] for p in pairs], [p[1] for p in pairs]) if len(pairs) >= 3 else None
    results["is_oos_rank_correlation"] = {"rho": rho, "wallets": len(pairs)}
    print("\nIS->OOS persistence: spearman rho=%s over %d wallets" % (rho, len(pairs)))

    # category breakdown of selected portfolio OOS (where do returns come from)
    cat = {}
    for a in top:
        for t in per_wallet[a]["oos"]:
            r = ret(t, finals[t["cid"]], sel_cost)
            if r is not None: cat.setdefault(t["cat"], []).append(r)
    results["selected_OOS_by_category"] = {
        k: dict(n=len(v), mean=round(sum(v) / len(v), 4)) for k, v in sorted(cat.items())}
    print("selected OOS by category:", results["selected_OOS_by_category"])

    # learning-signal comparison: none vs resolution-driven vs old noise loop
    sim = simulate_learning(per_wallet, finals, top, sel_cost)
    results["learning_comparison_at_+%dc" % round(100 * sel_cost)] = sim
    print("\nlearning-signal comparison on selected portfolio (OOS, +%dc):" % round(100 * sel_cost))
    for k, v in sim.items(): print("  %-26s %s" % (k, v))

    oos = results["scenarios"]["cost_%dc" % round(100 * sel_cost)]["selected_top%d_OOS" % TOP_K]
    edge = bool(oos.get("n", 0) >= 30 and oos.get("mean", 0) > 0 and oos.get("t_stat", 0) >= 2)
    results["verdict"] = {"edge_after_costs": edge, "note":
        "edge requires OOS mean>0 with t>=2 on n>=30 at +%dc cost; biases inflate results" % (100 * sel_cost)}
    print("\nVERDICT: edge after costs = %s" % edge)
    with open(__file__.rsplit("/", 1)[0] + "/backtest_results.json", "w") as f:
        json.dump(results, f, indent=1)
    print("written: backtest_results.json")

if __name__ == "__main__":
    main()
