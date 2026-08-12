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

import db

app = Flask(__name__)
app.secret_key = "leadscraper-2024-secret"

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)
DATA_DIR = Path("data")

# Status semua job scraping yang sedang/pernah berjalan
# Format: {job_id: {"status", "progress", "log", "found", "filename", "error", "cancel"}}
jobs: dict = {}

# Job yang sudah selesai dibuang setelah sekian detik supaya dict tidak
# tumbuh tanpa batas selama server hidup berhari-hari.
UMUR_JOB_SELESAI = 3600

db.init_db()


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


def _jalankan(job_id, fungsi, params, label):
    """Pola bersama semua scraper: jalankan di thread, laporkan lewat SSE."""
    bisa_stop = _terima_should_stop(fungsi)

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
            else:
                _update(job_id, progress=100, status="done", filename=filename,
                        message=f"✅ {label} selesai!")
        except Exception as e:
            _update(job_id, status="error", error=str(e), message=f"❌ Error: {e}")

    threading.Thread(target=run, daemon=True).start()


@app.route("/job/<job_id>/stop", methods=["POST"])
def job_stop(job_id):
    """
    Minta job berhenti. Scraper memeriksa sinyal ini di antara listing, jadi
    berhentinya rapi dan hasil yang sudah terkumpul tetap ditulis ke file + DB.
    """
    j = jobs.get(job_id)
    if not j:
        return jsonify({"ok": False, "error": "Job tidak ditemukan"}), 404
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

@app.route("/")
def dashboard():
    import pandas as pd
    files = sorted(OUTPUT_DIR.glob("*.xlsx"), key=lambda f: f.stat().st_mtime, reverse=True)[:8]
    recent = []
    for f in files:
        try:
            df = pd.read_excel(f)
            rows = len(df)
        except Exception:
            rows = "?"
        recent.append({
            "name": f.name,
            "rows": rows,
            "date": datetime.fromtimestamp(f.stat().st_mtime).strftime("%d %b %Y %H:%M"),
            "size": f"{max(f.stat().st_size // 1024, 1)} KB",
            "type": f.name.split("_")[0],
        })
    return render_template("dashboard.html", recent=recent, stats=db.stats())


# ─── Data wilayah (pemilih kota/kecamatan) ────────────────────────────────────

_wilayah_cache = None


@app.route("/api/wilayah")
def api_wilayah():
    """Daftar kota/kabupaten/kecamatan untuk widget pemilih target."""
    global _wilayah_cache
    if _wilayah_cache is None:
        berkas = DATA_DIR / "wilayah.json"
        if not berkas.exists():
            return jsonify({"provinsi": []})
        with open(berkas, encoding="utf-8") as f:
            _wilayah_cache = json.load(f)
    return jsonify(_wilayah_cache)


# ─── Google Maps ──────────────────────────────────────────────────────────────

@app.route("/gmaps")
def gmaps():
    return render_template("gmaps.html")


@app.route("/gmaps/run", methods=["POST"])
def gmaps_run():
    from scrapers.gmaps import run_scrape
    job_id = _new_job()
    _jalankan(job_id, run_scrape, request.json, "Scraping")
    return jsonify({"job_id": job_id})


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
        )
        return jsonify({"ok": True, "store": entry})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/pricing/stores/delete", methods=["POST"])
def pricing_stores_delete():
    from scrapers.pricing import delete_store
    store_id = (request.json or {}).get("id", "")
    ok = delete_store(store_id)
    return jsonify({"ok": ok})


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
            model="gemini-2.0-flash",
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

STATUS_PILIHAN = ["Belum Dihubungi", "Sudah Dihubungi", "Follow Up",
                  "Deal", "Tidak Tertarik"]


def _filter_dari_request():
    """Baca filter dari query string. String kosong dianggap 'tanpa filter'."""
    def s(nama):
        v = (request.args.get(nama) or "").strip()
        return v or None

    def i(nama):
        v = (request.args.get(nama) or "").strip()
        try:
            return int(v) if v else None
        except ValueError:
            return None

    return {
        "tier": s("tier"),
        "jasa_utama": s("jasa"),
        "area": s("area"),
        "status_leads": s("status"),
        "punya_wa": request.args.get("wa") == "1",
        "skor_min": i("skor_min"),
        "skor_max": i("skor_max"),
        "q": s("q"),
        "urut": request.args.get("urut") or "skor_pembeli",
    }


@app.route("/leads")
def leads():
    import config as cfg
    from scrapers.scoring import URUTAN_TIER, pesan_wa

    filters = _filter_dari_request()
    per_halaman = 50
    halaman = max(int(request.args.get("hal", 1) or 1), 1)

    rows, total = db.query_leads(**filters, limit=per_halaman,
                                 offset=(halaman - 1) * per_halaman)

    # Siapkan tautan WhatsApp berisi pesan pembuka yang sudah dirakit per lead —
    # inilah yang mengubah tabel data jadi alat penjualan.
    template = getattr(cfg, "PESAN_TEMPLATE", "")
    pengirim = getattr(cfg, "PESAN_PENGIRIM", "")
    usaha = getattr(cfg, "PESAN_USAHA", "")
    for r in rows:
        if r.get("whatsapp_link"):
            from urllib.parse import quote
            pesan = pesan_wa(r, pengirim=pengirim, usaha=usaha, template=template)
            r["wa_pitch"] = r["whatsapp_link"].split("?")[0] + "?text=" + quote(pesan)
        else:
            r["wa_pitch"] = ""

    total_halaman = max((total + per_halaman - 1) // per_halaman, 1)
    return render_template(
        "leads.html",
        leads=rows, total=total, halaman=halaman, total_halaman=total_halaman,
        filters=filters, args=request.args,
        opsi_tier=[t for t in URUTAN_TIER],
        opsi_jasa=db.nilai_unik("jasa_utama"),
        opsi_area=db.nilai_unik("area_pencarian"),
        opsi_status=STATUS_PILIHAN,
        stats=db.stats(),
    )


@app.route("/leads/update", methods=["POST"])
def leads_update():
    data = request.json or {}
    place_key = data.get("place_key")
    if not place_key:
        return jsonify({"ok": False, "error": "place_key kosong"}), 400
    ok = db.update_status(
        place_key,
        status_leads=data.get("status_leads"),
        catatan=data.get("catatan"),
        tanggal_follow_up=data.get("tanggal_follow_up"),
    )
    return jsonify({"ok": ok})


@app.route("/leads/detail/<path:place_key>")
def leads_detail(place_key):
    row = db.get(place_key)
    if not row:
        return jsonify({"error": "Lead tidak ditemukan"}), 404
    row["perubahan"] = db.perubahan_terbaru(place_key, batas=20)
    return jsonify(row)


@app.route("/leads/export")
def leads_export():
    """Ekspor hasil filter yang sedang aktif ke Excel, memakai writer yang sama."""
    from scrapers.gmaps import COLUMN_ORDER, _rapikan, _save_excel

    filters = _filter_dari_request()
    rows, total = db.query_leads(**filters, limit=100000, offset=0)
    if not rows:
        return jsonify({"error": "Tidak ada lead yang cocok dengan filter"}), 404

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
        try:
            import pandas as pd
            df = pd.read_excel(f)
            rows = len(df)
            cols = list(df.columns)[:6]
        except Exception:
            rows = "?"
            cols = []
        file_list.append({
            "name": f.name,
            "rows": rows,
            "date": datetime.fromtimestamp(f.stat().st_mtime).strftime("%d %b %Y %H:%M"),
            "size": f"{max(f.stat().st_size // 1024, 1)} KB",
            "cols": cols,
            "type": f.name.split("_")[0],
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


# ─── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "="*50)
    print("  LeadScraper Pro — Web App")
    print("  Buka browser: http://localhost:5000")
    print("="*50 + "\n")
    app.run(debug=False, host="0.0.0.0", port=5000, threaded=True)
