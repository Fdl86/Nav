import logging
from typing import Optional

import requests
import streamlit as st

UA = {"User-Agent": "vfr-prep-mobile/1.5"}
LOGGER = logging.getLogger(__name__)


@st.cache_resource
def session() -> requests.Session:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    s = requests.Session()
    s.headers.update(UA)
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def fetch_json(url: str, params: Optional[dict] = None, timeout: int = 20):
    r = session().get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()
