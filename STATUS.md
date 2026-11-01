# Durum — 2026-11-02

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

## Son paket: gözlenen en büyük portföy düşüşü

Geçerli fiyatla işlem öncesi ve sonrası değerlemelerde, açık pozisyonlar ve
kısmi satış maliyetleri dahil en büyük düşüş korunur. Fiyat toparlanınca anlık
düşüş azalabilir, geçmiş maksimum silinmez. Mevcut değerleme ve risk eşikleri
korunur. Geçersiz/eski fiyat ölçümü yenilemez; eski bilinmeyen geçmiş geriye
üretilmez. Bu yalnız örneklenen değerlerin maksimumudur, gözlenmeyen aralık
veya geçmiş mum içindeki gerçek maksimum değildir.

Güncel/arşiv raporu fiyatın rapor anındaki yaşını ve eksik geçmişi gösterir.
İlk geçerli değerleme öncesinde değer bilinmiyor olarak kalır. Yedek şema 4
bu bilgiyi korur; şema 1/2/3 desteklenir.

Yerel doğrulama: 471 backend testi başarılı, yerel PostgreSQL bulunmadığı için
178 varyant atlandı; 30 arayüz ve 5 tarayıcı akışı geçti. Lint, strict tip,
iki derleme ve gizli bilgi taraması başarılı. minimatch 11.0.1 güvenlik
düzeltmesi sonrası arayüz paketi yeniden geçti; iki bağımlılık taraması temiz.
Ayrı Astra/high son incelemesinde açık P1/P2 yok; hedefli 78 test başarılı,
17 PostgreSQL varyantı atlandı. [İnceleme](docs/REVIEW_DRAWDOWN.md).
Son commitin PostgreSQL ve CI kanıtı ilgili PR'da kaydedilir.

Geliştirme aynı clone'un ayrı worktree'sinde yapıldı. Bu paket çalışma
sürümünü, giriş yetkisini veya risk ayarlarını kendiliğinden değiştirmez;
canlı etkinleştirme, yeni deney veya gerçek hesap/borsa isteği içermez.

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
