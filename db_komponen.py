"""
db_komponen.py — Akses data khusus ruang "komponen" (data/komponen.db).

Fungsi generik (upsert, anti-duplikat, riwayat run) tetap di db.py dan bekerja
di ruang mana pun. Modul ini hanya berisi yang memang milik prospek komponen
komputer: CRM dengan status & jadwal follow-up otomatis, daftar bisnis yang
sengaja dilewati saat scraping, pengaturan proposal, dan nomor surat harian.

Setiap fungsi publik memaksa ruang "komponen", jadi aman dipanggil dari route
Flask maupun thread scraper tanpa perlu membungkusnya lagi.
"""
import functools
from datetime import datetime

import db
from scrapers.komponen import (KATEGORI_DIKECUALIKAN, STATUS_BELUM, STATUS_SELESAI,
                               STATUS_TANPA_EXPORT)

RUANG = "komponen"

HARI_LEWATI = db.HARI_LEWATI

PENGATURAN_BAWAAN = {
    "jabatan": "Sales Representative",
    "sla": "1x24 jam kerja",
    # {urut} nomor urut harian, {hari} {bulan} {tahun} tanggal, {romawi} bulan romawi.
    "pola_nomor": "{urut:03d}/ISB/SALES/{hari:02d}/{romawi}/{tahun}",
    "kota_surat": "Jakarta",
    "fu1_hari": "3",   # follow-up pertama, dihitung dari tanggal kirim
    "fu2_hari": "7",   # follow-up terakhir, dihitung dari tanggal kirim
    # Kategori Google Maps yang dilewati saat scraping (dipisah koma).
    "kategori_dikecualikan": KATEGORI_DIKECUALIKAN,
}


def _di_ruang(fn):
    @functools.wraps(fn)
    def bungkus(*a, **kw):
        with db.ruang(RUANG):
            return fn(*a, **kw)
    return bungkus


def _hari_ini():
    return datetime.now().strftime("%Y-%m-%d")


# ─── Daftar lewati & hapus (implementasi generik di db.py) ──────────────────

lewati_catat = _di_ruang(db.lewati_catat)
lewati_aktif = _di_ruang(db.lewati_aktif)
lewati_hapus = _di_ruang(db.lewati_hapus)
buang_lead = _di_ruang(db.buang_lead)
set_lolos = _di_ruang(db.set_lolos)
hapus_leads = _di_ruang(db.hapus_leads)
set_pin = _di_ruang(db.set_pin)
bersihkan_tertunda = _di_ruang(db.bersihkan_tertunda)


@_di_ruang
def hapus_sesuai_filter(f, jangan_ambil_lagi=False, kosongkan_lewati=False):
    """
    Hapus semua lead yang cocok dengan filter halaman Leads. Tanpa filter =
    SEMUA baris, termasuk yang tersembunyi (tertunda / tutup) — kalau tertinggal,
    anti-duplikat terus melewatinya. `kosongkan_lewati` = reset total daftar
    lewati, dijalankan lebih dulu supaya catatan "jangan ambil lagi" dari
    penghapusan ini tidak ikut terbuang.
    Return (jumlah_lead_terhapus, jumlah_lewati_terhapus).
    """
    f = dict(f or {})
    tanpa_filter = not any(v for k, v in f.items() if k not in ("urut", "untuk_export"))
    if tanpa_filter:
        keys = db.semua_keys()
    else:
        keys = [r["place_key"] for r in query(f, limit=0)[0]]
    lewati = db.kosongkan_lewati() if kosongkan_lewati else 0
    return db.hapus_leads(keys, jangan_ambil_lagi), lewati


# ─── Query CRM ────────────────────────────────────────────────────────────────

# Hanya lead yang lolos syarat saat scraping dan tidak tutup.
_AKTIF = db.SQL_AKTIF

URUTAN = {
    "skor": "COALESCE(skor_pembeli, 0) DESC, nama_bisnis",
    "terbaru": "first_seen_at DESC",
    "nama": "nama_bisnis COLLATE NOCASE ASC",
    "ulasan": "COALESCE(jumlah_ulasan, 0) DESC",
    "follow_up": "tanggal_follow_up IS NULL, tanggal_follow_up ASC",
    "dihubungi": "COALESCE(tanggal_dihubungi, '') DESC, COALESCE(skor_pembeli, 0) DESC",
}


def _where(f, pakai_tab=True):
    where, args = [_AKTIF], []
    if f.get("keys"):
        keys = list(f["keys"])
        where.append(f"place_key IN ({','.join('?' * len(keys))})")
        args += keys
    if f.get("run"):
        where.append("place_key IN (SELECT place_key FROM run_leads WHERE run_id = ?)")
        args.append(f["run"])
    for kolom in ("segmen", "tier", "area_pencarian", "kota"):
        if f.get(kolom):
            where.append(f"{kolom} = ?")
            args.append(f[kolom])
    if f.get("status"):
        where.append(f"COALESCE(status_leads, '{STATUS_BELUM}') = ?")
        args.append(f["status"])
    if f.get("punya_wa"):
        where.append("COALESCE(whatsapp_link, '') != ''")
    if f.get("punya_email"):
        where.append("COALESCE(email, '') != ''")
    kondisi = db.kondisi_kontak(f.get("kontak"))
    if kondisi:
        where.append(kondisi)
    if f.get("website") == "ada":
        where.append("COALESCE(website_utama, '') != ''")
    elif f.get("website") == "tidak":
        where.append("COALESCE(website_utama, '') = ''")
    if f.get("skor_min") is not None:
        where.append("COALESCE(skor_pembeli, 0) >= ?")
        args.append(int(f["skor_min"]))
    if f.get("kanal"):
        where.append("COALESCE(kanal_kontak, '') LIKE ?")
        args.append(f"%{f['kanal']}%")
    if pakai_tab and db._sql_tab(f.get("tab")):
        where.append(db._sql_tab(f["tab"]))
    if f.get("follow_up"):
        where.append("tanggal_follow_up IS NOT NULL AND tanggal_follow_up <= ?")
        args.append(_hari_ini())
        where.append(f"COALESCE(status_leads, '{STATUS_BELUM}') NOT IN "
                     f"({','.join('?' * len(STATUS_SELESAI))})")
        args += list(STATUS_SELESAI)
    if f.get("untuk_export"):
        where.append(f"COALESCE(status_leads, '{STATUS_BELUM}') NOT IN "
                     f"({','.join('?' * len(STATUS_TANPA_EXPORT))})")
        args += list(STATUS_TANPA_EXPORT)
    if f.get("q"):
        pola = f"%{f['q']}%"
        where.append("(nama_bisnis LIKE ? OR alamat LIKE ? OR kategori LIKE ? "
                     "OR email LIKE ? OR email_lain LIKE ? OR telepon LIKE ?)")
        args += [pola] * 6
    return " WHERE " + " AND ".join(where), args


@_di_ruang
def query(f=None, urut="skor", limit=50, offset=0):
    """Lead aktif sesuai filter. Return (rows, total)."""
    f = f or {}
    klausa, args = _where(f)
    order = db.urutan_dengan_pin(URUTAN.get(urut, URUTAN["skor"]))
    with db._lock:
        conn = db.get_conn()
        total = conn.execute(f"SELECT COUNT(*) AS n FROM businesses{klausa}",
                             args).fetchone()["n"]
        sql = f"SELECT * FROM businesses{klausa} ORDER BY {order}"
        if limit:
            sql += " LIMIT ? OFFSET ?"
            rows = conn.execute(sql, args + [int(limit), int(offset)]).fetchall()
        else:
            rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows], total


@_di_ruang
def hitung_tab(f=None):
    """Angka badge nav tab untuk filter aktif (tab sendiri diabaikan)."""
    return db.hitung_tab(*_where(f or {}, pakai_tab=False))


@_di_ruang
def get(place_key):
    return db.get(place_key)


@_di_ruang
def nilai_unik(kolom):
    if kolom not in {"segmen", "tier", "area_pencarian", "kota"}:
        return []
    with db._lock:
        cur = db.get_conn().execute(
            f"SELECT DISTINCT {kolom} AS v FROM businesses WHERE {_AKTIF} "
            f"AND {kolom} IS NOT NULL AND {kolom} != '' ORDER BY v")
        return [r["v"] for r in cur.fetchall()]


@_di_ruang
def stats():
    with db._lock:
        conn = db.get_conn()

        def satu(sql, args=()):
            return conn.execute(sql, args).fetchone()["n"] or 0

        dasar = f"SELECT COUNT(*) AS n FROM businesses WHERE {_AKTIF}"
        return {
            "total": satu(dasar),
            "punya_wa": satu(dasar + " AND COALESCE(whatsapp_link, '') != ''"),
            "punya_email": satu(dasar + " AND COALESCE(email, '') != ''"),
            "belum_dihubungi": satu(
                dasar + f" AND COALESCE(status_leads, '{STATUS_BELUM}') = ?",
                (STATUS_BELUM,)),
            "follow_up_hari_ini": satu(
                dasar + " AND tanggal_follow_up IS NOT NULL AND tanggal_follow_up <= ?"
                f" AND COALESCE(status_leads, '{STATUS_BELUM}') NOT IN "
                f"({','.join('?' * len(STATUS_SELESAI))})",
                (_hari_ini(), *STATUS_SELESAI)),
            "proposal_hari_ini": satu(
                "SELECT COUNT(*) AS n FROM nomor_surat WHERE tanggal = ?", (_hari_ini(),)),
            "dilewati": satu("SELECT COUNT(*) AS n FROM dilewati"),
        }


# ─── Update CRM & pengaturan (implementasi generik di db.py) ─────────────────

@_di_ruang
def update_crm(place_key, status=None, catatan=None, tanggal_follow_up=None,
               nama_pic=None):
    """db.update_crm dengan jadwal follow-up dari pengaturan komponen."""
    atur = pengaturan()
    return db.update_crm(place_key, status=status, catatan=catatan,
                         tanggal_follow_up=tanggal_follow_up, nama_pic=nama_pic,
                         fu1_hari=atur["fu1_hari"], fu2_hari=atur["fu2_hari"])


@_di_ruang
def pengaturan():
    return db.pengaturan(PENGATURAN_BAWAAN)


@_di_ruang
def simpan_pengaturan(nilai):
    return db.simpan_pengaturan(PENGATURAN_BAWAAN, nilai)


# ─── Nomor surat harian ───────────────────────────────────────────────────────

@_di_ruang
def ambil_nomor_surat(place_key, tanggal, bentuk, baru=False):
    """
    Nomor surat untuk satu lead pada `tanggal` (YYYY-MM-DD).

    Nomor urut reset tiap hari karena kuncinya tanggal. Lead yang sama pada hari
    yang sama mendapat nomor yang sama lagi (membuat ulang proposal tidak
    menghabiskan nomor), kecuali `baru=True`.

    `bentuk(urut) -> str` menyusun teks nomor dari pola di pengaturan.
    Return (nomor, urut).
    """
    with db._lock:
        conn = db.get_conn()
        if not baru:
            r = conn.execute(
                "SELECT nomor, urut FROM nomor_surat WHERE place_key = ? AND tanggal = ? "
                "ORDER BY urut DESC LIMIT 1", (place_key, tanggal)).fetchone()
            if r:
                return r["nomor"], r["urut"]
        urut = (conn.execute("SELECT MAX(urut) AS m FROM nomor_surat WHERE tanggal = ?",
                             (tanggal,)).fetchone()["m"] or 0) + 1
        nomor = bentuk(urut)
        conn.execute(
            "INSERT INTO nomor_surat (tanggal, urut, place_key, nomor, dibuat) "
            "VALUES (?, ?, ?, ?, ?)", (tanggal, urut, place_key, nomor, db._now()))
        conn.commit()
    return nomor, urut


@_di_ruang
def catat_proposal(place_key, nomor, tanggal, berkas):
    """Simpan jejak proposal di lead; status naik jadi 'Proposal Dibuat' bila belum dihubungi."""
    with db._lock:
        conn = db.get_conn()
        conn.execute(
            "UPDATE businesses SET proposal_nomor = ?, proposal_tanggal = ?, "
            "proposal_file = ?, status_leads = CASE "
            f"WHEN COALESCE(status_leads, '{STATUS_BELUM}') = '{STATUS_BELUM}' "
            "THEN 'Proposal Dibuat' ELSE status_leads END WHERE place_key = ?",
            (nomor, tanggal, berkas, place_key))
        conn.commit()
