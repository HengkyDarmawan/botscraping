"""
scrapers/scoring.py — Ubah data mentah jadi keputusan penjualan.

Menjawab dua pertanyaan untuk setiap bisnis:

  1. "Saya sebaiknya menawarkan jasa apa ke bisnis ini?"
     → rekomendasi_jasa(): mencocokkan kondisi aset digital bisnis dengan
       katalog jasa di data/jasa.json, lengkap dengan satu kalimat pembuka
       yang siap dikirim.

     Dua hal sengaja dipisah di sini:

       DIAGNOSA — fakta terukur hasil scraping ("listing tanpa foto", "sosmed
       kosong", "website mati"). Tinggal di modul ini karena diturunkan dari
       sinyal scraping.

       KATALOG JASA — jasa mana yang menjawab diagnosa mana, kalimat pitch-nya,
       dan tim penanggung jawabnya. Tinggal di data/jasa.json supaya menambah
       jasa baru tidak perlu menyentuh kode.

     Jasa utama dipilih berdasarkan BOBOT, bukan urutan daftar. Ini penting:
       versi lama memeriksa aturan dari atas ke bawah dan memakai yang pertama
       cocok, sehingga satu aturan longgar di posisi atas ("belum punya
       website") selalu mengalahkan aturan spesifik di bawahnya dan jalur
       marketing praktis tidak pernah bisa menang.

  2. "Seberapa besar peluang bisnis ini benar-benar membeli?"
     → skor_pembeli(): skor 0-100 dari tiga hal yang harus ada bersamaan —
       BUTUH (ada masalah digital), MAMPU BAYAR (bisnisnya cukup besar), dan
       BISA DIHUBUNGI (ada jalur kontak). Punya masalah tapi tidak punya uang
       bukan pembeli; punya uang tapi tidak bisa dihubungi juga bukan pembeli.

Modul ini murni perhitungan — tidak membuka jaringan dan tidak menyentuh
database, jadi gampang diuji sendiri dan aturannya gampang diubah.
"""
import json
import math
import re
from datetime import datetime
from pathlib import Path

from scrapers.enrich import PLATFORM_GRATISAN, url_pinjaman, url_sosmed, website_efektif

try:  # opsional — tanpa library ini nomor hanya dicek dari awalannya
    import phonenumbers
    from phonenumbers import PhoneNumberType as _JenisNomor
except ImportError:
    phonenumbers = None

KATALOG_FILE = Path("data") / "jasa.json"

# ─── Kategori bisnis → daya beli ──────────────────────────────────────────────
# Proksi kasar untuk nilai transaksi rata-rata. Bisnis di daftar "tinggi"
# terbiasa mengeluarkan jutaan rupiah untuk satu klien baru, jadi biaya website
# atau iklan terasa wajar. Daftar "rendah" margin per transaksinya tipis.
KATEGORI_TINGGI = (
    "klinik", "dokter", "gigi", "rumah sakit", "apotek", "laboratorium",
    "notaris", "pengacara", "advokat", "konsultan", "akuntan", "kontraktor",
    "arsitek", "interior", "properti", "real estate", "developer", "dealer",
    "showroom", "mobil", "hotel", "villa", "resort", "sekolah", "kampus",
    "universitas", "kursus", "bimbingan belajar", "catering", "wedding",
    "pernikahan", "event organizer", "percetakan", "advertising", "travel",
    "tour", "logistik", "ekspedisi", "asuransi", "leasing", "pabrik",
    "manufaktur", "distributor", "supplier", "bengkel resmi", "salon",
    "spa", "gym", "fitness", "veteriner", "hewan",
    # Penyewaan tempat/slot & bisnis beriuran rutin. Pendapatannya berulang tiap
    # bulan, jadi daya belinya setara kategori di atas. Yang dicocokkan adalah
    # teks KATEGORI Google Maps (sudah di-lowercase di sinyal()), bukan kata
    # kunci pencarian — mis. "Lapangan futsal", "Kompleks olahraga",
    # "Agen persewaan mobil" (mengandung "sewa"), "Layanan binatu".
    # "lapangan"/"olahraga" polos sengaja TIDAK di sini: lapangan voli/basket
    # umum dan "Klub Olahraga" bukan bisnis sewa (lihat KATEGORI_LEMAH_WEB).
    "lapangan futsal", "lapangan bulu tangkis", "lapangan tenis", "lapangan padel",
    "mini soccer", "sewa lapangan", "kompleks olahraga", "pusat olahraga",
    "futsal", "badminton", "bulu tangkis", "padel", "tenis",
    "gelanggang", "sport", "renang", "golf", "biliar", "bilyar",
    "kebugaran", "sasana", "bela diri", "senam", "coworking",
    "ruang kerja bersama",
    "sewa", "kost", "rumah kos", "kos-kosan", "laundry", "binatu",
    "koperasi", "penukaran mata uang", "money changer", "peternakan",
    "sablon", "fabrikasi",
)
KATEGORI_RENDAH = (
    "warung", "warteg", "kaki lima", "angkringan", "warnet", "konter",
    "pulsa", "gerobak", "kios", "pedagang", "jajanan", "burjo",
)
# Sektor yang jarang membeli website sendiri: keputusan ada di kantor pusat
# (gadai, cabang perusahaan), organisasi nirlaba, atau usaha yang tidak menjual
# ke publik. Tetap jadi lead, tapi skornya diturunkan supaya tidak menyalip
# sektor jasa di antrean verifikasi.
KATEGORI_LEMAH_WEB = (
    "kantor perusahaan", "gadai", "pegadaian", "gudang", "klub olahraga",
    # "Layanan Transportasi" sengaja tidak di sini: isinya travel & rental
    # (shuttle, sewa motor) — justru pembeli website yang baik.
    "produsen", "asosiasi", "organisasi",
    "yayasan", "perkumpulan", "lapangan voli", "lapangan basket",
    "lapangan sepak bola", "lapangan atletik",
)
# Kategori yang penjualannya cocok dipindah ke online.
KATEGORI_RETAIL = (
    "toko", "butik", "fashion", "baju", "sepatu", "tas", "kosmetik",
    "elektronik", "furniture", "mebel", "oleh-oleh", "grosir", "restoran",
    "rumah makan", "kafe", "cafe", "bakery", "kue", "roti", "katering",
    "catering", "frozen", "minuman",
)

# Kategori yang keputusan belinya sangat ditentukan tampilan: klien memutuskan
# dari apa yang mereka LIHAT, bukan dari spesifikasi tertulis. Ini pemicu jasa
# 3D design, fotografi produk, dan materi visual.
KATEGORI_VISUAL = (
    "properti", "real estate", "developer", "perumahan", "interior",
    "arsitek", "kontraktor", "furniture", "mebel", "kitchen set",
    "wedding", "pernikahan", "event organizer", "catering", "katering",
    "restoran", "rumah makan", "kafe", "cafe", "bakery", "kue", "roti",
    "salon", "spa", "butik", "fashion", "fotografi", "percetakan",
)

TIER_PANAS = "PANAS"
TIER_HANGAT = "HANGAT"
TIER_DINGIN = "DINGIN"
TIER_ARSIP = "ARSIP"

# Urutan dipakai untuk mengurutkan dropdown & warna di Excel/UI.
URUTAN_TIER = [TIER_PANAS, TIER_HANGAT, TIER_DINGIN, TIER_ARSIP]

JASA_RISET_MANUAL = "Perlu Riset Manual"


# ─── Nomor telepon Indonesia ──────────────────────────────────────────────────
# Ditaruh di sini (bukan di gmaps.py) supaya scoring tidak perlu mengimpor
# scraper — gmaps.py yang mengimpor modul ini, tidak sebaliknya.

def normalisasi_nomor(phone_raw):
    """08xx / +628xx / 628xx → 628xx. String kosong bila tidak ada digit."""
    digits = re.sub(r"\D", "", str(phone_raw or ""))
    if not digits:
        return ""
    if digits.startswith("0"):
        return "62" + digits[1:]
    if digits.startswith("8"):
        return "62" + digits
    if not digits.startswith("62"):
        return "62" + digits
    return digits


def nomor_wa_valid(phone_raw):
    """
    True bila nomor terlihat seperti HP seluler Indonesia (kandidat WhatsApp).

    Telepon kabel (021, 022, ...) jadi 6221... dan ditolak. Ini heuristik prefix
    saja — tidak memastikan nomornya benar-benar aktif di WhatsApp.
    """
    d = normalisasi_nomor(phone_raw)
    if not (d.startswith("628") and 10 <= len(d) <= 14):
        return False
    # Awalan 08 belum tentu seluler: 0800 (bebas pulsa), 0804 (premium), 0807
    # (UAN) ikut lolos cek awalan dan dulu dijadikan tautan wa.me. phonenumbers
    # memakai tabel penomoran resmi Indonesia untuk memastikannya.
    if phonenumbers is not None:
        try:
            nomor = phonenumbers.parse("+" + d)
            return phonenumbers.is_valid_number(nomor) and phonenumbers.number_type(nomor) in (
                _JenisNomor.MOBILE, _JenisNomor.FIXED_LINE_OR_MOBILE)
        except Exception:
            return False
    return True


def link_wa(phone_raw, pesan=""):
    """Tautan wa.me untuk nomor HP seluler; nomor kabel/kosong → string kosong."""
    if not nomor_wa_valid(phone_raw):
        return ""
    tautan = f"https://wa.me/{normalisasi_nomor(phone_raw)}"
    if pesan:
        from urllib.parse import quote
        tautan += "?text=" + quote(pesan)
    return tautan


def bisnis_tutup(status_buka):
    """
    True bila status Google Maps menyatakan bisnis tutup — permanen ATAU
    sementara. Dulu hanya "permanen"/"closed" yang dicek, sehingga "Tutup
    Sementara" dibuang dari Excel oleh filter tetapi tetap diberi skor tinggi.
    """
    st = str(status_buka or "").lower()
    return any(k in st for k in ("permanen", "sementara", "closed"))


# ─── Normalisasi sinyal ───────────────────────────────────────────────────────

def _angka(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _bulat(v, default=0):
    try:
        if v is None or v == "":
            return default
        return int(re.sub(r"\D", "", str(v)) or default)
    except (TypeError, ValueError):
        return default


def _tri(v):
    """
    Ubah nilai jadi tiga keadaan: True, False, atau None (tidak diketahui).

    Wajib dipakai untuk semua field boolean, karena SQLite menyimpannya sebagai
    angka 0/1. Saat lead dibaca kembali dari database, `nilai is True` akan
    bernilai False untuk angka 1 — dan seluruh aturan rekomendasi jasa gagal
    tanpa pesan error apa pun.
    """
    if v is None or v == "":
        return None
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("0", "false", "no", "tidak"):
            return False
        if s in ("1", "true", "yes", "ya"):
            return True
        return None
    return bool(v)


def sinyal(row):
    """
    Ringkas satu baris lead jadi sinyal yang dipakai aturan & skor.

    Nilai None berarti "tidak diketahui" (mis. bisnis tanpa website tidak punya
    informasi HTTPS) dan sengaja dibedakan dari False agar tidak ikut menambah
    poin "butuh".
    """
    kategori = str(row.get("kategori") or "").lower()
    tahun_ini = datetime.now().year
    tahun_web = row.get("web_tahun_update")
    url = str(row.get("website") or "").strip()
    # Listing OTA / link booking Google / pencari lokasi jaringan bukan website
    # bisnisnya: dinilai sama dengan belum punya website (bukan "website mati").
    if url_pinjaman(url):
        url = ""
    platform = str(row.get("web_platform") or "")

    hasil = {
        "nama": str(row.get("nama_bisnis") or "bisnis Anda").strip(),
        "kategori": kategori,
        "rating": _angka(row.get("rating")),
        "ulasan": _bulat(row.get("jumlah_ulasan")),

        "ada_url": bool(url) and url not in ("-", "n/a", "none"),
        "punya_web": website_efektif(row),
        # Website ada tapi belum pernah diperiksa (enrichment dimatikan) —
        # dibedakan dari website yang sudah diperiksa dan hasilnya sehat.
        "web_status_kosong": row.get("web_status") in (None, ""),
        "web_mati": row.get("web_status") in ("mati", "error"),
        "platform": platform,
        "gratisan": platform in PLATFORM_GRATISAN,
        # Diperiksa dari URL-nya juga, bukan hanya dari platform hasil enrichment:
        # kalau enrichment dimatikan, `platform` kosong sementara URL-nya jelas
        # mengarah ke Instagram/marketplace — dan lead itu justru yang paling
        # pantas ditawari landing page.
        "sosmed_saja": platform in ("sosmed_saja", "marketplace") or url_sosmed(url),

        "https": _tri(row.get("web_https")),
        "mobile": _tri(row.get("web_mobile")),
        "load_ms": row.get("web_load_ms"),
        "tahun_web": tahun_web,
        "basi": bool(tahun_web) and int(tahun_web) <= tahun_ini - 3,
        "pixel": _tri(row.get("web_ada_pixel")),
        "toko_online": _tri(row.get("web_ada_toko")),

        "telepon": str(row.get("telepon") or "").strip(),
        # WA dari website (kontak.rapikan_kontak) juga dihitung, bukan hanya
        # telepon Google Maps yang kebetulan nomor seluler.
        "wa": nomor_wa_valid(row.get("telepon"))
              or bool(str(row.get("whatsapp_link") or "").strip()),
        "email": str(row.get("email") or "").strip(),
        "ig": str(row.get("instagram") or "").strip(),
        "fb": str(row.get("facebook") or "").strip(),
        # Dulu kolom ini dipanen enrich.py dan disimpan ke database, tapi tidak
        # pernah dibaca di sini — jadi tidak ada satu pun aturan yang memakainya.
        "tiktok": str(row.get("tiktok") or "").strip(),

        "diklaim": _tri(row.get("sudah_diklaim")),
        "foto": row.get("jumlah_foto"),
        "harga": str(row.get("rentang_harga") or ""),
        "status_buka": str(row.get("status_buka") or ""),
    }

    # ── Sosmed: tiga keadaan, bukan dua ──
    # Akun sosmed hanya bisa dipanen DARI halaman website (enrich._ambil_sosmed).
    # Jadi kolom instagram/facebook/tiktok yang kosong itu ambigu: bisa berarti
    # "memang tidak punya", bisa juga "tidak pernah ada kesempatan memeriksa".
    # Membedakan keduanya wajib — kalau tidak, setiap bisnis tanpa website akan
    # ditawari jasa pembuatan akun sosmed, termasuk yang Instagramnya sudah
    # jalan bertahun-tahun.
    hasil["sosmed_ada"] = bool(hasil["ig"] or hasil["fb"] or hasil["tiktok"]
                               or hasil["sosmed_saja"])
    # Halaman benar-benar terbuka dan terbaca ("aktif") — barulah tidak adanya
    # tautan sosmed di sana jadi bukti. Status "mati"/"error"/"tidak_ada"
    # berarti tidak ada halaman untuk diperiksa sama sekali.
    hasil["sosmed_dicek"] = (row.get("web_status") == "aktif"
                             or hasil["sosmed_saja"])
    return hasil


def _cocok(teks, daftar):
    return any(k in teks for k in daftar)


# ─── Diagnosa ───────────────────────────────────────────────────────────────────
# Fakta terukur tentang satu bisnis, diturunkan dari sinyal hasil scraping.
# Tiap entri di data/jasa.json menyebut nama-nama diagnosa di sini sebagai
# syaratnya. Menambah JASA baru cukup mengedit JSON; menambah DIAGNOSA baru
# (fakta yang belum pernah diukur) barulah perlu menyentuh berkas ini.

DIAGNOSA = {
    # ── Kondisi website ──
    "web_mati":          lambda s: s["ada_url"] and s["web_mati"],
    "belum_punya_web":   lambda s: not s["ada_url"],
    "punya_web":         lambda s: s["punya_web"],
    "sosmed_saja":       lambda s: s["sosmed_saja"],
    "web_gratisan":      lambda s: s["punya_web"] and s["gratisan"],
    "tanpa_https":       lambda s: s["punya_web"] and s["https"] is False,
    "tidak_mobile":      lambda s: s["punya_web"] and s["mobile"] is False,
    "web_lambat":        lambda s: s["punya_web"] and (s["load_ms"] or 0) > 5000,
    "web_basi":          lambda s: s["punya_web"] and s["basi"],
    "tanpa_toko_online": lambda s: s["punya_web"] and s["toko_online"] is False,
    "web_belum_dicek":   lambda s: s["ada_url"] and s["web_status_kosong"],

    # ── Kehadiran & reputasi di Google Maps ──
    "belum_diklaim": lambda s: s["diklaim"] is False,
    "rating_buruk":  lambda s: bool(s["rating"]) and s["rating"] < 4.0 and s["ulasan"] >= 30,
    "ulasan_minim":  lambda s: s["rating"] >= 4.0 and 0 < s["ulasan"] < 10,
    "bisnis_mapan":  lambda s: s["ulasan"] >= 20 and s["rating"] >= 4.0,

    # ── Jejak iklan ──
    "sudah_pixel": lambda s: s["punya_web"] and s["pixel"] is True,
    "tanpa_pixel": lambda s: s["punya_web"] and s["pixel"] is False,

    # ── Media sosial (tiga keadaan — lihat catatan di sinyal()) ──
    "sosmed_ada":         lambda s: s["sosmed_ada"],
    "sosmed_kosong":      lambda s: s["sosmed_dicek"] and not s["sosmed_ada"],
    "sosmed_belum_dicek": lambda s: not s["sosmed_dicek"] and not s["sosmed_ada"],

    # ── Aset visual & jenis usaha ──
    "aset_visual_lemah": lambda s: s["foto"] is not None and _bulat(s["foto"]) == 0,
    "kategori_retail":   lambda s: _cocok(s["kategori"], KATEGORI_RETAIL),
    "kategori_visual":   lambda s: _cocok(s["kategori"], KATEGORI_VISUAL),
}


# ─── Katalog jasa ────────────────────────────────────────────────────────────
# Sumber sebenarnya ada di data/jasa.json. Daftar di bawah hanya jaring pengaman
# supaya scraping tetap menghasilkan rekomendasi kalau berkas itu hilang atau
# salah ketik — bukan salinan lengkap, jadi jangan dipakai sebagai acuan.

KATALOG_BAWAAN = [
    {"nama": "Website Baru (URGENT)", "jalur": "web", "bobot": 95,
     "diagnosa": ["web_mati"],
     "pitch": "Website Anda saat ini tidak bisa diakses — calon pelanggan yang "
              "mencari nama Anda di Google berakhir di halaman error."},
    {"nama": "Website Company Profile", "jalur": "web", "bobot": 85,
     "diagnosa": ["belum_punya_web", "bisnis_mapan"],
     "pitch": "Bisnis Anda jelas ramai — {ulasan} ulasan dengan rating {rating} — "
              "tapi belum punya website."},
    {"nama": "Kelola Akun Sosmed", "jalur": "marketing", "bobot": 84,
     "diagnosa": ["sosmed_kosong", "bisnis_mapan"],
     "pitch": "Bisnis Anda jelas ramai ({ulasan} ulasan), tapi belum ada akun "
              "media sosial yang menangkap calon pelanggan di sana."},
    {"nama": "Branding & Identitas Visual", "jalur": "kreatif", "bobot": 75,
     "diagnosa": ["aset_visual_lemah", "bisnis_mapan"],
     "pitch": "Bisnis Anda sudah dipercaya {ulasan} pelanggan, tapi listing "
              "Google Maps Anda belum punya satu pun foto."},
    {"nama": "Optimasi Google Business Profile", "jalur": "marketing", "bobot": 74,
     "diagnosa": ["belum_diklaim"],
     "pitch": "Listing Google Maps Anda terlihat belum diklaim pemiliknya."},
    {"nama": "Website Company Profile", "jalur": "web", "bobot": 40,
     "diagnosa": ["belum_punya_web"],
     "pitch": "Bisnis Anda belum punya website — pelanggan hanya bisa menilai "
              "dari listing Google Maps."},
]

_katalog_cache = None


def _muat_katalog(muat_ulang=False):
    """
    Baca data/jasa.json sekali lalu simpan di memori.

    Berkas rusak / hilang TIDAK boleh menjatuhkan scraping yang sudah berjalan
    setengah jalan, jadi kegagalan apa pun jatuh ke KATALOG_BAWAAN.
    """
    global _katalog_cache
    if _katalog_cache is not None and not muat_ulang:
        return _katalog_cache

    entri = []
    try:
        data = json.loads(KATALOG_FILE.read_text(encoding="utf-8"))
        entri = [e for e in data.get("jasa", [])
                 if isinstance(e, dict) and e.get("nama") and e.get("diagnosa")]
    except Exception:
        entri = []

    if not entri:
        entri = [dict(e) for e in KATALOG_BAWAAN]

    # Bobot tertinggi lebih dulu. Sort Python stabil, jadi saat bobotnya seri
    # urutan di dalam berkas yang menentukan — satu-satunya peran urutan.
    entri.sort(key=lambda e: -_angka(e.get("bobot"), 0))
    _katalog_cache = entri
    return _katalog_cache


def _isi_pitch(teks, s):
    """Ganti placeholder {ulasan}, {rating}, dst dengan angka bisnis ini."""
    nilai = {
        "nama": s["nama"],
        "kategori": s["kategori"] or "bisnis seperti Anda",
        "rating": f"{s['rating']:.1f}",
        "ulasan": s["ulasan"],
        "platform": s["platform"] or "builder gratisan",
        "tahun_web": s["tahun_web"] or "-",
        "load_detik": f"{(s['load_ms'] or 0) / 1000:.1f}",
    }
    try:
        return str(teks).format(**nilai)
    except Exception:
        # Placeholder salah ketik di JSON — kirim apa adanya, jangan buang pitch.
        return str(teks)


def rekomendasi_jasa(row):
    """
    Tentukan jasa yang paling pas ditawarkan ke satu bisnis.

    Return dict: jasa_utama, jasa_pendukung (maks 2, dipisah " | "),
    alasan_pitch (satu kalimat siap kirim), jalur (nama tim, mis. "web").
    """
    s = sinyal(row)

    # Diperiksa di luar mekanisme bobot: bisnis yang sudah tutup tidak boleh
    # sempat diberi label jasa apa pun, setinggi apa pun bobotnya.
    if bisnis_tutup(s["status_buka"]):
        return {
            "jasa_utama": "Bisnis Tutup — Lewati",
            "jasa_pendukung": "",
            "alasan_pitch": "",
            "jalur": "",
        }

    cocok = []
    for entri in _muat_katalog():
        try:
            # Semua diagnosa harus terpenuhi (AND). Nama diagnosa yang tidak
            # dikenal melempar KeyError dan entri itu dilewati — persis seperti
            # yang dijanjikan catatan di data/jasa.json.
            if all(DIAGNOSA[d](s) for d in entri["diagnosa"]):
                cocok.append(entri)
        except Exception:
            continue  # aturan tidak berlaku untuk data setengah lengkap

    if not cocok:
        return {
            "jasa_utama": JASA_RISET_MANUAL,
            "jasa_pendukung": "",
            "alasan_pitch": "",
            "jalur": "",
        }

    utama = cocok[0]  # katalog sudah urut bobot menurun
    pendukung = []
    for e in cocok[1:]:
        if e["nama"] != utama["nama"] and e["nama"] not in pendukung:
            pendukung.append(e["nama"])
        if len(pendukung) >= 2:
            break

    return {
        "jasa_utama": utama["nama"],
        "jasa_pendukung": " | ".join(pendukung),
        "alasan_pitch": _isi_pitch(utama.get("pitch", ""), s),
        "jalur": utama.get("jalur", ""),
    }


# ─── Skor potensi pembeli ─────────────────────────────────────────────────────

def skor_pembeli(row, jumlah_cabang=1):
    """
    Skor 0-100 dari tiga dimensi yang harus terpenuhi bersamaan.

      BUTUH (maks 40)          — seberapa besar masalah digitalnya, dari dua
                                 sisi yang dibatasi terpisah: aset web (maks 28)
                                 dan kehadiran marketing (maks 22)
      MAMPU BAYAR (maks 35)    — seberapa besar bisnisnya
      BISA DIHUBUNGI (maks 25) — ada jalur kontak yang benar-benar bisa dipakai

    `jumlah_cabang` datang dari db.hitung_cabang(): jaringan dengan beberapa
    lokasi biasanya punya anggaran lebih besar.

    Return dict: skor_pembeli, tier, rincian (untuk ditampilkan/di-debug).
    """
    s = sinyal(row)
    rincian = {}

    # ── A. BUTUH ──
    # Dipecah jadi dua sub-ember yang dibatasi sendiri-sendiri, baru dijumlahkan.
    #
    # Versi lama menumpuk semuanya jadi satu ember maks 40 yang isinya didominasi
    # poin website ("belum punya website" saja sudah 28 dari 40). Akibatnya
    # bisnis dengan website bagus tapi nol media sosial — justru klien ideal jasa
    # iklan & konten — tidak pernah bisa mengumpulkan poin BUTUH yang cukup dan
    # selalu terlempar ke tier DINGIN/ARSIP.
    #
    # Dengan dua ember terpisah, bisnis yang lemah hanya di SATU sisi tetap bisa
    # mencapai tier atas, tanpa membuat yang lemah di KEDUA sisi jadi kelebihan
    # poin — jumlahnya tetap dibatasi 40.
    butuh_web = 0
    if not s["ada_url"] or s["sosmed_saja"]:
        butuh_web += 28
        rincian["belum punya website sendiri"] = 28
    if s["ada_url"] and s["web_mati"]:
        butuh_web += 28
        rincian["website mati/error"] = 28
    if s["punya_web"] and s["gratisan"]:
        butuh_web += 12
        rincian["platform gratisan"] = 12
    if s["https"] is False:
        butuh_web += 8
        rincian["tanpa HTTPS"] = 8
    if s["mobile"] is False:
        butuh_web += 8
        rincian["tidak ramah HP"] = 8
    if (s["load_ms"] or 0) > 5000:
        butuh_web += 6
        rincian["website lambat"] = 6
    if s["basi"]:
        butuh_web += 6
        rincian["situs terbengkalai"] = 6
    butuh_web = min(butuh_web, 28)

    butuh_mkt = 0
    # Hanya dihitung kalau memang sempat diperiksa. Kolom sosmed yang kosong
    # karena bisnisnya tidak punya website bukan bukti mereka tidak punya akun.
    if s["sosmed_dicek"] and not s["sosmed_ada"]:
        butuh_mkt += 14
        rincian["belum ada media sosial"] = 14
    if s["diklaim"] is False:
        butuh_mkt += 8
        rincian["listing belum diklaim"] = 8
    if s["pixel"] is False:
        butuh_mkt += 6
        rincian["belum ada tracking iklan"] = 6
    if s["foto"] is not None and _bulat(s["foto"]) == 0:
        butuh_mkt += 6
        rincian["listing tanpa foto"] = 6
    if s["rating"] and s["rating"] < 4.0 and s["ulasan"] >= 30:
        butuh_mkt += 6
        rincian["rating jadi penghambat"] = 6
    butuh_mkt = min(butuh_mkt, 22)

    butuh = min(butuh_web + butuh_mkt, 40)

    # ── B. MAMPU BAYAR ──
    mampu = 0
    poin_ulasan = min(15.0, 5.0 * math.log10(s["ulasan"] + 1))
    mampu += poin_ulasan
    rincian[f"ukuran bisnis ({s['ulasan']} ulasan)"] = round(poin_ulasan, 1)
    if s["rating"] >= 4.0:
        mampu += 5
        rincian["rating baik"] = 5
    if _cocok(s["kategori"], KATEGORI_TINGGI):
        mampu += 10
        rincian["kategori bernilai tinggi"] = 10
    elif _cocok(s["kategori"], KATEGORI_LEMAH_WEB):
        mampu -= 10
        rincian["sektor jarang membeli website"] = -10
    elif _cocok(s["kategori"], KATEGORI_RENDAH):
        mampu -= 5
        rincian["kategori margin tipis"] = -5
    if s["pixel"] is True:
        # Bukti paling nyata: mereka pernah mengeluarkan uang untuk marketing.
        mampu += 5
        rincian["terbukti pernah belanja iklan"] = 5
    if s["harga"].count("Rp") >= 2 or "$$" in s["harga"]:
        mampu += 5
        rincian["rentang harga menengah ke atas"] = 5
    if int(jumlah_cabang or 1) >= 2:
        mampu += 5
        rincian[f"punya {jumlah_cabang} lokasi"] = 5
    mampu = max(0.0, min(mampu, 35.0))

    # ── C. BISA DIHUBUNGI ──
    hubungi = 0
    if s["wa"]:
        hubungi += 15
        rincian["nomor WhatsApp"] = 15
    if s["telepon"]:
        hubungi += 5
        rincian["ada nomor telepon"] = 5
    if s["email"]:
        hubungi += 5
        rincian["ada email"] = 5
    if s["ig"] or s["fb"]:
        hubungi += 3
        rincian["ada akun sosmed"] = 3
    hubungi = min(hubungi, 25)

    total = int(round(butuh + mampu + hubungi))

    # Bisnis yang tutup (permanen maupun sementara) tidak bisa dihubungi
    # sebagai pembeli sekarang.
    if bisnis_tutup(s["status_buka"]):
        total = 0
        rincian = {str(s["status_buka"]).lower() or "bisnis tutup": 0}

    if total >= 75:
        tier = TIER_PANAS
    elif total >= 55:
        tier = TIER_HANGAT
    elif total >= 35:
        tier = TIER_DINGIN
    else:
        tier = TIER_ARSIP

    return {
        "skor_pembeli": total,
        "tier": tier,
        "skor_butuh": int(round(butuh)),
        "skor_mampu": int(round(mampu)),
        "skor_hubungi": int(round(hubungi)),
        "rincian": rincian,
    }


def skor_popularitas(rating, ulasan):
    """
    Skor lama `rating × log10(ulasan+1)`, dibatasi 10.

    Ini mengukur POPULARITAS, bukan potensi beli — bisnis paling populer justru
    sering sudah punya website bagus. Dipertahankan sebagai kolom pembanding
    supaya data lama tetap bisa dibandingkan.
    """
    try:
        r = float(rating) if rating else 0.0
        u = int(ulasan) if ulasan else 0
        if r == 0:
            return 0.0
        return round(min(r * math.log10(u + 1), 10.0), 2)
    except (TypeError, ValueError):
        return 0.0


# ─── Gabungan ─────────────────────────────────────────────────────────────────

def nilai_lead(row, jumlah_cabang=1):
    """
    Hitung rekomendasi jasa + skor pembeli sekaligus, lalu tempelkan ke row.

    Row diubah di tempat dan juga dikembalikan, supaya enak dipakai dalam
    list comprehension maupun loop biasa.
    """
    # Impor malas: scrapers.kontak & scrapers.kualitas mengimpor modul ini.
    from scrapers import kontak, kualitas
    # Rapikan data kotor (email, alamat, kota, nama sapaan) sebelum dinilai.
    kualitas.perbaiki(row)
    kontak.rapikan_kontak(row)
    row.update(rekomendasi_jasa(row))
    hasil = skor_pembeli(row, jumlah_cabang)
    row["skor_pembeli"] = hasil["skor_pembeli"]
    row["tier"] = hasil["tier"]
    row["skor_popularitas"] = skor_popularitas(row.get("rating"),
                                               row.get("jumlah_ulasan"))
    return row
