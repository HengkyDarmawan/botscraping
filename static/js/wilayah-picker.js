/**
 * wilayah-picker.js — Pemilih target bergaya tag (chip): wilayah & jenis bisnis.
 *
 * Menggantikan input yang diketik bebas: salah ketik "Jakarta Slatan" dulu tetap
 * dikirim ke Google dan menghasilkan nol hasil tanpa penjelasan. Sekarang target
 * hanya bisa dipilih dari daftar resmi, dan untuk wilayah koordinat titik
 * pusatnya ikut terbawa otomatis sehingga pengaturan Radius (km) bekerja tanpa
 * perlu menyalin lat/long dari Google Maps.
 *
 * Mekanika chip + dropdown + keyboard tinggal di kelas dasar `ChipPicker`;
 * `WilayahPicker` dan `KeywordPicker` hanya menyediakan datanya dan isi panel
 * pintasannya. Ditulis tanpa library tambahan supaya tidak menambah beban CDN.
 */

/* ─── Pemuat data ─────────────────────────────────────────────────────────── */

const _cacheData = {};

/** Ambil satu endpoint JSON, ratakan dengan `ubah`, dan simpan hasilnya. */
function _muatSekali(kunci, url, ubah) {
  if (!_cacheData[kunci]) {
    _cacheData[kunci] = fetch(url).then(r => r.json()).then(ubah);
  }
  return _cacheData[kunci];
}

/** Daftar wilayah yang sudah diratakan: kota/kabupaten + kecamatan. */
function muatWilayah() {
  return _muatSekali('wilayah', '/api/wilayah', data => {
    const daftar = [];
    (data.provinsi || []).forEach(prov => {
      (prov.wilayah || []).forEach(w => {
        const tipeLabel = w.t === 'kota' ? 'Kota' : 'Kab.';
        daftar.push({
          // `query` dipakai apa adanya sebagai kata area di pencarian GMaps.
          query: w.n,
          label: `${w.n} — ${tipeLabel}, ${prov.nama}`,
          provinsi: prov.nama,
          tipe: w.t,
          lat: w.lat, lng: w.lng,
          cari: (w.n + ' ' + prov.nama).toLowerCase()
        });
        (w.kec || []).forEach(k => {
          daftar.push({
            query: `${k.n}, ${w.n}`,
            label: `${k.n} — Kec., ${w.n}`,
            provinsi: prov.nama,
            tipe: 'kec',
            induk: w.n,
            lat: k.lat, lng: k.lng,
            cari: (k.n + ' ' + w.n + ' ' + prov.nama).toLowerCase()
          });
        });
      });
    });
    return daftar;
  });
}

/** Daftar jenis bisnis yang sudah diratakan dari pustaka keyword. */
function muatKeywords() {
  return _muatSekali('keywords', '/api/keywords', data => {
    const daftar = [];
    (data.sektor || []).forEach(s => {
      (s.keywords || []).forEach(k => {
        daftar.push({
          query: k.k,
          label: `${k.k} — ${s.nama}`,
          sektor: s.nama,
          alasan: k.alasan || '',
          cari: (k.k + ' ' + s.nama + ' ' + (k.alasan || '')).toLowerCase()
        });
      });
    });
    return daftar;
  });
}

/* ─── Pintasan wilayah ────────────────────────────────────────────────────────
 *
 * Memilih 515 kota/kabupaten dan 188 kecamatan satu per satu tidak masuk akal
 * untuk target ribuan lead per hari. Pintasan di bawah dikelompokkan supaya
 * seluruh Indonesia bisa disusun dalam beberapa klik.
 *
 * Semuanya berupa fungsi penyaring atas daftar yang sudah diratakan, jadi
 * jumlahnya dihitung sendiri dari data dan tidak pernah basi.
 */

// wilayah.json tidak menyimpan nama pulau, jadi pemetaan ini hidup di sini.
const PULAU = {
  'Jawa': ['DKI Jakarta', 'Jawa Barat', 'Banten', 'Jawa Tengah',
           'DI Yogyakarta', 'Jawa Timur'],
  'Sumatera': ['Aceh', 'Sumatera Utara', 'Sumatera Barat', 'Riau',
               'Kepulauan Riau', 'Jambi', 'Sumatera Selatan', 'Bengkulu',
               'Lampung', 'Kepulauan Bangka Belitung'],
  'Kalimantan': ['Kalimantan Barat', 'Kalimantan Tengah', 'Kalimantan Selatan',
                 'Kalimantan Timur', 'Kalimantan Utara'],
  'Sulawesi': ['Sulawesi Utara', 'Sulawesi Tengah', 'Sulawesi Selatan',
               'Sulawesi Tenggara', 'Sulawesi Barat', 'Gorontalo'],
  'Bali & Nusa Tenggara': ['Bali', 'Nusa Tenggara Barat', 'Nusa Tenggara Timur'],
  'Maluku': ['Maluku', 'Maluku Utara'],
  'Papua': ['Papua', 'Papua Tengah', 'Papua Pegunungan', 'Papua Selatan',
            'Papua Barat', 'Papua Barat Daya']
};

// Kawasan metropolitan — kumpulan kota/kabupaten yang secara ekonomi menyatu,
// jadi satu kampanye biasanya menyasar seluruh kawasan sekaligus.
// `prov` dipakai saat nama wilayahnya ambigu: "Banjar" ada sebagai kota di Jawa
// Barat DAN kabupaten di Kalimantan Selatan.
const METRO = [
  { nama: 'Jabodetabek', wilayah: ['Jakarta Pusat', 'Jakarta Selatan', 'Jakarta Barat',
      'Jakarta Timur', 'Jakarta Utara', 'Bogor', 'Kabupaten Bogor', 'Depok',
      'Tangerang', 'Tangerang Selatan', 'Kabupaten Tangerang', 'Bekasi',
      'Kabupaten Bekasi'] },
  { nama: 'Bandung Raya', wilayah: ['Bandung', 'Kabupaten Bandung',
      'Kabupaten Bandung Barat', 'Cimahi', 'Sumedang'] },
  { nama: 'Gerbangkertosusila', wilayah: ['Surabaya', 'Gresik', 'Bangkalan',
      'Mojokerto', 'Kabupaten Mojokerto', 'Sidoarjo', 'Lamongan'] },
  { nama: 'Kedungsepur', wilayah: ['Semarang', 'Kabupaten Semarang', 'Salatiga',
      'Kendal', 'Demak', 'Grobogan'] },
  { nama: 'Solo Raya', wilayah: ['Surakarta (Solo)', 'Sukoharjo', 'Karanganyar',
      'Boyolali', 'Klaten', 'Sragen', 'Wonogiri'] },
  { nama: 'Kartamantul (Jogja)', wilayah: ['Yogyakarta', 'Sleman', 'Bantul'] },
  { nama: 'Malang Raya', wilayah: ['Malang', 'Kabupaten Malang', 'Batu'] },
  { nama: 'Sarbagita (Bali)', wilayah: ['Denpasar', 'Badung', 'Gianyar', 'Tabanan'] },
  { nama: 'Mebidangro (Medan)', wilayah: ['Medan', 'Binjai', 'Deli Serdang', 'Karo'] },
  { nama: 'Patungraya (Palembang)', wilayah: ['Palembang', 'Banyuasin', 'Ogan Ilir'] },
  { nama: 'Mamminasata (Makassar)', wilayah: ['Makassar', 'Maros', 'Gowa', 'Takalar'] },
  { nama: 'Banjar Bakula', prov: 'Kalimantan Selatan',
    wilayah: ['Banjarmasin', 'Banjarbaru', 'Banjar', 'Barito Kuala', 'Tanah Laut'] },
  { nama: 'Balikpapan-Samarinda', wilayah: ['Balikpapan', 'Samarinda', 'Kutai Kartanegara'] },
];

const KOTA_BESAR = [
  'Jakarta Pusat', 'Jakarta Selatan', 'Jakarta Barat', 'Jakarta Timur',
  'Jakarta Utara', 'Bandung', 'Surabaya', 'Medan', 'Semarang', 'Makassar',
  'Palembang', 'Bekasi', 'Depok', 'Tangerang', 'Tangerang Selatan', 'Bogor',
  'Batam', 'Pekanbaru', 'Bandar Lampung', 'Padang', 'Malang', 'Denpasar',
  'Samarinda', 'Balikpapan', 'Banjarmasin', 'Yogyakarta', 'Surakarta (Solo)',
  'Pontianak', 'Manado', 'Jambi', 'Cimahi'
];

const bukanKec = x => x.tipe !== 'kec';

/** Seluruh pintasan wilayah sebagai [{kelompok, nama, saring}]. */
function pintasanWilayah() {
  const out = [
    { kelompok: 'favorit', nama: 'Jabodetabek',
      saring: d => d.filter(x => bukanKec(x) && METRO[0].wilayah.includes(x.query)) },
    { kelompok: 'favorit', nama: 'Kota besar',
      saring: d => d.filter(x => bukanKec(x) && KOTA_BESAR.includes(x.query)) },
    { kelompok: 'favorit', nama: 'Semua kec. Jakarta',
      saring: d => d.filter(x => x.tipe === 'kec' && (x.induk || '').startsWith('Jakarta')) },
  ];
  Object.keys(PULAU).forEach(nama => out.push({
    kelompok: 'Pulau', nama,
    saring: d => d.filter(x => bukanKec(x) && PULAU[nama].includes(x.provinsi))
  }));
  METRO.forEach(m => out.push({
    kelompok: 'Metropolitan', nama: m.nama,
    saring: d => d.filter(x => bukanKec(x) && m.wilayah.includes(x.query) &&
                               (!m.prov || x.provinsi === m.prov))
  }));
  out.push(
    { kelompok: 'Tipe wilayah', nama: 'Semua kota',
      saring: d => d.filter(x => x.tipe === 'kota') },
    { kelompok: 'Tipe wilayah', nama: 'Semua kabupaten',
      saring: d => d.filter(x => x.tipe === 'kab') },
    // Kecamatan adalah kunci volume: satu pencarian GMaps mentok ±120 hasil,
    // jadi memecah kota jadi kecamatan melipatgandakan lead unik.
    { kelompok: 'Tipe wilayah', nama: 'Semua kecamatan',
      saring: d => d.filter(x => x.tipe === 'kec') },
  );
  return out;
}

/* ─── Kelas dasar ─────────────────────────────────────────────────────────── */

class ChipPicker {
  constructor(host) {
    this.host = host;
    this.dipilih = [];     // daftar objek terpilih
    this.sorotan = -1;     // indeks saran yang sedang disorot keyboard
    this.saran = [];
    this._bangunDOM();
    this._muat().then(() => {
      this.input.placeholder = this.placeholderSiap;
      this.input.disabled = false;
    });
  }

  /* — Yang wajib disediakan turunan — */
  _muat() { return Promise.resolve([]); }
  get placeholderSiap() { return 'Ketik untuk mencari...'; }
  get placeholderMuat() { return 'Memuat daftar...'; }
  get namaSatuan() { return 'item'; }
  get eventBerubah() { return 'picker-berubah'; }
  _isiPanel() {}                 // isi panel pintasan
  _chipKelas() { return ''; }    // kelas tambahan untuk chip
  _chipIkon() { return ''; }
  _chipJudul() { return ''; }
  _barisDropdown(x) { return ''; }  // elemen kanan tiap baris saran

  _bangunDOM() {
    this.host.classList.add('wilayah-picker');
    this.host.innerHTML = `
      <div class="wp-chips"></div>
      <div class="wp-input-wrap">
        <input type="text" class="form-control form-control-sm wp-input"
               placeholder="${_esc(this.placeholderMuat)}" disabled autocomplete="off">
        <div class="wp-dropdown"></div>
      </div>
      <div class="wp-shortcuts"></div>
      <div class="wp-panel"></div>
      <div class="wp-umpanbalik"></div>`;

    this.chips = this.host.querySelector('.wp-chips');
    this.input = this.host.querySelector('.wp-input');
    this.dropdown = this.host.querySelector('.wp-dropdown');
    this.panel = this.host.querySelector('.wp-panel');
    this.umpanbalik = this.host.querySelector('.wp-umpanbalik');
    this.bar = this.host.querySelector('.wp-shortcuts');

    // Turunan boleh mengisi panel secara asinkron (menunggu datanya datang),
    // jadi tombol toggle dipasang sekarang — supaya urutannya tetap di belakang
    // pintasan favorit — lalu baru ditampilkan setelah panelnya benar-benar ada.
    const panelSiap = Promise.resolve(this._isiPanel());

    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'btn btn-outline-primary btn-sm wp-shortcut';
    toggle.innerHTML = 'Pintasan lainnya <i class="bi bi-chevron-down"></i>';
    toggle.style.display = 'none';
    toggle.onclick = () => {
      const buka = this.panel.classList.toggle('show');
      toggle.innerHTML = 'Pintasan lainnya <i class="bi bi-chevron-' +
                         (buka ? 'up' : 'down') + '"></i>';
    };
    this.bar.appendChild(toggle);
    panelSiap.then(() => {
      if (this.panel.children.length) toggle.style.display = '';
    });

    const hapus = document.createElement('button');
    hapus.type = 'button';
    hapus.className = 'btn btn-outline-danger btn-sm wp-shortcut';
    hapus.textContent = 'Hapus semua';
    hapus.onclick = () => {
      this.dipilih = [];
      this._render();
      this._kabar(`Semua ${this.namaSatuan} dihapus.`);
    };
    this.bar.appendChild(hapus);

    this.input.addEventListener('input', () => this._cari());
    this.input.addEventListener('focus', () => this._cari());
    this.input.addEventListener('keydown', e => this._tombol(e));
    document.addEventListener('click', e => {
      if (!this.host.contains(e.target)) this._tutup();
    });
  }

  /** Satu kelompok tombol di dalam panel. */
  _grup(judul, pintasan, catatan) {
    const grup = document.createElement('div');
    grup.className = 'wp-grup';
    grup.innerHTML = `<div class="wp-grup-judul">${_esc(judul)}</div>`;
    const isi = document.createElement('div');
    isi.className = 'wp-grup-isi';
    pintasan.forEach(p => isi.appendChild(this._tombolPintasan(p)));
    grup.appendChild(isi);
    if (catatan) {
      const c = document.createElement('div');
      c.className = 'wp-catatan';
      c.textContent = catatan;
      grup.appendChild(c);
    }
    this.panel.appendChild(grup);
    return grup;
  }

  /**
   * Tombol pintasan dengan jumlahnya, dihitung dari data supaya tak pernah basi.
   * `p.tanpaJumlah` untuk pintasan yang isinya memang satu — "(1)" cuma bising.
   */
  _tombolPintasan(p) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn btn-outline-secondary btn-sm wp-shortcut';
    b.textContent = '+ ' + p.nama;
    if (p.judul) b.title = p.judul;
    if (!p.tanpaJumlah) {
      this._muat().then(d => { b.textContent = `+ ${p.nama} (${p.saring(d).length})`; });
    }
    b.onclick = () => this._muat().then(d => this._terapkan(p.saring(d), p.nama));
    return b;
  }

  /** Tambahkan sekumpulan pilihan sekaligus, lalu laporkan hasilnya. */
  _terapkan(daftar, nama) {
    const sebelum = this.dipilih.length;
    daftar.forEach(x => this._tambah(x, true));
    const baru = this.dipilih.length - sebelum;
    this._render();
    this._kabar(baru
      ? `+${baru} ${this.namaSatuan} dari "${nama}" — total ${this.dipilih.length}.`
      : `Semua ${this.namaSatuan} "${nama}" sudah ada di daftar.`);
  }

  /** Umpan balik singkat: tanpa ini, klik pintasan terasa seperti tidak terjadi apa-apa. */
  _kabar(teks) {
    this.umpanbalik.textContent = teks;
    this.umpanbalik.classList.add('show');
    clearTimeout(this._kabarTimer);
    this._kabarTimer = setTimeout(() => this.umpanbalik.classList.remove('show'), 4000);
  }

  _cari() {
    const q = this.input.value.trim().toLowerCase();
    this._muat().then(daftar => {
      const terpilih = new Set(this.dipilih.map(x => x.query));
      let hasil = daftar.filter(x => !terpilih.has(x.query));
      if (q) {
        hasil = hasil.filter(x => x.cari.includes(q));
        // Yang namanya diawali kata pencarian ditaruh paling atas.
        hasil.sort((a, b) => {
          const aw = a.label.toLowerCase().startsWith(q) ? 0 : 1;
          const bw = b.label.toLowerCase().startsWith(q) ? 0 : 1;
          return aw - bw;
        });
      }
      this.saran = hasil.slice(0, 50);
      this.sorotan = -1;
      this._renderDropdown(q);
    });
  }

  _renderDropdown(q) {
    if (!this.saran.length) {
      this.dropdown.innerHTML = q
        ? `<div class="wp-kosong">Tidak ada ${_esc(this.namaSatuan)} cocok</div>`
        : '';
      this.dropdown.classList.toggle('show', !!q);
      return;
    }
    this.dropdown.innerHTML = this.saran.map((x, i) => `
      <div class="wp-item${i === this.sorotan ? ' aktif' : ''}" data-i="${i}">
        <span>${_esc(x.label)}</span>
        ${this._barisDropdown(x)}
      </div>`).join('');
    this.dropdown.classList.add('show');
    this.dropdown.querySelectorAll('.wp-item').forEach(el => {
      el.onclick = () => {
        this._tambah(this.saran[parseInt(el.dataset.i)]);
        this.input.value = '';
        this._render();
        this._cari();
        this.input.focus();
      };
    });
  }

  _tombol(e) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!this.saran.length) return;
      this.sorotan += (e.key === 'ArrowDown' ? 1 : -1);
      if (this.sorotan < 0) this.sorotan = this.saran.length - 1;
      if (this.sorotan >= this.saran.length) this.sorotan = 0;
      this._renderDropdown(this.input.value.trim().toLowerCase());
      this.dropdown.querySelector('.wp-item.aktif')
        ?.scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const pilih = this.saran[this.sorotan >= 0 ? this.sorotan : 0];
      if (pilih) {
        this._tambah(pilih);
        this.input.value = '';
        this._render();
        this._cari();
      }
    } else if (e.key === 'Escape') {
      this._tutup();
    } else if (e.key === 'Backspace' && !this.input.value && this.dipilih.length) {
      this.dipilih.pop();
      this._render();
    }
  }

  _tambah(w, diam) {
    if (!w) return;
    if (this.dipilih.some(x => x.query === w.query)) return;
    this.dipilih.push(w);
    if (!diam) this._tutup();
  }

  _tutup() {
    this.dropdown.classList.remove('show');
    this.sorotan = -1;
  }

  _render() {
    this.chips.innerHTML = this.dipilih.map((x, i) => `
      <span class="wp-chip${this._chipKelas(x)}" title="${_esc(this._chipJudul(x))}">
        ${this._chipIkon(x)}${_esc(x.query)}
        <button type="button" class="wp-x" data-i="${i}" aria-label="Hapus">&times;</button>
      </span>`).join('');
    this.chips.querySelectorAll('.wp-x').forEach(b => {
      b.onclick = () => {
        this.dipilih.splice(parseInt(b.dataset.i), 1);
        this._render();
      };
    });
    this.host.dispatchEvent(new CustomEvent(this.eventBerubah, { bubbles: true }));
  }

  jumlah() { return this.dipilih.length; }
}

/* ─── Pemilih wilayah ─────────────────────────────────────────────────────── */

class WilayahPicker extends ChipPicker {
  _muat() { return muatWilayah(); }
  get placeholderSiap() { return 'Ketik nama kota / kecamatan...'; }
  get placeholderMuat() { return 'Memuat daftar wilayah...'; }
  get namaSatuan() { return 'wilayah'; }
  get eventBerubah() { return 'wilayah-berubah'; }

  _chipKelas(x) { return x.lat ? ' punya-geo' : ''; }
  _chipIkon(x) { return x.lat ? '<i class="bi bi-geo-alt-fill"></i>' : ''; }
  _chipJudul(x) {
    return x.lat ? 'Radius (km) berlaku'
                 : 'Tanpa koordinat — pengaturan radius tidak berlaku untuk wilayah ini';
  }
  _barisDropdown(x) {
    return x.lat
      ? '<i class="bi bi-geo-alt-fill wp-geo" title="Radius (km) berlaku untuk wilayah ini"></i>'
      : '';
  }

  _isiPanel() {
    const semua = pintasanWilayah();
    // Yang paling sering dipakai tetap terlihat; sisanya di panel supaya baris
    // pintasan tidak berubah jadi dinding berisi puluhan tombol.
    semua.filter(p => p.kelompok === 'favorit')
         .forEach(p => this.bar.appendChild(this._tombolPintasan(p)));

    this._grup('Pulau', semua.filter(p => p.kelompok === 'Pulau'));
    this._grup('Metropolitan', semua.filter(p => p.kelompok === 'Metropolitan'));

    const grupProv = document.createElement('div');
    grupProv.className = 'wp-grup';
    grupProv.innerHTML = `<div class="wp-grup-judul">Provinsi</div>`;
    const pilihProv = document.createElement('select');
    pilihProv.className = 'form-select form-select-sm wp-provinsi';
    pilihProv.innerHTML = '<option value="">Pilih provinsi — tambah semua wilayahnya…</option>';
    muatWilayah().then(d => {
      [...new Set(d.map(x => x.provinsi))].sort().forEach(nama => {
        const n = d.filter(x => x.provinsi === nama && bukanKec(x)).length;
        pilihProv.innerHTML += `<option value="${_esc(nama)}">${_esc(nama)} (${n})</option>`;
      });
    });
    pilihProv.onchange = () => {
      const prov = pilihProv.value;
      if (!prov) return;
      muatWilayah().then(d => {
        this._terapkan(d.filter(x => x.provinsi === prov && bukanKec(x)), prov);
        pilihProv.value = '';
      });
    };
    grupProv.appendChild(pilihProv);
    this.panel.appendChild(grupProv);

    this._grup('Tipe wilayah', semua.filter(p => p.kelompok === 'Tipe wilayah'),
      'Kecamatan menghasilkan lead paling banyak: satu pencarian Google Maps ' +
      'mentok di ±120 hasil, jadi memecah kota jadi kecamatan melipatgandakan ' +
      'lead unik.');
  }

  /** Daftar area terpilih: [{area, coords}] — coords "" bila tanpa koordinat. */
  nilai() {
    return this.dipilih.map(x => ({
      area: x.query,
      coords: (x.lat != null && x.lng != null) ? `${x.lat},${x.lng}` : ''
    }));
  }
}

/* ─── Pemilih jenis bisnis ────────────────────────────────────────────────── */

class KeywordPicker extends ChipPicker {
  _muat() { return muatKeywords(); }
  get placeholderSiap() { return 'Ketik jenis bisnis, atau pilih dari pustaka...'; }
  get placeholderMuat() { return 'Memuat pustaka jenis bisnis...'; }
  get namaSatuan() { return 'jenis bisnis'; }
  get eventBerubah() { return 'wilayah-berubah'; }

  _chipJudul(x) { return x.alasan || ''; }
  _barisDropdown(x) {
    return x.alasan ? `<small class="wp-alasan">${_esc(x.alasan)}</small>` : '';
  }

  _isiPanel() {
    // Panel baru bisa disusun setelah pustakanya datang; promise-nya dikembalikan
    // supaya kelas dasar tahu kapan tombol "Pintasan lainnya" boleh ditampilkan.
    return muatKeywords().then(d => {
      [...new Set(d.map(x => x.sektor))].forEach(nama => {
        const isi = d.filter(x => x.sektor === nama);
        this._grup(nama, [{
          nama: 'Semua ' + nama,
          saring: dd => dd.filter(x => x.sektor === nama)
        }].concat(isi.map(k => ({
          nama: k.query,
          judul: k.alasan,
          tanpaJumlah: true,
          saring: dd => dd.filter(x => x.query === k.query)
        }))));
      });
    });
  }

  /**
   * Jenis bisnis yang diketik bebas tetap diterima — pustaka ini rekomendasi,
   * bukan pagar. Kata kunci baru yang belum ada di pustaka boleh dipakai.
   */
  _cari() {
    const q = this.input.value.trim();
    super._cari();
    if (!q) return;
    this._muat().then(d => {
      const ada = d.some(x => x.query.toLowerCase() === q.toLowerCase()) ||
                  this.dipilih.some(x => x.query.toLowerCase() === q.toLowerCase());
      if (!ada) {
        this.saran = [{ query: q, label: `Pakai "${q}" (di luar pustaka)`,
                        sektor: '', alasan: '', cari: q.toLowerCase() }]
                     .concat(this.saran).slice(0, 50);
        this._renderDropdown(q.toLowerCase());
      }
    });
  }

  /** Daftar jenis bisnis terpilih sebagai teks biasa. */
  nilai() {
    return this.dipilih.map(x => x.query);
  }
}

function _esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
