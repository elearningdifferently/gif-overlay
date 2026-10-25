"""Bounded upload ingestion and disk-backed multipart storage."""

import tempfile

from flask import Request, current_app
from werkzeug.exceptions import RequestEntityTooLarge

from gif_overlay import GifOverlayError

CHUNK_SIZE = 64 * 1024


class FileTooLarge(RequestEntityTooLarge):
    pass


def copy_upload(source, target, limit):
    size = 0
    while chunk := source.read(CHUNK_SIZE):
        size += len(chunk)
        if size > limit:
            raise FileTooLarge()
        target.write(chunk)
    if not size:
        raise GifOverlayError("An uploaded file is empty.")


class UploadStream:
    def __init__(self, limit):
        self.file = tempfile.TemporaryFile(mode="w+b")
        self.limit = limit
        self.size = 0

    def write(self, data):
        self.size += len(data)
        if self.size > self.limit:
            raise FileTooLarge()
        return self.file.write(data)

    def __getattr__(self, name):
        return getattr(self.file, name)


class DiskUploadRequest(Request):
    def _get_file_stream(self, total_content_length, content_type, filename=None,
                         content_length=None):
        stream = UploadStream(current_app.config["MAX_FILE_LENGTH"])
        self.__dict__.setdefault("_upload_streams", []).append(stream)
        return stream

    def close(self):
        # Parsing may fail before Werkzeug assigns FileStorage objects to files.
        try:
            for stream in self.__dict__.get("_upload_streams", []):
                stream.close()
        finally:
            super().close()
