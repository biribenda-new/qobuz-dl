import hashlib
import logging
import time
from abc import ABC, abstractmethod
from typing import Generator, Tuple, Union

import requests
import tidalapi

from .exceptions import (
    AuthenticationError,
    IneligibleError,
    InvalidAppIdError,
    InvalidAppSecretError,
    InvalidQuality,
)
from .spoofbuz import Spoofer

logger = logging.getLogger(__name__)

# Qobuz
QOBUZ_BASE = "https://www.qobuz.com/api.json/0.2"
AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:83.0) Gecko/20100101 Firefox/83.0"

QOBUZ_FEATURED_KEYS = [
    "most-streamed",
    "recent-releases",
    "best-sellers",
    "press-awards",
    "ideal-discography",
    "editor-picks",
    "most-featured",
    "qobuzissims",
    "new-releases",
    "new-releases-full",
    "harmonia-mundi",
    "universal-classic",
    "universal-jazz",
    "universal-jeunesse",
    "universal-chanson",
]

# Tidal
TIDAL_Q_IDS = {
    4: tidalapi.Quality.low,  # AAC
    5: tidalapi.Quality.high,  # AAC
    6: tidalapi.Quality.lossless,  # Lossless, but it also could be MQA
}


# Deezer
DEEZER_BASE = "https://api.deezer.com"
DEEZER_DL = "http://dz.loaderapp.info/deezer"
DEEZER_Q_IDS = {4: 128, 5: 320, 6: 1411}


# ----------- Abstract Classes -----------------


class ClientInterface(ABC):
    """Common API for clients of all platforms.

    This is an Abstract Base Class. It cannot be instantiated;
    it is merely a template.
    """

    @abstractmethod
    def search(self, query: str, media_type="album"):
        """Search API for query.

        :param query:
        :type query: str
        :param type_:
        """
        pass

    @abstractmethod
    def get(self, item_id, media_type="album"):
        """Get metadata.

        :param meta_id:
        :param type_:
        """
        pass

    @abstractmethod
    def get_file_url(self, track_id, quality=6):
        """Get the direct download url for a file.

        :param track_id: id of the track
        """
        pass


class SecureClientInterface(ClientInterface):
    """Identical to a ClientInterface except for a login
    method.

    This is an Abstract Base Class. It cannot be instantiated;
    it is merely a template.
    """

    @abstractmethod
    def login(self, **kwargs):
        """Authenticate the client.

        :param kwargs:
        """
        pass


# ------------- Clients -----------------


class QobuzClient(SecureClientInterface):
    # ------- Public Methods -------------
    def login(self, email: str, pwd: str, **kwargs):
        """Authenticate the QobuzClient. Must have a paid membership.

        If `app_id` and `secrets` are not provided, this will run the
        Spoofer script, which retrieves them. This will take some time,
        so it is recommended to cache them somewhere for reuse.

        :param email: email for the qobuz account
        :type email: str
        :param pwd: password for the qobuz account
        :type pwd: str
        :param kwargs: app_id: str, secrets: list, return_secrets: bool
        """
        if not (kwargs.get("app_id") or kwargs.get("secrets")):
            logger.info("Fetching tokens from Qobuz")
            spoofer = Spoofer()
            kwargs["app_id"] = spoofer.get_app_id()
            kwargs["secrets"] = spoofer.get_secrets()

        self.app_id = str(kwargs["app_id"])  # Ensure it is a string
        self.secrets = kwargs["secrets"]

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": AGENT,
                "X-App-Id": self.app_id,
            }
        )

        self._api_login(email, pwd)
        logger.debug("Logged into Qobuz")
        self._validate_secrets()
        logger.debug("Qobuz client is ready to use")

        # used for caching app_id and secrets
        if kwargs.get("return_secrets"):
            return self.app_id, self.secrets

    def search(self, query: str, media_type: str = "album", limit: int = 500):
        return self._api_search(query, media_type, limit)

    def get(self, item_id: Union[str, int], media_type: str = "album"):
        return self._api_get(media_type, item_id=item_id)

    def get_file_url(self, item_id, quality=6):
        return self._api_get_file_url(item_id, quality=quality)

    # ---------- Private Methods ---------------

    # Credit to Sorrow446 for the original methods

    def _gen_pages(self, epoint: str, params):
        page, status_code = self._api_request(epoint, params)
        total = page["albums"]["total"]
        limit = page["albums"]["limit"]
        offset = page["albums"]["offset"]
        params.update({"limit": limit})
        yield page
        while (offset + limit) < total:
            offset += limit
            params.update({"offset": offset})
            page, status_code = self._api_request(epoint, params)
            yield page

    def _validate_secrets(self):
        for secret in self.secrets:
            if self._test_secret(secret):
                self.sec = secret
                logger.debug("Working secret and app_id: %s - %s", secret, self.app_id)
                break
        if not hasattr(self, "sec"):
            raise InvalidAppSecretError(f"Invalid secrets: {self.secrets}")

    def _api_get(self, media_type, **kwargs):
        item_id = kwargs.get("item_id")

        params = {
            "app_id": self.app_id,
            f"{media_type}_id": item_id,
            "limit": kwargs.get("limit", 500),
            "offset": kwargs.get("offset", 0),
        }
        extras = {
            "artist": "albums",
            "playlist": "tracks",
            "label": "albums",  # not tested
        }

        if media_type in extras:
            params.update({"extra": extras[media_type]})

        epoint = f"{media_type}/get"

        response, status_code = self._api_request(epoint, params)
        return response

    def _api_search(self, query, media_type, limit=500) -> Generator:
        params = {
            "query": query,
            "limit": limit,
        }
        # TODO: move featured, favorites, and playlists into _api_get later
        if media_type == "featured":
            assert query in QOBUZ_FEATURED_KEYS, f'query "{query}" is invalid.'
            params.update({"type": query})
            del params["query"]
            epoint = "album/getFeatured"

        elif query == "user-favorites":
            assert query in ("track", "artist", "album")
            params.update({"type": f"{media_type}s"})
            epoint = "favorite/getUserFavorites"

        elif query == "user-playlists":
            epoint = "playlist/getUserPlaylists"

        else:
            epoint = f"{media_type}/search"

        return self._gen_pages(epoint, params)

    def _api_login(self, email, pwd):
        # usr_info = self._api_call("user/login", email=email, pwd=pwd)
        params = {
            "email": email,
            "password": pwd,
            "app_id": self.app_id,
        }
        epoint = "user/login"
        resp, status_code = self._api_request(epoint, params)

        if status_code == 401:
            raise AuthenticationError(f"Invalid credentials from params {params}")
        elif status_code == 400:
            raise InvalidAppIdError(f"Invalid app id from params {params}")
        else:
            logger.info("Logged in to Qobuz")

        if not resp["user"]["credential"]["parameters"]:
            raise IneligibleError("Free accounts are not eligible to download tracks.")

        self.uat = resp["user_auth_token"]
        self.session.headers.update({"X-User-Auth-Token": self.uat})
        self.label = resp["user"]["credential"]["parameters"]["short_label"]

    def _api_get_file_url(self, track_id, quality=6, sec=None):
        unix_ts = time.time()

        if int(quality) not in (5, 6, 7, 27):  # Needed?
            raise InvalidQuality(f"Invalid quality id {quality}. Choose 5, 6, 7 or 27")

        secret = sec or self.sec
        r_sig = f"trackgetFileUrlformat_id{quality}intentstreamtrack_id{track_id}{unix_ts}{secret}"
        logger.debug("Raw request signature: %s", r_sig)
        r_sig_hashed = hashlib.md5(r_sig.encode("utf-8")).hexdigest()
        logger.debug("Hashed request signature: %s", r_sig_hashed)

        params = {
            "request_ts": unix_ts,
            "request_sig": r_sig_hashed,
            "track_id": track_id,
            "format_id": quality,
            "intent": "stream",
        }
        response, status_code = self._api_request("track/getFileUrl", params)
        if status_code == 400:
            raise InvalidAppSecretError("Invalid app secret from params %s" % params)
        return response

    def _api_request(self, epoint, params) -> Tuple[dict, int]:
        logging.debug(f"Calling API with endpoint {epoint} params {params}")
        r = self.session.get(f"{QOBUZ_BASE}/{epoint}", params=params)
        return r.json(), r.status_code

    def _test_secret(self, secret) -> bool:
        try:
            self._api_get_file_url("19512574", sec=secret)
            return True
        except InvalidAppSecretError as error:
            logger.debug("Test for %s secret didn't work: %s", secret, error)
            return False


class DeezerClient(ClientInterface):
    def __init__(self):
        self.session = requests.Session()

    def search(self, query: str, media_type: str = "album", limit: int = 200):
        """Search API for query.

        :param query:
        :type query: str
        :param media_type:
        :type media_type: str
        :param limit:
        :type limit: int
        """
        # TODO: more robust url sanitize
        query = query.replace(" ", "+")

        if media_type.endswith("s"):
            media_type = media_type[:-1]

        # TODO: use limit parameter
        response = self.session.get(f"{DEEZER_BASE}/search/{media_type}?q={query}")
        response.raise_for_status()

        return response.json()

    def get(self, meta_id: Union[str, int], type_: str = "album"):
        """Get metadata.

        :param meta_id:
        :type meta_id: Union[str, int]
        :param type_:
        :type type_: str
        """
        response = self.session.get(f"{DEEZER_BASE}/{type_}/{meta_id}")
        response.raise_for_status()

        return response.json()

    @staticmethod
    def get_file_url(meta_id: Union[str, int], quality: int = 6):
        return f'{DEEZER_DL}/{DEEZER_Q_IDS[quality]}/"{DEEZER_BASE}/track/{meta_id}"'


class TidalClient(SecureClientInterface):
    def login(self, email: str, pwd: str):

        config = tidalapi.Config()

        self.session = tidalapi.Session(config=config)
        self.session.login(email, pwd)
        logger.info("Logged into Tidal")

    def search(self, query: str, media_type: str = "album", limit: int = 50):
        """
        :param query:
        :type query: str
        :param media_type: artist, album, playlist, or track
        :type media_type: str
        :param limit:
        :type limit: int
        :raises ValueError: if field value is invalid
        """

        return self.session.search(media_type, query, limit)

    def get(self, meta_id: Union[str, int], media_type: str = "album"):
        """Get metadata.

        :param meta_id:
        :type meta_id: Union[str, int]
        :param media_type:
        :type media_type: str
        """
        f_map = {
            "album": self.session.get_album,
            "artist": self.session.get_artist,  # or get_artist_albums?
            "playlist": self.session.get_playlist,
            "track": self.session.get_track,
        }
        return f_map[media_type](meta_id)

    def get_file_url(self, meta_id: Union[str, int], quality: int = 6):
        """
        :param meta_id:
        :type meta_id: Union[str, int]
        :param quality:
        :type quality: int
        """
        # Not tested
        self.session._config.quality = TIDAL_Q_IDS[quality]
        return self.session.get_track_url(meta_id)
