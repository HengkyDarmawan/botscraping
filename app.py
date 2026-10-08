"""
app.py — LeadScraper Pro Web App
Jalankan: python app.py
Buka:     http://localhost:5000
"""
import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import (Flask, Response, abort, jsonify, redirect, render_template,
                   request, send_file, url_for)

# Semua path di proyek ini (data/, output/, dokumen/, config) relatif terhadap
# folder proyek. Tanpa ini, menjalankan `python botscraping/app.py` dari folder
# lain membuat database kosong baru di tempat yang salah dan lead "hilang".
BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

import config  # noqa: E402
import db  # noqa: E402

app = Flask(__name__)
app.secret_key = "leadscraper-2024-secret"

OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
DATA_DIR = BASE_DIR / "data"

# Status semua job scraping yang sedang/pernah berjalan
# Format: {job_id: {"status", "progress", "log", "found", "filename", "error", "cancel"}}
jobs: dict = {}

# Job yang sudah selesai dibuang setelah sekian detik supaya dict tidak
# tumbuh tanpa batas selama server hidup berhari-hari.
UMUR_JOB_SELESAI = 3600

# ─── Penjaga kode basi ────────────────────────────────────────────────────────
#
# app.run(debug=False) tidak pernah memuat ulang modul. Pada 14 Agustus itu
# menghabiskan satu sesi penuh: server hidup sejak 11:23 terus melayani versi
# lama sementara perbaikan sudah ada di disk sejak 11:43, dan tidak ada satu pun
# tanda di UI. Reloader otomatis SENGAJA tidak dipakai — ia akan membunuh job
# scraping yang sedang berjalan di tengah jalan. Cukup beri tahu user.
WAKTU_START = time.time()

_SUMBER_DIPANTAU = (
    "app.py", "db.py", "config.py",
    "scrapers/pricing.py", "scrapers/mp_api.py", "scrapers/mp_dom.py",
    "scrapers/mp_common.py", "scrapers/mp_session.py", "scrapers/mp_endpoints.py",
    "scrapers/mp_lexicon.py", "scrapers/mp_match.py", "scrapers/mp_harga.py",
    "scrapers/mp_cari.py", "scrapers/gmaps.py", "scrapers/scoring.py",
    "scrapers/enrich.py", "scrapers/komponen.py", "scrapers/dokumen_isb.py",
    "db_komponen.py", "komponen_routes.py", "ekspor.py",
    "scrapers/kualitas.py", "scrapers/kontak.py", "scrapers/pesan_web.py",
    "scrapers/docx_inti.py", "scrapers/dokumen_buat.py",
    "dokumen_db.py", "dokumen_routes.py",
)


def kode_basi():
    """Berkas sumber yang berubah setelah proses ini dimulai."""
    basi = []
    for rel in _SUMBER_DIPANTAU:
        try:
            p = Path(rel)
            if p.exists() and p.stat().st_mtime > WAKTU_START:
                basi.append(rel)
        except OSError:
            continue
    return basi


@app.context_processor
def _inject_kode_basi():
    return {"kode_basi": kode_basi()}


db.init_db()
# Saat start tidak ada run yang berjalan: lead "menunggu cek website" dari run
# yang mati di tengah jalan aman dibersihkan (diambil ulang di run berikutnya).
db.bersihkan_tertunda()

import backup  # noqa: E402

# Database tidak lagi ada di git — cadangan harian dibuat saat app dinyalakan.
backup.backup_harian_latar()


def _saring_awal():
    """
    Aturan scrapers/kualitas.py baru / berubah → bersihkan data lama Klien
    Website sekali (±30 detik untuk 20 ribu lead). Cadangan dibuat dulu.
    Jalan di latar supaya server langsung bisa dibuka.
    """
    try:
        hasil = db.pastikan_saring_terbaru(sebelum=backup.buat_backup)
        if hasil:
            print("  Saringan kualitas Klien Website: "
                  + ", ".join(f"{k} {v}" for k, v in hasil.most_common()))
    except Exception as e:
        print(f"  ⚠ Saringan kualitas gagal: {type(e).__name__}: {e}")


threading.Thread(target=_saring_awal, daemon=True).start()


# ─── Helper job ───────────────────────────────────────────────────────────────

def _bersihkan_job():
    sekarang = time.time()
    basi = [jid for jid, j in jobs.items()
            if j["status"] in ("done", "error", "dibatalkan")
            and sekarang - j.get("selesai_pada", sekarang) > UMUR_JOB_SELESAI]
    for jid in basi:
        jobs.pop(jid, None)


def _new_job() -> str:
    _bersihkan_job()
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {
        "status": "running",
        "progress": 0,
        "log": [],
        "found": 0,
        "filename": "",
        "error": "",
        "cancel": threading.Event(),
        "mulai_pada": time.time(),
    }
    return job_id


def _update(job_id, progress=None, message=None, found=None,
            status=None, filename=None, error=None):
    j = jobs.get(job_id)
    if not j:
        return
    if progress is not None:
        j["progress"] = int(progress)
    if message:
        j["log"].append(message)
    if found is not None:
        j["found"] = int(found)
    if status:
        j["status"] = status
        if status in ("done", "error", "dibatalkan"):
            j["selesai_pada"] = time.time()
    if filename:
        j["filename"] = filename
    if error:
        j["error"] = error


def _terima_should_stop(fungsi):
    """
    Apakah scraper ini menerima argumen should_stop?

    Diperiksa lewat signature, bukan dengan menangkap TypeError: TypeError yang
    muncul dari dalam scraper akan terlihat sama dan membuat run diulang.
    """
    import inspect
    try:
        return len(inspect.signature(fungsi).parameters) >= 3
    except (TypeError, ValueError):
        return False


def _jalankan(job_id, fungsi, params, label, jenis=""):
    """
    Pola bersama semua scraper: jalankan di thread, laporkan lewat SSE.

    `jenis` (mis. "komponen", "gmaps") membuat job bisa ditemukan lagi lewat
    /jobs/aktif — halaman yang dibuka ulang setelah user pindah menu menyambung
    kembali ke job yang masih berjalan, bukan menampilkan form kosong.
    """
    bisa_stop = _terima_should_stop(fungsi)
    if job_id in jobs:
        jobs[job_id].update(bisa_stop=bisa_stop, jenis=jenis, label=label)

    def run():
        def cb(pct, msg, found=0):
            _update(job_id, progress=pct, message=msg, found=found)

        def should_stop():
            j = jobs.get(job_id)
            return bool(j and j["cancel"].is_set())

        try:
            filename = (fungsi(params, cb, should_stop) if bisa_stop
                        else fungsi(params, cb))
            if should_stop():
                _update(job_id, status="dibatalkan", filename=filename,
                        message="⏹ Dihentikan — hasil sementara tetap disimpan.")
            elif filename:
                _update(job_id, progress=100, status="done", filename=filename,
                        message=f"✅ {label} selesai!")
            else:
                # Scraper mengembalikan None saat tidak ada satu pun hasil: tidak
                # ada file yang ditulis. Alasannya sudah dirinci di log job.
                _update(job_id, progress=100, status="done",
                        message=f"⚠ {label} selesai, tapi tidak ada hasil — "
                                f"lihat rincian di log. Tidak ada file dibuat.")
        except Exception as e:
            _update(job_id, status="error", error=str(e), message=f"❌ Error: {e}")

    threading.Thread(target=run, daemon=True).start()


# Job yang sudah selesai masih dilaporkan /jobs/aktif selama ini, supaya user
# yang kembali ke halaman scraper tetap melihat hasil & tombol download-nya.
TAMPIL_JOB_SELESAI = 15 * 60


def job_berjalan(jenis):
    """job_id scraping `jenis` yang masih berjalan, atau None."""
    for jid, j in jobs.items():
        if j.get("jenis") == jenis and j["status"] == "running":
            return jid
    return None


@app.route("/jobs/aktif")
def jobs_aktif():
    """Job yang sedang berjalan (dan yang baru selesai) untuk disambung ulang UI."""
    jenis = request.args.get("jenis") or None
    sekarang = time.time()
    hasil = []
    for jid, j in list(jobs.items()):
        if not j.get("jenis") or (jenis and j.get("jenis") != jenis):
            continue
        if j["status"] != "running" and                 sekarang - j.get("selesai_pada", sekarang) > TAMPIL_JOB_SELESAI:
            continue
        hasil.append({"job_id": jid, "jenis": j.get("jenis"), "label": j.get("label"),
                      "status": j["status"], "progress": j["progress"],
                      "found": j["found"], "filename": j["filename"],
                      "mulai_pada": j.get("mulai_pada"),
                      "selesai_pada": j.get("selesai_pada")})
    hasil.sort(key=lambda x: x["mulai_pada"] or 0, reverse=True)
    return jsonify({"jobs": hasil})


@app.route("/job/<job_id>/stop", methods=["POST"])
def job_stop(job_id):
    """
    Minta job berhenti. Scraper memeriksa sinyal ini di antara listing, jadi
    berhentinya rapi dan hasil yang sudah terkumpul tetap ditulis ke file + DB.
    """
    j = jobs.get(job_id)
    if not j:
        return jsonify({"ok": False, "error": "Job tidak ditemukan"}), 404
    if not j.get("bisa_stop", True):
        # Dulu Stop pada scraper seperti ini tetap "diterima", prosesnya jalan
        # terus sampai selesai, lalu hasilnya dilabeli "dibatalkan".
        return jsonify({"ok": False, "error": "Scraper ini tidak bisa dihentikan di "
                                              "tengah jalan — tunggu sampai selesai."})
    j["cancel"].set()
    _update(job_id, message="⏹ Perintah berhenti diterima...")
    return jsonify({"ok": True})


# ─── SSE — real-time progress ─────────────────────────────────────────────────

@app.route("/stream/<job_id>")
def stream(job_id):
    """Server-Sent Events untuk progress bar real-time."""
    def generate():
        last_idx = 0
        while True:
            j = jobs.get(job_id)
            if not j:
                yield f"data: {json.dumps({'error': 'Job tidak ditemukan'})}\n\n"
                return

            new_logs = j["log"][last_idx:]
            last_idx = len(j["log"])

            yield f"data: {json.dumps({'progress': j['progress'], 'found': j['found'], 'status': j['status'], 'logs': new_logs, 'filename': j['filename'], 'error': j['error']})}\n\n"

            if j["status"] in ("done", "error", "dibatalkan"):
                return
            time.sleep(0.4)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ─── Dashboard ────────────────────────────────────────────────────────────────

# Jumlah baris & kolom tiap file hasil, di-cache per (nama, mtime). Dulu SETIAP
# buka dashboard/hasil membaca seluruh isi semua xlsx dengan pandas — makin lama
# dipakai, makin lambat halamannya.
_info_berkas = {}


def _info_xlsx(f):
    kunci = (f.name, f.stat().st_mtime)
    if kunci not in _info_berkas:
        try:
            from openpyxl import load_workbook
            wb = load_workbook(f, read_only=True)
            ws = wb.worksheets[0]
            kepala = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
            _info_berkas[kunci] = (max((ws.max_row or 1) - 1, 0),
                                   [str(c) for c in kepala if c is not None][:6])
            wb.close()
        except Exception:
            _info_berkas[kunci] = ("?", [])
    return _info_berkas[kunci]


# Jenis file hasil dari awalan namanya → (label, kelas CSS badge).
JENIS_BERKAS = {
    "gmaps": ("Google Maps", "badge-gmaps"),
    "leads": ("Export Leads Web", "badge-gmaps"),
    "webslead": ("Website Leads", "badge-gmaps"),
    "social": ("Social Media", "badge-social"),
    "pricing": ("Price Comparison", "badge-pricing"),
    "komponen": ("Komponen", "badge-komponen"),
    "komponen-export": ("Export Komponen", "badge-komponen"),
}


def _jenis(nama):
    awalan = nama.split("_")[0]
    label, kelas = JENIS_BERKAS.get(awalan, ("File", "bg-secondary"))
    return {"type": awalan, "jenis_label": label, "jenis_kelas": kelas}


@app.route("/")
def dashboard():
    files = sorted(OUTPUT_DIR.glob("*.xlsx"), key=lambda f: f.stat().st_mtime, reverse=True)[:8]
    recent = []
    for f in files:
        rows, _ = _info_xlsx(f)
        recent.append({
            "name": f.name,
            "rows": rows,
            "date": datetime.fromtimestamp(f.stat().st_mtime).strftime("%d %b %Y %H:%M"),
            "size": f"{max(f.stat().st_size // 1024, 1)} KB",
            **_jenis(f.name),
        })
    import db_komponen
    return render_template("dashboard.html", recent=recent, stats=db.stats(),
                           stats_komponen=db_komponen.stats(),
                           backup_info=backup.terakhir())


@app.route("/backup", methods=["POST"])
def backup_sekarang():
    """Backup manual kedua database (tombol di Dashboard)."""
    try:
        backup.buat_backup()
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({"ok": True, "info": backup.terakhir()})


# ─── Data pemilih target (wilayah & jenis bisnis) ─────────────────────────────

_berkas_cache = {}


def _json_data(nama, kosong):
    """Baca satu berkas data JSON sekali lalu simpan di memori."""
    if nama not in _berkas_cache:
        berkas = DATA_DIR / nama
        if not berkas.exists():
            return kosong
        with open(berkas, encoding="utf-8") as f:
            _berkas_cache[nama] = json.load(f)
    return _berkas_cache[nama]


@app.route("/api/wilayah")
def api_wilayah():
    """Daftar kota/kabupaten/kecamatan untuk widget pemilih target."""
    return jsonify(_json_data("wilayah.json", {"provinsi": []}))


@app.route("/api/keywords")
def api_keywords():
    """
    Pustaka jenis bisnis untuk widget pemilih target.

    Kata kunci yang terlalu umum ("PT", "toko") menghasilkan kantor dan pabrik
    yang jarang membeli jasa website; daftar ini berisi sektor yang nilai
    transaksinya besar dan asetnya digitalnya biasanya lemah.
    """
    return jsonify(_json_data("keywords.json", {"sektor": []}))


# ─── Google Maps ──────────────────────────────────────────────────────────────

@app.route("/gmaps")
def gmaps():
    return render_template("gmaps.html")


@app.route("/gmaps/run", methods=["POST"])
def gmaps_run():
    from scrapers.gmaps import run_scrape
    job_id = _new_job()
    _jalankan(job_id, run_scrape, request.json, "Scraping Google Maps", jenis="gmaps")
    return jsonify({"job_id": job_id})


@app.route("/gmaps/terputus")
def gmaps_terputus():
    """
    Run terakhir yang mati sebelum semua targetnya selesai.

    Ditandai oleh db.init_db() saat app dijalankan lagi: run yang masih
    berstatus "berjalan" berarti prosesnya sudah tidak ada. Leadnya sudah aman
    di database — yang ditawarkan di sini adalah mengerjakan sisa targetnya.
    """
    run = db.run_terputus_terakhir()
    if not run:
        return jsonify({"ada": False})
    return jsonify({
        "ada": True,
        "run_id": run["run_id"],
        "mulai_pada": run.get("mulai_pada"),
        "target_selesai": run.get("target_index"),
        "total_target": run.get("total_target"),
        "sisa_target": run.get("sisa_target"),
        "jumlah_lead": run.get("jumlah_lead"),
    })


@app.route("/gmaps/lanjutkan", methods=["POST"])
def gmaps_lanjutkan():
    """
    Kerjakan sisa target sebuah run yang terputus, di bawah run_id yang SAMA.

    run_id dipertahankan supaya file hasil di akhir memuat seluruh lead run itu
    — termasuk yang dikumpulkan sebelum terputus — bukan cuma hasil sisanya.
    Kredensial (token proxy & API key Gemini) tidak pernah disimpan di database,
    jadi keduanya diambil ulang dari form yang sedang terbuka.
    """
    body = request.json or {}
    run_id = body.get("run_id")
    run = db.run_get(run_id)
    if not run:
        return jsonify({"ok": False, "error": "Run tidak ditemukan"}), 404

    params = dict(run.get("params") or {})
    semua_target = params.get("search_targets") or []
    sudah = int(run.get("target_index") or 0)
    sisa = semua_target[sudah:]
    if not sisa:
        return jsonify({"ok": False,
                        "error": "Run ini sudah menyelesaikan semua targetnya"}), 400

    params["search_targets"] = sisa
    params["_run_id"] = run_id
    params["_offset_target"] = sudah
    params["_total_target"] = int(run.get("total_target") or len(semua_target))
    for rahasia in ("apify_proxy_token", "gemini_api_key"):
        if body.get(rahasia):
            params[rahasia] = body[rahasia]

    from scrapers.gmaps import run_scrape
    job_id = _new_job()
    _jalankan(job_id, run_scrape, params, "Lanjutan scraping Google Maps", jenis="gmaps")
    return jsonify({"ok": True, "job_id": job_id, "sisa_target": len(sisa)})


# ─── Cek proxy Apify ──────────────────────────────────────────────────────────

@app.route("/check-proxy", methods=["POST"])
def check_proxy():
    """
    Buktikan proxy benar-benar jalan dengan mengambil IP publik lewat proxy itu.

    Tanpa tes ini, token yang salah baru ketahuan setelah scraping gagal total —
    dan gagalnya diam-diam, karena error proxy terlihat seperti listing timeout.
    """
    import urllib.parse

    import requests
    data = request.json or {}
    token = (data.get("apify_proxy_token") or "").strip()
    if not token:
        return jsonify({"ok": False, "error": "Apify API token belum diisi"})

    grup = data.get("apify_proxy_group") or "RESIDENTIAL"
    negara = (data.get("apify_proxy_country") or "").strip()
    bagian = [f"groups-{grup}"]
    if negara:
        bagian.append(f"country-{negara}")
    username = urllib.parse.quote(",".join(bagian), safe="")
    password = urllib.parse.quote(token, safe="")
    proxy_url = f"http://{username}:{password}@proxy.apify.com:8000"

    try:
        r = requests.get("https://api.ipify.org?format=json",
                         proxies={"http": proxy_url, "https": proxy_url}, timeout=25)
        r.raise_for_status()
        return jsonify({"ok": True, "ip": r.json().get("ip", "?")})
    except requests.exceptions.ProxyError:
        return jsonify({"ok": False,
                        "error": "Proxy menolak koneksi — token kemungkinan salah."})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"})


# ─── Price Comparison ─────────────────────────────────────────────────────────

@app.route("/pricing")
def pricing():
    return render_template("pricing.html")


@app.route("/pricing/run", methods=["POST"])
def pricing_run():
    from scrapers.pricing import run_price_comparison
    job_id = _new_job()
    _jalankan(job_id, run_price_comparison, request.json, "Price comparison")
    return jsonify({"job_id": job_id})


# ─── Database Toko (untuk mode scraping per-toko) ─────────────────────────────

@app.route("/pricing/stores", methods=["GET"])
def pricing_stores_list():
    from scrapers.pricing import load_stores
    return jsonify({"stores": load_stores()})


@app.route("/pricing/stores", methods=["POST"])
def pricing_stores_add():
    from scrapers.pricing import add_store
    data = request.json or {}
    try:
        entry = add_store(
            platform=data.get("platform", ""),
            nama=data.get("nama", ""),
            url=data.get("url", ""),
            is_own=data.get("is_own", False),
        )
        return jsonify({"ok": True, "store": entry})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/pricing/stores/own", methods=["POST"])
def pricing_stores_own():
    """Tandai toko sebagai milik sendiri / kompetitor.

    Penandaan ini menentukan baris mana dikeluarkan dari statistik pasar, jadi
    user harus bisa membetulkannya tanpa menghapus lalu menambah toko lagi.
    """
    from scrapers.pricing import set_store_own
    data = request.json or {}
    ok = set_store_own(data.get("id", ""), data.get("is_own", False))
    return jsonify({"ok": ok})


@app.route("/pricing/stores/delete", methods=["POST"])
def pricing_stores_delete():
    from scrapers.pricing import delete_store
    store_id = (request.json or {}).get("id", "")
    ok = delete_store(store_id)
    return jsonify({"ok": ok})


# ─── Cek Harga ────────────────────────────────────────────────────────────────

@app.route("/pricing/cek", methods=["POST"])
def pricing_cek():
    """Jawab 'harga saya baiknya pasang berapa' dari data yang sudah dipanen.

    Tidak membuka browser: user akan mengubah modal & persentase biaya berkali-
    kali, dan menunggu Chrome tiap kali akan membuat fitur ini tidak terpakai.
    """
    from scrapers.pricing import cek_harga
    d = request.json or {}

    def angka(nama):
        nilai = d.get(nama)
        if nilai in (None, "", "null"):
            return None
        try:
            return float(str(nilai).replace(".", "").replace(",", "."))
        except (TypeError, ValueError):
            return None

    try:
        hasil = cek_harga(
            kueri=d.get("kueri", ""),
            store_ids=d.get("store_ids") or None,
            modal=angka("modal"),
            kunci=d.get("kunci") or None,
            fee_persen=angka("fee_persen"),
            biaya_tetap=angka("biaya_tetap"),
            margin_target=angka("margin_target"),
        )
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
    # Baris produk mentah tidak ikut dikirim — hanya yang dipakai tabel.
    for kunci in ("rival", "milik"):
        hasil[kunci] = [
            {k: b.get(k) for k in ("nama_produk", "harga", "toko", "store_id",
                                   "terjual", "rating", "url", "platform",
                                   "is_own", "product_key")}
            for b in (hasil.get(kunci) or [])
        ]
    return jsonify(hasil)


@app.route("/pricing/cari-toko", methods=["POST"])
def pricing_cari_toko():
    """Panen bertarget: kueri → etalase yang cocok → panen kecil di tiap toko.

    Dijalankan sebagai job supaya progresnya mengalir lewat SSE yang sudah ada,
    dan tombol Stop ikut hidup.
    """
    from scrapers.mp_cari import cari_produk
    # Lewat _new_job: dulu job_id dibuat dengan uuid saja sehingga tidak pernah
    # masuk `jobs` — stream menjawab "Job tidak ditemukan", progres tidak pernah
    # tampil, dan tombol Stop mengembalikan 404.
    job_id = _new_job()
    _jalankan(job_id, cari_produk, request.json or {}, "Cari di toko")
    return jsonify({"job_id": job_id})


@app.route("/pricing/etalase", methods=["GET"])
def pricing_etalase():
    """Daftar etalase per toko, untuk dilihat & dipilih manual bila perlu."""
    return jsonify({"ok": True,
                    "etalase": db.mp_etalase_list(request.args.get("store_id") or None)})


@app.route("/pricing/biaya", methods=["GET", "POST"])
def pricing_biaya():
    if request.method == "GET":
        return jsonify({"ok": True, "biaya": db.mp_biaya()})
    d = request.json or {}
    try:
        db.mp_set_biaya(d.get("platform", ""), d.get("fee_persen", 0),
                        d.get("biaya_tetap", 0))
        return jsonify({"ok": True, "biaya": db.mp_biaya()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/pricing/modal", methods=["POST"])
def pricing_modal():
    d = request.json or {}
    key = d.get("product_key", "")
    if not key:
        return jsonify({"ok": False, "error": "product_key kosong"}), 400
    db.mp_set_modal(key, d.get("modal", 0), d.get("catatan", ""))
    return jsonify({"ok": True, "modal": db.mp_get_modal(key)})


@app.route("/pricing/gaya", methods=["GET"])
def pricing_gaya():
    from scrapers.pricing import gaya_toko
    return jsonify(gaya_toko(request.args.get("store_id") or None))


@app.route("/pricing/data", methods=["GET"])
def pricing_data():
    return jsonify({"ok": True, "stats": db.mp_stats()})


@app.route("/pricing/hapus-data", methods=["POST"])
def pricing_hapus_data():
    """Hapus data marketplace. Hanya tabel mp_*, tidak pernah menyentuh leads."""
    d = request.json or {}
    if (d.get("konfirmasi") or "").strip().upper() != "HAPUS":
        return jsonify({"ok": False,
                        "error": "Ketik HAPUS untuk konfirmasi."}), 400
    try:
        hasil = db.mp_hapus(d.get("scope", ""), d.get("nilai"))
        return jsonify({"ok": True, "terhapus": hasil, "stats": db.mp_stats()})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


# ─── Engine "Chrome Saya" (CDP) ───────────────────────────────────────────────

def _chrome_cfg():
    """Ambil konfigurasi Chrome dari config.py dengan default aman."""
    try:
        import config
    except Exception:
        config = None
    path = getattr(config, "CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    port = getattr(config, "CHROME_CDP_PORT", 9222)
    profile = getattr(config, "CHROME_PROFILE_DIR", "data/chrome_profile")
    return path, port, profile


@app.route("/pricing/launch-chrome", methods=["POST"])
def pricing_launch_chrome():
    """Buka Chrome dengan remote-debugging + profil khusus app (login persisten)."""
    import subprocess
    path, port, profile = _chrome_cfg()
    if not os.path.exists(path):
        return jsonify({"ok": False, "error": f"Chrome tidak ditemukan di {path}. "
                        f"Sesuaikan CHROME_PATH di config.py."}), 400
    profile_dir = str((Path(profile)).resolve())
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen([
            path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "https://shopee.co.id",
        ])
        return jsonify({"ok": True, "message": f"Chrome dibuka (port {port}). "
                        f"Login Shopee/Tokopedia di jendela itu, lalu jalankan scraping."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/pricing/chrome-status", methods=["GET"])
def pricing_chrome_status():
    """Cek apakah Chrome debug port sudah bisa disambung."""
    import socket
    _, port, _ = _chrome_cfg()
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=1.5):
            return jsonify({"connected": True, "port": port})
    except Exception:
        return jsonify({"connected": False, "port": port})


# ─── Social Media ─────────────────────────────────────────────────────────────

@app.route("/social")
def social():
    return render_template("social.html")


@app.route("/social/run", methods=["POST"])
def social_run():
    from scrapers.social import run_social_scrape
    job_id = _new_job()
    _jalankan(job_id, run_social_scrape, request.json, "Social media scraping")
    return jsonify({"job_id": job_id})


# ─── Gemini API key check ─────────────────────────────────────────────────────

@app.route("/check-gemini", methods=["POST"])
def check_gemini():
    api_key = ((request.json or {}).get("api_key") or "").strip()
    if not api_key:
        return jsonify({"ok": False, "error": "API key kosong"})
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents="Balas hanya dengan kata: OK"
        )
        return jsonify({"ok": True, "reply": (resp.text or "").strip()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ─── Website Leads ────────────────────────────────────────────────────────────

@app.route("/website-leads")
def website_leads_page():
    return render_template("website_leads.html")


@app.route("/website-leads/run", methods=["POST"])
def website_leads_run():
    from scrapers.website_leads import run_website_leads
    job_id = _new_job()
    _jalankan(job_id, run_website_leads, request.json, "Website Leads")
    return jsonify({"job_id": job_id})


# ─── CRM: database leads ──────────────────────────────────────────────────────

STATUS_PILIHAN = [
    "Belum Dihubungi", "WA Terkirim", "Email Terkirim", "Follow-up 1", "Follow-up 2",
    "Membalas", "Minta Penawaran", "Deal", "Tidak Tertarik", "Jangan Hubungi",
    # Status lama — tetap bisa dipilih supaya lead yang sudah memakainya terbaca.
    "Sudah Dihubungi", "Follow Up",
]


def _pengaturan_pesan():
    from scrapers import pesan_web
    return db.pengaturan(dict(pesan_web.BAWAAN, fu1_hari="3", fu2_hari="7"))


def _filter_dari_request(sumber=None):
    """
    Baca filter dari query string (atau dict `sumber`, mis. body JSON export).
    String kosong dianggap 'tanpa filter'.
    """
    a = request.args if sumber is None else sumber

    def s(nama):
        v = str(a.get(nama) or "").strip()
        return v or None

    def i(nama):
        v = str(a.get(nama) or "").strip()
        try:
            return int(v) if v else None
        except ValueError:
            return None

    return {
        "tier": s("tier"),
        "jasa_utama": s("jasa"),
        "area": s("area"),
        "status_leads": s("status"),
        "punya_wa": str(a.get("wa")) == "1",
        "skor_min": i("skor_min"),
        "skor_max": i("skor_max"),
        "q": s("q"),
        "run": s("run"),
        "kontak": s("kontak"),
        "website": s("website"),
        "kota": s("kota"),
        "kanal": s("kanal"),
        "tab": s("tab") if s("tab") != "semua" else None,
        "verif": s("verif"),
        "follow_up": str(a.get("fu")) == "1",
        # Tab "Sudah Dikontak" paling berguna diurutkan dari kontak terakhir.
        "urut": a.get("urut") or ("dihubungi" if s("tab") == "sudah" else "skor_pembeli"),
    }


def _siapkan_baris(r, atur):
    """
    Lengkapi satu lead untuk ditampilkan: tautan WhatsApp berisi pesan pembuka
    yang sudah dirakit (teks dari "Pengaturan pesan"), nomor WA 08xx, dan
    semua email.
    """
    from urllib.parse import quote

    from scrapers import kontak, pesan_web
    if r.get("whatsapp_link"):
        pesan = pesan_web.isi_pesan(r, atur)["wa"]["isi"]
        r["wa_pitch"] = r["whatsapp_link"].split("?")[0] + "?text=" + quote(pesan)
    else:
        r["wa_pitch"] = ""
    r["nomor_wa"] = kontak.nomor_wa(r)
    r["email_semua"] = kontak.email_semua(r)
    return r


@app.route("/leads")
def leads():
    from scrapers.scoring import URUTAN_TIER

    filters = _filter_dari_request()
    per_halaman = 50
    try:
        halaman = max(int(request.args.get("hal", 1) or 1), 1)
    except ValueError:
        halaman = 1

    rows, total = db.query_leads(**filters, limit=per_halaman,
                                 offset=(halaman - 1) * per_halaman)

    # Siapkan tautan WhatsApp berisi pesan pembuka yang sudah dirakit per lead —
    # inilah yang mengubah tabel data jadi alat penjualan. Teksnya dari template
    # yang diedit user di "Pengaturan pesan".
    atur = _pengaturan_pesan()
    for r in rows:
        _siapkan_baris(r, atur)

    total_halaman = max((total + per_halaman - 1) // per_halaman, 1)
    return render_template(
        "leads.html",
        leads=rows, total=total, halaman=halaman, total_halaman=total_halaman,
        filters=filters,
        args={k: v for k, v in request.args.items() if k not in ("hal", "kosong")},
        tabs=db.TAB_LEADS_WEB, tab_aktif=filters["tab"] or "semua",
        jumlah_tab=db.hitung_tab_leads(**filters),
        opsi_kota=db.nilai_unik("kota"),
        opsi_tier=[t for t in URUTAN_TIER],
        opsi_jasa=db.nilai_unik("jasa_utama"),
        opsi_area=db.nilai_unik("area_pencarian"),
        opsi_status=STATUS_PILIHAN,
        stats=db.stats(),
        kosong=request.args.get("kosong") == "1",
    )


# ─── Verifikasi manual & saringan kualitas ────────────────────────────────────

@app.route("/leads/verifikasi")
def leads_verifikasi():
    """Antrean verifikasi: satu lead per layar, filter halaman Leads ikut terbawa."""
    args = {k: v for k, v in request.args.items() if k not in ("hal", "tab")}
    return render_template("leads_verifikasi.html", args=args, stats=db.stats())


@app.route("/leads/verifikasi/berikut")
def leads_verifikasi_berikut():
    """Lead berikutnya yang belum diverifikasi (skor tertinggi dulu)."""
    filters = _filter_dari_request()
    filters["tab"] = None
    filters["verif"] = "belum"
    kecuali = [k for k in (request.args.get("kecuali") or "").split(",") if k]
    rows, sisa = db.query_leads(**filters, kecuali=kecuali, limit=1)
    _, total = db.query_leads(**filters, limit=1)
    if not rows:
        return jsonify({"ok": True, "lead": None, "sisa": 0, "total": total})
    lead = _siapkan_baris(rows[0], _pengaturan_pesan())
    return jsonify({"ok": True, "lead": lead, "sisa": sisa, "total": total})


@app.route("/leads/verifikasi", methods=["POST"])
def leads_verifikasi_simpan():
    """
    hasil: "valid" | "tidak_valid" (hapus + jangan ambil lagi) | "kembalikan"
    (dari tab Disaring → valid) | "batal" (urungkan Valid → belum).
    """
    d = request.get_json(silent=True) or {}
    keys = d.get("keys") or ([d["place_key"]] if d.get("place_key") else [])
    if not keys:
        return jsonify({"ok": False, "error": "Pilih minimal satu lead."}), 400
    hasil = d.get("hasil")
    if hasil in ("valid", "kembalikan"):
        n = db.set_verifikasi(keys, "valid")
    elif hasil == "batal":
        n = db.set_verifikasi(keys, "")
    elif hasil == "tidak_valid":
        n = db.hapus_leads(keys, jangan_ambil_lagi=True, alasan="tidak valid (verifikasi)")
    else:
        return jsonify({"ok": False, "error": "Hasil verifikasi tidak dikenal."}), 400
    return jsonify({"ok": True, "jumlah": n})


def _saring_ulang(params, cb):
    cb(5, "Menyaring ulang seluruh lead Klien Website...", 0)
    hasil = db.saring_kualitas(cb=lambda m: cb(None, m, None))
    for k, v in hasil.most_common():
        cb(None, f"  {k}: {v}", None)
    return "saring"


def _cek_email_mx(params, cb, should_stop):
    """Cek MX semua domain email lead, buang email ke domain mati, saring ulang."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from scrapers import kualitas
    domain = db.domain_email_belum_dicek()
    if not domain:
        cb(100, "Semua email sudah pernah dicek.", 0)
        return "cek-email"
    cb(2, f"Mengecek {len(domain)} domain email (MX record)...", 0)
    hasil = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        tugas = {ex.submit(kualitas.cek_domain_email, d): d for d in domain}
        for i, f in enumerate(as_completed(tugas), 1):
            if should_stop():
                for t in tugas:
                    t.cancel()
                break
            d = tugas[f]
            try:
                hasil[d] = f.result()
            except Exception:
                hasil[d] = None
            if hasil[d] is False:
                cb(None, f"✗ {d} tidak bisa menerima email", None)
            if i % 20 == 0:
                cb(int(5 + 80 * i / len(domain)), f"{i}/{len(domain)} domain dicek", i)
    mati = sum(1 for v in hasil.values() if v is False)
    baris, dibuang = db.terapkan_cek_email(hasil)
    cb(88, f"{mati} domain mati · {dibuang} email dibuang dari {baris} lead", len(hasil))
    cb(90, "Menyaring ulang (lead yang kini tanpa kontak ikut disaring)...", len(hasil))
    db.saring_kualitas()
    return "cek-email"


@app.route("/leads/saring-ulang", methods=["POST"])
def leads_saring_ulang():
    if job_berjalan("gmaps") or job_berjalan("saring"):
        return jsonify({"ok": False, "error": "Tunggu scraping / penyaringan yang sedang "
                                              "berjalan selesai dulu."}), 409
    job_id = _new_job()
    _jalankan(job_id, _saring_ulang, {}, "Saring ulang", jenis="saring")
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/leads/cek-email", methods=["POST"])
def leads_cek_email():
    if job_berjalan("saring"):
        return jsonify({"ok": False, "error": "Penyaringan lain sedang berjalan."}), 409
    job_id = _new_job()
    _jalankan(job_id, _cek_email_mx, {}, "Cek email (MX)", jenis="saring")
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/leads/hapus", methods=["POST"])
def leads_hapus():
    """
    Hapus lead klien website: `keys` (per baris / terpilih), atau semua lead yang
    cocok dengan `filter` (wajib ketik HAPUS). Tanpa filter = seluruh isi
    database, termasuk baris tersembunyi.
    """
    d = request.json or {}
    jangan = bool(d.get("jangan_ambil_lagi"))
    if d.get("keys"):
        n = db.hapus_leads(d["keys"], jangan_ambil_lagi=jangan)
        return jsonify({"ok": True, "terhapus": n, "stats": db.stats()})

    if (d.get("konfirmasi") or "").strip().upper() != "HAPUS":
        return jsonify({"ok": False, "error": "Ketik HAPUS untuk konfirmasi."}), 400
    if job_berjalan("gmaps"):
        return jsonify({"ok": False, "error": "Scraping Google Maps sedang berjalan. "
                                              "Hentikan atau tunggu selesai dulu."}), 409
    filters = _filter_dari_request(d.get("filter") or {})
    ada_filter = any(v for k, v in filters.items() if k != "urut")
    if ada_filter:
        rows, _ = db.query_leads(**filters, limit=0)
        keys = [r["place_key"] for r in rows]
    else:
        keys = db.semua_keys()
    lewati = db.kosongkan_lewati() if d.get("kosongkan_lewati") else 0
    n = db.hapus_leads(keys, jangan_ambil_lagi=jangan)
    return jsonify({"ok": True, "terhapus": n, "lewati_dikosongkan": lewati,
                    "stats": db.stats()})


@app.route("/leads/update", methods=["POST"])
def leads_update():
    data = request.json or {}
    place_key = data.get("place_key")
    if not place_key:
        return jsonify({"ok": False, "error": "place_key kosong"}), 400
    status = data.get("status", data.get("status_leads"))
    if status is not None and status not in STATUS_PILIHAN:
        return jsonify({"ok": False, "error": "Status tidak dikenal"}), 400
    atur = _pengaturan_pesan()
    # "WA/Email Terkirim" & "Follow-up 1" mengisi jadwal follow-up otomatis.
    row = db.update_crm(place_key, status=status, catatan=data.get("catatan"),
                        tanggal_follow_up=data.get("tanggal_follow_up"),
                        nama_pic=data.get("nama_pic"),
                        fu1_hari=atur["fu1_hari"], fu2_hari=atur["fu2_hari"])
    if not row:
        return jsonify({"ok": False, "error": "Lead tidak ditemukan"}), 404
    return jsonify({"ok": True, "status": row.get("status_leads"),
                    "tanggal_follow_up": row.get("tanggal_follow_up") or "",
                    "tanggal_dihubungi": row.get("tanggal_dihubungi") or ""})


@app.route("/leads/pin", methods=["POST"])
def leads_pin():
    """Pasang/lepas PIN "lead berpotensi" untuk satu atau banyak lead."""
    d = request.json or {}
    keys = d.get("keys") or ([d["place_key"]] if d.get("place_key") else [])
    if not keys:
        return jsonify({"ok": False, "error": "Pilih minimal satu lead."}), 400
    n = db.set_pin(keys, bool(d.get("dipin", True)))
    return jsonify({"ok": True, "jumlah": n, "dipin": bool(d.get("dipin", True))})


@app.route("/leads/pesan/<path:place_key>")
def leads_pesan(place_key):
    """Template WA & email yang sudah terisi untuk satu lead (popup salin)."""
    from scrapers import kontak, pesan_web
    row = db.get(place_key)
    if not row:
        return jsonify({"ok": False, "error": "Lead tidak ditemukan"}), 404
    return jsonify({
        "ok": True, "bagian": pesan_web.isi_pesan(row, _pengaturan_pesan()),
        "urutan": list(pesan_web.URUTAN),
        "nama": row.get("nama_bisnis"), "emails": kontak.email_semua(row),
        "nomor_wa": kontak.nomor_wa(row), "wa_link": row.get("whatsapp_link") or "",
        "nama_pic": row.get("nama_pic") or "", "status": row.get("status_leads") or "",
    })


@app.route("/leads/pengaturan-pesan", methods=["GET", "POST"])
def leads_pengaturan_pesan():
    """Identitas pengirim & teks template pesan klien website."""
    from scrapers import pesan_web
    bawaan = dict(pesan_web.BAWAAN, fu1_hari="3", fu2_hari="7")
    if request.method == "GET":
        return jsonify({"pengaturan": db.pengaturan(bawaan),
                        "placeholder": pesan_web.PLACEHOLDER})
    d = request.json or {}
    for k in ("fu1_hari", "fu2_hari"):
        if k in d and not str(d[k]).strip().isdigit():
            return jsonify({"ok": False, "error": "Hari follow-up harus berupa angka."}), 400
    return jsonify({"ok": True, "pengaturan": db.simpan_pengaturan(bawaan, d)})


@app.route("/leads/detail/<path:place_key>")
def leads_detail(place_key):
    row = db.get(place_key)
    if not row:
        return jsonify({"error": "Lead tidak ditemukan"}), 404
    row["perubahan"] = db.perubahan_terbaru(place_key, batas=20)
    return jsonify(row)


@app.route("/leads/export/kolom")
def leads_export_kolom():
    import ekspor
    return jsonify(ekspor.katalog("webdev"))


@app.route("/leads/export", methods=["POST"])
def leads_export_pilih():
    """Export dengan kolom pilihan user (dari modal pemilih kolom)."""
    import ekspor
    d = request.json or {}
    filters = _filter_dari_request(d.get("filter") or {})
    rows, _ = db.query_leads(**filters, limit=0, tanpa_opt_out=True)
    ringkasan = {"Diekspor pada": datetime.now().strftime("%d-%m-%Y %H:%M")}
    nama, n = ekspor.tulis(rows, "webdev", d.get("kolom") or [],
                           fmt=d.get("format") or "xlsx", wajib=d.get("wajib") or "",
                           awalan="leads", ringkasan=ringkasan)
    if not nama:
        return jsonify({"ok": False, "error": "Tidak ada lead yang cocok — periksa filter "
                                              "atau syarat kontak export."}), 404
    return jsonify({"ok": True, "jumlah": n, "url": url_for("download", filename=nama)})


@app.route("/leads/export")
def leads_export():
    """Ekspor hasil filter yang sedang aktif ke Excel, memakai writer yang sama."""
    from scrapers.gmaps import COLUMN_ORDER, _rapikan, _save_excel

    filters = _filter_dari_request()
    rows, total = db.query_leads(**filters, limit=0, tanpa_opt_out=True)
    if not rows:
        # Dulu browser menampilkan JSON mentah. Kembali ke halaman Leads dengan
        # pesan yang bisa dibaca.
        args = {k: v for k, v in request.args.items() if k != "hal"}
        return redirect(url_for("leads", kosong=1, **args))

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    nama = f"leads_{timestamp}.xlsx"
    ringkasan = {"Total lead diekspor": total}
    for k, v in filters.items():
        if v not in (None, "", False):
            ringkasan[f"Filter — {k}"] = v
    _save_excel({"Leads": _rapikan(rows, COLUMN_ORDER)},
                str(OUTPUT_DIR / f"leads_{timestamp}"), ringkasan)
    return redirect(url_for("download", filename=nama))


# ─── Results ──────────────────────────────────────────────────────────────────

@app.route("/results")
def results():
    files = sorted(OUTPUT_DIR.glob("*.xlsx"), key=lambda f: f.stat().st_mtime, reverse=True)
    file_list = []
    for f in files:
        rows, cols = _info_xlsx(f)
        file_list.append({
            "name": f.name,
            "rows": rows,
            "date": datetime.fromtimestamp(f.stat().st_mtime).strftime("%d %b %Y %H:%M"),
            "size": f"{max(f.stat().st_size // 1024, 1)} KB",
            "cols": cols,
            **_jenis(f.name),
        })
    return render_template("results.html", files=file_list)


def _berkas_output(filename):
    """
    Petakan nama file ke dalam folder output, tolak yang mencoba keluar darinya.

    Tanpa pemeriksaan ini, "../config.py" akan terbaca sebagai path yang sah dan
    isi file mana pun di komputer bisa diunduh lewat browser.
    """
    akar = OUTPUT_DIR.resolve()
    calon = (akar / filename).resolve()
    if calon.parent != akar or not calon.is_file():
        return None
    return calon


@app.route("/download/<path:filename>")
def download(filename):
    berkas = _berkas_output(filename)
    if berkas is None:
        abort(404)
    return send_file(berkas, as_attachment=True, download_name=berkas.name)


@app.route("/preview/<path:filename>")
def preview(filename):
    berkas = _berkas_output(filename)
    if berkas is None:
        return jsonify({"error": "File tidak ditemukan"}), 404
    try:
        import pandas as pd
        df = pd.read_excel(berkas)
        rows = df.head(50).fillna("").to_dict(orient="records")
        return jsonify({"columns": list(df.columns), "rows": rows, "total": len(df)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Menu Distributor Komponen ────────────────────────────────────────────────

import komponen_routes  # noqa: E402

komponen_routes.pasang(app, _new_job, _jalankan, job_berjalan)


# ─── Menu Dokumen & Surat ─────────────────────────────────────────────────────

import dokumen_routes  # noqa: E402

dokumen_routes.pasang(app)


# ─── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "="*50)
    print("  LeadScraper Pro — Web App")
    print("  Buka browser: http://localhost:5000")
    print("="*50 + "\n")
    app.run(debug=False, host="0.0.0.0", port=5000, threaded=True)
