"""
scrapers/pesan_web.py — Template pesan WA & email untuk calon klien jasa website.

Padanan "Template Email ISB.docx" untuk ruang klien website. Teksnya disimpan di
tabel `pengaturan` leads.db dan diedit user lewat modal "Pengaturan pesan" di
Database Leads; nilai di bawah hanya titik awal.

Teks bawaan mengikuti panduan yang sama dengan template ISB: kalimat biasa,
tanpa huruf tebal atau tanda pisah panjang, dan menyebut hal konkret (jasa yang
cocok + alasannya dari hasil analisis) alih-alih klaim umum.

Hasil `isi_pesan` berbentuk sama dengan dokumen_isb.isi_template_email, jadi
popup email/WA yang sama dipakai kedua menu.
"""
import re

import config

PLACEHOLDER = {
    "nama": "nama bisnis",
    "sapaan": "Bapak/Ibu, atau nama PIC bila diisi",
    "pengirim": "nama Anda",
    "usaha": "nama usaha/brand Anda",
    "wa_pengirim": "nomor WA Anda",
    "email_pengirim": "email Anda",
    "jasa": "jasa yang paling cocok (hasil analisis)",
    "jasa_pendukung": "jasa tambahan yang juga cocok (hasil analisis)",
    "alasan": "alasan singkat kenapa jasa itu cocok",
}

BAWAAN = {
    "pengirim": getattr(config, "PESAN_PENGIRIM", ""),
    "usaha": getattr(config, "PESAN_USAHA", ""),
    "wa_pengirim": "",
    "email_pengirim": "",
    "tpl_wa": getattr(config, "PESAN_TEMPLATE", "") or (
        "Halo {nama}, perkenalkan saya {pengirim} dari {usaha}.\n\n{alasan}\n\n"
        "Boleh saya kirimkan contoh hasil kerja dan estimasi biayanya?"),
    "tpl_email_subject": "Soal {jasa} untuk {nama}",
    "tpl_email": (
        "Selamat siang {sapaan},\n\n"
        "Perkenalkan, saya {pengirim} dari {usaha}.\n\n"
        "Saya melihat profil {nama} di Google Maps. {alasan}\n\n"
        "Kalau berkenan, saya bisa kirimkan contoh hasil kerja dan perkiraan biaya "
        "untuk {jasa}. Cukup balas email ini, tanpa kewajiban apa pun.\n\n"
        "Terima kasih atas waktunya.\n\n"
        "Salam,\n{pengirim}\n{usaha}\nWA {wa_pengirim}\n\n"
        "Jika penawaran ini tidak sesuai, cukup balas email ini dan saya tidak akan "
        "menghubungi kembali."),
    "tpl_proposal_subject": "Penawaran {jasa} untuk {nama}",
    "tpl_proposal": (
        "Selamat siang {sapaan},\n\n"
        "Perkenalkan, saya {pengirim} dari {usaha}. Berikut penawaran singkat kami "
        "untuk {nama}.\n\n"
        "Yang kami temukan\n"
        "{alasan}\n\n"
        "Yang kami tawarkan\n"
        "Jasa utama: {jasa}\n"
        "Pendukung: {jasa_pendukung}\n\n"
        "Ruang lingkup\n"
        "1. Analisis kebutuhan dan target pelanggan {nama}\n"
        "2. Pengerjaan {jasa} sesuai yang disepakati\n"
        "3. Revisi sampai sesuai, lalu serah terima dan panduan singkat\n\n"
        "Investasi\n"
        "[isi kisaran harga & paket]\n\n"
        "Waktu pengerjaan\n"
        "[isi estimasi waktu pengerjaan]\n\n"
        "Kalau berkenan, saya bisa kirimkan contoh hasil kerja untuk usaha yang mirip "
        "atau menjadwalkan obrolan singkat 15 menit. Cukup balas pesan ini.\n\n"
        "Salam,\n{pengirim}\n{usaha}\nWA {wa_pengirim}\n{email_pengirim}"),
    "tpl_fu1": (
        "Selamat siang {sapaan},\n\n"
        "Saya ingin menindaklanjuti email saya beberapa hari lalu soal {jasa} untuk "
        "{nama}. Kalau ingin melihat contoh untuk usaha yang mirip, silakan balas saja, "
        "nanti saya kirimkan.\n\n"
        "Salam,\n{pengirim}"),
    "tpl_fu2": (
        "Selamat siang {sapaan},\n\n"
        "Ini email terakhir saya soal penawaran ini, supaya tidak mengganggu. Kalau "
        "suatu saat {nama} butuh bantuan {jasa}, silakan hubungi saya di WA "
        "{wa_pengirim}.\n\n"
        "Terima kasih, semoga usahanya lancar selalu.\n\n"
        "Salam,\n{pengirim}\n{usaha}"),
}

# Urutan tab di popup Email/WA.
URUTAN = ("pembuka", "proposal", "fu1", "fu2", "wa")

_ALASAN_CADANGAN = ("Ada beberapa hal di profil online bisnis Anda yang menurut saya "
                    "bisa membantu mendatangkan lebih banyak pelanggan.")

_KOSONG = {
    "pengirim": "[isi nama Anda]",
    "usaha": "[isi nama usaha]",
    "wa_pengirim": "[isi nomor WA]",
    "email_pengirim": "[isi email]",
}

_RE_KOSONG = re.compile(r"\[isi [^\]]+\]")


class _Isian(dict):
    """format_map yang membiarkan placeholder tak dikenal apa adanya."""
    def __missing__(self, kunci):
        return "{" + kunci + "}"


def isi_pesan(row, atur):
    """
    {pembuka, proposal, fu1, fu2, wa} → {judul, subject, catatan, isi, sisa} untuk satu lead.

    Identitas pengirim yang belum diisi muncul sebagai "[isi nomor WA]" dan
    dilaporkan di `sisa`, supaya tidak terkirim pesan dengan tanda tangan bolong.
    """
    pic = str(row.get("nama_pic") or "").strip()
    isian = _Isian({
        # nama_sapaan = nama tanpa keyword promosi / HURUF KAPITAL (kualitas.py).
        "nama": str(row.get("nama_sapaan") or row.get("nama_bisnis") or "").strip()
                or "Bapak/Ibu",
        "sapaan": pic or "Bapak/Ibu",
        "jasa": str(row.get("jasa_utama") or "").strip() or "pengembangan bisnis online",
        "alasan": str(row.get("alasan_pitch") or "").strip() or _ALASAN_CADANGAN,
        "jasa_pendukung": str(row.get("jasa_pendukung") or "").replace(" | ", ", ").strip() or "-",
    })
    for k in ("pengirim", "usaha", "wa_pengirim", "email_pengirim"):
        isian[k] = str(atur.get(k) or "").strip() or _KOSONG[k]

    def isi(tpl):
        return str(tpl or "").format_map(isian).strip()

    subjek = isi(atur.get("tpl_email_subject"))
    hasil = {
        "pembuka": {"judul": "Email Pembuka", "subject": subjek, "catatan": "",
                    "isi": isi(atur.get("tpl_email"))},
        "proposal": {"judul": "Proposal", "subject": isi(atur.get("tpl_proposal_subject")),
                     "catatan": "Penawaran lengkap siap salin: tempel ke email atau WA.",
                     "isi": isi(atur.get("tpl_proposal"))},
        "fu1": {"judul": "Follow-up H+3", "subject": "Re: " + subjek,
                "catatan": "Kirim sebagai balasan di thread email yang sama.",
                "isi": isi(atur.get("tpl_fu1"))},
        "fu2": {"judul": "Follow-up H+7", "subject": "Re: " + subjek, "catatan": "",
                "isi": isi(atur.get("tpl_fu2"))},
        "wa": {"judul": "WhatsApp", "subject": "",
               "catatan": "Untuk lead yang hanya punya nomor WA.",
               "isi": isi(atur.get("tpl_wa"))},
    }
    for b in hasil.values():
        b["sisa"] = sorted(set(_RE_KOSONG.findall(b["subject"] + "\n" + b["isi"])))
    return hasil
