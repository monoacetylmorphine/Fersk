"""Validate resource bytes and untrusted metadata without network or disk I/O."""

import io
from pathlib import Path
import unittest
from unittest.mock import Mock

from fersk_codex.middleware.resource_validator import (
    ResourceValidationError, read_resource_bytes, validate_downloaded_resource,
)
from fersk_codex.utils.config_loader import CONFIG


class ResourceValidatorTests(unittest.TestCase):
    def validate(self, data=b"\x89PNG\r\n\x1a\n", **kwargs):
        options = dict(resource_key="image-key", resource_type="file", file_name=None)
        options.update(kwargs)
        return validate_downloaded_resource(data=data, **options)

    def test_supported_signatures_supply_canonical_extensions(self):
        cases = [
            (b"\xff\xd8\xff", "jpeg", ".jpg"),
            (b"\x89PNG\r\n\x1a\n", "png", ".png"),
            (b"GIF87a", "gif", ".gif"), (b"GIF89a", "gif", ".gif"),
            (b"RIFF1234WEBP", "webp", ".webp"), (b"BM", "bmp", ".bmp"),
            (b"%PDF-1.7", "pdf", ".pdf"),
            (b"0000ftypisom", "mp4", ".mp4"),
            (b"PK\x03\x04", "zip", ".zip"), (b"PK\x05\x06", "zip", ".zip"),
            (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole", ".bin"),
            (b"OggS", "ogg", ".ogg"), (b"RIFF1234WAVE", "wav", ".wav"),
            (b"ID3", "mp3", ".mp3"), (b"\xff\xe0", "mp3", ".mp3"),
        ]
        for data, format_name, extension in cases:
            with self.subTest(format=format_name, data=data):
                result = self.validate(data)
                self.assertEqual(result.data, data)
                self.assertEqual(result.detected_format, format_name)
                self.assertEqual(result.extension, extension)
                self.assertEqual(result.file_name, "image-key" + extension)

    def test_signature_repairs_misleading_extension(self):
        self.assertEqual(self.validate(file_name="photo.exe").file_name, "photo.png")

    def test_matching_office_and_jpeg_extensions_are_preserved(self):
        for suffix, data in [(s, b"PK\x03\x04") for s in (".docx", ".xlsx", ".pptx")] + [
            (".doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"), (".jpeg", b"\xff\xd8\xff")
        ]:
            with self.subTest(suffix=suffix):
                self.assertEqual(self.validate(data, file_name="report" + suffix).extension, suffix)

    def test_headers_are_case_insensitive_and_mime_parameters_are_removed(self):
        result = self.validate(headers={"CONTENT-TYPE": " Image/PNG ; charset=binary", "Content-Length": 8})
        self.assertEqual(result.mime_type, "image/png")

    def test_content_length_must_be_valid_and_complete(self):
        for value in ("bad", "7", "9", "-1", "0"):
            with self.subTest(value=value), self.assertRaises(ResourceValidationError):
                self.validate(headers={"Content-Length": value})

    def test_mime_conflict_and_nonimage_payload_are_rejected(self):
        for options in ({"headers": {"Content-Type": "application/pdf"}},
                        {"data": b"%PDF-1.7", "resource_type": "image"}):
            with self.subTest(options=options), self.assertRaises(ResourceValidationError):
                self.validate(**options)

    def test_generic_mime_allows_signature_detection(self):
        for mime in ("", "application/octet-stream", "binary/octet-stream"):
            with self.subTest(mime=mime):
                self.assertEqual(self.validate(headers={"Content-Type": mime}).detected_format, "png")

    def test_audio_container_metadata_selects_profile(self):
        for data, mime, name, expected in [
            (b"OggS", "audio/opus", None, "opus"),
            (b"0000ftypisom", "audio/mp4", None, "m4a"),
            (b"0000ftypisom", "video/mp4", "voice.m4a", "m4a"),
        ]:
            with self.subTest(expected=expected, mime=mime):
                self.assertEqual(self.validate(data, file_name=name, headers={"Content-Type": mime}).detected_format, expected)

    def test_json_and_text_validate_actual_content(self):
        for data, name, expected in [(b'\xef\xbb\xbf{"ok":true}', "a.json", "json"),
                                     ("你好\n".encode(), "a.txt", "text")]:
            with self.subTest(name=name):
                self.assertEqual(self.validate(data, file_name=name).detected_format, expected)
        for data, name in [(b"{bad", "a.json"), (b"\xfe", "a.json"),
                           (b"a\x00b", "a.txt"), (b"\xfe", "a.txt")]:
            with self.subTest(data=data, name=name), self.assertRaises(ResourceValidationError):
                self.validate(data, file_name=name)

    def test_known_binary_claim_requires_signature(self):
        for suffix in (".png", ".pdf", ".docx", ".mp4", ".wav", ".mp3"):
            with self.subTest(suffix=suffix), self.assertRaises(ResourceValidationError):
                self.validate(b"renamed payload", file_name="fake" + suffix)

    def test_unknown_file_extension_is_preserved_for_assembly_filter(self):
        result = self.validate(b"custom bytes", file_name="data.custom")
        self.assertEqual((result.detected_format, result.extension), ("custom", ".custom"))

    def test_invalid_type_empty_and_unidentified_payloads_are_rejected(self):
        for options in ({"resource_type": "video"}, {"data": b""}, {"data": "text"},
                        {"data": b"unknown"}, {"data": b"RIFF1234"}, {"data": b"\xff"}):
            with self.subTest(options=options), self.assertRaises(ResourceValidationError):
                self.validate(**options)

    def test_filenames_cannot_escape_destination(self):
        for name in ("../../照片.png", "/tmp/a b.png", r"..\..\photo.png", "...", None):
            with self.subTest(name=name):
                result = self.validate(file_name=name, resource_key="../../unsafe/key")
                self.assertEqual(Path(result.file_name).name, result.file_name)
                self.assertNotIn("\\", result.file_name)
                self.assertFalse(result.file_name.startswith("."))
                self.assertEqual(result.extension, ".png")
        self.assertEqual(self.validate(resource_key=".../").file_name,
                         CONFIG["resources"]["fileName"]["fallbackStem"] + ".png")

    def test_bytesio_is_read_in_full_even_when_cursor_is_at_end(self):
        stream = io.BytesIO(b"image")
        stream.seek(0, io.SEEK_END)
        self.assertEqual(read_resource_bytes(stream), b"image")

    def test_regular_stream_and_nonbinary_response(self):
        stream = Mock(spec=["read"], read=Mock(return_value=b"file"))
        self.assertEqual(read_resource_bytes(stream), b"file")
        stream.read.assert_called_once_with()
        with self.assertRaises(ResourceValidationError):
            read_resource_bytes(io.StringIO("not binary"))
