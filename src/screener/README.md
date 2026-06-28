# Stock Screener (bottom-up quality + trend, with optional SMC + buy/hold/discard)

## Purpose

Find "strong" US stocks: **solid financials + a bullish trend**. A bottom-up, single-name screen.
Two variants share the same fundamental core:

- **`screener.py`** — base quality + trend screen. Output = a clean watchlist.
- **`screener_smc.py`** — base screen **plus** a Smart Money Concepts (SMC) price-action filter
  **plus** a `buy` / `hold` / `discard` rating. Output = a ranked, decision-tagged list.

Full criteria spec: [`screener_params.md`](../../../screener_params.md) (repo root).

## Data source

[yfinance](https://github.com/ranaroussi/yfinance):

- `yf.EquityQuery` / `yf.screen` — the server-side universe filter (cheap, paginated).
- `yf.Ticker(...).income_stmt` / `.quarterly_income_stmt` / `.info` / `.insider_purchases` — per-ticker fundamentals.
- `yf.Ticker(...).history()` (~1y daily OHLC) — SMC price-action (SMC variant only).

## Pipeline (cheap → expensive, so heavy calls run on few names)

1. **EquityQuery screen** (server-side): `region=us`, `marketcap > 2B`, `price > 7`,
   `ROE TTM > 6%`, `avg vol (3m) > 1M`, `EBITDA margin TTM > 10%`, `institutional > 20%`.
2. **Trend pre-filter** (from the screen payload, no network): `price > SMA200`, `price > SMA50`,
   `SMA50 > SMA200`. Base screener requires price strictly above SMA50; the SMC variant allows price
   up to **10% below SMA50** (`sma50_tolerance=0.10`) to admit healthy pullbacks.
3. **Per-ticker fundamentals** (parallel, only on trend survivors): true operating margin, steady
   multi-year growth, insider activity (see below).
4. **SMC variant only**: market-structure filter from daily OHLC, then the buy/hold/discard rating.

## Metrics used & why

| Metric | Threshold | Why |
|---|---|---|
| Market cap / price / avg vol | >2B / >$7 / >1M | Size + liquidity floor (tradable names). |
| ROE (TTM) | > 6% | Profitability/quality. Lowered from 10% so profitable growers just under the bar (e.g. AMD at ~8%) aren't excluded. ROE can be leverage-inflated → balance-sheet checks live in the rating. |
| EBITDA margin (TTM) | > 10% (screen) | **Proxy only.** yfinance has no `operatingmargin` screener field; EBITDA margin ≥ operating margin, so this never *wrongly* excludes a name. The real test is per-ticker (below). |
| Operating margin (annual) | > 10% (per-ticker) | True `Operating Income / Total Revenue` from the income statement — the actual quality gate. |
| Steady EPS growth | annual Diluted EPS net-higher, ends positive, ≤1 down-year, CAGR ≥ 7% | Rewards smooth compounders; rejects cyclicals (e.g. MU, EPS below its prior peak) and unprofitable names by design. Uses **level** comparisons, not %, to dodge the negative-base sign trap. |
| Quarterly EPS confirmation | latest qtr > same qtr a year ago (when ≥5 quarters) | Recent momentum, **YoY** to be seasonality-neutral. Skipped when <5 quarters (avoids a seasonal mismatch). |
| Revenue growth | annual revenue net-higher, ≤1 down-year, CAGR ≥ 3% | Harder to game than EPS (no buyback/one-off distortion). |
| Insider activity | purchases > sales **iff** there is activity | Soft gate: absence of insider trades does **not** exclude; only contradicting activity does. |

### SMC layer (screener_smc.py) — ported from the LuxAlgo "Smart Money Concepts" indicator

- **Swing pivots** = ±5-bar fractals on ~1y daily highs/lows.
- **Market structure**: a close breaking the last swing high is a **BOS** (continuation, trend already up)
  or **CHoCH** (reversal, trend was down); symmetric for the downside. **Hard filter:** keep only
  structurally bullish names (bullish trend + bullish latest break).
- **Premium/discount** = price position in the recent dealing range (>0.55 premium, <0.45 discount).
  *Reported, not filtered* (it conflicts with the strong-uptrend gate, which sits mostly in premium).
- **Order block** = the base candle before a bullish break. Reported (`ob_low/ob_high/price_above_ob/near_ob`).

### Buy / Hold / Discard rating (screener_smc.py) — the axes the screen lacks

Tunable constants at the top of the rating section:

- **Valuation** — `PEG` (>3 → discard, >2 → too rich to buy), forward PE, FCF yield.
- **Balance-sheet health** — debt/equity (>2 → discard), free cash flow (<0 → discard).
- **Relative strength** — 6-month return vs SPY (<0 → discard, i.e. a laggard).
- **Entry timing** — `buy` needs a *good entry*: discount/equilibrium zone **or** an order-block
  *retest* (price within 5% above the OB — **not** merely "above the OB", which is true of any
  uptrend), not extended near the 52w high (within 3%), not within 7 days of earnings.
- **Logic**: any red flag → `discard`; else sane valuation + good entry → `buy`; else `hold`.

## Outputs

- **`screener_results.csv`** (base): `symbol, displayName, sector, industry`.
- **stdout JSON** (full detail). SMC variant adds: `rating, rating_reason, trailingPE, forwardPE, peg,
  fcf_yield, debt_to_equity, rel_strength_6m, pct_below_52w_high, days_to_earnings, structure_trend,
  last_break, bars_since_break, range_position, zone, ob_low, ob_high, price_above_ob, near_ob`.

## Run

```bash
python screener.py          # base
python screener_smc.py      # SMC + rating
```

Writes `screener_results.csv` in this folder and prints JSON to stdout. Use the project's Python
(Anaconda) interpreter; requires `yfinance`, `pandas`.

## Known caveats

- **yfinance field validity**: only `ebitdamargin`/`grossprofitmargin`/`netincomemargin` are valid
  margin screener fields — `operatingmargin` is not (hence the proxy + per-ticker check).
- **Yahoo data artifacts**: adjusted closes can carry corporate-action errors (e.g. SNDK shows a ~47×
  "return" from a botched spinoff adjustment) — affects price-derived metrics.
- **Rating fails open**: if valuation/RS data is missing, those red-flag checks are skipped, so a
  data-poor name can still rate `buy` on entry signals alone.
- A `rating` is a **ranking aid**, not advice — it captures no qualitative/moat/macro context.
