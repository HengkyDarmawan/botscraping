"""
scrapers/mp_common.py — Helper kecil yang dipakai bersama modul marketplace
===========================================================================
Satu rumah untuk parsing harga rupiah supaya `pricing.py`, `mp_dom.py`, dan
`mp_api.py` tidak masing-masing punya versi sendiri yang lama-lama menyimpang.
"""
import re

# Batas kewarasan harga marketplace Indonesia. Di luar rentang ini hampir pasti
# bukan harga produk — melainkan nomor resi, jumlah terjual, atau id.
HARGA_MIN = 1_000
HARGA_MAKS = 500_000_000

_POLA_RUPIAH = re.compile(r"Rp\s?[\d][\d\.\,]*", re.IGNORECASE)


def parse_harga(teks) -> int:
    """Ambil harga pertama yang masuk akal dari sebuah teks.

    'Rp 6.500.000' → 6500000. Berbeda dari versi lama yang selalu mengambil
    kecocokan pertama apa adanya: di sini kecocokan yang jatuh di luar rentang
    kewarasan dilewati, jadi 'Rp1' pada label promo tidak dianggap harga.
    """
    for nilai in parse_harga_semua(teks):
        return nilai
    # Tidak ada pola 'Rp' sama sekali — coba angka polos (mis. kolom Excel lama).
    digit = re.sub(r"[^\d]", "", str(teks or ""))
    if digit:
        angka = int(digit)
        if HARGA_MIN <= angka <= HARGA_MAKS:
            return angka
    return 0


def parse_harga_semua(teks) -> list:
    """Semua harga wajar di dalam teks, urut kemunculan.

    Berguna untuk kartu produk yang memuat harga jual DAN harga coret, atau
    rentang harga ('Rp10.000 - Rp25.000') seperti kartu Shopee.
    """
    hasil = []
    for m in _POLA_RUPIAH.finditer(str(teks or "")):
        digit = re.sub(r"[^\d]", "", m.group())
        if not digit:
            continue
        angka = int(digit)
        if HARGA_MIN <= angka <= HARGA_MAKS:
            hasil.append(angka)
    return hasil


def format_harga(jumlah) -> str:
    """6500000 → 'Rp 6.500.000'. Nol/kosong → '-'."""
    try:
        jumlah = int(jumlah or 0)
    except (TypeError, ValueError):
        return "-"
    if not jumlah:
        return "-"
    return "Rp " + f"{jumlah:,}".replace(",", ".")


def baris_bersih(teks) -> list:
    """Pecah teks kartu jadi baris yang sudah dibuang spasi & baris kosongnya."""
    return [b.strip() for b in str(teks or "").splitlines() if b.strip()]


def tebak_nama(teks_kartu) -> str:
    """Nama produk dari teks kartu = baris TERPANJANG yang bukan harga.

    Sengaja bukan "baris pertama": kartu marketplace kerap didahului badge
    ('Terlaris', 'Ad', 'Bebas Ongkir') yang cukup panjang untuk lolos ambang
    karakter apa pun. Judul produk hampir selalu baris terpanjang di kartu,
    sementara badge, rating, dan nama kota semuanya pendek.

    Kembalikan '' bila tidak ada kandidat — pemanggil yang memutuskan membuang
    kartunya, jangan pernah memaksakan harga jadi nama produk.
    """
    kandidat = []
    for baris in baris_bersih(teks_kartu):
        if _POLA_RUPIAH.search(baris):
            continue
        if re.fullmatch(r"[\d\.\,%\s\+\-\|/]*", baris):   # angka/rating/pemisah saja
            continue
        kandidat.append(baris)
    if not kandidat:
        return ""
    return max(kandidat, key=len)[:200]


# ─── Pemangkasan pencilan harga ───────────────────────────────────────────────

def pangkas_pencilan(harga_list, bawah=0.4, atas=2.5):
    """Buang harga yang terlalu jauh dari median. Return (dipakai, dibuang).

    Kontaminasi khas hasil panen marketplace, dan semuanya nyata:
      • aksesori yang menyebut nama produk — "Thermal Paste untuk i7 12700K"
        seharga Rp 35.000 di antara CPU Rp 4,7 juta
      • bundling — "PAKET RAKITAN PC i7 12700K + RTX 3060 Ti" Rp 24,5 juta
      • harga cicilan per bulan yang terbaca sebagai harga produk

    Satu baris seperti itu cukup untuk merusak seluruh rekomendasi harga: median
    bergeser, `min` jadi omong kosong, dan verdict "terlalu mahal" muncul untuk
    harga yang sebenarnya wajar. Ambang rasio dipakai, bukan standar deviasi —
    sebaran harga marketplace condong ke kanan dan tidak pernah normal.
    """
    angka = sorted(int(h) for h in harga_list if h and int(h) > 0)
    if len(angka) < 3:
        return list(angka), []          # terlalu sedikit untuk menilai pencilan
    tengah = angka[len(angka) // 2] if len(angka) % 2 else \
        (angka[len(angka) // 2 - 1] + angka[len(angka) // 2]) // 2
    if tengah <= 0:
        return list(angka), []
    dipakai = [h for h in angka if bawah * tengah <= h <= atas * tengah]
    dibuang = [h for h in angka if h not in dipakai]
    # Jangan pernah membuang semuanya: kalau sebarannya memang lebar, itu sinyal
    # pencocokan yang perlu dibetulkan user — bukan alasan mengosongkan statistik.
    return (dipakai, dibuang) if dipakai else (list(angka), [])
