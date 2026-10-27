# Kritik inceleme ve kanıt kaydı

23 Eylül 2026. Uygulamayı yazan bağlamdan ayrı Astra/high incelemesi;
`critical_review` görevinin hedefli tekrar doğrulaması. Bu, canlı kullanım onayı değildir.

| Önem | Bulgu | Düzeltme / doğrulama |
|---|---|---|
| P1 | Metadata/mum arızasının açık pozisyon çıkışını engellemesi | Çıkış döngüsü yalnız güncel kotasyon ister; API hata senaryosu testleri |
| P1 | Ağ hatasında manuel kapatma niyetinin kaybolması | İstek kotasyondan önce kalıcı yazılır; sonraki döngüde devam eder |
| P1 | Kısmi stop çıkışının fiyat toparlandığında bırakılması | Çıkış gerekçesi tam doluma kadar kilitlenir; bağımsız senaryo doğrulandı |
| P1 | Kayıp sayacının yalnız son kısmi doluma bakması | Pozisyon toplam gerçekleşmiş K/Z kullanılır; sayısal regresyon |
| P1 | Staged sır taramasının index yerine çalışma dosyasını okuması | Gerçek index blob'ları, NUL dosya listesi; staged sır/untracked/deleted/env testleri |
| P1 | Boşluklu mumların yeterli tarihçe gibi kabulü | Aralık genişliği ve süreklilik zorunlu; kalite testleri |
| P1 | Walk-forward/benzerlik örneklemesinde gelecek veya örtüşme riski | Yalnız önceki eğitim seçimi, ayrık anchor aralıkları, kronolojik blok CI |
| P1 | Kapanmış işlem düşüşünün tam portföy DD gibi etiketlenmesi | Ayrı closed_trade_equity_drawdown adı; tam portföy DD açıkça mevcut değil |
| P1 | Farklı coin envanterlerinin karışması | Envanter anahtarı scope + symbol; çapraz parite testi |
| P1 | İptal edilmiş ama henüz muhasebeleşmemiş fill rezervi | Cumulative-confirmed miktar korunur; geç fill ve çift satış testi |
| P1 | Eşzamanlı emir yazıcılarının aynı envanteri kullanması | SQLite immediate transaction / PostgreSQL advisory lock; yarış testleri |

Son bağımsız kontrol, incelenen kapsamda açık P1 bırakmadı. Sonrasında gerçek
PostgreSQL 16 ile CI tamamlandı: toplam 83 backend testi geçti; böylece daha önce
mock/kod düzeyinde olan PostgreSQL kilit yolu da veritabanında sınandı.

Entegrasyon: frontend eski kotasyonu 5 saniye sonunda engelli gösterir; coin/periyot
seçimi eski grafiği temizler. Araştırma başarısızlığı sinyal kartında açık görünür.
API canlı etkinleştirmeyi 409 ile reddeder; ortam değişkeni bu kilidi kaldırmaz.

Canlı öncesi tamamlanmamışlar STATUS.md'de açık tutulur. Testnet transportu, borsa
koruyucu emirleri, farklı fee varlıkları, gerçek hesap uzlaştırması ve ileri gözlem
olmadan üretim emir güvenliği veya ekonomik avantaj iddia edilemez.


## Testnet incelemesi — 2–5 Ekim 2026

Ayrı `testnet_design_review` Astra/high bağlamı gerçek değişiklikleri ve hedefli
testleri inceledi. Bu kapsamda P1 bildirmedi; üç P2 bulgusu düzeltildi:

- Yeniden başlatmada LIMIT fiyat/tür/GTC kimliği doğrulanmıyordu. ExchangeUpdate
  alanları ve kalıcı niyet karşılaştırması eklendi; yanlış yanıt UNKNOWN bırakır.
- Bilinmeyen veya değerlendirilmemiş borsa filtreleri yeni emri engellemiyordu.
  Gönderim kapısı bu kuralları reddeder; sorgulama yolunu engellemez.
- HALT durumunda metadata kontrolü mevcut emir sorgusunu da durduruyordu.
  Yeni emir uygunluğu metadata okuma ve kurtarma yolundan ayrıldı.

Astra tekrar kontrolünde ilk ve üçüncü bulguyu doğruladı; MIN_NOTIONAL için
aynı kapıda ek eksik buldu. Bu son kural engel listesine alındı ve yeni emir
gönderilmezken sorgunun sürdüğünü sınayan bağımsız beklenen sonuçlu test eklendi.
Son düzeltme ana ajan tarafından doğrulandı; Astra'nın kullanım sınırı nedeniyle
ondan ek kapanış değerlendirmesi alınmadı. Yerel tam paket: 108 başarılı,
36 PostgreSQL senaryosu servis olmadığı için atlandı. CI ayrıca değerlendirilir.

OCO hedef/stop tek rezerv kullanır; ALL_DONE tek başına rezerv bırakmaz.
Anahtarlı Testnet işlemi, otomatik kısmi giriş→koruma akışı ve üretim uygunluğu
kanıtlanmış değildir. Bu sınırlı entegrasyon temeli canlı kullanım onayı değildir.

## Süreli testnet çevrimi — 6 Ekim 2026

Ayrı Astra/high incelemesi yeni filtreleri, süreli giriş→OCO denetleyicisini,
komut satırını ve gerçek adaptör üzerinden taklit HTTP testlerini değerlendirdi.
Bulunan ve giderilenler:

- P1: Kalıcı durdurma, yeniden başlatmada yeni BUY gönderimini engellemiyordu.
  İlk kontrolden sonra, beklenen ağ ön kontrolleri tamamlanınca gönderimden hemen
  önce de kalıcı durdurma/süre kontrolü yapılır. Kesin gönderilmemiş sonuç ayrı
  SubmissionPrevented kaydıyla yerel reddedilir; belirsiz sonuç UNKNOWN kalır.
- P2: ABORTED deneme hiç gönderilmemiş emri borsada arıyordu. Yerel iptal kanıtı
  korunur, tekrar yönetim sorgu veya yeni BUY oluşturmaz.
- P2: Eski PROTECTED önbelleği belirsizleşen emir kaydına rağmen korunuyordu.
  Güncel emir NEW değilse veya beş saniyelik gözlem eskidiyse koruma doğrulanmış sayılmaz.
- P2: BUY öncesinde hedef/stop ve OCO türleri kontrol edilmiyordu. Gelecekteki
  tam miktarlı koruma, varsayımsal satın alınmış bakiye ile hem ilk ön kontrolde hem
  son BUY gönderiminden önce doğrulanır; gerçek koruma dolum/komisyon sonrası tekrar sınanır.
- P2: Quote MAX_ASSET için piyasa fiyat tahmini kesin sınır kabul ediliyordu.
  MARKET/OCO bu özel kural altında engellenir. MAX_POSITION için belgede açık
  olmayan kısmi miktar yorumu yerine toplam orijinal açık BUY miktarı muhafazakâr kullanılır.

Son bağımsız hedefli kontrolde 65 test geçti, 20 PostgreSQL varyantı yerel servis
olmadığından atlandı. İncelenen sınırlı testnet kapsamında açık P1/P2 bildirilmedi.
Ana ajanın tüm yerel paketi: 157 başarılı, 58 PostgreSQL varyantı atlandı; bunlar
CI’da çalıştırılır. Gerçek hesap anahtarı, imzalı hesap denemesi veya gerçek Testnet
emri kullanılmadı. Üretim onayı ve strateji başarısı iddia edilmez.

## Tam sıfır komisyon kanıtı — 13 Ekim 2026

Sol/high uygulaması sonrası ayrı Astra/high bağlamı gerçek kaynak farkını ve
testleri inceledi. Standart/vergi/özel komisyonun on iki oranı eksiksiz ve tam
sıfır olmadıkça bilinmeyen etkin indirim varlığı istisnası açılmaz. Sıfır indirim
oranı tek başına yeterli değildir. Son gönderim ön kontrolü BUY, SELL ve OCO için
yenilenir. Gerçekleşmiş sıfır ücretin boş varlık işareti hem taban hem karşılık
ücretinin sıfır olmasını gerektirir; pozitif desteklenmeyen ücret UNKNOWN ve
envanter rezervini korur.

Bağımsız hedefli test: 122 başarılı, yerel PostgreSQL olmadığı için 31 atlandı.
Kapsam içinde açık P1/P2 bulunmadı. Ana ajan tam yerel paket: 212 başarılı,
60 PostgreSQL varyantı atlandı; lint ve strict tip kontrolü başarılı. CI sonucu
ilgili PR üzerinde kaydedilir. Bu inceleme üretim kullanım onayı değildir.

## Yanıt kaybı ve kısmi dolum kurtarması — 17 Ekim 2026

Sol/high, gerçek Testnet taşıyıcısı ve kalıcı denetleyici üzerinden durum tutan
MockTransport testleri ekledi. Ayrı Astra/high bağlamı kaynak farkını, ilgili
emir/koruma akışlarını ve testleri inceledi. P2 test verisi bulgusu düzeltildi:
iptal edilmiş kısmi emir örneğinde toplam dolum artık orijinal miktardan küçüktür.
İki 0,2 dolum ve toplam 0,0004 taban komisyon, 0,3996 net miktar; 0,399 koruma
ve 0,0006 toz üretir. Eksik işlem ayrıntıları tamamlanmadan koruma gönderilmez.

Son bağımsız çevrim paketi: 24 başarılı; yerel PostgreSQL servisi olmadığı için
24 varyant atlandı ve CI'a bırakıldı. Açık P1/P2 yok. BUY/iptal/OCO yanıt kaybı,
ardından sorgu hatası ve yeni taşıyıcı/yöneticiyle kurtarma; tek gönderim, kalıcı
iptal talebi, komisyon ve envanter rezervi kontrol edildi. Uygulama kodu değişmedi.
Bu testlerde borsa HTTP yanıtları sahtedir; yeni nesneler süreç yeniden başlatmasını
benzetir. Ayrı anahtarlı deneme, yerelde atılan kabul yanıtlarından gerçek yeni
süreçlerle NO_FILL kurtarmasını doğruladı; gerçek ağ kesintisi veya anahtarlı
kısmi dolum kanıtı değildir. [Ayrıntılı kanıt ve sınırlar](TESTNET.md).

## Başlangıç açık emir denetimi — 18 Ekim 2026

Sol/high uygulama taslağı ve ana oturumun tamamladığı düzeltmeler, ayrı Astra/high
bağlamında incelendi. İnceleme sırasında giderilen P2 bulguları:

- Hiç gönderilmeyen girişin yeni denetim gerekçeleri yeniden başlatmada tanınmıyordu.
  İzinli gerekçeler korunur; yönetici borsada olmayan emri sorgulamadan ABORTED'a geçer.
- Son denetimin ağ çağrıları önceki ön kontrolü eskitebiliyordu. Yaş, saat sapması,
  süre ve durdurma koşulları son çağrıdan sonra yeniden sınanır.
- Katı denetim ayrıştırıcısı çıkış kapasite kontrolünü etkileyebiliyordu. Ayrı
  ayrıştırma mevcut koruyucu yönetimi giriş engellerinden bağımsız tutar.
- Kapasite ve yerel kayıtlar farklı anlardan gelebiliyordu. Borsa emir kapasitesi
  son ön kontrolle karşılaştırılır; yerel kayıtlar yazıcı kilidi altında tutarlı
  okunur ve gönderim yetkisinde tekrar karşılaştırılır.

İstisna metni yerine yalnız izinli gerekçe kodları saklanır. OCO çocuk kimlikleri
deterministik kimliklerle doğrulanır. Son bağımsız incelemede açık P1/P2 yok.
Ana oturumun son hedefli paketi 131 başarılı, yerel PostgreSQL olmadığı için
36 atlandı. Yeni HTTP sınırı testleri ayrıca Astra tarafından incelendi; ağ
yanıtları MockTransport ile benzetilir. Salt okunur anahtarlı denetim geçti;
bu paket yeni Testnet emri veya üretim isteği göndermedi. Hesap bağlama, tarihsel
tam uzlaştırma ve otomatik reset kurtarması kapsam dışında kalır.

## Kalıcı hesap bağlama ve geçmiş kaybı — 18 Ekim 2026

Astra/high sözleşmesiyle başlayan Sol/high uygulaması, kullanım sınırından sonra
ana oturumda tamamlandı. Ayrı Astra/high kaynak farkını ve testleri inceledi.
İncelemede giderilen bulgular:

- P1: henüz borsa kabulü doğrulanmamış emrin geçici yokluğu kalıcı geçmiş kaybı
  sayılabiliyordu. Kalıcı engel artık önceki borsa kimliği kanıtını gerektirir;
  belirsiz giriş sonradan dolarsa koruması bu yanlış gerekçeyle engellenmez.
- P2: aynı göreli SQLite URL'si başka dosyaları gösterebiliyordu. Taşıyıcı yalnız
  aynı defter nesnesini kabul eder; başka nesneye yeniden bağlanmaz.
- P2: komut satırı hata yakalayıcısı hesap doğrulaması başarısızken bile çevrimin
  stop kaydını değiştirebiliyordu. Kimlik hataları, bağlantı/izin/bozuk yanıt dahil,
  ayrı ve temiz gerekçeyle çıkar; önceki çevrim ve emir kanıtları korunur.
- Sayısal metin emir kimlikleri geçmiş kaybı eşleştirmesine dahil edildi. Son
  kimlik sorgusundan sonra süre/güncellik yeniden kontrol edilir. Kesin olarak
  gönderilmemiş OCO talebi aynı kimlikle yeniden denenebilir; belirsiz gönderim değil.

Son bağımsız hedefli doğrulama: 172 başarılı, PostgreSQL servisi olmadığı için
51 varyant atlandı. Kapsam içinde açık P1/P2 kod bulgusu yok. Tam yerel paket:
277 başarılı, 95 PostgreSQL varyantı atlandı; lint ve strict tip kontrolü geçti.
CI sonucu ilgili PR'dadır. Eski anahtarlı yerel defter migration_required verdi;
emir/dolum/koruma/çevrim satırları değişmedi. Bu paket borsada değişiklik yapmadı.
Hesap bağlama eski kayıtların sahipliğini veya reset dönemini kendiliğinden kanıtlamaz.
Kalıcı geçmiş kaybı engeli iptal ve yeni korumayı da durdurur; doğrulanmış sorgular
sürer. Eski defter geçişi, engel çözümü ve otomatik reset kurtarması henüz yoktur.


## Eski defterin doğrulanmış geçişi — 19 Ekim 2026

Astra/high sözleşmesi ve ilk kaynak incelemesi, Sol/high uygulamasıyla yürütüldü.
Her iki oturum kullanım sınırına ulaştı; kalan düzeltmeler ve testler ana oturumda
tamamlandı. Bağımsız son inceleme tamamlandı iddiası yoktur.

İlk incelemede bulunan iki geçerli akış hatası giderildi: bağımsız LIMIT emrinin
`orderListId=-1` değeri reddediliyordu; EXITED çevriminde koruma zarfı ile emir
farklı türlerde karşılaştırılıyordu. Ayrıca terminal durum/miktar tutarlılığı,
OCO liste zamanı ve kapanışı, çevrimin stop fiyatı, kilit sonrası güncellik ve açık
emirlerde OCO çocuk/liste sayısal kimlik kontrolü tamamlandı. Boş ama önceden var
olan geçiş makbuzu tablosu otomatik kullanılmaz; tutarsız durum olarak engellenir.

Sabit Testnet adresli ayrı okuyucu yalnız GET gönderir. Tam dolum kümeleri ve
komisyonlar eşleşmeden bağlama yapılmaz. Testler eksik/fazla/değişmiş dolumu,
farklı hesap kimliğini, kayıp geçmişi, yanlış emir/OCO şartlarını, açık çocuk
emirleri, yerel kayıt yarışını, kilit beklerken süre aşımını ve makbuz yazımı
hatasında bağlamanın da geri alınmasını sınar. Önizleme, açık uygulama seçimi,
yeniden açma ve dolum sayfalaması da sınanır. PostgreSQL varyantları CI'a bırakılır.
Son yerel geçiş paketi 50 başarılı; 49 PostgreSQL varyantı atlandı. Tam backend
paketi 327 başarılı, 144 atlandı; lint ve strict tip kontrolü geçti. Anahtarlı
önizleme ve uygulama geçti, yedek alındı, tarihsel satırlar değişmedi; ardından
normal başlangıç denetimi de geçti. Bu kapsam üretim izni, reset kurtarması veya
strateji kârlılığı kanıtı değildir.


## Eski defter geçişinin bağımsız son incelemesi — 20–21 Ekim 2026

Astra/high, PR #12'nin kaynak ve testlerini ayrı bağlamda yeniden inceledi;
önceki başarı özeti yerine sahte HTTP yanıtlarıyla üç P2 bulgusunu yeniden üretti.
Sol/high aşağıdaki düzeltmeleri ve regresyon testlerini ekledi:

- Eksik `commissionAsset` alanı, açık `null` veya boş değerli sıfır komisyonla
  karışabiliyordu. Alanın varlığı zorunlu; eksik alan bağlamayı engeller. Açık
  `null`/boş değer yalnız sıfır ücrette desteklenir.
- Ön sıfırlı borsa kimlikleri sayıya çevrilerek kabul ediliyor, yerel kimlik
  değişmediği için sonraki normal uzlaştırma çelişebiliyordu. LIMIT ve OCO
  kimliklerinin kanonik biçimi zorunludur; tarihsel kimlikler düzeltilmez.
- Yalnız yerel oluşturma zamanı çevrim süresiyle sınanıyordu. Borsanın alış
  oluşturma zamanı da saklanmış giriş son tarihi + mevcut 1000 ms toleransıyla
  karşılaştırılır; izin süresi uzatılmaz. +1000/+1001 ms sınırları sınanır.

Başarısız geçişlerde geçmiş satırların korunması, hesap bağlaması ve makbuz
oluşmaması test edilir. Bu pakette borsaya istek veya emir gönderilmedi.
Mevcut yerel defterin yalnız toplu kontrolü, dolumların tamamında boş olmayan
komisyon varlığı bulunduğunu doğruladı; eksik alanın boş yerel değerle eşleştiği
ilk bulgunun örneği bu kayıtlarda yoktur. Eski geçiş yeniden uygulanmadı.


21 Ekim’de ayrı Astra/high bağlamı üç düzeltmeyi ve ilgili normal LIMIT/OCO
uzlaştırma entegrasyonunu yeniden doğruladı. İncelenen kapsamda açık P1/P2 yok.
Bağımsız hedefli paket 249 başarılı; yerel PostgreSQL olmadığı için 134 varyant
atlandı. Tam yerel paket 335 başarılı, 152 PostgreSQL varyantı atlandı; lint,
strict tip kontrolü ve diff kontrolü geçti. PostgreSQL sonucu ilgili PR CI'ında
kaydedilir. Bu inceleme üretim kullanım onayı değildir.


Aynı PR'ın ilk CI koşusunda 487 backend testi geçti; bağımlılık taramaları
Vite 8.3.0 ve certifi 2026.7.22 için güncel açık bildirdi. Vite 8.3.5'e,
certifi 2026.10.19'a güncellendi. Ayrı Astra/high dar fark incelemesinde
beklenmeyen bağımlılık, betik veya güvenlik ayarı değişikliği bulmadı.
Yerel pip-audit ve npm audit temiz; 335 backend testi, 5 arayüz testi, 2 tarayıcı
akışı ve arayüz lint/tip/build kontrolü güncel bağımlılıklarla geçti.


## Mevcut çevrim izleyicisi — 23 Ekim 2026

Astra/high tasarımı ve ilk kaynak incelemesini, Sol/high ilk uygulamayı yaptı.
Uygulama oturumunun kullanım sınırından sonra ana oturum kalan hata düzeltmelerini
ve testleri tamamladı. İlk inceleme sonrasında hata yakalandığında eski PROTECTED
bilgisinin sağlıklı sayılması engellendi; okunamayan durum sıfır miktar yerine
bilinmiyor olarak gösterilir. Geçici hata sürerken süre dolması başarılı kapanış
sayılmaz. PostgreSQL kilidinin edinilme yanıtı belirsizse bağlantı havuza dönmeden
geçersizleştirilir; oturum kilidi açık kalmaz. Onaysız veya kilidi alamayan eski
yönetici komutunun hata yolundan kalıcı giriş durdurma yazması engellendi.

Testler salt okunur izlemeyi, kısmi dolum/iptal sonrası korumayı, kabul edilmiş OCO
yanıtı kaybolduktan sonra aynı niyetle yeniden başlatmayı, kilit çekişmesi/kaybını,
gerilemeli yeniden sorgulama sınırını, kesintide ek ağ isteği yapılmamasını ve
eski koruma bilgisinin başarısız kontrolde taze gösterilmemesini kapsar.
Gerçek Binance isteği veya emir gönderilmedi. Ayrı Astra/high son incelemesi
gerçek kaynak ve testleri inceledi, hedefli paketi bağımsız çalıştırdı: bu kapsamda
açık P1/P2 yok. Tam yerel paket 367 başarılı, PostgreSQL servisi olmadığı için
158 varyant atlandı; lint ve strict tip kontrolü geçti. PostgreSQL CI kanıtı
ilgili PR'da kaydedilir. Üretim kapalıdır.


İlk izleyici CI koşusunda 525 backend testi (SQLite + PostgreSQL 16),
5 arayüz testi ve 2 tarayıcı akışı geçti. Ön yüz taramasının yüksek önem eşiği
CI'ı geçirse de günlükler Vitest mocker için iki orta düzey açık bildirdi
(GHSA-qxg6-89mg-rqv8, GHSA-xp26-3454-mjgp). Vitest 5.0.5'e sabitlendi;
yerel npm audit sıfır açık, lint/tip/build, 5 arayüz testi ve 2 tarayıcı akışı
başarılı. Son commitin CI sonucu ilgili PR'da kaydedilir.


## Panelden mevcut çevrim yönetimi — 26 Ekim 2026

Astra/high tasarım ve bağımsız kritik inceleme; Sol/high backend ve Sol/medium
arayüz uygulaması. Backend uygulama oturumunun kullanım sınırından sonra ana
oturum kalan düzeltmeleri, yaşam döngüsü testlerini ve bütünleştirmeyi tamamladı.

Bağımsız incelemede iki P2 yeniden üretildi ve düzeltildi:
- Durdur isteğinden önce başlayan geç durum yanıtı, yeni durdurulmuş görünümü
  eski sağlıklı koruma görünümüyle değiştirebiliyordu. Kontrol isteği önceki
  sorgu neslini geçersiz kılar; kontrol sürerken yeni sorgu başlatılmaz.
- Süresi geçmiş koruma gözleminde başlık güncel değil dese de ayrıntıda Korumalı
  ve sıfır korumasız miktar kalabiliyordu. Güncellik kaybolunca ayrıntı da
  doğrulanmadı ve mevcut miktar kadar doğrulanmamış maruziyet olarak gösterilir.

Astra/high iki düzeltmeyi ayrı regresyon testleriyle bağımsız doğruladı; kapsam
içinde açık P1/P2 yok. Yetki sınırı, tek görev sahipliği, yeniden başlatmada
boşta kalma, salt okunur durum sorgusu, sabit anahtar yolu ve yeni alış
açılamaması incelendi. Eşzamanlı başlat/durdur, temizlik beklenirken ikinci
durdurmanın temizliği kesmemesi, sunucu kapanışı, farklı Origin/oturum ve
SQLite/PostgreSQL salt okunur defter testleri eklendi.

Yerel backend 381 başarılı / 161 PostgreSQL varyantı atlandı; arayüz 17 test,
3 tarayıcı akışı, lint/tip/build başarılı. Masaüstü paneli sentetik verilerle
görsel incelendi; 390px mobil onay/başlat/durdur tarayıcı testi geçti.
Gerçek anahtar/hesap veya Binance isteği kullanılmadı. Üretim kapalıdır.

Güncel güvenlik taraması geliştirme bağımlılıklarında undici, zod ve filelock
açıkları bildirdi. undici 8.12.3, zod 4.6.7 ve filelock 4.2.1'e dar kapsamlı
güncelleme yapıldı. Ana bağımlılık aralıkları ve güvenlik ayarları değişmedi.
Son sürüm CI ve tarama sonucu ilgili PR'da kaydedilir.

Astra/high dar son fark incelemesi üç bağımlılık sürümü dışında paket, betik,
CI veya güvenlik ayarı değişmediğini doğruladı. Saat güncellemesi ve Türkçe
etiketler önceki iki düzeltmeyi koruyor; bağımsız regresyonlar tekrar geçti.
Güncellenen bağımlılıklarla yerel tam backend, 17 arayüz, 3 tarayıcı testi ve
npm audit / pip-audit taramaları geçti (bilinen açık yok).


## Paper deney arşivi — 27 Ekim 2026

Astra/high tasarım, Sol/high backend ve Sol/medium arayüz uygulaması. Kullanım
sınırından sonra ana oturum eksik kontrolleri ve düzeltmeleri tamamladı.
Astra/high bağımsız son incelemesi kaynak ve gerçek test çıktılarıyla yapıldı.

Giderilen bulgular:
- P1: geri yükleme eksik/negatif pozisyon alanlarını ve JSON sayısı biçimindeki
  parasal alanları kabul ediyordu. Eksik stop işçiyi durdurabiliyor; float nakit
  ondalık muhasebeyi bozuyordu. Şema 1 ve 2 için para yalnız sonlu ondalık metin,
  pozisyon/kapsam/sayaç/zaman ve emir/dolum/denetim kayıtları yapısal olarak
  doğrulanır. Geçersiz yedek hedefe yazılmadan reddedilir.
- P2: ayarı eksik ama eski bakiye/dolum içeren yedek yeni sermayeyle eski
  sonuçları karıştırabiliyordu. Yapılandırmasız durum gerçekten boş olmalıdır.
- P2: aynı arşive yeniden tıklama veya hata sonrası aynı kaydı açma sonsuz
  yükleniyor durumunda kalabiliyordu. Her seçim yeni ayrıntı isteği üretir.

Bağımsız tekrar doğrulamada açık P1/P2 yok. Ana oturumun testleri geçmiş GET
yanıtını, eski işçi başarı/hatasını, gecikmiş başlatma/kapatmayı, çift arşivleme
ve arşiv/başlat yarışını, INSERT sonrası UPDATE hatasında tam geri almayı
kapsar. Bağımsız 8.34 birim örneğinde 1014.99532 sanal nakit, 14.99532 K/Z
ve iki dolum arşivde korunur. Çoklu arşivli yedek farklı boş hedefe taşınır;
yeni deney 2000 sanal sermaye ve sıfır geçmişle başlar.

Masaüstünde geçici sentetik veriyle arşivleme, boş form ve salt okunur ayrıntı
görsel doğrulandı; 390px mobil akış tarayıcı testiyle sınandı. Gerçek hesap,
API anahtarı veya Binance isteği kullanılmadı. PostgreSQL varyantları CI'da
çalışır; bu inceleme canlı kullanım onayı değildir.

Son yerel paket: 434 başarılı backend testi, yerel PostgreSQL servisi olmadığı
için 167 varyant atlandı; 22 arayüz testi ve 4 tarayıcı akışı geçti. Lint,
strict tip, build ve diff kontrolleri başarılı. npm audit sıfır açık, pip-audit
bilinen açık yok. PostgreSQL ve son commit CI kanıtı ilgili PR'da kaydedilir.

İlk arşiv CI koşusu 601 backend testi (SQLite + PostgreSQL 16), 22 arayüz
ve 4 tarayıcı testini geçti. Güncel pip-audit taraması msgpack 1.2.2 için
CVE-2026-114613 bildirdi; bağımlılık 1.2.3 düzeltme sürümüne sabitlendi.
Son sürümün tarama ve CI sonucu ilgili PR'da kaydedilir.
