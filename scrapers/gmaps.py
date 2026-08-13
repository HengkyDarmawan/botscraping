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
  6. Simpan ke database dan tulis Excel banyak sheet.
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
from scrapers import enrich, scoring

OUTPUT_DIR = Path("output")

# Kolom paling menentukan tindakan ditaruh paling kiri: begitu file dibuka,
# yang pertama terlihat adalah siapa, seberapa panas, dan mau ditawari apa.
COLUMN_ORDER = [
    "nama_bisnis", "kategori", "skor_pembeli", "tier",
    "jasa_utama", "alasan_pitch", "jasa_pendukung",
    "telepon", "whatsapp_link", "email", "alamat",
    "rating", "jumlah_ulasan", "skor_popularitas",
    "website", "web_status", "web_platform", "web_https", "web_mobile",
    "web_load_ms", "web_ada_pixel", "web_ada_toko", "web_tahun_update",
    "instagram", "facebook", "tiktok",
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


async def _human_type(element, text):
    for char in text:
        await element.type(char, delay=random.randint(50, 150))
        if random.random() < 0.10:
            await asyncio.sleep(random.uniform(0.1, 0.4))


# Nomor telepon & skor popularitas hidup di scrapers/scoring.py supaya ada satu
# sumber kebenaran. Nama lama dipertahankan sebagai alias karena modul lain
# (scrapers/website_leads.py) sudah mengimpornya.
_hitung_score = scoring.skor_popularitas
_is_wa = scoring.nomor_wa_valid
_wa_link = scoring.link_wa


def _has_website(row):
    """False jika website None/kosong/'-'/'n/a'."""
    w = str(row.get("website") or "").strip().lower()
    return bool(w) and w not in ("-", "n/a", "none")


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
# require_whatsapp, yang dulu tidak punya cara untuk mengatakan "email saja juga
# boleh" — lead yang cuma punya email selalu terbuang padahal bisa di-blast email.
MODE_WA = "wa"
MODE_TELEPON = "telepon"
MODE_EMAIL = "email"
MODE_APA_SAJA = "apa_saja"
MODE_BEBAS = "bebas"


def _mode_kontak(f):
    """
    Baca mode kontak dari dict filter, dengan terjemahan dari sakelar lama.

    config.py dan run lama masih mengirim require_phone/require_whatsapp, jadi
    keduanya tetap dihormati selama `mode_kontak` tidak diisi.
    """
    mode = str(f.get("mode_kontak") or "").strip().lower()
    if mode in (MODE_WA, MODE_TELEPON, MODE_EMAIL, MODE_APA_SAJA, MODE_BEBAS):
        return mode
    if f.get("require_whatsapp"):
        return MODE_WA
    if f.get("require_phone"):
        return MODE_TELEPON
    return MODE_BEBAS


def _kontak_kurang(row, mode):
    """Alasan lead ini gugur syarat kontak, atau None bila lolos."""
    telepon = row.get("telepon")
    email = str(row.get("email") or "").strip()
    if mode == MODE_WA:
        return None if _is_wa(telepon) else "bukan nomor HP/WhatsApp"
    if mode == MODE_TELEPON:
        return None if telepon else "tidak ada nomor telepon"
    if mode == MODE_EMAIL:
        return None if email else "tidak ada email"
    if mode == MODE_APA_SAJA:
        punya = (telepon or email or str(row.get("instagram") or "").strip()
                 or str(row.get("facebook") or "").strip())
        return None if punya else "tidak ada kontak sama sekali"
    return None


def _passes_filter(row, f):
    """
    Alasan lead ini dibuang filter akhir, atau None bila lolos.

    Mengembalikan alasan (bukan cuma True/False) supaya run yang menghasilkan nol
    lead bisa menjelaskan ke mana perginya semua listing, bukan sekadar melapor
    "0 hasil" dan menyuruh user menebak filter mana yang terlalu ketat.
    """
    if f.get("filter_rating"):
        try:
            if float(row.get("rating") or 0) < f.get("min_rating", 0):
                return f"rating < {f.get('min_rating', 0)}"
        except (TypeError, ValueError):
            return f"rating < {f.get('min_rating', 0)}"
    if f.get("filter_reviews"):
        try:
            if int(row.get("jumlah_ulasan") or 0) < f.get("min_reviews", 0):
                return f"ulasan < {f.get('min_reviews', 0)}"
        except (TypeError, ValueError):
            return f"ulasan < {f.get('min_reviews', 0)}"
    kurang = _kontak_kurang(row, _mode_kontak(f))
    if kurang:
        return kurang
    if f.get("require_no_website") and _has_website(row):
        return "sudah punya website"
    if not f.get("sertakan_tutup") and _bisnis_tutup(row):
        return "bisnis tutup"
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
        .map(x => x.href)
        .filter(h => h && !h.includes('/maps/') && !h.startsWith('javascript'));
      situs = luar[0] || '';
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
            "telepon": _baca_telepon_kartu(teks),
            "kategori": _baca_kategori_kartu(teks),
            "website": (m.get("situs") or "").strip(),
            "tutup": any(t in teks.lower() for t in STATUS_TUTUP),
        })
        if max_results and len(kartu) >= max_results:
            break
    return kartu


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
    # Tautan keluar di kartu = bukti pasti punya website.
    if f.get("require_no_website") and kartu.get("website"):
        return False
    # Nomor yang terlihat di kartu tapi jelas bukan seluler (mis. "(021) 788...").
    if _mode_kontak(f) == MODE_WA and kartu.get("telepon"):
        if not _is_wa(kartu["telepon"]):
            return False
    if not f.get("sertakan_tutup") and kartu.get("tutup"):
        return False
    return True


# ─── Scroll feed sampai kuota terpenuhi ───────────────────────────────────────

# Dua batas keras: tanpa ini, filter yang hampir tidak ada yang lolos (mis. rating
# 4.8 di area kecil) akan membuat scroll berjalan sampai feed Google habis.
MAKS_PUTARAN_SCROLL = 30
MAKS_LIPAT_KARTU = 8


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
        await _random_delay(1.5, 2.5)

    return kartu


# ─── Ekstrak detail ───────────────────────────────────────────────────────────

async def _extract_detail(page):
    async def st(sel, fb=None):
        try:
            el = await page.query_selector(sel)
            if el:
                return (await el.inner_text()).strip()
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

    tambahan = await _extract_sinyal_profil(page)

    return {
        "nama_bisnis": nama, "kategori": kategori, "rating": rating,
        "jumlah_ulasan": jumlah_ulasan, "telepon": telepon,
        "alamat": alamat, "website": website, "jam_operasional": jam,
        **tambahan,
    }


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
        panel = await page.query_selector('div[role="main"]')
        teks = (await panel.inner_text()) if panel else ""
    except Exception:
        teks = ""
    low = teks.lower()

    if teks:
        ada_klaim = any(k in low for k in (
            "klaim bisnis ini", "klaim bisnis", "claim this business",
            "own this business", "apakah anda pemilik bisnis ini",
        ))
        if not ada_klaim:
            try:
                el = await page.query_selector('a[href*="business.google.com"]')
                ada_klaim = el is not None
            except Exception:
                pass
        hasil["sudah_diklaim"] = not ada_klaim

        for t in STATUS_TUTUP:
            if t in low:
                hasil["status_buka"] = t.title()
                break

        if any(k in low for k in ("tambahkan foto", "add a photo", "tambah foto")):
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


# ─── Buka satu listing ────────────────────────────────────────────────────────

async def _buka_listing(browser, ua, href, params, percobaan=2):
    """
    Buka halaman detail satu bisnis dengan context (dan IP) sendiri.

    Dicoba ulang sekali kalau timeout: di koneksi yang tidak stabil, satu
    percobaan yang gagal berarti satu lead hilang percuma.
    """
    rotate_ip = params.get("rotate_ip_per_listing", True)
    galat = None
    for percobaan_ke in range(1, percobaan + 1):
        proxy = (_get_apify_proxy(params) if rotate_ip
                 else _get_apify_proxy(params, session_id="listing-fixed"))
        context = await browser.new_context(
            user_agent=ua, locale="id-ID",
            timezone_id="Asia/Jakarta", viewport={"width": 1366, "height": 768},
            proxy=proxy,
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)
        try:
            await page.goto(href, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_selector("h1", timeout=15_000)
            await _random_delay()
            data = await _extract_detail(page)
            return data, None
        except PlaywrightTimeoutError as e:
            galat = f"timeout (percobaan {percobaan_ke}/{percobaan})"
            _ = e
        except Exception as e:
            galat = f"{type(e).__name__}: {e}"
        finally:
            await context.close()
        if percobaan_ke < percobaan:
            await _random_delay(2.0, 4.0)
    return None, galat


# Data hasil pemeriksaan website & kontak tambahan: tetap berlaku walau run
# sekarang tidak mengambilnya (mis. enrichment dimatikan).
_FIELD_WARISAN = (
    "web_status", "web_https", "web_mobile", "web_load_ms", "web_platform",
    "web_tahun_update", "web_ada_pixel", "web_ada_toko",
    "email", "instagram", "facebook", "tiktok",
    "sudah_diklaim", "jumlah_foto", "rentang_harga",
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
    return row


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
                        offset_pct, range_pct, coords="", should_stop=None):
    """
    Scrape satu kombinasi "jenis bisnis + area".

    Return dict:
      baru      — list lead yang benar-benar baru (atau sudah kedaluwarsa)
      update    — list bisnis lama yang kontaknya berubah
      dilewati  — jumlah bisnis lama yang dilewati karena aturan anti-duplikat
      filter_awal — jumlah listing yang dibuang dari kartu feed tanpa dibuka
    """
    FEED = 'div[role="feed"]'
    hasil = {"baru": [], "update": [], "dilewati": 0, "filter_awal": 0, "gagal": 0}

    filters = params.get("_filters", {})
    dedup_aktif = bool(params.get("dedup_enabled", True))
    dedup_bulan = float(params.get("dedup_bulan", 6) or 6)
    refresh_aktif = bool(params.get("refresh_kontak", True))
    refresh_hari = int(params.get("refresh_interval_hari", 30) or 30)

    # ── Fase 1: kumpulkan kartu listing dengan satu session/IP ──
    context = await browser.new_context(
        user_agent=ua, locale="id-ID",
        timezone_id="Asia/Jakarta", viewport={"width": 1366, "height": 768},
        proxy=_get_apify_proxy(params, session_id="search"),
    )
    page = await context.new_page()
    await Stealth().apply_stealth_async(page)

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
        hemat = hasil["filter_awal"] * 6 / 60
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
    konkuren = max(1, min(int(params.get("listing_konkuren") or 3), 8))
    sem = asyncio.Semaphore(konkuren)
    selesai = 0

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

    async def _satu_listing(idx, k):
        href = k["href"]
        pct = _pct()
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
                data, galat = await _buka_listing(browser, ua, href, params)
                if data is None:
                    hasil["dilewati"] += 1
                    cb(pct, f"⚠ Gagal cek {nama_tampil}: {galat}", len(hasil["baru"]))
                    await _random_delay()
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
                    hasil["update"].append(baris)
                    cb(pct, f"🔄 Update: {baris.get('nama_bisnis', '?')} | "
                            f"{baris['perubahan']}", len(hasil["baru"]))
                else:
                    hasil["dilewati"] += 1
                    cb(pct, f"✓ Tidak ada perubahan: {nama_tampil}",
                       len(hasil["baru"]))
                await _random_delay()
                return

            # ── Bisnis baru atau sudah kedaluwarsa → ambil penuh ──
            label = "✨ Baru" if status == db.BARU else "♻ Data kedaluwarsa, diambil ulang"
            cb(pct, f"[{idx}/{total}] {label}: "
                    f"{nama_tampil if nama_tampil != '?' else 'mengambil detail...'}",
               len(hasil["baru"]))

            data, galat = await _buka_listing(browser, ua, href, params)
            if data is None:
                hasil["gagal"] += 1
                cb(pct, f"⚠ Listing {idx} dilewati: {galat}", len(hasil["baru"]))
                await _random_delay()
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
                        await _random_delay()
                        return

            baris = _rakit_row(data, href, area_tag, tanggal, k, place_key=kunci)
            hasil["baru"].append(baris)
            cb(pct, f"✓ {baris.get('nama_bisnis', '?')} | "
                    f"⭐{baris.get('rating') or '-'} ({baris.get('jumlah_ulasan') or 0}) | "
                    f"Tel: {baris.get('telepon') or '-'}", len(hasil["baru"]))
            await _random_delay()
        except Exception as e:
            hasil["gagal"] += 1
            cb(pct, f"⚠ Listing {idx} gagal: {type(e).__name__}: {e}",
               len(hasil["baru"]))

    if konkuren > 1:
        cb(offset_pct, f"Membuka {total} listing, {konkuren} sekaligus...", 0)
    await asyncio.gather(*(_kerjakan(i, k) for i, k in enumerate(kartu, 1)))
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
        response = client.models.generate_content(
            model="gemini-2.0-flash",
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
    }
    params["_filters"] = filter_flags
    params.setdefault("listing_konkuren", int(opsi("listing_konkuren", 3) or 3))
    params.setdefault("dedup_enabled", bool(opsi("dedup_enabled", True)))
    params.setdefault("dedup_bulan", float(opsi("dedup_bulan", 6) or 6))
    params.setdefault("refresh_kontak", bool(opsi("refresh_kontak", True)))
    params.setdefault("refresh_interval_hari", int(opsi("refresh_interval_hari", 30) or 30))

    output_format = params.get("output_format", "excel")
    headless = bool(params.get("headless", True))
    enrich_aktif = bool(opsi("enrich_website", True))
    enrich_konkuren = int(opsi("enrich_konkuren", 5) or 5)
    enrich_timeout = int(opsi("enrich_timeout", 12) or 12)
    use_gemini = bool(params.get("use_gemini", False))
    gemini_api_key = str(params.get("gemini_api_key", "") or "").strip()
    simpan_gemini = bool(params.get("simpan_gemini_ke_db", False))

    db.init_db()

    ua = UserAgent(os=["windows", "macos", "linux"])
    user_agent = ua.chrome

    semua_baru, semua_update = [], []
    total_dilewati = total_filter_awal = total_gagal = 0
    n_queries = max(len(search_targets), 1)

    launch_args = [
        f"--user-agent={user_agent}",
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox", "--disable-dev-shm-usage", "--lang=id-ID,id",
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=launch_args)
        try:
            for i, target in enumerate(search_targets):
                if should_stop and should_stop():
                    cb(None, "⏹ Dihentikan sebelum target berikutnya.", len(semua_baru))
                    break
                jenis = target.get("jenis", "")
                area = target.get("area", "")
                coords = target.get("coords", "")
                query = f"{jenis} {area}".strip()
                offset = int((i / n_queries) * 75)
                rng = int(75 / n_queries)
                cb(offset, f"[{i+1}/{n_queries}] Mulai scraping: {query}", len(semua_baru))

                try:
                    hasil = await _scrape_query(browser, user_agent, query, area,
                                                max_results, params, cb, offset, rng,
                                                coords, should_stop)
                except Exception as e:
                    # Satu target yang bermasalah tidak boleh menghanguskan hasil
                    # target-target sebelumnya: pada run 30 wilayah, kegagalan di
                    # wilayah ke-5 dulu membuat semuanya hilang.
                    cb(offset + rng, f"❌ Gagal pada '{query}': {type(e).__name__}: {e} "
                                     f"— dilanjutkan ke target berikutnya.", len(semua_baru))
                    continue

                semua_baru.extend(hasil["baru"])
                semua_update.extend(hasil["update"])
                total_dilewati += hasil["dilewati"]
                total_filter_awal += hasil["filter_awal"]
                total_gagal += hasil["gagal"]

                db.log_search(jenis, area, total_ditemukan=len(hasil["baru"]) + hasil["dilewati"],
                              baru=len(hasil["baru"]), dilewati=hasil["dilewati"],
                              diupdate=len(hasil["update"]))
                cb(offset + rng,
                   f"Subtotal '{query}': {len(hasil['baru'])} baru, "
                   f"{hasil['dilewati']} dilewati, {len(hasil['update'])} update",
                   len(semua_baru))
                if i < n_queries - 1:
                    await _random_delay(3.0, 5.0)
        finally:
            await browser.close()

    # ── Deduplikasi dalam-run berdasarkan identitas bisnis ──
    # Kunci lama (digit telepon) membuang cabang berbeda yang memakai nomor
    # pusat yang sama, dan diam-diam menghapus baris tanpa telepon.
    dilihat, deduped, dibuang = set(), [], 0
    for row in semua_baru:
        kunci = row.get("place_key") or row.get("source_url")
        if kunci and kunci in dilihat:
            dibuang += 1
            cb(None, f"⏭ Duplikat dalam run ini: {row.get('nama_bisnis', '?')}", None)
            continue
        if kunci:
            dilihat.add(kunci)
        deduped.append(row)
    semua_baru = deduped

    # ── Periksa website tiap lead ──
    if enrich_aktif and semua_baru and not (should_stop and should_stop()):
        cb(78, f"Memeriksa website {len(semua_baru)} lead...", len(semua_baru))
        await enrich.enrich_banyak(semua_baru, konkuren=enrich_konkuren, cb=cb,
                                   should_stop=should_stop, timeout=enrich_timeout)
    perlu_enrich_ulang = [r for r in semua_update if r.pop("_website_berubah", False)]
    if enrich_aktif and perlu_enrich_ulang:
        cb(84, f"Memeriksa ulang {len(perlu_enrich_ulang)} website yang berubah...",
           len(semua_baru))
        await enrich.enrich_banyak(perlu_enrich_ulang, konkuren=enrich_konkuren,
                                   cb=cb, should_stop=should_stop, timeout=enrich_timeout)

    # ── Hitung rekomendasi jasa + skor pembeli, lalu simpan ──
    cb(86, "Menghitung rekomendasi jasa & skor potensi pembeli...", len(semua_baru))
    for row in semua_baru + semua_update:
        _lengkapi_dari_db(row)
        cabang = db.hitung_cabang(row.get("nama_bisnis"))
        scoring.nilai_lead(row, jumlah_cabang=cabang)

    for row in semua_baru:
        if row.get("place_key"):
            try:
                db.upsert_business(row)
            except Exception as e:
                cb(None, f"⚠ Gagal simpan {row.get('nama_bisnis', '?')}: {e}", None)
    for row in semua_update:
        if row.get("place_key"):
            try:
                db.simpan_intelijen(row["place_key"], row.get("skor_pembeli"),
                                    row.get("tier"), row.get("jasa_utama"),
                                    row.get("jasa_pendukung"), row.get("alasan_pitch"))
            except Exception:
                pass

    # ── Gemini: sumber terpisah, tidak dicampur ──
    hasil_gemini = []
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
                scoring.nilai_lead(row)
                try:
                    db.upsert_business(row)
                except Exception:
                    pass
    elif use_gemini and not gemini_api_key:
        cb(90, "⚠ Gemini dilewati — API key tidak diisi", len(semua_baru))

    # ── Filter akhir (syarat yang butuh data detail) ──
    lolos, sebab_buang = [], {}
    for r in semua_baru:
        alasan = _passes_filter(r, filter_flags)
        if alasan is None:
            lolos.append(r)
        else:
            sebab_buang[alasan] = sebab_buang.get(alasan, 0) + 1
    dibuang_filter = len(semua_baru) - len(lolos)
    lolos.sort(key=lambda r: r.get("skor_pembeli", 0), reverse=True)
    semua_update.sort(key=lambda r: r.get("skor_pembeli", 0), reverse=True)

    cb(95, f"Selesai: {len(lolos)} leads baru, {len(semua_update)} update, "
           f"{total_dilewati} dilewati (duplikat). Menyimpan...", len(lolos))

    ringkasan = {
        "Leads baru (lolos filter)": len(lolos),
        "Dibuang filter akhir": dibuang_filter,
        "Dilewati — sudah ada di database": total_dilewati,
        "Dilewati — filter awal dari feed": total_filter_awal,
        "Duplikat dalam run ini": dibuang,
        "Kontak berubah (diupdate)": len(semua_update),
        "Gagal dibuka": total_gagal,
        "Hasil Gemini (perlu verifikasi)": len(hasil_gemini),
    }

    if not (lolos or semua_update or hasil_gemini):
        _lapor_nol(ringkasan, sebab_buang, filter_flags, cb)
        return None

    return _tulis_output(lolos, semua_update, hasil_gemini, ringkasan,
                         output_format, cb)


def _saran_pelonggaran(ringkasan, sebab_buang, f):
    """Saran konkret berdasarkan penyebab yang PALING banyak membuang lead."""
    if sebab_buang:
        terbesar = max(sebab_buang, key=sebab_buang.get)
        if terbesar == "sudah punya website":
            return ('matikan "wajib belum punya website" — bisnis yang sudah punya '
                    'website tetap prospek untuk jasa redesign & iklan')
        if terbesar.startswith("bukan nomor HP"):
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

def _rapikan(rows, kolom):
    df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=kolom)
    urut = [c for c in kolom if c in df.columns]
    sisa = [c for c in df.columns if c not in urut and not c.startswith("_")
            and c != "place_key"]
    return df[urut + sisa] if urut else df


def _tulis_output(leads, updates, gemini, ringkasan, output_format, cb):
    OUTPUT_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = OUTPUT_DIR / f"gmaps_{timestamp}"

    df_leads = _rapikan(leads, COLUMN_ORDER)

    if output_format != "excel":
        filename = f"gmaps_{timestamp}.csv"
        df_leads.to_csv(f"{base}.csv", index=False, encoding="utf-8-sig")
        cb(98, f"File tersimpan: {filename}", len(leads))
        return filename

    sheets = {"Leads Baru": df_leads}
    if updates:
        sheets["Update Kontak"] = _rapikan(updates, KOLOM_UPDATE)
    if gemini:
        sheets["Gemini (Perlu Verifikasi)"] = _rapikan(gemini, COLUMN_ORDER)

    filename = f"gmaps_{timestamp}.xlsx"
    _save_excel(sheets, str(base), ringkasan)
    cb(98, f"File tersimpan: {filename}", len(leads))
    return filename


# Warna baris per tier — supaya prioritas terbaca tanpa menyortir apa pun.
_WARNA_TIER = {
    scoring.TIER_PANAS:  "#FFD6D6",
    scoring.TIER_HANGAT: "#FFF2CC",
    scoring.TIER_DINGIN: "#EDF2F7",
    scoring.TIER_ARSIP:  "#F2F2F2",
}


def _save_excel(sheets, base, ringkasan=None):
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
        kolom_link = {cols.index(c) for c in ("whatsapp_link", "website", "source_url",
                                              "instagram", "facebook", "tiktok")
                      if c in cols}

        for ci, col in enumerate(cols):
            ws.write(0, ci, col, hdr)
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
        _sheet_ringkasan(writer, wb, hdr, sheets, ringkasan)

    writer.close()


def _sheet_ringkasan(writer, wb, hdr, sheets, ringkasan):
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
        for kolom, judul_bagian in (("tier", "LEAD PER TIER"),
                                    ("jasa_utama", "JASA YANG DITAWARKAN"),
                                    ("area_pencarian", "LEAD PER AREA")):
            if kolom not in df_leads.columns:
                continue
            baris += 1
            ws.write(baris, 0, judul_bagian, judul)
            baris += 1
            hitung = df_leads[kolom].fillna("(kosong)").value_counts()
            if kolom == "tier":
                hitung = hitung.reindex(
                    [t for t in scoring.URUTAN_TIER if t in hitung.index])
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
    tombol Stop. Hasil yang sudah terkumpul tetap disimpan.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    return asyncio.run(_async_main(params, callback, should_stop))
