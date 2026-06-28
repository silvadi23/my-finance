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
- Price > SMA 50   (bullish short-term trend)
- SMA 50 > SMA 200 (bullish MA alignment)
- Insider purchases > insider sales IF there is insider activity (last 6 months);
  absence of activity does not exclude
- Steady multi-year EPS growth (annual Diluted EPS: net-higher, <=1 down-year,
  CAGR >= 7%) + recent quarterly YoY confirmation + steady revenue growth

Note: yfinance exposes SMA, not EMA - used as proxy.
"""
import yfinance as yf
import csv
import json
import sys
from pathlib import Path
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


def trend_ok(quote):
    """Bullish trend gate: price above both SMAs AND SMA50 above SMA200
    (proper moving-average alignment). SMA fields are in the screener response
    but not valid EquityQuery filter fields, so they're checked here."""
    sma50  = quote.get('fiftyDayAverage', 0)
    sma200 = quote.get('twoHundredDayAverage', 0)
    return (
        quote.get('fiftyDayAverageChangePercent', -1) > 0 and
        quote.get('twoHundredDayAverageChangePercent', -1) > 0 and
        sma50 > sma200
    )


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

    # Single .info call for the fields not present in the screener payload
    # (sector, industry, displayName, institutional ownership). Only runs for
    # tickers that already passed every filter, so it's a handful of calls.
    try:
        info = t.info
    except Exception:
        info = {}

    market_cap = quote.get('marketCap', 0)
    return {
        'symbol':                   symbol,
        'name':                     quote.get('longName') or quote.get('shortName', ''),
        'displayName':             info.get('displayName') or quote.get('longName') or quote.get('shortName', ''),
        'sector':                  info.get('sector'),
        'industry':                info.get('industry'),
        'price':                    quote.get('regularMarketPrice'),
        'marketCap':               format_market_cap(market_cap),
        'sma50':                   quote.get('fiftyDayAverage'),
        'sma50_pct_chg':           round(quote.get('fiftyDayAverageChangePercent', 0), 2),
        'sma200':                  quote.get('twoHundredDayAverage'),
        'avgVol3M':               quote.get('averageDailyVolume3Month'),
        'trailingPE':             quote.get('trailingPE'),
        'heldPercentInstitutions': quote.get('heldPercentInstitutions') or info.get('heldPercentInstitutions'),
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
    print(f"Trend filter (price>SMA50>SMA200): {len(sma_candidates)} candidates → fetching per-ticker data in parallel...", file=sys.stderr)

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

    print(f"\nPassed all filters: {len(matches)} stocks", file=sys.stderr)
    matches.sort(key=lambda x: x['sma50_pct_chg'])

    output_path = Path(__file__).resolve().parent / 'screener_results.csv'
    fields = ['symbol', 'displayName', 'sector', 'industry']
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(fields)
        for m in matches:
            writer.writerow([m.get(k, '') for k in fields])
    print(f"Saved {len(matches)} tickers to {output_path}", file=sys.stderr)

    print(json.dumps(matches, indent=2))


if __name__ == '__main__':
    main()
