"""
scraper_gmaps.py — Jalankan scraper Google Maps dari terminal.

    pip install -r requirements.txt
    python -m playwright install chromium
    python scraper_gmaps.py

Semua pengaturan dibaca dari config.py (SEARCH_TARGETS, filter, proxy, dst).

Berkas ini sengaja tipis: seluruh logika scraping ada di scrapers/gmaps.py yang
juga dipakai oleh web app. Dulu file ini menyalin ulang seluruh pipeline
(selector, penilaian, penulisan Excel), sehingga setiap kali Google mengubah
tampilannya perbaikan harus dikerjakan dua kali dan satu salinan selalu
tertinggal.
"""
import signal
import sys
from datetime import datetime

import config
from scrapers.gmaps import run_scrape


def _targets_dari_config():
    """SEARCH_TARGETS berisi tuple ("jenis", "area") atau ("jenis", "area", "lat,long")."""
    targets = []
    for t in getattr(config, "SEARCH_TARGETS", []):
        if isinstance(t, (list, tuple)) and len(t) >= 2:
            targets.append({
                "jenis": t[0],
                "area": t[1],
                "coords": t[2] if len(t) > 2 else "",
            })
        elif isinstance(t, dict):
            targets.append(t)
    return targets


def _params_dari_config():
    return {
        "search_targets": _targets_dari_config(),
        "max_results": getattr(config, "MAX_RESULTS", 20),
        "radius_km": getattr(config, "SEARCH_RADIUS_KM", 0),

        "filter_rating": getattr(config, "FILTER_RATING", True),
        "min_rating": getattr(config, "MIN_RATING", 0),
        "filter_reviews": getattr(config, "FILTER_REVIEWS", True),
        "min_reviews": getattr(config, "MIN_REVIEWS", 0),
        "mode_kontak": getattr(config, "MODE_KONTAK", ""),
        "require_phone": getattr(config, "REQUIRE_PHONE", True),
        "require_whatsapp": getattr(config, "REQUIRE_WHATSAPP", True),
        "require_no_website": getattr(config, "REQUIRE_NO_WEBSITE", True),
        "sertakan_bisnis_tutup": getattr(config, "SERTAKAN_BISNIS_TUTUP", False),
        "listing_konkuren": getattr(config, "LISTING_KONKUREN", 3),

        "dedup_enabled": getattr(config, "DEDUP_ENABLED", True),
        "dedup_bulan": getattr(config, "DEDUP_BULAN", 6),
        "refresh_kontak": getattr(config, "REFRESH_KONTAK", True),
        "refresh_interval_hari": getattr(config, "REFRESH_INTERVAL_HARI", 30),
        "enrich_website": getattr(config, "ENRICH_WEBSITE", True),

        "output_format": getattr(config, "OUTPUT_FORMAT", "excel"),
        "headless": getattr(config, "HEADLESS", False),
        "use_proxy": getattr(config, "USE_PROXY", False),
        "rotate_ip_per_listing": getattr(config, "ROTATE_IP_PER_LISTING", True),
    }


def _siapkan_stdout():
    """
    Pesan progress memakai emoji (✨ ⏭ ⚠). Saat output dialihkan ke file atau
    pipe, Windows memilih encoding lokal (cp1252) yang tidak punya karakter itu,
    dan `print` melempar UnicodeEncodeError — jadi `python scraper_gmaps.py > log.txt`
    mati di tengah jalan. Di konsol biasa ini tidak pernah terlihat.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _cetak(pct, pesan, ditemukan=0):
    """Callback progress versi terminal — bentuknya sama dengan yang dipakai web."""
    if not pesan:
        return
    waktu = datetime.now().strftime("%H:%M:%S")
    kolom = f"[{pct:>3}%]" if pct is not None else "      "
    print(f"{waktu} {kolom} {pesan}", flush=True)


_berhenti = False


def _minta_berhenti(signum, frame):
    """
    Ctrl+C pertama = berhenti rapi, hasil yang sudah terkumpul tetap ditulis ke
    Excel + database. Dulu Ctrl+C membatalkan seluruh run dan semua lead yang
    sudah dikumpulkan hangus — versi web sudah punya tombol Stop yang menyimpan,
    terminal tidak. Ctrl+C kedua tetap memaksa keluar.
    """
    global _berhenti
    if _berhenti:
        raise KeyboardInterrupt
    _berhenti = True
    print("\n⏹ Menghentikan setelah listing ini — hasil tetap disimpan. "
          "(Ctrl+C sekali lagi untuk paksa keluar)", flush=True)


def main():
    _siapkan_stdout()
    params = _params_dari_config()
    if not params["search_targets"]:
        print("SEARCH_TARGETS di config.py masih kosong — isi dulu targetnya.")
        return 1

    print("=" * 60)
    print("  LeadScraper Pro — Google Maps (mode terminal)")
    print(f"  {len(params['search_targets'])} target · maks {params['max_results']} hasil per target")
    if params["dedup_enabled"]:
        print(f"  Anti-duplikat aktif: lewati bisnis yang sudah ada "
              f"< {params['dedup_bulan']} bulan")
    print("  Tekan Ctrl+C untuk berhenti — hasil sementara tetap disimpan.")
    print("=" * 60)

    signal.signal(signal.SIGINT, _minta_berhenti)
    try:
        nama_file = run_scrape(params, _cetak, lambda: _berhenti)
    except KeyboardInterrupt:
        print("\nDihentikan paksa — hasil run ini tidak disimpan.")
        return 130

    status = "Dihentikan" if _berhenti else "Selesai"
    print("=" * 60)
    if nama_file:
        print(f"  {status}. Hasil: output/{nama_file}")
    else:
        # Bukan error: run berjalan normal, hasilnya saja yang nol. Sebabnya sudah
        # dirinci di log oleh scraper, jadi di sini tidak perlu diulang.
        print(f"  {status} — tidak ada lead yang lolos, jadi tidak ada file dibuat.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
