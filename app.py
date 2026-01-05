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

def get_db_connection(db_path):
    try:
        conn = sqlite3.connect(db_path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try: conn.execute("PRAGMA journal_mode=WAL;") 
        except: pass
        return conn
    except Exception as e:
        print(f"DB Bağlantı hatası ({db_path}): {e}")
        return None

def init_db():
    conn = get_db_connection(PANEL_DB)
    if not conn: return
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS admin_users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_settings (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, monthly_price REAL DEFAULT 0, last_payment_date TEXT, next_payment_date TEXT, notes TEXT, quota_start_date TEXT, quota_reset_date TEXT, total_usage_ever REAL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, folder TEXT DEFAULT 'Tümü')''')
    c.execute('''CREATE TABLE IF NOT EXISTS payment_history (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, amount REAL NOT NULL, payment_date TEXT NOT NULL, payment_method TEXT, notes TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS quota_reset_log (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, reset_date TEXT NOT NULL, reset_type TEXT DEFAULT 'auto', created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')

    try: c.execute("SELECT quota_reset_date FROM user_settings LIMIT 1")
    except sqlite3.OperationalError:
        try: c.execute("ALTER TABLE user_settings ADD COLUMN quota_reset_date INTEGER DEFAULT 0")
        except: pass

    c.execute("SELECT COUNT(*) FROM admin_users WHERE username = 'novacell'")
    if c.fetchone()[0] == 0:
        hashed = bcrypt.hashpw('NovaCell25Hakki'.encode('utf-8'), bcrypt.gensalt())
        c.execute("INSERT INTO admin_users (username, password_hash) VALUES (?, ?)", ('novacell', hashed))
    conn.close()

# --- TRAFİK KAYDINI SİLME (RESET) ---
def hard_reset_user_traffic(email):
    """
    Bu fonksiyon trafiği 0 yapmak yerine SATIRI SİLER.
    Böylece X-UI müşteriyi 'yeni' sanar ve anında trafiği açar.
    """
    try:
        # Önce mevcut kullanımı alıp arşive ekleyelim
        conn_xui = get_db_connection(XUI_DB)
        c_xui = conn_xui.cursor()
        c_xui.execute("SELECT up, down FROM client_traffics WHERE email = ?", (email,))
        res = c_xui.fetchone()
        
        current_gb = 0
        if res: current_gb = ((res[0] or 0) + (res[1] or 0)) / (1024**3)

        # Admin paneline arşivi kaydet
        admin_conn = get_db_connection(PANEL_DB)
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
        admin_conn.close()

        # --- KRİTİK HAMLE: SATIRI SİL ---
        # Update yapmak yerine DELETE yapıyoruz.
        c_xui.execute("DELETE FROM client_traffics WHERE email = ?", (email,))
        print(f"♻️ {email} trafik kaydı tamamen silindi (Hard Reset).")
        
        conn_xui.close()
        return True
    except Exception as e:
        print(f"Hard Reset Hatası: {e}")
        return False

# --- MONITOR (SADECE CEZA KESER) ---
def monitor_loop():
    print("✅ Bekçi Devrede (Otomatik Açma İPTAL EDİLDİ - Sadece Kapatır).")
    while True:
        try:
            if not os.path.exists(XUI_DB):
                time.sleep(5); continue
            
            conn = get_db_connection(XUI_DB)
            if not conn:
                time.sleep(5); continue

            c = conn.cursor()
            c.execute("SELECT id, settings FROM inbounds")
            inbounds = c.fetchall()
            
            try:
                c.execute("SELECT email, up, down FROM client_traffics")
                traffic_rows = c.fetchall()
                traffic_dict = {row['email']: {'up': row['up'] or 0, 'down': row['down'] or 0} for row in traffic_rows}
            except: traffic_dict = {}
            
            current_time = int(time.time() * 1000)
            db_modified = False
            banned_users = []
            
            for inbound in inbounds:
                inbound_id = inbound['id']
                settings = json.loads(inbound['settings'])
                clients = settings.get('clients', [])
                inbound_mod = False
                
                for client in clients:
                    # Sadece AKTİF olanları kontrol et. Pasiflere dokunma (Manuel kapatılmış olabilir).
                    if client.get('enable') == True:
                        email = client.get('email')
                        
                        # 1. KOTA KONTROLÜ
                        total_gb = client.get('totalGB', 0)
                        if total_gb > 0:
                            tr = traffic_dict.get(email, {'up': 0, 'down': 0})
                            used = (tr['up'] + tr['down'])
                            if used >= total_gb:
                                client['enable'] = False
                                inbound_mod = True
                                db_modified = True
                                banned_users.append(f"{email} (Kota)")
                        
                        # 2. SÜRE KONTROLÜ
                        expiry = client.get('expiryTime', 0)
                        if expiry > 0 and expiry < current_time:
                            client['enable'] = False
                            inbound_mod = True
                            db_modified = True
                            banned_users.append(f"{email} (Süre)")

                if inbound_mod:
                    c.execute("UPDATE inbounds SET settings = ? WHERE id = ?", (json.dumps(settings), inbound_id))
            
            if db_modified:
                print(f"🚫 [MONITOR] Engellendi: {', '.join(banned_users)}")
                print("🔄 [MONITOR] X-UI Restart Ediliyor...")
                os.system("systemctl restart x-ui")
            
            conn.close()
        except Exception as e: print(f"Monitor: {e}")
        time.sleep(5)

# --- API ---
@app.route('/')
def index(): return send_from_directory('.', 'index.html')

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    conn = get_db_connection(PANEL_DB)
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

def get_xui_users():
    try:
        if not os.path.exists(XUI_DB): return []
        conn = get_db_connection(XUI_DB)
        if not conn: return []
        c = conn.cursor()
        c.execute("SELECT id, settings FROM inbounds")
        inbounds = c.fetchall()
        
        traffic_dict = {}
        try:
            c.execute("SELECT email, up, down, inbound_id, last_online FROM client_traffics")
            for row in c.fetchall():
                traffic_dict[row['email']] = {'up':row['up'], 'down':row['down'], 'inbound_id':row['inbound_id'], 'last_online':row['last_online']}
        except: pass
        conn.close()
        
        admin_conn = get_db_connection(PANEL_DB)
        settings_dict = {}
        if admin_conn:
            ac = admin_conn.cursor()
            ac.execute("SELECT * FROM user_settings")
            settings_dict = {row['email']: dict(row) for row in ac.fetchall()}
            admin_conn.close()
        
        current_time_ms = int(time.time() * 1000)
        users = []
        for inbound in inbounds:
            try: settings = json.loads(inbound['settings']); clients = settings.get('clients', [])
            except: continue
            for client in clients:
                email = client.get('email', '')
                if not email: continue
                traffic = traffic_dict.get(email, {'up':0,'down':0,'inbound_id':inbound['id'],'last_online':0})
                used = (traffic['up'] + traffic['down']) / (1024**3)
                user_set = settings_dict.get(email, {})
                total_gb = client.get('totalGB', 0)
                kota_limit = total_gb / (1024**3) if total_gb > 0 else 0
                expiry = client.get('expiryTime', 0)
                
                last_online = traffic.get('last_online', 0)
                online_stat="never"; last_seen="Yok"
                if last_online>0:
                    diff=(current_time_ms-last_online)/60000
                    if diff<=2: online_stat="online"; last_seen="Aktif"
                    elif diff<=60: online_stat="idle"; last_seen=f"{int(diff)} dk"
                    else: online_stat="offline"; last_seen=f"{int(diff/60)} sa"

                users.append({
                    'id': client.get('id'), 'email': email, 'kullanici_adi': email,
                    'paket_tipi': 'Sınırsız' if kota_limit==0 else f"{int(kota_limit)} GB",
                    'sunucu_adi': SERVER_NAME,
                    'kota_limit_gb': "Sınırsız" if kota_limit==0 else round(kota_limit,2),
                    'kullanilan_kota_gb': round(used, 2),
                    'toplam_kullanim_gb': round(user_set.get('total_usage_ever',0)+used, 2),
                    'durum': 'aktif' if client.get('enable') else 'pasif',
                    'online_status': online_stat, 'son_gorunme_kisa': last_seen,
                    'monthly_price': user_set.get('monthly_price', 0),
                    'notes': user_set.get('notes', ''), 'folder': user_set.get('folder', 'Tümü'),
                    'payment_status': 'ok', 'days_until_payment': None, # Basitleştirildi
                    'next_payment_date': user_set.get('next_payment_date', ''),
                    'quota_days': int((expiry-current_time_ms)/86400000) if expiry>0 else None
                })
        return users
    except: return []

@app.route('/api/stats')
def get_stats_route():
    if 'user_id' not in session: return jsonify({'error'}), 401
    u = get_xui_users()
    return jsonify({'total_users':len(u), 'active_users':sum(1 for x in u if x['durum']=='aktif'), 'total_usage_gb':round(sum(x['toplam_kullanim_gb'] for x in u),2), 'overdue_count':0})

@app.route('/api/users')
def get_users_route():
    if 'user_id' not in session: return jsonify({'error'}), 401
    return jsonify(get_xui_users())

# --- GÜNCELLEME (STOP -> UPDATE -> DELETE TRAFFIC -> START) ---
@app.route('/api/update-user-settings', methods=['POST'])
def update_user_settings():
    if 'user_id' not in session: return jsonify({'error'}), 401
    try:
        data=request.json; email=data.get('email')
        conn=get_db_connection(PANEL_DB); c=conn.cursor()
        # ... Admin DB Update işlemleri (Kısaltıldı, değişmedi) ...
        c.execute("SELECT * FROM user_settings WHERE email=?",(email,))
        if c.fetchone(): c.execute("UPDATE user_settings SET monthly_price=?, next_payment_date=?, notes=?, folder=? WHERE email=?", (data.get('monthly_price',0), data.get('next_payment_date'), data.get('notes'), data.get('folder'), email))
        else: c.execute("INSERT INTO user_settings (email, monthly_price, notes, folder) VALUES (?,?,?,?)", (email, 0, '', data.get('folder')))
        conn.close()

        if data.get('quota') is not None or data.get('expiry_date'):
            print(f"🛑 [UPDATE] {email} için X-UI durduruluyor...")
            os.system("systemctl stop x-ui")
            time.sleep(2)
            
            try:
                x_conn = get_db_connection(XUI_DB); xc = x_conn.cursor()
                xc.execute("SELECT id, settings FROM inbounds")
                inbounds = xc.fetchall()
                
                reset_traffic = False
                new_ms = None
                if data.get('expiry_date'):
                    new_ms = int(datetime.strptime(data.get('expiry_date'), '%Y-%m-%d').replace(hour=23,minute=59).timestamp()*1000)

                for row in inbounds:
                    settings = json.loads(row[1]); clients = settings.get('clients', [])
                    mod = False
                    for cl in clients:
                        if cl.get('email') == email:
                            cl['enable'] = True # ZORLA AÇ
                            mod = True
                            if data.get('quota') is not None:
                                q = float(data.get('quota'))
                                cl['totalGB'] = 0 if q==0 else int(q*1024**3)
                                reset_traffic = True
                            if new_ms: cl['expiryTime'] = new_ms
                            break
                    if mod: xc.execute("UPDATE inbounds SET settings=? WHERE id=?", (json.dumps(settings), row[0]))
                
                x_conn.close() # Inbounds bitti
                
                # --- TRAFİĞİ SİLME İŞLEMİ ---
                if reset_traffic:
                    hard_reset_user_traffic(email)
                
            except Exception as e: print(e)
            
            print(f"🚀 [UPDATE] X-UI Başlatılıyor...")
            os.system("systemctl start x-ui")

        return jsonify({'success': True})
    except Exception as e: return jsonify({'success':False, 'message':str(e)}), 500

@app.route('/api/toggle-user', methods=['POST'])
def toggle_user():
    if 'user_id' not in session: return jsonify({'error'}), 401
    try:
        data=request.json; email=data.get('email'); enable=data.get('enable')
        
        print("🛑 Toggle işlemi için X-UI Durduruluyor...")
        os.system("systemctl stop x-ui")
        time.sleep(2)
        
        x_conn = get_db_connection(XUI_DB); xc = x_conn.cursor()
        xc.execute("SELECT id, settings FROM inbounds")
        for row in xc.fetchall():
            s = json.loads(row[1]); c = s.get('clients', [])
            mod = False
            for cl in c:
                if cl.get('email') == email:
                    cl['enable'] = enable
                    mod = True
                    break
            if mod: xc.execute("UPDATE inbounds SET settings=? WHERE id=?", (json.dumps(s), row[0]))
        x_conn.close()
        
        print("🚀 X-UI Başlatılıyor...")
        os.system("systemctl start x-ui")
        return jsonify({'success': True})
    except Exception as e: return jsonify({'success':False}), 500

@app.route('/api/add-payment', methods=['POST'])
def add_payment():
    if 'user_id' not in session: return jsonify({'error'}), 401
    try:
        data=request.json; email=data.get('email')
        # ... Admin DB (Aynı) ...
        conn=get_db_connection(PANEL_DB); c=conn.cursor()
        c.execute("INSERT INTO payment_history (email, amount, payment_date) VALUES (?,?,?)", (email, data.get('amount'), data.get('payment_date')))
        
        try: np = (datetime.strptime(data.get('payment_date'), '%Y-%m-%d')+timedelta(days=30)).strftime('%Y-%m-%d')
        except: np = (datetime.now()+timedelta(days=30)).strftime('%Y-%m-%d')
        
        c.execute("UPDATE user_settings SET next_payment_date=?, updated_at=CURRENT_TIMESTAMP WHERE email=?", (np, email))
        conn.close()

        # Stop -> Update -> Start
        print("🛑 Ödeme işlemi için X-UI Durduruluyor...")
        os.system("systemctl stop x-ui")
        time.sleep(2)
        
        try:
            new_ms = int(datetime.strptime(np, '%Y-%m-%d').replace(hour=23,minute=59).timestamp()*1000)
            x_conn = get_db_connection(XUI_DB); xc = x_conn.cursor()
            xc.execute("SELECT id, settings FROM inbounds")
            for row in xc.fetchall():
                s = json.loads(row[1]); cl = s.get('clients', [])
                mod = False
                for c in cl:
                    if c.get('email') == email:
                        c['enable'] = True; c['expiryTime'] = new_ms; mod = True
                        break
                if mod: xc.execute("UPDATE inbounds SET settings=? WHERE id=?", (json.dumps(s), row[0]))
            x_conn.close()
            
            # Trafiği SİL ve Arşivle
            hard_reset_user_traffic(email)
            
        except Exception as e: print(e)
        
        print("🚀 X-UI Başlatılıyor...")
        os.system("systemctl start x-ui")
        return jsonify({'success': True})
    except Exception as e: return jsonify({'success':False}), 500

# Diğer yardımcı route'lar (Not, Klasör, History, Notif) - Kısaltıldı, mantık aynı
@app.route('/api/update-user-note', methods=['POST'])
def un(): 
    d=request.json; c=get_db_connection(PANEL_DB); cur=c.cursor()
    cur.execute("SELECT id FROM user_settings WHERE email=?",(d.get('email'),))
    if cur.fetchone(): cur.execute("UPDATE user_settings SET notes=? WHERE email=?",(d.get('note'),d.get('email')))
    else: cur.execute("INSERT INTO user_settings (email, notes) VALUES (?,?)",(d.get('email'),d.get('note')))
    c.close(); return jsonify({'success':True})

@app.route('/api/move-to-folder', methods=['POST'])
def mf(): 
    d=request.json; c=get_db_connection(PANEL_DB); cur=c.cursor()
    cur.execute("SELECT id FROM user_settings WHERE email=?",(d.get('email'),))
    if cur.fetchone(): cur.execute("UPDATE user_settings SET folder=? WHERE email=?",(d.get('folder'),d.get('email')))
    else: cur.execute("INSERT INTO user_settings (email, folder) VALUES (?,?)",(d.get('email'),d.get('folder')))
    c.close(); return jsonify({'success':True})

@app.route('/api/payment-history/<email>')
def ph(email):
    c=get_db_connection(PANEL_DB); cur=c.cursor()
    cur.execute("SELECT * FROM payment_history WHERE email=? ORDER BY payment_date DESC",(email,))
    r=[dict(row) for row in cur.fetchall()]; c.close(); return jsonify(r)

@app.route('/api/notifications')
def notif(): return jsonify([]) # Basit return

if __name__ == '__main__':
    init_db()
    t = threading.Thread(target=monitor_loop)
    t.daemon = True
    t.start()
    app.run(host='0.0.0.0', port=8888, debug=False)
