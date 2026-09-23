# Gerçek veri araştırması — 23 Eylül 2026

Binance Global üretim mumlarıyla yapılan ilk keşifsel çalışma: **10 parite/periyot karşılaştırmasının tamamı İŞLEM YAPMA**. Bu strateji ailesi ve maliyet varsayımları altında pozitif ekonomik avantaj desteklenmedi. Sonuçlar bir canlı hesap performansı değildir.

Sonuçlar görülmeden sabitlenen kapsam: BTCUSDT ve SOLUSDT; 1m, 3m, 5m, 15m, 1h; yaklaşık 181 gün; 60 dakika azami elde tutma. Her grupta 3 strateji × 3 hedef/stop çifti; toplam 90 karşılaştırma. Kronolojik %60/%20/%20 ayrım, tutma süresi kadar boşluk ve eğitim içinde walk-forward. Son testle seçim yapılmadı.

Varsayılan normal maliyet: giriş ve çıkışta ayrı %0,1 komisyon, 5 baz puan toplam spread, her yönde 5 baz puan kayma. Stres: komisyon 1,5×, spread/kayma 2×. Hesaba özgü ücret alınmadığından bunlar açık varsayımdır.

| Parite | Periyot | Mum | OOS işlem | İşlem başı net | Düzeltilmiş GA | Stres net beklenti | Net bileşik |
|---|---|---:|---:|---:|---|---:|---:|
| BTCUSDT | 1h | 4344 | 9 | -0.461% | [-0.702%, -0.092%] | -0.709% | -4.083% |
| BTCUSDT | 15m | 17376 | 44 | -0.352% | [-0.504%, -0.248%] | -0.601% | -14.388% |
| BTCUSDT | 5m | 52128 | 147 | -0.306% | [-0.357%, -0.256%] | -0.555% | -36.336% |
| BTCUSDT | 3m | 86881 | 360 | -0.303% | [-0.357%, -0.242%] | -0.552% | -66.546% |
| BTCUSDT | 1m | 260641 | 542 | -0.349% | [-0.396%, -0.301%] | -0.598% | -85.046% |
| SOLUSDT | 1h | 4344 | 7 | -0.260% | [-0.414%, -0.029%] | -0.509% | -1.806% |
| SOLUSDT | 15m | 17376 | 58 | -0.270% | [-0.399%, -0.155%] | -0.519% | -14.541% |
| SOLUSDT | 5m | 52129 | 291 | -0.352% | [-0.409%, -0.294%] | -0.601% | -64.264% |
| SOLUSDT | 3m | 86881 | 420 | -0.342% | [-0.393%, -0.292%] | -0.591% | -76.378% |
| SOLUSDT | 1m | 260641 | 802 | -0.328% | [-0.368%, -0.279%] | -0.576% | -92.834% |

GA: zamansal bağımlılık için dairesel blok bootstrap; tohum 1729, 400 tekrar, 90 deneme için Bonferroni düzeltmesi. 400 tekrar çok uç kuyrukları hassas çözemez; aralıklar keşifsel belirsizlik göstergesidir. Bunlardan canlı güven düzeyi garantisi çıkarılmaz. Bileşik getiri, ardışık normalize işlem getirilerinin çarpımıdır; risk ölçekli hesap getirisi değildir.

## Tekrar üretim ve sınırlar

Ham mumlar ve ayrıntılı raporlar yerel `data/market/` ve `data/reports/` altındadır; ilk çalışma raporları ayrıca `data/research-initial-20260923/` altında korunur; public Git içine kişisel kayıt veya büyük ham veri konmaz. Raporlarda veri SHA-256, çekirdek kaynak SHA-256, parametreler, ayrım sınırları ve seed bulunur. Her grubun gerçek zaman aralığı aşağıdadır. İlk indirme seri olduğundan uç mumların kapanışları grup başına farklıdır. Betiğe daha sonra ortak bitiş anından sonra kapanan mumları dışlama eklendi; bu tablo ilk tamamlanan çalışmayı saklar, düzeltme sonrasında sonuç seçmek için yeniden optimize edilmedi.

| Parite | Periyot | Başlangıç UTC | Son mum kapanışı UTC | Veri SHA-256 |
|---|---|---|---|---|
| BTCUSDT | 1h | 2026-03-26T06:00:00+00:00 | 2026-09-23T05:59:59.999000+00:00 | `be0e43f95fd68c56dccfe693e21c8d79f2e68cf4ee56c98af29bd71efbf68f71` |
| BTCUSDT | 15m | 2026-03-26T06:15:00+00:00 | 2026-09-23T06:14:59.999000+00:00 | `7fb893302028aaf154ee62e51fca4d81d2e9634c3c487e00b0370b0302152eed` |
| BTCUSDT | 5m | 2026-03-26T06:20:00+00:00 | 2026-09-23T06:19:59.999000+00:00 | `b59a7935f1c0274e4422311bd63b8e6d1953acafaf0dbd32d8141a04da51c6a3` |
| BTCUSDT | 3m | 2026-03-26T06:21:00+00:00 | 2026-09-23T06:23:59.999000+00:00 | `f526a82ae9c3c37750ddf535f42b30002c9d1b136ca2a003831ffb545f88188d` |
| BTCUSDT | 1m | 2026-03-26T06:23:00+00:00 | 2026-09-23T06:23:59.999000+00:00 | `9fc60359701b3096a227367e0bc126e9f8200a0ff170a7c0ce164dc5018b3081` |
| SOLUSDT | 1h | 2026-03-26T06:00:00+00:00 | 2026-09-23T05:59:59.999000+00:00 | `3c20251fac6336bd30a06b28791c2f827abbe6e887f6c0c1c7c1d82f76241fb4` |
| SOLUSDT | 15m | 2026-03-26T06:15:00+00:00 | 2026-09-23T06:14:59.999000+00:00 | `1ef0f94488df1cfb3fc8d0868f01021defbba4aa8623f4b52a8952e21386cc19` |
| SOLUSDT | 5m | 2026-03-26T06:20:00+00:00 | 2026-09-23T06:24:59.999000+00:00 | `f74955f4526db01b85420e8ed9cb8a10f9b7054b4239a4ea831ec4f54527a938` |
| SOLUSDT | 3m | 2026-03-26T06:21:00+00:00 | 2026-09-23T06:23:59.999000+00:00 | `0d893a101930004f638cc4304fe106ebee1bf87140800272dc3594197075a31e` |
| SOLUSDT | 1m | 2026-03-26T06:23:00+00:00 | 2026-09-23T06:23:59.999000+00:00 | `4bee3cc7f82bf4fe4dd3e5368ba2300e69699e162245bab033736c5b109639b1` |

Araştırma çekirdeği: `source-sha256:37941351c671c0998148eaf145828d9e5ff467cafddb09e010a2440426354936`.

1h ve 15m gruplarında 100 etkin örneklem dışı işlem eşiği karşılanmadı; bunlar pilot sayılır. Daha kısa periyotlarda işlem sayısı eşiği aşılsa da net beklenti ve maliyet stresi başarısızdır. Aynı raporu tekrar çalıştırmak bağımsız yeni kanıt değildir.

Ayrıntılı JSON raporlarında profit factor, kazanma oranı, süre, nakit ve al-tut referansları bulunur. Kapanmış işlem serisi düşüşü hesaplanır; pozisyon içi gerçek portföy maksimum düşüşü henüz mevcut değildir. Mum modeli limit kuyruğunu ve kesin stop dolum fiyatını kanıtlamaz. 30 takvim günü ileri paper gözlemi henüz yapılmadı.
