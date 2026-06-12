# StreetEasy Housing Bot

A free Python bot that polls StreetEasy for NYC rentals matching your criteria and
emails you and your roommate the instant a new match appears, with a direct link
so you can contact the agent before the listing is gone.

## What it does

1. Loads your search (a pasted StreetEasy URL) and filters from `config.yaml`.
2. Every ~60-120 seconds, renders the search results with a stealthed headless
   browser and parses the listings.
3. For each brand-new listing it opens the detail page to read **year built** and
   **available date**, then applies your filters (price cap, beds/baths, exclude
   pre-war, Aug 1-15 move-in window).
4. Emails both recipients about every new match. A SQLite database ensures each
   listing is sent **once**.

```
config.yaml  ->  scraper (Playwright + stealth)  ->  filters  ->  email (Gmail)
                         |                                          ^
                         v                                          |
                  SQLite dedup  --------------------- only-once ----+
```

## Requirements

- Python 3.10+
- A Gmail account (to send mail for free)
- A machine to run it on (your laptop/Pi for a residential IP, or a free
  Oracle Cloud VM for always-on)

## 1. Install

```bash
cd streeteasy_bot
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium        # downloads the browser Playwright drives
```

On a fresh Linux VM you may also need system libraries for Chromium:

```bash
playwright install-deps chromium   # or: sudo apt-get install -y libnss3 libatk1.0-0 libgbm1 ...
```

## 2. Set up Gmail sending (free)

1. Turn on **2-Step Verification** for the Gmail account:
   https://myaccount.google.com/security
2. Create an **App Password**: https://myaccount.google.com/apppasswords
   (pick "Mail" / "Other"). You'll get a 16-character code.
3. Copy `.env.example` to `.env` and fill it in:

```bash
cp .env.example .env
```

```dotenv
GMAIL_USER=youraddress@gmail.com
GMAIL_APP_PASSWORD=abcdefghijklmnop      # the 16-char app password (spaces ok)
MAIL_RECIPIENTS=you@example.com,roommate@example.com
```

4. Verify it works:

```bash
python main.py --test-email
```

You and your roommate should receive a "bot is working" email.

## 3. Configure the search

Your search is already set in `config.yaml` under `search.urls` (pasted from your
browser, sorted newest-first). To change it, set up the search on StreetEasy,
copy the URL from the address bar, and replace the value.

Key filter settings in `config.yaml`:

- `filters.max_price: 4700` - hard price cap (your $2,500 + roommate's $2,200)
- `filters.beds: 2`, `filters.min_baths: 1`, `filters.max_baths: 2`
- `filters.exclude_pre_war: true`, `filters.min_year_built: 1947`
- `filters.move_in_start / move_in_end` - the Aug 1-15, 2026 window
- `filters.notify_on_missing_data: true` - still alert when year/date can't be read
- `poll.min_seconds / max_seconds` - how often it checks

## 4. Run it

```bash
python main.py            # runs forever
python main.py --once     # single cycle (good for testing)
python main.py -v         # verbose/debug logging
```

**First run** seeds a silent baseline of everything currently live, so you are
only emailed about listings that appear *afterward*. To be emailed about
currently-live listings too, run once with `--notify-existing`.

## Fetch modes (how it gets the data)

Set `fetch_mode` in `config.yaml`:

- **`http` (default, recommended):** plain HTTP requests. StreetEasy server-renders
  the listings (and detail pages), and plain requests are currently not challenged
  by PerimeterX -- even from datacenter IPs. No browser, no Chrome, no "Press &
  Hold". This is what makes cron and free 24/7 cloud hosting possible.
- **`browser` (fallback):** drives a real browser via Playwright. Only needed if
  StreetEasy ever starts blocking plain requests. See "Browser fallback" below.

With `http` mode you do **not** need Chrome open or Playwright installed -- just
run `python main.py`.

## Running continuously

Two easy options once `fetch_mode: http`:

### Option A: long-running loop (built in)
```bash
python main.py            # polls forever every poll.min_seconds..max_seconds
```
Keep it alive across reboots/crashes with the included systemd unit (see
"Run always-on, free" below) or any process manager.

### Option B: cron (like the reference project)
Run a single cycle on a schedule. The SQLite DB remembers what's been seen, so
each run only emails genuinely new listings.
```cron
# every 8 minutes (edit path to your venv + project)
*/8 * * * * cd /path/to/streeteasy_bot && .venv/bin/python main.py --once >> cron.log 2>&1
```
Run once manually first (without `--notify-existing`) to seed the silent baseline.

## Browser fallback: connect to your own Chrome

Only needed if you set `fetch_mode: browser` because plain requests started
getting blocked. This drives your real Chrome so you appear as a normal human.

1. Quit Chrome, then launch a dedicated debugging instance (separate profile so it
   won't touch your normal browsing):

   macOS:
   ```bash
   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
     --remote-debugging-port=9222 \
     --user-data-dir="$HOME/se-bot-chrome"
   ```

2. In that Chrome window, go to your StreetEasy search URL once and **solve the
   Press & Hold** if it appears. You should now see real listings. Keep this
   window open.

3. Point the bot at it - in `config.yaml`:
   ```yaml
   browser:
     cdp_url: "http://localhost:9222"
   ```

4. Run the bot as usual (`python main.py`). It attaches to your Chrome, reuses
   your cookies/session, and never launches its own browser. If a challenge ever
   reappears, just solve it in that window; the bot keeps polling.

> Leave `cdp_url: null` to instead have the bot launch its own browser (works on
> some networks, but more likely to be challenged).

## 5. Run always-on, free (Oracle Cloud)

With `fetch_mode: http`, plain requests work from datacenter IPs, so a free cloud
VM can run this 24/7 -- no browser, no Chrome, no Playwright needed.

1. Create an Oracle Cloud account and an **Always Free** VM (an Ampere A1 or
   VM.Standard.E2.1.Micro running Ubuntu).
2. SSH in, install Python and clone/copy this project.
3. Set up the venv, `pip install -r requirements.txt`, and create `.env`.
   (You can skip `playwright install` entirely in HTTP mode.)
4. Install the service, then either let it run as a loop or use cron (Option B above):

```bash
sudo cp deploy/streeteasy-bot.service /etc/systemd/system/
# edit the file's User / WorkingDirectory / ExecStart paths to match your VM
sudo systemctl daemon-reload
sudo systemctl enable --now streeteasy-bot
journalctl -u streeteasy-bot -f        # watch the logs
```

> If StreetEasy ever starts blocking plain requests from the VM (you'll see
> "Blocked" / backing-off logs), switch `fetch_mode: browser`, which then needs a
> residential IP (your home machine) to reliably solve challenges.

## Troubleshooting

- **"Bot challenge detected" / backing off** - StreetEasy is rate-limiting/blocking.
  The bot automatically waits longer. Increase `poll.min_seconds`/`max_seconds`,
  or switch to a residential IP.
- **No listings parsed (0 every cycle)** - StreetEasy likely changed their HTML.
  Update the selectors in `parse_search_html()` in `scraper.py`.
- **Login error sending email** - make sure you used a Gmail **App Password**, not
  your normal password, and that 2-Step Verification is on.
- **Flooded on first run** - that shouldn't happen (baseline is silent); if it does,
  delete `seen_listings.db` and start again without `--notify-existing`.

## Notes / disclaimer

Scraping StreetEasy is against their Terms of Service and your IP may be
challenged or blocked. This project is for personal use; use it respectfully
(modest poll rate) and at your own risk.
