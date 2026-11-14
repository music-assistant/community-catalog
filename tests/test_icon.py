"""Tests for the check of the SVG icon of a provider."""

from __future__ import annotations

import pytest

from registry.icon import MAX_ICON_BYTES, check_icon
from tests.conftest import ICON

SVG = 'xmlns="http://www.w3.org/2000/svg"'
XLINK = 'xmlns:xlink="http://www.w3.org/1999/xlink"'


@pytest.mark.parametrize(
    "icon",
    [
        ICON,
        f'<?xml version="1.0" encoding="UTF-8"?>\n{ICON}',
        f"<svg {SVG}><!-- a comment --><title>Sample</title></svg>",
        (
            f'<svg {SVG}><defs><linearGradient id="g"><stop offset="0"/></linearGradient></defs>'
            '<rect fill="url(#g)" width="1" height="1"/></svg>'
        ),
        f'<svg {SVG} {XLINK}><defs><path id="p" d="M0 0"/></defs><use xlink:href="#p"/></svg>',
        f"<svg {SVG}><style><![CDATA[.a{{fill:url('#g')}}]]></style></svg>",
        f'<svg {SVG}><image href="data:image/png;base64,iVBORw0KGgo=" width="1"/></svg>',
        f'<svg {SVG} xml:space="preserve"><text>Sample</text></svg>',
    ],
    ids=[
        "plain",
        "declaration",
        "comment",
        "gradient",
        "use",
        "style",
        "raster",
        "xml-space",
    ],
)
def test_icon_that_only_draws(icon: str) -> None:
    """An icon that only draws passes."""
    check_icon(icon)


@pytest.mark.parametrize(
    ("icon", "message"),
    [
        (f"<svg {SVG}><script>alert(1)</script></svg>", "element script"),
        (f"<svg {SVG}><foreignObject/></svg>", "element foreignObject"),
        (f'<svg {SVG}><a href="#x"><path d="M0 0"/></a></svg>', "element a"),
        (f'<svg {SVG}><set attributeName="href" to="x"/></svg>', "element set"),
        (
            f'<svg {SVG}><script xmlns="http://www.w3.org/1999/xhtml">x</script></svg>',
            "element script",
        ),
        (f'<svg {SVG} onload="alert(1)"/>', "event handler onload"),
        (f'<svg {SVG}><path ONCLICK="x" d="M0 0"/></svg>', "event handler ONCLICK"),
        (f'<svg {SVG}><use href="other.svg#p"/></svg>', "refers to 'other.svg#p'"),
        (f'<svg {SVG} {XLINK}><use xlink:href="https://x/p.svg"/></svg>', "refers to"),
        (f'<svg {SVG}><image href="https://x/a.png"/></svg>', "refers to"),
        (f'<svg {SVG}><image href="data:image/svg+xml;base64,PHN2Zz4="/></svg>', "refers to"),
        (f'<svg {SVG}><rect fill="url(https://x/g.svg#g)"/></svg>', "refers to"),
        (f"<svg {SVG}><rect style=\"fill: URL( '//x/g' )\"/></svg>", "refers to '//x/g'"),
        (f'<svg {SVG}><rect fill="url(#g"/></svg>', "not closed"),
        (f"<svg {SVG}><style>@import 'https://x/a.css';</style></svg>", "imports a style"),
        (f"<svg {SVG}><style>.a{{fill:url(https://x/g)}}</style></svg>", "refers to"),
        (
            f'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "x.dtd"><svg {SVG}/>',
            "document type",
        ),
        (f'<!DOCTYPE svg [<!ENTITY a "b">]><svg {SVG}>&a;</svg>', "document type"),
        (f'<?xml-stylesheet href="https://x/a.css"?><svg {SVG}/>', "processing instruction"),
        (
            f'<svg {SVG} xmlns:i="http://www.inkscape.org/namespaces/inkscape" i:version="1"/>',
            "attribute {http://www.inkscape.org/namespaces/inkscape}version",
        ),
        ('<svg xmlns="http://www.w3.org/1999/xhtml"/>', "root element is not an SVG"),
        ("<svg>", "not valid XML"),
        (f"<svg {SVG}>{' ' * MAX_ICON_BYTES}</svg>", "at most 5120 are allowed"),
    ],
    ids=lambda value: value if len(value) < 40 else None,
)
def test_icon_refused(icon: str, message: str) -> None:
    """An icon that runs code, refers outside itself, declares an entity or is too large fails."""
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace("{", r"\{")):
        check_icon(icon)
