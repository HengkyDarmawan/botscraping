# =============================================================================
# config.py — Konfigurasi Google Maps Business Scraper
# Ubah nilai di sini untuk menyesuaikan target pencarian dan pengaturan lainnya
# =============================================================================

# ─── TARGET PENCARIAN ────────────────────────────────────────────────────────
# Format: ("jenis_bisnis", "area/kota") atau ("jenis_bisnis", "area", "lat,long")
# Elemen ke-3 (koordinat) OPSIONAL — dipakai untuk membatasi radius pencarian di
# sekitar titik itu (lihat SEARCH_RADIUS_KM). Cara ambil koordinat: buka Google
# Maps → klik kanan pada titik yang diinginkan → klik angka koordinat paling atas
# (mis. "-6.261500, 106.810600") untuk menyalinnya, lalu tempel di sini.
# Semua kombinasi di-scrape dalam satu kali jalan, output jadi satu file.
SEARCH_TARGETS = [
    ("Servis Komputer", "Jakarta Selatan"),
    ("Warnet",          "Jakarta Timur"),
    ("Sekolah SMK",     "Depok"),
    # Contoh dengan titik pusat radius:
    # ("Servis Komputer", "Jakarta Selatan", "-6.2615,106.8106"),
    # ("Kantor Percetakan", "Bekasi"),
]

# ─── RADIUS AREA PENCARIAN ───────────────────────────────────────────────────
# Batasi hasil di sekitar koordinat target (elemen ke-3 di SEARCH_TARGETS).
# Diisi dalam KM lalu dikonversi otomatis ke level zoom Google Maps.
# 0 = nonaktif (Google menentukan area sendiri — perilaku lama).
# Catatan: ini membatasi AREA TAMPILAN peta, bukan filter jarak presisi.
SEARCH_RADIUS_KM = 0

# ─── FILTER KUALITAS LEADS ────────────────────────────────────────────────────
# Tiap filter punya sakelar sendiri; set False untuk MELEWATI filter itu.
# Bisnis yang tidak lolos filter yang AKTIF akan dibuang dari hasil akhir.
FILTER_RATING      = True   # Aktifkan filter rating minimum
MIN_RATING         = 4.0    #   → hanya bisnis dengan rating >= nilai ini
FILTER_REVIEWS     = True   # Aktifkan filter jumlah ulasan minimum
MIN_REVIEWS        = 5      #   → minimal jumlah ulasan (singkirkan bisnis baru)
REQUIRE_PHONE      = True   # Wajib punya nomor telepon (apa pun jenisnya)
REQUIRE_WHATSAPP   = True   # Wajib nomor HP seluler (kandidat WhatsApp); telepon
                            #   kabel (021, 022, dst) dibuang
REQUIRE_NO_WEBSITE = True   # Wajib BELUM punya website di GMaps (lead jasa website)
SERTAKAN_BISNIS_TUTUP = False  # False = buang bisnis "Tutup permanen"/"Tutup sementara"

# ─── ANTI-DUPLIKAT (database leads) ──────────────────────────────────────────
# Bisnis yang sudah pernah masuk database tidak diambil lagi sebagai lead baru
# selama DEDUP_BULAN bulan. Data tersimpan di data/leads.db (SQLite).
DEDUP_ENABLED = True   # False = perilaku lama (boleh duplikat antar-run)
DEDUP_BULAN   = 6      # Berapa bulan sebuah bisnis dianggap "sudah pernah diambil"

# Bisnis lama tetap dicek perubahan telepon/website/email-nya. Kalau berubah,
# database diperbarui dan lead itu muncul di sheet "Update Kontak".
REFRESH_KONTAK        = True
REFRESH_INTERVAL_HARI = 30   # Jeda minimal sebelum satu bisnis dicek ulang.
                             # Tanpa jeda ini, tiap run tetap membuka semua
                             # halaman bisnis lama dan hemat waktunya hilang.

# ─── ENRICHMENT WEBSITE ───────────────────────────────────────────────────────
# Membuka website tiap lead untuk menilai kondisinya (mati/HTTPS/mobile/kecepatan/
# platform/pixel iklan) dan memanen email + akun sosmed. Ini yang menentukan
# rekomendasi jasa dan skor potensi pembeli.
ENRICH_WEBSITE   = True
ENRICH_KONKUREN  = 5    # Berapa website diperiksa bersamaan
ENRICH_TIMEOUT   = 12   # Detik per website

# ─── TEMPLATE PESAN WHATSAPP ─────────────────────────────────────────────────
# Dipakai tombol "Chat WA" di halaman /leads. {nama} {pengirim} {usaha} {alasan}
# {jasa} diganti otomatis per lead.
PESAN_PENGIRIM = "Hengky"
PESAN_USAHA    = "tim digital kami"
PESAN_TEMPLATE = (
    "Halo {nama}, perkenalkan saya {pengirim} dari {usaha}.\n\n"
    "{alasan}\n\n"
    "Boleh saya kirimkan contoh hasil kerja dan estimasi biayanya?"
)

# ─── OUTPUT ───────────────────────────────────────────────────────────────────
OUTPUT_FILE   = "output/leads_gmaps"  # Path file output (ekstensi ditambah otomatis)
OUTPUT_FORMAT = "excel"               # "excel" = file .xlsx | "csv" = file .csv

# ─── APIFY PROXY (Rotasi IP Otomatis) ────────────────────────────────────────
# Daftar: apify.com → Settings → Integrations → API tokens
USE_PROXY             = False                    # Ubah ke True untuk mengaktifkan proxy
PROXY_SERVER          = "http://proxy.apify.com:8000"
APIFY_PROXY_TOKEN     = "YOUR_APIFY_TOKEN_HERE"  # Ganti dengan token dari akun Apify
APIFY_PROXY_GROUP     = "RESIDENTIAL"            # "RESIDENTIAL" atau "DATACENTER"
APIFY_PROXY_COUNTRY   = "ID"                     # Kode negara IP (kosongkan untuk random)
ROTATE_IP_PER_LISTING = True                     # True = IP baru tiap listing bisnis

# ─── TIMING ───────────────────────────────────────────────────────────────────
# Jeda acak antara setiap aksi (scroll, klik, navigasi) dalam satuan detik
MIN_DELAY = 2.0  # Jeda minimum (detik)
MAX_DELAY = 4.0  # Jeda maksimum (detik)

# ─── BROWSER ──────────────────────────────────────────────────────────────────
HEADLESS    = False  # False = tampilkan jendela browser (bagus untuk development)
                     # True  = mode background tanpa jendela (untuk production/server)
MAX_RESULTS = 20     # 20 per query × 3 query = ~50 leads unik setelah filter

# ─── PRICE SCRAPER (Shopee & Tokopedia) ───────────────────────────────────────
# Nilai default untuk modul scrapers/pricing.py (bisa di-override dari UI).
PRICING_HEADLESS   = False  # False = browser tampil (paling sulit dideteksi bot)
PRICING_USE_PROXY  = False  # True  = wajibkan proxy residential (disarankan untuk Shopee)
# Field proxy memakai konfigurasi proxy yang sama di atas (PROXY_SERVER, dll).
PRICING_MAX_RESULTS = 20    # Maksimal produk per sumber/toko

# ─── ENGINE "CHROME SAYA" (sesi login — untuk Shopee) ─────────────────────────
# Scraper menyambung ke Chrome Anda lewat remote debugging (CDP). Chrome dibuka
# dengan profil khusus app supaya login Shopee/Tokopedia tersimpan & tidak
# mengganggu Chrome harian Anda.
CHROME_PATH        = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CHROME_CDP_PORT    = 9222
CHROME_CDP_URL     = "http://localhost:9222"
CHROME_PROFILE_DIR = "data/chrome_profile"  # folder profil persisten (relatif ke proyek)
