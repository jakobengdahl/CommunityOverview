"""Interactive graph widget delivered as an MCP UI resource.

One mechanism serves both ChatGPT and Claude: the MCP Apps extension
(``io.modelcontextprotocol/ui``, spec 2026-01-26). A tool names a ``ui://``
resource in its ``_meta``; the host reads that resource (HTML with the
``text/html;profile=mcp-app`` MIME type), renders it in a sandboxed iframe and
talks to it over postMessage JSON-RPC. ChatGPT implements the same standard;
the only adapter needed on the server is the ``openai/outputTemplate`` alias on
the tool, which older ChatGPT builds use to find the template.

The widget bundle (``frontend/widget/dist``) is inlined into the HTML so the
resource is self-contained: the host sandbox needs no ``resourceDomains`` and
the server's public URL never has to be known here.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

WIDGET_RESOURCE_URI = "ui://community-graph/graph-widget.html"
MCP_APP_MIME_TYPE = "text/html;profile=mcp-app"

_WIDGET_SCRIPT = "widget.iife.js"
_WIDGET_STYLE = "style.css"

# Height the widget asks the host for; the canvas fills it.
WIDGET_HEIGHT_PX = 520


def default_widget_dist_path() -> Path:
    env_path = os.getenv("WIDGET_STATIC_PATH")
    if env_path:
        return Path(env_path)
    return Path(__file__).resolve().parents[2] / "frontend" / "widget" / "dist"


def widget_tool_meta() -> Dict[str, Any]:
    """``_meta`` linking a tool to the widget resource.

    ``ui.resourceUri`` is the MCP Apps key. The flat ``ui/resourceUri`` is the
    spec's deprecated form, still emitted by the reference SDK for hosts that
    predate the nested key. ``openai/outputTemplate`` is ChatGPT's alias.
    """
    return {
        "ui": {"resourceUri": WIDGET_RESOURCE_URI},
        "ui/resourceUri": WIDGET_RESOURCE_URI,
        "openai/outputTemplate": WIDGET_RESOURCE_URI,
    }


def widget_resource_meta() -> Dict[str, Any]:
    # Everything is inlined and the widget reaches the server only through the
    # host's tools/call proxy, so the sandbox needs no network origins.
    return {
        "ui": {
            "csp": {"connectDomains": [], "resourceDomains": []},
            "prefersBorder": True,
        }
    }


def _inline_script(js: str) -> str:
    # A literal "</script" inside the bundle would close the tag early;
    # "<\/script" is the same string to the JS parser.
    return js.replace("</script", "<\\/script")


def build_widget_html(dist_path: Optional[Path] = None) -> str:
    """Return the widget document, or a placeholder when the bundle is not built."""
    dist = Path(dist_path) if dist_path else default_widget_dist_path()
    script_file = dist / _WIDGET_SCRIPT
    style_file = dist / _WIDGET_STYLE
    if not script_file.is_file():
        logger.warning("Graph widget bundle not found at %s", script_file)
        return (
            "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
            "<p>The graph widget is not built on this server "
            "(run <code>npm run build:widget</code>).</p></body></html>"
        )
    js = script_file.read_text(encoding="utf-8")
    css = style_file.read_text(encoding="utf-8") if style_file.is_file() else ""
    # Same early-close guard for the stylesheet; an escaped "/" is valid CSS.
    css = css.replace("</style", "<\\/style")
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Graph Widget</title>"
        f"<style>html,body{{margin:0;padding:0}}"
        f"#widget-root{{width:100%;height:{WIDGET_HEIGHT_PX}px}}</style>"
        f"<style>{css}</style>"
        "</head><body>"
        '<div id="widget-root"></div>'
        f"<script>{_inline_script(js)}</script>"
        "</body></html>"
    )
