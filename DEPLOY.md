# 🚀 Railway Deploy Guide

## پیش‌نیازها

- اکانت [GitHub](https://github.com)
- اکانت [Railway](https://railway.app) (ماهانه $5 رایگان)
- توکن ربات تلگرام از [@BotFather](https://t.me/BotFather)
- آیدی عددی تلگرام از [@userinfobot](https://t.me/userinfobot)

---

## قدم ۱: Fork کردن

1. برو به https://github.com/T0h1/yt-bot-railway
2. روی **"Fork"** کلیک کن (بالا سمت راست)
3. صبر کن تا fork کامل بشه

---

## قدم ۲: ساخت پروژه Railway

1. برو به https://railway.app
2. روی **"New Project"** کلیک کن
3. **"Deploy from GitHub repo"** رو انتخاب کن
4. **fork خودت** رو انتخاب کن
5. Railway خودش build می‌کنه

---

## قدم ۳: تنظیم متغیرها

1. توی dashboard روی **bot service** کلیک کن
2. بریم به تب **"Variables"**
3. **"New Variable"** رو بزن و اضافه کن:

```
BOT_TOKEN = توکن رباتت
ADMIN_ID = آیدی عددیت
```

### اختیاری

```
GENIUS_API_TOKEN = توکن Genius (برای lyrics)
```

> 💡 توکن Genius از https://genius.com/api-clients/new بگیر

---

## قدم ۴: PostgreSQL (اختیاری)

1. توی Railway dashboard روی **"+ New"** کلیک کن
2. **"Database"** → **"PostgreSQL"** رو انتخاب کن
3. Railway خودکار connect می‌کنه
4. ربات `DATABASE_URL` رو خودکار تشخیص میده

---

## قدم ۵: دیپلوی

1. Railway خودکار deploy می‌کنه
2. توی تب **"Deployments"** وضعیت رو ببین
3. توی تب **"Logs"** بگرد دنبال **"Application started"**

---

## قدم ۶: تست

1. تلگرام رو باز کن
2. رباتت رو پیدا کن
3. `/start` بفرست
4. یه لینک YouTube یا SoundCloud بفرست

---

## 🔴 عیب‌یابی

### ربات جواب نمیده
- لاگ‌ها رو چک کن
- `BOT_TOKEN` درست باشه
- فقط **1 replica** داشته باش (Settings → Deployment)

### Lyrics پیدا نمیشه
- `GENIUS_API_TOKEN` رو تنظیم کن
- از https://genius.com/api-clients/new بگیر

### SoundCloud 403
- کوکی آپلود کن: `/cookie` بفرست
- یا cookies.txt اضافه کن

### فایل خیلی بزرگه
- ربات **خودکار فشرده می‌کنه** — نیازی به کار نیست

### Conflict error هنگام deploy
- **عادیه** — بعد از چند ثانیه حل میشه
- Instance قدیمی متوقف میشه، جدید تنها میمونه

---

## ⚡ Railway Features

### Health Check
```
GET /health → 200 OK (برای Railway monitoring)
```

### Graceful Degradation
```
بدون PostgreSQL: تاریخچه و پلی‌لیست غیرفعال
بدون Redis: Queue غیرفعال
بدون Genius Token: Lyrics غیرفعال
```

### Auto Restart
```
restartPolicyType: ON_FAILURE
restartPolicyMaxRetries: 10
```

---

## 📊 Railway Pricing

| Feature | Free Tier |
|---------|-----------|
| Monthly credit | $5 |
| Hobby plan | $5/mo (بیشتر از credit) |
| PostgreSQL | $1/mo |
| Redis | $1/mo |
| Bot (Hobby) | $5/mo |

> 💡 با credit رایگان $5، ربات + PostgreSQL رایگان اجرا میشه!
# Deploy trigger Sat Aug  8 03:47:25 UTC 2026
