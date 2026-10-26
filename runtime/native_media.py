"""Internal media locators; native XML, URLs and crypto fields never enter prompts."""
import re
import xml.etree.ElementTree as ET


def details(text,native_type):
    if not text or native_type not in (3,47):return {}
    try:root=ET.fromstring(text[text.find('<'):])
    except ET.ParseError:return {}
    node=root.find('.//emoji' if native_type==47 else './/img')
    return dict(node.attrib) if node is not None else {}


def metadata(text,native_type):
    if not text or native_type not in (3,47):return None
    start=text.find('<')
    try:root=ET.fromstring(text[start:])
    except ET.ParseError:return None
    node=root.find('.//emoji' if native_type==47 else './/img')
    if node is None:return None
    digest=node.get('md5','').lower()
    if not re.fullmatch('[0-9a-f]{32}',digest):return None
    result={'kind':'sticker' if native_type==47 else 'image','md5':digest}
    # Only public, directly viewable emoji URLs; encrypted CDN endpoints are not fetched.
    if native_type==47:
        for field in ('cdnurl','thumburl'):
            if node.get(field):result[field]=node.get(field)
    return result
