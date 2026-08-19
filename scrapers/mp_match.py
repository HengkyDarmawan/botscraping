"""
mp_match.py — kueri longgar → produk yang benar, lalu dikelompokkan per varian.

Masalah yang dipecahkan: user mengetik **"intel 7 gen 12"** dan berharap sistem
mengerti. Tiga langkah:

1. `urai_kueri`  — kueri diterjemahkan ke keluarga model lewat alias yang sudah
                   dipelajari `mp_lexicon` dari cara toko menulis judul.
2. `cari`        — produk diberi skor dengan bobot per kelas token; kata promosi
                   berbobot NOL supaya "garansi resmi" tidak mencocokkan segalanya.
3. `kelompokkan` — hasil dipisah per model penuh. "intel 7 gen 12" sah cocok ke
                   12700K, 12700F, dan 12700KF sekaligus — tiga produk berbeda
                   dengan harga berbeda. Menggabungkannya jadi satu "harga pasar"
                   akan mengulang persis bug yang sedang kita perbaiki, jadi user
                   memilih dulu varian mana yang dimaksud.
"""
import difflib
import re
from collections import Counter, defaultdict

from . import mp_common
from . import mp_lexicon as LX

# Bobot per kelas token. Angka-angka ini yang menentukan apakah pencocokan
# masuk akal, jadi alasannya ditulis di sini, bukan dianggap sihir:
#   model 3.0 — di elektronik, kode model ADALAH produknya
#   spek  2.0 — 8GB vs 16GB itu barang berbeda dengan harga berbeda
#   merek 1.5 — mempersempit, tapi satu merek punya ratusan produk
#   umum  1.0 — kata biasa
#   promo 0.0 — "ready", "garansi resmi", "cod" tidak menunjuk produk apa pun
BOBOT = {"model": 3.0, "spek": 2.0, "merek": 1.5, "umum": 1.0, "promo": 0.0}

AMBANG_SARAN = 0.72      # di atas ini: pasangan diyakini
AMBANG_KANDIDAT = 0.45   # di bawah ini: tidak ditampilkan sama sekali


# ─── Kueri ────────────────────────────────────────────────────────────────────

def urai_kueri(teks, lex=None, bukti_min=2) -> dict:
    """Kueri bebas → token + keluarga model yang dimaksud.

    Kuncinya ada di **irisan**. Dari korpus, `i7` pernah menemani 127xx dan 117xx;
    `gen12` pernah menemani 127xx, 124xx, 129xx. Masing-masing sendirian terlalu
    longgar — irisannya tepat satu: `127xx`. Pemetaan itu tidak saya tulis di
    kamus mana pun; ia dipanen dari cara toko-toko itu sendiri menulis judul.
    """
    lex = lex or LX.Lexicon()
    token = LX.normalisasi(teks)
    langsung, dari_alias = set(), []
    prefiks = set()

    for t in token:
        if lex.kelas(t) == "model":
            k = LX.keluarga(t)
            if k:
                langsung.add(k)
            continue
        # "gen 12" → nomor model diawali 12 (12700K, 12400F). Aturan ini tidak
        # bergantung pada ko-okurensi: pada korpus nyata hampir tidak ada judul
        # yang menulis "Gen 12" — mereka menulis "Alder Lake" atau "LGA 1700" —
        # jadi alias tidak pernah punya bukti, padahal kaitannya pasti.
        m = re.match(r"^gen(\d{1,2})$", t)
        if m:
            prefiks.add(m.group(1))
            continue
        fam = lex.keluarga_dari_alias(t, bukti_min=bukti_min)
        if fam:
            dari_alias.append(fam)

    if langsung:
        # Kueri menyebut kode model persis — itu paling kuat, alias tidak perlu.
        keluarga = langsung
    elif dari_alias:
        irisan = set.intersection(*dari_alias)
        keluarga = irisan or set().union(*dari_alias)
    else:
        keluarga = set()

    # Prefiks generasi mempersempit keluarga yang sudah ada, atau berdiri sendiri
    # bila alias tidak punya apa-apa.
    if prefiks and keluarga:
        disaring = {k for k in keluarga if any(k.startswith(p) for p in prefiks)}
        keluarga = disaring or keluarga

    return {
        "teks": teks,
        "token": token,
        "token_inti": [t for t in token if lex.kelas(t) != "promo"],
        "keluarga": keluarga,
        "prefiks_model": prefiks,
        "irisan_tegas": bool(langsung or prefiks
                             or (dari_alias and set.intersection(*dari_alias))),
        "model_persis": {t for t in token if lex.kelas(t) == "model"},
    }


# ─── Skor ─────────────────────────────────────────────────────────────────────

def _peta_spek(tokens):
    """Token → {dimensi: {nilai, ...}}, mis. {'gb': {'8'}, 'ddr': {'4'}}."""
    peta = defaultdict(set)
    for t in tokens:
        pas = LX.spek_pasangan(t)
        if pas:
            peta[pas[0]].add(pas[1])
    return peta


def spek_bentrok(kueri_token, produk_token) -> bool:
    """True bila kueri dan produk menyebut dimensi sama dengan nilai berbeda.

    Ini saringan keras kedua, sejajar dengan saringan keluarga model. Bobot saja
    tidak cukup: pada kueri "ram ddr4 8gb", produk "RAM DDR4 16GB" masih ikut
    terjaring karena `ram` dan `ddr4` sudah cukup melewati ambang — padahal 8GB
    dan 16GB berbeda harga hampir dua kali lipat, dan mencampurnya merusak
    seluruh statistik harga sesudahnya.
    """
    q = _peta_spek(kueri_token)
    if not q:
        return False
    p = _peta_spek(produk_token)
    for dimensi, nilai_q in q.items():
        nilai_p = p.get(dimensi)
        if nilai_p and not (nilai_p & nilai_q):
            return True
    return False


def _bobot_token(t, lex):
    return BOBOT.get(lex.kelas(t), 1.0) * lex.idf(t)


def skor(kueri_token, produk_token, lex=None, fam_produk=None,
         bukti_min=2) -> float:
    """Seberapa cocok satu produk dengan kueri. 0..1, berbasis cakupan kueri.

    `fam_produk` = keluarga model yang dimiliki produk. Token kueri dihitung
    "kena" bila cocok LITERAL **atau** bila alias yang dipelajari mengarah ke
    keluarga model yang sama.

    Tanpa jalur alias itu, "intel 7 gen 12" hanya cocok ke toko yang kebetulan
    menuliskan "Gen 12" di judul. Toko yang menulis "Intel Core i7 12700K BOX
    Garansi Resmi" — produk yang sama persis — akan tersaring keluar hanya karena
    gaya penulisannya berbeda. Padahal justru itu masalah yang modul ini ada
    untuk memecahkannya.
    """
    lex = lex or LX.Lexicon()
    qt = [t for t in dict.fromkeys(kueri_token)]
    if not qt:
        return 0.0
    pt = set(produk_token)
    fam_produk = fam_produk or set()
    total = sum(_bobot_token(t, lex) for t in qt)
    if total <= 0:
        return 0.0

    def kena(t):
        if t in pt:
            return True
        if not fam_produk:
            return False
        # "gen 12" dianggap terpenuhi oleh nomor model yang diawali 12 — 12700K
        # ADALAH generasi 12, meski judulnya tidak pernah menulis kata itu.
        # Tanpa kredit ini, `gen12` (langka, jadi berbobot paling besar) selalu
        # tidak tercakup dan menyeret setiap kandidat ke bawah ambang.
        m = re.match(r"^gen(\d{1,2})$", t)
        if m:
            return any(f.startswith(m.group(1)) for f in fam_produk)
        fam_t = lex.keluarga_dari_alias(t, bukti_min=bukti_min)
        return bool(fam_t and (fam_t & fam_produk))

    # Token kueri yang TIDAK ADA di korpus sama sekali dikeluarkan dari penyebut.
    #
    # Ini bukan kelonggaran, ini perbaikan: idf memberi bobot TERTINGGI kepada
    # token paling langka, jadi token yang tidak dimiliki satu produk pun justru
    # mendominasi penyebut dan menekan skor semua kandidat di bawah ambang.
    # Nyata terjadi pada "intel 7 gen 12": tidak satu pun judul menulis "Gen 12"
    # (mereka menulis "Alder Lake" atau "LGA 1700"), dan token itu sendirian
    # membuat 23 produk yang benar-benar cocok gagal semua.
    ada = [t for t in qt if lex.df.get(t, 0) > 0 or kena(t)]
    if not ada:
        return 0.0

    # Membuang token yang tidak dikenal korpus bisa berbalik menjadi bencana
    # kalau yang TERSISA cuma remah. Kueri "kulkas 2 pintu" di korpus komputer
    # menyisakan token "2" — dan setiap produk yang memuat angka 2 mendadak
    # cocok sempurna. Jadi wajib ada minimal satu token BERISI yang tersisa;
    # angka telanjang dan kata dua huruf tidak memenuhi syarat.
    def berisi(t):
        return (lex.kelas(t) in ("model", "spek", "merek")
                or (t.isalpha() and len(t) >= 3))

    if not any(berisi(t) for t in ada):
        return 0.0

    total_ada = sum(_bobot_token(t, lex) for t in ada)
    if total_ada <= 0:
        return 0.0
    return sum(_bobot_token(t, lex) for t in ada if kena(t)) / total_ada


def skor_judul(a, b, lex=None) -> float:
    """Kemiripan dua judul produk — dipakai saat tidak ada kode model sama sekali.

    Penalti angka di akhir itu yang penting: "RAM DDR4 8GB" vs "RAM DDR4 16GB"
    punya SequenceMatcher ~0.94 — nyaris identik menurut fuzzy string, padahal
    produk berbeda dengan harga dua kali lipat. Di elektronik, justru angkanya
    yang membedakan.
    """
    lex = lex or LX.Lexicon()
    ta, tb = LX.normalisasi(a), LX.normalisasi(b)
    sa = {t for t in ta if lex.kelas(t) != "promo"}
    sb = {t for t in tb if lex.kelas(t) != "promo"}
    if not sa or not sb:
        return 0.0

    jac = len(sa & sb) / len(sa | sb)
    seq = difflib.SequenceMatcher(None, " ".join(sorted(sa)),
                                  " ".join(sorted(sb))).ratio()
    na = {t for t in sa if any(c.isdigit() for c in t)}
    nb = {t for t in sb if any(c.isdigit() for c in t)}
    jac_angka = len(na & nb) / len(na | nb) if (na | nb) else 0.0

    nilai = 0.45 * jac + 0.35 * seq + 0.20 * jac_angka
    if na and nb and not (na & nb):
        nilai *= 0.55
    return round(nilai, 4)


# ─── Pencarian ────────────────────────────────────────────────────────────────

def cari(kueri, produk, lex=None, batas=200, ambang=AMBANG_KANDIDAT) -> list:
    """Cari produk yang cocok dengan kueri. Return baris + skor, terurut.

    `produk` = daftar dict dari `db.mp_products_semua()`.
    """
    lex = lex or LX.Lexicon()
    q = urai_kueri(kueri, lex) if isinstance(kueri, str) else kueri
    qt = q["token_inti"] or q["token"]
    hasil = []

    for p in produk or []:
        nama = p.get("nama_produk") or ""
        pt = LX.normalisasi(nama)
        if not pt:
            continue

        model_produk = {t for t in pt if lex.kelas(t) == "model"}
        fam_produk = {LX.keluarga(t) for t in model_produk} - {None}

        # SARINGAN KERAS. Kalau kueri jelas menunjuk keluarga tertentu dan produk
        # ini punya kode model dari keluarga lain, ia bukan barang yang dicari —
        # sedekat apa pun judulnya. Tanpa ini `12700K` dan `10700K` bersaing
        # ketat menurut fuzzy string, padahal beda dua generasi.
        if q["keluarga"] and fam_produk and not (fam_produk & q["keluarga"]):
            continue
        # Generasi: "gen 12" hanya cocok ke nomor model yang diawali 12.
        pref = q.get("prefiks_model") or set()
        if pref and fam_produk and not any(
                f.startswith(p) for f in fam_produk for p in pref):
            continue
        # Kueri menyebut kode model persis tapi produk memuat kode model lain.
        if q["model_persis"] and model_produk and not (model_produk & q["model_persis"]):
            continue
        # Spesifikasi bentrok: 8GB vs 16GB, DDR4 vs DDR5, Gen 12 vs Gen 11.
        if spek_bentrok(q["token"], pt):
            continue

        # Merek yang disebut kueri WAJIB ada di produk.
        #
        # Bentuk "tolak kalau mereknya bertentangan" tidak cukup: kueri
        # "ssd sandisk 1tb" tetap menjaring "SSD HIKSEMI Wave S 1TB" dan
        # "SSD VENOMRX M.2 1TB" karena merek-merek itu tidak ada di daftar benih,
        # jadi tidak terbaca sebagai merek dan tidak pernah dianggap bertentangan.
        # Daftar merek tidak akan pernah lengkap — tapi kalau user repot-repot
        # mengetik "sandisk", ia memang sedang meminta Sandisk.
        merek_q = {t for t in q["token"] if lex.kelas(t) == "merek"}
        if merek_q and not (merek_q & set(pt)):
            continue

        s = skor(qt, pt, lex, fam_produk=fam_produk)
        if s < ambang:
            continue
        baris = dict(p)
        baris["_skor"] = round(s, 4)
        baris["_token"] = pt
        baris["_model"] = sorted(model_produk)
        hasil.append(baris)

    hasil.sort(key=lambda r: (-r["_skor"], r.get("harga") or 0))
    return hasil[:batas]


# ─── Pengelompokan varian ─────────────────────────────────────────────────────

_SERI_LABEL = re.compile(r"^(i\d|ryzen\d|rtx|gtx|rx|ddr\d|gen\d+)$")


def _label_varian(baris_list, lex):
    """Nama varian yang bisa dibaca manusia, mis. 'i7 12700K'."""
    model = Counter()
    seri = Counter()
    merek = Counter()
    for b in baris_list:
        for t in b.get("_token", []):
            if lex.kelas(t) == "model":
                model[t] += 1
            elif _SERI_LABEL.match(t):
                seri[t] += 1
            elif lex.kelas(t) == "merek":
                merek[t] += 1
    bagian = []
    if merek:
        bagian.append(merek.most_common(1)[0][0].title())
    if seri:
        bagian.append(seri.most_common(1)[0][0])
    # Nama lini produk (NYTRIX / LUMINEX / BLAZEVIEW) — tanpa ini tiga pendingin
    # yang berbeda tampil dengan label identik "Cooler 240" dan user tidak punya
    # cara membedakan mana yang sedang dianalisis.
    lini = Counter()
    for b in baris_list:
        for t in _token_langka(b, lex):
            lini[t] += 1
    # Hanya berguna sebagai pembeda kalau grupnya memang berisi lebih dari satu
    # baris; pada grup tunggal ia cuma menempelkan kata acak dari judul
    # ("Intel i7 THREADS 12700KF").
    umum_di_semua = ([t for t, n in lini.items() if n == len(baris_list)]
                     if len(baris_list) > 1 else [])
    if umum_di_semua:
        pilih = max(umum_di_semua, key=lambda t: lex.idf(t))
        if pilih.title() not in bagian:
            bagian.append(pilih.upper())
    if model:
        bagian.append(model.most_common(1)[0][0].upper())
    if bagian:
        label = " ".join(bagian)
        # Beberapa kode model dari keluarga berbeda dalam satu judul = bundling
        # ("i7 12700K + RTX 3060 Ti"). Tanpa penanda ini ia tampil dengan label
        # yang sama persis seperti CPU satuan dan user tidak bisa membedakannya.
        keluarga_unik = {LX.keluarga(t) for t in model} - {None}
        if len(keluarga_unik) > 1:
            label += " (paket/bundling)"
        return label
    nama = (baris_list[0].get("nama_produk") or "?")
    return nama[:48]


SEBAR_PISAH = 1.6      # rasio harga maks/min yang memicu pemisahan lini produk


def _token_langka(baris, lex, batas_df=0.12):
    """Token alfabetis yang jarang muncul di korpus — biasanya NAMA LINI PRODUK.

    'cube', 'gaming', 'aio', 'cooler' ada di mana-mana; 'nytrix', 'luminex',
    'blazeview' tidak. Yang langka itulah yang membedakan barang.
    """
    n = max(lex.n_dok, 1)
    return {t for t in baris.get("_token", [])
            if t.isalpha() and len(t) >= 4
            and lex.kelas(t) in ("umum", "merek")
            and lex.df.get(t, 0) / n <= batas_df}


def _pisah_lini_produk(kunci, baris, lex):
    """Pisahkan satu grup kode-model jadi beberapa lini produk bila perlu.

    Kenapa ada: angka pada sebagian produk bukan nomor model melainkan UKURAN.
    "CUBE GAMING NYTRIX 240", "LUMINEX 240", dan "BLAZEVIEW 240" semuanya
    membawa token `240` — radiator 240 mm — jadi ketiganya masuk satu grup dan
    rentang harganya melebar dari Rp 615.000 sampai Rp 1.600.000. Median dari
    campuran itu tidak menggambarkan produk mana pun.

    Pemisahan hanya dijalankan bila harga di dalam grup memang merenggang
    (`SEBAR_PISAH`). Untuk grup yang harganya sudah rapat — kode model asli
    seperti `12700K` — tidak ada yang disentuh, karena di situ token langka
    seperti 'box' atau 'tray' cuma varian kemasan, bukan produk berbeda.
    """
    # Rentang diukur SETELAH pencilan dipangkas. Kalau tidak, satu botol thermal
    # paste Rp 35.000 di antara CPU Rp 4,8 juta membuat rasionya 137× dan
    # memicu pemisahan pada grup yang sebenarnya sehat — lalu CPU yang sama
    # terpecah jadi 'PROSESOR', 'TRAY', dan 'BOX' hanya karena beda kemasan.
    harga = [int(b.get("harga") or 0) for b in baris if (b.get("harga") or 0) > 0]
    dipakai, _ = mp_common.pangkas_pencilan(harga)
    if len(baris) < 3 or not dipakai or min(dipakai) <= 0:
        return [{"kunci": kunci, "baris": baris}]
    if max(dipakai) / min(dipakai) < SEBAR_PISAH:
        return [{"kunci": kunci, "baris": baris}]

    per_lini = defaultdict(list)
    tanpa = []
    for b in baris:
        langka = _token_langka(b, lex)
        if langka:
            # Satu token terkuat saja, bukan seluruh himpunan: memakai semuanya
            # akan memisahkan "NYTRIX BLACK" dari "NYTRIX WHITE" — warna berbeda,
            # produk dan harga sama.
            per_lini[max(langka, key=lambda t: lex.idf(t))].append(b)
        else:
            tanpa.append(b)

    if len(per_lini) < 2:
        return [{"kunci": kunci, "baris": baris}]

    keluar = [{"kunci": f"{kunci}#{lini}", "baris": v} for lini, v in per_lini.items()]
    if tanpa:
        keluar.append({"kunci": f"{kunci}#lain", "baris": tanpa})
    return keluar


def kelompokkan(hasil, lex=None, ambang_gabung=AMBANG_SARAN) -> list:
    """Kelompokkan hasil `cari` per varian produk.

    Produk dengan kode model dikelompokkan berdasarkan kode itu — paling andal,
    dan tetap hidup walau kompetitor mengganti judulnya. Produk tanpa kode model
    jatuh ke pengelompokan berbasis kemiripan judul.

    Return daftar grup terurut dari yang paling banyak tokonya.
    """
    lex = lex or LX.Lexicon()
    per_model = defaultdict(list)
    tanpa_model = []

    for b in hasil or []:
        if b.get("_model"):
            # Kunci = seluruh kode model di judul. `12700k` dan `12700kf` adalah
            # kunci berbeda, jadi keduanya tidak pernah tercampur.
            per_model["|".join(sorted(b["_model"]))].append(b)
        else:
            tanpa_model.append(b)

    grup = []
    for k, v in per_model.items():
        grup.extend(_pisah_lini_produk(k, v, lex))

    # Produk tanpa kode model: gabung yang judulnya cukup mirip.
    sisa = list(tanpa_model)
    while sisa:
        inti = sisa.pop(0)
        anggota = [inti]
        lain = []
        for b in sisa:
            if skor_judul(inti.get("nama_produk", ""),
                          b.get("nama_produk", ""), lex) >= ambang_gabung:
                anggota.append(b)
            else:
                lain.append(b)
        sisa = lain
        grup.append({"kunci": f"judul:{(inti.get('nama_produk') or '')[:40]}",
                     "baris": anggota})

    keluar = []
    for g in grup:
        baris = g["baris"]
        harga = [int(b.get("harga") or 0) for b in baris if (b.get("harga") or 0) > 0]
        # Aksesori yang menyebut nama produk ("Thermal Paste untuk i7 12700K")
        # membawa kode model yang sama, jadi ia masuk grup ini dan membuat
        # `harga_min` jadi Rp 35.000. Dipangkas di sini juga, bukan cuma di
        # mp_harga, supaya rentang yang dilihat user saat memilih varian jujur.
        dipakai, dibuang = mp_common.pangkas_pencilan(harga)
        sisa = set(dibuang)
        baris_bersih = [b for b in baris
                        if int(b.get("harga") or 0) not in sisa or not b.get("harga")]
        toko = {b.get("store_id") for b in baris_bersih}
        keluar.append({
            "kunci": g["kunci"],
            "label": _label_varian(baris_bersih or baris, lex),
            "n_produk": len(baris_bersih),
            "n_toko": len(toko),
            "harga_min": min(dipakai) if dipakai else 0,
            "harga_maks": max(dipakai) if dipakai else 0,
            "n_pencilan": len(dibuang),
            "skor_maks": max((b.get("_skor") or 0) for b in baris),
            "punya_sendiri": any(b.get("is_own") for b in baris_bersih),
            "baris": baris_bersih or baris,
        })

    # Urut: yang paling banyak toko pembandingnya dulu — itu yang statistik
    # harganya paling bisa dipercaya.
    keluar.sort(key=lambda g: (-g["n_toko"], -g["skor_maks"]))
    return keluar


def cari_dan_kelompokkan(kueri, produk, lex=None, batas=200):
    lex = lex or LX.Lexicon()
    return kelompokkan(cari(kueri, produk, lex, batas=batas), lex)
