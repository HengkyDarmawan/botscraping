# Audit LeadScraper Pro — 26 September 2026

Audit ini dikerjakan bersamaan dengan penambahan menu **Distributor Komponen**.
Semua temuan di bawah **sudah diperbaiki**, kecuali yang ditandai *catatan*.

## Akurasi data (paling berdampak)

| # | Temuan | Dampak | Perbaikan |
|---|--------|--------|-----------|
| 1 | Status "tutup" di halaman detail dibaca dari **seluruh panel**, termasuk ulasan pelanggan | Bisnis aktif dicap tutup dan dibuang. Contoh: *Service Infocus Bogor* ditandai tutup permanen karena pelanggan bertanya "apa sudah tutup permanen?". Dari 6 status tutup di DB, hanya 1 yang benar-benar dari Google. | Status hanya dibaca dari blok judul (`div.TIHn2`). Sinyal baru: balasan **pemilik** yang menyatakan usahanya tutup. 5 baris lama di `leads.db` sudah dikoreksi. |
| 2 | Deteksi "klaim bisnis" & "tambah foto" juga membaca seluruh panel | Sinyal Google Business Profile bisa salah | Hanya elemen yang teksnya persis cocok |
| 3 | Mode Cepat selalu menulis "Tutup Permanen", termasuk untuk tutup sementara | Label salah | Status diambil apa adanya dari kartu |
| 4 | "Tutup Sementara" tidak diberi skor nol | Lead tutup bisa ber-tier PANAS | `scoring.bisnis_tutup()` |
| 5 | Email dari `mailto:` tidak di-decode (`%20info@...`) | 46 email di DB tidak bisa dipakai | Di-decode dan divalidasi ulang. 46 baris sudah diperbaiki. |
| 6 | Hanya email **pertama** yang disimpan, tanpa urutan prioritas | Email kotak resmi kalah oleh gmail pribadi | Semua email disimpan. Yang terbaik (domain sendiri, sales@/info@) di kolom `email`. |
| 7 | Alamat di kartu feed kosong bila Google menyisipkan segmen kosong (`Toko ·  · Mall ...`) | Mode Cepat kehilangan alamat | Ambil segmen berisi pertama |
| 8 | Website di kartu = tautan luar pertama (bisa link pesan antar/booking) | Filter "tanpa website" salah buang | Utamakan tombol berlabel "Situs Web" |
| 9 | Jalur "Update Kontak" hanya menyimpan skor | `whatsapp_link` tetap nomor lama; hasil cek ulang website dibuang | Data segar ditulis tanpa me-reset masa anti-duplikat |
| 10 | `refresh_contacts` tidak pernah memeriksa email | Perubahan email tidak tercatat | Email dari cek ulang website ikut dibandingkan |

## Keandalan

| # | Temuan | Perbaikan |
|---|--------|-----------|
| 11 | `db.init_db()` dipanggil di awal setiap run, sehingga run lain yang sedang jalan ditandai "terputus" | Hanya dipanggil saat app/CLI mulai |
| 12 | `/pricing/cari-toko` tidak mendaftarkan job, sehingga progres tidak tampil dan Stop 404 | Lewat `_new_job()` |
| 13 | Path relatif: `python botscraping/app.py` dari folder lain membuat DB kosong baru | `app.py` pindah ke folder proyek saat start; path DB absolut |
| 14 | Tombol Stop pada Social/Website Leads "diterima" tapi proses jalan terus, lalu dilabeli dibatalkan | Server menolak dengan pesan jelas |
| 15 | Panggilan Gemini sinkron di dalam async (membekukan scraping) | `asyncio.to_thread` |
| 16 | Model `gemini-2.0-flash` ditulis di 5 tempat | Satu konstanta `config.GEMINI_MODEL` |
| 17 | Website Leads menyimpan ke CRM tanpa run_id, dan membuka listing yang sudah pasti punya website | Tercatat sebagai run; filter awal "tanpa website" |

## UI / CRM

| # | Temuan | Perbaikan |
|---|--------|-----------|
| 18 | Link pagination & export rusak bila filter memuat `&`, `#`, `+` | `urlencode` |
| 19 | `?hal=abc` menghasilkan error 500 | Divalidasi |
| 20 | Export kosong menampilkan JSON mentah | Kembali ke halaman dengan pesan |
| 21 | Hitungan "Belum Dihubungi" tidak sama antara kartu dan filter (NULL) | `COALESCE` di semua query |
| 22 | Dashboard & Hasil membaca seluruh isi semua xlsx setiap dibuka | Hanya header, di-cache per waktu ubah file |
| 23 | Badge jenis file hilang untuk `leads_`, `webslead_` | Satu tabel `JENIS_BERKAS` |
| 24 | Modal preview menyisipkan data hasil scrape tanpa escape | `escapeHtml` |
| 25 | Jumlah ulasan yang tidak terbaca ditampilkan "(0)" | Tidak ditampilkan |

## Kebersihan repo

* `.gitignore` baru. `data/leads.db*` dan `__pycache__` **dikeluarkan dari git** (`git rm --cached`; file di disk tetap ada). Belum di-commit.
* Kode mati dihapus: `_human_type`, `db.touch_checked`, `db.riwayat_pencarian`, `db.simpan_intelijen`, dan konstanta config yang tidak dipakai (`MIN_DELAY`, `MAX_DELAY`, `OUTPUT_FILE`, `PROXY_SERVER`).
* *Catatan:* folder `dokumen/` (template ISB + company profile) belum masuk git. Putuskan sendiri apakah dokumen perusahaan ini boleh ada di repo.
* *Catatan:* selector kelas Google (`h1.DUwDvf`, `div.F7nice`, `span.UY7F9`, `div.TIHn2`) tetap rapuh by design. Kalau Google mengubah UI, cek bagian ini dulu.
* *Catatan:* Google sesekali tidak menampilkan jumlah ulasan (kartu maupun detail). Nilainya disimpan kosong, bukan 0.

## Tambahan 26-09-2026 (sore)

| # | Temuan | Perbaikan |
|---|--------|-----------|
| 26 | Server dimatikan di tengah scraping komponen → lead "menunggu cek website" (`lolos_filter=0`) tertinggal: tersembunyi dari daftar tapi tetap menahan anti-duplikat, jadi tidak pernah bisa di-scrape ulang | `db_komponen.bersihkan_tertunda()` saat app start menghapusnya (tanpa masuk daftar lewati); "Lanjutkan run" / run berikutnya mengambilnya ulang lengkap |
| 27 | Pindah menu saat scraping → progres "hilang" (job tetap jalan di server, UI tidak tahu) | `/jobs/aktif` + `pulihkanJob()`; indikator & notifikasi di sidebar semua halaman |
| 28 | Target N lead komponen bisa terlampaui 1-2 karena listing dibuka bersamaan | Kuota dicek lagi sebelum lead disimpan |

## Klien Website disetarakan dengan Distributor Komponen (26-09-2026)

| # | Sebelum | Sesudah |
|---|---------|---------|
| 29 | Web-dev menyaring hanya di file Excel; bisnis tutup & tanpa kontak tetap masuk Database Leads (5.216 lead tanpa WA/email) | Disaring **saat scraping** seperti komponen. Lead lama dibiarkan; filter "Kontak → Tanpa WA & email" untuk menemukannya |
| 30 | Mode WA membuang kartu bertelepon kantor tanpa membuka websitenya | Ditunda sampai website dicek; nomor WA dari website (`wa.me`) dipakai |
| 31 | Email lain, WA lain, Tokopedia, Shopee, LinkedIn, YouTube, kota dipanen tapi dibuang (kolom tidak ada di leads.db) | Kolom ditambahkan ke kedua database, tampil di tabel & bisa di-export |
| 32 | Daftar lewati "kontak" berlaku 30 hari walau run berikutnya memakai mode kontak lain | Hanya berlaku untuk mode yang sama |
| 33 | Target N lead bisa berakhir kurang: lead yang menunggu cek website ikut dihitung kuota, dan cadangan kartu hanya 30% | Kuota hanya menghitung lead yang pasti lolos; cadangan kartu 2x (dibuka hanya bila perlu) |
| 34 | Database Leads tidak bisa menghapus | Hapus per baris / terpilih / semua sesuai filter, opsi "jangan ambil lagi" |
