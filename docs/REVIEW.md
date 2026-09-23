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
