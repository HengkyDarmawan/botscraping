"""
scrapers/social.py — Social media leads scraper
Platform: Instagram Business (via instaloader) + Facebook Pages + LinkedIn (via Google Search)

PENTING: Hanya mengambil data yang SENGAJA dipublikasikan di profil publik.
"""
import asyncio
import json
import re
import sys
import random
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import pandas as pd
import instaloader
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import Stealth

OUTPUT_DIR = Path("output")

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
_STEALTH = Stealth()

COLUMN_ORDER_SOCIAL = [
    "platform", "username", "nama", "jabatan_bio", "perusahaan",
    "email", "telepon", "website", "alamat", "followers",
    "kategori_bisnis", "status_leads", "tanggal_scraping", "source_url",
]


async def _random_delay(mn=3.0, mx=6.0):
    await asyncio.sleep(random.uniform(mn, mx))


# ─── Instagram via instaloader ────────────────────────────────────────────────

def _scrape_instagram(keyword: str, max_results: int, cb) -> list:
    results = []
    L = instaloader.Instaloader(
        download_pictures=False, download_videos=False,
        download_video_thumbnails=False, save_metadata=False,
        quiet=True,
    )

    if keyword.startswith("@"):
        usernames = [keyword.lstrip("@")]
        cb(10, f"Instagram: memuat profil @{usernames[0]}...", 0)
    else:
        cb(10, f"Instagram: mencari #{keyword} ...", 0)
        usernames = []
        try:
            hashtag = instaloader.Hashtag.from_name(L.context, keyword.lstrip("#"))
            seen = set()
            for post in hashtag.get_posts():
                if post.owner_username not in seen:
                    seen.add(post.owner_username)
                    usernames.append(post.owner_username)
                if len(usernames) >= max_results * 3:
                    break
                time.sleep(random.uniform(1.0, 2.0))
        except Exception as e:
            cb(20, f"Instagram hashtag error: {e}", 0)

    cb(20, f"Instagram: {len(usernames)} username ditemukan. Mengambil profil...", 0)

    for i, username in enumerate(usernames):
        if len(results) >= max_results:
            break
        try:
            pct = 20 + int((i / max(len(usernames), 1)) * 60)
            cb(pct, f"Instagram [{i+1}]: memuat @{username}...", len(results))

            profile = instaloader.Profile.from_username(L.context, username)

            email = getattr(profile, "business_email", None) or ""
            phone = getattr(profile, "business_phone_number", None) or ""
            category = getattr(profile, "business_category_name", None) or ""
            city = getattr(profile, "city_name", None) or ""
            bio = profile.biography or ""

            if not phone:
                phone_match = re.search(r"(\+?62|0)[\d\s\-]{8,14}", bio)
                if phone_match:
                    phone = phone_match.group(0)
            if not email:
                email_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", bio)
                if email_match:
                    email = email_match.group(0)

            results.append({
                "platform": "Instagram",
                "username": f"@{username}",
                "nama": profile.full_name or username,
                "jabatan_bio": bio[:150] if bio else "-",
                "perusahaan": "-",
                "email": email or "-",
                "telepon": phone or "-",
                "website": profile.external_url or "-",
                "alamat": city or "-",
                "followers": profile.followers,
                "kategori_bisnis": category or "-",
                "status_leads": "Belum Dihubungi",
                "tanggal_scraping": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "source_url": f"https://www.instagram.com/{username}/",
            })
            cb(pct, f"✓ @{username} | Email: {email or '-'} | Tel: {phone or '-'}", len(results))
            time.sleep(random.uniform(2.0, 4.0))

        except instaloader.exceptions.ProfileNotExistsException:
            cb(pct, f"⚠ @{username} tidak ditemukan", len(results))
        except Exception as e:
            cb(pct, f"⚠ @{username} error: {str(e)[:60]}", len(results))

    return results


# ─── Facebook Pages via Playwright ────────────────────────────────────────────

async def _scrape_facebook(page, keyword: str, max_results: int, cb) -> list:
    results = []
    search_url = f"https://www.facebook.com/search/pages/?q={keyword.replace(' ', '+')}"

    cb(10, f"Facebook: membuka pencarian halaman bisnis '{keyword}'...", 0)
    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
        await _random_delay(3.0, 5.0)
    except Exception as e:
        cb(20, f"Facebook: gagal membuka halaman ({e})", 0)
        return results

    for sel in ['[aria-label="Allow all cookies"]', '[aria-label="Terima semua cookie"]',
                'button[data-testid="cookie-policy-manage-dialog-accept-button"]',
                'button[title="Allow all cookies"]']:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                await _random_delay(1.0, 2.0)
                break
        except Exception:
            continue

    for sel in ['[aria-label="Close"]', '[role="dialog"] [aria-label="Tutup"]']:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                await _random_delay(1.0, 2.0)
                break
        except Exception:
            continue

    for _ in range(3):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await _random_delay(2.0, 3.0)

    page_links = await page.query_selector_all('a[href*="facebook.com/"][role="link"]')
    hrefs = []
    for link in page_links:
        href = await link.get_attribute("href")
        if href and "/search/" not in href and "/groups/" not in href:
            if re.search(r"facebook\.com/(?!story|photo|video|reel)[a-zA-Z0-9._\-]+", href):
                hrefs.append(href.split("?")[0])

    hrefs = list(dict.fromkeys(hrefs))
    cb(25, f"Facebook: {len(hrefs)} halaman ditemukan. Mengambil detail...", 0)

    for i, href in enumerate(hrefs[:max_results]):
        pct = 25 + int((i / max(len(hrefs[:max_results]), 1)) * 65)
        cb(pct, f"Facebook [{i+1}]: membuka {href[:50]}...", len(results))
        try:
            await page.goto(href, wait_until="domcontentloaded", timeout=30_000)
            await _random_delay(2.0, 4.0)

            nama = await _fb_text(page, 'h1')
            bio = await _fb_text(page, '[data-key="intro_card_bio"]')

            about_url = href.rstrip("/") + "/about"
            await page.goto(about_url, wait_until="domcontentloaded", timeout=20_000)
            await _random_delay(2.0, 3.0)

            page_text = await page.inner_text("body")

            telepon = ""
            phone_match = re.search(r"(\+?62|0)[\d\s\-\(\)]{8,16}", page_text)
            if phone_match:
                telepon = re.sub(r"[^\d+]", "", phone_match.group(0))

            email = ""
            email_match = re.search(r"[\w\.\+\-]+@[\w\.\-]+\.\w{2,}", page_text)
            if email_match:
                email = email_match.group(0)

            website = ""
            web_el = await page.query_selector('a[href*="http"]:not([href*="facebook"])')
            if web_el:
                website = await web_el.get_attribute("href") or ""

            alamat = ""
            for addr_sel in ['[data-key="address"]', '[class*="address"]']:
                try:
                    el = await page.query_selector(addr_sel)
                    if el:
                        alamat = (await el.inner_text()).strip()
                        break
                except Exception:
                    continue

            results.append({
                "platform": "Facebook",
                "username": href.split("facebook.com/")[-1].split("/")[0],
                "nama": nama or "-",
                "jabatan_bio": (bio or "")[:150],
                "perusahaan": "-",
                "email": email or "-",
                "telepon": telepon or "-",
                "website": website or "-",
                "alamat": alamat or "-",
                "followers": "-",
                "kategori_bisnis": "-",
                "status_leads": "Belum Dihubungi",
                "tanggal_scraping": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "source_url": href,
            })
            cb(pct, f"✓ {nama or href[:40]} | Email: {email or '-'} | Tel: {telepon or '-'}", len(results))

        except PlaywrightTimeoutError:
            cb(pct, f"⚠ Timeout: {href[:50]}", len(results))
        except Exception as e:
            cb(pct, f"⚠ Error: {str(e)[:60]}", len(results))

        await _random_delay(3.0, 5.0)

    return results


async def _fb_text(page, selector) -> str:
    try:
        el = await page.query_selector(selector)
        if el:
            return (await el.inner_text()).strip()
    except Exception:
        pass
    return ""


# ─── LinkedIn via Google Search ───────────────────────────────────────────────

def _parse_linkedin_title(title: str) -> tuple[str, str, str]:
    """
    Parse Google result title untuk profil LinkedIn.
    Format umum: "Nama - Jabatan - Perusahaan | LinkedIn"
    Returns: (nama, jabatan, perusahaan)
    """
    title = re.sub(r"\s*\|\s*LinkedIn\s*$", "", title, flags=re.IGNORECASE).strip()
    parts = [p.strip() for p in title.split(" - ") if p.strip()]
    if len(parts) >= 3:
        return parts[0], parts[1], " - ".join(parts[2:])
    elif len(parts) == 2:
        return parts[0], parts[1], "-"
    elif len(parts) == 1:
        return parts[0], "-", "-"
    return title, "-", "-"


def _parse_linkedin_location(snippet: str) -> str:
    """Extract lokasi dari snippet Google: 'Jakarta, Indonesia · 500+ connections...'"""
    if not snippet:
        return "-"
    # Ambil bagian sebelum "·" pertama
    first = snippet.split("·")[0].strip()
    # Harus terlihat seperti lokasi (ada koma, atau nama kota/negara)
    if first and len(first) < 60 and re.search(r"[A-Za-z]", first):
        return first
    return "-"


async def _google_search_snippets(page, query: str, cb_msg: str, cb) -> list:
    """Buka Google Search dan kembalikan list {title, snippet, url}."""
    search_url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query) + "&hl=id&num=20"
    cb(0, cb_msg, 0)

    await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
    await asyncio.sleep(random.uniform(2.5, 4.0))

    # Terima cookies Google jika muncul
    for btn_sel in ['button#L2AGLb', 'button:has-text("Accept all")',
                    'button:has-text("Setuju semua")', 'button:has-text("I agree")']:
        try:
            btn = await page.query_selector(btn_sel)
            if btn and await btn.is_visible():
                await btn.click()
                await asyncio.sleep(1.5)
                break
        except Exception:
            continue

    # Deteksi CAPTCHA
    body = await page.evaluate("document.body.innerText")
    if any(kw in body.lower() for kw in ["unusual traffic", "captcha", "robot", "tidak biasa"]):
        return []

    # Ekstrak hasil pencarian via JavaScript (robust terhadap perubahan class Google)
    results = await page.evaluate("""
        () => {
            const items = [];
            // Cari semua link yang punya href ke linkedin.com/in/
            document.querySelectorAll('a').forEach(a => {
                const href = a.href || '';
                if (!href.includes('linkedin.com/in/')) return;
                // Naik ke container result
                let container = a.closest('[data-hveid]') || a.closest('.g') || a.parentElement;
                if (!container) return;
                const h3 = container.querySelector('h3');
                // Cari elemen snippet (bukan h3, bukan a)
                let snippet = '';
                container.querySelectorAll('span, div').forEach(el => {
                    const txt = el.innerText || '';
                    if (txt.length > 40 && !el.querySelector('h3') && txt !== (h3 ? h3.innerText : '')) {
                        if (!snippet) snippet = txt;
                    }
                });
                items.push({
                    url: href.split('?')[0],
                    title: h3 ? h3.innerText : '',
                    snippet: snippet.slice(0, 300)
                });
            });
            // Deduplikasi by URL
            const seen = new Set();
            return items.filter(i => {
                if (seen.has(i.url)) return false;
                seen.add(i.url);
                return true;
            });
        }
    """)
    return results or []


async def _scrape_linkedin_google(keyword: str, max_results: int, cb) -> list:
    """Cari profil LinkedIn via Google Search — tidak butuh login LinkedIn."""
    results = []
    query = f'site:linkedin.com/in/ "{keyword}"'
    cb(10, f"LinkedIn: mencari di Google → {query}", 0)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            locale="id-ID",
            timezone_id="Asia/Jakarta",
            viewport={"width": 1366, "height": 768},
            user_agent=DESKTOP_UA,
        )
        page = await context.new_page()
        await _STEALTH.apply_stealth_async(page)

        try:
            items = await _google_search_snippets(
                page, query,
                f"LinkedIn: membuka Google Search '{keyword}'...", cb
            )

            if not items:
                cb(90, "⚠ LinkedIn: Google memblokir atau tidak ada hasil. "
                       "Coba keyword lebih spesifik (mis. 'IT Manager Jakarta PT ABC').", 0)
                return results

            cb(25, f"LinkedIn: {len(items)} profil ditemukan di Google. Mengolah data...", 0)

            for i, item in enumerate(items[:max_results]):
                pct = 25 + int((i / max(len(items[:max_results]), 1)) * 55)
                nama, jabatan, perusahaan = _parse_linkedin_title(item["title"])
                lokasi = _parse_linkedin_location(item["snippet"])
                username = item["url"].rstrip("/").split("/")[-1]

                # Email enrichment: cari di Google hanya jika batch kecil
                email = "-"
                if max_results <= 10 and nama != "-" and perusahaan != "-":
                    try:
                        await asyncio.sleep(random.uniform(2.0, 3.5))
                        email_query = f'"{nama}" "{perusahaan}" email contact'
                        email_items = await _google_search_snippets(
                            page, email_query,
                            f"LinkedIn [{i+1}]: mencari email {nama}...", cb
                        )
                        for ei in email_items[:3]:
                            m = re.search(r"[\w\.\+\-]+@[\w\.\-]+\.\w{2,}", ei.get("snippet", ""))
                            if m and "linkedin" not in m.group(0).lower():
                                email = m.group(0)
                                break
                    except Exception:
                        pass

                results.append({
                    "platform": "LinkedIn",
                    "username": username,
                    "nama": nama,
                    "jabatan_bio": jabatan,
                    "perusahaan": perusahaan,
                    "email": email,
                    "telepon": "-",
                    "website": item["url"],
                    "alamat": lokasi,
                    "followers": "-",
                    "kategori_bisnis": "-",
                    "status_leads": "Belum Dihubungi",
                    "tanggal_scraping": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "source_url": item["url"],
                })
                cb(pct, f"✓ {nama} | {jabatan} | {perusahaan} | {lokasi}", len(results))
                await asyncio.sleep(random.uniform(0.5, 1.0))

        except Exception as e:
            cb(90, f"LinkedIn error: {e}", len(results))
        finally:
            await browser.close()

    return results


# ─── Gemini AI tambahan ───────────────────────────────────────────────────────

async def _gemini_social(platform, keyword, max_results, api_key, cb):
    """Cari profil sosial media dari Gemini AI + Google Search Grounding."""
    results = []
    tanggal = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Tentukan platform yang dicari
    if platform in ("instagram", "both"):
        plat_list = ["Instagram"]
    elif platform == "facebook":
        plat_list = ["Facebook"]
    elif platform == "linkedin":
        plat_list = ["LinkedIn"]
    else:
        plat_list = ["Instagram", "Facebook"]
    if platform == "both":
        plat_list = ["Instagram", "Facebook"]

    for plat in plat_list:
        cb(None, f"Gemini AI: mencari {plat} '{keyword}'...", len(results))
        try:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=api_key)
            if plat == "LinkedIn":
                prompt = (
                    f'Cari profil LinkedIn profesional terkait "{keyword}" dari Indonesia.\n'
                    f'Kembalikan JSON array {max_results} item:\n'
                    '[{"username": "URL profil LinkedIn", "nama": "nama lengkap", '
                    '"jabatan_bio": "jabatan/posisi", "perusahaan": "nama perusahaan", '
                    '"email": "email atau -", "telepon": "-", "website": "-", '
                    '"alamat": "kota atau -", "followers": "0", '
                    '"kategori_bisnis": "bidang industri"}]\n'
                    'Kembalikan HANYA JSON array valid.'
                )
            else:
                prompt = (
                    f'Cari akun {plat} bisnis terkait "{keyword}" dari Indonesia.\n'
                    f'Kembalikan JSON array {max_results} item:\n'
                    '[{"username": "username akun", "nama": "nama bisnis/pemilik", '
                    '"jabatan_bio": "bio singkat", "perusahaan": "-", '
                    '"email": "email bisnis atau -", "telepon": "nomor telepon atau -", '
                    '"website": "website atau -", "alamat": "kota atau -", '
                    '"followers": "jumlah followers atau 0", '
                    '"kategori_bisnis": "kategori bisnis"}]\n'
                    'Kembalikan HANYA JSON array valid.'
                )
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())]
                ),
            )
            raw_text = (response.text or "").strip()
            raw_text = re.sub(r'^```(?:json)?\s*', '', raw_text, flags=re.MULTILINE)
            raw_text = re.sub(r'```\s*$', '', raw_text, flags=re.MULTILINE).strip()
            if not raw_text:
                cb(None, f"⚠ Gemini {plat}: respons kosong", len(results))
                continue
            json_match = re.search(r'\[[\s\S]*\]', raw_text)
            if not json_match:
                cb(None, f"⚠ Gemini {plat}: tidak ada JSON dalam respons", len(results))
                continue
            json_str = re.sub(r',\s*([}\]])', r'\1', json_match.group())
            items = json.loads(json_str)
            for item in items[:max_results]:
                if not isinstance(item, dict):
                    continue
                username = str(item.get("username") or "-").strip()
                src = username if username.startswith("http") else f"https://{plat.lower()}.com/{username.lstrip('@')}"
                hasil = {
                    "platform": f"{plat} (via Gemini)",
                    "username": username,
                    "nama": str(item.get("nama") or "-").strip(),
                    "jabatan_bio": str(item.get("jabatan_bio") or "-").strip(),
                    "perusahaan": str(item.get("perusahaan") or "-").strip(),
                    "email": str(item.get("email") or "-").strip(),
                    "telepon": str(item.get("telepon") or "-").strip(),
                    "website": str(item.get("website") or "-").strip(),
                    "alamat": str(item.get("alamat") or "-").strip(),
                    "followers": str(item.get("followers") or "0").strip(),
                    "kategori_bisnis": str(item.get("kategori_bisnis") or "-").strip(),
                    "status_leads": "Belum Dihubungi",
                    "tanggal_scraping": tanggal,
                    "source_url": src,
                }
                results.append(hasil)
                cb(None, f"✓ Gemini {plat}: {hasil['nama']} | {hasil['email']}", len(results))
        except ImportError:
            cb(None, "❌ google-genai belum terinstall. Jalankan: pip install google-genai", 0)
        except Exception as e:
            cb(None, f"❌ Gemini {plat} error: {type(e).__name__}: {e}", len(results))
    return results


# ─── Main async ───────────────────────────────────────────────────────────────

async def _async_main(params, cb):
    platform = params.get("platform", "instagram")
    keyword = params.get("keyword", "")
    max_results = int(params.get("max_results", 20))
    use_gemini = bool(params.get("use_gemini", False))
    gemini_api_key = str(params.get("gemini_api_key", "") or "").strip()

    all_results = []

    if platform in ("instagram", "both"):
        cb(5, f"Memulai Instagram scraping: '{keyword}'", 0)
        ig_results = _scrape_instagram(keyword, max_results, cb)
        all_results.extend(ig_results)
        cb(80, f"Instagram selesai: {len(ig_results)} profil", len(all_results))

    if platform in ("facebook", "both"):
        from camoufox.async_api import AsyncCamoufox
        cb(5 if platform == "facebook" else 80,
           f"Memulai Facebook Pages scraping: '{keyword}'", len(all_results))
        fb_results = []
        async with AsyncCamoufox(headless=True, humanize=True) as browser:
            page = await browser.new_page()
            fb_results = await _scrape_facebook(page, keyword, max_results, cb)
            all_results.extend(fb_results)
        cb(95, f"Facebook selesai: {len(fb_results)} halaman", len(all_results))

    if platform == "linkedin":
        cb(5, f"Memulai LinkedIn scraping via Google: '{keyword}'", 0)
        li_results = await _scrape_linkedin_google(keyword, max_results, cb)
        all_results.extend(li_results)
        cb(95, f"LinkedIn selesai: {len(li_results)} profil", len(all_results))

    # Gemini AI — sumber tambahan
    if use_gemini and gemini_api_key:
        cb(96, f"Gemini AI: mencari profil '{keyword}' via Google Search...", len(all_results))
        g_results = await _gemini_social(platform, keyword, max_results, gemini_api_key, cb)
        all_results.extend(g_results)
        cb(97, f"Gemini selesai: {len(g_results)} leads tambahan", len(all_results))
    elif use_gemini and not gemini_api_key:
        cb(96, "⚠ Gemini dilewati — API key tidak diisi", len(all_results))

    cb(96, f"Total {len(all_results)} leads. Menyimpan...", len(all_results))

    OUTPUT_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"social_{timestamp}.xlsx"
    out_path = OUTPUT_DIR / filename

    df = pd.DataFrame(all_results) if all_results else pd.DataFrame(columns=COLUMN_ORDER_SOCIAL)
    cols = [c for c in COLUMN_ORDER_SOCIAL if c in df.columns]
    df = df[cols] if cols else df

    writer = pd.ExcelWriter(str(out_path), engine="xlsxwriter")
    df.to_excel(writer, index=False, sheet_name="Social Leads")
    wb, ws = writer.book, writer.sheets["Social Leads"]
    hdr = wb.add_format({"bold": True, "bg_color": "#6C3483", "font_color": "white", "align": "center"})
    for ci, col in enumerate(cols):
        ws.write(0, ci, col, hdr)
        try:
            w = min(max(df[col].astype(str).map(len).max(), len(col)) + 2, 45)
        except Exception:
            w = 15
        ws.set_column(ci, ci, w)
    ws.freeze_panes(1, 0)
    writer.close()

    return filename


def run_social_scrape(params: dict, callback) -> str:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    return asyncio.run(_async_main(params, callback))
