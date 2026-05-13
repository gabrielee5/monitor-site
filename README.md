# monitor-site

Watches the **Comune di Latina** news page for posts mentioning the electronic ID card
(*carta d'identità elettronica* / CIE / anagrafe) and pings a Telegram chat.

- Source: `https://www.comune.latina.it/home/novita.html`
- Runs on GitHub Actions, hourly **08:00–18:00 Europe/Rome** for fetch, with extra
  late-evening runs as EOD-recap safety nets. No server.
- State lives in `state.json`, committed back to the repo by the workflow.

## How it works

`main.py` fetches the news listing, finds article links (`/notizie/*.html`), and matches
keywords against the title + surrounding card text:

- `carta d'identità` (all apostrophe variants)
- `CIE`
- `identità elettronica`
- `anagrafe`
- `documento di riconoscimento`

Behavior:

1. **New URL + keyword match** → immediate Telegram message.
2. **First run at or after 17:00 Europe/Rome with no matches sent today** → one EOD
   message `❌ No CIE-related news today on comune.latina.it`. The EOD path is decoupled
   from the fetch window: a run delayed past 18:59 Rome still fires the recap.
3. **HTTP fetch fails** → exit non-zero, state untouched, next run retries.
4. **Telegram send fails for a match** → that URL is *not* marked seen, next run retries.

## Setup

1. Create a Telegram bot via [@BotFather](https://t.me/BotFather), copy the token.
2. Get your chat ID: message the bot, then
   `curl "https://api.telegram.org/bot<TOKEN>/getUpdates"` and read `result[].message.chat.id`.
3. In the GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**:
   - `TELEGRAM_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. Push this repo to GitHub.
5. In **Actions** tab, open *Monitor CIE news* → **Run workflow** (manual first run).
   This creates `state.json`; any currently-listed CIE article counts as a match
   and will notify on the first run.
6. After the first run looks correct, let the cron take over.

## Manual / local

```bash
pip install -r requirements.txt
python main.py --dry-run   # prints what would happen, no Telegram, no state write
```

`--dry-run` ignores `TELEGRAM_TOKEN`/`TELEGRAM_CHAT_ID` and does not modify `state.json`.

## Schedule details

Workflow cron is `0 6-19 * * *` UTC (14 runs/day).

| Italy time | CET (winter, UTC+1)  | CEST (summer, UTC+2) |
| ---------- | -------------------- | -------------------- |
| 08–18      | covered by 07–17 UTC | covered by 06–16 UTC |

`main.py` distinguishes two windows by Rome hour:

- **Active fetch window** (08–18 Rome): fetches the page, sends per-article matches.
- **EOD window** (≥17 Rome): sends the daily recap if nothing matched today.

Runs at 17–19 UTC (= 19–21 Rome CEST / 18–20 CET) fall outside the fetch window but
inside the EOD window — they exist purely as safety nets, since GitHub Actions cron is
routinely delayed 30+ min at the top of the hour and the recap must not be missed.
