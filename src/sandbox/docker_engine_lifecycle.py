"""Explicit Docker Engine wire protocol; no transport or execution backend bundled.

Trusted transport/daemon required. No retries, image pulls, sockets or subprocesses.
This does not implement exec, idle expiry or host-stage cleanup, so it is NOT a
complete SandboxBackend or a production-ready StagedAttempt lifecycle.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from math import isfinite
import re
from time import monotonic
from typing import Protocol
from uuid import uuid4

from src.sandbox.docker_manager import SandboxError, SandboxPolicy

_IMAGE = re.compile(r"[A-Za-z0-9._/:+-]+@sha256:[0-9a-f]{64}")
_ID = re.compile(r"[0-9a-f]{64}")
_OWNER = "org.oss-contributor.instance"
_ATTEMPT = "org.oss-contributor.attempt-sha256"


@dataclass(frozen=True)
class EngineResponse:
    status: int
    body: object = None


class EngineTransport(Protocol):
    """Version paths explicitly; enforce deadline/capture/JSON bounds in flight.

    No implicit retries, redirects or image pulls. The cap applies to raw response
    bytes, not just the decoded JSON. Authentication belongs to the trusted caller.
    """
    def request(
        self, method: str, path: str, *, body: dict | None,
        timeout_seconds: float, max_response_bytes: int,
    ) -> EngineResponse: ...


def _deadline(seconds: float) -> float:
    if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not isfinite(seconds) or not 0 < seconds <= 3600):
        raise SandboxError("finite operation timeout in (0, 3600] required")
    return monotonic() + seconds


def _left(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("Engine lifecycle deadline exhausted")
    return remaining


class DockerEngineLifecycle:
    """Single-caller, single-attempt create/start/remove wire state machine."""

    def __init__(
        self, *, transport: EngineTransport, idle_entrypoint: tuple[str, ...],
        pids_limit: int = 128, max_response_bytes: int = 65536,
    ) -> None:
        if not callable(getattr(transport, "request", None)):
            raise SandboxError("explicit Engine transport required")
        if (type(idle_entrypoint) is not tuple or not idle_entrypoint
                or len(idle_entrypoint) > 32
                or any(type(x) is not str or not x or "\0" in x for x in idle_entrypoint)
                or not idle_entrypoint[0].startswith("/")
                or sum(len(x.encode("utf-8")) for x in idle_entrypoint) > 8192):
            raise SandboxError("explicit bounded absolute idle entrypoint required")
        if type(pids_limit) is not int or not 16 <= pids_limit <= 512:
            raise SandboxError("pids_limit must be an integer in [16, 512]")
        if type(max_response_bytes) is not int or not 1024 <= max_response_bytes <= 1048576:
            raise SandboxError("invalid response cap")
        self._transport, self._entrypoint = transport, idle_entrypoint
        self._pids, self._cap = pids_limit, max_response_bytes
        self._owner = uuid4().hex
        self._state = "new"
        self._attempt: str | None = None
        self._name: str | None = None
        self._cid: str | None = None
        self._body: dict = {}

    @property
    def state(self) -> str:
        return self._state

    @property
    def container_id(self) -> str | None:
        """Last known immutable ID, including after an unconfirmed operation."""
        return self._cid

    @property
    def container_name(self) -> str | None:
        return self._name

    def _call(self, end: float, method: str, path: str, body: dict | None = None) -> EngineResponse:
        reply = self._transport.request(
            method, path, body=deepcopy(body), timeout_seconds=_left(end),
            max_response_bytes=self._cap,
        )
        _left(end)  # late success is an unknown outcome, not confirmation
        if (type(reply) is not EngineResponse or type(reply.status) is not int
                or not 100 <= reply.status <= 599):
            raise SandboxError("malformed Engine response")
        try:
            size = len(json.dumps(reply.body, ensure_ascii=True, allow_nan=False).encode("ascii"))
        except (TypeError, ValueError, RecursionError):
            raise SandboxError("invalid Engine JSON") from None
        if size > self._cap:
            raise SandboxError("Engine response exceeds decoded response cap")
        _left(end)
        return reply

    def _check(self, attempt_id: str) -> None:
        if type(attempt_id) is not str or attempt_id != self._attempt:
            raise SandboxError("wrong attempt ID")

    def _payload(self, attempt_id: str, policy: SandboxPolicy) -> dict:
        if (type(policy) is not SandboxPolicy or not _IMAGE.fullmatch(policy.image)
                or len(policy.image) > 512):
            raise SandboxError("digest-pinned pre-provisioned image required")
        if not 0 < policy.cpu_limit <= 64 or not 16 <= policy.memory_limit_mb <= 65536:
            raise SandboxError("unsupported CPU/memory policy")
        nano = round(policy.cpu_limit * 1_000_000_000)
        if nano < 1:
            raise SandboxError("CPU limit below Engine precision")
        memory = policy.memory_limit_mb * 1024 * 1024
        return {
            "Image": policy.image, "User": policy.user,
            "Entrypoint": list(self._entrypoint), "Cmd": [], "Env": [],
            "Volumes": {}, "WorkingDir": "/", "ExposedPorts": {},
            "Labels": {_OWNER: self._owner, _ATTEMPT: sha256(attempt_id.encode()).hexdigest()},
            "HostConfig": {
                "NetworkMode": "none", "ReadonlyRootfs": True,
                "Privileged": False, "CapDrop": ["ALL"], "CapAdd": [],
                "SecurityOpt": ["no-new-privileges:true"],
                "NanoCpus": nano, "Memory": memory, "MemorySwap": memory,
                "PidsLimit": self._pids, "Binds": [], "Mounts": [],
                "VolumesFrom": [], "PortBindings": {}, "Devices": [],
                "DeviceRequests": [], "DeviceCgroupRules": [], "Tmpfs": {},
                "PidMode": "", "IpcMode": "none", "UTSMode": "",
                "CgroupnsMode": "private", "Sysctls": {},
                "AutoRemove": False, "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            },
            "NetworkingConfig": {"EndpointsConfig": {}},
        }

    def _identity(self, doc: object) -> str:
        if type(doc) is not dict:
            raise SandboxError("inspect is not an object")
        cid, config = doc.get("Id"), doc.get("Config")
        if (type(cid) is not str or not _ID.fullmatch(cid)
                or (self._cid is not None and cid != self._cid)
                or doc.get("Name") != "/" + self._name
                or type(config) is not dict
                or config.get("Labels") != self._body["Labels"]):
            raise SandboxError("container identity/ownership mismatch; refusing operation")
        return cid

    def _security(self, doc: dict) -> None:
        config, host = doc["Config"], doc.get("HostConfig")
        if type(host) is not dict:
            raise SandboxError("inspect lacks HostConfig")
        # Empty list/dict can be encoded as null by Engine; non-empty is forbidden.
        for key in ("Image", "User", "Entrypoint", "WorkingDir"):
            if config.get(key) != self._body[key]:
                raise SandboxError("container configuration mismatch: " + key)
        for key in ("Cmd", "Env", "Volumes", "ExposedPorts"):
            if config.get(key):
                raise SandboxError("unexpected image defaults: " + key)
        for key, wanted in self._body["HostConfig"].items():
            actual = host.get(key)
            if wanted in ([], {}):
                valid = not actual
            else:
                valid = type(actual) is type(wanted) and actual == wanted
            if not valid:
                raise SandboxError("container security mismatch: " + key)
        if doc.get("Mounts"):
            raise SandboxError("container has unexpected mounts")

    def _inspect(self, end: float, *, secure: bool = False) -> dict | None:
        key = self._cid or self._name
        reply = self._call(end, "GET", f"/containers/{key}/json")
        if reply.status == 404:
            return None
        if reply.status != 200:
            raise SandboxError(f"inspect failed: HTTP {reply.status}")
        cid = self._identity(reply.body)
        self._cid = cid
        if secure:
            self._security(reply.body)
        return reply.body

    def create(self, attempt_id: str, *, policy: SandboxPolicy, timeout_seconds: float) -> None:
        if self._state != "new":
            raise SandboxError("create already consumed")
        if type(attempt_id) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", attempt_id):
            raise SandboxError("invalid attempt ID")
        end = _deadline(timeout_seconds)
        body = self._payload(attempt_id, policy)  # invalid input has no side effects
        self._attempt, self._body = attempt_id, body
        self._name = "oss-" + self._owner + "-" + sha256(attempt_id.encode()).hexdigest()[:24]
        self._state = "creating"
        try:
            reply = self._call(end, "POST", f"/containers/create?name={self._name}", body)
            if reply.status != 201 or type(reply.body) is not dict:
                raise SandboxError(f"create failed: HTTP {reply.status}")
            cid = reply.body.get("Id")
            if type(cid) is not str or not _ID.fullmatch(cid):
                raise SandboxError("create returned invalid container ID")
            self._cid = cid
            if reply.body.get("Warnings"):
                raise SandboxError("Engine warned about creation; refusing start")
            doc = self._inspect(end, secure=True)
            if doc is None or doc.get("State", {}).get("Running") is not False:
                raise SandboxError("new container must be confirmed stopped")
            _left(end)
        except BaseException:
            self._state = "create_failed"
            raise
        self._state = "created"

    def start(self, attempt_id: str, *, timeout_seconds: float) -> None:
        self._check(attempt_id)
        if self._state != "created":
            raise SandboxError("start requires unconsumed created state")
        end = _deadline(timeout_seconds)
        self._state = "starting"
        try:
            doc = self._inspect(end, secure=True)
            if doc is None or doc.get("State", {}).get("Running") is not False:
                raise SandboxError("container missing or unexpectedly running before start")
            reply = self._call(end, "POST", f"/containers/{self._cid}/start")
            if reply.status != 204:
                raise SandboxError(f"start failed: HTTP {reply.status}")
            doc = self._inspect(end, secure=True)
            if doc is None or doc.get("State", {}).get("Running") is not True:
                raise SandboxError("start not confirmed running")
            _left(end)
        except BaseException:
            self._state = "start_failed"
            raise
        self._state = "running"

    def remove(self, attempt_id: str, *, timeout_seconds: float) -> None:
        self._check(attempt_id)
        if self._state not in {"created", "running", "create_failed", "start_failed"}:
            raise SandboxError("remove unavailable or already consumed")
        end = _deadline(timeout_seconds)
        self._state = "removing"
        try:
            # Identity only: unsafe config drift must not prevent own-container cleanup.
            doc = self._inspect(end)
            if doc is not None:
                reply = self._call(end, "DELETE", f"/containers/{self._cid}?force=true&v=false")
                if reply.status not in (204, 404):
                    raise SandboxError(f"delete failed or pending: HTTP {reply.status}")
                if self._inspect(end) is not None:
                    raise SandboxError("container removal unconfirmed")
            _left(end)
        except BaseException:
            self._state = "remove_failed"
            raise
        self._state = "removed"
