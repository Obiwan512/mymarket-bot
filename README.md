# mymarket.ge -> Telegram bot (v3)

## 1. Install on your PC (5 minutes)
1. Unzip into your bot folder. Keep your `.env` and `seen.json` (imported once, nothing is re-sent).
   Better: move the folder out of OneDrive first (e.g. `C:\bots\mymarket-bot`) and delete the old `.venv`.
2. Open PowerShell in the folder and run the self test:
   ```
   .venv\Scripts\python.exe bot.py --selftest
   ```
   (no `.venv` yet? double-click `start_bot.bat` once, close it, then run the line above)
   `Telegram` and `mymarket API` must say OK. Photos / Detail endpoint / AI may say SKIP - the bot works without them.
3. Double-click `start_bot.bat`. It checks every 5 minutes until the window is closed.

## 2. Commands
| command | what it does |
|---|---|
| `python bot.py` | one check |
| `python bot.py --loop` | check forever (`--loop 180` for another interval, minimum 60) |
| `python bot.py --dry-run` | lists what is on the pages now and which listings pass your filters. Sends nothing. Use it to tune `watches.json` |
| `python bot.py --selftest` | checks Telegram, mymarket, photos, detail endpoint, exchange rate, AI key |
| `python bot.py --resolve "URL"` | turns a mymarket.ge URL into a payload and shows the first listings it returns |
| `python bot.py --reset` | forget everything seen |

## 3. watches.json
You only ever edit this file. The bot re-reads it on every check, no restart needed.
Adding a watch or changing its filters sends up to 3 previews and remembers the rest silently.

| key | meaning |
|---|---|
| `name` | unique label shown in messages |
| `enabled` | `false` switches the watch off |
| `payload` | sent to the mymarket API (`CatID`, `Brands`, `SortID`, ...) |
| `url` | alternative to `payload`: a mymarket.ge page URL, resolved automatically (test it with `--resolve` first) |
| `pages` | result pages to scan, 56 listings each (default 2) |
| `min_price` / `max_price` | always in GEL; dollar listings are converted at the National Bank rate |
| `must_include_any` | at least one of these words in title/description |
| `must_include_all` | all of these words |
| `exclude` | none of these words (`8GB` does not block `48GB`) |
| `ai_rule` | plain-language rule checked by an AI model, e.g. `"M2 or newer, 16GB+, not damaged, private seller"`. Needs a key in `.env`. Only listings that already passed price/keywords are sent to the AI |
| `fetch_details` | `true` = read each new listing's full description before filtering |
| `allow_no_price` | `false` = skip listings without a price when a price filter is set |
| `price_drops` | alert when an already-seen listing gets at least 3% cheaper |
| `skip_renewed` | `true` = no alerts for old listings bumped back to the top |

How to get a payload for another category: open the category on mymarket.ge, press F12 > Network > Fetch/XHR,
reload, click the `products` request > Payload, copy the fields. Or try `--resolve "URL"`.

## 4. AI filter (optional)
Put ONE key into `.env`:
```
GEMINI_API_KEY=...        (Google AI Studio)
# or
ANTHROPIC_API_KEY=...
```
Optional `AI_MODEL=...` overrides the default model. If the AI is down, out of quota or the key is wrong,
listings are still sent (marked "AI შემოწმება ვერ მოხერხდა") - a broken key never hides a listing.

## 5. Running 24/7
Pick ONE place to run it (two copies = every listing twice).

### A. Your PC (works today)
`start_bot.bat`, plus a shortcut to it in `shell:startup`. Runs while the PC is on.

### B. VPS (most reliable, about $4-5/month)
1. Copy the folder with your `.env` to an Ubuntu/Debian server.
2. `sudo bash deploy/install_vps.sh`
3. Look at the self-test it prints: if `mymarket API` says FAIL with HTTP 403, that provider's IP range is
   blocked by mymarket's Cloudflare - use another provider/region before paying further.
Logs: `journalctl -u mymarket-bot -f`.

### C. GitHub Actions (free)
1. Create a repository and push this folder (`.env` is ignored and must stay out of it).
2. Repo > Settings > Secrets and variables > Actions: add `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   (and `GEMINI_API_KEY` if you use `ai_rule`).
3. Repo > Settings > Actions > General > Workflow permissions: "Read and write".
4. Actions tab > mymarket-bot > Run workflow. Green = it works. Red with "mymarket HTTP 403" = GitHub's IPs
   are blocked; use A or B.
Notes: runs every 10 minutes (GitHub often starts late). Memory is kept in `state.json`, committed by the workflow.
A private repository on the free plan has a monthly Actions minute quota that a 10-minute schedule can exceed -
make the repository public (it contains no secrets) or change the cron in `.github/workflows/bot.yml` to `*/30`.

## 6. Optional .env settings
`CHECK_INTERVAL=300`, `FIRST_RUN_LIMIT=3`, `PRICE_DROP_MIN_PCT=3`, `SEND_PHOTOS=1`, `RENEWED_ID_GAP=150000`,
`KEEP_DAYS=60`, `ALERT_AFTER_FAILS=3`, `AI_MAX_PER_RUN=25`, `USD_RATE=2.7` (fallback only)

## 7. If something breaks
After 3 failed checks in a row the bot messages you with the reason and messages again when it recovers.
Details are in `bot.log`. Run `python bot.py --selftest` and read which line says FAIL.
