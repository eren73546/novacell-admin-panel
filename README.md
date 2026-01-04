# novacell-admin-panel

bash <(curl -sL https://raw.githubusercontent.com/eren73546/novacell-admin-panel/main/install-v4.5.sh)


ÇÖZÜM 1 yaptım oldu sonunda ama neden githubta güncelledik ona rağmen güncel olanı çekmiyor ?
NEDEN GÜNCEL DOSYAYI ÇEKMİYOR?
Sebep: GitHub'ın CDN cache sistemi!
GitHub Raw CDN Nasıl Çalışır:

İlk istek: raw.githubusercontent.com → Dosyayı çeker
CDN Cache: Dosya 5-10 dakika boyunca cache'lenir
Sonraki istekler: Cache'den döner (eski versiyon!)
Alttaki kısmı komple Termius yap eski cacheleri silip yapıyor
# Dosyayı indir
wget https://raw.githubusercontent.com/eren73546/novacell-admin-panel/main/install-v4.5.sh -O /tmp/install-v4.5.sh

# Kontrol et
tail -20 /tmp/install-v4.5.sh

# Çalıştır
bash /tmp/install-v4.5.sh


NOTLAR
3X-Uİ DE PASİF AKTİF YAPARSAN Novacell panelde aktif yapamıyoruz, ve süresiz yapıp düzenlemek gerekiyor
