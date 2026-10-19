# Durum — 2026-10-19

Yerel kaynak gerçek `berkerden/codexalsat` clone'udur. Public depo kullanılır.
PR #3 araştırma/paper sürümünü main’e taşıdı; issue #2 canlı öncesi kalan işleri izler.
PR #4 testnet temelini, PR #5 süreli alış→OCO çevrimini main’e taşıdı; üretim kapalıdır.

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
  Yerel anahtarla BTCUSDT ve SOLUSDT salt okunur hesap/komisyon ve kotasyon kontrolü
  6 Ekim’de geçti. 9 Ekim’de hesap sorgusu ve eşleştirme yapmayan order/test isteği
  BTC/SOL komisyon bileşenlerini sıfır doğruladı. Tam sıfır komisyon kanıtı dışında
  etkin bilinmeyen indirim varlığı emir gönderimini engeller. Bu istisna yalnız
  Testnet içindir; sıfır indirim oranı tek başına yeterli değildir.
  13 Ekim’de sınırlı BTC limit emri NEW→CANCELED, çevrim NO_FILL sonucuyla doğrulandı.
  Yeni süreçte aynı çevrim NO_FILL kaldı. İkinci sınırlı denemede BTC alımı doldu,
  hedef/stop OCO emirlerinin ikisi açık doğrulandı. Yeni yönetici süreci ikinci
  alım açmadan mevcut korumayı izledi. 17 Ekim’de borsadan kapanış doğrulandı:
  stop emri FILLED, hedef emri EXPIRED; çevrim EXITED ve kalan miktar/toz sıfır.
  Yeni süreçte tekrar sorgulama aynı sonucu verdi; emir ve dolum sayıları değişmedi.
  Bu sanal entegrasyon denemesi strateji kârlılığı kanıtı değildir.
  Aynı gün tek ek sanal girişte kabul edilen alış ve iptal yanıtları yerelde atılıp
  süreçler kesildi. UNKNOWN kayıtları yeni süreçte sorguyla çözüldü; sonuç NO_FILL,
  tek niyet ve sıfır dolum. Son tekrar yönetiminde emir gönderimi denenmedi.
  Gerçek ağ kesintisi veya borsada kısmi dolum doğrulaması iddia edilmez.
  18 Ekim’de hesap çapındaki açık emirleri yerel kayıtlarla karşılaştıran salt
  okunur denetim eklendi. Belirsiz kayıt, kalan envanter, kimlik uyuşmazlığı ve
  tanınmayan bot biçimindeki kimlikler yeni girişi engeller. Son gönderimden önce
  süre, saat, emir kapasitesi ve yerel kayıt değişimi tekrar kontrol edilir;
  mevcut çıkış yönetimi bu giriş denetiminden bağımsızdır. Anahtarlı denetim geçti.
  Ardından tek hesap kimliğine kalıcı bağlama eklendi; yalnız boş defter ilk kez
  bağlanabilir. Tüm emir yollarında hesap doğrulanır. Daha önce doğrulanmış geçmiş
  kaybolursa kalıcı engel korunur; otomatik reset kurtarması veya engel temizliği yoktur.
  Mevcut eski defter anahtarlı kontrolde migration_required verdi; tarihsel emir,
  dolum, koruma ve çevrim kayıtlarının değişmediği doğrulandı. Bu defterin emir
  işlemleri doğrulanmış geçiş tamamlanana kadar engellidir; yeni emir gönderilmedi.
  19 Ekim’de ayrı, yalnız GET kullanan eski defter doğrulayıcısı eklendi. Kapanmış,
  envanteri sıfır olan kayıtlar emir/OCO şartları ve eksiksiz dolum/komisyon kümeleriyle
  eşleşirse tek işlemde hesap bağlaması ve yerel doğrulama makbuzu yazılır. Kilit altında
  tüm yerel kayıtlar yeniden karşılaştırılır; geçmiş satırlar ve süreler değiştirilmez.
  Anahtarlı önizleme 4 emir, 2 dolum, 3 kapanmış çevrimi doğruladı. Özel SQLite yedeği
  alındı; yeniden doğrulanan geçiş tamamlandı ve tarihsel satırlar birebir korundu.
  Sonraki normal başlangıç denetimi hesap bağlaması doğrulanmış olarak geçti;
  borsaya yeni emir veya iptal gönderilmedi.
- A7: ayrı Astra bağlamında kritik inceleme ve hedefli tekrar doğrulama yapıldı;
  kapsam içindeki P1 bulguları giderildi. [İnceleme kaydı](docs/REVIEW.md).
- A8: temiz GitHub clone'unda kurulum, iki sunucunun başlatılması ve supervisor
  kapanınca iki portun da serbest kalması doğrulandı. Kurtarma testleri ve
  [operasyon](docs/OPERATIONS.md) belgesi mevcut. Docker bu makinede yok.

Önceki ana sürüm PR #11 CI’da 372 backend testi (SQLite + PostgreSQL 16),
5 frontend birim testi, 2 tarayıcı akışı, lint/tip/build ve güvenlik kontrollerini geçti.
19 Ekim eski defter geçişinin yerel kontrolü: 327 başarılı, PostgreSQL servisi olmadığı
için 144 varyant atlandı; lint ve strict tip kontrolü başarılı. Yeni geçiş paketi
50 başarılı / 49 PostgreSQL varyantı atlandı. CI sonucu ilgili PR'da kaydedilir.
Astra/high ilk incelemesi ve Sol/high uygulaması kullanım sınırına ulaştı; kalan
düzeltme ve testler ana oturumda tamamlandı. Bağımsız son inceleme tamamlanmadı.

Gerçek tarayıcıda BTC/SOL, periyot değişimi, araştırma formu, sanal oturum başlatma/durdurma,
canlı mod kilidi ve 390×844 mobil menü/durdurma çubuğu kontrol edildi. Deneme paper oturumu durduruldu, açık pozisyon yok.
Bu kısa kontrol ileri gözlem süresi yerine geçmez.

Canlı **kapalı**. Eksikler: geçiş paketinin bağımsız son incelemesi, anahtarlı testnet
kısmi dolum/hata senaryoları, stratejiyle sürekli
giriş ve kesintisiz koruma/izleme entegrasyonu, hesap çapında sahipsiz emir ve reset kurtarma, BNB komisyon muhasebesi, pozisyon içi portföy maksimum düşüşü, 30 takvim günü ileri paper,
yeterli ekonomik avantaj, üretim operasyon değerlendirmesi. PostgreSQL mantıksal
kurtarma ve eşzamanlılık CI'da sınanır; fiziksel pg_dump/restore ve Docker testi
ayrıca gerekir. Kullanıcı canlı para işlemi yetkisi vermedi; yalnız sanal Testnet
denemeleri yürütüldü; üretim bağlantısı kullanılmadı.

Main koruma kuralını kaydetme otomatik onay incelemesince erişim politikası
değişikliği olarak reddedildi; özel onay sorusu yanıtlanana kadar etkinleştirilmez.
CI başarılı PR birleştirme yetkisi kullanıcı tarafından verildi.

Model devirleri: Terra medium iskelet; Sol high veri, finans/araştırma ve emir/secret
çekirdeği; Sol medium arayüz; ayrı Astra high inceleme. Kullanım sınırları çalışmayı
kesintiye uğrattı. Sayısal token/maliyet ölçümü yok; tasarruf yüzdesi iddiası yok.
