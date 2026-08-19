"""
scrapers/mp_endpoints.py — Alamat API internal marketplace, disimpan sebagai DATA
=================================================================================
Endpoint di bawah ini TIDAK berdokumentasi resmi dan bisa berubah kapan saja tanpa
pemberitahuan. Karena itu ia sengaja tidak ditanam di dalam logika scraper: default
di file ini bisa ditimpa oleh `data/mp_endpoints.json`.

Saat Shopee/Tokopedia mengubah API-nya, perbaikannya tidak perlu ganti kode:
  1. Buka toko target di Chrome scraping (yang sudah login)
  2. DevTools (F12) → tab Network → filter XHR → reload halaman
  3. Klik kanan request daftar produknya → Copy → Copy link address
  4. Tempel URL-nya di menu "Data Harga" pada aplikasi

Placeholder yang dikenali: {username} {shopid} {itemid} {keyword} {limit} {offset} {page}
"""
import json
from pathlib import Path

OVERRIDE_FILE = Path("data") / "mp_endpoints.json"

# Berapa kali lipat harga disimpan pada respons API dibanding rupiah sebenarnya.
# Shopee memakai "micro-rupiah" (Rp 150.000 dikirim sebagai 15000000000).
# Kalau suatu saat berubah, gejalanya adalah harga median yang absurd — dan
# mp_api._periksa_skala() akan meneriakkannya, bukan diam-diam salah.
DEFAULT = {
    "shopee": {
        "origin": "https://shopee.co.id",
        "harga_skala": 100000,
        "shop_base": "/api/v4/shop/get_shop_base?username={username}",
        "shop_items": (
            "/api/v4/shop/search_items"
            "?filter_sold_out=0&limit={limit}&offset={offset}"
            "&order=desc&shopid={shopid}&sort_by=pop&use_case=1"
        ),
        "shop_search": (
            "/api/v4/search/search_items"
            "?by=relevancy&keyword={keyword}&limit={limit}&match_id={shopid}"
            "&newest={offset}&order=desc&page_type=shop"
            "&scenario=PAGE_OTHERS&version=2"
        ),
        "search": (
            "/api/v4/search/search_items"
            "?by=relevancy&keyword={keyword}&limit={limit}&newest={offset}"
            "&order=desc&page_type=search&scenario=PAGE_GLOBAL_SEARCH&version=2"
        ),
        "item_detail": "/api/v4/pdp/get_pc?item_id={itemid}&shop_id={shopid}",
        "akun": "/api/v4/account/basic/get_account_info",
        # Nama cookie yang hanya ada setelah login. context.cookies() ikut membaca
        # cookie HttpOnly — document.cookie tidak, jadi jangan pakai yang terakhir.
        "cookie_login": ["SPC_ST", "SPC_U", "SPC_EC"],
    },
    "tokopedia": {
        "origin": "https://www.tokopedia.com",
        "harga_skala": 1,
        "gql": "https://gql.tokopedia.com/graphql/{op}",
        "shop_page": "/{username}",
        # Semua bentuk di bawah dibuktikan langsung terhadap tokopedia.com
        # (probe 14 Agustus, toko jayapc) — bukan hasil menebak:
        #   /product?perpage=80        → 80 kartu, next = /product/page/2?perpage=80
        #   /product/page/2?perpage=80 → 80 kartu, next = .../page/3
        #   /etalase/processor         → 52 kartu (etalase kecil muat satu halaman)
        #   /product?q=processor       → 80 kartu, paginasi ikut membawa ?q=
        # `perpage=80` adalah maksimum yang ditawarkan UI (pilihannya 20/40/80).
        "shop_product_page": "/{username}/product?perpage=80",
        "shop_product_halaman": "/{username}/product/page/{halaman}?perpage=80",
        "shop_etalase": "/{username}/etalase/{slug}?perpage=80",
        "shop_etalase_halaman": "/{username}/etalase/{slug}/page/{halaman}?perpage=80",
        # Pencarian DI DALAM toko. Ini jaring pengaman recall yang paling penting:
        # tiap toko menulis judul dengan gaya berbeda, tapi kode model (mis. 12700K)
        # nyaris selalu ada di judul mana pun — jadi cari pakai kode model, bukan
        # kalimat bebas.
        "shop_search": "/{username}/product?q={keyword}&perpage=80",
        "akun": "https://gql.tokopedia.com/graphql/getUserProfile",
        "cookie_login": ["_SID_Tokopedia_", "DID"],
    },
}

# Query GraphQL Tokopedia. Dipisah dari DEFAULT supaya gampang ditimpa sendiri.
# CATATAN JUJUR: bentuk operasi GraphQL Tokopedia paling sering berubah di antara
# semua yang ada di file ini. Kalau gagal, tier DOM Tokopedia masih bagus karena
# punya data-testid yang stabil — kerugiannya kecil.
GQL = {
    "ShopProducts": """
query ShopProducts($sid: String!, $page: Int, $perPage: Int, $keyword: String, $sort: Int) {
  GetShopProduct(shopID: $sid, filter: {page: $page, perPage: $perPage,
                                        fkeyword: $keyword, sort: $sort}) {
    status
    errors
    links { prev next }
    data {
      name product_url product_id price { text_idr }
      primary_image { original thumbnail }
      stats { countView countReview countTalk }
      badge { title image_url }
      rating sold
    }
  }
}""",
    # Bentuk argumen dicontek dari cache Apollo halaman toko yang tersimpan di
    # output/diag_jayapc_20260814_104429/page.html — di sana operasinya tercatat
    # sebagai shopInfoByID({"input":{"domain":...,"fields":[...],"shopIDs":[0],
    # "source":"shoppage"}}). Bentuk lama `shopInfoByDomain(domain:)` yang dulu
    # ditulis di sini tidak cocok dengan skema Tokopedia mana pun yang dipakai
    # halaman itu, jadi lapis 0 selalu gagal diam-diam.
    "ShopInfoCore": """
query ShopInfoCore($input: ShopInfoByIDRequest!) {
  shopInfoByID(input: $input) {
    result { shopCore { shopID name domain description } }
  }
}""",
}


def _deep_merge(dasar: dict, atas: dict) -> dict:
    """Gabung dict bersarang; nilai di `atas` menang."""
    hasil = dict(dasar)
    for k, v in (atas or {}).items():
        if isinstance(v, dict) and isinstance(hasil.get(k), dict):
            hasil[k] = _deep_merge(hasil[k], v)
        else:
            hasil[k] = v
    return hasil


def muat(paksa_baca=False) -> dict:
    """Endpoint efektif = DEFAULT ditimpa data/mp_endpoints.json (bila ada).

    Sengaja tidak di-cache: user bisa mengedit override lewat UI di tengah sesi
    dan wajar berharap run berikutnya langsung memakainya.
    """
    try:
        if OVERRIDE_FILE.exists():
            atas = json.loads(OVERRIDE_FILE.read_text(encoding="utf-8"))
            return _deep_merge(DEFAULT, atas)
    except Exception:
        # Override rusak tidak boleh mematikan scraper — pakai default saja.
        pass
    return dict(DEFAULT)


def url(platform: str, nama: str, **kw) -> str:
    """Bangun URL absolut dari template. Placeholder yang tidak diisi dibiarkan apa adanya."""
    ep = muat().get(platform) or {}
    tpl = ep.get(nama)
    if not tpl:
        raise KeyError(f"Endpoint '{nama}' tidak dikenal untuk platform '{platform}'")
    jalur = tpl.format(**kw) if kw else tpl
    if jalur.startswith("http"):
        return jalur
    return ep.get("origin", "").rstrip("/") + jalur


def origin(platform: str) -> str:
    return (muat().get(platform) or {}).get("origin", "")


def skala_harga(platform: str) -> int:
    return int((muat().get(platform) or {}).get("harga_skala", 1)) or 1


def cookie_login(platform: str) -> list:
    return list((muat().get(platform) or {}).get("cookie_login", []))


def simpan_override(data: dict):
    """Tulis data/mp_endpoints.json dari UI. Hanya menyimpan selisih dari default."""
    OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                             encoding="utf-8")
