"""
scrapers/website_leads.py — Cari calon klien jasa website
Sumber: Google Maps (bisnis tanpa website) + Gemini AI Search (intent posts)
"""
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from fake_useragent import UserAgent
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

OUTPUT_DIR = Path("output")

COLUMN_ORDER = [
    "sumber", "nama", "telepon", "whatsapp_link", "email",
    "alamat", "kategori_bisnis", "mengapa_butuh_website",
    "detail_post", "potensi",
    "status_leads", "tanggal_scraping", "source_url",
]

_STEALTH = Stealth()


def _wa_link_local(phone_raw: str) -> str:
    if not phone_raw or phone_raw in ("-", ""):
        return ""
    digits = re.sub(r"\D", "", str(phone_raw))
    if not digits:
        return ""
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    elif not digits.startswith("62"):
        digits = "62" + digits
    return f"https://wa.me/{digits}"


def _potensi_gmaps(rating, ulasan) -> str:
    try:
        r = float(str(rating).replace(",", ".") or 0)
    except Exception:
        r = 0.0
    try:
        u = int(re.sub(r"\D", "", str(ulasan) or "0") or 0)
    except Exception:
        u = 0
    if r >= 4.0 and u >= 20:
        return "Tinggi"
    if u < 5 or r < 3.0:
        return "Rendah"
    return "Sedang"


# ─── Google Maps Source ───────────────────────────────────────────────────────

async def _gmaps_no_website(keyword, area, max_results, headless, cb):
    """Cari bisnis di Google Maps yang belum punya website."""
    from scrapers.gmaps import _scrape_query, _wa_link

    results = []
    ua = UserAgent(os=["windows", "macos", "linux"])
    user_agent = ua.chrome

    cb(5, f"Google Maps: mencari '{keyword}' di {area}...", 0)

    # _scrape_query membuat context/page-nya sendiri per listing (agar IP bisa
    # dirotasi), jadi di sini cukup sediakan browser-nya.
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-dev-shm-usage", "--lang=id-ID,id"]
        )

        try:
            query = f"{keyword} {area}".strip()
            # Halaman ini punya file output sendiri dan tidak memakai aturan
            # anti-duplikat database, jadi dedup dimatikan.
            params = {
                "_filters": {},
                "dedup_enabled": False,
                "refresh_kontak": False,
                "radius_km": 0,
            }
            hasil = await _scrape_query(browser, user_agent, query, area,
                                        max_results, params, cb, 5, 50)
            raw = hasil["baru"]

            tanggal = datetime.now().strftime("%Y-%m-%d %H:%M")
            for r in raw:
                ws = r.get("website") or ""
                if ws and ws not in ("-", ""):
                    continue  # sudah punya website

                telepon = r.get("telepon") or "-"
                ulasan = r.get("jumlah_ulasan") or 0
                rating = r.get("rating") or 0

                hasil = {
                    "sumber": "Google Maps",
                    "nama": r.get("nama_bisnis") or "-",
                    "telepon": telepon,
                    "whatsapp_link": r.get("whatsapp_link") or _wa_link_local(telepon),
                    "email": "-",
                    "alamat": r.get("alamat") or "-",
                    "kategori_bisnis": r.get("kategori") or "-",
                    "mengapa_butuh_website": (
                        f"Aktif di Google Maps ({ulasan} ulasan, rating {rating}) — belum punya website"
                    ),
                    "detail_post": f"Jam operasional: {r.get('jam_operasional') or '-'}",
                    "potensi": _potensi_gmaps(rating, ulasan),
                    "status_leads": "Belum Dihubungi",
                    "tanggal_scraping": tanggal,
                    "source_url": r.get("source_url") or "-",
                }
                results.append(hasil)
                cb(50, f"✓ GMaps (tanpa website): {hasil['nama']} | {hasil['potensi']}", len(results))
        finally:
            await browser.close()

    return results


# ─── Gemini AI Search Source ──────────────────────────────────────────────────

async def _gemini_search(intent_keyword, max_results, api_key, cb):
    """Gunakan Gemini API + Google Search Grounding untuk cari intent posts."""
    results = []
    cb(60, "Gemini AI: menghubungi Google Search Grounding...", 0)

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)

        prompt = (
            f'Cari postingan publik di internet dari orang atau bisnis Indonesia yang '
            f'sedang mencari jasa pembuatan website, ingin buat toko online, atau butuh website bisnis.\n'
            f'Gunakan keyword pencarian: "{intent_keyword}"\n\n'
            f'Fokus pada forum Indonesia (Kaskus, Loker.id), Facebook publik, Twitter/X, blog, atau komentar di marketplace.\n\n'
            f'Kembalikan JSON array dengan maksimal {max_results} hasil. Format setiap item:\n'
            '[\n'
            '  {\n'
            '    "nama": "nama penulis atau bisnis",\n'
            '    "platform": "sumber (Kaskus/Facebook/Twitter/Blog/dll)",\n'
            '    "detail": "ringkasan kebutuhan max 200 karakter",\n'
            '    "telepon": "nomor HP jika ada atau -",\n'
            '    "email": "email jika ada atau -",\n'
            '    "source_url": "URL langsung postingan"\n'
            '  }\n'
            ']\n\n'
            'Kembalikan HANYA JSON array valid, tanpa teks penjelasan lain.'
        )

        cb(65, "Gemini AI: mengirim query...", 0)

        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            ),
        )

        raw_text = (response.text or "").strip()

        # Strip markdown code block jika Gemini membungkus JSON dengan ```json ... ```
        raw_text = re.sub(r'^```(?:json)?\s*', '', raw_text, flags=re.MULTILINE)
        raw_text = re.sub(r'```\s*$', '', raw_text, flags=re.MULTILINE).strip()

        cb(80, f"Gemini AI: respons diterima ({len(raw_text)} karakter)", 0)

        if not raw_text:
            cb(82, "❌ Gemini: respons kosong — periksa API key atau coba lagi", 0)
            return results

        # Ekstrak JSON array dari respons
        json_match = re.search(r'\[[\s\S]*\]', raw_text)
        if not json_match:
            cb(82, "⚠ Gemini: tidak ada JSON array dalam respons — coba ubah intent keyword", 0)
            return results

        json_str = json_match.group()
        # Bersihkan trailing commas yang tidak valid
        json_str = re.sub(r',\s*([}\]])', r'\1', json_str)

        try:
            items = json.loads(json_str)
        except json.JSONDecodeError as je:
            cb(82, f"⚠ Gemini: gagal parse JSON — {je}", 0)
            return results

        if not isinstance(items, list):
            cb(82, "⚠ Gemini: JSON bukan array", 0)
            return results

        tanggal = datetime.now().strftime("%Y-%m-%d %H:%M")
        for item in items[:max_results]:
            if not isinstance(item, dict):
                continue
            telepon = str(item.get("telepon") or "-").strip()
            email = str(item.get("email") or "-").strip()

            potensi = "Sedang"
            if telepon not in ("-", "", "null") or email not in ("-", "", "null"):
                potensi = "Tinggi"

            hasil = {
                "sumber": f"Gemini ({item.get('platform') or 'Web'})",
                "nama": str(item.get("nama") or "-").strip(),
                "telepon": telepon,
                "whatsapp_link": _wa_link_local(telepon),
                "email": email,
                "alamat": "-",
                "kategori_bisnis": "Cari Jasa Website",
                "mengapa_butuh_website": str(item.get("detail") or "-")[:200],
                "detail_post": str(item.get("detail") or "-"),
                "potensi": potensi,
                "status_leads": "Belum Dihubungi",
                "tanggal_scraping": tanggal,
                "source_url": str(item.get("source_url") or "-"),
            }
            results.append(hasil)
            cb(85, f"✓ Gemini: {hasil['nama']} | {hasil['sumber']} | {potensi}", len(results))

    except ImportError:
        cb(60, "❌ google-genai belum terinstall. Jalankan: pip install google-genai", 0)
    except Exception as e:
        cb(85, f"❌ Gemini error: {type(e).__name__}: {e}", len(results))

    return results


# ─── Main ─────────────────────────────────────────────────────────────────────

async def _async_main(params, cb):
    sources = params.get("sources", ["gmaps"])
    keyword = params.get("keyword", "")
    area = params.get("area", "")
    intent_keyword = params.get("intent_keyword", "cari jasa website")
    gemini_api_key = params.get("gemini_api_key", "")
    max_results = int(params.get("max_results", 20))
    headless = bool(params.get("headless", True))

    all_results = []

    if "gmaps" in sources and keyword:
        cb(5, f"Memulai Google Maps — keyword: '{keyword}', area: '{area}'", 0)
        gmaps_results = await _gmaps_no_website(keyword, area, max_results, headless, cb)
        all_results.extend(gmaps_results)
        cb(55, f"Google Maps selesai: {len(gmaps_results)} bisnis tanpa website ditemukan", len(all_results))
    elif "gmaps" in sources and not keyword:
        cb(5, "⚠ Google Maps dilewati — keyword bisnis tidak diisi", 0)

    if "gemini" in sources:
        if gemini_api_key:
            cb(60, f"Memulai Gemini AI Search — intent: '{intent_keyword}'", len(all_results))
            gemini_results = await _gemini_search(intent_keyword, max_results, gemini_api_key, cb)
            all_results.extend(gemini_results)
            cb(90, f"Gemini selesai: {len(gemini_results)} leads dari postingan web", len(all_results))
        else:
            cb(60, "⚠ Gemini dilewati — API key tidak diisi", len(all_results))

    if not all_results:
        cb(95, "⚠ Tidak ada leads ditemukan. Coba ubah keyword atau area.", 0)

    cb(92, f"Total {len(all_results)} leads. Membuat file Excel...", len(all_results))

    OUTPUT_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"webslead_{timestamp}.xlsx"
    out_path = OUTPUT_DIR / filename

    df = pd.DataFrame(all_results) if all_results else pd.DataFrame(columns=COLUMN_ORDER)
    cols = [c for c in COLUMN_ORDER if c in df.columns]
    df = df[cols] if cols else df

    writer = pd.ExcelWriter(str(out_path), engine="xlsxwriter")
    df.to_excel(writer, index=False, sheet_name="Website Leads")
    wb, ws = writer.book, writer.sheets["Website Leads"]

    hdr = wb.add_format({"bold": True, "bg_color": "#1A5276", "font_color": "white", "align": "center"})
    high_fmt = wb.add_format({"bg_color": "#D5F5E3", "bold": True})
    low_fmt = wb.add_format({"bg_color": "#FADBD8"})

    for ci, col in enumerate(cols):
        ws.write(0, ci, col, hdr)
        try:
            w = min(max(df[col].astype(str).map(len).max(), len(col)) + 2, 50)
        except Exception:
            w = 18
        ws.set_column(ci, ci, w)
    ws.freeze_panes(1, 0)

    if "potensi" in cols:
        pot_col = cols.index("potensi")
        for ri in range(len(df)):
            val = str(df.iloc[ri].get("potensi", ""))
            if val == "Tinggi":
                ws.set_row(ri + 1, None, high_fmt)
            elif val == "Rendah":
                ws.set_row(ri + 1, None, low_fmt)

    writer.close()
    cb(98, f"File tersimpan: {filename}", len(all_results))
    return filename


def run_website_leads(params: dict, callback) -> str:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    return asyncio.run(_async_main(params, callback))
