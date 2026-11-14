"""The check of the SVG icon of a provider, which the index hands to every server."""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

# the budget the server sets for the SVG icons of its own providers
MAX_ICON_BYTES = 5 * 1024

_SVG = "http://www.w3.org/2000/svg"
_XLINK = "http://www.w3.org/1999/xlink"
_XML = "http://www.w3.org/XML/1998/namespace"
# drawing elements only: nothing that runs code, links or animates; an image only embeds a raster
_ELEMENTS = frozenset(
    {
        "circle",
        "clipPath",
        "defs",
        "desc",
        "ellipse",
        "feBlend",
        "feColorMatrix",
        "feComponentTransfer",
        "feComposite",
        "feConvolveMatrix",
        "feDiffuseLighting",
        "feDisplacementMap",
        "feDistantLight",
        "feDropShadow",
        "feFlood",
        "feFuncA",
        "feFuncB",
        "feFuncG",
        "feFuncR",
        "feGaussianBlur",
        "feMerge",
        "feMergeNode",
        "feMorphology",
        "feOffset",
        "fePointLight",
        "feSpecularLighting",
        "feSpotLight",
        "feTile",
        "feTurbulence",
        "filter",
        "g",
        "line",
        "linearGradient",
        "marker",
        "mask",
        "path",
        "pattern",
        "polygon",
        "polyline",
        "radialGradient",
        "rect",
        "stop",
        "style",
        "svg",
        "symbol",
        "text",
        "textPath",
        "image",
        "title",
        "tspan",
        "use",
    }
)
_XML_DECLARATION = re.compile(r"\A\s*<\?xml\s[^>]*\?>")
# a document type or entity declaration; comments and CDATA sections are fine
_DECLARATION = re.compile(r"<!(?!--|\[CDATA\[)")
_URL = re.compile(r"url\s*\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE | re.DOTALL)
_URL_START = re.compile(r"url\s*\(", re.IGNORECASE)
_RASTER = re.compile(r"data:image/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/=\s]*")


def check_icon(svg: str) -> None:
    """
    Check that an SVG icon only draws: no scripts, no event handlers, no reference out of the file.

    :param svg: The content of the icon.
    :raises ValueError: The icon is too large, is no valid SVG, or holds something not allowed.
    """
    if (size := len(svg.encode())) > MAX_ICON_BYTES:
        msg = f"it is {size} bytes, at most {MAX_ICON_BYTES} are allowed"
        raise ValueError(msg)
    body = _XML_DECLARATION.sub("", svg, count=1)
    if "<?" in body:
        raise ValueError("it holds a processing instruction")
    if _DECLARATION.search(body):
        raise ValueError("it holds a document type or entity declaration")
    try:
        root = ET.fromstring(svg)  # noqa: S314 - declarations are refused above
    except ET.ParseError as err:
        msg = f"it is not valid XML: {err}"
        raise ValueError(msg) from err
    if root.tag != f"{{{_SVG}}}svg":
        raise ValueError("its root element is not an SVG element")
    for element in root.iter():
        namespace, name = _split(element.tag)
        if namespace != _SVG or name not in _ELEMENTS:
            msg = f"the element {name} is not allowed"
            raise ValueError(msg)
        for key, value in element.attrib.items():
            _check_attribute(name, key, value)
        if name == "style":
            _check_style(element.text or "")


def _check_attribute(element: str, key: str, value: str) -> None:
    """
    Check an attribute of an element of an icon.

    :param element: The name of the element.
    :param key: The name of the attribute, with its namespace in braces.
    :param value: The value of the attribute.
    :raises ValueError: The attribute is not allowed.
    """
    namespace, name = _split(key)
    if namespace not in {"", _XLINK, _XML}:
        msg = f"the attribute {key} of {element} is not allowed"
        raise ValueError(msg)
    if name.lower().startswith("on"):
        msg = f"the event handler {name} of {element} is not allowed"
        raise ValueError(msg)
    if name == "href" and not (
        value.startswith("#") or (element == "image" and _RASTER.fullmatch(value))
    ):
        msg = f"{element} refers to {value!r}, only a reference within the icon is allowed"
        raise ValueError(msg)
    _check_urls(f"the attribute {name} of {element}", value)


def _check_style(css: str) -> None:
    """
    Check the content of a style element of an icon.

    :param css: The style sheet.
    :raises ValueError: The style sheet loads something.
    """
    if "@import" in css.lower():
        raise ValueError("a style element imports a style sheet")
    _check_urls("a style element", css)


def _check_urls(where: str, value: str) -> None:
    """
    Check that every url() in a value refers to an element within the icon.

    :param where: What holds the value, for the message.
    :param value: An attribute value or a style sheet.
    :raises ValueError: A url() refers to something outside the icon.
    """
    targets = [match.group(2) for match in _URL.finditer(value)]
    if len(targets) != len(_URL_START.findall(value)):
        msg = f"{where} holds a url() that is not closed"
        raise ValueError(msg)
    for target in targets:
        if not target.startswith("#"):
            msg = f"{where} refers to {target!r}, only a reference within the icon is allowed"
            raise ValueError(msg)


def _split(name: str) -> tuple[str, str]:
    """Return the namespace and the local part of an element or attribute name."""
    if name.startswith("{"):
        namespace, _, local = name[1:].partition("}")
        return namespace, local
    return "", name
