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
menghasilkan surat yang separuh terisi.
"""
import re
import threading
import zipfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
FOLDER_DOKUMEN = BASE_DIR / "dokumen"
TEMPLATE_PROPOSAL = FOLDER_DOKUMEN / "Proposal Kerja Sama ISB.docx"
TEMPLATE_EMAIL = FOLDER_DOKUMEN / "Template Email ISB.docx"
FOLDER_PROPOSAL = BASE_DIR / "output" / "proposal"

BULAN = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
         "Agustus", "September", "Oktober", "November", "Desember"]

_RE_SISA = re.compile(r"\[[^\]\n]{2,60}\]|\{\{[^}]+\}\}")


def company_profile():
    """PDF company profile di folder dokumen/ (nama file boleh berubah)."""
    kandidat = sorted(FOLDER_DOKUMEN.glob("Company Profile*.pdf"))
    return kandidat[0] if kandidat else None


# ─── Format tanggal & nomor ───────────────────────────────────────────────────

def romawi(n):
    angka = [(10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    hasil = ""
    for nilai, huruf in angka:
        while n >= nilai:
            hasil += huruf
            n -= nilai
    return hasil


def tanggal_indonesia(tgl):
    return f"{tgl.day} {BULAN[tgl.month - 1]} {tgl.year}"


def bentuk_nomor(pola, urut, tgl):
    """Teks nomor surat dari pola pengaturan, mis. 001/ISB/SALES/26/IX/2026."""
    try:
        return pola.format(urut=urut, hari=tgl.day, bulan=tgl.month,
                           tahun=tgl.year, romawi=romawi(tgl.month))
    except (KeyError, IndexError, ValueError) as e:
        raise ValueError(f"Pola nomor surat tidak valid ({pola!r}): {e}") from None


def alamat_lengkap(row):
    """Alamat GMaps, ditambah kota bila alamatnya belum menyebut kota."""
    alamat = str(row.get("alamat") or "").strip()
    kota = str(row.get("kota") or "").strip()
    if kota:
        inti = re.sub(r"^(kota|kabupaten|kab\.)\s+", "", kota, flags=re.I).lower()
        if inti and inti not in alamat.lower():
            alamat = f"{alamat}, {kota}" if alamat else kota
    return alamat or "-"


def _nama_file(teks, batas=60):
    bersih = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", str(teks or "")).strip(" .")
    return re.sub(r"\s+", " ", bersih)[:batas].strip() or "Lead"


# ─── Pengisian dokumen Word ───────────────────────────────────────────────────

def _semua_paragraf(doc):
    """Paragraf badan, tabel (bersarang), header, dan footer."""
    def dari(wadah):
        for p in wadah.paragraphs:
            yield p
        for t in getattr(wadah, "tables", []):
            for baris in t.rows:
                for sel in baris.cells:
                    yield from dari(sel)
    yield from dari(doc)
    for s in doc.sections:
        for bagian in (s.header, s.first_page_header, s.even_page_header,
                       s.footer, s.first_page_footer, s.even_page_footer):
            if bagian is not None and not bagian.is_linked_to_previous:
                yield from dari(bagian)


def _hapus_sorotan(run):
    try:
        run.font.highlight_color = None
    except Exception:
        pass


def _isi_paragraf(p, ganti, nomor=None, pic=None):
    """Ganti placeholder di satu paragraf. Return True bila ada yang berubah."""
    runs = p.runs
    berubah = False

    # Baris "Nomor: [no. urut]/ISB/[bulan romawi]/2026" diganti utuh oleh nomor
    # dari pola pengaturan: sisa run di belakangnya dikosongkan.
    if nomor is not None and "[no. urut]" in p.text:
        for i, r in enumerate(runs):
            if "[no. urut]" in r.text:
                r.text = r.text.split("[no. urut]")[0] + nomor
                _hapus_sorotan(r)
                for r2 in runs[i + 1:]:
                    r2.text = ""
                return True

    for r in runs:
        teks = r.text
        for lama, baru in ganti.items():
            if lama in teks:
                teks = teks.replace(lama, baru)
        if teks != r.text:
            r.text = teks
            _hapus_sorotan(r)
            berubah = True

    if pic and p.text.strip() == "Bapak/Ibu Pimpinan":
        for r in runs:
            if "Bapak/Ibu Pimpinan" in r.text:
                r.text = r.text.replace("Bapak/Ibu Pimpinan", pic)
                berubah = True

    # Cadangan: placeholder yang terpecah ke beberapa run (terjadi bila template
    # diedit di Word). Teksnya digabung ke run pertama — formatnya ikut run itu.
    if runs and any(k in p.text for k in ganti):
        teks = p.text
        for lama, baru in ganti.items():
            teks = teks.replace(lama, baru)
        runs[0].text = teks
        _hapus_sorotan(runs[0])
        for r in runs[1:]:
            r.text = ""
        berubah = True
    return berubah


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

    sisa = sorted({m.group() for p in _semua_paragraf(doc)
                   for m in _RE_SISA.finditer(p.text)})
    if sisa:
        raise ValueError("Template proposal berubah — placeholder ini tidak terisi: "
                         + ", ".join(sisa))
    tujuan_docx.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(tujuan_docx))
    return tujuan_docx


# ─── Konversi PDF lewat Microsoft Word ────────────────────────────────────────

_KUNCI_WORD = threading.Lock()


def ke_pdf(pasangan):
    """
    Konversi [(docx, pdf), ...] memakai satu instance Microsoft Word.

    Word COM tidak aman dipakai paralel, jadi dijaga satu kunci global. Tiap file
    diputuskan sendiri: PDF lama yang sedang terbuka di pembaca PDF (terkunci)
    hanya menggagalkan file itu, bukan seluruh batch.

    Return {docx: None | pesan galat}. DOCX selalu tetap ada walau PDF gagal.
    """
    hasil = {asal: None for asal, _ in pasangan}
    if not pasangan:
        return hasil
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return {a: "pywin32 belum terpasang (pip install pywin32)" for a in hasil}
    with _KUNCI_WORD:
        pythoncom.CoInitialize()
        word = None
        try:
            word = win32com.client.DispatchEx("Word.Application")
            word.Visible = False
            word.DisplayAlerts = 0
            for asal, tujuan in pasangan:
                tujuan = Path(tujuan).resolve()
                try:
                    if tujuan.exists():
                        # Gagal di sini = file sedang dibuka aplikasi lain.
                        tujuan.unlink()
                    d = word.Documents.Open(str(Path(asal).resolve()), ReadOnly=True,
                                            AddToRecentFiles=False, Visible=False)
                    try:
                        d.SaveAs2(str(tujuan), FileFormat=17)  # wdFormatPDF
                    finally:
                        d.Close(False)
                except PermissionError:
                    hasil[asal] = (f"{tujuan.name} sedang dibuka aplikasi lain — tutup "
                                   f"dulu lalu buat ulang")
                except Exception as e:
                    hasil[asal] = f"{type(e).__name__}: {e}"
            return hasil
        except Exception as e:
            return {a: (v or f"Microsoft Word tidak bisa dijalankan ({type(e).__name__}: {e})")
                    for a, v in hasil.items()}
        finally:
            if word is not None:
                try:
                    word.Quit()
                except Exception:
                    pass
            pythoncom.CoUninitialize()


def ringkas_galat_pdf(hasil):
    """Satu kalimat peringatan dari hasil ke_pdf, atau None bila semua berhasil."""
    galat = [f"{Path(a).stem}: {g}" for a, g in hasil.items() if g]
    if not galat:
        return None
    return ("PDF gagal dibuat untuk " + "; ".join(galat[:5])
            + (f" (+{len(galat) - 5} lainnya)" if len(galat) > 5 else "")
            + ". File DOCX-nya tetap tersedia.")


def folder_hari(tgl):
    return FOLDER_PROPOSAL / tgl.strftime("%Y-%m-%d")


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
        b["sisa"] = sorted({m.group() for v in (b["subject"], b["isi"])
                            for m in _RE_SISA.finditer(v)})
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
