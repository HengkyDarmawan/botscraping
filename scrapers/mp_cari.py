"""
mp_cari.py — cari SATU produk di beberapa toko, dengan sedikit request.

Masalahnya dirumuskan user sendiri:

    "enggak perlu banyak — selama dia bisa dapat data spesifik yang dibutuhin
     udah cukup. Kalau toko itu tidak jual, tidak masalah. Tapi kalau toko itu
     jual, ya harus ada harganya. Cuma keyword masing-masing toko suka
     berbeda-beda, makanya agak susah scraping spesifik."

Jadi yang dikejar bukan banyak data, melainkan **recall pada satu produk**:
kalau toko itu menjualnya, harganya wajib ketemu; kalau tidak, jawaban kosong
itu sah — asal jelas bahwa sudah dicari sungguh-sungguh.

Kuncinya: judul produk ditulis semaunya tiap toko, tapi **etalase** (kategori
buatan penjual) jauh lebih seragam. Semua toko komputer punya etalase processor,
VGA, memory. Jadi kueri dipetakan ke etalase dulu, lalu hanya etalase itu yang
dipanen — 1-3 request per toko, bukan seluruh katalog.

Kalau etalase meleset, ada tangga cadangan yang naik bertahap dan berhenti
begitu ketemu. Tangga ke-2 memakai **kode model** (`12700K`), bukan kalimat
bebas, karena kode model satu-satunya token yang selamat dari semua gaya
penulisan.
"""
import re
import urllib.parse

import db
from . import mp_dom
from . import mp_endpoints as EP
from . import mp_lexicon as LX
from . import mp_match
from . import mp_session as S

# Etalase dianggap masih segar selama ini; di bawahnya tidak dipanen ulang.
UMUR_SEGAR_JAM = 12

AMBANG_ETALASE = 0.30      # skor minimal sebuah etalase dianggap relevan
MAKS_ETALASE = 3           # etalase per toko yang dipanen
MAKS_HAL_PRODUK = 3        # batas halaman pada tangga terakhir


def _slug_ke_teks(slug):
    """'processor-intel-gen-14' → 'processor intel gen 14'."""
    return re.sub(r"[-_/]+", " ", str(slug or "")).strip()


# Jembatan kosakata antara cara USER mengetik dan cara TOKO menamai kategori.
# Tanpa ini, "ram ddr4 8gb" tidak pernah menemukan etalase "MEMORY PC AND LAPTOP"
# dan "rtx 3060" tidak pernah menemukan "VGA NVIDIA GEFORCE" — nol token yang sama.
#
# Ini memang daftar tulis-tangan, dan di tempat lain daftar semacam itu sengaja
# dihindari. Bedanya: kosakata KATEGORI jauh lebih kecil dan jauh lebih stabil
# daripada judul produk — "VGA" dan "PROCESSOR" tidak berganti nama tiap deploy,
# sementara judul produk berubah tiap promo. Daftar ini juga cuma menentukan
# halaman mana yang dibuka, bukan produk mana yang cocok; kalau meleset, tangga
# cadangan di bawahnya tetap menangkap.
_KATEGORI_SINONIM = {
    "processor": {"processor", "prosesor", "cpu", "intel", "amd", "ryzen", "core",
                  "i3", "i5", "i7", "i9", "celeron", "pentium", "athlon", "xeon",
                  "threadripper", "ultra"},
    "vga":       {"vga", "gpu", "geforce", "nvidia", "radeon", "rtx", "gtx", "rx",
                  "quadro", "grafis", "graphic"},
    "memory":    {"memory", "memori", "ram", "ddr3", "ddr4", "ddr5", "sodimm",
                  "udimm", "dimm"},
    "storage":   {"storage", "ssd", "hdd", "harddisk", "hardisk", "nvme", "m2",
                  "sata", "flashdisk", "flashdrive", "microsd", "penyimpanan"},
    "motherboard": {"motherboard", "mobo", "mainboard", "lga", "am4", "am5",
                    "chipset", "socket"},
    "psu":       {"psu", "power", "supply", "watt", "daya"},
    "monitor":   {"monitor", "lcd", "led", "inch", "inci", "ips", "va", "oled"},
    "peripheral": {"keyboard", "mouse", "mousepad", "headset", "peripheral",
                   "combo", "webcam", "speaker"},
    "cooler":    {"cooler", "fan", "hsf", "aio", "liquid", "pendingin",
                  "heatsink", "thermal"},
    "casing":    {"casing", "case", "cube"},
    "networking": {"router", "switch", "wifi", "lan", "networking", "modem",
                   "access", "point"},
    "laptop":    {"laptop", "notebook", "macbook", "ultrabook"},
    "printer":   {"printer", "tinta", "toner", "scanner"},
}

# token → konsep, dibalik sekali saat impor
_TOKEN_KE_KONSEP = {t: k for k, ts in _KATEGORI_SINONIM.items() for t in ts}

# Etalase berisi barang rakitan/paket, bukan komponen satuan.
_BUNDEL_RE = re.compile(r"\b(rakitan|paket|bundling|bundle|komplit|komputer set|"
                        r"pc built|built up|simulator)\b")


def _konsep_kepala(token_list):
    """Konsep kategori dari daftar token, diambil dari token PERTAMA yang dikenal.

    Nama etalase di marketplace Indonesia hampir selalu dipimpin kategorinya:
    "MOTHERBOARD INTEL" itu motherboard, bukan processor — meski memuat 'intel'.
    Mengambil konsep dari token pertama yang cocok menghormati urutan itu;
    mengambil semua konsep membuat "MOTHERBOARD INTEL" ikut menang untuk kueri CPU.
    """
    for t in token_list or []:
        k = _TOKEN_KE_KONSEP.get(t)
        if k:
            return k
    return None


def _konsep_semua(token_list):
    return {_TOKEN_KE_KONSEP[t] for t in token_list or [] if t in _TOKEN_KE_KONSEP}


def pilih_etalase(kueri, etalase_list, lex=None, ambang=AMBANG_ETALASE,
                  maks=MAKS_ETALASE):
    """Etalase mana yang mungkin memuat produk ini, terurut dari yang paling cocok.

    Memakai mesin skor yang sama dengan pencocokan produk (`mp_match.skor`),
    hanya saja dokumennya adalah NAMA ETALASE. "intel 7 gen 12" cocok ke
    'processor', 'processor-intel-gen-14', 'rakitan-i7-gen12'; tidak ke
    'monitor' atau 'casing'.
    """
    lex = lex or LX.Lexicon()
    q = mp_match.urai_kueri(kueri, lex) if isinstance(kueri, str) else kueri
    qt = q["token_inti"] or q["token"]

    konsep_q = _konsep_semua(qt)

    nilai = []
    for e in etalase_list or []:
        nama_tok = LX.normalisasi(e.get("nama") or "")
        et = LX.normalisasi(f"{e.get('nama') or ''} {_slug_ke_teks(e.get('slug'))}")
        if not et:
            continue
        # Skor dua arah: berapa banyak kueri tercakup etalase, DAN seberapa
        # spesifik etalase itu terhadap kueri. Tanpa arah kedua, etalase raksasa
        # bernama "SEMUA PRODUK" akan menang atas "PROCESSOR INTEL GEN 14".
        s_maju = mp_match.skor(qt, et, lex)
        s_balik = mp_match.skor(et, qt, lex)
        s = 0.55 * s_maju + 0.20 * s_balik

        # Kecocokan KONSEP jauh lebih menentukan daripada tumpang-tindih token:
        # "ram ddr4 8gb" vs "MEMORY PC AND LAPTOP" nol token sama, tapi jelas
        # kategori yang benar.
        kepala = _konsep_kepala(nama_tok) or _konsep_kepala(et)
        if kepala and kepala in konsep_q:
            s += 0.45
            # Etalase yang dipimpin kata kategori kanonik ("PROCESSOR", "VGA")
            # lebih dipercaya daripada nama karangan penjual.
            if nama_tok and nama_tok[0] in _TOKEN_KE_KONSEP:
                s += 0.35
        elif kepala and konsep_q:
            s -= 0.30      # kategori lain sama sekali

        # Etalase paket rakitan ("RAKITAN I7 GEN12") memuat i7+gen12 secara
        # harfiah, jadi tumpang-tindih tokennya justru paling tinggi — padahal
        # isinya PC rakitan seharga puluhan juta, bukan processor satuan.
        if _BUNDEL_RE.search(" ".join(et)):
            s -= 0.40

        # Spesifikasi bentrok: kueri i7/gen12 vs etalase "PROCESSOR INTEL I3 GEN 14".
        # Memakai penjaga yang sama dengan pencocokan produk.
        if mp_match.spek_bentrok(qt, et):
            s -= 0.35

        # Merek bertentangan: kueri "intel …" tidak semestinya membuka etalase
        # "PROCESSOR AMD" lebih dulu, walau kategorinya memang benar.
        merek_q = {t for t in qt if lex.kelas(t) == "merek"}
        merek_e = {t for t in et if lex.kelas(t) == "merek"}
        if merek_q and merek_e and not (merek_q & merek_e):
            s -= 0.25

        if s >= ambang:
            nilai.append((s, e))

    nilai.sort(key=lambda x: -x[0])
    return [dict(e, _skor=round(s, 3)) for s, e in nilai[:maks]]


def _istilah_cari(kueri, lex=None):
    """Kata kunci untuk pencarian DI DALAM toko, dari yang paling berdaya.

    Kode model lebih dulu: 'PROCESSOR INTEL CORE I7 12700K BOX GARANSI RESMI'
    dan 'Intel i7-12700K Tray' tidak berbagi satu pun frasa, tapi keduanya
    memuat '12700K'. Kalimat bebas ditaruh terakhir karena paling gampang meleset.
    """
    lex = lex or LX.Lexicon()
    q = mp_match.urai_kueri(kueri, lex) if isinstance(kueri, str) else kueri
    istilah = []
    for t in sorted(q.get("model_persis") or [], key=len, reverse=True):
        istilah.append(t)
    if not istilah:
        # Kueri tidak menyebut kode model. Pakai token spek + merek yang paling
        # informatif, mis. 'ddr4 8gb' atau 'logitech g502'.
        kuat = [t for t in q["token_inti"]
                if lex.kelas(t) in ("model", "spek", "merek")]
        if kuat:
            istilah.append(" ".join(kuat[:3]))
    teks = q["teks"] if isinstance(q.get("teks"), str) else str(kueri)
    if teks and teks not in istilah:
        istilah.append(teks)
    return istilah


async def _panen_url(page, url, store, maks, cb=None, label=""):
    """Buka satu URL dan panen kartunya. Return (rows, galat)."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await S.jeda(1.5, 3.0)
    except Exception as e:
        return [], f"gagal membuka {label or url}: {type(e).__name__}: {e}"

    kena, alasan = await S.kena_challenge(page)
    if kena:
        return [], f"terhalang verifikasi ({alasan})"

    # Halaman toko Tokopedia dirender server-side pada perpage=80, jadi tidak
    # perlu menggulir dulu. `muat_semua_kartu` tetap dipanggil dengan target
    # kecil sebagai jaring pengaman bila render sempat tertunda.
    await mp_dom.muat_semua_kartu(page, target=1, batas_detik=12, minimal=1, cb=None)
    rows, galat = await mp_dom.panen_anchor(page, store.get("platform"), store,
                                            maks=maks, cb=None)
    return rows, galat


def _simpan(rows, store, run_id=None):
    baru = 0
    for r in rows:
        try:
            if db.mp_upsert_product(r, store_id=store.get("id"),
                                    is_own=store.get("is_own")) == "insert":
                baru += 1
            db.mp_snapshot_price(r.get("product_key"), r.get("harga"),
                                 r.get("terjual"), r.get("stok"), run_id=run_id)
        except Exception:
            continue
    return baru


def _cocok(rows, kueri, lex):
    """Baris hasil panen yang benar-benar cocok dengan kueri."""
    return mp_match.cari(kueri, rows, lex)


async def cari_di_toko(page, store, kueri, lex=None, cb=None, run_id=None,
                       pacer=None, maks=80):
    """Cari satu produk di satu toko. Return laporan dict.

    Naik tangga sampai ketemu, lalu berhenti. Tiap anak tangga dicatat supaya
    hasil kosong tetap bisa dipertanggungjawabkan.
    """
    lex = lex or LX.Lexicon()
    nama_toko = store.get("nama") or store.get("username") or "?"
    username = store.get("username") or ""
    lapor = {"store_id": store.get("id"), "toko": nama_toko,
             "is_own": int(bool(store.get("is_own"))),
             "tingkat": [], "n_request": 0, "produk": [], "galat": ""}

    def catat(nama, n, ket=""):
        lapor["tingkat"].append({"tingkat": nama, "hasil": n, "ket": ket})
        if cb:
            cb(None, f"  · {nama_toko} — {nama}: {n} kandidat"
                     + (f" ({ket})" if ket else ""), 0)

    async def coba(url, label):
        lapor["n_request"] += 1
        if pacer:
            await pacer.tunggu()
        rows, galat = await _panen_url(page, url, store, maks, cb, label)
        if galat and not rows:
            if "verifikasi" in galat:
                lapor["galat"] = galat
                if pacer:
                    pacer.gagal(galat)
            return [], galat
        if pacer:
            pacer.sukses()
        _simpan(rows, store, run_id)
        return rows, ""

    # ── Tangga 1: etalase yang cocok ──
    etalase = db.mp_etalase_list(store.get("id"))
    if not etalase:
        daftar, galat = await tokopedia_etalase_aman(page, username, cb)
        lapor["n_request"] += 1
        for e in daftar:
            db.mp_etalase_upsert(store.get("id"), e["slug"], e["nama"], e["url"])
        etalase = db.mp_etalase_list(store.get("id"))

    terpilih = pilih_etalase(kueri, etalase, lex)
    for e in terpilih:
        umur = db.mp_etalase_umur_jam(store.get("id"), e["slug"])
        if umur is not None and umur < UMUR_SEGAR_JAM:
            # Sudah dipanen baru-baru ini — pakai yang tersimpan, jangan menembak
            # jaringan lagi hanya untuk mengambil data yang sama.
            tersimpan = [p for p in db.mp_products_semua(store_ids=[store.get("id")])]
            cocok = _cocok(tersimpan, kueri, lex)
            if cocok:
                catat(f"etalase '{e['nama']}' (cache {umur:.1f} jam)", len(cocok))
                lapor["produk"] = cocok
                return lapor
            continue
        url = EP.url("tokopedia", "shop_etalase", username=username, slug=e["slug"])
        rows, galat = await coba(url, f"etalase {e['slug']}")
        if galat:
            catat(f"etalase '{e['nama']}'", 0, galat)
            if lapor["galat"]:
                return lapor
            continue
        db.mp_etalase_dipanen(store.get("id"), e["slug"], len(rows))
        cocok = _cocok(rows, kueri, lex)
        catat(f"etalase '{e['nama']}'", len(cocok), f"dari {len(rows)} produk")
        if cocok:
            lapor["produk"] = cocok
            return lapor

    # ── Tangga 2: cari di dalam toko pakai kode model ──
    for istilah in _istilah_cari(kueri, lex)[:2]:
        url = EP.url("tokopedia", "shop_search", username=username,
                     keyword=urllib.parse.quote_plus(istilah))
        rows, galat = await coba(url, f"cari '{istilah}'")
        if galat:
            catat(f"cari dalam toko '{istilah}'", 0, galat)
            if lapor["galat"]:
                return lapor
            continue
        cocok = _cocok(rows, kueri, lex)
        catat(f"cari dalam toko '{istilah}'", len(cocok), f"dari {len(rows)} hasil")
        if cocok:
            lapor["produk"] = cocok
            return lapor

    # ── Tangga 3: tab produk, beberapa halaman pertama ──
    for hal in range(1, MAKS_HAL_PRODUK + 1):
        url = (EP.url("tokopedia", "shop_product_page", username=username) if hal == 1
               else EP.url("tokopedia", "shop_product_halaman",
                           username=username, halaman=hal))
        rows, galat = await coba(url, f"produk halaman {hal}")
        if galat:
            catat(f"tab produk hal {hal}", 0, galat)
            if lapor["galat"]:
                return lapor
            break
        cocok = _cocok(rows, kueri, lex)
        catat(f"tab produk hal {hal}", len(cocok), f"dari {len(rows)} produk")
        if cocok:
            lapor["produk"] = cocok
            return lapor
        if len(rows) < 40:      # halaman terakhir
            break

    return lapor


async def tokopedia_etalase_aman(page, username, cb=None):
    """Bungkus `mp_api.tokopedia_etalase` — impor di dalam fungsi untuk
    menghindari impor melingkar (mp_api tidak mengimpor modul ini)."""
    from . import mp_api
    try:
        return await mp_api.tokopedia_etalase(page, username, cb)
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def ringkas_laporan(laporan):
    """Laporan per toko → kalimat Indonesia yang bisa langsung ditampilkan."""
    n = len(laporan.get("produk") or [])
    if laporan.get("galat"):
        return f"⚠ {laporan['toko']}: {laporan['galat']}"
    if n:
        jalur = laporan["tingkat"][-1]["tingkat"] if laporan["tingkat"] else "?"
        return (f"✓ {laporan['toko']}: {n} produk cocok "
                f"(lewat {jalur}, {laporan['n_request']} request)")
    dicoba = len(laporan.get("tingkat") or [])
    return (f"— {laporan['toko']}: kemungkinan tidak menjual produk ini "
            f"({dicoba} cara dicoba, {laporan['n_request']} request)")


# ─── Orkestrator lintas toko ──────────────────────────────────────────────────

async def _async_cari(kueri, store_ids=None, cb=None, should_stop=None,
                      engine="camoufox", headless=False):
    from . import pricing          # impor malas: pricing sudah mengimpor modul ini
    from datetime import datetime

    lex = LX.bangun_dari_db()
    stores = [s for s in pricing.load_stores()
              if s.get("platform") == "tokopedia"
              and (not store_ids or s.get("id") in store_ids)]
    if not stores:
        return {"ok": False, "error": "Tidak ada toko Tokopedia yang dipilih."}

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        db.mp_run_start(run_id, mode="cari", kueri=kueri, engine=engine,
                        store_ids=",".join(s["id"] for s in stores))
    except Exception:
        pass

    pacer = S.Pacer()
    laporan = []
    cb(3, f"Mencari '{kueri}' di {len(stores)} toko...", 0)

    async with pricing._browser_session(headless, None, cb, engine) as page:
        for i, store in enumerate(stores):
            if should_stop and should_stop():
                cb(None, "⏹ Dihentikan user — hasil sementara tetap disimpan.", 0)
                break
            rem = pacer.harus_berhenti()
            if rem:
                cb(None, f"⏹ Pacer: {rem}", 0)
                break
            cb(5 + int(i / len(stores) * 85),
               f"[{i+1}/{len(stores)}] {store.get('nama')}", 0)
            lap = await cari_di_toko(page, store, kueri, lex, cb, run_id, pacer)
            laporan.append(lap)
            cb(None, ringkas_laporan(lap), sum(len(l["produk"]) for l in laporan))

    total = sum(len(l["produk"]) for l in laporan)
    try:
        db.mp_run_finish(run_id, total=total)
    except Exception:
        pass
    cb(100, f"Selesai — {total} produk cocok dari {len(laporan)} toko.", total)
    return {"ok": True, "kueri": kueri, "run_id": run_id,
            "laporan": laporan, "total": total}


def cari_produk(params: dict, callback, should_stop=None):
    """Entry point gaya scraper lain: 3 parameter supaya tombol Stop hidup."""
    import asyncio
    import sys as _sys
    kueri = str(params.get("kueri") or params.get("keyword") or "").strip()
    if not kueri:
        callback(100, "❌ Ketik dulu nama produknya.", 0)
        return None
    if _sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    hasil = asyncio.run(_async_cari(
        kueri, params.get("store_ids"), callback, should_stop,
        engine=params.get("engine", "camoufox"),
        headless=bool(params.get("headless", False))))
    return None if not hasil or not hasil.get("ok") else ""


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    # Konsol Windows cp1252 crash kena emoji di pesan log.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="Cari satu produk di toko-toko")
    ap.add_argument("--kueri", required=True)
    ap.add_argument("--toko", default="semua", help="id toko, koma; 'semua' = semua")
    ap.add_argument("--headless", action="store_true")
    a = ap.parse_args()

    ids = None if a.toko == "semua" else [x.strip() for x in a.toko.split(",")]

    def konsol(pct, msg, found):
        if msg:
            print(msg, flush=True)

    cari_produk({"kueri": a.kueri, "store_ids": ids, "headless": a.headless},
                konsol, lambda: False)

    print("\n=== HASIL TERSIMPAN ===")
    lex = LX.bangun_dari_db()
    for g in mp_match.cari_dan_kelompokkan(a.kueri, db.mp_products_semua(), lex)[:5]:
        print(f"  {g['label'][:40]:42} {g['n_toko']} toko  "
              f"Rp {g['harga_min']:,} - Rp {g['harga_maks']:,}")
