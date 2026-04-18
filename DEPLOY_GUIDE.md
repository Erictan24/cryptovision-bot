# Deploy Guide — Bot 2 Scalping Production

Step-by-step panduan deploy Bot 2 ke VPS untuk live trading.

## Prasyarat

- VPS (Contabo/DigitalOcean/Vultr) — minimum 2GB RAM, Singapore region
- Bitunix account dengan $100+ capital
- Bitunix API key (trade permission only, bukan withdraw)
- Telegram bot token (buat via @BotFather)

## Step 1: VPS Setup

### 1.1 Rent VPS
**Rekomendasi**:
- **Contabo VPS S** — ~$6/bulan, Singapore
- **DigitalOcean** — $6/bulan droplet, Singapore
- **Vultr** — $6/bulan, Singapore

### 1.2 Initial Setup
```bash
# SSH ke VPS
ssh root@your-vps-ip

# Update system
apt update && apt upgrade -y

# Install Python 3.11 + pip + git
apt install python3.11 python3-pip python3-venv git tmux -y

# Install system deps untuk pandas/numpy
apt install build-essential libatlas-base-dev -y

# Timezone ke UTC (penting untuk trading)
timedatectl set-timezone UTC
```

## Step 2: Deploy Code

### 2.1 Clone atau upload code
```bash
# Option A: Git clone (kalau sudah push)
cd /root
git clone your-repo-url crypto_bot
cd crypto_bot

# Option B: SCP upload (dari laptop)
scp -r /c/Users/erict/OneDrive/crypto_bot_scalp root@vps:/root/crypto_bot
```

### 2.2 Virtual env + dependencies
```bash
cd /root/crypto_bot
python3.11 -m venv venv
source venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt

# Kalau tidak ada requirements.txt, install manual:
pip install pandas numpy requests python-telegram-bot
pip install matplotlib scipy python-dotenv
pip install fastapi uvicorn sqlalchemy
```

### 2.3 Environment variables
```bash
cat > .env << EOF
TELEGRAM_BOT_TOKEN=your_telegram_token_here
BITUNIX_API_KEY=your_bitunix_api_key
BITUNIX_API_SECRET=your_bitunix_secret
CRYPTOCOMPARE_API_KEY=your_key_here
EOF

chmod 600 .env  # secure permissions
```

## Step 3: Test Run

### 3.1 Test bot connectivity
```bash
source venv/bin/activate
python -c "
from trading_engine import TradingEngine
e = TradingEngine()
price = e.get_price('BTC')
print(f'BTC price: {price}')
"
```

### 3.2 Test bot 2 imports
```bash
python -c "
from scalping_signal_engine import generate_scalping_signal
from trading_engine_scalp import ScalpingEngine
from scalp_trade_journal import count_trades
print(f'Total trades in journal: {count_trades()}')
print('All imports OK')
"
```

## Step 4: Run Bot 2 with tmux (persistent session)

### 4.1 Start bot 2 in tmux
```bash
# Create tmux session
tmux new -s bot2

# Inside tmux
cd /root/crypto_bot
source venv/bin/activate
python main_scalp.py

# Detach: Ctrl+B then D
```

### 4.2 View logs
```bash
# Attach back to tmux
tmux attach -t bot2

# Or check log file
tail -f /root/crypto_bot/bot_scalp.log
```

## Step 5: Run Dashboard API (optional)

### 5.1 Start dashboard
```bash
tmux new -s dashboard
cd /root/crypto_bot
source venv/bin/activate
python dashboard_api.py

# Dashboard accessible at http://your-vps-ip:8080
# Detach: Ctrl+B then D
```

### 5.2 Firewall setup
```bash
ufw allow 8080/tcp  # dashboard
ufw allow 22/tcp    # ssh
ufw enable
```

### 5.3 Nginx reverse proxy (optional — untuk domain)
```bash
apt install nginx certbot python3-certbot-nginx -y

cat > /etc/nginx/sites-available/dashboard << 'EOF'
server {
    listen 80;
    server_name stats.yourdomain.com;

    location / {
        proxy_pass http://localhost:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
EOF

ln -s /etc/nginx/sites-available/dashboard /etc/nginx/sites-enabled/
nginx -t
systemctl reload nginx

# SSL
certbot --nginx -d stats.yourdomain.com
```

## Step 6: Auto-start on reboot

### 6.1 Systemd service untuk bot 2
```bash
cat > /etc/systemd/system/bot2-scalp.service << 'EOF'
[Unit]
Description=Bot 2 Scalping
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/crypto_bot
Environment="PATH=/root/crypto_bot/venv/bin"
ExecStart=/root/crypto_bot/venv/bin/python main_scalp.py
Restart=always
RestartSec=30
StandardOutput=append:/root/crypto_bot/bot_scalp.log
StandardError=append:/root/crypto_bot/bot_scalp.log

[Install]
WantedBy=multi-user.target
EOF

systemctl enable bot2-scalp
systemctl start bot2-scalp
systemctl status bot2-scalp
```

### 6.2 Systemd untuk dashboard
```bash
cat > /etc/systemd/system/bot2-dashboard.service << 'EOF'
[Unit]
Description=Bot 2 Dashboard API
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/crypto_bot
Environment="PATH=/root/crypto_bot/venv/bin"
ExecStart=/root/crypto_bot/venv/bin/python dashboard_api.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl enable bot2-dashboard
systemctl start bot2-dashboard
```

## Step 7: Monitoring

### 7.1 Log rotation
```bash
cat > /etc/logrotate.d/bot2 << 'EOF'
/root/crypto_bot/bot_scalp.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    create 0644 root root
}
EOF
```

### 7.2 Manual health check
```bash
# Cek status
systemctl status bot2-scalp

# Cek log error
grep -i error /root/crypto_bot/bot_scalp.log | tail -20

# Cek trade count hari ini
python -c "
import scalp_trade_journal as j
print(j.get_recent_summary(days=1))
"
```

## Step 8: Emergency Controls

### Kalau bot bermasalah:

**Stop bot:**
```bash
systemctl stop bot2-scalp
```

**Check if running:**
```bash
systemctl status bot2-scalp
```

**Restart fresh:**
```bash
systemctl restart bot2-scalp
```

**Kill all open positions manually:**
- Login ke Bitunix
- Close all positions manually
- Pause bot dulu sampai investigasi selesai

**Backup trade journal sebelum kirim ke claude/analysis:**
```bash
cp /root/crypto_bot/data/scalp_trades.db /root/backups/trades_$(date +%Y%m%d).db
```

## Step 9: Weekly Maintenance

Setiap Senin:
```bash
# Backup journal
cp /root/crypto_bot/data/scalp_trades.db /root/backups/trades_$(date +%Y%m%d).db

# Refresh learning (otomatis jalan Senin 02:00, tapi bisa manual)
cd /root/crypto_bot
source venv/bin/activate
python -c "
import scalp_coin_learning as cl
learning = cl.get_learning()
learning.refresh()
learning.print_summary()
"

# Check disk usage
df -h
du -sh /root/crypto_bot
```

## Troubleshooting

### Bot crash berulang
- Check log: `tail -100 bot_scalp.log`
- Common: API rate limit, internet drop, Bitunix API issue
- Systemd akan auto-restart, tapi investigate root cause

### Tidak ada signal
- Check BTC condition (bot skip kalau sideways)
- Check learning blocks: `python -c "import scalp_coin_learning as cl; cl.get_learning().print_summary()"`
- Check session filter: mungkin sedang DEAD session

### Daily loss hit terus
- Investigate setelah reset harian
- Mungkin perlu raise score threshold
- Mungkin market regime berubah

### Trade execution error
- Check Bitunix API key permission
- Check margin available
- Check Bitunix server status

## Cost Summary

| Item | Monthly Cost |
|------|:-:|
| VPS (Contabo) | ~$6 (Rp 90k) |
| Domain (optional) | $1 (Rp 15k) |
| Total | **~$7 (Rp 105k)** |

## Next Steps After Deploy

1. ✅ Bot running 24/7 di VPS
2. ✅ Dashboard public accessible
3. 📊 Monitor 2 minggu pertama intensively
4. 📈 Share stats di channel kamu
5. 🎯 Collect feedback dari waitlist
6. 💰 Launch paid tier kalau profitable

**Target bulan 1**: Break-even on $100 capital, prove WR > 45% live.
