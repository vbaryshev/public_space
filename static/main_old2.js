let map;
let selectedGpkg = null;
const poisSourceId = "pois-src";
const poisLayerId = "pois-layer";
let chart; // Chart.js instance
let isSimulated = false;

function $(sel) { return document.querySelector(sel); }

function parseJSONOr(objText, fallback) {
  try {
    const v = JSON.parse(objText);
    return v && typeof v === "object" ? v : fallback;
  } catch {
    return fallback;
  }
}

function parseNumber(val, def = 0) {
  const n = Number(val);
  return Number.isFinite(n) ? n : def;
}

function parseBins(text) {
  return text.split(",").map(s => parseNumber(s.trim())).filter(n => Number.isFinite(n));
}

function setStatus(id, text, isError=false) {
  const el = $(id);
  el.textContent = text || "";
  el.style.color = isError ? "#b91c1c" : "#374151";
}

function enableActions(enabled) {
  $("#btnLoadPoi").disabled = !enabled;
  $("#btnRun").disabled = !enabled;
}

function initMap() {
  map = new maplibregl.Map({
    container: 'map',
    style: 'https://raw.githubusercontent.com/vbaryshev/basemap/refs/heads/main/voyager-nolabels.json',
    center: [37.62, 55.75],
    zoom: 9
  });
  map.addControl(new maplibregl.NavigationControl(), 'top-right');
  map.addControl(new maplibregl.ScaleControl({maxWidth: 200, unit: 'metric'}));

  map.on('click', (e) => {
    if (!map.getLayer(poisLayerId)) return;
    const features = map.queryRenderedFeatures(e.point, { layers: [poisLayerId] });
    if (!features.length) return;

    const f = features[0];
    const p = f.properties || {};
    const name = p.name || "—";
    const poiType = p.poi_type || "—";
    let visits = "—";
    if (isSimulated) {
      visits = p.visits ? Number(p.visits).toFixed(0) : "0";
    }

    const line1 = document.createElement('div');
    const b = document.createElement('b');
    b.textContent = name;
    line1.appendChild(b);

    const line2 = document.createElement('div');
    line2.textContent = 'Тип: ' + poiType;

    const lines = [line1, line2];
    if (isSimulated) {
    const div = document.createElement('div');
    div.textContent = 'Посетители за день: $ + visits +';
    lines.push(div);
    }

    new maplibregl.Popup({ closeOnClick: true })
      .setLngLat(f.geometry.type === 'Point' ? f.geometry.coordinates : e.lngLat)
      .setHTML('<div style="min-width:220px">' + lines.join('') + '</div>')
      .addTo(map);
  });
}
async function loadGpkgList(selectName=null) {
  setStatus("#uploadStatus", "Загрузка списка файлов...");
  try {
    const resp = await fetch("/gpkg_list");
    if (!resp.ok) throw new Error(resp.statusText);
    const data = await resp.json();
    const files = data.files || [];
    const sel = $("#gpkgSelect");
    sel.innerHTML = "";
    files.forEach(f => {
      const opt = document.createElement("option");
      opt.value = f.filename;
      opt.textContent = 'File: ' + f.filename;
      sel.appendChild(opt);
    });
    if (files.length) {
      // Выбираем либо переданное имя, либо первый файл
      if (selectName && files.some(f => f.filename === selectName)) {
        sel.value = selectName;
      } else {
        sel.selectedIndex = 0;
      }
      selectedGpkg = sel.value;
      enableActions(true);
      setStatus("#uploadStatus", "Доступных файлов: " + files.length + ". Выбран: " + selectedGpkg);
    } else {
      selectedGpkg = null;
      enableActions(false);
      setStatus("#uploadStatus", "Список пуст. Загрузите .gpkg файл.");
    }
  } catch (e) {
    setStatus("#uploadStatus", "Ошибка получения списка: " + e.message, true);
  }
}

async function uploadGpkg() {
  const fileInput = $("#gpkgFile");
  if (!fileInput.files || !fileInput.files.length) {
    setStatus("#uploadStatus", "Выберите .gpkg файл для загрузки", true);
    return;
  }
  const file = fileInput.files[0];
  const formData = new FormData();
  formData.append("file", file);

  setStatus("#uploadStatus", "Загрузка...");
  try {
    const resp = await fetch("/upload_gpkg", { method: "POST", body: formData });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || resp.statusText);
    }
    const data = await resp.json();
    setStatus("#uploadStatus", "Загружено: " + data.filename);
    await loadGpkgList(data.filename); // обновляем список и выбираем загруженный
  } catch (e) {
    setStatus("#uploadStatus", "Ошибка загрузки: " + e.message, true);
  } finally {
    fileInput.value = "";
  }
}

async function loadPois() {
  if (!selectedGpkg) {
    setStatus("#uploadStatus", "Выберите файл в списке", true);
    return;
  }
  setStatus("#uploadStatus", "Загрузка POI...");
  try {
    const resp = await fetch("/load_poi", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ gpkg_filename: selectedGpkg })
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || resp.statusText);
    }
    const data = await resp.json();
    addOrUpdatePoisPre(data.geojson, data.bounds);
    setStatus("#uploadStatus", "Файл: " + selectedGpkg + ". POI: " + (data?.count ?? 0));
    updateLegend(null);
    updateTypeChart([]);
  } catch (e) {
    setStatus("#uploadStatus", "Ошибка загрузки POI: " + e.message, true);
  }
}

function ensurePoiSource(geojson) {
  if (!map.getSource(poisSourceId)) {
    map.addSource(poisSourceId, { type: "geojson", data: geojson || { type: "FeatureCollection", features: [] } });
  } else if (geojson) {
    map.getSource(poisSourceId).setData(geojson);
  }
}

function addOrUpdatePoisPre(geojson, bounds) {
  isSimulated = false;
  if (map.getLayer(poisLayerId)) map.removeLayer(poisLayerId);
  ensurePoiSource(geojson);

  map.addLayer({
    id: poisLayerId,
    type: "circle",
    source: poisSourceId,
    paint: {
      "circle-color": "#6b7280",
      "circle-opacity": 0.7,
      "circle-stroke-color": "#374151",
      "circle-stroke-width": 0.5,
      "circle-radius": 4
    }
  });

  if (Array.isArray(bounds) && bounds.length === 4) {
    const b = [[bounds[0], bounds[1]], [bounds[2], bounds[3]]];
    map.fitBounds(b, { padding: 40, duration: 800 });
  }
}

function addOrUpdatePoisSimulated(geojson, stats, bounds) {
  isSimulated = true;
  if (map.getLayer(poisLayerId)) map.removeLayer(poisLayerId);
  ensurePoiSource(geojson);

  const vmin = stats?.min ?? 0;
  const vmax = stats?.max ?? 0;
  const rMin = 4, rMax = 36;

  const radiusExpr = (vmax - vmin) > 0
    ? ["interpolate", ["linear"], ["to-number", ["get", "visits"]],
        vmin, rMin,
        vmax, rMax]
    : rMin;

  const colorExpr = (vmax - vmin) > 0
    ? ["interpolate", ["linear"], ["to-number", ["get", "visits"]],
        vmin, "#93c5fd",
        (vmin + vmax) / 2, "#fbbf24",
        vmax, "#ef4444"
      ]
    : "#93c5fd";

  map.addLayer({
    id: poisLayerId,
    type: "circle",
    source: poisSourceId,
    paint: {
      "circle-color": colorExpr,
      "circle-opacity": 0.75,
      "circle-stroke-color": "#1f2937",
      "circle-stroke-width": 1,
      "circle-radius": radiusExpr
    }
  });

  if (Array.isArray(bounds) && bounds.length === 4) {
    const b = [[bounds[0], bounds[1]], [bounds[2], bounds[3]]];
    map.fitBounds(b, { padding: 40, duration: 800 });
  }
}

function updateLegend(stats) {
  const grad = $("#legendGradient");
  const lmin = $("#legendMin");
  const lmean = $("#legendMean");
  const lmax = $("#legendMax");

  if (!stats) {
    grad.style.background = "linear-gradient(90deg, #e5e7eb 0%, #9ca3af 100%)";
    lmin.textContent = "min: —";
    lmean.textContent = "mean: —";
    lmax.textContent = "max: —";
    return;
  }

  grad.style.background = "linear-gradient(90deg, #93c5fd 0%, #fbbf24 50%, #ef4444 100%)";
  // Concatenation
  lmin.textContent = 'min: ' + Number(stats?.min ?? 0).toFixed(1);
  lmean.textContent = 'mean: ' + Number(stats.mean ?? 0).toFixed(1);
  lmax.textContent = 'max: ' + Number(stats.max ?? 0).toFixed(1);
}

async function simulate() {
  try {
    const resp = await fetch("/simulate", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || resp.statusText);
    }
    const data = await resp.json();

    addOrUpdatePoisSimulated(data.geojson, data.stats, data.bounds);
    updateLegend(data.stats);
    updateTypeChart(data.distribution_by_type || []);

    setStatus("#simStatus", "Готово");
  } catch (e) {
    setStatus("#simStatus", "Ошибка симуляции: " + e.message, true);
  }
}




async function runSimulation() {
  if (!selectedGpkg) {
    setStatus("#simStatus", "Выберите файл в списке", true);
    return;
  }
  setStatus("#simStatus", "Симуляция запущена, ожидайте...");

  const payload = {
    gpkg_filename: selectedGpkg,
    start: $("#start").value || "2025-01-01",
    end: $("#end").value || "2025-12-31",
    freq: $("#freq").value || "H",
    base_rate_per_person_per_hour: parseNumber($("#baseRate").value, 0.02),
    cutoff_distance: parseNumber($("#cutoff").value, 7000),
    alpha: parseNumber($("#alpha").value, 1.0),
    beta: parseNumber($("#beta").value, 1.2),
    gamma: parseNumber($("#gamma").value, 1.0),
    d0: parseNumber($("#d0").value, 50),
    distance_bins: parseBins($("#distBins").value),
    poi_type_weights: parseJSONOr($("#poiWeights").value, {"mall":1.5,"park":0.8,"cafe":1.1}),
    tod_multipliers: parseJSONOr($("#tod").value, {"morning":0.9,"afternoon":1.1,"evening":1.0}),
    weather_multipliers: parseJSONOr($("#weather").value, {"sunny":1.2,"rainy":0.3,"windy":0.8,"snowy":0.8}),
    season_multipliers: parseJSONOr($("#season").value, {"spring":1.0,"summer":1.5,"autumn":0.7,"winter":0.5})
  };

    try {
      const resp = await fetch("/simulate", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
      });

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || resp.statusText);
      }

      const data = await resp.json();

      // 1. Добавляем проверку на существование данных перед обновлением
      if (data.geojson && data.stats && data.bounds) {
        addOrUpdatePoisSimulated(data.geojson, data.stats, data.bounds);
      }

      if (data.stats) {
        updateLegend(data.stats);
      }

      // 2. Обеспечиваем, что chartData всегда будет массивом и ограничиваем размер если нужно
      const chartData = Array.isArray(data.distribution_by_type) ? data.distribution_by_type : [];
      updateTypeChart(chartData);

      // 3. Добавляем обработку размеров графика
      setTimeout(() => {
        const chartContainer = document.querySelector('.chart-container');
        if (chartContainer) {
          chartContainer.style.overflow = '20%';
          chartContainer.style.maxWidth = '20%';
        }
      }, 100);

      setStatus("#simStatus", "Готово");
    } catch (e) {
      setStatus("#simStatus", "Ошибка симуляции: " + (e.message || "Неизвестная ошибка"), true);
    }
}

document.addEventListener("DOMContentLoaded", async () => {
  initMap();

  $("#btnUpload").addEventListener("click", uploadGpkg);
  $("#btnRefresh").addEventListener("click", () => loadGpkgList());
  $("#btnLoadPoi").addEventListener("click", loadPois);
  $("#btnRun").addEventListener("click", runSimulation);
  $("#gpkgSelect").addEventListener("change", (e) => {
    selectedGpkg = e.target.value || null;
    enableActions(!!selectedGpkg);
  });

  // Загружаем список при старте
  await loadGpkgList();
});
