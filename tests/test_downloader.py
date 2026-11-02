import gzip

import httpx
import pytest

from opal_sync.sync.downloader import FileDownloader


@pytest.mark.asyncio
async def test_compressed_response_uses_decoded_body_length(tmp_path):
    body = b'{"cells": []}' * 100
    compressed = gzip.compress(body)
    response = httpx.Response(
        200,
        headers={
            "content-encoding": "gzip",
            "content-length": str(len(compressed)),
            "content-type": "application/json",
        },
        content=compressed,
    )
    target = tmp_path / "download.ipynb"
    temporary = tmp_path / "download.ipynb.part"

    result = await FileDownloader(client=None)._save_response(
        response, target, temporary, None
    )

    assert result.success
    assert result.bytes_downloaded == len(body)
    assert target.read_bytes() == body
