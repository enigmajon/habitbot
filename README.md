# Telegram Habit & Time Tracker Bot

Production-quality Telegram bot (`Python 3.11+`, `aiogram 3.x`, `SQLAlchemy 2.x`, `Alembic`, `Matplotlib`) for tracking habits, limits, Pomodoro sessions, daily goals, streaks, and calendar plans in **Uzbek (Latin)** and **English**.

## 1. Free Cloud Deployment (GitHub + Koyeb + Neon.tech)
1. Upload all files in this repository to your GitHub repo (`https://github.com/enigmajon/habitbot`).
2. Create a free PostgreSQL database at [neon.tech](https://neon.tech) and copy your connection string (`postgresql://...`).
3. Create a free account at [koyeb.com](https://www.koyeb.com/), click **Create Web Service** -> **GitHub**, select `enigmajon/habitbot`, choose **Dockerfile** builder, and choose the **Free** instance.
4. Under **Environment Variables**, set:
   - `BOT_TOKEN`: your token from `@BotFather`
   - `DATABASE_URL`: your Neon connection string
   - `DEFAULT_TZ`: `Asia/Tashkent`
5. Click **Deploy** — the bot automatically creates all database tables on startup and begins polling!

## 2. Running Locally (SQLite or Docker)
### Local Python (SQLite out-of-the-box)
```bash
pip install -r requirements.txt
cp .env.example .env
python -m bot.main
```

### Docker Compose (Bot + PostgreSQL 16)
```bash
docker compose up --build -d
```

## 3. Running Tests
```bash
pytest -v
```

## 4. Daily Automatic Database Backups
- **PostgreSQL (Docker / Linux cron)**:
  Add to `crontab -e`:
  ```cron
  0 3 * * * docker exec habitbot-postgres-1 pg_dump -U habitbot habitbot | gzip > /backups/habitbot_$(date +\%F).sql.gz
  ```
- **Neon.tech Free Tier**: Includes automatic point-in-time backup history out of the box.
