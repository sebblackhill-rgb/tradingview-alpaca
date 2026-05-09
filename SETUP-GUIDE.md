# TradingView → Alpaca Automated Trading Bot
## Complete Beginner Setup Guide

This bot automatically buys and sells stocks and crypto on your Alpaca account
whenever your TradingView UTBot indicator fires a signal.

---

## What You Will Need

- A Mac computer
- A TradingView account (paid plan — Plus, Premium or Expert)
- An Alpaca account (free at alpaca.markets)
- A GitHub account (free at github.com)
- A Railway account (free at railway.app)

---

## PART 1 — Install Git on Your Mac

1. Press **Command + Space** on your keyboard
2. Type **Terminal** and press Enter
3. A black and white window opens — this is Terminal
4. Type the following and press Enter:

```
xcode-select --install
```

5. A popup appears — click **Install** and wait a few minutes
6. When it finishes, Git is ready

---

## PART 2 — Set Up Your Files

1. Create a new folder on your Desktop called **tradingview-alpaca**
2. Download all these files into that folder:
   - main.py
   - requirements.txt
   - Procfile
   - env.example.txt
   - gitignore.txt

3. Open Terminal and rename two files by typing these commands one by one,
   pressing Enter after each:

```
mv ~/Desktop/tradingview-alpaca/env.example.txt ~/Desktop/tradingview-alpaca/.env
```

```
mv ~/Desktop/tradingview-alpaca/gitignore.txt ~/Desktop/tradingview-alpaca/.gitignore
```

4. Open the **.env** file — you can use TextEdit on your Mac
5. Fill in your details (see Part 3 below for how to get them):

```
ALPACA_API_KEY=paste your key here
ALPACA_SECRET_KEY=paste your secret here
ALPACA_BASE_URL=https://paper-api.alpaca.markets
WEBHOOK_PASSPHRASE=make up any secret word e.g. mybot2024
```

6. Save and close the file

---

## PART 3 — Get Your Alpaca API Keys

1. Go to app.alpaca.markets and create a free account
2. Once logged in, look at the top of the page — make sure it says **Paper Trading**
3. Click **API Keys** on the left sidebar
4. Click **Generate New Key**
5. Copy both the **Key** and the **Secret** — paste them into your .env file
6. Keep these private — never share them with anyone

---

## PART 4 — Push Your Code to GitHub

1. Go to github.com and create a free account
2. Click the **+** icon at the top right → **New repository**
3. Name it **tradingview-alpaca**
4. Leave everything else as default
5. Click **Create repository**

Now go back to Terminal and run these commands one by one:

```
cd ~/Desktop/tradingview-alpaca
```
```
git init
```
```
git add .
```
```
git commit -m "first commit"
```

Then GitHub will show you three commands on the screen after you created
the repository. Copy and run those three lines in Terminal one by one.

When it asks for your GitHub username and password — use your username
and paste your **Personal Access Token** as the password (not your
actual GitHub password).

To get a Personal Access Token:
- Go to github.com → click your profile picture → Settings
- Scroll down → click **Developer settings**
- Click **Personal access tokens** → **Tokens (classic)**
- Click **Generate new token (classic)**
- Give it any name, set expiry to 90 days, tick **repo**
- Click **Generate token** and copy it immediately

---

## PART 5 — Deploy on Railway

1. Go to railway.app and click **Sign up with GitHub**
2. Click **New Project** → **Deploy from GitHub repo**
3. If no repositories appear, click **Configure GitHub App** and give
   Railway permission to access your tradingview-alpaca repository
4. Select **tradingview-alpaca** — Railway starts building automatically

5. Once deployed, click your **web** service → **Variables** tab
6. Add these 4 variables one by one:

| Key                | Value                                    |
|--------------------|------------------------------------------|
| ALPACA_API_KEY     | your Alpaca paper API key                |
| ALPACA_SECRET_KEY  | your Alpaca paper secret key             |
| ALPACA_BASE_URL    | https://paper-api.alpaca.markets         |
| WEBHOOK_PASSPHRASE | the secret word you chose in your .env   |

7. Click **Deploy** — Railway rebuilds with your variables

8. Click **Settings** → **Networking** → **Generate Domain**
9. Copy your public URL — it looks like:
   https://web-production-xxxxx.up.railway.app

---

## PART 6 — Test Your Bot

Open Terminal and run this command — replace the URL and passphrase
with your own values:

```
curl -X POST https://YOUR-RAILWAY-URL.up.railway.app/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "passphrase": "YOUR_PASSPHRASE",
    "symbol": "AAPL",
    "side": "buy",
    "qty": 1
  }'
```

You should see a response like:
```
{"status": "order_submitted", "order_id": "..."}
```

Then check your Alpaca paper account → Orders to confirm the order appeared.

---

## PART 7 — Connect TradingView Alerts

For each stock or crypto you want to trade, create TWO alerts in TradingView
— one for buy and one for sell.

### Buy Alert:
1. Open the chart with UTBot indicator
2. Right click on the chart → **Add Alert**
3. Condition: **UT Bot Alerts (1, 10)** → **UT Long**
4. Trigger: **Once per bar close**
5. Click **Message** and paste:

```json
{
  "passphrase": "YOUR_PASSPHRASE",
  "symbol": "{{ticker}}",
  "side": "buy",
  "qty": 1
}
```

6. Click **Notifications** → tick **Webhook URL** → paste your Railway URL
   followed by /webhook:
   https://YOUR-RAILWAY-URL.up.railway.app/webhook
7. Click **Create**

### Sell Alert:
Repeat the same steps but:
- Condition: **UT Long** → change to **UT Short**
- Message: change "side": "buy" to "side": "sell"

### For Crypto:
- Bitcoin: use "symbol": "BTC/USD" in the message
- Solana: use "symbol": "SOL/USD"
- Ethereum: use "symbol": "ETH/USD"
- For stocks: use "symbol": "{{ticker}}" — TradingView fills this in automatically

---

## PART 8 — Important Notes

**Quantity:**
- qty means number of shares for stocks
- qty means number of coins for crypto (use 0.1 for Bitcoin — 1 whole Bitcoin is expensive)

**Paper vs Live Trading:**
- You are on paper trading — this uses fake money, no real risk
- Watch how the bot performs for several weeks before switching to live
- To go live: get live API keys from Alpaca and update your Railway variables

**Your bot runs 24/7 — you can close your Mac.**
Railway keeps the server running in the cloud all the time.

**Railway costs $5/month after the free 30-day trial.**
Your server is lightweight so usage will be well under $5/month.

---

## Checking Your Bot is Working

- **TradingView**: click the clock/bell icon on the right → Log tab to see alerts firing
- **Alpaca**: go to your paper account → Orders to see trades placed
- **Railway**: click your web service → Deploy Logs to see live server activity

---

## Getting Help

If you see an error:
1. Take a screenshot of the error message
2. Check Railway → Deploy Logs for the full error details
3. Most common issues are wrong passphrase, wrong symbol format,
   or environment variables not saved in Railway

---

## Disclaimer

Automated trading carries significant financial risk.
Always test thoroughly on paper trading before using real money.
Past performance of any indicator does not guarantee future results.
