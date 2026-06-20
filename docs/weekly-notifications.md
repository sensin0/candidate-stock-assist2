# Discord weekly notifications

This repository can update the cloud ranking once a week with GitHub Actions and send the latest top ranking to Discord on a smartphone.

## How it works

- `.github/workflows/weekly-ranking-notify.yml` runs every Monday at 08:23 JST.
- `scripts/weekly_cloud_ranker.py` reads `cyclical_tickers.csv`, fetches latest market and financial data with `yfinance`, builds a Ta-chan-style ranking, and sends the top results to Discord.
- If a stock is in the top 10 and its latest quarterly financial period is recent, the Discord message adds it to a priority earnings-check section. This is a practical proxy for recent earnings updates because free data does not always expose the exact Japanese earnings announcement date.
- The full JSON report is uploaded as a GitHub Actions artifact named `weekly-ranking-report`.

## Discord setup

1. Install Discord on your phone and enable notifications.
2. Create a private server and a channel for ranking notifications.
3. Open the channel settings, create a webhook, and copy the webhook URL.
4. In GitHub, open the repository settings.
5. Go to `Secrets and variables` -> `Actions`.
6. Add a repository secret:
   - `DISCORD_WEBHOOK_URL`: the Discord webhook URL

## Manual test

After pushing the workflow to GitHub, open the repository's `Actions` tab and run `Weekly Ranking Notify` manually with `workflow_dispatch`.

If no notification arrives, check the workflow logs. The report is still uploaded as an artifact even when `DISCORD_WEBHOOK_URL` is missing.
