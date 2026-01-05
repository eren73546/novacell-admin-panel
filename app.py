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

app = Flask(__name__, static_folder='.')
app.secret_key = secrets.token_hex(32)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)
CORS(app, supports_credentials=True)

# --- VERITABANI DOSYA YOLLARI ---
PANEL_DB = 'admin_panel.db'
XUI_DB = '/etc/x-ui/x-ui.db'

def init_db():
    conn = sqlite3.connect(PANEL_DB)
    c = conn.cursor()
    
    # Tabloları oluştur
    c.execute('''CREATE TABLE IF NOT EXISTS admin_users
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  username TEXT NOT NULL UNIQUE,
                  password_hash TEXT NOT NULL)''')
                  
    c.execute('''CREATE TABLE IF NOT EXISTS user_settings
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  email TEXT NOT NULL UNIQUE,
                  monthly_price REAL DEFAULT 0,
                  last_payment_date TEXT,
                  next_payment_date TEXT,
                  notes TEXT,
                  quota_start_date TEXT,
                  quota_reset_date TEXT,
                  total_usage_ever REAL DEFAULT 0,
                  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                  folder TEXT DEFAULT 'Tümü')''')
                  
    c.execute('''CREATE TABLE IF NOT EXISTS payment_history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  email TEXT NOT NULL,
                  amount REAL NOT NULL,
                  payment_date TEXT NOT NULL,
                  payment_method TEXT,
                  notes TEXT,
                  created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
                  
    c.execute('''CREATE TABLE IF NOT EXISTS quota_reset_log
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  email TEXT NOT NULL,
                  reset_date TEXT NOT NULL,
                  reset_type TEXT DEFAULT 'auto',
                  created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')

    # --- KRITIK DUZELTME: EKSIK SUTUN KONTROLU (MIGRATION) ---
    try:
        c.execute("SELECT quota_reset_date FROM user_settings LIMIT 1")
    except sqlite3.OperationalError:
        print("Sistem: quota_reset_date sütunu eksik, otomatik ekleniyor...")
        try:
            c.execute("ALTER TABLE user_settings ADD COLUMN quota_reset_date INTEGER DEFAULT 0")
            print("Sistem: Sütun başarıyla eklendi.")
        except Exception as e:
            print(f"Sistem: Sütun ekleme hatası (Tablo boş olabilir): {e}")
    # ---------------------------------------------------------

    # Admin kullanıcısı oluştur
    c.execute("SELECT COUNT(*) FROM admin_users WHERE username = 'novacell'")
    if c.fetchone()[0] == 0:
        hashed = bcrypt.hashpw('NovaCell25Hakki'.encode('utf-8'), bcrypt.gensalt())
        c.execute("INSERT INTO admin_users (username, password_hash) VALUES (?, ?)", ('novacell', hashed))
    
    conn.commit()
    conn.close()

def sync_xui_expiry(email, expiry_timestamp_ms):
    """
    X-UI Veritabanını (client_traffics) doğrudan günceller.
    Bu, kullanıcının anında açılmasını/kapanmasını sağlar.
    """
    try:
        if not os.path.exists(XUI_DB): return
        
        conn = sqlite3.connect(XUI_DB)
        c = conn.cursor()
        # client_traffics tablosunu güncelle
        c.execute("UPDATE client_traffics SET expiry_time = ? WHERE email = ?", (expiry_timestamp_ms, email))
        conn.commit()
        conn.close()
        print(f"X-UI Sync: {email} -> {expiry_timestamp_ms} olarak güncellendi.")
    except Exception as e:
        print(f"X-UI Sync Hatası: {e}")

def check_and_disable_quota_exceeded():
    try:
        if not os.path.exists(XUI_DB): return
        
        conn = sqlite3.connect(XUI_DB)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT id, settings FROM inbounds")
        inbounds = c.fetchall()
        
        c.execute("SELECT email, up, down FROM client_traffics")
        traffic_dict = {}
        for row in c.fetchall():
            traffic_dict[row['email']] = {'up': row['up'] or 0, 'down': row['down'] or 0}
        
        modified = False
        for inbound in inbounds:
            inbound_id = inbound['id']
            settings = json.loads(inbound['settings'])
            clients = settings.get('clients', [])
            
            inbound_modified = False
            for client in clients:
                email = client.get('email', '')
                total_gb = client.get('totalGB', 0)
                
                # KOTA KONTROLU: KOTA DOLMUSSA PASIF ET
                if total_gb > 0 and client.get('enable') == True:
                    traffic = traffic_dict.get(email, {'up': 0, 'down': 0})
                    used = (traffic['up'] + traffic['down'])
                    
                    if used >= total_gb:
                        client['enable'] = False
                        inbound_modified = True
                        modified = True
                        print(f"Kullanıcı {email} kotası doldu, devre dışı bırakıldı")
                
                # YENI EKLENEN: KOTA ALTINDAYSA VE PASIFSE OTOMATIK AKTIF ET!
                elif total_gb > 0 and client.get('enable') == False:
                    traffic = traffic_dict.get(email, {'up': 0, 'down': 0})
                    used = (traffic['up'] + traffic['down'])
                    
                    # Kota altındaysa otomatik aktif et
                    if used < total_gb:
                        client['enable'] = True
                        inbound_modified = True
                        modified = True
                        print(f"Kullanıcı {email} kota altında, otomatik aktif edildi")
            
            if inbound_modified:
                settings['clients'] = clients
                new_settings_json = json.dumps(settings, ensure_ascii=False)
                c.execute("UPDATE inbounds SET settings = ? WHERE id = ?", (new_settings_json, inbound_id))
        
        if modified: 
            conn.commit()
            # Değişiklik olduysa x-ui restart et
            os.system('x-ui restart')
        conn.close()
    except Exception as e:
        print(f"Kota kontrol hatası: {e}")

def check_and_disable_expired_users():
    try:
        if not os.path.exists(XUI_DB): return
        
        conn = sqlite3.connect(XUI_DB)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT id, settings FROM inbounds")
        inbounds = c.fetchall()
        
        current_time_ms = int(time.time() * 1000)
        modified = False
        
        for inbound in inbounds:
            inbound_id = inbound['id']
            settings = json.loads(inbound['settings'])
            clients = settings.get('clients', [])
            
            inbound_modified = False
            for client in clients:
                email = client.get('email', '')
                expiry = client.get('expiryTime', 0)
                
                if expiry > 0 and expiry < current_time_ms and client.get('enable') == True:
                    client['enable'] = False
                    inbound_modified = True
                    modified = True
                    print(f"Kullanıcı {email} süresi doldu, devre dışı bırakıldı")
            
            if inbound_modified:
                settings['clients'] = clients
                new_settings_json = json.dumps(settings, ensure_ascii=False)
                c.execute("UPDATE inbounds SET settings = ? WHERE id = ?", (new_settings_json, inbound_id))
        
        if modified: 
            conn.commit()
        conn.close()
    except Exception as e:
        print(f"Süre kontrol hatası: {e}")

def get_xui_users():
    try:
        check_and_disable_quota_exceeded()
        check_and_disable_expired_users()
        
        if not os.path.exists(XUI_DB): return []
        
        conn = sqlite3.connect(XUI_DB)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT id, settings FROM inbounds")
        inbounds = c.fetchall()
        c.execute("SELECT email, up, down, inbound_id, last_online FROM client_traffics")
        
        traffic_dict = {}
        for row in c.fetchall():
            traffic_dict[row['email']] = {
                'up': row['up'] or 0,
                'down': row['down'] or 0,
                'inbound_id': row['inbound_id'],
                'last_online': row['last_online'] or 0
            }
        conn.close()
        
        admin_conn = sqlite3.connect(PANEL_DB)
        admin_conn.row_factory = sqlite3.Row
        admin_c = admin_conn.cursor()
        admin_c.execute("SELECT * FROM user_settings")
        settings_dict = {row['email']: dict(row) for row in admin_c.fetchall()}
        admin_conn.close()
        
        current_time_ms = int(time.time() * 1000)
        users = []
        
        for inbound in inbounds:
            settings = json.loads(inbound['settings'])
            clients = settings.get('clients', [])
            for client in clients:
                email = client.get('email', '')
                if not email or len(email) != 4: 
                    continue
                
                traffic = traffic_dict.get(email, {'up': 0, 'down': 0, 'inbound_id': inbound['id'], 'last_online': 0})
                upload_gb = traffic['up'] / (1024**3)
                download_gb = traffic['down'] / (1024**3)
                kullanilan_kota = upload_gb + download_gb
                
                user_settings = settings_dict.get(email, {})
                total_usage_ever = user_settings.get('total_usage_ever', 0) or 0
                toplam_kullanim = total_usage_ever + kullanilan_kota
                
                total = client.get('totalGB', 0)
                kota_limit = total / (1024**3) if total > 0 else 0
                paket_tipi = "Sınırsız" if kota_limit == 0 else ("Gold" if kota_limit >= 100 else ("Silver" if kota_limit >= 50 else "Bronze"))
                
                expiry = client.get('expiryTime', 0)
                bitis_tarihi = datetime.fromtimestamp(expiry/1000).strftime('%Y-%m-%d %H:%M:%S') if expiry > 0 else "Süresiz"
                expiry_date_only = datetime.fromtimestamp(expiry/1000).strftime('%Y-%m-%d') if expiry > 0 else ""
                
                is_expired = False
                if expiry > 0:
                    is_expired = expiry < current_time_ms
                
                last_online = traffic.get('last_online', 0)
                if last_online > 0:
                    time_diff_minutes = (current_time_ms - last_online) / 1000 / 60
                    if time_diff_minutes <= 1: 
                        online_status = "online"
                        son_gorunme_kisa = "Aktif"
                    elif time_diff_minutes <= 10: 
                        online_status = "idle"
                        son_gorunme_kisa = f"{int(time_diff_minutes)}dk"
                    elif time_diff_minutes <= 1440: 
                        hours = int(time_diff_minutes / 60)
                        online_status = "offline"
                        son_gorunme_kisa = f"{hours}s"
                    else: 
                        days = int(time_diff_minutes / 1440)
                        online_status = "offline"
                        son_gorunme_kisa = f"{days}g"
                else: 
                    online_status = "never"
                    son_gorunme_kisa = "Yok"
                
                next_payment = user_settings.get('next_payment_date', '') or expiry_date_only
                payment_status = "none"
                days_until_payment = None
                
                if next_payment:
                    try:
                        next_date = datetime.strptime(next_payment, '%Y-%m-%d')
                        today = datetime.now()
                        # Saatleri sıfırla ki sadece gün farkını alalım
                        next_date = next_date.replace(hour=0, minute=0, second=0, microsecond=0)
                        today = today.replace(hour=0, minute=0, second=0, microsecond=0)
                        
                        days_diff = (next_date - today).days
                        days_until_payment = days_diff
                        if days_diff < 0: 
                            payment_status = "overdue"
                        elif days_diff <= 6: 
                            payment_status = "urgent"
                        elif days_diff <= 14: 
                            payment_status = "warning"
                        else: 
                            payment_status = "ok"
                    except: 
                        pass
                
                quota_days = None
                if expiry > 0:
                    try:
                        days_diff = (expiry - current_time_ms) / 1000 / 86400
                        quota_days = max(0, int(days_diff))
                    except:
                        quota_days = None
                
                folder = user_settings.get('folder', 'Tümü')
                
                users.append({
                    'id': client.get('id'),
                    'kullanici_adi': email,
                    'email': email,
                    'paket_tipi': paket_tipi,
                    'sunucu_adi': 'NovaCell-3',
                    'kota_limit_gb': round(kota_limit, 2) if kota_limit > 0 else "Sınırsız",
                    'kullanilan_kota_gb': round(kullanilan_kota, 2),
                    'toplam_kullanim_gb': round(toplam_kullanim, 2),
                    'durum': 'aktif' if client.get('enable') == True else 'pasif',
                    'bitis_tarihi': bitis_tarihi,
                    'is_expired': is_expired,
                    'inbound_id': inbound['id'],
                    'online_status': online_status,
                    'son_gorunme_kisa': son_gorunme_kisa,
                    'monthly_price': user_settings.get('monthly_price', 0),
                    'last_payment_date': user_settings.get('last_payment_date', ''),
                    'next_payment_date': next_payment,
                    'notes': user_settings.get('notes', ''),
                    'payment_status': payment_status,
                    'days_until_payment': days_until_payment,
                    'expiry_date_only': expiry_date_only,
                    'quota_days': quota_days,
                    'quota_reset_date': user_settings.get('quota_reset_date', ''),
                    'folder': folder
                })
        return users
    except Exception as e:
        print(f"Hata: {e}")
        return []

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

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
        session['user_id'] = user['id']
        session['username'] = user['username']
        session.permanent = True
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': 'Hatalı giriş!'}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})

@app.route('/api/check-auth')
def check_auth():
    if 'user_id' in session:
        return jsonify({'authenticated': True, 'username': session.get('username')})
    return jsonify({'authenticated': False}), 401

@app.route('/api/stats')
def get_stats():
    if 'user_id' not in session: 
        return jsonify({'error': 'Unauthorized'}), 401
    users = get_xui_users()
    overdue_count = sum(1 for u in users if u.get('payment_status') == 'overdue')
    total_usage = sum(u['toplam_kullanim_gb'] for u in users)
    return jsonify({
        'total_users': len(users),
        'active_users': sum(1 for u in users if u['durum'] == 'aktif'),
        'passive_users': len(users) - sum(1 for u in users if u['durum'] == 'aktif'),
        'online_users': sum(1 for u in users if u['online_status'] == 'online'),
        'total_usage_gb': round(total_usage, 2),
        'overdue_count': overdue_count
    })

@app.route('/api/users')
def get_users():
    if 'user_id' not in session: 
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify(get_xui_users())

@app.route('/api/toggle-user', methods=['POST'])
def toggle_user():
    if 'user_id' not in session: 
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        data = request.json
        user_email = data.get('email')
        new_enable = data.get('enable')
        
        if not os.path.exists(XUI_DB): 
            return jsonify({'success': False, 'message': 'Database bulunamadı'}), 500
        
        conn = sqlite3.connect(XUI_DB)
        c = conn.cursor()
        
        # 1. INBOUNDS JSON'UNU GUNCELLE
        c.execute("SELECT id, settings FROM inbounds")
        inbounds = c.fetchall()
        
        updated = False
        for inbound in inbounds:
            inbound_id = inbound[0]
            settings = json.loads(inbound[1])
            clients = settings.get('clients', [])
            for client in clients:
                if client.get('email') == user_email:
                    client['enable'] = new_enable
                    updated = True
                    break
            if updated:
                settings['clients'] = clients
                new_settings_json = json.dumps(settings, ensure_ascii=False)
                c.execute("UPDATE inbounds SET settings = ? WHERE id = ?", (new_settings_json, inbound_id))
                break
        
        if not updated:
            conn.close()
            return jsonify({'success': False, 'message': 'Kullanıcı bulunamadı'}), 404
        
        # 2. CLIENT_TRAFFICS'I DE GUNCELLE
        if new_enable:
            c.execute("SELECT expiry_time FROM client_traffics WHERE email = ?", (user_email,))
            result = c.fetchone()
            if result:
                current_expiry = result[0] or 0
                current_time_ms = int(time.time() * 1000)
                
                if current_expiry < current_time_ms:
                    new_expiry = current_time_ms + (30 * 24 * 60 * 60 * 1000)
                    c.execute("UPDATE client_traffics SET expiry_time = ? WHERE email = ?", 
                             (new_expiry, user_email))
                    
                    # Inbounds JSON'undaki expiryTime'ı da güncelle
                    c.execute("SELECT id, settings FROM inbounds")
                    for inbound in c.fetchall():
                        inbound_id = inbound[0]
                        settings = json.loads(inbound[1])
                        clients = settings.get('clients', [])
                        changed = False
                        for client in clients:
                            if client.get('email') == user_email:
                                client['expiryTime'] = new_expiry
                                changed = True
                                break
                        if changed:
                            settings['clients'] = clients
                            new_json = json.dumps(settings, ensure_ascii=False)
                            c.execute("UPDATE inbounds SET settings = ? WHERE id = ?", (new_json, inbound_id))
                            break
                    
                    print(f"Kullanıcı {user_email} aktif edildi ve süre 30 gün uzatıldı")
        
        # 3. COMMIT VE CLOSE
        conn.commit()
        conn.close()
        
        # 4. X-UI CLI İLE RESTART (TEK KOMUT YETERLİ!)
        print(f"Kullanıcı {user_email} durumu değişti, x-ui restart ediliyor...")
        os.system('x-ui restart')
        
        # 5. BİRAZ BEKLE Kİ RESTART TAMAMLANSIN
        time.sleep(3)
        
        return jsonify({'success': True, 'message': 'Durum güncellendi ve x-ui yenilendi!'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/update-user-settings', methods=['POST'])
def update_user_settings():
    if 'user_id' not in session: 
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        data = request.json
        email = data.get('email')
        
        conn = sqlite3.connect(PANEL_DB)
        c = conn.cursor()
        
        expiry_or_payment = data.get('expiry_date') or data.get('next_payment_date')
        folder = data.get('folder', 'Tümü')
        
        quota_reset_date = None
        if data.get('quota') is not None:
            quota_reset_date = datetime.now().strftime('%Y-%m-%d')
        
        c.execute("SELECT * FROM user_settings WHERE email = ?", (email,))
        existing = c.fetchone()
        
        if existing:
            if quota_reset_date:
                c.execute("""UPDATE user_settings 
                             SET monthly_price = ?, next_payment_date = ?, notes = ?, folder = ?, 
                                 quota_reset_date = ?, updated_at = CURRENT_TIMESTAMP
                             WHERE email = ?""",
                          (data.get('monthly_price', 0), expiry_or_payment, data.get('notes', ''), 
                           folder, quota_reset_date, email))
            else:
                c.execute("""UPDATE user_settings 
                             SET monthly_price = ?, next_payment_date = ?, notes = ?, folder = ?, 
                                 updated_at = CURRENT_TIMESTAMP
                             WHERE email = ?""",
                          (data.get('monthly_price', 0), expiry_or_payment, data.get('notes', ''), 
                           folder, email))
        else:
            if quota_reset_date:
                c.execute("""INSERT INTO user_settings (email, monthly_price, next_payment_date, notes, folder, quota_reset_date)
                             VALUES (?, ?, ?, ?, ?, ?)""",
                          (email, data.get('monthly_price', 0), expiry_or_payment, data.get('notes', ''), 
                           folder, quota_reset_date))
            else:
                c.execute("""INSERT INTO user_settings (email, monthly_price, next_payment_date, notes, folder)
                             VALUES (?, ?, ?, ?, ?)""",
                          (email, data.get('monthly_price', 0), expiry_or_payment, data.get('notes', ''), folder))
        
        conn.commit()
        conn.close()
        
        # --- X-UI TARAFINI GUNCELLE (KRITIK KISIM!) ---
        if data.get('quota') is not None or data.get('expiry_date'):
            xui_conn = sqlite3.connect(XUI_DB)
            xui_c = xui_conn.cursor()
            xui_c.execute("SELECT id, settings FROM inbounds")
            inbounds = xui_c.fetchall()
            
            for inbound in inbounds:
                inbound_id = inbound[0]
                settings = json.loads(inbound[1])
                clients = settings.get('clients', [])
                inbound_changed = False
                
                for client in clients:
                    if client.get('email') == email:
                        
                        # KOTA AYARLA
                        if data.get('quota') is not None:
                            quota_gb = float(data.get('quota'))
                            if quota_gb == 0: 
                                client['totalGB'] = 0
                            else: 
                                client['totalGB'] = int(quota_gb * 1024 * 1024 * 1024)
                            
                            # KULLANICIYI AKTIF ET!
                            client['enable'] = True
                            
                            inbound_changed = True
                            
                            # Kotayı sıfırla
                            reset_user_quota(email)
                        
                        # TARIH AYARLA VE SYNC ET
                        if data.get('expiry_date'):
                            try:
                                expiry_dt = datetime.strptime(data.get('expiry_date'), '%Y-%m-%d')
                                expiry_dt = expiry_dt.replace(hour=23, minute=59, second=59)
                                new_expiry_ms = int(expiry_dt.timestamp() * 1000)
                                
                                # 1. JSON Ayarı
                                client['expiryTime'] = new_expiry_ms
                                
                                # 2. Kullanıcıyı aktif et
                                client['enable'] = True
                                
                                inbound_changed = True
                                
                                # 3. Canlı Tablo Ayarı (MOTOR ICIN ONEMLI)
                                sync_xui_expiry(email, new_expiry_ms)
                                
                            except Exception as ex: 
                                print(f"Tarih convert hatasi: {ex}")
                                pass
                        break
                
                if inbound_changed:
                    settings['clients'] = clients
                    new_settings_json = json.dumps(settings, ensure_ascii=False)
                    
                    # KRITIK: INBOUNDS TABLOSUNU GUNCELLE!
                    xui_c.execute("UPDATE inbounds SET settings = ? WHERE id = ?", 
                                 (new_settings_json, inbound_id))
                    print(f"✅ Inbound {inbound_id} güncellendi: {email}")
            
            xui_conn.commit()
            xui_conn.close()
            
            # X-UI CLI İLE RESTART!
            print(f"Kullanıcı {email} ayarları güncellendi, x-ui restart ediliyor...")
            os.system('x-ui restart')
            time.sleep(3)
        
        return jsonify({'success': True, 'message': 'Ayarlar güncellendi ve x-ui yenilendi!'})
    except Exception as e:
        print(f"❌ Ayar güncelleme hatası: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/move-to-folder', methods=['POST'])
def move_to_folder():
    if 'user_id' not in session: 
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        data = request.json
        email = data.get('email')
        new_folder = data.get('folder')
        
        valid_folders = ['Tümü', 'Superbox', 'AX', 'GSM', 'ÖZEL', 'KLASÖR-1', 'KLASÖR-2', 'KLASÖR-3', 'KLASÖR-4']
        if new_folder not in valid_folders:
            return jsonify({'success': False, 'message': 'Geçersiz klasör adı'}), 400
        
        conn = sqlite3.connect(PANEL_DB)
        c = conn.cursor()
        c.execute("SELECT email FROM user_settings WHERE email = ?", (email,))
        exists = c.fetchone()
        
        if exists:
            c.execute("UPDATE user_settings SET folder = ?, updated_at = CURRENT_TIMESTAMP WHERE email = ?", (new_folder, email))
        else:
            c.execute("INSERT INTO user_settings (email, folder, monthly_price, notes) VALUES (?, ?, 0, '')", (email, new_folder))
        
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': f'Kullanıcı {new_folder} klasörüne taşındı!'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/update-user-note', methods=['POST'])
def update_user_note():
    if 'user_id' not in session: 
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        data = request.json
        email = data.get('email')
        note = data.get('note', '')
        
        conn = sqlite3.connect(PANEL_DB)
        c = conn.cursor()
        c.execute("SELECT * FROM user_settings WHERE email = ?", (email,))
        existing = c.fetchone()
        
        if existing:
            c.execute("UPDATE user_settings SET notes = ?, updated_at = CURRENT_TIMESTAMP WHERE email = ?", (note, email))
        else:
            c.execute("INSERT INTO user_settings (email, notes) VALUES (?, ?)", (email, note))
        
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Not güncellendi!'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/add-payment', methods=['POST'])
def add_payment():
    if 'user_id' not in session: 
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        data = request.json
        email = data.get('email')
        amount = data.get('amount')
        payment_date = data.get('payment_date')
        payment_method = data.get('payment_method', '')
        notes = data.get('notes', '')
        
        conn = sqlite3.connect(PANEL_DB)
        c = conn.cursor()
        
        c.execute("""INSERT INTO payment_history (email, amount, payment_date, payment_method, notes)
                     VALUES (?, ?, ?, ?, ?)""",
                  (email, amount, payment_date, payment_method, notes))
        
        c.execute("SELECT next_payment_date FROM user_settings WHERE email = ?", (email,))
        existing_record = c.fetchone()
        
        next_payment = None
        quota_reset_date = None

        if existing_record and existing_record[0]:
            try:
                current_next_payment = datetime.strptime(existing_record[0], '%Y-%m-%d')
                payment_day = current_next_payment.day
                
                next_month = current_next_payment.month + 1
                next_year = current_next_payment.year
                
                if next_month > 12:
                    next_month = 1
                    next_year += 1
                
                max_day = calendar.monthrange(next_year, next_month)[1]
                safe_day = min(payment_day, max_day)
                
                next_payment = datetime(next_year, next_month, safe_day).strftime('%Y-%m-%d')
                quota_reset_date = existing_record[0]
            except:
                payment_dt = datetime.strptime(payment_date, '%Y-%m-%d')
                next_payment = (payment_dt + timedelta(days=30)).strftime('%Y-%m-%d')
                quota_reset_date = payment_date
        else:
            try:
                payment_dt = datetime.strptime(payment_date, '%Y-%m-%d')
                next_payment = (payment_dt + timedelta(days=30)).strftime('%Y-%m-%d')
                quota_reset_date = payment_date
            except:
                next_payment = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')
                quota_reset_date = datetime.now().strftime('%Y-%m-%d')
        
        c.execute("SELECT * FROM user_settings WHERE email = ?", (email,))
        if c.fetchone():
            c.execute("""UPDATE user_settings 
                         SET last_payment_date = ?, next_payment_date = ?, quota_reset_date = ?, 
                             updated_at = CURRENT_TIMESTAMP
                         WHERE email = ?""",
                      (payment_date, next_payment, quota_reset_date, email))
        else:
            c.execute("""INSERT INTO user_settings (email, last_payment_date, next_payment_date, quota_reset_date)
                         VALUES (?, ?, ?, ?)""",
                      (email, payment_date, next_payment, quota_reset_date))
        
        conn.commit()
        conn.close()
        
        reset_user_quota(email)
        
        # --- X-UI MOTORUNU DA UZAT VE AKTIF ET ---
        if next_payment:
            try:
                expiry_dt = datetime.strptime(next_payment, '%Y-%m-%d')
                expiry_dt = expiry_dt.replace(hour=23, minute=59, second=59)
                new_expiry_ms = int(expiry_dt.timestamp() * 1000)
                
                # 1. Sync fonksiyonunu çağır (client_traffics için)
                sync_xui_expiry(email, new_expiry_ms)
                
                # 2. Inbounds JSON'ı da güncelle
                xui_conn = sqlite3.connect(XUI_DB)
                xui_c = xui_conn.cursor()
                xui_c.execute("SELECT id, settings FROM inbounds")
                for row in xui_c.fetchall():
                    inbound_id = row[0]
                    settings = json.loads(row[1])
                    clients = settings.get('clients', [])
                    changed = False
                    for client in clients:
                        if client.get('email') == email:
                            client['expiryTime'] = new_expiry_ms
                            client['enable'] = True # Süre uzadıysa kullanıcıyı aktif et
                            changed = True
                            break
                    if changed:
                        settings['clients'] = clients
                        new_json = json.dumps(settings, ensure_ascii=False)
                        xui_c.execute("UPDATE inbounds SET settings = ? WHERE id = ?", (new_json, inbound_id))
                xui_conn.commit()
                xui_conn.close()
                
                # 3. X-UI CLI ile restart
                print(f"Ödeme sonrası x-ui restart ediliyor: {email} -> {next_payment}")
                os.system('x-ui restart')
                time.sleep(3)
                
            except Exception as e:
                print(f"Ödeme sonrası süre uzatma hatası: {e}")
        
        return jsonify({'success': True, 'message': 'Ödeme kaydedildi, kota sıfırlandı, süre uzatıldı ve kullanıcı aktif edildi!'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/payment-history/<email>')
def get_payment_history(email):
    if 'user_id' not in session: 
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        conn = sqlite3.connect(PANEL_DB)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM payment_history WHERE email = ? ORDER BY payment_date DESC", (email,))
        history = [dict(row) for row in c.fetchall()]
        conn.close()
        return jsonify(history)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/notifications')
def get_notifications():
    if 'user_id' not in session: 
        return jsonify({'error': 'Unauthorized'}), 401
    users = get_xui_users()
    notifications = []
    
    for user in users:
        if user.get('payment_status') == 'overdue':
            notifications.append({
                'type': 'payment_overdue',
                'user': user['kullanici_adi'],
                'message': f"Ödeme {abs(user['days_until_payment'])} gün gecikti!",
                'priority': 'high'
            })
        elif user.get('payment_status') == 'urgent':
            notifications.append({
                'type': 'payment_urgent',
                'user': user['kullanici_adi'],
                'message': f"{user['days_until_payment']} gün içinde ödeme",
                'priority': 'medium'
            })
        elif user.get('payment_status') == 'warning':
            notifications.append({
                'type': 'payment_warning',
                'user': user['kullanici_adi'],
                'message': f"{user['days_until_payment']} gün içinde ödeme",
                'priority': 'low'
            })
        
        if user['kota_limit_gb'] != "Sınırsız":
            usage_percent = (user['kullanilan_kota_gb'] / user['kota_limit_gb']) * 100
            if usage_percent >= 90:
                notifications.append({
                    'type': 'quota_high',
                    'user': user['kullanici_adi'],
                    'message': f"Kota %{int(usage_percent)} doldu",
                    'priority': 'medium'
                })
        
        if user.get('quota_days') is not None and user['quota_days'] <= 3:
            notifications.append({
                'type': 'quota_reset_soon',
                'user': user['kullanici_adi'],
                'message': f"Kota {user['quota_days']} gün içinde sıfırlanacak",
                'priority': 'low'
            })
        
        if user.get('is_expired'):
            notifications.append({
                'type': 'expired',
                'user': user['kullanici_adi'],
                'message': "Kullanım süresi dolmuş!",
                'priority': 'high'
            })
    
    return jsonify(notifications)

def reset_user_quota(email):
    try:
        if not os.path.exists(XUI_DB): 
            return False
        
        conn = sqlite3.connect(XUI_DB)
        c = conn.cursor()
        c.execute("SELECT up, down FROM client_traffics WHERE email = ?", (email,))
        result = c.fetchone()
        
        if result:
            current_usage_bytes = (result[0] or 0) + (result[1] or 0)
            current_usage_gb = current_usage_bytes / (1024**3)
            
            admin_conn = sqlite3.connect(PANEL_DB)
            admin_c = admin_conn.cursor()
            admin_c.execute("SELECT total_usage_ever FROM user_settings WHERE email = ?", (email,))
            admin_result = admin_c.fetchone()
            
            if admin_result:
                new_total = (admin_result[0] or 0) + current_usage_gb
                admin_c.execute("UPDATE user_settings SET total_usage_ever = ? WHERE email = ?", 
                              (new_total, email))
            else:
                admin_c.execute("INSERT INTO user_settings (email, total_usage_ever) VALUES (?, ?)", 
                              (email, current_usage_gb))
            
            admin_conn.commit()
            admin_conn.close()
        
        c.execute("UPDATE client_traffics SET up = 0, down = 0 WHERE email = ?", (email,))
        conn.commit()
        conn.close()
        
        admin_conn = sqlite3.connect(PANEL_DB)
        admin_c = admin_conn.cursor()
        admin_c.execute("INSERT INTO quota_reset_log (email, reset_date, reset_type) VALUES (?, ?, ?)",
                       (email, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'manual'))
        admin_conn.commit()
        admin_conn.close()
        
        return True
    except Exception as e:
        print(f"Kota sıfırlama hatası: {e}")
        return False

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=8888, debug=False)
