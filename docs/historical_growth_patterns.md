# Historical Growth Pattern Validation

Generated: 2026-11-15 08:20:55

## Method

Annual financial event + 60-day reporting lag; train before 2024; validate from 2024; target cyclical sectors only.

## Robust patterns

| Pattern | Train n / median / alpha | Validation n / median / alpha / win-vs / stop |
|---|---:|---:|
| Profit Pattern=profitable / Revenue Bucket=10-14.9% / Price Bucket=30-49.9% | 12 / 29.4% / 12.5% | 12 / 34.2% / 10.2% / 75.0% / 33.3% |
| Profit Pattern=profitable / Revenue Bucket=30%+ / Price Bucket=50%+ | 18 / 35.6% / 15.0% | 7 / 34.1% / 37.0% / 57.1% / 28.6% |
| Profit Pattern=profitable / Revenue Bucket=15-19.9% / Price Bucket=50%+ | 35 / 28.2% / 7.6% | 27 / 17.4% / 12.2% / 63.0% / 37.0% |
| Profit Pattern=profitable / Revenue Bucket=15-19.9% | 56 / 25.2% / 4.8% | 40 / 12.9% / 3.4% / 60.0% / 40.0% |
| Profit Pattern=loss_to_profit / Price Bucket=50%+ | 17 / 32.4% / 11.8% | 32 / 10.2% / 10.0% / 71.9% / 28.1% |

## Limitations

- Historical DB does not contain operating-income forecasts or historical market caps.
- Patterns require at least 5 train observations, 3 validation observations, and 4 unique tickers.
- This is pattern discovery, not a guarantee of future returns.
