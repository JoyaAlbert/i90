from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from urllib.parse import urljoin

import httpx

BASE_URL = "https://api.esios.ree.es"
ARCHIVE_ID = 34


@dataclass(frozen=True)
class ArchiveCandidate:
    data_date: date
    download_url: str
    metadata: dict


class EsiosClient:
    def __init__(self, api_key: str, timeout: float = 60.0) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self._headers = {
            "Accept": "application/json; application/vnd.esios-api-v1+json",
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "User-Agent": "JoyaAlbert/i90",
        }

    def _client(self) -> httpx.Client:
        return httpx.Client(
            headers=self._headers,
            timeout=self.timeout,
            follow_redirects=False,
        )

    @staticmethod
    def _params(day: date) -> dict[str, str]:
        return {
            "date_type": "datos",
            "locale": "es",
            "start_date": f"{day.isoformat()}T00:00:00",
            "end_date": f"{day.isoformat()}T23:59:59",
        }

    @staticmethod
    def _archive_obj(payload: dict) -> dict:
        obj = payload.get("archive", payload)
        if isinstance(obj, list):
            if not obj:
                return {}
            obj = obj[0]
        return obj if isinstance(obj, dict) else {}

    def get_candidate(self, day: date) -> ArchiveCandidate | None:
        """
        Resolve metadata for I90DIA by FECHA DE DATOS.
        We try the archive-specific endpoint first and fall back to the archive list.
        """
        attempts = [
            (f"{BASE_URL}/archives/{ARCHIVE_ID}", self._params(day)),
            (f"{BASE_URL}/archives", {**self._params(day), "id": str(ARCHIVE_ID)}),
        ]

        with self._client() as client:
            for url, params in attempts:
                response = client.get(url, params=params)
                if response.status_code in (404, 204):
                    continue
                response.raise_for_status()
                payload = response.json()
                obj = self._archive_obj(payload)

                # Some list responses may nest archives.
                if not obj and isinstance(payload.get("archives"), list):
                    matches = [
                        x for x in payload["archives"]
                        if str(x.get("id")) == str(ARCHIVE_ID)
                    ]
                    obj = matches[0] if matches else {}

                download = obj.get("download")
                if isinstance(download, dict):
                    download = download.get("url")

                if download:
                    return ArchiveCandidate(
                        data_date=day,
                        download_url=urljoin(BASE_URL, str(download)),
                        metadata=obj,
                    )
        return None

    def direct_download_url(self, day: date) -> str:
        """
        Fallback route documented by eSIOS. The request itself is performed with
        x-api-key and query params; any cross-host redirect is followed WITHOUT the key.
        """
        params = httpx.QueryParams(self._params(day))
        return f"{BASE_URL}/archives/{ARCHIVE_ID}/download?{params}"

    def download(self, url: str) -> tuple[bytes, dict]:
        with self._client() as client:
            response = client.get(url)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    response.raise_for_status()
                with httpx.Client(
                    timeout=self.timeout,
                    follow_redirects=True,
                    headers={"User-Agent": "JoyaAlbert/i90"},
                ) as plain:
                    redirected = plain.get(location)
                    redirected.raise_for_status()
                    return redirected.content, {
                        "initial_status": response.status_code,
                        "redirected": True,
                        "final_url": str(redirected.url),
                        "content_type": redirected.headers.get("content-type"),
                    }

            response.raise_for_status()
            return response.content, {
                "initial_status": response.status_code,
                "redirected": False,
                "final_url": str(response.url),
                "content_type": response.headers.get("content-type"),
            }
