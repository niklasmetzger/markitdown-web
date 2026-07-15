"""markitdown wrapper with the .potx content-type fix baked in."""
from __future__ import annotations

import io
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from markitdown import MarkItDown
from markitdown._exceptions import FileConversionException

# Singleton — MarkItDown is reusable, registering plugins is a one-time cost.
_md: Optional[MarkItDown] = None


def get_converter() -> MarkItDown:
    global _md
    if _md is None:
        _md = MarkItDown()
    return _md


def _patch_potx_to_pptx(src: Path, dst: Path) -> None:
    """markitdown refuses .potx because the internal content-type ends in
    `template.main+xml` rather than `presentation.main+xml`. We patch it
    in a temporary copy and pass the copy to markitdown."""
    shutil.copyfile(src, dst)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    with zipfile.ZipFile(dst, "r") as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "[Content_Types].xml":
                data = data.replace(
                    b"presentationml.template.main+xml",
                    b"presentationml.presentation.main+xml",
                )
            zout.writestr(item, data)
    os.replace(tmp, dst)


def convert_file(src_path: Path) -> str:
    """Convert a file on disk to Markdown. Returns the markdown text."""
    md = get_converter()
    suffix = src_path.suffix.lower()
    if suffix == ".potx":
        with tempfile.TemporaryDirectory() as td:
            patched = Path(td) / (src_path.stem + ".pptx")
            _patch_potx_to_pptx(src_path, patched)
            result = md.convert(patched)
            return result.text_content
    result = md.convert(src_path)
    return result.text_content


def convert_upload(filename: str, content: bytes) -> str:
    """Convert an uploaded file (filename + raw bytes) to Markdown."""
    md = get_converter()
    suffix = Path(filename).suffix.lower()

    if suffix == ".potx":
        # Write, patch, then convert from disk
        with tempfile.TemporaryDirectory() as td:
            patched = Path(td) / (Path(filename).stem + ".pptx")
            tmp_in = Path(td) / filename
            tmp_in.write_bytes(content)
            _patch_potx_to_pptx(tmp_in, patched)
            result = md.convert(patched)
            return result.text_content

    # Stream-based conversion for everything else
    stream = io.BytesIO(content)
    stream_info_guesses = None
    try:
        result = md.convert_stream(stream, file_extension=suffix)
        return result.text_content
    except FileConversionException:
        # Fallback: write to temp file and use convert_local
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / filename
            tmp.write_bytes(content)
            result = md.convert_local(tmp)
            return result.text_content
