# Maneshi AI — Railway Edition

این نسخه همان پنل کامل Self-Hosted است و هیچ بخش پنل حذف نشده است. فقط لایه Deploy برای Railway ساده شده.

## ورود آزمایشی
- License: `MANSHI-DEMO-2026`
- Username: مقدار `ADMIN_USER` در Railway (پیشنهاد: `admin`)
- Password: مقدار `ADMIN_PASSWORD` در Railway

## Deploy
1. پوشه را در یک GitHub Repository قرار بده.
2. Railway > New Project > Deploy from GitHub Repo.
3. Repo را انتخاب کن. Railway فایل `Dockerfile` ریشه را خودکار تشخیص می‌دهد.
4. در Service > Variables حداقل این‌ها را قرار بده:

```
APP_NAME=Maneshi AI
TZ=Asia/Tehran
ADMIN_USER=admin
ADMIN_PASSWORD=یک-رمز-قوی-برای-خودت
DEFAULT_LICENSE_KEY=MANSHI-DEMO-2026
GEMINI_TEXT_MODEL=gemini-2.5-flash
GEMINI_LIVE_MODEL=gemini-3.1-flash-live-preview
GEMINI_VOICE=Kore
DATA_DIR=/data
DATABASE_PATH=/data/maneshi.db
```

5. یک Volume به همین Service وصل کن و Mount Path را `/data` بگذار. بدون Volume پنل بالا می‌آید اما داده‌ها بعد از Redeploy پایدار نیستند.
6. Service > Settings > Networking > Generate Domain.
7. Service > Settings > Deploy > Healthcheck Path را `/health` بگذار.
8. Domain را باز کن، License را بزن و وارد پنل شو.
9. داخل پنل > اتصال سرویس‌ها، Gemini API Key خودت را وارد کن.

## تماس تلفنی
Railway WebSocket را پشتیبانی می‌کند. برای تماس واقعی، Twilio/VoIP را از داخل پنل تنظیم کن و Voice Webhook نمایش‌داده‌شده در بخش اتصال تلفن را در Provider قرار بده. برای شماره موبایل شخصی، Call Forwarding در حالت No Answer باید توسط اپراتور به شماره VoIP/Twilio انجام شود.

## نکات
- برنامه روی `0.0.0.0:$PORT` گوش می‌دهد تا با Railway سازگار باشد.
- HTTPS و دامنه را Railway می‌دهد؛ Caddy در این نسخه لازم نیست.
- دیتابیس SQLite و Upload/Backup همگی در `/data` هستند.
- Secretهای داخلی اگر در Variables تعریف نشوند، اولین بار داخل Volume تولید می‌شوند.
- برای محصول واقعی، لایسنس دمو را حذف و `LICENSE_SERVER_URL` را به سرویس لایسنس خودت وصل کن.
