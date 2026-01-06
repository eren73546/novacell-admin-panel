#!/usr/bin/env python3
"""
NovaCell-3 Otomatik Kota Yenileme Scripti
Her gün 00:01'de çalışır
quota_start_date'in gününü kontrol eder
E�leşenlerin kotasını yeniler
"""

import sqlite3
from datetime import datetime
import os
import time
import json

PANEL_DB = '/opt/xui-admin-panel/admin_panel.db'
XUI_DB = '/etc/x-ui/x-ui.db'

def log(message):
    """Log mesajı yazdır"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {message}")

def reset_user_quota(email):
    """Kullanıcının kotasını sıfırla"""
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
            
            # Total usage kaydet
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
        
        # Kota sıfırla
        c.execute("UPDATE client_traffics SET up = 0, down = 0 WHERE email = ?", (email,))
        conn.commit()
        conn.close()
        
        # Log kaydet
        admin_conn = sqlite3.connect(PANEL_DB)
        admin_c = admin_conn.cursor()
        admin_c.execute("INSERT INTO quota_reset_log (email, reset_date, reset_type) VALUES (?, ?, ?)",
                       (email, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'auto'))
        admin_conn.commit()
        admin_conn.close()
        
        return True
    except Exception as e:
        log(f"❌ Kota sıfırlama hatası ({email}): {e}")
        return False

def disable_user(email):
    """Kullanıcıyı pasif et (ödeme yapılmamış)"""
    try:
        if not os.path.exists(XUI_DB):
            return False
        
        conn = sqlite3.connect(XUI_DB)
        c = conn.cursor()
        
        # inbounds JSON'u güncelle
        c.execute("SELECT id, settings FROM inbounds")
        inbounds = c.fetchall()
        
        for inbound in inbounds:
            inbound_id = inbound[0]
            settings = json.loads(inbound[1])
            clients = settings.get('clients', [])
            
            for client in clients:
                if client.get('email') == email:
                    client['enable'] = False
                    break
            
            settings['clients'] = clients
            new_json = json.dumps(settings, ensure_ascii=False)
            c.execute("UPDATE inbounds SET settings = ? WHERE id = ?", (new_json, inbound_id))
            break
        
        # client_traffics güncelle
        c.execute("UPDATE client_traffics SET enable = 0 WHERE email = ?", (email,))
        
        conn.commit()
        conn.close()
        
        return True
    except Exception as e:
        log(f"❌ Kullanıcı pasif etme hatası ({email}): {e}")
        return False

def calculate_next_reset_date(quota_start_date_str, today):
    """Bir sonraki kota yenileme tarihini hesapla"""
    try:
        quota_start = datetime.strptime(quota_start_date_str, '%Y-%m-%d')
        start_day = quota_start.day
        
        today_dt = datetime.strptime(today, '%Y-%m-%d')
        
        # Bugünün gününü kontrol et
        if today_dt.day == start_day:
            return today
        
        return None
    except Exception as e:
        log(f"❌ Tarih hesaplama hatası: {e}")
        return None

def check_and_reset_quotas():
    """Bugün kota yenileme günü olan kullanıcıları kontrol et"""
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        today_day = datetime.now().day
        
        log(f"🔍 Kota yenileme kontrolü başlatılıyor... (Bugün: {today})")
        
        # Kullanıcı ayarlarını çek
        admin_conn = sqlite3.connect(PANEL_DB)
        admin_conn.row_factory = sqlite3.Row
        admin_c = admin_conn.cursor()
        admin_c.execute("""
            SELECT email, quota_start_date, last_payment_date, next_payment_date 
            FROM user_settings 
            WHERE quota_start_date IS NOT NULL
        """)
        users = admin_c.fetchall()
        admin_conn.close()
        
        if not users:
            log("ℹ️  Kota yenileme tarihine sahip kullanıcı yok.")
            return
        
        reset_count = 0
        disabled_count = 0
        
        for user in users:
            email = user['email']
            quota_start_date = user['quota_start_date']
            last_payment_date = user['last_payment_date']
            
            try:
                # quota_start_date'in gününü al
                start_dt = datetime.strptime(quota_start_date, '%Y-%m-%d')
                start_day = start_dt.day
                
                # Bugünün günü ile karşılaştır
                if start_day != today_day:
                    continue  # Bu kullanıcının günü değil
                
                log(f"📅 {email}: Kota yenileme günü! (Gün: {start_day})")
                
                # Ödeme kontrolü
                if last_payment_date:
                    try:
                        payment_dt = datetime.strptime(last_payment_date, '%Y-%m-%d')
                        
                        # Son ödeme tarihi quota_start_date'den sonra mı?
                        if payment_dt >= start_dt:
                            # Ödeme yapılmış, kota yenile
                            if reset_user_quota(email):
                                log(f"✅ {email}: Kota yenilendi (Ödeme: {last_payment_date})")
                                reset_count += 1
                            else:
                                log(f"❌ {email}: Kota yenileme başarısız")
                        else:
                            # Ödeme yapılmamış, data kes
                            reset_user_quota(email)  # Kotayı yine de sıfırla
                            disable_user(email)
                            log(f"⚠️  {email}: Ödeme yapılmadı, data kesildi (Son ödeme: {last_payment_date})")
                            disabled_count += 1
                    except:
                        # Tarih parse hatası, güvenli tarafta kal
                        log(f"⚠️  {email}: Ödeme tarihi parse edilemedi")
                        continue
                else:
                    # Hiç ödeme kaydı yok, data kes
                    reset_user_quota(email)
                    disable_user(email)
                    log(f"⚠️  {email}: Hiç ödeme kaydı yok, data kesildi")
                    disabled_count += 1
                    
            except Exception as e:
                log(f"❌ {email}: İşlem hatası: {e}")
                continue
        
        # Özet
        log(f"📊 ÖZET: {reset_count} kota yenilendi, {disabled_count} kullanıcı data kesildi")
        
        # x-ui restart
        if reset_count > 0 or disabled_count > 0:
            log("🔄 x-ui restart ediliyor...")
            os.system('/usr/bin/systemctl stop x-ui')
            time.sleep(2)
            os.system('/usr/bin/systemctl start x-ui')
            time.sleep(3)
            log("✅ x-ui restart tamamlandı")
        
    except Exception as e:
        log(f"❌ HATA: {e}")

if __name__ == '__main__':
    log("=" * 60)
    log("NovaCell-3 Otomatik Kota Yenileme Scripti")
    log("=" * 60)
    check_and_reset_quotas()
    log("=" * 60)
    log("Script tamamlandı")
    log("=" * 60)
