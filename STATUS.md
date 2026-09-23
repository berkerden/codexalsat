# Durum — 2026-09-23

Yerel kaynak gerçek `berkerden/codexalsat` clone'udur. Public depo kullanılır.
PR #3 araştırma/paper sürümünü taşır; #2 canlı öncesi kalan işleri izler.

- A0/A1: GitHub, CI, SPEC ve yerel kurulum hazır. Tamamlanan kaynaklar küçük
  commitlerle gönderiliyor; bağlı GitHub API ve aynı clone'a fetch kullanılır.
- A2: Global gerçek REST/WS, kapalı mum doğrulama, kalite/saat kontrolleri ve
  sürümlü Parquet arşivi çalışıyor. Binance TR doğrulanmış adaptör değildir.
- A3: 3 nedensel strateji, 9 aday/grup, kronolojik ayrım, purge, walk-forward,
  maliyet stresi ve benzer örüntü analizi. Gerçek 10 grubun tamamında avantaj
  desteklenmedi; [sayısal sonuçlar](docs/RESULTS.md). Testle yeniden optimizasyon yok.
- A4/A5: Türkçe panel, Decimal maliyet/marj, kalıcı risk sınırlı paper motoru,
  süreli giriş yetkisi ve çıkış yönetiminden ayrı durdurma kontrolleri hazır.
- A6: kalıcı emir niyeti, belirsiz yanıt sorgulama, kısmi dolum/iptal ve bot
  envanteri uzlaştırma çekirdeği test edildi. Gerçek testnet/üretim emir
  transportu ve borsa tarafı koruma adaptörü uygulanmış değildir.
- A7: ayrı Astra bağlamında kritik inceleme ve hedefli tekrar doğrulama yapıldı;
  kapsam içindeki P1 bulguları giderildi. [İnceleme kaydı](docs/REVIEW.md).
- A8: temiz GitHub clone'unda kurulum, iki sunucunun başlatılması ve supervisor
  kapanınca iki portun da serbest kalması doğrulandı. Kurtarma testleri ve
  [operasyon](docs/OPERATIONS.md) belgesi mevcut. Docker bu makinede yok.

Doğrulama: 6df94d1 üzerinde GitHub CI üç işi geçti; backend 84 test (SQLite +
PostgreSQL 16), frontend 5 birim test; 2 masaüstü/mobil tarayıcı akışı, lint/typecheck/build ve
bağımlılık güvenlik taramaları başarılı. Son entegrasyon değişiklikleri aynı
kontrollerden yeniden geçirilir. Yerelde PostgreSQL senaryoları atlanır.

Gerçek tarayıcıda BTC/SOL, periyot değişimi, araştırma formu, sanal oturum başlatma/durdurma,
canlı mod kilidi ve 390×844 mobil menü/durdurma çubuğu kontrol edildi. Deneme paper oturumu durduruldu, açık pozisyon yok.
Bu kısa kontrol ileri gözlem süresi yerine geçmez.

Canlı **kapalı**. Eksikler: testnet ve koruyucu emir entegrasyonu, BNB/base komisyon
muhasebesi, pozisyon içi portföy maksimum düşüşü, 30 takvim günü ileri paper,
yeterli ekonomik avantaj, üretim operasyon değerlendirmesi. PostgreSQL mantıksal
kurtarma ve eşzamanlılık CI'da sınanır; fiziksel pg_dump/restore ve Docker testi
ayrıca gerekir. Kullanıcı canlı para işlemi yetkisi vermedi; geliştirmede emir yok.

Main koruma kuralını kaydetme otomatik onay incelemesince erişim politikası
değişikliği olarak reddedildi; özel onay sorusu yanıtlanana kadar etkinleştirilmez.
CI başarılı PR birleştirme yetkisi kullanıcı tarafından verildi.

Model devirleri: Terra medium iskelet; Sol high veri, finans/araştırma ve emir/secret
çekirdeği; Sol medium arayüz; ayrı Astra high inceleme. Kullanım sınırları çalışmayı
kesintiye uğrattı. Sayısal token/maliyet ölçümü yok; tasarruf yüzdesi iddiası yok.
