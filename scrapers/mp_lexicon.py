"""
mp_lexicon.py — belajar cara toko menulis judul produk.

Kenapa modul ini ada: judul produk marketplace Indonesia ditulis semaunya.
Produk yang sama muncul sebagai

    "PROCESSOR INTEL CORE I7 12700K BOX GARANSI RESMI"
    "Intel i7-12700K Tray + Fan Ready Stock COD"
    "🔥PROMO🔥 Prosesor Intel Core i7 12700K Gen 12 LGA1700 Original"

Mencocokkan kueri longgar seperti **"intel 7 gen 12"** ke ketiganya tidak bisa
dilakukan dengan daftar sinonim yang ditulis tangan — daftar itu langsung basi
begitu Intel merilis generasi berikutnya. Jadi modul ini **memanen aturannya dari
judul yang sudah dipanen sendiri**: token mana yang cuma bumbu promosi, dan token
mana yang benar-benar menunjuk produk tertentu.

Semuanya deterministik (`re`, `Counter`, `statistics`) — tidak butuh API key, dan
hasilnya bisa diperiksa manusia.
"""
import math
import re
import statistics
import unicodedata
from collections import Counter, defaultdict

# ─── Normalisasi ──────────────────────────────────────────────────────────────

_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿⬀-⯿️←-⇿]"
)

# Semua ini jadi spasi. Tanda hubung ikut dipisah supaya `i7-12700k` pecah jadi
# `i7` + `12700k` — dua token yang masing-masing berguna, sedangkan bentuk
# gabungannya tidak pernah cocok dengan toko yang menulisnya tanpa tanda hubung.
_PISAH = re.compile(r"[|/,\-–—()\[\]{}+&:;!?\"'`~*_\\.<>@#$%^=]")

_SATUAN = r"gb|tb|mb|kb|ghz|mhz|khz|hz|w|watt|va|mm|cm|inch|inci|bit|pin|rpm|ml|kg|gr"

# Kata seri yang biasa dipisah spasi oleh sebagian toko dan dirapatkan oleh yang
# lain. Kalau tidak diseragamkan, dua toko yang menjual barang sama tidak pernah
# berbagi satu token pun di bagian yang justru paling menentukan.
#
# `rtx`/`gtx`/`rx` SENGAJA tidak ada di sini: pada GPU, angkanya sendiri yang
# jadi nomor model (`3060`), jadi merapatkannya jadi `rtx3060` malah memutus
# kecocokan dengan toko yang menulis "GeForce 3060". Biarkan angkanya berdiri
# sendiri sebagai token model.
_SERI = r"ddr|pcie|pci|usb|sata|nvme|gen|generasi|ryzen|lga|cat|pc"

# Akhiran varian yang sering dipisah spasi: "3060 Ti" → "3060ti", "5800 X3D".
# Ini pembeda produk, bukan hiasan — 3060 dan 3060 Ti beda harga jutaan.
_VARIAN_RE = re.compile(r"\b(\d{3,5})\s+(ti|super|xt|x3d|ko|kf|kс)\b")

_PROMO_BENIH = {
    "ready", "readystock", "ori", "original", "promo", "murah", "termurah",
    "cod", "garansi", "resmi", "bergaransi", "gratis", "ongkir", "terlaris",
    "bnib", "grosir", "diskon", "sale", "best", "seller", "free", "bonus",
    "limited", "stock", "stok", "siap", "kirim", "kualitas", "terjamin",
    "dijamin", "asli", "terbaik", "berkualitas", "official", "store", "shop",
    "toko", "jual", "baru", "new", "hot", "recommended", "terpercaya",
    "lengkap", "komplit", "amanah", "fast", "respon", "bisa", "dan", "untuk",
    "dengan", "yang", "atau", "the", "of", "for",
}

_MEREK_BENIH = {
    "intel", "amd", "nvidia", "asus", "msi", "gigabyte", "asrock", "corsair",
    "kingston", "samsung", "seagate", "logitech", "razer", "steelseries",
    "hyperx", "adata", "team", "vgen", "sandisk", "crucial", "wd", "western",
    "digital", "cooler", "master", "deepcool", "noctua", "thermaltake", "aigo",
    "xiaomi", "lenovo", "hp", "dell", "acer", "apple", "sapphire", "powercolor",
    "zotac", "palit", "galax", "inno3d", "colorful", "biostar", "rexus",
    "fantech", "armaggeddon", "digital alliance", "venom", "pny", "patriot",
    # Ditambahkan setelah melihat katalog nyata ketiga toko, 14 Agustus.
    "hiksemi", "venomrx", "transcend", "lexar", "rapoo", "tecgear", "edifier",
    "cube", "canon", "epson", "brother", "tplink", "totolink", "gigabyte",
    "nzxt", "lian", "antec", "seasonic", "silverstone", "montech", "infinity",
}

_SATUAN_RE = re.compile(rf"\b(\d+)\s+({_SATUAN})\b")
_SERI_RE = re.compile(rf"\b({_SERI})\s+(\d+)\b")
_CORE_I = re.compile(r"\bcore\s+i\s*(\d)\b")
_I_SPASI = re.compile(r"\bi\s+(\d)\b")
# "intel 7" itu cara user mengetik, bukan cara toko menulis. Tanpa aturan ini
# kueri "intel 7 gen 12" tidak pernah berbagi token `i7` dengan judul mana pun,
# dan pencocokan bergantung sepenuhnya pada saringan keluarga model.
_MEREK_SERI = re.compile(r"\b(intel|core)\s+(\d)\b")
_TH_GEN = re.compile(r"\b(\d{1,2})\s*(?:th|nd|rd|st)\s+gen\w*\b")
_GEN_TH = re.compile(r"\bgen\w*\s+(\d{1,2})\s*(?:th|nd|rd|st)\b")


def normalisasi(judul: str) -> list:
    """Judul mentah → daftar token yang sudah diseragamkan.

    >>> normalisasi("PROMO! Intel Core i7-12700K Gen 12 DDR 4 8 GB")
    ['promo', 'intel', 'i7', '12700k', 'gen12', 'ddr4', '8gb']
    """
    t = unicodedata.normalize("NFKD", str(judul or "")).lower()
    t = _EMOJI.sub(" ", t)
    t = _PISAH.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()

    # Rapatkan SEBELUM dipecah: "12 th gen" dan "gen 12" harus bertemu di satu
    # bentuk, kalau tidak dua toko yang menulis generasi dengan gaya berbeda
    # tidak pernah dianggap membicarakan barang yang sama.
    t = _TH_GEN.sub(r"gen\1", t)
    t = _GEN_TH.sub(r"gen\1", t)
    t = _MEREK_SERI.sub(r"\1 i\2", t)
    t = _CORE_I.sub(r"i\1", t)
    t = _I_SPASI.sub(r"i\1", t)
    for _ in range(2):  # dua lintasan: "ddr 4 8 gb" butuh giliran kedua
        t = _SATUAN_RE.sub(r"\1\2", t)
        t = _SERI_RE.sub(r"\1\2", t)
    t = _VARIAN_RE.sub(r"\1\2", t)
    t = re.sub(r"\bgenerasi(\d+)\b", r"gen\1", t)

    token = []
    for kata in t.split():
        kata = kata.strip()
        if not kata or len(kata) > 24:
            continue
        # Buang token yang murni tanda baca sisa atau satu huruf tak berarti.
        if len(kata) == 1 and not kata.isdigit():
            continue
        token.append(kata)
    return token


# ─── Kelas token ──────────────────────────────────────────────────────────────

_RE_MODEL = re.compile(r"^[a-z]{0,3}\d{3,5}[a-z]{0,4}$")
_RE_SPEK = re.compile(rf"^\d+({_SATUAN})$")
_RE_SERI_SPEK = re.compile(rf"^({_SERI})\d+[a-z]*$")
_RE_TAHUN = re.compile(r"^(19|20)\d{2}$")


def kelas(token: str, promo=None, merek=None) -> str:
    """model | spek | merek | promo | umum.

    Pembedaan ini yang membuat pencocokan bekerja: di elektronik, **kode model
    adalah pembedanya**, sedangkan kata promosi tidak membedakan apa pun.
    """
    promo = promo if promo is not None else _PROMO_BENIH
    merek = merek if merek is not None else _MEREK_BENIH
    if token in promo:
        return "promo"
    if token in merek:
        return "merek"
    if _RE_SPEK.match(token) or _RE_SERI_SPEK.match(token) or _RE_SPEK_ISERI.match(token):
        return "spek"
    if _RE_TAHUN.match(token):
        return "umum"          # 2024 itu tahun, bukan nomor model
    if _RE_MODEL.match(token) and any(c.isdigit() for c in token):
        return "model"
    return "umum"


_RE_SPEK_NILAI = re.compile(rf"^(\d+)({_SATUAN})$")
_RE_SPEK_SERI = re.compile(rf"^({_SERI})(\d+)$")
# Seri Intel Core: i3/i5/i7/i9. Diperlakukan sebagai dimensi spesifikasi supaya
# kueri i7 tidak pernah cocok ke produk i5 — pembeda yang sama pentingnya dengan
# 8GB vs 16GB, dan sama-sama berselisih harga jutaan.
_RE_SPEK_ISERI = re.compile(r"^i([3579])$")


def spek_pasangan(token: str):
    """Token spek → (dimensi, nilai). `8gb`→`('gb','8')`, `ddr4`→`('ddr','4')`,
    `i7`→`('iseri','7')`.

    Dipakai untuk menolak produk yang spesifikasinya bentrok dengan kueri: kalau
    user mencari 8GB, RAM 16GB bukan "kandidat yang agak kurang cocok" — ia
    barang lain dengan harga lain.
    """
    m = _RE_SPEK_NILAI.match(token or "")
    if m:
        satuan, nilai = m.group(2), m.group(1)
        # Samakan skala kapasitas supaya bisa dibandingkan: tanpa ini `1tb` dan
        # `500gb` terlihat sebagai DIMENSI berbeda, tidak pernah dianggap
        # bentrok, dan SSD 500GB ikut terjaring pada kueri 1TB.
        if satuan == "tb":
            return "gb", str(int(nilai) * 1000)
        if satuan == "mb":
            return "gb", str(max(int(nilai) // 1000, 0))
        return satuan, nilai
    m = _RE_SPEK_SERI.match(token or "")
    if m:
        return m.group(1), m.group(2)
    m = _RE_SPEK_ISERI.match(token or "")
    if m:
        return "iseri", m.group(1)
    return None


def keluarga(token: str):
    """Token model → keluarga produknya. `12700k` → `127xx`, `3060ti` → `306x`.

    Keluarga inilah yang menjembatani kueri longgar ke produk: user mengetik
    "gen 12", toko menulis "12700K", dan keduanya bertemu di `127xx`.
    """
    m = re.search(r"\d{3,5}", token or "")
    if not m:
        return None
    d = m.group(0)
    if len(d) >= 5:
        return d[:3] + "xx"
    if len(d) == 4:
        return d[:3] + "x"
    return d[:2] + "x"


# ─── Lexicon ──────────────────────────────────────────────────────────────────

class Lexicon:
    """Apa yang dipelajari dari korpus judul.

    `promo` — token yang ternyata cuma bumbu, ditemukan sendiri dari data.
    `alias` — token ber-angka → keluarga model yang sering menemaninya.
    `idf`   — token langka lebih informatif daripada token yang ada di mana-mana.
    """

    def __init__(self):
        self.df = Counter()
        self.n_dok = 0
        self.promo = set(_PROMO_BENIH)
        self.merek = set(_MEREK_BENIH)
        self.alias = defaultdict(Counter)
        self.model_df = Counter()

    def idf(self, token: str) -> float:
        return math.log(1 + (self.n_dok + 1) / (self.df.get(token, 0) + 1))

    def kelas(self, token: str) -> str:
        return kelas(token, self.promo, self.merek)

    def keluarga_dari_alias(self, token: str, bukti_min=3) -> set:
        """Keluarga model yang dipelajari untuk token ini."""
        return {k for k, n in self.alias.get(token, {}).items() if n >= bukti_min}

    def ringkas(self) -> dict:
        return {
            "n_judul": self.n_dok,
            "n_token": len(self.df),
            "promo_dipelajari": sorted(self.promo - _PROMO_BENIH),
            "alias_teratas": {
                t: dict(c.most_common(4))
                for t, c in sorted(self.alias.items(),
                                   key=lambda kv: -sum(kv[1].values()))[:12]
            },
        }


def bangun(judul_per_toko: dict, ambang_promo=0.35, min_toko_promo=2) -> Lexicon:
    """Bangun lexicon dari `{store_id: [judul, ...]}`.

    Dua hal yang dipelajari:

    1. **Stopword promo, ditemukan sendiri.** Token yang muncul di lebih dari
       `ambang_promo` judul pada minimal `min_toko_promo` toko, dan bukan kelas
       model/spek, hampir pasti bumbu. Toko yang menulis "GARANSI RESMI" di
       setiap judul dengan sendirinya membuat frasa itu kehilangan bobot — tanpa
       saya perlu menebak daftar kata promosi yang berlaku di niche ini.

    2. **Alias dari ko-okurensi.** Judul yang memuat `gen12` DAN `12700k`
       mengajarkan `gen12 → 127xx`. Judul lain mengajarkan `i7 → 127xx` dan
       `i7 → 117xx`. Irisan keduanya nanti yang menjawab "intel 7 gen 12".
       Hanya token ber-angka yang dicatat: `intel` menemani semua kode model
       Intel, jadi ia tidak mempersempit apa pun dan cuma jadi derau.
    """
    lex = Lexicon()
    df_per_toko = defaultdict(Counter)
    judul_token = []

    for store_id, judul_list in (judul_per_toko or {}).items():
        for j in judul_list:
            tok = normalisasi(j)
            if not tok:
                continue
            judul_token.append(tok)
            unik = set(tok)
            lex.df.update(unik)
            lex.n_dok += 1
            df_per_toko[store_id].update(unik)

    if not lex.n_dok:
        return lex

    # 1) Promo yang ditemukan sendiri.
    #
    # Dua penjaga, dan keduanya berasal dari kesalahan nyata saat modul ini diuji:
    #   • Token BER-ANGKA tidak pernah boleh diturunkan. `i7` muncul di 40% judul
    #     korpus CPU dan nyaris ikut dibuang sebagai bumbu — padahal ia justru
    #     token paling menentukan dalam kueri "intel 7 gen 12". Angka di dalam
    #     judul marketplace hampir selalu spesifikasi, bukan hiasan.
    #   • Merek benih dilindungi: "samsung" di toko yang khusus jual Samsung
    #     memang muncul di mana-mana, tapi ia tetap identitas produk.
    n_toko_punya = Counter()
    for store_id, c in df_per_toko.items():
        total = max(len(judul_per_toko.get(store_id) or []), 1)
        for t, n in c.items():
            if n / total >= ambang_promo:
                n_toko_punya[t] += 1
    for t, n in n_toko_punya.items():
        if n < min_toko_promo:
            continue
        if any(ch.isdigit() for ch in t):
            continue
        if t in _MEREK_BENIH:
            continue
        if kelas(t) in ("model", "spek"):
            continue
        lex.promo.add(t)

    # 2) Alias dari ko-okurensi dalam satu judul
    for tok in judul_token:
        keluarga_di_judul = set()
        for t in tok:
            if kelas(t, lex.promo, lex.merek) == "model":
                lex.model_df[t] += 1
                k = keluarga(t)
                if k:
                    keluarga_di_judul.add(k)
        if not keluarga_di_judul:
            continue
        for t in set(tok):
            if not any(c.isdigit() for c in t):
                continue
            if kelas(t, lex.promo, lex.merek) == "model":
                continue
            for k in keluarga_di_judul:
                lex.alias[t][k] += 1

    return lex


def bangun_dari_db(store_ids=None):
    """Lexicon dari judul yang sudah tersimpan di mp_products."""
    import db
    judul = defaultdict(list)
    for p in db.mp_products_semua(store_ids=store_ids):
        if p.get("nama_produk"):
            judul[p.get("store_id") or "?"].append(p["nama_produk"])
    return bangun(judul)


# ─── Gaya penulisan ───────────────────────────────────────────────────────────

def _posisi(tok, target):
    try:
        i = tok.index(target)
    except ValueError:
        return None
    if i < len(tok) / 3:
        return "awal"
    return "tengah" if i < 2 * len(tok) / 3 else "akhir"


def ringkas_gaya(judul_list, terjual_list=None, lex=None) -> dict:
    """Statistik gaya penulisan satu toko.

    Bagian yang paling berguna bukan "frasa apa yang paling sering dipakai" tapi
    **frasa apa yang dipakai produk yang laku** — pola yang umum belum tentu pola
    yang bekerja, dan menyalin pola yang umum saja berarti menyalin kebiasaan,
    bukan strategi.
    """
    judul_list = [j for j in (judul_list or []) if j]
    if not judul_list:
        return {"n": 0}

    lex = lex or bangun({"x": judul_list})
    terjual_list = list(terjual_list or [])
    panjang, jumlah_kata, caps, emoji = [], [], 0, 0
    sep = Counter()
    frasa_pos = defaultdict(Counter)
    ngram = Counter()
    token_per_judul = []

    for j in judul_list:
        panjang.append(len(j))
        kata = str(j).split()
        jumlah_kata.append(len(kata))
        besar = [k for k in kata if len(k) > 2 and k.isupper()]
        if kata and len(besar) / len(kata) >= 0.5:
            caps += 1
        if _EMOJI.search(str(j)):
            emoji += 1
        for s in ("|", "-", ",", "/", "–"):
            if s in str(j):
                sep[s] += 1
        tok = normalisasi(j)
        token_per_judul.append(tok)
        for t in set(tok):
            if t in lex.promo:
                p = _posisi(tok, t)
                if p:
                    frasa_pos[t][p] += 1
        for n in (2, 3):
            for i in range(len(tok) - n + 1):
                potongan = tok[i:i + n]
                if all(x in lex.promo for x in potongan):
                    ngram[" ".join(potongan)] += 1

    hasil = {
        "n": len(judul_list),
        "panjang_judul_median": int(statistics.median(panjang)),
        "jumlah_kata_median": int(statistics.median(jumlah_kata)),
        "persen_allcaps": round(caps / len(judul_list) * 100),
        "persen_emoji": round(emoji / len(judul_list) * 100),
        "separator_dominan": sep.most_common(1)[0][0] if sep else "(spasi)",
        "frasa_promo": [
            {"frasa": t, "dipakai": sum(c.values()),
             "posisi": c.most_common(1)[0][0]}
            for t, c in sorted(frasa_pos.items(),
                               key=lambda kv: -sum(kv[1].values()))[:10]
        ],
        "ngram_promo": ngram.most_common(6),
    }

    # Korelasi dengan penjualan — hanya bila datanya ada dan sepadan.
    if terjual_list and len(terjual_list) == len(judul_list):
        angka = [int(t or 0) for t in terjual_list]
        med = statistics.median(angka) if angka else 0
        pengaruh = []
        for t in list(frasa_pos)[:20]:
            dengan = [angka[i] for i, tok in enumerate(token_per_judul) if t in tok]
            tanpa = [angka[i] for i, tok in enumerate(token_per_judul) if t not in tok]
            if len(dengan) >= 3 and len(tanpa) >= 3:
                pengaruh.append({
                    "frasa": t,
                    "terjual_dengan": int(statistics.median(dengan)),
                    "terjual_tanpa": int(statistics.median(tanpa)),
                })
        pengaruh.sort(key=lambda d: -(d["terjual_dengan"] - d["terjual_tanpa"]))
        hasil["terjual_median"] = int(med)
        hasil["frasa_vs_terjual"] = pengaruh[:6]

    return hasil


def kalimat_gaya(gaya: dict) -> list:
    """Statistik → kalimat Indonesia. Berguna tanpa API key sama sekali."""
    if not gaya or not gaya.get("n"):
        return ["Belum ada judul untuk dianalisis."]
    b = [
        f"Dari {gaya['n']} judul: panjang khas {gaya['panjang_judul_median']} karakter "
        f"({gaya['jumlah_kata_median']} kata), pemisah dominan "
        f"'{gaya['separator_dominan']}'.",
    ]
    if gaya["persen_allcaps"] >= 25:
        b.append(f"{gaya['persen_allcaps']}% judul ditulis KAPITAL — gaya toko ini "
                 f"memang berteriak.")
    if gaya["persen_emoji"] >= 15:
        b.append(f"{gaya['persen_emoji']}% judul memakai emoji.")
    if gaya.get("frasa_promo"):
        atas = ", ".join(f"{f['frasa']} ({f['posisi']})" for f in gaya["frasa_promo"][:5])
        b.append(f"Kata bumbu yang paling sering: {atas}.")
    for f in (gaya.get("frasa_vs_terjual") or [])[:2]:
        selisih = f["terjual_dengan"] - f["terjual_tanpa"]
        if selisih > 0:
            b.append(f"Judul yang memuat '{f['frasa']}' terjual median "
                     f"{f['terjual_dengan']} vs {f['terjual_tanpa']} yang tidak "
                     f"— selisih {selisih}.")
    return b
