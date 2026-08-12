"""
scrapers/enrich.py — Periksa website calon klien.

Data Google Maps hanya memberi tahu *apakah* sebuah bisnis mencantumkan website.
Modul ini membuka website itu dan mencari sinyal yang menentukan JASA APA yang
pantas ditawarkan:

  • website mati / tanpa HTTPS / tidak ramah HP / lambat / dibangun di platform
    gratisan  → prospek jasa WEB DEVELOPMENT
  • website sehat tapi tanpa pixel iklan, atau sudah pasang pixel (bukti pernah
    belanja marketing)                        → prospek jasa DIGITAL MARKETING

Sekaligus memanen email dan akun sosial media sebagai jalur kontak cadangan.

Memakai requests + BeautifulSoup yang sudah ada di requirements.txt (tidak ada
dependensi baru). Request yang memblokir dijalankan lewat asyncio.to_thread agar
event loop Playwright tidak ikut berhenti.
"""
import asyncio
import re
import time
import urllib.parse
from datetime import datetime

import requests
from bs4 import BeautifulSoup

TIMEOUT = 12
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# Nilai default: dipakai saat bisnis tidak punya website sama sekali, dan sebagai
# bentuk hasil yang selalu sama supaya pemanggil tidak perlu cek key.
HASIL_KOSONG = {
    "web_status": "tidak_ada",
    "web_https": None,
    "web_mobile": None,
    "web_load_ms": None,
    "web_platform": "",
    "web_tahun_update": None,
    "web_ada_pixel": None,
    "web_ada_toko": None,
    "web_final_url": "",
    "email": "",
    "instagram": "",
    "facebook": "",
    "tiktok": "",
}

# Website yang sebenarnya cuma tautan ke sosmed/marketplace/link-in-bio.
# Untuk penilaian, ini dihitung SAMA DENGAN belum punya website: bisnisnya sudah
# jualan online tapi belum punya aset sendiri — justru prospek web dev terbaik.
_HOST_SOSMED = (
    "instagram.com", "facebook.com", "fb.com", "tiktok.com",
    # Tautan WhatsApp & "link in bio" — sangat lazim dipakai UMKM Indonesia
    # sebagai pengganti website.
    "wa.me", "wa.link", "whatsapp.com", "linktr.ee", "lynk.id", "linkin.bio",
    "bio.link", "heylink.me", "solo.to", "campsite.bio",
    "twitter.com", "x.com", "youtube.com", "t.me",
)
_HOST_MARKETPLACE = (
    "tokopedia.com", "shopee.co.id", "bukalapak.com", "lazada.co.id",
    "blibli.com", "tokopedia.link", "shopee.com",
)

# Platform yang menandakan website belum digarap serius (domain bukan milik
# sendiri / builder gratisan) — target upgrade ke website profesional.
PLATFORM_GRATISAN = {
    "Blogspot", "Wix (subdomain)", "WordPress.com", "Google Sites",
    "Weebly", "Webnode", "sosmed_saja", "marketplace",
}

_RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_RE_PENANDA_HAKCIPTA = re.compile(r"(?:©|&copy;|copyright)", re.I)
_RE_TAHUN = re.compile(r"(20\d{2})")

# Potongan yang sering muncul di alamat email palsu / contoh / aset gambar.
_EMAIL_SAMPAH = (
    "example.com", "example.org", "yourdomain", "domain.com", "email.com",
    "sentry.io", "wixpress.com", "@2x", ".png", ".jpg", ".jpeg", ".gif",
    ".webp", ".svg", "core-js", "polyfill", "@sentry", "godaddy.com",
    "namecheap", "sample@", "user@", "nama@", "info@example",
)

_JEJAK_PIXEL = (
    "googletagmanager.com", "google-analytics.com", "gtag(", "gtm.js",
    "connect.facebook.net", "fbq(", "fbevents.js", "clarity.ms",
    "analytics.tiktok.com", "hotjar.com", "ads-twitter.com",
)

_JEJAK_TOKO = (
    "tambah ke keranjang", "keranjang belanja", "add to cart", "checkout",
    "beli sekarang", "woocommerce", "snap.midtrans", "xendit", "add-to-cart",
    "shopping-cart", "keranjang saya",
)

_LINK_KONTAK = re.compile(r"(kontak|contact|hubungi|about|tentang|profil)", re.I)


# ─── Deteksi platform ─────────────────────────────────────────────────────────

def _host_cocok(host, daftar):
    """
    Cocokkan host dengan daftar domain, per bagian nama — bukan substring.

    Pencocokan substring berbahaya di sini: daftar sosmed memuat "x.com"
    (Twitter), sehingga "phoenixdental.com" akan ikut cocok dan website milik
    klinik itu salah dianggap sekadar tautan media sosial.
    """
    h = (host or "").lower().split(":")[0].strip(".")
    if h.startswith("www."):
        h = h[4:]
    return any(h == d or h.endswith("." + d) for d in daftar)


def _deteksi_platform(host, html, headers):
    h = (host or "").lower()
    low = (html or "")[:200_000].lower()
    hdr = " ".join(f"{k}:{v}" for k, v in (headers or {}).items()).lower()

    if _host_cocok(h, _HOST_SOSMED):
        return "sosmed_saja"
    if _host_cocok(h, _HOST_MARKETPLACE):
        return "marketplace"
    if "blogspot." in h:
        return "Blogspot"
    if h.endswith(".wordpress.com") or ".wordpress.com" in h:
        return "WordPress.com"
    if "sites.google.com" in h:
        return "Google Sites"
    if "wixsite.com" in h:
        return "Wix (subdomain)"
    if "weebly.com" in h:
        return "Weebly"
    if "webnode." in h:
        return "Webnode"
    if "wix.com" in low or "x-wix-" in hdr or "_wixcssimports" in low:
        return "Wix"
    if "cdn.shopify.com" in low or "shopify" in hdr:
        return "Shopify"
    if "webflow.io" in h or "webflow.com" in low:
        return "Webflow"
    if "squarespace" in low:
        return "Squarespace"
    if "wp-content" in low or "wp-includes" in low or "wp-json" in low:
        return "WordPress"
    m = re.search(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)',
                  html or "", re.I)
    if m:
        return m.group(1).strip()[:40]
    return "custom"


def _bersihkan_email(kandidat):
    hasil = []
    for e in kandidat:
        e = e.strip().strip(".,;:").lower()
        if not e or len(e) > 80:
            continue
        if any(s in e for s in _EMAIL_SAMPAH):
            continue
        if e not in hasil:
            hasil.append(e)
    return hasil


def _ambil_sosmed(soup, base_url):
    ditemukan = {"instagram": "", "facebook": "", "tiktok": ""}
    for a in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(base_url, a["href"])
        low = href.lower()
        # Tautan "bagikan ke ..." bukan akun milik bisnis.
        if any(s in low for s in ("sharer", "share.php", "/share?", "intent/",
                                  "developers.", "business.facebook.com")):
            continue
        if not ditemukan["instagram"] and "instagram.com/" in low:
            jalur = low.split("instagram.com/", 1)[1].strip("/")
            if jalur and not jalur.startswith(("p/", "reel", "explore", "accounts")):
                ditemukan["instagram"] = href.split("?")[0]
        elif not ditemukan["facebook"] and ("facebook.com/" in low or "fb.com/" in low):
            pemisah = "facebook.com/" if "facebook.com/" in low else "fb.com/"
            jalur = low.split(pemisah, 1)[1].strip("/")
            if jalur and not jalur.startswith(("sharer", "plugins", "tr?", "dialog")):
                ditemukan["facebook"] = href.split("?")[0]
        elif not ditemukan["tiktok"] and "tiktok.com/@" in low:
            ditemukan["tiktok"] = href.split("?")[0]
    return ditemukan


def _tahun_terakhir(teks):
    """
    Tahun hak cipta terbaru di halaman — penanda kapan situs terakhir diurus.

    Diambil yang TERBESAR, karena footer sering ditulis sebagai rentang
    ("© 2001-2026"): yang menentukan situs masih diurus atau tidak adalah tahun
    akhirnya, bukan tahun berdirinya.
    """
    teks = teks or ""
    sekarang = datetime.now().year
    tahun = []
    for m in _RE_PENANDA_HAKCIPTA.finditer(teks):
        # Tahun yang relevan ada tepat di sekitar penanda hak cipta.
        jendela = teks[m.start(): m.end() + 40]
        tahun += [int(t) for t in _RE_TAHUN.findall(jendela)]
    if not tahun:
        # Cadangan: 800 karakter terakhir halaman (area footer).
        tahun = [int(t) for t in _RE_TAHUN.findall(teks[-800:])]
    tahun = [t for t in tahun if 2000 <= t <= sekarang + 1]
    return max(tahun) if tahun else None


# ─── Pemeriksaan utama (memblokir) ────────────────────────────────────────────

def periksa_website(url, timeout=TIMEOUT):
    """
    Buka satu website dan kembalikan sinyal-sinyalnya.

    Selalu mengembalikan dict dengan key yang sama seperti HASIL_KOSONG, jadi
    pemanggil tidak perlu memeriksa keberadaan key. Tidak pernah melempar
    exception — website milik orang lain memang sering mati atau aneh, dan itu
    justru informasi yang kita cari.
    """
    hasil = dict(HASIL_KOSONG)
    url = str(url or "").strip()
    if not url or url in ("-", "n/a", "none", "None"):
        return hasil

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    sesi = requests.Session()
    sesi.headers.update({
        "User-Agent": UA,
        "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })

    mulai = time.monotonic()
    resp = None
    sertifikat_valid = True
    try:
        resp = sesi.get(url, timeout=timeout, allow_redirects=True)
    except requests.exceptions.SSLError:
        # Sertifikat bermasalah tetap dicatat sebagai "tidak aman", tapi isinya
        # tetap diambil supaya sinyal lain (platform, email) tidak hilang.
        sertifikat_valid = False
        try:
            resp = sesi.get(url, timeout=timeout, allow_redirects=True, verify=False)
        except Exception:
            resp = None
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        hasil["web_status"] = "mati"
        hasil["web_load_ms"] = int((time.monotonic() - mulai) * 1000)
        return hasil
    except Exception:
        hasil["web_status"] = "error"
        return hasil

    hasil["web_load_ms"] = int((time.monotonic() - mulai) * 1000)

    if resp is None:
        hasil["web_status"] = "mati"
        hasil["web_https"] = False
        return hasil

    final_url = resp.url or url
    host = urllib.parse.urlparse(final_url).netloc.lower()
    hasil["web_final_url"] = final_url
    hasil["web_https"] = bool(final_url.startswith("https://")) and sertifikat_valid

    if resp.status_code >= 500:
        hasil["web_status"] = "mati"
    elif resp.status_code >= 400:
        hasil["web_status"] = "error"
    else:
        hasil["web_status"] = "aktif"

    html = resp.text or ""
    low = html.lower()

    hasil["web_platform"] = _deteksi_platform(host, html, dict(resp.headers))
    hasil["web_ada_pixel"] = any(j in low for j in _JEJAK_PIXEL)
    hasil["web_ada_toko"] = any(j in low for j in _JEJAK_TOKO)

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return hasil

    hasil["web_mobile"] = soup.find("meta", attrs={"name": re.compile("viewport", re.I)}) is not None

    teks = soup.get_text(" ", strip=True)
    hasil["web_tahun_update"] = _tahun_terakhir(teks)
    hasil.update(_ambil_sosmed(soup, final_url))

    # Email: mailto: paling dipercaya, baru regex di seluruh halaman.
    kandidat = [a["href"].split("mailto:", 1)[1].split("?")[0]
                for a in soup.find_all("a", href=True)
                if a["href"].lower().startswith("mailto:")]
    kandidat += _RE_EMAIL.findall(teks)
    bersih = _bersihkan_email(kandidat)

    # Belum ketemu di beranda → coba halaman kontak/tentang (maksimal 2).
    if not bersih and hasil["web_status"] == "aktif":
        dicoba = 0
        for a in soup.find_all("a", href=True):
            if dicoba >= 2:
                break
            if not _LINK_KONTAK.search(a["href"] + " " + a.get_text(" ", strip=True)):
                continue
            tujuan = urllib.parse.urljoin(final_url, a["href"])
            if urllib.parse.urlparse(tujuan).netloc.lower() != host:
                continue
            dicoba += 1
            try:
                r2 = sesi.get(tujuan, timeout=timeout, allow_redirects=True,
                              verify=sertifikat_valid)
                s2 = BeautifulSoup(r2.text or "", "html.parser")
                k2 = [x["href"].split("mailto:", 1)[1].split("?")[0]
                      for x in s2.find_all("a", href=True)
                      if x["href"].lower().startswith("mailto:")]
                k2 += _RE_EMAIL.findall(s2.get_text(" ", strip=True))
                bersih = _bersihkan_email(k2)
                if not hasil["instagram"]:
                    hasil.update({k: v for k, v in _ambil_sosmed(s2, tujuan).items() if v})
                if bersih:
                    break
            except Exception:
                continue

    hasil["email"] = bersih[0] if bersih else ""
    return hasil


# ─── Pembungkus async ─────────────────────────────────────────────────────────

async def enrich_website(url, timeout=TIMEOUT):
    """Versi async dari periksa_website — dijalankan di thread terpisah."""
    return await asyncio.to_thread(periksa_website, url, timeout)


async def enrich_banyak(rows, konkuren=5, cb=None, should_stop=None,
                        field_url="website", timeout=TIMEOUT):
    """
    Periksa website untuk banyak lead sekaligus dan tempelkan hasilnya ke tiap row.

    `rows` diubah di tempat. Lead tanpa website dilewati (tidak membuang waktu
    request) tapi tetap diberi field default supaya bentuk datanya seragam.

    cb(pct, pesan, jumlah) — callback progress yang sama dengan scraper lain.
    should_stop() — dicek tiap lead; bila True, sisanya dilewati.
    """
    perlu = [r for r in rows if str(r.get(field_url) or "").strip() not in ("", "-", "n/a")]
    for r in rows:
        if r not in perlu:
            r.update(dict(HASIL_KOSONG))

    total = len(perlu)
    if not total:
        return rows

    sem = asyncio.Semaphore(max(1, int(konkuren)))
    selesai = 0

    async def satu(row):
        nonlocal selesai
        if should_stop and should_stop():
            row.update(dict(HASIL_KOSONG))
            return
        async with sem:
            try:
                hasil = await enrich_website(row.get(field_url), timeout)
            except Exception as e:
                hasil = dict(HASIL_KOSONG)
                hasil["web_status"] = "error"
                if cb:
                    cb(None, f"⚠ Gagal cek website {row.get(field_url)}: {e}", None)
            # Email/sosmed dari GMaps (bila ada) tidak ditimpa nilai kosong.
            for k, v in list(hasil.items()):
                if k in ("email", "instagram", "facebook", "tiktok") and not v:
                    hasil[k] = row.get(k) or ""
            row.update(hasil)
            selesai += 1
            if cb:
                cb(None, f"🌐 [{selesai}/{total}] {row.get('nama_bisnis', '?')} — "
                         f"{hasil['web_status']} / {hasil['web_platform'] or '-'}", None)

    await asyncio.gather(*(satu(r) for r in perlu))
    return rows


def url_sosmed(url):
    """True bila URL ini sebenarnya akun sosmed / marketplace / link-in-bio."""
    u = str(url or "").strip().lower()
    if not u or u in ("-", "n/a", "none"):
        return False
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    host = urllib.parse.urlparse(u).netloc
    return _host_cocok(host, _HOST_SOSMED + _HOST_MARKETPLACE)


def website_efektif(row):
    """
    True bila bisnis benar-benar punya website sendiri yang hidup.

    Tautan ke Instagram/marketplace/link-in-bio TIDAK dihitung sebagai website —
    itu justru prospek web development terbaik.
    """
    url = str(row.get("website") or "").strip().lower()
    if not url or url in ("-", "n/a", "none"):
        return False
    if row.get("web_platform") in ("sosmed_saja", "marketplace"):
        return False
    if url_sosmed(url):
        return False
    if row.get("web_status") in ("mati", "error"):
        return False
    return True
