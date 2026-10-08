"""
dokumen_db.py — Database menu Dokumen & Surat (data/dokumen.db).

Database TERSENDIRI, bukan "ruang" ketiga di db.py. `db.get_conn()` hanya
mengenal ruang webdev/komponen dan cabang `else`-nya mengasumsikan skema
komponen, jadi ruang ketiga akan diam-diam mendapat tabel `businesses` plus
kolom komponen. Modul ini memakai koneksi sendiri dengan disiplin yang sama
(satu RLock, WAL, sqlite3.Row).

Kuncinya juga sendiri, bukan `db._lock`: kunci itu ditahan selama transaksi
scraping yang panjang, dan memakainya bersama akan membuat tombol "Buat
Invoice" menggantung di belakang run Google Maps yang sedang jalan.

Logika baca/tulis pengaturan TIDAK diduplikasi — intinya ada di
`db.kv_ambil`/`db.kv_simpan` yang menerima koneksi.

Penomoran sengaja berbeda dari `db_komponen.ambil_nomor_surat` yang reset
HARIAN dan berkunci place_key. Di sini nomor urut per (jenis, periode) dengan
periode = bulan untuk surat dan TAHUN untuk invoice/kwitansi/surat jalan/BAST,
dijamin `UNIQUE(jenis, periode, urut)` dan diambil di dalam transaksi yang sama
dengan penyisipan baris dokumen. Nomor invoice yang terulang adalah cacat
pembukuan, bukan sekadar kosmetik.
"""
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import backup
import db
from scrapers import docx_inti as inti
from scrapers import dokumen_buat as buat

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "dokumen.db"

_lock = threading.RLock()
_conn = None

SKEMA = """
CREATE TABLE IF NOT EXISTS pengaturan (
    kunci TEXT PRIMARY KEY,
    nilai TEXT
);

-- Katalog produk/jasa milik user. Tidak ada data benih: user mengisinya
-- sendiri, satu per satu atau lewat impor Excel.
CREATE TABLE IF NOT EXISTS produk (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kode          TEXT DEFAULT '',
    nama          TEXT NOT NULL,
    deskripsi     TEXT DEFAULT '',
    satuan        TEXT DEFAULT '',
    harga         INTEGER DEFAULT 0,      -- rupiah, SELALU bilangan bulat
    siklus        TEXT DEFAULT 'sekali',  -- sekali | bulanan | tahunan
    kategori      TEXT DEFAULT '',
    syarat_bawaan TEXT DEFAULT '[]',      -- JSON: butir S&K yang ikut produk ini
    aktif         INTEGER DEFAULT 1,
    dibuat        TEXT,
    diubah        TEXT
);
CREATE INDEX IF NOT EXISTS idx_produk_nama ON produk(nama);
CREATE INDEX IF NOT EXISTS idx_produk_aktif ON produk(aktif);

-- Bundel beberapa baris produk yang sering ditawarkan bersamaan.
CREATE TABLE IF NOT EXISTS paket (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    nama     TEXT NOT NULL,
    kategori TEXT DEFAULT '',
    items    TEXT DEFAULT '[]',
    catatan  TEXT DEFAULT '',
    dibuat   TEXT,
    diubah   TEXT
);

-- Buku pelanggan: terisi sendiri setiap dokumen dibuat, bisa dipakai ulang.
CREATE TABLE IF NOT EXISTS pelanggan (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT UNIQUE,
    nama        TEXT NOT NULL,
    alamat      TEXT DEFAULT '',
    kota        TEXT DEFAULT '',
    pic         TEXT DEFAULT '',
    jabatan_pic TEXT DEFAULT '',
    telepon     TEXT DEFAULT '',
    email       TEXT DEFAULT '',
    npwp        TEXT DEFAULT '',
    place_key   TEXT DEFAULT '',
    ruang       TEXT DEFAULT '',
    catatan     TEXT DEFAULT '',
    dibuat      TEXT,
    diubah      TEXT
);
CREATE INDEX IF NOT EXISTS idx_pelanggan_nama ON pelanggan(nama);

CREATE TABLE IF NOT EXISTS dokumen (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    jenis           TEXT NOT NULL,
    nomor           TEXT NOT NULL,
    periode         TEXT,
    urut            INTEGER,
    revisi          INTEGER DEFAULT 0,
    induk_id        INTEGER,
    status          TEXT DEFAULT 'draft',   -- draft | jadi
    tanggal         TEXT,
    pelanggan_id    INTEGER,
    pelanggan_nama  TEXT,
    pelanggan_kota  TEXT DEFAULT '',
    place_key       TEXT DEFAULT '',
    ruang           TEXT DEFAULT '',
    perihal         TEXT DEFAULT '',
    hal             TEXT DEFAULT '',
    data            TEXT DEFAULT '{}',      -- seluruh isian form (JSON)
    items           TEXT DEFAULT '[]',      -- baris hasil hitung (JSON)
    syarat          TEXT DEFAULT '[]',
    ppn_mode        TEXT DEFAULT 'tidak',
    subtotal        INTEGER DEFAULT 0,
    diskon_persen   INTEGER DEFAULT 0,
    diskon_global   INTEGER DEFAULT 0,
    biaya_kirim     INTEGER DEFAULT 0,
    dpp             INTEGER DEFAULT 0,
    dpp_nilai_lain  INTEGER DEFAULT 0,
    ppn_tarif       INTEGER DEFAULT 0,
    ppn             INTEGER DEFAULT 0,
    total           INTEGER DEFAULT 0,
    uang_muka       INTEGER DEFAULT 0,
    sisa            INTEGER DEFAULT 0,
    docx            TEXT DEFAULT '',        -- relatif terhadap output/dokumen
    pdf             TEXT DEFAULT '',
    dibuat          TEXT,
    diubah          TEXT
);
CREATE INDEX IF NOT EXISTS idx_dok_jenis ON dokumen(jenis, tanggal);
CREATE INDEX IF NOT EXISTS idx_dok_pelanggan ON dokumen(pelanggan_nama);
CREATE INDEX IF NOT EXISTS idx_dok_status ON dokumen(status);

-- Nomor urut. UNIQUE memastikan tabrakan GAGAL KERAS, bukan diam-diam
-- memberi nomor yang sama untuk dua dokumen.
CREATE TABLE IF NOT EXISTS nomor (
    jenis      TEXT NOT NULL,
    periode    TEXT NOT NULL,
    urut       INTEGER NOT NULL,
    nomor      TEXT NOT NULL,
    dokumen_id INTEGER,
    dibuat     TEXT,
    PRIMARY KEY (jenis, periode, urut)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_nomor_teks ON nomor(jenis, nomor);
"""

# ─── Pengaturan ───────────────────────────────────────────────────────────────

IDENTITAS_BAWAAN = {
    # Wajib — daftar lengkapnya ada di dokumen_buat.WAJIB_PENGATURAN
    "nama_usaha": "PT. NEXA TEKNOLOGI GROUP",
    "alamat": "",
    "kota_surat": "Jakarta",
    "telepon": "0821-8662-9996",
    "email": "nexateknologigroup@gmail.com",
    "status_pkp": "non-pkp",            # pkp | non-pkp
    "penanda_nama": "",
    "penanda_jabatan": "",
    "bank": "",
    "rekening": "",
    "rekening_atas_nama": "",
    # Opsional
    "website": "",
    "npwp": "",
    "nib": "",
    "penanda_wa": "",
    "penanda_email": "",
    "profil_usaha": "",
    "catatan_footer": "",
    # Nilai bawaan dokumen
    "ppn_mode": "nonmewah",
    "berlaku_hari": "14",
    "sla": "1x24 jam kerja",
    "termin": "DP 50% saat pemesanan, pelunasan sebelum pengiriman",
    "waktu_kerja": "7 hari kerja",
    "garansi": "",
    "tempo_hari": "14",                 # jatuh tempo invoice
}

# Pengaturan yang sah dikosongkan user: tanpa ini `db.kv_ambil` akan
# mengembalikannya ke nilai bawaan dan user tidak bisa menghapus isinya.
KOSONG_BOLEH = frozenset((
    "website", "npwp", "nib", "penanda_wa", "penanda_email", "profil_usaha",
    "catatan_footer", "garansi", "alamat", "penanda_nama", "penanda_jabatan",
    "bank", "rekening", "rekening_atas_nama",
))

# Pengaturan berupa angka. db.kv_simpan menyimpan semuanya sebagai TEXT, jadi
# nilainya di-parse saat dibaca — bukan dipakai mentah sebagai string.
ANGKA = ("berlaku_hari", "tempo_hari")

POLA_NOMOR_BAWAAN = {f"pola_nomor_{k}": v["pola"] for k, v in buat.JENIS.items()}

PENGATURAN_BAWAAN = {**IDENTITAS_BAWAAN, **POLA_NOMOR_BAWAAN, **buat.TEKS_BAWAAN}


# ─── Koneksi ──────────────────────────────────────────────────────────────────

def get_conn():
    global _conn
    with _lock:
        if _conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SKEMA)
            c.commit()
            _conn = c
        return _conn


def init():
    """Buat database + tabel, dan daftarkan diri ke cadangan harian."""
    get_conn()
    # backup.buat_backup() hanya mengulangi db.RUANG_PATH, jadi database ini
    # harus mendaftarkan diri atau invoice tidak akan pernah ikut tercadangkan.
    backup.EKSTRA["dokumen"] = DB_PATH


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _json(teks, bawaan):
    try:
        nilai = json.loads(teks or "")
    except (TypeError, ValueError):
        return bawaan
    return nilai if isinstance(nilai, type(bawaan)) else bawaan


def pengaturan():
    """Pengaturan dokumen; angka sudah berupa int, bukan string."""
    with _lock:
        nilai = db.kv_ambil(get_conn(), PENGATURAN_BAWAAN, KOSONG_BOLEH)
    for k in ANGKA:
        try:
            nilai[k] = int(str(nilai[k]).strip() or 0)
        except ValueError:
            nilai[k] = int(PENGATURAN_BAWAAN[k])
    return nilai


def simpan_pengaturan(nilai):
    with _lock:
        db.kv_simpan(get_conn(), PENGATURAN_BAWAAN, nilai)
    return pengaturan()


# ─── Katalog produk ───────────────────────────────────────────────────────────

KOLOM_PRODUK = ("kode", "nama", "deskripsi", "satuan", "harga", "siklus",
                "kategori", "syarat_bawaan", "aktif")
SIKLUS = ("sekali", "bulanan", "tahunan")


def _bersih_produk(d, baris_ke=None):
    """Normalkan satu produk. Raise ValueError dengan nomor baris saat impor."""
    awalan = f"Baris {baris_ke}: " if baris_ke else ""
    nama = str(d.get("nama") or "").strip()
    if not nama:
        raise ValueError(awalan + "Nama produk belum diisi")
    harga = inti.ke_int(d.get("harga"), awalan + "Harga")
    if harga < 0:
        raise ValueError(awalan + "Harga tidak boleh minus")
    siklus = str(d.get("siklus") or "sekali").strip().lower()
    if siklus not in SIKLUS:
        siklus = "sekali"
    syarat = d.get("syarat_bawaan") or []
    if isinstance(syarat, str):
        syarat = [s.strip() for s in syarat.split("\n") if s.strip()]
    return {
        "kode": str(d.get("kode") or "").strip(),
        "nama": nama,
        "deskripsi": str(d.get("deskripsi") or "").strip(),
        "satuan": str(d.get("satuan") or "").strip(),
        "harga": harga,
        "siklus": siklus,
        "kategori": str(d.get("kategori") or "").strip(),
        "syarat_bawaan": json.dumps([str(s) for s in syarat], ensure_ascii=False),
        "aktif": 1 if str(d.get("aktif", 1)) not in ("0", "False", "false", "") else 0,
    }


def produk_simpan(d):
    bersih = _bersih_produk(d)
    with _lock:
        conn = get_conn()
        pid = d.get("id")
        if pid:
            conn.execute(
                f"UPDATE produk SET {', '.join(f'{k} = ?' for k in KOLOM_PRODUK)}, "
                "diubah = ? WHERE id = ?",
                [bersih[k] for k in KOLOM_PRODUK] + [_now(), int(pid)])
        else:
            cur = conn.execute(
                f"INSERT INTO produk ({', '.join(KOLOM_PRODUK)}, dibuat, diubah) "
                f"VALUES ({', '.join('?' * len(KOLOM_PRODUK))}, ?, ?)",
                [bersih[k] for k in KOLOM_PRODUK] + [_now(), _now()])
            pid = cur.lastrowid
        conn.commit()
    return produk_get(pid)


def produk_get(pid):
    with _lock:
        r = get_conn().execute("SELECT * FROM produk WHERE id = ?", (int(pid),)).fetchone()
    return _baca_produk(r) if r else None


def _baca_produk(r):
    d = dict(r)
    d["syarat_bawaan"] = _json(d.get("syarat_bawaan"), [])
    return d


def produk_daftar(q="", kategori="", aktif_saja=True, limit=500):
    sql = "SELECT * FROM produk WHERE 1=1"
    arg = []
    if aktif_saja:
        sql += " AND aktif = 1"
    if q:
        sql += " AND (nama LIKE ? OR kode LIKE ? OR deskripsi LIKE ? OR kategori LIKE ?)"
        arg += [f"%{q}%"] * 4
    if kategori:
        sql += " AND kategori = ?"
        arg.append(kategori)
    sql += " ORDER BY kategori, nama LIMIT ?"
    arg.append(int(limit))
    with _lock:
        return [_baca_produk(r) for r in get_conn().execute(sql, arg)]


def produk_kategori():
    with _lock:
        return [r[0] for r in get_conn().execute(
            "SELECT DISTINCT kategori FROM produk WHERE kategori <> '' ORDER BY 1")]


def produk_hapus(ids):
    ids = [int(i) for i in (ids or [])]
    if not ids:
        return 0
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            f"DELETE FROM produk WHERE id IN ({','.join('?' * len(ids))})", ids)
        conn.commit()
        return cur.rowcount


def produk_impor(baris):
    """
    Impor massal. Return (jumlah_masuk, [pesan galat per baris]).

    Baris yang gagal dilaporkan dengan NOMOR BARISNYA dan tidak menggagalkan
    baris lain — mengimpor 300 produk lalu ditolak seluruhnya karena satu sel
    kosong adalah pengalaman yang buruk.
    """
    masuk, galat = 0, []
    with _lock:
        conn = get_conn()
        for i, d in enumerate(baris or [], 1):
            try:
                bersih = _bersih_produk(d, baris_ke=i)
            except ValueError as e:
                galat.append(str(e))
                continue
            lama = None
            if bersih["kode"]:
                lama = conn.execute("SELECT id FROM produk WHERE kode = ?",
                                    (bersih["kode"],)).fetchone()
            if lama:
                conn.execute(
                    f"UPDATE produk SET {', '.join(f'{k} = ?' for k in KOLOM_PRODUK)}, "
                    "diubah = ? WHERE id = ?",
                    [bersih[k] for k in KOLOM_PRODUK] + [_now(), lama["id"]])
            else:
                conn.execute(
                    f"INSERT INTO produk ({', '.join(KOLOM_PRODUK)}, dibuat, diubah) "
                    f"VALUES ({', '.join('?' * len(KOLOM_PRODUK))}, ?, ?)",
                    [bersih[k] for k in KOLOM_PRODUK] + [_now(), _now()])
            masuk += 1
        conn.commit()
    return masuk, galat


# ─── Paket ────────────────────────────────────────────────────────────────────

def paket_daftar():
    with _lock:
        hasil = []
        for r in get_conn().execute("SELECT * FROM paket ORDER BY kategori, nama"):
            d = dict(r)
            d["items"] = _json(d.get("items"), [])
            hasil.append(d)
        return hasil


def paket_simpan(d):
    nama = str(d.get("nama") or "").strip()
    if not nama:
        raise ValueError("Nama paket belum diisi")
    items = json.dumps(d.get("items") or [], ensure_ascii=False)
    with _lock:
        conn = get_conn()
        if d.get("id"):
            conn.execute("UPDATE paket SET nama = ?, kategori = ?, items = ?, "
                         "catatan = ?, diubah = ? WHERE id = ?",
                         (nama, str(d.get("kategori") or ""), items,
                          str(d.get("catatan") or ""), _now(), int(d["id"])))
        else:
            conn.execute("INSERT INTO paket (nama, kategori, items, catatan, "
                         "dibuat, diubah) VALUES (?, ?, ?, ?, ?, ?)",
                         (nama, str(d.get("kategori") or ""), items,
                          str(d.get("catatan") or ""), _now(), _now()))
        conn.commit()
    return paket_daftar()


def paket_hapus(pid):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM paket WHERE id = ?", (int(pid),))
        conn.commit()


# ─── Buku pelanggan ───────────────────────────────────────────────────────────

KOLOM_PELANGGAN = ("nama", "alamat", "kota", "pic", "jabatan_pic", "telepon",
                   "email", "npwp", "place_key", "ruang", "catatan")


def _slug(nama):
    return " ".join(str(nama or "").lower().split())


def pelanggan_simpan(d):
    """Simpan/perbarui satu pelanggan berdasarkan namanya. Return id."""
    nama = str(d.get("nama") or "").strip()
    if not nama:
        return None
    nilai = {k: str(d.get(k) or "").strip() for k in KOLOM_PELANGGAN}
    nilai["nama"] = nama
    with _lock:
        conn = get_conn()
        lama = conn.execute("SELECT id FROM pelanggan WHERE slug = ?",
                            (_slug(nama),)).fetchone()
        if lama:
            # Nilai kosong tidak menimpa data lama yang sudah terisi.
            isi = {k: v for k, v in nilai.items() if v}
            if isi:
                conn.execute(
                    f"UPDATE pelanggan SET {', '.join(f'{k} = ?' for k in isi)}, "
                    "diubah = ? WHERE id = ?",
                    list(isi.values()) + [_now(), lama["id"]])
            pid = lama["id"]
        else:
            cur = conn.execute(
                f"INSERT INTO pelanggan (slug, {', '.join(KOLOM_PELANGGAN)}, "
                f"dibuat, diubah) VALUES (?, {', '.join('?' * len(KOLOM_PELANGGAN))}, ?, ?)",
                [_slug(nama)] + [nilai[k] for k in KOLOM_PELANGGAN] + [_now(), _now()])
            pid = cur.lastrowid
        conn.commit()
    return pid


def pelanggan_cari(q, limit=10):
    if not str(q or "").strip():
        return []
    with _lock:
        return [dict(r) for r in get_conn().execute(
            "SELECT * FROM pelanggan WHERE nama LIKE ? ORDER BY diubah DESC LIMIT ?",
            (f"%{q}%", int(limit)))]


# ─── Nomor & dokumen ──────────────────────────────────────────────────────────

def periode(jenis, tgl):
    """'2026-10' untuk surat (lingkup bulan), '2026' untuk invoice dkk."""
    return tgl.strftime("%Y-%m" if buat.JENIS[jenis]["lingkup"] == "bulan" else "%Y")


def _ambil_nomor(conn, jenis, tgl, pola):
    """Nomor urut berikutnya untuk (jenis, periode). Dipanggil DI DALAM transaksi."""
    per = periode(jenis, tgl)
    baris = conn.execute("SELECT MAX(urut) FROM nomor WHERE jenis = ? AND periode = ?",
                         (jenis, per)).fetchone()
    urut = int(baris[0] or 0) + 1
    nomor = inti.bentuk_nomor(pola, urut, tgl)
    return nomor, urut, per


KOLOM_DOK_UANG = ("subtotal", "diskon_persen", "diskon_global", "biaya_kirim",
                  "dpp", "dpp_nilai_lain", "ppn_tarif", "ppn", "total",
                  "uang_muka", "sisa")


def mulai_dokumen(jenis, d, total, atur, induk_id=None):
    """
    Ambil nomor DAN sisipkan baris dokumen dalam SATU transaksi.

    Berkas baru dibuat setelah ini; kalau pembuatannya gagal, pemanggil
    memanggil `batalkan_dokumen` sehingga nomornya tidak hangus. Urutan ini
    dipilih supaya satu nomor tidak pernah dipakai dua dokumen.

    Pengambilan nomor diserialkan oleh `_lock` (satu koneksi per proses), dan
    `UNIQUE(jenis, periode, urut)` menjadi jaring terakhir bila proses lain
    menulis ke database yang sama: tabrakan GAGAL KERAS, bukan diam-diam
    memberi nomor ganda. BEGIN eksplisit sengaja tidak dipakai — sqlite3 Python
    sudah membuka transaksi sendiri untuk DML, dan menumpuknya menimbulkan
    "cannot start a transaction within a transaction".
    """
    tgl = d["tanggal"]
    with _lock:
        conn = get_conn()
        try:
            if induk_id:
                induk = conn.execute("SELECT * FROM dokumen WHERE id = ?",
                                     (int(induk_id),)).fetchone()
                if not induk:
                    raise ValueError("Dokumen yang direvisi tidak ditemukan")
                rev = int(conn.execute(
                    "SELECT COUNT(*) FROM dokumen WHERE induk_id = ?",
                    (int(induk_id),)).fetchone()[0]) + 1
                dasar = str(induk["nomor"]).split("-R")[0]
                nomor, urut, per = f"{dasar}-R{rev}", induk["urut"], induk["periode"]
            else:
                rev = 0
                pola = atur.get(f"pola_nomor_{jenis}") or buat.JENIS[jenis]["pola"]
                nomor, urut, per = _ambil_nomor(conn, jenis, tgl, pola)

            pelanggan_id = None
            cur = conn.execute(
                "INSERT INTO dokumen (jenis, nomor, periode, urut, revisi, induk_id,"
                " status, tanggal, pelanggan_id, pelanggan_nama, pelanggan_kota,"
                " place_key, ruang, perihal, hal, data, items, syarat, ppn_mode,"
                f" {', '.join(KOLOM_DOK_UANG)}, dibuat, diubah)"
                f" VALUES ({', '.join('?' * (19 + len(KOLOM_DOK_UANG) + 2))})",
                [jenis, nomor, per, urut, rev, induk_id, "draft",
                 tgl.isoformat(), pelanggan_id,
                 d.get("pelanggan_nama"), d.get("pelanggan_kota") or "",
                 d.get("place_key") or "", d.get("ruang") or "",
                 d.get("perihal") or "", buat.hal_dokumen(jenis, d),
                 json.dumps(d, ensure_ascii=False, default=str),
                 json.dumps(total["baris"], ensure_ascii=False, default=str),
                 json.dumps(buat.syarat_otomatis(d, atur, jenis), ensure_ascii=False),
                 total["ppn_mode"]]
                + [int(total[k]) for k in KOLOM_DOK_UANG] + [_now(), _now()])
            if rev == 0:
                conn.execute("INSERT INTO nomor (jenis, periode, urut, nomor, "
                             "dokumen_id, dibuat) VALUES (?, ?, ?, ?, ?, ?)",
                             (jenis, per, urut, nomor, cur.lastrowid, _now()))
            conn.commit()
            return cur.lastrowid, nomor
        except Exception:
            conn.rollback()
            raise


def selesaikan_dokumen(dok_id, docx_rel, pdf_rel=""):
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE dokumen SET status = 'jadi', docx = ?, pdf = ?, "
                     "diubah = ? WHERE id = ?",
                     (docx_rel, pdf_rel or "", _now(), int(dok_id)))
        conn.commit()
    return dokumen_get(dok_id)


def batalkan_dokumen(dok_id):
    """Buang baris draft + nomornya setelah pembuatan berkas gagal."""
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM nomor WHERE dokumen_id = ?", (int(dok_id),))
        conn.execute("DELETE FROM dokumen WHERE id = ? AND status = 'draft'",
                     (int(dok_id),))
        conn.commit()


def set_pelanggan(dok_id, pelanggan_id):
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE dokumen SET pelanggan_id = ? WHERE id = ?",
                     (pelanggan_id, int(dok_id)))
        conn.commit()


def _baca_dokumen(r):
    d = dict(r)
    d["data"] = _json(d.get("data"), {})
    d["items"] = _json(d.get("items"), [])
    d["syarat"] = _json(d.get("syarat"), [])
    d["jenis_label"] = buat.JENIS.get(d["jenis"], {}).get("label", d["jenis"])
    return d


def dokumen_get(dok_id):
    with _lock:
        r = get_conn().execute("SELECT * FROM dokumen WHERE id = ?",
                               (int(dok_id),)).fetchone()
    return _baca_dokumen(r) if r else None


def dokumen_daftar(jenis="", q="", limit=200, offset=0):
    sql = "SELECT * FROM dokumen WHERE status = 'jadi'"
    arg = []
    if jenis:
        sql += " AND jenis = ?"
        arg.append(jenis)
    if q:
        sql += " AND (pelanggan_nama LIKE ? OR nomor LIKE ? OR perihal LIKE ?)"
        arg += [f"%{q}%"] * 3
    sql += " ORDER BY tanggal DESC, id DESC LIMIT ? OFFSET ?"
    arg += [int(limit), int(offset)]
    with _lock:
        return [_baca_dokumen(r) for r in get_conn().execute(sql, arg)]


def dokumen_hitung(jenis="", q=""):
    sql = "SELECT COUNT(*) FROM dokumen WHERE status = 'jadi'"
    arg = []
    if jenis:
        sql += " AND jenis = ?"
        arg.append(jenis)
    if q:
        sql += " AND (pelanggan_nama LIKE ? OR nomor LIKE ? OR perihal LIKE ?)"
        arg += [f"%{q}%"] * 3
    with _lock:
        return int(get_conn().execute(sql, arg).fetchone()[0])


def dokumen_hapus(dok_id):
    """Hapus catatan dokumen. Nomornya TIDAK dilepas — nomor yang sudah
    terbit tidak boleh dipakai ulang oleh dokumen lain."""
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE nomor SET dokumen_id = NULL WHERE dokumen_id = ?",
                     (int(dok_id),))
        conn.execute("DELETE FROM dokumen WHERE id = ?", (int(dok_id),))
        conn.commit()


def stats():
    with _lock:
        conn = get_conn()
        return {
            "dokumen": int(conn.execute(
                "SELECT COUNT(*) FROM dokumen WHERE status = 'jadi'").fetchone()[0]),
            "produk": int(conn.execute("SELECT COUNT(*) FROM produk").fetchone()[0]),
            "pelanggan": int(conn.execute(
                "SELECT COUNT(*) FROM pelanggan").fetchone()[0]),
        }
