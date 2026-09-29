# BookBuddy

BookBuddy is a self-hosted family reading tracker: photograph a children's book cover and it automatically identifies the book, fetches metadata, and lets you log ratings per child. It runs as a single Docker container on your home server.

---

## Prerequisites

You need an Anthropic API key for book cover identification and recommendations.

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Sign in or create an account
3. Navigate to **API Keys** and create a new key
4. Copy it — you'll paste it into `.env` below

---

## Quick Start

```bash
# 1. Clone or download the project
git clone <your-repo-url> bookbuddy
cd bookbuddy

# 2. Create your environment file
cp .env.example .env

# 3. Edit .env and paste your Anthropic API key
#    (Optional: add a Google Books API key for higher rate limits,
#     and a Hardcover API key to enable Hardcover as a metadata source)
nano .env

# 4. Start the container
docker-compose up -d
```

---

## Accessing the App

Open your browser to:

```
http://[your-server-ip]:7842
```

If running on the same machine: `http://localhost:7842`

---

## Adding to Unraid (Community Applications)

1. In Unraid, go to **Docker → Add Container**
2. Fill in:
   - **Name:** `bookbuddy`
   - **Repository:** `bookbuddy` (after building locally, or push to a registry)
   - **Port Mapping:** Host `7842` → Container `8000`
   - **Volume Mapping:** Host `/mnt/user/appdata/bookbuddy` → Container `/data`
   - **Variable:** `ANTHROPIC_API_KEY` → your key
   - **Variable (optional):** `GOOGLE_BOOKS_API_KEY` → your key
   - **Variable (optional):** `HARDCOVER_API_KEY` → your key
   - **Variable:** `DATABASE_URL` → `sqlite:////data/bookbuddy.db`
3. Click **Apply**

---

## Updating

```bash
# Pull latest code, rebuild, and restart
git pull
docker-compose up -d --build
```

---

## Your Data

All book and rating data is stored in:

```
./data/bookbuddy.db
```

The `./data/` directory is a Docker volume that persists across container restarts and updates.

### Backups

BookBuddy backs up the database automatically to `./data/backups/` — once at startup if no recent backup exists, then every 24 hours — and keeps the latest 14. You can also back up on demand and download any backup from **Settings → Backups**.

| Variable | Default | Meaning |
|---|---|---|
| `BACKUP_INTERVAL_HOURS` | `24` | Hours between backups; `0` turns automatic backups off |
| `BACKUP_KEEP` | `14` | How many backups to keep |
| `BACKUP_DIR` | `<database folder>/backups` | Where backups are written |

Backups live on the same disk as the database, so for real safety copy `./data/backups/` somewhere else too (e.g. an Unraid backup share or cloud sync).

To restore, stop the container, replace `./data/bookbuddy.db` with a backup file (and delete any `bookbuddy.db-wal` / `bookbuddy.db-shm` next to it), then start the container again.

---

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest tests
```

Tests use a temporary SQLite database and make no network calls. They also run automatically on GitHub for every push to `main` and every pull request.
