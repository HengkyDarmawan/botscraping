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


def _parse_price(text) -> int:
    """Ekstrak angka dari string harga: 'Rp 6.500.000' → 6500000"""
    if not text:
        return 0
    # Ambil potongan 'Rp...' pertama bila ada teks panjang
    m = re.search(r"Rp\s?[\d\.\,]+", str(text))
    src = m.group() if m else str(text)
    digits = re.sub(r"[^\d]", "", src)
    return int(digits) if digits else 0


def _format_price(amount: int) -> str:
    """Format angka ke rupiah: 6500000 → 'Rp 6.500.000'"""
    if not amount:
        return "-"
    return f"Rp {amount:,.0f}".replace(",", ".")


# ─── Database toko ────────────────────────────────────────────────────────────

def load_stores() -> list:
    """Baca daftar toko dari data/stores.json."""
    try:
        return json.loads(STORES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_stores(stores: list):
    DATA_DIR.mkdir(exist_ok=True)
    STORES_FILE.write_text(json.dumps(stores, indent=2, ensure_ascii=False), encoding="utf-8")


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


def add_store(platform: str, nama: str, url: str) -> dict:
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
    entry = {"id": new_id, "platform": platform, "nama": nama, "url": url, "username": username}
    stores.append(entry)
    save_stores(stores)
    return entry


def delete_store(store_id: str) -> bool:
    stores = load_stores()
    new = [s for s in stores if s.get("id") != store_id]
    if len(new) == len(stores):
        return False
    save_stores(new)
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

def _store_product_url(store):
    platform = store["platform"]
    url = (store.get("url") or "").rstrip("/")
    if platform == "tokopedia" and not url.endswith("/product"):
        url = url + "/product"
    return url


def _store_search_url(store, keyword):
    """URL pencarian keyword DI DALAM toko."""
    platform = store["platform"]
    url = (store.get("url") or "").rstrip("/")
    q = urllib.parse.quote_plus(keyword)
    if platform == "tokopedia":
        base = url[:-len("/product")] if url.endswith("/product") else url
        return f"{base}/product?q={q}"
    # Shopee: tak ada shop-id → buka halaman toko, andalkan filter keyword sisi-klien
    return url


async def _scrape_store(page, store, max_results, cb, base_pct, manual=False):
    platform = store["platform"]
    url = _store_product_url(store)
    cb(base_pct, f"Toko '{store['nama']}' ({platform.title()}): membuka {url[:55]}...", 0)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await _random_delay(3.0, 5.0)
        if not await _pass_challenge(page, cb, label=f"Toko {store['nama']}", manual=manual):
            return []
        await _human_scroll(page, times=6)
        res = await _extract_products(page, platform, max_results, store["nama"], cb, base_pct + 2, 8)
        cb(None, f"Toko '{store['nama']}': {len(res)} produk", 0)
        return res
    except Exception as e:
        cb(None, f"Toko '{store['nama']}' error: {type(e).__name__}: {e}", 0)
        return []


async def _scrape_store_search(page, store, keyword, max_results, cb, base_pct, manual=False):
    """Cari keyword DI DALAM satu toko."""
    platform = store["platform"]
    url = _store_search_url(store, keyword)
    cb(base_pct, f"Toko '{store['nama']}': cari '{keyword}' → {url[:55]}...", 0)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await _random_delay(3.0, 5.0)
        if not await _pass_challenge(page, cb, label=f"Toko {store['nama']}", manual=manual):
            return []
        await _human_scroll(page, times=6)
        res = await _extract_products(page, platform, max_results, store["nama"], cb,
                                      base_pct + 2, 8, keyword_filter=keyword)
        cb(None, f"Toko '{store['nama']}': {len(res)} produk cocok '{keyword}'", 0)
        return res
    except Exception as e:
        cb(None, f"Toko '{store['nama']}' error: {type(e).__name__}: {e}", 0)
        return []


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
    if not all_results:
        return []
    valid_prices = [r["harga"] for r in all_results if r["harga"] > 0]
    if not valid_prices:
        return all_results

    avg = int(sum(valid_prices) / len(valid_prices))
    median_price = sorted(valid_prices)[len(valid_prices) // 2]
    rekomendasi = int(avg * 0.95)

    for r in all_results:
        r["rata2_pasar"] = _format_price(avg)
        r["median_pasar"] = _format_price(median_price)
        r["rekomendasi_harga"] = _format_price(rekomendasi)
        if r["harga"] > 0:
            selisih = r["harga"] - avg
            r["vs_rata2"] = f"+{_format_price(selisih)}" if selisih > 0 else f"-{_format_price(abs(selisih))}"
        else:
            r["vs_rata2"] = "-"

    all_results.sort(key=lambda x: x["harga"] if x["harga"] > 0 else 999_999_999)
    return all_results


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

    col_order = ["platform", "nama_produk", "harga_tampil", "toko", "rating",
                 "terjual", "rata2_pasar", "median_pasar", "rekomendasi_harga", "vs_rata2"]
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


async def _async_main(params, cb):
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

    # ── MODE TOKO / KEYWORD+TOKO ──
    if mode in ("toko", "keyword_toko"):
        is_kw = (mode == "keyword_toko")
        if is_kw and not keyword:
            cb(100, "❌ Keyword kosong untuk mode Keyword + Toko.", 0)
            return _write_excel([], cb)
        stores = load_stores()
        selected = [s for s in stores if s.get("id") in store_ids]
        if not selected:
            cb(100, "❌ Tidak ada toko dipilih. Tambah/pilih toko dulu.", 0)
            return _write_excel([], cb)
        cb(5, f"Mode {'KEYWORD+TOKO' if is_kw else 'TOKO'}: {len(selected)} toko dipilih"
              + (f", keyword '{keyword}'" if is_kw else ""), 0)

        async with _browser_session(headless, proxy, cb, engine) as page:
            total = len(selected)
            for idx, store in enumerate(selected):
                base = 8 + int(idx / total * 80)
                if is_kw:
                    res = await _scrape_store_search(page, store, keyword, max_results, cb, base, manual)
                else:
                    res = await _scrape_store(page, store, max_results, cb, base, manual)
                all_results.extend(res)
                cb(8 + int((idx + 1) / total * 80),
                   f"[{idx + 1}/{total}] Toko '{store['nama']}' selesai ({len(res)} produk)",
                   len(all_results))
                await _random_delay(2.0, 4.0)

    # ── MODE PRODUK ──
    else:
        if not keyword:
            cb(100, "❌ Keyword kosong.", 0)
            return _write_excel([], cb)

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

    cb(96, f"Total {len(all_results)} produk. Menyusun perbandingan harga...", len(all_results))
    all_results = _build_comparison(all_results)
    return _write_excel(all_results, cb)


def run_price_comparison(params: dict, callback) -> str:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    return asyncio.run(_async_main(params, callback))
