"""
dokumen_routes.py — Menu "Dokumen & Surat" (kop PT. NEXA TEKNOLOGI GROUP).

Membuat surat penawaran, proposal, invoice, surat jalan, BAST, kwitansi, dan
surat pengantar sebagai DOCX + PDF ber-kop NXTG, lengkap dengan nomor urut
otomatis dan total yang terhitung sendiri.

Modul ini TIDAK mengimpor `app` — saat dijalankan sebagai `python app.py`,
modul itu bernama `__main__` dan impor ulang akan membuat objek aplikasi kedua
(alasan yang sama dengan `komponen_routes.py`).

Semua handler POST memakai konvensi proyek ini: JSON masuk, JSON keluar
`{ok: bool, ...}`, tanpa `flash()` — base.html tidak punya
`get_flashed_messages`.
"""
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import (Blueprint, abort, jsonify, render_template, request,
                   send_file, url_for)

import db
import dokumen_db as ddb
from scrapers import docx_inti as inti
from scrapers import dokumen_buat as buat

bp = Blueprint("dokumen", __name__)

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"

RUANG_LABEL = {"webdev": "Klien Website", "komponen": "Distributor Komponen"}
KOLOM_IMPOR = ("kode", "nama", "deskripsi", "satuan", "harga", "siklus",
               "kategori")


def pasang(app):
    ddb.init()
    app.register_blueprint(bp)


# ─── Halaman ──────────────────────────────────────────────────────────────────

def _konteks():
    atur = ddb.pengaturan()
    return {"jenis": buat.daftar_jenis(), "atur": atur,
            "kurang_pengaturan": buat.cek_siap(atur),
            "stats": ddb.stats()}


@bp.route("/dokumen")
def riwayat():
    jenis = request.args.get("jenis", "")
    q = request.args.get("q", "").strip()
    return render_template("dokumen.html", dokumen=ddb.dokumen_daftar(jenis, q),
                           total=ddb.dokumen_hitung(jenis, q), filter_jenis=jenis,
                           q=q, label_ppn=buat.LABEL_PPN,
                           teks_bawaan=buat.TEKS_BAWAAN, **_konteks())


@bp.route("/dokumen/baru")
def form_baru():
    awal = {"tanggal": date.today().isoformat()}
    atur = ddb.pengaturan()
    awal["ppn_mode"] = atur["ppn_mode"]
    awal["termin"] = atur["termin"]
    awal["waktu_kerja"] = atur["waktu_kerja"]
    awal["garansi"] = atur["garansi"]
    awal["jatuh_tempo"] = (date.today() + timedelta(days=atur["tempo_hari"])).isoformat()

    # Prefill dari lead (tombol "Dokumen" di halaman Leads).
    place_key = request.args.get("place_key", "")
    ruang = request.args.get("ruang", "webdev")
    if place_key and ruang in db.RUANG_PATH:
        lead = _lead(ruang, place_key)
        if lead:
            awal.update(lead)

    # Prefill dari dokumen lama (Duplikat / Revisi).
    sumber = request.args.get("duplikat") or request.args.get("revisi")
    induk = None
    if sumber:
        lama = ddb.dokumen_get(sumber)
        if lama:
            awal.update(lama["data"])
            awal["tanggal"] = date.today().isoformat()
            if request.args.get("revisi"):
                induk = lama["id"]
    return render_template("dokumen_form.html", awal=awal,
                           jenis_aktif=request.args.get("jenis", "penawaran"),
                           induk_id=induk, paket=ddb.paket_daftar(),
                           kategori=ddb.produk_kategori(),
                           label_ppn=buat.LABEL_PPN, **_konteks())


@bp.route("/dokumen/produk")
def halaman_produk():
    return render_template("dokumen_produk.html",
                           produk=ddb.produk_daftar(aktif_saja=False),
                           kategori=ddb.produk_kategori(),
                           siklus=ddb.SIKLUS, paket=ddb.paket_daftar(),
                           **_konteks())


# ─── Pembuatan dokumen ────────────────────────────────────────────────────────

def _lead(ruang, place_key):
    """Data pelanggan dari satu lead, siap dipakai form."""
    with db.ruang(ruang):
        row = db.get(place_key)
    if not row:
        return None
    email = str(row.get("email") or "").strip() or \
        str(row.get("email_lain") or "").split("|")[0].strip()
    return {
        "pelanggan_nama": row.get("nama_bisnis") or "",
        "pelanggan_alamat": row.get("alamat") or "",
        "pelanggan_kota": row.get("kota") or "",
        "pelanggan_pic": row.get("nama_pic") or "",
        "pelanggan_telepon": row.get("telepon") or "",
        "pelanggan_email": email,
        "place_key": place_key,
        "ruang": ruang,
    }


@bp.route("/dokumen/cari-pelanggan")
def cari_pelanggan():
    """Saran pelanggan dari buku pelanggan + kedua ruang lead."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify({"ok": True, "hasil": []})
    hasil = [{"sumber": "Buku pelanggan", "nama": p["nama"],
              "pelanggan_nama": p["nama"], "pelanggan_alamat": p["alamat"],
              "pelanggan_kota": p["kota"], "pelanggan_pic": p["pic"],
              "pelanggan_jabatan": p["jabatan_pic"],
              "pelanggan_telepon": p["telepon"], "pelanggan_email": p["email"],
              "pelanggan_npwp": p["npwp"]}
             for p in ddb.pelanggan_cari(q, 6)]
    sudah = {h["nama"].lower() for h in hasil}
    for ruang, label in RUANG_LABEL.items():
        with db.ruang(ruang):
            with db._lock:
                baris = db.get_conn().execute(
                    "SELECT place_key, nama_bisnis, alamat, kota, telepon, email, "
                    "email_lain, nama_pic FROM businesses "
                    "WHERE nama_bisnis LIKE ? ORDER BY nama_bisnis LIMIT 6",
                    (f"%{q}%",)).fetchall()
        for r in baris:
            nama = str(r["nama_bisnis"] or "").strip()
            if not nama or nama.lower() in sudah:
                continue
            sudah.add(nama.lower())
            hasil.append({
                "sumber": label, "nama": nama, "pelanggan_nama": nama,
                "pelanggan_alamat": r["alamat"] or "", "pelanggan_kota": r["kota"] or "",
                "pelanggan_pic": r["nama_pic"] or "", "pelanggan_jabatan": "",
                "pelanggan_telepon": r["telepon"] or "",
                "pelanggan_email": (r["email"] or "")
                or str(r["email_lain"] or "").split("|")[0].strip(),
                "pelanggan_npwp": "", "place_key": r["place_key"], "ruang": ruang,
            })
    return jsonify({"ok": True, "hasil": hasil[:12]})


def _relatif(p):
    return p.relative_to(buat.FOLDER_KELUARAN).as_posix()


def _url(p):
    return url_for("dokumen.berkas", jalur=_relatif(p))


@bp.route("/dokumen/buat", methods=["POST"])
def buat_dokumen():
    d = request.get_json(silent=True) or {}
    jenis = d.pop("jenis", "")
    induk_id = d.pop("induk_id", None) or None
    if jenis not in buat.JENIS:
        return jsonify({"ok": False, "error": "Jenis dokumen belum dipilih."}), 400

    atur = ddb.pengaturan()
    kurang = buat.cek_siap(atur, jenis)
    if kurang:
        return jsonify({"ok": False, "error": "Lengkapi dulu Pengaturan Dokumen: "
                                              + ", ".join(kurang),
                        "pengaturan": True}), 400
    try:
        bersih = buat.validasi(jenis, d)
        total = buat.hitung(bersih)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    dok_id, nomor = ddb.mulai_dokumen(jenis, bersih, total, atur, induk_id=induk_id)
    try:
        folder = buat.folder_hari(bersih["tanggal"])
        # Nama file dirangkai sebagai teks, BUKAN lewat Path.with_suffix():
        # pelanggan "PT. Lion Wings" membuat with_suffix memperlakukan
        # " Lion Wings" sebagai ekstensi lalu menggantinya, sehingga filenya
        # jadi "SPH 001 - PT.docx".
        nama = buat.nama_berkas(jenis, nomor, bersih)
        info = buat.buat(jenis, bersih, nomor, folder / f"{nama}.docx", atur)
    except Exception as e:
        # Nomor tidak boleh hangus hanya karena pembuatan berkas gagal.
        ddb.batalkan_dokumen(dok_id)
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 400

    docx_path = info["docx"]
    pdf_path = folder / f"{nama}.pdf"
    hasil_pdf = inti.ke_pdf([(docx_path, pdf_path)])
    peringatan = inti.ringkas_galat_pdf(hasil_pdf)
    ada_pdf = pdf_path.exists() and not hasil_pdf[docx_path]

    ddb.selesaikan_dokumen(dok_id, _relatif(docx_path),
                           _relatif(pdf_path) if ada_pdf else "")
    ddb.set_pelanggan(dok_id, ddb.pelanggan_simpan({
        "nama": bersih.get("pelanggan_nama"),
        "alamat": bersih.get("pelanggan_alamat"), "kota": bersih.get("pelanggan_kota"),
        "pic": bersih.get("pelanggan_pic"), "jabatan_pic": bersih.get("pelanggan_jabatan"),
        "telepon": bersih.get("pelanggan_telepon"), "email": bersih.get("pelanggan_email"),
        "npwp": bersih.get("pelanggan_npwp"), "place_key": bersih.get("place_key"),
        "ruang": bersih.get("ruang"),
    }))
    return jsonify({"ok": True, "id": dok_id, "nomor": nomor,
                    "jenis": buat.JENIS[jenis]["label"],
                    "total": info["total"]["total"],
                    "total_teks": inti.rupiah(info["total"]["total"]),
                    "peringatan": peringatan,
                    "docx": _url(docx_path),
                    "pdf": _url(pdf_path) if ada_pdf else ""})


@bp.route("/dokumen/hitung", methods=["POST"])
def hitung_jalan():
    """Rekap total untuk pratinjau di layar — sumber angka yang sama dengan PDF."""
    d = request.get_json(silent=True) or {}
    try:
        t = buat.hitung(d)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    baris = [{"jumlah": b["jumlah"], "teks": inti.rupiah(b["jumlah"])}
             for b in t["baris"]]
    rekap = [("Subtotal", t["subtotal"])]
    if t["diskon_global"]:
        rekap.append(("Diskon" + (f" {t['diskon_persen']}%" if t["diskon_persen"] else ""),
                      -t["diskon_global"]))
    if t["biaya_kirim"]:
        rekap.append(("Ongkos kirim", t["biaya_kirim"]))
    if t["dpp"] != t["subtotal"]:
        rekap.append(("Dasar Pengenaan Pajak" if t["ppn_tarif"] else "Jumlah", t["dpp"]))
    if t["ppn_tarif"]:
        if t["dpp_nilai_lain"] != t["dpp"]:
            rekap.append(("DPP Nilai Lain (11/12)", t["dpp_nilai_lain"]))
        rekap.append((t["ppn_label"], t["ppn"]))
    rekap.append(("TOTAL", t["total"]))
    if t["uang_muka"]:
        rekap.append(("Uang muka diterima", -t["uang_muka"]))
        rekap.append(("SISA TAGIHAN", t["sisa"]))
    nilai = t["sisa"] if t["uang_muka"] else t["total"]
    return jsonify({"ok": True, "baris": baris,
                    "rekap": [{"label": a, "teks": inti.rupiah(b)} for a, b in rekap],
                    "terbilang": inti.rupiah_kata(nilai)})


@bp.route("/dokumen/<int:dok_id>")
def detail(dok_id):
    d = ddb.dokumen_get(dok_id)
    if not d:
        return jsonify({"ok": False, "error": "Dokumen tidak ditemukan"}), 404
    for kunci in ("docx", "pdf"):
        if d[kunci]:
            p = buat.FOLDER_KELUARAN / d[kunci]
            d[kunci + "_url"] = _url(p) if p.is_file() else ""
        else:
            d[kunci + "_url"] = ""
    return jsonify({"ok": True, "dokumen": d})


@bp.route("/dokumen/hapus", methods=["POST"])
def hapus():
    d = request.get_json(silent=True) or {}
    dok = ddb.dokumen_get(d.get("id"))
    if not dok:
        return jsonify({"ok": False, "error": "Dokumen tidak ditemukan"}), 404
    ddb.dokumen_hapus(dok["id"])
    return jsonify({"ok": True, "catatan": "Catatan dokumen dihapus. Nomor "
                                           f"{dok['nomor']} tidak dipakai ulang."})


@bp.route("/dokumen/berkas/<path:jalur>")
def berkas(jalur):
    """Unduh dari output/dokumen — jalur di luar folder itu ditolak."""
    akar = buat.FOLDER_KELUARAN.resolve()
    calon = (akar / jalur).resolve()
    if akar not in calon.parents or not calon.is_file():
        abort(404)
    return send_file(calon, as_attachment=request.args.get("lihat") != "1",
                     download_name=calon.name)


# ─── Pengaturan ───────────────────────────────────────────────────────────────

@bp.route("/dokumen/pengaturan", methods=["GET", "POST"])
def pengaturan():
    if request.method == "POST":
        atur = ddb.simpan_pengaturan(request.get_json(silent=True) or {})
    else:
        atur = ddb.pengaturan()
    return jsonify({"ok": True, "atur": atur, "kurang": buat.cek_siap(atur),
                    "wajib": list(buat.WAJIB_PENGATURAN)
                    + list(buat.WAJIB_PENGATURAN_BAYAR)})


# ─── Katalog produk ───────────────────────────────────────────────────────────

@bp.route("/dokumen/produk/cari")
def produk_cari():
    return jsonify({"ok": True,
                    "hasil": ddb.produk_daftar(q=request.args.get("q", "").strip(),
                                               kategori=request.args.get("kategori", ""),
                                               limit=30)})


@bp.route("/dokumen/produk/simpan", methods=["POST"])
def produk_simpan():
    try:
        p = ddb.produk_simpan(request.get_json(silent=True) or {})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "produk": p})


@bp.route("/dokumen/produk/hapus", methods=["POST"])
def produk_hapus():
    d = request.get_json(silent=True) or {}
    n = ddb.produk_hapus(d.get("ids") or ([d["id"]] if d.get("id") else []))
    return jsonify({"ok": True, "jumlah": n})


@bp.route("/dokumen/produk/impor", methods=["POST"])
def produk_impor():
    """
    Impor katalog dari Excel/CSV.

    pandas membaca kolom harga sebagai float64, dan '1250000.0' yang lolos ke
    katalog akan merembet jadi angka pecahan di dokumen — `ddb.produk_impor`
    menormalkan ke int dan MENOLAK (bukan memotong) bagian desimal.
    """
    berkas_unggah = request.files.get("berkas")
    if berkas_unggah is None or not berkas_unggah.filename:
        return jsonify({"ok": False, "error": "Pilih dulu file Excel atau CSV."}), 400
    nama = berkas_unggah.filename.lower()
    try:
        import pandas as pd
        if nama.endswith(".csv"):
            df = pd.read_csv(berkas_unggah, dtype=str, keep_default_na=False)
        else:
            df = pd.read_excel(berkas_unggah, dtype=str, keep_default_na=False)
    except Exception as e:
        return jsonify({"ok": False,
                        "error": f"File tidak terbaca ({type(e).__name__}: {e})"}), 400

    peta = {str(c).strip().lower(): c for c in df.columns}
    if "nama" not in peta:
        return jsonify({"ok": False, "error": "Kolom 'nama' tidak ada. Judul kolom yang "
                                              "dikenali: " + ", ".join(KOLOM_IMPOR)}), 400
    baris = [{k: str(r[peta[k]]).strip() for k in KOLOM_IMPOR if k in peta}
             for _, r in df.iterrows()]
    baris = [b for b in baris if any(b.values())]
    masuk, galat = ddb.produk_impor(baris)
    return jsonify({"ok": True, "jumlah": masuk, "gagal": galat,
                    "produk": ddb.produk_daftar(aktif_saja=False)})


@bp.route("/dokumen/produk/ekspor")
def produk_ekspor():
    import pandas as pd

    data = ddb.produk_daftar(aktif_saja=False, limit=100000)
    if not data:
        return jsonify({"ok": False, "error": "Katalog masih kosong."}), 404
    df = pd.DataFrame([{k: p[k] for k in KOLOM_IMPOR} for p in data])
    OUTPUT_DIR.mkdir(exist_ok=True)
    nama = f"katalog_produk_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    df.to_excel(OUTPUT_DIR / nama, index=False)
    return jsonify({"ok": True, "url": url_for("download", filename=nama)})


# ─── Paket ────────────────────────────────────────────────────────────────────

@bp.route("/dokumen/paket", methods=["GET", "POST"])
def paket():
    if request.method == "POST":
        d = request.get_json(silent=True) or {}
        try:
            if d.get("hapus"):
                ddb.paket_hapus(d["hapus"])
            else:
                ddb.paket_simpan(d)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "paket": ddb.paket_daftar()})
