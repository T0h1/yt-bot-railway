# 🎵 YouTube/SoundCloud Media Bot

A powerful Telegram bot for downloading music and videos from YouTube, SoundCloud, Instagram, TikTok, Twitter, Facebook, and Twitch.

[![Deploy to Railway](https://railway.app/button.svg)](https://railway.app/template/yt-bot-railway)

## ✨ Features

- 🎵 **Audio Download** - Highest quality MP3 320kbps with metadata
- 🎬 **Video Download** - Quality selection (1080p/720p/480p/360p)
- 🖼️ **Album Art** - Automatic cover art embedding
- 📝 **Lyrics** - Auto-fetch lyrics from Genius API (lyricsgenius)
- 🎭 **Multi-platform** - YouTube, SoundCloud, Instagram, TikTok, Twitter, Facebook, Twitch
- 📊 **Progress Bar** - Real-time download progress
- ❤️ **Like/Dislike** - Rate songs
- 📜 **History** - Download history
- 🔍 **Search** - Search for artists on SoundCloud
- 👑 **Admin Dashboard** - Inline keyboard admin panel
- 🚦 **Rate Limiting** - 3-tier rate limit (per-minute, per-hour, per-day)
- 🧹 **Auto-cleanup** - Files deleted after upload
- 📱 **Range Download** - Download specific range of artist tracks

## 🚀 Deploy to Railway

### One-Click Deploy

[![Deploy to Railway](https://railway.app/button.svg)](https://railway.app/template/yt-bot-railway)

### Manual Deploy

1. **Fork this repository**
   ```bash
   https://github.com/T0h1/yt-bot-railway/fork
   ```

2. **Create Railway project**
   - Go to https://railway.app
   - Click "New Project" → "Deploy from GitHub repo"
   - Select your fork

3. **Set Environment Variables**

| Variable | Required | Description |
|----------|----------|-------------|
| `BOT_TOKEN` | ✅ | Telegram Bot Token (from @BotFather) |
| `ADMIN_ID` | ✅ | Your Telegram User ID |
| `GENIUS_API_TOKEN` | ❌ | Genius API token (for lyrics) |

4. **Add PostgreSQL (Optional)**
   - Railway Dashboard → New → Database → PostgreSQL
   - It auto-connects via `DATABASE_URL`

5. **Deploy**
   - Railway auto-deploys on push
   - Check logs for "Application started"

## 🔧 Environment Variables

### Required

| Variable | Description | Example |
|----------|-------------|---------|
| `BOT_TOKEN` | Telegram Bot Token | `123456:ABC-DEF...` |
| `ADMIN_ID` | Your Telegram User ID | `1902235346` |

### Optional

| Variable | Description | Default |
|----------|-------------|---------|
| `GENIUS_API_TOKEN` | Genius API token for lyrics | Empty (no lyrics) |
| `RATE_LIMIT_PER Minute` | Downloads per minute | `5` |
| `RATE_LIMIT_PER_HOUR` | Downloads per hour | `20` |
| `ADMIN_API_KEY` | API key for web dashboard | Auto-generated |

### Auto-Provided by Railway

| Variable | Description |
|----------|-------------|
| `PORT` | Web server port |
| `DATABASE_URL` | PostgreSQL connection string |
| `REDIS_URL` | Redis connection string |
| `RAILWAY_PUBLIC_DOMAIN` | Public domain |

## 📋 Bot Commands

| Command | Description | Access |
|---------|-------------|--------|
| `/start` | Welcome message | All |
| `/help` | Show help | All |
| `/history` | Download history | All |
| `/favorites` | Liked songs | All |
| `/albums` | Album collection | All |
| `/search <name>` | Search artist | All |
| `/admin` | Admin dashboard | Admin |
| `/adduser <id>` | Add allowed user | Admin |
| `/removeuser <id>` | Remove user | Admin |
| `/listusers` | List allowed users | Admin |

## 📁 Project Structure

```
yt-bot-railway/
├── bot.py                     # Entry point
├── youtube_downloader_bot.py  # Main bot logic (2000+ lines)
├── config.py                  # Settings & env vars
├── database.py                # PostgreSQL + SQLite
├── admin_dashboard.py         # Admin inline keyboard
├── rate_limiter.py            # 3-tier rate limiting
├── yt_dlp_async.py            # Async yt-dlp wrapper
├── health_check.py            # /health endpoint
├── requirements.txt           # Dependencies
├── railway.json               # Railway config
├── Dockerfile                 # Docker build
└── nixpacks.toml              # Nixpacks config
```

## 🛠️ Local Development

```bash
git clone https://github.com/T0h1/yt-bot-railway.git
cd yt-bot-railway
pip install -r requirements.txt

# Create .env or export vars
export BOT_TOKEN="your_token"
export ADMIN_ID="your_id"

python bot.py
```

## 📊 Lyrics Sources (Priority Order)

1. **lyrics.ovh** - Free API, most reliable
2. **lyrics.fandom.com** - MediaWiki API
3. **Genius API** (lyricsgenius) - Best for Persian/Iranian music
4. **textylate.com** - Simple lyrics API

Genius API setup:
1. Go to https://genius.com/api-clients/new
2. Create app → Copy "Client Access Token"
3. Set as `GENIUS_API_TOKEN` in Railway

## ⚡ Performance

- **Download format**: opus > m4a > mp3 > best (highest quality first)
- **Auto-compress**: Files >50MB compressed to fit Telegram limit
- **Rate limit**: 2s delay between dl_all downloads (SoundCloud 403 protection)
- **Retry**: Auto-retry on 403 errors

## 📄 License

MIT License

## 🙏 Credits

- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [mutagen](https://github.com/quodlibet/mutagen)
- [lyricsgenius](https://github.com/johnwmillr/LyricsGenius)
