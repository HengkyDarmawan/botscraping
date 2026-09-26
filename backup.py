"""
backup.py — Cadangan otomatis database lead.

leads.db dan komponen.db tidak lagi disimpan di git (data kerja, bukan kode),
jadi cadangan dibuat di sini: sekali sehari saat app dinyalakan, atau manual
lewat tombol "Backup sekarang" di Dashboard.

Salinan dibuat dengan API backup SQLite, bukan dengan menyalin file: aman walau
server sedang menulis dan walau sebagian data masih di file -wal. Hasilnya
dimampatkan jadi satu zip per backup; 14 terbaru disimpan.
"""
import sqlite3
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path

import db

FOLDER = Path(__file__).resolve().parent / "data" / "backup"
SIMPAN = 14
_kunci = threading.Lock()


def daftar():
    """Zip backup yang ada, terbaru dulu."""
    if not FOLDER.exists():
        return []
    return sorted(FOLDER.glob("backup_*.zip"), reverse=True)


def terakhir():
    """Info backup terbaru untuk Dashboard, atau None."""
    semua = daftar()
    if not semua:
        return None
    f = semua[0]
    return {
        "nama": f.name,
        "waktu": datetime.fromtimestamp(f.stat().st_mtime).strftime("%d %b %Y %H:%M"),
        "ukuran": f"{f.stat().st_size / 1_048_576:.1f} MB",
        "jumlah": len(semua),
    }


def buat_backup():
    """Buat satu zip berisi salinan semua database ruang. Return path zip."""
    with _kunci:
        FOLDER.mkdir(parents=True, exist_ok=True)
        stempel = datetime.now().strftime("%Y%m%d_%H%M%S")
        tujuan = FOLDER / f"backup_{stempel}.zip"
        with tempfile.TemporaryDirectory() as tmp, \
                zipfile.ZipFile(tujuan, "w", zipfile.ZIP_DEFLATED) as z:
            for ruang, asal in db.RUANG_PATH.items():
                if not Path(asal).exists():
                    continue
                salinan = Path(tmp) / Path(asal).name
                sumber = sqlite3.connect(f"file:{asal}?mode=ro", uri=True)
                target = sqlite3.connect(salinan)
                try:
                    sumber.backup(target)
                finally:
                    target.close()
                    sumber.close()
                z.write(salinan, Path(asal).name)
        for lama in daftar()[SIMPAN:]:
            try:
                lama.unlink()
            except OSError:
                pass
        return tujuan


def backup_harian():
    """Buat backup kalau hari ini belum ada. Return path zip baru, atau None."""
    hari_ini = datetime.now().strftime("%Y%m%d")
    if any(f.name.startswith(f"backup_{hari_ini}") for f in daftar()):
        return None
    return buat_backup()


def backup_harian_latar():
    """Jalankan backup_harian di thread latar supaya start app tidak tertahan."""
    def kerja():
        try:
            backup_harian()
        except Exception as e:  # cadangan gagal tidak boleh menjatuhkan app
            print(f"⚠ Backup harian gagal: {type(e).__name__}: {e}")
    threading.Thread(target=kerja, daemon=True).start()
