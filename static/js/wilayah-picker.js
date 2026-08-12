/**
 * wilayah-picker.js — Pemilih kota/kecamatan bergaya tag (chip).
 *
 * Menggantikan input area yang diketik bebas: salah ketik "Jakarta Slatan"
 * dulu tetap dikirim ke Google dan menghasilkan nol hasil tanpa penjelasan.
 * Sekarang area hanya bisa dipilih dari daftar resmi, dan koordinat titik
 * pusatnya ikut terbawa otomatis sehingga pengaturan Radius (km) bekerja tanpa
 * perlu menyalin lat/long dari Google Maps.
 *
 * Ditulis tanpa library tambahan supaya tidak menambah beban CDN.
 */

let _wilayahCache = null;
let _wilayahPromise = null;

/** Ambil & ratakan data wilayah. Hanya sekali per halaman. */
function muatWilayah() {
  if (_wilayahCache) return Promise.resolve(_wilayahCache);
  if (_wilayahPromise) return _wilayahPromise;

  _wilayahPromise = fetch('/api/wilayah')
    .then(r => r.json())
    .then(data => {
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
      _wilayahCache = daftar;
      return daftar;
    });
  return _wilayahPromise;
}

// Pintasan: kombinasi yang paling sering dipakai.
const PINTASAN = {
  'Jabodetabek': d => d.filter(x => x.tipe !== 'kec' && [
    'Jakarta Pusat', 'Jakarta Selatan', 'Jakarta Barat', 'Jakarta Timur',
    'Jakarta Utara', 'Bogor', 'Depok', 'Tangerang', 'Tangerang Selatan', 'Bekasi'
  ].includes(x.query)),
  'Semua kec. Jakarta': d => d.filter(x => x.tipe === 'kec' &&
    (x.induk || '').startsWith('Jakarta')),
  'Kota besar': d => d.filter(x => x.tipe !== 'kec' && [
    'Jakarta Pusat', 'Jakarta Selatan', 'Bandung', 'Surabaya', 'Medan',
    'Semarang', 'Makassar', 'Palembang', 'Bekasi', 'Depok', 'Tangerang',
    'Bogor', 'Batam', 'Pekanbaru', 'Bandar Lampung', 'Padang', 'Malang',
    'Denpasar', 'Samarinda', 'Balikpapan', 'Banjarmasin', 'Yogyakarta',
    'Surakarta (Solo)', 'Pontianak', 'Manado', 'Jambi', 'Cimahi'
  ].includes(x.query))
};

class WilayahPicker {
  constructor(host) {
    this.host = host;
    this.dipilih = [];     // daftar objek wilayah terpilih
    this.sorotan = -1;     // indeks saran yang sedang disorot keyboard
    this.saran = [];
    this._bangunDOM();
    muatWilayah().then(() => {
      this.input.placeholder = 'Ketik nama kota / kecamatan...';
      this.input.disabled = false;
    });
  }

  _bangunDOM() {
    this.host.classList.add('wilayah-picker');
    this.host.innerHTML = `
      <div class="wp-chips"></div>
      <div class="wp-input-wrap">
        <input type="text" class="form-control form-control-sm wp-input"
               placeholder="Memuat daftar wilayah..." disabled autocomplete="off">
        <div class="wp-dropdown"></div>
      </div>
      <div class="wp-shortcuts"></div>`;

    this.chips = this.host.querySelector('.wp-chips');
    this.input = this.host.querySelector('.wp-input');
    this.dropdown = this.host.querySelector('.wp-dropdown');
    const bar = this.host.querySelector('.wp-shortcuts');

    Object.keys(PINTASAN).forEach(nama => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'btn btn-outline-secondary btn-sm wp-shortcut';
      b.textContent = '+ ' + nama;
      b.onclick = () => muatWilayah().then(d => {
        PINTASAN[nama](d).forEach(x => this._tambah(x, true));
        this._render();
      });
      bar.appendChild(b);
    });
    const hapus = document.createElement('button');
    hapus.type = 'button';
    hapus.className = 'btn btn-outline-danger btn-sm wp-shortcut';
    hapus.textContent = 'Hapus semua';
    hapus.onclick = () => { this.dipilih = []; this._render(); };
    bar.appendChild(hapus);

    this.input.addEventListener('input', () => this._cari());
    this.input.addEventListener('focus', () => this._cari());
    this.input.addEventListener('keydown', e => this._tombol(e));
    document.addEventListener('click', e => {
      if (!this.host.contains(e.target)) this._tutup();
    });
  }

  _cari() {
    const q = this.input.value.trim().toLowerCase();
    muatWilayah().then(daftar => {
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
        ? '<div class="wp-kosong">Tidak ada wilayah cocok</div>'
        : '';
      this.dropdown.classList.toggle('show', !!q);
      return;
    }
    this.dropdown.innerHTML = this.saran.map((x, i) => `
      <div class="wp-item${i === this.sorotan ? ' aktif' : ''}" data-i="${i}">
        <span>${_esc(x.label)}</span>
        ${x.lat ? '<i class="bi bi-geo-alt-fill wp-geo" title="Radius (km) berlaku untuk wilayah ini"></i>' : ''}
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
      <span class="wp-chip${x.lat ? ' punya-geo' : ''}"
            title="${x.lat ? 'Radius (km) berlaku' : 'Tanpa koordinat — pengaturan radius tidak berlaku untuk wilayah ini'}">
        ${x.lat ? '<i class="bi bi-geo-alt-fill"></i>' : ''}${_esc(x.query)}
        <button type="button" class="wp-x" data-i="${i}" aria-label="Hapus">&times;</button>
      </span>`).join('');
    this.chips.querySelectorAll('.wp-x').forEach(b => {
      b.onclick = () => {
        this.dipilih.splice(parseInt(b.dataset.i), 1);
        this._render();
      };
    });
    this.host.dispatchEvent(new CustomEvent('wilayah-berubah', { bubbles: true }));
  }

  /** Daftar area terpilih: [{area, coords}] — coords "" bila tanpa koordinat. */
  nilai() {
    return this.dipilih.map(x => ({
      area: x.query,
      coords: (x.lat != null && x.lng != null) ? `${x.lat},${x.lng}` : ''
    }));
  }

  jumlah() { return this.dipilih.length; }
}

function _esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
