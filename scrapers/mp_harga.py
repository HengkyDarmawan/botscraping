"""
mp_harga.py — "harga saya baiknya pasang berapa?"

Menggantikan `_build_comparison` lama yang merata-ratakan SELURUH hasil scrape
tanpa peduli produknya apa, lalu menyarankan `rata2 × 0.95` untuk semuanya.
Rumus itu buta produk, buta modal, dan buta potongan marketplace — ia bisa
menyarankan harga di bawah modal tanpa satu pun tanda peringatan.

Tiga lapis, dan urutannya disengaja:

  Lapis 1  Statistik pasar dari kompetitor saja → tiga kandidat harga.
           **Tidak butuh modal sama sekali.**
  Lapis 2  Potongan marketplace → "uang bersih diterima" dan, yang paling
           berguna, **modal maksimum agar tidak rugi** di harga itu. User
           mencocokkan modalnya sendiri ke angka itu; sistem tidak menuntut
           input apa pun.
  Lapis 3  Kalau modal memang diisi: laba, margin, harga minimum, dan verdict
           rugi/tipis. Opsional, bukan syarat.

Baris milik toko sendiri (`is_own`) DIKELUARKAN dari statistik pasar. Kalau
tidak, harga kita ikut menaikkan rata-rata yang sedang kita bandingkan dengan
diri sendiri. Baris hasil tebakan AI (`sumber='ai'`) juga dikeluarkan.
"""
import re
import statistics

from . import mp_common

format_harga = mp_common.format_harga


def _int(nilai) -> int:
    """Angka dari apa pun. Baris Excel membawa `terjual` sebagai teks tampilan
    ('-', '1,2rb terjual'), bukan bilangan — `int()` polos meledak di situ."""
    if isinstance(nilai, bool):
        return 0
    if isinstance(nilai, (int, float)):
        return int(nilai)
    m = re.search(r"\d+", str(nilai or "").replace(".", "").replace(",", ""))
    return int(m.group(0)) if m else 0

MARGIN_MIN = 0.05          # di bawah ini dianggap "margin tipis"
SEBARAN_LEBAR = 2.0        # p75/p25 di atas ini = kandidat kemungkinan beda barang


# ─── Aritmetika harga ─────────────────────────────────────────────────────────

def _grid(harga):
    """Kelipatan pembulatan yang wajar untuk harga sebesar ini."""
    if harga < 100_000:
        return 500
    if harga < 1_000_000:
        return 1_000
    if harga < 10_000_000:
        return 5_000
    return 10_000


def bulatkan(harga, arah="terdekat"):
    """Bulatkan ke grid harga yang lazim dipakai penjual Indonesia.

    Rp 4.737.412 bukan harga yang pernah dipasang siapa pun; angka seperti itu
    membuat rekomendasi terlihat seperti keluaran mesin, bukan keputusan harga.
    """
    harga = _int(harga)
    if harga <= 0:
        return 0
    g = _grid(harga)
    if arah == "bawah":
        return (harga // g) * g
    if arah == "atas":
        return -((-harga) // g) * g
    return int(round(harga / g)) * g


def uang_bersih(harga, fee_persen=0.0, biaya_tetap=0):
    """Uang yang benar-benar masuk rekening setelah potongan marketplace."""
    harga = _int(harga)
    return int(harga * (1 - float(fee_persen or 0) / 100) - _int(biaya_tetap))


def modal_maks(harga, fee_persen=0.0, biaya_tetap=0, margin_target=0.0):
    """Modal tertinggi yang masih untung bila dijual di `harga`.

    Ini angka yang membuat modal jadi tidak wajib diisi: sistem menyebutkan
    batasnya, user yang mencocokkan biaya sebenarnya — termasuk biaya-biaya lain
    yang tidak diketahui sistem.
    """
    return int(uang_bersih(harga, fee_persen, biaya_tetap)
               - float(margin_target or 0) * _int(harga))


def harga_minimum(modal, fee_persen=0.0, biaya_tetap=0, margin_target=0.0):
    """Harga jual terendah yang masih memenuhi margin target.

    Kebalikan dari `modal_maks`. Penyebutnya bisa nol atau negatif kalau fee +
    margin target ≥ 100% — itu permintaan yang mustahil, bukan angka yang boleh
    dibiarkan meledak jadi ZeroDivisionError di tengah laporan.
    """
    penyebut = 1 - float(fee_persen or 0) / 100 - float(margin_target or 0)
    if penyebut <= 0.01:
        return 0
    return int((_int(modal) + _int(biaya_tetap)) / penyebut)


def _persentil(terurut, q):
    """Persentil sederhana dengan interpolasi linear."""
    if not terurut:
        return 0
    if len(terurut) == 1:
        return terurut[0]
    pos = (len(terurut) - 1) * q
    bawah = int(pos)
    atas = min(bawah + 1, len(terurut) - 1)
    sisa = pos - bawah
    return int(terurut[bawah] + (terurut[atas] - terurut[bawah]) * sisa)


# ─── Statistik pasar ──────────────────────────────────────────────────────────

def statistik_pasar(baris_rival) -> dict:
    """Statistik harga dari baris kompetitor, setelah pencilan dipangkas."""
    harga_mentah = [_int(b.get("harga")) for b in baris_rival
                    if _int(b.get("harga")) > 0 and b.get("sumber") != "ai"]
    dipakai, dibuang = mp_common.pangkas_pencilan(harga_mentah)
    dipakai = sorted(dipakai)
    if not dipakai:
        return {"n": 0, "n_pencilan": len(dibuang)}

    p25 = _persentil(dipakai, 0.25)
    p75 = _persentil(dipakai, 0.75)
    return {
        "n": len(dipakai),
        "n_pencilan": len(dibuang),
        "pencilan": dibuang,
        "min": dipakai[0],
        "p25": p25,
        "median": int(statistics.median(dipakai)),
        "p75": p75,
        "maks": dipakai[-1],
        "sebaran": round(p75 / p25, 2) if p25 else 0,
    }


def _keyakinan(stat) -> dict:
    """Seberapa boleh angka ini dipercaya. Ditampilkan, bukan disembunyikan."""
    n = stat.get("n", 0)
    if n == 0:
        return {"tingkat": "KOSONG",
                "alasan": "Tidak ada harga pembanding sama sekali."}
    if n < 3:
        return {"tingkat": "LEMAH",
                "alasan": f"Hanya {n} harga pembanding — belum cukup untuk "
                          f"menyimpulkan harga pasar."}
    if stat.get("sebaran", 0) > SEBARAN_LEBAR:
        return {"tingkat": "SEBARAN LEBAR",
                "alasan": f"Harga pembanding terpaut {stat['sebaran']}× "
                          f"(p25 ke p75) — kemungkinan sebagian kandidat bukan "
                          f"produk yang sama. Periksa daftar di bawah."}
    return {"tingkat": "BAIK",
            "alasan": f"{n} harga pembanding dengan sebaran wajar."}


# ─── Analisis lengkap ─────────────────────────────────────────────────────────

def analisa(grup, modal=None, fee_persen=0.0, biaya_tetap=0,
            margin_target=MARGIN_MIN) -> dict:
    """Analisis harga satu grup produk hasil `mp_match.kelompokkan`.

    `modal` boleh None — itu jalur utamanya, bukan pengecualian.
    """
    baris = grup.get("baris") if isinstance(grup, dict) else list(grup or [])
    baris = baris or []
    rival = [b for b in baris if not b.get("is_own")]
    milik = [b for b in baris if b.get("is_own")]

    stat = statistik_pasar(rival)
    hasil = {
        "label": grup.get("label") if isinstance(grup, dict) else "",
        "statistik": stat,
        "keyakinan": _keyakinan(stat),
        "n_rival": stat.get("n", 0),
        "biaya": {"fee_persen": float(fee_persen or 0),
                  "biaya_tetap": int(biaya_tetap or 0),
                  "margin_target": float(margin_target or 0)},
        "rival": rival,
        "milik": milik,
        "catatan": [],
    }

    if not stat.get("n"):
        hasil["catatan"].append(
            "Belum ada harga kompetitor untuk produk ini — tambahkan toko "
            "pembanding, atau periksa apakah pencocokan produknya benar.")
        return hasil

    # ── Lantai harga (hanya bila modal diketahui) ──
    lantai = 0
    if modal:
        lantai = harga_minimum(modal, fee_persen, biaya_tetap, margin_target)
        hasil["harga_minimum"] = lantai
        hasil["modal"] = _int(modal)

    def kandidat(nilai, arah="terdekat"):
        """Bulatkan, lalu jaga agar tidak pernah jatuh di bawah lantai modal.

        Menyarankan "ikuti median pasar" saat median ada di bawah modal berarti
        menyuruh user jualan rugi dengan nada percaya diri. Lantai ini yang
        mencegahnya, dan pelanggarannya dilaporkan — tidak diam-diam dinaikkan.
        """
        n = bulatkan(nilai, arah)
        if lantai and n < lantai:
            return bulatkan(lantai, "atas"), True
        return n, False

    # ── Tiga band ──
    langkah = _grid(stat["min"])
    agresif, a_naik = kandidat(stat["min"] - langkah, "bawah")
    seimbang, s_naik = kandidat(stat["median"], "terdekat")
    premium, p_naik = kandidat(stat["p75"], "terdekat")

    band = []
    for nama, harga, alasan, dinaikkan in (
        ("Agresif", agresif,
         f"Sedikit di bawah kompetitor termurah ({format_harga(stat['min'])}) — "
         f"jadi yang termurah tanpa membuang margin lebih dari perlunya.", a_naik),
        ("Seimbang", seimbang,
         f"Setara median pasar ({format_harga(stat['median'])}) — posisi paling "
         f"aman untuk volume.", s_naik),
        ("Premium", premium,
         f"Di kuartil atas ({format_harga(stat['p75'])}) — hanya masuk akal bila "
         f"rating dan jumlah terjual toko kita di atas rata-rata pesaing.", p_naik),
    ):
        b = {
            "nama": nama,
            "harga": harga,
            "harga_tampil": format_harga(harga),
            "alasan": alasan,
            "dinaikkan_ke_lantai": dinaikkan,
            "uang_bersih": uang_bersih(harga, fee_persen, biaya_tetap),
            "potongan": int(harga * float(fee_persen or 0) / 100),
            "biaya_tetap": int(biaya_tetap or 0),
            "modal_maks_impas": modal_maks(harga, fee_persen, biaya_tetap, 0),
            "modal_maks_margin": modal_maks(harga, fee_persen, biaya_tetap,
                                            margin_target),
        }
        if modal:
            b["laba"] = b["uang_bersih"] - _int(modal)
            b["margin_persen"] = round(b["laba"] / harga * 100, 1) if harga else 0
        band.append(b)
    hasil["band"] = band

    # ── Posisi harga kita sekarang ──
    if milik:
        kita = max(_int(m.get("harga")) for m in milik)
        hasil["harga_kita"] = kita
        if kita > 0:
            if kita <= stat["min"]:
                posisi = "TERMURAH"
            elif kita <= stat["p25"]:
                posisi = "MURAH"
            elif kita <= stat["p75"]:
                posisi = "WAJAR"
            else:
                posisi = "MAHAL"
            lebih_murah = sum(1 for h in [_int(b.get("harga")) for b in rival]
                              if 0 < h < kita)
            hasil["posisi"] = posisi
            hasil["peringkat"] = f"{lebih_murah + 1} dari {stat['n'] + 1} termurah"
            hasil["selisih_median"] = kita - stat["median"]
            hasil["selisih_median_persen"] = round(
                (kita - stat["median"]) / stat["median"] * 100, 1) if stat["median"] else 0
            hasil["uang_bersih_kita"] = uang_bersih(kita, fee_persen, biaya_tetap)
            hasil["modal_maks_kita"] = modal_maks(kita, fee_persen, biaya_tetap, 0)

            if modal:
                laba = hasil["uang_bersih_kita"] - _int(modal)
                hasil["laba_kita"] = laba
                hasil["margin_kita"] = round(laba / kita * 100, 1) if kita else 0
                if laba < 0:
                    hasil["verdict"] = "RUGI"
                elif hasil["margin_kita"] < margin_target * 100:
                    hasil["verdict"] = "MARGIN TIPIS"
                else:
                    hasil["verdict"] = posisi
            else:
                hasil["verdict"] = posisi

            # Termurah tapi tidak laku → masalahnya bukan harga.
            terjual_rival = [_int(b.get("terjual")) for b in rival]
            terjual_kita = max((_int(m.get("terjual")) for m in milik), default=0)
            if posisi == "TERMURAH" and terjual_rival:
                med_terjual = statistics.median(terjual_rival)
                if terjual_kita < med_terjual:
                    hasil["catatan"].append(
                        f"Sudah paling murah ({format_harga(kita)}) tapi terjual "
                        f"{terjual_kita} — di bawah median pesaing "
                        f"({int(med_terjual)}). Masalahnya bukan harga: periksa "
                        f"judul, foto, dan ulasan.")

    # ── Pasar di bawah modal ──
    if modal and lantai and stat["median"] < lantai:
        hasil["verdict"] = "PASAR DI BAWAH MODAL"
        # Ketiga band sudah dinaikkan ke lantai yang sama, jadi menampilkannya
        # sebagai tiga pilihan itu bohong — tidak ada pilihan apa pun di sini.
        # Sisakan satu angka dan katakan apa adanya.
        hasil["band"] = [dict(band[1], nama="Harga Impas Minimum", alasan=(
            f"Pasar tidak mendukung harga ini. {format_harga(band[1]['harga'])} "
            f"adalah harga terendah yang masih memberi margin "
            f"{round(margin_target * 100)}% — dan itu sudah di atas harga "
            f"kompetitor termahal ({format_harga(stat['maks'])})."
            if band[1]["harga"] > stat["maks"] else
            f"Pasar tidak mendukung harga ini. {format_harga(band[1]['harga'])} "
            f"adalah harga terendah yang masih memberi margin "
            f"{round(margin_target * 100)}%."))]
        hasil["catatan"].append(
            f"Median pasar {format_harga(stat['median'])} ada DI BAWAH harga "
            f"minimum kamu {format_harga(lantai)} (modal {format_harga(modal)} "
            f"+ potongan {fee_persen}%). Ikut perang harga di produk ini berarti "
            f"jualan rugi — cari sumber modal yang lebih murah atau tinggalkan "
            f"produknya.")

    if stat.get("n_pencilan"):
        hasil["catatan"].append(
            f"{stat['n_pencilan']} harga dibuang dari perhitungan karena terlalu "
            f"jauh dari median (biasanya aksesori, bundling, atau harga cicilan).")

    return hasil


def ringkas_kalimat(hasil) -> list:
    """Hasil analisis → beberapa kalimat Indonesia yang bisa langsung dibaca."""
    if not hasil or not hasil.get("statistik", {}).get("n"):
        return hasil.get("catatan", ["Belum ada data pembanding."]) if hasil else []
    s = hasil["statistik"]
    b = [f"{hasil.get('label') or 'Produk ini'}: {s['n']} harga pembanding, "
         f"{format_harga(s['min'])} – {format_harga(s['maks'])}, "
         f"median {format_harga(s['median'])}."]
    if hasil.get("harga_kita"):
        arah = "di atas" if hasil["selisih_median"] > 0 else "di bawah"
        b.append(f"Harga kita {format_harga(hasil['harga_kita'])} — "
                 f"{hasil['peringkat']}, {abs(hasil['selisih_median_persen'])}% "
                 f"{arah} median. Verdict: {hasil.get('verdict')}.")
    for band in hasil.get("band", []):
        if band["nama"] == "Seimbang":
            b.append(f"Saran seimbang {band['harga_tampil']}: uang bersih "
                     f"{format_harga(band['uang_bersih'])}, jadi modal harus di "
                     f"bawah {format_harga(band['modal_maks_impas'])} supaya "
                     f"tidak rugi.")
    b += hasil.get("catatan", [])
    return b
