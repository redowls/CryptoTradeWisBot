# Professional Swing-Trading Strategy Logic for a Crypto Trading Bot

## TL;DR
- **Build the bot as a confluence engine, not a single-signal trigger:** identify support/resistance algorithmically (swing pivots, price-cluster levels, volume profile), require a *confirmed* breakout (close beyond the level + volume ≥1.5× average + optional retest), then gate every entry through a triple-moving-average trend filter and an "overextension" check, and combine all of it into a 0–100 confidence score that drives position size.
- **Risk management is the real edge:** risk a fixed 1–2% of equity per trade, size via `Position = (Equity × Risk%) / StopDistance`, place ATR-based stops (2–3× ATR for crypto swing trades), cap total portfolio "heat" at ~6%, and scale size with conviction using fractional-Kelly logic (quarter-Kelly normal, half-Kelly highest conviction).
- **Crypto needs wider tolerances:** 24/7 markets mean no gaps but thin weekend liquidity and 3–7% daily Bitcoin ranges; use EMAs over SMAs, widen ATR multipliers, treat levels as zones (±0.2–0.5%) not lines, and add a volatility/regime filter so the bot trades trends and stands aside in chop.

## Key Findings

1. **Support/resistance is best detected programmatically via confirmed swing pivots plus clustering.** A swing high is a bar whose high exceeds N bars on each side; a swing low is the mirror. Cluster nearby pivots into zones, weight them by touch count and volume, and treat the strongest as S/R. Volume Profile's Point of Control (POC) and Value Area edges (VAH/VAL) are among the most reliable levels because they mark where real volume traded.

2. **A genuine breakout requires confirmation, not just a level cross.** Professionals require (a) a *candle close* beyond the level (not an intrabar wick), (b) a volume surge (commonly ≥1.5× the 20-period average; some use ≥120% or even 200%+), and (c) ideally a successful *retest* where old resistance becomes support. A buffer of ~0.5% or 1 ATR beyond the level filters noise.

3. **Triple-MA systems generate a single trend signal through "stacking."** When fast > mid > slow and price is above all three, the trend is bullishly aligned; the reverse is bearish; intertwined = no trade. EMAs are preferred over SMAs for crypto. The short set (8/10/20) times entries; the long set (21/34/55, Fibonacci numbers) acts as the trend filter.

4. **Combine signals with a weighted confluence score (0–100) and trade only above a threshold (~70).** Each component (trend alignment, breakout quality, momentum, volume, volatility regime) contributes weighted points. Higher score → larger position, implemented via tiered risk (e.g., 0.5%/1%/2%) or a fractional-Kelly multiplier.

5. **Crypto strategies need wider stops, EMA-based MAs, and volatility filters** to handle 24/7 trading, weekend liquidity gaps, and far higher volatility than equities.

6. **Risk management rules are concrete and codifiable:** 1–2% risk per trade, ATR-based stops, R-multiple targets with partial scale-outs, trailing stops (Chandelier/ATR), max concurrent positions tied to ~6% portfolio heat, and explicit anti-overtrading gates.

## Details

### 1. Support and Resistance — Programmatic Detection

**Swing highs/lows (pivot points).** The foundational, easily-coded method: a bar `i` is a swing high if `High[i] > High[i±1..n]` for a lookback/lookforward of `n` bars (the "pivot strength"). A swing low is the mirror. A pivot is only *confirmed* `n` bars after it forms (this is a deliberate lag, not repainting). Series of higher swing highs + higher swing lows = uptrend; lower highs + lower lows = downtrend. Higher `n` yields fewer, more significant levels; lower `n` yields more, noisier levels.

**Classic (floor-trader) pivot points.** Computed from the prior period's high/low/close: `P = (H + L + C) / 3`; `R1 = 2P − L`, `S1 = 2P − H`; `R2 = P + (H − L)`, `S2 = P − (H − L)`. In 24/7 crypto, define the "period" as a rolling daily (e.g., UTC day) since there's no session close.

**Horizontal levels from price clusters.** Collect recent confirmed pivots, sort by price, and merge any within a tolerance band (a "cluster tolerance" as a % of price) into a single level that carries a *touch count* and *time span*. Levels with more touches and longer spans score higher. Recommended touch tolerance by asset class: **crypto 0.2–0.5%**, forex 0.05–0.15%, equities 0.1–0.3%. This is directly codifiable: bucket prices into bins, count touches per bin, keep the highest-scoring bins above/below current price.

**Volume Profile (POC / Value Area).** Plots traded volume horizontally by price. Per TradingView's definition, the **POC** is "the price level… with the highest traded volume" (a "magnet" and strong S/R), and the **Value Area (VA)** is "the range of price levels in which the specified percentage of all volume was traded… Typically, this percentage is set to 70%" — the 70% deriving from the normal-distribution (~1σ) model — bounded by **VAH** (Value Area High) and **VAL** (Value Area Low). High-Volume Nodes (HVNs) act as support/resistance where price stalls; Low-Volume Nodes (LVNs) are where price moves fast (breakout zones). These are described as among the most statistically reliable S/R zones because they reflect actual participation rather than just price.

**Other algorithmic approaches** documented for code: Rolling Midpoint Range, Fibonacci retracement levels (especially 61.8%), K-Means price clustering, Donchian channels (highest-high/lowest-low over a lookback), and regression channels. For a bot, the pragmatic stack is: **confirmed pivots → cluster into zones → rank by touch count + volume → overlay Volume Profile POC/VAH/VAL.**

**Breakout vs. fakeout confirmation.** A *true* breakout typically shows:
- A decisive **candle close** beyond the level (intrabar wicks through the level are the classic trap — "the first tick is often a trap").
- A **volume surge** at the break. Concrete thresholds in the literature: breakout volume ≥1.5× (or "50%+ above") the 20-day average is considered more likely to succeed; some use ≥120% of the 20-bar average as a filter; single-candle volume spikes that immediately fade are suspect (often stop-runs).
- **Follow-through:** price holds above the level and begins building higher lows.
- **Strong candle body** (e.g., body > 70% of range, or body > 0.8× ATR) signals genuine momentum vs. a thin wick.

A *false* breakout shows low/average volume, immediate reversal back into the range, long rejection wicks, and breaks against the higher-timeframe trend.

**The retest.** After a real breakout, price often drifts back to the broken level; if old resistance now holds as support (rejection candle + sustained volume), the breakout is confirmed and offers a lower-risk entry with a tight stop just below the retest low. Why traders wait: it filters a large share of fakeouts and improves risk/reward. Caveat: retests don't always happen — the bot should support both an aggressive "enter on confirmed breakout close" mode and a conservative "wait for retest hold" mode. In crypto/forex where centralized volume is imperfect, ATR *expansion* on the breakout bar serves as a proxy for participation.

### 2. Overpriced / Extended Entry Detection (Chasing vs. "Nice to Have")

After a breakout, the bot must judge whether price is still a good entry or already overextended. Codifiable measures:

**Distance from the breakout level.** The further price has already traveled beyond the level, the worse the entry. A practical rule: only enter within X% (or within ~1 ATR) of the breakout level; if price is already several ATRs above, flag as "chasing."

**Distance from a key moving average (extension).** Price stretched too far above a reference MA is a mean-reversion risk. Two codifiable quantifications:
- **Percent extension:** `(Price − MA) / MA × 100`. Daily-Price-Action-style traders use the 10/20 EMA zone; gaps of >5–8% above the 50-day MA are flagged as overextended.
- **Z-score / ATR units:** `Z = (Price − MA) / StdDev` — fade-risk thresholds commonly at +2 to +3 standard deviations. Equivalently measure extension in ATR units above the MA; "super-extended" risk zones in published tools sit around +3 to +5× ATR from a baseline SMA.

**RSI overbought.** Per J. Welles Wilder ("New Concepts in Technical Trading Systems," 1978, default 14-period), RSI is "considered overbought when above 70 and oversold when below 30" — and, critically, "during strong trends, the RSI may remain in overbought or oversold for extended periods" (some traders raise the threshold to 80 in strong uptrends). So RSI alone is not a sell signal. Use it as a *throttle*: a long entry rule like "enter only if RSI < 70" stops the bot chasing a parabolic move, while bearish RSI divergence (price higher high, RSI lower high) is a stronger exhaustion warning.

**Risk/reward to the next resistance.** This is the decisive filter. Compute the distance from entry to the next overhead resistance (target) and divide by the distance from entry to the logical stop. If `(NextResistance − Entry) / (Entry − Stop)` falls below a minimum (commonly **2:1**, sometimes 1.5:1), the trade is "overpriced" — the easy money is gone because price is too close to the next ceiling relative to its risk. Professionals explicitly skip a "good-looking" setup if it doesn't offer ≥2:1.

**Stop/target for evaluation.** Place the stop where the setup is invalidated (below the breakout level, below the retest low, or 1.5–2 ATR below entry) and the target at the next significant resistance (or measured move = range height projected from the breakout). Then evaluate R:R *before* entry. If favorable (≥2:1), the entry is "nice to have"; if not, stand aside and wait for a pullback/retest that resets the risk.

### 3. Triple Moving Average Systems

**Core logic (R.C. Allen's triple MA crossover).** Three MAs of increasing length. Buy signals come early in a trend; the third (slowest) MA confirms/denies signals from the other two, reducing false signals. Shorter MAs react first when a trend starts.

**Stacking / ribbon alignment (the single most useful rule for a bot):**
- **Bullish:** `fast > mid > slow` AND price above all three → strong, aligned uptrend.
- **Bearish:** `fast < mid < slow` AND price below all three → strong downtrend.
- **No trend / avoid:** MAs intertwined (not in order) → consolidation; skip.
The wider the separation between the MAs, the stronger the momentum; tightly bunched/flat MAs warn of chop.

**Crossover signals:**
- Bullish trigger: fast crosses above mid, then mid crosses above slow (full alignment = strongest confirmation).
- Bearish trigger: fast crosses below mid and slow.
- Partial crossovers (fast over mid only, while still below slow) are weaker, counter-trend signals — useful for the confidence score but not a standalone entry.

**The two configurations:**
- **Short-term set (8, 10, 20):** faster, for *entry timing* and short swing momentum. (Common scalping cousins: 5/8/13.)
- **Long-term set (21, 34, 55):** these are Fibonacci numbers heavily used in crypto. Use as the *trend filter / regime* — only take long entries when the 21/34/55 set is bullishly stacked. The 8/21/34/55 EMA "Fibonacci ribbon" is a documented crypto convention (e.g., trade only when the Fib EMAs are stacked and above the 200).

**How to use both together (recommended bot architecture):** Use the **long set (21/34/55) as a gate** — define the dominant trend. Use the **short set (8/10/20) for the trigger** — fire the entry only in the direction the long set permits. This is the standard "higher set filters, lower set times" structure and mirrors multi-timeframe alignment (e.g., daily trend, hourly entry).

**EMA vs SMA.** For crypto swing trading, **EMA is preferred** — it weights recent prices (smoothing multiplier `2/(N+1)`) and reacts faster, which matters in volatile, fast-moving crypto. SMA is smoother but laggier and will not align as responsively; since the strategy requires multiple MAs to "line up," EMAs are the better choice. The trade-off: EMAs produce more false signals in chop, so pair with the volatility/regime filter.

**Generating a single buy/hold/avoid signal from three MAs:**
- BUY: long set bullishly stacked AND short set bullishly stacked AND price > fast EMA.
- HOLD: stacking intact but no fresh trigger / price extended.
- AVOID: MAs not stacked (intertwined) or stacked bearishly.
Add ADX > 20–25 as a trend-strength gate to avoid range-bound whipsaws (a documented enhancement to triple-EMA systems).

### 4. Combining Signals + Confidence Scoring

**Confluence principle.** True confluence requires agreement across *independent* signal categories (trend, momentum, volume, volatility, structure) — not five indicators from the same family. Stacking correlated indicators creates false confidence. Backtests cited by educators suggest single indicators run ~40–50% accuracy while layered, non-correlated confluence can reach ~65–80%.

**Weighted scoring (0–100).** Published multi-factor systems compute a composite as a weighted average of independent components and trade only above a threshold. A representative professional weighting scheme (5-factor, trend-following preset):
- **Structure/trend alignment: ~30%**
- **Proximity to key level: ~25%**
- **Session/higher-timeframe context: ~20%**
- **Momentum: ~15%**
- **Volatility regime: ~10%**
Higher-timeframe trend agreement is often up-weighted (e.g., 1.5× a normal component). For swing trading specifically, weights shift toward structure (~35%) and proximity (~30%).

**Thresholds and tiers** (common defaults across confluence tools): trade only if score **≥ 70**; treat **≥ 80** as "prime" (all factors aligned) and **≥ 90** as extreme conviction; 65–79 = good; 40–64 = wait; < 40 = avoid. Some additive systems use "N of M" points (e.g., ≥5 of 10 factors, or grade A = ≥3 of 4).

**A concrete bot scoring template** (adapt weights, then normalize to 100):
- Trend (21/34/55 stacked + price above): 0–30
- Breakout quality (clean close beyond level + buffer): 0–20
- Volume confirmation (≥1.5× avg): 0–15
- Short-set trigger (8/10/20 stacked/cross): 0–15
- Momentum/RSI in healthy zone (50–70, not >70): 0–10
- Volatility/regime OK (ATR in normal band, ADX>20): 0–10
→ Sum = confidence 0–100. Require ≥70 to enter; below that, alert-only.

**Using confidence to scale position size.** Two professional approaches:
- **Tiered fixed-risk:** low conviction (score 70–79) → risk 0.5–1%; medium (80–89) → 1–1.5%; high (≥90) → 2% (never exceed the per-trade cap). This keeps the math mechanical.
- **Fractional-Kelly conviction multiplier:** Kelly fraction `f* = (b·p − q)/b` (b = win/loss payoff ratio, p = win prob, q = 1−p). Professionals almost never use full Kelly (drawdowns of 30–50%); the practical standard is **25–50% Kelly**. Map conviction to the fraction: **quarter-Kelly (×0.25) for normal-conviction trades, half-Kelly (×0.5) for the highest-conviction setups.** Per MacLean, Ziemba & Blazenko (1992, *Management Science*), "using Half Kelly provides approximately 75% of the growth rate of Full Kelly but with only 50% of the volatility" — a strong reason to stay fractional. Notably, per JournalPlus's Kelly guide, for a thin-edge strategy "a trader with a 45% win rate and a 1.33R average gets f* ≈ 3.6%. Quarter Kelly is then 0.9% — nearly identical to the standard 1% rule… This validates traditional risk guidelines as a reasonable default for strategies with thin edges."

**Note the doctrinal split (be explicit in the spec):** One camp (Van Tharp, many educators) argues size should be *mechanical* (fixed %) and conviction should affect *whether* you trade, not *how much* — "no matter how good your conviction, you can still lose." The Kelly/quant camp argues size *should* scale with edge/probability. A defensible middle path for the bot: fixed 1% base risk, with a modest conviction multiplier (0.5×–2×) bounded by a hard 2% cap.

**Standard position-sizing formula (Van Tharp CPR):**
`Position Size = (Account Equity × Risk%) / Risk-per-unit`, where Risk-per-unit = (Entry − Stop). Example: $50,000 × 1% = $500 risk; if stop is $2.50/unit away, buy 200 units. This is dollar-risk control, not position-value control — a $30,000 position on a $25,000 account is fine if the stop caps loss at $500. Tharp's "anti-martingale" principle: always size off *current* equity so size shrinks after losses and grows after wins.

### 5. Crypto-Specific Considerations

**24/7 markets, no open/close.** Moving averages never "gap" (continuous data, more reliable TA), but a 20-period MA on a 1-hour chart covers less than a calendar day. Define daily reference levels on a fixed boundary (e.g., UTC 00:00). Pivot-point "previous day" must use the rolling UTC day.

**Higher volatility.** Bitcoin's daily ATR has run roughly 3–7% (mid-cap alts routinely 5–15%/day; micro-caps 20–50% on a single whale trade). Practical adjustments:
- **Treat S/R as zones, not lines:** allow ±0.2–0.5% (BTC/ETH) and wider for alts, because wicks pierce levels routinely.
- **Wider, ATR-based stops:** crypto swing stops commonly **2–3× ATR** (vs. 1.5–2× for calmer equities); in strong trends 3–4× ATR. Use 14-period ATR (21 for smoother swing data).
- **Volatility filter / regime gate:** only trade breakouts when ATR is expanding or in a normal band; express ATR as a percentile of its own trailing range (e.g., trade trends in the 25th–75th percentile; expect mean reversion above the 75th). Reduce size when ATR is in the top quartile.
- **Weekend liquidity:** volume typically drops ~20–25% below weekday averages, thinning order books and amplifying moves while you sleep — consider reducing size or widening stops over weekends, and treat weekend breakouts with extra skepticism (thin-volume breaks fail more).

**MA periods for crypto.** EMA strongly preferred. Popular crypto swing combos: 9/21 EMA on 4H/daily, 21/50/200 for trend, the 8/21/34/55 Fibonacci ribbon for stacking. Higher timeframes (4H+) give more reliable signals. On perpetuals/leverage, traders deliberately use *slower* filters (50-period on 4H+) to cut whipsaw.

**Volume caveat:** crypto volume is venue-fragmented and partly unreliable; supplement with OBV, and use ATR expansion as a participation proxy for breakout confirmation.

### 6. Risk Management Basics for Swing Bots

**Stop-loss placement (use the *widest-justified* logical stop, then size down):**
- **ATR-based:** Stop = Entry − (ATR × multiplier); 2× ATR for normal swing, 1.5× in quiet/ranging, 3× in strongly trending/volatile crypto. A 1× ATR stop triggers on noise ~50% of the time, so ≥2× is the practical floor.
- **Structure-based:** just below the broken level / retest low / recent swing low. Don't place stops *inside* the range you just broke (noise will hit them).
- **Percentage:** simpler but ignores volatility; ATR is preferred because it adapts per asset.

**Take-profit / targets:**
- **R-multiples:** Target = Entry + (StopDistance × R). Require ≥2:1 minimum; trend trades 3:1+. A 2:1 minimum keeps a strategy profitable even at a 40% win rate.
- **Structure targets:** next resistance, VAH, prior swing high, or measured move (range height projected from breakout).
- **Partial scale-outs:** a common structure is **50% at 1R, 25% at 2R, trail the final 25%**; or 30%/40%/30% across 1.5R/2R/3R. Move stop to breakeven after the first partial. Caveat: scaling out mathematically lowers expected value per winner (blended R falls) — its value is reduced variance and psychological/operational robustness; take partials at *logical levels* (resistance), not arbitrary ones.

**Trailing stops:**
- **Chandelier Exit:** `HighestHigh since entry − (ATR × 3)` for longs. Developed by Chuck LeBeau (popularized in Alexander Elder's 2002 *Come Into My Trading Room*); per StockCharts ChartSchool the default parameters are (22, 3.0) — 22 = trading days per month for the ATR period, 3.0 = the ATR multiplier. It only ratchets up, never down.
- **ATR trail / MA trail:** trail at 1.5–2× ATR or under the rising 20 EMA / recent swing lows. Crypto-specific trailing distances: ~5–10% for BTC/ETH, 10–15% for volatile alts, but always ≥1× ATR to avoid premature exits.
- Rule: never move a trailing stop backward; tighten as the trade reaches higher R multiples.

**Max concurrent positions & portfolio heat:**
- **Portfolio heat** = sum of risk across all open positions. Cap at **~6%** (conservative 3–4%, aggressive up to 6–8%). At 1% risk/trade that's ~5–6 positions; at 1.2% it's 5.
- **Correlation:** crypto is highly correlated (alts follow BTC); intra-asset correlation spikes toward 1.0 in selloffs, so 5 "different" coins can behave as one position. Limit correlated exposure and reduce heat in volatile/crisis regimes (e.g., 2–3% in crisis).
- Keep cash reserves (20–30%) for new setups and to avoid over-allocation.

**Avoiding overtrading:**
- Hard gate: no entry unless confidence ≥ threshold AND R:R ≥ 2:1 AND trend filter aligned. Quality over quantity (2–5 quality trades/week beats many marginal ones).
- **Time stop:** if a breakout hasn't moved ≥~1% in your favor within a set window (e.g., a few bars/days), exit and free the capital — crypto's 24/7 opportunity cost is high.
- Trade only in the direction of the higher-timeframe trend; skip choppy/ranging regimes (ADX < 20–25, flat MAs).
- After a drawdown (e.g., >10%), automatically reduce risk % until equity recovers.

## Recommendations

**Stage 1 — Build the level + trend engine.** Implement confirmed swing-pivot detection (start with pivot strength `n = 5` on your trading timeframe), cluster pivots into zones (crypto tolerance 0.3–0.5%), and overlay Volume Profile POC/VAH/VAL. Compute both EMA sets (8/10/20 and 21/34/55) and ADX. Output a per-bar trend state: bullish-stacked / bearish-stacked / no-trend. *Benchmark to advance:* on historical data, the engine should label obvious trends correctly and produce stable (non-repainting) levels.

**Stage 2 — Add breakout confirmation + overextension filter.** Require: close beyond level + 0.5%/1-ATR buffer, volume ≥1.5× 20-bar average (or OBV/ATR-expansion proxy), and optionally a retest hold. Reject entries where price is >X ATR beyond the level, RSI > 70, extension Z-score > +2, or R:R to next resistance < 2:1. *Benchmark:* in backtest, the false-breakout rate should drop materially vs. naive "cross the line" entries.

**Stage 3 — Wire the 0–100 confidence score and position sizing.** Use the scoring template above (normalize to 100), require ≥70 to enter. Size with `Position = (Equity × Risk%) / (Entry − Stop)`, base risk 1%, conviction multiplier 0.5×–2× (quarter-Kelly normal, half-Kelly for ≥90 scores), hard cap 2% per trade and 6% portfolio heat. *Benchmark:* on a 100+ trade backtest, confirm max drawdown stays tolerable and that higher-confidence buckets show better expectancy (if not, flatten the multiplier toward fixed sizing).

**Stage 4 — Layer exits and portfolio controls.** ATR/Chandelier trailing stop (22-period, 3× ATR), partial scale-out (50% at 1R → breakeven stop → trail rest), time stop, max 5 concurrent positions, correlation cap, and a drawdown-triggered risk reducer. *Benchmark:* paper-trade live for a defined period; require the live false-breakout and slippage behavior to match backtest assumptions before scaling capital.

**Thresholds that should change your behavior:** if win rate < 40% with R:R ≥ 2:1, the edge is likely gone — pause and re-tune. If ATR percentile is in the top quartile market-wide, cut size and widen stops. If realized correlation across open positions > 0.7, treat them as one position for heat purposes.

## Caveats

- **Indicator-tool defaults are suggestions, not validated optima.** The specific numbers from TradingView/MQL5 confluence scripts (score ≥70, weights like 30/25/20/15/10, "5 of 10") are vendor defaults carrying "not financial advice" disclaimers — treat them as starting points and validate by backtest on your assets and timeframe.
- **There is a genuine professional disagreement on conviction-based sizing.** Van Tharp–style educators argue size should be mechanical and conviction should only affect *whether* you trade; the Kelly/quant camp argues size should scale with edge. The bot should make this a configurable choice, defaulting to conservative (small multiplier, hard cap).
- **Moving averages and breakout systems are lagging and fail in chop.** They whipsaw in range-bound markets; the volatility/ADX/regime filter is essential, and even then expect a low win rate offset by larger winners (typical of breakout strategies).
- **Crypto volume data is fragmented and partly unreliable**, weakening classic volume-confirmation rules; lean on ATR expansion and OBV as supplements.
- **Backtests overstate live performance.** They routinely ignore slippage, fees, funding (on perps), thin-liquidity gaps, and stop-hunting/whale activity common in crypto. Size conservatively until live results corroborate the backtest.
- **All thresholds here are industry rules of thumb, not laws.** They require backtesting and walk-forward validation (ideally 100+ trades) on the specific instruments the bot will trade before being trusted with size.