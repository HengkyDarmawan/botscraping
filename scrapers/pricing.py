"""
scrapers/pricing.py — Price comparison scraper Shopee & Tokopedia
=================================================================
Dua mode:
  • "produk" : cari berdasarkan keyword di hasil pencarian marketplace
  • "toko"   : ambil produk dari satu/lebih toko (database data/stores.json)

Engine anti-deteksi: Camoufox (Firefox binary-patched, humanize) dengan
fallback otomatis ke Playwright + stealth bila Camoufox tidak tersedia.
Mendukung proxy residential opsional & toggle headless.

Catatan jujur: Tokopedia (Cloudflare) relatif lolos dengan Camoufox.
Shopee memakai DataDome — tanpa residential proxy sering kena captcha/blok.
Fallback paling stabil tetap Gemini AI (mode produk).
"""
import asyncio
import contextlib
import json
import re
import sys
import random
import urllib.parse
from datetime import datetime
from pathlib import Path

import pandas as pd
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

import db
from . import mp_api
from . import mp_common
from . import mp_harga
from . import mp_lexicon
from . import mp_match
from . import mp_session

OUTPUT_DIR = Path("output")
DATA_DIR = Path("data")
STORES_FILE = DATA_DIR / "stores.json"

_STEALTH = Stealth()

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# Penanda halaman challenge / blokir (Cloudflare, DataDome, captcha)
_CHALLENGE_MARKERS = [
    "just a moment", "checking your browser", "enable javascript",
    "verify you are human", "verifying you are human", "captcha",
    "akses ditolak", "access denied", "unusual traffic",
    "please wait while we", "ddos protection", "are you a robot",
]


# ─── Util umum ────────────────────────────────────────────────────────────────

async def _random_delay(mn=1.5, mx=3.0):
    await asyncio.sleep(random.uniform(mn, mx))


# Parsing rupiah tinggal di scrapers/mp_common.py supaya modul marketplace lain
# memakai aturan yang sama persis. Alias di bawah menjaga pemanggil lama tetap jalan.
_parse_price = mp_common.parse_harga
_format_price = mp_common.format_harga


# ─── Database toko ────────────────────────────────────────────────────────────

def _baca_stores_json() -> list:
    try:
        return json.loads(STORES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


# Toko sendiri milik user. Dipakai sekali saat migrasi supaya `is_own` tidak
# perlu dicentang manual untuk toko yang sudah lama ada di stores.json.
_TOKO_SENDIRI = {"jayapc", "jaya_pc"}

_sudah_migrasi = False


def _migrasi_stores():
    """Pindahkan data/stores.json ke tabel mp_stores, sekali saja.

    stores.json dipertahankan sebagai cadangan yang bisa dibaca manusia, tapi
    sejak sekarang mp_stores yang jadi sumber kebenaran — statistik harga perlu
    JOIN ke produk, dan itu tidak bisa dilakukan terhadap file JSON.
    """
    global _sudah_migrasi
    if _sudah_migrasi:
        return
    _sudah_migrasi = True
    try:
        if db.mp_stores(hanya_aktif=False):
            return
        for s in _baca_stores_json():
            if not s.get("id"):
                continue
            s = dict(s)
            s["is_own"] = int((s.get("username") or "").lower() in _TOKO_SENDIRI)
            db.mp_store_upsert(s)
    except Exception:
        # Migrasi gagal tidak boleh menjatuhkan halaman /pricing.
        pass


def load_stores() -> list:
    """Daftar toko dari mp_stores (migrasi otomatis dari stores.json)."""
    _migrasi_stores()
    try:
        return db.mp_stores(hanya_aktif=False)
    except Exception:
        return _baca_stores_json()


def save_stores(stores: list):
    """Tulis daftar toko ke mp_stores + salinan stores.json."""
    for s in stores:
        try:
            db.mp_store_upsert(s)
        except Exception:
            pass
    DATA_DIR.mkdir(exist_ok=True)
    ringkas = [{k: s.get(k) for k in
                ("id", "platform", "nama", "url", "username", "shop_id", "is_own")}
               for s in stores]
    STORES_FILE.write_text(json.dumps(ringkas, indent=2, ensure_ascii=False),
                           encoding="utf-8")


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "toko"


def _username_from_url(platform: str, url: str) -> str:
    """Ambil username toko dari URL marketplace."""
    try:
        path = urllib.parse.urlparse(url).path.strip("/")
    except Exception:
        return ""
    if not path:
        return ""
    return path.split("/")[0]


def add_store(platform: str, nama: str, url: str, is_own=False) -> dict:
    """Tambah toko ke database. Mengembalikan entri yang ditambahkan."""
    platform = (platform or "").strip().lower()
    nama = (nama or "").strip()
    url = (url or "").strip()
    if platform not in ("tokopedia", "shopee"):
        raise ValueError("Platform harus 'tokopedia' atau 'shopee'")
    if not url.startswith("http"):
        # Anggap user hanya mengetik username/nama toko
        username = _slugify(url) if url else _slugify(nama)
        base = "https://www.tokopedia.com" if platform == "tokopedia" else "https://shopee.co.id"
        url = f"{base}/{username}"
    username = _username_from_url(platform, url)
    if not nama:
        nama = username
    stores = load_stores()
    new_id = f"{platform[:3]}-{_slugify(nama)}-{random.randint(100, 999)}"
    entry = {"id": new_id, "platform": platform, "nama": nama, "url": url,
             "username": username, "is_own": int(bool(is_own))}
    stores.append(entry)
    save_stores(stores)
    return entry


def set_store_own(store_id: str, is_own) -> bool:
    """Tandai toko sebagai milik sendiri / kompetitor.

    Penandaan ini yang menentukan baris mana dikeluarkan dari statistik pasar,
    jadi ia harus bisa diubah kapan saja — bukan hanya saat toko dibuat.
    """
    stores = load_stores()
    if not any(s.get("id") == store_id for s in stores):
        return False
    db.mp_store_set_own(store_id, is_own)
    save_stores(load_stores())
    return True


def delete_store(store_id: str) -> bool:
    stores = load_stores()
    if not any(s.get("id") == store_id for s in stores):
        return False
    # Produk & riwayat toko ini ikut dihapus; membiarkannya menggantung berarti
    # statistik pasar terus memakai harga dari toko yang sudah tidak dipantau.
    db.mp_hapus("store", store_id)
    save_stores([s for s in load_stores() if s.get("id") != store_id])
    return True


# ─── Selector berlapis per platform ───────────────────────────────────────────

_SEL = {
    "tokopedia": {
        "card": [
            '[data-testid="master-product-card"]',
            '[data-testid="divProductWrapper"]',
            'div[class*="prd_container-card"]',
            'a[href*="/product"][data-testid]',
            'div.css-5wh65g',
        ],
        "nama": [
            '[data-testid="spnSRPProdName"]',
            '[class*="prd_link-product-name"]',
            'span.css-20kt3o',
        ],
        "harga": [
            '[data-testid="spnSRPProdPrice"]',
            '[data-testid="linkProductPrice"]',
            '[class*="prd_link-product-price"]',
            'span.css-o5uqvq',
        ],
        "toko": [
            '[data-testid="spnSRPProdTabShopName"]',
            '[class*="prd_link-product-shop-name"]',
            'span.css-1rn0irl',
        ],
        "rating": [
            '[data-testid="spnSRPProdRating"]',
            '[class*="prd_rating-average-text"]',
            'span.css-t70v7i',
        ],
        "terjual": [
            '[data-testid="lblItemSoldCountReview"]',
            '[class*="prd_label-integrity"]',
        ],
    },
    "shopee": {
        "card": [
            '.shopee-search-item-result__item',
            '[data-sqe="item"]',
            'li.col-xs-2-4',
            '.col-xs-2-4',
            '.shop-search-result-view__item',
        ],
        "nama": [
            'div[data-sqe="name"]',
            '.line-clamp-2',
            '._10Wbs-',
        ],
        "harga": [
            'span[class*="font-medium"]',
            '._1xk7ak',
            '.shopee-product-rating__price',
        ],
        "toko": [],
        "rating": [
            '.shopee-rating-stars',
            '._1Wr91I',
        ],
        "terjual": [
            '._18SLan',
            'div[class*="text-shopee-black"]',
        ],
    },
}


async def _first_text(item, selectors) -> str:
    for sel in selectors:
        try:
            el = await item.query_selector(sel)
            if el:
                txt = (await el.inner_text()).strip()
                if txt:
                    return txt
        except Exception:
            continue
    return ""


# ─── Browser session (Camoufox → fallback Playwright) ─────────────────────────

def _cdp_url():
    try:
        import config
        return getattr(config, "CHROME_CDP_URL", "http://localhost:9222")
    except Exception:
        return "http://localhost:9222"


@contextlib.asynccontextmanager
async def _connect_my_chrome(cb):
    """Yield `page` dari Chrome user yang sudah login (via CDP). Tidak menutup browser user."""
    cdp = _cdp_url()
    cb(None, f"🔗 Engine: Chrome Anda (sesi login) — menyambung {cdp}...", 0)
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp)
        except Exception as e:
            cb(None, f"❌ Tidak bisa menyambung ke Chrome ({type(e).__name__}). "
                     f"Klik 'Buka Chrome Scraping' lalu login dulu, baru jalankan lagi.", 0)
            raise RuntimeError("Chrome CDP tidak terhubung") from e
        context = browser.contexts[0] if browser.contexts else await browser.new_context(
            locale="id-ID", timezone_id="Asia/Jakarta")
        page = await context.new_page()
        cb(None, "✓ Terhubung ke Chrome Anda (sesi login dipakai)", 0)
        try:
            yield page
        finally:
            with contextlib.suppress(Exception):
                await page.close()  # tutup hanya tab kita; biarkan browser user tetap terbuka


@contextlib.asynccontextmanager
async def _browser_session(headless: bool, proxy, cb, engine="camoufox"):
    """Yield satu `page` siap pakai.

    engine='my_chrome' → sambung ke Chrome user (login). Selain itu: Camoufox → fallback Playwright.
    """
    if engine == "my_chrome":
        async with _connect_my_chrome(cb) as page:
            yield page
        return

    AsyncCamoufox = None
    try:
        from camoufox.async_api import AsyncCamoufox as _AC
        AsyncCamoufox = _AC
    except ImportError:
        cb(None, "⚠ Camoufox tidak terpasang — fallback Playwright + stealth", 0)

    if AsyncCamoufox is not None:
        kwargs = {"headless": headless, "humanize": True, "locale": "id-ID"}
        if proxy:
            kwargs["proxy"] = proxy
            kwargs["geoip"] = True
        mgr = None
        try:
            mgr = AsyncCamoufox(**kwargs)
            browser = await mgr.__aenter__()
            page = await browser.new_page()
        except Exception as e:
            cb(None, f"⚠ Camoufox gagal start ({type(e).__name__}: {e}); fallback Playwright. "
                     f"Jalankan: python -m camoufox fetch", 0)
            if mgr is not None:
                with contextlib.suppress(Exception):
                    await mgr.__aexit__(None, None, None)
        else:
            cb(None, "🦊 Engine: Camoufox (anti-deteksi aktif)", 0)
            try:
                yield page
            finally:
                with contextlib.suppress(Exception):
                    await mgr.__aexit__(None, None, None)
            return

    # ── Fallback: Playwright + stealth ──
    launch_kwargs = {
        "headless": headless,
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-http2",
        ],
    }
    if proxy:
        launch_kwargs["proxy"] = proxy
    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            locale="id-ID",
            timezone_id="Asia/Jakarta",
            viewport={"width": 1366, "height": 768},
            user_agent=DESKTOP_UA,
        )
        page = await context.new_page()
        await _STEALTH.apply_stealth_async(page)
        cb(None, "🎭 Engine: Playwright + stealth", 0)
        try:
            yield page
        finally:
            with contextlib.suppress(Exception):
                await browser.close()


async def _pass_challenge(page, cb, label="", max_wait=24, manual=False) -> bool:
    """True bila halaman lolos challenge/blokir. Tunggu bertahap bila perlu.

    manual=True (engine Chrome login): tunggu lebih lama & minta user selesaikan
    captcha 'tekan & tahan' (DataDome/Cloudflare) secara manual di jendela Chrome.
    """
    if manual:
        max_wait = max(max_wait, 90)
    rounds = max(max_wait // 4, 1)
    warned = False
    for attempt in range(rounds):
        try:
            txt = await page.evaluate("document.body ? document.body.innerText.slice(0, 3000) : ''")
            title = await page.title()
        except Exception:
            txt, title = "", ""
        blob = f"{title}\n{txt}".lower()
        if not any(m in blob for m in _CHALLENGE_MARKERS):
            if warned:
                cb(None, f"✓ {label}: verifikasi lolos, melanjutkan...", 0)
            return True
        if manual:
            if not warned:
                cb(None, f"🖐 {label}: ada VERIFIKASI/captcha — selesaikan manual di jendela Chrome "
                         f"(tekan & tahan tombol). Scraper menunggu hingga {max_wait} detik...", 0)
                warned = True
            else:
                cb(None, f"⏳ {label}: menunggu verifikasi diselesaikan ({(attempt + 1) * 4}/{max_wait} dtk)...", 0)
        else:
            cb(None, f"⚠ {label}: terdeteksi proteksi bot — menunggu ({attempt + 1}/{rounds})...", 0)
        await asyncio.sleep(4)
    if manual:
        cb(None, f"❌ {label}: verifikasi belum selesai. Coba ulang & selesaikan captcha lebih cepat, "
                 f"atau pakai residential proxy.", 0)
    else:
        cb(None, f"❌ {label}: masih terblokir proteksi bot. Saran: pakai engine 'Chrome saya' (login) "
                 f"+ selesaikan captcha manual, aktifkan residential proxy, atau fallback Gemini AI.", 0)
    return False


async def _human_scroll(page, times=5):
    for _ in range(times):
        try:
            await page.mouse.wheel(0, random.randint(500, 1100))
        except Exception:
            await page.evaluate("window.scrollBy(0, 800)")
        await _random_delay(1.0, 2.2)


# ─── Ekstraksi produk dari halaman (search atau toko) ─────────────────────────

def _keyword_match(nama: str, keyword_filter: str) -> bool:
    """True bila nama produk memuat minimal satu token keyword (case-insensitive)."""
    if not keyword_filter:
        return True
    name_low = (nama or "").lower()
    tokens = [t for t in re.split(r"\s+", keyword_filter.lower()) if len(t) >= 2]
    if not tokens:
        return True
    return any(t in name_low for t in tokens)


async def _extract_products(page, platform, max_results, toko_name, cb,
                            base_pct=20, span=15, keyword_filter=None):
    sel = _SEL[platform]
    items = []
    for c in sel["card"]:
        items = await page.query_selector_all(c)
        if items:
            break

    label = platform.title()
    cb(None, f"{label}: {len(items)} kartu produk ditemukan"
             + (f" — filter keyword '{keyword_filter}'" if keyword_filter else ""), 0)
    results = []
    # Bila ada filter keyword, telusuri lebih banyak kartu untuk mengumpulkan match.
    scan_limit = len(items) if keyword_filter else max_results
    seen = 0

    for item in items[:scan_limit]:
        if len(results) >= max_results:
            break
        seen += 1
        try:
            card_text = (await item.inner_text()).strip()
            nama = await _first_text(item, sel["nama"])
            harga_int = _parse_price(await _first_text(item, sel["harga"]))
            if not harga_int:  # fallback: regex dari seluruh teks kartu
                harga_int = _parse_price(card_text)
            if not nama:
                nama = card_text.split("\n")[0][:100] if card_text else "-"

            if not harga_int and (not nama or nama == "-"):
                continue
            if not _keyword_match(nama, keyword_filter):
                continue

            toko = toko_name or (await _first_text(item, sel["toko"])) or "-"
            rating = await _first_text(item, sel["rating"]) or "-"
            terjual = await _first_text(item, sel["terjual"]) or "-"

            results.append({
                "platform": label,
                "nama_produk": nama,
                "harga": harga_int,
                "harga_tampil": _format_price(harga_int),
                "toko": toko,
                "rating": rating.replace("\n", " ")[:12],
                "terjual": terjual.replace("\n", " ")[:20],
            })
            pct = base_pct + int(len(results) / max(max_results, 1) * span)
            cb(pct, f"{label} [{len(results)}]: {nama[:38]} | {_format_price(harga_int)}", 0)
        except Exception:
            continue

    if keyword_filter and not results:
        cb(None, f"⚠ {label}: tidak ada produk cocok keyword '{keyword_filter}' di toko ini "
                 f"(dari {seen} kartu diperiksa).", 0)
    return results


# ─── Mode PRODUK (search) ─────────────────────────────────────────────────────

def _search_url(platform, keyword):
    q = urllib.parse.quote_plus(keyword)
    if platform == "tokopedia":
        return f"https://www.tokopedia.com/search?st=product&q={q}"
    return f"https://shopee.co.id/search?keyword={q}"


async def _scrape_search(page, platform, keyword, max_results, cb, base_pct, manual=False):
    url = _search_url(platform, keyword)
    cb(base_pct, f"{platform.title()}: membuka pencarian '{keyword}'...", 0)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await _random_delay(3.0, 5.0)
        if not await _pass_challenge(page, cb, label=platform.title(), manual=manual):
            return []
        await _human_scroll(page, times=5)
        return await _extract_products(page, platform, max_results, None, cb, base_pct + 5, 18)
    except Exception as e:
        cb(None, f"{platform.title()} error: {type(e).__name__}: {e}", 0)
        return []


# ─── Mode TOKO ────────────────────────────────────────────────────────────────

# Pembentukan URL toko pindah ke mp_api._url_toko — satu tempat saja, karena
# lapis API dan lapis DOM harus membuka halaman yang sama persis.


def _ke_baris(rec, store):
    """Record panen → baris hasil yang dipakai Excel & analisis.

    Kolom lama dipertahankan supaya `_build_comparison` dan `_write_excel` tidak
    perlu diubah, sambil membawa serta field baru (url, harga coret, sumber)
    yang selama ini hilang.
    """
    return {
        "platform": (rec.get("platform") or "").title(),
        "nama_produk": rec.get("nama_produk") or "-",
        "harga": int(rec.get("harga") or 0),
        "harga_tampil": _format_price(rec.get("harga")),
        "toko": store.get("nama") or store.get("username") or "-",
        "rating": rec.get("rating") or "-",
        "terjual": rec.get("terjual") or 0,
        "harga_coret": _format_price(rec.get("harga_coret")),
        "url": rec.get("url") or "",
        "sumber": rec.get("sumber") or "",
        "product_key": rec.get("product_key") or "",
        # Wajib ikut: statistik pasar mengeluarkan baris toko sendiri, dan tanpa
        # penanda ini harga kita ikut menaikkan rata-rata pembandingnya sendiri.
        "is_own": int(bool(store.get("is_own"))),
        "store_id": store.get("id") or "",
    }


def _simpan_shop_id(store):
    """Tulis balik shop_id hasil resolve ke stores.json.

    `panen_toko` menyetelnya di dict yang ada di memori lalu run selesai dan
    hasilnya hilang, jadi setiap run mengulang resolve dari nol — satu navigasi
    ekstra ke halaman toko, dan satu kesempatan ekstra kena captcha.
    """
    shop_id = store.get("shop_id")
    if not shop_id or not store.get("id"):
        return
    stores = load_stores()
    berubah = False
    for s in stores:
        if s.get("id") == store["id"] and s.get("shop_id") != shop_id:
            s["shop_id"] = str(shop_id)
            berubah = True
    if berubah:
        save_stores(stores)


async def _scrape_store(page, store, max_results, cb, base_pct,
                        manual=False, keyword="", berhenti=None, pacer=None,
                        run_id=None):
    """Panen satu toko lewat mp_api.panen_toko (api → anchor → selector).

    Menggantikan dua fungsi lama yang langsung menembak `_extract_products`
    dengan selector halaman pencarian — penyebab tiga run 0-hasil pada 14 Agustus.
    """
    nama = store.get("nama") or store.get("username") or "?"
    label = f"Toko '{nama}'"
    if keyword:
        label += f" (keyword '{keyword}')"
    cb(base_pct, f"{label}: memanen...", 0)
    try:
        rows, sumber, sebab = await mp_api.panen_toko(
            store, page, maks=max_results, keyword=keyword,
            sel=_SEL.get(store["platform"]), cb=cb, berhenti=berhenti,
            pacer=pacer)
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"

    _simpan_shop_id(store)

    if not rows:
        cb(None, f"⚠ {label}: 0 produk — {sebab}", 0)
        db.mp_store_status(store.get("id"), ok=False, error=sebab)
        return [], sebab or "tidak ada produk"

    # Simpan record MENTAH (17 field), bukan baris Excel yang sudah dipangkas —
    # `image_url`, `stok`, `lokasi`, dan `jumlah_ulasan` hilang di `_ke_baris`,
    # dan tanpa itu analisis berikutnya tidak punya bahan.
    baru = diperbarui = 0
    for r in rows:
        try:
            aksi = db.mp_upsert_product(r, store_id=store.get("id"),
                                        is_own=store.get("is_own"))
            baru += (aksi == "insert")
            diperbarui += (aksi == "update")
            db.mp_snapshot_price(r.get("product_key"), r.get("harga"),
                                 r.get("terjual"), r.get("stok"), run_id=run_id)
        except Exception as e:
            cb(None, f"⚠ gagal menyimpan '{(r.get('nama_produk') or '')[:40]}': {e}", 0)
    db.mp_store_status(store.get("id"), ok=True)

    cb(None, f"✓ {label}: {len(rows)} produk (sumber: {sumber}) "
             f"— {baru} baru, {diperbarui} diperbarui", 0)
    return [_ke_baris(r, store) for r in rows], ""


# ─── Custom URL (Camoufox sendiri) ────────────────────────────────────────────

_AUTO_PRICE_SELECTORS = [
    '[class*="harga"]', '[class*="price"]', '[class*="rupiah"]',
    '.price-tag', '.product-price', '.item-price', '.harga', '.price',
    'span:has-text("Rp")', 'div:has-text("Rp")',
]
_AUTO_NAME_SELECTORS = [
    '[class*="product-name"]', '[class*="product-title"]',
    '[class*="item-name"]', '[class*="nama"]', 'h2', 'h3',
]


async def _scrape_custom_url(url, selector_harga, selector_nama, headless, proxy, cb, engine="camoufox"):
    results = []
    cb(75, f"Custom URL: membuka {url[:60]}...", 0)
    try:
        async with _browser_session(headless, proxy, cb, engine) as page:
            await page.goto(url, wait_until="load", timeout=60_000)
            await _random_delay(3.0, 5.0)
            if not await _pass_challenge(page, cb, label="Custom URL"):
                return results
            await _human_scroll(page, times=3)

            if selector_harga:
                price_els = await page.query_selector_all(selector_harga)
            else:
                price_els = []
                for s in _AUTO_PRICE_SELECTORS:
                    try:
                        found = await page.query_selector_all(s)
                        valid = []
                        for el in found:
                            val = _parse_price((await el.inner_text()).strip())
                            if 1_000 <= val <= 500_000_000:
                                valid.append(el)
                        if valid:
                            price_els = valid
                            cb(80, f"Custom URL: auto-detect '{s}' → {len(price_els)} harga", 0)
                            break
                    except Exception:
                        continue

            if not price_els:
                cb(90, "⚠ Custom URL: 0 harga ditemukan — isi CSS Selector Harga manual.", 0)
                return results

            if selector_nama:
                nama_els = await page.query_selector_all(selector_nama)
            else:
                nama_els = []
                for s in _AUTO_NAME_SELECTORS:
                    try:
                        found = await page.query_selector_all(s)
                        if found:
                            nama_els = found
                            break
                    except Exception:
                        continue

            for i, el in enumerate(price_els[:30]):
                try:
                    harga_int = _parse_price((await el.inner_text()).strip())
                    if not (1_000 <= harga_int <= 500_000_000):
                        continue
                    nama = (await nama_els[i].inner_text()).strip()[:100] if i < len(nama_els) else f"Produk {i + 1}"
                    results.append({
                        "platform": "Custom",
                        "nama_produk": nama,
                        "harga": harga_int,
                        "harga_tampil": _format_price(harga_int),
                        "toko": url,
                        "rating": "-",
                        "terjual": "-",
                    })
                except Exception:
                    continue
            cb(90, f"Custom URL: {len(results)} harga ditemukan ✓", len(results))
    except Exception as e:
        cb(90, f"Custom URL error: {type(e).__name__}: {e}", 0)
    return results


# ─── Analisis & rekomendasi ───────────────────────────────────────────────────

def _build_comparison(all_results: list) -> list:
    """Statistik pasar PER PRODUK, bukan satu angka untuk seluruh sheet.

    Versi lama merata-ratakan setiap baris hasil scrape apa pun produknya, lalu
    menyarankan `rata2 × 0.95` yang sama untuk semuanya — harga RAM dan GPU masuk
    ke satu rata-rata. Sekarang produk dikelompokkan dulu lewat `mp_match`, dan
    tiap kelompok punya statistiknya sendiri.

    Tiga hal yang dikeluarkan dari statistik pasar:
      • baris toko sendiri (`is_own`) — kalau tidak, harga kita ikut menaikkan
        rata-rata yang sedang kita bandingkan dengan diri sendiri
      • baris tebakan Gemini (`sumber == 'ai'`)
      • pencilan harga (aksesori, bundling, cicilan)
    """
    if not all_results:
        return []

    try:
        lex = mp_lexicon.bangun(_judul_per_toko(all_results))
        # Bekali tiap baris dengan token & kode model supaya `kelompokkan` bisa
        # memisahkan varian — bentuk yang sama seperti keluaran `mp_match.cari`.
        siap = []
        for r in all_results:
            tok = mp_lexicon.normalisasi(r.get("nama_produk") or "")
            r["_token"] = tok
            r["_model"] = sorted({t for t in tok if lex.kelas(t) == "model"})
            r["_skor"] = 1.0
            r.setdefault("store_id", r.get("toko"))
            siap.append(r)
        grup_list = mp_match.kelompokkan(siap, lex)
    except Exception:
        grup_list = []

    # Peta baris → hasil analisis kelompoknya. `id()` aman di sini karena
    # `kelompokkan` mengembalikan objek dict yang sama, bukan salinannya.
    per_baris = {}
    for g in grup_list:
        hasil = mp_harga.analisa(g, modal=None)
        stat = hasil.get("statistik") or {}
        if not stat.get("n"):
            continue
        seimbang = next((b for b in hasil.get("band", [])
                         if b["nama"] in ("Seimbang", "Harga Impas Minimum")), None)
        for b in g["baris"]:
            per_baris[id(b)] = (g, stat, seimbang)

    for r in all_results:
        for k in ("_token", "_model", "_skor"):
            r.pop(k, None)
        cocok = per_baris.get(id(r))
        if not cocok:
            r.setdefault("varian", "-")
            r.setdefault("median_pasar", "-")
            r.setdefault("rekomendasi_harga", "-")
            r.setdefault("vs_median", "-")
            r.setdefault("n_pembanding", 0)
            continue
        g, stat, seimbang = cocok
        r["varian"] = g["label"]
        r["n_pembanding"] = stat["n"]
        r["median_pasar"] = _format_price(stat["median"])
        r["rekomendasi_harga"] = seimbang["harga_tampil"] if seimbang else "-"
        if r.get("harga"):
            selisih = r["harga"] - stat["median"]
            r["vs_median"] = (f"+{_format_price(selisih)}" if selisih > 0
                              else f"-{_format_price(abs(selisih))}" if selisih else "0")
        else:
            r["vs_median"] = "-"

    all_results.sort(key=lambda x: (x.get("varian") or "",
                                    x.get("harga") or 999_999_999))
    return all_results


def _judul_per_toko(baris):
    peta = {}
    for r in baris:
        peta.setdefault(r.get("toko") or "?", []).append(r.get("nama_produk") or "")
    return peta


# ─── Gemini AI tambahan (mode produk) ─────────────────────────────────────────

async def _gemini_pricing(keyword, sources, max_results, api_key, cb):
    results = []
    plat_map = {"tokopedia": "Tokopedia", "shopee": "Shopee", "custom": "Online Store"}
    platforms_str = " dan ".join(plat_map.get(s, s.title()) for s in sources if s != "custom") or "Tokopedia dan Shopee"
    cb(None, f"Gemini AI: mencari harga '{keyword}' di {platforms_str}...", 0)
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        prompt = (
            f'Cari harga produk "{keyword}" di marketplace Indonesia ({platforms_str}).\n'
            f'Kembalikan JSON array maksimal {max_results} item:\n'
            '[\n'
            '  {"platform": "nama marketplace", "nama_produk": "nama produk", '
            '"harga": 1500000, "toko": "nama toko", "rating": "4.5", "terjual": "100 terjual"}\n'
            ']\n'
            'PENTING: harga harus integer angka saja (tanpa Rp/titik/koma).\n'
            'Kembalikan HANYA JSON array valid.'
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
        json_match = re.search(r'\[[\s\S]*\]', raw_text)
        if not json_match:
            cb(None, "⚠ Gemini Pricing: tidak ada JSON dalam respons", 0)
            return results
        json_str = re.sub(r',\s*([}\]])', r'\1', json_match.group())
        items = json.loads(json_str)
        for item in items[:max_results]:
            if not isinstance(item, dict):
                continue
            harga_raw = item.get("harga", 0)
            if isinstance(harga_raw, str):
                harga_int = _parse_price(harga_raw)
            else:
                try:
                    harga_int = int(harga_raw)
                except Exception:
                    harga_int = 0
            plat_name = str(item.get("platform") or "Marketplace").strip()
            hasil = {
                "platform": f"{plat_name} (via Gemini)",
                "nama_produk": str(item.get("nama_produk") or "-").strip(),
                "harga": harga_int,
                "harga_tampil": _format_price(harga_int),
                "toko": str(item.get("toko") or "-").strip(),
                "rating": str(item.get("rating") or "-").strip(),
                "terjual": str(item.get("terjual") or "-").strip(),
                # Ditandai supaya statistik pasar bisa mengeluarkannya. Tanpa
                # penanda ini tebakan model ikut menentukan median dan
                # rekomendasi harga — dan itu bug, bukan sekadar kelemahan.
                "sumber": "ai",
            }
            results.append(hasil)
            cb(None, f"✓ Gemini: {hasil['nama_produk'][:40]} | {hasil['harga_tampil']}", len(results))
    except ImportError:
        cb(None, "❌ google-genai belum terinstall. Jalankan: pip install google-genai", 0)
    except Exception as e:
        cb(None, f"❌ Gemini Pricing error: {type(e).__name__}: {e}", len(results))
    return results


# ─── Excel writer ─────────────────────────────────────────────────────────────

def _write_excel(all_results, cb) -> str:
    OUTPUT_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"pricing_{timestamp}.xlsx"
    out_path = OUTPUT_DIR / filename

    col_order = ["varian", "platform", "nama_produk", "harga_tampil", "harga_coret",
                 "toko", "rating", "terjual", "n_pembanding", "median_pasar",
                 "rekomendasi_harga", "vs_median", "sumber", "url"]
    df = pd.DataFrame(all_results)
    cols = [c for c in col_order if c in df.columns]
    df = df[cols] if cols else df

    writer = pd.ExcelWriter(str(out_path), engine="xlsxwriter")
    df.to_excel(writer, index=False, sheet_name="Harga")
    wb, ws = writer.book, writer.sheets["Harga"]
    hdr = wb.add_format({"bold": True, "bg_color": "#145A32", "font_color": "white", "align": "center"})
    rec_fmt = wb.add_format({"bg_color": "#D5F5E3", "bold": True})
    for ci, col in enumerate(cols):
        ws.write(0, ci, col, hdr)
        try:
            w = min(max(df[col].astype(str).map(len).max(), len(col)) + 2, 40)
        except Exception:
            w = 15
        ws.set_column(ci, ci, w)
    ws.freeze_panes(1, 0)

    if "rekomendasi_harga" in cols:
        rec_col = cols.index("rekomendasi_harga")
        for ri in range(len(df)):
            ws.write(ri + 1, rec_col, df.iloc[ri]["rekomendasi_harga"], rec_fmt)

    writer.close()
    cb(98, f"File tersimpan: {filename}", len(all_results))
    return filename


# ─── Main async ───────────────────────────────────────────────────────────────

def _build_proxy(params):
    if not params.get("use_proxy"):
        return None
    server = str(params.get("proxy_server", "") or "").strip()
    if not server:
        return None
    proxy = {"server": server}
    if params.get("proxy_username"):
        proxy["username"] = str(params["proxy_username"]).strip()
    if params.get("proxy_password"):
        proxy["password"] = str(params["proxy_password"]).strip()
    return proxy


def _lapor_nol(sebab_per_toko, cb):
    """Jelaskan kenapa hasilnya nol, dan JANGAN tulis file.

    Sebelum ini, run 0-hasil tetap menghasilkan pricing_*.xlsx 4991 byte tanpa
    kolom — menumpuk di output/ tanpa memberi tahu apa pun. Yang dibutuhkan user
    bukan filenya, tapi sebabnya. Pola ini menyalin `_lapor_nol` di gmaps.py:1334.
    """
    cb(None, "⚠ 0 produk — tidak ada file dibuat.", 0)
    for nama, sebab in sebab_per_toko:
        cb(None, f"   • {nama}: {sebab}", 0)
    if sebab_per_toko:
        cb(None, "   → Buka folder output/diag_* untuk melihat page.html & "
                 "screenshot halaman saat panen gagal.", 0)
    cb(100, "Selesai tanpa hasil — tidak ada file yang ditulis.", 0)
    return None


async def _async_main(params, cb, should_stop=None):
    mode = params.get("mode", "produk")
    keyword = str(params.get("keyword", "") or "").strip()
    sources = params.get("sources", ["tokopedia", "shopee"])
    store_ids = params.get("store_ids", []) or []
    custom_url = str(params.get("custom_url", "") or "").strip()
    custom_sel_harga = params.get("custom_sel_harga", "")
    custom_sel_nama = params.get("custom_sel_nama", "")
    max_results = int(params.get("max_results", 20))
    use_gemini = bool(params.get("use_gemini", False))
    gemini_api_key = str(params.get("gemini_api_key", "") or "").strip()
    headless = bool(params.get("headless", False))
    engine = params.get("engine", "camoufox")
    manual = (engine == "my_chrome")  # captcha bisa diselesaikan manual di jendela Chrome
    proxy = _build_proxy(params)

    if engine == "my_chrome":
        cb(2, "🔗 Engine: Chrome Anda (sesi login)", 0)
    if proxy:
        cb(2, f"🛡 Proxy aktif: {proxy['server']}", 0)

    all_results = []
    sebab_per_toko = []

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        db.mp_run_start(run_id, mode=mode, kueri=keyword, engine=engine,
                        store_ids=",".join(store_ids))
    except Exception:
        pass

    # ── MODE TOKO / KEYWORD+TOKO ──
    if mode in ("toko", "keyword_toko"):
        is_kw = (mode == "keyword_toko")
        if is_kw and not keyword:
            cb(100, "❌ Keyword kosong untuk mode Keyword + Toko.", 0)
            return None
        stores = load_stores()
        selected = [s for s in stores if s.get("id") in store_ids]
        if not selected:
            cb(100, "❌ Tidak ada toko dipilih. Tambah/pilih toko dulu.", 0)
            return None
        cb(5, f"Mode {'KEYWORD+TOKO' if is_kw else 'TOKO'}: {len(selected)} toko dipilih"
              + (f", keyword '{keyword}'" if is_kw else ""), 0)

        # Satu Pacer dipakai bersama seluruh toko dalam run ini: rem-nya menghitung
        # request kumulatif, jadi memberi tiap toko Pacer sendiri justru menghapus
        # gunanya. Selama ini web app memanggil panen_toko tanpa pacer sama sekali.
        pacer = mp_session.Pacer()

        async with _browser_session(headless, proxy, cb, engine) as page:
            total = len(selected)
            for idx, store in enumerate(selected):
                if should_stop and should_stop():
                    cb(None, "⏹ Dihentikan user — hasil sementara tetap disimpan.", 0)
                    break
                rem = pacer.harus_berhenti()
                if rem:
                    cb(None, f"⏹ Pacer: {rem}", 0)
                    sebab_per_toko.append(("(rem pacer)", rem))
                    break
                base = 8 + int(idx / total * 80)
                res, sebab = await _scrape_store(
                    page, store, max_results, cb, base, manual,
                    keyword=(keyword if is_kw else ""), berhenti=should_stop,
                    pacer=pacer, run_id=run_id)
                all_results.extend(res)
                if sebab:
                    sebab_per_toko.append((store.get("nama") or store.get("id"), sebab))
                cb(8 + int((idx + 1) / total * 80),
                   f"[{idx + 1}/{total}] Toko '{store['nama']}' selesai ({len(res)} produk)",
                   len(all_results))
                await _random_delay(3.0, 6.0)

    # ── MODE PRODUK ──
    else:
        if not keyword:
            cb(100, "❌ Keyword kosong.", 0)
            return None

        marketplaces = [s for s in sources if s in ("tokopedia", "shopee")]
        if marketplaces:
            async with _browser_session(headless, proxy, cb, engine) as page:
                step = 70 // max(len(marketplaces), 1)
                for i, plat in enumerate(marketplaces):
                    base = 5 + i * step
                    cb(base, f"Memulai {plat.title()}: '{keyword}'", len(all_results))
                    res = await _scrape_search(page, plat, keyword, max_results, cb, base, manual)
                    all_results.extend(res)
                    cb(base + step, f"{plat.title()} selesai: {len(res)} produk", len(all_results))
                    await _random_delay(2.0, 4.0)

        if "custom" in sources and custom_url:
            res = await _scrape_custom_url(custom_url, custom_sel_harga, custom_sel_nama,
                                           headless, proxy, cb, engine)
            all_results.extend(res)

        if use_gemini and gemini_api_key:
            cb(93, f"Gemini AI: mencari harga '{keyword}'...", len(all_results))
            g = await _gemini_pricing(keyword, sources, max_results, gemini_api_key, cb)
            all_results.extend(g)
        elif use_gemini and not gemini_api_key:
            cb(93, "⚠ Gemini dilewati — API key tidak diisi", len(all_results))

    if not all_results:
        try:
            db.mp_run_finish(run_id, total=0)
        except Exception:
            pass
        return _lapor_nol(sebab_per_toko, cb)

    cb(96, f"Total {len(all_results)} produk. Menyusun perbandingan harga...", len(all_results))
    all_results = _build_comparison(all_results)
    berkas = _write_excel(all_results, cb)
    try:
        db.mp_run_finish(run_id, total=len(all_results), filename=berkas or "")
    except Exception:
        pass
    return berkas


def run_price_comparison(params: dict, callback, should_stop=None) -> str:
    """Tiga parameter disengaja: app.py:_terima_should_stop memeriksa signature,
    dan hanya dengan >=3 parameter tombol Stop di UI ikut hidup."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    return asyncio.run(_async_main(params, callback, should_stop))


# ─── Cek Harga: tanya DB, bukan buka browser ──────────────────────────────────

def cek_harga(kueri, store_ids=None, modal=None, kunci=None,
              fee_persen=None, biaya_tetap=None, margin_target=None) -> dict:
    """Jawab "harga saya baiknya pasang berapa?" dari data yang SUDAH dipanen.

    Sengaja tidak menyentuh browser sama sekali. Mengubah modal atau persentase
    biaya seharusnya tidak pernah memicu scraping ulang — user akan mengutak-atik
    angka itu berkali-kali, dan menunggu Chrome tiap kali membuat fiturnya tidak
    terpakai. Panen dijalankan terpisah lewat tombol scraping yang sudah ada.
    """
    kueri = str(kueri or "").strip()
    if not kueri:
        return {"ok": False, "error": "Ketik dulu nama produknya."}

    produk = db.mp_products_semua(store_ids=store_ids or None)
    if not produk:
        return {"ok": False, "error":
                "Belum ada produk tersimpan. Jalankan scraping toko dulu "
                "(mode Toko), lalu kembali ke sini."}

    lex = mp_lexicon.bangun_dari_db(store_ids=store_ids or None)
    grup = mp_match.cari_dan_kelompokkan(kueri, produk, lex)
    if not grup:
        return {"ok": False, "error":
                f"Tidak ada produk yang cocok dengan '{kueri}' di "
                f"{len(produk)} produk tersimpan.",
                "n_produk_dicek": len(produk)}

    # Varian yang dipilih user; kalau belum memilih, ambil yang pembandingnya
    # paling banyak — itu yang statistiknya paling bisa dipercaya.
    terpilih = next((g for g in grup if g["kunci"] == kunci), grup[0])

    platform = (terpilih["baris"][0].get("platform") or "tokopedia").lower()
    biaya = db.mp_biaya(platform)
    fee = biaya["fee_persen"] if fee_persen is None else float(fee_persen)
    tetap = biaya["biaya_tetap"] if biaya_tetap is None else int(biaya_tetap)
    margin = (mp_harga.MARGIN_MIN if margin_target is None
              else float(margin_target) / 100)

    # Modal: dari input, atau dari yang pernah disimpan untuk produk kita.
    if modal in (None, "", 0):
        modal = None
        for b in terpilih["baris"]:
            if b.get("is_own"):
                m = db.mp_get_modal(b.get("product_key"))
                if m and m.get("modal"):
                    modal = int(m["modal"])
                    break

    hasil = mp_harga.analisa(terpilih, modal=modal, fee_persen=fee,
                             biaya_tetap=tetap, margin_target=margin)
    hasil["ok"] = True
    hasil["kueri"] = kueri
    hasil["kunci"] = terpilih["kunci"]
    hasil["platform"] = platform

    # Status per toko. "Toko ini tidak menjualnya" adalah jawaban yang sah, tapi
    # hanya kalau dikatakan — kalau tokonya cuma hilang diam-diam dari tabel,
    # user tidak bisa membedakannya dari panen yang gagal.
    punya = {b.get("store_id") for b in terpilih["baris"]}
    hasil["toko_status"] = [
        {"store_id": s["id"], "nama": s.get("nama") or s.get("username"),
         "is_own": int(bool(s.get("is_own"))),
         "punya": s["id"] in punya,
         "n_produk_tersimpan": sum(
             1 for p in produk if p.get("store_id") == s["id"]),
         "last_error": s.get("last_error") or ""}
        for s in db.mp_stores(hanya_aktif=False)
        if s.get("platform") == platform
        and (not store_ids or s["id"] in store_ids)
    ]
    hasil["ringkasan"] = mp_harga.ringkas_kalimat(hasil)
    hasil["varian"] = [
        {"kunci": g["kunci"], "label": g["label"], "n_toko": g["n_toko"],
         "n_produk": g["n_produk"], "harga_min": g["harga_min"],
         "harga_maks": g["harga_maks"], "punya_sendiri": g["punya_sendiri"],
         "dipilih": g["kunci"] == terpilih["kunci"]}
        for g in grup
    ]
    return hasil


def gaya_toko(store_id) -> dict:
    """Ringkasan cara toko ini menulis judul produknya."""
    produk = db.mp_products_semua(store_ids=[store_id] if store_id else None)
    judul = [p.get("nama_produk") or "" for p in produk]
    terjual = [p.get("terjual") or 0 for p in produk]
    gaya = mp_lexicon.ringkas_gaya(judul, terjual)
    return {"ok": True, "store_id": store_id, "gaya": gaya,
            "kalimat": mp_lexicon.kalimat_gaya(gaya)}
