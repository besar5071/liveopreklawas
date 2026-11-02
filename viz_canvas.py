"""Streamlit component for a draggable VIZ composition preview."""
from pathlib import Path

import streamlit.components.v1 as components


_COMPONENT_DIR = Path(__file__).parent / "viz_canvas_frontend"
_viz_canvas = components.declare_component("liveop_viz_canvas", path=str(_COMPONENT_DIR))


def viz_canvas(*, background_data: str, aspect: str, title: str, subtitle: str,
               visualizer: str, color_a: str, color_b: str, positions: dict,
               text_color: str, font_size: int, font: str, text_effect: str,
               text_opacity: float, visualizer_size: int, key: str) -> dict:
    value = _viz_canvas(
        background_data=background_data or "", aspect=aspect,
        title=title or "", subtitle=subtitle or "", visualizer=visualizer,
        color_a=color_a, color_b=color_b, positions=positions,
        text_color=text_color, font_size=font_size, font=font, text_effect=text_effect,
        text_opacity=text_opacity, visualizer_size=visualizer_size,
        default=positions, key=key,
    )
    return value if isinstance(value, dict) else positions
