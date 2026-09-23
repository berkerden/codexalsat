# Yerel operasyon ve kurtarma

## Sürüm ve durdurma

API `/api/status` commit bilgisini verir. Yerel çalışma `git status --short` temiz
olmadan sürüm etiketiyle eşdeğer sayılmaz. `main` güncellendiğinde çalışan süreç
kendiliğinden değişmez; paper girişlerini durdurun, açık pozisyonları inceleyin,
yedek alın ve seçilmiş commit'i kontrollü olarak çalıştırın. Eski commit'e geçişte
veri şeması uyumluluğunu kontrol edin; force push/reset --hard kullanmayın.

Panelde giriş durdurma ile pozisyon kapatma ayrı işlemdir. Kapatma isteği önce
kalıcı yazılır, sonra yeni kotasyonda kalan miktar için sürdürülür. Motor kapanırsa
simülasyon çıkışı bekler; gerçek borsa koruması varmış gibi yorumlanamaz.

## Yedek ve geri yükleme

Önce panelden yeni girişleri durdurun; ardından `Ctrl+C` ile backend'i kapatın.
SQLite dosyasını çalışan yazıcı varken kopyalamayın. Kapalı sistem için:

```bash
mkdir -p backups
cp -p data/paper.sqlite "backups/paper-$(date -u +%Y%m%dT%H%M%SZ).sqlite"
```

Geri yükleme öncesi mevcut dosyayı farklı bir adla koruyun. Yedeği `data/paper.sqlite`
konumuna kopyalayın ve backend'i başlatın. Başlangıç tüm giriş yetkisini sıfırlar;
açık pozisyonlar/fills/nakit korunur. `test_restore_verified_and_does_not_restore_authorization`
ve yeniden başlatma testleri mantıksal yedek geri yükleme değişmezlerini doğrular.
Yedekler kişisel veri içerir; Git'e veya genel paylaşıma yüklenmez.

PostgreSQL için backend kapalıyken `pg_dump` alın; boş ayrı test veritabanına
`pg_restore` yapıp `/api/paper` bakiyeleri/pozisyonları/fill sayılarıyla karşılaştırın.
Bu makinede PostgreSQL fiziksel dump/restore uygulanmadı; üretim öncesi dış engeldir.

## Kesinti

Eski/bozuk veri girişleri durdurur. Çıkış yönetimi yeni giriş araştırma servisinden
bağımsızdır ve güncel bid/ask geldiğinde devam eder. Metadata veya mum servisi
kesintisi güncel kotasyonla yapılabilen çıkışı bloke etmez. Yeni girişleri tekrar
başlatma açık kullanıcı eylemidir. Saat farkı ve veri kaybı gerekçeleri panelde görünür.

Paper fee varsayımı quote asset'tir. BNB veya base komisyonu ve gerçek account
reconciliation tamamlanmadan gerçek para moduna geçilemez. Testnet sonuçları gerçek
likidite/başarı kanıtı değildir. Canlı anahtarları sohbet, frontend veya Git'e yazmayın.

## Sunucuya geçiş

Aynı uygulama Docker/PostgreSQL ile ayrı, sürekli açık Linux makinede çalıştırılabilir.
Önce sadece paper kurulumunu taşıyın, UTC saat senkronizasyonu ve yeniden başlatma
senaryolarını test edin. Uzak erişimi authenticated HTTPS ters proxy üzerinden sağlayın;
mevcut loopback geliştirme API'sini doğrudan internete açmayın. VPS seçimi ve ücret
kullanıcı onayına bağlıdır. Bu görevde ücretli sunucu kurulmadı.
