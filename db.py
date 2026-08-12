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
import re
import sqlite3
import threading
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path("data") / "leads.db"

_conn = None
_lock = threading.RLock()

# Rata-rata hari per bulan — dipakai untuk mengubah "6 bulan" jadi rentang hari.
_HARI_PER_BULAN = 30.44

# Kolom yang TIDAK boleh ditimpa saat upsert bisnis yang sudah ada:
# ini hasil kerja manual user, bukan hasil scraping.
_KOLOM_MILIK_USER = {"status_leads", "catatan", "tanggal_follow_up", "first_seen_at"}

SCHEMA = """
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
"""


# ─── Koneksi ──────────────────────────────────────────────────────────────────

def get_conn():
    """Koneksi tunggal yang dipakai bersama semua thread (dilindungi _lock)."""
    global _conn
    with _lock:
        if _conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
            _conn.commit()
        return _conn


def init_db():
    """Buat file database + tabel bila belum ada. Aman dipanggil berkali-kali."""
    get_conn()
    return str(DB_PATH.resolve())


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

def upsert_business(row):
    """
    Simpan bisnis baru, atau perbarui yang sudah ada.

    Saat memperbarui: `first_seen_at` dipertahankan, `times_seen` naik satu, dan
    kolom CRM (status_leads/catatan/tanggal_follow_up) TIDAK ditimpa — itu hasil
    kerja manual user.

    Return "insert" atau "update".
    """
    place_key = row.get("place_key")
    if not place_key:
        raise ValueError("upsert_business butuh place_key")

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
        data["last_scraped_at"] = now
        data["last_checked_at"] = now
        data["times_seen"] = int(ada.get("times_seen") or 0) + 1
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


def touch_checked(place_key):
    """Tandai bisnis sudah dicek hari ini walau tidak ada yang berubah."""
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE businesses SET last_checked_at = ? WHERE place_key = ?",
            (_now(), place_key),
        )
        conn.commit()


def simpan_intelijen(place_key, skor_pembeli, tier, jasa_utama,
                     jasa_pendukung, alasan_pitch):
    """Perbarui hasil scoring/rekomendasi tanpa menyentuh kolom lain."""
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE businesses SET skor_pembeli = ?, tier = ?, jasa_utama = ?, "
            "jasa_pendukung = ?, alasan_pitch = ? WHERE place_key = ?",
            (skor_pembeli, tier, jasa_utama, jasa_pendukung, alasan_pitch, place_key),
        )
        conn.commit()


def update_status(place_key, status_leads=None, catatan=None, tanggal_follow_up=None):
    """Ubah kolom CRM dari halaman /leads."""
    set_parts, nilai = [], []
    if status_leads is not None:
        set_parts.append("status_leads = ?")
        nilai.append(status_leads)
    if catatan is not None:
        set_parts.append("catatan = ?")
        nilai.append(catatan)
    if tanggal_follow_up is not None:
        set_parts.append("tanggal_follow_up = ?")
        nilai.append(tanggal_follow_up or None)
    if not set_parts:
        return False
    nilai.append(place_key)
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            f"UPDATE businesses SET {', '.join(set_parts)} WHERE place_key = ?", nilai
        )
        conn.commit()
    return cur.rowcount > 0


def log_search(keyword, area, total_ditemukan=0, baru=0, dilewati=0, diupdate=0):
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO searches (keyword, area, run_at, total_ditemukan, baru, "
            "dilewati, diupdate) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (keyword, area, _now(), total_ditemukan, baru, dilewati, diupdate),
        )
        conn.commit()


def riwayat_pencarian(keyword, area, batas=1):
    """Kapan kombinasi keyword+area ini terakhir dijalankan (untuk info di UI)."""
    with _lock:
        cur = get_conn().execute(
            "SELECT * FROM searches WHERE keyword = ? AND area = ? "
            "ORDER BY run_at DESC LIMIT ?",
            (keyword, area, batas),
        )
        return [dict(r) for r in cur.fetchall()]


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


# ─── Query untuk halaman CRM ──────────────────────────────────────────────────

def query_leads(tier=None, jasa_utama=None, area=None, status_leads=None,
                punya_wa=None, skor_min=None, skor_max=None, q=None,
                urut="skor_pembeli", limit=50, offset=0):
    """
    Ambil lead dengan filter. Return (rows, total_sebelum_paginasi).
    """
    where, args = [], []
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
        where.append("status_leads = ?")
        args.append(status_leads)
    if punya_wa:
        where.append("whatsapp_link IS NOT NULL AND whatsapp_link != ''")
    if skor_min is not None:
        where.append("COALESCE(skor_pembeli, 0) >= ?")
        args.append(int(skor_min))
    if skor_max is not None:
        where.append("COALESCE(skor_pembeli, 0) <= ?")
        args.append(int(skor_max))
    if q:
        where.append("(nama_bisnis LIKE ? OR alamat LIKE ? OR kategori LIKE ?)")
        pola = f"%{q}%"
        args += [pola, pola, pola]

    klausa = (" WHERE " + " AND ".join(where)) if where else ""
    kolom_urut = {
        "skor_pembeli": "COALESCE(skor_pembeli, 0) DESC",
        "ulasan": "COALESCE(jumlah_ulasan, 0) DESC",
        "rating": "COALESCE(rating, 0) DESC",
        "terbaru": "first_seen_at DESC",
        "nama": "nama_bisnis ASC",
    }.get(urut, "COALESCE(skor_pembeli, 0) DESC")

    with _lock:
        conn = get_conn()
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM businesses{klausa}", args
        ).fetchone()["n"]
        cur = conn.execute(
            f"SELECT * FROM businesses{klausa} ORDER BY {kolom_urut} LIMIT ? OFFSET ?",
            args + [int(limit), int(offset)],
        )
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

        def satu(sql, args=()):
            return conn.execute(sql, args).fetchone()["n"] or 0

        def kelompok(sql, args=()):
            return {r["k"]: r["n"] for r in conn.execute(sql, args).fetchall() if r["k"]}

        return {
            "total": satu("SELECT COUNT(*) AS n FROM businesses"),
            "per_tier": kelompok(
                "SELECT tier AS k, COUNT(*) AS n FROM businesses GROUP BY tier"
            ),
            "per_jasa": kelompok(
                "SELECT jasa_utama AS k, COUNT(*) AS n FROM businesses "
                "GROUP BY jasa_utama ORDER BY n DESC LIMIT 12"
            ),
            "per_area": kelompok(
                "SELECT area_pencarian AS k, COUNT(*) AS n FROM businesses "
                "GROUP BY area_pencarian ORDER BY n DESC LIMIT 10"
            ),
            "per_status": kelompok(
                "SELECT status_leads AS k, COUNT(*) AS n FROM businesses "
                "GROUP BY status_leads"
            ),
            "belum_dihubungi": satu(
                "SELECT COUNT(*) AS n FROM businesses "
                "WHERE status_leads IS NULL OR status_leads = 'Belum Dihubungi'"
            ),
            "punya_wa": satu(
                "SELECT COUNT(*) AS n FROM businesses "
                "WHERE whatsapp_link IS NOT NULL AND whatsapp_link != ''"
            ),
            "follow_up_hari_ini": satu(
                "SELECT COUNT(*) AS n FROM businesses "
                "WHERE tanggal_follow_up IS NOT NULL AND tanggal_follow_up <= ? "
                "AND status_leads NOT IN ('Deal', 'Tidak Tertarik')",
                (hari_ini,),
            ),
            "baru_30_hari": satu(
                "SELECT COUNT(*) AS n FROM businesses WHERE first_seen_at >= ?",
                (batas_30,),
            ),
            "perubahan_30_hari": satu(
                "SELECT COUNT(*) AS n FROM business_changes WHERE changed_at >= ?",
                (batas_30,),
            ),
        }


if __name__ == "__main__":
    print("Database:", init_db())
    for k, v in stats().items():
        print(f"  {k}: {v}")
