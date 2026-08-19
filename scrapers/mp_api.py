"""
scrapers/mp_api.py — Tier 0: panen produk lewat API internal marketplace
========================================================================
Dipanggil dari halaman marketplace yang SUDAH LOGIN (lihat mp_session), jadi
kebal terhadap perubahan nama class CSS — masalah yang selama ini membuat
selector Shopee di scrapers/pricing.py mati diam-diam.

Setiap fungsi mengembalikan `(rows, galat)`. `galat` kosong berarti sukses;
kalau terisi, pemanggil turun ke tier DOM. Tidak ada exception yang bocor.

Uji cepat tanpa menjalankan web app:
    python -m scrapers.mp_api --platform shopee --username jayapc
    python -m scrapers.mp_api --cek-login
"""
import asyncio
import contextlib
import json
import re
import sys
import urllib.parse

from . import mp_endpoints as EP
from . import mp_session as S


def _cetak(msg):
    """Cetak ke konsol tanpa peduli code page Windows.

    Konsol Windows default cp1252 dan melempar UnicodeEncodeError begitu bertemu
    emoji — yang dipakai di hampir semua pesan log kita. Diam-diam mengganti
    karakter jauh lebih baik daripada scraper mati di tengah jalan.
    """
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(str(msg).encode(enc, "replace").decode(enc), flush=True)


def _cb_konsol(pct, msg, found=0):
    _cetak(msg)


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def username_dari_url(platform, url):
    """Ambil slug toko dari URL. 'https://shopee.co.id/jayapc?x=1' → 'jayapc'."""
    teks = str(url or "").strip()
    if not teks:
        return ""
    if not teks.startswith("http"):
        return teks.strip("/").split("/")[0]
    try:
        jalur = urllib.parse.urlparse(teks).path.strip("/")
    except Exception:
        return ""
    if not jalur:
        return ""
    bagian = jalur.split("/")[0]
    # Tokopedia kadang dipakai dengan awalan yang bukan nama toko.
    if platform == "tokopedia" and bagian in ("shop", "p", "find"):
        return jalur.split("/")[1] if len(jalur.split("/")) > 1 else ""
    return bagian


# ─── SHOPEE ───────────────────────────────────────────────────────────────────

async def shopee_resolve_shop(page, username, cb=None):
    """username → {'shop_id','nama','jumlah_produk'}. Wajib sebelum ambil produk."""
    if not await S.pastikan_origin(page, "shopee", cb):
        return None, "tidak bisa membuka shopee.co.id"
    url = EP.url("shopee", "shop_base", username=username)
    data, galat = await S.fetch_json(page, url)
    if galat:
        return None, f"get_shop_base gagal: {galat}"
    if not isinstance(data, dict):
        return None, "get_shop_base: respons bukan objek"

    d = data.get("data") or {}
    shop_id = d.get("shopid") or d.get("shop_id")
    if not shop_id:
        kunci = ", ".join(list(data.keys())[:6]) or "(kosong)"
        pesan = data.get("error_msg") or data.get("error") or ""
        return None, (f"shopid tidak ada di respons (kunci: {kunci}"
                      + (f"; pesan: {pesan}" if pesan else "") + ")")
    return {
        "shop_id": str(shop_id),
        "nama": (d.get("name") or username or "").strip(),
        "jumlah_produk": _int(d.get("item_count")),
    }, ""


def _shopee_normalisasi(raw, shop_id, sumber="api"):
    """Satu item API Shopee → record produk kita.

    Menangani dua bentuk respons yang pernah dipakai Shopee sekaligus, karena
    keduanya masih bisa muncul tergantung endpoint mana yang menjawab.
    """
    if not isinstance(raw, dict):
        return None
    d = (raw.get("item_basic")
         or (raw.get("item_card") or {}).get("item_data")
         or raw)
    item_id = d.get("itemid") or d.get("item_id")
    if not item_id:
        return None

    skala = EP.skala_harga("shopee")
    harga = _int(d.get("price") or d.get("price_min")) // skala
    coret = _int(d.get("price_before_discount")) // skala
    shopid = str(d.get("shopid") or d.get("shop_id") or shop_id)

    gambar = d.get("image") or ((d.get("images") or [None]) or [None])[0]
    rating = ((d.get("item_rating") or {}).get("rating_star")
              if isinstance(d.get("item_rating"), dict) else d.get("rating"))

    return {
        "platform": "shopee",
        "item_id": str(item_id),
        "shop_id": shopid,
        "product_key": f"shp:{shopid}:{item_id}",
        "nama_produk": (d.get("name") or "").strip(),
        "url": f"https://shopee.co.id/product/{shopid}/{item_id}",
        "image_url": (f"https://down-id.img.susercontent.com/file/{gambar}"
                      if gambar else ""),
        "harga": harga,
        "harga_coret": coret if coret > harga else 0,
        "diskon_persen": _int(d.get("raw_discount")),
        "rating": round(float(rating or 0), 2),
        "jumlah_ulasan": _int(d.get("cmt_count")),
        "terjual": _int(d.get("historical_sold") or d.get("sold")),
        "stok": _int(d.get("stock")),
        "lokasi": d.get("shop_location") or "",
        "sumber": sumber,
    }


def _ambil_daftar_item(data):
    """Cari array item di respons, apa pun nama kuncinya."""
    if not isinstance(data, dict):
        return None
    for kunci in ("items", "item", "data"):
        nilai = data.get(kunci)
        if isinstance(nilai, list):
            return nilai
        if isinstance(nilai, dict):
            for k2 in ("items", "item"):
                if isinstance(nilai.get(k2), list):
                    return nilai[k2]
    return None


def periksa_skala(rows, cb=None):
    """Peringatkan bila harga hasil pembagian terlihat mustahil.

    Konvensi micro-rupiah Shopee sudah lama stabil, tapi kalau berubah, gejalanya
    adalah seluruh laporan harga salah tanpa satu pun error. Lebih baik berisik.
    """
    harga = sorted(r["harga"] for r in rows if r.get("harga"))
    if not harga:
        return
    median = harga[len(harga) // 2]
    if median < 100 or median > 5_000_000_000:
        angka = f"{median:,}".replace(",", ".")   # hanya angkanya, bukan kalimatnya
        pesan = (f"⚠ Skala harga tampak janggal (median Rp {angka}). "
                 f"Cek satu produk di browser; bila beda, ubah 'harga_skala' "
                 f"di data/mp_endpoints.json.")
        if cb:
            cb(None, pesan, 0)
        else:
            _cetak(pesan)


async def shopee_produk_toko(page, shop_id, *, maks=60, per_halaman=30,
                             pacer=None, cb=None, berhenti=None):
    """Paginasi produk satu toko Shopee lewat API."""
    rows, offset = [], 0
    while len(rows) < maks:
        if berhenti and berhenti():
            break
        if pacer:
            alasan = pacer.harus_berhenti()
            if alasan:
                return rows, alasan
            await pacer.tunggu()

        limit = min(per_halaman, maks - len(rows))
        url = EP.url("shopee", "shop_items", shopid=shop_id,
                     limit=limit, offset=offset)
        data, galat = await S.fetch_json(page, url)
        if galat:
            if pacer:
                pacer.gagal(galat)
            return rows, (f"shop_items gagal di offset {offset}: {galat}"
                          if not rows else "")

        items = _ambil_daftar_item(data)
        if items is None:
            kunci = ", ".join(list(data.keys())[:8]) if isinstance(data, dict) else "?"
            return rows, f"struktur respons Shopee berubah (kunci: {kunci})"
        if not items:
            break

        for it in items:
            rec = _shopee_normalisasi(it, shop_id)
            if rec and rec["nama_produk"]:
                rows.append(rec)
        if pacer:
            pacer.sukses()
        if cb:
            cb(None, f"Shopee: {len(rows)} produk terkumpul...", len(rows))
        if len(items) < limit:
            break
        offset += limit

    periksa_skala(rows, cb)
    return rows[:maks], ""


async def shopee_cari_di_toko(page, shop_id, keyword, *, maks=60,
                              pacer=None, cb=None, berhenti=None):
    """Cari keyword DI DALAM satu toko Shopee.

    Menggantikan perilaku lama yang cuma membuka halaman toko lalu menyaring
    kartu yang kebetulan ter-render — praktis acak untuk toko besar.
    """
    rows, offset = [], 0
    q = urllib.parse.quote_plus(keyword)
    while len(rows) < maks:
        if berhenti and berhenti():
            break
        if pacer:
            alasan = pacer.harus_berhenti()
            if alasan:
                return rows, alasan
            await pacer.tunggu()

        limit = min(30, maks - len(rows))
        url = EP.url("shopee", "shop_search", shopid=shop_id, keyword=q,
                     limit=limit, offset=offset)
        data, galat = await S.fetch_json(page, url)
        if galat:
            if pacer:
                pacer.gagal(galat)
            return rows, f"shop_search gagal: {galat}"
        items = _ambil_daftar_item(data)
        if not items:
            break
        for it in items:
            rec = _shopee_normalisasi(it, shop_id)
            if rec and rec["nama_produk"]:
                rows.append(rec)
        if pacer:
            pacer.sukses()
        if len(items) < limit:
            break
        offset += limit
    return rows[:maks], ""


# ─── TOKOPEDIA ────────────────────────────────────────────────────────────────

async def _tokped_gql(page, op, variables, cb=None):
    body = json.dumps([{
        "operationName": op,
        "variables": variables,
        "query": EP.GQL[op],
    }])
    url = EP.url("tokopedia", "gql", op=op)
    return await S.fetch_json(page, url, method="POST", body=body, headers={
        "content-type": "application/json",
        "x-tkpd-akamai": "gqlGetShopProduct",
        "x-source": "tokopedia-lite",
        "x-device": "desktop",
        "x-tkpd-lite-service": "zeus",
    })


async def tokopedia_resolve_shop(page, username, cb=None):
    """username → shop_id. Dua cara, dari yang paling murah.

    Tokopedia paling sering berubah di antara semua endpoint di sini, jadi jalur
    cadangan lewat state yang tertanam di HTML bukan kemewahan — pada 14 Agustus
    justru itu satu-satunya yang jalan.
    """
    if not await S.pastikan_origin(page, "tokopedia", cb):
        return None, "tidak bisa membuka tokopedia.com"

    # 1) GraphQL — `fields`/`source` ikut dikirim persis seperti yang dipakai
    #    halaman toko sendiri; skema Tokopedia menolak input yang tidak lengkap.
    data, galat = await _tokped_gql(page, "ShopInfoCore", {"input": {
        "domain": username,
        "shopIDs": [0],
        "fields": ["core", "location", "status", "is_open"],
        "source": "shoppage",
    }}, cb)
    if not galat and isinstance(data, list) and data:
        try:
            inti = (data[0]["data"]["shopInfoByID"]["result"][0]
                    ["shopCore"])
            if inti.get("shopID"):
                return {"shop_id": str(inti["shopID"]),
                        "nama": inti.get("name") or username}, ""
        except (KeyError, IndexError, TypeError):
            pass

    # 2) Baca state yang ditanam di HTML halaman toko.
    #
    # `__NEXT_DATA__` sudah TIDAK ADA lagi di halaman toko Tokopedia (dibuktikan
    # atas `output/diag_jayapc_20260814_104429/page.html`: 0 kemunculan), jadi
    # cabang `getElementById` yang dulu ada di sini selalu gagal dan dibuang.
    # Yang sekarang hidup adalah cache Apollo yang diserialisasi ke dalam HTML:
    #   ...shopCore":{"domain":"jayapc","shopID":"243690","name":"Jaya PC"...
    # Regex atas `innerHTML` menemukannya — sudah diuji atas berkas itu.
    #
    # Kalau bisa, baca dari halaman yang SEDANG terbuka dulu: halaman daftar
    # produk memuat state yang sama, jadi navigasi ke `/{username}` sering tidak
    # perlu — dan setiap navigasi ekstra adalah satu kesempatan kena captcha.
    # Backslash opsional di dua tempat: state itu kadang tertanam apa adanya
    # (`"shopID":"243690"`) dan kadang sebagai string JS yang di-escape
    # (`\"shopID\":\"243690\"`). Satu pola menangani keduanya.
    js_shop_id = """() => {
        const m = document.documentElement.innerHTML
                  .match(/shopI[dD]\\\\?"\\s*:\\s*\\\\?"?(\\d{3,})/);
        return m ? m[1] : null;
    }"""
    try:
        shop_id = await page.evaluate(js_shop_id)
        if shop_id:
            return {"shop_id": str(shop_id), "nama": username}, ""
    except Exception:
        pass

    try:
        await page.goto(EP.url("tokopedia", "shop_page", username=username),
                        wait_until="domcontentloaded", timeout=60_000)
        await S.jeda(2.0, 3.5)
        shop_id = await page.evaluate(js_shop_id)
        if shop_id:
            return {"shop_id": str(shop_id), "nama": username}, ""
    except Exception as e:
        return None, f"gagal membuka halaman toko: {type(e).__name__}: {e}"

    return None, "shop_id Tokopedia tidak ditemukan (GraphQL & halaman toko gagal)"


_JS_ETALASE = """
() => {
  const out = [];
  for (const a of document.querySelectorAll('a[href*="/etalase/"]')) {
    const nama = (a.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim();
    let href = a.getAttribute('href') || '';
    if (!href) continue;
    const m = href.match(/\\/etalase\\/([^/?#]+)/);
    if (!m) continue;
    const slug = m[1];
    if (out.some(o => o.slug === slug)) continue;
    out.push({slug: slug, nama: nama || slug, href: href});
  }
  return out;
}
"""

# Etalase bawaan Tokopedia, bukan kategori buatan penjual. Tidak berguna untuk
# mencocokkan produk dan hanya menambah request kalau ikut dipilih.
_ETALASE_SEMU = {"sold", "preorder", "all-products", "all-etalase"}


async def tokopedia_etalase(page, username, cb=None):
    """Daftar etalase (kategori buatan penjual) satu toko.

    Dipanen dari tab PRODUK, bukan beranda toko — probe 14 Agustus menunjukkan
    beranda `/jayapc` mengembalikan 0 tautan etalase sedangkan `/jayapc/product`
    memuat keempat puluhnya.
    """
    url = EP.url("tokopedia", "shop_product_page", username=username)
    try:
        if _lepas_query(page.url) != _lepas_query(url):
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await S.jeda(2.0, 3.5)
        daftar = await page.evaluate(_JS_ETALASE)
    except Exception as e:
        return [], f"gagal membaca etalase: {type(e).__name__}: {e}"

    bersih = []
    for e in daftar or []:
        slug = (e.get("slug") or "").strip()
        if not slug or slug in _ETALASE_SEMU:
            continue
        nama = re.sub(r"^[-\s]+", "", e.get("nama") or slug)  # "- - VGA AMD" → "VGA AMD"
        bersih.append({"slug": slug, "nama": nama or slug,
                       "url": EP.origin("tokopedia") + (e.get("href") or "")})
    if cb:
        cb(None, f"  ↳ {len(bersih)} etalase ditemukan di toko '{username}'", 0)
    return bersih, ("" if bersih else "tidak ada etalase di halaman toko")


def _tokped_normalisasi(d, shop_id, username=""):
    if not isinstance(d, dict):
        return None
    pid = d.get("product_id") or d.get("productID") or d.get("id")
    if not pid:
        return None
    harga_teks = ((d.get("price") or {}).get("text_idr")
                  if isinstance(d.get("price"), dict) else d.get("price"))
    harga = _int(re.sub(r"[^\d]", "", str(harga_teks or "")))
    gambar = ((d.get("primary_image") or {}).get("original")
              if isinstance(d.get("primary_image"), dict) else "")
    return {
        "platform": "tokopedia",
        "item_id": str(pid),
        "shop_id": str(shop_id),
        "product_key": f"tkp:{shop_id}:{pid}",
        "nama_produk": (d.get("name") or "").strip(),
        "url": d.get("product_url") or "",
        "image_url": gambar or "",
        "harga": harga,
        "harga_coret": 0,
        "diskon_persen": 0,
        "rating": round(float(d.get("rating") or 0), 2),
        "jumlah_ulasan": _int((d.get("stats") or {}).get("countReview")
                              if isinstance(d.get("stats"), dict) else 0),
        "terjual": _int(d.get("sold")),
        "stok": 0,
        "lokasi": "",
        "sumber": "api",
    }


async def tokopedia_produk_toko(page, shop_id, *, maks=60, per_halaman=40,
                                keyword="", pacer=None, cb=None, berhenti=None):
    rows, halaman = [], 1
    while len(rows) < maks:
        if berhenti and berhenti():
            break
        if pacer:
            alasan = pacer.harus_berhenti()
            if alasan:
                return rows, alasan
            await pacer.tunggu()

        data, galat = await _tokped_gql(page, "ShopProducts", {
            "sid": str(shop_id), "page": halaman,
            "perPage": min(per_halaman, maks - len(rows)),
            "keyword": keyword, "sort": 1,
        }, cb)
        if galat:
            if pacer:
                pacer.gagal(galat)
            return rows, f"ShopProducts gagal: {galat}"
        try:
            blok = data[0]["data"]["GetShopProduct"]
            items = blok.get("data") or []
        except (KeyError, IndexError, TypeError):
            return rows, "struktur GraphQL Tokopedia berubah"
        if not items:
            break
        for it in items:
            rec = _tokped_normalisasi(it, shop_id)
            if rec and rec["nama_produk"]:
                rows.append(rec)
        if pacer:
            pacer.sukses()
        if cb:
            cb(None, f"Tokopedia: {len(rows)} produk terkumpul...", len(rows))
        halaman += 1
    return rows[:maks], ""


# ─── Dispatcher ───────────────────────────────────────────────────────────────

async def resolve_shop(page, platform, username, cb=None):
    if platform == "shopee":
        return await shopee_resolve_shop(page, username, cb)
    if platform == "tokopedia":
        return await tokopedia_resolve_shop(page, username, cb)
    return None, f"platform tidak dikenal: {platform}"


def _url_toko(store):
    """URL daftar produk toko.

    Untuk Tokopedia harus '/product' — terbukti lewat percobaan: '/product'
    merender 80+ kartu katalog, sedangkan '?tab=product' hanya 14 (widget
    etalase) dan '/etalase/all-products' nol.
    """
    platform = store.get("platform")
    url = (store.get("url") or "").rstrip("/")
    if platform == "tokopedia" and not url.endswith("/product"):
        url += "/product"
    return url


def _lepas_query(url):
    """URL tanpa query string & fragment, untuk membandingkan 'halaman yang sama'.

    Marketplace rutin menempelkan `?source=`/`#` sendiri setelah navigasi, jadi
    perbandingan string mentah akan mengira halaman berpindah padahal tidak.
    """
    return (url or "").split("?")[0].split("#")[0].rstrip("/")


async def panen_toko(store, page, *, maks=60, keyword="", sel=None,
                     pacer=None, cb=None, berhenti=None):
    """Panen produk satu toko dengan tiga lapis berjenjang.

    Return `(rows, sumber, sebab)`. `sumber` menyebut lapis mana yang berhasil
    ('api' / 'dom_anchor' / 'dom_selector') supaya log jujur — selama ini tidak
    ada cara tahu jalur mana yang dipakai, dan itulah kenapa 0-hasil kemarin
    butuh tebak-tebakan.
    """
    from . import mp_dom
    platform = store.get("platform")
    username = store.get("username") or username_dari_url(platform, store.get("url"))
    nama = store.get("nama") or username
    sebab = []

    def lapor(msg):
        if cb:
            cb(None, msg, 0)

    # Navigasi sekali ke halaman toko. Lapis API pun butuh origin yang benar.
    url = _url_toko(store)
    lapor(f"Toko '{nama}': membuka {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await S.jeda(2.0, 3.5)
    except Exception as e:
        return [], "", f"gagal membuka halaman toko: {type(e).__name__}: {e}"

    kena, alasan = await S.kena_challenge(page)
    if kena:
        return [], "", f"terhalang verifikasi ({alasan})"

    # ── Lapis 0: API resmi situs ──
    shop_id = store.get("shop_id") or ""
    if not shop_id:
        info, galat = await resolve_shop(page, platform, username, cb)
        if info:
            shop_id = info.get("shop_id") or ""
            store["shop_id"] = shop_id
        else:
            sebab.append(f"resolve shop_id gagal: {galat}")
    if shop_id:
        rows, galat = await produk_toko(page, platform, shop_id, keyword=keyword,
                                        maks=maks, pacer=pacer, cb=cb,
                                        berhenti=berhenti)
        if rows:
            lapor(f"  ↳ api: {len(rows)} produk")
            return rows[:maks], "api", ""
        sebab.append(f"api: {galat or 'tidak mengembalikan produk'}")

    if berhenti and berhenti():
        return [], "", "dibatalkan user"

    # Lapis 0 boleh berpindah halaman: `tokopedia_resolve_shop` membuka `/{username}`
    # untuk membaca shop_id dari state yang ditanam di HTML. Kalau tidak dikembalikan,
    # lapis anchor di bawah memanen beranda toko — yang memang tidak memuat kartu
    # produk. Itu persis penyebab tiga run 0-hasil pada 14 Agustus: `ringkas.txt`
    # mencatat `url final: /jayapc` padahal panen dimulai dari `/jayapc/product`.
    if _lepas_query(page.url) != _lepas_query(url):
        lapor(f"  ↳ halaman bergeser ke {page.url} — kembali ke {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await S.jeda(2.0, 3.5)
        except Exception as e:
            return [], "", f"gagal kembali ke daftar produk: {type(e).__name__}: {e}"

    # ── Lapis 1: panen anchor (tidak bergantung nama class) ──
    # Muat sampai habis → panen → klik "halaman berikutnya" bila masih kurang.
    # Navigasi `?page=N` sengaja tidak dipakai: pada tab produk Tokopedia
    # parameter itu diabaikan dan mengembalikan halaman yang sama persis.
    kumpul, terlihat, galat = [], set(), ""
    for halaman in range(1, 11):
        if berhenti and berhenti():
            break
        await mp_dom.muat_semua_kartu(page, target=maks - len(kumpul), cb=cb)
        rows, galat = await mp_dom.panen_anchor(page, platform, store,
                                                maks=maks, cb=cb)
        baru = 0
        for r in rows:
            if r["product_key"] in terlihat:
                continue
            terlihat.add(r["product_key"])
            kumpul.append(r)
            baru += 1
        if cb and halaman > 1:
            cb(None, f"  ↳ halaman {halaman}: +{baru} produk baru "
                     f"(total {len(kumpul)})", 0)
        if len(kumpul) >= maks or baru == 0:
            break
        if pacer:
            await pacer.tunggu()
        if not await mp_dom.halaman_berikutnya(page, cb):
            break
        await S.jeda(2.0, 3.5)

    if galat and not kumpul:
        sebab.append(f"anchor: {galat}")

    if keyword:
        sebelum = len(kumpul)
        kumpul = [r for r in kumpul
                  if keyword.lower() in (r["nama_produk"] or "").lower()]
        if cb:
            cb(None, f"  ↳ filter '{keyword}': {sebelum} → {len(kumpul)} produk", 0)
        if not kumpul:
            sebab.append(f"tidak ada produk memuat '{keyword}' "
                         f"dari {sebelum} produk toko ini")
    if kumpul:
        return kumpul[:maks], "dom_anchor", ""

    # ── Lapis 2: selector warisan ──
    rows, galat = await mp_dom.panen_selector(page, platform, store, sel,
                                              maks=maks, cb=cb)
    if rows:
        return rows[:maks], "dom_selector", ""
    sebab.append(f"selector: {galat}")

    # Semua lapis gagal — simpan bukti, jangan biarkan user menebak.
    folder = await mp_dom.simpan_diagnostik(page, nama, sel, cb)
    return [], "", "; ".join(sebab) + (f" | bukti: {folder}" if folder else "")


async def produk_toko(page, platform, shop_id, *, keyword="", maks=60,
                      pacer=None, cb=None, berhenti=None):
    if platform == "shopee":
        if keyword:
            return await shopee_cari_di_toko(page, shop_id, keyword, maks=maks,
                                             pacer=pacer, cb=cb, berhenti=berhenti)
        return await shopee_produk_toko(page, shop_id, maks=maks, pacer=pacer,
                                        cb=cb, berhenti=berhenti)
    if platform == "tokopedia":
        return await tokopedia_produk_toko(page, shop_id, maks=maks, keyword=keyword,
                                           pacer=pacer, cb=cb, berhenti=berhenti)
    return [], f"platform tidak dikenal: {platform}"


# ─── Spike CLI (Fase 0) ───────────────────────────────────────────────────────

async def _spike(platform, username, keyword, maks):
    _cetak(f"\n=== SPIKE {platform} / {username} ===")
    async with S.chrome_login(_cb_konsol) as (context, page):
        info_login = await S.cek_login(context, page, platform, _cb_konsol)
        _cetak(f"Login: {info_login['login']} — {info_login['detail']}"
               + (f" (akun: {info_login['akun']})" if info_login.get("akun") else ""))
        if not info_login["login"]:
            _cetak("→ Login dulu di jendela Chrome, lalu ulangi perintah ini.")

        toko, galat = await resolve_shop(page, platform, username, _cb_konsol)
        if galat:
            _cetak(f"[GAGAL] resolve_shop: {galat}")
            return 1
        _cetak(f"[OK] shop_id={toko['shop_id']}  nama={toko.get('nama')}"
               f"  produk={toko.get('jumlah_produk', '?')}")

        pacer = S.Pacer(_cb_konsol)
        rows, galat = await produk_toko(page, platform, toko["shop_id"],
                                        keyword=keyword, maks=maks,
                                        pacer=pacer, cb=_cb_konsol)
        if galat:
            _cetak(f"[GAGAL] produk_toko: {galat}")
        _cetak(f"\n[OK] {len(rows)} produk. Tiga pertama:")
        for r in rows[:3]:
            _cetak(json.dumps(r, indent=2, ensure_ascii=False))
        return 0 if rows else 1


async def _spike_login():
    hasil = await S.cek_semua_login(cb=_cb_konsol)
    for h in hasil:
        tanda = "[OK]" if h["login"] else "[--]"
        _cetak(f"{tanda} {h['platform']}: {h['detail']}"
               + (f" (akun: {h['akun']})" if h.get("akun") else ""))


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="Spike API marketplace (Fase 0)")
    p.add_argument("--platform", default="shopee", choices=["shopee", "tokopedia"])
    p.add_argument("--username", help="slug toko, mis. jayapc")
    p.add_argument("--url", help="URL toko lengkap (alternatif --username)")
    p.add_argument("--keyword", default="", help="cari keyword di dalam toko")
    p.add_argument("--maks", type=int, default=10)
    p.add_argument("--cek-login", action="store_true", help="hanya cek status login")
    a = p.parse_args(argv)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        # Konsol Windows default cp1252; pesan log kita penuh emoji.
        with contextlib.suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if a.cek_login:
        return asyncio.run(_spike_login()) or 0

    username = a.username or username_dari_url(a.platform, a.url or "")
    if not username:
        p.error("butuh --username atau --url")
    return asyncio.run(_spike(a.platform, username, a.keyword, a.maks))


if __name__ == "__main__":
    sys.exit(main() or 0)
