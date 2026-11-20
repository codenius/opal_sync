import gzip

import httpx
import pytest

from opal_sync.sync.downloader import FileDownloader
from opal_sync.models.file_entry import DownloadRequest


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


@pytest.mark.asyncio
async def test_retries_direct_file_get_after_wicket_action_returns_bad_request(tmp_path):
    direct_gets = 0
    wicket_posts = 0
    remote_url = "https://example.test/course/file.pdf"
    wicket_url = "https://example.test/course"
    body = b"%PDF-1.7 recovered"

    async def handler(request):
        nonlocal direct_gets, wicket_posts
        if request.method == "GET" and str(request.url) == remote_url:
            direct_gets += 1
            if direct_gets == 1:
                return httpx.Response(
                    200,
                    text="<html>temporary page</html>",
                    headers={"content-type": "text/html"},
                    request=request,
                )
            return httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=body,
                request=request,
            )
        if request.method == "POST" and str(request.url) == wicket_url:
            wicket_posts += 1
            return httpx.Response(400, text="Bad Request", request=request)
        return httpx.Response(404, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloader = FileDownloader(
            client,
            max_retries=1,
            request_delay_seconds=0,
        )
        target = tmp_path / "file.pdf"
        result = await downloader.download_file(
            remote_url,
            target,
            download_request=DownloadRequest(
                url=wicket_url,
                data={"selection": "file"},
                headers={"Wicket-Ajax": "true"},
                method="POST",
            ),
        )

    assert result.success
    assert direct_gets == 2
    assert wicket_posts == 1
    assert target.read_bytes() == body
