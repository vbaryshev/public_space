/* global maplibregl */
let map;
let rawPoiSourceId = "poi-raw-source";
let rawPoiLayerId = "poi-raw-layer";
let poiSourceId = "poi-source";
let poiLayerId = "poi-layer";
let variantSourceId = "poi-variant-source";
let variantLayerId = "poi-variant-layer";

let pickOnMap = false;

function $(id) { return document.getElementById(id); }

function readJSON(id, fallback) {
  try { return JSON.parse($(id).value); }
  catch(e) { return fallback; }
}

function setStatus(el, msg) {
  el.textContent = msg;
}

async function fetchJSON(url, opts) {
  const r = await fetch(url, opts || {});
  if (!r.ok) {
    const t = await r.text();
    throw new Error(t || (r.status + " " + r.statusText));
  }
  return r.json();
}




// Модалка и слайдер
function initIntroModal() {
  const modal = document.getElementById('intro-modal');
  const closeBtn = document.getElementById('intro-close');
  const dontShowChk = document.getElementById('intro-dont-show');

  const slides = Array.from(document.querySelectorAll('#intro-slider .slide'));
  const prev = document.getElementById('slider-prev');
  const next = document.getElementById('slider-next');
  const dotsContainer = document.getElementById('slider-dots');

  let idx = 0;

  function renderDots() {
    dotsContainer.innerHTML = '';
    slides.forEach((_, i) => {
      const b = document.createElement('button');
      if (i === idx) b.classList.add('active');
      b.addEventListener('click', () => { idx = i; update(); });
      dotsContainer.appendChild(b);
    });
  }

  function update() {
    slides.forEach((s, i) => s.classList.toggle('active', i === idx));
    Array.from(dotsContainer.children).forEach((d, i) => d.classList.toggle('active', i === idx));
  }

  function show() {
    modal.classList.remove('hidden');
    // Блокируем прокрутку фона
    document.documentElement.style.overflow = 'hidden';
  }

  function hide() {
    modal.classList.add('hidden');
    document.documentElement.style.overflow = '';
    if (dontShowChk.checked) {
      localStorage.setItem('intro_modal_hide', '1');
    }
  }

  prev.addEventListener('click', () => { idx = (idx - 1 + slides.length) % slides.length; update(); });
  next.addEventListener('click', () => { idx = (idx + 1) % slides.length; update(); });
  closeBtn.addEventListener('click', hide);
  modal.addEventListener('click', (e) => { if (e.target === modal) hide(); });
  document.addEventListener('keydown', (e) => { if (!modal.classList.contains('hidden') && e.key === 'Escape') hide(); });

  renderDots();
  update();

  // Показывать при первом заходе, скрывать если пользователь выбрал "не показывать"
  if (!localStorage.getItem('intro_modal_hide')) {
    show();
  }
}

// Вариант А: показать при загрузке страницы
document.addEventListener('DOMContentLoaded', initIntroModal);

// Вариант Б: показать только после загрузки карты
// Внутри вашего initMap, в map.on('load', ...) добавьте:
// initIntroModal();


















function initMap(center, zoom=12) {
  map = new maplibregl.Map({
    container: 'map',
    style: 'https://raw.githubusercontent.com/vbaryshev/basemap/refs/heads/main/voyager-nolabels.json',
    center: center || [41.38, 54.94],
    zoom: zoom
  });

  map.addControl(new maplibregl.NavigationControl({ showCompass: true }), 'top-left');

  map.on('load', async () => {
    // Попытка загрузить метаданные (bounds/center) и сырые POI
    try {
      const meta = await fetchJSON('/metadata');
      if (meta && meta.center) {
        map.setCenter(meta.center);
      }
      if (meta && meta.bounds) {
        const b = meta.bounds; // [minx, miny, maxx, maxy]
        map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 40, duration: 0 });
      }

      // Сырые POI
      const raw = await fetchJSON('/poi_raw');
      addOrUpdateRawPoi(raw);
    } catch(e) {
      // ignore
    }

    // Клик по карте для выбора точки (A/B)
    map.on('click', (ev) => {
      if (!pickOnMap) return;
      const lngLat = ev.lngLat;
      $('abLat').value = lngLat.lat.toFixed(8);
      $('abLon').value = lngLat.lng.toFixed(8);
    });
  });
}

function addOrUpdateRawPoi(geojson) {
  if (!map) return;
  if (map.getSource(rawPoiSourceId)) {
    map.getSource(rawPoiSourceId).setData(geojson);
  } else {
    map.addSource(rawPoiSourceId, { type: 'geojson', data: geojson });
    map.addLayer({
      id: rawPoiLayerId,
      type: 'circle',
      source: rawPoiSourceId,
      paint: {
        'circle-radius': 4,
        'circle-color': '#999999',
        'circle-opacity': 0.6,
        'circle-stroke-color': '#ffffff',
        'circle-stroke-width': 0.5
      }
    });
  }
}

function addOrUpdatePoi(geojson, layerId, sourceId) {
  if (map.getSource(sourceId)) {
    map.getSource(sourceId).setData(geojson);
  } else {
    map.addSource(sourceId, { type: 'geojson', data: geojson });
    map.addLayer({
      id: layerId,
      type: 'circle',
      source: sourceId,
      paint: {
        // Радиус-скейлинг по mean_daily_visitors
        'circle-radius': [
          'interpolate', ['linear'], ["coalesce", ['get', 'mean_daily_visitors'],0],
          0, 1,
          10, 3,
          50, 6,
          200, 12,
          500, 18,
          1000, 24
        ],
          "circle-color": [
            "step",
            ["coalesce", ["get", "mean_daily_visitors"], 0],
            "#e3b64d",    // 0
            5, "#e8c35d",
            10, "#ecd06d",
            15, "#f1dd7d",
            20, "#f5ea8d", // 20
            30, "#d9e89e",
            40, "#bde6af",
            50, "#a1e4c0", // 50
            70, "#85d9c8",
            90, "#69ced0",
            110, "#4dc3d8", // 110
            140, "#41b0c9",
            170, "#359dba",
            200, "#298aab", // 200
            250, "#3d7da4",
            300, "#51709d", // 300
            350, "#8a7ba9",
            400, "#c386b5",
            450, "#d497c7", // 450
            500, "#e5a8d9",
            550, "#e695c2",
            600, "#e782ab"  // 600+
          ],
        'circle-opacity': 0.6,
        'circle-stroke-color': '#2c7fb8',
        'circle-stroke-width': 0.6
      }
    });

    // Слой кругов с радиусом от visits
//    map.addLayer({
//      id: poisLayerId,
//      source: poisSourceId,
//      type: "circle",
//      paint: {
//        "circle-color": [
//          "interpolate", ["linear"], ["coalesce", ["get", "visits"], 0],
//          0, "#2c7fb8",
//          10, "#41b6c4",
//          50, "#a5d439",
//          100, "#d6d62d",
//          200, "#e3b64d",
//          300, "#d4a1da",
//          400, "#d4a1da",
//          600, "#75000a"
//        ],
//        "circle-opacity": 0.8,
//        "circle-stroke-color": "#333",
//        "circle-stroke-width": 1.0,
//        "circle-radius": [
//          "interpolate", ["linear"], ["coalesce", ["get", "visits"], 0],
//          0, 1,
//          10, 2,
//          50, 4,
//          100, 6,
//          200, 8,
//          300, 10,
//          400, 12,
//          600, 16,
//        ]
//      }
//    });




























    // Попап по клику
    map.on('click', layerId, (e) => {
      const f = e.features && e.features[0];
      if (!f) return;
      const props = f.properties || {};
      const typ = props.type || 'n/a';
      const md = props.mean_daily_visitors !== undefined ? Number(props.mean_daily_visitors).toFixed(2) : 'n/a';
      const mm = props.mean_monthly_visitors !== undefined ? Number(props.mean_monthly_visitors).toFixed(2) : 'n/a';

      new maplibregl.Popup()
        .setLngLat(e.lngLat)
        .setHTML(
          '<div><b>type:</b> ' + typ +
          '<br><b>mean_daily_visitors:</b> ' + md +
          '<br><b>mean_monthly_visitors:</b> ' + mm +
          '</div>'
        )
      .addTo(map);
    });

    map.on('mouseenter', layerId, () => map.getCanvas().style.cursor = 'pointer');
    map.on('mouseleave', layerId, () => map.getCanvas().style.cursor = '');
  }
}

async function handleUpload() {
  const f = $('gpkgFile').files[0];
  if (!f) {
    setStatus($('uploadStatus'), 'Выберите файл .gpkg');
    return;
  }
  setStatus($('uploadStatus'), 'Загрузка...');
  const fd = new FormData();
  fd.append('file', f, f.name);
  try {
    const res = await fetchJSON('/upload', { method: 'POST', body: fd });
    setStatus($('uploadStatus'), 'OK: ' + res.message + '\nPOI: ' + res.poi_count);
    // Обновить карту: bounds/center и сырые POI
    const raw = await fetchJSON('/poi_raw');
    addOrUpdateRawPoi(raw);

    if (res.bounds) {
      map.fitBounds([[res.bounds[0], res.bounds[1]], [res.bounds[2], res.bounds[3]]], { padding: 40 });
    }
  } catch(e) {
    setStatus($('uploadStatus'), 'Ошибка: '+ (e.message));
  }
}

async function handleRunSimulation() {
  setStatus($('simStatus'), 'Запуск симуляции...');
  const body = {
    dist_bins: readJSON('distBins', [0, 500, 1500, 3000, 5000]),
    edges: [
      ["distance_cat", "visit"],
      ["weather", "visit"],
      ["time_of_day", "visit"],
      ["poi_type", "visit"],
      ["season", "visit"]
    ],
    gravity: {
      alpha: Number($('alpha').value),
      beta: Number($('beta').value),
      gamma: Number($('gamma').value),
      d0: Number($('d0').value),
    },
    poi_type_weights: readJSON('typeWeights', {"mall": 1.5, "park": 0.8, "cafe": 1.1}),
    precompute_max_distance: Number($('precomputeMaxDist').value),
    sim_cfg: {
      start: $('start').value,
      end: $('end').value,
      freq: $('freq').value,
      base_rate_per_person_per_hour: Number($('baseRate').value),
      tod_multipliers: readJSON('todMultipliers', {"morning":0.9,"afternoon":1.1,"evening":1.0}),
      weather_multipliers: readJSON('weatherMultipliers', {"sunny":1.2,"rainy":0.3,"windy":0.8,"snowy":0.8}),
      season_multipliers: readJSON('seasonMultipliers', {"spring":1.0,"summer":1.5,"autumn":0.7,"winter":0.5}),
      cutoff_distance: Number($('cutoffDistance').value)
    }
  };

  try {
    const res = await fetchJSON('/run-simulation', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    setStatus($('simStatus'), 'OK: ' + res.message + '\nPOI: ' + res.poi_count);

    const gj = await fetchJSON('/poi_geojson');
    addOrUpdatePoi(gj, poiLayerId, poiSourceId);

    if (res.bounds) {
      map.fitBounds([[res.bounds[0], res.bounds[1]], [res.bounds[2], res.bounds[3]]], { padding: 40 });
    }
  } catch(e) {
    setStatus($('simStatus'), 'Ошибка: ' + e.message);
  }
}

async function handleAB() {
  setStatus($('abStatus'), 'Выполняется A/B тест...');
  const body = {
    lat: Number($('abLat').value),
    lon: Number($('abLon').value),
    new_type: $('abType').value || 'cafe'
  };
  try {
    const res = await fetchJSON('/ab-test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });

    const lines = [];
    lines.push('Новая точка: (' + res.new_point.lat.toFixed(6) + ', ' + res.new_point.lon.toFixed(6) + ') type=' + res.new_point.type);
    lines.push('Mean daily (base): ' + res.mean_daily_base.toFixed(3));
    lines.push('Mean daily (variant): ' + res.mean_daily_variant.toFixed(3));
    lines.push('Δ = ' + res.mean_diff.toFixed(3));
    lines.push('p(one-sided) = ' + res.p_value_one_sided.toFixed(4) + '; alpha=0.05');
    lines.push(res.reject_H0 ? 'Итог: отвергаем H0 (увеличение статистически значимо).' : 'Итог: не отвергаем H0.');
    setStatus($('abStatus'), lines.join('\n'));

    // Показать вариант (например, другим цветом)
    if (res.variant_poi_geojson) {
      addOrUpdatePoi(res.variant_poi_geojson, variantLayerId, variantSourceId);
    }
  } catch(e) {
    setStatus($('abStatus'), 'Ошибка: ' + e.message);
  }
}

function bindUI() {
  $('btnUpload').addEventListener('click', handleUpload);
  $('btnRunSim').addEventListener('click', handleRunSimulation);
  $('btnAB').addEventListener('click', handleAB);
  $('pickOnMap').addEventListener('change', (e) => pickOnMap = e.target.checked);
}

(async function main() {
  bindUI();
  // Инициализация карты
  try {
    const meta = await fetchJSON('/metadata');
    initMap(meta && meta.center ? meta.center : [41.3814, 54.9451], 12);
  } catch(e) {
    initMap([41.3814, 54.9451], 12);
  }







})();
