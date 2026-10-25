"""Disk-backed ZIP creation; source files and downloads are copied in chunks."""

import shutil
import tempfile
from zipfile import ZipFile


def create_zip(sources):
    output = tempfile.TemporaryFile()
    try:
        with ZipFile(output, "w") as archive:
            for name, source in sources:
                with archive.open(name, "w", force_zip64=True) as target:
                    shutil.copyfileobj(source, target, length=64 * 1024)
        output.seek(0)
        return output
    except Exception:
        output.close()
        raise
