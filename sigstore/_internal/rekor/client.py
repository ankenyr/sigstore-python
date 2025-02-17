# Copyright 2022 The Sigstore Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Client implementation for interacting with Rekor.
"""

from __future__ import annotations

import base64
import logging
from abc import ABC
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urljoin

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509 import Certificate
from pydantic import BaseModel, Field, StrictInt, StrictStr

from sigstore._internal.ctfe import CTKeyring
from sigstore._internal.tuf import TrustUpdater
from sigstore._utils import base64_encode_pem_cert
from sigstore.transparency import LogEntry

logger = logging.getLogger(__name__)

DEFAULT_REKOR_URL = "https://rekor.sigstore.dev"
STAGING_REKOR_URL = "https://rekor.sigstage.dev"


class RekorBundle(BaseModel):
    """
    Represents an offline Rekor bundle.

    This model contains most of the same information as `RekorEntry`, but
    with a slightly different layout.

    See: <https://github.com/sigstore/cosign/blob/main/specs/SIGNATURE_SPEC.md#properties>
    """

    class Config:
        allow_population_by_field_name = True

    class _Payload(BaseModel):
        body: StrictStr = Field(alias="body")
        integrated_time: StrictInt = Field(alias="integratedTime")
        log_index: StrictInt = Field(alias="logIndex")
        log_id: StrictStr = Field(alias="logID")

        class Config:
            allow_population_by_field_name = True

    signed_entry_timestamp: StrictStr = Field(alias="SignedEntryTimestamp")
    payload: RekorBundle._Payload = Field(alias="Payload")

    def to_entry(self) -> LogEntry:
        """
        Creates a `RekorEntry` from this offline Rekor bundle.
        """

        return LogEntry(
            uuid=None,
            body=self.payload.body,
            integrated_time=self.payload.integrated_time,
            log_id=self.payload.log_id,
            log_index=self.payload.log_index,
            inclusion_proof=None,
            signed_entry_timestamp=self.signed_entry_timestamp,
        )

    @classmethod
    def from_entry(cls, entry: LogEntry) -> RekorBundle:
        """
        Returns a `RekorBundle` for this `RekorEntry`.
        """

        return RekorBundle(
            signed_entry_timestamp=entry.signed_entry_timestamp,
            payload=RekorBundle._Payload(
                body=entry.body,
                integrated_time=entry.integrated_time,
                log_index=entry.log_index,
                log_id=entry.log_id,
            ),
        )


@dataclass(frozen=True)
class RekorLogInfo:
    """
    Represents information about the Rekor log.
    """

    root_hash: str
    tree_size: int
    signed_tree_head: str
    tree_id: str
    raw_data: dict

    @classmethod
    def from_response(cls, dict_: Dict[str, Any]) -> RekorLogInfo:
        """
        Create a new `RekorLogInfo` from the given API response.
        """
        return cls(
            root_hash=dict_["rootHash"],
            tree_size=dict_["treeSize"],
            signed_tree_head=dict_["signedTreeHead"],
            tree_id=dict_["treeID"],
            raw_data=dict_,
        )


class RekorClientError(Exception):
    """
    A generic error in the Rekor client.
    """

    pass


class _Endpoint(ABC):
    def __init__(self, url: str, session: requests.Session) -> None:
        self.url = url
        self.session = session


class RekorLog(_Endpoint):
    """
    Represents a Rekor instance's log endpoint.
    """

    def get(self) -> RekorLogInfo:
        """
        Returns information about the Rekor instance's log.
        """
        resp: requests.Response = self.session.get(self.url)
        try:
            resp.raise_for_status()
        except requests.HTTPError as http_error:
            raise RekorClientError from http_error
        return RekorLogInfo.from_response(resp.json())

    @property
    def entries(self) -> RekorEntries:
        """
        Returns a `RekorEntries` capable of accessing detailed information
        about individual log entries.
        """
        return RekorEntries(urljoin(self.url, "entries/"), session=self.session)


class RekorEntries(_Endpoint):
    """
    Represents the individual log entry endpoints on a Rekor instance.
    """

    def get(
        self, *, uuid: Optional[str] = None, log_index: Optional[int] = None
    ) -> LogEntry:
        """
        Retrieve a specific log entry, either by UUID or by log index.

        Either `uuid` or `log_index` must be present, but not both.
        """
        if not (bool(uuid) ^ bool(log_index)):
            raise RekorClientError("uuid or log_index required, but not both")

        resp: requests.Response

        if uuid is not None:
            resp = self.session.get(urljoin(self.url, uuid))
        else:
            resp = self.session.get(self.url, params={"logIndex": log_index})

        try:
            resp.raise_for_status()
        except requests.HTTPError as http_error:
            raise RekorClientError from http_error
        return LogEntry._from_response(resp.json())

    def post(
        self,
        b64_artifact_signature: str,
        sha256_artifact_hash: str,
        b64_cert: str,
    ) -> LogEntry:
        """
        Submit a new entry for inclusion in the Rekor log.
        """
        # TODO(ww): Dedupe this payload construction with the retrieve endpoint below.
        data = {
            "kind": "hashedrekord",
            "apiVersion": "0.0.1",
            "spec": {
                "signature": {
                    "content": b64_artifact_signature,
                    "publicKey": {"content": b64_cert},
                },
                "data": {
                    "hash": {"algorithm": "sha256", "value": sha256_artifact_hash}
                },
            },
        }

        resp: requests.Response = self.session.post(self.url, json=data)
        try:
            resp.raise_for_status()
        except requests.HTTPError as http_error:
            raise RekorClientError from http_error

        return LogEntry._from_response(resp.json())

    @property
    def retrieve(self) -> RekorEntriesRetrieve:
        """
        Returns a `RekorEntriesRetrieve` capable of retrieving entries.
        """
        return RekorEntriesRetrieve(
            urljoin(self.url, "retrieve/"), session=self.session
        )


class RekorEntriesRetrieve(_Endpoint):
    """
    Represents the entry retrieval endpoints on a Rekor instance.
    """

    def post(
        self,
        signature: bytes,
        artifact_hash: str,
        certificate: Certificate,
    ) -> Optional[LogEntry]:
        """
        Retrieves an extant Rekor entry, identified by its artifact signature,
        artifact hash, and signing certificate.

        Returns None if Rekor has no entry corresponding to the signing
        materials.
        """
        data = {
            "entries": [
                {
                    "kind": "hashedrekord",
                    "apiVersion": "0.0.1",
                    "spec": {
                        "signature": {
                            "content": base64.b64encode(signature).decode(),
                            "publicKey": {
                                "content": base64_encode_pem_cert(certificate),
                            },
                        },
                        "data": {
                            "hash": {
                                "algorithm": "sha256",
                                "value": artifact_hash,
                            }
                        },
                    },
                }
            ]
        }

        resp: requests.Response = self.session.post(self.url, json=data)
        try:
            resp.raise_for_status()
        except requests.HTTPError as http_error:
            if http_error.response.status_code == 404:
                return None
            raise RekorClientError(resp.json()) from http_error

        results = resp.json()

        # The response is a list of `{uuid: LogEntry}` objects.
        # We select the oldest entry for our actual return value,
        # since a malicious actor could conceivably spam the log with
        # newer duplicate entries.
        oldest_entry: Optional[LogEntry] = None
        for result in results:
            entry = LogEntry._from_response(result)
            if (
                oldest_entry is None
                or entry.integrated_time < oldest_entry.integrated_time
            ):
                oldest_entry = entry

        return oldest_entry


class RekorClient:
    """The internal Rekor client"""

    def __init__(self, url: str, pubkey: bytes, ct_keyring: CTKeyring) -> None:
        """
        Create a new `RekorClient` from the given URL.
        """
        self.url = urljoin(url, "api/v1/")
        self.session = requests.Session()
        self.session.headers.update(
            {"Content-Type": "application/json", "Accept": "application/json"}
        )

        pubkey = serialization.load_pem_public_key(pubkey)
        if not isinstance(
            pubkey,
            ec.EllipticCurvePublicKey,
        ):
            raise RekorClientError(f"Invalid public key type: {pubkey}")
        self._pubkey = pubkey

        self._ct_keyring = ct_keyring

    def __del__(self) -> None:
        """
        Terminates the underlying network session.
        """
        self.session.close()

    @classmethod
    def production(cls, updater: TrustUpdater) -> RekorClient:
        """
        Returns a `RekorClient` populated with the default Rekor production instance.

        updater must be a `TrustUpdater` for the production TUF repository.
        """
        rekor_key = updater.get_rekor_key()
        ctfe_keys = updater.get_ctfe_keys()

        return cls(DEFAULT_REKOR_URL, rekor_key, CTKeyring(ctfe_keys))

    @classmethod
    def staging(cls, updater: TrustUpdater) -> RekorClient:
        """
        Returns a `RekorClient` populated with the default Rekor staging instance.

        updater must be a `TrustUpdater` for the staging TUF repository.
        """
        rekor_key = updater.get_rekor_key()
        ctfe_keys = updater.get_ctfe_keys()

        return cls(STAGING_REKOR_URL, rekor_key, CTKeyring(ctfe_keys))

    @property
    def log(self) -> RekorLog:
        """
        Returns a `RekorLog` adapter for making requests to a Rekor log.
        """
        return RekorLog(urljoin(self.url, "log/"), session=self.session)
