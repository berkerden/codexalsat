# Durum — 2026-10-27

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

Önceki ana sürüm PR #13 CI’da 487 backend testi (SQLite + PostgreSQL 16),
5 frontend birim testi, 2 tarayıcı akışı, lint/tip/build ve güvenlik kontrollerini geçti.
Test edilen kaynak dfc7c23; main birleştirme kaydı 1281e38.
19 Ekim eski defter geçişinin yerel kontrolü: 327 başarılı, PostgreSQL servisi olmadığı
için 144 varyant atlandı; lint ve strict tip kontrolü başarılı. Yeni geçiş paketi
50 başarılı / 49 PostgreSQL varyantı atlandı. CI’da bu varyantların tamamı geçti.
Astra/high ilk incelemesi ve Sol/high uygulaması kullanım sınırına ulaştı; kalan
düzeltme ve testler ana oturumda tamamlandı. 20–21 Ekim’de bağımsız Astra/high
son incelemesi tamamlandı. Üç P2 bulgusu Sol/high ile düzeltildi: eksik komisyon
varlığı alanı, kanonik olmayan emir kimliği ve borsa giriş zamanının yetki süresiyle
karşılaştırılması. Hedefli bağımsız doğrulama 249 başarılı / 134 PostgreSQL varyantı
atlandı; kapsam içinde açık P1/P2 yok. Son tam yerel paket 335 başarılı / 152 atlandı;
lint ve strict tip kontrolü başarılı. İlk CI'da 487 backend testi geçti;
güvenlik taramalarının işaretlediği Vite ve certifi sürümleri güncellendi. Güncel
bağımlılıklarla yerel güvenlik taramaları, 5 arayüz testi ve 2 tarayıcı akışı geçti.
Son PostgreSQL ve sürüm CI sonucu ilgili PR'dadır.

Gerçek tarayıcıda BTC/SOL, periyot değişimi, araştırma formu, sanal oturum başlatma/durdurma,
canlı mod kilidi ve 390×844 mobil menü/durdurma çubuğu kontrol edildi. Deneme paper oturumu durduruldu, açık pozisyon yok.
Bu kısa kontrol ileri gözlem süresi yerine geçmez.

Canlı **kapalı**. Eksikler: anahtarlı testnet
kısmi dolum/hata senaryoları, stratejiyle sürekli
giriş ve gözetimsiz işletim sistemi servisi, hesap çapında sahipsiz emir ve reset kurtarma, BNB komisyon muhasebesi, pozisyon içi portföy maksimum düşüşü, 30 takvim günü ileri paper,
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


23 Ekim — mevcut çevrim için ön planda izleyici

`cycle_cli watch` mevcut tek çevrimi izler; yeni alış açmaz ve giriş süresini
uzatmaz. Varsayılanı salt okunurdur. Açık Testnet seçimi hesap doğrulamasından
sonra girişi kalıcı durdurur, kalan alışın iptalini ve mevcut envanterin OCO
korumasını yönetir. Belirsiz emir aynı kimlikle sorgulanır, yeniden gönderilmez.
Geçici hatalarda sınırlı geri çekilme, hata sonrası eski koruma bilgisini geçersiz
sayma, süre dolumu/kesinti durumu ve tek yönetici kilidi eklendi. SQLite dosya
kilidi ve PostgreSQL oturum kilidi işbirliği yapan yöneticileri ayırır; borsada
fencing veya yüksek erişilebilirlik sağlamaz. Terminal ve bilgisayar açık kalmalıdır.

Sahte HTTP ile gerçek taşıma katmanında salt okunur izleme, kısmi dolum/iptal,
OCO kabul yanıtı kaybından sonra yeniden başlatma ve ikinci yöneticiyi reddetme
sınanır. Gerçek hesap/anahtar kullanılmadı. PostgreSQL oturum kaybı ve kilit
bırakma testleri CI'da çalışır. Bağımsız Astra/high son incelemesinde kapsam içinde
açık P1/P2 yok. Yerel paket 367 başarılı / 158 PostgreSQL varyantı atlandı; lint
ve strict tip kontrolü geçti. CI sonucu PR'da izlenir.


İlk izleyici CI koşusunda 525 backend testi (SQLite + PostgreSQL 16),
5 arayüz testi ve 2 tarayıcı akışı geçti. Ön yüz taramasının yüksek önem eşiği
CI'ı geçirse de günlükler Vitest mocker için iki orta düzey açık bildirdi
(GHSA-qxg6-89mg-rqv8, GHSA-xp26-3454-mjgp). Vitest 5.0.5'e sabitlendi;
yerel npm audit sıfır açık, lint/tip/build, 5 arayüz testi ve 2 tarayıcı akışı
başarılı. Son commitin CI sonucu ilgili PR'da kaydedilir.


26 Ekim — panelden mevcut Testnet çevrimi yönetimi

PR #14 main'e birleştirildi (35ea54b); test edilen eae4946 için CI'da
525 backend, 5 arayüz ve 2 tarayıcı testi ile iki bağımlılık taraması geçti.

Operasyon panelinde mevcut açık çevrimi seçme, varsayılan salt okunur izleme,
ayrı onayla kalan alış iptali/mevcut envanter koruması ve izleyiciyi durdurma
eklendi. Görev uygulama sunucusuna aittir; tarayıcı kapanınca devam eder,
sunucu kapanınca temizlenir ve yeniden başlangıçta boşta kalır. Yeni alış
açılmaz; durdur düğmesi pozisyonu kapatmaz veya emir iptal etmez.

Kontrol istekleri sabit yerel Origin, JSON ve süreç içi oturum anahtarı ister.
Durum sorgusu defteri değiştirmez; eksik defter oluşturmaz. Anahtar ve defter
yollarını tarayıcı seçemez. Tek görev ve mevcut yönetici kilidi korunur.
Gecikmiş durum yanıtı durdurma sonucunu geri alamaz; eski koruma gözlemi
korumalı veya sıfır korumasız miktar olarak sunulmaz.

Yerel paket 381 başarılı / 161 PostgreSQL varyantı atlandı; 17 arayüz testi,
390px mobil başlatma/durdurma dahil 3 tarayıcı akışı, lint/tip/build geçti.
Astra/high bağımsız incelemesinde iki P2 giderilip tekrar doğrulandı; açık
P1/P2 yok. Gerçek anahtar veya borsa isteği kullanılmadı. PostgreSQL ve son
bağımlılık sürümlerinin CI kanıtı ilgili PR'da kaydedilir. Üretim kapalıdır.


PR #15 main'e birleştirildi (da55277); test edilen f5e5098 için CI 40246691600
542 backend (SQLite + PostgreSQL 16), 17 arayüz ve 3 tarayıcı testini geçti.
Gizli bilgi taraması ve iki bağımlılık denetimi temiz; yerel kaynak eşitlendi.


27 Ekim — paper deney arşivi

Paper ekranında durdurulmuş ve pozisyonsuz deney açık onayla arşivlenir;
ayarlar, bakiye, emir/dolum ve denetim geçmişi korunur. Yeni boş deney ayrı
kimlikle oluşur ve ayrıca başlatılır. Sayfalı arşiv listesi ve salt okunur
ayrıntı ekranı eklendi; deney sonuçları tek bir performansa birleştirilmez.

Oturum kimliği tüm paper kontrollerinde ve ağ bekleyen işçide doğrulanır.
Gecikmiş yanıt yeni deneye işlem veya hata durdurması taşıyamaz. Arşiv yazımı
ve yeni boş durum tek işlemde; hata iki değişikliği de geri alır. Mantıksal
yedek sürüm 2 tüm arşivleri korur, yalnız boş hedefe yüklenir. Eski sürüm 1
yedek desteklenir; giriş yetkisi geri yüklenmez. Bozuk pozisyon, sayısal
para alanı veya yapılandırmasız geçmiş bakiye/dolum içeren yedek reddedilir.

Bağımsız Astra/high incelemesinde bir P1 ve iki P2 giderildi; yeniden
doğrulamada kapsam içinde açık P1/P2 yok. Sol/high backend ve Sol/medium
arayüz uygulamasını yaptı; kullanım sınırından sonra kalan düzeltmeler ve
testler ana oturumda tamamlandı. Gerçek hesap, anahtar veya borsa isteği
kullanılmadı. Canlı kapalı; ileri gözlem süresi ve avantaj kanıtı hâlâ eksik.

Son yerel paket: 434 başarılı backend testi, yerel PostgreSQL servisi olmadığı
için 167 varyant atlandı; 22 arayüz testi ve 4 tarayıcı akışı geçti. Lint,
strict tip, build ve diff kontrolleri başarılı. npm audit sıfır açık, pip-audit
bilinen açık yok. PostgreSQL ve son commit CI kanıtı ilgili PR'da kaydedilir.

İlk arşiv CI koşusu 601 backend testi (SQLite + PostgreSQL 16), 22 arayüz
ve 4 tarayıcı testini geçti. Güncel pip-audit taraması msgpack 1.2.2 için
CVE-2026-114613 bildirdi; bağımlılık 1.2.3 düzeltme sürümüne sabitlendi.
Son sürümün tarama ve CI sonucu ilgili PR'da kaydedilir.
