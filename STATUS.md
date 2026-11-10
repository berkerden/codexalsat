# Durum — 2026-11-10

Public kaynak depo: [berkerden/codexalsat](https://github.com/berkerden/codexalsat).
Kararlı kaynak dalı main; canlı kullanım öncesi kalan işler issue #2'de izlenir.
Bu belge yazılım durumunu ve sentetik doğrulamayı özetler; kişisel hesap veya
operasyon geçmişi içermez. Üretim işlemleri kod içinde kapalıdır.

## Hazır özellikler

- Binance Global REST/WS verisi, kapalı mum/kalite/saat kontrolleri ve sürümlü
  tarihsel veri; Binance TR doğrulanmış adaptör değildir.
- Kronolojik araştırma, walk-forward, maliyet stresi ve benzer örüntüler.
  Araştırma sınırları ve sonuçları [RESULTS.md](docs/RESULTS.md) içinde.
- Türkçe mobil panel, Decimal komisyon/marj ve risk sınırlı paper muhasebesi;
  süreli giriş yetkisi, çıkış yönetimi, ayrı deneyler ve değişmez arşivler.
- Kalıcı niyet, belirsiz emri tekrar göndermeden sorgulama, kısmi dolum,
  hesap bağlama ve kapalı eski kayıt doğrulama için sınırlı Testnet altyapısı.
  Mevcut tek çevrimin izlenmesi; stratejiyle sürekli yeni giriş değildir.
- İleri paper gözlem kaydı: örneklenen sağlıklı kapsam ile takvim süresi ayrı,
  ayar/kod kimliği ve devralınmış geçmiş görünür; JSON rapor indirilebilir.

## Son paket: yerel bildirim ve kontrollü yeniden başlatma

`start --notify-local` mevcut izleme sürecinin fark ettiği sorunlar için
kişisel bilgi içermeyen Mac bildirimi göndermeyi dener. Varsayılan kapalıdır.
Aynı uyarı yinelenmez; farklı uyarılar dahil denemeler arasında en az beş
dakika bulunur. Bildirim işlemi ve sonucu bellekte ayrı tutulur; gecikmesi
veya hatası sağlık kontrolünü durdurmaz. İşletim sisteminin bildirimi kabul
etmesi ekranda gösterim kanıtı değildir. Sürecin kendisinin çökmesi veya
bilgisayarın uyuması için bağımsız bir alarm kurulmuş değildir.

`restart`, aynı kaynak sürümünü, veri dizinini, uyku korumasını ve bildirim
tercihini korur. Kaynak, veri, çalıştırıcı ve yüklü servis sahipliği kontrolleri
geçmeden durdurma yapmaz. Eski süreç ve servis kaydı kalkmadan yeni başlatma
denenmez; kapanış doğrulaması süre sınırlıdır. Otomatik tekrar, sürüm yükseltme,
pozisyon kapatma veya yeni işlem yetkisi vermez.

88 odak test ve 15 başlatıcı testi geçti; ayrı Astra/high incelemesinde açık
P1/P2 kalmadı. Native macOS yapay servisinde bildirim yolu, değişmiş kaynakta
durdurmadan reddetme, yanıt vermeyen servisi aynı kayıtlarla yeniden başlatma
ve dört ardışık yeniden başlatma doğrulandı. Tam CI sonucu ilgili PR'dadır.

## Önceki paket: servis yanıt kontrolü ve sınırlı günlükler

Arka plan servisi API yanıtını, panelin API bağlantısını ve gerçek işçi
döngüsünün ilerlemesini ayrı izler. Her çalıştırmanın kimliği farklıdır;
eski veya başka sürüme ait başarılı kayıt güncel sağlık sayılmaz. Salt okunur
durum komutu güncel kimliği yeniden doğrular. Yanıt veya işçi uyarısı
otomatik yeniden başlatma, işlem veya yeni giriş yetkisi oluşturmaz.

Standart çıktı ve hata günlükleri toplam en çok 40 MiB tutulur. Eski büyük
günlükler sınıra indirilir; yazma hatasında çıktı tüketilmeye devam edilir.
Hesap, emir, deney arşivi ve yedekler bu temizliğin dışındadır. Güncel durum
tek, boyutu sınırlı yerel kayıtla tutulur. Piyasa veri kalitesi ve ekonomik
avantaj, servisin yanıt vermesinden çıkarılmaz.

50 odak test geçti; ayrı Astra/high incelemesinde açık P1/P2 bulunmadı.
Geçici native macOS servisinde 100 MiB satır sonsuz çıktı 40 MiB sınırında
kaldı; yanıt kaybı/düzelme, aynı süreçlerin korunması, normal kapanış,
üst süreç çökmesinde alt süreç temizliği ve eski başarıyı reddetme doğrulandı.
Sentetik hesap kaydı değişmedi. Tam CI sonucu ilgili PR'dadır.
İşlem/risk kararları değişmedi.

## Önceki paket: terminalden bağımsız Mac oturumu

Mevcut SQLite sanal hesabı, açık Mac kullanıcı oturumunda `launchd` ile
başlatma/durum/durdurma komutlarıyla yönetilir. Terminal kapanabilir; kullanıcı
oturumundan çıkış, kapak kapatma veya uyku çalışmayı kesebilir. Otomatik
yeniden başlatma ve oturum açılışında otomatik çalışma yoktur.

Kaynak sürümü ve veri dizini sabitlenir. Değişmiş sürüm için durmuş serviste
açık kabul gerekir; veri yolu sessizce değişmez, boş hesap oluşturulmaz.
Mevcut paper kaydı salt okunur kontrol edilir. Başlatıcı yalnız kullanıcı
oturumundaki tek SpotLab servisini yönetir; günlük ve yapılandırma yereldir.
Yeni sanal giriş yetkisi veya Testnet izleyicisi kendiliğinden başlatılmaz.

macOS'un Documents altına arka plan günlüğü yazmayı reddedebildiği doğrulandı.
Günlükler özel `~/Library/Logs/SpotLab` dizininde, yapılandırma ise mevcut
`data/service` dizininde tutulur; kaynak ve hesap verisi taşınmaz.

27 odak test, lint ve tip kontrolü geçti. Ayrı Astra/high incelemesinde açık
P1/P2 kalmadı. Native macOS geçici servis denemesinde komut bittikten sonra
çalışmanın sürmesi, normal durdurma/çökmede alt süreç temizliği, açık sürüm
kabulü ve kayıp veritabanında yeni hesap açılmaması doğrulandı. Yapay
veritabanı başlatma ön kontrolünde değişmedi. Tam CI sonucu ilgili PR'dadır.

## Önceki paket: yerel gözlem başlatıcısı

`bash scripts/run-local.sh --keep-awake`, macOS'ta başlatıcı ömrüne bağlı
boşta sistem uykusunu önleme isteği oluşturur. Kalıcı güç ayarı veya işletim
sistemi servisi kurmaz. Kapak kapatma, elle uyutma, elektrik/ağ kesintisi ve
süreç içi takılma için kesintisiz çalışma garantisi değildir.

Başlatma öncesinde Python/Node/Vite ve iki yerel port kontrol edilir. Kullanımda
olan portta yeni backend açılmaz; mevcut süreç sonlandırılmaz. Sunuculardan
biri veya istenmiş uyku koruması durduğunda başlatıcı hata vererek diğerlerini
kapatır. Otomatik yeniden başlatma veya işlem yetkisi yenileme yapılmaz.
Sanal/borsa işlemlerinin kararları ve kayıt biçimleri değişmez.

Yerel macOS doğrulamasında geçici bir süreç için `PreventUserIdleSystemSleep`
isteği görüldü; izlenen süreç bitince koruma da kapandı. Bu doğrulama uygulama
verisi, gerçek emir veya yeni sanal oturum kullanmadı.

Yeni başlatıcı için 15 yalıtılmış süreç testi macOS Bash 3.2 ile geçti; iki
dolu port, hızlı yeniden başlatma, sinyalle kapanış ve koruma/sunucu hataları
kontrol edildi. Ayrı Astra/high incelemesinde bu kapsamda açık P1/P2 bulunmadı.
Tam CI sonucu ilgili PR'dadır; bu çalışma canlı kullanım uygunluğu incelemesi değildir.

## Önceki paket: ayrıntılı paper veri sorunu kaydı

Saat farkı, mum kalitesi/güncelliği, fiyat güncelliği, sembol kuralları ve
veri alma aşaması hataları sabit açıklamalarla kaydedilir. Güncel/arşiv
raporu son ayrıntılı sorunu ve tarihini gösterir; bu geçmiş olay kaydıdır,
şimdiki sağlık veya düzelme kanıtı değildir. Eski genel hatadan neden üretilmez.

Tanı aynı oturum işlemi içinde yazılır. Tekrarlayan aynı hata birleştirilir;
sağlıklı gözlem, yeniden başlatma veya yeni yetkilendirme sonrası tekrar ayrı
olaydır. Yalnız son 100 tanı tutulur; diğer denetim geçmişi korunur. Saat
gerilese de son eklenen geçerli kayıt kullanılır. Ham hata veya yanıt
metinleri rapora yazılmaz. Mevcut risk/giriş/çıkış kararları korunur; bozuk
sembol kuralları girişleri kapatır. Yedek biçimi değişmez.

Önceki PR #18 açık pozisyonlar dahil örneklenen en büyük düşüşü tamamladı;
CI 649 backend, 30 arayüz ve 5 tarayıcı akışı geçti. Bu ölçüm gözlenmeyen
aralığın/mum içinin gerçek maksimumunu bildiği anlamına gelmez.

Bu paket aynı clone'un geliştirme worktree'sinde, sentetik testlerle hazırlandı.
Gerçek hesap, anahtar veya borsa isteği kullanılmadı; yeni deney/giriş yetkisi
ve canlı etkinleştirme oluşturmaz. Son doğrulama ve bağımsız inceleme kanıtı
[inceleme kaydında](docs/REVIEW_DIAGNOSTICS.md), son commit CI sonucu PR'dadır.

Yerel sonuç: 486 backend başarılı / 178 PostgreSQL varyantı atlandı;
32 arayüz ve 5 tarayıcı akışı geçti. Ayrı Astra/high son incelemesinde açık
P1/P2 yok; temel sürüme karşı 1000 durum ve 6 işçi akışı karşılaştırıldı.
Lint, tip, derleme, gizli bilgi ve bağımlılık kontrolleri başarılı.

## Kalan kabul koşulları

- Testnet'te kısmi dolum ve olumsuz bağlantı senaryolarının yeterli doğrulaması;
  hesap çapında sürekli koruma, sahipsiz emir ve geçmiş kaybı kurtarması.
- BNB komisyon değerlemesi, desteklenmeyen aktif eski kayıtlar ve kalan küçük
  envanter; araştırmada mum içi portföy düşüşü bu paketle tamamlanmış değildir.
- En az 30 takvim günü ileri paper gözlem, yeterli bağımsız örneklem dışı kanıt,
  maliyet stresi ve piyasa koşulları. Yazılım testleri ekonomik avantaj değildir.
- Bağımsız kesinti bildirimi/kurtarma, fiziksel PostgreSQL kurtarma, Docker ve güvenli uzak işletim
  doğrulaması; son bağımsız üretim incelemesi ve uygulama içi açık kullanıcı onayı.

Kurulum ve kurtarma: [OPERATIONS.md](docs/OPERATIONS.md).
Testnet kapsamı ve sınırları: [TESTNET.md](docs/TESTNET.md).
