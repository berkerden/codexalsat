# Araştırma ve finans çekirdeği

Bu modül bir kâr vaadi üretmez. Sabit protokolün normal sonuçlarından biri
`İŞLEM YAPMA`dır. Bir çalışma en az 180 takvim günü ve mühürlü test bölümünde
en az 100 örtüşmeyen işlem içermiyorsa `pilot` olarak işaretlenir ve işlem
yetkisi vermez. Bu iki eşik sağlansa bile avantaj kanıtlanmış sayılmaz.

## Finans arayüzü

Tüm parasal değerler `Decimal` olmak zorundadır. Float girdiler reddedilir.

```python
from decimal import Decimal
from spotlab.finance import CostModel, margin, position_size

costs = CostModel(
    entry_fee=Decimal("0.001"),
    exit_fee=Decimal("0.001"),
    spread_bps=Decimal("5"),
    slippage_bps=Decimal("5"),
)

card = margin(
    entry=Decimal("100"),
    target=Decimal("102"),
    stop=Decimal("99"),
    quantity=Decimal("0.5"),
    costs=costs,
)

size = position_size(
    capital=Decimal("1000"),
    risk_fraction=Decimal("0.005"),
    entry=Decimal("100"),
    stop=Decimal("99"),
    costs=costs,
    step=Decimal("0.0001"),
    min_notional=Decimal("10"),
)
```

`CostModel` varsayılanları açıkça birer varsayımdır: giriş ve çıkışta binde
bir komisyon, 5 baz puan tam spread ve dolum başına 5 baz puan kayma. Hesaba
ve sembole ait gerçek oranlar biliniyorsa çağıran bunları sağlamalıdır.
Simülasyonda tam spread iki tarafa bölünür; kayma her doluma bir kez eklenir.
Gerçekleşmiş fiyatta spread/kayma zaten varsa tekrar düşülmemelidir.

`margin` giriş/çıkış dolum fiyatlarını, her iki komisyonu, brüt ve net hedef
kârını, stop kaybını, başabaş fiyatını, net yüzdeleri ve net risk/getiri
oranını ayrı alanlarda döndürür. `position_size` hem risk bütçesine hem nakit
sınırına uyar ve miktarı lot adımına aşağı yuvarlar. Minimum tutar sağlanamazsa
miktar sıfır ve gerekçe `below_min_notional` olur.

## Sabit araştırma protokolü

```python
from spotlab.research import run_research, similar_patterns, strategy_signal

report = run_research(
    candles,
    interval="5m",
    hold_minutes=30,
    costs=costs,
    code_version="git-commit-sha",
)
patterns = similar_patterns(candles, window=12, horizon=12, interval="5m")
```

`run_research` bildirilen periyodu veri üzerinde zorunlu kılar: her mumun
genişliği tam olarak periyot kadar olmalı ve ardışık açılışlar arasında boşluk
bulunmamalıdır. Boşluklu, yanlış genişlikte veya başka periyot gibi etiketlenen
veri araştırmaya girmez. Bu nedenle takvim kapsamı eksik mumlar arasındaki
geçen zamandan yapay biçimde büyütülemez. `similar_patterns` için `interval`
verilirse aynı kontrol uygulanır; verilmezse desteklenen periyot mum
genişliğinden çıkarılır ve düzenlilik yine zorunludur.

Üç basit ve nedensel strateji ailesi vardır:

- `pullback`: yükselen trend içinde sığ geri çekilmeden pozitif kapanış;
- `breakout`: önceki 20 mumun tepesini 1,5 kat hacimle aşan pozitif kapanış;
- `mean_reversion`: yatay rejimde ortalamadan 1,25 standart sapma aşağıdaki
  pozitif kapanış.

`strategy_signal(candles, strategy)` mumlarla aynı uzunlukta yalnızca `0` ve
`1` üretir. `1`, kapanan mumdan sonraki ilk mumun açılışında uzun spot giriş
adayını ifade eder. Short veya elde olmayan varlığı satma sinyali yoktur.
`trend_pullback` ve `volume_breakout` eski açıklayıcı adları sırasıyla
`pullback` ve `breakout` için takma ad olarak kabul edilir.

Her aile için sonuç görülmeden sabitlenmiş üç hedef/stop çifti denenir; toplam
deneme sayısı dokuzdur. Veri kronolojik olarak yüzde 60 eğitim, yüzde 20
doğrulama ve yüzde 20 mühürlü test bölümlerine ayrılır. Eğitim ve doğrulama
sonundaki etiket ufku kadar veri purge edilir. Eğitim bölümündeki üç gerçek
walk-forward katında aday yalnız o katın geçmişindeki genişleyen seçim
penceresinde seçilir; etiket ufku temizlendikten sonraki kat bölümünde
değerlendirilir. Daha sonraki doğrulama veya test sonucu bu tanıya sızmaz.
Aday seçimi yalnız
doğrulama bölümündeki aile bazında düzeltilmiş alt güven sınırı ile yapılır.
Seçilen tek aday test bölümünde bir kez değerlendirilir. Test sonucuna bakarak
aday değiştirilmez.

Güven aralığı zaman bağımlılığını korumak için yaklaşık karekök işlem
uzunluğunda dairesel blok bootstrap kullanır. Sabit seed, 400 tekrar ve dokuz
deneme için Bonferroni düzeltilmiş alfa rapora yazılır. Bu yaklaşım özellikle
az örneklemde ihtiyatlıdır. Alt sınır sıfırın üzerinde değilse avantaj
desteklenmez.

Normal maliyetlerin yanında komisyonu 1,5 kat, spread ve kaymayı iki kat alan
sabit stres senaryosu çalışır. Stres beklentisi pozitif değilse karar yine
`İŞLEM YAPMA`dır. Nakit getirisi sıfır ve maliyetli al-tut test getirisi
referans olarak verilir. Net bileşik getiri, işlem başına net beklenti,
medyan işlem getirisi, kapanmış işlemler arasındaki özsermaye düşüşü, profit
factor, kazanma oranı, işlem sayısı ve tipik süre raporlanır. OHLC mumları
intrabar portföy yolunu belirlemediği için gerçek portföy maksimum düşüşü
`portfolio_maximum_drawdown=null` ve
`portfolio_maximum_drawdown_available=false` olarak açıkça gösterilir. Kapanış
bazlı düşüş gerçek portföy maksimum düşüşü veya sertifikasyon metriği değildir.

Sinyal `t` kapanışında hesaplanır ve giriş en erken `t+1` açılışındadır.
Pozisyonlar örtüşmez. Hedef ve stop aynı mumda görülürse stop önce kabul edilir.
Stopun altına gap varsa belirlenen stop yerine daha kötü açılış kullanılır.
Hedef dolumu muhafazakâr olarak hedef fiyatındadır. Model limit emri kuyruğu
veya yalnızca fiyata dokunmaya dayalı kesin limit dolumu iddia etmez.

Rapor JSON ile doğrudan serileştirilebilir. Parasal ve oran alanları string,
zamanlar UTC epoch milisaniyedir. Rapor veri SHA256 değerini, kod sürümünü,
sabit seed'i, tüm parametreleri, deneme sayısını, örneklem sınırını ve varsa
300.000 mum çalışma sınırına takıldığını içerir. Sınır aşılırsa deterministik
olarak en yeni 300.000 mum kullanılır ve `data.capped=true` yazılır. Böylece
1 dakikalık veride 180 günlük asgari eşik de sınır içinde değerlendirilebilir.

## Benzer örüntüler

`similar_patterns` son kapanmış getiri penceresini geçmiş pencerelerle
karşılaştırır. Her pencere kendi ortalama ve standart sapmasıyla normalize
edilir. Uzaklık
`sqrt(mean((z(query_returns) - z(candidate_returns))**2))` olarak açıkça
raporlanır. Bu değer bir başarı skoru veya olasılık değildir.

Aday örüntünün tüm sonuç ufku sorgu özellik penceresi başlamadan biter.
Seçilen tarihsel örnekler, ilk getirinin dayandığı bir önceki kapanış dahil
özellik ve sonuç aralıklarının tamamı bakımından birbiriyle örtüşmez. Hedef,
stop, ikisinin aynı mumda görünmesi (`ambiguous`)
ve hiçbirinin görülmemesi (`neither`) ayrı sonuçlardır. Ufuk sonu getirisi,
medyanı ve zamansal blok bootstrap güven aralığı ayrıca verilir. Bootstrap'a
giren seçilmiş örnekler benzerlik sırasından kronolojik sıraya çevrilir;
zamansal bloklar bu sırada kurulur. Benzerlikten uydurma bir başarı oranı
türetilmez.
