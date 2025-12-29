#!/bin/bash
set -e

# Renkler
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
NC='\033[0m'

clear
echo -e "${PURPLE}"
cat << "EOF"
╔══════════════════════════════════════════════════════════════╗
║    🚀 NovaCell-3 v4.5 - BİREYSEL KOTA DÖNGÜSÜ              ║
║  ✅ Kullanıcı: novacell / NovaCell25Hakki                    ║
║  🔄 Her kullanıcı kendi kota döngüsünde                      ║
║  ⏰ Kota reset'i son ödeme + 30 gün bazında                  ║
║  🎯 Süre dolunca otomatik pasif                              ║
╚══════════════════════════════════════════════════════════════╝
EOF
echo -e "${NC}"

# Root kontrolü
if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}❌ Root olarak çalıştırın: sudo su${NC}"
   exit 1
fi

echo -e "${GREEN}[1/10] TEMİZLİK...${NC}"
systemctl stop xui-admin-panel 2>/dev/null || true
systemctl disable xui-admin-panel 2>/dev/null || true
rm -rf /opt/xui-admin-panel/app.py /opt/xui-admin-panel/index.html
rm -f /etc/systemd/system/xui-admin-panel.service
rm -f /usr/local/bin/reset-quota.sh
rm -f /usr/local/bin/check-individual-quotas.sh
rm -f /root/novacell-nightly-backup.sh
crontab -l 2>/dev/null | grep -v -E "reset-quota|novacell-nightly-backup|check-individual-quotas" | crontab - 2>/dev/null || true
systemctl daemon-reload

echo -e "${GREEN}[2/10] PAKETLER...${NC}"
apt update -qq
apt install -y python3 python3-pip python3-venv sqlite3 curl bc >/dev/null 2>&1

INSTALL_DIR="/opt/xui-admin-panel"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

# Veritabanı kontrolü
if [ -f "$INSTALL_DIR/admin_panel.db" ]; then
    echo -e "${YELLOW}ℹ️  Mevcut veritabanı korunuyor${NC}"
    sqlite3 "$INSTALL_DIR/admin_panel.db" "PRAGMA table_info(user_settings);" | grep -q "folder" || {
        echo -e "${YELLOW}🔧 folder sütunu ekleniyor...${NC}"
        sqlite3 "$INSTALL_DIR/admin_panel.db" "ALTER TABLE user_settings ADD COLUMN folder TEXT DEFAULT 'Tümü';"
    }
    sqlite3 "$INSTALL_DIR/admin_panel.db" "PRAGMA table_info(user_settings);" | grep -q "quota_reset_date" || {
        echo -e "${YELLOW}🔧 quota_reset_date sütunu ekleniyor...${NC}"
        sqlite3 "$INSTALL_DIR/admin_panel.db" "ALTER TABLE user_settings ADD COLUMN quota_reset_date TEXT;"
    }
else
    echo -e "${YELLOW}🆕 Yeni kurulum${NC}"
fi

echo -e "${GREEN}[3/10] PYTHON ORTAMI...${NC}"
python3 -m venv venv
source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet flask flask-cors bcrypt

echo -e "${GREEN}[4/10] BACKEND DOSYASI İNDİRİLİYOR...${NC}"
# GitHub'dan backend dosyasını indir
curl -sL https://raw.githubusercontent.com/eren73546/novacell-admin-panel/main/app.py -o app.py || {
    echo -e "${RED}❌ Backend indirilemedi!${NC}"
    exit 1
}

echo -e "${GREEN}[5/10] FRONTEND DOSYASI İNDİRİLİYOR...${NC}"
# GitHub'dan frontend dosyasını indir
curl -sL https://raw.githubusercontent.com/eren73546/novacell-admin-panel/main/index.html -o index.html || {
    echo -e "${RED}❌ Frontend indirilemedi!${NC}"
    exit 1
}

echo -e "${GREEN}[6/10] BİREYSEL KOTA KONTROL SCRIPTI...${NC}"
cat > /usr/local/bin/check-individual-quotas.sh << 'CHECKSCRIPT'
#!/bin/bash
ADMIN_DB="/opt/xui-admin-panel/admin_panel.db"
XUI_DB="/etc/x-ui/x-ui.db"

if [ ! -f "$ADMIN_DB" ] || [ ! -f "$XUI_DB" ]; then
    exit 1
fi

TODAY=$(date +%Y-%m-%d)

sqlite3 "$ADMIN_DB" "SELECT email, quota_reset_date FROM user_settings WHERE quota_reset_date IS NOT NULL AND quota_reset_date != ''" | while IFS='|' read -r email reset_date; do
    if [ -z "$reset_date" ]; then
        continue
    fi
    
    reset_timestamp=$(date -d "$reset_date" +%s 2>/dev/null)
    if [ $? -ne 0 ]; then
        continue
    fi
    
    today_timestamp=$(date +%s)
    diff_days=$(( ($today_timestamp - $reset_timestamp) / 86400 ))
    
    if [ $diff_days -ge 30 ]; then
        echo "[$(date)] Kullanıcı $email kotası sıfırlanıyor (30 gün doldu)"
        
        current_usage=$(sqlite3 "$XUI_DB" "SELECT COALESCE(up,0) + COALESCE(down,0) FROM client_traffics WHERE email='$email'")
        if [ -n "$current_usage" ] && [ "$current_usage" -gt 0 ]; then
            current_usage_gb=$(echo "scale=2; $current_usage / 1073741824" | bc)
            sqlite3 "$ADMIN_DB" "UPDATE user_settings SET total_usage_ever = COALESCE(total_usage_ever, 0) + $current_usage_gb WHERE email='$email'"
        fi
        
        sqlite3 "$XUI_DB" "UPDATE client_traffics SET up=0, down=0 WHERE email='$email'"
        
        new_reset_date=$(date -d "$reset_date +30 days" +%Y-%m-%d)
        sqlite3 "$ADMIN_DB" "UPDATE user_settings SET quota_reset_date='$new_reset_date' WHERE email='$email'"
        
        sqlite3 "$ADMIN_DB" "INSERT INTO quota_reset_log (email, reset_date, reset_type) VALUES ('$email', datetime('now'), 'auto_individual')"
        
        echo "[$(date)] ✅ $email kotası sıfırlandı, yeni reset: $new_reset_date"
    fi
done

systemctl restart x-ui
CHECKSCRIPT
chmod +x /usr/local/bin/check-individual-quotas.sh

echo -e "${GREEN}[7/10] SERVİS DOSYASI...${NC}"
cat > /etc/systemd/system/xui-admin-panel.service << EOF
[Unit]
Description=NovaCell-3 v4.5 Admin Panel
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
Environment="PATH=$INSTALL_DIR/venv/bin"
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/app.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

echo -e "${GREEN}[8/10] TELEGRAM YEDEK SCRIPTI...${NC}"
cat > /root/novacell-nightly-backup.sh << 'BACKUPSCRIPT'
#!/bin/bash
BOT_TOKEN="8477336717:AAHTcUycB6ttRkAU9ecncsfFthqcWgm0mQY"
CHAT_ID="151288168"
DATE=$(date +%Y%m%d-%H%M)
BACKUP_DIR="/root/backups"

mkdir -p "$BACKUP_DIR"

cp /opt/xui-admin-panel/admin_panel.db "$BACKUP_DIR/admin_panel-$DATE.db"
[ -f /etc/x-ui/x-ui.db ] && cp /etc/x-ui/x-ui.db "$BACKUP_DIR/x-ui-$DATE.db"

cd "$BACKUP_DIR"
tar -czf "NovaCell-3-v4.5-$DATE.tar.gz" admin_panel-$DATE.db x-ui-$DATE.db 2>/dev/null || tar -czf "NovaCell-3-v4.5-$DATE.tar.gz" admin_panel-$DATE.db

curl -F document=@"NovaCell-3-v4.5-$DATE.tar.gz" \
     -F chat_id="$CHAT_ID" \
     -F caption="🌙 NovaCell-3 v4.5 Gece Yedeği — $DATE" \
     "https://api.telegram.org/bot$BOT_TOKEN/sendDocument" >/dev/null 2>&1

find "$BACKUP_DIR" -name "*.tar.gz" -mtime +14 -delete
find "$BACKUP_DIR" -name "*.db" -mtime +14 -delete
BACKUPSCRIPT
chmod +x /root/novacell-nightly-backup.sh

echo -e "${GREEN}[9/10] CRON AYARLARI...${NC}"
(crontab -l 2>/dev/null | grep -v -E "reset-quota|novacell-nightly-backup|check-individual-quotas"; 
echo "0 */6 * * * /usr/local/bin/check-individual-quotas.sh >> /var/log/individual-quota.log 2>&1"; 
echo "30 4 * * * /root/novacell-nightly-backup.sh") | crontab -

echo -e "${GREEN}[10/10] BAŞLATILIYOR...${NC}"
cd "$INSTALL_DIR"
source venv/bin/activate
python3 -c "from app import init_db; init_db()"

systemctl daemon-reload
systemctl enable xui-admin-panel
systemctl start xui-admin-panel
sleep 3

if systemctl is-active --quiet xui-admin-panel; then
    clear
    echo -e "${GREEN}"
    cat << "EOF"
╔══════════════════════════════════════════════════════════╗
║   ✅ NovaCell-3 v4.5 KURULDU!                           ║
║   🔐 Admin: novacell / NovaCell25Hakki                  ║
║   🔄 Bireysel kota döngüsü aktif                         ║
║   ⏰ Her kullanıcı kendi 30 günlük döngüsünde            ║
║   🎯 Süre dolunca otomatik pasif                         ║
╚══════════════════════════════════════════════════════════╝
EOF
    echo -e "${NC}"
    echo ""
    SERVER_IP=$(hostname -I | awk '{print $1}')
    echo -e "${BLUE}🌐 Panel: http://${SERVER_IP}:8888${NC}"
    echo -e "${BLUE}👤 Kullanıcı: novacell${NC}"
    echo -e "${BLUE}🔑 Şifre: NovaCell25Hakki${NC}"
    echo ""
    echo -e "${YELLOW}═══════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}✅ BİREYSEL KOTA DÖNGÜSÜ:${NC}"
    echo -e "   • Her kullanıcının kendi 30 günlük kota döngüsü var"
    echo -e "   • Ödeme aldığında kota sıfırlanır ve yeni döngü başlar"
    echo -e "   • Artık ay başı toplu sıfırlama YOK!"
    echo ""
    echo -e "${GREEN}✅ SÜRE KONTROLÜ:${NC}"
    echo -e "   • Süresi dolan kullanıcılar otomatik pasif"
    echo -e "   • 3x-ui'de 'Bitti' gözükse bile data kesilir"
    echo ""
    echo -e "${GREEN}✅ OTOMATİK:${NC}"
    echo -e "   • Her 6 saatte kota kontrolü"
    echo -e "   • Her gece 04:30'da Telegram yedek"
    echo ""
    echo -e "${YELLOW}═══════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}🔧 Komutlar:${NC}"
    echo -e "   Servis: ${BLUE}systemctl status xui-admin-panel${NC}"
    echo -e "   Log: ${BLUE}journalctl -u xui-admin-panel -f${NC}"
    echo -e "   Kota Log: ${BLUE}tail -f /var/log/individual-quota.log${NC}"
    echo -e "   Manuel Kontrol: ${BLUE}bash /usr/local/bin/check-individual-quotas.sh${NC}"
    echo ""
    echo -e "${GREEN}✅ Tarayıcıda CTRL+F5 ile yenileyin!${NC}"
else
    echo -e "${RED}❌ BAŞLATMA HATASI!${NC}"
    echo ""
    echo -e "${YELLOW}Log:${NC}"
    journalctl -u xui-admin-panel -n 30 --no-pager
fi
