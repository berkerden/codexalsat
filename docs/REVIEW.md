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
