# Durum — 2026-11-06

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

## Son paket: ayrıntılı paper veri sorunu kaydı

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
- Gözetimsiz servis, fiziksel PostgreSQL kurtarma, Docker ve güvenli uzak işletim
  doğrulaması; son bağımsız üretim incelemesi ve uygulama içi açık kullanıcı onayı.

Kurulum ve kurtarma: [OPERATIONS.md](docs/OPERATIONS.md).
Testnet kapsamı ve sınırları: [TESTNET.md](docs/TESTNET.md).
