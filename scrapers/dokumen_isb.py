"""
scrapers/dokumen_isb.py — Proposal & template email PT. Inti Sentosa Bersama.

Dua template Word di folder dokumen/ adalah sumber kebenarannya — kalau user
mengedit kalimatnya di Word, hasil aplikasi ikut berubah tanpa menyentuh kode:

  • "Proposal Kerja Sama ISB.docx" — diisi per lead lalu disimpan sebagai
    DOCX + PDF. Placeholder bersorot kuning (`[Jabatan]`, `[Nama Perusahaan /
    Usaha / Instansi]`, ...) diganti di tingkat RUN, jadi format huruf, kop
    surat, dan tata letak tetap utuh; sorotan kuningnya dihapus setelah terisi.
  • "Template Email ISB.docx" — dibaca bagiannya (email pembuka, follow-up
    hari ke-3 & ke-7, versi WhatsApp) untuk popup salin-tempel. Aplikasi TIDAK
    mengirim email; user mengirim sendiri.

Setelah diisi, dokumen selalu dipindai ulang. Placeholder yang tersisa berarti
templatenya berubah bentuk — proses berhenti dengan pesan jelas daripada
menghasilkan surat yang separuh terisi. Tanda klasifikasi yang disisipkan Word
lewat sensitivity label Microsoft 365 (mis. "[ OFFICIAL ]" di header/footer)
bukan placeholder — dibiarkan apa adanya.

Helper generik (penelusuran paragraf, pengganti placeholder, format tanggal &
nomor, konversi PDF) tinggal di `scrapers/docx_inti.py` dan dipakai bersama
dengan `scrapers/dokumen_buat.py`. Modul ini MENGIMPOR, tidak menyalin: `ke_pdf`
dijaga satu `threading.Lock` di tingkat modul, dan dua salinan fungsi itu berarti
dua kunci berbeda — yaitu dua Microsoft Word berjalan bersamaan dan PDF rusak.
Nama-nama di bawah di-ekspor ulang karena `komponen_routes.py` memanggilnya
sebagai `dok.<nama>`.
"""
import re
import zipfile
from pathlib import Path

from scrapers import docx_inti
from scrapers.docx_inti import (  # noqa: F401 — ekspor ulang, lihat docstring
    BULAN, _hapus_sorotan, _isi_paragraf, _KUNCI_WORD, _nama_file,
    _RE_PENANDA, _RE_SISA, _semua_paragraf, _sisa_placeholder, bentuk_nomor,
    ke_pdf, ringkas_galat_pdf, romawi, tanggal_indonesia)

BASE_DIR = Path(__file__).resolve().parent.parent
FOLDER_DOKUMEN = BASE_DIR / "dokumen"
TEMPLATE_PROPOSAL = FOLDER_DOKUMEN / "Proposal Kerja Sama ISB.docx"
TEMPLATE_EMAIL = FOLDER_DOKUMEN / "Template Email ISB.docx"
FOLDER_PROPOSAL = BASE_DIR / "output" / "proposal"


def company_profile():
    """PDF company profile di folder dokumen/ (nama file boleh berubah)."""
    kandidat = sorted(FOLDER_DOKUMEN.glob("Company Profile*.pdf"))
    return kandidat[0] if kandidat else None


def alamat_lengkap(row):
    """Alamat GMaps, ditambah kota bila alamatnya belum menyebut kota."""
    # Lead lama tersimpan dengan ikon pin GMaps ("\n...") yang di Word
    # tampil sebagai kotak di baris sendiri — ikon dibuang, spasi dirapikan.
    alamat = re.sub(r"[-]", "", str(row.get("alamat") or ""))
    alamat = re.sub(r"\s+", " ", alamat).strip()
    kota = str(row.get("kota") or "").strip()
    if kota:
        inti = re.sub(r"^(kota|kabupaten|kab\.)\s+", "", kota, flags=re.I).lower()
        if inti and inti not in alamat.lower():
            alamat = f"{alamat}, {kota}" if alamat else kota
    return alamat or "-"


# ─── Pengisian dokumen Word ───────────────────────────────────────────────────

def isi_proposal(row, atur, nomor, tgl, tujuan_docx):
    """Tulis proposal terisi ke `tujuan_docx`. Raise ValueError bila template rusak."""
    import docx

    if not TEMPLATE_PROPOSAL.exists():
        raise ValueError(f"Template tidak ditemukan: {TEMPLATE_PROPOSAL.name}")
    doc = docx.Document(str(TEMPLATE_PROPOSAL))
    nama = str(row.get("nama_bisnis") or "").strip()
    if not nama:
        raise ValueError("Lead ini tidak punya nama bisnis.")
    ganti = {
        "[tanggal bulan tahun]": tanggal_indonesia(tgl),
        "[Nama Perusahaan / Usaha / Instansi]": nama,
        "[Alamat lengkap, Kota]": alamat_lengkap(row),
        "[1x24 jam kerja]": atur.get("sla") or "1x24 jam kerja",
        "[Jabatan]": atur.get("jabatan") or "",
    }
    kota_surat = (atur.get("kota_surat") or "").strip()
    pic = str(row.get("nama_pic") or "").strip()

    for p in _semua_paragraf(doc):
        # Kota surat ("Jakarta, [tanggal]") mengikuti pengaturan.
        if kota_surat and "[tanggal bulan tahun]" in p.text and p.runs:
            awal = p.runs[0]
            if awal.text.startswith("Jakarta") and kota_surat != "Jakarta":
                awal.text = awal.text.replace("Jakarta", kota_surat, 1)
        _isi_paragraf(p, ganti, nomor=nomor, pic=pic)

    sisa = sorted(set().union(*(_sisa_placeholder(p.text) for p in _semua_paragraf(doc))))
    if sisa:
        raise ValueError("Template proposal berubah — placeholder ini tidak terisi: "
                         + ", ".join(sisa))
    tujuan_docx.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(tujuan_docx))
    return tujuan_docx


def folder_hari(tgl):
    """Folder harian proposal ISB.

    Tanda tangan satu argumen dipertahankan untuk `komponen_routes.py`; versi
    umum yang menerima akar folder ada di `docx_inti.folder_hari`.
    """
    return docx_inti.folder_hari(FOLDER_PROPOSAL, tgl)


def nama_berkas(urut, row, tgl):
    return f"{urut:03d} - {_nama_file(row.get('nama_bisnis'))}"


# ─── Template email ───────────────────────────────────────────────────────────

BAGIAN_EMAIL = (
    ("pembuka", "email pembuka", "Email Pembuka"),
    ("fu1", "follow-up pertama", "Follow-up H+3"),
    ("fu2", "follow-up terakhir", "Follow-up H+7"),
    ("wa", "versi whatsapp", "WhatsApp"),
)

_cache_email = {"mtime": None, "data": None}


def _judul_bagian(p):
    r = p.runs[0] if p.runs else None
    return bool(r is not None and r.bold and r.font.size is not None
                and r.font.size.pt >= 13 and p.text.strip())


def _teks_sel(sel):
    """Paragraf dalam kotak isi email → teks biasa, mengikuti jarak paragrafnya."""
    potong = []
    for p in sel.paragraphs:
        t = p.text.rstrip()
        rapat = p.paragraph_format.space_after == 0
        if not t:
            if potong and potong[-1] == "\n":
                potong[-1] = "\n\n"
            continue
        if potong and potong[-1] not in ("\n", "\n\n"):
            potong.append("\n\n")
        potong.append(t)
        potong.append("\n" if rapat else "\n\n")
    return "".join(potong).strip()


def baca_template_email():
    """
    {kunci: {judul, subject, catatan, isi}} dari Template Email ISB.docx.
    Di-cache per waktu ubah file, jadi edit di Word langsung terbaca.
    """
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    if not TEMPLATE_EMAIL.exists():
        raise ValueError(f"Template tidak ditemukan: {TEMPLATE_EMAIL.name}")
    mtime = TEMPLATE_EMAIL.stat().st_mtime
    if _cache_email["mtime"] == mtime:
        return _cache_email["data"]

    doc = docx.Document(str(TEMPLATE_EMAIL))
    hasil, aktif = {}, None
    for el in doc.element.body.iterchildren():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(el, doc)
            if _judul_bagian(p):
                judul = p.text.strip().lower()
                aktif = next((k for k, awalan, _ in BAGIAN_EMAIL
                              if judul.startswith(awalan)), None)
                if aktif:
                    label = next(lbl for k, _, lbl in BAGIAN_EMAIL if k == aktif)
                    hasil[aktif] = {"judul": label, "subject": "", "catatan": "", "isi": ""}
                continue
            if aktif and p.text.strip():
                t = p.text.strip()
                if t.lower().startswith("subject:"):
                    hasil[aktif]["subject"] = t.split(":", 1)[1].strip()
                else:
                    hasil[aktif]["catatan"] = (hasil[aktif]["catatan"] + " " + t).strip()
        elif tag == "tbl" and aktif:
            t = Table(el, doc)
            isi = "\n\n".join(_teks_sel(sel) for baris in t.rows for sel in baris.cells
                              if sel.text.strip())
            hasil[aktif]["isi"] = (hasil[aktif]["isi"] + "\n\n" + isi).strip()

    # Follow-up dikirim sebagai balasan di thread email pembuka.
    subjek_pembuka = (hasil.get("pembuka") or {}).get("subject", "")
    for k in ("fu1", "fu2"):
        if k in hasil and not hasil[k]["subject"] and subjek_pembuka:
            hasil[k]["subject"] = "Re: " + subjek_pembuka

    kurang = [lbl for k, _, lbl in BAGIAN_EMAIL if not (hasil.get(k) or {}).get("isi")]
    if kurang:
        raise ValueError("Bagian template email tidak ditemukan: " + ", ".join(kurang)
                         + ". Pastikan judul bagiannya masih tebal dan isinya di dalam kotak.")
    _cache_email.update(mtime=mtime, data=hasil)
    return hasil


def isi_template_email(row, atur):
    """Semua bagian template email yang sudah terisi untuk satu lead."""
    nama = str(row.get("nama_bisnis") or "").strip() or "Bapak/Ibu"
    pic = str(row.get("nama_pic") or "").strip()
    ganti = {
        "{{nama_bisnis}}": nama,
        "[Jabatan]": atur.get("jabatan") or "",
        "[1x24 jam kerja]": atur.get("sla") or "1x24 jam kerja",
    }
    if pic:
        ganti["Bapak/Ibu Pimpinan"] = pic

    def isi(teks):
        for lama, baru in ganti.items():
            teks = teks.replace(lama, baru)
        return teks

    hasil = {}
    for kunci, bagian in baca_template_email().items():
        b = {k: isi(v) for k, v in bagian.items()}
        b["sisa"] = sorted(_sisa_placeholder(b["subject"]) | _sisa_placeholder(b["isi"]))
        hasil[kunci] = b
    return hasil


def cek_template():
    """Daftar masalah pada kedua template (kosong = siap dipakai)."""
    masalah = []
    for f in (TEMPLATE_PROPOSAL, TEMPLATE_EMAIL):
        if not f.exists():
            masalah.append(f"{f.name} tidak ada di folder dokumen/")
        elif not zipfile.is_zipfile(f):
            masalah.append(f"{f.name} bukan file .docx yang valid")
    if not masalah:
        try:
            baca_template_email()
        except ValueError as e:
            masalah.append(str(e))
    return masalah
