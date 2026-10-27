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

## Ayrı paper deneyi

Mevcut paper oturumunun sembol, periyot ve risk kapsamı sabittir. Farklı bir
coin veya strateji denemek için Paper bölümünden:

1. **Girişleri durdur** ile yeni sanal girişleri kapatın.
2. Açık pozisyon varsa **Paper pozisyonunu kapat** isteğini verin ve güncel
   kotasyonla kalan miktarın sıfırlanmasını bekleyin. Kısmi çıkış tamamlanmadan
   arşivleme yapılamaz.
3. Oturumun geçmişiyle arşivlenmesine ilişkin onayı seçip arşivleme düğmesini
   kullanın. Kayıtlı ayarlar, sanal bakiye, emirler, dolumlar ve denetim kayıtları
   aynı veritabanında salt okunur geçmiş olarak korunur.
4. Boş deney formunda yeni coin/periyot ve sanal risk sınırlarını seçin. Yeni
   deney yalnız **Paper oturumu başlat** ile ayrıca yetkilendirilir.

Arşivleme giriş yetkisi vermez, pozisyon kapatmaz ve geçmişi silmez. Etkin veya
açık pozisyonlu oturum reddedilir. Arşivler Paper bölümünde listelenir ve
ayrıntıları okunabilir; yeniden etkinleştirme veya silme kontrolü yoktur.
Her deneyin bakiyesi ayrıdır; birden çok deneyin getirisi tek hesap performansı
veya kesintisiz ileri gözlem süresi olarak birleştirilmez. Eski oturuma ait
gecikmiş istekler yeni deneyde çalıştırılmaz.

Tamamen ayrı yerel veri alanı gerekiyorsa sunucular kapalıyken
`SPOTLAB_DATA_DIR=data/ikinci-deney bash scripts/run-local.sh` kullanılabilir.
Bu dizinin ayrı sanal bakiyesi ve piyasa arşivi olur. Önceki dizine dönüldüğünde
girişler kapalı başlar. Paper arşivleme Testnet defterini etkilemez; gerçek hesap
sıfırlaması veya canlı risk sınırını aşma yolu değildir.

## Yedek ve geri yükleme

Önce panelden yeni girişleri durdurun; ardından `Ctrl+C` ile backend'i kapatın.
SQLite dosyasını çalışan yazıcı varken kopyalamayın. Kapalı sistem için:

```bash
mkdir -p backups
cp -p data/paper.sqlite "backups/paper-$(date -u +%Y%m%dT%H%M%SZ).sqlite"
```

Geri yükleme öncesi mevcut dosyayı farklı bir adla koruyun. Yedeği `data/paper.sqlite`
konumuna kopyalayın ve backend'i başlatın. Başlangıç tüm giriş yetkisini sıfırlar;
açık pozisyonlar, dolumlar, nakit ve arşiv tablosu korunur. `test_restore_verified_and_does_not_restore_authorization`
ve yeniden başlatma testleri mantıksal yedek geri yükleme değişmezlerini doğrular.
Programatik `PaperEngine.backup` mantıksal yedeğinin sürüm 2 biçimi mevcut
oturumu ve bütün arşivleri aynı işlemden okur. `restore` yalnız henüz
yapılandırılmamış ve arşivsiz hedefe uygulanabilir; mevcut deney veya arşiv
üzerine yazmaz. Eski sürüm 1 yedekleri de boş hedefe yüklenebilir. Geri yükleme
aktif oturuma yeni kimlik verir, geçmiş arşiv kimliklerini korur ve girişleri
kapalı bırakır. Panelde geri yükleme veya arşiv silme yolu bulunmaz.

Yedekler kişisel veri içerir; Git'e veya genel paylaşıma yüklenmez.

PostgreSQL için backend kapalıyken `pg_dump` alın; boş ayrı test veritabanına
`pg_restore` yapıp `/api/paper` bakiyeleri/pozisyonları/fill sayılarıyla karşılaştırın.
Bu makinede PostgreSQL fiziksel dump/restore uygulanmadı; üretim öncesi dış engeldir.

## Kesinti

Testnet için aynı yerel emir defterini koruyun ve önce salt okunur denetimi çalıştırın:

```bash
.venv/bin/python -m spotlab.testnet_cli audit
```

Engel varsa özgün çevrim/niyet üzerinden [Testnet uzlaştırma adımlarını](TESTNET.md)
uygulayın. Açık emir denetimi yeni girişleri engelleyebilir; doğrulanmış aynı hesapta
mevcut çıkış yönetimi sürer. Hesap kimliği doğrulanamıyorsa sorgu, iptal ve koruyucu
emirler de engellenir; başka hesaba eski defterin envanteriyle emir gönderilmez.
Açık emir listesinde görünmemek iptal kanıtı değildir. Testnet sıfırlaması, eksik
yerel kayıt veya farklı hesap şüphesinde defteri silmeyin; otomatik reset kurtarması
henüz yoktur. Denetim hesabın bakiyelerini veya özel emir kimliklerini yazdırmaz.

Eski sürümlerden kalan, henüz hesap kimliğine bağlanmamış defterin geçişi ayrıca
doğrulanmalıdır. Başarılı hesap bağlantısı eski emirlerin o hesaba ait olduğunu
kanıtlamaz. Bu sürüm eski deftere kendiliğinden hesap atamaz. Kayıtları koruyun;
engeli aşmak için defteri silmeyin veya boş bir veritabanına geçmeyin. Salt okunur
yerel durum ve hesap ön kontrolü kullanılabilir. API anahtarının değişmemesi,
Testnet sıfırlaması olmadığını kanıtlamaz.

Tümü kapanmış, dolumları tamamlanmış ve net bot envanteri sıfır olan eski defter
için önce `testnet_cli migrate-legacy` kontrolünü çalıştırın. Bütün eski emirler,
OCO ayakları ve dolum/komisyon kayıtları aynı hesabın borsa geçmişiyle eşleşmelidir.
Başarılı kontrolden sonra tutarlı veritabanı yedeği alın. `migrate-legacy
--confirm-testnet` aynı doğrulamayı yeniden yapar; değişmemiş deftere yalnız hesap
bağlaması ve geçiş kanıtı yazar. Ardından `testnet_cli audit` çalıştırın.
[Tam komutlar ve desteklenmeyen durumlar](TESTNET.md#verified-migration-of-a-historical-unbound-ledger).
Eksik tarihçe, açık pozisyon veya kalıcı hesap olayı bu yolla temizlenmez.

`account_history_missing`, daha önce borsada doğrulanmış bir emir/liste artık
bulunamadığında kalıcı olarak kaydedilir. Bu durumda aynı hesap doğrulansa bile
iptal ve yeni koruma dahil tüm emir değişiklikleri engellenir; doğrulanmış salt
okunur sorgular sürebilir. Sonradan başarılı sorgu engeli kendiliğinden kaldırmaz.
Borsada önceden kurulmuş emirler bu yerel engel nedeniyle iptal edilmez. Henüz
borsa kabulü doğrulanmamış bir emrin geçici yokluğu bu kalıcı engeli oluşturmaz;
özgün kimlikle sorgulanarak belirsizliği çözülür.

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


## Mevcut Testnet çevrimini uzun süre izleme

Terminalde proje klasörüne geçtikten sonra, mevcut çevrimin aynı kimliğiyle
izleyiciyi başlatın. Varsayılan yalnız borsa gözlemidir; süreyi `--run-seconds 60`
ile sınırlayabilirsiniz. `--confirm-testnet` mevcut girişin durdurulmasını ve
aynı çevrimin çıkış/koruma yönetimini etkinleştirir; yeni alış oluşturmaz.
[Komutlar ve hata koşulları](TESTNET.md#foreground-watcher-for-an-existing-cycle).

Terminali kapatmak veya bilgisayarı uyutmak izlemeyi keser. Ctrl+C sonrasında
aynı kimlik ve veritabanıyla yeniden başlatın. `attention` sonucu, açıkta kalan
miktar veya doğrulanamayan koruma incelenmeden yeni işlem başlatmayın. İzleyici
başka yöneticinin kilidini zorla kaldırmaz; kilit dosyasını silmeyin. PostgreSQL
bağlantısının kaybında süreç eski kilidi yeniden kazandığını varsaymaz.
Bu komut sunucu servisi, panelden etkinleştirme veya gerçek para izni değildir.


## Panelden Testnet izleme

Operasyon bölümündeki Testnet alanı, seçilmiş yerel defterin mevcut çevrimlerini
gösterir. Bu liste son yerel kayıttır; borsadaki güncel koruma kanıtı değildir.
Paneli açmak veya yenilemek borsa sorgusu ya da izleyici başlatmaz.

1. Açık mevcut çevrimi seçip **Yalnız izle** ile salt okunur izlemeyi başlatın.
2. Kalan alışın iptali ve mevcut envanterin korumasını yönetmek için paneldeki
   Testnet onayını seçin, ardından **Korumayı yönet** kullanın. Bu seçim kalıcı
   giriş durdurma ister; yeni alış ya da yenilenmiş giriş süresi oluşturmaz.
3. **İzleyiciyi durdur** yalnız arka plandaki izleme görevini keser. Emir iptali
   veya pozisyon kapatma değildir. Borsada kurulmuş emirlerin durumunu ayrıca
   doğrulayın; izleme durduğunda panel korumayı güncel olarak göstermez.

Tek izleyici bu backend sürecine aittir. Tarayıcı sekmesini kapatmak görevi kesmez;
`run-local.sh` ile açılan sunucuları kapatmak keser. Bu paket macOS açılış servisi
veya bilgisayar uykusunda çalışan bir servis kurmaz. Backend yeniden başladığında
izleyici kapalıdır; aynı çevrim için kullanıcı yeniden seçim yapar. Eski tarayıcı
oturumunun kontrol belirteci geçersiz olur; başarısız komut otomatik tekrarlanmaz.

Yalnız tek Uvicorn worker kullanın. CLI izleyicisi çalışıyorsa panel yöneticisi
mevcut kilidi zorlamaz. Panelde bu backend'in boşta olması, başka bir CLI
yöneticisinin bulunmadığı anlamına gelmez. Yerel API'yi internete açmayın;
oturum belirteci ve kaynak kontrolü uzak çok kullanıcılı kimlik doğrulaması değildir.

Varsayılan Testnet defteri ve anahtar dosyası `SPOTLAB_DATA_DIR` altındadır
(`testnet-orders.sqlite`, `testnet-credentials.json`). Özel veritabanı yalnız
sunucu yapılandırmasındaki `SPOTLAB_TESTNET_DATABASE_URL` ile seçilir.
Tarayıcıdan dosya yolu, API anahtarı veya yeni emir koşulu gönderilmez.
Defter yoksa panel boş görünür; izleyiciyi çalıştırmak için defter silmeyin veya
yeni boş defter oluşturarak hesap/geçmiş engellerini aşmaya çalışmayın.
