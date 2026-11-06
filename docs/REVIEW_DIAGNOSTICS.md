# Paper veri sorunu tanılaması — 6 Kasım 2026

Kapsam: mevcut güvenlik kararlarını değiştirmeden sabit açıklamalı tanı,
oturuma bağlı kalıcı kayıt ve güncel/arşiv raporunda geçmiş olay görünümü.

Astra/high sözleşmeyi, Sol/high ilk backend uygulamasını, Sol/medium arayüzü
hazırladı. İlk backend ve inceleme oturumları kullanım sınırına ulaştı; ana
oturum kalan düzeltme ve testleri tamamladı. Ayrı Astra/high son incelemesi
bu paketin gerçek değişikliklerini ve bağımsız senaryolarını değerlendirir.

Ana oturum, tanı eklerken ortak fiyat okuma davranışını değiştiren kontrolü
kaldırdı; önceki hata nedenlerinin atanması aynen korundu. Risk/süre aşımı,
manuel kapatma niyeti ve güncel fiyatla çıkış yönetimi ayrı sınandı. Gerçek
işçi akışında saat farkı, mum kalitesi/güncelliği, bozuk sembol kuralları ve
ilk/son fiyat dahil beş istek aşamasının sabit açıklaması test edildi.
Ham istisna adres/metni kalıcı duruma girmez. Eski oturum sonucu kayıt yazamaz;
yedekten dönüş ve rapor okumaları tanıyı korur. Tekrar birleştirme, yeni olay,
100 kayıt sınırı ve gerileyen saatle eklenme sırası test edildi.

Arayüz geçmiş kaydını güncel sağlıkla karıştırmaz; eski/eksik ayrıntı yokluğu
hatasızlık sayılmaz. Aşırı büyük tarih değeri kartı çökertmez. Mobil rapor ve
JSON indirme akışı sentetik verilerle sınandı. Gerçek hesap/anahtar/borsa
isteği kullanılmadı; bu paket ekonomik avantaj veya canlı kullanım onayı değildir.

Yerel tam doğrulama: 486 backend testi başarılı, PostgreSQL servisi olmadığı
için 178 varyant atlandı; 32 arayüz testi ve 5 tarayıcı akışı geçti. Lint,
strict tip, derleme ve iki bağımlılık taraması başarılı. PostgreSQL ve son
commitin güvenlik/derleme sonuçları PR'daki CI kanıtıyla tamamlanır.

Ayrı Astra/high son incelemesinde kapsam içinde açık P1/P2 bulunmadı.
899ed19 temel sürümüne karşı 1000 sentetik durum karşılaştırması; giriş,
durdurma, yetkilendirme, eski/geçersiz fiyat, risk/süre aşımı, kısmi çıkış
ve manuel kapatma niyetinde aynı sonuçları doğruladı. Altı asenkron işçi
karşılaştırması; ilk eski fiyat sonrasında güncel fiyat yenilenmesini de
kapsadı. Diğer istekler beklerken hatanın gecikmeden kaydı, atomik geri alma,
arşiv korunması ve yeni oturumun yalıtımı bağımsız sınandı. Bozuk sembol
filtrelerinin güvenli biçimde girişi durdurması, bilinçli hata işleme
iyileştirmesidir. İnceleme PostgreSQL veya gerçek piyasa doğrulaması değildir.
