#!/usr/bin/env python3
"""
Sector & Industry Relative-Strength Screener

A top-down screener: ranks all GICS sectors (11) and industries (~145) by their
relative strength versus the market (SPY) over 1m / 3m / 6m / 1y windows.

Method (hybrid, both legs use clean tradable prices -- NOT the ^YH market-cap
indices, whose levels step-jump on index reconstitution and produce phantom
returns):

  SECTORS    -> the canonical SPDR sector ETF (XLK, XLI, ...). Clean total-return
                price series; one fetch each.
  INDUSTRIES -> constituent aggregation. yfinance has no industry ETF, so we take
                each industry's top-N constituents (from Industry.top_companies)
                and compute a CAP-weighted (<=15%/name, so no single mega-cap
                dominates), winsorised average of their relative strength. A
                weight-free median (rs_median) is reported as a robustness check.

Per window:  return       = price change over the window
             rel_strength = return - SPY return over the SAME window (excess)
A weighted composite `rs_score` across the windows gives a single ranking.
"""
import yfinance as yf
from yfinance import const
import csv
import json
import sys
import statistics
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

BENCHMARK = 'SPY'

# Trading-day lookbacks per window.
PERIODS = {'1m': 21, '3m': 63, '6m': 126, '1y': 250}

# Composite weighting of relative strength across windows (tunable). Favours the
# intermediate term (3-6m) -- long enough to be a real trend, recent enough to
# still be actionable.
WEIGHTS = {'1m': 0.20, '3m': 0.30, '6m': 0.30, '1y': 0.20}

# Constituents per industry to fetch -- used for BOTH the industry aggregate and
# the per-industry top-company drill-down pool. Note yfinance's top_companies
# returns at most ~50 names per industry, so values above that just take all
# available. The drill-down still displays the top 25 by rs_score.
TOP_N_CONSTITUENTS = 100

# Winsorisation cap on each constituent's per-window relative strength (fraction).
# Yahoo's adjusted-close data carries corporate-action artifacts (e.g. botched
# spinoff/split adjustments -- SNDK shows a 47x "return"), and one corrupted
# high-weight name otherwise detonates the cap-weighted average. Clamping each
# constituent's excess return to +/-200% neutralises that without discarding the
# name. `clipped` in the output flags how many constituents were capped.
RS_CLIP = 2.0

# Max weight any single constituent may carry in the industry aggregate. Raw
# market weights are extremely top-heavy (NVDA ~46% of Semiconductors), so one
# mega-cap would *be* the industry's reading. Capping at 15% and redistributing
# de-concentrates the signal toward breadth. Lower it toward 1/N for fully
# equal-weight. `rs_median` is a weight-free robustness cross-check.
MAX_WEIGHT = 0.15

# Canonical SPDR sector ETFs -- clean, broad, liquid proxies (avoids the
# leveraged/thematic ETFs that appear in Sector.top_etfs, e.g. SOXL/ITA).
SECTOR_ETFS = {
    'Basic Materials':        'XLB',
    'Communication Services': 'XLC',
    'Consumer Cyclical':      'XLY',
    'Consumer Defensive':     'XLP',
    'Energy':                 'XLE',
    'Financial Services':     'XLF',
    'Healthcare':             'XLV',
    'Industrials':            'XLI',
    'Real Estate':            'XLRE',
    'Technology':             'XLK',
    'Utilities':              'XLU',
}


def resolve_industries():
    """Industry list from the sector->industry map: {name, key, sector, market_weight}."""
    industries = []
    for sector_name in const.SECTOR_INDUSTY_MAPPING:
        key = sector_name.lower().replace(' ', '-')
        try:
            tbl = yf.Sector(key).industries
        except Exception as e:
            print(f"  ! sector {key} failed: {e}", file=sys.stderr)
            continue
        for ind_key, row in tbl.iterrows():
            w = row['market weight']
            industries.append({
                'name': row['name'], 'key': ind_key, 'sector': sector_name,
                'market_weight': round(float(w), 4) if w == w else None,
            })
    return industries


def fetch_top_companies(ind_key, n=TOP_N_CONSTITUENTS):
    """Top-n constituents (ticker, weight, name) of an industry by market weight."""
    try:
        tc = yf.Industry(ind_key).top_companies
        if tc is None or tc.empty:
            return []
        tc = tc.head(n)
        return [(sym, float(w), str(nm))
                for sym, w, nm in zip(tc.index, tc['market weight'], tc['name']) if w == w]
    except Exception:
        return []


def fetch_screen_universe(batch_size=250):
    """Symbols passing the size/price/liquidity legs of the basic-strength filter
    (see strength/README.md): MarketCap > 2B, Price > 7, Avg Vol > 1M. Returned as
    {symbol: quote} so the SMA-change fields are available for the rest of that
    screen. Same yf.EquityQuery pattern as screener.py."""
    query = yf.EquityQuery('and', [
        yf.EquityQuery('eq', ['region', 'us']),
        yf.EquityQuery('gt', ['intradaymarketcap', 2_000_000_000]),
        yf.EquityQuery('gt', ['eodprice', 7]),
        yf.EquityQuery('gt', ['avgdailyvol3m', 1_000_000]),
    ])
    out = {}
    offset = 0
    while True:
        resp = yf.screen(query, offset=offset, size=batch_size)
        quotes = resp.get('quotes') if resp else None
        if not quotes:
            break
        for q in quotes:
            sym = q.get('symbol')
            if sym:
                out[sym] = q
        if len(out) >= (resp.get('total', 0) or 0):
            break
        offset += batch_size
    return out


def basic_strength_pass(symbol, closes, screened):
    """Basic-strength filter (see strength/README.md): in the size/price/liquidity
    universe, price above both SMAs (from the screen payload), and 6-month price
    change > 10%."""
    q = screened.get(symbol)
    if q is None:
        return False
    if not (q.get('fiftyDayAverageChangePercent', -1) > 0 and
            q.get('twoHundredDayAverageChangePercent', -1) > 0):
        return False
    chg6m = period_return(closes, PERIODS['6m'])
    return chg6m is not None and chg6m > 0.10


def fetch_closes_bulk(tickers, chunk=200):
    """Bulk-fetch ~1y daily closes for many tickers. Returns {ticker: Series}
    (date-indexed, NaNs dropped). Uses yf.download (batched HTTP)."""
    out = {}
    tickers = list(tickers)
    for i in range(0, len(tickers), chunk):
        batch = tickers[i:i + chunk]
        try:
            df = yf.download(batch, period='1y', interval='1d', auto_adjust=True,
                             group_by='ticker', threads=True, progress=False)
        except Exception:
            continue
        for t in batch:
            try:
                col = df[t]['Close'] if len(batch) > 1 else df['Close']
                s = col.dropna()
                if len(s):
                    out[t] = s
            except Exception:
                pass
        print(f"  fetched {min(i + chunk, len(tickers))}/{len(tickers)} symbols", file=sys.stderr, end='\r')
    return out


def period_return(closes, days):
    """Price return over `days` trading days (fraction), or None if too short."""
    if not closes or len(closes) <= days or closes[-1 - days] == 0:
        return None
    return closes[-1] / closes[-1 - days] - 1


def compute_strength(closes, bench_returns, clip=None):
    """Per-window return + relative strength (excess vs benchmark) + composite
    rs_score, for a single price series. Used for sector ETFs (clip=None) and for
    individual companies (clip=RS_CLIP, winsorising each window's return to
    bench +/- clip so a data-artifact return can't distort the row)."""
    out = {}
    sn = sd = 0.0
    for label, days in PERIODS.items():
        r = period_return(closes, days)
        b = bench_returns.get(label)
        if r is not None and b is not None and clip is not None:
            r = max(min(r, b + clip), b - clip)
        out[f'ret_{label}'] = round(r * 100, 1) if r is not None else None
        rs = (r - b) if (r is not None and b is not None) else None
        out[f'rs_{label}'] = round(rs * 100, 1) if rs is not None else None
        if rs is not None:
            sn += WEIGHTS[label] * rs * 100
            sd += WEIGHTS[label]
    out['rs_score'] = round(sn / sd, 2) if sd else None
    return out


def cap_weights(items, cap):
    """Normalise weights to sum 1 and iteratively cap any single weight at `cap`,
    redistributing the excess proportionally to the others. Returns {key: weight}.
    With cap <= 1/len it converges to equal weight."""
    w = {k: max(v, 0.0) for k, v in items}
    keys = list(w)
    if not keys:
        return {}
    for _ in range(50):
        total = sum(w.values()) or 1.0
        w = {k: v / total for k, v in w.items()}
        over = [k for k in keys if w[k] > cap + 1e-9]
        if not over:
            break
        under = [k for k in keys if k not in over]
        for k in over:
            w[k] = cap
        remaining = 1.0 - cap * len(over)
        under_total = sum(w[k] for k in under) or 1.0
        if remaining <= 0 or not under:   # cap too tight -> equal weight
            return {k: 1.0 / len(keys) for k in keys}
        for k in under:
            w[k] = remaining * (w[k] / under_total)
    return w


def aggregate_industry(constituents, closes_by_ticker, bench_returns):
    """Cap-weighted, winsorised average relative strength across an industry's
    constituents, per window, plus a composite rs_score, a weight-free median
    (rs_median), constituent count, coverage and clip count.

    - Weights are capped at MAX_WEIGHT so no single mega-cap dominates.
    - Each constituent's excess return is winsorised to +/-RS_CLIP.
    """
    have = [(item[0], item[1]) for item in constituents if closes_by_ticker.get(item[0])]
    total_listed = sum(item[1] for item in constituents) or 1.0
    capw = cap_weights(have, MAX_WEIGHT)
    clipped = set()

    # Per-constituent winsorised relative strength, per window (computed once).
    cons_rs = {}
    for t, _ in have:
        c = closes_by_ticker[t]
        d = {}
        for label, days in PERIODS.items():
            r = period_return(c, days)
            b = bench_returns.get(label)
            if r is None or b is None:
                d[label] = None
                continue
            r_clamped = max(min(r, b + RS_CLIP), b - RS_CLIP)
            if r_clamped != r:
                clipped.add(t)
            d[label] = r_clamped - b
        cons_rs[t] = d

    out = {}
    sn = sd = 0.0
    for label in PERIODS:
        b = bench_returns.get(label)
        num = den = 0.0
        for t, _ in have:
            rs = cons_rs[t][label]
            if rs is None:
                continue
            num += capw[t] * rs
            den += capw[t]
        rs_w = (num / den) if den else None
        out[f'rs_{label}'] = round(rs_w * 100, 1) if rs_w is not None else None
        out[f'ret_{label}'] = round((rs_w + b) * 100, 1) if (rs_w is not None and b is not None) else None
        if rs_w is not None:
            sn += WEIGHTS[label] * rs_w * 100
            sd += WEIGHTS[label]
    out['rs_score'] = round(sn / sd, 2) if sd else None

    # Weight-free robustness cross-check: median of each constituent's own
    # composite relative strength.
    comps = []
    for t, _ in have:
        cs = cd = 0.0
        for label in PERIODS:
            rs = cons_rs[t][label]
            if rs is not None:
                cs += WEIGHTS[label] * rs * 100
                cd += WEIGHTS[label]
        if cd:
            comps.append(cs / cd)
    out['rs_median'] = round(statistics.median(comps), 2) if comps else None

    out['n_constituents'] = len(have)
    out['coverage'] = round(sum(w for _, w in have) / total_listed, 2)
    out['clipped'] = len(clipped)
    return out


def rs_median_biweekly(constituents, series_by_ticker, spy_series, step=10):
    """Bi-weekly time series for charting: for each consecutive ~2-week
    (`step` trading-day) bucket, the median over constituents of that bucket's
    relative strength (constituent return - SPY return), winsorised to +/-RS_CLIP.
    Returns [{date, rs_2w}] (rs_2w in %). For a single 'constituent' (a sector
    ETF) the median is just that series' own 2-week excess return."""
    if spy_series is None or len(spy_series) < step + 1:
        return []
    idx = spy_series.index
    spy = spy_series.values
    have = [item[0] for item in constituents
            if item[0] in series_by_ticker and len(series_by_ticker[item[0]]) > 1]
    if not have:
        return []
    aligned = {t: series_by_ticker[t].reindex(idx).ffill().values for t in have}

    bounds = list(range(0, len(idx), step))
    if bounds[-1] != len(idx) - 1:
        bounds.append(len(idx) - 1)

    series = []
    for i in range(1, len(bounds)):
        a, b = bounds[i - 1], bounds[i]
        if not spy[a]:
            continue
        spy_ret = spy[b] / spy[a] - 1
        vals = []
        for t in have:
            s = aligned[t]
            pa, pb = s[a], s[b]
            if pa and pa == pa and pb == pb:   # both present (not NaN) and pa != 0
                rs = (pb / pa - 1) - spy_ret
                vals.append(max(min(rs, RS_CLIP), -RS_CLIP))
        if vals:
            series.append({'date': idx[b].strftime('%Y-%m-%d'),
                           'rs_2w': round(statistics.median(vals) * 100, 2)})
    return series


def main():
    # 1) Resolve the industry universe (11 calls) and pick sector ETFs.
    print("Resolving industry universe...", file=sys.stderr)
    industries = resolve_industries()
    print(f"  {len(industries)} industries across {len(SECTOR_ETFS)} sectors.", file=sys.stderr)

    # 2) Resolve each industry's top constituents (threaded).
    print(f"Fetching top {TOP_N_CONSTITUENTS} constituents per industry...", file=sys.stderr)
    constituents_by_industry = {}
    done = 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(fetch_top_companies, ind['key']): ind['key'] for ind in industries}
        for fut in as_completed(futs):
            done += 1
            print(f"  [{done}/{len(industries)}] resolved", file=sys.stderr, end='\r')
            constituents_by_industry[futs[fut]] = fut.result()

    # 3) Bulk-fetch every price series we need: SPY + sector ETFs + all constituents.
    symbols = {BENCHMARK} | set(SECTOR_ETFS.values())
    for cons in constituents_by_industry.values():
        symbols |= {item[0] for item in cons}
    print(f"\nFetching price history for {len(symbols)} symbols...", file=sys.stderr)
    series_by_symbol = fetch_closes_bulk(symbols)
    closes = {k: v.tolist() for k, v in series_by_symbol.items()}  # list form for return calcs
    spy_series = series_by_symbol.get(BENCHMARK)

    bench = closes.get(BENCHMARK)
    if not bench:
        print(f"\nERROR: benchmark {BENCHMARK} unavailable.", file=sys.stderr)
        sys.exit(1)
    bench_returns = {label: period_return(bench, days) for label, days in PERIODS.items()}
    print(f"\n{BENCHMARK} returns: "
          + "  ".join(f"{k}={v*100:+.1f}%" if v is not None else f"{k}=NA" for k, v in bench_returns.items()),
          file=sys.stderr)

    # 3b) Basic-strength screening universe (size/price/liquidity) for the company drill-down.
    print("Screening basic-strength universe...", file=sys.stderr)
    screened = fetch_screen_universe()
    print(f"  {len(screened)} US stocks pass MarketCap>2B / Price>7 / AvgVol>1M.", file=sys.stderr)

    # 4a) Sectors via ETF.
    sectors = []
    for name, etf in SECTOR_ETFS.items():
        c = closes.get(etf)
        if not c:
            continue
        sectors.append({'id': name.lower().replace(' ', '-'),
                        'level': 'sector', 'name': name, 'sector': name, 'proxy': etf,
                        'n_constituents': '', 'coverage': '', **compute_strength(c, bench_returns),
                        'rs_history': rs_median_biweekly([(etf, 1.0)], series_by_symbol, spy_series)})

    # 4b) Industries via constituent aggregation.
    industry_rows = []
    for ind in industries:
        cons = constituents_by_industry.get(ind['key'], [])
        if not cons:
            continue
        agg = aggregate_industry(cons, closes, bench_returns)
        if agg['n_constituents'] == 0:
            continue
        industry_rows.append({'id': ind['key'],
                              'level': 'industry', 'name': ind['name'], 'sector': ind['sector'],
                              'proxy': f"{agg['n_constituents']} cons", **agg,
                              'rs_history': rs_median_biweekly(cons, series_by_symbol, spy_series)})

    # 4c) Per-industry top companies: constituents passing basic_strength, ranked by rs_score.
    company_rows = []
    for ind in industries:
        comps = []
        for sym, weight, name in constituents_by_industry.get(ind['key'], []):
            c = closes.get(sym)
            if not c or not basic_strength_pass(sym, c, screened):
                continue
            s = compute_strength(c, bench_returns, clip=RS_CLIP)
            if s['rs_score'] is None:
                continue
            # Absolute price change % (raw, NOT winsorised/relative) over 1w/1m/3m.
            def _chg(days, _c=c):
                r = period_return(_c, days)
                return round(r * 100, 1) if r is not None else None
            comps.append({
                'industry_id': ind['key'], 'symbol': sym, 'name': name, 'weight': round(weight, 4),
                'rs_score': s['rs_score'], 'rs_1m': s['rs_1m'], 'rs_3m': s['rs_3m'],
                'rs_6m': s['rs_6m'], 'rs_1y': s['rs_1y'], 'ret_1y': s['ret_1y'],
                'chg_1w': _chg(5), 'chg_1m': _chg(21), 'chg_3m': _chg(63),
                'rs_history': rs_median_biweekly([(sym, 1.0)], series_by_symbol, spy_series),
            })
        comps.sort(key=lambda x: x['rs_score'], reverse=True)
        for rank, r in enumerate(comps[:25], 1):
            r['rank'] = rank
            company_rows.append(r)

    # 5) Rank within each level by composite relative strength.
    def sort_key(x):
        return (x['rs_score'] is not None, x['rs_score'] if x['rs_score'] is not None else 0)

    sectors.sort(key=sort_key, reverse=True)
    industry_rows.sort(key=sort_key, reverse=True)
    for rank, r in enumerate(sectors, 1):
        r['rank'] = rank
    for rank, r in enumerate(industry_rows, 1):
        r['rank'] = rank

    # 6) Output.
    fields = ['rank', 'id', 'level', 'name', 'sector', 'proxy', 'n_constituents', 'coverage', 'clipped',
              'ret_1m', 'ret_3m', 'ret_6m', 'ret_1y',
              'rs_1m', 'rs_3m', 'rs_6m', 'rs_1y', 'rs_score', 'rs_median']
    output_path = 'strength_results.csv'
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(fields)
        for r in sectors + industry_rows:
            writer.writerow([r.get(k, '') for k in fields])
    print(f"Saved {len(sectors)} sectors + {len(industry_rows)} industries to {output_path}", file=sys.stderr)

    # Tidy (long-format) bi-weekly RS history for charting: one row per period.
    history_path = 'strength_history.csv'
    n_points = 0
    with open(history_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['id', 'level', 'name', 'sector', 'date', 'rs_2w'])
        for r in sectors + industry_rows:
            for pt in r.get('rs_history', []):
                writer.writerow([r['id'], r['level'], r['name'], r['sector'], pt['date'], pt['rs_2w']])
                n_points += 1
    print(f"Saved bi-weekly RS history ({n_points} points) to {history_path}", file=sys.stderr)

    # Per-industry top-company drill-down (snapshot + bi-weekly history).
    companies_path = 'strength_companies.csv'
    cfields = ['industry_id', 'rank', 'symbol', 'name', 'weight', 'rs_score',
               'rs_1m', 'rs_3m', 'rs_6m', 'rs_1y', 'ret_1y', 'chg_1w', 'chg_1m', 'chg_3m']
    with open(companies_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(cfields)
        for r in company_rows:
            writer.writerow([r.get(k, '') for k in cfields])
    print(f"Saved {len(company_rows)} company rows to {companies_path}", file=sys.stderr)

    chist_path = 'strength_companies_history.csv'
    cpoints = 0
    with open(chist_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['industry_id', 'symbol', 'date', 'rs_2w'])
        for r in company_rows:
            for pt in r.get('rs_history', []):
                writer.writerow([r['industry_id'], r['symbol'], pt['date'], pt['rs_2w']])
                cpoints += 1
    print(f"Saved company bi-weekly history ({cpoints} points) to {chist_path}", file=sys.stderr)

    print("\n=== SECTORS by relative strength (ETF excess vs SPY, %) ===", file=sys.stderr)
    print(f"{'#':>2} {'sector':22} {'etf':5} {'1m':>6} {'3m':>6} {'6m':>6} {'1y':>6}  {'score':>6}", file=sys.stderr)
    for r in sectors:
        print(f"{r['rank']:>2} {r['name']:22} {r['proxy']:5} {str(r['rs_1m']):>6} {str(r['rs_3m']):>6} {str(r['rs_6m']):>6} {str(r['rs_1y']):>6}  {str(r['rs_score']):>6}", file=sys.stderr)
    print("\n=== TOP 15 INDUSTRIES by relative strength (cap-weighted <=15%, winsorised) ===", file=sys.stderr)
    print(f"{'#':>2} {'industry':32} {'sector':12}  {'n':>2} {'clip':>4} {'score':>6} {'median':>6}", file=sys.stderr)
    for r in industry_rows[:15]:
        print(f"{r['rank']:>2} {r['name'][:32]:32} {r['sector'][:12]:12}  {r['n_constituents']:>2} {r['clipped']:>4} {str(r['rs_score']):>6} {str(r['rs_median']):>6}", file=sys.stderr)

    print(json.dumps({'sectors': sectors, 'industries': industry_rows}, indent=2))


if __name__ == '__main__':
    main()
