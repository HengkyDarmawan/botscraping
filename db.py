"""
db.py — Penyimpanan lead permanen (SQLite) untuk LeadScraper Pro.

Satu-satunya sumber kebenaran untuk:
  • anti-duplikat  — bisnis yang sudah pernah di-scrape tidak diambil lagi
                     selama N bulan (default 6)
  • refresh kontak — bisnis lama tetap dicek perubahan telepon/website/email
  • CRM            — status follow-up, catatan, dan riwayat perubahan

Tidak butuh dependensi tambahan: memakai modul `sqlite3` bawaan Python.
Semua akses dibungkus satu lock karena app.py menjalankan job scraping di
thread terpisah sementara Flask melayani request di thread lain.
"""
import hashlib
import json
import re
import sqlite3
import threading
import urllib.parse
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ─── Ruang data ───────────────────────────────────────────────────────────────
# Satu file database per jenis prospek. "webdev" = calon klien jasa website &
# digital marketing, "komponen" = calon pembeli komponen komputer (ISB). Kedua
# ruang memakai skema lead yang sama, tapi datanya — termasuk anti-duplikat —
# terpisah total: bisnis yang sama boleh jadi lead di dua ruang sekaligus.
#
# Ruang aktif dibawa lewat ContextVar, jadi seluruh fungsi di modul ini dan
# seluruh pipeline scrapers/gmaps.py cukup memanggil `get_conn()` seperti biasa.
# asyncio.run/gather/to_thread menyalin context, sehingga satu job yang dibungkus
# `with db.ruang("komponen")` menulis ke komponen.db sampai selesai.
RUANG_PATH = {
    "webdev":   BASE_DIR / "data" / "leads.db",
    "komponen": BASE_DIR / "data" / "komponen.db",
}
_RUANG = ContextVar("ruang_db", default="webdev")

# Alias lama; menunjuk database web-dev.
DB_PATH = RUANG_PATH["webdev"]

_conns = {}
_lock = threading.RLock()

# Rata-rata hari per bulan — dipakai untuk mengubah "6 bulan" jadi rentang hari.
_HARI_PER_BULAN = 30.44

# Kolom yang TIDAK boleh ditimpa saat upsert bisnis yang sudah ada:
# ini hasil kerja manual user, bukan hasil scraping. Scraper juga memakainya
# untuk memulihkan kolom ini saat merakit baris "Update Kontak", jadi namanya
# publik; `_KOLOM_MILIK_USER` dipertahankan sebagai alias lama.
KOLOM_MILIK_USER = {"status_leads", "catatan", "tanggal_follow_up", "first_seen_at",
                    # ruang komponen
                    "nama_pic", "proposal_nomor", "proposal_tanggal",
                    "proposal_file", "tanggal_dihubungi", "kanal_kontak",
                    # tanda "lead berpotensi" yang dipasang user (kedua ruang)
                    "dipin"}
_KOLOM_MILIK_USER = KOLOM_MILIK_USER

SCHEMA_LEADS = """
CREATE TABLE IF NOT EXISTS businesses (
    place_key         TEXT PRIMARY KEY,

    -- identitas
    nama_bisnis       TEXT,
    kategori          TEXT,
    alamat            TEXT,
    area_pencarian    TEXT,
    source_url        TEXT,
    koordinat         TEXT,

    -- kontak
    telepon           TEXT,
    whatsapp_link     TEXT,
    email             TEXT,
    instagram         TEXT,
    facebook          TEXT,
    tiktok            TEXT,
    website           TEXT,

    -- metrik Google Maps
    rating            REAL,
    jumlah_ulasan     INTEGER,
    jam_operasional   TEXT,
    skor_popularitas  REAL,
    sudah_diklaim     INTEGER,
    status_buka       TEXT,
    jumlah_foto       INTEGER,
    rentang_harga     TEXT,

    -- hasil pemeriksaan website
    web_status        TEXT,
    web_https         INTEGER,
    web_mobile        INTEGER,
    web_load_ms       INTEGER,
    web_platform      TEXT,
    web_tahun_update  INTEGER,
    web_ada_pixel     INTEGER,
    web_ada_toko      INTEGER,

    -- intelijen penjualan
    skor_pembeli      INTEGER,
    tier              TEXT,
    jasa_utama        TEXT,
    -- Tim penanggung jawab jasa_utama: "web" / "marketing" / "kreatif".
    -- Nilainya datang dari data/jasa.json, jadi jalur baru bisa ditambahkan
    -- tanpa mengubah skema ini.
    jalur             TEXT,
    jasa_pendukung    TEXT,
    alasan_pitch      TEXT,

    -- CRM (diisi manual oleh user, tidak pernah ditimpa scraping)
    status_leads      TEXT DEFAULT 'Belum Dihubungi',
    catatan           TEXT DEFAULT '',
    tanggal_follow_up TEXT,

    -- jejak waktu
    first_seen_at     TEXT,
    last_scraped_at   TEXT,
    last_checked_at   TEXT,
    times_seen        INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_biz_tier   ON businesses(tier);
CREATE INDEX IF NOT EXISTS idx_biz_status ON businesses(status_leads);
CREATE INDEX IF NOT EXISTS idx_biz_area   ON businesses(area_pencarian);
CREATE INDEX IF NOT EXISTS idx_biz_jasa   ON businesses(jasa_utama);
CREATE INDEX IF NOT EXISTS idx_biz_nama   ON businesses(nama_bisnis);
CREATE INDEX IF NOT EXISTS idx_biz_scrape ON businesses(last_scraped_at);

CREATE TABLE IF NOT EXISTS business_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    place_key   TEXT NOT NULL,
    nama_bisnis TEXT,
    field       TEXT NOT NULL,
    nilai_lama  TEXT,
    nilai_baru  TEXT,
    changed_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chg_key ON business_changes(place_key);
CREATE INDEX IF NOT EXISTS idx_chg_at  ON business_changes(changed_at);

CREATE TABLE IF NOT EXISTS searches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword         TEXT,
    area            TEXT,
    run_at          TEXT,
    total_ditemukan INTEGER DEFAULT 0,
    baru            INTEGER DEFAULT 0,
    dilewati        INTEGER DEFAULT 0,
    diupdate        INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_search_at ON searches(run_at);

-- ─── Riwayat run scraping ────────────────────────────────────────────────────
-- Dipakai untuk dua hal yang dulu tidak mungkin: (1) merakit ulang file Excel
-- dari database kalau run mati di tengah jalan, dan (2) melanjutkan run yang
-- terputus dari target yang belum sempat dikerjakan.
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    mulai_pada   TEXT,
    selesai_pada TEXT,
    -- berjalan | selesai | terputus | dibatalkan
    status       TEXT DEFAULT 'berjalan',
    -- params run TANPA kredensial (token proxy & API key sengaja dibuang):
    -- database lead bukan tempat menyimpan rahasia, dan saat melanjutkan run
    -- token diisi ulang dari form.
    params_json  TEXT,
    target_index INTEGER DEFAULT 0,   -- berapa target yang sudah TUNTAS
    total_target INTEGER DEFAULT 0,
    filename     TEXT,
    baru         INTEGER DEFAULT 0,
    diupdate     INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, mulai_pada);

-- Lead mana milik run mana. Diisi SEGERA setiap satu listing selesai dibaca,
-- bukan di akhir run — inilah yang membuat data tidak hilang saat run gagal.
CREATE TABLE IF NOT EXISTS run_leads (
    run_id    TEXT NOT NULL,
    place_key TEXT NOT NULL,
    jenis     TEXT DEFAULT 'baru',    -- baru | update
    dicatat   TEXT,
    PRIMARY KEY (run_id, place_key)
);

CREATE INDEX IF NOT EXISTS idx_run_leads_run ON run_leads(run_id);
"""

SCHEMA_MP = """
-- ─── Marketplace intelligence (namespace mp_) ────────────────────────────────
-- Terpisah total dari tabel leads di atas. `mp_hapus()` hanya menyentuh tabel
-- ber-prefix mp_, jadi membersihkan data harga tidak pernah bisa menghapus lead.

CREATE TABLE IF NOT EXISTS mp_stores (
    id          TEXT PRIMARY KEY,
    platform    TEXT NOT NULL,
    nama        TEXT,
    url         TEXT,
    username    TEXT,
    shop_id     TEXT,
    is_own      INTEGER DEFAULT 0,   -- 0/1, bukan True/False: lihat catatan mp_*
    aktif       INTEGER DEFAULT 1,
    created_at  TEXT,
    last_ok_at  TEXT,
    last_error  TEXT
);

CREATE TABLE IF NOT EXISTS mp_products (
    product_key    TEXT PRIMARY KEY,   -- 'tkp:<shop>:<pid>' / 'shp:<shop>:<itemid>'
    store_id       TEXT,
    platform       TEXT,
    is_own         INTEGER DEFAULT 0,
    nama_produk    TEXT,
    url            TEXT,
    image_url      TEXT,
    harga          INTEGER DEFAULT 0,
    harga_coret    INTEGER DEFAULT 0,
    diskon_persen  INTEGER DEFAULT 0,
    rating         REAL DEFAULT 0,
    jumlah_ulasan  INTEGER DEFAULT 0,
    terjual        INTEGER DEFAULT 0,
    stok           INTEGER DEFAULT 0,
    lokasi         TEXT,
    sumber         TEXT,
    first_seen_at  TEXT,
    last_seen_at   TEXT,
    times_seen     INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_mp_prod_store ON mp_products(store_id);
CREATE INDEX IF NOT EXISTS idx_mp_prod_own   ON mp_products(is_own);
CREATE INDEX IF NOT EXISTS idx_mp_prod_nama  ON mp_products(nama_produk);

CREATE TABLE IF NOT EXISTS mp_price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_key TEXT NOT NULL,
    run_id      TEXT,
    harga       INTEGER,
    terjual     INTEGER,
    stok        INTEGER,
    captured_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mp_hist ON mp_price_history(product_key, captured_at);

-- Modal & biaya adalah angka yang DIKETIK USER, jadi tabelnya sendiri. Kalau
-- ia jadi kolom di mp_products, satu re-scrape bisa menimpanya diam-diam.
CREATE TABLE IF NOT EXISTS mp_modal (
    product_key TEXT PRIMARY KEY,
    modal       INTEGER,
    catatan     TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS mp_biaya (
    platform     TEXT PRIMARY KEY,
    fee_persen   REAL DEFAULT 0,
    biaya_tetap  INTEGER DEFAULT 0,
    updated_at   TEXT
);

-- Etalase = kategori yang dibuat penjual sendiri ("PROCESSOR INTEL GEN 14",
-- "VGA NVIDIA GEFORCE"). Judul produk ditulis berbeda-beda tiap toko, tapi
-- kategorinya jauh lebih seragam — jadi memetakan kueri ke etalase adalah cara
-- paling murah untuk memanen sedikit tapi tepat sasaran.
CREATE TABLE IF NOT EXISTS mp_etalase (
    id                TEXT PRIMARY KEY,   -- '<store_id>:<slug>'
    store_id          TEXT NOT NULL,
    slug              TEXT NOT NULL,
    nama              TEXT,
    url               TEXT,
    n_produk          INTEGER DEFAULT 0,
    terakhir_panen_at TEXT,
    created_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_mp_etalase_store ON mp_etalase(store_id);

CREATE TABLE IF NOT EXISTS mp_runs (
    run_id       TEXT PRIMARY KEY,
    mode         TEXT,
    kueri        TEXT,
    engine       TEXT,
    store_ids    TEXT,
    sumber_tier  TEXT,
    total        INTEGER DEFAULT 0,
    filename     TEXT,
    started_at   TEXT,
    finished_at  TEXT
);
"""

# Alias lama: seluruh skema ruang web-dev.
SCHEMA = SCHEMA_LEADS + SCHEMA_MP

# Kolom & tabel yang hanya ada di ruang komponen. Kolom ditambahkan lewat
# _pastikan_kolom (ALTER TABLE) supaya database yang sudah ada ikut terbarui.
# Kolom kontak & status saring — dipakai KEDUA ruang.
KOLOM_KONTAK = {
    "kota":             "TEXT",   # dari baris "plus code" halaman detail GMaps
    "email_lain":       "TEXT",
    "whatsapp_lain":    "TEXT",   # nomor WA yang dipanen dari website bisnis
    "website_utama":    "TEXT",   # website sungguhan (bukan IG/marketplace)
    "tokopedia":        "TEXT",
    "shopee":           "TEXT",
    "marketplace_lain": "TEXT",
    "linkedin":         "TEXT",
    "youtube":          "TEXT",
    # 0 = baru disimpan, syarat kontak belum bisa diputuskan (menunggu
    # pemeriksaan website); 1 = lolos. Daftar & export hanya membaca 1.
    # Baris lama (sebelum kolom ini ada) terbaca 1 lewat DEFAULT.
    "lolos_filter":     "INTEGER DEFAULT 1",
    # CRM bersama: kapan & lewat apa lead dihubungi, dan nama orang yang dituju.
    "nama_pic":         "TEXT",
    "tanggal_dihubungi": "TEXT",
    "kanal_kontak":     "TEXT",
    # 1 = di-PIN user sebagai lead berpotensi: punya tab sendiri & selalu di atas.
    "dipin":            "INTEGER DEFAULT 0",
}

# Kolom yang hanya ada di ruang komponen (proposal ISB).
KOLOM_KOMPONEN = {
    "segmen":           "TEXT",
    "proposal_nomor":   "TEXT",
    "proposal_tanggal": "TEXT",
    "proposal_file":    "TEXT",
}

# Tabel bersama kedua ruang.
SCHEMA_BERSAMA = """
-- Bisnis yang sengaja TIDAK dijadikan lead (tutup / kontak tidak memenuhi /
-- dihapus manual). Dicatat supaya run berikutnya tidak membuka halamannya lagi.
CREATE TABLE IF NOT EXISTS dilewati (
    place_key  TEXT PRIMARY KEY,
    nama       TEXT,
    alasan     TEXT,
    jenis      TEXT,          -- tutup | kontak | manual
    mode       TEXT DEFAULT '',  -- mode kontak saat dilewati (jenis kontak)
    dicek_pada TEXT
);

-- Pengaturan yang diubah user lewat UI (identitas pengirim, template, dll.).
CREATE TABLE IF NOT EXISTS pengaturan (
    kunci TEXT PRIMARY KEY,
    nilai TEXT
);
"""

SCHEMA_KOMPONEN = """
-- Nomor surat proposal: urut harian, reset otomatis karena kuncinya tanggal.
CREATE TABLE IF NOT EXISTS nomor_surat (
    tanggal   TEXT NOT NULL,
    urut      INTEGER NOT NULL,
    place_key TEXT,
    nomor     TEXT NOT NULL,
    dibuat    TEXT,
    PRIMARY KEY (tanggal, urut)
);
CREATE INDEX IF NOT EXISTS idx_nomor_key ON nomor_surat(place_key, tanggal);
"""

# Kolom mp_products yang tidak boleh ditimpa hasil scrape berikutnya.
# `is_own` adalah keputusan user, `first_seen_at` adalah sejarah.
KOLOM_MP_TERLINDUNGI = {"first_seen_at", "is_own", "product_key"}


# ─── Koneksi ──────────────────────────────────────────────────────────────────

def ruang_aktif():
    return _RUANG.get()


@contextmanager
def ruang(nama):
    """Jalankan blok kode terhadap database ruang `nama`."""
    if nama not in RUANG_PATH:
        raise ValueError(f"Ruang database tidak dikenal: {nama}")
    token = _RUANG.set(nama)
    try:
        yield
    finally:
        _RUANG.reset(token)


def _pastikan_kolom(conn, tabel, kolom):
    """Tambahkan kolom yang belum ada (migrasi ringan, aman diulang)."""
    ada = {r["name"] for r in conn.execute(f"PRAGMA table_info({tabel})")}
    for nama, tipe in kolom.items():
        if nama not in ada:
            conn.execute(f"ALTER TABLE {tabel} ADD COLUMN {nama} {tipe}")


def get_conn():
    """
    Koneksi ruang aktif. Satu koneksi per file, dipakai bersama semua thread
    (dilindungi _lock).
    """
    nama = _RUANG.get()
    with _lock:
        conn = _conns.get(nama)
        if conn is None:
            path = RUANG_PATH[nama]
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA_LEADS)
            _pastikan_kolom(conn, "businesses", KOLOM_KONTAK)
            conn.executescript(SCHEMA_BERSAMA)
            _pastikan_kolom(conn, "dilewati", {"mode": "TEXT DEFAULT ''"})
            if nama == "webdev":
                conn.executescript(SCHEMA_MP)
            else:
                _pastikan_kolom(conn, "businesses", KOLOM_KOMPONEN)
                conn.executescript(SCHEMA_KOMPONEN)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_biz_segmen "
                             "ON businesses(segmen)")
            conn.commit()
            _conns[nama] = conn
        return conn


def init_db():
    """
    Buat file database + tabel ruang aktif bila belum ada.

    HANYA dipanggil saat app/CLI mulai. Ia menandai setiap run 'berjalan' sebagai
    'terputus' — dipanggil di tengah proses, ia akan memutus run lain yang sedang
    benar-benar berjalan di thread sebelah.
    """
    conn = get_conn()
    # Run yang masih berstatus "berjalan" saat proses baru dimulai berarti proses
    # yang memegangnya sudah mati (crash / laptop tidur / app di-restart). Ditandai
    # "terputus" di sini supaya UI bisa menawarkan tombol Lanjutkan — kalau tidak,
    # run itu menggantung selamanya dan leadnya tidak pernah diklaim siapa pun.
    with _lock:
        conn.execute(
            "UPDATE runs SET status = 'terputus', selesai_pada = ? "
            "WHERE status = 'berjalan'",
            (_now(),),
        )
        conn.commit()
    return str(RUANG_PATH[_RUANG.get()])


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(value):
    """Baca timestamp dari DB. Toleran terhadap format lama tanpa detik."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    return None


# ─── Kunci identitas bisnis ───────────────────────────────────────────────────

def place_key_from_href(href, nama=None, alamat=None, telepon=None):
    """
    Kunci stabil untuk satu bisnis, dipakai sebagai primary key anti-duplikat.

    Prioritas:
      1. CID Google (`0x...:0x...` atau `?cid=`) — permanen, tidak berubah
         walau nama/alamat/telepon bisnis berganti.
      2. Nomor telepon (digit saja).
      3. Sidik jari nama + alamat.
      4. URL apa adanya (jalan terakhir).

    Dipanggil saat href masih di feed pencarian, SEBELUM halaman detail dibuka,
    supaya keputusan lewati/ambil diambil tanpa biaya membuka halaman.
    """
    raw = urllib.parse.unquote(str(href or ""))

    m = re.search(r"(0x[0-9a-f]+:0x[0-9a-f]+)", raw, re.IGNORECASE)
    if m:
        return "cid:" + m.group(1).lower()

    m = re.search(r"[?&]cid=(\d+)", raw)
    if m:
        return "cid:" + m.group(1)

    digits = re.sub(r"\D", "", str(telepon or ""))
    if len(digits) >= 8:
        if digits.startswith("0"):
            digits = "62" + digits[1:]
        return "tel:" + digits

    nama_n = re.sub(r"[^a-z0-9]", "", str(nama or "").lower())
    alamat_n = re.sub(r"[^a-z0-9]", "", str(alamat or "").lower())[:30]
    if nama_n:
        sig = hashlib.sha1(f"{nama_n}|{alamat_n}".encode()).hexdigest()[:16]
        return "sig:" + sig

    if raw:
        return "url:" + hashlib.sha1(raw.encode()).hexdigest()[:16]
    return ""


# ─── Klasifikasi anti-duplikat ────────────────────────────────────────────────

BARU = "BARU"
LAMA_SEGAR = "LAMA_SEGAR"            # sudah ada & belum lewat batas bulan
LAMA_KEDALUWARSA = "LAMA_KEDALUWARSA"  # sudah ada tapi sudah lewat batas bulan


def classify(place_key, bulan=6):
    """
    Tentukan perlakuan untuk satu bisnis:
      BARU             → belum pernah ada di database, ambil penuh
      LAMA_SEGAR       → sudah ada dan di-scrape < `bulan` bulan lalu, jangan
                         diambil lagi sebagai lead baru
      LAMA_KEDALUWARSA → sudah ada tapi terakhir di-scrape > `bulan` bulan lalu,
                         perlakukan seperti baru dan perbarui datanya
    """
    if not place_key:
        return BARU
    row = get(place_key)
    if row is None:
        return BARU
    terakhir = _parse_ts(row.get("last_scraped_at"))
    if terakhir is None:
        return LAMA_KEDALUWARSA
    batas = datetime.now() - timedelta(days=int(float(bulan) * _HARI_PER_BULAN))
    return LAMA_KEDALUWARSA if terakhir < batas else LAMA_SEGAR


def umur_hari(place_key, field="last_scraped_at"):
    """Berapa hari sejak bisnis ini terakhir di-scrape/dicek. None bila belum ada."""
    row = get(place_key)
    if not row:
        return None
    ts = _parse_ts(row.get(field))
    if ts is None:
        return None
    return (datetime.now() - ts).days


def perlu_refresh_kontak(place_key, interval_hari=30):
    """
    True bila bisnis lama sudah waktunya dicek ulang kontaknya.

    Tanpa jeda ini, setiap run akan tetap membuka halaman semua bisnis lama
    dan penghematan waktu dari anti-duplikat jadi hilang.
    """
    row = get(place_key)
    if not row:
        return False
    ts = _parse_ts(row.get("last_checked_at")) or _parse_ts(row.get("last_scraped_at"))
    if ts is None:
        return True
    return (datetime.now() - ts).days >= int(interval_hari)


# ─── Baca ─────────────────────────────────────────────────────────────────────

def get(place_key):
    """Satu baris bisnis sebagai dict, atau None."""
    if not place_key:
        return None
    with _lock:
        cur = get_conn().execute(
            "SELECT * FROM businesses WHERE place_key = ?", (place_key,)
        )
        row = cur.fetchone()
    return dict(row) if row else None


def _kolom_tabel():
    with _lock:
        cur = get_conn().execute("PRAGMA table_info(businesses)")
        return [r["name"] for r in cur.fetchall()]


# ─── Tulis ────────────────────────────────────────────────────────────────────

def upsert_business(row, sentuh_scrape=True):
    """
    Simpan bisnis baru, atau perbarui yang sudah ada.

    Saat memperbarui: `first_seen_at` dipertahankan, `times_seen` naik satu, dan
    kolom CRM (status_leads/catatan/tanggal_follow_up) TIDAK ditimpa — itu hasil
    kerja manual user.

    Baris yang datang tanpa skor dihitung skornya di sini. Ini titik jaga tunggal:
    setiap lead di database dijamin punya skor_pembeli + tier, dari jalur mana pun
    ia masuk. Tanpa ini, jaminan itu bergantung pada setiap pemanggil ingat
    memanggil scoring.nilai_lead lebih dulu — dan satu yang lupa berarti lead
    tanpa skor yang tidak akan pernah muncul di penyaringan tier.

    `sentuh_scrape=False` dipakai saat menyegarkan data bisnis LAMA (jalur
    "Update Kontak"): datanya ditulis, tapi `last_scraped_at` & `times_seen`
    tidak disentuh — kalau disentuh, masa tenggang anti-duplikat 6 bulan ikut
    ter-reset setiap kali kontaknya dicek.

    Return "insert" atau "update".
    """
    place_key = row.get("place_key")
    if not place_key:
        raise ValueError("upsert_business butuh place_key")

    if row.get("skor_pembeli") in (None, "") or not row.get("tier"):
        # Impor malas: db.py tetap bisa dipakai tanpa menyeret requests/bs4 yang
        # dibutuhkan scrapers.scoring lewat scrapers.enrich.
        if _RUANG.get() == "komponen":
            from scrapers import komponen
            komponen.nilai_lead(row, jumlah_cabang=hitung_cabang(row.get("nama_bisnis")))
        else:
            from scrapers import scoring
            scoring.nilai_lead(row, jumlah_cabang=hitung_cabang(row.get("nama_bisnis")))

    kolom_valid = set(_kolom_tabel())
    data = {k: v for k, v in row.items() if k in kolom_valid}
    now = _now()

    with _lock:
        conn = get_conn()
        baris = conn.execute(
            "SELECT * FROM businesses WHERE place_key = ?", (place_key,)
        ).fetchone()
        ada = dict(baris) if baris else None

        if ada is None:
            data.setdefault("first_seen_at", now)
            data.setdefault("status_leads", "Belum Dihubungi")
            data.setdefault("catatan", "")
            data["last_scraped_at"] = now
            data["last_checked_at"] = now
            data["times_seen"] = 1
            kolom = list(data.keys())
            conn.execute(
                f"INSERT INTO businesses ({', '.join(kolom)}) "
                f"VALUES ({', '.join('?' * len(kolom))})",
                [data[k] for k in kolom],
            )
            conn.commit()
            return "insert"

        for k in _KOLOM_MILIK_USER:
            data.pop(k, None)
        # Nilai None berarti "tidak berhasil diekstrak kali ini", bukan "sekarang
        # kosong". Menimpanya akan menghapus data bagus yang sudah tersimpan —
        # mis. jumlah ulasan yang terbaca saat run pertama tapi tidak terbaca
        # saat run berikutnya karena tampilan halamannya berbeda.
        data = {k: v for k, v in data.items()
                if v is not None or ada.get(k) is None}
        data.pop("place_key", None)
        data["last_checked_at"] = now
        if sentuh_scrape:
            data["last_scraped_at"] = now
            data["times_seen"] = int(ada.get("times_seen") or 0) + 1
        else:
            data.pop("last_scraped_at", None)
            data.pop("times_seen", None)
        kolom = list(data.keys())
        conn.execute(
            f"UPDATE businesses SET {', '.join(f'{k} = ?' for k in kolom)} "
            f"WHERE place_key = ?",
            [data[k] for k in kolom] + [place_key],
        )
        conn.commit()
        return "update"


def refresh_contacts(place_key, telepon=None, website=None, email=None, **extra):
    """
    Bandingkan kontak terbaru dengan yang tersimpan, catat setiap perubahan.

    Dipakai untuk bisnis yang sudah ada di database < 6 bulan: datanya tidak
    diambil ulang sebagai lead baru, tapi kalau nomor/website/email-nya berganti,
    database ikut diperbarui dan perubahannya tercatat.

    Return list perubahan: [{"field", "lama", "baru"}, ...] — kosong bila tidak
    ada yang berubah.
    """
    lama = get(place_key)
    if not lama:
        return []

    kandidat = {"telepon": telepon, "website": website, "email": email}
    kandidat.update(extra)

    perubahan = []
    for field, nilai_baru in kandidat.items():
        if nilai_baru is None:
            continue  # tidak diperiksa kali ini
        baru_s = str(nilai_baru).strip()
        lama_s = str(lama.get(field) or "").strip()
        # Nilai baru yang kosong tidak dianggap perubahan: kemungkinan besar
        # ekstraksi yang gagal, bukan kontak yang benar-benar dihapus.
        if not baru_s or baru_s in ("-", "n/a"):
            continue
        if baru_s != lama_s:
            perubahan.append({"field": field, "lama": lama_s, "baru": baru_s})

    now = _now()
    with _lock:
        conn = get_conn()
        if perubahan:
            for p in perubahan:
                conn.execute(
                    "UPDATE businesses SET " + p["field"] + " = ? WHERE place_key = ?",
                    (p["baru"], place_key),
                )
                conn.execute(
                    "INSERT INTO business_changes "
                    "(place_key, nama_bisnis, field, nilai_lama, nilai_baru, changed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (place_key, lama.get("nama_bisnis"), p["field"],
                     p["lama"], p["baru"], now),
                )
        conn.execute(
            "UPDATE businesses SET last_checked_at = ? WHERE place_key = ?",
            (now, place_key),
        )
        conn.commit()
    return perubahan


def log_search(keyword, area, total_ditemukan=0, baru=0, dilewati=0, diupdate=0):
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO searches (keyword, area, run_at, total_ditemukan, baru, "
            "dilewati, diupdate) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (keyword, area, _now(), total_ditemukan, baru, dilewati, diupdate),
        )
        conn.commit()


def perubahan_terbaru(place_key=None, batas=100):
    sql = "SELECT * FROM business_changes"
    args = []
    if place_key:
        sql += " WHERE place_key = ?"
        args.append(place_key)
    sql += " ORDER BY changed_at DESC LIMIT ?"
    args.append(batas)
    with _lock:
        cur = get_conn().execute(sql, args)
        return [dict(r) for r in cur.fetchall()]


# ─── Daftar lewati & hapus lead (ruang aktif) ─────────────────────────────────

# Berapa hari bisnis yang dilewati tidak dibuka lagi. None = selamanya
# ("manual": user menghapus lead salah scrape dan memilih jangan diambil lagi).
HARI_LEWATI = {"tutup": 180, "kontak": 30, "manual": None}

# Lead "aktif" = lolos syarat saat scraping dan tidak tutup. Daftar, hitungan,
# dan export hanya membaca baris ini.
SQL_AKTIF = ("COALESCE(lolos_filter, 1) = 1 AND "
             "LOWER(COALESCE(status_buka, '')) NOT LIKE '%tutup%' AND "
             "LOWER(COALESCE(status_buka, '')) NOT LIKE '%closed%'")


# ─── Tab & filter bersama halaman Leads (kedua ruang) ─────────────────────────

# Status yang berarti lead BELUM benar-benar dihubungi. "Proposal Dibuat" (ruang
# komponen) baru menyiapkan dokumen, belum mengirim apa pun.
STATUS_BELUM_KONTAK = ("Belum Dihubungi", "Proposal Dibuat")

_STATUS = "COALESCE(status_leads, 'Belum Dihubungi')"
_DAFTAR_BELUM = ", ".join(f"'{s}'" for s in STATUS_BELUM_KONTAK)
_DAFTAR_SELESAI = ", ".join(f"'{s}'" for s in ("Deal", "Tidak Tertarik", "Jangan Hubungi"))
# Sudah dikontak = pernah ditandai terkirim, atau statusnya sudah maju. Status
# lama ("Sudah Dihubungi", "Follow Up") ikut terbaca lewat cabang kedua.
_SQL_SUDAH = (f"(COALESCE(tanggal_dihubungi, '') != '' OR "
              f"{_STATUS} NOT IN ({_DAFTAR_BELUM}))")
_SQL_SELESAI = f"{_STATUS} IN ({_DAFTAR_SELESAI})"

# (kunci, label) — urutan tampil nav tab.
TAB_LEADS = [("semua", "Semua"), ("dipin", "Dipin"), ("belum", "Belum Dikontak"),
             ("sudah", "Sudah Dikontak"), ("fu", "Follow-up Hari Ini"),
             ("selesai", "Selesai")]


def _sql_tab(tab):
    """Kondisi SQL tab (tanpa argumen; tanggal hari ini ditanam sebagai literal)."""
    hari_ini = datetime.now().strftime("%Y-%m-%d")
    return {
        "dipin": "COALESCE(dipin, 0) = 1",
        "belum": f"NOT {_SQL_SUDAH}",
        "sudah": f"{_SQL_SUDAH} AND NOT {_SQL_SELESAI}",
        "fu": (f"tanggal_follow_up IS NOT NULL AND tanggal_follow_up != '' AND "
               f"tanggal_follow_up <= '{hari_ini}' AND NOT {_SQL_SELESAI}"),
        "selesai": _SQL_SELESAI,
    }.get(tab)


def hitung_tab(klausa, args):
    """
    Jumlah baris tiap tab untuk WHERE `klausa` (filter lain yang aktif, TANPA
    tab) — angka badge sama dengan jumlah baris saat tab itu diklik.
    """
    kolom = ", ".join(f"COALESCE(SUM(CASE WHEN {_sql_tab(k)} THEN 1 ELSE 0 END), 0) AS {k}"
                      for k, _ in TAB_LEADS if k != "semua")
    with _lock:
        r = get_conn().execute(
            f"SELECT COUNT(*) AS semua, {kolom} FROM businesses{klausa}", args).fetchone()
    return dict(r)


def kondisi_kontak(kontak, kolom_email="email"):
    """Kondisi SQL dropdown "Kontak" (punya WA / email / keduanya / tanpa)."""
    ada_wa = "COALESCE(whatsapp_link, '') != ''"
    ada_email = f"COALESCE({kolom_email}, '') != ''"
    return {
        "wa": ada_wa,
        "email": ada_email,
        "wa_atau_email": f"({ada_wa} OR {ada_email})",
        "wa_dan_email": f"({ada_wa} AND {ada_email})",
        "tanpa": f"NOT ({ada_wa} OR {ada_email})",
    }.get(kontak)


def urutan_dengan_pin(order):
    """Lead yang di-PIN selalu di atas, lalu urutan pilihan user."""
    return f"COALESCE(dipin, 0) DESC, {order}"


def lewati_catat(place_key, nama, alasan, jenis="kontak", mode=""):
    if not place_key:
        return
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO dilewati (place_key, nama, alasan, jenis, mode, "
            "dicek_pada) VALUES (?, ?, ?, ?, ?, ?)",
            (place_key, nama or "", alasan or "", jenis, mode or "", _now()))
        conn.commit()


def lewati_aktif(place_key, mode=""):
    """
    Alasan bila bisnis ini masih dalam masa lewati, selain itu None.

    Jenis "kontak" hanya berlaku untuk mode kontak yang SAMA: bisnis yang
    dilewati karena tidak punya WA tetap harus dibuka pada run yang mencari
    email. "tutup" dan "manual" berlaku untuk mode apa pun.
    """
    if not place_key:
        return None
    with _lock:
        r = get_conn().execute(
            "SELECT alasan, jenis, mode, dicek_pada FROM dilewati WHERE place_key = ?",
            (place_key,)).fetchone()
    if not r:
        return None
    if r["jenis"] == "kontak" and (r["mode"] or "") != (mode or ""):
        return None
    batas = HARI_LEWATI.get(r["jenis"], 30)
    if batas is None:
        return r["alasan"] or r["jenis"]
    ts = _parse_ts(r["dicek_pada"])
    if ts is None or (datetime.now() - ts).days >= batas:
        return None
    return r["alasan"] or r["jenis"]


def lewati_hapus(place_key):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM dilewati WHERE place_key = ?", (place_key,))
        conn.commit()


def kosongkan_lewati():
    with _lock:
        conn = get_conn()
        n = conn.execute("DELETE FROM dilewati").rowcount
        conn.commit()
    return n


def set_lolos(place_key, nilai=1):
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE businesses SET lolos_filter = ? WHERE place_key = ?",
                     (int(nilai), place_key))
        conn.commit()


def buang_lead(place_key, nama, alasan, jenis="kontak", mode=""):
    """
    Keluarkan lead yang ternyata tidak memenuhi syarat dari daftar leads.

    Hanya menghapus lead yang BELUM disentuh user (status masih Belum
    Dihubungi, tanpa catatan, baru sekali ditemukan): lead yang sudah dihubungi
    atau diberi catatan tetap dipertahankan.
    """
    with _lock:
        conn = get_conn()
        r = conn.execute(
            "SELECT status_leads, catatan, times_seen FROM businesses "
            "WHERE place_key = ?", (place_key,)).fetchone()
        if r and (r["status_leads"] or "Belum Dihubungi") == "Belum Dihubungi" \
                and not (r["catatan"] or "").strip() and int(r["times_seen"] or 1) <= 1:
            conn.execute("DELETE FROM businesses WHERE place_key = ?", (place_key,))
            conn.execute("DELETE FROM run_leads WHERE place_key = ?", (place_key,))
        elif r:
            conn.execute("UPDATE businesses SET lolos_filter = 1 WHERE place_key = ?",
                         (place_key,))
        conn.commit()
    lewati_catat(place_key, nama, alasan, jenis, mode)


def _hapus_keys(conn, keys, jangan_ambil_lagi):
    n = 0
    for i in range(0, len(keys), 500):
        potong = keys[i:i + 500]
        tanda = ",".join("?" * len(potong))
        if jangan_ambil_lagi:
            sekarang = _now()
            conn.executemany(
                "INSERT OR REPLACE INTO dilewati (place_key, nama, alasan, jenis, mode, "
                "dicek_pada) SELECT place_key, nama_bisnis, 'dihapus manual', 'manual', "
                "'', ? FROM businesses WHERE place_key = ?",
                [(sekarang, k) for k in potong])
        n += conn.execute(f"DELETE FROM businesses WHERE place_key IN ({tanda})",
                          potong).rowcount
        conn.execute(f"DELETE FROM run_leads WHERE place_key IN ({tanda})", potong)
        conn.execute(f"DELETE FROM business_changes WHERE place_key IN ({tanda})", potong)
    return n


def hapus_leads(keys, jangan_ambil_lagi=False):
    """
    Hapus lead tertentu beserta jejaknya. Return jumlah yang terhapus.

    `jangan_ambil_lagi` mencatat bisnisnya di daftar lewati tanpa masa berlaku,
    jadi lead salah scrape tidak muncul lagi di run berikutnya.
    """
    keys = [k for k in dict.fromkeys(keys or []) if k]
    if not keys:
        return 0
    with _lock:
        conn = get_conn()
        n = _hapus_keys(conn, keys, jangan_ambil_lagi)
        conn.commit()
    return n


def set_pin(keys, nilai=True):
    """Pasang/lepas PIN "lead berpotensi" pada lead tertentu. Return jumlah baris."""
    keys = [k for k in dict.fromkeys(keys or []) if k]
    n = 0
    with _lock:
        conn = get_conn()
        for i in range(0, len(keys), 500):
            potong = keys[i:i + 500]
            n += conn.execute(
                f"UPDATE businesses SET dipin = ? WHERE place_key IN "
                f"({','.join('?' * len(potong))})", [1 if nilai else 0] + potong).rowcount
        conn.commit()
    return n


def semua_keys():
    """Seluruh place_key, termasuk yang tersembunyi (tertunda / tutup)."""
    with _lock:
        return [r["place_key"] for r in
                get_conn().execute("SELECT place_key FROM businesses")]


def bersihkan_tertunda():
    """
    Hapus lead yang masih menunggu pemeriksaan website (lolos_filter = 0).

    HANYA dipanggil saat app mulai, ketika pasti tidak ada run yang berjalan.
    Baris seperti ini tertinggal kalau server dimatikan di tengah scraping:
    tersembunyi dari daftar, tapi tetap menahan anti-duplikat. Dihapus tanpa
    masuk daftar lewati, supaya run berikutnya (atau "Lanjutkan run")
    mengambilnya ulang lengkap dengan pemeriksaan websitenya.
    """
    with _lock:
        conn = get_conn()
        keys = [r["place_key"] for r in conn.execute(
            "SELECT place_key FROM businesses WHERE lolos_filter = 0")]
        n = _hapus_keys(conn, keys, jangan_ambil_lagi=False)
        conn.commit()
    return n


# ─── CRM: status & jadwal follow-up (ruang aktif) ─────────────────────────────

_KANAL = {"Email Terkirim": "email", "WA Terkirim": "wa"}
# Status yang berarti urusan dengan lead ini selesai — jadwal follow-up dikosongkan.
STATUS_SELESAI = ("Deal", "Tidak Tertarik", "Jangan Hubungi")


def update_crm(place_key, status=None, catatan=None, tanggal_follow_up=None,
               nama_pic=None, fu1_hari=3, fu2_hari=7):
    """
    Ubah kolom CRM. Status kirim mengisi jadwal follow-up otomatis:

      Email/WA Terkirim → tanggal_dihubungi = hari ini, follow-up = +fu1_hari
      Follow-up 1       → follow-up = tanggal_dihubungi + fu2_hari
      Follow-up 2 / Deal / Tidak Tertarik / Jangan Hubungi → follow-up dikosongkan

    `tanggal_follow_up` yang dikirim eksplisit selalu menang atas jadwal otomatis.
    Return baris terbaru, atau None bila lead tidak ada.
    """
    lama = get(place_key)
    if not lama:
        return None
    fu1, fu2 = int(fu1_hari or 3), int(fu2_hari or 7)
    ubah = {}
    if catatan is not None:
        ubah["catatan"] = catatan
    if nama_pic is not None:
        ubah["nama_pic"] = nama_pic.strip()
    if status is not None:
        ubah["status_leads"] = status
        sekarang = datetime.now()
        if status in _KANAL:
            ubah["tanggal_dihubungi"] = sekarang.strftime("%Y-%m-%d")
            kanal = set(filter(None, str(lama.get("kanal_kontak") or "").split(",")))
            kanal.add(_KANAL[status])
            ubah["kanal_kontak"] = ",".join(sorted(kanal))
            ubah["tanggal_follow_up"] = (sekarang + timedelta(days=fu1)).strftime("%Y-%m-%d")
        elif status == "Follow-up 1":
            dasar = _parse_ts(lama.get("tanggal_dihubungi")) or sekarang
            tgl = dasar + timedelta(days=fu2)
            if tgl.date() <= sekarang.date():
                tgl = sekarang + timedelta(days=max(fu2 - fu1, 1))
            ubah["tanggal_follow_up"] = tgl.strftime("%Y-%m-%d")
        elif status == "Follow-up 2" or status in STATUS_SELESAI:
            ubah["tanggal_follow_up"] = None
    if tanggal_follow_up is not None:
        ubah["tanggal_follow_up"] = tanggal_follow_up or None
    if ubah:
        with _lock:
            conn = get_conn()
            conn.execute(
                f"UPDATE businesses SET {', '.join(f'{k} = ?' for k in ubah)} "
                f"WHERE place_key = ?", list(ubah.values()) + [place_key])
            conn.commit()
    return get(place_key)


def pengaturan(bawaan):
    """Nilai pengaturan ruang aktif; kunci yang belum disimpan memakai `bawaan`."""
    hasil = dict(bawaan)
    with _lock:
        for r in get_conn().execute("SELECT kunci, nilai FROM pengaturan"):
            if r["kunci"] in hasil and r["nilai"] not in (None, ""):
                hasil[r["kunci"]] = r["nilai"]
    return hasil


def simpan_pengaturan(bawaan, nilai):
    """Simpan kunci yang dikenal `bawaan`; kunci lain diabaikan."""
    with _lock:
        conn = get_conn()
        for k, v in (nilai or {}).items():
            if k in bawaan:
                conn.execute("INSERT OR REPLACE INTO pengaturan (kunci, nilai) "
                             "VALUES (?, ?)", (k, str(v).strip()))
        conn.commit()
    return pengaturan(bawaan)


# ─── Query untuk halaman CRM ──────────────────────────────────────────────────

# ─── Riwayat run scraping ──────────────────────────────────────────────────
#
# Sebelum ini, satu run menumpuk seluruh leadnya di RAM dan baru menulis ke
# database di baris terakhir. Run 30 wilayah yang mati di wilayah ke-25
# kehilangan semuanya. Tabel `runs` + `run_leads` membalik itu: tiap lead dicatat
# begitu dibaca, dan run yang mati tetap bisa dirakit jadi Excel atau dilanjutkan.

# Field params yang TIDAK boleh ikut tersimpan di database.
_PARAM_RAHASIA = ("apify_proxy_token", "gemini_api_key")


def run_start(run_id, params=None, total_target=0):
    """Catat satu run baru sebagai 'berjalan'. Aman dipanggil ulang (resume)."""
    bersih = {k: v for k, v in dict(params or {}).items()
              if k not in _PARAM_RAHASIA and not k.startswith("_")}
    try:
        params_json = json.dumps(bersih, ensure_ascii=False)
    except (TypeError, ValueError):
        params_json = "{}"
    now = _now()
    with _lock:
        conn = get_conn()
        ada = conn.execute(
            "SELECT run_id FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if ada:
            # Melanjutkan run lama: params & target_index yang tersimpan JANGAN
            # ditimpa — itulah yang menentukan target mana yang masih tersisa.
            conn.execute(
                "UPDATE runs SET status = 'berjalan', selesai_pada = NULL "
                "WHERE run_id = ?", (run_id,))
        else:
            conn.execute(
                "INSERT INTO runs (run_id, mulai_pada, status, params_json, "
                "target_index, total_target) VALUES (?, ?, 'berjalan', ?, 0, ?)",
                (run_id, now, params_json, int(total_target or 0)),
            )
        conn.commit()
    return run_id


def run_tandai_target(run_id, index):
    """Tandai bahwa `index` target pertama sudah TUNTAS dikerjakan."""
    if not run_id:
        return
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE runs SET target_index = ? WHERE run_id = ? AND target_index < ?",
            (int(index), run_id, int(index)),
        )
        conn.commit()


def run_catat_lead(run_id, place_key, jenis="baru"):
    """
    Klaim satu lead untuk run ini. INSERT OR IGNORE: satu bisnis boleh muncul
    berkali-kali dalam satu run (mis. dari dua kecamatan bertetangga) tanpa
    menggandakan barisnya di file hasil.
    """
    if not run_id or not place_key:
        return
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO run_leads (run_id, place_key, jenis, dicatat) "
            "VALUES (?, ?, ?, ?)",
            (run_id, place_key, jenis, _now()),
        )
        conn.commit()


def run_finish(run_id, status="selesai", filename="", baru=0, diupdate=0):
    """Tutup run dengan status akhir: selesai / dibatalkan / terputus."""
    if not run_id:
        return
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE runs SET status = ?, selesai_pada = ?, filename = ?, "
            "baru = ?, diupdate = ? WHERE run_id = ?",
            (status, _now(), filename or "", int(baru), int(diupdate), run_id),
        )
        conn.commit()


def run_leads_rows(run_id, jenis="baru"):
    """
    Semua lead yang diklaim run ini, langsung dari tabel businesses.

    Inilah sumber file Excel — bukan list di RAM. Konsekuensinya: file bisa
    dirakit kapan saja, termasuk untuk run yang prosesnya sudah mati, dan isinya
    selalu gambaran terlengkap (sudah termasuk hasil enrichment yang tersimpan
    belakangan).
    """
    if not run_id:
        return []
    sql = ("SELECT b.* FROM businesses b "
           "JOIN run_leads rl ON rl.place_key = b.place_key "
           "WHERE rl.run_id = ?")
    args = [run_id]
    if jenis:
        sql += " AND rl.jenis = ?"
        args.append(jenis)
    sql += " ORDER BY COALESCE(b.skor_pembeli, 0) DESC"
    with _lock:
        cur = get_conn().execute(sql, args)
        return [dict(r) for r in cur.fetchall()]


def run_get(run_id):
    """Satu baris run sebagai dict (params_json sudah di-parse jadi `params`)."""
    if not run_id:
        return None
    with _lock:
        row = get_conn().execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["params"] = json.loads(d.get("params_json") or "{}")
    except (TypeError, ValueError):
        d["params"] = {}
    return d


def run_hitung_lead(run_id):
    """Berapa lead sudah aman tersimpan untuk run ini."""
    if not run_id:
        return 0
    with _lock:
        row = get_conn().execute(
            "SELECT COUNT(*) AS n FROM run_leads WHERE run_id = ?", (run_id,)
        ).fetchone()
    return int(row["n"]) if row else 0


def run_terputus_terakhir():
    """
    Run terputus paling baru yang MASIH punya sisa target, atau None.

    Run yang terputus tepat sesudah target terakhir tidak dilaporkan: tidak ada
    yang bisa dilanjutkan darinya, dan menawarkan tombol Lanjutkan yang tidak
    mengerjakan apa pun hanya membingungkan.
    """
    with _lock:
        row = get_conn().execute(
            "SELECT run_id FROM runs WHERE status = 'terputus' "
            "ORDER BY mulai_pada DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    d = run_get(row["run_id"])
    if not d:
        return None
    sisa = int(d.get("total_target") or 0) - int(d.get("target_index") or 0)
    if sisa <= 0:
        return None
    d["sisa_target"] = sisa
    d["jumlah_lead"] = run_hitung_lead(d["run_id"])
    return d


def _where_leads(tier=None, jasa_utama=None, area=None, status_leads=None,
                 punya_wa=None, skor_min=None, skor_max=None, q=None,
                 run=None, kontak=None, follow_up=False, tanpa_opt_out=False,
                 website=None, kota=None, kanal=None, tab=None, **_abaikan):
    """WHERE halaman Database Leads. Return (klausa, args)."""
    where, args = [SQL_AKTIF], []
    if run:
        where.append("place_key IN (SELECT place_key FROM run_leads WHERE run_id = ?)")
        args.append(run)
    if tier:
        where.append("tier = ?")
        args.append(tier)
    if jasa_utama:
        where.append("jasa_utama = ?")
        args.append(jasa_utama)
    if area:
        where.append("area_pencarian = ?")
        args.append(area)
    if status_leads:
        # NULL diperlakukan sama dengan "Belum Dihubungi", seperti di stats().
        where.append("COALESCE(status_leads, 'Belum Dihubungi') = ?")
        args.append(status_leads)
    if punya_wa:
        where.append("whatsapp_link IS NOT NULL AND whatsapp_link != ''")
    kondisi = kondisi_kontak(kontak)
    if kondisi:
        where.append(kondisi)
    if website == "ada":
        where.append("COALESCE(website, '') != '' AND COALESCE(web_status, '') != 'tidak_ada'")
    elif website == "tidak":
        where.append("(COALESCE(website, '') = '' OR web_status = 'tidak_ada')")
    if kota:
        where.append("kota = ?")
        args.append(kota)
    if kanal:
        where.append("COALESCE(kanal_kontak, '') LIKE ?")
        args.append(f"%{kanal}%")
    if tab and _sql_tab(tab):
        where.append(_sql_tab(tab))
    if follow_up:
        where.append("tanggal_follow_up IS NOT NULL AND tanggal_follow_up <= ? AND "
                     "COALESCE(status_leads, 'Belum Dihubungi') NOT IN "
                     "('Deal', 'Tidak Tertarik', 'Jangan Hubungi')")
        args.append(datetime.now().strftime("%Y-%m-%d"))
    if tanpa_opt_out:
        # Lead yang minta tidak dihubungi lagi tidak boleh ikut export (UU PDP).
        where.append("COALESCE(status_leads, '') != 'Jangan Hubungi'")
    if skor_min is not None:
        where.append("COALESCE(skor_pembeli, 0) >= ?")
        args.append(int(skor_min))
    if skor_max is not None:
        where.append("COALESCE(skor_pembeli, 0) <= ?")
        args.append(int(skor_max))
    if q:
        where.append("(nama_bisnis LIKE ? OR alamat LIKE ? OR kategori LIKE ? "
                     "OR email LIKE ? OR email_lain LIKE ? OR telepon LIKE ?)")
        args += [f"%{q}%"] * 6
    return " WHERE " + " AND ".join(where), args


URUTAN_LEADS = {
    "skor_pembeli": "COALESCE(skor_pembeli, 0) DESC",
    "ulasan": "COALESCE(jumlah_ulasan, 0) DESC",
    "rating": "COALESCE(rating, 0) DESC",
    "terbaru": "first_seen_at DESC",
    "nama": "nama_bisnis ASC",
    "dihubungi": "COALESCE(tanggal_dihubungi, '') DESC, COALESCE(skor_pembeli, 0) DESC",
    "follow_up": "tanggal_follow_up IS NULL, tanggal_follow_up ASC",
}


def hitung_tab_leads(**filters):
    """Angka badge nav tab Database Leads untuk filter aktif (tab diabaikan)."""
    filters.pop("tab", None)
    return hitung_tab(*_where_leads(**filters))


def query_leads(urut="skor_pembeli", limit=50, offset=0, **filters):
    """
    Ambil lead dengan filter (lihat `_where_leads`). Return (rows, total_sebelum_paginasi).

    `run` membatasi hasil ke lead yang diklaim satu run scraping — dipakai tombol
    "Buat Excel dari run itu" untuk run yang terputus.
    """
    klausa, args = _where_leads(**filters)
    kolom_urut = urutan_dengan_pin(URUTAN_LEADS.get(urut, URUTAN_LEADS["skor_pembeli"]))

    with _lock:
        conn = get_conn()
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM businesses{klausa}", args
        ).fetchone()["n"]
        sql = f"SELECT * FROM businesses{klausa} ORDER BY {kolom_urut}"
        if limit:
            cur = conn.execute(sql + " LIMIT ? OFFSET ?", args + [int(limit), int(offset)])
        else:
            cur = conn.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
    return rows, total


def nilai_unik(kolom):
    """Daftar nilai berbeda pada satu kolom — untuk mengisi dropdown filter."""
    if kolom not in set(_kolom_tabel()):
        return []
    with _lock:
        cur = get_conn().execute(
            f"SELECT DISTINCT {kolom} AS v FROM businesses "
            f"WHERE {kolom} IS NOT NULL AND {kolom} != '' ORDER BY v"
        )
        return [r["v"] for r in cur.fetchall()]


def hitung_cabang(nama_bisnis, alamat=None):
    """
    Berapa lokasi dengan nama bisnis sama di database.

    Jaringan dengan beberapa cabang biasanya punya anggaran lebih besar — dipakai
    sebagai sinyal "mampu bayar" di scrapers/scoring.py.
    """
    nama = str(nama_bisnis or "").strip()
    if len(nama) < 4:
        return 1
    with _lock:
        cur = get_conn().execute(
            "SELECT COUNT(DISTINCT COALESCE(alamat, place_key)) AS n "
            "FROM businesses WHERE nama_bisnis = ?",
            (nama,),
        )
        n = cur.fetchone()["n"] or 0
    return max(n, 1)


# ─── Statistik dashboard ──────────────────────────────────────────────────────

def stats():
    hari_ini = datetime.now().strftime("%Y-%m-%d")
    batas_30 = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        conn = get_conn()

        # Hanya lead aktif — sama dengan yang tampil di halaman Leads.
        B = f"(SELECT * FROM businesses WHERE {SQL_AKTIF})"

        def satu(sql, args=()):
            return conn.execute(sql, args).fetchone()["n"] or 0

        def kelompok(sql, args=()):
            return {r["k"]: r["n"] for r in conn.execute(sql, args).fetchall() if r["k"]}

        return {
            "total": satu(f"SELECT COUNT(*) AS n FROM {B}"),
            "per_tier": kelompok(
                f"SELECT tier AS k, COUNT(*) AS n FROM {B} GROUP BY tier"
            ),
            "per_jasa": kelompok(
                f"SELECT jasa_utama AS k, COUNT(*) AS n FROM {B} "
                "GROUP BY jasa_utama ORDER BY n DESC LIMIT 12"
            ),
            "per_area": kelompok(
                f"SELECT area_pencarian AS k, COUNT(*) AS n FROM {B} "
                "GROUP BY area_pencarian ORDER BY n DESC LIMIT 10"
            ),
            "per_status": kelompok(
                "SELECT COALESCE(status_leads, 'Belum Dihubungi') AS k, "
                f"COUNT(*) AS n FROM {B} GROUP BY k"
            ),
            "belum_dihubungi": satu(
                f"SELECT COUNT(*) AS n FROM {B} "
                "WHERE status_leads IS NULL OR status_leads = 'Belum Dihubungi'"
            ),
            "punya_wa": satu(
                f"SELECT COUNT(*) AS n FROM {B} "
                "WHERE whatsapp_link IS NOT NULL AND whatsapp_link != ''"
            ),
            "follow_up_hari_ini": satu(
                f"SELECT COUNT(*) AS n FROM {B} "
                "WHERE tanggal_follow_up IS NOT NULL AND tanggal_follow_up <= ? "
                "AND COALESCE(status_leads, 'Belum Dihubungi') "
                "NOT IN ('Deal', 'Tidak Tertarik', 'Jangan Hubungi')",
                (hari_ini,),
            ),
            "baru_30_hari": satu(
                "SELECT COUNT(*) AS n FROM businesses WHERE first_seen_at >= ?",
                (batas_30,),
            ),
            "tanpa_kontak": satu(
                f"SELECT COUNT(*) AS n FROM {B} WHERE COALESCE(whatsapp_link, '') = '' "
                "AND COALESCE(email, '') = ''"
            ),
            "perubahan_30_hari": satu(
                "SELECT COUNT(*) AS n FROM business_changes WHERE changed_at >= ?",
                (batas_30,),
            ),
        }


# ─── Marketplace intelligence (mp_*) ──────────────────────────────────────────
#
# JEBAKAN BOOLEAN: SQLite menyimpan boolean sebagai 0/1, jadi baris yang dibaca
# kembali dari DB TIDAK PERNAH lolos `x is True`. Semua kode di bawah menulis
# dengan `int(bool(v))` dan membaca dengan truthiness biasa. Aturan yang sama
# sudah menggigit sisi leads sebelumnya — jangan diulang di sini.

BIAYA_BAWAAN = {
    # Angka awal yang masuk akal untuk penjual non-official di 2026, dan memang
    # dimaksudkan untuk diedit user lewat UI — biaya tiap seller berbeda
    # tergantung program yang diikuti (Gratis Ongkir XTRA, Bebas Ongkir, dll).
    "shopee":    {"fee_persen": 8.0, "biaya_tetap": 5000},
    "tokopedia": {"fee_persen": 6.5, "biaya_tetap": 5000},
}


def _kolom_mp(tabel):
    with _lock:
        cur = get_conn().execute(f"PRAGMA table_info({tabel})")
        return [r["name"] for r in cur.fetchall()]


# ── Toko ──

def mp_store_upsert(store):
    """Simpan/perbarui satu toko. `is_own` tidak pernah ditimpa oleh scraper."""
    sid = store.get("id")
    if not sid:
        raise ValueError("mp_store_upsert butuh id")
    kolom_valid = set(_kolom_mp("mp_stores"))
    data = {k: v for k, v in store.items() if k in kolom_valid}
    if "is_own" in data:
        data["is_own"] = int(bool(data["is_own"]))
    if "aktif" in data:
        data["aktif"] = int(bool(data["aktif"]))

    with _lock:
        conn = get_conn()
        ada = conn.execute("SELECT * FROM mp_stores WHERE id = ?", (sid,)).fetchone()
        if ada is None:
            data.setdefault("created_at", _now())
            data.setdefault("is_own", 0)
            data.setdefault("aktif", 1)
            kolom = list(data.keys())
            conn.execute(
                f"INSERT INTO mp_stores ({', '.join(kolom)}) "
                f"VALUES ({', '.join('?' * len(kolom))})",
                [data[k] for k in kolom],
            )
            conn.commit()
            return "insert"
        data = {k: v for k, v in data.items()
                if v is not None or dict(ada).get(k) is None}
        data.pop("created_at", None)
        if not data:
            return "update"
        kolom = list(data.keys())
        conn.execute(
            f"UPDATE mp_stores SET {', '.join(f'{k} = ?' for k in kolom)} WHERE id = ?",
            [data[k] for k in kolom] + [sid],
        )
        conn.commit()
        return "update"


def mp_stores(hanya_aktif=True, is_own=None):
    sql = "SELECT * FROM mp_stores WHERE 1=1"
    args = []
    if hanya_aktif:
        sql += " AND aktif = 1"
    if is_own is not None:
        sql += " AND is_own = ?"
        args.append(int(bool(is_own)))
    sql += " ORDER BY is_own DESC, platform, nama"
    with _lock:
        return [dict(r) for r in get_conn().execute(sql, args).fetchall()]


def mp_store_set_own(store_id, is_own):
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE mp_stores SET is_own = ? WHERE id = ?",
                     (int(bool(is_own)), store_id))
        # Produk yang sudah tersimpan ikut ditandai ulang, kalau tidak statistik
        # pasar masih memakai penandaan lama sampai scrape berikutnya.
        conn.execute("UPDATE mp_products SET is_own = ? WHERE store_id = ?",
                     (int(bool(is_own)), store_id))
        conn.commit()


def mp_store_status(store_id, ok=True, error=""):
    with _lock:
        conn = get_conn()
        if ok:
            conn.execute("UPDATE mp_stores SET last_ok_at = ?, last_error = '' "
                         "WHERE id = ?", (_now(), store_id))
        else:
            conn.execute("UPDATE mp_stores SET last_error = ? WHERE id = ?",
                         (str(error)[:500], store_id))
        conn.commit()


# ── Produk & riwayat ──

def mp_upsert_product(rec, store_id=None, is_own=None):
    """Simpan produk hasil panen. Return "insert" | "update".

    Meniru disiplin `upsert_business`: nilai None berarti "gagal dibaca kali ini",
    bukan "sekarang kosong", jadi tidak pernah menimpa nilai lama yang bagus.
    """
    key = rec.get("product_key")
    if not key:
        raise ValueError("mp_upsert_product butuh product_key")

    kolom_valid = set(_kolom_mp("mp_products"))
    data = {k: v for k, v in rec.items() if k in kolom_valid}
    if store_id:
        data["store_id"] = store_id
    if is_own is not None:
        data["is_own"] = int(bool(is_own))
    now = _now()

    with _lock:
        conn = get_conn()
        baris = conn.execute("SELECT * FROM mp_products WHERE product_key = ?",
                             (key,)).fetchone()
        ada = dict(baris) if baris else None

        if ada is None:
            data["first_seen_at"] = now
            data["last_seen_at"] = now
            data["times_seen"] = 1
            data.setdefault("is_own", 0)
            kolom = list(data.keys())
            conn.execute(
                f"INSERT INTO mp_products ({', '.join(kolom)}) "
                f"VALUES ({', '.join('?' * len(kolom))})",
                [data[k] for k in kolom],
            )
            conn.commit()
            return "insert"

        for k in KOLOM_MP_TERLINDUNGI:
            # `is_own` boleh diubah lewat mp_store_set_own, tapi tidak lewat scrape.
            if k != "is_own" or is_own is None:
                data.pop(k, None)
        data = {k: v for k, v in data.items()
                if v is not None or ada.get(k) is None}
        data["last_seen_at"] = now
        data["times_seen"] = int(ada.get("times_seen") or 0) + 1
        kolom = list(data.keys())
        conn.execute(
            f"UPDATE mp_products SET {', '.join(f'{k} = ?' for k in kolom)} "
            f"WHERE product_key = ?",
            [data[k] for k in kolom] + [key],
        )
        conn.commit()
        return "update"


def mp_snapshot_price(product_key, harga, terjual=0, stok=0, run_id=None):
    """Catat harga HANYA bila berubah dari snapshot terakhir.

    Menyimpan tiap run apa adanya akan membuat riwayat penuh baris identik dan
    grafik trennya jadi datar penuh titik palsu. Return True bila benar mencatat.
    """
    if not product_key or not harga:
        return False
    with _lock:
        conn = get_conn()
        akhir = conn.execute(
            "SELECT harga, terjual FROM mp_price_history WHERE product_key = ? "
            "ORDER BY id DESC LIMIT 1", (product_key,)
        ).fetchone()
        if akhir and int(akhir["harga"] or 0) == int(harga) \
                and int(akhir["terjual"] or 0) == int(terjual or 0):
            return False
        conn.execute(
            "INSERT INTO mp_price_history "
            "(product_key, run_id, harga, terjual, stok, captured_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (product_key, run_id, int(harga), int(terjual or 0),
             int(stok or 0), _now()),
        )
        conn.commit()
        return True


def mp_products_semua(store_ids=None, is_own=None, batas=0):
    sql = "SELECT * FROM mp_products WHERE 1=1"
    args = []
    if store_ids:
        sql += f" AND store_id IN ({', '.join('?' * len(store_ids))})"
        args += list(store_ids)
    if is_own is not None:
        sql += " AND is_own = ?"
        args.append(int(bool(is_own)))
    sql += " ORDER BY nama_produk"
    if batas:
        sql += f" LIMIT {int(batas)}"
    with _lock:
        return [dict(r) for r in get_conn().execute(sql, args).fetchall()]


def mp_riwayat_harga(product_key, batas=60):
    with _lock:
        return [dict(r) for r in get_conn().execute(
            "SELECT * FROM mp_price_history WHERE product_key = ? "
            "ORDER BY captured_at DESC LIMIT ?", (product_key, int(batas))
        ).fetchall()]


# ── Modal & biaya (data milik user) ──

def mp_set_modal(product_key, modal, catatan=""):
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO mp_modal (product_key, modal, catatan, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(product_key) DO UPDATE SET "
            "modal = excluded.modal, catatan = excluded.catatan, "
            "updated_at = excluded.updated_at",
            (product_key, int(modal or 0), catatan or "", _now()),
        )
        conn.commit()


def mp_get_modal(product_key):
    with _lock:
        r = get_conn().execute("SELECT * FROM mp_modal WHERE product_key = ?",
                               (product_key,)).fetchone()
        return dict(r) if r else None


def mp_biaya(platform=None):
    """Biaya per platform, dengan nilai bawaan untuk platform yang belum diatur."""
    with _lock:
        rows = {r["platform"]: dict(r) for r in
                get_conn().execute("SELECT * FROM mp_biaya").fetchall()}
    hasil = {}
    for plat, bawaan in BIAYA_BAWAAN.items():
        hasil[plat] = dict(bawaan, **{k: v for k, v in rows.get(plat, {}).items()
                                      if k in ("fee_persen", "biaya_tetap")})
    for plat, r in rows.items():
        hasil.setdefault(plat, {"fee_persen": r.get("fee_persen") or 0,
                                "biaya_tetap": r.get("biaya_tetap") or 0})
    if platform:
        return hasil.get(platform, {"fee_persen": 0.0, "biaya_tetap": 0})
    return hasil


def mp_set_biaya(platform, fee_persen, biaya_tetap):
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO mp_biaya (platform, fee_persen, biaya_tetap, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(platform) DO UPDATE SET "
            "fee_persen = excluded.fee_persen, biaya_tetap = excluded.biaya_tetap, "
            "updated_at = excluded.updated_at",
            (platform, float(fee_persen or 0), int(biaya_tetap or 0), _now()),
        )
        conn.commit()


# ── Etalase ──

def mp_etalase_upsert(store_id, slug, nama="", url="", n_produk=None):
    """Simpan/perbarui satu etalase. `terakhir_panen_at` hanya diubah oleh
    `mp_etalase_dipanen` — mendaftar etalase bukan berarti sudah memanennya."""
    if not store_id or not slug:
        raise ValueError("mp_etalase_upsert butuh store_id dan slug")
    eid = f"{store_id}:{slug}"
    with _lock:
        conn = get_conn()
        ada = conn.execute("SELECT id FROM mp_etalase WHERE id = ?", (eid,)).fetchone()
        if ada is None:
            conn.execute(
                "INSERT INTO mp_etalase (id, store_id, slug, nama, url, n_produk, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (eid, store_id, slug, nama or slug, url or "",
                 int(n_produk or 0), _now()))
        else:
            data, kolom = {}, []
            if nama:
                data["nama"] = nama
            if url:
                data["url"] = url
            if n_produk is not None:
                data["n_produk"] = int(n_produk)
            if data:
                kolom = list(data.keys())
                conn.execute(
                    f"UPDATE mp_etalase SET {', '.join(f'{k} = ?' for k in kolom)} "
                    f"WHERE id = ?", [data[k] for k in kolom] + [eid])
        conn.commit()
    return eid


def mp_etalase_dipanen(store_id, slug, n_produk=0):
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE mp_etalase SET terakhir_panen_at = ?, n_produk = ? "
                     "WHERE id = ?", (_now(), int(n_produk or 0),
                                      f"{store_id}:{slug}"))
        conn.commit()


def mp_etalase_list(store_id=None):
    sql = "SELECT * FROM mp_etalase"
    args = []
    if store_id:
        sql += " WHERE store_id = ?"
        args.append(store_id)
    sql += " ORDER BY nama"
    with _lock:
        return [dict(r) for r in get_conn().execute(sql, args).fetchall()]


def mp_etalase_umur_jam(store_id, slug):
    """Berapa jam sejak etalase ini terakhir dipanen. None bila belum pernah."""
    with _lock:
        r = get_conn().execute(
            "SELECT terakhir_panen_at FROM mp_etalase WHERE id = ?",
            (f"{store_id}:{slug}",)).fetchone()
    t = _parse_ts(r["terakhir_panen_at"]) if r else None
    return None if not t else (datetime.now() - t).total_seconds() / 3600


# ── Run ──

def mp_run_start(run_id, mode="", kueri="", engine="", store_ids=""):
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO mp_runs "
            "(run_id, mode, kueri, engine, store_ids, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, mode, kueri, engine, str(store_ids), _now()),
        )
        conn.commit()


def mp_run_finish(run_id, total=0, filename="", sumber_tier=""):
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE mp_runs SET total = ?, filename = ?, sumber_tier = ?, "
            "finished_at = ? WHERE run_id = ?",
            (int(total or 0), filename or "", sumber_tier or "", _now(), run_id),
        )
        conn.commit()


# ── Hapus ──

# Scope dicocokkan ke daftar literal ini, BUKAN dipakai sebagai nama tabel dari
# input user — itu jalan tol untuk SQL injection sekaligus penghapusan tabel leads.
_SCOPE_HAPUS = {"run", "store", "produk", "riwayat", "produk_semua", "semua"}


def mp_hapus(scope, nilai=None):
    """Hapus data mp_* sesuai scope. Tidak pernah menyentuh tabel leads.

    Return dict jumlah baris terhapus per tabel.
    """
    if scope not in _SCOPE_HAPUS:
        raise ValueError(f"scope tidak dikenal: {scope}")

    hasil = {}
    with _lock:
        conn = get_conn()

        def jalan(nama, sql, args=()):
            cur = conn.execute(sql, args)
            hasil[nama] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

        if scope == "run":
            jalan("mp_price_history",
                  "DELETE FROM mp_price_history WHERE run_id = ?", (nilai,))
            jalan("mp_runs", "DELETE FROM mp_runs WHERE run_id = ?", (nilai,))
        elif scope == "store":
            jalan("mp_price_history",
                  "DELETE FROM mp_price_history WHERE product_key IN "
                  "(SELECT product_key FROM mp_products WHERE store_id = ?)", (nilai,))
            jalan("mp_products", "DELETE FROM mp_products WHERE store_id = ?", (nilai,))
            jalan("mp_etalase", "DELETE FROM mp_etalase WHERE store_id = ?", (nilai,))
            jalan("mp_stores", "DELETE FROM mp_stores WHERE id = ?", (nilai,))
        elif scope == "produk":
            jalan("mp_price_history",
                  "DELETE FROM mp_price_history WHERE product_key = ?", (nilai,))
            jalan("mp_products",
                  "DELETE FROM mp_products WHERE product_key = ?", (nilai,))
        elif scope == "riwayat":
            batas = (datetime.now() - timedelta(days=int(nilai or 90))
                     ).strftime("%Y-%m-%d %H:%M:%S")
            jalan("mp_price_history",
                  "DELETE FROM mp_price_history WHERE captured_at < ?", (batas,))
        elif scope == "produk_semua":
            jalan("mp_price_history", "DELETE FROM mp_price_history")
            jalan("mp_products", "DELETE FROM mp_products")
        elif scope == "semua":
            # Sengaja TIDAK menghapus mp_modal dan mp_biaya: itu angka yang user
            # ketik sendiri dan tidak bisa dipanen ulang oleh scraper mana pun.
            for t in ("mp_price_history", "mp_products", "mp_etalase",
                      "mp_runs", "mp_stores"):
                jalan(t, f"DELETE FROM {t}")

        conn.commit()
    return hasil


def mp_stats():
    with _lock:
        conn = get_conn()

        def satu(sql, args=()):
            return conn.execute(sql, args).fetchone()["n"] or 0

        return {
            "toko": satu("SELECT COUNT(*) AS n FROM mp_stores"),
            "toko_sendiri": satu("SELECT COUNT(*) AS n FROM mp_stores WHERE is_own = 1"),
            "produk": satu("SELECT COUNT(*) AS n FROM mp_products"),
            "produk_sendiri": satu("SELECT COUNT(*) AS n FROM mp_products WHERE is_own = 1"),
            "riwayat": satu("SELECT COUNT(*) AS n FROM mp_price_history"),
            "etalase": satu("SELECT COUNT(*) AS n FROM mp_etalase"),
            "modal_terisi": satu("SELECT COUNT(*) AS n FROM mp_modal WHERE modal > 0"),
            "run": satu("SELECT COUNT(*) AS n FROM mp_runs"),
        }


if __name__ == "__main__":
    print("Database:", init_db())
    for k, v in stats().items():
        print(f"  {k}: {v}")
    print("  marketplace:", mp_stats())
