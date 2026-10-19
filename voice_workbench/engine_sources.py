"""Alternate transports for the same checksum-pinned GitHub release assets."""
from typing import Literal

import httpx

from .downloads import DownloadPaused, download_file

EngineSource = Literal['auto', 'ghproxy', 'ghfast', 'github']
SOURCE_NAMES = {'auto': '自动切换（推荐）', 'ghfast': 'ghfast 加速',
                'ghproxy': 'GH-Proxy 加速', 'github': 'GitHub 官方直连'}
PREFIXES = {'ghfast': 'https://ghfast.top/', 'ghproxy': 'https://gh-proxy.org/', 'github': ''}


def download_archive(target, entry, official_url, event, client_factory, progress, source, label='引擎'):
    candidates = tuple(PREFIXES) if source == 'auto' else (source,)
    errors = []
    for candidate in candidates:
        if event.is_set():
            raise DownloadPaused()
        progress(active_source=candidate, source_errors=list(errors))
        try:
            download_file(target, entry, PREFIXES[candidate] + official_url, event, client_factory, progress)
            return
        except (httpx.HTTPError, ValueError) as exc:
            # A corrupt complete download cache must not poison the next source.
            # Partial transfers are retained for Range resume and final verification.
            if isinstance(exc, ValueError) and target.is_file():
                target.unlink()
            errors.append({'source': candidate, 'message': str(exc)})
            progress(source_errors=list(errors))
            if source != 'auto':
                raise
    detail = '；'.join(f"{SOURCE_NAMES[e['source']]}：{e['message']}" for e in errors)
    raise ValueError(f'全部{label}下载源均失败：' + detail)
