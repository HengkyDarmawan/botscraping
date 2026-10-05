"""
scrapers/komponen.py — Intelijen prospek untuk distributor komponen komputer
(PT. Inti Sentosa Bersama).

Padanan scrapers/scoring.py untuk ruang data "komponen". Pertanyaan yang
dijawab berbeda: bukan "jasa digital apa yang dia butuhkan", tapi

  • bisnis ini masuk SEGMEN pembeli apa (reseller, warnet, sekolah, ...)
  • bisa dihubungi lewat apa (WA seluler / email / telepon kantor)
  • seberapa besar dan aktif usahanya

lalu merangkumnya jadi `skor_pembeli` 0-100 + tier Tinggi/Sedang/Rendah yang
disimpan di kolom yang sama dengan ruang web-dev, supaya pengurutan, filter
tier, dan warna baris Excel tetap memakai mesin yang sudah ada.

Modul ini juga pemilik aturan KONTAK untuk ruang komponen (mode WA / email /
WA atau email / WA dan email) — dipakai scrapers/gmaps.py saat scraping untuk
memutuskan listing mana yang dijadikan lead.
"""
import re

from scrapers import scoring

TIER_TINGGI = "Tinggi"
TIER_SEDANG = "Sedang"
TIER_RENDAH = "Rendah"
URUTAN_TIER = [TIER_TINGGI, TIER_SEDANG, TIER_RENDAH]

# ─── Segmen pembeli ───────────────────────────────────────────────────────────
# (nama, bobot volume 0-40, kebutuhan umum, pola kategori/nama GMaps)
# Urutan menentukan prioritas: yang pertama cocok dipakai. Nama segmen sengaja
# sama dengan nama sektor di data/keywords_komponen.json.
SEGMEN = [
    ("Reseller & Toko Komputer", 40,
     "stok komponen, laptop & aksesoris untuk dijual kembali",
     r"komputer|computer|laptop|notebook|printer|aksesoris (komputer|laptop|hp)|"
     r"gaming gear|\bservis (komputer|laptop|printer)|\bservice (komputer|laptop|printer)|"
     r"toko elektronik|electronics? store|toko perlengkapan komputer|pc store|it store"),
    ("IT, Jaringan & CCTV", 36,
     "perangkat jaringan, server & PC untuk proyek klien",
     r"konsultan (it|teknologi)|\bit (consultant|solution|service)|jaringan|network|"
     r"cctv|keamanan|security system|software|perangkat lunak|internet service|"
     r"penyedia (layanan )?internet|\bisp\b|system integrator|teknologi informasi|"
     r"pengembang (aplikasi|perangkat)|web design|data center|pusat data"),
    ("Warnet & Game Center", 32,
     "PC gaming, upgrade VGA/RAM/SSD untuk banyak unit",
     r"warnet|internet cafe|kafe internet|game center|pusat permainan|e-?sports?|"
     r"gaming|rental (ps|playstation)|playstation"),
    ("Pendidikan & Kursus", 30,
     "lab komputer, laptop guru, printer & proyektor",
     r"sekolah|\bsmk\b|\bsma\b|\bsmp\b|\bsd\b|madrasah|universitas|university|"
     r"kampus|institut|politeknik|akademi|sekolah tinggi|kursus|pelatihan|"
     r"training|bimbingan belajar|bimbel|pesantren|lembaga pendidikan|college"),
    ("Percetakan & Kreatif", 26,
     "PC desain/editing spesifikasi tinggi, monitor & printer",
     r"percetakan|printing|fotokopi|photocopy|foto ?copy|studio foto|fotografer|"
     r"photograph|desain grafis|graphic design|advertising|periklanan|"
     r"rumah produksi|production house|studio rekaman|videografer|sablon"),
    ("Kesehatan, Hotel & Ritel", 20,
     "PC kasir/front office, printer & jaringan",
     r"klinik|clinic|rumah sakit|hospital|apotek|pharmacy|laboratorium|"
     r"puskesmas|dokter|hotel|penginapan|resort|minimarket|supermarket|"
     r"swalayan|toserba|department store"),
    ("Instansi Pemerintah", 18,
     "pengadaan PC & printer (umumnya lewat e-katalog)",
     r"kantor (pemerintah|dinas|kelurahan|kecamatan|desa|bupati|walikota|camat)|"
     r"\bdinas\b|kelurahan|kecamatan|pemerintah|kementerian|badan pusat|"
     r"\bpemkot\b|\bpemkab\b|\bpemprov\b|dprd|kantor polisi|polsek|polres"),
    ("Kantor & Perusahaan", 28,
     "PC, laptop, printer & jaringan untuk operasional",
     r"kantor|perusahaan|corporate|office|\bpt\b|\bcv\b|notaris|akuntan|"
     r"konsultan|logistik|logistic|ekspedisi|kurir|courier|pergudangan|gudang|"
     r"warehouse|pabrik|factory|manufaktur|industri|distributor|agen|"
     r"asuransi|bank|koperasi|coworking|developer properti|kontraktor"),
]
SEGMEN_LAIN = ("Lainnya", 10, "kebutuhan komputer umum")

_POLA_SEGMEN = [(nama, bobot, butuh, re.compile(pola, re.I))
                for nama, bobot, butuh, pola in SEGMEN]


# Kategori Google Maps yang hampir pasti bukan pembeli komponen PC. Bisa diubah
# user lewat Pengaturan (kunci "kategori_dikecualikan").
KATEGORI_DIKECUALIKAN = (
    "audio mobil, aksesori mobil, aksesoris mobil, variasi mobil, bengkel, "
    "cuci mobil, salon mobil, dealer, showroom, suku cadang, spare part, "
    "ponsel, handphone, konter, pulsa, gps, toko kamera, restoran, rumah makan, kafe, "
    "salon kecantikan"
)


def daftar_kecualian(teks):
    """"a, b ,c" → ["a", "b", "c"] (huruf kecil, kosong dibuang)."""
    return [k.strip().lower() for k in str(teks or "").split(",") if k.strip()]


def dikecualikan(kategori, nama, daftar):
    """
    Alasan bila bisnis ini jelas bukan calon pembeli komponen, selain itu None.

    Kategori yang cocok daftar pengecualian tetap DILOLOSKAN bila nama bisnisnya
    menunjukkan segmen pembeli — "Vision CCTV" berkategori "Toko Kamera" adalah
    toko CCTV, bukan toko kamera foto.
    """
    kat = str(kategori or "").lower()
    if not kat or not daftar:
        return None
    kena = next((k for k in daftar if k in kat), None)
    if not kena:
        return None
    # Pola "Kantor & Perusahaan" (PT, CV, agen, ...) terlalu umum untuk jadi
    # alasan meloloskan — "PT Audio Mobil" tetap bengkel audio mobil.
    if any(pola.search(str(nama or "")) for seg, _, _, pola in _POLA_SEGMEN
           if seg != "Kantor & Perusahaan"):
        return None
    return f"kategori tidak relevan: {kategori}"


def klasifikasi_segmen(kategori, nama=""):
    """
    (nama_segmen, bobot, kebutuhan) untuk satu bisnis.

    Kategori Google Maps dicocokkan lebih dulu karena paling bisa dipercaya;
    nama bisnis hanya cadangan (mis. "CV Maju Komputer" berkategori "Toko").
    """
    for teks in (kategori, nama):
        teks = str(teks or "")
        if not teks.strip():
            continue
        for seg, bobot, butuh, pola in _POLA_SEGMEN:
            if pola.search(teks):
                return seg, bobot, butuh
    return SEGMEN_LAIN


# Nama segmen yang bisa dipilih user saat tambah/edit lead manual.
NAMA_SEGMEN = [s[0] for s in SEGMEN] + [SEGMEN_LAIN[0]]


def _segmen_pilihan(nama):
    """(nama, bobot, kebutuhan) untuk segmen yang dipilih user, atau None."""
    for seg, bobot, butuh, _ in SEGMEN:
        if seg == nama:
            return seg, bobot, butuh
    return SEGMEN_LAIN if nama == SEGMEN_LAIN[0] else None


# ─── Kontak ───────────────────────────────────────────────────────────────────
# Aturan kontak dipakai kedua ruang — tinggal di scrapers/kontak.py.
from scrapers.kontak import punya_email, punya_wa, rapikan_kontak  # noqa: E402


# ─── Skor prioritas ───────────────────────────────────────────────────────────

def _ada(row, kolom):
    return bool(str(row.get(kolom) or "").strip())


def nilai_lead(row, jumlah_cabang=1):
    """
    Hitung segmen + skor prioritas 0-100, tempelkan ke row, dan kembalikan row.

      SEGMEN  (maks 40) — seberapa besar & sering segmen ini biasanya membeli
      KONTAK  (maks 35) — WA seluler, email, atau hanya telepon kantor
      SKALA   (maks 25) — jumlah ulasan, rating, cabang, website/toko online
    """
    rapikan_kontak(row)
    # Segmen yang dipilih user (kolom terkunci) menang atas tebakan dari kategori.
    dikunci = {k.strip() for k in str(row.get("kolom_dikunci") or "").split(",")}
    pilihan = _segmen_pilihan(row.get("segmen")) if "segmen" in dikunci else None
    seg, bobot, butuh = pilihan or klasifikasi_segmen(row.get("kategori"),
                                                      row.get("nama_bisnis"))
    row["segmen"] = seg

    alasan = []
    kontak = 0
    if punya_wa(row):
        dari_web = not scoring.nomor_wa_valid(row.get("telepon"))
        kontak += 18 if dari_web else 22
        alasan.append("WA dari website" if dari_web else "WA seluler")
    elif _ada(row, "telepon"):
        kontak += 6
        alasan.append("hanya telepon kantor")
    if punya_email(row):
        kontak += 13
        alasan.append("email tersedia")
    kontak = min(kontak, 35)

    skala = 0
    ulasan = scoring._bulat(row.get("jumlah_ulasan"), 0)
    rating = scoring._angka(row.get("rating"), 0.0)
    if ulasan >= 500:
        skala += 12
    elif ulasan >= 100:
        skala += 9
    elif ulasan >= 20:
        skala += 6
    elif ulasan > 0:
        skala += 3
    if ulasan:
        alasan.append(f"{ulasan} ulasan")
    if rating >= 4.5 and ulasan >= 10:
        skala += 3
    if int(jumlah_cabang or 1) > 1:
        skala += 4
        alasan.append(f"{int(jumlah_cabang)} cabang")
    if _ada(row, "website_utama"):
        skala += 3
    if _ada(row, "tokopedia") or _ada(row, "shopee") or _ada(row, "marketplace_lain"):
        skala += 3
        alasan.append("jualan di marketplace")
    skala = min(skala, 25)

    total = int(round(bobot + kontak + skala))
    if scoring.bisnis_tutup(row.get("status_buka")):
        total = 0
        alasan = [str(row.get("status_buka"))]

    row["skor_pembeli"] = total
    row["tier"] = (TIER_TINGGI if total >= 70 else
                   TIER_SEDANG if total >= 50 else TIER_RENDAH)
    row["alasan_pitch"] = f"Kebutuhan: {butuh}." + (
        f" ({', '.join(alasan)})" if alasan else "")
    row["skor_popularitas"] = scoring.skor_popularitas(row.get("rating"),
                                                       row.get("jumlah_ulasan"))
    return row


# ─── Kolom ────────────────────────────────────────────────────────────────────

COLUMN_ORDER = [
    "nama_bisnis", "segmen", "kategori", "skor_pembeli", "tier",
    "telepon", "whatsapp_link", "whatsapp_lain", "email", "email_lain",
    "website_utama", "instagram", "facebook", "tiktok", "tokopedia", "shopee",
    "marketplace_lain", "linkedin", "youtube",
    "alamat", "kota", "area_pencarian", "rating", "jumlah_ulasan",
    "jam_operasional", "status_buka", "alasan_pitch",
    "status_leads", "tanggal_dihubungi", "tanggal_follow_up", "proposal_nomor",
    "nama_pic", "catatan", "koordinat", "source_url",
]

# Kolom yang tidak relevan untuk calon pembeli komponen — tidak ikut Excel run.
KOLOM_TERSEMBUNYI = {
    "web_status", "web_https", "web_mobile", "web_load_ms", "web_platform",
    "web_tahun_update", "web_ada_pixel", "web_ada_toko", "jasa_utama", "jalur",
    "jasa_pendukung", "sudah_diklaim", "jumlah_foto", "rentang_harga",
    "lolos_filter", "proposal_file", "proposal_tanggal", "kanal_kontak",
    "last_checked_at", "times_seen", "website", "whatsapp_web",
}

# Pilihan kolom untuk export: (kunci, label header, grup). Kunci berawalan "_"
# adalah kolom turunan yang dihitung saat export.
KOLOM_EXPORT = [
    ("nama_bisnis", "Nama Bisnis", "Identitas"),
    ("segmen", "Segmen", "Identitas"),
    ("kategori", "Kategori", "Identitas"),
    ("tier", "Prioritas", "Identitas"),
    ("skor_pembeli", "Skor", "Identitas"),
    ("_nomor_wa", "Nomor WA", "Kontak"),
    ("whatsapp_link", "Link WA", "Kontak"),
    ("whatsapp_lain", "WA Lain", "Kontak"),
    ("telepon", "Telepon", "Kontak"),
    ("email", "Email", "Kontak"),
    ("email_lain", "Email Lain", "Kontak"),
    ("nama_pic", "Nama PIC", "Kontak"),
    ("website_utama", "Website", "Online"),
    ("instagram", "Instagram", "Online"),
    ("facebook", "Facebook", "Online"),
    ("tiktok", "TikTok", "Online"),
    ("tokopedia", "Tokopedia", "Online"),
    ("shopee", "Shopee", "Online"),
    ("marketplace_lain", "Marketplace Lain", "Online"),
    ("linkedin", "LinkedIn", "Online"),
    ("youtube", "YouTube", "Online"),
    ("alamat", "Alamat", "Lokasi"),
    ("kota", "Kota", "Lokasi"),
    ("area_pencarian", "Area Pencarian", "Lokasi"),
    ("koordinat", "Koordinat", "Lokasi"),
    ("source_url", "Link Google Maps", "Lokasi"),
    ("rating", "Rating", "Google Maps"),
    ("jumlah_ulasan", "Jumlah Ulasan", "Google Maps"),
    ("jam_operasional", "Jam Operasional", "Google Maps"),
    ("alasan_pitch", "Kebutuhan & Alasan", "Google Maps"),
    ("status_leads", "Status", "CRM"),
    ("tanggal_dihubungi", "Tanggal Dihubungi", "CRM"),
    ("tanggal_follow_up", "Follow-up", "CRM"),
    ("proposal_nomor", "No. Proposal", "CRM"),
    ("catatan", "Catatan", "CRM"),
    ("first_seen_at", "Pertama Ditemukan", "CRM"),
]

PRESET_EXPORT = {
    "nama_wa": ["nama_bisnis", "_nomor_wa"],
    "nama_wa_email": ["nama_bisnis", "_nomor_wa", "email"],
    "kontak": ["nama_bisnis", "segmen", "_nomor_wa", "telepon", "email",
               "website_utama", "instagram", "tokopedia", "shopee", "alamat", "kota"],
    "lengkap": [k for k, _, _ in KOLOM_EXPORT],
}

# ─── Status CRM ───────────────────────────────────────────────────────────────

STATUS_BELUM = "Belum Dihubungi"
STATUS_PILIHAN = [
    STATUS_BELUM, "Proposal Dibuat", "Email Terkirim", "WA Terkirim",
    "Follow-up 1", "Follow-up 2", "Membalas", "Minta Quotation", "Deal",
    "Tidak Tertarik", "Jangan Hubungi",
]
# Status yang berarti lead sudah selesai — tidak masuk daftar follow-up.
STATUS_SELESAI = ("Deal", "Tidak Tertarik", "Jangan Hubungi")
# Lead yang minta tidak dihubungi lagi tidak boleh ikut export (UU PDP).
STATUS_TANPA_EXPORT = ("Jangan Hubungi",)
