"""
scrapers/mp_session.py — Sesi browser login untuk scraping marketplace
======================================================================
Inti pendekatan ini: **user login & menyelesaikan captcha manual sekali di awal,
lalu scraper memakai sesi itu.** Chrome dibuka dengan profil persisten
(`data/chrome_profile`) supaya login bertahan antar-run; scraper menyambung lewat
CDP dan hanya membuka tab sendiri — jendela Chrome user tidak pernah ditutup.

Kenapa `fetch()` dijalankan DI DALAM halaman marketplace, bukan lewat requests:
request yang lahir di dalam origin marketplace otomatis membawa cookie sesi
(termasuk yang HttpOnly), TLS/HTTP2 fingerprint Chrome asli, header sec-ch-ua,
dan token anti-bot apa pun yang sudah dipasang JS situs di `window`. Tidak ada
satu pun dari itu yang bisa ditiru dari luar browser.

Catatan penting: jalur API ini **hanya berlaku untuk engine "Chrome saya"**.
Camoufox adalah Firefox, dan `connect_over_cdp` khusus Chromium.
"""
import asyncio
import contextlib
import json
import random
import re

from playwright.async_api import async_playwright

from . import mp_endpoints as EP

# Penanda halaman challenge / blokir (Cloudflare, DataDome, captcha).
PENANDA_CHALLENGE = [
    "just a moment", "checking your browser", "enable javascript",
    "verify you are human", "verifying you are human", "captcha",
    "akses ditolak", "access denied", "unusual traffic",
    "please wait while we", "ddos protection", "are you a robot",
    "verifikasi", "tekan dan tahan",
]

# Jejak URL yang berarti kita dilempar ke halaman verifikasi/login.
PENANDA_URL_BLOKIR = ["/verify/traffic", "/verify/captcha", "/buyer/login",
                      "/login", "captcha"]


def cdp_url():
    try:
        import config
        return getattr(config, "CHROME_CDP_URL", "http://localhost:9222")
    except Exception:
        return "http://localhost:9222"


async def jeda(mn=1.5, mx=3.0):
    await asyncio.sleep(random.uniform(mn, mx))


# ─── Primitif: fetch() di dalam halaman ───────────────────────────────────────

_JS_FETCH = """
async ([url, headers, body, method]) => {
  try {
    const opt = {
      method: method || 'GET',
      credentials: 'include',
      headers: Object.assign({'x-requested-with': 'XMLHttpRequest'}, headers || {}),
    };
    if (body) opt.body = body;
    const r = await fetch(url, opt);
    const t = await r.text();
    return {ok: r.ok, status: r.status, url: r.url, text: t.slice(0, 3000000)};
  } catch (e) {
    return {ok: false, status: 0, text: '', error: String(e)};
  }
}
"""


async def pastikan_origin(page, platform, cb=None):
    """Pindahkan tab ke origin marketplace bila belum di sana.

    Ini syarat mutlak, bukan kerapian: dari origin lain, cookie sesi tidak ikut
    terkirim dan CORS memblokir respons. Gejalanya 'TypeError: Failed to fetch'.
    """
    org = EP.origin(platform)
    if not org:
        return False
    kini = page.url or ""
    if kini.startswith(org):
        return True
    try:
        await page.goto(org + "/", wait_until="domcontentloaded", timeout=60_000)
        await jeda(1.5, 3.0)
        return True
    except Exception as e:
        if cb:
            cb(None, f"⚠ Gagal membuka {org}: {type(e).__name__}: {e}", 0)
        return False


async def fetch_json(page, url, *, headers=None, body=None, method="GET"):
    """Ambil JSON lewat fetch() di dalam halaman yang sudah login.

    Mengembalikan `(data, galat)` dan TIDAK PERNAH melempar exception: turun tier
    adalah jalur normal di sini, bukan keadaan luar biasa. Pemanggil yang
    memutuskan mau mundur ke DOM atau menyerah.
    """
    try:
        res = await page.evaluate(_JS_FETCH, [url, headers or {}, body, method])
    except Exception as e:
        return None, f"evaluate gagal: {type(e).__name__}: {e}"

    if not res.get("ok"):
        status = res.get("status")
        extra = res.get("error") or ""
        if status == 0 and "Failed to fetch" in extra:
            extra += " (biasanya origin salah atau diblokir CORS)"
        return None, (f"HTTP {status} {extra}".strip() or "fetch gagal")

    teks = res.get("text") or ""
    try:
        return json.loads(teks), ""
    except json.JSONDecodeError:
        # HTTP 200 tapi bukan JSON hampir selalu berarti halaman captcha/login.
        cuplik = re.sub(r"\s+", " ", teks[:180]).strip()
        return None, f"respons bukan JSON (HTTP 200): {cuplik}"


# ─── Koneksi ke Chrome user ───────────────────────────────────────────────────

@contextlib.asynccontextmanager
async def chrome_login(cb=None):
    """Yield `(context, page)` dari Chrome user yang sudah login, lewat CDP.

    Hanya tab kita yang ditutup di akhir — browser user dibiarkan terbuka.
    """
    def lapor(msg):
        if cb:
            cb(None, msg, 0)

    cdp = cdp_url()
    lapor(f"🔗 Menyambung ke Chrome Anda ({cdp})...")
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp)
        except Exception as e:
            lapor(f"❌ Tidak bisa menyambung ke Chrome ({type(e).__name__}). "
                  f"Klik 'Buka Chrome Scraping', login dulu, baru jalankan lagi.")
            raise RuntimeError("Chrome CDP tidak terhubung") from e

        context = (browser.contexts[0] if browser.contexts
                   else await browser.new_context(locale="id-ID",
                                                  timezone_id="Asia/Jakarta"))
        page = await context.new_page()
        lapor("✓ Terhubung ke Chrome Anda (sesi login dipakai)")
        try:
            yield context, page
        finally:
            with contextlib.suppress(Exception):
                await page.close()


# ─── Cek login sungguhan (bukan sekadar cek port) ─────────────────────────────

async def cek_login(context, page, platform, cb=None):
    """Apakah benar-benar sudah login di platform ini?

    Dua sinyal digabung dengan sengaja: cookie saja bisa basi (ada tapi sudah
    kedaluwarsa di sisi server), panggilan API saja bisa gagal karena rate-limit
    sesaat. Kalau keduanya sepakat, hasilnya bisa dipercaya.
    """
    hasil = {"platform": platform, "login": False, "akun": None, "detail": ""}
    org = EP.origin(platform)

    # 1) Cookie sesi. context.cookies() ikut membaca HttpOnly; document.cookie tidak.
    punya_cookie = False
    try:
        nama_penting = set(EP.cookie_login(platform))
        cookies = await context.cookies(org)
        ada = {c["name"] for c in cookies if c.get("value")}
        punya_cookie = bool(nama_penting & ada)
    except Exception as e:
        hasil["detail"] = f"gagal baca cookie: {type(e).__name__}"

    if not punya_cookie:
        hasil["detail"] = "cookie sesi tidak ditemukan — belum login"
        return hasil

    # 2) Panggilan API ringan dari dalam origin.
    if not await pastikan_origin(page, platform, cb):
        hasil["detail"] = "cookie ada, tapi origin tidak bisa dibuka"
        return hasil

    ep = EP.muat().get(platform) or {}
    akun_url = ep.get("akun")
    if not akun_url:
        hasil["login"] = True
        hasil["detail"] = "cookie sesi ada (tanpa verifikasi API)"
        return hasil
    if not akun_url.startswith("http"):
        akun_url = org.rstrip("/") + akun_url

    data, galat = await fetch_json(page, akun_url)
    if galat:
        # Cookie ada tapi API menolak: anggap belum login, dan katakan kenapa.
        hasil["detail"] = f"cookie ada, tapi API menolak — {galat}"
        return hasil

    hasil["login"] = True
    hasil["akun"] = _nama_akun(data)
    hasil["detail"] = "cookie + API cocok"
    return hasil


def _nama_akun(data):
    """Cari username di respons akun tanpa menebak-nebak struktur terlalu dalam."""
    if not isinstance(data, dict):
        return None
    for jalur in (("data", "username"), ("data", "userid"), ("username",),
                  ("data", "user", "name"), ("data", "profile", "name")):
        kur = data
        for k in jalur:
            if not isinstance(kur, dict):
                kur = None
                break
            kur = kur.get(k)
        if kur:
            return str(kur)
    return None


async def cek_semua_login(platforms=("shopee", "tokopedia"), cb=None):
    """Dipakai route /pricing/session-status."""
    hasil = []
    async with chrome_login(cb) as (context, page):
        for plat in platforms:
            try:
                hasil.append(await cek_login(context, page, plat, cb))
            except Exception as e:
                hasil.append({"platform": plat, "login": False, "akun": None,
                              "detail": f"{type(e).__name__}: {e}"})
    return hasil


# ─── Deteksi challenge ────────────────────────────────────────────────────────

async def kena_challenge(page):
    """(bool, sebab). Memeriksa URL juga, bukan cuma teks — DataDome sering
    melempar ke /verify/traffic yang teksnya tidak memuat kata kunci apa pun."""
    try:
        url_kini = (page.url or "").lower()
        for jejak in PENANDA_URL_BLOKIR:
            if jejak in url_kini:
                return True, f"dialihkan ke {jejak}"
        txt = await page.evaluate(
            "document.body ? document.body.innerText.slice(0, 3000) : ''")
        judul = await page.title()
    except Exception:
        return False, ""
    blob = f"{judul}\n{txt}".lower()
    for m in PENANDA_CHALLENGE:
        if m in blob:
            return True, m
    return False, ""


async def tunggu_challenge(page, cb, label="", maks_detik=90, berhenti=None):
    """Tunggu user menyelesaikan captcha manual di jendela Chrome.

    `berhenti` dicek tiap putaran supaya tombol Stop tetap responsif — tanpa ini,
    run yang nyangkut di captcha menggantung tombol Stop selama 90 detik penuh.
    """
    kena, sebab = await kena_challenge(page)
    if not kena:
        return True

    cb(None, f"🖐 {label}: ada verifikasi ({sebab}) — selesaikan manual di jendela "
             f"Chrome. Menunggu sampai {maks_detik} detik...", 0)
    putaran = max(maks_detik // 4, 1)
    for i in range(putaran):
        if berhenti and berhenti():
            cb(None, f"⏹ {label}: dibatalkan saat menunggu verifikasi.", 0)
            return False
        await asyncio.sleep(4)
        kena, _ = await kena_challenge(page)
        if not kena:
            cb(None, f"✓ {label}: verifikasi lolos, melanjutkan...", 0)
            return True
        if i % 3 == 2:
            cb(None, f"⏳ {label}: menunggu verifikasi ({(i + 1) * 4}/{maks_detik} dtk)...", 0)
    cb(None, f"❌ {label}: verifikasi belum selesai dalam {maks_detik} detik.", 0)
    return False


# ─── Pengatur ritme ───────────────────────────────────────────────────────────

class Pacer:
    """Jeda adaptif + rem darurat.

    Tujuannya melindungi AKUN, bukan sekadar menghindari blokir. Menghantam terus
    saat sudah kena challenge berulang adalah cara tercepat membuat akun toko
    ditandai — jadi 3 challenge beruntun menghentikan platform itu untuk run ini.
    """

    def __init__(self, cb=None, jeda_min=2.5, jeda_maks=5.0, maks_req=120):
        self.cb = cb
        self.jeda_min = jeda_min
        self.jeda_maks = jeda_maks
        self.maks_req = maks_req
        self.faktor = 1.0
        self.beruntun = 0
        self.req = 0

    async def tunggu(self):
        self.req += 1
        await asyncio.sleep(random.uniform(self.jeda_min, self.jeda_maks) * self.faktor)

    def sukses(self):
        self.faktor = max(1.0, self.faktor * 0.8)
        self.beruntun = 0

    def gagal(self, sebab=""):
        self.faktor = min(self.faktor * 2, 8.0)
        self.beruntun += 1
        if self.cb:
            self.cb(None, f"⚠ Melambat (×{self.faktor:.1f}) setelah gagal: {sebab}", 0)

    def harus_berhenti(self):
        if self.beruntun >= 3:
            return "3 kali verifikasi/blokir beruntun — dihentikan demi keamanan akun"
        if self.req > self.maks_req:
            return f"batas {self.maks_req} request per platform tercapai"
        return None
