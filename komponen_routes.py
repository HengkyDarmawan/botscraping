"""
komponen_routes.py — Menu "Distributor Komponen" (PT. Inti Sentosa Bersama).

Scraper, CRM, export, proposal, dan template email untuk calon pembeli komponen
komputer. Seluruh datanya tinggal di data/komponen.db (lihat db.ruang), terpisah
dari lead klien website.

Helper job (_new_job/_jalankan) disuntikkan oleh app.py lewat `pasang()` —
modul ini TIDAK mengimpor app, karena saat dijalankan sebagai `python app.py`
modul itu bernama __main__ dan impor ulang akan membuat dict `jobs` kedua.
"""
import json
import zipfile
from datetime import date, datetime
from pathlib import Path

from flask import (Blueprint, abort, jsonify, render_template, request,
                   send_file, url_for)

import db
import db_komponen as dk
import ekspor
from scrapers import dokumen_isb as dok
from scrapers import komponen, kontak

bp = Blueprint("komponen", __name__)
_job = {"baru": None, "jalankan": None, "berjalan": None}
JENIS_JOB = "komponen"

DATA_DIR = Path(__file__).resolve().parent / "data"


def pasang(app, new_job, jalankan, berjalan):
    _job.update(baru=new_job, jalankan=jalankan, berjalan=berjalan)
    with db.ruang(dk.RUANG):
        db.init_db()
    # Saat start tidak ada run yang berjalan — lead tertunda dari run yang mati
    # (server dimatikan di tengah scraping) aman dibersihkan sekarang.
    dk.bersihkan_tertunda()
    app.register_blueprint(bp)


# ─── Scraper ──────────────────────────────────────────────────────────────────

@bp.route("/api/keywords-komponen")
def api_keywords():
    with open(DATA_DIR / "keywords_komponen.json", encoding="utf-8") as f:
        return jsonify(json.load(f))


@bp.route("/komponen")
def halaman_scraper():
    return render_template("komponen.html", stats=dk.stats(),
                           mode_kontak=kontak.LABEL_MODE)


def _tolak_bila_berjalan():
    """Respons 409 bila scraping komponen lain masih berjalan, selain itu None."""
    jid = _job["berjalan"](JENIS_JOB)
    if jid:
        return jsonify({"ok": False, "job_id": jid,
                        "error": "Masih ada scraping komponen yang berjalan — tunggu "
                                 "selesai atau tekan Stop dulu."}), 409
    return None


@bp.route("/komponen/run", methods=["POST"])
def run():
    from scrapers.gmaps import run_scrape_komponen
    tolak = _tolak_bila_berjalan()
    if tolak:
        return tolak
    job_id = _job["baru"]()
    _job["jalankan"](job_id, run_scrape_komponen, request.json or {},
                     "Scraping komponen", jenis=JENIS_JOB)
    return jsonify({"job_id": job_id})


@bp.route("/komponen/terputus")
def terputus():
    with db.ruang(dk.RUANG):
        run = db.run_terputus_terakhir()
    if not run:
        return jsonify({"ada": False})
    return jsonify({"ada": True, "run_id": run["run_id"],
                    "mulai_pada": run.get("mulai_pada"),
                    "target_selesai": run.get("target_index"),
                    "total_target": run.get("total_target"),
                    "sisa_target": run.get("sisa_target"),
                    "jumlah_lead": run.get("jumlah_lead")})


@bp.route("/komponen/lanjutkan", methods=["POST"])
def lanjutkan():
    body = request.json or {}
    with db.ruang(dk.RUANG):
        run = db.run_get(body.get("run_id"))
    if not run:
        return jsonify({"ok": False, "error": "Run tidak ditemukan"}), 404
    params = dict(run.get("params") or {})
    semua = params.get("search_targets") or []
    sudah = int(run.get("target_index") or 0)
    sisa = semua[sudah:]
    if not sisa:
        return jsonify({"ok": False,
                        "error": "Run ini sudah menyelesaikan semua targetnya"}), 400
    params.update(search_targets=sisa, _run_id=run["run_id"], _offset_target=sudah,
                  _total_target=int(run.get("total_target") or len(semua)))
    if body.get("apify_proxy_token"):
        params["apify_proxy_token"] = body["apify_proxy_token"]
    from scrapers.gmaps import run_scrape_komponen
    tolak = _tolak_bila_berjalan()
    if tolak:
        return tolak
    job_id = _job["baru"]()
    _job["jalankan"](job_id, run_scrape_komponen, params, "Lanjutan scraping komponen",
                     jenis=JENIS_JOB)
    return jsonify({"ok": True, "job_id": job_id, "sisa_target": len(sisa)})


# ─── CRM ──────────────────────────────────────────────────────────────────────

def _filter(sumber=None):
    a = sumber if sumber is not None else request.args

    def s(nama):
        return (str(a.get(nama) or "")).strip() or None

    try:
        skor_min = int(s("skor_min")) if s("skor_min") else None
    except ValueError:
        skor_min = None
    tab = s("tab")
    return {
        "q": s("q"), "segmen": s("segmen"), "tier": s("tier"),
        "area_pencarian": s("area"), "kota": s("kota"), "status": s("status"),
        # wa=1 / email=1 = tautan lama sebelum ada dropdown "Kontak".
        "punya_wa": a.get("wa") == "1", "punya_email": a.get("email") == "1",
        "kontak": s("kontak"), "website": s("website"), "skor_min": skor_min,
        "kanal": s("kanal"),
        "tab": tab if tab and tab != "semua" else None,
        "follow_up": a.get("fu") == "1", "run": s("run"),
    }


def _halaman(nilai):
    try:
        return max(int(nilai or 1), 1)
    except (TypeError, ValueError):
        return 1


@bp.route("/komponen/leads")
def leads():
    f = _filter()
    # Tab "Sudah Dikontak" paling berguna diurutkan dari kontak terakhir.
    urut = request.args.get("urut") or ("dihubungi" if f["tab"] == "sudah" else "skor")
    per = 50
    hal = _halaman(request.args.get("hal"))
    rows, total = dk.query(f, urut=urut, limit=per, offset=(hal - 1) * per)
    for r in rows:
        r["nomor_wa"] = kontak.nomor_wa(r)
        r["email_semua"] = kontak.email_semua(r)
    total_hal = max((total + per - 1) // per, 1)
    return render_template(
        "komponen_leads.html", leads=rows, total=total, halaman=hal,
        total_halaman=total_hal, f=f, urut=urut,
        args={k: v for k, v in request.args.items() if k != "hal"},
        tabs=db.TAB_LEADS, tab_aktif=f["tab"] or "semua", jumlah_tab=dk.hitung_tab(f),
        opsi_segmen=dk.nilai_unik("segmen"), opsi_tier=komponen.URUTAN_TIER,
        opsi_area=dk.nilai_unik("area_pencarian"), opsi_kota=dk.nilai_unik("kota"),
        opsi_status=komponen.STATUS_PILIHAN, stats=dk.stats(),
        atur=dk.pengaturan(), masalah_template=dok.cek_template(),
        ada_profil=bool(dok.company_profile()),
    )


@bp.route("/komponen/status", methods=["POST"])
def status():
    d = request.json or {}
    if not d.get("place_key"):
        return jsonify({"ok": False, "error": "place_key kosong"}), 400
    if d.get("status") is not None and d["status"] not in komponen.STATUS_PILIHAN:
        return jsonify({"ok": False, "error": "Status tidak dikenal"}), 400
    row = dk.update_crm(d["place_key"], status=d.get("status"),
                        catatan=d.get("catatan"),
                        tanggal_follow_up=d.get("tanggal_follow_up"),
                        nama_pic=d.get("nama_pic"))
    if not row:
        return jsonify({"ok": False, "error": "Lead tidak ditemukan"}), 404
    return jsonify({"ok": True, "status": row.get("status_leads"),
                    "tanggal_follow_up": row.get("tanggal_follow_up") or "",
                    "tanggal_dihubungi": row.get("tanggal_dihubungi") or ""})


@bp.route("/komponen/pin", methods=["POST"])
def pin():
    """Pasang/lepas PIN "lead berpotensi" untuk satu atau banyak lead."""
    d = request.json or {}
    keys = d.get("keys") or ([d["place_key"]] if d.get("place_key") else [])
    if not keys:
        return jsonify({"ok": False, "error": "Pilih minimal satu lead."}), 400
    n = dk.set_pin(keys, bool(d.get("dipin", True)))
    return jsonify({"ok": True, "jumlah": n, "dipin": bool(d.get("dipin", True))})


@bp.route("/komponen/detail/<path:place_key>")
def detail(place_key):
    row = dk.get(place_key)
    if not row:
        return jsonify({"error": "Lead tidak ditemukan"}), 404
    with db.ruang(dk.RUANG):
        row["perubahan"] = db.perubahan_terbaru(place_key, batas=20)
    return jsonify(row)


@bp.route("/komponen/hapus", methods=["POST"])
def hapus():
    """
    Hapus lead komponen: `keys` (per baris / terpilih), atau semua lead yang
    cocok dengan `filter` (wajib ketik HAPUS). Nomor surat & file proposal tidak
    pernah ikut terhapus.
    """
    d = request.json or {}
    jangan = bool(d.get("jangan_ambil_lagi"))
    if d.get("keys"):
        n = dk.hapus_leads(d["keys"], jangan_ambil_lagi=jangan)
        return jsonify({"ok": True, "terhapus": n, "stats": dk.stats()})

    if (d.get("konfirmasi") or "").strip().upper() != "HAPUS":
        return jsonify({"ok": False, "error": "Ketik HAPUS untuk konfirmasi."}), 400
    if _job["berjalan"](JENIS_JOB):
        # Run yang masih jalan akan langsung menulis lead baru lagi — hasil
        # "hapus semua" jadi tidak jelas. Hapus per baris tetap boleh.
        return jsonify({"ok": False, "error": "Scraping komponen sedang berjalan. "
                                              "Hentikan atau tunggu selesai dulu."}), 409
    n, lewati = dk.hapus_sesuai_filter(_filter(d.get("filter") or {}),
                                       jangan_ambil_lagi=jangan,
                                       kosongkan_lewati=bool(d.get("kosongkan_lewati")))
    return jsonify({"ok": True, "terhapus": n, "lewati_dikosongkan": lewati,
                    "stats": dk.stats()})


# ─── Export ───────────────────────────────────────────────────────────────────

@bp.route("/komponen/export/kolom")
def export_kolom():
    return jsonify(ekspor.katalog("komponen"))


@bp.route("/komponen/export", methods=["POST"])
def export():
    d = request.json or {}
    f = _filter(d.get("filter") or {})
    if d.get("keys"):
        f = {"keys": d["keys"]}
    f["untuk_export"] = True
    rows, _ = dk.query(f, urut=d.get("urut") or "skor", limit=0)
    ringkasan = {"Diekspor pada": datetime.now().strftime("%d-%m-%Y %H:%M"),
                 "Sumber": "baris terpilih" if d.get("keys") else "filter aktif"}
    nama, n = ekspor.tulis(rows, "komponen", d.get("kolom") or [],
                           fmt=d.get("format") or "xlsx", wajib=d.get("wajib") or "",
                           awalan="komponen-export", ringkasan=ringkasan)
    if not nama:
        return jsonify({"ok": False, "error": "Tidak ada lead yang cocok — periksa filter "
                                              "atau syarat kontak export."}), 404
    return jsonify({"ok": True, "jumlah": n,
                    "url": url_for("download", filename=nama)})


# ─── Proposal ─────────────────────────────────────────────────────────────────

def _buat_satu(row, atur, tgl, baru=False):
    """Isi proposal satu lead (DOCX). Return dict info; PDF dibuat terpisah."""
    tanggal = tgl.strftime("%Y-%m-%d")
    nomor, urut = dk.ambil_nomor_surat(
        row["place_key"], tanggal,
        lambda u: dok.bentuk_nomor(atur["pola_nomor"], u, tgl), baru=baru)
    folder = dok.folder_hari(tgl)
    dasar = folder / dok.nama_berkas(urut, row, tgl)
    dok.isi_proposal(row, atur, nomor, tgl, dasar.with_suffix(".docx"))
    return {"place_key": row["place_key"], "nama": row.get("nama_bisnis"),
            "nomor": nomor, "docx": dasar.with_suffix(".docx"),
            "pdf": dasar.with_suffix(".pdf")}


def _relatif(p):
    return p.relative_to(dok.FOLDER_PROPOSAL).as_posix()


def _url_berkas(p):
    return url_for("komponen.berkas", jalur=_relatif(p))


@bp.route("/komponen/proposal", methods=["POST"])
def proposal():
    d = request.json or {}
    row = dk.get(d.get("place_key"))
    if not row:
        return jsonify({"ok": False, "error": "Lead tidak ditemukan"}), 404
    atur = dk.pengaturan()
    try:
        info = _buat_satu(row, atur, date.today(), baru=bool(d.get("baru")))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    hasil_pdf = dok.ke_pdf([(info["docx"], info["pdf"])])
    peringatan = dok.ringkas_galat_pdf(hasil_pdf)
    ada_pdf = info["pdf"].exists() and not hasil_pdf[info["docx"]]
    dk.catat_proposal(row["place_key"], info["nomor"], date.today().isoformat(),
                      _relatif(info["pdf"] if ada_pdf else info["docx"]))
    return jsonify({"ok": True, "nomor": info["nomor"], "peringatan": peringatan,
                    "docx": _url_berkas(info["docx"]),
                    "pdf": _url_berkas(info["pdf"]) if ada_pdf else ""})


@bp.route("/komponen/proposal-massal", methods=["POST"])
def proposal_massal():
    d = request.json or {}
    keys = list(dict.fromkeys(d.get("keys") or []))[:200]
    if not keys:
        return jsonify({"ok": False, "error": "Pilih minimal satu lead."}), 400
    atur = dk.pengaturan()
    tgl = date.today()
    hasil, gagal = [], []
    for k in keys:
        row = dk.get(k)
        if not row:
            continue
        try:
            hasil.append(_buat_satu(row, atur, tgl))
        except ValueError as e:
            gagal.append(f"{row.get('nama_bisnis')}: {e}")
    if not hasil:
        return jsonify({"ok": False, "error": "; ".join(gagal) or "Tidak ada lead."}), 400
    hasil_pdf = dok.ke_pdf([(h["docx"], h["pdf"]) for h in hasil])
    peringatan = dok.ringkas_galat_pdf(hasil_pdf)
    zip_path = dok.folder_hari(tgl) / f"Proposal {tgl:%Y-%m-%d} {datetime.now():%H%M%S}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for h in hasil:
            ada_pdf = h["pdf"].exists() and not hasil_pdf[h["docx"]]
            for p in ([h["pdf"]] if ada_pdf else []) + [h["docx"]]:
                z.write(p, p.name)
            dk.catat_proposal(h["place_key"], h["nomor"], tgl.isoformat(),
                              _relatif(h["pdf"] if ada_pdf else h["docx"]))
    return jsonify({"ok": True, "jumlah": len(hasil), "gagal": gagal,
                    "peringatan": peringatan, "zip": _url_berkas(zip_path)})


@bp.route("/komponen/berkas/<path:jalur>")
def berkas(jalur):
    """Unduh file dari output/proposal — jalur di luar folder itu ditolak."""
    akar = dok.FOLDER_PROPOSAL.resolve()
    calon = (akar / jalur).resolve()
    if akar not in calon.parents or not calon.is_file():
        abort(404)
    return send_file(calon, as_attachment=request.args.get("lihat") != "1",
                     download_name=calon.name)


@bp.route("/komponen/company-profile")
def company_profile():
    p = dok.company_profile()
    if not p:
        abort(404)
    return send_file(p, as_attachment=True, download_name=p.name)


# ─── Template email ───────────────────────────────────────────────────────────

@bp.route("/komponen/email/<path:place_key>")
def email(place_key):
    row = dk.get(place_key)
    if not row:
        return jsonify({"ok": False, "error": "Lead tidak ditemukan"}), 404
    try:
        bagian = dok.isi_template_email(row, dk.pengaturan())
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    proposal_url = ""
    if row.get("proposal_file"):
        p = dok.FOLDER_PROPOSAL / row["proposal_file"]
        if p.is_file():
            proposal_url = _url_berkas(p)
    return jsonify({
        "ok": True, "bagian": bagian, "urutan": [k for k, _, _ in dok.BAGIAN_EMAIL],
        "nama": row.get("nama_bisnis"), "emails": kontak.email_semua(row),
        "nomor_wa": kontak.nomor_wa(row), "wa_link": row.get("whatsapp_link") or "",
        "nama_pic": row.get("nama_pic") or "", "status": row.get("status_leads") or "",
        "proposal_nomor": row.get("proposal_nomor") or "",
        "proposal_tanggal": row.get("proposal_tanggal") or "",
        "proposal_url": proposal_url,
        "company_profile": url_for("komponen.company_profile") if dok.company_profile() else "",
    })


# ─── Pengaturan ───────────────────────────────────────────────────────────────

@bp.route("/komponen/pengaturan", methods=["GET", "POST"])
def pengaturan():
    if request.method == "GET":
        return jsonify(dk.pengaturan())
    d = request.json or {}
    try:
        contoh = dok.bentuk_nomor(d.get("pola_nomor") or dk.pengaturan()["pola_nomor"],
                                  1, date.today())
        for k in ("fu1_hari", "fu2_hari"):
            if k in d and not str(d[k]).strip().isdigit():
                raise ValueError("Hari follow-up harus berupa angka.")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "pengaturan": dk.simpan_pengaturan(d), "contoh": contoh})
