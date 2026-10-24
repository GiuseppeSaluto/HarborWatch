"""Dashboard look: state labels and colors, colors per theme, page CSS."""

import streamlit as st

LABELS = {"at_berth": "At berth", "anchored": "At anchor", "underway": "Underway"}
ICONS = {"at_berth": ":material/directions_boat:", "anchored": ":material/anchor:", "underway": ":material/sailing:"}
# Categorical slots 1-3 of the dataviz skill's validated reference palette, stepped per theme.
# Same color for a state everywhere: map dots and berth-plan bars.
PALETTE = {"light": {"at_berth": "#2a78d6", "anchored": "#eb6834", "underway": "#1baf7a", "surface": "#ffffff"},
           "dark": {"at_berth": "#3987e5", "anchored": "#d95926", "underway": "#199e70", "surface": "#0e1117"}}


# Chart-paper grid behind the page header, after aisstream.io's home page: 1 px lines every
# 32 px and a faint glow, fading out downwards so it never sits behind charts.
GRID = {"light": {"line": "rgba(49, 51, 63, 0.08)", "glow": "rgba(42, 120, 214, 0.10)"},
        "dark": {"line": "rgba(250, 250, 250, 0.06)", "glow": "rgba(57, 135, 229, 0.16)"}}


def header_grid_css():
    g = GRID["dark" if st.context.theme.type == "dark" else "light"]
    return f"""<style>
[data-testid=stMain]::before {{
    content: ""; position: absolute; inset: 0 0 auto 0; height: 340px; pointer-events: none;
    background-image: linear-gradient({g["line"]} 1px, transparent 1px),
        linear-gradient(90deg, {g["line"]} 1px, transparent 1px),
        radial-gradient(circle at 78% 24%, {g["glow"]}, transparent 30%);
    background-size: 32px 32px, 32px 32px, auto;
    mask-image: linear-gradient(to bottom, black 60%, transparent);
}}
</style>"""


# Streamlit's "Running..." icon cycles people doing sports, and st.spinner shows a turning ring:
# a ship bobbing on moving waves instead, for both.
# Two mask layers (hull, wave tile) over the text color, like the original faded icon.
# < and > escaped in the SVGs: st.html drops a <style> whose text contains a tag.
RUNNING_ICON_CSS = """<style>
[data-testid=stStatusWidgetRunningIcon] > * {display: none;}
[data-testid=stSpinnerIcon] {width: 1.6rem; height: 1.6rem; border: 0; border-radius: 0;}
:is([data-testid=stStatusWidgetRunningIcon], [data-testid=stSpinnerIcon]) {
    background-color: currentColor; opacity: 0.6;
    mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 18'%3E%3Cpath d='M2 11h20l-3 5H5zM7 7h9v4H7zM13 3h2v4h-2z'/%3E%3C/svg%3E") 50% 0% / 100% 72% no-repeat,
        url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 8 4'%3E%3Cpath d='M0 2.5Q2 .5 4 2.5T8 2.5' fill='none' stroke='black' stroke-width='1.2'/%3E%3C/svg%3E") 0 100% / 8px 4px repeat-x;
    animation: hw-sail 1.2s linear infinite;
}
@keyframes hw-sail {
    50% {mask-position: 50% 30%, 4px 100%;}
    to {mask-position: 50% 0%, 8px 100%;}
}
@media (prefers-reduced-motion: reduce) {:is([data-testid=stStatusWidgetRunningIcon], [data-testid=stSpinnerIcon]) {animation: none;}}
</style>"""


def colors():
    return PALETTE["dark" if st.context.theme.type == "dark" else "light"]


def state_card_css():
    """State colors and visibility controls for the map cards."""
    c = colors()
    css = "<style>.st-key-cards > div {flex: 1 1 170px; min-width: 150px;}"
    for s in LABELS:
        card = f".st-key-state-{s}"
        visible = st.session_state.get(f"show_{s}", True)
        css += (
            f"{card} {{position: relative;}}"
            f"{card} [data-testid=stMetric] {{box-shadow: inset 0 3px 0 {c[s] if visible else 'transparent'};"
            f"opacity: {1 if visible else 0.45}; padding-right: 48px;}}"
            f"{card} [data-testid=stMetricIcon] {{color: {c[s]};}}"
            f".st-key-map-{s} {{position: absolute; inset: 0; z-index: 1;}}"
            f"{card} [data-testid=stButton] {{height: 100%;}}"
            f"{card} button {{height: 100%; border: none; background: transparent;"
            f"color: {c[s] if visible else 'inherit'}; justify-content: flex-end; align-items: flex-start; padding: 16px;}}"
            f"{card} button [data-testid=stIconMaterial] {{position: absolute; right: 16px; top: 16px;}}"
            f"{card} button:hover {{background: {c[s]}14; color: {c[s]};}}"
            f"{card} button p {{position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%);}}"
        )
    return css + "</style>"
