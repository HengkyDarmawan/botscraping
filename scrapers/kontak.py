"""
scrapers/kontak.py — Aturan kontak yang dipakai KEDUA ruang (klien website &
distributor komponen).

  • rapikan_kontak  — tautan GMaps yang sebenarnya IG/Tokopedia/... dipindah ke
                      kolomnya; nomor WA dari website melengkapi WhatsApp bila
                      Google Maps hanya mencantumkan telepon kantor
  • kontak_kurang   — apakah lead memenuhi mode kontak yang diminta
  • bisa_dilengkapi_website — apakah pemeriksaan website masih mungkin
                      menambah WA/email (menentukan keputusan ditunda atau tidak)

Pembagian impor: modul ini mengimpor scrapers.scoring (helper nomor telepon);
scoring memanggil rapikan_kontak lewat impor malas di dalam nilai_lead.
"""
import re

from scrapers import scoring
from scrapers.enrich import klasifikasi_tautan

MODE_WA = "wa"
MODE_EMAIL = "email"
MODE_WA_ATAU_EMAIL = "wa_atau_email"
MODE_WA_DAN_EMAIL = "wa_dan_email"
MODE_TELEPON = "telepon"
MODE_APA_SAJA = "apa_saja"
MODE_BEBAS = "bebas"
MODE_KONTAK = (MODE_WA, MODE_EMAIL, MODE_WA_ATAU_EMAIL, MODE_WA_DAN_EMAIL,
               MODE_TELEPON, MODE_APA_SAJA, MODE_BEBAS)

LABEL_MODE = {
    MODE_WA: "wajib WhatsApp",
    MODE_EMAIL: "wajib email",
    MODE_WA_ATAU_EMAIL: "WhatsApp atau email",
    MODE_WA_DAN_EMAIL: "WhatsApp dan email",
    MODE_TELEPON: "telepon apa saja",
    MODE_APA_SAJA: "salah satu kontak ada",
    MODE_BEBAS: "tanpa syarat kontak",
}


def punya_wa(row):
    return bool(str(row.get("whatsapp_link") or "").strip())


def punya_email(row):
    return bool(str(row.get("email") or "").strip())


def kontak_kurang(row, mode):
    """Alasan lead gugur syarat kontak, atau None bila lolos."""
    wa, em = punya_wa(row), punya_email(row)
    telepon = str(row.get("telepon") or "").strip()
    if mode == MODE_WA and not wa:
        return "tidak ada nomor WhatsApp"
    if mode == MODE_EMAIL and not em:
        return "tidak ada email"
    if mode == MODE_WA_ATAU_EMAIL and not (wa or em):
        return "tidak ada WhatsApp maupun email"
    if mode == MODE_WA_DAN_EMAIL and not (wa and em):
        return "WhatsApp & email tidak lengkap"
    if mode == MODE_TELEPON and not (wa or telepon):
        return "tidak ada nomor telepon"
    if mode == MODE_APA_SAJA and not (wa or em or telepon
                                      or str(row.get("instagram") or "").strip()
                                      or str(row.get("facebook") or "").strip()):
        return "tidak ada kontak sama sekali"
    return None


def bisa_dilengkapi_website(row):
    """
    True bila pemeriksaan website MUNGKIN menambah WA/email untuk lead ini.

    Instagram/marketplace tidak dihitung: halamannya di balik login atau tidak
    memuat kontak penjual, jadi membukanya tidak akan menghasilkan apa-apa.
    """
    url = str(row.get("website") or "").strip()
    if not url:
        return False
    return klasifikasi_tautan(url) in ("", "linkinbio")


_KOLOM_TAUTAN = ("instagram", "facebook", "tiktok", "tokopedia", "shopee",
                 "marketplace_lain", "linkedin", "youtube")


def _nomor_08(digit62):
    d = scoring.normalisasi_nomor(digit62)
    return ("0" + d[2:]) if d.startswith("62") else d


def rapikan_kontak(row):
    """
    Pindahkan tautan ke kolom yang tepat dan lengkapi WhatsApp.

      • website GMaps yang sebenarnya IG/Tokopedia/... → kolom itu; `website_utama`
        hanya berisi website sungguhan. Kolom `website` sendiri TIDAK diubah —
        analisis jasa web di ruang klien website bergantung padanya.
      • telepon GMaps bukan seluler tapi website mencantumkan wa.me → nomor itu
        jadi WhatsApp utama; nomor WA lain masuk `whatsapp_lain`
    """
    web = str(row.get("website") or "").strip()
    jenis = klasifikasi_tautan(web) if web else ""
    if jenis in _KOLOM_TAUTAN:
        if not str(row.get(jenis) or "").strip():
            row[jenis] = web.split("?")[0]
    row["website_utama"] = web if (web and jenis == "") else (
        row.get("website_utama") or "")

    wa_web = [scoring.normalisasi_nomor(n) for n in
              re.split(r"[;,\s]+", str(row.get("whatsapp_web") or
                                       row.get("whatsapp_lain") or ""))
              if n.strip()]
    wa_web = [n for n in wa_web if scoring.nomor_wa_valid(n)]
    utama = scoring.normalisasi_nomor(row.get("telepon")) \
        if scoring.nomor_wa_valid(row.get("telepon")) else ""
    if not utama and wa_web:
        utama = wa_web[0]
    if utama:
        row["whatsapp_link"] = scoring.link_wa(utama)
    lain = [n for n in wa_web if n != utama]
    row["whatsapp_lain"] = "; ".join(_nomor_08(n) for n in dict.fromkeys(lain))
    return row


def nomor_wa(row):
    """Nomor WhatsApp utama dalam format 08xx (untuk export & template)."""
    m = re.search(r"wa\.me/(\d+)", str(row.get("whatsapp_link") or ""))
    return _nomor_08(m.group(1)) if m else ""
