# 🚀 Railway Template Deployment Guide

## Prerequisites

- [GitHub account](https://github.com)
- [Railway account](https://railway.app) (free $5/month credit)
- Telegram Bot Token from [@BotFather](https://t.me/BotFather)
- Your Telegram User ID (from [@userinfobot](https://t.me/userinfobot))

## Step 1: Fork Repository

1. Go to https://github.com/T0h1/yt-bot-railway
2. Click **"Fork"** button (top right)
3. Wait for fork to complete

## Step 2: Create Railway Project

1. Go to https://railway.app
2. Click **"New Project"**
3. Select **"Deploy from GitHub repo"**
4. Select your **forked repository**
5. Railway will start building automatically

## Step 3: Set Environment Variables

1. In Railway dashboard, click on your **bot service**
2. Go to **"Variables"** tab
3. Click **"New Variable"** and add:

```
BOT_TOKEN = your_telegram_bot_token_here
ADMIN_ID = your_telegram_user_id_here
```

### Optional Variables

```
GENIUS_API_TOKEN = your_genius_api_token_here
```

> 💡 Get Genius API token from https://genius.com/api-clients/new

## Step 4: Add PostgreSQL Database (Optional but Recommended)

1. In Railway dashboard, click **"+ New"**
2. Select **"Database"** → **"PostgreSQL"**
3. Railway auto-provisions and connects it
4. Bot detects `DATABASE_URL` automatically

## Step 5: Deploy

1. Railway auto-deploys on every push
2. Check **"Deployments"** tab for status
3. Check **"Logs"** tab for "Application started"

## Step 6: Test the Bot

1. Open Telegram
2. Find your bot by username
3. Send `/start`
4. Try downloading a song: send a YouTube/SoundCloud link

---

## Troubleshooting

### Bot not responding
- Check logs for errors
- Verify `BOT_TOKEN` is correct
- Ensure only 1 replica is running

### Lyrics not found
- Set `GENIUS_API_TOKEN` in Railway Variables
- Get token from https://genius.com/api-clients/new

### SoundCloud 403 errors
- Upload SoundCloud cookies via `/cookie` command
- Or add cookies.txt file

### File too large
- Bot auto-compresses files >50MB
- No action needed

---

## Environment Variables Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `BOT_TOKEN` | ✅ | Telegram Bot Token |
| `ADMIN_ID` | ✅ | Your Telegram User ID |
| `GENIUS_API_TOKEN` | ❌ | For lyrics fetching |
| `RATE_LIMIT_PER_MINUTE` | ❌ | Default: 5 |
| `RATE_LIMIT_PER_HOUR` | ❌ | Default: 20 |
