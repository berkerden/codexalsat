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
