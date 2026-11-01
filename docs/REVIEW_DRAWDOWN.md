# Örneklenen paper portföy düşüşü — 2 Kasım 2026


Kapsam: açık pozisyon dahil geçerli fiyatla değerlenen portföy maksimum
düşüşünün saklanması, raporlanması, arşiv ve şema 4 yedek uyumu.

Astra/high tasarım sözleşmesi, Sol/high backend ve Sol/medium arayüz
uygulaması kullanıldı. Ana oturum bağımsız sayısal muhasebe testleri ekledi.
1000 sanal sermaye / 8.34 birim örneğinde giriş sonrası özsermaye 998.332,
101 fiyatında tepe 1006.66366, 99.5 fiyatında değer 994.16617 olarak bağımsız
hesaplandı; toparlanmada eski maksimum korunuyor. Ayrı kısmi satış örneği,
gerçekleşen kayma ve komisyon sonrası 898.37808 özsermaye ve %10.162192
düşüşle işlem sonrası değerlemenin de maksimuma katıldığını doğruluyor.

Eski fiyat ve işçi hatası, yeniden başlatma, arşiv, geri yükleme, eski şema
ve bozuk yedeğin atomik reddi sınandı. Mobil güncel/arşiv raporu ve indirilen
JSON aynı sabit görünümü kullanır; bilinmeyen geçmiş sıfır gibi sunulmaz.
Yerel sonuçlar: 471 backend başarılı / 178 PostgreSQL varyantı atlandı,
30 arayüz ve 5 tarayıcı testi başarılı. Lint, tip ve derlemeler başarılı.
PostgreSQL ve son commit CI kanıtı ilgili PR'da kaydedilir.

Son npm taraması geliştirme bağımlılığı minimatch 10.2.6 için orta düzey
[GHSA-g3hv-75wg-m249](https://github.com/advisories/GHSA-g3hv-75wg-m249)
bildirdi. ESLint bağımlılık aralığı henüz düzeltme sürümünü almadığı için
dar kapsamlı override ile 11.0.1 sabitlendi; kilit dosyasındaki ek bağımlılık
@isaacs/nonrecursive-glob-match oldu. Node 22 uyumu, temiz npm ci, lint/tip,
30 arayüz testi, 5 tarayıcı akışı ve derleme yeniden doğrulandı. npm audit
sıfır açık, pip-audit bilinen açık yok bildirdi.

2 Kasım bağımsız son inceleme: uygulamadan ayrı Astra/high bağlamı gerçek
değişiklikleri inceledi; kapsam içinde açık P1/P2 bulunmadı. Hedefli backend
paketi 78 başarılı / 17 PostgreSQL varyantı atlandı. Ek sentetik doğrulamalar:
saat gerilemesinde maksimum tarihi son değerlemeden ileride kalırken şema 4
geri dönüşü; 5000/5001 ms ve gelecek kotasyon sınırı; raporun salt okunurluğu;
eski arşiv ham içeriğinin liste/ayrıntı/rapor/yedek/yeniden başlatmada değişmemesi
ve geri yüklenen raporun bilinmeyen geçmişi koruması. Bağımlılık değişikliği
dar kapsamlı bulundu. Gerçek hesap, anahtar veya borsa isteği kullanılmadı;
bu inceleme ekonomik avantaj veya canlı kullanım onayı değildir.
