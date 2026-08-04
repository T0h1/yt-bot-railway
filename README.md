# 🎵 YouTube/SoundCloud Media Bot

A Telegram bot for downloading music and videos from YouTube, SoundCloud, Instagram, TikTok, Twitter, Facebook, and Twitch.

## ✨ Features

- 🎵 **Audio Download** - High quality MP3 320kbps with metadata
- 🎬 **Video Download** - Quality selection (1080p/720p/480p/360p)
- 🖼️ **Album Art** - Automatic cover art embedding
- 📝 **Lyrics** - Auto-fetch and embed lyrics in audio files
- 🎭 **Multi-platform** - YouTube, SoundCloud, Instagram, TikTok, Twitter, Facebook, Twitch
- 📊 **Progress Bar** - Real-time download progress
- ❤️ **Like/Dislike** - Rate songs
- 📜 **History** - Download history
- 🔍 **Search** - Search for artists on SoundCloud
- 🧹 **Auto-cleanup** - Files deleted after upload

## 🚀 Deploy to Railway

### Prerequisites
- GitHub account
- Railway account (https://railway.app)
- Telegram Bot Token (from @BotFather)

### Steps

1. **Fork or clone this repository**

2. **Create a new Railway project**
   - Go to https://railway.app
   - Click "New Project"
   - Select "Deploy from GitHub repo"
   - Select this repository

3. **Set Environment Variables**
   - In Railway dashboard, go to "Variables" tab
   - Add:
     ```
     BOT_TOKEN=your_telegram_bot_token_here
     ADMIN_ID=your_telegram_user_id_here
     ```

4. **Deploy**
   - Railway will automatically deploy
   - Check logs for "Application started"

## 🔧 Local Development

1. **Clone the repository**
   ```bash
   git clone https://github.com/yourusername/yt-bot-railway.git
   cd yt-bot-railway
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Create .env_ytdl file**
   ```
   TELEGRAM_BOT_TOKEN_YTDL=your_token_here
   ADMIN_ID_YTDL=your_user_id_here
   ```

4. **Run the bot**
   ```bash
   python bot.py
   ```

## 📝 Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `BOT_TOKEN` | Telegram Bot Token | ✅ Yes |
| `ADMIN_ID` | Your Telegram User ID | ✅ Yes |
| `TELEGRAM_BOT_TOKEN_YTDL` | Alternative token variable | ❌ No |
| `ADMIN_ID_YTDL` | Alternative admin ID variable | ❌ No |

## 📁 Project Structure

```
yt-bot-railway/
├── bot.py                    # Entry point
├── youtube_downloader_bot.py # Main bot code
├── requirements.txt          # Python dependencies
├── Procfile                  # Railway process type
└── README.md                 # This file
```

## 🎯 Bot Commands

- `/start` - Show welcome message
- `/help` - Show help
- `/history` - Show download history
- `/favorites` - Show liked songs
- `/albums` - Show album collection
- `/stats` - Show system stats (admin only)
- `/logs` - Show recent logs (admin only)
- `/search <name>` - Search for artist

## 📝 Notes

- Files are automatically deleted after upload to Telegram
- Maximum file size: 50MB (Telegram limit)
- Rate limiting: 5 downloads per minute per user
- Cleanup runs every hour

## 📄 License

MIT License

## 🙏 Credits

- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [mutagen](https://github.com/quodlibet/mutagen)
