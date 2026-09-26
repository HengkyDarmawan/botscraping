"""
ekspor.py — Export Excel/CSV dengan kolom yang dipilih user.

Dipakai halaman Leads web-dev dan Leads Komponen. Writer-nya sama dengan file
hasil scraping (scrapers/gmaps.py: hyperlink, warna prioritas, header beku,
autofilter), bedanya hanya kolom yang ikut dan urutannya ditentukan user.

Kolom berawalan "_" adalah kolom turunan yang dihitung saat export, mis.
"_nomor_wa" = nomor WhatsApp utama dalam format 08xx yang siap diketik/diimpor.
"""
from datetime import datetime
from pathlib import Path

from scrapers import komponen, kontak

OUTPUT_DIR = Path(__file__).resolve().parent / "output"

# Pilihan kolom untuk Leads web-dev: (kunci, label, grup).
KOLOM_WEBDEV = [
    ("nama_bisnis", "Nama Bisnis", "Identitas"),
    ("kategori", "Kategori", "Identitas"),
    ("tier", "Tier", "Identitas"),
    ("skor_pembeli", "Skor Pembeli", "Identitas"),
    ("jasa_utama", "Jasa Utama", "Penawaran"),
    ("jalur", "Tim", "Penawaran"),
    ("alasan_pitch", "Kalimat Pembuka", "Penawaran"),
    ("jasa_pendukung", "Jasa Pendukung", "Penawaran"),
    ("_nomor_wa", "Nomor WA", "Kontak"),
    ("whatsapp_link", "Link WA", "Kontak"),
    ("whatsapp_lain", "WA Lain", "Kontak"),
    ("telepon", "Telepon", "Kontak"),
    ("email", "Email", "Kontak"),
    ("email_lain", "Email Lain", "Kontak"),
    ("website", "Website", "Online"),
    ("web_status", "Kondisi Website", "Online"),
    ("web_platform", "Platform Website", "Online"),
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
    ("sudah_diklaim", "Sudah Diklaim", "Google Maps"),
    ("jam_operasional", "Jam Operasional", "Google Maps"),
    ("status_leads", "Status", "CRM"),
    ("tanggal_follow_up", "Follow-up", "CRM"),
    ("catatan", "Catatan", "CRM"),
    ("first_seen_at", "Pertama Ditemukan", "CRM"),
]

PRESET_WEBDEV = {
    "nama_wa": ["nama_bisnis", "_nomor_wa"],
    "nama_wa_email": ["nama_bisnis", "_nomor_wa", "email"],
    "kontak": ["nama_bisnis", "kategori", "_nomor_wa", "telepon", "email",
               "website", "instagram", "alamat"],
    "lengkap": [k for k, _, _ in KOLOM_WEBDEV],
}

KATALOG = {
    "webdev": {"kolom": KOLOM_WEBDEV, "preset": PRESET_WEBDEV},
    "komponen": {"kolom": komponen.KOLOM_EXPORT, "preset": komponen.PRESET_EXPORT},
}

_TURUNAN = {
    "_nomor_wa": kontak.nomor_wa,
}


def katalog(ruang):
    """Data pemilih kolom untuk front-end: kolom, grup, dan preset."""
    k = KATALOG[ruang]
    return {
        "kolom": [{"k": a, "label": b, "grup": c} for a, b, c in k["kolom"]],
        "preset": k["preset"],
        # jsonify mengurutkan kunci dict; urutan tombol preset dikirim terpisah.
        "urutan_preset": list(k["preset"]),
    }


def _syarat_kontak(row, wajib):
    punya_wa = bool(kontak.nomor_wa(row))
    punya_email = bool(str(row.get("email") or "").strip())
    if wajib == "wa":
        return punya_wa
    if wajib == "email":
        return punya_email
    if wajib == "wa_atau_email":
        return punya_wa or punya_email
    if wajib == "wa_dan_email":
        return punya_wa and punya_email
    return True


def tulis(rows, ruang, kolom, fmt="xlsx", wajib="", awalan="export", ringkasan=None):
    """
    Tulis export dan kembalikan (nama_file, jumlah_baris).

    `kolom` = daftar kunci kolom sesuai urutan pilihan user; kunci yang tidak
    dikenal diabaikan. `wajib` membuang baris yang tidak punya kontak terpilih
    (mis. export "Nama + WA" tanpa baris yang nomornya kosong).
    """
    from scrapers.gmaps import _rapikan, _save_excel

    label = {a: b for a, b, _ in KATALOG[ruang]["kolom"]}
    kolom = [k for k in kolom if k in label] or [k for k, _, _ in KATALOG[ruang]["kolom"]]
    rows = [r for r in rows if _syarat_kontak(r, wajib)]
    if not rows:
        return None, 0
    for r in rows:
        for k in kolom:
            if k in _TURUNAN:
                r[k] = _TURUNAN[k](r)

    OUTPUT_DIR.mkdir(exist_ok=True)
    stempel = datetime.now().strftime("%Y%m%d_%H%M%S")
    dasar = OUTPUT_DIR / f"{awalan}_{stempel}"
    df = _rapikan(rows, kolom, hanya=True)
    if fmt == "csv":
        df.columns = [label.get(c, c) for c in df.columns]
        df.to_csv(f"{dasar}.csv", index=False, encoding="utf-8-sig")
        return f"{dasar.name}.csv", len(rows)
    _save_excel({"Leads": df}, str(dasar), ringkasan, label=label)
    return f"{dasar.name}.xlsx", len(rows)
