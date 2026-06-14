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
#    (Optional: add a Google Books API key for higher rate limits)
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

Back this file up regularly to keep your reading history. The `./data/` directory is a Docker volume that persists across container restarts and updates.

```bash
# Simple backup example
cp ./data/bookbuddy.db ./data/bookbuddy.db.backup-$(date +%Y%m%d)
```
