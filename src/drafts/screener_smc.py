#!/usr/bin/env python3
"""
Stock Screener using yfinance EquityQuery

Screening Criteria:
- MarketCap > 2B
- Price > 7 USD
- ROE TTM > 6%
- Avg Vol 90D > 1M
- Operating Margin (annual) > 10%  (ebitdamargin used as screen-level superset proxy)
- Institutional Ownership > 20%
- Price > SMA 200  (bullish long-term trend)
- Price >= SMA 50 * 0.90 (within 10% below SMA50; allows short-term pullbacks)
- SMA 50 > SMA 200 (bullish MA alignment)
- Insider purchases > insider sales IF there is insider activity (last 6 months);
  absence of activity does not exclude
- Steady multi-year EPS growth (annual Diluted EPS: net-higher, <=1 down-year,
  CAGR >= 7%) + recent quarterly YoY confirmation + steady revenue growth
- Bullish market structure (Smart Money Concepts): from ~1y daily OHLC, require a
  structurally bullish trend with a bullish latest break (BOS/CHoCH). HARD FILTER.

Reported (not filtered) SMC fields: last_break, bars_since_break, range_position
and premium/equilibrium/discount zone, and the last bullish order block
(ob_low/ob_high/price_above_ob). Concepts ported from the LuxAlgo "Smart Money
Concepts" TradingView indicator (smc.script).

Note: yfinance exposes SMA, not EMA - used as proxy.
"""
import yfinance as yf
import csv
import json
import sys
import time
from pathlib import Path
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed


def build_query():
    return yf.EquityQuery('and', [
        yf.EquityQuery('eq',   ['region',                             'us']),
        yf.EquityQuery('gt',   ['intradaymarketcap',                  2_000_000_000]),
        yf.EquityQuery('gt',   ['eodprice',                           7]),
        yf.EquityQuery('gt',   ['returnonequity.lasttwelvemonths',    0.06]),
        yf.EquityQuery('gt',   ['avgdailyvol3m',                      1_000_000]),
        # operatingmargin isn't a valid EquityQuery field; ebitdamargin is a safe
        # superset (EBITDA = operating income + D&A >= operating income), so this
        # never excludes a stock with operating margin > 10%. True operating margin
        # is enforced per-ticker in operating_margin_ok().
        yf.EquityQuery('gt',   ['ebitdamargin.lasttwelvemonths',      0.10]),
        yf.EquityQuery('gt',   ['pctheldinst',                        0.20]),
    ])


def fetch_all_results(query, batch_size=250):
    results = []
    offset = 0

    while True:
        response = yf.screen(query, offset=offset, size=batch_size)
        if not response or not response.get('quotes'):
            break

        results.extend(response['quotes'])
        total = response.get('total', 0)

        if len(results) >= total:
            break

        offset += batch_size

    return results


def insider_activity_ok(symbol):
    """Insider check is a soft gate: absence of activity does NOT exclude a
    ticker. Only when there are actual movements must purchases exceed sales
    over the last 6 months."""
    try:
        df = yf.Ticker(symbol).insider_purchases
        if df is None or df.empty:
            return True  # no data -> do not exclude
        purchases = float(df.loc[df['Insider Purchases Last 6m'] == 'Purchases', 'Shares'].iloc[0])
        sales     = float(df.loc[df['Insider Purchases Last 6m'] == 'Sales',     'Shares'].iloc[0])
        if purchases == 0 and sales == 0:
            return True  # no movement -> do not exclude
        return purchases > sales
    except Exception:
        return True  # missing/unexpected data -> do not exclude


def _series(stmt, row, n):
    """Most recent n values of a statement row, oldest -> newest, NaNs dropped."""
    if stmt is None or row not in stmt.index:
        return None
    s = stmt.loc[row].dropna().sort_index()  # ascending by date
    return s.tail(n).tolist() if len(s) else None


def _steady_growth(annual, min_years, max_declines, min_cagr):
    """Steadiness gate on an annual series: net-higher and ends positive,
    at most `max_declines` down-years, and CAGR >= `min_cagr`.
    Compares levels (not percentages) to avoid the negative-base sign trap."""
    if not annual or len(annual) < min_years:
        return False
    first, last = annual[0], annual[-1]
    if last <= 0 or last <= first:
        return False
    declines = sum(1 for i in range(len(annual) - 1) if annual[i + 1] < annual[i])
    if declines > max_declines:
        return False
    if first > 0:  # CAGR only meaningful with a positive base
        cagr = (last / first) ** (1 / (len(annual) - 1)) - 1
        if cagr < min_cagr:
            return False
    return True


def operating_margin_ok(annual, threshold=0.10):
    """True operating margin (Operating Income / Total Revenue) for the most
    recent fiscal year > threshold. yfinance has no operatingmargin screener
    field, so this enforces it precisely from the income statement."""
    op  = _series(annual, 'Operating Income', 1)
    rev = _series(annual, 'Total Revenue', 1)
    if not op or not rev or rev[0] == 0:
        return False
    return (op[0] / rev[0]) > threshold


def growth_ok(annual, quarterly, min_years=3, max_annual_declines=1,
              min_eps_cagr=0.07, min_rev_cagr=0.03, require_revenue=True):
    """Steady multi-year EPS growth, confirmed by recent YoY momentum and
    (optionally) backed by steady revenue growth, which is harder to game than
    EPS. Operates on pre-fetched statements (caller fetches once).

    - Annual Diluted EPS: net-higher, ends positive, <= max_annual_declines
      down-years (steadiness), and CAGR >= min_eps_cagr (magnitude).
    - Quarterly Diluted EPS: latest quarter > same quarter a year ago, but only
      when >=5 quarters exist (clean, seasonality-neutral YoY).
    - Annual Total Revenue: same steadiness test with min_rev_cagr.
    """
    eps_annual = _series(annual, 'Diluted EPS', 4)
    if not _steady_growth(eps_annual, min_years, max_annual_declines, min_eps_cagr):
        return False

    eps_q = _series(quarterly, 'Diluted EPS', 5)
    if eps_q and len(eps_q) >= 5 and eps_q[-1] <= eps_q[-5]:
        return False  # only enforce YoY when a clean compare exists

    if require_revenue:
        rev_annual = _series(annual, 'Total Revenue', 4)
        if not _steady_growth(rev_annual, min_years, max_annual_declines, min_rev_cagr):
            return False

    return True


def trend_ok(quote, sma50_tolerance=0.10):
    """Bullish trend gate: price above SMA200, SMA50 above SMA200 (proper MA
    alignment), and price no more than `sma50_tolerance` (10%) below SMA50 --
    allowing short-term pullbacks within an established uptrend. SMA fields are
    in the screener response but not valid EquityQuery filter fields, so they're
    checked here. fiftyDayAverageChangePercent is a fraction ((price-sma50)/sma50),
    so -0.10 means 10% below SMA50."""
    sma50  = quote.get('fiftyDayAverage', 0)
    sma200 = quote.get('twoHundredDayAverage', 0)
    return (
        quote.get('fiftyDayAverageChangePercent', -1) >= -sma50_tolerance and
        quote.get('twoHundredDayAverageChangePercent', -1) > 0 and
        sma50 > sma200
    )


# ---------------------------------------------------------------------------
# Smart Money Concepts (SMC) — basic price-action layer
# Ported from the LuxAlgo "Smart Money Concepts" TradingView indicator
# (smc.script). Operates on ~1y of daily OHLC. All pure functions.
# ---------------------------------------------------------------------------

def swing_pivots(highs, lows, length=5):
    """Fractal swing pivots (port of leg()/getCurrentStructure()). A swing high
    at i is a high strictly greater than every high within +/-length; a swing
    low is symmetric. Returns chronological list of (index, price, kind)."""
    n = len(highs)
    pivots = []
    for i in range(length, n - length):
        hi = highs[i]
        if hi > max(highs[i - length:i]) and hi > max(highs[i + 1:i + length + 1]):
            pivots.append((i, hi, 'high'))
            continue
        lo = lows[i]
        if lo < min(lows[i - length:i]) and lo < min(lows[i + 1:i + length + 1]):
            pivots.append((i, lo, 'low'))
    return pivots


def market_structure(highs, lows, closes, length=5):
    """Distilled port of displayStructure(): walk bars, detect BOS/CHoCH.
    A pivot becomes active `length` bars after its index (confirmation lag).
    Close crossing above the active swing high is a bullish break (CHoCH if
    prior trend was bearish/neutral, else BOS); crossing below the active swing
    low is bearish. Returns trend (+1/-1/0), the last break, and the pivots."""
    n = len(closes)
    pivots = swing_pivots(highs, lows, length)
    by_confirm = {}
    for (idx, price, kind) in pivots:
        by_confirm.setdefault(idx + length, []).append((price, kind))

    trend = 0
    active_high = active_low = None
    high_crossed = low_crossed = False
    last_break = None  # (bar_index, 'bull'/'bear', 'BOS'/'CHoCH')

    for t in range(n):
        for (price, kind) in by_confirm.get(t, ()):
            if kind == 'high':
                active_high, high_crossed = price, False
            else:
                active_low, low_crossed = price, False

        close = closes[t]
        if active_high is not None and not high_crossed and close > active_high:
            last_break = (t, 'bull', 'CHoCH' if trend <= 0 else 'BOS')
            trend, high_crossed = 1, True
        elif active_low is not None and not low_crossed and close < active_low:
            last_break = (t, 'bear', 'CHoCH' if trend >= 0 else 'BOS')
            trend, low_crossed = -1, True

    return {'trend': trend, 'last_break': last_break, 'pivots': pivots}


def premium_discount(highs, lows, close, pivots, lookback=60):
    """Dealing-range position (port of drawPremiumDiscountZones()). Range is
    bounded by the most recent confirmed swing high/low, falling back to the
    rolling high/low over `lookback` bars when pivots are sparse. Returns
    (position 0-1, zone, top, bottom)."""
    last_high = last_low = None
    for (idx, price, kind) in reversed(pivots):
        if kind == 'high' and last_high is None:
            last_high = price
        elif kind == 'low' and last_low is None:
            last_low = price
        if last_high is not None and last_low is not None:
            break
    if last_high is None:
        last_high = max(highs[-lookback:])
    if last_low is None:
        last_low = min(lows[-lookback:])

    top, bottom = max(last_high, last_low), min(last_high, last_low)
    if top == bottom:
        return 0.5, 'equilibrium', top, bottom
    pos = max(0.0, min(1.0, (close - bottom) / (top - bottom)))
    zone = 'discount' if pos < 0.45 else 'premium' if pos > 0.55 else 'equilibrium'
    return pos, zone, top, bottom


def last_bullish_order_block(highs, lows, pivots, break_index):
    """Simplified port of storeOrdeBlock(): the lowest-low candle in the leg
    from the swing low preceding the bullish break up to the break itself.
    Returns (ob_low, ob_high) or None."""
    base_idx = None
    for (idx, price, kind) in reversed(pivots):
        if kind == 'low' and idx <= break_index:
            base_idx = idx
            break
    if base_idx is None:
        base_idx = max(0, break_index - 20)
    window = lows[base_idx:break_index + 1]
    if not window:
        return None
    ob_idx = base_idx + window.index(min(window))
    return lows[ob_idx], highs[ob_idx]


def smc_analysis(hist):
    """Compute the basic SMC verdict from a daily OHLC DataFrame: bullish/bearish
    structure, last BOS/CHoCH, premium/discount zone, and last bullish order
    block. Returns a dict (or None if there isn't enough data)."""
    highs = hist['High'].tolist()
    lows = hist['Low'].tolist()
    closes = hist['Close'].tolist()
    if len(closes) < 60:
        return None

    structure = market_structure(highs, lows, closes)
    close = closes[-1]
    pos, zone, _top, _bottom = premium_discount(highs, lows, close, structure['pivots'])
    lb = structure['last_break']

    result = {
        'structure_trend': 'bullish' if structure['trend'] == 1 else 'bearish' if structure['trend'] == -1 else 'neutral',
        'last_break':      f"{'bullish' if lb[1] == 'bull' else 'bearish'}_{lb[2]}" if lb else None,
        'bars_since_break': (len(closes) - 1 - lb[0]) if lb else None,
        'range_position':  round(pos, 3),
        'zone':            zone,
        'ob_low':          None,
        'ob_high':         None,
        'price_above_ob':  None,
        '_bullish_break':  bool(lb and lb[1] == 'bull'),  # consumed by the filter, not output
    }

    if lb and lb[1] == 'bull':
        ob = last_bullish_order_block(highs, lows, structure['pivots'], lb[0])
        if ob:
            result['ob_low'] = round(ob[0], 2)
            result['ob_high'] = round(ob[1], 2)
            result['price_above_ob'] = bool(close > ob[1])
    return result


# ---------------------------------------------------------------------------
# Buy / Hold / Discard rating
# The screen already guarantees quality fundamentals + bullish structure, so
# this layer adds the three missing axes -- valuation, balance-sheet health,
# relative strength -- and entry timing, then classifies each name.
# ---------------------------------------------------------------------------

PEG_RICH               = 2.0    # PEG above this is too pricey to BUY (may still HOLD)
PEG_DISCARD            = 3.0    # egregious valuation -> discard
FCF_YIELD_MIN          = 0.0    # require non-negative FCF yield to avoid discard
DEBT_EQUITY_MAX        = 2.0    # debt/equity ratio (2.0 = 200%); above -> discard
RS_BUY_MIN             = 0.0    # 6m relative strength vs SPY: < 0 -> discard (laggard)
FRESH_BREAK_BARS       = 40     # a BOS within ~2 months is a "fresh" trigger
NEAR_HIGH_PCT          = 0.03   # within 3% of 52w high = extended (favor HOLD)
OB_RETEST_BAND         = 0.05   # price within 5% above the order block top = a retest entry
EARNINGS_BLACKOUT_DAYS = 7      # don't BUY within a week before earnings
LOOKBACK_6M            = 126    # ~6 months of trading days

# 6-month SPY return, computed once in main() and read by the worker threads.
_SPY_RET_6M = None


def valuation_health(info, market_cap):
    """Valuation + balance-sheet metrics from the (already fetched) .info dict.
    Note: yfinance reports debtToEquity as a percent (e.g. 45.2), converted to a
    ratio here; freeCashflow is absolute, turned into a yield against market cap."""
    peg = info.get('trailingPegRatio')
    fpe = info.get('forwardPE')
    fcf = info.get('freeCashflow')
    de  = info.get('debtToEquity')
    return {
        'peg':            round(peg, 2) if isinstance(peg, (int, float)) else None,
        'forwardPE':      round(fpe, 1) if isinstance(fpe, (int, float)) else None,
        'fcf_yield':      round(fcf / market_cap, 4) if (fcf and market_cap) else None,
        'debt_to_equity': round(de / 100, 2) if isinstance(de, (int, float)) else None,
    }


def period_return(closes, lookback):
    """Simple price return over `lookback` bars, or None if not enough history."""
    if len(closes) <= lookback or closes[-1 - lookback] == 0:
        return None
    return closes[-1] / closes[-1 - lookback] - 1


def days_until_earnings(quote):
    """Days until the next earnings date from the screener payload, or None."""
    ts = quote.get('earningsTimestampStart') or quote.get('earningsTimestamp')
    if not ts:
        return None
    days = (ts - time.time()) / 86400
    return round(days, 1) if days >= 0 else None


def rate(m):
    """Classify a (quality + bullish-structure) ticker as buy / hold / discard.
    Returns (rating, reason). `m` merges the SMC, valuation/health and timing
    fields."""
    # --- DISCARD: any hard red flag on health, valuation, or relative strength ---
    red = []
    if m['debt_to_equity'] is not None and m['debt_to_equity'] > DEBT_EQUITY_MAX:
        red.append(f"high leverage D/E={m['debt_to_equity']}")
    if m['fcf_yield'] is not None and m['fcf_yield'] < FCF_YIELD_MIN:
        red.append("negative FCF")
    if m['peg'] is not None and m['peg'] > PEG_DISCARD:
        red.append(f"valuation egregious PEG={m['peg']}")
    if m['rel_strength_6m'] is not None and m['rel_strength_6m'] < RS_BUY_MIN:
        red.append(f"lags market RS={m['rel_strength_6m']}")
    if red:
        return 'discard', '; '.join(red)

    # --- BUY: sane valuation + good entry, not extended / pre-earnings ---
    # A "good entry" is a pullback into the discount/equilibrium half of the range
    # OR a retest of the order block (price near the OB top) -- NOT merely "above
    # the OB", which is true of almost any uptrend and would wave through names
    # sitting at their highs.
    val_rich   = m['peg'] is not None and m['peg'] > PEG_RICH
    good_entry = m['zone'] in ('discount', 'equilibrium') or m['near_ob']
    extended   = m['pct_below_52w_high'] is not None and m['pct_below_52w_high'] < NEAR_HIGH_PCT
    pre_earn   = m['days_to_earnings'] is not None and m['days_to_earnings'] <= EARNINGS_BLACKOUT_DAYS
    fresh      = m['bars_since_break'] is not None and m['bars_since_break'] <= FRESH_BREAK_BARS

    if good_entry and not val_rich and not extended and not pre_earn:
        why = [m['zone'] if m['zone'] in ('discount', 'equilibrium') else 'OB retest']
        if fresh:
            why.append(f"fresh BOS {m['bars_since_break']}b")
        return 'buy', 'good entry: ' + ', '.join(why)

    # --- otherwise HOLD / WATCH: good name, await a better entry ---
    hold = []
    if val_rich:
        hold.append(f"valuation rich PEG={m['peg']}")
    if extended:
        hold.append("near 52w high")
    if m['zone'] == 'premium' and not m['near_ob']:
        hold.append("premium zone (extended)")
    if pre_earn:
        hold.append(f"earnings in {m['days_to_earnings']}d")
    return 'hold', '; '.join(hold) or 'quality+bullish, await better entry'


def check_ticker(quote):
    symbol = quote.get('symbol', '')
    if not insider_activity_ok(symbol):
        return None

    try:
        t = yf.Ticker(symbol)
        annual = t.income_stmt
        quarterly = t.quarterly_income_stmt
    except Exception:
        return None

    if not operating_margin_ok(annual):
        return None
    if not growth_ok(annual, quarterly):
        return None

    # SMC price-action layer: one daily-history call, only for growth-passers.
    try:
        hist = t.history(period='1y', interval='1d', auto_adjust=True)
    except Exception:
        return None
    smc = smc_analysis(hist)
    # HARD FILTER: require structurally bullish price action (bullish trend with
    # a bullish latest break). Fail-closed when structure can't be confirmed.
    if not smc or smc['structure_trend'] != 'bullish' or not smc['_bullish_break']:
        return None
    smc.pop('_bullish_break', None)

    # Single .info call for the fields not present in the screener payload
    # (sector, industry, displayName, institutional ownership). Only runs for
    # tickers that already passed every filter, so it's a handful of calls.
    try:
        info = t.info
    except Exception:
        info = {}

    market_cap = quote.get('marketCap', 0)

    # Valuation / health / relative-strength / timing -> buy/hold/discard rating.
    closes = hist['Close'].tolist()
    highs = hist['High'].tolist()
    stock_ret_6m = period_return(closes, LOOKBACK_6M)
    rel_strength_6m = (stock_ret_6m - _SPY_RET_6M) if (stock_ret_6m is not None and _SPY_RET_6M is not None) else None
    high_52w = max(highs) if highs else None
    pct_below_52w_high = ((high_52w - closes[-1]) / high_52w) if high_52w else None

    # Order-block RETEST: price sitting near (within OB_RETEST_BAND above) the OB
    # top, i.e. a pullback to support -- not merely anywhere above the OB.
    ob_high = smc['ob_high']
    near_ob = ob_high is not None and closes[-1] <= ob_high * (1 + OB_RETEST_BAND)

    vh = valuation_health(info, market_cap)
    metrics = {
        **smc,
        **vh,
        'near_ob':            bool(near_ob),
        'rel_strength_6m':    round(rel_strength_6m, 3) if rel_strength_6m is not None else None,
        'pct_below_52w_high': round(pct_below_52w_high, 3) if pct_below_52w_high is not None else None,
        'days_to_earnings':   days_until_earnings(quote),
    }
    rating, rating_reason = rate(metrics)

    return {
        'symbol':                   symbol,
        'name':                     quote.get('longName') or quote.get('shortName', ''),
        'displayName':             info.get('displayName') or quote.get('longName') or quote.get('shortName', ''),
        'sector':                  info.get('sector'),
        'industry':                info.get('industry'),
        # Buy / Hold / Discard verdict
        'rating':                  rating,
        'rating_reason':           rating_reason,
        'price':                    quote.get('regularMarketPrice'),
        'marketCap':               format_market_cap(market_cap),
        # Valuation + health + relative strength
        'trailingPE':             quote.get('trailingPE'),
        'forwardPE':              vh['forwardPE'],
        'peg':                    vh['peg'],
        'fcf_yield':              vh['fcf_yield'],
        'debt_to_equity':         vh['debt_to_equity'],
        'rel_strength_6m':        metrics['rel_strength_6m'],
        'pct_below_52w_high':     metrics['pct_below_52w_high'],
        'days_to_earnings':       metrics['days_to_earnings'],
        'sma50':                   quote.get('fiftyDayAverage'),
        'sma50_pct_chg':           round(quote.get('fiftyDayAverageChangePercent', 0), 2),
        'sma200':                  quote.get('twoHundredDayAverage'),
        'avgVol3M':               quote.get('averageDailyVolume3Month'),
        'heldPercentInstitutions': quote.get('heldPercentInstitutions') or info.get('heldPercentInstitutions'),
        # SMC price-action signals
        'structure_trend':        smc['structure_trend'],
        'last_break':             smc['last_break'],
        'bars_since_break':       smc['bars_since_break'],
        'range_position':         smc['range_position'],
        'zone':                   smc['zone'],
        'ob_low':                 smc['ob_low'],
        'ob_high':                smc['ob_high'],
        'price_above_ob':         smc['price_above_ob'],
        'near_ob':                metrics['near_ob'],
    }


def format_market_cap(value):
    if value >= 1_000_000_000_000:
        return f"${value / 1_000_000_000_000:.2f}T"
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    return f"${value / 1_000_000:.0f}M"


def main():
    print("Running screener query...", file=sys.stderr)
    query = build_query()
    candidates = fetch_all_results(query)
    print(f"Fundamental + institutional filter: {len(candidates)} candidates → applying per-ticker filters...", file=sys.stderr)

    # pre-filter using payload fields (no network cost)
    sma_candidates = [q for q in candidates if trend_ok(q)]
    print(f"Trend filter (price>=SMA50*0.9, price>SMA200, SMA50>SMA200): {len(sma_candidates)} candidates → fetching per-ticker data in parallel...", file=sys.stderr)

    # SPY 6-month return: the relative-strength benchmark (fetched once).
    global _SPY_RET_6M
    try:
        spy = yf.Ticker('SPY').history(period='1y', interval='1d', auto_adjust=True)
        _SPY_RET_6M = period_return(spy['Close'].tolist(), LOOKBACK_6M)
    except Exception:
        _SPY_RET_6M = None
    print(f"SPY 6m benchmark return: {_SPY_RET_6M}", file=sys.stderr)

    matches = []
    completed = 0
    with ThreadPoolExecutor(max_workers=25) as executor:
        futures = {executor.submit(check_ticker, quote): quote for quote in sma_candidates}
        for future in as_completed(futures):
            completed += 1
            print(f"  [{completed}/{len(sma_candidates)}] checked", file=sys.stderr, end='\r')
            result = future.result()
            if result:
                matches.append(result)

    print(f"\nPassed all filters (incl. SMC bullish structure): {len(matches)} stocks", file=sys.stderr)
    print(f"Ratings: {dict(Counter(m['rating'] for m in matches))}", file=sys.stderr)
    # Sort by verdict (buy -> hold -> discard), then best entry (discount first).
    rank = {'buy': 0, 'hold': 1, 'discard': 2}
    matches.sort(key=lambda x: (rank.get(x['rating'], 3), x['range_position'], x['sma50_pct_chg']))

    output_path = Path(__file__).resolve().parent / 'screener_results.csv'
    fields = ['symbol', 'displayName', 'sector', 'industry', 'rating', 'rating_reason',
              'zone', 'peg', 'rel_strength_6m', 'structure_trend', 'last_break']
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(fields)
        for m in matches:
            writer.writerow([m.get(k, '') for k in fields])
    print(f"Saved {len(matches)} tickers to {output_path}", file=sys.stderr)

    print(json.dumps(matches, indent=2))


if __name__ == '__main__':
    main()
