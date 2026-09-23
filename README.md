# SpotLab

Türkçe, tek kullanıcılı Binance Global spot araştırma ve **paper trading** paneli.
BTCUSDT ve SOLUSDT dahil doğrulanmış spot pariteleri analiz eder; bir paper
oturumunda tek parite yönetir. Gerçek emir gönderme **kapalıdır**.

## Çalıştırma

Python 3.12+ ve Node 22.12+ gerekir. macOS Codex'in mevcut çalışma ortamı da
kurulum betiği tarafından bulunur. PostgreSQL olmadan yerel çalışma SQLite ile olur.

```bash
git clone https://github.com/berkerden/codexalsat.git
cd codexalsat
bash scripts/setup-local.sh
bash scripts/run-local.sh
```

Panel: <http://127.0.0.1:5173> · API: <http://127.0.0.1:8000/docs>

Bu makinedeki kaynak klasörü:
`/Users/berkerden/Documents/Codex/Binance Alsat Botu`.
Bu klasörde tekrar clone yapmayın; mevcut clone kullanılmalıdır.

Tarayıcı kapansa da backend çalışır. Terminal kapandığında, bilgisayar uyuduğunda
veya süreç çöktüğünde yerel paper yönetimi durabilir. Yeniden başlatma girişleri
kendiliğinden açmaz. Durdurmak için `Ctrl+C`; açık paper pozisyonları kayıtta korunur.

## Kullanım

1. Genel bakışta parite ve mum periyodunu seçin. Mum grafiği, kotasyon, veri kalitesi
   ve işlem yapılmama gerekçesi gerçek Binance verisinden gelir.
2. Araştırmada veri gün sayısı, pozisyonda kalma süresi ve maliyet varsayımlarını
   girin. Sonuçlar kronolojik örneklem dışı test, maliyet stresi ve belirsizlik içerir.
3. Paper bölümünde sanal sermaye/risk sınırlarını inceleyip süreli oturumu açın.
   Örnek başlangıç alanları canlı hesap ayarı değildir. Komisyon kotasyon varlığında
   varsayılır. Aynı oturumun risk ayarları sessizce değiştirilemez.
4. **Yeni girişleri durdur** mevcut pozisyonun çıkış yönetimini korur.
   **Bot pozisyonlarını kapat** sadece sanal bot pozisyonunu kapatır; bağlantı yoksa
   istek kalıcı olarak bekler. Hiçbir manuel borsa varlığına erişilmez.
5. Canlı bölümündeki engelleri takip edin. Bu sürümde gerçek emir adaptörü etkin
   değildir; bir ortam değişkeni bunu açamaz.

Mum süresi ve pozisyon süresi ayrı ayarlardır. İşlem sayısı günlük bir üst sınırdır,
hedef kota değildir. Yeterli kanıt yoksa `İŞLEM YAPMA` normal sonuçtur.
Varsayımsal hedefte net kâr, olasılık ağırlıklı beklenen kâr değildir. Sermaye
belirlenmemiş senaryoda marj **1 coin için birim hesap** olarak etiketlenir.
USDT kotasyon tutarı, risksiz veya bire bir USD nakit garantisi değildir.

## Test ve güvenlik

```bash
source .venv/bin/activate
python -m pytest
python -m ruff check backend/src backend/tests
python -m mypy backend/src
python scripts/secret_scan.py --staged
python -m pip_audit
source scripts/node-env.sh
(cd frontend && spotlab_npm run lint && spotlab_npm run typecheck && spotlab_npm test && spotlab_npm run build)
(cd frontend && spotlab_npm run test:e2e)
```

Kurulum yerel `.githooks/pre-commit` taramasını etkinleştirir. Tarayıcı testleri
Playwright Chromium gerektirir: `npx playwright install chromium`.
CI SQLite ve ayrı PostgreSQL servisiyle backend kontrollerini; frontend test,
build, bağımlılık güvenliği ve tarayıcı akışlarını çalıştırır. Hiçbir test canlı
anahtar istemez. Push/merge botu başlatmaz, güncellemez ve risk ayarı değiştirmez.

`.env`, `data/`, veritabanı, piyasa arşivi, kişisel işlem kayıtları, yedekler ve
anahtarlar Git dışında kalır. Sır taraması index'teki gerçek blob'u okur.
Bir sır sızarsa yalnız dosyayı silmek yeterli değildir: anahtarı iptal/rotate edin,
etkilenen kayıtları inceleyin; geçmiş temizliğini ayrı, kontrollü planlayın.

## Docker / PostgreSQL

```bash
cp .env.example .env
# .env içinde yerel parola ve DATABASE_URL'u eşleştirin.
docker compose --profile web up --build
```

Portlar yalnız `127.0.0.1` üzerinden açılır. PostgreSQL ve piyasa verileri ayrı
kalıcı volume'larda tutulur. Docker olmayan bu geliştirme makinesinde Compose
çalıştırması henüz doğrulanmadı; PostgreSQL yolu CI'da sınanır.
Sunucuya geçiş ayrı HTTPS/kimlik doğrulama, secret saklama, izleme ve yedekleme
kurulumu gerektirir. Ücretli hizmet otomatik başlatılmaz.

## Araştırma ve sınırlar

`python scripts/research_batch.py`: sonuçları görmeden sabitlenmiş BTCUSDT/SOLUSDT,
1m/3m/5m/15m/1h, 181 gün, 60 dakika elde tutma ve toplam 90 aday için keşifsel
karşılaştırma. Ham veri/raporlar `data/` altında; bağımsız özet `docs/RESULTS.md`.
Aynı testi tekrar çalıştırmak yeni bağımsız kanıt sayılmaz.

Bu ilk sürüm mum tabanlı backtestte emir kuyruğu, gerçek limit dolumu veya kesin
stop fiyatı iddia etmez. `closed_trade_equity_drawdown` yalnız kapanmış işlem
serisinin düşüşüdür; gerçek pozisyon içi portföy maksimum düşüşü henüz raporlanmaz.
Paper dolumları kotasyon miktarıyla sınırlanan simülasyonlardır; borsa kuyruğu değildir.

Canlı öncesinde gerçek testnet emir/koruma adaptörü, hesaba özgü ücretler ve BNB/base
komisyon muhasebesi, 30 takvim günü ileri paper gözlemi, yeterli etkin örnek ve
bağımsız operasyonel onay gerekir. Yazılım testleri kârlılık kanıtı değildir.

[SPEC](SPEC.md) · [Durum](STATUS.md) · [Veri](docs/DATA.md) ·
[Araştırma](docs/RESEARCH.md) · [Emir çekirdeği](docs/ORDERS.md) ·
[Operasyon](docs/OPERATIONS.md) · [PR #3](https://github.com/berkerden/codexalsat/pull/3)
