#!/bin/bash
set -e

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
║   🚀 NovaCell-3 v4.7 - KURULUM (FİNAL)                       ║
║   ✅ Kullanıcı: novacell / NovaCell25Hakki                   ║
║   🔄 Ödeme alınca kota sıfırlanmaz                           ║
║   ⏰ Otomatik kota yenileme (cron job)                       ║
║   📅 quota_start_date sistemi (sabit gün)                    ║
║   📱 Telegram günlük yedekleme (opsiyonel)                   ║
║   🎨 Özelleştirilebilir panel ismi (ZORLAMALI MOD)           ║
╚══════════════════════════════════════════════════════════════╝
EOF
echo -e "${NC}"

if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}❌ Root olarak çalıştırın: sudo su${NC}"
   exit 1
fi

echo -e "${GREEN}[1/15] TEMİZLİK...${NC}"
systemctl stop xui-admin-panel 2>/dev/null || true
systemctl disable xui-admin-panel 2>/dev/null || true
rm -rf /opt/xui-admin-panel/app.py /opt/xui-admin-panel/index.html
rm -f /etc/systemd/system/xui-admin-panel.service
rm -f /usr/local/bin/reset-quota.sh
rm -f /usr/local/bin/check-individual-quotas.sh
rm -f /root/novacell-telegram-backup.sh
crontab -l 2>/dev/null | grep -v -E "reset-quota|novacell|check-individual-quotas|reset_quotas_daily" | crontab - 2>/dev/null || true
systemctl daemon-reload

echo -e "${GREEN}[2/15] PAKETLER...${NC}"
apt update -qq
apt install -y python3 python3-pip python3-venv sqlite3 curl bc >/dev/null 2>&1

INSTALL_DIR="/opt/xui-admin-panel"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

if [ -f "$INSTALL_DIR/admin_panel.db" ]; then
    echo -e "${YELLOW}ℹ️  Mevcut veritabanı korunuyor${NC}"
    sqlite3 "$INSTALL_DIR/admin_panel.db" "PRAGMA table_info(user_settings);" | grep -q "folder" || {
        echo -e "${YELLOW}🔧 folder sütunu ekleniyor...${NC}"
        sqlite3 "$INSTALL_DIR/admin_panel.db" "ALTER TABLE user_settings ADD COLUMN folder TEXT DEFAULT 'Tümü';"
    }
    sqlite3 "$INSTALL_DIR/admin_panel.db" "PRAGMA table_info(user_settings);" | grep -q "quota_start_date" || {
        echo -e "${YELLOW}🔧 quota_start_date sütunu ekleniyor...${NC}"
        sqlite3 "$INSTALL_DIR/admin_panel.db" "ALTER TABLE user_settings ADD COLUMN quota_start_date TEXT DEFAULT NULL;"
    }
else
    echo -e "${YELLOW}🆕 Yeni kurulum${NC}"
fi

echo -e "${GREEN}[3/15] PYTHON ORTAMI...${NC}"
python3 -m venv venv
source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet flask flask-cors bcrypt

echo -e "${GREEN}[4/15] BACKEND DOSYASI...${NC}"
curl -sL https://raw.githubusercontent.com/eren73546/novacell-admin-panel/main/app.py -o app.py || {
    echo -e "${RED}❌ Backend indirilemedi!${NC}"
    exit 1
}

echo -e "${GREEN}[5/15] FRONTEND DOSYASI...${NC}"
curl -sL https://raw.githubusercontent.com/eren73546/novacell-admin-panel/main/index.html -o index.html || {
    echo -e "${RED}❌ Frontend indirilemedi!${NC}"
    exit 1
}

echo -e "${GREEN}[6/15] PANEL AYARLARI...${NC}"
echo ""
echo -e "${BLUE}═══════════════════════════════════════════════════════${NC}"
echo -e "${YELLOW}📋 PANEL İSMİ ÖZELLEŞTİRME${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════${NC}"
echo ""
echo -e "${YELLOW}Panel başlığını özelleştirmek ister misiniz?${NC}"
echo ""
read -p "Panel ismi girin (boş bırakırsanız 'NovaCell-3 v4.7'): " PANEL_NAME
PANEL_NAME=${PANEL_NAME:-NovaCell-3 v4.7}

echo -e "${GREEN}✅ Seçilen İsim: $PANEL_NAME${NC}"
echo -e "${YELLOW}⚙️  Dosya içerikleri güncelleniyor...${NC}"

python3 -c "
import sys

yeni_isim = '''$PANEL_NAME'''

# 1. APP.PY DÜZENLEME
try:
    with open('app.py', 'r', encoding='utf-8') as f:
        kod = f.read()
    
    kod = kod.replace(\"'sunucu_adi': 'NovaCell-3'\", f\"'sunucu_adi': '{yeni_isim}'\")
    kod = kod.replace('\"NovaCell-3\"', f'\"{yeni_isim}\"')
    
    if 'SERVER_NAME =' in kod:
        import re
        kod = re.sub(r'SERVER_NAME = .*', f'SERVER_NAME = \"{yeni_isim}\"', kod)
    
    with open('app.py', 'w', encoding='utf-8') as f:
        f.write(kod)
    print('✅ app.py içindeki isimler güncellendi.')
except Exception as e:
    print(f'❌ app.py düzenleme hatası: {e}')

# 2. INDEX.HTML DÜZENLEME
try:
    with open('index.html', 'r', encoding='utf-8') as f:
        html = f.read()
    
    html = html.replace('NovaCell-3 v4.7', yeni_isim)
    html = html.replace('NovaCell-3 v4.5', yeni_isim)
    html = html.replace('NovaCell-3', yeni_isim)
    
    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(html)
    print('✅ index.html içindeki başlıklar güncellendi.')
except Exception as e:
    print(f'❌ index.html düzenleme hatası: {e}')
"

echo ""
echo -e "${GREEN}[7/15] VERITABANI...${NC}"
cd "$INSTALL_DIR"
source venv/bin/activate
python3 -c "from app import init_db; init_db()"

echo -e "${GREEN}[8/15] CRON SCRİPTİ...${NC}"
curl -sL https://raw.githubusercontent.com/eren73546/novacell-admin-panel/main/reset_quotas_daily.py -o /opt/xui-admin-panel/reset_quotas_daily.py || {
    echo -e "${RED}❌ Cron scripti indirilemedi!${NC}"
    exit 1
}
chmod +x /opt/xui-admin-panel/reset_quotas_daily.py
echo -e "${GREEN}✅ Cron scripti indirildi${NC}"

echo -e "${GREEN}[9/15] CRONTAB AYARLARI...${NC}"
CRON_LINE="1 0 * * * root /usr/bin/python3 /opt/xui-admin-panel/reset_quotas_daily.py >> /var/log/quota-reset.log 2>&1"

if ! grep -q "reset_quotas_daily.py" /etc/crontab 2>/dev/null; then
    echo "$CRON_LINE" >> /etc/crontab
    echo -e "${GREEN}✅ Crontab'a eklendi (Her gece 00:01)${NC}"
else
    echo -e "${YELLOW}ℹ️  Crontab'da zaten mevcut${NC}"
fi

systemctl restart cron
echo -e "${GREEN}✅ Cron servisi restart edildi${NC}"

echo -e "${GREEN}[10/15] SERVİS DOSYASI...${NC}"
cat > /etc/systemd/system/xui-admin-panel.service << EOF
[Unit]
Description=$PANEL_NAME Admin Panel
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

echo -e "${GREEN}[11/15] SERVİS BAŞLATILIYOR...${NC}"
systemctl daemon-reload
systemctl enable xui-admin-panel
systemctl start xui-admin-panel
sleep 3

if ! systemctl is-active --quiet xui-admin-panel; then
    echo -e "${RED}❌ Servis başlatılamadı!${NC}"
    journalctl -u xui-admin-panel -n 20 --no-pager
    exit 1
fi

echo -e "${GREEN}[12/15] TELEGRAM YEDEKLEME (İSTEĞE BAĞLI)...${NC}"
echo ""
echo -e "${BLUE}═══════════════════════════════════════════════════════${NC}"
echo -e "${YELLOW}📱 TELEGRAM YEDEKLEME KURULUMU (Opsiyonel)${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════${NC}"
echo ""
echo -e "${YELLOW}Telegram'a günlük otomatik yedek göndermek ister misiniz?${NC}"
echo -e "${YELLOW}(Her gece 04:30'da admin_panel.db ve x-ui.db yedeği)${NC}"
echo ""
read -p "Telegram yedeği kurmak istiyor musunuz? (e/h) [h]: " TELEGRAM_CHOICE
TELEGRAM_CHOICE=${TELEGRAM_CHOICE:-h}

if [[ "$TELEGRAM_CHOICE" =~ ^[Ee]$ ]]; then
    echo ""
    echo -e "${BLUE}═══════════════════════════════════════════════════════${NC}"
    echo -e "${YELLOW}📝 TELEGRAM BOT KURULUM ADIMLARI:${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════════════════${NC}"
    echo ""
    echo -e "${YELLOW}1️⃣  Telegram'da @BotFather'a git${NC}"
    echo -e "${YELLOW}2️⃣  /newbot komutu ile yeni bot oluştur${NC}"
    echo -e "${YELLOW}3️⃣  Bot adı ve username belirle${NC}"
    echo -e "${YELLOW}4️⃣  Aldığın token'ı kopyala (örn: 123456:ABC-DEF...)${NC}"
    echo ""
    echo -e "${YELLOW}5️⃣  @userinfobot'a git${NC}"
    echo -e "${YELLOW}6️⃣  /start yaz${NC}"
    echo -e "${YELLOW}7️⃣  'Id' numaranı kopyala (örn: 123456789)${NC}"
    echo ""
    echo -e "${BLUE}═══════════════════════════════════════════════════════${NC}"
    echo ""
    
    read -p "📱 Bot Token girin: " BOT_TOKEN
    while [ -z "$BOT_TOKEN" ]; do
        echo -e "${RED}❌ Token boş olamaz!${NC}"
        read -p "📱 Bot Token girin: " BOT_TOKEN
    done
    
    read -p "👤 Chat ID girin: " CHAT_ID
    while [ -z "$CHAT_ID" ]; do
        echo -e "${RED}❌ Chat ID boş olamaz!${NC}"
        read -p "👤 Chat ID girin: " CHAT_ID
    done
    
    cat > /root/.novacell-telegram << EOF
BOT_TOKEN="$BOT_TOKEN"
CHAT_ID="$CHAT_ID"
EOF
    chmod 600 /root/.novacell-telegram
    
    cat > /root/novacell-telegram-backup.sh << 'BACKUPSCRIPT'
#!/bin/bash
source /root/.novacell-telegram

DATE=$(date +%Y%m%d-%H%M)
BACKUP_DIR="/root/novacell-backups"
mkdir -p "$BACKUP_DIR"

cp /opt/xui-admin-panel/admin_panel.db "$BACKUP_DIR/admin_panel-$DATE.db" 2>/dev/null
[ -f /etc/x-ui/x-ui.db ] && cp /etc/x-ui/x-ui.db "$BACKUP_DIR/x-ui-$DATE.db" 2>/dev/null

cd "$BACKUP_DIR"
if [ -f "x-ui-$DATE.db" ]; then
    tar -czf "NovaCell-v4.7-$DATE.tar.gz" admin_panel-$DATE.db x-ui-$DATE.db
else
    tar -czf "NovaCell-v4.7-$DATE.tar.gz" admin_panel-$DATE.db
fi

curl -F document=@"NovaCell-v4.7-$DATE.tar.gz" \
     -F chat_id="$CHAT_ID" \
     -F caption="📱 NovaCell-3 v4.7 Günlük Yedek — $DATE" \
     "https://api.telegram.org/bot$BOT_TOKEN/sendDocument" >/dev/null 2>&1

find "$BACKUP_DIR" -name "*.tar.gz" -mtime +14 -delete
find "$BACKUP_DIR" -name "*.db" -mtime +14 -delete
BACKUPSCRIPT
    chmod +x /root/novacell-telegram-backup.sh
    
    echo ""
    echo -e "${YELLOW}📤 Test mesajı gönderiliyor...${NC}"
    TEST_RESPONSE=$(curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/sendMessage" \
         -d chat_id="$CHAT_ID" \
         -d text="✅ $PANEL_NAME v4.7 kurulumu tamamlandı! Günlük yedekler her gece 04:30'da gönderilecek.")
    
    if echo "$TEST_RESPONSE" | grep -q '"ok":true'; then
        echo -e "${GREEN}✅ Test mesajı başarıyla gönderildi!${NC}"
        echo -e "${GREEN}✅ Telegram'ınızı kontrol edin${NC}"
        TELEGRAM_ENABLED=true
    else
        echo -e "${RED}❌ Test mesajı gönderilemedi!${NC}"
        echo -e "${YELLOW}Token veya Chat ID hatalı olabilir${NC}"
        echo -e "${YELLOW}Telegram yedeği devre dışı bırakılıyor...${NC}"
        TELEGRAM_ENABLED=false
        rm -f /root/.novacell-telegram
        rm -f /root/novacell-telegram-backup.sh
    fi
else
    echo -e "${YELLOW}⚠️  Telegram yedeği atlandı${NC}"
    TELEGRAM_ENABLED=false
    
    cat > /root/novacell-telegram-backup.sh << 'BACKUPSCRIPT'
#!/bin/bash
echo "Telegram yedeği devre dışı"
exit 0
BACKUPSCRIPT
    chmod +x /root/novacell-telegram-backup.sh
fi

echo -e "${GREEN}[13/15] TELEGRAM CRON AYARLARI...${NC}"
if [ "$TELEGRAM_ENABLED" = true ]; then
    (crontab -l 2>/dev/null | grep -v "novacell-telegram-backup"; 
    echo "30 4 * * * /root/novacell-telegram-backup.sh") | crontab -
    echo -e "${GREEN}✅ Telegram yedeği cron'a eklendi (Her gece 04:30)${NC}"
else
    echo -e "${YELLOW}⚠️  Telegram yedeği cron'a eklenmedi${NC}"
fi

echo -e "${GREEN}[14/15] SON KONTROL...${NC}"
sleep 2

echo -e "${GREEN}[15/15] TAMAMLANDI!${NC}"
clear
echo -e "${GREEN}"
cat << "EOF"
╔══════════════════════════════════════════════════════════╗
║    ✅ KURULUM BAŞARIYLA TAMAMLANDI!                      ║
╚══════════════════════════════════════════════════════════╝
EOF
echo -e "${NC}"
echo ""
SERVER_IP=$(hostname -I | awk '{print $1}')
echo -e "${BLUE}🌐 Panel Adresi: http://${SERVER_IP}:8888${NC}"
echo -e "${BLUE}👤 Kullanıcı Adı: novacell${NC}"
echo -e "${BLUE}🔑 Şifre: NovaCell25Hakki${NC}"
echo -e "${BLUE}📋 Panel İsmi: $PANEL_NAME${NC}"
echo ""
echo -e "${YELLOW}═══════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}✅ YENİ ÖZELLİKLER (v4.7):${NC}"
echo -e "   • Ödeme alınca kota sıfırlanmaz ✅"
echo -e "   • quota_start_date sistemi (sabit gün) ✅"
echo -e "   • Otomatik kota yenileme (her gece 00:01) ✅"
echo -e "   • Ödeme yapılmamışsa data kesilir ✅"
echo -e "   • Panel Adı: $PANEL_NAME"
echo ""
if [ "$TELEGRAM_ENABLED" = true ]; then
    echo -e "${GREEN}✅ TELEGRAM YEDEKLEME:${NC}"
    echo -e "   • Otomatik yedek: Her gece 04:30"
    echo -e "   • Yedek süresi: 14 gün"
    echo -e "   • Manuel yedek: ${BLUE}bash /root/novacell-telegram-backup.sh${NC}"
    echo ""
fi
echo -e "${YELLOW}═══════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}🔧 Yönetim Komutları:${NC}"
echo -e "   Durum: ${BLUE}systemctl status xui-admin-panel${NC}"
echo -e "   Log: ${BLUE}journalctl -u xui-admin-panel -f${NC}"
echo -e "   Yeniden Başlat: ${BLUE}systemctl restart xui-admin-panel${NC}"
echo -e "   Cron Test: ${BLUE}/usr/bin/python3 /opt/xui-admin-panel/reset_quotas_daily.py${NC}"
echo -e "   Cron Log: ${BLUE}tail -f /var/log/quota-reset.log${NC}"
if [ "$TELEGRAM_ENABLED" = true ]; then
    echo -e "   Telegram Test: ${BLUE}bash /root/novacell-telegram-backup.sh${NC}"
fi
echo ""
echo -e "${GREEN}✅ Tarayıcıda paneli açın ve CTRL+F5 yapın!${NC}"
echo ""
