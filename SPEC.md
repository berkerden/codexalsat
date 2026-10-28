# SpotLab — ürün ve güvenlik sözleşmesi

Durum: ilk uygulama; canlı kullanıma onay değildir. Tek kullanıcı, spot, tek etkin
parite. API anahtarı gerekmeden araştırma ve paper çalışır. LLM zorunlu değildir.
Binance Global/TR tercihi, canlı sermaye ve risk yetkisi kullanıcıya aittir.

## Modüller ve veri akışı

`market` (REST/WS + kalite) → `research` (kapanmış mum, strateji/kanıt)
→ `finance` (Decimal maliyet ve boyut) → `engine` (yetki/risk/kalıcı durum)
→ `api` → React Türkçe panel. Motor tarayıcıdan bağımsız backend sürecidir.
PostgreSQL işlem günlüğü; geliştirme/testte SQLite; Parquet fiyat arşivi.
UI ve hesaplama aynı backend sonuçlarını kullanır. REST/WS yalnız doğrulanan
borsanın uçlarına gider. Desteklenmeyen hizmet açık hata verir, başka hizmete düşmez.

### Ortak veri sözleşmeleri

`Candle`: UTC epoch ms `open_time`, `close_time`; Decimal `open,high,low,close,volume`.
`Quote`: `symbol,bid,ask,bid_quantity,ask_quantity,observed_at` (UTC epoch ms).
API parasal değerleri JSON string olarak taşır; grafik için dönüşüm gösterimle sınırlıdır.
`MarketSnapshot`: sembol, periyot, mumlar, kotasyon, metadata, kalite sorunları ve kaynak.
Para ve emir miktarlarında float yok. Gözlem/durum/işlem kayıtları Git dışında tutulur.

### HTTP arayüzü v1

- `GET /api/status`: sürüm, hizmet, mod, veri sağlığı, açık engeller.
- `GET /api/symbols?venue=global`: doğrulanmış spot sembolleri.
- `GET /api/market?venue=global&symbol=BTCUSDT&interval=5m`: gerçek verili görünüm.
- `POST /api/research`: belirtilen sembol, zaman aralığı, periyotlar, maliyet
  varsayımları ile tekrar üretilebilir araştırma; kısa veri pilot olarak etiketlenir.
- `GET /api/paper`: yalnız sanal bakiye, emir, pozisyon ve gerçekleşmeler.
- `POST /api/paper/start`: kullanıcı girilmiş sanal sermaye/risk ile başlatma;
  düz yapılandırma gövdesine `expected_session_id` eklenir.
- `POST /api/paper/stop`: girişleri durdurma; açık pozisyon yönetimi sürer.
- `POST /api/paper/close`: yalnız paper bot pozisyonlarını kapatma.
- `POST /api/paper/archive`: `expected_session_id` ve gerçek boolean
  `confirm_archive: true` ile yalnız durdurulmuş, pozisyonsuz deneyi arşivler;
  yeni kimlikli, yapılandırılmamış ve girişleri kapalı paper durumunu döndürür.
- `GET /api/paper/archives?limit=20&offset=0`: sayfalı salt okunur özet ve toplam.
- `GET /api/paper/archives/{session_id}`: değişmez arşiv durumu ve arşiv zamanı.
- `GET /api/paper/observation?expected_session_id=...`: oturum kimliğiyle korunan,
  salt okunur ileri gözlem raporu; uyuşmazlıkta 409.
- `GET /api/paper/archives/{session_id}/observation`: arşiv anında dondurulmuş
  deneyin ileri gözlem raporu; bulunamayan arşiv için 404.
- Paper durumu boşken de `session_id` taşır; stop/close gövdeleri de
  `expected_session_id` ister. Oturum uyuşmazlığı 409 ile reddedilir.
- `POST /api/live/activate`: değerlendirme engelleri bitmeden reddedilir.
- `GET /api/testnet/status`: yalnız yerel çevrim kayıtları ve bu backend'in izleyicisi;
  hesap sorgusu, anahtar okuma veya defter/şema oluşturma yapmaz.
- `GET /api/testnet/session`: yalnız süreç belleğinde tutulan kontrol belirteci.
- `POST /api/testnet/watch/start`: mevcut çevrimde varsayılan salt okunur izleme;
  açık Testnet seçimi kalan girişin iptalini ve mevcut koruma yönetimini açar.
- `POST /api/testnet/watch/{run_id}/stop`: yalnız eşleşen izleyici görevini durdurur;
  borsa emirlerini iptal etmez, pozisyon kapatmaz.

## Araştırma protokolü (sonuçlar görülmeden)

Az sayıda sabit kural: trendde geri çekilme, hacimli kırılım, yatay ortalamaya dönüş.
1m/3m/5m/15m/1h ayrı; mum periyodu ile elde tutma süresi ayrı.
Kronolojik %60 eğitim, %20 doğrulama, %20 mühürlü test; etiket ufku kadar purge.
Walk-forward eğitim içi tanılama. Aday seçimi doğrulamada; test tek seçilmiş adayda.
Her çalışmada veri SHA256, kod commit, parametreler, seed ve deneme sayısı tutulur.
Test sonucuna bakılarak aday değiştirilmez; tekrar çalıştırmalar yeni kanıt sayılmaz.
İlk araştırma hedefi en az 180 gün ve 100 örtüşmeyen örneklem dışı işlem;
bu asgari operasyonel eşik avantaj kanıtı değildir, rejim kapsamı ayrıca incelenir.
Kısa veriyle çalışan akış pilot; uygun periyot veya AL yetkisi üretemez.
Bağımlılığı dikkate alan blok bootstrap, masraf stresi, al-tut ve nakit referansı.
Çoklu denemede aile bazında ihtiyatlı güven düzeyi; veri azsa karar İŞLEM YAPMA.

Sinyal t mum kapanışında hesaplanır; ilk mümkün dolum t+1 açılışı + gecikme.
Mum içi hedef/stop çakışmasında stop önce, gap stopta daha kötü açılış.
Limit dokunuşu kesin dolum sayılmaz; mum backtestinde kuyruk modellenmez.
Spread/kayma simülasyon fiyatına bir kez dahil, gerçek dolumdan tekrar düşülmez.
Benzerlik: önceki kapanmış getirilerin normalize Öklid mesafesi; adayın tüm sonuç
penceresi karar zamanından önce bitmeli; örnekler örtüşmemeli. Benzerlik olasılık değil.

## Değişmez koşullar

1. Canlı varsayılan kapalı. Başlangıç/çökme/ayar veya sürüm değişimi giriş yetkisini açmaz.
2. Belirsiz emir sonucu UNKNOWN → sorgu/uzlaştırma; otomatik tekrar gönderim yok.
3. Bakiye rezervasyonu, niyet kimliği ve tek yazıcı atomik. Kısmi/tekrarlı dolumlar
   yalnız doğrulanmış trade kimlikleriyle bir kez muhasebeleştirilir.
4. Spot satış botun komisyon sonrası sahip olduğu miktarı aşamaz; manuel varlık dokunulmaz.
5. İşlem başı, toplam açık risk, UTC günlük net zarar (açık K/Z + komisyon dahil),
   sermaye, düşüş, sıklık sınırları backend'de kontrol edilir.
6. Eski kotasyon (>5s), saat farkı (>1s), veri boşluğu, desteklenmeyen filtre,
   işlem dışı sembol, eksik maliyet, tutarsız hesap veya koruma hatası girişleri engeller.
7. Durdurmak girişleri durdurur; çıkış/koruyucu yönetim ayrı yaşam döngüsündedir.
8. Güncel lot/tick/minimum tutar metadata'dan; adım aşağı yuvarlanır, minimum sonrası
   tekrar kontrol edilir. Maliyetler ve komisyon varlık dönüşümleri bilinmeden K/Z kesinleşmez.
9. Canlı koruma, Binance testnet yaşam döngüsü, gerçek veriyle ileri paper gözlemi ve
   bağımsız kritik inceleme tamamlanmadan canlı etkinleştirme engellenir.
10. Push/merge bot başlatmaz/güncellemez. Canlı sürüm sabit commit, değişimi ayrı onay.
11. Testnet emir defteri tek bir borsa ortamı ve doğrulanmış hesap kimliğine bağlanır.
    İlk kurulum bağlaması yalnız boş defterde yapılır. Eski kayıtlara mevcut hesabın
    kimliği kendiliğinden atanmaz: ayrı geçiş işlemi tüm kapanmış emir/koruma/dolum
    kanıtını aynı doğrulanmış hesaptan eşleştirmeli, sıfır net envanteri doğrulamalı
    ve değişmemiş deftere bağlama ile geçiş kaydını tek işlemde yazmalıdır. Hesap doğrulanamıyorsa
    emir sorgusu, gönderimi ve iptali engellenir; başka hesabın verisi eski deftere işlenmez.
12. Aynı hesap kimliği Testnet sıfırlamasının çözüldüğünü kanıtlamaz. Kaybolan geçmiş,
    iptal veya sıfır envanter olarak yorumlanmaz. Özgün kayıtlar korunur; belirsizliği
    gidermek için defteri silmek, yeni kimlik vermek veya korumayı tekrar göndermek yoktur.

13. Mevcut çevrim izleyicisi yeni alış veya giriş yetkisi oluşturmaz. Aynı defteri
    kullanan çevrim yöneticileri özel kilitle sıralanır. Hata/eski gözlem koruma
    kanıtı değildir; yeniden deneme yalnız açıkça tanımlanmış geçici hatalarda ve
    aynı emir kimliklerinin sorgusuyla mevcut yaşam döngüsünü izler.
14. Panel açılması, yeniden bağlanması veya backend yeniden başlaması Testnet
    izleyicisini başlatmaz. Kontroller geçerli yerel kaynak ve süreç oturum belirteci
    ister; tarayıcıdan dosya yolu, anahtar, sembol veya yeni emir şartı alınmaz.
    Tek görev backend'e aittir; tarayıcı kapanması onu durdurmaz. Backend kapanması
    görevi bitirir. Eski, başarısız veya durdurulmuş gözlem koruma kanıtı değildir.

15. Paper arşivleme, mevcut oturum kilidi altında tam geçmişi koruyup yeni boş
    oturum oluşturur. Etkin giriş veya açık/kısmi pozisyon varken uygulanmaz.
    Yeni oturum otomatik başlamaz. Ağ bekleyen eski işçi, kapatma veya başlatma
    isteği yeni oturumu değiştiremez; kilit altında oturum kimliği doğrulanır.
    Arşivler ayrı deneylerdir; arşivleme zarar geçmişini aynı deneyde sıfırlamaz.
    Mantıksal yedek mevcut durumla tüm arşivleri tutarlı okur; geri yükleme
    yalnız boş hedefe yapılır, giriş yetkisi geri yüklenmez.

16. İleri gözlem yalnız açıkça işaretlenen paper işçi denemelerinden kaydedilir;
    durum okumak, oturum başlatmak veya elle kapatmak kanıt oluşturmaz. Giriş
    değerlendirmesinde kotasyon, kapanmış güncel mum, saat ve filtre kontrolleri
    gerekir. Pozisyon yönetiminde yalnız kotasyon kontrolü ayrı sınıfta tutulur.
    Yakın sağlıklı örnekler arasındaki süreler tahminidir; süreç kesintisi, durdurma,
    saat gerilemesi, kod değişikliği ve geri yükleme süre zincirini keser.
    Eski veriden geriye dönük kayıt üretilmez. Ayar ve kod kimliği raporlanır;
    geçmişten devralınan veya değişmiş kodla toplanan kanıt tek sabit deney diye
    sunulmaz. Rapor canlı uygunluğu veya ekonomik avantaj onayı vermez.

## Aşama kabulü ve operasyon

A0: gerçek GitHub deposu, ilk push, CI, secret taraması. Public depo ve CI geçen
PR'ları birleştirme seçimi kullanıcı tarafından onaylandı.
A1: bu sözleşme ve bağımsız beklenen sonuçlu test senaryoları.
A2: sayfalama/artımlı Parquet, kalite, REST/WS kesinti/limit testi ve gerçek veri kanıtı.
A3: sızıntısız aday seçimi, tekrar üretim, stres/örneklem dışı rapor; avantaj şart değil.
A4: gerçek kullanıcı akışları ve mobil panel; no-data/error/stale durumları.
A5: sanal dolum/komisyon/boyut/KZ bağımsız örneklerle; paper her zaman etiketli.
A6: kalıcı durum makinesi/risk + testnet; testnet anahtarı yoksa dış engel açık yazılır.
A7: ayrı bağlamda kod ve test incelemesi; kritik/yüksek bulgular çözülür.
A8: kurulum, yedek/geri yükleme testleri ve sürüm notları.

Üretim hesabı, API anahtarı, ücretli sunucu veya canlı emir geliştirme sırasında kullanılmaz.
Uzak sunucu yayını ayrı HTTPS/kimlik doğrulama doğrulaması gerektirir; yerelde loopback.
Bilgisayar uykusu botu durdurabilir. Tarayıcı kapanması çalışan backend'i durdurmaz.
