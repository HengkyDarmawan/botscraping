"""
scrapers/gmaps.py — Scraper Google Maps yang menghasilkan leads siap dijual.

Alur satu run:

  1. Buka halaman pencarian, scroll feed, kumpulkan kartu listing.
  2. FILTER AWAL dari kartu feed (rating & jumlah ulasan sudah terlihat di situ)
     — listing yang tidak lolos dibuang SEBELUM halamannya dibuka. Ini
     penghematan waktu terbesar dalam pipeline.
  3. Cek tiap listing ke database (db.py):
       • belum pernah ada / sudah > 6 bulan → ambil penuh sebagai lead baru
       • sudah ada < 6 bulan                → tidak diambil lagi; hanya dicek
         apakah telepon/website/email-nya berubah
  4. Periksa website tiap lead baru (scrapers/enrich.py) untuk tahu kondisi
     aset digitalnya.
  5. Hitung rekomendasi jasa + skor potensi pembeli (scrapers/scoring.py).
  6. Tulis Excel banyak sheet — isinya dibaca ULANG dari database.

Setiap lead disimpan ke database pada langkah 3, detik itu juga, bukan setelah
seluruh target selesai. Itu sebabnya file hasil dirakit dari database dan bukan
dari list di memori: run yang mati di target ke-25 dari 30 tetap meninggalkan
lead-nya utuh, tetap menghasilkan Excel, dan tetap bisa dilanjutkan.
"""
import asyncio
import json
import math
import re
import random
import sys
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd
from fake_useragent import UserAgent
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import Stealth

import db
from scrapers import enrich, komponen, kontak, kualitas, scoring

OUTPUT_DIR = Path("output")

# Kolom paling menentukan tindakan ditaruh paling kiri: begitu file dibuka,
# yang pertama terlihat adalah siapa, seberapa panas, dan mau ditawari apa.
COLUMN_ORDER = [
    "nama_bisnis", "kategori", "skor_pembeli", "tier",
    "jasa_utama", "jalur", "alasan_pitch", "jasa_pendukung",
    "telepon", "whatsapp_link", "whatsapp_lain", "email", "email_lain",
    "alamat", "kota",
    "rating", "jumlah_ulasan", "skor_popularitas",
    "website", "web_status", "web_platform", "web_https", "web_mobile",
    "web_load_ms", "web_ada_pixel", "web_ada_toko", "web_tahun_update",
    "instagram", "facebook", "tiktok", "tokopedia", "shopee", "marketplace_lain",
    "linkedin", "youtube",
    "sudah_diklaim", "status_buka", "rentang_harga", "jumlah_foto",
    "jam_operasional", "koordinat", "area_pencarian", "status_leads",
    "tanggal_scraping", "source_url",
]

# Kolom versi ringkas untuk sheet "Update Kontak".
KOLOM_UPDATE = [
    "nama_bisnis", "kategori", "perubahan", "telepon", "whatsapp_link",
    "email", "website", "alamat", "area_pencarian", "status_leads",
    "tanggal_scraping", "source_url",
]

STATUS_TUTUP = ("tutup permanen", "permanently closed",
                "tutup sementara", "temporarily closed")

# Balasan pemilik di ulasan yang menyatakan usahanya sudah berhenti. Google
# tidak selalu menandai listing seperti ini "Tutup permanen" — padahal pemiliknya
# sendiri sudah menulis begitu ("mohon maaf toko kami sudah tutup permanen").
_RE_PEMILIK_TUTUP = re.compile(
    r"tutup permanen|sudah tutup (usaha|toko|selamanya)|tidak beroperasi lagi|"
    r"berhenti beroperasi|sudah tidak (buka|beroperasi) lagi|permanently closed",
    re.I)


def _profil(params):
    """
    Profil pipeline: bagian yang berbeda antara ruang "webdev" (calon klien jasa
    website) dan "komponen" (calon pembeli komponen komputer). Browser, feed,
    ekstraksi, anti-duplikat, dan penyimpanan crash-safe dipakai bersama.
    """
    return PROFIL.get((params or {}).get("_profil") or "webdev", PROFIL["webdev"])


class _SatuHasil(Exception):
    """Pencarian langsung membuka satu halaman bisnis, bukan daftar hasil."""


# ─── Utilitas ─────────────────────────────────────────────────────────────────

def _get_apify_proxy(params=None, session_id=None):
    """
    Buat proxy config Apify dari params (web form) atau config.py sebagai fallback.
    session_id=None → UUID acak → IP baru. Session tetap → IP sama.
    Return None jika proxy dinonaktifkan.
    """
    import config as cfg
    p = params or {}
    # `use_proxy` dari form web bisa bernilai False secara sengaja. Memakai
    # `p.get(...) or cfg.USE_PROXY` membuat False itu falsy dan nilai config yang
    # menang — sakelar proxy di UI jadi tidak bisa dimatikan.
    aktif = (bool(p["use_proxy"]) if "use_proxy" in p
             else bool(getattr(cfg, "USE_PROXY", False)))
    if not aktif:
        return None
    if session_id is None:
        session_id = uuid.uuid4().hex[:10]
    token   = p.get("apify_proxy_token")   or getattr(cfg, "APIFY_PROXY_TOKEN", "")
    group   = p.get("apify_proxy_group")   or getattr(cfg, "APIFY_PROXY_GROUP", "RESIDENTIAL")
    country = p.get("apify_proxy_country") or getattr(cfg, "APIFY_PROXY_COUNTRY", "")
    parts   = [f"groups-{group}"]
    if country:
        parts.append(f"country-{country}")
    parts.append(f"session-{session_id}")
    return {
        "server":   "http://proxy.apify.com:8000",
        "username": ",".join(parts),
        "password": token,
    }


async def _random_delay(mn=2.0, mx=4.0):
    await asyncio.sleep(random.uniform(mn, mx))


# Stealth dibuat SEKALI. Objeknya tidak menyimpan state per-halaman, jadi
# membuatnya ulang untuk tiap listing hanya menambah pekerjaan.
_STEALTH = Stealth()

# Aset yang tidak pernah dibaca scraper ini: foto bisnis, tile peta, video, font.
# Di halaman detail Google Maps inilah bagian terbesar byte yang diunduh.
#
# `stylesheet` SENGAJA tidak diblokir. Ekstraksi memakai inner_text, yang
# bergantung pada layout — tanpa CSS, elemen yang normalnya disembunyikan ikut
# terbaca di div[role="main"] dan mengotori status_buka / rentang_harga.
_ASET_DIBLOKIR = {"image", "media", "font"}


async def _blokir_aset(context):
    async def _rute(route):
        try:
            if route.request.resource_type in _ASET_DIBLOKIR:
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            # Halaman sudah ditutup di tengah request — bukan kesalahan yang
            # perlu menghentikan apa pun.
            pass
    await context.route("**/*", _rute)


async def _buat_context(browser, ua, params, session_id=None):
    """Context browser siap pakai: proxy, stealth, dan pemblokiran aset berat."""
    context = await browser.new_context(
        user_agent=ua, locale="id-ID",
        timezone_id="Asia/Jakarta", viewport={"width": 1366, "height": 768},
        proxy=_get_apify_proxy(params, session_id=session_id),
    )
    await _STEALTH.apply_stealth_async(context)
    if params.get("blokir_gambar", True):
        await _blokir_aset(context)
    return context


def _konkuren_listing(params):
    """
    Berapa halaman listing dibuka bersamaan.

    0/kosong = otomatis. Tanpa proxy semua permintaan keluar dari satu IP dan
    membuka terlalu banyak sekaligus memancing blokir Google; dengan proxy Apify
    tiap context punya IP sendiri sehingga batasnya bisa dinaikkan.
    """
    minta = int(params.get("listing_konkuren") or 0)
    if minta > 0:
        return max(1, min(minta, 12))
    return 8 if _get_apify_proxy(params) else 5


# Nomor telepon & skor popularitas hidup di scrapers/scoring.py supaya ada satu
# sumber kebenaran. Nama lama dipertahankan sebagai alias karena modul lain
# (scrapers/website_leads.py) sudah mengimpornya.
_hitung_score = scoring.skor_popularitas
_is_wa = scoring.nomor_wa_valid
_wa_link = scoring.link_wa


def _has_website(row):
    """
    False jika website None/kosong/'-'/'n/a', ATAU tautannya cuma ke sosmed.

    Tautan Instagram/marketplace/Linktree sengaja tidak dihitung sebagai
    website, supaya filter "hanya bisnis TANPA website" sepakat dengan
    enrich.website_efektif() dan scoring.skor_pembeli() yang sama-sama
    menganggap bisnis seperti itu belum punya aset sendiri (+28 poin BUTUH).
    Sebelum ini filternya justru membuang mereka — persis lead yang paling
    pantas ditawari landing page sekaligus jasa kelola sosmed.
    """
    w = str(row.get("website") or "").strip().lower()
    if not w or w in ("-", "n/a", "none"):
        return False
    # Listing OTA / link booking Google juga bukan website milik bisnisnya.
    return not (enrich.url_sosmed(w) or enrich.url_pinjaman(w))


def _radius_km_to_zoom(km):
    """Konversi radius km -> level zoom GMaps (perkiraan). None jika km<=0/invalid."""
    try:
        km = float(km)
    except (ValueError, TypeError):
        return None
    if km <= 0:
        return None
    for r, z in [(1, 15), (2, 14), (4, 13), (8, 12), (15, 11), (30, 10), (60, 9)]:
        if km <= r:
            return z
    return 8


def _build_search_url(query, coords, radius_km):
    """URL pencarian GMaps; sisipkan viewport @lat,lng,zoom bila koordinat valid
    dan radius aktif untuk membatasi area."""
    base = f"https://www.google.com/maps/search/{urllib.parse.quote_plus(query)}"
    coords = (coords or "").strip()
    zoom = _radius_km_to_zoom(radius_km)
    if coords and zoom and re.fullmatch(r"-?\d+\.\d+,\s*-?\d+\.\d+", coords):
        coords = re.sub(r"\s+", "", coords)
        return f"{base}/@{coords},{zoom}z"
    return base


def _koordinat_dari_href(href):
    """Ambil koordinat bisnis yang tertanam di URL listing."""
    m = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", str(href or ""))
    return f"{m.group(1)},{m.group(2)}" if m else ""


# Mode kontak: satu pilihan menggantikan pasangan sakelar require_phone +
# require_whatsapp. Daftar mode & aturannya tinggal di scrapers/kontak.py, dipakai
# kedua ruang (klien website & distributor komponen).
MODE_WA = kontak.MODE_WA


def _mode_kontak(f, bawaan=kontak.MODE_BEBAS):
    """
    Baca mode kontak dari dict filter, dengan terjemahan dari sakelar lama.

    config.py dan run lama masih mengirim require_phone/require_whatsapp, jadi
    keduanya tetap dihormati selama `mode_kontak` tidak diisi.
    """
    mode = str(f.get("mode_kontak") or "").strip().lower()
    if mode in kontak.MODE_KONTAK:
        return mode
    if f.get("require_whatsapp"):
        return kontak.MODE_WA
    if f.get("require_phone"):
        return kontak.MODE_TELEPON
    return bawaan


def _syarat_non_kontak(row, f):
    """
    Alasan lead gugur syarat NON-kontak (rating, ulasan, "hanya tanpa website"),
    atau None. Dicek di halaman detail sebelum disimpan. Tidak dicatat di daftar
    lewati: syarat ini berubah-ubah antar run.
    """
    if f.get("filter_rating"):
        nilai = _angka_atau_none(row.get("rating"))
        if nilai is not None and nilai < f.get("min_rating", 0):
            return f"rating < {f.get('min_rating', 0)}"
    if f.get("filter_reviews"):
        nilai = _angka_atau_none(row.get("jumlah_ulasan"))
        if nilai is not None and nilai < f.get("min_reviews", 0):
            return f"ulasan < {f.get('min_reviews', 0)}"
    if f.get("require_no_website") and _has_website(row):
        return "sudah punya website"
    return None


def _passes_filter(row, f):
    """
    Alasan lead ini dibuang filter akhir, atau None bila lolos.

    Mengembalikan alasan (bukan cuma True/False) supaya run yang menghasilkan nol
    lead bisa menjelaskan ke mana perginya semua listing, bukan sekadar melapor
    "0 hasil" dan menyuruh user menebak filter mana yang terlalu ketat.
    """
    # Angka yang KOSONG berarti tidak berhasil dibaca, bukan bernilai nol.
    # Dulu keduanya diperlakukan sama (`or 0`), sehingga lead yang jumlah
    # ulasannya tidak tertera dibuang seolah-olah belum punya ulasan sama sekali.
    # Google sekarang memang tidak lagi menampilkan jumlah ulasan di kartu feed,
    # jadi aturan lama itu membuang lead yang sempurna hanya karena satu angka
    # tidak terbaca. Aturannya disamakan dengan filter awal: buang hanya kalau
    # datanya ADA dan jelas tidak lolos.
    alasan = _syarat_non_kontak(row, f)
    if alasan:
        return alasan
    kurang = kontak.kontak_kurang(row, _mode_kontak(f))
    if kurang:
        return kurang
    if not f.get("sertakan_tutup") and _bisnis_tutup(row):
        return "bisnis tutup"
    return None


def _angka_atau_none(v):
    """Angka dari nilai apa pun; None kalau kosong atau tidak bisa dibaca."""
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _bisnis_tutup(row):
    s = str(row.get("status_buka") or "").lower()
    return any(t in s for t in STATUS_TUTUP)


# ─── Kartu feed (untuk filter awal) ───────────────────────────────────────────

# Rating & jumlah ulasan di kartu feed. Dua bentuk yang dipakai Google:
#   "4,7(1.234)" → ada ulasan
#   "5,0"        → ada rating tapi jumlah ulasan tidak ditampilkan
_RE_KARTU_LENGKAP = re.compile(r"(\d+[.,]\d)\s*\(\s*([\d.,]+)\s*\)")
_RE_KARTU_RATING = re.compile(r"^\s*(\d[.,]\d)\s*$", re.MULTILINE)


def _baca_rating_kartu(teks):
    """
    Ambil (rating, jumlah_ulasan) dari teks kartu feed.

    Kartu feed adalah sumber paling andal untuk kedua angka ini: teksnya polos
    dan tidak bergantung pada nama kelas CSS Google yang berubah-ubah. Halaman
    detail justru sering tidak menampilkan jumlah ulasan sama sekali (mis. pada
    listing hotel), jadi kartu dijadikan acuan utama.

    Mengembalikan (None, None) bila tidak terbaca — pemanggil tidak boleh
    membuang listing hanya karena angkanya tidak terbaca.
    """
    teks = teks or ""
    m = _RE_KARTU_LENGKAP.search(teks)
    if m:
        try:
            return (float(m.group(1).replace(",", ".")),
                    int(re.sub(r"\D", "", m.group(2)) or 0))
        except ValueError:
            return None, None
    m = _RE_KARTU_RATING.search(teks)
    if m:
        try:
            # Rating terbaca tapi Google tidak menampilkan jumlah ulasannya.
            # Dikembalikan sebagai None (tidak diketahui), BUKAN 0: menganggapnya
            # 0 akan membuat filter "minimal N ulasan" membuang listing ini
            # sebelum halamannya sempat dibuka, padahal bisa jadi lead bagus.
            return float(m.group(1).replace(",", ".")), None
        except ValueError:
            return None, None
    return None, None

# Kartu feed menyimpan jauh lebih banyak daripada yang dulu dipanen. Contoh nyata
# satu kartu (Depok, Agustus 2026):
#
#   Bengkel Dokter Mobil Depok
#   4,6
#   Bengkel Mobil · Jl. Raya Sawangan Blk. K No.kav.286
#   Tutup · Buka Jum pukul 09.00 · 0813-1899-8477
#   Situs Web · Rute
#
# Telepon, kategori, alamat, status buka, dan ADA/TIDAKNYA website semuanya sudah
# terlihat. Karena itu tautan keluar ikut diambil: kehadirannya adalah bukti pasti
# bisnis ini punya website, dan itu satu halaman detail yang tidak perlu dibuka.
_JS_KARTU = """() => {
  const out = [];
  document.querySelectorAll('div[role="feed"] a[href*="/maps/place/"]').forEach(a => {
    const kartu = a.closest('div[role="feed"] > div') || a.parentElement;
    let situs = '';
    if (kartu) {
      const luar = [...kartu.querySelectorAll('a[href]')]
        .filter(x => x.href && !x.href.includes('/maps/') && !x.href.startsWith('javascript'));
      // Tombol "Situs Web" diberi label oleh Google. Tautan luar lain di kartu
      // (pesan antar, reservasi, booking) BUKAN website bisnisnya.
      const berlabel = luar.find(x => /situs web|website/i.test(
        (x.getAttribute('data-value') || '') + ' ' + (x.getAttribute('aria-label') || '')));
      const bukanPesan = luar.find(x => !/pesan|order|reserv|booking|menu|janji/i.test(
        (x.getAttribute('data-value') || '') + ' ' + (x.getAttribute('aria-label') || '')));
      situs = (berlabel || bukanPesan || {}).href || '';
    }
    out.push({
      href: a.href,
      nama: a.getAttribute('aria-label') || '',
      teks: kartu ? kartu.innerText : '',
      situs: situs
    });
  });
  return out;
}"""

# Nomor telepon Indonesia sebagaimana ditampilkan Google di kartu:
#   0813-1899-8477 · 0881-2577-793 · (021) 78880202 · +62 812-3456-7890
_RE_TELEPON_KARTU = re.compile(
    r"(?:\+62|\(0\d{2,4}\)|0)[\d\s().-]{6,15}\d"
)


def _baca_telepon_kartu(teks):
    """
    Nomor telepon dari teks kartu feed, atau None bila tidak ada.

    Dipakai hanya untuk MEMBUANG lebih awal (nomor kabel saat yang dicari WA),
    tidak pernah untuk meloloskan: kartu yang tidak menampilkan nomor belum tentu
    tidak punya nomor, halaman detailnya yang menentukan.
    """
    for baris in str(teks or "").splitlines():
        # Baris jam operasional penuh angka dan mudah tertukar dengan nomor.
        if "Buka" in baris or "Tutup" in baris:
            # Nomor biasanya di ujung baris ini, sesudah "·".
            baris = baris.split("·")[-1]
        m = _RE_TELEPON_KARTU.search(baris)
        if m:
            kandidat = m.group().strip()
            if len(re.sub(r"\D", "", kandidat)) >= 8:
                return kandidat
    return None


def _rapikan_nomor(nomor):
    """
    "0813-1899-8477" / "(021) 78880202" → "081318998477" / "02178880202".

    Halaman detail memberi nomor tanpa pemisah; kartu feed memberinya dengan
    tanda hubung. Tanpa penyeragaman ini, bisnis yang sama tersimpan dengan dua
    bentuk nomor berbeda tergantung mode, dan refresh_contacts membacanya sebagai
    "nomor berubah" pada tiap run.
    """
    nomor = str(nomor or "").strip()
    if not nomor:
        return None
    awalan = "+" if nomor.startswith("+") else ""
    digit = re.sub(r"\D", "", nomor)
    return (awalan + digit) if digit else None


def _baca_alamat_kartu(teks):
    """
    Potongan alamat dari kartu feed — bagian sesudah "·" pada baris kategori.

    Google memotongnya ("Jl. Raya Sawangan Blk. K No.kav.286", tanpa kelurahan
    dan kota), jadi ini cadangan untuk Mode Cepat, bukan pengganti alamat penuh
    dari halaman detail.
    """
    for baris in str(teks or "").splitlines():
        if "·" not in baris or "Buka" in baris or "Tutup" in baris:
            continue
        # Google kadang menyisipkan segmen kosong (ikon aksesibilitas):
        # "Toko Komputer ·  · Mangga Dua Mall, ..." — ambil segmen berisi pertama.
        bagian = [b.strip() for b in baris.split("·")]
        for b in bagian[1:]:
            if b:
                return b
    return None


def _baca_kategori_kartu(teks):
    """Kategori bisnis = potongan pertama pada baris yang memuat '·' setelah rating."""
    for baris in str(teks or "").splitlines():
        if "·" not in baris:
            continue
        if "Buka" in baris or "Tutup" in baris:
            continue
        kandidat = baris.split("·")[0].strip()
        # Alamat hampir selalu diawali "Jl."/"Jalan"; kategori tidak.
        if kandidat and not re.match(r"(?i)^(jl\.?|jalan|gg\.?)\b", kandidat):
            return kandidat
    return None


async def _kartu_feed(page, max_results):
    """
    Ambil semua yang bisa dibaca dari kartu di feed pencarian.

    Dipakai untuk membuang listing yang jelas tidak lolos filter sebelum
    halaman detailnya dibuka. Semuanya dibaca dari teks kartu (bukan nama kelas
    CSS Google yang berubah-ubah), jadi relatif tahan terhadap perubahan UI.

    Field yang tidak terbaca dikembalikan None — pemanggil TIDAK boleh membuang
    kartu karenanya, supaya kegagalan baca tidak pernah menghilangkan lead.
    """
    try:
        mentah = await page.evaluate(_JS_KARTU)
    except Exception:
        # Ekstraksi kartu gagal total — jatuh ke cara lama (href saja).
        els = await page.query_selector_all('div[role="feed"] a[href*="/maps/place/"]')
        mentah = [{"href": h, "nama": "", "teks": "", "situs": ""}
                  for el in els if (h := await el.get_attribute("href"))]

    kartu = []
    dilihat = set()
    for m in mentah:
        href = m.get("href") or ""
        if not href or href in dilihat:
            continue
        dilihat.add(href)
        teks = m.get("teks") or ""
        rating, ulasan = _baca_rating_kartu(teks)
        kartu.append({
            "href": href,
            "nama": (m.get("nama") or "").strip(),
            "rating": rating,
            "jumlah_ulasan": ulasan,
            "telepon": _rapikan_nomor(_baca_telepon_kartu(teks)),
            "kategori": _baca_kategori_kartu(teks),
            "alamat": _baca_alamat_kartu(teks),
            "website": (m.get("situs") or "").strip(),
            "tutup": _status_tutup_teks(teks),
        })
        if max_results and len(kartu) >= max_results:
            break
    return kartu


def _status_tutup_teks(teks):
    """
    "Tutup Permanen" / "Tutup Sementara" bila teks kartu memuat status itu,
    selain itu "". Kartu feed tidak memuat ulasan, jadi pencocokan teks di sini
    aman — beda dengan halaman detail (lihat _extract_sinyal_profil).
    """
    low = str(teks or "").lower()
    if "tutup permanen" in low or "permanently closed" in low:
        return "Tutup Permanen"
    if "tutup sementara" in low or "temporarily closed" in low:
        return "Tutup Sementara"
    return ""


def _lolos_filter_awal(kartu, f):
    """
    True bila kartu ini layak dibuka halaman detailnya.

    Hanya memeriksa syarat yang jawabannya sudah PASTI dari kartu. Semua
    pemeriksaan di sini satu arah: kartu hanya dibuang kalau datanya ada dan
    jelas tidak lolos. Data yang tidak terbaca selalu diloloskan — lebih baik
    membuka satu halaman ekstra daripada kehilangan lead.

    Ini titik hemat terbesar di seluruh pipeline. Dengan filter "belum punya
    website", sekitar tiga dari empat kartu bisa dibuang di sini — dulu keempatnya
    dibuka dulu (±10 detik masing-masing) baru tiga di antaranya dibuang.
    """
    if f.get("filter_rating") and kartu.get("rating") is not None:
        if kartu["rating"] < f.get("min_rating", 0):
            return False
    if f.get("filter_reviews") and kartu.get("jumlah_ulasan") is not None:
        if kartu["jumlah_ulasan"] < f.get("min_reviews", 0):
            return False
    # Tautan keluar di kartu = bukti punya website — kecuali kalau tautannya
    # cuma ke Instagram/marketplace, yang tidak dihitung sebagai website.
    if f.get("require_no_website") and _has_website(kartu):
        return False
    # Nomor yang terlihat di kartu tapi jelas bukan seluler (mis. "(021) 788...").
    # Hanya dibuang kalau website TIDAK akan diperiksa: banyak bisnis yang di
    # Google Maps mencantumkan telepon kantor ternyata punya nomor WA di
    # websitenya — keputusan untuk mereka ditunda sampai website dicek.
    if (not f.get("_enrich") and _mode_kontak(f) == kontak.MODE_WA
            and kartu.get("telepon")):
        if not _is_wa(kartu["telepon"]):
            return False
    if not f.get("sertakan_tutup") and kartu.get("tutup"):
        return False
    # Klien Website: Blogspot, fasilitas umum, nama generik — sudah pasti dari
    # kartu, jadi halaman detailnya tidak perlu dibuka.
    if f.get("saring_kualitas") and kualitas.alasan_kartu(kartu):
        return False
    # Bisnis yang pada run sebelumnya sudah dinyatakan tutup, tidak punya kontak
    # yang diminta (mode yang sama), atau dihapus user — tidak perlu dibuka lagi.
    lewati = f.get("_lewati")
    if lewati and lewati(kartu):
        return False
    # Ruang komponen: kategori yang jelas bukan pembeli komponen (audio mobil,
    # bengkel, toko ponsel, ...) — kategorinya sudah terlihat di kartu.
    if f.get("_kecualikan") and komponen.dikecualikan(
            kartu.get("kategori"), kartu.get("nama"), f["_kecualikan"]):
        return False
    return True


# ─── Mode Cepat: cukupkah kartu feed? ─────────────────────────────────────────

# Field yang HANYA ada di halaman detail. Didaftar terpisah supaya jelas apa yang
# dikorbankan Mode Cepat — dan supaya _lengkapi_dari_db bisa mengisinya kembali
# dari database kalau bisnis ini pernah diambil penuh sebelumnya.
# `jumlah_ulasan` ikut di sini sejak Google berhenti menampilkannya di kartu
# feed (diperiksa Agustus 2026: kartu hanya menyisakan aria-label "4,6 bintang",
# tanpa angka dalam kurung seperti dulu). Ini yang paling terasa, karena jumlah
# ulasan ikut menentukan skor popularitas — karena itu Mode Cepat menghasilkan
# skor pembeli yang lebih konservatif untuk bisnis yang belum pernah diambil.
FIELD_HANYA_DETAIL = ("jam_operasional", "sudah_diklaim", "rentang_harga",
                      "jumlah_foto", "jumlah_ulasan")


def _kartu_cukup(kartu):
    """
    True bila kartu feed sudah memuat semua yang menentukan sebuah lead: nama,
    kategori, telepon, dan rating.

    Sengaja ketat. Kartu yang kurang satu saja tetap dibuka halaman detailnya —
    Mode Cepat menghemat waktu, ia tidak boleh mengorbankan lead.
    """
    return bool(kartu.get("nama") and kartu.get("kategori")
                and kartu.get("telepon") and kartu.get("rating") is not None)


def _detail_dari_kartu(kartu):
    """
    Bentuk hasil yang sama dengan _extract_detail, tapi dirakit dari kartu feed.

    Field yang cuma ada di halaman detail dikembalikan None — BUKAN string kosong.
    None berarti "tidak diperiksa kali ini", dan itulah yang membuat
    db.upsert_business tidak menimpa nilai lama yang sudah tersimpan.
    """
    return {
        "nama_bisnis": kartu.get("nama"),
        "kategori": kartu.get("kategori"),
        "rating": kartu.get("rating"),
        "jumlah_ulasan": kartu.get("jumlah_ulasan"),
        "telepon": kartu.get("telepon"),
        "alamat": kartu.get("alamat"),
        "website": kartu.get("website") or None,
        "jam_operasional": None,
        "sudah_diklaim": None,
        "status_buka": kartu.get("tutup") or "",
        "rentang_harga": "",
        "jumlah_foto": None,
    }


# ─── Scroll feed sampai kuota terpenuhi ───────────────────────────────────────

# Dua batas keras: tanpa ini, filter yang hampir tidak ada yang lolos (mis. rating
# 4.8 di area kecil) akan membuat scroll berjalan sampai feed Google habis.
MAKS_PUTARAN_SCROLL = 30
MAKS_LIPAT_KARTU = 8

# Jeda antar listing, dijalankan DI LUAR semaphore (lihat _kerjakan). Karena ia
# tidak lagi menahan slot konkurensi, jeda sepanjang beberapa detik tidak lagi
# ada gunanya — pacing yang sebenarnya datang dari batas jumlah listing serentak.
JEDA_LISTING_MIN = 0.3
JEDA_LISTING_MAX = 1.0

# Perkiraan waktu satu listing sesudah semua penghematan (blokir gambar, context
# dipakai ulang, jeda sia-sia dibuang). Dipakai untuk melaporkan berapa lama yang
# dihemat filter awal.
DETIK_PER_LISTING = 3.5


async def _kumpulkan_kartu(page, max_results, filters, cb, should_stop=None):
    """
    Scroll feed sampai terkumpul `max_results` kartu yang LOLOS filter awal.

    Dulu scroll berhenti begitu JUMLAH KARTU mencapai max_results dan pemotongan
    dilakukan di angka yang sama, sementara filter awal baru berjalan sesudahnya.
    Artinya "maks 20 hasil" sebenarnya berarti "20 kartu pertama yang kebetulan
    terlihat" — dengan filter rating 4.0 + minimal 5 ulasan, yang benar-benar
    dibuka sering tinggal 5-10. Sekarang yang dihitung adalah kartu yang lolos,
    jadi angka yang diminta user berarti angka yang dia dapat.

    Mengembalikan SELURUH kartu yang terkumpul (belum difilter) supaya pemanggil
    tetap bisa melaporkan berapa yang dibuang filter awal.
    """
    FEED = 'div[role="feed"]'
    END_TEXTS = ["You've reached the end", "Anda telah mencapai akhir"]
    batas_kartu = max_results * MAKS_LIPAT_KARTU if max_results else 0

    kartu = []
    stale = 0
    prev = 0
    for _ in range(MAKS_PUTARAN_SCROLL):
        kartu = await _kartu_feed(page, None)
        lolos = sum(1 for k in kartu if _lolos_filter_awal(k, filters))

        if max_results and lolos >= max_results:
            break
        if should_stop and should_stop():
            break
        if batas_kartu and len(kartu) >= batas_kartu:
            cb(None, f"⚠ Berhenti scroll di {len(kartu)} listing — baru {lolos} yang "
                     f"lolos filter awal. Filter untuk area ini terlalu ketat.", 0)
            break

        try:
            last = await page.query_selector(f"{FEED} > div:last-child")
            if last:
                txt = await last.inner_text()
                if any(p.lower() in txt.lower() for p in END_TEXTS):
                    break
        except Exception:
            pass

        if len(kartu) == prev:
            stale += 1
            if stale >= 2:
                break
        else:
            stale = 0
        prev = len(kartu)

        await page.evaluate(
            "(s)=>{const e=document.querySelector(s);if(e)e.scrollTop=e.scrollHeight;}", FEED
        )
        cb(None, f"Scroll... {lolos}/{max_results or '∞'} lolos filter awal "
                 f"({len(kartu)} listing termuat)", 0)
        await _random_delay(0.8, 1.6)

    return kartu


# ─── Ekstrak detail ───────────────────────────────────────────────────────────

# Tombol info di halaman detail (alamat, jam, plus code) diawali ikon dari font
# ikon Google — karakter Private Use Area, mis. "\nJl. ...". Di Word/Excel
# ikon itu tampil sebagai kotak kosong, jadi dibuang sebelum disimpan.
_RE_IKON = re.compile(r"[-]")


async def _extract_detail(page):
    async def st(sel, fb=None):
        try:
            el = await page.query_selector(sel)
            if el:
                return _RE_IKON.sub("", await el.inner_text()).strip()
        except Exception:
            pass
        return await st(fb) if fb else None

    async def sa(sel, attr, fb=None):
        try:
            el = await page.query_selector(sel)
            if el:
                v = await el.get_attribute(attr)
                return v.strip() if v else None
        except Exception:
            pass
        return await sa(fb, attr) if fb else None

    nama = await st("h1.DUwDvf", "h1")
    kategori = await st("div.DkEaL", "button.DkEaL")
    rating_raw = await st('div.F7nice span[aria-hidden="true"]')
    rating = rating_raw.replace(",", ".") if rating_raw else None

    jumlah_ulasan = await _extract_jumlah_ulasan(page)

    phone_id = await sa('[data-item-id^="phone:tel:"]', "data-item-id")
    telepon = phone_id.replace("phone:tel:", "").strip() if phone_id else None
    alamat = await st('button[data-item-id="address"]')
    website = await sa('a[data-item-id="authority"]', "href")
    jam = await st('[data-item-id*="oh"]', 'div[class*="o0Svhf"]')
    plus_code = await st('[data-item-id="oloc"]')

    tambahan = await _extract_sinyal_profil(page)

    return {
        "nama_bisnis": nama, "kategori": kategori, "rating": rating,
        "jumlah_ulasan": jumlah_ulasan, "telepon": telepon,
        "alamat": alamat, "website": website, "jam_operasional": jam,
        "kota": _kota_dari_teks(plus_code) or _kota_dari_teks(alamat),
        **tambahan,
    }


_RE_KOTA = re.compile(r"\b((?:Kota|Kabupaten|Kab\.)\s+[A-Z][\w .'-]*?)(?=,|$)")


def _kota_dari_teks(teks):
    """
    "Kota Jakarta Pusat" / "Kabupaten Bogor" dari baris plus code halaman detail
    ("VR7F+48 Mangga Dua Sel., Kota Jakarta Pusat, Daerah Khusus ...").
    """
    m = _RE_KOTA.search(str(teks or ""))
    return m.group(1).strip() if m else None


async def _extract_jumlah_ulasan(page):
    """
    Jumlah ulasan dari halaman detail.

    Google memindahkan angka ini keluar dari `div.F7nice`, jadi selector lama
    (`div.F7nice span[aria-label*="ulasan"]`) selalu mengembalikan kosong dan
    membuat semua lead terlihat punya 0 ulasan. Sekarang dicari di blok induk
    rating, dengan beberapa lapis cadangan.

    Halaman detail memuat banyak kartu "tempat serupa" yang juga memakai kelas
    yang sama, jadi pencarian SELALU dibatasi pada blok rating utama — kalau
    tidak, angka milik bisnis lain bisa terbaca sebagai milik lead ini.

    Return None bila tidak terbaca; pemanggil memakai angka dari kartu feed.
    """
    try:
        hasil = await page.evaluate("""() => {
          const f7 = document.querySelector('div.F7nice');
          if (!f7) return null;
          const blok = f7.parentElement || f7;
          const el = blok.querySelector('span.UY7F9')
                  || blok.querySelector('[aria-label*="ulasan"], [aria-label*="review"]');
          if (el) return el.getAttribute('aria-label') || el.innerText;
          return blok.innerText || null;
        }""")
    except Exception:
        return None
    if not hasil:
        return None
    m = re.search(r"\(?\s*([\d][\d.,]*)\s*\)?\s*(?:ulasan|review)", str(hasil), re.I)
    if not m:
        m = re.search(r"\(\s*([\d][\d.,]*)\s*\)", str(hasil))
    if not m:
        return None
    d = re.sub(r"\D", "", m.group(1))
    return int(d) if d else None


# Dibaca sekali per halaman detail. Setiap sinyal dibatasi pada elemen yang
# tepat — halaman detail juga memuat ulasan dan kartu "tempat serupa" yang teksnya
# tidak boleh terbaca sebagai milik bisnis ini.
_JS_SINYAL = r"""() => {
  const main = document.querySelector('div[role="main"]');
  if (!main) return null;
  const h1 = main.querySelector('h1') || document.querySelector('h1');
  let head = h1 ? h1.closest('div.TIHn2') : null;
  if (!head && h1) {
    head = h1;
    for (let i = 0; i < 3 && head.parentElement; i++) head = head.parentElement;
  }
  const persis = re => [...main.querySelectorAll('a, button, span, div')]
    .some(e => e.children.length <= 2 && re.test((e.innerText || '').trim()));
  const teks = main.innerText || '';
  const balasan = [];
  const re = /Tanggapan dari pemilik[^\n]*\n+([^\n]+)/g;
  let m;
  while ((m = re.exec(teks)) !== null && balasan.length < 20) balasan.push(m[1]);
  return {
    main: teks.slice(0, 20000),
    header: head ? head.innerText : '',
    balasan: balasan,
    klaim: persis(/^(klaim bisnis ini|claim this business|own this business\??)$/i)
           || !!main.querySelector('a[href*="business.google.com"]'),
    tambah_foto: persis(/^(tambahkan foto|tambah foto|add a photo|add photos)$/i),
  };
}"""


async def _extract_sinyal_profil(page):
    """
    Sinyal kualitas listing yang menentukan peluang penjualan:

      sudah_diklaim  — listing yang BELUM diklaim pemiliknya masih menampilkan
                       tautan "Klaim bisnis ini". Ini sinyal terkuat untuk jasa
                       Google Business Profile: pemiliknya belum memegang
                       kendali atas listing-nya sendiri.
                       True di sini berarti "tidak ditemukan tautan klaim",
                       yang pada praktiknya berarti sudah diklaim.
      status_buka    — "Tutup permanen"/"Tutup sementara"; lead mati harus
                       dibuang, bukan ditelepon.
      rentang_harga  — "Rp"–"RpRpRp", proksi kasar daya beli pelanggannya.
      jumlah_foto    — 0 bila listing masih menampilkan ajakan "Tambahkan foto"
                       (profil terbengkalai). None bila tidak bisa dipastikan.
    """
    hasil = {"sudah_diklaim": None, "status_buka": "",
             "rentang_harga": "", "jumlah_foto": None}
    try:
        info = await page.evaluate(_JS_SINYAL)
    except Exception:
        info = None
    info = info or {}
    teks = info.get("main") or ""

    if teks:
        hasil["sudah_diklaim"] = not info.get("klaim")
        # Status resmi Google ada di blok judul (div.TIHn2: nama, rating,
        # kategori, lalu "Tutup permanen"). Dulu seluruh panel dibaca, sehingga
        # ulasan pelanggan "apa sudah tutup permanen?" menandai bisnis yang
        # masih buka sebagai tutup — dan membuangnya dari hasil.
        hasil["status_buka"] = _status_tutup_teks(info.get("header"))
        if not hasil["status_buka"]:
            # Sinyal kedua: pemilik sendiri menulis di balasan ulasan bahwa
            # usahanya sudah berhenti. Hanya balasan PEMILIK yang dipercaya.
            for balasan in info.get("balasan") or []:
                if _RE_PEMILIK_TUTUP.search(balasan):
                    hasil["status_buka"] = "Tutup Permanen (menurut pemilik)"
                    break
        if info.get("tambah_foto"):
            hasil["jumlah_foto"] = 0

    try:
        harga_el = await page.query_selector(
            '[aria-label*="Rentang harga"], [aria-label*="Price range"], '
            '[aria-label*="harga" i]'
        )
        if harga_el:
            lbl = await harga_el.get_attribute("aria-label")
            m = re.search(r"(Rp+|\$+)", lbl or "")
            if m:
                hasil["rentang_harga"] = m.group(1)
    except Exception:
        pass
    if not hasil["rentang_harga"] and teks:
        m = re.search(r"·\s*(Rp{1,4})\s*(?:·|\n)", teks)
        if m:
            hasil["rentang_harga"] = m.group(1)

    return hasil


async def _dismiss_consent(page):
    for sel in ['button[aria-label="Accept all"]', 'button[aria-label="Terima semua"]',
                'form[action*="consent"] button', "#L2AGLb"]:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                await _random_delay(1.0, 2.0)
                return
        except Exception:
            continue


# ─── Kolam context: dipakai ulang, bukan dibuang tiap listing ────────────────

# Berapa listing dibuka satu context sebelum IP-nya dirotasi. Hanya berarti kalau
# proxy Apify menyala — tanpa proxy, context baru tetap memakai IP yang sama,
# jadi merotasinya cuma membuang waktu.
ROTASI_TIAP = 10


class _KolamContext:
    """
    Sekumpulan context browser yang dipakai bergantian oleh worker listing.

    Sebelumnya tiap listing membuat context + stealth sendiri lalu membuangnya:
    ±1-1,5 detik overhead per listing, dan cache HTTP yang tidak pernah sempat
    terpakai. Sekarang satu context melayani banyak listing, dan yang dibuka lalu
    ditutup hanya halamannya.

    Ukuran kolam = jumlah listing konkuren, jadi peminjaman tidak pernah antre.
    """

    def __init__(self, browser, ua, params, ukuran):
        self._browser = browser
        self._ua = ua
        self._params = params
        self._antre = asyncio.Queue()
        self._semua = []
        for i in range(max(1, int(ukuran))):
            slot = {"no": i, "ctx": None, "dipakai": 0, "putaran": 0}
            self._semua.append(slot)
            self._antre.put_nowait(slot)
        # Rotasi IP hanya masuk akal kalau proxy benar-benar aktif.
        self._rotasi = (bool(params.get("rotate_ip_per_listing", True))
                        and _get_apify_proxy(params) is not None)

    async def pinjam(self):
        """Satu slot siap pakai; context-nya dibuat saat pertama dibutuhkan."""
        slot = await self._antre.get()
        if slot["ctx"] is None:
            sid = f"listing-{slot['no']}-{slot['putaran']}"
            slot["ctx"] = await _buat_context(self._browser, self._ua,
                                              self._params, session_id=sid)
            slot["dipakai"] = 0
        return slot

    async def kembalikan(self, slot, buang=False):
        """
        Kembalikan slot ke kolam. `buang=True` untuk context yang mungkin sudah
        rusak — lebih murah membuat context baru daripada mewariskan halaman
        yang menggantung ke listing berikutnya.
        """
        slot["dipakai"] += 1
        if buang or (self._rotasi and slot["dipakai"] >= ROTASI_TIAP):
            await self._tutup(slot)
        self._antre.put_nowait(slot)

    async def _tutup(self, slot):
        ctx, slot["ctx"] = slot["ctx"], None
        slot["dipakai"] = 0
        slot["putaran"] += 1
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass

    async def tutup_semua(self):
        for slot in self._semua:
            await self._tutup(slot)


# ─── Buka satu listing ────────────────────────────────────────────────────────

async def _buka_listing(kolam, href, percobaan=2):
    """
    Buka halaman detail satu bisnis memakai context dari kolam.

    Dicoba ulang sekali kalau gagal: di koneksi yang tidak stabil, satu percobaan
    yang gagal berarti satu lead hilang percuma. Percobaan kedua selalu memakai
    context yang baru — kegagalan pertama bisa datang dari context yang sudah
    rusak, bukan dari jaringannya.
    """
    galat = None
    for percobaan_ke in range(1, percobaan + 1):
        slot = await kolam.pinjam()
        page = None
        buang = False
        try:
            page = await slot["ctx"].new_page()
            await page.goto(href, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_selector("h1", timeout=15_000)
            # `h1` muncul lebih dulu daripada blok rating, jadi menunggu h1 saja
            # membuat rating & jumlah ulasan sering terbaca kosong. Dulu jeda acak
            # 2-4 detik menutupi itu secara kebetulan; menunggu elemennya secara
            # langsung jauh lebih cepat DAN lebih andal. Ketiadaannya bukan
            # kesalahan — ada listing yang memang belum punya rating sama sekali.
            try:
                await page.wait_for_selector("div.F7nice", timeout=6_000)
            except PlaywrightTimeoutError:
                pass
            data = await _extract_detail(page)
            return data, None
        except PlaywrightTimeoutError:
            galat = f"timeout (percobaan {percobaan_ke}/{percobaan})"
            buang = percobaan_ke < percobaan
        except Exception as e:
            galat = f"{type(e).__name__}: {e}"
            buang = True
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    buang = True
            await kolam.kembalikan(slot, buang=buang)
        if percobaan_ke < percobaan:
            await _random_delay(1.0, 2.0)
    return None, galat


# Data hasil pemeriksaan website & kontak tambahan: tetap berlaku walau run
# sekarang tidak mengambilnya (mis. enrichment dimatikan).
_FIELD_WARISAN = (
    "web_status", "web_https", "web_mobile", "web_load_ms", "web_platform",
    "web_tahun_update", "web_ada_pixel", "web_ada_toko",
    "email", "instagram", "facebook", "tiktok",
    "sudah_diklaim", "jumlah_foto", "rentang_harga",
    # Dipakai Mode Cepat: kartu feed tidak lagi memuat jumlah ulasan, tapi kalau
    # bisnis ini pernah diambil penuh, angka lamanya masih jauh lebih berguna
    # daripada kosong.
    "jumlah_ulasan", "jam_operasional",
    # ruang komponen
    "email_lain", "tokopedia", "shopee", "marketplace_lain", "linkedin",
    "youtube", "whatsapp_lain", "website_utama", "kota",
)


def _lengkapi_dari_db(row):
    """
    Isi field yang tidak diambil run ini dengan nilai yang sudah tersimpan.

    Tanpa ini, men-scrape ulang bisnis lama dengan enrichment dimatikan akan
    menghitung rekomendasi jasa dari data web yang kosong — menghasilkan
    "Perlu Riset Manual" padahal database masih menyimpan hasil pemeriksaan
    website sebelumnya. Skor harus dihitung dari gambaran yang paling lengkap.
    """
    place_key = row.get("place_key")
    if not place_key:
        return row
    lama = db.get(place_key)
    if not lama:
        return row
    for field in _FIELD_WARISAN:
        if row.get(field) in (None, "") and lama.get(field) not in (None, ""):
            row[field] = lama[field]
    # Kolom yang diedit user menang atas hasil scrape, supaya skor dihitung dari
    # data editan (upsert_business juga tidak akan menimpanya).
    dikunci = db.kolom_terkunci(lama)
    if "nama_bisnis" in dikunci and row.get("nama_bisnis"):
        row["nama_gmaps"] = row["nama_bisnis"]   # nama terbaru di Google Maps
    for field in dikunci:
        if field in lama:
            row[field] = lama[field]
    if lama.get("kolom_dikunci"):
        row["kolom_dikunci"] = lama["kolom_dikunci"]
    return row


def _cek_email_ganda(row, filters):
    """Alasan lead dilewati karena semua emailnya sudah dipakai lead lain, atau None."""
    if not filters.get("lewati_email_ganda"):
        return None
    dipakai = db.email_dipakai(kontak.email_semua(row), row.get("place_key"))
    return kontak.saring_email(row, dipakai)


def _simpan_segera(row, run_id, jenis, cb, profil=None):
    """
    Tulis satu lead ke database SEKARANG, tanpa menunggu run selesai.

    Inti perbaikan "data hilang". Dulu seluruh lead menumpuk di memori dan baru
    ditulis setelah target terakhir + enrichment selesai, jadi run 30 wilayah
    yang mati di wilayah ke-25 kehilangan semuanya. `db.upsert_business` bersifat
    idempoten dan tidak pernah menimpa nilai bagus dengan None, jadi baris yang
    sama boleh ditulis lagi nanti setelah website-nya diperiksa.

    Kegagalan menulis dilaporkan tapi tidak pernah dilempar: satu baris yang
    gagal disimpan tidak boleh menjatuhkan listing lain, apalagi seluruh run.
    """
    place_key = row.get("place_key")
    if not place_key:
        return
    pf = profil or PROFIL["webdev"]
    try:
        _lengkapi_dari_db(row)
        pf["nilai"](row, jumlah_cabang=db.hitung_cabang(row.get("nama_bisnis")))
        if jenis == "update":
            # Bisnis lama: seluruh data segarnya ditulis (termasuk whatsapp_link
            # yang dihitung ulang dari nomor baru, dan hasil pemeriksaan ulang
            # website), tapi `last_scraped_at` tidak disentuh — kalau disentuh,
            # masa tenggang anti-duplikat 6 bulan ikut ter-reset. Dulu yang
            # disimpan hanya skornya, sehingga link WA di DB tetap nomor lama.
            db.upsert_business(row, sentuh_scrape=False)
        else:
            db.upsert_business(row)
        db.run_catat_lead(run_id, place_key, jenis)
    except Exception as e:
        cb(None, f"⚠ Gagal menyimpan {row.get('nama_bisnis', '?')}: "
                 f"{type(e).__name__}: {e}", None)


def _perubahan_tersimpan(place_key):
    """
    Rangkuman perubahan kontak dari tabel business_changes.

    Dipakai saat sheet "Update Kontak" dirakit ulang dari database — mis. untuk
    run yang prosesnya mati sebelum sempat menuliskan filenya, sehingga teks
    perubahan yang tadi hidup di memori sudah ikut hilang.
    """
    try:
        return "; ".join(
            f"{p['field']}: {p['nilai_lama'] or '(kosong)'} → {p['nilai_baru']}"
            for p in db.perubahan_terbaru(place_key, batas=5)
        )
    except Exception:
        return ""


def _rakit_row(data, href, area_tag, tanggal, kartu=None, place_key=None):
    """
    Lengkapi hasil ekstraksi jadi satu baris lead.

    `kartu` berisi apa yang sudah terbaca dari feed pencarian. Nilainya dipakai
    sebagai CADANGAN bila halaman detail tidak menyediakannya — pada UI Google
    sekarang jumlah ulasan sering memang tidak muncul di halaman detail,
    sementara di kartu feed selalu ada. Halaman detail tetap jadi acuan utama
    karena datanya lebih lengkap (alamat penuh, bukan potongan).

    `place_key` WAJIB diisi pemanggil yang sudah menghitung kunci itu untuk
    keputusan anti-duplikat. Menghitungnya ulang di sini dengan argumen yang
    berbeda (di feed telepon & alamat belum diketahui) bisa menghasilkan kunci
    lain, sehingga baris tersimpan di bawah kunci yang bukan kunci yang diperiksa.
    """
    if kartu:
        if data.get("rating") in (None, "", "-") and kartu.get("rating") is not None:
            data["rating"] = kartu["rating"]
        if data.get("jumlah_ulasan") is None and kartu.get("jumlah_ulasan") is not None:
            data["jumlah_ulasan"] = kartu["jumlah_ulasan"]
        for field in ("telepon", "kategori", "website"):
            if not data.get(field) and kartu.get(field):
                data[field] = kartu[field]
        if not data.get("nama_bisnis") and kartu.get("nama"):
            data["nama_bisnis"] = kartu["nama"]

    data["whatsapp_link"] = _wa_link(data.get("telepon"))
    data["koordinat"] = _koordinat_dari_href(href)
    data["area_pencarian"] = area_tag
    data["status_leads"] = "Belum Dihubungi"
    data["tanggal_scraping"] = tanggal
    data["source_url"] = href
    data["place_key"] = place_key or db.place_key_from_href(
        href, nama=data.get("nama_bisnis"), alamat=data.get("alamat"),
        telepon=data.get("telepon"),
    )
    return data


# ─── Scrape satu query ────────────────────────────────────────────────────────

async def _scrape_query(browser, ua, query, area_tag, max_results, params, cb,
                        offset_pct, range_pct, coords="", should_stop=None,
                        run_id=None):
    """
    Scrape satu kombinasi "jenis bisnis + area".

    Setiap lead yang berhasil dibaca langsung ditulis ke database lewat
    `_simpan_segera`. List yang dikembalikan hanya untuk pelaporan progres dan
    untuk fase enrichment sesudahnya — bukan lagi satu-satunya tempat data hidup.

    Return dict:
      baru      — list lead yang benar-benar baru (atau sudah kedaluwarsa)
      update    — list bisnis lama yang kontaknya berubah
      dilewati  — jumlah bisnis lama yang dilewati karena aturan anti-duplikat
      filter_awal — jumlah listing yang dibuang dari kartu feed tanpa dibuka
      dari_kartu  — jumlah lead yang cukup diambil dari kartu feed (Mode Cepat)
    """
    FEED = 'div[role="feed"]'
    hasil = {"baru": [], "update": [], "dilewati": 0, "filter_awal": 0,
             "gagal": 0, "dari_kartu": 0, "tutup": 0, "kontak_kurang": 0,
             "filter_detail": 0, "email_ganda": 0, "kualitas": 0}

    filters = params.get("_filters", {})
    pf = _profil(params)
    enrich_aktif = bool(params.get("enrich_website", True))
    mode_kontak = _mode_kontak(filters)
    # `max_results` = jumlah lead AKTIF yang diinginkan. Sebagian listing baru
    # ketahuan tutup / tanpa kontak setelah dibuka (di area pertokoan, separuh
    # toko hanya mencantumkan telepon kantor), jadi kartu CADANGAN dikumpulkan
    # 2x lipat. Cadangan hanya dibuka bila perlu: begitu target tercapai, sisa
    # kartu langsung dilewati tanpa membuka halamannya.
    kuota_lead = max_results
    if max_results:
        max_results = max_results * 2
    dedup_aktif = bool(params.get("dedup_enabled", True))
    dedup_bulan = float(params.get("dedup_bulan", 6) or 6)
    refresh_aktif = bool(params.get("refresh_kontak", True))
    refresh_hari = int(params.get("refresh_interval_hari", 30) or 30)
    mode_cepat = bool(params.get("mode_cepat", False))

    # ── Fase 1: kumpulkan kartu listing dengan satu session/IP ──
    context = await _buat_context(browser, ua, params, session_id="search")
    page = await context.new_page()

    kartu = []
    try:
        url = _build_search_url(query, coords, params.get("radius_km", 0))
        cb(offset_pct, f"Navigasi ke Google Maps: {query}", 0)
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await _random_delay()
        await _dismiss_consent(page)

        try:
            await page.wait_for_selector(FEED, timeout=25_000)
        except PlaywrightTimeoutError:
            # Pencarian yang sangat spesifik (mis. nama bisnis persis) membuat
            # Google membuka HALAMAN DETAIL langsung, tanpa daftar hasil.
            if "/maps/place/" in (page.url or ""):
                cb(offset_pct, f"'{query}' langsung membuka satu bisnis.", 0)
                kartu = [{"href": page.url, "nama": "", "rating": None,
                          "jumlah_ulasan": None}]
                raise _SatuHasil()

            await _random_delay(2.0, 3.0)
            try:
                # "domcontentloaded", bukan "networkidle": peta Google terus
                # mengalirkan request di latar belakang sehingga jaringan
                # praktis tidak pernah benar-benar sepi.
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await _dismiss_consent(page)
                await page.wait_for_selector(FEED, timeout=20_000)
            except PlaywrightTimeoutError:
                if "/maps/place/" in (page.url or ""):
                    kartu = [{"href": page.url, "nama": "", "rating": None,
                              "jumlah_ulasan": None}]
                    raise _SatuHasil()
                cb(offset_pct + range_pct, f"⚠ Panel tidak muncul untuk: {query}", 0)
                return hasil

        await _random_delay()
        kartu = await _kumpulkan_kartu(page, max_results, filters, cb, should_stop)
    except _SatuHasil:
        pass
    except Exception as e:
        # Fase 1 gagal tak terduga: kembalikan `hasil` kosong, jangan melempar —
        # pemanggil menghitung subtotal dari nilai balik ini.
        cb(offset_pct + range_pct,
           f"⚠ Gagal membuka daftar hasil '{query}': {type(e).__name__}: {e}", 0)
        return hasil
    finally:
        await context.close()

    if not kartu:
        return hasil

    # ── Filter awal: buang yang jelas tidak lolos SEBELUM halamannya dibuka ──
    total_kartu = len(kartu)
    lolos_awal = [k for k in kartu if _lolos_filter_awal(k, filters)]
    hasil["filter_awal"] = total_kartu - len(lolos_awal)
    kartu = lolos_awal[:max_results] if max_results else lolos_awal
    if hasil["filter_awal"]:
        hemat = hasil["filter_awal"] * DETIK_PER_LISTING / 60
        cb(offset_pct,
           f"⏭ {hasil['filter_awal']} dari {total_kartu} listing dilewati "
           f"(filter awal — hemat ±{hemat:.1f} menit)", 0)

    tanggal = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = len(kartu)

    # ── Fase 2: kunjungi listing yang lolos, beberapa sekaligus ──
    # Dulu listing dibuka satu per satu: ±10 detik masing-masing, jadi 20 listing
    # = ±3 menit yang hampir seluruhnya cuma menunggu jaringan. Pola semaphore +
    # gather ini sama persis dengan yang sudah dipakai enrich.enrich_banyak.
    # Tiap listing memang sudah punya context & IP sendiri (_buka_listing), jadi
    # yang berubah hanya penjadwalannya.
    konkuren = _konkuren_listing(params)
    jeda_min = float(params.get("jeda_listing_min", JEDA_LISTING_MIN) or 0)
    jeda_max = max(float(params.get("jeda_listing_max", JEDA_LISTING_MAX) or 0),
                   jeda_min)
    sem = asyncio.Semaphore(konkuren)
    kolam = _KolamContext(browser, ua, params, konkuren)
    selesai = 0

    def _terpenuhi():
        # Hanya lead yang PASTI lolos. Lead yang masih menunggu cek website bisa
        # gugur — kalau ikut dihitung, listing lain berhenti diproses dan target
        # berakhir kurang.
        return sum(1 for r in hasil["baru"] if r.get("lolos_filter") != 0)

    def _pct():
        return offset_pct + int((selesai / total) * range_pct) if total else offset_pct

    async def _kerjakan(idx, k):
        nonlocal selesai
        if should_stop and should_stop():
            return
        async with sem:
            if should_stop and should_stop():
                return
            await _satu_listing(idx, k)
            selesai += 1
        # Jeda dilakukan SESUDAH slot dilepas. Dulu ia berada di dalam semaphore,
        # jadi 2-4 detik tidur itu menahan slot konkurensi yang seharusnya sudah
        # bisa dipakai listing berikutnya. Pacing anti-bot yang sesungguhnya
        # datang dari batas konkurensi, bukan dari lamanya tidur.
        await _random_delay(jeda_min, jeda_max)

    async def _satu_listing(idx, k):
        href = k["href"]
        pct = _pct()
        if kuota_lead and _terpenuhi() >= kuota_lead:
            return
        place_key = db.place_key_from_href(href, nama=k.get("nama"))

        # Satu listing yang bermasalah tidak boleh menghanguskan lead yang sudah
        # terkumpul di target ini: `hasil` hanya sampai ke pemanggil lewat nilai
        # balik, jadi exception yang lolos dari sini membuang semuanya.
        try:
            status = db.classify(place_key, dedup_bulan) if dedup_aktif else db.BARU
            tersimpan = db.get(place_key) if status != db.BARU else None
            # Kartu feed tidak selalu punya nama (mis. saat pencarian langsung
            # membuka satu bisnis) — pakai nama dari database supaya log tetap jelas.
            nama_tampil = k.get("nama") or (tersimpan or {}).get("nama_bisnis") or "?"

            # ── Bisnis lama yang masih dalam masa tenggang ──
            if status == db.LAMA_SEGAR:
                umur = db.umur_hari(place_key) or 0
                if not (refresh_aktif and db.perlu_refresh_kontak(place_key, refresh_hari)):
                    hasil["dilewati"] += 1
                    cb(pct, f"⏭ Dilewati: {nama_tampil} "
                            f"(sudah ada, {umur} hari lalu)", len(hasil["baru"]))
                    return

                cb(pct, f"[{idx}/{total}] 🔄 Cek perubahan kontak: {nama_tampil}",
                   len(hasil["baru"]))
                data, galat = await _buka_listing(kolam, href)
                if data is None:
                    hasil["dilewati"] += 1
                    cb(pct, f"⚠ Gagal cek {nama_tampil}: {galat}", len(hasil["baru"]))
                    return

                perubahan = db.refresh_contacts(
                    place_key,
                    telepon=data.get("telepon"),
                    website=data.get("website"),
                )
                if perubahan:
                    lama = db.get(place_key) or {}
                    baris = dict(lama)
                    baris.update(_rakit_row(data, href, area_tag, tanggal, k,
                                            place_key=place_key))
                    # _rakit_row menandai setiap baris "Belum Dihubungi" karena
                    # dirancang untuk lead baru. Untuk bisnis yang sudah ada, itu
                    # menghapus hasil kerja user di sheet Update Kontak — lead
                    # yang sudah "Deal" terbaca seolah belum pernah disentuh.
                    for kol in db.KOLOM_MILIK_USER:
                        if lama.get(kol) not in (None, ""):
                            baris[kol] = lama[kol]
                    baris["perubahan"] = "; ".join(
                        f"{p['field']}: {p['lama'] or '(kosong)'} → {p['baru']}"
                        for p in perubahan
                    )
                    baris["_website_berubah"] = any(p["field"] == "website"
                                                    for p in perubahan)
                    _simpan_segera(baris, run_id, "update", cb, pf)
                    hasil["update"].append(baris)
                    cb(pct, f"🔄 Update: {baris.get('nama_bisnis', '?')} | "
                            f"{baris['perubahan']}", len(hasil["baru"]))
                else:
                    hasil["dilewati"] += 1
                    cb(pct, f"✓ Tidak ada perubahan: {nama_tampil}",
                       len(hasil["baru"]))
                return

            # ── Bisnis baru atau sudah kedaluwarsa → ambil penuh ──
            label = "✨ Baru" if status == db.BARU else "♻ Data kedaluwarsa, diambil ulang"
            cb(pct, f"[{idx}/{total}] {label}: "
                    f"{nama_tampil if nama_tampil != '?' else 'mengambil detail...'}",
               len(hasil["baru"]))

            # Mode Cepat: kartu feed sudah memuat nama, kategori, telepon, rating,
            # alamat singkat, dan ada/tidaknya website. Kalau semuanya terbaca,
            # halaman detailnya tidak perlu dibuka sama sekali — dari ±10 detik
            # jadi nol. Kartu yang kurang lengkap tetap dibuka seperti biasa,
            # jadi mode ini menghemat waktu tanpa mengorbankan satu lead pun.
            if mode_cepat and _kartu_cukup(k):
                data, galat = _detail_dari_kartu(k), None
                hasil["dari_kartu"] += 1
            else:
                data, galat = await _buka_listing(kolam, href)
            if data is None:
                hasil["gagal"] += 1
                cb(pct, f"⚠ Listing {idx} dilewati: {galat}", len(hasil["baru"]))
                return
            nama_baca = data.get("nama_bisnis") or nama_tampil
            # Cek kuota lagi: beberapa listing dibuka bersamaan, jadi saat halaman
            # ini selesai dibaca, listing lain mungkin sudah memenuhi target.
            if kuota_lead and _terpenuhi() >= kuota_lead:
                return

            # Penyaringan SAAT scraping: bisnis tutup tidak pernah menjadi lead.
            # Dicatat di daftar lewati supaya run berikutnya tidak membuka
            # halamannya lagi.
            if (not filters.get("sertakan_tutup")
                    and scoring.bisnis_tutup(data.get("status_buka"))):
                db.lewati_catat(place_key, nama_baca, data.get("status_buka"), "tutup")
                hasil["tutup"] += 1
                cb(pct, f"⛔ Dilewati: {nama_baca} ({data.get('status_buka')})",
                   len(hasil["baru"]))
                return

            # Kunci dari kartu feed dihitung tanpa telepon & alamat — keduanya
            # baru diketahui sekarang. Selama href memuat CID Google kunci itu
            # sudah final; kalau tidak, kunci yang kaya bisa berbeda, dan baris
            # akan tersimpan di bawah kunci yang BUKAN kunci yang tadi diperiksa.
            kunci = place_key
            if not str(kunci).startswith("cid:"):
                kunci_kaya = db.place_key_from_href(
                    href, nama=data.get("nama_bisnis"), alamat=data.get("alamat"),
                    telepon=data.get("telepon"),
                )
                if kunci_kaya and kunci_kaya != kunci:
                    kunci = kunci_kaya
                    # Klasifikasi tadi dijalankan atas kunci yang keliru — ulangi,
                    # kalau tidak bisnis ini masuk lagi sebagai lead "baru".
                    if dedup_aktif and db.classify(kunci, dedup_bulan) == db.LAMA_SEGAR:
                        hasil["dilewati"] += 1
                        cb(pct, f"⏭ Dilewati: {data.get('nama_bisnis') or nama_tampil} "
                                f"(sudah ada — ketahuan setelah detail dibaca)",
                           len(hasil["baru"]))
                        return

            baris = _rakit_row(data, href, area_tag, tanggal, k, place_key=kunci)

            # Syarat rating/ulasan/"tanpa website" yang baru bisa dipastikan
            # setelah halaman detail dibaca.
            alasan = _syarat_non_kontak(baris, filters)
            if not alasan and filters.get("_kecualikan"):
                alasan = komponen.dikecualikan(baris.get("kategori"),
                                               baris.get("nama_bisnis"),
                                               filters["_kecualikan"])
            if alasan:
                hasil["filter_detail"] += 1
                cb(pct, f"⏭ Dilewati: {nama_baca} ({alasan})", len(hasil["baru"]))
                return

            kontak.rapikan_kontak(baris)
            # Saringan kualitas Klien Website (scrapers/kualitas.py). Syarat
            # kontak belum dicek di sini: email sering baru ketemu di website.
            if filters.get("saring_kualitas"):
                sampah = kualitas.alasan_sampah(baris, cek_kontak=False)
                if not sampah:
                    pemilik = db.wa_dipakai(baris.get("whatsapp_link"), kunci)
                    if pemilik:
                        sampah = f"nomor WA sama dengan '{pemilik[:40]}'"
                if sampah:
                    db.lewati_catat(kunci, nama_baca, sampah, "kualitas")
                    hasil["kualitas"] += 1
                    cb(pct, f"⏭ Dilewati: {nama_baca} ({sampah})", len(hasil["baru"]))
                    return
            ganda = _cek_email_ganda(baris, filters)
            if ganda:
                db.lewati_catat(kunci, nama_baca, ganda, "email")
                hasil["email_ganda"] += 1
                cb(pct, f"⏭ Dilewati: {nama_baca} ({ganda})", len(hasil["baru"]))
                return
            kurang = kontak.kontak_kurang(baris, mode_kontak)
            if kurang and enrich_aktif and kontak.bisa_dilengkapi_website(baris):
                # Belum pasti: WA/email mungkin tercantum di website-nya.
                # Disimpan tersembunyi dulu, diputuskan setelah website dicek.
                baris["lolos_filter"] = 0
            elif kurang:
                db.lewati_catat(kunci, nama_baca, kurang, "kontak", mode_kontak)
                hasil["kontak_kurang"] += 1
                cb(pct, f"⏭ Dilewati: {nama_baca} ({kurang})", len(hasil["baru"]))
                return
            else:
                baris["lolos_filter"] = 1

            # Ditulis ke database SEKARANG, bukan setelah semua target selesai.
            # Mulai detik ini lead tersebut aman walau prosesnya mati.
            _simpan_segera(baris, run_id, "baru", cb, pf)
            hasil["baru"].append(baris)
            if baris.get("lolos_filter") == 0:
                cb(pct, f"… {baris.get('nama_bisnis', '?')} — kontak belum lengkap, "
                        f"menunggu pemeriksaan website", len(hasil["baru"]))
            else:
                cb(pct, f"✓ {baris.get('nama_bisnis', '?')} | "
                        f"⭐{baris.get('rating') or '-'}"
                        + (f" ({baris['jumlah_ulasan']})" if baris.get('jumlah_ulasan') is not None else "")
                        + " | "
                        f"Tel: {baris.get('telepon') or '-'}"
                        + (f" | {baris['email']}" if baris.get("email") else ""),
                   len(hasil["baru"]))
        except Exception as e:
            hasil["gagal"] += 1
            cb(pct, f"⚠ Listing {idx} gagal: {type(e).__name__}: {e}",
               len(hasil["baru"]))

    if konkuren > 1:
        cb(offset_pct, f"Target {kuota_lead} lead — {total} listing tersedia "
                       f"(termasuk cadangan), {konkuren} dibuka sekaligus...", 0)
    try:
        await asyncio.gather(*(_kerjakan(i, k) for i, k in enumerate(kartu, 1)))
    finally:
        await kolam.tutup_semua()
    if hasil["dari_kartu"]:
        cb(None, f"⚡ {hasil['dari_kartu']} lead diambil langsung dari kartu feed "
                 f"(Mode Cepat — halaman detailnya tidak dibuka)", len(hasil["baru"]))
    if should_stop and should_stop():
        cb(None, "⏹ Dihentikan oleh pengguna.", len(hasil["baru"]))

    return hasil


# ─── Gemini AI tambahan ───────────────────────────────────────────────────────

async def _gemini_gmaps(jenis, area, max_results, api_key, cb):
    """
    Cari data bisnis lewat Gemini + Google Search Grounding.

    Hasilnya TIDAK dicampur dengan data Playwright: model bahasa bisa mengarang
    nomor telepon, jadi baris-baris ini masuk sheet terpisah dan harus
    diverifikasi manual sebelum dihubungi.
    """
    results = []
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        prompt = (
            f'Cari daftar bisnis "{jenis}" di {area}, Indonesia dari Google Maps dan direktori bisnis.\n'
            f'Kembalikan JSON array maksimal {max_results} item:\n'
            '[\n'
            '  {"nama_bisnis": "...", "kategori": "...", "rating": "4.5", "jumlah_ulasan": 120,\n'
            '   "telepon": "08xx...", "alamat": "...", "website": "", "jam_operasional": "..."}\n'
            ']\n'
            'rating = angka 1-5, jumlah_ulasan = integer, telepon = nomor HP Indonesia.\n'
            'Kosongkan field yang tidak diketahui, JANGAN mengarang.\n'
            'Kembalikan HANYA JSON array valid, tanpa teks penjelasan.'
        )
        import config as cfg
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=getattr(cfg, "GEMINI_MODEL", "gemini-2.5-flash"),
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            ),
        )
        raw_text = (response.text or "").strip()
        raw_text = re.sub(r'^```(?:json)?\s*', '', raw_text, flags=re.MULTILINE)
        raw_text = re.sub(r'```\s*$', '', raw_text, flags=re.MULTILINE).strip()
        if not raw_text:
            cb(None, "⚠ Gemini GMaps: respons kosong", 0)
            return results
        json_match = re.search(r'\[[\s\S]*\]', raw_text)
        if not json_match:
            cb(None, "⚠ Gemini GMaps: tidak ada JSON dalam respons", 0)
            return results
        json_str = re.sub(r',\s*([}\]])', r'\1', json_match.group())
        items = json.loads(json_str)
        tanggal = datetime.now().strftime("%Y-%m-%d %H:%M")
        for item in items[:max_results]:
            if not isinstance(item, dict):
                continue
            rating = item.get("rating") or ""
            ulasan = item.get("jumlah_ulasan") or 0
            telepon = str(item.get("telepon") or "").strip()
            website = str(item.get("website") or "").strip()
            # Sengaja "" dan bukan "-": nilai "-" akan lolos filter
            # "hanya bisnis tanpa website" seolah-olah sudah terverifikasi.
            if website in ("-", "n/a", "none"):
                website = ""
            hasil = {
                "nama_bisnis": str(item.get("nama_bisnis") or "").strip(),
                "kategori": str(item.get("kategori") or "").strip(),
                "rating": str(rating),
                "jumlah_ulasan": ulasan,
                "skor_popularitas": _hitung_score(rating, ulasan),
                "telepon": telepon,
                "whatsapp_link": _wa_link(telepon),
                "alamat": str(item.get("alamat") or "").strip(),
                "website": website,
                "jam_operasional": str(item.get("jam_operasional") or "").strip(),
                "area_pencarian": area,
                "status_leads": "Belum Dihubungi",
                "tanggal_scraping": tanggal,
                "source_url": "Gemini AI Search",
                "sumber": "Gemini AI — belum terverifikasi",
            }
            results.append(hasil)
            cb(None, f"✓ Gemini: {hasil['nama_bisnis']} | {telepon or '-'}", len(results))
    except ImportError:
        cb(None, "❌ google-genai belum terinstall. Jalankan: pip install google-genai", 0)
    except Exception as e:
        cb(None, f"❌ Gemini GMaps error: {type(e).__name__}: {e}", len(results))
    return results


# ─── Main async ───────────────────────────────────────────────────────────────

async def _async_main(params, cb, should_stop=None):
    import config as cfg

    def opsi(nama, default):
        """Ambil dari form web; kalau tidak dikirim, pakai nilai config.py."""
        return params[nama] if nama in params else getattr(cfg, nama.upper(), default)

    search_targets = params.get("search_targets", [])
    max_results = int(params.get("max_results") or 20)
    pf = _profil(params)
    if pf["nama"] == "komponen":
        # Default ruang komponen: website tidak jadi syarat, rating/ulasan tidak
        # difilter kecuali diminta, halaman detail selalu dibuka (status aktif,
        # alamat lengkap, dan website hanya ada di sana).
        for k, v in (("require_no_website", False), ("filter_rating", False),
                     ("filter_reviews", False), ("mode_cepat", False),
                     ("require_phone", False), ("require_whatsapp", False)):
            params.setdefault(k, v)
    filter_flags = {
        "filter_rating":      bool(params.get("filter_rating", True)),
        "min_rating":         float(params.get("min_rating", 0.0) or 0),
        "filter_reviews":     bool(params.get("filter_reviews", True)),
        "min_reviews":        int(params.get("min_reviews", 0) or 0),
        "require_phone":      bool(params.get("require_phone", True)),
        "require_whatsapp":   bool(params.get("require_whatsapp", True)),
        "require_no_website": bool(params.get("require_no_website", True)),
        "sertakan_tutup":     bool(opsi("sertakan_bisnis_tutup", False)),
        # Diselesaikan sekali di sini supaya _passes_filter dan _lolos_filter_awal
        # membaca mode yang sama, tanpa masing-masing menebak dari sakelar lama.
        "mode_kontak":        _mode_kontak({
            "mode_kontak":      opsi("mode_kontak", ""),
            "require_phone":    bool(params.get("require_phone", True)),
            "require_whatsapp": bool(params.get("require_whatsapp", True)),
        }),
        "_profil": pf["nama"],
        "_enrich": bool(opsi("enrich_website", True)),
        "_kecualikan": params.get("_kecualikan") or [],
        # Cabang jaringan yang emailnya sudah dipakai lead lain tidak diambil
        # lagi — satu email cukup satu penawaran. Bawaan aktif di ruang komponen.
        "lewati_email_ganda": bool(opsi("lewati_email_ganda",
                                        pf["nama"] == "komponen")),
        # Klien Website: buang yang bukan calon pembeli saat scraping
        # (scrapers/kualitas.py) — Blogspot, fasilitas umum, nama generik,
        # listing hantu, cabang yang nomor WA/email-nya sudah dipakai.
        "saring_kualitas": bool(opsi("saring_kualitas", pf["nama"] == "webdev")),
    }
    if pf["nama"] == "komponen" and not params.get("mode_kontak"):
        filter_flags["mode_kontak"] = kontak.MODE_WA_ATAU_EMAIL
    if filter_flags["saring_kualitas"]:
        # Lead tanpa WA & email tidak bisa ditawari apa pun: mode kontak yang
        # lebih longgar dinaikkan ke "WhatsApp atau email".
        if filter_flags["mode_kontak"] in (kontak.MODE_BEBAS, kontak.MODE_TELEPON,
                                           kontak.MODE_APA_SAJA):
            filter_flags["mode_kontak"] = kontak.MODE_WA_ATAU_EMAIL
        filter_flags["lewati_email_ganda"] = True
    filter_flags["_lewati"] = _penyaring_lewati(filter_flags["mode_kontak"])
    params["_filters"] = filter_flags
    # 0 = otomatis (lihat _konkuren_listing): 5 tanpa proxy, 8 dengan proxy.
    params.setdefault("listing_konkuren", int(opsi("listing_konkuren", 0) or 0))
    params.setdefault("blokir_gambar", bool(opsi("blokir_gambar", True)))
    params.setdefault("jeda_listing_min", float(opsi("jeda_listing_min",
                                                     JEDA_LISTING_MIN)))
    params.setdefault("jeda_listing_max", float(opsi("jeda_listing_max",
                                                     JEDA_LISTING_MAX)))
    params.setdefault("mode_cepat", bool(opsi("mode_cepat", False)))
    params.setdefault("dedup_enabled", bool(opsi("dedup_enabled", True)))
    params.setdefault("dedup_bulan", float(opsi("dedup_bulan", 6) or 6))
    params.setdefault("refresh_kontak", bool(opsi("refresh_kontak", True)))
    params.setdefault("refresh_interval_hari", int(opsi("refresh_interval_hari", 30) or 30))
    params.setdefault("enrich_website", bool(opsi("enrich_website", True)))

    output_format = params.get("output_format", "excel")
    headless = bool(params.get("headless", True))
    enrich_aktif = bool(opsi("enrich_website", True))
    enrich_konkuren = int(opsi("enrich_konkuren", 5) or 5)
    enrich_timeout = int(opsi("enrich_timeout", 12) or 12)
    use_gemini = bool(params.get("use_gemini", False))
    gemini_api_key = str(params.get("gemini_api_key", "") or "").strip()
    simpan_gemini = bool(params.get("simpan_gemini_ke_db", False))

    # db.init_db() SENGAJA tidak dipanggil di sini: ia menandai setiap run
    # 'berjalan' sebagai 'terputus', termasuk run lain yang sedang berjalan di
    # thread sebelah. Cukup dipanggil sekali saat app/CLI mulai.

    # Identitas run. Semua lead yang dibaca run ini diklaim di bawah kunci ini,
    # sehingga file hasilnya bisa dirakit ulang dari database kapan saja — juga
    # setelah prosesnya mati.
    run_id = params.get("_run_id") or uuid.uuid4().hex[:12]
    params["_run_id"] = run_id
    # Nomor target pertama pada daftar ini, dihitung dari seluruh target run
    # aslinya. Saat melanjutkan run, `search_targets` hanya berisi sisanya —
    # tanpa offset ini penomoran di log mengulang dari 1 dan penanda kemajuan
    # di database mundur.
    offset_target = int(params.get("_offset_target") or 0)
    total_target = int(params.get("_total_target") or 0) or (
        offset_target + len(search_targets))
    db.run_start(run_id, params, total_target)

    if params.get("mode_cepat"):
        cb(0, "⚡ Mode Cepat aktif — halaman detail hanya dibuka bila kartu feed "
              "kurang lengkap. Google tidak lagi menampilkan JUMLAH ULASAN di "
              "kartu, jadi lead yang diambil dari kartu tidak punya angka itu "
              "dan skor pembelinya lebih konservatif.", 0)
        if params["_filters"].get("filter_reviews"):
            cb(0, "   ↳ Filter 'minimal ulasan' tidak bisa diterapkan pada lead "
                  "yang diambil dari kartu (angkanya tidak diketahui). Lead itu "
                  "TETAP dimasukkan, bukan dibuang — matikan Mode Cepat kalau "
                  "batas ulasan ini penting.", 0)

    ua = UserAgent(os=["windows", "macos", "linux"])
    user_agent = ua.chrome

    semua_baru, semua_update, hasil_gemini = [], [], []
    dilihat = set()
    stat = {"dilewati": 0, "filter_awal": 0, "gagal": 0, "duplikat": 0,
            "dari_kartu": 0, "tutup": 0, "kontak_kurang": 0, "filter_detail": 0,
            "email_ganda": 0, "kualitas": 0}
    n_queries = max(len(search_targets), 1)
    galat_fatal = None

    launch_args = [
        f"--user-agent={user_agent}",
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox", "--disable-dev-shm-usage", "--lang=id-ID,id",
    ]

    async def _enrich_dan_simpan(baris_baru, baris_update, pct):
        """
        Periksa website lalu tulis ulang hasilnya ke database.

        Dijalankan per target, bukan sekali di akhir run. Kalau enrichment 30
        target ditunda sampai target ke-30 selesai, seluruh data web-nya ikut
        hilang saat run mati di tengah — persis masalah yang sedang diperbaiki.

        Return list baris yang dibuang karena syarat kontak tetap tidak
        terpenuhi setelah website diperiksa (ruang komponen).
        """
        if not enrich_aktif:
            return []
        dibuang = []
        if baris_baru and not (should_stop and should_stop()):
            cb(pct, f"🌐 Memeriksa website {len(baris_baru)} lead...", len(semua_baru))
            await enrich.enrich_banyak(baris_baru, konkuren=enrich_konkuren, cb=cb,
                                       should_stop=should_stop, timeout=enrich_timeout)
            for row in baris_baru:
                if filter_flags.get("saring_kualitas") and kualitas.website_blog(
                        row.get("website"), row.get("web_platform")):
                    db.buang_lead(row.get("place_key"), row.get("nama_bisnis"),
                                  "website blog (Blogspot)", "kualitas")
                    row["_kualitas"] = True
                    dibuang.append(row)
                    cb(None, f"⏭ Dilewati: {row.get('nama_bisnis', '?')} "
                             f"(website blog)", None)
                    continue
                # Email umumnya baru muncul dari website, jadi cek ganda di sini
                # untuk SEMUA baris baru — termasuk yang sudah lolos lewat WA.
                # Diproses berurutan & disimpan sebelum baris berikutnya dicek,
                # jadi dua cabang dalam satu target pun tertangkap.
                ganda = _cek_email_ganda(row, filter_flags)
                if ganda:
                    db.buang_lead(row.get("place_key"), row.get("nama_bisnis"),
                                  ganda, "email")
                    row["_email_ganda"] = True
                    dibuang.append(row)
                    cb(None, f"⏭ Dilewati: {row.get('nama_bisnis', '?')} ({ganda})", None)
                    continue
                if row.get("lolos_filter") == 0:
                    kontak.rapikan_kontak(row)
                    kurang = kontak.kontak_kurang(row, filter_flags.get("mode_kontak"))
                    if kurang:
                        db.buang_lead(row.get("place_key"), row.get("nama_bisnis"),
                                      kurang, "kontak", filter_flags.get("mode_kontak"))
                        dibuang.append(row)
                        cb(None, f"⏭ Dilewati: {row.get('nama_bisnis', '?')} "
                                 f"({kurang}, juga di website)", None)
                        continue
                    row["lolos_filter"] = 1
                    cb(None, f"✓ {row.get('nama_bisnis', '?')} — kontak ditemukan di "
                             f"website", None)
                _simpan_segera(row, run_id, "baru", cb, pf)
        perlu_ulang = [r for r in baris_update if r.pop("_website_berubah", False)]
        if perlu_ulang and not (should_stop and should_stop()):
            cb(pct, f"🌐 Memeriksa ulang {len(perlu_ulang)} website yang berubah...",
               len(semua_baru))
            await enrich.enrich_banyak(perlu_ulang, konkuren=enrich_konkuren, cb=cb,
                                       should_stop=should_stop, timeout=enrich_timeout)
            for row in perlu_ulang:
                # Email baru dari website juga dicatat sebagai perubahan kontak.
                db.refresh_contacts(row.get("place_key"), email=row.get("email") or None)
                _simpan_segera(row, run_id, "update", cb, pf)
        return dibuang

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless, args=launch_args)
            try:
                for i, target in enumerate(search_targets):
                    if should_stop and should_stop():
                        cb(None, "⏹ Dihentikan sebelum target berikutnya.",
                           len(semua_baru))
                        break
                    jenis = target.get("jenis", "")
                    area = target.get("area", "")
                    coords = target.get("coords", "")
                    query = f"{jenis} {area}".strip()
                    offset = int((i / n_queries) * 75)
                    rng = int(75 / n_queries)
                    # Sebagian jatah progres target ini disisakan untuk fase
                    # pemeriksaan website yang menyusul.
                    rng_scrape = max(int(rng * 0.8), 1)
                    cb(offset, f"[{offset_target + i + 1}/{total_target}] "
                               f"Mulai scraping: {query}", len(semua_baru))

                    try:
                        hasil = await _scrape_query(
                            browser, user_agent, query, area, max_results, params,
                            cb, offset, rng_scrape, coords, should_stop,
                            run_id=run_id)
                    except Exception as e:
                        # Satu target yang bermasalah tidak boleh menghanguskan
                        # hasil target-target sebelumnya: pada run 30 wilayah,
                        # kegagalan di wilayah ke-5 dulu membuat semuanya hilang.
                        cb(offset + rng,
                           f"❌ Gagal pada '{query}': {type(e).__name__}: {e} "
                           f"— dilanjutkan ke target berikutnya.", len(semua_baru))
                        continue

                    # Deduplikasi dalam-run berdasarkan identitas bisnis. Baris
                    # yang terbuang di sini SUDAH tersimpan di database (dan sudah
                    # diklaim run ini); yang dicegah hanya pemeriksaan website
                    # ganda untuk bisnis yang sama.
                    segar = []
                    for row in hasil["baru"]:
                        kunci = row.get("place_key") or row.get("source_url")
                        if kunci and kunci in dilihat:
                            stat["duplikat"] += 1
                            cb(None, f"⏭ Duplikat dalam run ini: "
                                     f"{row.get('nama_bisnis', '?')}", None)
                            continue
                        if kunci:
                            dilihat.add(kunci)
                        segar.append(row)

                    dibuang = await _enrich_dan_simpan(segar, hasil["update"],
                                                       offset + rng_scrape)
                    if dibuang:
                        segar = [r for r in segar if r not in dibuang]
                        hasil["baru"] = [r for r in hasil["baru"] if r not in dibuang]
                        n_ganda = sum(1 for r in dibuang if r.get("_email_ganda"))
                        n_kualitas = sum(1 for r in dibuang if r.get("_kualitas"))
                        hasil["email_ganda"] += n_ganda
                        hasil["kualitas"] += n_kualitas
                        hasil["kontak_kurang"] += len(dibuang) - n_ganda - n_kualitas
                    stat["tutup"] += hasil.get("tutup", 0)
                    stat["kualitas"] += hasil.get("kualitas", 0)
                    stat["email_ganda"] += hasil.get("email_ganda", 0)
                    stat["kontak_kurang"] += hasil.get("kontak_kurang", 0)
                    stat["filter_detail"] += hasil.get("filter_detail", 0)

                    semua_baru.extend(segar)
                    semua_update.extend(hasil["update"])
                    stat["dilewati"] += hasil["dilewati"]
                    stat["filter_awal"] += hasil["filter_awal"]
                    stat["gagal"] += hasil["gagal"]
                    stat["dari_kartu"] += hasil.get("dari_kartu", 0)

                    db.log_search(jenis, area,
                                  total_ditemukan=len(hasil["baru"]) + hasil["dilewati"],
                                  baru=len(hasil["baru"]), dilewati=hasil["dilewati"],
                                  diupdate=len(hasil["update"]))
                    # Target ini TUNTAS — termasuk pemeriksaan websitenya. Ditandai
                    # setelahnya, bukan sebelum, supaya "lanjutkan run" mengulang
                    # target yang enrichmentnya belum sempat selesai.
                    db.run_tandai_target(run_id, offset_target + i + 1)
                    tersaring = (f", {hasil['tutup']} tutup, "
                                 f"{hasil['kontak_kurang']} tanpa kontak yang diminta")
                    if hasil.get("filter_detail"):
                        tersaring += f", {hasil['filter_detail']} tidak lolos filter"
                    if hasil.get("email_ganda"):
                        tersaring += f", {hasil['email_ganda']} email sudah dipakai lead lain"
                    if hasil.get("kualitas"):
                        tersaring += f", {hasil['kualitas']} bukan calon pembeli (saringan kualitas)"
                    cb(offset + rng,
                       f"Subtotal '{query}': {len(hasil['baru'])} baru, "
                       f"{hasil['dilewati']} sudah ada di database{tersaring}, "
                       f"{len(hasil['update'])} update",
                       len(semua_baru))
                    if i < n_queries - 1:
                        await _random_delay(1.5, 3.0)
            finally:
                await browser.close()

        # ── Gemini: sumber terpisah, tidak dicampur ──
        if use_gemini and gemini_api_key and not (should_stop and should_stop()):
            for target in search_targets:
                jenis, area = target.get("jenis", ""), target.get("area", "")
                if not jenis:
                    continue
                cb(90, f"Gemini AI: mencari '{jenis}' di {area}...", len(semua_baru))
                hasil_gemini.extend(
                    await _gemini_gmaps(jenis, area, max_results, gemini_api_key, cb))
            cb(92, f"Gemini selesai: {len(hasil_gemini)} baris (perlu verifikasi manual)",
               len(semua_baru))
            if simpan_gemini:
                for row in hasil_gemini:
                    row["place_key"] = db.place_key_from_href(
                        "", nama=row.get("nama_bisnis"), alamat=row.get("alamat"),
                        telepon=row.get("telepon"))
                    pf["nilai"](row)
                    try:
                        db.upsert_business(row)
                    except Exception:
                        pass
        elif use_gemini and not gemini_api_key:
            cb(90, "⚠ Gemini dilewati — API key tidak diisi", len(semua_baru))

    except Exception as e:
        # Kegagalan di titik mana pun tidak boleh membatalkan file hasil: lead
        # yang sudah dibaca ada di database, dan blok `finally` di bawah tetap
        # merakitnya jadi Excel. Errornya dilaporkan, bukan dilempar ulang.
        galat_fatal = f"{type(e).__name__}: {e}"
        cb(None, f"❌ Run berhenti karena error: {galat_fatal} — "
                 f"lead yang sudah terkumpul tetap disimpan dan tetap dibuatkan file.",
           len(semua_baru))
    dibatalkan = bool(should_stop and should_stop())
    status = ("dibatalkan" if dibatalkan
              else "terputus" if galat_fatal else "selesai")

    return _selesaikan_run(run_id, status, stat, filter_flags, hasil_gemini,
                           output_format, cb, galat_fatal, pf)


def _penyaring_lewati(mode_kontak=""):
    """
    Fungsi kartu → bool untuk filter awal: True bila bisnis ini ada di daftar
    lewati ruang aktif (tutup / dihapus user / kontak kurang pada mode yang
    sama). Di-cache per href karena _kumpulkan_kartu memanggil filter awal
    berulang kali di setiap putaran scroll.
    """
    cache = {}

    def lewati(kartu):
        href = kartu.get("href") or ""
        if href not in cache:
            kunci = db.place_key_from_href(href, nama=kartu.get("nama"))
            cache[href] = bool(db.lewati_aktif(kunci, mode_kontak))
        return cache[href]
    return lewati


def _putuskan_tertunda(run_id, filter_flags, cb):
    """
    Lead run ini yang masih menunggu pemeriksaan website (lolos_filter = 0) —
    mis. karena run dihentikan sebelum websitenya dicek — diputuskan sekarang
    dengan data yang ada, supaya tidak tersembunyi selamanya.
    """
    mode = filter_flags.get("mode_kontak")
    for row in db.run_leads_rows(run_id, "baru"):
        if row.get("lolos_filter") != 0:
            continue
        kurang = kontak.kontak_kurang(row, mode)
        if kurang:
            db.buang_lead(row["place_key"], row.get("nama_bisnis"), kurang, "kontak", mode)
        else:
            db.set_lolos(row["place_key"], 1)


def _selesaikan_run(run_id, status, stat, filter_flags, hasil_gemini,
                    output_format, cb, galat_fatal=None, profil=None):
    """
    Rakit file hasil DARI DATABASE, lalu tutup run.

    Sumbernya database dan bukan list di memori, karena itulah satu-satunya
    tempat yang isinya utuh setelah run yang gagal separuh jalan. Efek
    sampingnya bagus: baris yang keluar sudah membawa data terlengkap yang
    pernah tersimpan untuk bisnis itu, bukan cuma apa yang terbaca run ini.
    """
    pf = profil or PROFIL["webdev"]
    _putuskan_tertunda(run_id, filter_flags, cb)
    leads_db = db.run_leads_rows(run_id, "baru")
    update_db = db.run_leads_rows(run_id, "update")
    for row in update_db:
        row["perubahan"] = _perubahan_tersimpan(row.get("place_key"))

    # ── Filter akhir (syarat yang butuh data detail) ──
    lolos, sebab_buang = [], {}
    for r in leads_db:
        alasan = _passes_filter(r, filter_flags)
        if alasan is None:
            lolos.append(r)
        else:
            sebab_buang[alasan] = sebab_buang.get(alasan, 0) + 1
    dibuang_filter = len(leads_db) - len(lolos)

    if dibuang_filter:
        cb(None, f"ℹ {dibuang_filter} lead tidak masuk file karena filter akhir — "
                 f"semuanya TETAP tersimpan di database dan bisa dilihat di halaman "
                 f"Leads.", len(lolos))

    cb(95, f"Selesai: {len(lolos)} leads baru, {len(update_db)} update, "
           f"{stat['dilewati']} dilewati (duplikat). Menyimpan...", len(lolos))

    ringkasan = {
        "Leads baru (lolos filter)": len(lolos),
        "Dibuang filter akhir": dibuang_filter,
        "Dilewati — sudah ada di database": stat["dilewati"],
        "Dilewati — filter awal dari feed": stat["filter_awal"],
        "Duplikat dalam run ini": stat["duplikat"],
        "Kontak berubah (diupdate)": len(update_db),
        "Gagal dibuka": stat["gagal"],
        "Diambil dari kartu feed (Mode Cepat)": stat.get("dari_kartu", 0),
        "Hasil Gemini (perlu verifikasi)": len(hasil_gemini),
        "Status run": status,
    }
    ringkasan["Dilewati — bisnis tutup"] = stat.get("tutup", 0)
    ringkasan["Dilewati — kontak tidak memenuhi"] = stat.get("kontak_kurang", 0)
    ringkasan["Dilewati — rating/ulasan/website/kategori"] = stat.get("filter_detail", 0)
    if filter_flags.get("lewati_email_ganda"):
        ringkasan["Dilewati — email sudah dipakai lead lain"] = stat.get("email_ganda", 0)
    if filter_flags.get("saring_kualitas"):
        ringkasan["Dilewati — saringan kualitas (blog/fasilitas umum/cabang/listing hantu)"] = \
            stat.get("kualitas", 0)
    ringkasan["Syarat kontak"] = kontak.LABEL_MODE.get(
        filter_flags.get("mode_kontak"), "-")
    if galat_fatal:
        ringkasan["Error"] = galat_fatal

    if not (lolos or update_db or hasil_gemini):
        _lapor_nol(ringkasan, sebab_buang, filter_flags, cb)
        db.run_finish(run_id, status, "", 0, 0)
        return None

    filename = _tulis_output(lolos, update_db, hasil_gemini, ringkasan,
                             output_format, cb, pf)
    db.run_finish(run_id, status, filename, len(lolos), len(update_db))
    return filename


def _saran_pelonggaran(ringkasan, sebab_buang, f):
    """Saran konkret berdasarkan penyebab yang PALING banyak membuang lead."""
    if sebab_buang:
        terbesar = max(sebab_buang, key=sebab_buang.get)
        if terbesar == "sudah punya website":
            return ('matikan "wajib belum punya website" — bisnis yang sudah punya '
                    'website tetap prospek untuk jasa redesign & iklan')
        if terbesar.startswith("tidak ada nomor WhatsApp"):
            return 'longgarkan Kontak wajib jadi "telepon apa pun" atau "salah satu ada"'
        if terbesar == "tidak ada email":
            return ('email hanya bisa dipanen dari website bisnis — matikan "wajib '
                    'belum punya website" dan pastikan Periksa Website menyala')
        if terbesar.startswith("rating") or terbesar.startswith("ulasan"):
            return "turunkan rating/ulasan minimum"
        if terbesar == "bisnis tutup":
            return "hampir semua listing di area ini sudah tutup — coba wilayah lain"
    # Anti-duplikat diperiksa lebih dulu daripada filter awal: kalau SEMUA listing
    # yang lolos filter ternyata sudah ada di database, melonggarkan rating tidak
    # menolong sama sekali — yang perlu diganti adalah wilayahnya.
    if ringkasan["Dilewati — sudah ada di database"]:
        return ("wilayah ini sudah pernah dipanen — pilih wilayah/jenis bisnis lain, "
                "atau turunkan jeda anti-duplikat")
    if ringkasan["Dilewati — filter awal dari feed"] > ringkasan["Gagal dibuka"]:
        return "turunkan rating/ulasan minimum — kebanyakan listing gugur sejak di feed"
    if ringkasan["Gagal dibuka"]:
        return ("semua listing gagal dibuka — cek koneksi, atau matikan proxy kalau "
                "tokennya belum diisi")
    return "coba wilayah atau jenis bisnis yang lain"


def _lapor_nol(ringkasan, sebab_buang, f, cb):
    """
    Jelaskan ke mana perginya semua listing saat hasilnya nol.

    File Excel sengaja TIDAK ditulis: run nol-lead dulu tetap menghasilkan file
    yang isinya cuma baris judul, menumpuk di folder output tanpa memberi tahu
    apa pun. Yang dibutuhkan user bukan filenya, tapi sebabnya.
    """
    baris = [
        "⚠ 0 lead — tidak ada file dibuat.",
        f"   {ringkasan['Dilewati — filter awal dari feed']} dibuang filter awal "
        f"(rating/ulasan, sebelum halaman dibuka)",
        f"   {ringkasan['Dilewati — sudah ada di database']} dilewati — sudah ada di database",
        f"   {ringkasan['Dibuang filter akhir']} dibuang filter akhir"
        + (":" if sebab_buang else ""),
    ]
    for alasan, n in sorted(sebab_buang.items(), key=lambda x: -x[1]):
        baris.append(f"      • {n} {alasan}")
    if ringkasan["Duplikat dalam run ini"]:
        baris.append(f"   {ringkasan['Duplikat dalam run ini']} duplikat dalam run ini")
    if ringkasan["Gagal dibuka"]:
        baris.append(f"   {ringkasan['Gagal dibuka']} gagal dibuka")
    baris.append(f"   → Saran: {_saran_pelonggaran(ringkasan, sebab_buang, f)}")
    for b in baris:
        cb(None, b, 0)
    cb(98, "Selesai tanpa hasil — tidak ada file yang ditulis.", 0)


# ─── Penulisan file ───────────────────────────────────────────────────────────

def _rapikan(rows, kolom, sembunyikan=None, hanya=False):
    """
    DataFrame dengan kolom `kolom` di depan. `hanya=True` membuang kolom lain
    (dipakai export pilih-kolom); `sembunyikan` membuang kolom tertentu.
    """
    df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=kolom)
    for c in kolom:
        if hanya and c not in df.columns:
            df[c] = ""
    urut = [c for c in kolom if c in df.columns]
    if hanya:
        return df[urut]
    sembunyikan = set(sembunyikan or ())
    sisa = [c for c in df.columns if c not in urut and not c.startswith("_")
            and c != "place_key" and c not in sembunyikan]
    return df[urut + sisa] if urut else df


def _tulis_output(leads, updates, gemini, ringkasan, output_format, cb, profil=None):
    pf = profil or PROFIL["webdev"]
    OUTPUT_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    awalan = pf["prefix"]
    base = OUTPUT_DIR / f"{awalan}_{timestamp}"

    df_leads = _rapikan(leads, pf["kolom"], pf.get("sembunyikan"))

    if output_format != "excel":
        filename = f"{awalan}_{timestamp}.csv"
        df_leads.to_csv(f"{base}.csv", index=False, encoding="utf-8-sig")
        cb(98, f"File tersimpan: {filename}", len(leads))
        return filename

    sheets = {"Leads Baru": df_leads}
    if updates:
        sheets["Update Kontak"] = _rapikan(updates, KOLOM_UPDATE, pf.get("sembunyikan"))
    if gemini:
        sheets["Gemini (Perlu Verifikasi)"] = _rapikan(gemini, pf["kolom"],
                                                       pf.get("sembunyikan"))

    filename = f"{awalan}_{timestamp}.xlsx"
    _save_excel(sheets, str(base), ringkasan, label=pf.get("label"),
                bagian_ringkasan=pf.get("ringkasan"), urutan_tier=pf.get("tier"))
    cb(98, f"File tersimpan: {filename}", len(leads))
    return filename


# Warna baris per tier — supaya prioritas terbaca tanpa menyortir apa pun.
_WARNA_TIER = {
    scoring.TIER_PANAS:  "#FFD6D6",
    scoring.TIER_HANGAT: "#FFF2CC",
    scoring.TIER_DINGIN: "#EDF2F7",
    scoring.TIER_ARSIP:  "#F2F2F2",
    komponen.TIER_TINGGI: "#FFD6D6",
    komponen.TIER_SEDANG: "#FFF2CC",
    komponen.TIER_RENDAH: "#EDF2F7",
}

# Kolom berisi URL yang ditulis sebagai hyperlink di Excel.
KOLOM_TAUTAN = ("whatsapp_link", "website", "website_utama", "source_url",
                "instagram", "facebook", "tiktok", "tokopedia", "shopee",
                "marketplace_lain", "linkedin", "youtube")

PROFIL = {
    "webdev": {
        "nama": "webdev",
        "nilai": scoring.nilai_lead,
        "kolom": COLUMN_ORDER,
        "sembunyikan": {"lolos_filter", "whatsapp_web", "website_utama"},
        "prefix": "gmaps",
        "tier": scoring.URUTAN_TIER,
        "ringkasan": (("tier", "LEAD PER TIER"),
                      ("jasa_utama", "JASA YANG DITAWARKAN"),
                      # Berapa lead jatuh ke tim web, marketing, dan kreatif —
                      # dipakai untuk membagi tindak lanjut begitu file dibuka.
                      ("jalur", "LEAD PER TIM"),
                      ("area_pencarian", "LEAD PER AREA")),
    },
    "komponen": {
        "nama": "komponen",
        "nilai": komponen.nilai_lead,
        "kolom": komponen.COLUMN_ORDER,
        "sembunyikan": komponen.KOLOM_TERSEMBUNYI,
        "prefix": "komponen",
        "tier": komponen.URUTAN_TIER,
        "label": {k: lbl for k, lbl, _ in komponen.KOLOM_EXPORT},
        "ringkasan": (("tier", "LEAD PER PRIORITAS"),
                      ("segmen", "LEAD PER SEGMEN"),
                      ("kota", "LEAD PER KOTA"),
                      ("area_pencarian", "LEAD PER AREA")),
    },
}


def _save_excel(sheets, base, ringkasan=None, label=None, bagian_ringkasan=None,
                urutan_tier=None):
    out = f"{base}.xlsx"
    writer = pd.ExcelWriter(out, engine="xlsxwriter")
    wb = writer.book

    hdr = wb.add_format({"bold": True, "bg_color": "#1F4E79", "font_color": "white",
                         "border": 1, "align": "center", "valign": "vcenter",
                         "text_wrap": True})
    lnk = wb.add_format({"font_color": "#0563C1", "underline": True})
    fmt_tier = {t: wb.add_format({"bg_color": w, "valign": "vcenter", "text_wrap": True})
                for t, w in _WARNA_TIER.items()}
    fmt_normal = wb.add_format({"valign": "vcenter", "text_wrap": True})
    fmt_skor = {t: wb.add_format({"bg_color": w, "bold": True, "align": "center"})
                for t, w in _WARNA_TIER.items()}

    for nama_sheet, df in sheets.items():
        df.to_excel(writer, index=False, sheet_name=nama_sheet[:31])
        ws = writer.sheets[nama_sheet[:31]]
        cols = list(df.columns)
        i_tier = cols.index("tier") if "tier" in cols else -1
        i_skor = cols.index("skor_pembeli") if "skor_pembeli" in cols else -1
        kolom_link = {cols.index(c) for c in KOLOM_TAUTAN if c in cols}

        for ci, col in enumerate(cols):
            ws.write(0, ci, (label or {}).get(col, col), hdr)
            lebar = {"alasan_pitch": 55, "jasa_utama": 26, "jasa_pendukung": 30,
                     "alamat": 40, "perubahan": 45}.get(col)
            if lebar is None:
                # Pada sheet tanpa baris, .max() mengembalikan NaN — dan di Python
                # max(NaN, 5) tetap NaN, begitu juga min(NaN, 40). NaN yang lolos
                # ke set_column ditulis sebagai width="nan" di XML, dan Excel
                # menolak membuka filenya ("perlu diperbaiki"). Jadi lebar hanya
                # dihitung kalau memang ada isinya.
                lebar = max(len(col) + 2, 15)
                if len(df):
                    try:
                        terpanjang = int(df[col].astype(str).map(len).max())
                        lebar = min(max(terpanjang, len(col)) + 2, 40)
                    except (ValueError, TypeError):
                        pass
            ws.set_column(ci, ci, lebar)

        for ri in range(len(df)):
            # iloc[ri, i] — akses per POSISI; df.iloc[ri][i] akan menafsirkan i
            # sebagai nama kolom dan gagal.
            tier = str(df.iloc[ri, i_tier]) if i_tier >= 0 else ""
            baris_fmt = fmt_tier.get(tier, fmt_normal)
            for ci, val in enumerate(df.iloc[ri].tolist()):
                kosong = val is None or (isinstance(val, float) and math.isnan(val))
                v = "" if kosong else val
                if ci == i_skor:
                    ws.write(ri + 1, ci, v, fmt_skor.get(tier, baris_fmt))
                elif ci in kolom_link and str(v).startswith("http"):
                    ws.write_url(ri + 1, ci, str(v), lnk, str(v)[:100])
                else:
                    ws.write(ri + 1, ci, v, baris_fmt)

        ws.freeze_panes(1, 1)
        if len(df):
            ws.autofilter(0, 0, len(df), max(len(cols) - 1, 0))

    if ringkasan:
        _sheet_ringkasan(writer, wb, hdr, sheets, ringkasan,
                         bagian_ringkasan or PROFIL["webdev"]["ringkasan"],
                         urutan_tier or scoring.URUTAN_TIER)

    writer.close()


def _sheet_ringkasan(writer, wb, hdr, sheets, ringkasan, bagian, urutan_tier):
    """Sheet ringkasan: langsung terbaca sebagai rencana campaign."""
    ws = wb.add_worksheet("Ringkasan")
    writer.sheets["Ringkasan"] = ws
    judul = wb.add_format({"bold": True, "font_size": 12, "font_color": "#1F4E79"})
    tebal = wb.add_format({"bold": True})
    ws.set_column(0, 0, 42)
    ws.set_column(1, 1, 14)

    baris = 0
    ws.write(baris, 0, "HASIL RUN", judul)
    baris += 1
    for k, v in ringkasan.items():
        ws.write(baris, 0, k)
        ws.write(baris, 1, v)
        baris += 1

    df_leads = sheets.get("Leads Baru")
    if df_leads is not None and len(df_leads):
        for kolom, judul_bagian in bagian:
            if kolom not in df_leads.columns:
                continue
            baris += 1
            ws.write(baris, 0, judul_bagian, judul)
            baris += 1
            hitung = df_leads[kolom].fillna("(kosong)").value_counts()
            if kolom == "tier":
                hitung = hitung.reindex(
                    [t for t in urutan_tier if t in hitung.index])
            for nama, n in hitung.items():
                ws.write(baris, 0, str(nama))
                ws.write(baris, 1, int(n), tebal)
                baris += 1

    baris += 1
    ws.write(baris, 0, "Catatan", judul)
    ws.write(baris + 1, 0,
             "Sheet 'Gemini (Perlu Verifikasi)' berisi data dari model AI — "
             "nomor & alamatnya belum diverifikasi, cek dulu sebelum dihubungi.")


# ─── Entry point ──────────────────────────────────────────────────────────────

def run_scrape(params: dict, callback, should_stop=None) -> str:
    """
    Jalankan satu run scraping.

    should_stop — fungsi tanpa argumen yang mengembalikan True bila user menekan
    tombol Stop.

    Setiap lead ditulis ke database begitu selesai dibaca, dan file hasil dirakit
    dari database. Jadi apa pun yang terjadi di tengah jalan — Stop, error, atau
    proses yang mati sama sekali — lead yang sudah terkumpul tidak hilang. Untuk
    melanjutkan run yang terputus, isi `params["_run_id"]` dengan run_id lama.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    params.setdefault("_run_id", uuid.uuid4().hex[:12])
    return asyncio.run(_async_main(params, callback, should_stop))


def run_scrape_komponen(params: dict, callback, should_stop=None) -> str:
    """
    Run scraping untuk ruang "komponen" (calon pembeli komponen komputer).

    Pipeline-nya sama dengan run_scrape, tapi seluruh data ditulis ke
    data/komponen.db, lead dinilai dengan scrapers/komponen.py, dan bisnis yang
    tutup atau tidak punya kontak yang diminta disaring saat scraping.
    """
    import db_komponen
    params["_profil"] = "komponen"
    if "_kecualikan" not in params:
        params["_kecualikan"] = komponen.daftar_kecualian(
            db_komponen.pengaturan().get("kategori_dikecualikan"))
    with db.ruang("komponen"):
        return run_scrape(params, callback, should_stop)
