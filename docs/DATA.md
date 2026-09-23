# Piyasa verisi sözleşmesi

SpotLab A2 yalnız Binance Global'in kimlik doğrulaması gerektirmeyen spot piyasa
verisini kullanır. REST istekleri `https://data-api.binance.vision`, akışlar
`wss://data-stream.binance.vision` adresine gider. Bu modülde API anahtarı,
hesap verisi veya emir uçları yoktur.

Binance TR bu aşamada **kullanılamaz**. TR uçları ve veri sözleşmeleri bağımsız olarak
doğrulanmadan desteklenmiş sayılmaz. TR isteği açık `VenueUnavailable` hatası verir;
Binance Global'e sessiz geçiş yapılmaz.

## Zaman ve kapanmış mumlar

Tüm zamanlar UTC Unix epoch milisaniyesidir. Python arayüzünde `open_time`,
`close_time`, `start_ms`, `end_ms` ve `observed_at` alanları `int` olarak taşınır.
Yerel saat dilimi bu değerlere uygulanmaz. Bir `1m` mumu için örneğin kapanış zamanı
`open_time + 60_000 - 1` olmalıdır.

REST kline sorgusunun `start_ms` ve `end_ms` sınırları mum açılış zamanı bakımından
dahildir. İstemci en fazla 1000 satırlık sayfaları açılış zamanıyla ilerletir. Sunucu
saatine göre kapanmamış son mum sonuçtan çıkarılır. `latest()` mevcut, henüz kapanmamış
UTC zaman dilimini atlayıp son kapanmış mumdan geriye doğru sorgular.

WebSocket kline akışı yalnız payload içindeki `x=true` olduğunda mum üretir. Olay zamanı
varsayılan 5 saniyelik tazelik sınırını aşarsa veri reddedilir. Bağlantı kesintilerinde
gecikme 0,25 saniyeden başlayarak üstel artar, 8 saniyede sınırlanır ve varsayılan olarak
en fazla 8 yeniden bağlantı denenir. Yeniden bağlantı sonrası eksik kapanmış mum varsa
akış veri kalitesi hatası verir; REST ile açık bir geri doldurma yapılmadan devam etmez.

Kotasyonun `observed_at` alanı yanıtın yerel UTC epoch ms alınma zamanıdır. Giriş
kararlarında kotasyon yaşı 5 saniyeyi aşmamalıdır. `clock()` ağ gidiş-dönüş süresinin
orta noktasını kullanarak sunucu farkını raporlar; mutlak fark 1000 ms üstündeyse saat
senkron sayılmaz.

## Kesin veri kalitesi

`validate_candles()` gelen sırayı değiştirmeden şu sorunları raporlar:

- yinelenen veya geriye giden `open_time`;
- ardışık mumlar arasında boşluk;
- periyoda hizalanmamış açılış veya yanlış kapanış zamanı;
- kapanmamış mum;
- sonlu olmayan, sıfır/negatif fiyat veya negatif hacim;
- high/low sınırlarıyla çelişen OHLC.

Hatalı seri sıralanmaz, tekilleştirilmez, enterpole edilmez ve boşluklar doldurulmaz.
REST istemcisi ve Parquet deposu kalite raporu başarısız olduğunda `DataQualityError`
üretir. Raporun JSON şekli `ok`, `candle_count`, `interval` ve `issues` alanlarından
oluşur. Her sorun `code`, `message` ve ilgiliyse `open_time` taşır.

## Parquet arşivi

`ParquetStore(root)` her sembol/periyot için
`root/<SYMBOL>/<interval>.parquet` ve yanında
`<interval>.manifest.json` oluşturur. Parasal alanlar ondalık gösterimlerini kaybetmemek
için Parquet'te metin olarak saklanır ve okunurken `Decimal` değerine çevrilir.

`upsert()` önce yeni parçayı doğrular, mevcut mumları aynı `open_time` için yeni değerle
günceller, bütün birleşik seriyi tekrar doğrular ve dosyayı atomik değiştirir. Manifest
şema sürümü, kaynak, sembol, periyot, satır aralığı ve gerçek Parquet dosyasının SHA-256
özetini içerir. `read()` varsayılan olarak özeti doğrular. Üretilen `data/` ve
`*.parquet` dosyaları Git dışında tutulur.

## Birincil kaynaklar

- [Binance Spot genel REST bilgisi](https://developers.binance.com/en/docs/products/spot/rest-api):
  salt piyasa verisi için `data-api.binance.vision`, kronolojik sıralama ve varsayılan
  milisaniye zaman sözleşmesi.
- [Binance Spot WebSocket akışları](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md):
  market-data-only host, küçük harfli stream sembolü, kline `x` kapanış bayrağı ve
  bağlantı sınırları.
- [Binance Spot market data uçları](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#market-data-endpoints):
  `exchangeInfo`, `time`, `ticker/bookTicker`, `klines` alanları ve 1000 kline sınırı.

Bu kaynaklar uç sözleşmesini doğrular; SpotLab'in tazelik, kalite ve yeniden deneme
sınırları daha sıkı yerel güvenlik kurallarıdır.

## Gerçek salt okunur doğrulama

Fixture testlerinden ayrı olarak 2026-09-23 Türkiye saatinde, UTC
`2026-09-22T21:24:32+00:00` anında doğrudan üretim piyasa verisi uçlarına salt okunur
bir kontrol yapıldı:

- REST `https://data-api.binance.vision`: `BTCUSDT` durumu `TRADING`; en iyi alış
  fiyatı en iyi satış fiyatını aşmadı; kotasyon alındığı anda 5 saniye sınırı içinde
  tazeydi.
- `/api/v3/time`: yerel ağ orta noktasına göre sunucu farkı `+466 ms`, gidiş-dönüş
  `1233 ms`; 1000 ms yerel sınırına göre senkron sonucu `true`.
- Son üç kapanmış `1m` mum alındı. Son mum `open_time=1790112180000`,
  `close_time=1790112239999`; kalite sonucu `ok=true`.
- WebSocket `wss://data-stream.binance.vision`: ilk `BTCUSDT@bookTicker` kotasyonunda
  alış fiyatı satış fiyatını aşmadı ve kotasyon alındığı anda tazeydi.

Bu kayıt yalnız o andaki erişim ve veri şeklinin kanıtıdır; hizmet sürekliliği veya
ilerideki veri kalitesi için garanti değildir.
