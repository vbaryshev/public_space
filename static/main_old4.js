let map;
let abPickMode = false;
let abMarker = null;

async function fetchJSON(url, opts = {}) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    const t = await res.text();
    //throw new Error(${res.status}: ${t});
  }
  return await res.json();
}

function parseJSONInput(id) {
  const v = document.getElementById(id).value.trim();
  return v ? JSON.parse(v) : {};
}

function status(elId, msg) {
  document.getElementById(elId).textContent = msg;
}

function fmt(num, digits = 2) {
  return typeof num === "number" ? num.toFixed(digits) : String(num);
}

async function initMap() {
  const meta = await fetchJSON("/metadata");
  const center = meta.center || [37.618423, 55.751244]; // fallback: Москва
  const bounds = meta.bounds || null;

  map = new maplibregl.Map({
    container: 'map',
    style: 'https://raw.githubusercontent.com/vbaryshev/basemap/refs/heads/main/voyager-nolabels.json',
    center: center,
    zoom: 12
  });

  map.addControl(new maplibregl.NavigationControl(), 'top-right');

  map.on('load', () => {
    if (bounds) {
      map.fitBounds(bounds, { padding: 20, maxZoom: 14 });
    }

    // Исходные POI (серые)
    map.addSource('poi-raw', {
      type: 'geojson',
      data: '/poi/raw'
    });
    map.addLayer({
      id: 'poi-raw-circles',
      type: 'circle',
      source: 'poi-raw',
      paint: {
        'circle-radius': 4,
        'circle-color': '#8c8c8c',
        'circle-opacity': 0.7,
        'circle-stroke-color': '#333',
        'circle-stroke-width': 0.5
      }
    });

    // Результаты симуляции
    map.addSource('poi-sim', {
      type: 'geojson',
      data: '/poi/sim'
    });
    map.addLayer({
      id: 'poi-sim-circles',
      type: 'circle',
      source: 'poi-sim',
      paint: {
        'circle-radius': [
          'interpolate', ['linear'], ['coalesce', ['get', 'mean_daily_visitors'], 0],
          0, 2,
          5, 4,
          20, 10,
          50, 18,
          100, 26,
          200, 36
        ],
        'circle-color': [
          'interpolate', ['linear'], ['coalesce', ['get', 'mean_daily_visitors'], 0],
          0, '#2c7bb6',
          20, '#abd9e9',
          50, '#ffffbf',
          100, '#fdae61',
          200, '#d7191c'
        ],
        'circle-opacity': 0.8,
        'circle-stroke-color': '#222',
        'circle-stroke-width': 0.8
      }
    });

    const popup = new maplibregl.Popup({ closeButton: true, closeOnClick: true });

    function bindPopup(layerId) {
      map.on('click', layerId, (e) => {
        if (!e.features || !e.features.length) return;
        const f = e.features[0];
        const p = f.properties || {};
        const typ = p.type || 'n/a';
        const md = p.mean_daily_visitors ? Number(p.mean_daily_visitors) : 0;
        const mm = p.mean_monthly_visitors ? Number(p.mean_monthly_visitors) : null;

        const html =
          '<div>' +
            '<div><b>Тип:</b> ' + typ + '</div>' +
            '<div><b>Среднедневные визиты:</b> ' + fmt(md) + '</div>' +
            (mm !== null ? '<div><b>Среднемесячные:</b> ' + fmt(mm) + '</div>' : '') +
          '</div>';
        popup.setLngLat(e.lngLat).setHTML(html).addTo(map);
      });

      map.on('mouseenter', layerId, () => map.getCanvas().style.cursor = 'pointer');
      map.on('mouseleave', layerId, () => map.getCanvas().style.cursor = '');
    }

    bindPopup('poi-raw-circles');
    bindPopup('poi-sim-circles');

    map.on('click', (e) => {
      if (!abPickMode) return;
      const { lng, lat } = e.lngLat;
      document.getElementById('abLat').value = lat.toFixed(6);
      document.getElementById('abLon').value = lng.toFixed(6);

      if (abMarker) {
        abMarker.remove();
      }
      abMarker = new maplibregl.Marker({ color: '#1111cc' }).setLngLat([lng, lat]).addTo(map);
      abPickMode = false;
      document.getElementById('pickBtn').textContent = "Выбрать на карте";
    });
  });
}

async function refreshSources(fit = false) {
  const meta = await fetchJSON("/metadata");
  const poiRaw = map.getSource('poi-raw');
  if (poiRaw) poiRaw.setData('/poi/raw');
  const poiSim = map.getSource('poi-sim');
  if (poiSim) poiSim.setData('/poi/sim');

  if (fit && meta.bounds) {
    map.fitBounds(meta.bounds, { padding: 20, maxZoom: 14 });
  }
}

async function uploadGpkg() {
  const input = document.getElementById('gpkgInput');
  if (!input.files.length) {
    alert("Выберите .gpkg");
    return;
  }
  const fd = new FormData();
  fd.append('file', input.files[0]);
  status('initStatus', 'Загрузка...');
  try {
    await fetchJSON('/upload-gpkg', { method: 'POST', body: fd });
    status('initStatus', 'Файл загружен. Нажмите «Инициализировать».');
  } catch (e) {
    status('initStatus', 'Ошибка загрузки: ' + e.message);
  }
}

async function initPipeline() {
  status('initStatus', 'Инициализация...');
  try {
    const res = await fetchJSON('/init', { method: 'POST' });
    status('initStatus', 'OK. POI: '+ res.poi_count);
    await refreshSources(true);
  } catch (e) {
    status('initStatus', 'Ошибка инициализации: ' + e.message);
  }
}

async function runSimulation() {
  status('simStatus', 'Симуляция...');
  try {
    const body = {
      alpha: Number(document.getElementById('alpha').value),
      beta: Number(document.getElementById('beta').value),
      gamma: Number(document.getElementById('gamma').value),
      d0: Number(document.getElementById('d0').value),
      cutoff_distance: Number(document.getElementById('cutoff').value),
      base_rate_per_person_per_hour: Number(document.getElementById('baseRate').value),
      start: document.getElementById('startDate').value || '2025-01-01',
      end: document.getElementById('endDate').value || '2025-12-31',
      freq: document.getElementById('freq').value || 'H',
      poi_type_weights: parseJSONInput('typeWeights'),
      tod_multipliers: parseJSONInput('tod'),
      weather_multipliers: parseJSONInput('weather'),
      season_multipliers: parseJSONInput('season'),
    };

    const res = await fetchJSON('/run-sim', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });

    status('simStatus', 'Готово.');
    const summaryEl = document.getElementById('summaryByType');
    const rows = (res.summary_by_type || [])
      .map(r => r.type + ': ' + fmt(r.mean_daily_visitors))
      .join('\n');
    summaryEl.textContent = rows || 'нет данных';
    await refreshSources(false);
  } catch (e) {
    status('simStatus', 'Ошибка симуляции: ' + e.message);
  }
}

function togglePick() {
  abPickMode = !abPickMode;
  document.getElementById('pickBtn').textContent = abPickMode ? "Кликните на карте..." : "Выбрать на карте";
}

async function runAB() {
  status('abStatus', 'A/B-тест...');
  try {
    const body = {
      lat: Number(document.getElementById('abLat').value),
      lon: Number(document.getElementById('abLon').value),
      new_type: document.getElementById('abType').value || 'cafe'
    };
    const res = await fetchJSON('/ab-test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    'let msg = Δ среднедневных визитов к cafe: '  + fmt(res.mean_delta) + '\nДней: '+ res.n_days;
    if (res.p_value !== null && res.p_value !== undefined) {
      "msg += \np-value (H1: increase): " + fmt(res.p_value, 4) + '\nВывод: ' + res.conclusion;
    } else {
      'msg += \nВывод: ' + res.conclusion;
    }
    status('abStatus', msg);
  } catch (e) {
    status('abStatus', 'Ошибка A/B: ' + e.message);
  }
}
document.addEventListener('DOMContentLoaded', () => {
  initMap();
  document.getElementById('uploadBtn').addEventListener('click', uploadGpkg);
  document.getElementById('initBtn').addEventListener('click', initPipeline);
  document.getElementById('runSimBtn').addEventListener('click', runSimulation);
  document.getElementById('pickBtn').addEventListener('click', togglePick);
  document.getElementById('runAbBtn').addEventListener('click', runAB);
});
