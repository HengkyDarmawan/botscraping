"""
scrapers/scoring.py — Ubah data mentah jadi keputusan penjualan.

Menjawab dua pertanyaan untuk setiap bisnis:

  1. "Saya sebaiknya menawarkan jasa apa ke bisnis ini?"
     → rekomendasi_jasa(): aturan berurutan yang membaca kondisi aset digital
       bisnis dan memilih antara jalur WEB DEVELOPMENT atau DIGITAL MARKETING,
       lengkap dengan satu kalimat pembuka yang siap dikirim.

  2. "Seberapa besar peluang bisnis ini benar-benar membeli?"
     → skor_pembeli(): skor 0-100 dari tiga hal yang harus ada bersamaan —
       BUTUH (ada masalah digital), MAMPU BAYAR (bisnisnya cukup besar), dan
       BISA DIHUBUNGI (ada jalur kontak). Punya masalah tapi tidak punya uang
       bukan pembeli; punya uang tapi tidak bisa dihubungi juga bukan pembeli.

Modul ini murni perhitungan — tidak membuka jaringan dan tidak menyentuh
database, jadi gampang diuji sendiri dan aturannya gampang diubah.
"""
import math
import re
from datetime import datetime

from scrapers.enrich import PLATFORM_GRATISAN, url_sosmed, website_efektif

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
)
KATEGORI_RENDAH = (
    "warung", "warteg", "kaki lima", "angkringan", "warnet", "konter",
    "pulsa", "gerobak", "kios", "pedagang", "jajanan", "burjo",
)
# Kategori yang penjualannya cocok dipindah ke online.
KATEGORI_RETAIL = (
    "toko", "butik", "fashion", "baju", "sepatu", "tas", "kosmetik",
    "elektronik", "furniture", "mebel", "oleh-oleh", "grosir", "restoran",
    "rumah makan", "kafe", "cafe", "bakery", "kue", "roti", "katering",
    "catering", "frozen", "minuman",
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
    return d.startswith("628") and 10 <= len(d) <= 14


def link_wa(phone_raw, pesan=""):
    """Tautan wa.me untuk nomor HP seluler; nomor kabel/kosong → string kosong."""
    if not nomor_wa_valid(phone_raw):
        return ""
    tautan = f"https://wa.me/{normalisasi_nomor(phone_raw)}"
    if pesan:
        from urllib.parse import quote
        tautan += "?text=" + quote(pesan)
    return tautan


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
    platform = str(row.get("web_platform") or "")

    return {
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
        "wa": nomor_wa_valid(row.get("telepon")),
        "email": str(row.get("email") or "").strip(),
        "ig": str(row.get("instagram") or "").strip(),
        "fb": str(row.get("facebook") or "").strip(),

        "diklaim": _tri(row.get("sudah_diklaim")),
        "foto": row.get("jumlah_foto"),
        "harga": str(row.get("rentang_harga") or ""),
        "status_buka": str(row.get("status_buka") or ""),
    }


def _cocok(teks, daftar):
    return any(k in teks for k in daftar)


# ─── Aturan rekomendasi jasa ──────────────────────────────────────────────────
# Diperiksa dari atas ke bawah: yang pertama cocok jadi jasa utama, yang cocok
# berikutnya jadi jasa pendukung. Tambah/ubah aturan cukup di daftar ini.

RULES = [
    {
        # Diperiksa paling awal supaya bisnis yang sudah tutup tidak sempat
        # diberi label jasa yang menyesatkan.
        "jasa": "Bisnis Tutup — Lewati",
        "jalur": "",
        "cek": lambda s: ("permanen" in s["status_buka"].lower()
                          or "closed" in s["status_buka"].lower()),
        "alasan": lambda s: "",
    },
    {
        "jasa": "Website Baru (URGENT)",
        "jalur": "web",
        "cek": lambda s: s["ada_url"] and s["web_mati"],
        "alasan": lambda s: (
            "Website Anda saat ini tidak bisa diakses — calon pelanggan yang "
            "mencari nama Anda di Google berakhir di halaman error dan pindah "
            "ke kompetitor."
        ),
    },
    {
        "jasa": "Landing Page + Iklan Sosmed",
        "jalur": "web",
        "cek": lambda s: s["sosmed_saja"],
        "alasan": lambda s: (
            f"Anda sudah aktif berjualan online, tapi tautan {s['nama']} masih "
            "mengarah ke media sosial/marketplace — belum ada halaman milik "
            "sendiri yang bisa dioptimasi dan diiklankan."
        ),
    },
    {
        "jasa": "Upgrade Website Profesional",
        "jalur": "web",
        "cek": lambda s: s["punya_web"] and s["gratisan"],
        "alasan": lambda s: (
            f"Website Anda masih memakai platform gratisan ({s['platform']}) — "
            "alamatnya bukan domain milik sendiri dan kurang meyakinkan di mata "
            "calon klien."
        ),
    },
    {
        "jasa": "Website Company Profile",
        "jalur": "web",
        "cek": lambda s: not s["ada_url"] and s["ulasan"] >= 20 and s["rating"] >= 4.0,
        "alasan": lambda s: (
            f"Bisnis Anda jelas ramai — {s['ulasan']} ulasan dengan rating "
            f"{s['rating']:.1f} — tapi belum punya website. Calon pelanggan yang "
            "mencari di Google tidak menemukan Anda."
        ),
    },
    {
        "jasa": "Landing Page + Iklan Instagram",
        "jalur": "web",
        "cek": lambda s: not s["ada_url"] and (s["ig"] or s["fb"]),
        "alasan": lambda s: (
            "Anda sudah punya audiens di media sosial tapi belum punya website. "
            "Satu landing page saja sudah cukup untuk mengubah pengikut jadi "
            "pesanan yang terukur."
        ),
    },
    {
        "jasa": "Website Company Profile",
        "jalur": "web",
        "cek": lambda s: not s["ada_url"],
        "alasan": lambda s: (
            "Bisnis Anda belum punya website — pelanggan hanya bisa menilai dari "
            "listing Google Maps, sementara kompetitor sudah tampil lebih "
            "profesional."
        ),
    },
    {
        "jasa": "Redesign + Pasang SSL",
        "jalur": "web",
        "cek": lambda s: s["punya_web"] and s["https"] is False,
        "alasan": lambda s: (
            "Website Anda belum memakai HTTPS, jadi Chrome menandainya "
            "'Tidak Aman' dan memperingatkan pengunjung sebelum mereka masuk."
        ),
    },
    {
        "jasa": "Redesign Mobile-First",
        "jalur": "web",
        "cek": lambda s: s["punya_web"] and s["mobile"] is False,
        "alasan": lambda s: (
            "Website Anda belum ramah layar HP, padahal hampir semua pelanggan "
            "membukanya dari ponsel."
        ),
    },
    {
        "jasa": "Optimasi Kecepatan Website",
        "jalur": "web",
        "cek": lambda s: s["punya_web"] and (s["load_ms"] or 0) > 5000,
        "alasan": lambda s: (
            f"Website Anda butuh sekitar {(s['load_ms'] or 0) / 1000:.1f} detik "
            "untuk terbuka. Google menurunkan peringkat situs selambat itu dan "
            "sebagian pengunjung pergi sebelum halaman muncul."
        ),
    },
    {
        "jasa": "Redesign (situs terbengkalai)",
        "jalur": "web",
        "cek": lambda s: s["punya_web"] and s["basi"],
        "alasan": lambda s: (
            f"Website Anda terakhir diperbarui tahun {s['tahun_web']} — "
            "informasi dan tampilannya sudah tertinggal dari kondisi bisnis "
            "Anda sekarang."
        ),
    },
    {
        "jasa": "Toko Online / Sistem Order",
        "jalur": "web",
        "cek": lambda s: (s["punya_web"] and s["toko_online"] is False
                          and _cocok(s["kategori"], KATEGORI_RETAIL)),
        "alasan": lambda s: (
            "Pelanggan Anda masih harus chat satu per satu untuk memesan. "
            "Sistem order online membuat mereka bisa memesan sendiri kapan pun."
        ),
    },
    {
        "jasa": "Optimasi Google Business Profile",
        "jalur": "marketing",
        "cek": lambda s: s["diklaim"] is False,
        "alasan": lambda s: (
            "Listing Google Maps Anda terlihat belum diklaim pemiliknya — "
            "artinya Anda belum bisa mengatur info, foto, dan membalas ulasan, "
            "dan kompetitor bisa tampil di atas Anda."
        ),
    },
    {
        "jasa": "Reputation Management + Ads",
        "jalur": "marketing",
        "cek": lambda s: s["rating"] and s["rating"] < 4.0 and s["ulasan"] >= 30,
        "alasan": lambda s: (
            f"Rating {s['rating']:.1f} dari {s['ulasan']} ulasan jadi penghambat "
            "utama — calon pelanggan membandingkan angka ini sebelum menghubungi."
        ),
    },
    {
        "jasa": "Optimasi Google Business Profile",
        "jalur": "marketing",
        "cek": lambda s: s["rating"] >= 4.0 and 0 < s["ulasan"] < 10,
        "alasan": lambda s: (
            f"Rating Anda bagus ({s['rating']:.1f}) tapi baru {s['ulasan']} "
            "ulasan, jadi Anda kalah tampil dari kompetitor di pencarian Maps."
        ),
    },
    {
        "jasa": "Scale-up Google & Meta Ads",
        "jalur": "marketing",
        "cek": lambda s: s["punya_web"] and s["pixel"] is True,
        "alasan": lambda s: (
            "Website Anda sudah terpasang tracking iklan — tinggal dioptimasi "
            "supaya biaya per leadnya turun dan jumlah leadnya naik."
        ),
    },
    {
        "jasa": "Google Ads + Setup Tracking",
        "jalur": "marketing",
        "cek": lambda s: s["punya_web"] and s["pixel"] is False,
        "alasan": lambda s: (
            "Website Anda sudah bagus tapi belum ada tracking sama sekali, jadi "
            "setiap rupiah yang dikeluarkan untuk promosi tidak bisa diukur "
            "hasilnya."
        ),
    },
    {
        # Jaring terakhir: bisnis punya website tapi belum pernah diperiksa,
        # jadi belum ada dasar untuk menentukan jasanya. Diberi label yang
        # menyebut langkah berikutnya, bukan sekadar "tidak tahu".
        "jasa": "Perlu Cek Website Dulu",
        "jalur": "",
        "cek": lambda s: s["ada_url"] and s["web_status_kosong"],
        "alasan": lambda s: (
            "Bisnis ini punya website tapi kondisinya belum diperiksa. Nyalakan "
            "opsi \"Buka website tiap lead\" saat scraping untuk mendapat "
            "rekomendasi jasa yang tepat."
        ),
    },
]


def rekomendasi_jasa(row):
    """
    Tentukan jasa yang paling pas ditawarkan ke satu bisnis.

    Return dict: jasa_utama, jasa_pendukung (maks 2, dipisah " | "),
    alasan_pitch (satu kalimat siap kirim), jalur ("web"/"marketing").
    """
    s = sinyal(row)
    cocok = []
    for r in RULES:
        try:
            if r["cek"](s):
                cocok.append(r)
        except Exception:
            continue  # aturan tidak berlaku untuk data setengah lengkap

    if not cocok:
        return {
            "jasa_utama": JASA_RISET_MANUAL,
            "jasa_pendukung": "",
            "alasan_pitch": "",
            "jalur": "",
        }

    utama = cocok[0]
    pendukung = []
    for r in cocok[1:]:
        if r["jasa"] != utama["jasa"] and r["jasa"] not in pendukung:
            pendukung.append(r["jasa"])
        if len(pendukung) >= 2:
            break

    try:
        alasan = utama["alasan"](s)
    except Exception:
        alasan = ""

    return {
        "jasa_utama": utama["jasa"],
        "jasa_pendukung": " | ".join(pendukung),
        "alasan_pitch": alasan,
        "jalur": utama["jalur"],
    }


# ─── Skor potensi pembeli ─────────────────────────────────────────────────────

def skor_pembeli(row, jumlah_cabang=1):
    """
    Skor 0-100 dari tiga dimensi yang harus terpenuhi bersamaan.

      BUTUH (maks 40)          — seberapa besar masalah digitalnya
      MAMPU BAYAR (maks 35)    — seberapa besar bisnisnya
      BISA DIHUBUNGI (maks 25) — ada jalur kontak yang benar-benar bisa dipakai

    `jumlah_cabang` datang dari db.hitung_cabang(): jaringan dengan beberapa
    lokasi biasanya punya anggaran lebih besar.

    Return dict: skor_pembeli, tier, rincian (untuk ditampilkan/di-debug).
    """
    s = sinyal(row)
    rincian = {}

    # ── A. BUTUH ──
    # Catatan kalibrasi: bisnis TANPA website sama sekali tidak punya sinyal
    # HTTPS/mobile/kecepatan untuk dinilai, jadi kalau bobotnya kecil ia justru
    # kalah skor dari bisnis yang punya website jelek — padahal ia prospek yang
    # jauh lebih baik. Karena itu dua kondisi di bawah diberi bobot besar.
    butuh = 0
    if not s["ada_url"] or s["sosmed_saja"]:
        butuh += 28
        rincian["belum punya website sendiri"] = 28
    if s["ada_url"] and s["web_mati"]:
        butuh += 28
        rincian["website mati/error"] = 28
    if s["punya_web"] and s["gratisan"]:
        butuh += 12
        rincian["platform gratisan"] = 12
    if s["diklaim"] is False:
        butuh += 8
        rincian["listing belum diklaim"] = 8
    if s["https"] is False:
        butuh += 8
        rincian["tanpa HTTPS"] = 8
    if s["mobile"] is False:
        butuh += 8
        rincian["tidak ramah HP"] = 8
    if (s["load_ms"] or 0) > 5000:
        butuh += 6
        rincian["website lambat"] = 6
    if s["basi"]:
        butuh += 6
        rincian["situs terbengkalai"] = 6
    if s["pixel"] is False:
        butuh += 6
        rincian["belum ada tracking iklan"] = 6
    if s["foto"] is not None and _bulat(s["foto"]) == 0:
        butuh += 4
        rincian["listing tanpa foto"] = 4
    butuh = min(butuh, 40)

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

    # Bisnis yang sudah tutup permanen tidak mungkin jadi pembeli.
    if "permanen" in s["status_buka"].lower() or "closed" in s["status_buka"].lower():
        total = 0
        rincian = {"tutup permanen": 0}

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
    row.update(rekomendasi_jasa(row))
    hasil = skor_pembeli(row, jumlah_cabang)
    row["skor_pembeli"] = hasil["skor_pembeli"]
    row["tier"] = hasil["tier"]
    row["skor_popularitas"] = skor_popularitas(row.get("rating"),
                                               row.get("jumlah_ulasan"))
    row.pop("jalur", None)
    return row


def pesan_wa(row, pengirim="", usaha="", template=""):
    """
    Rakit pesan WhatsApp pembuka dari alasan_pitch.

    Template bisa diubah lewat config.py (PESAN_TEMPLATE) tanpa menyentuh kode.
    """
    if not template:
        template = (
            "Halo {nama}, perkenalkan saya {pengirim} dari {usaha}.\n\n"
            "{alasan}\n\n"
            "Boleh saya kirimkan contoh hasil kerja dan estimasi biayanya?"
        )
    alasan = str(row.get("alasan_pitch") or "").strip()
    if not alasan:
        alasan = ("Saya melihat profil Google Maps bisnis Anda dan ada beberapa hal "
                  "yang bisa dioptimasi untuk mendatangkan lebih banyak pelanggan.")
    try:
        return template.format(
            nama=row.get("nama_bisnis") or "Bapak/Ibu",
            pengirim=pengirim or "saya",
            usaha=usaha or "tim digital kami",
            alasan=alasan,
            jasa=row.get("jasa_utama") or "",
        )
    except (KeyError, IndexError):
        return alasan
