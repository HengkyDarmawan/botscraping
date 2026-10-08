"""
scrapers/dokumen_buat.py — Penyusun badan dokumen bisnis di atas kop NXTG.

Berbeda dari `dokumen_isb.py` yang mengganti placeholder di template berisi,
`dokumen/template NXTG.docx` hanyalah SHELL: badannya satu paragraf kosong, nol
tabel, nol placeholder, dan seluruh isinya ada di `word/header1.xml`. Jadi
seluruh badan surat dirakit di sini, sementara kop surat, ukuran A4, margin
1440 twip, dan pengulangan kop di setiap halaman tetap diwarisi dari `sectPr`
template.

Menambah jenis dokumen = menambah satu entri di `JENIS`, bukan menulis kode
baru: setiap blok adalah fungsi di `BLOK` dengan tanda tangan yang sama
`(doc, d, k)`, dan jenis hanya menyusun urutan bloknya.

Seluruh kalimat baku ada di `TEKS_BAWAAN` dan disimpan di tabel `pengaturan`
(`dokumen_db`), jadi user mengubah bunyi suratnya lewat UI tanpa menyentuh kode
— pola yang sama dengan `scrapers/pesan_web.py`.

Dua hal yang gampang salah dan sengaja dijaga di sini:

  • Uang selalu bilangan bulat, dan setiap angka yang dicetak diambil dari
    hasil `hitung()` yang tersimpan — tidak pernah dihitung ulang saat render.
  • `_sisa_placeholder()` dari docx_inti TIDAK dipakai pada dokumen hasil
    susunan. Di sini tidak ada placeholder, tapi deskripsi barang dari
    pelanggan seperti 'Monitor LG 24" [refurbished]' akan terbaca sebagai
    placeholder dan membatalkan dokumen yang sebenarnya sah.
"""
from datetime import date, datetime, timedelta
from pathlib import Path

from scrapers import docx_inti as inti
from scrapers.docx_inti import (PT_ISI, PT_JUDUL, PT_KECIL, RATA_KANAN,
                                RATA_KIRI, RATA_PENUH, RATA_TENGAH)

TEMPLATE = inti.FOLDER_DOKUMEN / "template NXTG.docx"
FOLDER_KELUARAN = inti.BASE_DIR / "output" / "dokumen"

# ─── Lebar kolom (twips) ──────────────────────────────────────────────────────
# Lebar kerja A4 margin 1 inci = 11906 - 2*1440 = 9026 twip. Setiap set harus
# berjumlah tepat 9026; lebar diukur dari lebar huruf Times New Roman 11pt
# ditambah 216 twip margin sel.
# Kolom Satuan 900 twip, bukan 822: judul "Satuan" ditulis TEBAL dan huruf
# tebal lebih lebar ~4% dari lebar yang diukur dari huruf biasa — di 822 twip
# judulnya terpotong jadi "Satua/n" dua baris.
KOLOM_HARGA = (510, 3419, 740, 900, 1644, 1813)   # No|Deskripsi|Qty|Satuan|Harga|Jumlah
KOLOM_BARANG = (510, 4022, 740, 900, 2854)        # No|Nama Barang|Qty|Satuan|Keterangan
KOLOM_LABEL = (1300, 280, 7446)                   # Label | : | nilai
# Label kwitansi jauh lebih panjang ("Sudah terima dari", "Untuk pembayaran")
# dan terpotong dua baris di 1300 twip.
KOLOM_LABEL_PANJANG = (2200, 280, 6546)
KOLOM_TTD2 = (4513, 4513)
KOLOM_TTD3 = (3008, 3009, 3009)

# Ruang tanda tangan: cukup untuk tanda tangan + stempel, tapi tidak boleh
# berlebihan. Blok tanda tangan dijaga tidak terpecah, jadi ruang yang terlalu
# tinggi membuat seluruh bloknya pindah sendirian ke halaman berikutnya.
RUANG_TTD = 42          # pt (~1,5 cm)
TINGGI_TTD = 1100       # twip (~1,9 cm), tinggi minimal baris tanda tangan
BATAS_METERAI = 5_000_000

# ─── PPN ──────────────────────────────────────────────────────────────────────
# PMK 131/2024: barang & jasa non-mewah tetap 11% efektif lewat DPP Nilai Lain
# 11/12 lalu dikali 12%. Barang mewah 12% atas DPP penuh. Penjual non-PKP tidak
# boleh memungut PPN — barisnya DIHILANGKAN, bukan dicetak "Rp 0", karena
# mencantumkan PPN tanpa status PKP adalah masalah pajak.
MODE_PPN = {
    "tidak":    {"label": None, "dpp_lain": False, "tarif": 0},
    "nonmewah": {"label": "PPN 12% atas DPP Nilai Lain (11/12)",
                 "dpp_lain": True, "tarif": 12},
    "mewah":    {"label": "PPN 12%", "dpp_lain": False, "tarif": 12},
}
LABEL_PPN = {"tidak": "Tanpa PPN (non-PKP)",
             "nonmewah": "PPN efektif 11% (barang/jasa non-mewah)",
             "mewah": "PPN 12% (barang mewah)"}

# ─── Kalimat baku (semua bisa diubah user lewat Pengaturan) ──────────────────

TEKS_BAWAAN = {
    "teks_salam": "Dengan hormat,",
    "teks_hormat": "Hormat kami,",
    "teks_pembuka_referensi":
        "Menindaklanjuti permintaan penawaran Bapak/Ibu melalui {referensi}"
        "{tanggal_referensi}, berikut kami sampaikan penawaran harga untuk "
        "kebutuhan {perihal}.",
    "teks_pembuka_polos":
        "Sehubungan dengan kebutuhan {perihal} di {pelanggan}, berikut kami "
        "sampaikan penawaran harga sebagai bahan pertimbangan Bapak/Ibu.",
    "teks_pembuka_proposal":
        "Perkenalkan, kami dari {usaha}. {profil} Melalui surat ini kami "
        "bermaksud menawarkan kerja sama dengan {pelanggan}.",
    "teks_pembuka_pengantar":
        "Bersama surat ini kami sampaikan berkas terlampir untuk menjadi bahan "
        "pertimbangan Bapak/Ibu.",
    # Syarat & ketentuan
    "teks_harga_berlaku":
        "Harga di atas berlaku sampai dengan {berlaku}. Setelah tanggal "
        "tersebut harga dapat berubah mengikuti kondisi pasar dan ketersediaan "
        "stok.",
    "teks_harga_ppn_termasuk": "Harga dalam Rupiah dan sudah termasuk PPN.",
    "teks_harga_ppn_belum":
        "Harga dalam Rupiah dan belum termasuk PPN. PPN ditambahkan pada baris "
        "tersendiri di rincian di atas.",
    "teks_harga_nonpkp":
        "Harga dalam Rupiah. Pajak yang timbul menjadi tanggungan masing-masing "
        "pihak sesuai ketentuan yang berlaku.",
    "teks_harga_jumlah":
        "Harga berlaku untuk pembelian sesuai jumlah pada tabel di atas. "
        "Perubahan jumlah dapat mengubah harga satuan.",
    "teks_pembayaran":
        "Pembayaran {termin}. Pembayaran ditujukan ke rekening {bank} nomor "
        "{rekening} atas nama {atas_nama}. Kami tidak menerima pembayaran ke "
        "rekening pribadi.",
    "teks_waktu":
        "Waktu pengerjaan dan pengiriman {waktu} setelah pesanan dan pembayaran "
        "tahap pertama kami terima.",
    "teks_scope":
        "Pekerjaan di luar rincian di atas, termasuk permintaan tambahan "
        "setelah pekerjaan berjalan, kami hitung sebagai biaya terpisah dan "
        "dibicarakan terlebih dahulu.",
    "teks_garansi": "Garansi {garansi}. Klaim garansi dapat diurus melalui kami.",
    "teks_stok":
        "Ketersediaan stok mengikuti kondisi pada saat pemesanan. Untuk barang "
        "indent, waktu tunggu kami informasikan sebelum pesanan diproses.",
    "teks_dasar_pesanan":
        "Pesanan kami proses setelah menerima konfirmasi tertulis berupa "
        "Purchase Order, persetujuan melalui email, atau penawaran ini yang "
        "telah ditandatangani.",
    "teks_ongkir_belum":
        "Ongkos kirim belum termasuk dan dihitung sesuai alamat pengiriman.",
    # Penutup
    "teks_penutup_cta":
        "Apabila ada spesifikasi atau jumlah yang ingin disesuaikan, silakan "
        "sampaikan dan kami kirimkan penawaran revisi paling lambat {sla}.",
    "teks_penutup":
        "Demikian kami sampaikan. Atas perhatian Bapak/Ibu, kami ucapkan "
        "terima kasih.",
    "teks_kontak_judul":
        "Untuk informasi lebih lanjut, Bapak/Ibu dapat menghubungi:",
    "teks_persetujuan":
        "Apabila penawaran ini disetujui, Bapak/Ibu dapat menandatangani kolom "
        "di bawah ini dan mengirimkannya kembali kepada kami sebagai dasar "
        "pemesanan.",
    # Per jenis
    "teks_invoice_bayar":
        "Mohon pembayaran dilakukan sebelum {jatuh_tempo} ke rekening tersebut "
        "di atas. Setelah pembayaran, mohon kirimkan bukti transfer kepada kami "
        "agar kwitansi dapat kami terbitkan.",
    "teks_sj_periksa":
        "Mohon diperiksa dan ditandatangani sebagai bukti barang telah diterima "
        "dalam keadaan baik dan lengkap. Keluhan mengenai jumlah atau kondisi "
        "barang kami terima paling lambat 2x24 jam setelah barang diterima.",
    "teks_bast_pembuka":
        "Pada hari ini, {hari} tanggal {tanggal}, yang bertanda tangan di bawah "
        "ini:",
    "teks_bast_serah":
        "Pihak Pertama menyerahkan dan Pihak Kedua menerima barang/pekerjaan "
        "sebagaimana tercantum dalam daftar di bawah ini{acuan}, dalam keadaan "
        "baik, lengkap, dan dapat digunakan.",
    "teks_bast_rangkap":
        "Berita acara ini dibuat dalam 2 (dua) rangkap bermaterai cukup, "
        "masing-masing pihak memegang satu rangkap dengan kekuatan hukum yang "
        "sama.",
    "teks_kwitansi_peruntukan": "Pembayaran {peruntukan}",
}

# ─── Jenis dokumen ────────────────────────────────────────────────────────────
#
# `tabel`  : harga | barang | tanpa
# `lingkup`: bulan | tahun — lingkup nomor urut. Invoice, kwitansi, surat jalan,
#            dan BAST WAJIB per tahun: nomor yang terulang adalah cacat
#            pembukuan, bukan sekadar kosmetik.
# `wajib`  : kolom yang harus terisi, di luar yang wajib untuk semua jenis.

WAJIB_UMUM = ("tanggal", "pelanggan_nama")

JENIS = {
    "penawaran": {
        "label": "Surat Penawaran Harga",
        "judul": None,
        "kode": "SPH",
        "lingkup": "bulan",
        "pola": "{urut:03d}/SPH-NXTG/{romawi}/{tahun}",
        "hal": "Penawaran Harga {perihal}",
        "tabel": "harga",
        "wajib": ("perihal", "item"),
        "blok": ("tanggal_kota", "kepala_surat", "kepada", "salam", "pembuka",
                 "tabel_item", "terbilang", "syarat", "penutup", "kontak",
                 "tanda_tangan", "persetujuan"),
    },
    "proposal": {
        "label": "Proposal Penawaran Kerja Sama",
        "judul": None,
        "kode": "PRP",
        "lingkup": "bulan",
        "pola": "{urut:03d}/PRP-NXTG/{romawi}/{tahun}",
        "hal": "Penawaran Kerja Sama {perihal}",
        "tabel": "tanpa",
        "wajib": ("perihal",),
        "blok": ("tanggal_kota", "kepala_surat", "kepada", "salam", "pembuka",
                 "catatan", "syarat", "penutup", "kontak", "tanda_tangan"),
    },
    "pengantar": {
        "label": "Surat Pengantar / Perkenalan",
        "judul": None,
        "kode": "SP",
        "lingkup": "bulan",
        "pola": "{urut:03d}/SP-NXTG/{romawi}/{tahun}",
        "hal": "{perihal}",
        "tabel": "tanpa",
        "wajib": ("perihal",),
        "blok": ("tanggal_kota", "kepala_surat", "kepada", "salam", "pembuka",
                 "catatan", "penutup", "kontak", "tanda_tangan"),
    },
    "invoice": {
        "label": "Invoice / Surat Tagihan",
        "judul": "INVOICE",
        "kode": "INV",
        "lingkup": "tahun",
        "pola": "INV/{tahun}/{urut:04d}",
        "hal": "{perihal}",
        "tabel": "harga",
        "wajib": ("perihal", "item", "jatuh_tempo"),
        "blok": ("judul", "kepala_dua_kolom", "tabel_item", "terbilang",
                 "rincian_bayar", "catatan", "tanda_tangan"),
    },
    "surat_jalan": {
        "label": "Surat Jalan (Delivery Order)",
        "judul": "SURAT JALAN",
        "kode": "SJ",
        "lingkup": "tahun",
        "pola": "SJ/{tahun}/{urut:04d}",
        "hal": "{perihal}",
        "tabel": "barang",
        "wajib": ("item", "alamat_kirim"),
        "blok": ("judul", "kepala_kirim", "tabel_item", "catatan_kirim",
                 "catatan", "ttd3"),
    },
    "bast": {
        "label": "Berita Acara Serah Terima",
        "judul": "BERITA ACARA SERAH TERIMA",
        "kode": "BAST",
        "lingkup": "tahun",
        "pola": "{urut:03d}/BAST-NXTG/{romawi}/{tahun}",
        "hal": "{perihal}",
        "tabel": "barang",
        "wajib": ("item", "penerima_nama", "penerima_jabatan"),
        "blok": ("judul", "nomor_tengah", "pembuka_bast", "pihak", "serah_bast",
                 "tabel_item", "rangkap_bast", "catatan", "ttd_bast"),
    },
    "kwitansi": {
        "label": "Kwitansi / Tanda Terima Pembayaran",
        "judul": "KWITANSI",
        "kode": "KW",
        "lingkup": "tahun",
        "pola": "KW/{tahun}/{urut:04d}",
        "hal": "{perihal}",
        "tabel": "tanpa",
        "wajib": ("jumlah_terima", "peruntukan"),
        "blok": ("judul", "nomor_tengah", "blok_kwitansi", "meterai",
                 "catatan", "tanda_tangan"),
    },
}

# Label kolom untuk pesan galat — disebut namanya, bukan "ada kolom yang kosong".
LABEL_KOLOM = {
    "tanggal": "Tanggal",
    "pelanggan_nama": "Nama pelanggan / instansi",
    "perihal": "Perihal / Hal",
    "item": "Rincian item (minimal satu baris)",
    "jatuh_tempo": "Tanggal jatuh tempo",
    "alamat_kirim": "Alamat pengiriman",
    "penerima_nama": "Nama penerima",
    "penerima_jabatan": "Jabatan penerima",
    "jumlah_terima": "Jumlah yang diterima",
    "peruntukan": "Untuk pembayaran",
}

WAJIB_PENGATURAN = {
    "nama_usaha": "Nama badan usaha",
    "alamat": "Alamat lengkap",
    "kota_surat": "Kota penerbitan surat",
    "telepon": "Telepon / WA kantor",
    "email": "Email kantor",
    "status_pkp": "Status PKP / non-PKP",
    "penanda_nama": "Nama penanda tangan",
    "penanda_jabatan": "Jabatan penanda tangan",
}
# Wajib hanya untuk jenis tertentu.
WAJIB_PENGATURAN_BAYAR = {
    "bank": "Nama bank",
    "rekening": "Nomor rekening",
    "rekening_atas_nama": "Rekening atas nama",
}
JENIS_BUTUH_REKENING = ("invoice", "kwitansi")


# ─── Hitung uang ──────────────────────────────────────────────────────────────

def hitung(d):
    """
    Semua angka uang satu dokumen, bilangan bulat, dalam URUTAN TETAP.

    Urutan pembulatan dikunci di sini supaya angka yang tercetak selalu
    menjumlah: tiap angka di PDF adalah salah satu bilangan bulat di dict ini.
    """
    baris = []
    for it in d.get("item") or []:
        harga = inti.ke_int(it.get("harga"), "Harga satuan")
        qty = inti.ke_int(it.get("qty"), "Jumlah (qty)")
        bruto = harga * qty
        persen = inti.ke_int(it.get("diskon_persen") or 0, "Diskon item")
        diskon = (inti.bagi_bulat(bruto * persen, 100) if persen
                  else inti.ke_int(it.get("diskon_nilai") or 0, "Diskon item"))
        baris.append({**it, "harga": harga, "qty": qty,
                      "diskon_persen": persen, "diskon_nilai": diskon,
                      "jumlah": bruto - diskon})

    subtotal = sum(b["jumlah"] for b in baris)
    persen_global = inti.ke_int(d.get("diskon_persen") or 0, "Diskon")
    diskon_global = inti.bagi_bulat(subtotal * persen_global, 100) if persen_global \
        else inti.ke_int(d.get("diskon_nilai") or 0, "Diskon")
    kirim = inti.ke_int(d.get("biaya_kirim") or 0, "Ongkos kirim")
    dpp = subtotal - diskon_global + kirim

    mode = d.get("ppn_mode") or "tidak"
    if mode not in MODE_PPN:
        raise ValueError(f"Mode PPN tidak dikenal: {mode!r}")
    spek = MODE_PPN[mode]
    tarif = spek["tarif"]
    if tarif == 0:
        dpp_lain, ppn = 0, 0
    elif spek["dpp_lain"]:
        dpp_lain = inti.bagi_bulat(dpp * 11, 12)
        ppn = inti.bagi_bulat(dpp_lain * tarif, 100)
    else:
        dpp_lain = dpp
        ppn = inti.bagi_bulat(dpp * tarif, 100)

    total = dpp + ppn
    uang_muka = min(max(inti.ke_int(d.get("uang_muka") or 0, "Uang muka"), 0), total)
    return {
        "baris": baris, "subtotal": subtotal, "diskon_persen": persen_global,
        "diskon_global": diskon_global, "biaya_kirim": kirim, "dpp": dpp,
        "ppn_mode": mode, "ppn_label": spek["label"], "dpp_nilai_lain": dpp_lain,
        "ppn_tarif": tarif, "ppn": ppn, "total": total,
        "uang_muka": uang_muka, "sisa": total - uang_muka,
    }


# ─── Validasi ─────────────────────────────────────────────────────────────────

def _kosong(nilai):
    if nilai is None:
        return True
    if isinstance(nilai, (list, tuple, dict)):
        return len(nilai) == 0
    return str(nilai).strip() == ""


KUNCI_TANGGAL = ("tanggal", "jatuh_tempo", "tanggal_referensi",
                 "invoice_tanggal", "tanggal_serah")


def ke_tanggal(nilai, nama="Tanggal"):
    """'2026-10-08' / date / datetime → date. Kosong → None."""
    if nilai is None or nilai == "":
        return None
    if isinstance(nilai, datetime):
        return nilai.date()
    if isinstance(nilai, date):
        return nilai
    try:
        return datetime.strptime(str(nilai).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"{nama} bukan tanggal yang sah: {nilai!r}") from None


def kolom_wajib(jenis):
    """Daftar kunci wajib untuk `jenis`. Dipakai validasi DAN penanda * di UI."""
    spek = JENIS.get(jenis)
    if not spek:
        raise ValueError(f"Jenis dokumen tidak dikenal: {jenis!r}")
    return tuple(WAJIB_UMUM) + tuple(spek["wajib"])


def validasi(jenis, d):
    """
    Periksa kolom wajib lalu kembalikan data yang sudah dinormalkan.

    Raise ValueError yang MENYEBUT NAMA KOLOM yang kosong — pesan umum membuat
    user menebak-nebak kolom mana yang salah.
    """
    spek = JENIS.get(jenis)
    if not spek:
        raise ValueError(f"Jenis dokumen tidak dikenal: {jenis!r}")
    d = dict(d or {})
    # Tanggal dinormalkan lebih dulu: form mengirim string, skrip uji mengirim
    # date, dan seluruh blok di bawah mengandaikan objek date.
    for kunci in KUNCI_TANGGAL:
        if kunci in d:
            d[kunci] = ke_tanggal(d[kunci], LABEL_KOLOM.get(kunci, kunci))

    kurang = [LABEL_KOLOM.get(k, k) for k in kolom_wajib(jenis) if _kosong(d.get(k))]
    if kurang:
        raise ValueError("Belum diisi: " + ", ".join(kurang))

    item = []
    for i, it in enumerate(d.get("item") or [], 1):
        deskripsi = str(it.get("deskripsi") or "").strip()
        if not deskripsi:
            raise ValueError(f"Baris {i}: deskripsi belum diisi")
        qty = inti.ke_int(it.get("qty") or 0, f"Baris {i}: jumlah (qty)")
        if qty <= 0:
            raise ValueError(f"Baris {i}: jumlah (qty) harus lebih dari nol")
        baris = {
            "deskripsi": deskripsi,
            "qty": qty,
            "satuan": str(it.get("satuan") or "").strip(),
            "keterangan": str(it.get("keterangan") or "").strip(),
            "diskon_persen": inti.ke_int(it.get("diskon_persen") or 0,
                                         f"Baris {i}: diskon"),
        }
        if spek["tabel"] == "harga":
            baris["harga"] = inti.ke_int(it.get("harga"), f"Baris {i}: harga satuan")
            if baris["harga"] < 0:
                raise ValueError(f"Baris {i}: harga satuan tidak boleh minus")
        else:
            # Surat jalan & BAST beredar di gudang dan sering difotokopi pihak
            # lain — harga tidak boleh ikut, bahkan kalau dikirim dari form.
            baris["harga"] = 0
        item.append(baris)
    d["item"] = item

    if spek["tabel"] != "harga":
        d["ppn_mode"] = "tidak"
        d["diskon_persen"] = 0
        d["biaya_kirim"] = 0
    if jenis == "kwitansi":
        d["jumlah_terima"] = inti.ke_int(d.get("jumlah_terima"), "Jumlah yang diterima")
        if d["jumlah_terima"] <= 0:
            raise ValueError("Jumlah yang diterima harus lebih dari nol")
    return d


def cek_siap(atur, jenis=None):
    """Kolom Pengaturan wajib yang masih kosong (kosong = siap dipakai)."""
    wajib = dict(WAJIB_PENGATURAN)
    if jenis is None or jenis in JENIS_BUTUH_REKENING:
        wajib.update(WAJIB_PENGATURAN_BAYAR)
    if str(atur.get("status_pkp") or "").lower() == "pkp":
        wajib["npwp"] = "NPWP"
    return [label for k, label in wajib.items() if _kosong(atur.get(k))]


# ─── Helper teks ──────────────────────────────────────────────────────────────

class _Isian(dict):
    """format_map yang membiarkan placeholder tak dikenal apa adanya."""
    def __missing__(self, kunci):
        return "{" + kunci + "}"


def _teks(atur, kunci, **isian):
    pola = atur.get(kunci) or TEKS_BAWAAN.get(kunci) or ""
    return str(pola).format_map(_Isian(isian)).strip()


def _nama_sapaan(d):
    pic = str(d.get("pelanggan_pic") or "").strip()
    jab = str(d.get("pelanggan_jabatan") or "").strip()
    if not pic:
        return "Bapak/Ibu Pimpinan"
    return f"{pic} ({jab})" if jab else pic


def _tanggal_berlaku(d, atur):
    hari = inti.ke_int(atur.get("berlaku_hari") or 14, "Masa berlaku")
    return inti.tanggal_indonesia(d["tanggal"] + timedelta(days=max(hari, 1)))


def syarat_otomatis(d, atur, jenis):
    """Daftar syarat & ketentuan yang dirakit dari pengaturan + isi dokumen."""
    if d.get("syarat"):
        return [str(s).strip() for s in d["syarat"] if str(s).strip()]
    spek = JENIS[jenis]
    butir = []
    if spek["tabel"] == "harga":
        butir.append(_teks(atur, "teks_harga_berlaku", berlaku=_tanggal_berlaku(d, atur)))
        mode = d.get("ppn_mode") or "tidak"
        butir.append(_teks(atur, "teks_harga_nonpkp" if mode == "tidak"
                           else "teks_harga_ppn_belum"))
        butir.append(_teks(atur, "teks_harga_jumlah"))
    if atur.get("bank") and atur.get("rekening"):
        butir.append(_teks(atur, "teks_pembayaran",
                           termin=d.get("termin") or atur.get("termin") or "sesuai kesepakatan",
                           bank=atur.get("bank"), rekening=atur.get("rekening"),
                           atas_nama=atur.get("rekening_atas_nama") or atur.get("nama_usaha")))
    waktu = d.get("waktu_kerja") or atur.get("waktu_kerja")
    if waktu:
        butir.append(_teks(atur, "teks_waktu", waktu=waktu))
    if not inti.ke_int(d.get("biaya_kirim") or 0) and spek["tabel"] == "harga":
        butir.append(_teks(atur, "teks_ongkir_belum"))
    garansi = d.get("garansi") or atur.get("garansi")
    if garansi:
        butir.append(_teks(atur, "teks_garansi", garansi=garansi))
    butir.append(_teks(atur, "teks_stok"))
    butir.append(_teks(atur, "teks_scope"))
    butir.append(_teks(atur, "teks_dasar_pesanan"))
    # Butir tambahan yang menempel di produk katalog (mis. masa langganan SaaS).
    for it in d.get("item") or []:
        for s in it.get("syarat_bawaan") or []:
            s = str(s).strip()
            if s and s not in butir:
                butir.append(s)
    return [b for b in butir if b]


# ─── Blok penyusun badan surat ────────────────────────────────────────────────
#
# Setiap blok punya tanda tangan sama: (doc, d, k) di mana `k` berisi konteks
# turunan (jenis, spek, atur, hasil hitung, nomor). Itulah yang membuat jenis
# dokumen baru cukup menyusun ulang daftar nama blok.

def blok_tanggal_kota(doc, d, k):
    inti.paragraf(doc, f"{k['atur'].get('kota_surat') or 'Jakarta'}, "
                       f"{inti.tanggal_indonesia(d['tanggal'])}",
                  rata=RATA_KANAN, spasi_sesudah=10)


def blok_judul(doc, d, k):
    # Jarak sesudah judul tidak boleh nol: blok "Nomor/Tanggal" di bawahnya
    # adalah tabel, dan tabel yang menempel persis di bawah paragraf membuat
    # baris pertamanya terbaca sejajar dengan judul.
    inti.paragraf(doc, k["spek"]["judul"], pt=PT_JUDUL, tebal=True,
                  rata=RATA_TENGAH, spasi_sesudah=8)


def blok_nomor_tengah(doc, d, k):
    inti.paragraf(doc, f"Nomor: {k['nomor']}", rata=RATA_TENGAH, spasi_sesudah=12)


def _label_baris(doc, baris, lebar=KOLOM_LABEL, spasi_sesudah=0):
    """
    Blok "Label : nilai" sebagai tabel tanpa garis.

    Template ISB memakai karakter tab untuk ini dan hasilnya salah rata begitu
    panjang label atau nilainya berubah. Dengan tabel, kolom titik dua membuat
    semua ":" sejajar dan nilai yang panjang membungkus rapi di bawah dirinya
    sendiri, bukan kembali ke margin kiri.
    """
    t = inti.tabel(doc, lebar, garis=False, margin_sel=0)
    for label, nilai in baris:
        r = inti.baris_tabel(t)
        inti.isi_sel(r.cells[0], label)
        inti.isi_sel(r.cells[1], ":")
        inti.isi_sel(r.cells[2], nilai)
        inti.jangan_pecah(r)
    if spasi_sesudah:
        inti.paragraf(doc, spasi_sesudah=spasi_sesudah)
    return t


def blok_kepala_surat(doc, d, k):
    lampiran = d.get("lampiran") or []
    baris = [("Nomor", k["nomor"])]
    if lampiran:
        baris.append(("Lampiran", f"{len(lampiran)} ({inti.angka_ke_kata(len(lampiran))}) "
                                  f"berkas {', '.join(lampiran)}"))
    baris.append(("Hal", k["hal"]))
    _label_baris(doc, baris)
    inti.paragraf(doc, spasi_sesudah=6)


def blok_kepada(doc, d, k):
    inti.paragraf(doc, "Kepada Yth.", spasi_sesudah=0)
    inti.paragraf(doc, _nama_sapaan(d), spasi_sesudah=0)
    inti.paragraf(doc, d["pelanggan_nama"], tebal=True, spasi_sesudah=0)
    alamat = ", ".join(x for x in (str(d.get("pelanggan_alamat") or "").strip(),
                                   str(d.get("pelanggan_kota") or "").strip()) if x)
    if alamat:
        inti.paragraf(doc, alamat, spasi_sesudah=0)
    inti.paragraf(doc, spasi_sesudah=6)


def blok_kepala_dua_kolom(doc, d, k):
    """Kepala invoice: identitas tagihan di kiri, nomor & tanggal di kanan."""
    t = inti.tabel(doc, KOLOM_TTD2, garis=False, margin_sel=0)
    r = inti.baris_tabel(t)
    kiri, kanan = r.cells
    inti.isi_sel(kiri, "Kepada Yth.", tebal=True)
    for teks in (_nama_sapaan(d), d["pelanggan_nama"],
                 str(d.get("pelanggan_alamat") or "").strip(),
                 str(d.get("pelanggan_kota") or "").strip(),
                 (f"NPWP {d['pelanggan_npwp']}" if d.get("pelanggan_npwp") else "")):
        if teks:
            inti.paragraf(kiri, teks, spasi_sesudah=0)
    kanan_baris = [("Nomor", k["nomor"]),
                   ("Tanggal", inti.tanggal_indonesia(d["tanggal"]))]
    if d.get("jatuh_tempo"):
        kanan_baris.append(("Jatuh tempo", inti.tanggal_indonesia(d["jatuh_tempo"])))
    if d.get("po_nomor"):
        kanan_baris.append(("No. PO", d["po_nomor"]))
    p = inti.isi_sel(kanan, "")
    for i, (label, nilai) in enumerate(kanan_baris):
        if i:
            p = inti.paragraf(kanan, spasi_sesudah=0)
        inti.gaya_run(p.add_run(f"{label} : "), PT_ISI, tebal=True)
        inti.gaya_run(p.add_run(str(nilai)), PT_ISI)
    inti.jangan_pecah(r)
    inti.paragraf(doc, spasi_sesudah=8)


def blok_kepala_kirim(doc, d, k):
    baris = [("Nomor", k["nomor"]),
             ("Tanggal", inti.tanggal_indonesia(d["tanggal"])),
             ("Kepada", d["pelanggan_nama"]),
             ("Alamat kirim", d["alamat_kirim"])]
    for label, kunci in (("No. PO", "po_nomor"), ("Ekspedisi", "ekspedisi"),
                         ("No. kendaraan", "kendaraan"), ("Pengemudi", "pengemudi")):
        if d.get(kunci):
            baris.append((label, d[kunci]))
    _label_baris(doc, baris)
    inti.paragraf(doc, spasi_sesudah=8)


def blok_salam(doc, d, k):
    inti.paragraf(doc, _teks(k["atur"], "teks_salam"), spasi_sesudah=8)


def blok_pembuka(doc, d, k):
    atur, jenis = k["atur"], k["jenis"]
    if jenis == "proposal":
        teks = _teks(atur, "teks_pembuka_proposal", usaha=atur.get("nama_usaha"),
                     profil=atur.get("profil_usaha") or "",
                     pelanggan=d["pelanggan_nama"])
    elif jenis == "pengantar":
        teks = _teks(atur, "teks_pembuka_pengantar")
    elif d.get("referensi"):
        tgl_ref = d.get("tanggal_referensi")
        teks = _teks(atur, "teks_pembuka_referensi", referensi=d["referensi"],
                     tanggal_referensi=(f" tanggal {inti.tanggal_indonesia(tgl_ref)}"
                                        if tgl_ref else ""),
                     perihal=d.get("perihal") or "")
    else:
        teks = _teks(atur, "teks_pembuka_polos", perihal=d.get("perihal") or "",
                     pelanggan=d["pelanggan_nama"])
    inti.paragraf(doc, teks, rata=RATA_PENUH, spasi_sesudah=8)


def blok_pembuka_bast(doc, d, k):
    tgl = d["tanggal"]
    inti.paragraf(doc, _teks(k["atur"], "teks_bast_pembuka",
                             hari=inti.hari_indonesia(tgl),
                             tanggal=inti.tanggal_indonesia(tgl)),
                  rata=RATA_PENUH, spasi_sesudah=8)


def blok_pihak(doc, d, k):
    atur = k["atur"]
    _label_baris(doc, [("Nama", atur.get("penanda_nama")),
                       ("Jabatan", atur.get("penanda_jabatan")),
                       ("Perusahaan", atur.get("nama_usaha"))])
    inti.paragraf(doc, "selanjutnya disebut PIHAK PERTAMA.", spasi_sesudah=8)
    _label_baris(doc, [("Nama", d["penerima_nama"]),
                       ("Jabatan", d["penerima_jabatan"]),
                       ("Perusahaan", d["pelanggan_nama"])])
    inti.paragraf(doc, "selanjutnya disebut PIHAK KEDUA.", spasi_sesudah=8)


def blok_serah_bast(doc, d, k):
    acuan = str(d.get("acuan") or "").strip()
    inti.paragraf(doc, _teks(k["atur"], "teks_bast_serah",
                             acuan=(f", sesuai dengan {acuan}" if acuan else "")),
                  rata=RATA_PENUH, spasi_sesudah=8)


def blok_rangkap_bast(doc, d, k):
    inti.paragraf(doc, _teks(k["atur"], "teks_bast_rangkap"), rata=RATA_PENUH,
                  spasi_sesudah=10)


def _kepala_tabel(t, judul):
    r = inti.baris_tabel(t)
    for i, teks in enumerate(judul):
        inti.isi_sel(r.cells[i], teks, tebal=True, rata=RATA_TENGAH)
        inti.warnai(r.cells[i])
    inti.ulang_kepala(r)
    return r


def blok_tabel_item(doc, d, k):
    if k["spek"]["tabel"] == "barang":
        return _tabel_barang(doc, d, k)
    return _tabel_harga(doc, d, k)


def _tabel_barang(doc, d, k):
    lebar = KOLOM_BARANG
    t = inti.tabel(doc, lebar)
    _kepala_tabel(t, ("No", "Nama Barang / Pekerjaan", "Qty", "Satuan",
                      "Keterangan"))
    for i, b in enumerate(k["total"]["baris"], 1):
        r = inti.baris_tabel(t)
        inti.isi_sel(r.cells[0], i, rata=RATA_TENGAH)
        inti.isi_sel(r.cells[1], b["deskripsi"])
        inti.isi_sel(r.cells[2], inti.angka(b["qty"]), rata=RATA_TENGAH)
        inti.isi_sel(r.cells[3], b["satuan"] or "-", rata=RATA_TENGAH)
        inti.isi_sel(r.cells[4], b.get("keterangan") or "")
        inti.jangan_pecah(r)
    inti.paragraf(doc, spasi_sesudah=8)
    return t


def _baris_rekap(t, lebar, label, nilai, tebal=False, atas=None):
    """
    Baris rekap di DALAM tabel item lewat gridSpan.

    Hanya dengan begitu tepi kanan kolom "Jumlah" lurus dengan angka totalnya.
    Sel digabung SEBELUM diisi teks: merge() menggabungkan paragraf yang sudah
    ada, jadi mengisi lebih dulu menghasilkan teks ganda.
    """
    r = inti.baris_tabel(t)
    gab = r.cells[0].merge(r.cells[len(lebar) - 2])
    inti.isi_sel(gab, label, tebal=tebal, rata=RATA_KANAN)
    inti.sel_uang(r.cells[-1], nilai, lebar[-1], tebal=tebal)
    if atas:
        inti.garis_sel(gab, top=atas)
        inti.garis_sel(r.cells[-1], top=atas)
    inti.jangan_pecah(r)
    return r


def _tabel_harga(doc, d, k):
    lebar = KOLOM_HARGA
    tot = k["total"]
    t = inti.tabel(doc, lebar)
    _kepala_tabel(t, ("No", "Deskripsi / Spesifikasi", "Qty", "Satuan",
                      "Harga Satuan", "Jumlah"))
    for i, b in enumerate(tot["baris"], 1):
        r = inti.baris_tabel(t)
        inti.isi_sel(r.cells[0], i, rata=RATA_TENGAH)
        p = inti.isi_sel(r.cells[1], b["deskripsi"])
        if b["diskon_persen"]:
            inti.gaya_run(p.add_run(f"  (diskon {b['diskon_persen']}%)"),
                          PT_KECIL, miring=True)
        inti.isi_sel(r.cells[2], inti.angka(b["qty"]), rata=RATA_TENGAH)
        inti.isi_sel(r.cells[3], b["satuan"] or "-", rata=RATA_TENGAH)
        inti.sel_uang(r.cells[4], b["harga"], lebar[4])
        inti.sel_uang(r.cells[5], b["jumlah"], lebar[5])
        inti.jangan_pecah(r)

    rekap = [("Subtotal", tot["subtotal"], False, "single")]
    if tot["diskon_global"]:
        label = ("Diskon" + (f" {tot['diskon_persen']}%" if tot["diskon_persen"] else ""))
        rekap.append((label, -tot["diskon_global"], False, None))
    if tot["biaya_kirim"]:
        rekap.append(("Ongkos kirim", tot["biaya_kirim"], False, None))
    if tot["dpp"] != tot["subtotal"]:
        # Tanpa baris ini pembaca tidak bisa memeriksa sendiri bahwa
        # TOTAL = (Subtotal - Diskon + Ongkir) + PPN; angka dasar pengenaan
        # pajaknya hilang di antara diskon dan PPN.
        rekap.append(("Dasar Pengenaan Pajak" if tot["ppn_tarif"] else "Jumlah",
                      tot["dpp"], False, "single"))
    if tot["ppn_tarif"]:
        if tot["dpp_nilai_lain"] != tot["dpp"]:
            rekap.append(("DPP Nilai Lain (11/12)", tot["dpp_nilai_lain"], False, None))
        rekap.append((tot["ppn_label"], tot["ppn"], False, None))
    rekap.append(("TOTAL", tot["total"], True, "double"))
    if tot["uang_muka"]:
        rekap.append(("Uang muka diterima", -tot["uang_muka"], False, None))
        rekap.append(("SISA TAGIHAN", tot["sisa"], True, "single"))
    for label, nilai, tebal, atas in rekap:
        _baris_rekap(t, lebar, label, nilai, tebal=tebal, atas=atas)
    inti.paragraf(doc, spasi_sesudah=6)
    return t


def blok_terbilang(doc, d, k):
    nilai = k["total"]["sisa"] if k["total"]["uang_muka"] else k["total"]["total"]
    p = inti.paragraf(doc, spasi_sesudah=10, rata=RATA_PENUH)
    inti.gaya_run(p.add_run("Terbilang: "), PT_ISI, tebal=True)
    inti.gaya_run(p.add_run(f"# {inti.rupiah_kata(nilai)} #"), PT_ISI, miring=True)


def blok_syarat(doc, d, k):
    butir = syarat_otomatis(d, k["atur"], k["jenis"])
    if not butir:
        return
    inti.paragraf(doc, "Syarat dan ketentuan:", tebal=True, spasi_sesudah=4,
                  jaga=True)
    inti.daftar_nomor(doc, butir)
    inti.paragraf(doc, spasi_sesudah=6)


def blok_rincian_bayar(doc, d, k):
    atur = k["atur"]
    baris = []
    if atur.get("bank"):
        baris.append(("Bank", atur["bank"]))
    if atur.get("rekening"):
        baris.append(("No. rekening", atur["rekening"]))
    if atur.get("rekening_atas_nama"):
        baris.append(("Atas nama", atur["rekening_atas_nama"]))
    if baris:
        inti.paragraf(doc, "Pembayaran ditujukan ke:", tebal=True, spasi_sesudah=4,
                      jaga=True)
        _label_baris(doc, baris)
        inti.paragraf(doc, spasi_sesudah=6)
    if d.get("jatuh_tempo"):
        inti.paragraf(doc, _teks(atur, "teks_invoice_bayar",
                                 jatuh_tempo=inti.tanggal_indonesia(d["jatuh_tempo"])),
                      rata=RATA_PENUH, spasi_sesudah=10)


def blok_blok_kwitansi(doc, d, k):
    jumlah = inti.ke_int(d.get("jumlah_terima"), "Jumlah yang diterima")
    acuan = ""
    if d.get("invoice_nomor"):
        acuan = f", sesuai Invoice No. {d['invoice_nomor']}"
        if d.get("invoice_tanggal"):
            acuan += f" tanggal {inti.tanggal_indonesia(d['invoice_tanggal'])}"
    _label_baris(doc, [
        ("Sudah terima dari", d["pelanggan_nama"]),
        ("Uang sejumlah", f"# {inti.rupiah_kata(jumlah)} #"),
        ("Untuk pembayaran", _teks(k["atur"], "teks_kwitansi_peruntukan",
                                   peruntukan=d["peruntukan"]) + acuan),
    ], lebar=KOLOM_LABEL_PANJANG)
    inti.paragraf(doc, spasi_sesudah=10)
    p = inti.paragraf(doc, spasi_sesudah=10)
    inti.gaya_run(p.add_run(inti.rupiah(jumlah)), PT_JUDUL + 2, tebal=True)
    if d.get("cara_bayar"):
        inti.paragraf(doc, f"Cara pembayaran: {d['cara_bayar']}", spasi_sesudah=8)


def blok_meterai(doc, d, k):
    """Pengingat bea meterai, bukan jaminan hukum."""
    if inti.ke_int(d.get("jumlah_terima") or 0) > BATAS_METERAI:
        inti.paragraf(doc, "Bermaterai Rp10.000", pt=PT_KECIL, miring=True,
                      spasi_sesudah=6)


def blok_catatan_kirim(doc, d, k):
    inti.paragraf(doc, _teks(k["atur"], "teks_sj_periksa"), rata=RATA_PENUH,
                  spasi_sesudah=10)


def blok_catatan(doc, d, k):
    teks = str(d.get("catatan") or "").strip()
    if not teks:
        return
    for alinea in teks.split("\n"):
        if alinea.strip():
            inti.paragraf(doc, alinea.strip(), rata=RATA_PENUH, spasi_sesudah=6)
    inti.paragraf(doc, spasi_sesudah=4)


def blok_penutup(doc, d, k):
    atur = k["atur"]
    if k["spek"]["tabel"] == "harga":
        inti.paragraf(doc, _teks(atur, "teks_penutup_cta",
                                 sla=atur.get("sla") or "1x24 jam kerja"),
                      rata=RATA_PENUH, spasi_sesudah=6)
    inti.paragraf(doc, _teks(atur, "teks_penutup"), rata=RATA_PENUH,
                  spasi_sesudah=8)


def blok_kontak(doc, d, k):
    atur = k["atur"]
    baris = [("Nama", f"{atur.get('penanda_nama')} ({atur.get('penanda_jabatan')})")]
    for label, kunci in (("Telepon", "telepon"), ("WhatsApp", "penanda_wa"),
                         ("Email", "penanda_email")):
        if atur.get(kunci):
            baris.append((label, atur[kunci]))
    if not atur.get("penanda_email") and atur.get("email"):
        baris.append(("Email", atur["email"]))
    inti.paragraf(doc, _teks(atur, "teks_kontak_judul"), spasi_sesudah=4, jaga=True)
    # Blok kontak diikat ke blok tanda tangan di bawahnya: kalau tidak muat,
    # keduanya pindah bersama, bukan menyisakan halaman yang hanya berisi
    # "Hormat kami".
    inti.jaga_bersama(_label_baris(doc, baris))
    p = inti.paragraf(doc, spasi_sesudah=10)
    p.paragraph_format.keep_with_next = True


def _kolom_ttd(sel, atas, nama, jabatan, ruang=RUANG_TTD):
    """
    Satu kolom tanda tangan.

    Ruang tanda tangan dibuat dengan space_before pada paragraf nama, BUKAN
    dengan paragraf kosong: satu node, tingginya pasti, dan tidak bisa
    tertinggal sebagai baris kosong yatim di halaman berikutnya.
    """
    p0 = sel.paragraphs[0]
    pf = p0.paragraph_format
    pf.space_after = inti.Pt(0)
    pf.line_spacing = 1.0
    pf.keep_with_next = True
    inti.gaya_run(p0.add_run(atas), PT_ISI)
    pn = inti.paragraf(sel, spasi_sebelum=ruang, spasi_sesudah=0, baris=1.0, jaga=True)
    inti.gaya_run(pn.add_run(nama or "(" + "." * 26 + ")"), PT_ISI, tebal=bool(nama))
    inti.garis_bawah_paragraf(pn)
    inti.paragraf(sel, jabatan or "", spasi_sesudah=0, baris=1.0)


def _tabel_ttd(doc, d, k, kolom, isi):
    t = inti.tabel(doc, kolom, garis=False, margin_sel=0)
    r = inti.baris_tabel(t)
    inti.jangan_pecah(r, tinggi_min=TINGGI_TTD)
    for sel, (atas, nama, jabatan) in zip(r.cells, isi):
        if atas:            # kolom kosong dibiarkan benar-benar kosong
            _kolom_ttd(sel, atas, nama, jabatan)
    return t


def blok_tanda_tangan(doc, d, k):
    atur = k["atur"]
    _tabel_ttd(doc, d, k, KOLOM_TTD2, [
        (_teks(atur, "teks_hormat"), atur.get("penanda_nama"),
         f"{atur.get('penanda_jabatan')}, {atur.get('nama_usaha')}"),
        ("", "", ""),
    ])


def blok_persetujuan(doc, d, k):
    inti.paragraf(doc, _teks(k["atur"], "teks_persetujuan"), rata=RATA_PENUH,
                  spasi_sebelum=6, spasi_sesudah=4, jaga=True)
    _tabel_ttd(doc, d, k, KOLOM_TTD2, [
        ("Disetujui oleh,", str(d.get("pelanggan_pic") or ""),
         ", ".join(x for x in (str(d.get("pelanggan_jabatan") or ""),
                               d["pelanggan_nama"]) if x)),
        ("Tanggal,", "", ""),
    ])


def blok_ttd3(doc, d, k):
    _tabel_ttd(doc, d, k, KOLOM_TTD3, [
        ("Diterima oleh,", "", d["pelanggan_nama"]),
        ("Pengemudi / Ekspedisi,", str(d.get("pengemudi") or ""),
         str(d.get("ekspedisi") or "")),
        ("Pengirim,", k["atur"].get("penanda_nama"), k["atur"].get("nama_usaha")),
    ])


def blok_ttd_bast(doc, d, k):
    atur = k["atur"]
    _tabel_ttd(doc, d, k, KOLOM_TTD2, [
        ("PIHAK PERTAMA", atur.get("penanda_nama"), atur.get("penanda_jabatan")),
        ("PIHAK KEDUA", d.get("penerima_nama"), d.get("penerima_jabatan")),
    ])


BLOK = {
    "tanggal_kota": blok_tanggal_kota,
    "judul": blok_judul,
    "nomor_tengah": blok_nomor_tengah,
    "kepala_surat": blok_kepala_surat,
    "kepala_dua_kolom": blok_kepala_dua_kolom,
    "kepala_kirim": blok_kepala_kirim,
    "kepada": blok_kepada,
    "salam": blok_salam,
    "pembuka": blok_pembuka,
    "pembuka_bast": blok_pembuka_bast,
    "pihak": blok_pihak,
    "serah_bast": blok_serah_bast,
    "rangkap_bast": blok_rangkap_bast,
    "tabel_item": blok_tabel_item,
    "terbilang": blok_terbilang,
    "syarat": blok_syarat,
    "rincian_bayar": blok_rincian_bayar,
    "blok_kwitansi": blok_blok_kwitansi,
    "meterai": blok_meterai,
    "catatan_kirim": blok_catatan_kirim,
    "catatan": blok_catatan,
    "penutup": blok_penutup,
    "kontak": blok_kontak,
    "tanda_tangan": blok_tanda_tangan,
    "persetujuan": blok_persetujuan,
    "ttd3": blok_ttd3,
    "ttd_bast": blok_ttd_bast,
}


# ─── Perakit ──────────────────────────────────────────────────────────────────

def hal_dokumen(jenis, d):
    """Teks "Hal" otomatis dari jenis + perihal."""
    spek = JENIS[jenis]
    return spek["hal"].format(perihal=str(d.get("perihal") or "").strip()).strip()


def nama_berkas(jenis, nomor, d):
    """
    Nama file tanpa ekstensi, mis. 'SPH 007 - PT. Lion Wings'.

    Letak nomor urut berbeda antar pola: surat memakai "001/SPH-NXTG/X/2026"
    (urut di depan), sedangkan invoice dkk memakai "INV/2026/0001" (kode di
    depan, urut di belakang). Tanpa membedakannya, nama file invoice jadi
    "INV INV - ...". Akhiran revisi dipertahankan supaya revisi tidak menimpa
    berkas aslinya.
    """
    kode = JENIS[jenis]["kode"]
    teks, rev = str(nomor), ""
    if "-R" in teks:
        teks, _, r = teks.rpartition("-R")
        rev = f" R{r}"
    bagian = [b for b in teks.split("/") if b]
    if not bagian:
        urut = teks
    elif bagian[0].upper() == kode.upper():
        urut = bagian[-1]
    else:
        urut = bagian[0]
    return inti._nama_file(f"{kode} {urut}{rev} - "
                           f"{d.get('pelanggan_nama') or 'Dokumen'}", 80)


def buat(jenis, d, nomor, tujuan_docx, atur=None):
    """
    Rakit satu dokumen ke `tujuan_docx`. Return dict info + hasil hitung.

    `tujuan_docx` adalah parameter, bukan dihitung di dalam, supaya skrip uji
    bisa menulis ke scratchpad tanpa menyentuh output/dokumen produksi.
    """
    import docx

    if not TEMPLATE.exists():
        raise ValueError(f"Template kop tidak ditemukan: {TEMPLATE.name}")
    atur = dict(atur or {})
    kurang = cek_siap(atur, jenis)
    if kurang:
        raise ValueError("Lengkapi dulu Pengaturan Dokumen: " + ", ".join(kurang))

    d = validasi(jenis, d)
    spek = JENIS[jenis]
    k = {"jenis": jenis, "spek": spek, "atur": atur, "nomor": nomor,
         "hal": hal_dokumen(jenis, d), "total": hitung(d)}

    doc = docx.Document(str(TEMPLATE))
    inti.kosongkan_badan(doc)
    inti.pasang_footer(doc, atur.get("catatan_footer") or atur.get("nama_usaha") or "")
    for nama in spek["blok"]:
        BLOK[nama](doc, d, k)

    tujuan_docx = _siapkan(tujuan_docx)
    doc.save(str(tujuan_docx))
    return {"jenis": jenis, "nomor": nomor, "hal": k["hal"], "docx": tujuan_docx,
            "total": k["total"], "data": d}


def _siapkan(jalur):
    jalur = Path(jalur)
    jalur.parent.mkdir(parents=True, exist_ok=True)
    return jalur


def folder_hari(tgl=None):
    return inti.folder_hari(FOLDER_KELUARAN, tgl or date.today())


def daftar_jenis():
    """[(kunci, label, butuh_harga, wajib_tambahan)] untuk dropdown di UI."""
    return [{"kunci": k, "label": v["label"], "tabel": v["tabel"],
             "wajib": list(kolom_wajib(k)),
             "label_wajib": {w: LABEL_KOLOM.get(w, w) for w in kolom_wajib(k)}}
            for k, v in JENIS.items()]
