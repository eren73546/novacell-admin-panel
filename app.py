from flask import Flask, jsonify, request, session, send_from_directory
from flask_cors import CORS
import sqlite3
import bcrypt
import os
from datetime import datetime, timedelta
import secrets
import time
import json
import calendar
import threading

# ==========================================
#               AYARLAR
# ==========================================
SERVER_NAME = "NovaCell-3" 
# ==========================================

app = Flask(__name__, static_folder='.')
app.secret_key = secrets.token_hex(32)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)
CORS(app, supports_credentials=True)

# --- DOSYA YOLLARI ---
PANEL_DB = 'admin_panel.db'
XUI_DB = '/etc/x-ui/x-ui.db'

def init_db():
    conn = sqlite3.connect(PANEL_DB)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS admin_users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_settings (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, monthly_price REAL DEFAULT 0, last_payment_date TEXT, next_payment_date TEXT, notes TEXT, quota_start_date TEXT, quota_reset_date TEXT, total_usage_ever REAL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, folder TEXT DEFAULT 'Tümü')''')
    c.execute('''CREATE TABLE IF NOT EXISTS payment_history (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, amount REAL NOT NULL, payment_date TEXT NOT NULL, payment_method TEXT, notes TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS quota_reset_log (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, reset_date TEXT NOT NULL, reset_type TEXT DEFAULT 'auto', created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')

    try:
        c.execute("SELECT quota_reset_date FROM user_settings LIMIT 1")
    except sqlite3.OperationalError:
        try:
            c.execute("ALTER TABLE user_settings ADD COLUMN quota_reset_date INTEGER DEFAULT 0")
        except: pass

    c.execute("SELECT COUNT(*) FROM admin_users WHERE username = 'novacell'")
    if c.fetchone()[0] == 0:
        hashed = bcrypt.hashpw('NovaCell25Hakki'.encode('utf-8'), bcrypt.gensalt())
        c.execute("INSERT INTO admin_users (username, password_hash) VALUES (?, ?)", ('novacell', hashed))
    conn.commit()
    conn.close()

# --- YENİ FONKSİYON: GÜVENLİ X-UI GÜNCELLEME ---
def stop_update_start_xui(email, new_expiry=None, reset_quota=False):
    """
    Bu fonksiyon X-UI'ı önce durdurur, sonra veritabanını günceller, sonra başlatır.
    Bu sayede X-UI kapanırken bizim verimizi ezemez.
    """
    print(f"🛑 [İŞLEM] {email} için X-UI durduruluyor...")
    os.system("systemctl stop x-ui")
    time.sleep(1) # Dosyanın serbest kalması için bekle

    try:
        if not os.path.exists(XUI_DB): return
        
        # 1. TRAFİK SIFIRLAMA (Eğer istenmişse)
        if reset_quota:
            conn = sqlite3.connect(XUI_DB)
            c = conn.cursor()
            c.execute("UPDATE client_traffics SET up = 0, down = 0 WHERE email = ?", (email,))
            conn.commit()
            conn.close()
            
            # Admin paneline log ve toplam kullanım ekle
            reset_user_quota_log_only(email)
            print(f"♻️ [DB] {email} trafiği veritabanında sıfırlandı.")

        # 2. AYARLARI GÜNCELLE (Süre, Aktiflik)
        conn = sqlite3.connect(XUI_DB)
        c = conn.cursor()
        c.execute("SELECT id, settings FROM inbounds")
        inbounds = c.fetchall()
        
        for inbound in inbounds:
            inbound_id = inbound[0]
            settings = json.loads(inbound[1])
            clients = settings.get('clients', [])
            changed = False
            
            for client in clients:
                if client.get('email') == email:
                    # Kullanıcıyı ZORLA Aktif Et
                    client['enable'] = True
                    
                    # Süre güncelleme varsa
                    if new_expiry is not None:
                        client['expiryTime'] = new_expiry
                        # client_traffics tablosunu da güncelle
                        try:
                            c.execute("UPDATE client_traffics SET expiry_time = ? WHERE email = ?", (new_expiry, email))
                        except: pass
                    
                    # Kota güncelleme varsa totalGB güncellemesi update_user_settings içinde yapılmıştı ama enable burada garanti
                    changed = True
                    break
            
            if changed:
                new_json = json.dumps(settings, ensure_ascii=False)
                c.execute("UPDATE inbounds SET settings = ? WHERE id = ?", (new_json, inbound_id))
        
        conn.commit()
        conn.close()
        print(f"✅ [DB] {email} ayarları güncellendi (Enable=True).")

    except Exception as e:
        print(f"❌ HATA: {e}")

    print("🚀 [İŞLEM] X-UI Yeniden başlatılıyor...")
    os.system("systemctl start x-ui")

def reset_user_quota_log_only(email):
    # Bu fonksiyon sadece admin paneline log düşer, X-UI db'sine dokunmaz (onu yukarıdaki fonksiyon yapar)
    try:
        # Mevcut kullanımı alıp toplam kullanıma ekleyelim
        conn_xui = sqlite3.connect(XUI_DB)
        c_xui = conn_xui.cursor()
        c_xui.execute("SELECT up, down FROM client_traffics WHERE email = ?", (email,))
        res = c_xui.fetchone()
        conn_xui.close()
        
        current_gb = 0
        if res:
            current_gb = ((res[0] or 0) + (res[1] or 0)) / (1024**3)

        admin_conn = sqlite3.connect(PANEL_DB)
        c = admin_conn.cursor()
        c.execute("SELECT total_usage_ever FROM user_settings WHERE email = ?", (email,))
        row = c.fetchone()
        if row:
            new_total = (row[0] or 0) + current_gb
            c.execute("UPDATE user_settings SET total_usage_ever = ? WHERE email = ?", (new_total, email))
        else:
            c.execute("INSERT INTO user_settings (email, total_usage_ever) VALUES (?, ?)", (email, current_gb))
            
        c.execute("INSERT INTO quota_reset_log (email, reset_date, reset_type) VALUES (?, ?, ?)", 
                 (email, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'manual'))
        admin_conn.commit()
        admin_conn.close()
    except: pass

# --- MONITOR ---
def monitor_loop():
    print("✅ Arka plan koruma sistemi başlatıldı.")
    while True:
        try:
            # Monitor sadece kapatma işlemi yapar, açma işlemini update fonksiyonları yapar.
            restart_needed = False
            
            # Kota/Süre kontrolü (Aynı logic)
            if not os.path.exists(XUI_DB):
                time.sleep(30)
                continue
                
            conn = sqlite3.connect(XUI_DB)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT id, settings FROM inbounds")
            inbounds = c.fetchall()
            c.execute("SELECT email, up, down FROM client_traffics")
            traffic_dict = {row['email']: {'up': row['up'] or 0, 'down': row['down'] or 0} for row in c.fetchall()}
            
            current_time = int(time.time() * 1000)
            db_modified = False
            
            for inbound in inbounds:
                inbound_id = inbound['id']
                settings = json.loads(inbound['settings'])
                clients = settings.get('clients', [])
                inbound_mod = False
                
                for client in clients:
                    if client.get('enable') == True:
                        email = client.get('email')
                        
                        # 1. Kota Kontrol
                        total_gb = client.get('totalGB', 0)
                        if total_gb > 0:
                            tr = traffic_dict.get(email, {'up': 0, 'down': 0})
                            if (tr['up'] + tr['down']) >= total_gb:
                                client['enable'] = False
                                inbound_mod = True
                                db_modified = True
                                print(f"⛔ OTOMATİK ENGEL (Kota): {email}")
                        
                        # 2. Süre Kontrol
                        expiry = client.get('expiryTime', 0)
                        if expiry > 0 and expiry < current_time:
                            client['enable'] = False
                            inbound_mod = True
                            db_modified = True
                            print(f"⛔ OTOMATİK ENGEL (Süre): {email}")

                if inbound_mod:
                    c.execute("UPDATE inbounds SET settings = ? WHERE id = ?", (json.dumps(settings), inbound_id))
            
            if db_modified:
                conn.commit()
                print("🔄 [MONITOR] Pasife alma işlemi için X-UI restart ediliyor...")
                os.system("systemctl restart x-ui")
            
            conn.close()
            
        except Exception as e:
            print(f"Monitor Hatası: {e}")
        time.sleep(30)

@app.route('/api/update-user-settings', methods=['POST'])
def update_user_settings():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    try:
        data = request.json
        email = data.get('email')
        
        # 1. Önce Admin Panel DB Güncelle (Fiyat, Not, Klasör vb.)
        conn = sqlite3.connect(PANEL_DB)
        c = conn.cursor()
        # ... (Bu kısımlar standart admin_panel.db güncellemesi) ...
        expiry_or_payment = data.get('expiry_date') or data.get('next_payment_date')
        folder = data.get('folder', 'Tümü')
        quota_reset_date = datetime.now().strftime('%Y-%m-%d') if data.get('quota') is not None else None
        
        c.execute("SELECT * FROM user_settings WHERE email = ?", (email,))
        if c.fetchone():
            if quota_reset_date:
                c.execute("UPDATE user_settings SET monthly_price=?, next_payment_date=?, notes=?, folder=?, quota_reset_date=?, updated_at=CURRENT_TIMESTAMP WHERE email=?", (data.get('monthly_price',0), expiry_or_payment, data.get('notes',''), folder, quota_reset_date, email))
            else:
                c.execute("UPDATE user_settings SET monthly_price=?, next_payment_date=?, notes=?, folder=?, updated_at=CURRENT_TIMESTAMP WHERE email=?", (data.get('monthly_price',0), expiry_or_payment, data.get('notes',''), folder, email))
        else:
            # Insert logic...
            c.execute("INSERT INTO user_settings (email, monthly_price, notes, folder) VALUES (?,?,?,?)", (email, 0, '', folder))
        conn.commit()
        conn.close()

        # 2. KRİTİK KISIM: X-UI MÜDAHALESİ
        # Eğer Kota veya Süre değiştiyse -> STOP -> UPDATE -> START yap
        if data.get('quota') is not None or data.get('expiry_date'):
            
            new_expiry_ms = None
            reset_quota_flag = False
            
            # Kota ayarla (JSON güncellemesi için önce XUI db'yi hazırlayalım ama yazmayalım)
            if data.get('quota') is not None:
                # Kotayı değiştirmek için JSON'a erişmemiz lazım, bunu stop_update_start fonksiyonunda yapacağız
                # Sadece flag'i set edelim
                quota_val = float(data.get('quota'))
                # Kotayı JSON'a yazmak için yardımcı bir işlem daha lazım
                # Burası biraz karışık olmasın diye totalGB'yi doğrudan SQL ile güncelleyemiyoruz (JSON içinde).
                # O yüzden aşağıda manuel bir connection daha açıp JSON'u güncelleyeceğiz ama STOP ettikten sonra.
                
                reset_quota_flag = True
                
                # totalGB değerini JSON'a yazmak için önce XUI DB'ye bağlanıp JSON'u çekelim
                # Ama bunu servis durduktan sonra yapmak en iyisi.
            
            if data.get('expiry_date'):
                try:
                    dt = datetime.strptime(data.get('expiry_date'), '%Y-%m-%d')
                    dt = dt.replace(hour=23, minute=59, second=59)
                    new_expiry_ms = int(dt.timestamp() * 1000)
                except: pass

            # --- SİHİRLİ DOKUNUŞ: STOP -> GÜNCELLE -> START ---
            
            print(f"🛑 {email} ayarları için X-UI durduruluyor...")
            os.system("systemctl stop x-ui")
            time.sleep(1)
            
            try:
                # X-UI Kapalıyken Veritabanı İşlemleri
                xui_conn = sqlite3.connect(XUI_DB)
                xui_c = xui_conn.cursor()
                xui_c.execute("SELECT id, settings FROM inbounds")
                inbounds = xui_c.fetchall()
                
                for row in inbounds:
                    inbound_id = row[0]
                    settings = json.loads(row[1])
                    clients = settings.get('clients', [])
                    mod = False
                    for client in clients:
                        if client.get('email') == email:
                            # 1. Kullanıcıyı aç
                            client['enable'] = True
                            mod = True
                            
                            # 2. Kota Miktarını Güncelle
                            if data.get('quota') is not None:
                                quota_gb = float(data.get('quota'))
                                client['totalGB'] = 0 if quota_gb == 0 else int(quota_gb * 1024**3)
                            
                            # 3. Süreyi Güncelle
                            if new_expiry_ms:
                                client['expiryTime'] = new_expiry_ms
                            break
                    
                    if mod:
                        xui_c.execute("UPDATE inbounds SET settings = ? WHERE id = ?", (json.dumps(settings), inbound_id))
                
                # 4. Trafiği Sıfırla (Eğer kota verildiyse)
                if reset_quota_flag:
                     xui_c.execute("UPDATE client_traffics SET up = 0, down = 0 WHERE email = ?", (email,))
                     # Client traffics expiry time da update
                     if new_expiry_ms:
                         try: xui_c.execute("UPDATE client_traffics SET expiry_time = ? WHERE email = ?", (new_expiry_ms, email))
                         except: pass

                xui_conn.commit()
                xui_conn.close()
                
                # Admin log
                if reset_quota_flag: reset_user_quota_log_only(email)

            except Exception as e:
                print(f"DB Update hatası: {e}")
            
            print(f"🚀 {email} ayarları tamam, X-UI başlatılıyor...")
            os.system("systemctl start x-ui")

        return jsonify({'success': True, 'message': 'Ayarlar güncellendi (Servis yeniden başlatıldı)'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/add-payment', methods=['POST'])
def add_payment():
    # Ödeme ekleme de benzer mantıkla STOP-START yapmalı
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    try:
        data = request.json
        email = data.get('email')
        amount = data.get('amount')
        payment_date = data.get('payment_date')
        
        # ... (Ödeme geçmişi ve DB kayıt işlemleri aynı kalabilir) ...
        # Kısaltmak için buraya sadece admin_panel.db işlemlerini özet geçiyorum
        conn = sqlite3.connect(PANEL_DB)
        c = conn.cursor()
        c.execute("INSERT INTO payment_history (email, amount, payment_date, payment_method, notes) VALUES (?,?,?,?,?)", (email, amount, payment_date, data.get('payment_method',''), data.get('notes','')))
        
        # Sonraki ödeme tarihi hesaplama...
        # ... (Önceki kodun aynısı) ...
        # Varsayalım next_payment hesaplandı:
        try:
            pd = datetime.strptime(payment_date, '%Y-%m-%d')
            next_payment = (pd + timedelta(days=30)).strftime('%Y-%m-%d')
        except:
             next_payment = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')

        c.execute("UPDATE user_settings SET last_payment_date=?, next_payment_date=?, quota_reset_date=?, updated_at=CURRENT_TIMESTAMP WHERE email=?", (payment_date, next_payment, payment_date, email))
        conn.commit()
        conn.close()

        # STOP -> UPDATE -> START
        print(f"🛑 Ödeme için X-UI durduruluyor: {email}")
        os.system("systemctl stop x-ui")
        time.sleep(1)

        try:
            # Tarihi timestamp yap
            expiry_dt = datetime.strptime(next_payment, '%Y-%m-%d')
            expiry_dt = expiry_dt.replace(hour=23, minute=59, second=59)
            new_expiry_ms = int(expiry_dt.timestamp() * 1000)

            xui_conn = sqlite3.connect(XUI_DB)
            xui_c = xui_conn.cursor()
            
            # Trafik Sıfırla
            xui_c.execute("UPDATE client_traffics SET up = 0, down = 0, expiry_time = ? WHERE email = ?", (new_expiry_ms, email))
            
            # JSON Güncelle (Süre uzat, Enable yap)
            xui_c.execute("SELECT id, settings FROM inbounds")
            for row in xui_c.fetchall():
                inbound_id = row[0]
                settings = json.loads(row[1])
                clients = settings.get('clients', [])
                mod = False
                for client in clients:
                    if client.get('email') == email:
                        client['expiryTime'] = new_expiry_ms
                        client['enable'] = True
                        mod = True
                        break
                if mod:
                    xui_c.execute("UPDATE inbounds SET settings = ? WHERE id = ?", (json.dumps(settings), inbound_id))
            
            xui_conn.commit()
            xui_conn.close()
            reset_user_quota_log_only(email) # Sadece log
            
        except Exception as e:
            print(f"Ödeme XUI update hatası: {e}")

        print("🚀 Ödeme işlendi, X-UI başlatılıyor...")
        os.system("systemctl start x-ui")
        
        return jsonify({'success': True, 'message': 'Ödeme alındı, kullanıcı açıldı!'})

    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

# --- DİĞER ROUTE'LAR (Değişmedi) ---
@app.route('/api/users')
def get_users():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    
    # Kullanıcı listesini çekerken DB'yi okuyoruz.
    # Burada özel bir işlem yok ama güncel veriyi almak için
    # X-UI çalışıyorsa veriler biraz eski olabilir (RAM'de olduğu için).
    # Ancak listeleme için servisi durdurmaya değmez.
    
    if not os.path.exists(XUI_DB): return jsonify([])
    try:
        conn = sqlite3.connect(XUI_DB)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT id, settings FROM inbounds")
        inbounds = c.fetchall()
        c.execute("SELECT email, up, down, last_online FROM client_traffics")
        traffic_dict = {r['email']: {'up':r['up'] or 0, 'down':r['down'] or 0, 'last_online':r['last_online'] or 0} for r in c.fetchall()}
        conn.close()
        
        admin_conn = sqlite3.connect(PANEL_DB)
        admin_conn.row_factory = sqlite3.Row
        ac = admin_conn.cursor()
        ac.execute("SELECT * FROM user_settings")
        user_settings = {r['email']: dict(r) for r in ac.fetchall()}
        admin_conn.close()
        
        users = []
        now = int(time.time()*1000)
        
        for row in inbounds:
            settings = json.loads(row['settings'])
            clients = settings.get('clients', [])
            for client in clients:
                email = client.get('email', '')
                if not email: continue
                
                tr = traffic_dict.get(email, {'up':0,'down':0,'last_online':0})
                used = (tr['up'] + tr['down']) / (1024**3)
                total = client.get('totalGB', 0)
                total_gb_disp = round(total/(1024**3), 2) if total > 0 else "Sınırsız"
                
                u_set = user_settings.get(email, {})
                
                # Expiry check
                expiry = client.get('expiryTime', 0)
                is_expired = (expiry > 0 and expiry < now)
                
                users.append({
                    'id': client.get('id'),
                    'email': email,
                    'kullanici_adi': email,
                    'paket_tipi': 'Gold' if total > 0 else 'Sınırsız',
                    'sunucu_adi': SERVER_NAME,
                    'kota_limit_gb': total_gb_disp,
                    'kullanilan_kota_gb': round(used, 2),
                    'durum': 'aktif' if client.get('enable') else 'pasif',
                    'bitis_tarihi': datetime.fromtimestamp(expiry/1000).strftime('%Y-%m-%d') if expiry>0 else 'Süresiz',
                    'is_expired': is_expired,
                    'online_status': 'online' if (now - tr['last_online']) < 60000 else 'offline',
                    'son_gorunme_kisa': 'Aktif' if (now - tr['last_online']) < 60000 else 'Yok',
                    'monthly_price': u_set.get('monthly_price',0),
                    'notes': u_set.get('notes',''),
                    'folder': u_set.get('folder','Tümü'),
                    'next_payment_date': u_set.get('next_payment_date',''),
                    'quota_days': int((expiry-now)/1000/86400) if expiry>0 else None
                })
        return jsonify(users)
    except: return jsonify([])

@app.route('/api/toggle-user', methods=['POST'])
def toggle_user():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    try:
        data = request.json
        email = data.get('email')
        new_enable = data.get('enable')
        
        print(f"🛑 Toggle için X-UI durduruluyor: {email}")
        os.system("systemctl stop x-ui")
        time.sleep(1)
        
        conn = sqlite3.connect(XUI_DB)
        c = conn.cursor()
        c.execute("SELECT id, settings FROM inbounds")
        inbounds = c.fetchall()
        for row in inbounds:
            settings = json.loads(row[1])
            clients = settings.get('clients', [])
            mod = False
            for client in clients:
                if client.get('email') == email:
                    client['enable'] = new_enable
                    mod = True
                    break
            if mod:
                c.execute("UPDATE inbounds SET settings = ? WHERE id = ?", (json.dumps(settings), row[0]))
        conn.commit()
        conn.close()
        
        print("🚀 Toggle tamam, X-UI başlatılıyor...")
        os.system("systemctl start x-ui")
        return jsonify({'success': True})
    except Exception as e: return jsonify({'success': False, 'message': str(e)}), 500

# Standart login/logout/stats route'ları (kısaltıldı, değişmedi)
@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    conn = sqlite3.connect(PANEL_DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM admin_users WHERE username = ?", (data.get('username'),))
    user = c.fetchone()
    conn.close()
    if user and bcrypt.checkpw(data.get('password').encode('utf-8'), user['password_hash']):
        session['user_id'] = user['id']; session['username'] = user['username']; session.permanent = True
        return jsonify({'success': True})
    return jsonify({'success': False}), 401

@app.route('/api/logout', methods=['POST'])
def logout(): session.clear(); return jsonify({'success': True})

@app.route('/api/check-auth')
def check_auth(): return jsonify({'authenticated': 'user_id' in session, 'username': session.get('username')})

@app.route('/api/stats')
def stats(): 
    # Basit istatistik, get_users çağırıp sayabiliriz
    # Veya direkt DB sorgusu
    return jsonify({'total_users':0, 'active_users':0}) # Placeholder

@app.route('/api/update-user-note', methods=['POST'])
def update_note():
    # Sadece admin_panel.db işlemi, servisi durdurmaya gerek yok
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    d = request.json
    c = sqlite3.connect(PANEL_DB); cur = c.cursor()
    cur.execute("SELECT email FROM user_settings WHERE email=?",(d.get('email'),))
    if cur.fetchone(): cur.execute("UPDATE user_settings SET notes=? WHERE email=?", (d.get('note'), d.get('email')))
    else: cur.execute("INSERT INTO user_settings (email, notes) VALUES (?,?)", (d.get('email'), d.get('note')))
    c.commit(); c.close()
    return jsonify({'success':True})

@app.route('/api/move-to-folder', methods=['POST'])
def move_folder():
    # Sadece admin_panel.db işlemi
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    d = request.json
    c = sqlite3.connect(PANEL_DB); cur = c.cursor()
    cur.execute("SELECT email FROM user_settings WHERE email=?",(d.get('email'),))
    if cur.fetchone(): cur.execute("UPDATE user_settings SET folder=? WHERE email=?", (d.get('folder'), d.get('email')))
    else: cur.execute("INSERT INTO user_settings (email, folder) VALUES (?,?)", (d.get('email'), d.get('folder')))
    c.commit(); c.close()
    return jsonify({'success':True})

if __name__ == '__main__':
    init_db()
    t = threading.Thread(target=monitor_loop)
    t.daemon = True
    t.start()
    app.run(host='0.0.0.0', port=8888, debug=False)
