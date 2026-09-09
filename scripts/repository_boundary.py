"""Narrow semantic scan projection for the reviewed Excalidraw SVG fonts.

This is NOT a general data-URL exclusion or a secret-scanning filter. CI must
continue scanning the entire original file with every secret pattern. Only the
base64 characters of a reviewed font inside a real SVG style/font-face rule may
be omitted from the business-word/developer-path projection.

Updating fonts: review the font's provenance/license, decode every table with a
font validator, inspect metadata and confirm there is no private-data block;
then explicitly review the new SHA256 and add a regression fixture. Never learn
hashes automatically from the same untrusted file being scanned. These 25 fonts
were decoded with FontTools 4.64.0 (all tables, cmap and glyphs checked) during
review. FontTools is not a CI or product dependency. Header reference:
https://www.w3.org/TR/WOFF2/#WOFFHeader
"""
from __future__ import annotations

import base64
import binascii
from hashlib import sha256
from pathlib import PurePosixPath
import re
import struct
from xml.parsers import expat


REVIEWED_FONT_SHA256 = frozenset({
    "08cf5478cbfebc5178cf6802f238ccaced6a65e95739d7691c8fde7e61a8bf36",
    "0d0f60c93a33bcc111c558c8791fb4997de79fba3f476d8541a07c72f9bfe696",
    "0dd5e346b14d30c9115544d622c6043c4185115b8937fb3882c405dd763f9614",
    "0ec7a03f562b0353b3c6fcd1dd3842374f54cde00a9edcb88344c4be81ab6f87",
    "0f544546179fd6aadb8ff454864b2623b64fdadcb9bcb9ed29e94040308f7b08",
    "12a9a348a4dd0daf0828962e3216c3d2355c112314110d456fa9c87f07df0f6d",
    "12f4b0788b338720f0ddfd0394adee7b08a16e4ab19981ad421d825ba2073f6f",
    "26a251946c20c6821824e41ae7c1e6113fd2380f1544014d6dd4fa1e94db0925",
    "4799cd5027a2219bbc756228b4bf3366011b54e14cd6ecceb80c0f3aafac7b25",
    "5ad25ea87645f2e0fdb8030412aca4a895da96616cbcce604188c1eef1c833b4",
    "5d39bb09f5043de20b5e864c536fec8b280a49d3308d64ee849b73083656b165",
    "5f3fc573d373600e2ff2370a385a9fe736002ce858e2388d3c1f43743524a9fe",
    "653a602f096ae1da9b9008717e98052bceda6e86e66e6eb31ef312e675126127",
    "69e97d516f9799a29a63188ff8128558a9c69382c43619f69a769e22b311dec4",
    "782e7513fb9a534aba117e8b50dbf207b64a7f4795d1d161a90ec6fd6bf726ad",
    "881e95a82ebd16e58f9f830e0d9812234d42377a59975ee2c3ac4d18891c4143",
    "8b75190dbfa3d595f024ff2f65476c8c7cd47a318501c8ab1254c86d4b9ddd68",
    "8ce93b0af529543b377f2c2ad455136c7e60d56e30321ecba34be9bf78d90be2",
    "adf798a5e1b111470a52c266a6a777cc765b0dc639bb102f240078055720a2ef",
    "af69b79ca350eea8750ef14283607a5c83228f8321aab18c1bfa85dae5cbc356",
    "b0c402f9746d98eb488acb54c73fb702ae5b6c5f92878310e201d4b5b1e04a26",
    "eaf46542e864d93347b40e486bb0529b483aec1e53526a4d5363ef01ed9a3812",
    "f4f7f28ba57a35fd0d1155f9dba85519fdca9893444e8ede239d5a1ea4694f94",
    "f6f01e0a3c6d7120fc92c72a4f6d92c962a38af28e717b997544a906ba65caa6",
    "f99357db1e0dd90bdbcf71d5eb88f5c85e7e1c1a01c5bbe177f147f9b0fd2c17",
})

_SVG = "http://www.w3.org/2000/svg "
_MAX_SVG_BYTES = 8 * 1024 * 1024
_FONT_RULE = re.compile(
    r"@font-face\s*\{\s*font-family:\s*(?:Xiaolai|Excalifont)\s*;\s*"
    r"src:\s*url\(\s*data:font/woff2;base64,"
    r"(?P<payload>[A-Za-z0-9+/]*={0,2})\s*\)\s*;\s*\}"
)


def reviewed_font(payload: str) -> bool:
    """Strict encoding/header checks plus immutable, previously reviewed bytes."""
    if not 64 <= len(payload) <= 1024 * 1024:
        return False
    try:
        data = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error):
        return False
    if len(data) < 48 or base64.b64encode(data).decode("ascii") != payload:
        return False
    header = struct.unpack(">4sIIHHIIHHIIIII", data[:48])
    signature, flavor, length, tables, reserved, sfnt_size, compressed = header[:7]
    if (signature != b"wOF2" or flavor != 0x00010000 or length != len(data)
            or not 1 <= tables <= 128 or reserved != 0
            or not 12 <= sfnt_size <= _MAX_SVG_BYTES
            or not 1 <= compressed <= len(data) - 48
            or any(header[9:])):
        return False
    # Requiring zero metadata/private offsets AND lengths disallows unaudited
    # metadata or trailing private data, even before the immutable hash check.
    return sha256(data).hexdigest() in REVIEWED_FONT_SHA256


def semantic_text(path: str, text: str) -> str:
    """Preserve all semantic content; omit only pinned font base64 spans.

    Malformed XML, entities inside style text, CSS comments, extra declarations,
    nested style elements, unrecognized fonts, or changed font bytes fail closed:
    their original payload is retained. No image or arbitrary data URL is masked.
    """
    if PurePosixPath(path).suffix.lower() != ".svg":
        return text
    source = text.encode("utf-8")
    if len(source) > _MAX_SVG_BYTES:
        return text
    parser = expat.ParserCreate(namespace_separator=" ")
    stack: list[str] = []
    root_name = ""
    style: dict | None = None
    masks: list[tuple[int, int]] = []
    decoded_semantics: list[str] = []

    def start(name: str, attributes: dict[str, str]) -> None:
        nonlocal root_name, style
        if not stack:
            root_name = name
        stack.append(name)
        decoded_semantics.append("\n")
        decoded_semantics.extend(value + "\n" for value in attributes.values())
        if style is not None:
            style["nested"] = True
        elif name == _SVG + "style":
            style = {"depth": len(stack), "chunks": [], "nested": False}

    def characters(value: str) -> None:
        if style is not None:
            style["chunks"].append((parser.CurrentByteIndex, value))
        else:
            decoded_semantics.append(value)

    def end(_name: str) -> None:
        nonlocal style
        if style is not None and len(stack) == style["depth"]:
            chunks = style["chunks"]
            css = "".join(value for _offset, value in chunks)
            rules = list(_FONT_RULE.finditer(css))
            cursor = 0
            valid_grammar = bool(rules) and not style["nested"]
            for rule in rules:
                valid_grammar = valid_grammar and not css[cursor:rule.start()].strip()
                cursor = rule.end()
            valid_grammar = valid_grammar and not css[cursor:].strip()
            if chunks:
                first = chunks[0][0]
                last = chunks[-1][0] + len(chunks[-1][1].encode("utf-8"))
                valid_grammar = valid_grammar and source[first:last] == css.encode("utf-8")
            if valid_grammar:
                for rule in rules:
                    if reviewed_font(rule["payload"]):
                        begin = first + len(css[:rule.start("payload")].encode("utf-8"))
                        finish = begin + len(rule["payload"])
                        masks.append((begin, finish))
            else:
                # Also scan XML-decoded style strings if no masking is allowed.
                decoded_semantics.append(css)
            style = None
        decoded_semantics.append("\n")
        stack.pop()

    def forbidden_xml(*_args) -> None:
        raise ValueError("external entities and document declarations are not permitted")

    parser.StartElementHandler = start
    parser.CharacterDataHandler = characters
    parser.EndElementHandler = end
    parser.StartDoctypeDeclHandler = forbidden_xml
    parser.EntityDeclHandler = forbidden_xml
    parser.ExternalEntityRefHandler = forbidden_xml
    try:
        parser.Parse(source, True)
    except (expat.ExpatError, ValueError):
        return text
    if root_name != _SVG + "svg":
        return text
    for begin, finish in sorted(masks, reverse=True):
        source = source[:begin] + b"[reviewed-font-binary]" + source[finish:]
    # Appending decoded text/attributes strengthens checks for XML numeric entity
    # spellings without discarding original comments, attributes, or CSS syntax.
    return source.decode("utf-8") + "\n" + "".join(decoded_semantics)
