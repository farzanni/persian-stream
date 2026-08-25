# فی‌استریم (Fistream) — Deploy Guide

Persian movie/series streaming site: Cinemeta catalog + multi-provider
players + Persian subtitles (subf2m scrape → AvalAI AI-translate fallback).

Local dev already runs as a systemd user service on this laptop:
`systemctl --user status fistream` → http://localhost:8000

## Deploy to a VPS (Parspack or any Debian/Ubuntu)

### 1. Copy the code
```bash
ssh root@YOUR_VPS 'mkdir -p /opt/fistream'
scp -r site/ root@YOUR_VPS:/opt/fistream/
```

### 2. One-time setup on the VPS
```bash
ssh root@YOUR_VPS
cd /opt/fistream/site
python3 -m venv /opt/fistream/.venv
/opt/fistream/.venv/bin/pip install fastapi "uvicorn[standard]" jinja2 httpx pysubs2

# systemd service
cat > /etc/systemd/system/fistream.service <<'EOF'
[Unit]
Description=Fistream
After=network-online.target

[Service]
WorkingDirectory=/opt/fistream/site
ExecStart=/opt/fistream/.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8100
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable --now fistream
```

### 3. nginx reverse proxy
```nginx
server {
    server_name YOUR_DOMAIN;
    location / {
        proxy_pass http://127.0.0.1:8100;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```
Then `certbot --nginx -d YOUR_DOMAIN` for HTTPS.

### 4. AI translation key on the VPS
The app reads the AvalAI key from `~/.hermes/.env`. On the VPS either:
- create `/root/.hermes/.env` with `HERMES_CUSTOM_API_AVALAI_IR_API_KEY=...`, or
- edit `app.py:_load_key()` to read from `/opt/fistream/.env`

## Notes
- Subtitle cache lives in `/tmp/fistream-subs` — consider a cron to clean
  files older than 30 days.
- All traffic flows viewer ↔ provider CDNs directly; your VPS only serves
  pages, catalogs and small VTT files. Bandwidth stays tiny.
- Cost per AI-translated movie ≈ 2,400 IRT (~$0.012).
