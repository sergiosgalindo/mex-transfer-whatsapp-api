import os
from typing import Any, Dict, Optional

import httpx


class CoreApi:
    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("CODI_CORE_API_URL", "http://localhost:8080")).rstrip("/")
        self.token = os.getenv("INTERNAL_API_TOKEN", "") if token is None else token
        self._client = client or httpx.Client(timeout=30.0)

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        response = self._client.get(self.base_url + path, params=params, headers=self._headers())
        response.raise_for_status()
        return response.json()

    def post(self, path: str, body: Dict[str, Any]) -> Any:
        response = self._client.post(self.base_url + path, json=body, headers=self._headers())
        response.raise_for_status()
        return response.json()

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-Internal-Token"] = self.token
        return headers
