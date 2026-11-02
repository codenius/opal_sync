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


@pytest.mark.asyncio
async def test_read_timeout_returns_slow_network_advice(tmp_path):
    async def handler(request):
        raise httpx.ReadTimeout("", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloader = FileDownloader(
            client,
            max_retries=1,
            request_delay_seconds=0,
        )
        target = tmp_path / "slow-file.bin"
        result = await downloader.download_file(
            "https://example.test/slow-file.bin", target
        )

    assert not result.success
    assert "network may be slow" in result.error_message
    assert "try syncing again" in result.error_message
    assert not target.exists()
    assert not target.with_name(target.name + ".part").exists()
