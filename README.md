# 🎵 YT-Bot Railway

ربات تلگرام دانلود موسیقی و ویدیو از YouTube, SoundCloud, Instagram, TikTok, Twitter و بیشتر.

[![Deploy to Railway](https://railway.app/button.svg)](https://railway.app/template/yt-bot-railway?env=BOT_TOKEN=&ADMIN_ID=)

---

## ✨ قابلیت‌ها

### 🎵 دانلود موسیقی
- **بالاترین کیفیت** — opus > m4a > MP3 320kbps
- **خودکار فشرده‌سازی** — فایل‌های بزرگ‌تر از 50MB
- **-cover art** — کاور آلبوم خودکار
- **متن آهنگ** — Genius API (lyricsgenius)
- **اطلاعات ترک** — آلبوم، آرتیست، ژانر، شماره ترک

### 🎬 دانلود ویدیو
- **کیفیت قابل انتخاب** — 1080p/720p/480p/360p
- **ویدیوی بزرگ** — chunked upload برای فایل‌های >50MB

### 🎭 پلتفرم‌ها
YouTube • SoundCloud • Instagram • TikTok • Twitter/X • Facebook • Twitch • Spotify • Podcast • M3U8/HLS

### 👑 ادمین
- **داشبورد ادمین** — inline keyboard
- **مدیریت کاربران** — add/remove/toggle
- **محدودیت دانلود** — 3 سطحی (دقیقه/ساعت/روز)
- **آمار و لاگ** — از داخل ربات

### 🎤 آرتیست پروفایل
- **لیست آهنگ‌ها** — با pagination
- **دانلود همه** — dl_all
- **بازه عددی** — dl_range (مثلاً 10-20)

---

## 🚀 Deploy روی Railway

### One-Click Deploy

[![Deploy to Railway](https://railway.app/button.svg)](https://railway.app/template/yt-bot-railway)

### قدم به قدم

> راهنمای کامل: [DEPLOY.md](DEPLOY.md)

### متغیرهای ضروری

| Variable | Description | از کجا بگیریم |
|----------|-------------|---------------|
| `BOT_TOKEN` | توکن ربات تلگرام | [@BotFather](https://t.me/BotFather) |
| `ADMIN_ID` | آیدی عددی شما | [@userinfobot](https://t.me/userinfobot) |

### متغیرهای اختیاری

| Variable | Default | Description |
|----------|---------|-------------|
| `GENIUS_API_TOKEN` | — | توکن Genius برای lyrics |
| `RATE_LIMIT_PER_MINUTE` | 5 | حداکثر دانلود در دقیقه |
| `RATE_LIMIT_PER_HOUR` | 20 | حداکثر دانلود در ساعت |

### سرویس‌های Railway (اختیاری)

| Plugin | فایده |
|--------|-------|
| **PostgreSQL** | دیتابیس پایدار، تاریخچه، پلی‌لیست |
| **Redis** | Queue سریع‌تر، cache |

> هر دو **اختیاری** هستن — ربات بدون اونا هم کار می‌کنه (graceful degradation).

---

## 📋 دستورات ربات

### عمومی
| دستور | توضیح |
|-------|-------|
| `/start` | پیام خوش‌آمد |
| `/help` | راهنما |
| `/history` | تاریخچه دانلود |
| `/favorites` | آهنگ‌های مورد علاقه |
| `/albums` | مجموعه آلبوم‌ها |
| `/search <name>` | جستجوی آرتیست |
| `/cookie` | آپلود کوکی |

### ادمین
| دستور | توضیح |
|-------|-------|
| `/admin` | داشبورد ادمین |
| `/adduser <id>` | اضافه کردن کاربر |
| `/removeuser <id>` | حذف کاربر |
| `/listusers` | لیست کاربران |
| `/toggleuser <id>` | فعال/غیرفعال |
| `/userinfo <id>` | اطلاعات کاربر |

---

## 🏗️ ساختار پروژه

```
yt-bot-railway/
├── bot.py                     # Entry point
├── youtube_downloader_bot.py  # منطق اصلی ربات (~2000 خط)
├── config.py                  # تنظیمات Pydantic
├── database.py                # PostgreSQL + SQLite
├── admin_dashboard.py         # داشبورد ادمین inline keyboard
├── rate_limiter.py            # محدودیت 3 سطحی
├── yt_dlp_async.py            # Async yt-dlp wrapper
├── cookie_manager.py          # مدیریت کوکی
├── health_check.py            # /health endpoint
├── web_dashboard.py           # داشبورد وب FastAPI
├── audio_processing.py        # نرمال‌سازی صدا + حذف سکوت
├── batch_download.py          # دانلود دسته‌ای ZIP
├── chunked_upload.py          # آپلود ویدیوی بزرگ
├── download_queue.py          # صف دانلود با Redis
├── lyrics_lrc.py              # متن هماهنگ (LRC)
├── m3u8_support.py            # پشتیبانی HLS/Live
├── metrics.py                 # Prometheus metrics
├── playlists.py               # پلی‌لیست شخصی
├── podcast_support.py         # پشتیبانی Podcast
├── spotify_resolver.py        # تبدیل Spotify URL
├── video_thumbnail.py         # thumbnail ویدیو
├── requirements.txt           # وابستگی‌ها
├── railway.json               # Railway config
├── Dockerfile                 # Docker build
├── nixpacks.toml              # Nixpacks config
├── DEPLOY.md                  # راهنمای deploy
└── README.md                  # این فایل
```

---

## ⚡ عملکرد

| Feature | Detail |
|---------|--------|
| Format | `bestaudio[ext=opus]/bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best` |
| Compress | خودکار اگه >50MB → 128/96kbps |
| Delay | 2s بین دانلودها (جلوگیری از SoundCloud 403) |
| Retry | خودکار retry روی خطای 403 |
| Lyrics | lyrics.ovh → fandom → Genius API → textylate |
| Album | Genius API → yt-dlp → tags → خالی |

---

## 🔧 Local Development

```bash
git clone https://github.com/T0h1/yt-bot-railway.git
cd yt-bot-railway
pip install -r requirements.txt

export BOT_TOKEN="your_token"
export ADMIN_ID="your_id"

python bot.py
```

---

## 📄 License

MIT
