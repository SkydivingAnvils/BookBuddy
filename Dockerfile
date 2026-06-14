FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Generate PWA icon PNGs (solid green #1B5E3B) — no extra deps required
RUN python3 gen_icons.py

# Stamp a unique build version into the service worker so browsers always
# fetch fresh assets after a redeploy (avoids stale cache issues).
RUN BUILD_TS=$(date +%Y%m%d%H%M%S) && \
    sed -i "s/bookbuddy-v1/bookbuddy-${BUILD_TS}/" app/static/sw.js

RUN mkdir -p /data

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
