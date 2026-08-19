"""
scrapers/mp_dom.py — Panen produk dari DOM tanpa bergantung pada nama class
===========================================================================
Pelajaran dari dua kali kejadian di project ini: selector berbasis nama class
atau `data-testid` **selalu** mati pada akhirnya. `div.F7nice` di Google Maps
mati; `spnSRPProdName` dipakai di halaman toko Tokopedia padahal itu testid
halaman pencarian, dan hasilnya tiga run beruntun 0 produk tanpa satu pun error.

Karena itu lapis utama di sini tidak menebak nama apa pun. Ia memakai satu
kebenaran struktural yang tidak berubah antar-deploy: **setiap kartu produk
adalah sebuah tautan yang teksnya memuat harga.**

Lapis kedua (selector) tetap disediakan sebagai cadangan, tapi selectornya
dikirim dari pemanggil — modul ini tidak menyimpan daftar class apa pun.
"""
import hashlib
import re
import urllib.parse
from datetime import datetime
from pathlib import Path

from . import mp_common as C

OUTPUT_DIR = Path("output")

# Ambil semua tautan yang teksnya memuat harga, lalu buang pembungkusnya.
# Dijalankan di dalam halaman supaya penyaringannya satu kali jalan, bukan
# ratusan bolak-balik Python↔browser.
_JS_PANEN = """
(batas) => {
  const polaHarga = /Rp\\s?\\d[\\d.]{2,}/;
  const kandidat = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const teks = (a.innerText || '').trim();
    if (!teks || !polaHarga.test(teks)) continue;
    let href = '';
    try { href = a.href; } catch (e) { continue; }
    if (!href || !href.startsWith(location.origin)) continue;
    kandidat.push(a);
  }
  // Kalau anchor A memuat anchor kandidat lain, A itu pembungkus grid —
  // bukan kartu produk. Hanya simpan yang paling dalam.
  const kartu = kandidat.filter(a => !kandidat.some(b => b !== a && a.contains(b)));
  const out = [];
  for (const a of kartu.slice(0, batas)) {
    const img = a.querySelector('img');
    // `baris` diambil per ELEMEN DAUN, bukan dengan memecah innerText pada
    // newline. innerText hanya menyisipkan newline di batas elemen `display:block`,
    // jadi memecahnya berarti menyerahkan nasib panen kepada CSS Tokopedia:
    // begitu judul dan harga jatuh di satu baris, judulnya ikut memuat 'Rp',
    // ditolak sebagai kandidat nama, dan SELURUH kartu gagal tanpa satu pun error.
    // Struktur elemen tidak punya kelemahan itu.
    const baris = [];
    for (const el of a.querySelectorAll('*')) {
      if (el.children.length) continue;              // hanya daun
      const t = (el.textContent || '').replace(/\\s+/g, ' ').trim();
      if (t && !baris.includes(t)) baris.push(t);
    }
    out.push({
      href: a.href,
      teks: (a.innerText || '').trim(),
      baris: baris,
      img: img ? (img.currentSrc || img.src || '') : '',
    });
  }
  return out;
}
"""


def _kunci_produk(platform, username, url):
    """Kunci stabil antar-run untuk produk yang dipanen dari DOM.

    Tanpa id dari API, satu-satunya identitas yang bertahan adalah path URL-nya.
    Query string dibuang karena Tokopedia menempelkan parameter pelacakan yang
    berubah tiap kali halaman dimuat — kalau ikut di-hash, produk yang sama akan
    terlihat baru pada setiap run.
    """
    try:
        jalur = urllib.parse.urlparse(url).path.rstrip("/")
    except Exception:
        jalur = str(url or "")
    sidik = hashlib.sha1(jalur.encode("utf-8", "ignore")).hexdigest()[:12]
    awalan = "tkp" if platform == "tokopedia" else "shp"
    return f"{awalan}:{username or '-'}:{sidik}"


def _baca_terjual(teks):
    """'120 terjual' → 120 · '1,2rb terjual' → 1200 · '1.200 terjual' → 1200.

    Pemisah harus dibaca berbeda tergantung ada tidaknya akhiran 'rb'/'k':
    pada '1,2rb' tandanya desimal, pada '1.200' tandanya ribuan. Menghapus semua
    tanda baca lebih dulu — cara yang tampak paling gampang — membuat '1,2rb'
    terbaca 12.000, sepuluh kali lipat dari yang sebenarnya.
    """
    teks = str(teks or "")
    m = re.search(r"([\d][\d.,]*)\s*(rb|ribu|k|jt|juta)?\s*(?:\+\s*)?terjual",
                  teks, re.IGNORECASE)
    if not m:   # sebagian tampilan menaruh katanya di depan: 'Terjual 50'
        m = re.search(r"terjual\s*([\d][\d.,]*)\s*(rb|ribu|k|jt|juta)?",
                      teks, re.IGNORECASE)
    if not m:
        return 0

    angka, akhiran = m.group(1), (m.group(2) or "").lower()
    if akhiran:
        pengali = 1_000_000 if akhiran in ("jt", "juta") else 1_000
        # Dengan akhiran, satu pemisah yang diikuti 1-2 digit di ujung adalah
        # DESIMAL ('1,2rb' dan '1.2rb' sama-sama 1200). Pemisah ribuan selalu
        # datang bergrup tiga digit, jadi keduanya bisa dibedakan dengan aman.
        if re.fullmatch(r"\d+[.,]\d{1,2}", angka):
            angka = angka.replace(",", ".")
        else:
            angka = re.sub(r"[^\d]", "", angka) or "0"
        try:
            return int(float(angka) * pengali)
        except ValueError:
            return 0

    digit = re.sub(r"[^\d]", "", angka)
    return int(digit) if digit else 0


def _url_bersih(url):
    try:
        p = urllib.parse.urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}"
    except Exception:
        return url


def _nama_dari_baris(baris):
    """Nama produk dari daftar teks per elemen. '' bila tidak ada kandidat.

    Aturannya sama dengan `C.tebak_nama` — buang yang memuat harga, buang yang
    cuma angka/tanda baca, ambil yang terpanjang — tapi sumbernya struktur DOM,
    bukan newline hasil render. Itu bedanya: yang ini tidak bisa dimatikan oleh
    perubahan CSS.
    """
    kandidat = []
    for b in baris or []:
        b = str(b or "").strip()
        if not b or C._POLA_RUPIAH.search(b):
            continue
        if re.fullmatch(r"[\d\.\,%\s\+\-\|/]*", b):
            continue
        kandidat.append(b)
    return max(kandidat, key=len)[:200] if kandidat else ""


def rakit_produk(kartu, platform, store, sumber="dom_anchor"):
    """Satu kartu mentah → record produk. None bila tidak layak."""
    teks = kartu.get("teks") or ""
    baris = kartu.get("baris") or []

    # Harga: coba struktur dulu, lalu teks utuh. Keduanya memakai parser yang sama.
    harga_semua = C.parse_harga_semua(" \n".join(baris)) if baris else []
    if not harga_semua:
        harga_semua = C.parse_harga_semua(teks)
    if not harga_semua:
        return None

    # Nama: struktur lebih dipercaya; `tebak_nama` jadi cadangan.
    nama = _nama_dari_baris(baris) or C.tebak_nama(teks)
    if not nama:
        return None

    # Harga jual = harga PERTAMA sesuai urutan baca kartu (judul → harga →
    # harga coret → badge). Sempat memakai min(), tapi itu tidak aman: kartu
    # marketplace juga memuat angka rupiah yang bukan harga produk — nominal
    # cashback, potongan voucher, dan cicilan per bulan semuanya lebih kecil
    # dari harga jual dan akan menang kalau diambil yang terkecil.
    harga = harga_semua[0]
    lebih_besar = [h for h in harga_semua[1:] if h > harga]
    coret = max(lebih_besar) if lebih_besar else 0

    url = _url_bersih(kartu.get("href") or "")
    username = (store or {}).get("username") or ""
    # Terjual & rating juga dibaca dari gabungan struktur + teks: kalau innerText
    # sedang kosong/terkolaps, `teks` sendirian tidak cukup.
    teks_penuh = "\n".join([*baris, teks]) if baris else teks
    terjual = _baca_terjual(teks_penuh)
    rating = 0.0
    mr = re.search(r"\b([1-5][.,]\d)\b", teks_penuh)
    if mr:
        try:
            rating = float(mr.group(1).replace(",", "."))
        except ValueError:
            rating = 0.0

    return {
        "platform": platform,
        "product_key": _kunci_produk(platform, username, url),
        "shop_id": (store or {}).get("shop_id") or "",
        "item_id": "",
        "nama_produk": nama,
        "url": url,
        "image_url": kartu.get("img") or "",
        "harga": harga,
        "harga_coret": coret,
        "diskon_persen": int(round((coret - harga) / coret * 100)) if coret else 0,
        "rating": rating,
        "jumlah_ulasan": 0,
        "terjual": terjual,
        "stok": 0,
        "lokasi": "",
        "sumber": sumber,
    }


_JS_HITUNG = """
() => {
  const pola = /Rp\\s?\\d[\\d.]{2,}/;
  let n = 0;
  for (const a of document.querySelectorAll('a[href]')) {
    if (pola.test(a.innerText || '')) n++;
  }
  return n;
}
"""


_JS_UKUR = """
() => {
  const pola = /Rp\\s?\\d[\\d.]{2,}/;
  let n = 0;
  for (const a of document.querySelectorAll('a[href]')) {
    if (pola.test(a.innerText || '')) n++;
  }
  return [n, document.body ? document.body.scrollHeight : 0];
}
"""


async def muat_semua_kartu(page, *, target=0, batas_detik=75, minimal=3, cb=None):
    """Gulir sampai grid berhenti bertambah, lalu kembalikan jumlah kartu.

    Menggantikan kombinasi 'tunggu sebentar lalu gulir n kali'. Dua alasan:

    1. Grid Tokopedia dimuat bertahap. Menunggu ambang rendah membuat panen
       berhenti pada gelombang pertama — pernah berhenti di 14 kartu padahal
       halamannya akhirnya memuat 84.
    2. `?page=2` pada tab produk TIDAK memaginasi; ia mengembalikan halaman yang
       sama persis. Jadi menggulir adalah satu-satunya cara memuat sisanya, dan
       menavigasi ulang hanya memboroskan request tanpa menambah hasil.

    Berhenti setelah jumlah kartu sama pada 3 pemeriksaan berturut-turut — bukan
    sekali — supaya jeda antar-gelombang pemuatan tidak disalahartikan sebagai
    'sudah habis'.
    """
    import asyncio
    import time

    mulai = time.monotonic()
    sebelumnya, stabil, terbanyak = (-1, -1), 0, 0
    MINIMAL_DETIK = 10          # jangan pernah menyimpulkan "habis" sebelum ini
    STABIL_DIBUTUHKAN = 4       # ±5 dtk tanpa perubahan apa pun

    while time.monotonic() - mulai < batas_detik:
        # Lompat ke dasar halaman, bukan menggulir sedikit-sedikit: pemuat
        # infinite-scroll baru terpicu ketika sentinel di bawah grid masuk viewport.
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        await asyncio.sleep(1.3)

        try:
            kini = await page.evaluate(_JS_UKUR)
        except Exception:
            kini = [0, 0]
        n = int(kini[0] or 0)
        terbanyak = max(terbanyak, n)
        lewat = time.monotonic() - mulai

        if target and n >= target:
            if cb:
                cb(None, f"  ↳ {n} kartu (target {target} tercapai, {lewat:.1f} dtk)", 0)
            return n

        # Dua sinyal harus sama-sama diam: jumlah kartu DAN tinggi halaman.
        # Hitungan saja pernah menipu — grid sempat menahan >4 detik di 14 kartu
        # sebelum melanjutkan sampai 80, dan run itu berhenti terlalu cepat.
        if tuple(kini) == sebelumnya and n >= minimal:
            stabil += 1
            if stabil >= STABIL_DIBUTUHKAN and lewat >= MINIMAL_DETIK:
                if cb:
                    cb(None, f"  ↳ {n} kartu, berhenti bertambah setelah {lewat:.1f} dtk", 0)
                return n
        else:
            stabil = 0
        sebelumnya = tuple(kini)

    if cb:
        cb(None, f"  ↳ berhenti di batas {batas_detik} dtk ({terbanyak} kartu)", 0)
    return terbanyak


# Kandidat tombol "halaman berikutnya", diurutkan dari yang paling spesifik.
# Yang pertama ditemukan lewat diagnostik pada tab produk toko Tokopedia; sisanya
# jaring pengaman kalau nama testid-nya berubah.
_SEL_HALAMAN_BERIKUT = [
    '[data-testid="btnShopProductPageNext"]',
    'button[aria-label*="erikutnya"]',
    'button[aria-label*="ext page" i]',
    '[data-testid*="PageNext"]',
]


async def halaman_berikutnya(page, cb=None):
    """Klik tombol halaman berikutnya. True bila berhasil pindah halaman.

    Tab produk toko Tokopedia memuat 80 kartu per halaman dan sisanya hanya bisa
    dicapai lewat tombol ini — `?page=N` diabaikan, dan parameter yang dikenali
    situs adalah `perpage` (huruf kecil), bukan `perPage`.
    """
    for sel in _SEL_HALAMAN_BERIKUT:
        try:
            tombol = await page.query_selector(sel)
        except Exception:
            continue
        if not tombol:
            continue
        try:
            if not await tombol.is_enabled() or not await tombol.is_visible():
                return False
            await tombol.scroll_into_view_if_needed(timeout=5_000)
            await tombol.click(timeout=10_000)
            if cb:
                cb(None, "  ↳ pindah ke halaman berikutnya", 0)
            return True
        except Exception as e:
            if cb:
                cb(None, f"  ↳ gagal klik halaman berikutnya: {type(e).__name__}", 0)
            return False
    return False


async def tunggu_konten(page, *, minimal=3, batas_detik=30, cb=None):
    """Tunggu sampai kartu produk benar-benar ter-render, bukan sekadar DOM siap.

    Tokopedia sekarang aplikasi Apollo/React ('zeus'): HTML awalnya 200KB tapi
    isinya skrip — teks yang terlihat cuma ~300 karakter dan nol tautan berharga.
    Memanen pada `domcontentloaded` + jeda 3 detik pasti membaca halaman kosong,
    dan itulah yang membuat tiga run beruntun melaporkan 0 produk.

    Menunggu kondisi yang benar-benar kita butuhkan (ada tautan berharga) jauh
    lebih andal daripada menunggu selector tertentu muncul — tidak ada nama class
    yang perlu ditebak, dan ia otomatis ikut kalau Tokopedia mengubah tata letak.
    """
    import asyncio
    import time

    mulai = time.monotonic()
    sebelumnya, stabil, terbanyak = -1, 0, 0
    while time.monotonic() - mulai < batas_detik:
        try:
            n = await page.evaluate(_JS_HITUNG)
        except Exception:
            n = 0
        terbanyak = max(terbanyak, n)

        # Berhenti saat jumlahnya BERHENTI BERTAMBAH, bukan saat melewati ambang.
        # Ambang rendah membuat panen berhenti pada render pertama yang baru
        # memuat sebagian grid — hasilnya berbeda-beda tiap run untuk toko yang sama.
        if n >= minimal and n == sebelumnya:
            stabil += 1
            if stabil >= 2:
                if cb:
                    cb(None, f"  ↳ konten stabil setelah "
                             f"{time.monotonic() - mulai:.1f} dtk ({n} tautan berharga)", 0)
                return n
        else:
            stabil = 0
        sebelumnya = n

        # Gulir: grid Tokopedia memuat kartunya saat masuk viewport.
        try:
            await page.evaluate("window.scrollBy(0, 900)")
        except Exception:
            pass
        await asyncio.sleep(1.2)

    if cb:
        cb(None, f"  ↳ konten belum stabil dalam {batas_detik} dtk "
                 f"(maksimum {terbanyak} tautan berharga) — lanjut dengan yang ada", 0)
    return terbanyak


async def panen_anchor(page, platform, store, *, maks=60, cb=None):
    """Lapis 1 — panen berbasis anchor. Tidak menyebut satu pun nama class."""
    try:
        kartu = await page.evaluate(_JS_PANEN, max(maks * 3, 60))
    except Exception as e:
        return [], f"panen anchor gagal: {type(e).__name__}: {e}"

    if not kartu:
        return [], "tidak ada tautan ber-harga di halaman"

    # Alasan penolakan dihitung terpisah. Versi lama selalu melaporkan "tidak ada
    # nama produk" bahkan ketika yang gagal adalah parsing harga — menuduh yang
    # salah, dan itu membuat satu sesi penuh dihabiskan menebak.
    rows, terlihat = [], set()
    n_tanpa_harga = n_tanpa_nama = n_duplikat = 0
    for k in kartu:
        rec = rakit_produk(k, platform, store)
        if not rec:
            sumber_teks = "\n".join(k.get("baris") or []) or (k.get("teks") or "")
            if not C.parse_harga_semua(sumber_teks):
                n_tanpa_harga += 1
            else:
                n_tanpa_nama += 1
            continue
        if rec["product_key"] in terlihat:
            n_duplikat += 1
            continue
        terlihat.add(rec["product_key"])
        rows.append(rec)
        if len(rows) >= maks:
            break

    if cb:
        cb(None, f"  ↳ anchor: {len(kartu)} tautan ber-harga → {len(rows)} produk", 0)
    if rows:
        return rows, ""
    return [], (f"{len(kartu)} tautan ber-harga ditemukan, tapi 0 jadi produk "
                f"(harga tak terbaca: {n_tanpa_harga}, nama tak terbaca: "
                f"{n_tanpa_nama}, duplikat: {n_duplikat})")


async def panen_selector(page, platform, store, sel, *, maks=60, cb=None):
    """Lapis 2 — selector warisan. `sel` dikirim pemanggil, tidak disimpan di sini."""
    if not sel:
        return [], "tidak ada selector yang diberikan"

    items = []
    dipakai = ""
    for c in sel.get("card") or []:
        try:
            items = await page.query_selector_all(c)
        except Exception:
            continue
        if items:
            dipakai = c
            break
    if not items:
        return [], "tidak ada selector kartu yang cocok"

    rows = []
    for it in items[:maks]:
        try:
            teks = (await it.inner_text()).strip()
        except Exception:
            continue
        href = ""
        try:
            a = await it.query_selector("a[href]")
            if a:
                href = await a.get_attribute("href") or ""
            elif (await it.get_attribute("href")):
                href = await it.get_attribute("href")
        except Exception:
            pass
        rec = rakit_produk({"teks": teks, "href": href, "img": ""},
                           platform, store, sumber="dom_selector")
        if rec:
            rows.append(rec)
    if cb:
        cb(None, f"  ↳ selector '{dipakai}': {len(items)} kartu → {len(rows)} produk", 0)
    return rows, ("" if rows else f"selector '{dipakai}' cocok {len(items)} kartu "
                                  f"tapi tidak ada harga/nama yang terbaca")


# ─── Diagnostik ───────────────────────────────────────────────────────────────

async def simpan_diagnostik(page, label, sel=None, cb=None):
    """Simpan bukti saat panen 0 produk, supaya tidak ada lagi tebak-tebakan.

    Tiga run 0-hasil sebelumnya hanya meninggalkan file Excel kosong 4991 byte —
    tidak ada cara mengetahui apakah halamannya kosong, diblokir, atau
    selectornya yang meleset. Folder ini menjawabnya dalam 30 detik.
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    aman = re.sub(r"[^A-Za-z0-9_-]+", "-", str(label or "toko")).strip("-") or "toko"
    folder = OUTPUT_DIR / f"diag_{aman}_{stamp}"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        if cb:
            cb(None, f"⚠ Gagal membuat folder diagnostik: {type(e).__name__}: {e}", 0)
        return ""

    baris = [f"label      : {label}", f"waktu      : {stamp}"]
    try:
        baris.append(f"url final  : {page.url}")
        baris.append(f"judul      : {await page.title()}")
    except Exception as e:
        baris.append(f"url/judul  : gagal dibaca ({type(e).__name__})")

    try:
        html = await page.content()
        (folder / "page.html").write_text(html, encoding="utf-8", errors="replace")
        baris.append(f"panjang html: {len(html)} karakter")
    except Exception as e:
        baris.append(f"page.html  : gagal disimpan ({type(e).__name__})")

    try:
        hitung = await page.evaluate("""() => {
            const a = document.querySelectorAll('a[href]');
            let berharga = 0;
            const pola = /Rp\\s?\\d[\\d.]{2,}/;
            for (const el of a) if (pola.test(el.innerText || '')) berharga++;
            return {anchor: a.length, anchorBerharga: berharga,
                    img: document.querySelectorAll('img').length,
                    teks: (document.body ? document.body.innerText.length : 0)};
        }""")
        baris.append(f"jumlah anchor        : {hitung['anchor']}")
        baris.append(f"anchor memuat harga  : {hitung['anchorBerharga']}")
        baris.append(f"jumlah <img>         : {hitung['img']}")
        baris.append(f"panjang teks body    : {hitung['teks']}")
    except Exception as e:
        baris.append(f"hitung DOM : gagal ({type(e).__name__})")

    # Angka di atas menghitung himpunan yang BERBEDA dari yang dipanen: ia tidak
    # menguji origin maupun anchor-terdalam. Sesi lalu habis menebak justru karena
    # itu — "80 anchor ber-harga" ternyata bukan "80 kartu bisa dipanen". Bagian
    # di bawah menjalankan _JS_PANEN yang sesungguhnya dan membuka tiap tahap.
    try:
        tahap = await page.evaluate("""() => {
            const pola = /Rp\\s?\\d[\\d.]{2,}/;
            const semua = Array.from(document.querySelectorAll('a[href]'));
            const berharga = semua.filter(a => pola.test((a.innerText || '').trim()));
            const seOrigin = berharga.filter(a => {
                try { return a.href && a.href.startsWith(location.origin); }
                catch (e) { return false; }
            });
            const terdalam = seOrigin.filter(
                a => !seOrigin.some(b => b !== a && a.contains(b)));
            const contoh = terdalam[0] || seOrigin[0] || berharga[0] || null;
            let baris = [];
            if (contoh) {
                for (const el of contoh.querySelectorAll('*')) {
                    if (el.children.length) continue;
                    const t = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (t && !baris.includes(t)) baris.push(t);
                }
            }
            return {
                origin: location.origin,
                nBerharga: berharga.length,
                nSeOrigin: seOrigin.length,
                nTerdalam: terdalam.length,
                contohHref: contoh ? contoh.href : '',
                contohTeks: contoh ? (contoh.innerText || '') : '',
                contohBaris: baris,
            };
        }""")
        baris.append("")
        baris.append("PIPELINE PANEN yang sesungguhnya (_JS_PANEN):")
        baris.append(f"  location.origin        : {tahap['origin']}")
        baris.append(f"  1. anchor ber-harga    : {tahap['nBerharga']}")
        baris.append(f"  2. lolos uji origin    : {tahap['nSeOrigin']}")
        baris.append(f"  3. anchor terdalam     : {tahap['nTerdalam']}  <-- ini yang dipanen")
        baris.append(f"  contoh href            : {tahap['contohHref'][:120]}")
        baris.append(f"  contoh innerText       : {tahap['contohTeks']!r}")
        baris.append(f"  contoh baris (struktur): {tahap['contohBaris']}")
        if tahap["nTerdalam"]:
            contoh_rec = rakit_produk(
                {"href": tahap["contohHref"], "teks": tahap["contohTeks"],
                 "baris": tahap["contohBaris"], "img": ""}, "tokopedia", {})
            baris.append(f"  rakit_produk(contoh)   : "
                         + (f"OK — nama={contoh_rec['nama_produk']!r} "
                            f"harga={contoh_rec['harga']}" if contoh_rec
                            else "DITOLAK (harga atau nama tidak terbaca)"))
    except Exception as e:
        baris.append(f"pipeline panen : gagal ({type(e).__name__}: {e})")

    if sel:
        baris.append("")
        baris.append("hasil tiap selector warisan:")
        for jenis, daftar in sel.items():
            for s in daftar or []:
                try:
                    n = len(await page.query_selector_all(s))
                except Exception:
                    n = -1
                baris.append(f"  [{jenis:7}] {n:4}  {s}")

    try:
        # `full_page=True` karena `muat_semua_kartu` sudah menggulir ke dasar
        # halaman lebih dulu — potret viewport hanya menangkap footer, bukan grid
        # produk yang justru sedang dipertanyakan.
        await page.evaluate("window.scrollTo(0, 0)")
        await page.screenshot(path=str(folder / "screenshot.png"), full_page=True)
    except Exception as e:
        baris.append(f"screenshot : gagal ({type(e).__name__})")

    (folder / "ringkas.txt").write_text("\n".join(baris) + "\n", encoding="utf-8")
    if cb:
        cb(None, f"🔎 Bukti diagnostik disimpan di: {folder}", 0)
    return str(folder)
