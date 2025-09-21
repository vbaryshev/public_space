let map;
let poisSourceId = "pois";
let poisLayerId = "pois-layer";

function initMap() {
  map = new maplibregl.Map({
    container: "map",
    style: "https://raw.githubusercontent.com/vbaryshev/basemap/refs/heads/main/voyager-nolabels.json",
    center: [37.62, 55.75],
    zoom: 5
  });

  map.addControl(new maplibregl.NavigationControl(), "top-right");

  map.on("load", () => {
    // Источник для POI
    map.addSource(poisSourceId, {
      type: "geojson",
      data: {
        type: "FeatureCollection",
        features: []
      }
    });

    // Слой кругов с радиусом от visits
    map.addLayer({
      id: poisLayerId,
      source: poisSourceId,
      type: "circle",
      paint: {
        "circle-color": [
          "interpolate", ["linear"], ["coalesce", ["get", "visits"], 0],
          0, "#2c7fb8",
          10, "#41b6c4",
          50, "#a5d439",
          100, "#d6d62d",
          200, "#e3b64d",
          300, "#d4a1da",
          400, "#d4a1da",
          600, "#75000a"
        ],
        "circle-opacity": 0.8,
        "circle-stroke-color": "#333",
        "circle-stroke-width": 1.0,
        "circle-radius": [
          "interpolate", ["linear"], ["coalesce", ["get", "visits"], 0],
          0, 1,
          10, 2,
          50, 4,
          100, 6,
          200, 8,
          300, 10,
          400, 12,
          600, 16,
        ]
      }
    });

    map.on("click", poisLayerId, (e) => {
      const f = e.features && e.features[0];
      if (!f) return;
      const props = f.properties || {};
      const coords = e.lngLat;
      const poiType = props.poi_type || "poi";
      const visits = Number(props.visits || 0).toFixed(1);

      new maplibregl.Popup({ closeButton: true })
        .setLngLat([coords.lng, coords.lat])
        .setHTML('<div><b>Тип:</b> ' + poiType + '<br/><b>Посетители/день:</b> ' + visits + '</div>')
        .addTo(map);
    });

    map.on("mouseenter", poisLayerId, () => map.getCanvas().style.cursor = "pointer");
    map.on("mouseleave", poisLayerId, () => map.getCanvas().style.cursor = "");
  });
}

function fitToBounds(bounds) {
  if (!bounds || bounds.length !== 4) return;
  map.fitBounds([[bounds[0], bounds[1]], [bounds[2], bounds[3]]], { padding: 40, duration: 500 });
}

function setPOIs(data) {
  const src = map.getSource(poisSourceId);
  if (src) {
    src.setData(data);
  }
}

async function apiUpload(file) {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch("/api/upload", { method: "POST", body: form });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

async function apiSimulate(payload) {
  const res = await fetch("/api/simulate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function parseJsonTextarea(id, fallback) {
  try {
    const val = document.getElementById(id).value.trim();
    if (!val) return fallback;
    return JSON.parse(val);
  } catch (e) {
    console.warn("JSON parse failed for", id, e);
    return fallback;
  }
}

function collectParams() {
  const start = document.getElementById("start").value.replace("T", " ");
  const end = document.getElementById("end").value.replace("T", " ");
  const freq = document.getElementById("freq").value || "H";
  const baseRate = parseFloat(document.getElementById("baseRate").value) || 0.02;

  const tod = parseJsonTextarea("todJson", { "morning": 0.9, "afternoon": 1.1, "evening": 1.0 });
  const weather = parseJsonTextarea("weatherJson", { "sunny": 1.2, "rainy": 0.3, "windy": 0.8, "snowy": 0.8 });
  const season = parseJsonTextarea("seasonJson", { "spring": 1.0, "summer": 1.5, "autumn": 0.7, "winter": 0.5 });

  const gravity = {
    alpha: parseFloat(document.getElementById("alpha").value) || 1.0,
    beta: parseFloat(document.getElementById("beta").value) || 1.2,
    gamma: parseFloat(document.getElementById("gamma").value) || 1.0,
    d0: parseFloat(document.getElementById("d0").value) || 50.0,
    max_distance: parseFloat(document.getElementById("maxDistance").value) || 7000.0
  };

  const cutoff = parseFloat(document.getElementById("cutoffDistance").value) || 7000.0;

  const poiWeightsRaw = document.getElementById("poiWeightsJson").value.trim();
  let poi_type_weights = undefined;
  if (poiWeightsRaw) {
    try { poi_type_weights = JSON.parse(poiWeightsRaw); } catch (e) { console.warn("poiWeights JSON error", e); }
  }

  return {
    start,
    end,
    freq,
    base_rate_per_person_per_hour: baseRate,
    tod_multipliers: tod,
    weather_multipliers: weather,
    season_multipliers: season,
    gravity,
    cutoff_distance: cutoff,
    poi_type_weights
  };
}

function bindUI() {
  const uploadBtn = document.getElementById("btnUpload");
  const uploadStatus = document.getElementById("uploadStatus");
  const fileInput = document.getElementById("fileInput");

  uploadBtn.addEventListener("click", async () => {
    try {
      const f = fileInput.files && fileInput.files[0];
      if (!f) {
        uploadStatus.textContent = "Выберите .gpkg файл.";
        return;
      }
      uploadStatus.textContent = "Загрузка и подготовка данных...";
      const res = await apiUpload(f);
      uploadStatus.textContent = "OK. POIs: " + res.counts.pois + ", Buildings: " + res.counts.buildings;
      if (res.bounds) fitToBounds(res.bounds);
    } catch (e) {
      console.error(e);
      uploadStatus.textContent = "Ошибка: " + e;
    }
  });

  const simulateBtn = document.getElementById("btnSimulate");
  const simulateStatus = document.getElementById("simulateStatus");

  simulateBtn.addEventListener("click", async () => {
    try {
      simulateStatus.textContent = "Симуляция...";
      const payload = collectParams();
      const res = await apiSimulate(payload);
      setPOIs(res.geojson);
      if (res.bounds) fitToBounds(res.bounds);
      simulateStatus.textContent = "Готово. max_visits=" + res.max_visits.toFixed(2);
    } catch (e) {
      console.error(e);
      simulateStatus.textContent = "Ошибка: " + e;
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  initMap();
  bindUI();
});
