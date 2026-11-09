"""The replay map: a timeline with a helm thumb, and the vessels animated in the browser.

A Streamlit v2 component (spec section 8, point 11, step 2): dragging the helm moves the
vessels on a deck.gl map without asking Python; on release the component's state `t` reruns
the page once. The map matches the Live one (app_charts.port_map): same basemap, berth rings,
icons, colors and tooltip.
"""

import streamlit as st

import config
from app_charts import berth_areas
from app_theme import LABELS, colors
from classify import COMMERCIAL, ship_category
from icons import ICONS

# Every value ship_category can return, for the commercial-only toggle switched off.
CATEGORIES = sorted({ship_category(code) for code in range(0, 101)})

# Exact versions: a CDN alias like @9 could change under a published page.
# deck.gl must be the version Streamlit bundles for st.pydeck_chart (9.4.1 in Streamlit 1.67):
# deck.gl refuses a second, different version on the same page, and the Live map loads Streamlit's.
# Known limit: a Streamlit upgrade that changes it breaks the replay map until this is updated.
DECK_VERSION = "9.4.1"
DECK = f"https://cdn.jsdelivr.net/npm/deck.gl@{DECK_VERSION}/dist.min.js"
MAPLIBRE = "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl"
# The basemaps Streamlit's own map uses, light or dark with the theme.
BASEMAPS = {"light": "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
            "dark": "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json"}


def rgb(hex_color):
    return [int(hex_color[i:i + 2], 16) for i in (1, 3, 5)]


def map_style():
    """Theme-dependent drawing data: changes only when the viewer switches theme."""
    theme = "dark" if st.context.theme.type == "dark" else "light"
    c = colors()
    return {
        "basemap": BASEMAPS[theme], "states": {s: rgb(c[s]) for s in LABELS}, "labels": LABELS,
        "surface": rgb(c["surface"]), "ink": [255, 255, 255] if theme == "dark" else [11, 11, 11],
        "icons": {k: {key: v[key] for key in ("url", "width", "height", "mask")} for k, v in ICONS.items()},
        "areas": berth_areas(),
    }


HTML = """
<div class="hw-replay">
  <div class="hw-map"></div>
  <div class="hw-timeline">
    <div class="hw-clock">-</div>
    <div class="hw-track" role="slider" tabindex="0" aria-label="Replay time">
      <div class="hw-rail"></div><div class="hw-done"></div>
      <svg class="hw-thumb" viewBox="-12 -12 24 24" width="26" height="26" aria-hidden="true">
        <g class="hw-wheel">
          <circle r="6.2" class="hw-rim"/>
          <circle r="2" class="hw-hub"/>
          <g class="hw-spokes">
            <line y1="-2" y2="-10.5"/><line y1="2" y2="10.5"/><line x1="-2" x2="-10.5"/><line x1="2" x2="10.5"/>
            <line x1="-1.4" y1="-1.4" x2="-7.4" y2="-7.4"/><line x1="1.4" y1="1.4" x2="7.4" y2="7.4"/>
            <line x1="1.4" y1="-1.4" x2="7.4" y2="-7.4"/><line x1="-1.4" y1="1.4" x2="-7.4" y2="7.4"/>
          </g>
        </g>
      </svg>
    </div>
  </div>
</div>
"""

CSS = """
.hw-replay { font-family: var(--st-font, sans-serif); color: var(--st-text-color); }
.hw-map { position: relative; height: 560px; border-radius: 0.5rem; overflow: hidden; }
.hw-timeline { display: grid; grid-template-columns: auto 1fr; gap: 16px; align-items: center; margin-top: 8px; }
.hw-clock { font-weight: 600; font-variant-numeric: tabular-nums; min-width: 8.5em; }
.hw-track { position: relative; height: 32px; margin: 0 13px; touch-action: none; cursor: pointer; outline: none; }
.hw-rail, .hw-done { position: absolute; top: 50%; height: 4px; margin-top: -2px; border-radius: 2px; left: 0; }
.hw-rail { right: 0; background: var(--st-secondary-background-color); }
.hw-done { background: var(--st-primary-color); }
.hw-thumb { position: absolute; top: 50%; margin: -13px 0 0 -13px; color: var(--st-primary-color); }
.hw-track:focus-visible .hw-thumb { outline: 2px solid var(--st-primary-color); outline-offset: 2px; border-radius: 50%; }
.hw-track.dragging { cursor: grabbing; }
.hw-rim { fill: var(--st-background-color); stroke: currentColor; stroke-width: 2.2; }
.hw-hub { fill: currentColor; }
.hw-spokes line { stroke: currentColor; stroke-width: 2; stroke-linecap: round; }
"""

JS = """
const DECK = "__DECK__", MAPLIBRE = "__MAPLIBRE__";

function script(src) {
  return new Promise((ok, fail) => {
    const s = document.createElement("script");
    s.src = src; s.onload = ok; s.onerror = fail;
    document.head.appendChild(s);
  });
}
// Loaded once per page, shared by every mount.
window.hwReplayLibs = window.hwReplayLibs ||
  script(MAPLIBRE + ".js").then(() => script(DECK));

export default async function (component) {
  const { data, parentElement: root, setStateValue } = component;
  // The stylesheet must sit inside the component's shadow root to reach the map.
  if (!root.querySelector("link[data-maplibre]")) {
    const css = document.createElement("link");
    css.rel = "stylesheet"; css.href = MAPLIBRE + ".css"; css.dataset.maplibre = "";
    root.appendChild(css);
  }
  await window.hwReplayLibs;
  if (!window.deck || !window.deck.MapboxOverlay) {
    throw new Error(`replay map: deck.gl ${window.deck && window.deck.VERSION} on the page is not the full ` +
      "build; is DECK_VERSION the version Streamlit bundles?");
  }
  const { MapboxOverlay, ScatterplotLayer, TextLayer, IconLayer, TripsLayer } = window.deck;
  // The instance survives reruns: new data (filters, theme) updates it, the time stays.
  const hw = root.hwReplay || (root.hwReplay = { t: data.end });
  hw.data = data;
  hw.t = Math.min(data.end, Math.max(data.start, hw.t));
  const style = data.style, stale = data.stale, step = data.step;
  const shownStates = new Set(data.shown.states), shownCategories = new Set(data.shown.categories);
  const category = m => (data.static[m] || {}).category || "unknown";
  const fmt = new Intl.DateTimeFormat("en-GB", { timeZone: data.timezone, weekday: "short", day: "2-digit",
    month: "short", hour: "2-digit", minute: "2-digit" });

  // Trails: one piece per state stretch, broken where a vessel went unheard for over STALE,
  // like vessel_tracks; colored by the state of that stretch.
  const pieces = [];
  for (const v of data.vessels) {
    let start = 0;
    for (let i = 1; i <= v.times.length; i++) {
      const gap = i < v.times.length && v.times[i] - v.times[i - 1] > stale;
      const end = i === v.times.length || gap || v.states[i] !== v.states[start];
      if (!end) continue;
      const last = gap || i === v.times.length ? i : i + 1;  // a state change shares its first fix
      if (last - start > 1) pieces.push({ mmsi: v.mmsi, state: v.states[start],
        path: v.positions.slice(start, last), timestamps: v.times.slice(start, last) });
      start = i;
    }
  }

  // The vessels at t: states_at's rules, with positions interpolated between fixes at most
  // STALE apart so they move smoothly.
  function at(v, t) {
    const ts = v.times, rel = t - data.t0;
    if (rel < ts[0]) return null;
    let lo = 0, hi = ts.length - 1;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (ts[mid] <= rel) lo = mid; else hi = mid - 1; }
    const state = v.states[lo], sog = v.sog[lo];
    if (lo === ts.length - 1 || ts[lo + 1] - ts[lo] > stale) {
      return rel - ts[lo] <= stale ? { mmsi: v.mmsi, position: v.positions[lo], state, sog } : null;
    }
    const k = (rel - ts[lo]) / (ts[lo + 1] - ts[lo]), a = v.positions[lo], b = v.positions[lo + 1];
    return { mmsi: v.mmsi, position: [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k], state, sog };
  }

  let shownKey = "", shownPieces = [];
  function layers() {
    const t = hw.t;
    const vessels = data.vessels.map(v => at(v, t))
      .filter(v => v && shownStates.has(v.state) && shownCategories.has(category(v.mmsi)));
    // Rebuild the trail data only when the set of vessels on the map changes, not every frame.
    const key = vessels.map(v => v.mmsi + v.state).join();
    if (key !== shownKey) {
      const on = new Map(vessels.map(v => [v.mmsi, v.state]));
      shownPieces = data.shown.tracks ? pieces.filter(p => on.has(p.mmsi)) : [];
      shownKey = key;
    }
    const ink = style.ink;
    return [
      new ScatterplotLayer({ id: "berth-areas", data: style.areas, getPosition: d => d.position, getRadius: 220,
        filled: true, getFillColor: [...ink, 30], stroked: true, getLineColor: ink, lineWidthMinPixels: 1.5,
        pickable: true }),
      new TextLayer({ id: "berth-labels", data: style.areas.filter(a => a.label), getPosition: d => d.position,
        getText: d => d.name, getSize: 13, getColor: ink, getPixelOffset: [0, -20] }),
      new TripsLayer({ id: "vessel-tracks", data: shownPieces, getPath: d => d.path, getTimestamps: d => d.timestamps,
        getColor: d => style.states[d.state], widthMinPixels: 2, opacity: 0.7,
        currentTime: t - data.t0, trailLength: data.track }),  // fading: without it trailLength is ignored
      new ScatterplotLayer({ id: "vessel-halos", data: vessels, getPosition: d => d.position,
        getFillColor: [...style.surface, 200], getRadius: 11, radiusUnits: "pixels" }),
      new IconLayer({ id: "vessels", data: vessels, getPosition: d => d.position,
        getIcon: d => style.icons[category(d.mmsi)] || style.icons.other,
        getColor: d => style.states[d.state], getSize: 20, pickable: true }),
    ];
  }

  function tooltip({ object, layer }) {
    if (!object) return null;
    const known = (v, unit) => v === null || v === undefined ? "?" : `${v}${unit}`;
    let title, line1, line2;
    if (layer.id === "vessels") {
      const s = data.static[object.mmsi] || {};
      title = s.name || `MMSI ${object.mmsi}`;
      line1 = `${s.category || "unknown"}, ${known(s.length, " m")}`;
      line2 = `${style.labels[object.state]}, ${known(object.sog, " kn")}`;
    } else {
      ({ title, line1, line2 } = object);
    }
    const div = document.createElement("div");
    for (const [i, text] of [title, line1, line2].entries()) {
      const line = document.createElement(i ? "div" : "b"); line.textContent = text; div.appendChild(line);
    }
    return { html: div.innerHTML };
  }

  // The map: created once, then only its layers change.
  if (!hw.map) {
    hw.map = new maplibregl.Map({ container: root.querySelector(".hw-map"), style: style.basemap,
      center: [8.85, 44.405], zoom: 11.3, attributionControl: { compact: true } });
    hw.overlay = new MapboxOverlay({ layers: [], getTooltip: d => hw.tooltip(d),
      onClick: d => { if (d.layer && d.layer.id === "vessels") setStateValue("picked", d.object.mmsi); } });
    hw.map.addControl(hw.overlay);
    hw.map.addControl(new maplibregl.NavigationControl({ showCompass: false }));
    hw.basemap = style.basemap;
  } else if (hw.basemap !== style.basemap) {
    hw.map.setStyle(style.basemap); hw.basemap = style.basemap;
  }
  hw.tooltip = tooltip;

  // Timeline: the thumb is a helm turning with the time, one turn every REPLAY_TURN_HOURS, so
  // clockwise going forward and anticlockwise going back.
  const track = root.querySelector(".hw-track"), thumb = root.querySelector(".hw-thumb");
  const wheel = root.querySelector(".hw-wheel"), done = root.querySelector(".hw-done");
  const clock = root.querySelector(".hw-clock");
  function draw() {
    const k = data.end > data.start ? (hw.t - data.start) / (data.end - data.start) : 1;
    thumb.style.left = done.style.width = `${k * 100}%`;
    wheel.setAttribute("transform", `rotate(${((hw.t - data.start) / data.turn) * 360})`);
    clock.textContent = fmt.format(new Date(hw.t * 1000));
    track.setAttribute("aria-valuetext", clock.textContent);
    hw.overlay.setProps({ layers: layers() });
  }
  hw.draw = draw;
  // The rest of the page follows once, with t on the REPLAY_STEP_MINUTES grid.
  const commit = () => {
    hw.t = Math.min(data.end, data.start + Math.round((hw.t - data.start) / step) * step);
    hw.draw(); setStateValue("t", hw.t);
  };
  if (!hw.listening) {
    hw.listening = true;
    let dragging = false, keyTimer = 0;
    const seek = e => {
      const r = track.getBoundingClientRect(), d = hw.data;
      hw.t = d.start + Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)) * (d.end - d.start);
      hw.draw();
    };
    track.addEventListener("pointerdown", e => { dragging = true; track.setPointerCapture(e.pointerId);
      track.classList.add("dragging"); seek(e); });
    track.addEventListener("pointermove", e => { if (dragging) seek(e); });
    const release = () => { if (!dragging) return; dragging = false; track.classList.remove("dragging"); hw.commit(); };
    track.addEventListener("pointerup", release);
    track.addEventListener("pointercancel", release);
    track.addEventListener("keydown", e => {
      const d = hw.data, move = { ArrowRight: d.step, ArrowLeft: -d.step, PageUp: 3600, PageDown: -3600 }[e.key];
      if (!move) return;
      e.preventDefault(); hw.t = Math.min(d.end, Math.max(d.start, hw.t + move)); hw.draw();
      clearTimeout(keyTimer); keyTimer = setTimeout(() => hw.commit(), 400);
    });
  }
  hw.commit = commit;
  draw();
}
""".replace("__DECK__", DECK).replace("__MAPLIBRE__", MAPLIBRE)

_component = st.components.v2.component("harborwatch_replay_map", html=HTML, css=CSS, js=JS)


def replay_map(tracks, static, shown_states, commercial_only, show_tracks):
    """Mount the replay map; its state (t, picked) is read from session_state["replay-map"]."""
    data = {
        **tracks,
        "shown": {"states": shown_states, "categories": sorted(COMMERCIAL) if commercial_only else CATEGORIES,
                  "tracks": show_tracks},
        "static": static, "style": map_style(), "timezone": config.TIMEZONE,
        "stale": config.STALE_MINUTES * 60, "step": config.REPLAY_STEP_MINUTES * 60,
        "turn": config.REPLAY_TURN_HOURS * 3600, "track": config.TRACK_HOURS * 3600,
    }
    _component(data=data, key="replay-map", on_t_change=lambda: None, on_picked_change=lambda: None)
