"""Write transcripts as real Word documents (.docx), stdlib only.

A .docx is an OPC zip of XML parts; the minimal package Word accepts is
[Content_Types].xml + _rels/.rels + word/document.xml (+ docProps/core.xml for
author metadata). Building it with zipfile keeps python-docx (and its lxml
binary dependency) out of the PyInstaller build for what is one export format.

All text is XML-escaped; formatting is direct (w:rPr) so no styles part is
needed. Also hosts the pure segment->paragraph grouping used by the
file-transcription tab, so it is unit-testable without Qt or models.
"""
import datetime
import re
import zipfile
from xml.sax.saxutils import escape

# Direct-formatting constants (half-points for sizes, hex for colors).
_TITLE_SIZE = 40      # 20pt
_META_SIZE = 18       # 9pt
_BODY_SIZE = 22       # 11pt
_META_COLOR = "64748B"
_TS_COLOR = "94A3B8"
_SPEAKER_COLOR = "334155"

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    '</Types>'
)

_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
    '</Relationships>'
)


def format_timestamp(seconds):
    """Seconds -> "MM:SS" under an hour, else "H:MM:SS"."""
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def group_segments(segments, max_gap=2.5, max_chars=550):
    """Merge timestamped transcript segments into readable paragraphs.

    A new paragraph starts on a speaker change, a silence gap of ``max_gap``
    seconds, or when the current one grows past ``max_chars`` - a wall-of-text
    docx is exactly what this feature must not produce.

    ``segments``: dicts with "start", "end", "text", optional "speaker".
    Returns dicts: {"start", "speaker" (int|None), "text"}.
    """
    paragraphs = []
    cur = None
    for seg in segments or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        spk = seg.get("speaker")
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", start))
        if (cur is None
                or spk != cur["speaker"]
                or start - cur["_last_end"] > max_gap
                or len(cur["text"]) + len(text) > max_chars):
            cur = {"start": start, "speaker": spk, "text": text, "_last_end": end}
            paragraphs.append(cur)
        else:
            cur["text"] += " " + text
            cur["_last_end"] = end
    for p in paragraphs:
        p.pop("_last_end", None)
    return paragraphs


def _run(text, *, bold=False, color=None, size=_BODY_SIZE):
    props = [f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>']
    if bold:
        props.append("<w:b/>")
    if color:
        props.append(f'<w:color w:val="{color}"/>')
    return ('<w:r><w:rPr>' + "".join(props) + '</w:rPr>'
            f'<w:t xml:space="preserve">{escape(text)}</w:t></w:r>')


def _para(runs, *, space_after=160):
    return (f'<w:p><w:pPr><w:spacing w:after="{space_after}"/></w:pPr>'
            + "".join(runs) + "</w:p>")


def speaker_name(spk, names=None):
    if names and spk in names:
        return names[spk]
    if spk is None:
        return ""
    return f"Speaker {int(spk) + 1}"


def build_docx(title, paragraphs, meta_line="", speaker_names=None,
               include_timestamps=True):
    """The complete .docx as bytes.

    ``paragraphs``: output of :func:`group_segments`.
    """
    body = [_para([_run(title or "Transcript", bold=True, size=_TITLE_SIZE)],
                  space_after=80)]
    if meta_line:
        body.append(_para([_run(meta_line, color=_META_COLOR, size=_META_SIZE)],
                          space_after=280))
    for p in paragraphs or []:
        runs = []
        if include_timestamps:
            runs.append(_run(f"[{format_timestamp(p.get('start', 0))}]  ",
                             color=_TS_COLOR, size=_META_SIZE))
        label = speaker_name(p.get("speaker"), speaker_names)
        if label:
            runs.append(_run(f"{label}:  ", bold=True, color=_SPEAKER_COLOR))
        runs.append(_run(p.get("text", "")))
        body.append(_para(runs))
    if len(body) == (2 if meta_line else 1):
        body.append(_para([_run("(No speech was detected in this file.)",
                                color=_META_COLOR)]))

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body>' + "".join(body) +
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1200" w:right="1200" w:bottom="1200" w:left="1200"/>'
        '</w:sectPr></w:body></w:document>'
    )

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties'
        ' xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:dcterms="http://purl.org/dc/terms/"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f'<dc:title>{escape(title or "Transcript")}</dc:title>'
        '<dc:creator>Transcribe App</dc:creator>'
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
        '</cp:coreProperties>'
    )

    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", document)
        z.writestr("docProps/core.xml", core)
    return buf.getvalue()


def save_docx(path, title, paragraphs, **kwargs):
    data = build_docx(title, paragraphs, **kwargs)
    with open(path, "wb") as f:
        f.write(data)
    return path


def safe_filename(name, fallback="transcript"):
    """A filesystem-safe stem for the suggested save name."""
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", name or "").strip().strip(".")
    return stem or fallback
