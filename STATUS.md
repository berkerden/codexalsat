# Durum — 2026-10-06

Yerel kaynak gerçek `berkerden/codexalsat` clone'udur. Public depo kullanılır.
PR #3 araştırma/paper sürümünü main’e taşıdı; issue #2 canlı öncesi kalan işleri izler.
PR #4 testnet temelini main’e taşıdı. Devamı `codex/testnet-entry-protection` dalındadır;
üretim kapalıdır.

- A0/A1: GitHub, CI, SPEC ve yerel kurulum hazır. Tamamlanan kaynaklar küçük
  commitlerle gönderiliyor; bağlı GitHub API ve aynı clone'a fetch kullanılır.
- A2: Global gerçek REST/WS, kapalı mum doğrulama, kalite/saat kontrolleri ve
  sürümlü Parquet arşivi çalışıyor. Binance TR doğrulanmış adaptör değildir.
- A3: 3 nedensel strateji, 9 aday/grup, kronolojik ayrım, purge, walk-forward,
  maliyet stresi ve benzer örüntü analizi. Gerçek 10 grubun tamamında avantaj
  desteklenmedi; [sayısal sonuçlar](docs/RESULTS.md). Testle yeniden optimizasyon yok.
- A4/A5: Türkçe panel, Decimal maliyet/marj, kalıcı risk sınırlı paper motoru,
  süreli giriş yetkisi ve çıkış yönetiminden ayrı durdurma kontrolleri hazır.
- A6 kısmi: hesap/sembol/borsa filtreleri, süreli ve tek çevrimlik testnet alış→OCO
  yönetimi eklendi. Kısmi alışta kalan giriş iptal edilir; dolum/komisyon
  kesinleştikten sonra net envanter korunur. Kalıcı durdurma, gönderim öncesi son
  kontrol, eski koruma gözlemi ve belirsiz emirleri tekrar göndermeme testleri var.
  Ayrı araç açık testnet onayı ve en fazla 100 sanal USDT giriş tutarı ister;
  stratejiyle tekrar alış açmaz. [Kurulum ve sınırlar](docs/TESTNET.md).
  Yerel testnet anahtarı yok; imzalı gerçek hesap sorgusu veya testnet emri yapılmadı.
- A7: ayrı Astra bağlamında kritik inceleme ve hedefli tekrar doğrulama yapıldı;
  kapsam içindeki P1 bulguları giderildi. [İnceleme kaydı](docs/REVIEW.md).
- A8: temiz GitHub clone'unda kurulum, iki sunucunun başlatılması ve supervisor
  kapanınca iki portun da serbest kalması doğrulandı. Kurtarma testleri ve
  [operasyon](docs/OPERATIONS.md) belgesi mevcut. Docker bu makinede yok.

Önceki main sürümü PR #4 CI’da 144 backend testi (SQLite + PostgreSQL 16),
5 frontend birim testi, 2 tarayıcı akışı, lint/tip/build ve güvenlik kontrollerini geçti.
Yeni çevrim ve filtre paketi yerelde 157 testi geçti; 58 PostgreSQL varyantı
servis olmadığı için atlandı ve CI’da yürütülür. Lint ve strict tip kontrolü geçti.
Son CI sonucu ilgili PR üzerinde kaydedilir.

Gerçek tarayıcıda BTC/SOL, periyot değişimi, araştırma formu, sanal oturum başlatma/durdurma,
canlı mod kilidi ve 390×844 mobil menü/durdurma çubuğu kontrol edildi. Deneme paper oturumu durduruldu, açık pozisyon yok.
Bu kısa kontrol ileri gözlem süresi yerine geçmez.

Canlı **kapalı**. Eksikler: anahtarlı testnet/borsa doğrulaması, stratejiyle sürekli
giriş ve kesintisiz koruma/izleme entegrasyonu, hesap çapında sahipsiz emir ve reset kurtarma, BNB komisyon muhasebesi, pozisyon içi portföy maksimum düşüşü, 30 takvim günü ileri paper,
yeterli ekonomik avantaj, üretim operasyon değerlendirmesi. PostgreSQL mantıksal
kurtarma ve eşzamanlılık CI'da sınanır; fiziksel pg_dump/restore ve Docker testi
ayrıca gerekir. Kullanıcı canlı para işlemi yetkisi vermedi; geliştirmede borsaya emir gönderilmedi.

Main koruma kuralını kaydetme otomatik onay incelemesince erişim politikası
değişikliği olarak reddedildi; özel onay sorusu yanıtlanana kadar etkinleştirilmez.
CI başarılı PR birleştirme yetkisi kullanıcı tarafından verildi.

Model devirleri: Terra medium iskelet; Sol high veri, finans/araştırma ve emir/secret
çekirdeği; Sol medium arayüz; ayrı Astra high inceleme. Kullanım sınırları çalışmayı
kesintiye uğrattı. Sayısal token/maliyet ölçümü yok; tasarruf yüzdesi iddiası yok.
