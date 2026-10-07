"""
scrapers/kualitas.py — Pembersih kualitas lead ruang Klien Website.

Dua tahap dengan satu sumber aturan:

  1. perbaiki(row)      — data yang SALAH TULIS diperbaiki, bukan dibuang:
                          email kotor ("<b>info@x.co.id</b>", dua alamat dalam
                          satu sel), alamat berawalan ikon, kota kosong, nomor
                          0800/0804 yang dijadikan tautan WhatsApp, website
                          pihak ketiga di `website_utama`, dan `nama_sapaan`
                          yang rapi untuk template pesan.
  2. alasan_sampah(row) — lead yang memang bukan calon pembeli jasa website
                          (Blogspot, fasilitas umum, nama generik, listing
                          hantu, tanpa WA & email) → alasan, atau None.
     kelompok_jaringan  — cabang satu jaringan (nomor WA / domain / nama sama)
                          → satu wakil dipertahankan, sisanya disaring.

Dipakai saat scraping (scrapers/gmaps.py, lewat scoring.nilai_lead) dan untuk
membersihkan data lama (db.saring_kualitas). Library pembersih bersifat
opsional: tanpa ftfy / tldextract / rapidfuzz / email-validator modul ini tetap
jalan dengan aturan regex, hanya kurang teliti.

Naikkan VERSI setiap kali aturan di sini berubah — saat app dinyalakan, data
lama disaring ulang otomatis (db.pastikan_saring_terbaru).
"""
import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from scrapers import scoring
from scrapers.enrich import (_HOST_PINJAMAN, _host_cocok, bersihkan_email,
                             url_pinjaman, url_sosmed)

try:
    import ftfy
except ImportError:
    ftfy = None
try:
    import tldextract
    # suffix_list_urls=() → pakai daftar akhiran bawaan paket, tanpa unduhan.
    # Domain "pribadi" (x.blogspot.com, x.wixsite.com) dihitung domain sendiri,
    # bukan blogspot.com — kalau tidak, semua blog terbaca satu jaringan.
    _TLD = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)
except Exception:
    _TLD = None
try:
    from rapidfuzz import fuzz, process
except ImportError:
    fuzz = process = None
try:
    from email_validator import EmailUndeliverableError, validate_email
except ImportError:
    validate_email = None

VERSI = 2

WILAYAH_FILE = Path(__file__).resolve().parent.parent / "data" / "wilayah.json"


# ─── Teks ─────────────────────────────────────────────────────────────────────

# Ikon font Google Maps (Private Use Area) yang ikut terbaca di depan alamat:
# "\nJl. ..." — di Excel tampil sebagai kotak kosong.
_RE_PUA = re.compile("[-]")


def teks_bersih(teks):
    """Perbaiki mojibake (ftfy), buang ikon PUA, rapikan spasi. None tetap None."""
    if teks is None:
        return None
    t = str(teks)
    if ftfy is not None:
        t = ftfy.fix_text(t)
    t = _RE_PUA.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


# ─── Kota ─────────────────────────────────────────────────────────────────────

# Singkatan yang dipakai Google Maps di alamat ("Kota Bks", "Jkt Utara").
_ALIAS_KOTA = {"bks": "bekasi", "jkt": "jakarta", "tng": "tangerang",
               "tangsel": "tangerang selatan", "dki jakarta": "jakarta",
               "banjar baru": "banjarbaru", "pangkal pinang": "pangkalpinang",
               "tanjung pinang": "tanjungpinang"}


def _kunci(nama):
    return re.sub(r"[^a-z]", "", str(nama or "").lower())


@lru_cache(maxsize=1)
def _indeks_wilayah():
    """
    ({kunci nama kota/kab: "Kota X"/"Kabupaten X"}, {kunci kecamatan: kota induk}).
    Format sama dengan kolom `kota` hasil scraping ("Kota Depok", "Kabupaten Bogor").
    """
    kota, kec = {}, {}
    try:
        data = json.loads(WILAYAH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return kota, kec
    for prov in data.get("provinsi", []):
        for w in prov.get("wilayah", []):
            n = w.get("n") or ""
            if n.lower().startswith(("kota ", "kabupaten ")):
                kanonik = n
                polos = n.split(" ", 1)[1]
            else:
                kanonik = ("Kota " if w.get("t") == "kota" else "Kabupaten ") + n
                polos = n
            kota[_kunci(kanonik)] = kanonik
            # Nama polos ("Bekasi") menunjuk ke kota; kabupaten bernama sama
            # selalu tertulis lengkap "Kabupaten Bekasi" di data ini.
            kota.setdefault(_kunci(polos), kanonik)
            for k in w.get("kec", []):
                kec.setdefault(_kunci(k.get("n")), kanonik)
    return kota, kec


_RE_KOTA = re.compile(r"\b(Kota|Kabupaten|Kab\.?)\s+([A-Za-z][A-Za-z .'-]*?)(?=,|\s+\d|$)")
_RE_JKT = re.compile(r"\b(?:Kota\s+)?(?:Jkt|Jakarta)\.?\s+(Utara|Selatan|Barat|Timur|Pusat)\b", re.I)


def kota_kanonik(nama, jenis=""):
    """'Bks' / 'Bekasi' / 'Kota Bekasi' → 'Kota Bekasi'; tidak dikenal → None."""
    kota, kec = _indeks_wilayah()
    n = str(nama or "").strip().lower()
    n = _ALIAS_KOTA.get(n, n)
    if jenis:
        hit = kota.get(_kunci(f"{jenis} {n}"))
        if hit:
            return hit
    return kota.get(_kunci(n)) or kec.get(_kunci(n))


def kota_dari_alamat(alamat):
    """Kota/kabupaten dari teks alamat Google Maps, atau None."""
    teks = str(alamat or "")
    m = _RE_JKT.search(teks)
    if m:
        return f"Kota Jakarta {m.group(1).capitalize()}"
    for m in _RE_KOTA.finditer(teks):
        jenis = "Kota" if m.group(1).lower() == "kota" else "Kabupaten"
        hit = kota_kanonik(m.group(2), jenis)
        if hit:
            return hit
    return None


# ─── Nama sapaan ──────────────────────────────────────────────────────────────

# Ditulis kapital apa pun bentuk aslinya.
_SINGKATAN = {"PT", "CV", "UD", "PD", "KSP", "KUD", "RS", "RSIA", "RSU", "RSUD",
              "SD", "SMP", "SMA", "SMK", "TK", "PAUD", "BPR", "BPRS", "LPK", "LKP",
              "PPAT", "AC", "TV", "EO", "IT", "CCTV", "GPS", "UMKM", "II", "III",
              "IV", "VI", "VII", "VIII", "IX", "XI", "XII", "KB", "PGRI"}
# Kata pendek yang BUKAN singkatan — ikut Title Case biasa.
_KATA_BIASA = {"DAN", "DI", "KE", "THE", "OF", "AND", "BY", "FOR", "BAR", "CAR",
               "SPA", "GYM", "JL", "NO", "INN", "PET", "ART", "TOP", "BIG", "NEW",
               "ONE", "KITA", "AYAM", "MIE", "SOP", "KUE", "TOKO", "BUKU", "MAS",
               # kata sandang Arab & sapaan yang lazim di nama usaha/sekolah
               "AL", "EL", "AN", "AR", "AS", "AT", "UL", "BIN", "NUR", "ABU", "IBU",
               "BU", "PAK", "MBA", "ADA", "YA", "UNO", "DUA", "TIGA"}
_PEMISAH_NAMA = (" | ", " - ", " – ", " — ", " ~ ", " : ", " :: ")


def _title(nama):
    kata = []
    for k in nama.split(" "):
        inti = k.strip(".,&()/'")
        if inti.upper() in _SINGKATAN:
            kata.append(k.upper())
        elif inti.upper() == "TBK":
            kata.append(k.replace(inti, "Tbk"))
        elif any(c.isdigit() for c in inti) or (
                len(inti) <= 3 and inti.isalpha() and inti.upper() not in _KATA_BIASA):
            kata.append(k.upper())  # kemungkinan singkatan merek: "BCA", "JNE"
        else:
            kata.append("-".join(b[:1].upper() + b[1:].lower() for b in k.split("-")))
    return " ".join(kata)


def nama_sapaan(nama):
    """
    Nama yang enak dibaca di pesan: tanpa deretan keyword promosi dan tidak
    HURUF KAPITAL semua. "Cv Nusantara Engineering Solution - layanan jasa
    service ac - jabodetabek" → "CV Nusantara Engineering Solution".
    """
    n = teks_bersih(nama) or ""
    for pemisah in _PEMISAH_NAMA:
        if pemisah in n:
            kiri = n.split(pemisah)[0].strip(" ,")
            if len(kiri) >= 4:
                n = kiri
    # "(Jasa Pembuatan PT / CV Perusahaan)" di ujung nama.
    tanpa_kurung = re.sub(r"\s*\([^)]*\)?\s*$", "", n).strip()
    if len(tanpa_kurung) >= 4:
        n = tanpa_kurung
    # "CV.BERLIAN" → "CV. BERLIAN"
    n = re.sub(r"\b(PT|CV|UD|PD)\.(?=\S)", r"\1. ", n, flags=re.I)
    # Nama kapital diikuti deskripsi huruf kecil ("CV.BERLIAN SUKSES MANDIRI
    # General contractor & supplier ...") → bagian kapitalnya saja.
    if len(n) > 40:
        m = re.match(r"^((?:[A-Z0-9&.'/]+\s+){1,6}[A-Z0-9&.'/]+)\s+[A-Z]?[a-z]", n)
        if m:
            n = m.group(1)
    if len(n) > 60:
        n = " ".join(n[:60].split(" ")[:-1]) or n[:60]
    huruf = [c for c in n if c.isalpha()]
    if huruf and (all(c.isupper() for c in huruf) or all(c.islower() for c in huruf)):
        n = _title(n)
    elif n.lower().startswith(("cv ", "pt ", "ud ")):
        n = n[:2].upper() + n[2:]
    return n.strip(" ,.-") or str(nama or "").strip()


# ─── Perbaikan per baris ──────────────────────────────────────────────────────

# Salah ketik domain penyedia email gratis yang ditemukan di data nyata (cek MX
# menandainya mati) — dibetulkan, bukan dibuang.
_TYPO_DOMAIN = {
    "gmail.co": "gmail.com", "gmail.co.id": "gmail.com", "gmail.con": "gmail.com",
    "gmail.cm": "gmail.com", "gmail.om": "gmail.com", "gmial.com": "gmail.com",
    "gmai.com": "gmail.com", "gamil.com": "gmail.com", "gnail.com": "gmail.com",
    "gmaill.com": "gmail.com", "gmail.id": "gmail.com",
    "yahoo.con": "yahoo.com", "yaho.com": "yahoo.com", "yahooo.com": "yahoo.com",
    "yahoo.co.od": "yahoo.co.id", "yahoo.coid": "yahoo.co.id",
    "hotmail.co": "hotmail.com", "hotmail.con": "hotmail.com",
}


def _betulkan_domain(email):
    lokal, _, domain = email.rpartition("@")
    return f"{lokal}@{_TYPO_DOMAIN.get(domain, domain)}" if lokal else email


def _nomor_dari_link(link):
    m = re.search(r"wa\.me/(\d+)", str(link or ""))
    return m.group(1) if m else ""


def perbaiki(row):
    """
    Perbaiki kolom yang salah tulis pada `row` (diubah di tempat).

    Return {kolom: (lama, baru)} untuk kolom yang berubah. Kolom yang tidak ada
    di row (None) tidak disentuh — "tidak terbaca" bukan berarti "kosong".
    """
    ubah = {}

    def set_(kolom, baru):
        lama = row.get(kolom)
        if (lama or "") != (baru or ""):
            ubah[kolom] = (lama, baru)
            row[kolom] = baru

    if row.get("alamat"):
        set_("alamat", teks_bersih(row["alamat"]))

    if row.get("email") or row.get("email_lain"):
        semua = bersihkan_email(row.get("email") or "", row.get("email_lain") or "")
        semua = list(dict.fromkeys(_betulkan_domain(e) for e in semua))
        set_("email", semua[0] if semua else "")
        set_("email_lain", "; ".join(semua[1:6]))

    # Nomor bebas pulsa / premium (0800, 0804, 0807) bukan WhatsApp.
    link = row.get("whatsapp_link")
    if link and not scoring.nomor_wa_valid(_nomor_dari_link(link)):
        set_("whatsapp_link", "")

    if row.get("website_utama") and url_pinjaman(row["website_utama"]):
        set_("website_utama", "")

    if not row.get("kota"):
        kota = kota_dari_alamat(row.get("alamat")) or kota_kanonik(row.get("area_pencarian"))
        if kota:
            set_("kota", kota)

    if row.get("nama_bisnis"):
        set_("nama_sapaan", nama_sapaan(row["nama_bisnis"]))
    return ubah


# ─── Aturan sampah ────────────────────────────────────────────────────────────

_RE_BLOGSPOT = re.compile(r"(^|\.)blogspot\.[a-z.]+$")


def website_blog(website="", platform=""):
    """
    True bila website-nya Blogspot/Blogger (umumnya sudah lama tidak aktif).
    WordPress.com sengaja TIDAK termasuk — masih sering dipakai aktif.
    """
    p = str(platform or "").strip().lower()
    if p == "blogspot" or p.startswith("blogger"):
        return True
    u = str(website or "").strip().lower()
    if not u:
        return False
    if "://" not in u:
        u = "https://" + u
    host = urlparse(u).netloc.split(":")[0]
    return bool(_RE_BLOGSPOT.search(host))


# Kategori Google Maps milik fasilitas umum / instansi — bukan pembeli jasa.
_RE_KATEGORI_PUBLIK = re.compile(
    r"^(taman|taman kota|monumen|museum.*|pura|vihara|klenteng|masjid.*|gereja.*|"
    r"tempat ibadah|kantor (pemerintah.*|pemda|distrik|polisi|pajak|pos|catatan sipil|"
    r"registrasi kependudukan|jaminan sosial|pendaftaran pemilih|administrasi kota|"
    r"keselamatan publik|jaksa.*|pelayanan (pajak|stnk.*)|paten|"
    r"pendaftaran kekayaan intelektual)|polisi.*|pengadilan.*|rumah sakit pemerintah|"
    r"stasiun.*|terminal.*|halte.*|atm|balai (masyarakat|desa|warga)|universitas negeri|"
    r"toko sembako pemerintah|pemakaman.*|"
    r"lapangan (voli|bola basket|basket|atletik|bola tangan))$", re.I)
# Lapangan sepak bola: umum, KECUALI namanya menunjukkan usaha sewa.
_RE_KATEGORI_BOLA = re.compile(r"^lapangan (sepak bola|football)$", re.I)
_RE_NAMA_KOMERSIAL = re.compile(
    r"\b(sewa|rental|arena|soccer|futsal|center|centre|sport|academy|akademi|"
    r"booking|fc|club)\b", re.I)
# Nama yang jelas milik instansi/fasilitas umum, apa pun kategorinya. Diikat ke
# AWAL nama supaya "Toko Perlengkapan Masjid" tidak ikut tersaring.
_RE_NAMA_PUBLIK = re.compile(
    r"^(masjid|mushol+a|musala|surau|gereja|vihara|klenteng|kantor (kelurahan|"
    r"kecamatan|desa|camat|lurah|bupati|wali ?kota|gubernur|pos)|kelurahan|kecamatan|"
    r"balai (desa|warga|rw|rt)|polsek|polres|polda|koramil|kodim|samsat|pengadilan|"
    r"kejaksaan|kejari|puskesmas|pustu|posyandu|rsud|sdn|smpn|sman|smkn|mtsn|tpu|"
    r"alun-alun|atm|kpp|dinas|bpjs)\b"
    r"|\b(sd|smp|sma|smk) ?negeri\b", re.I)


def fasilitas_publik(nama, kategori):
    """True bila ini fasilitas umum / instansi pemerintah, bukan usaha."""
    n = str(nama or "").strip()
    k = str(kategori or "").strip()
    if _RE_KATEGORI_PUBLIK.match(k) or _RE_NAMA_PUBLIK.search(n):
        return True
    return bool(_RE_KATEGORI_BOLA.match(k) and not _RE_NAMA_KOMERSIAL.search(n))


_NAMA_GENERIK = {"cv", "pt", "ud", "toko", "warung", "kantor", "gudang", "rumah",
                 "lapangan", "lapangan voli", "lapangan basket", "lapangan futsal",
                 "toko elektronik", "toko bangunan", "bengkel", "salon", "kost",
                 "kos", "rental", "sewa mobil"}


def nama_generik(nama, kategori=""):
    """Nama yang sama sekali tidak menyebut identitas usaha ("CV", "Toko")."""
    n = re.sub(r"[^a-z0-9 ]", "", str(nama or "").lower()).strip()
    k = str(kategori or "").strip().lower()
    return len(n) < 4 or n in _NAMA_GENERIK or (bool(k) and n == k)


def listing_hantu(row):
    """Tanpa rating dan tanpa ulasan sama sekali — listing yang tidak pernah hidup."""
    ulasan = row.get("jumlah_ulasan")
    return row.get("rating") in (None, "", 0) and (ulasan in (None, "") or int(ulasan or 0) == 0)


def alasan_kartu(kartu):
    """Aturan yang sudah pasti dari kartu feed (sebelum halaman detail dibuka)."""
    if website_blog(kartu.get("website")):
        return "website blog (Blogspot)"
    if fasilitas_publik(kartu.get("nama"), kartu.get("kategori")):
        return "fasilitas umum / instansi"
    if kartu.get("nama") and nama_generik(kartu.get("nama"), kartu.get("kategori")):
        return "nama generik"
    return None


def alasan_sampah(row, cek_kontak=True):
    """
    Alasan lead ini bukan calon pembeli jasa website, atau None bila layak.

    `cek_kontak=False` dipakai saat scraping sebelum website diperiksa: email
    sering baru ditemukan di website, jadi syarat kontak diputuskan belakangan
    oleh jalur lolos_filter yang sudah ada.
    """
    from scrapers import kontak  # impor malas: kontak → scoring → kualitas

    if website_blog(row.get("website"), row.get("web_platform")):
        return "website blog (Blogspot)"
    if fasilitas_publik(row.get("nama_bisnis"), row.get("kategori")):
        return "fasilitas umum / instansi"
    if nama_generik(row.get("nama_bisnis"), row.get("kategori")):
        return "nama generik"
    if listing_hantu(row):
        return "listing hantu (tanpa rating & ulasan)"
    if cek_kontak and not (kontak.punya_wa(row) or kontak.punya_email(row)):
        return "tanpa WhatsApp & email"
    return None


# ─── Jaringan / cabang ────────────────────────────────────────────────────────

EMAIL_GRATIS = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.id", "ymail.com",
                "rocketmail.com", "hotmail.com", "outlook.com", "outlook.co.id",
                "live.com", "icloud.com", "me.com", "aol.com", "mail.com",
                "protonmail.com", "proton.me", "yandex.com", "zoho.com", "gmx.com"}

_RE_BENTUK_USAHA = re.compile(r"\b(pt|cv|ud|pd|tbk|persero|perseroan|koperasi)\b\.?", re.I)
_RE_AKHIRAN_CABANG = re.compile(
    r"\s+(?:cabang|cab\.?|unit|outlet|kcp|kc|kantor cabang|branch|near|@|-|–|\||di|"
    r"plus @|express @)\s+.*$", re.I)


def domain_terdaftar(url_atau_host):
    """'https://lokasi.pusatgadai.id/x' → 'pusatgadai.id'. Kosong bila gagal."""
    u = str(url_atau_host or "").strip().lower()
    if not u:
        return ""
    host = urlparse(u if "://" in u else f"https://{u}").netloc.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if _TLD is not None:
        try:
            r = _TLD(host)
            d = getattr(r, "top_domain_under_public_suffix", None) or r.registered_domain
            if d:
                return d
        except Exception:
            pass
    bagian = host.split(".")
    if len(bagian) >= 3 and bagian[-2] in ("co", "or", "ac", "go", "web", "my", "sch", "net", "biz"):
        return ".".join(bagian[-3:])
    return ".".join(bagian[-2:])


# Host yang dipakai bersama banyak pemilik tanpa subdomain sendiri (pemendek
# tautan, toko/link-in-bio di path) — bukan tanda satu jaringan.
_HOST_BERSAMA = ("bit.ly", "s.id", "youtu.be", "tinyurl.com", "cutt.ly", "goo.gl",
                 "msha.ke", "desty.page", "linkr.bio", "lynk.id", "wa.link",
                 "linktr.ee", "heylink.me", "tautan.id", "bit.do", "rebrand.ly")


def _domain_website(url):
    """Domain website milik bisnis untuk pengelompokan; kosong untuk sosmed/OTA/Google."""
    u = str(url or "").strip()
    if not u or url_sosmed(u):
        return ""
    host = urlparse(u if "://" in u else f"https://{u}").netloc.lower()
    if _host_cocok(host, _HOST_PINJAMAN + _HOST_BERSAMA) or _host_cocok(
            host, ("google.com", "google.co.id")):
        return ""
    # Host lengkap, bukan domain terdaftar: x.wordpress.com & y.wordpress.com
    # dua website berbeda, sedangkan cabang jaringan memakai host yang sama
    # (lokasi.pusatgadaiindonesia.id/<cabang>).
    return host[4:] if host.startswith("www.") else host


# Kata yang tidak membedakan satu usaha dari usaha lain.
_KATA_UMUM = {"toko", "jasa", "sewa", "rental", "rent", "agen", "pusat", "kantor",
              "usaha", "jaya", "maju", "makmur", "abadi", "sejahtera", "mandiri",
              "sentosa", "bersama", "indonesia", "nusantara", "group", "grup",
              "official", "store", "shop", "mart", "center", "centre", "box",
              "baru", "lama", "murah", "terpercaya", "terbaik", "the", "and", "dan",
              "jakarta", "bogor", "depok", "tangerang", "bekasi", "selatan", "utara",
              "barat", "timur", "pusat", "raya", "kota", "kabupaten"}


def nama_inti(nama):
    """Nama tanpa bentuk usaha & keterangan cabang, untuk mencocokkan jaringan."""
    n = teks_bersih(nama) or ""
    n = _RE_AKHIRAN_CABANG.sub("", n)
    n = _RE_BENTUK_USAHA.sub(" ", n.lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", n)).strip()


def kelompok_jaringan(rows, min_nama=3, ambang_mirip=93):
    """
    Kelompokkan lead yang sebenarnya satu jaringan.

    Sambungan KUAT (cukup 2 lead): nomor WhatsApp sama, domain email non-gratis
    sama, domain website sendiri sama. Sambungan NAMA (nama inti sama / mirip
    ≥ `ambang_mirip` lewat rapidfuzz) baru dihitung bila kelompok namanya
    ≥ `min_nama` lokasi — dua usaha bernama mirip di kota berbeda belum tentu
    satu pemilik.

    Return {place_key: id_kelompok} hanya untuk lead yang punya kelompok ≥ 2.
    """
    induk = {}

    def cari(x):
        while induk.setdefault(x, x) != x:
            induk[x] = induk[induk[x]]
            x = induk[x]
        return x

    def satukan(a, b):
        ra, rb = cari(a), cari(b)
        if ra != rb:
            induk[rb] = ra

    pemilik = {}
    for r in rows:
        key = r["place_key"]
        cari(key)
        tanda = []
        wa = _nomor_dari_link(r.get("whatsapp_link"))
        if wa:
            tanda.append("wa:" + wa)
        for e in str(r.get("email") or "").split(";") + str(r.get("email_lain") or "").split(";"):
            d = e.strip().lower().rpartition("@")[2]
            if d and d not in EMAIL_GRATIS:
                tanda.append("em:" + d)
        dw = _domain_website(r.get("website"))
        if dw:
            tanda.append("web:" + dw)
        for t in tanda:
            if t in pemilik:
                satukan(pemilik[t], key)
            else:
                pemilik[t] = key

    # Nama: kelompok nama inti yang sama persis, lalu gabungkan nama inti yang
    # mirip di dalam blok kata pertama yang sama (supaya tidak n² atas 20 ribu).
    # Nama yang seluruh katanya kata umum ("sewa mobil box", "toko bangunan
    # jaya") dipakai banyak usaha yang tidak berhubungan — bukan tanda jaringan.
    umum = set(_KATA_UMUM)
    for r in rows:
        umum.update(nama_inti(r.get("kategori")).split())
        umum.update(nama_inti(r.get("kota")).split())
        umum.update(nama_inti(r.get("area_pencarian")).split())
    per_nama = {}
    for r in rows:
        inti = nama_inti(r.get("nama_bisnis"))
        if len(inti) < 6 or nama_generik(inti, r.get("kategori")):
            continue
        if all(k in umum or k.isdigit() for k in inti.split()):
            continue
        per_nama.setdefault(inti, []).append(r["place_key"])
    if process is not None and fuzz is not None:
        blok = {}
        for inti in per_nama:
            blok.setdefault(inti.split(" ")[0], []).append(inti)
        gabung = {}
        for daftar in blok.values():
            if len(daftar) < 2:
                continue
            skor = process.cdist(daftar, daftar, scorer=fuzz.token_sort_ratio,
                                 score_cutoff=ambang_mirip)
            for i, j in zip(*skor.nonzero()):
                if i < j:
                    gabung.setdefault(daftar[i], set()).add(daftar[j])
        for a, lain in gabung.items():
            for b in lain:
                if b in per_nama and a in per_nama and b != a:
                    per_nama[a].extend(per_nama.pop(b))
    for keys in per_nama.values():
        if len(set(keys)) >= min_nama:
            for k in keys[1:]:
                satukan(keys[0], k)

    hasil = {}
    for k in induk:
        hasil.setdefault(cari(k), []).append(k)
    return {k: akar for akar, anggota in hasil.items() if len(anggota) >= 2 for k in anggota}


# ─── Cek email (MX) ───────────────────────────────────────────────────────────

def cek_domain_email(domain, timeout=3):
    """
    True bila domain bisa menerima email (punya MX, atau A sebagai cadangan),
    False bila terbukti tidak bisa, None bila tidak bisa dipastikan (DNS
    timeout / library belum terpasang). Penyedia email gratis langsung True.
    """
    d = str(domain or "").strip().lower()
    if not d:
        return None
    if d in EMAIL_GRATIS:
        return True
    if validate_email is None:
        return None
    try:
        validate_email(f"postmaster@{d}", check_deliverability=True, timeout=timeout)
        return True
    except EmailUndeliverableError as e:
        # Timeout DNS juga dilaporkan sebagai undeliverable — itu bukan bukti.
        return None if "timed out" in str(e).lower() or "timeout" in str(e).lower() else False
    except Exception:
        return None
