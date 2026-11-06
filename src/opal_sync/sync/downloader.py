import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote, urljoin
import httpx

from opal_sync.models.file_entry import DownloadRequest
from opal_sync.scraper.wicket import WicketResponse
from opal_sync.utils.logging import get_logger
from opal_sync.utils.sanitize import sanitize_filename

logger = get_logger()


def _filesystem_path(path: Path) -> Path:
    """Use the Win32 extended-length syntax for long local paths."""
    path = Path(path)
    if os.name == "nt":
        value = str(path)
        if not value.startswith("\\\\?\\"):
            return Path("\\\\?\\" + value)
    return path

# RFC 6266 / RFC 5987 content-disposition regexes
FILENAME_STAR_REGEX = re.compile(r"filename\*\s*=\s*(?:[^\'\"]*\'[^\'\"]*\')?([^\";]+)", re.I)
FILENAME_STANDARD_REGEX = re.compile(r'filename\s*=\s*(?:"([^"]+)"|([^;\s]+))', re.I)


@dataclass
class DownloadResult:
    success: bool
    bytes_downloaded: int = 0
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    content_type: Optional[str] = None
    resolved_filename: Optional[str] = None
    error_message: Optional[str] = None


def parse_content_disposition(header_val: str) -> Optional[str]:
    """Parse filename from Content-Disposition header supporting UTF-8 and standard syntax."""
    if not header_val:
        return None

    # Check filename* first (RFC 5987 / RFC 6266)
    match_star = FILENAME_STAR_REGEX.search(header_val)
    if match_star:
        raw_val = match_star.group(1).strip()
        decoded = unquote(raw_val)
        return sanitize_filename(decoded)

    # Fallback to standard filename=
    match_std = FILENAME_STANDARD_REGEX.search(header_val)
    if match_std:
        raw_val = match_std.group(1) or match_std.group(2)
        if raw_val:
            return sanitize_filename(unquote(raw_val.strip()))

    return None


class FileDownloader:
    """Handles resilient streaming downloads with retries and atomic writes."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        max_retries: int = 3,
        backoff_factor: float = 1.5,
        chunk_size: int = 65536,
        request_delay_seconds: float = 0.5,
        max_file_size_bytes: Optional[int] = None,
    ):
        self.client = client
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.chunk_size = chunk_size
        self.request_delay_seconds = request_delay_seconds
        self.max_file_size_bytes = max_file_size_bytes

    async def download_file(
        self,
        remote_url: str,
        target_path: Path,
        progress_callback: Optional[Callable[[int, Optional[int]], None]] = None,
        download_request: Optional[DownloadRequest] = None,
        preserve_target_filename: bool = False,
    ) -> DownloadResult:
        """Download file from remote_url to target_path with retries and atomic write."""
        target_path = _filesystem_path(Path(target_path).resolve())
        target_dir = target_path.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = target_dir / f"{target_path.name}.part"

        if self.request_delay_seconds > 0:
            await asyncio.sleep(self.request_delay_seconds)

        attempt = 0
        last_error = ""
        attempted_urls: list[str] = []

        def failed(error_message: str) -> DownloadResult:
            urls = " -> ".join(attempted_urls or [remote_url])
            logger.error(
                f"Download failed for {target_path.name}; URLs tried: {urls}; "
                f"error: {error_message}"
            )
            return DownloadResult(success=False, error_message=error_message)

        while attempt < self.max_retries:
            attempt += 1
            try:
                # Page-bound Wicket GET actions must be sent before another
                # request advances the page instance. They are not direct
                # file URLs, so skip the optimistic GET for a GET action.
                request_method = (
                    download_request.method.upper() if download_request else None
                )
                if download_request and request_method != "GET":
                    attempted_urls.append(remote_url)
                    async with self.client.stream(
                        "GET",
                        remote_url,
                        follow_redirects=True,
                    ) as direct_resp:
                        if str(direct_resp.url) != remote_url:
                            attempted_urls.append(str(direct_resp.url))
                        content_type = direct_resp.headers.get("content-type", "").lower()
                        content_disposition = direct_resp.headers.get(
                            "content-disposition", ""
                        ).lower()
                        is_attachment = "attachment" in content_disposition
                        if (
                            direct_resp.status_code == 200
                            and ("text/html" not in content_type or is_attachment)
                        ):
                            return await self._save_response(
                                direct_resp,
                                target_path,
                                tmp_path,
                                progress_callback,
                                preserve_target_filename,
                            )

                method = request_method or "GET"
                request_url = download_request.url if download_request else remote_url
                attempted_urls.append(request_url)
                if download_request and method == "POST":
                    request_context = self.client.stream(
                        method,
                        request_url,
                        data=download_request.data,
                        headers=download_request.headers,
                        follow_redirects=True,
                    )
                elif download_request:
                    request_context = self.client.stream(
                        method,
                        request_url,
                        headers=download_request.headers,
                        follow_redirects=True,
                    )
                else:
                    request_context = self.client.stream(
                        method,
                        request_url,
                        follow_redirects=True,
                    )

                async with request_context as resp:
                    if str(resp.url) != request_url:
                        attempted_urls.append(str(resp.url))
                    if resp.status_code != 200:
                        # Retry on 5xx or 429
                        if resp.status_code in (429, 500, 502, 503, 504):
                            if attempt < self.max_retries:
                                wait_time = self.backoff_factor ** attempt
                                logger.warning(
                                    f"HTTP {resp.status_code} for {remote_url}. Retrying in {wait_time:.1f}s (attempt {attempt}/{self.max_retries})..."
                                )
                                await asyncio.sleep(wait_time)
                                continue
                            return failed(f"HTTP {resp.status_code}: {resp.reason_phrase}")
                        return failed(
                            f"HTTP {resp.status_code}: {resp.reason_phrase}"
                        )

                    if download_request and "xml" in resp.headers.get("content-type", "").lower():
                        wicket_response = WicketResponse(
                            (await resp.aread()).decode("utf-8", errors="replace")
                        )
                        script_redirect = re.search(
                            r"window\.location\s*=\s*['\"]([^'\"]+)['\"]",
                            "\n".join(wicket_response.scripts),
                            re.I,
                        )
                        redirect_url = (
                            wicket_response.redirect_url
                            or (script_redirect.group(1) if script_redirect else None)
                        )
                        if redirect_url:
                            download_url = urljoin(str(resp.url), redirect_url)
                            attempted_urls.append(download_url)
                            async with self.client.stream(
                                "GET",
                                download_url,
                                follow_redirects=True,
                            ) as download_resp:
                                if str(download_resp.url) != download_url:
                                    attempted_urls.append(str(download_resp.url))
                                if download_resp.status_code != 200:
                                    if download_resp.status_code in (429, 500, 502, 503, 504):
                                        raise IOError(
                                            f"HTTP {download_resp.status_code}: {download_resp.reason_phrase}"
                                        )
                                    return failed(
                                        f"HTTP {download_resp.status_code}: {download_resp.reason_phrase}"
                                    )
                                result = await self._save_response(
                                    download_resp,
                                    target_path,
                                    tmp_path,
                                    progress_callback,
                                    preserve_target_filename,
                                )
                                if not result.success:
                                    failed(result.error_message or "Unknown download error")
                                return result
                        tmp_path.unlink(missing_ok=True)
                        return failed(
                            "OPAL download action did not return a download redirect"
                        )

                    result = await self._save_response(
                        resp,
                        target_path,
                        tmp_path,
                        progress_callback,
                        preserve_target_filename,
                    )
                    if not result.success:
                        failed(result.error_message or "Unknown download error")
                    return result

            except (httpx.TransportError, httpx.TimeoutException, IOError, ValueError) as e:
                if isinstance(e, httpx.ReadTimeout):
                    last_error = (
                        "Timed out while waiting for file data. Your network may "
                        "be slow or temporarily stalled; check your connection "
                        "and try syncing again."
                    )
                else:
                    last_error = str(e) or repr(e)
                tmp_path.unlink(missing_ok=True)
                if attempt < self.max_retries:
                    wait_time = self.backoff_factor ** attempt
                    logger.warning(
                        f"Download error on {remote_url}: {last_error}. "
                        f"Retrying in {wait_time:.1f}s "
                        f"(attempt {attempt}/{self.max_retries})..."
                    )
                    await asyncio.sleep(wait_time)
                else:
                    last_error = (
                        f"Download failed after {self.max_retries} attempts: "
                        f"{last_error}"
                    )
                    failed(last_error)
            except asyncio.CancelledError:
                tmp_path.unlink(missing_ok=True)
                raise

        return DownloadResult(success=False, error_message=last_error)

    async def _save_response(
        self,
        resp: httpx.Response,
        target_path: Path,
        tmp_path: Path,
        progress_callback: Optional[Callable[[int, Optional[int]], None]],
        preserve_target_filename: bool = False,
    ) -> DownloadResult:
        headers = resp.headers
        content_length_str = headers.get("content-length")
        # httpx decodes compressed response bodies in aiter_bytes(), while
        # Content-Length describes the compressed bytes on the wire.
        content_encoding = headers.get("content-encoding", "").strip().lower()
        if content_encoding and content_encoding != "identity":
            content_length_str = None
        total_bytes = (
            int(content_length_str)
            if content_length_str and content_length_str.isdigit()
            else None
        )
        etag = headers.get("etag")
        last_modified = headers.get("last-modified")
        content_type = headers.get("content-type")
        disposition = headers.get("content-disposition", "")
        resolved_filename = parse_content_disposition(disposition)

        if (
            self.max_file_size_bytes
            and total_bytes
            and total_bytes > self.max_file_size_bytes
        ):
            mb = total_bytes / (1024 * 1024)
            max_mb = self.max_file_size_bytes / (1024 * 1024)
            return DownloadResult(
                success=False,
                error_message=f"File size ({mb:.1f} MB) exceeds configured limit ({max_mb:.1f} MB)",
            )

        bytes_received = 0

        with open(tmp_path, "wb") as f:
            async for chunk in resp.aiter_bytes(chunk_size=self.chunk_size):
                if not chunk:
                    continue
                f.write(chunk)
                bytes_received += len(chunk)

                if self.max_file_size_bytes and bytes_received > self.max_file_size_bytes:
                    tmp_path.unlink(missing_ok=True)
                    return DownloadResult(
                        success=False,
                        error_message="File exceeded maximum size limit during download",
                    )

                if progress_callback:
                    progress_callback(bytes_received, total_bytes)

        if total_bytes is not None and bytes_received != total_bytes:
            tmp_path.unlink(missing_ok=True)
            raise IOError(f"Truncated download: expected {total_bytes} bytes, received {bytes_received}")

        final_path = target_path
        if (
            not preserve_target_filename
            and resolved_filename
            and resolved_filename != target_path.name
        ):
            final_path = target_path.parent / resolved_filename

        if tmp_path.exists():
            if final_path.exists():
                final_path.unlink()
            tmp_path.rename(final_path)

        return DownloadResult(
            success=True,
            bytes_downloaded=bytes_received,
            etag=etag,
            last_modified=last_modified,
            content_type=content_type,
            resolved_filename=None if preserve_target_filename else final_path.name,
        )
