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

# --- YARDIMCI: VERİTABANI BAĞLANTISI (Kod Tasarrufu Sağlayan Kısım) ---
def get_db_connection(db_path):
    try:
        conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try: conn.execute("PRAGMA journal_mode=WAL;") 
        except: pass
        return conn
    except Exception as e:
        print(f"DB Hatası ({db_path}): {e}")
        return None

def init_db():
    conn = get_db_connection(PANEL_DB)
    if not conn: return
    c = conn.cursor()
    # Tablolar
    c.execute('''CREATE TABLE IF NOT EXISTS admin_users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_settings (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, monthly_price REAL DEFAULT 0, last_payment_date TEXT, next_payment_date TEXT, notes TEXT, quota_start_date TEXT, quota_reset_date TEXT, total_usage_ever REAL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, folder TEXT DEFAULT 'Tümü')''')
    c.execute('''CREATE TABLE IF NOT EXISTS payment_history (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, amount REAL NOT NULL, payment_date TEXT NOT NULL, payment_method TEXT, notes TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS quota_reset_log (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, reset_date TEXT NOT NULL, reset_type TEXT DEFAULT 'auto', created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    
    # Migration
    try: c.execute("SELECT quota_reset_date FROM user_settings LIMIT 1")
    except: 
        try: c.execute("ALTER TABLE user_settings ADD COLUMN quota_reset_date INTEGER DEFAULT 0")
        except: pass

    # Admin
    c.execute("SELECT COUNT(*) FROM admin_users WHERE username = 'novacell'")
    if c.fetchone()[0] == 0:
        hashed = bcrypt.hashpw('NovaCell25Hakki'.encode('utf-8'), bcrypt.gensalt())
        c.execute("INSERT INTO admin_users (username, password_hash) VALUES (?, ?)", ('novacell', hashed))
    conn.close()

# --- KRİTİK: TRAFİĞİ KÖKTEN SİLME (HARD RESET) ---
def hard_reset_user_traffic(email):
    try:
        # 1. YEDEKLE
        conn_xui = get_db_connection(XUI_DB)
        res = conn_xui.execute("SELECT up, down FROM client_traffics WHERE email = ?", (email,)).fetchone()
        current_gb = ((res['up'] or 0) + (res['down'] or 0)) / (1024**3) if res else 0
        conn_xui.close()

        admin_conn = get_db_connection(PANEL_DB)
        row = admin_conn.execute("SELECT total_usage_ever FROM user_settings WHERE email = ?", (email,)).fetchone()
        new_total = (row['total_usage_ever'] or 0) + current_gb if row else current_gb
        
        if row: admin_conn.execute("UPDATE user_settings SET total_usage_ever = ? WHERE email = ?", (new_total, email))
        else: admin_conn.execute("INSERT INTO user_settings (email, total_usage_ever) VALUES (?, ?)", (email, current_gb))
        
        admin_conn.execute("INSERT INTO quota_reset_log (email, reset_date, reset_type) VALUES (?, ?, ?)", (email, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'manual'))
        admin_conn.close()

        # 2. SİL (DELETE)
        conn_xui = get_db_connection(XUI_DB)
        conn_xui.execute("DELETE FROM client_traffics WHERE email = ?", (email,))
        conn_xui.close()
        print(f"☢️ {email} trafiği silindi.")
        return True
    except Exception as e:
        print(f"Hard Reset Hatası: {e}")
        return False

# --- BEKÇİ (MONITOR) ---
def monitor_loop():
    print("✅ Bekçi Aktif.")
    while True:
        try:
            if not os.path.exists(XUI_DB): time.sleep(5); continue
            conn = get_db_connection(XUI_DB)
            if not conn: time.sleep(5); continue

            inbounds = conn.execute("SELECT id, settings FROM inbounds").fetchall()
            traffic_rows = conn.execute("SELECT email, up, down FROM client_traffics").fetchall()
            traffic = {r['email']: (r['up']+r['down']) for r in traffic_rows}
            
            curr_time = int(time.time() * 1000)
            mod = False
            
            for row in inbounds:
                sets = json.loads(row['settings'])
                clients = sets.get('clients', [])
                i_mod = False
                for c in clients:
                    if c.get('enable'):
                        # Kota Kontrol
                        if c.get('totalGB', 0) > 0 and traffic.get(c['email'], 0) >= c['totalGB']:
                            c['enable'] = False; i_mod = True; mod = True
                            print(f"⛔ Kota Doldu: {c['email']}")
                        # Süre Kontrol
                        elif c.get('expiryTime', 0) > 0 and c['expiryTime'] < curr_time:
                            c['enable'] = False; i_mod = True; mod = True
                            print(f"⛔ Süre Bitti: {c['email']}")
                
                if i_mod:
                    conn.execute("UPDATE inbounds SET settings = ? WHERE id = ?", (json.dumps(sets), row['id']))
            
            if mod:
                print("🔄 X-UI Restart...")
                os.system("systemctl restart x-ui")
            
            conn.close()
        except: pass
        time.sleep(5)

# --- API ---
@app.route('/')
def index(): return send_from_directory('.', 'index.html')

@app.route('/api/login', methods=['POST'])
def login():
    d = request.json
    c = get_db_connection(PANEL_DB)
    u = c.execute("SELECT * FROM admin_users WHERE username = ?", (d.get('username'),)).fetchone()
    c.close()
    if u and bcrypt.checkpw(d.get('password').encode(), u['password_hash']):
        session['user_id'] = u['id']; session['username'] = u['username']; session.permanent = True
        return jsonify({'success': True})
    return jsonify({'success': False}), 401

@app.route('/api/logout', methods=['POST'])
def logout(): session.clear(); return jsonify({'success': True})

@app.route('/api/check-auth')
def check_auth(): return jsonify({'authenticated': 'user_id' in session})

# --- KULLANICI LİSTESİ ---
def get_xui_users():
    try:
        if not os.path.exists(XUI_DB): return []
        c = get_db_connection(XUI_DB)
        inbounds = c.execute("SELECT id, settings FROM inbounds").fetchall()
        traffic_data = {r['email']: r for r in c.execute("SELECT email, up, down, last_online FROM client_traffics").fetchall()}
        c.close()
        
        ac = get_db_connection(PANEL_DB)
        user_sets = {r['email']: dict(r) for r in ac.execute("SELECT * FROM user_settings").fetchall()}
        ac.close()
        
        users = []
        now = int(time.time() * 1000)
        
        for row in inbounds:
            try: clients = json.loads(row['settings']).get('clients', [])
            except: continue
            for cl in clients:
                email = cl.get('email')
                if not email: continue
                
                tr = traffic_data.get(email, {'up':0, 'down':0, 'last_online':0})
                used = (tr['up'] + tr['down']) / (1024**3)
                total = cl.get('totalGB', 0) / (1024**3)
                uset = user_sets.get(email, {})
                
                # Online Durumu
                last = tr['last_online']
                status = "never"; seen = "Yok"
                if last > 0:
                    mins = (now - last) / 60000
                    if mins <= 2: status="online"; seen="Aktif"
                    elif mins <= 60: status="idle"; seen=f"{int(mins)} dk"
                    else: status="offline"; seen=f"{int(mins/60)} sa"

                users.append({
                    'id': cl.get('id'), 'email': email, 'kullanici_adi': email,
                    'paket_tipi': "Sınırsız" if total==0 else f"{int(total)} GB",
                    'kota_limit_gb': "Sınırsız" if total==0 else round(total,2),
                    'kullanilan_kota_gb': round(used, 2),
                    'toplam_kullanim_gb': round((uset.get('total_usage_ever',0)+used), 2),
                    'durum': 'aktif' if cl.get('enable') else 'pasif',
                    'online_status': status, 'son_gorunme_kisa': seen,
                    'monthly_price': uset.get('monthly_price', 0),
                    'notes': uset.get('notes', ''), 'folder': uset.get('folder', 'Tümü'),
                    'payment_status': 'ok', 'days_until_payment': None,
                    'next_payment_date': uset.get('next_payment_date', ''),
                    'quota_days': int((cl.get('expiryTime',0)-now)/86400000) if cl.get('expiryTime',0)>0 else None
                })
        return users
    except: return []

@app.route('/api/users')
def users(): return jsonify(get_xui_users())

@app.route('/api/stats')
def stats():
    u = get_xui_users()
    return jsonify({'total_users':len(u), 'active_users':sum(1 for x in u if x['durum']=='aktif'), 'total_usage_gb':round(sum(x['toplam_kullanim_gb'] for x in u),2), 'overdue_count':0})

# --- GÜNCELLEME (STOP -> DELETE -> UPDATE -> START) ---
@app.route('/api/update-user-settings', methods=['POST'])
def update_settings():
    if 'user_id' not in session: return jsonify({'error'}), 401
    try:
        d=request.json; email=d.get('email')
        c = get_db_connection(PANEL_DB)
        reset_q = datetime.now().strftime('%Y-%m-%d') if d.get('quota') is not None else None
        
        # Admin DB Güncelle
        if c.execute("SELECT 1 FROM user_settings WHERE email=?", (email,)).fetchone():
            sql = "UPDATE user_settings SET monthly_price=?, next_payment_date=?, notes=?, folder=?, updated_at=CURRENT_TIMESTAMP"
            params = [d.get('monthly_price',0), d.get('expiry_date'), d.get('notes'), d.get('folder')]
            if reset_q: sql += ", quota_reset_date=?"; params.append(reset_q)
            sql += " WHERE email=?"; params.append(email)
            c.execute(sql, params)
        else:
            c.execute("INSERT INTO user_settings (email, monthly_price, notes, folder) VALUES (?,?,?,?)", (email, 0, '', d.get('folder')))
        c.close()

        # X-UI İşlemleri
        if d.get('quota') is not None or d.get('expiry_date'):
            print(f"🛑 [UPDATE] {email} X-UI Durduruluyor...")
            os.system("systemctl stop x-ui")
            time.sleep(2)
            
            try:
                # 1. Trafiği Sil (Hard Reset)
                if d.get('quota') is not None: hard_reset_user_traffic(email)
                
                # 2. Ayarları Güncelle
                xc = get_db_connection(XUI_DB)
                inbounds = xc.execute("SELECT id, settings FROM inbounds").fetchall()
                new_ms = int(datetime.strptime(d.get('expiry_date'), '%Y-%m-%d').replace(hour=23,minute=59).timestamp()*1000) if d.get('expiry_date') else None
                
                for row in inbounds:
                    s = json.loads(row['settings'])
                    mod = False
                    for cl in s.get('clients', []):
                        if cl['email'] == email:
                            cl['enable'] = True; mod = True # Aç
                            if d.get('quota') is not None: cl['totalGB'] = 0 if float(d['quota'])==0 else int(float(d['quota'])*1024**3)
                            if new_ms: cl['expiryTime'] = new_ms
                            break
                    if mod: xc.execute("UPDATE inbounds SET settings=? WHERE id=?", (json.dumps(s), row['id']))
                xc.close()
            except Exception as e: print(e)
            
            print("🚀 X-UI Başlatılıyor...")
            os.system("systemctl start x-ui")
            
        return jsonify({'success': True})
    except Exception as e: return jsonify({'success':False, 'message':str(e)}), 500

@app.route('/api/toggle-user', methods=['POST'])
def toggle():
    if 'user_id' not in session: return jsonify({'error'}), 401
    try:
        d=request.json; email=d.get('email'); enable=d.get('enable')
        print("🛑 Toggle -> X-UI Stop")
        os.system("systemctl stop x-ui")
        time.sleep(1)
        
        xc = get_db_connection(XUI_DB)
        inbounds = xc.execute("SELECT id, settings FROM inbounds").fetchall()
        for row in inbounds:
            s = json.loads(row['settings'])
            mod = False
            for cl in s.get('clients', []):
                if cl['email'] == email: cl['enable'] = enable; mod = True; break
            if mod: xc.execute("UPDATE inbounds SET settings=? WHERE id=?", (json.dumps(s), row['id']))
        xc.close()
        
        print("🚀 X-UI Start")
        os.system("systemctl start x-ui")
        return jsonify({'success': True})
    except Exception as e: return jsonify({'success':False}), 500

@app.route('/api/add-payment', methods=['POST'])
def pay():
    if 'user_id' not in session: return jsonify({'error'}), 401
    try:
        d=request.json; email=d.get('email')
        c = get_db_connection(PANEL_DB)
        c.execute("INSERT INTO payment_history (email, amount, payment_date) VALUES (?,?,?)", (email, d.get('amount'), d.get('payment_date')))
        np = (datetime.strptime(d.get('payment_date'), '%Y-%m-%d')+timedelta(days=30)).strftime('%Y-%m-%d')
        c.execute("UPDATE user_settings SET next_payment_date=?, updated_at=CURRENT_TIMESTAMP WHERE email=?", (np, email))
        c.close()

        print("🛑 Ödeme -> X-UI Stop")
        os.system("systemctl stop x-ui")
        time.sleep(2)
        
        hard_reset_user_traffic(email) # Trafiği sil
        
        xc = get_db_connection(XUI_DB)
        new_ms = int(datetime.strptime(np, '%Y-%m-%d').replace(hour=23,minute=59).timestamp()*1000)
        inbounds = xc.execute("SELECT id, settings FROM inbounds").fetchall()
        for row in inbounds:
            s = json.loads(row['settings'])
            mod = False
            for cl in s.get('clients', []):
                if cl['email'] == email:
                    cl['enable'] = True; cl['expiryTime'] = new_ms; mod = True; break
            if mod: xc.execute("UPDATE inbounds SET settings=? WHERE id=?", (json.dumps(s), row['id']))
        xc.close()
        
        print("🚀 X-UI Start")
        os.system("systemctl start x-ui")
        return jsonify({'success': True})
    except Exception as e: return jsonify({'success':False}), 500

@app.route('/api/update-user-note', methods=['POST'])
def un(): 
    d=request.json; c=get_db_connection(PANEL_DB)
    if c.execute("SELECT 1 FROM user_settings WHERE email=?",(d['email'],)).fetchone():
        c.execute("UPDATE user_settings SET notes=? WHERE email=?",(d['note'],d['email']))
    else: c.execute("INSERT INTO user_settings (email, notes) VALUES (?,?)",(d['email'],d['note']))
    c.close(); return jsonify({'success':True})

@app.route('/api/move-to-folder', methods=['POST'])
def mf(): 
    d=request.json; c=get_db_connection(PANEL_DB)
    if c.execute("SELECT 1 FROM user_settings WHERE email=?",(d['email'],)).fetchone():
        c.execute("UPDATE user_settings SET folder=? WHERE email=?",(d['folder'],d['email']))
    else: c.execute("INSERT INTO user_settings (email, folder) VALUES (?,?)",(d['email'],d['folder']))
    c.close(); return jsonify({'success':True})

@app.route('/api/payment-history/<email>')
def ph(email):
    c=get_db_connection(PANEL_DB)
    r=[dict(row) for row in c.execute("SELECT * FROM payment_history WHERE email=? ORDER BY payment_date DESC",(email,)).fetchall()]
    c.close(); return jsonify(r)

@app.route('/api/notifications')
def notif(): return jsonify([])

if __name__ == '__main__':
    init_db()
    t = threading.Thread(target=monitor_loop)
    t.daemon = True
    t.start()
    app.run(host='0.0.0.0', port=8888, debug=False)
