"""Contract tests against an in-memory Engine; never contacts Docker."""
from copy import deepcopy

import pytest

from src.sandbox.docker_manager import SandboxError, SandboxPolicy
from src.sandbox import docker_engine_lifecycle as lifecycle_module
from src.sandbox.docker_engine_lifecycle import DockerEngineLifecycle, EngineResponse

IMAGE = "example.invalid/fixture@sha256:" + "a" * 64
CID = "b" * 64
ATTEMPT = "c" * 32


class Engine:
    def __init__(self):
        self.calls = []
        self.document = None
        self.lost_create = False
        self.delete_status = 204

    def request(self, method, path, *, body, timeout_seconds, max_response_bytes):
        assert 0 < timeout_seconds <= 10
        assert max_response_bytes == 65536
        self.calls.append((method, path, deepcopy(body), timeout_seconds))
        if method == "POST" and path.startswith("/containers/create?name="):
            name = path.split("=", 1)[1]
            self.document = {
                "Id": CID, "Name": "/" + name,
                "Config": deepcopy({k: v for k, v in body.items() if k != "HostConfig"}),
                "HostConfig": deepcopy(body["HostConfig"]),
                "Mounts": [], "State": {"Running": False},
            }
            if self.lost_create:
                raise TimeoutError("allocation outcome unknown")
            return EngineResponse(201, {"Id": CID, "Warnings": []})
        if method == "GET":
            return EngineResponse(404, {}) if self.document is None else EngineResponse(200, deepcopy(self.document))
        if method == "POST" and path.endswith("/start"):
            self.document["State"]["Running"] = True
            return EngineResponse(204, None)
        if method == "DELETE":
            if self.delete_status == 204:
                self.document = None
            return EngineResponse(self.delete_status, None)
        raise AssertionError((method, path))


def adapter(engine):
    return DockerEngineLifecycle(transport=engine, idle_entrypoint=("/bin/sleep", "2700"))


def test_security_payload_and_confirmed_lifecycle():
    engine = Engine()
    backend = adapter(engine)
    assert engine.calls == []
    policy = SandboxPolicy(IMAGE, 0.5, 128)
    backend.create(ATTEMPT, policy=policy, timeout_seconds=10)
    payload = engine.calls[0][2]
    host = payload["HostConfig"]
    assert payload["Image"] == IMAGE
    assert payload["User"] == "65534:65534"
    assert payload["Entrypoint"] == ["/bin/sleep", "2700"]
    assert payload["Env"] == [] and payload["Volumes"] == {}
    assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"] and host["CapAdd"] == []
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["NanoCpus"] == 500_000_000
    assert host["Memory"] == host["MemorySwap"] == 128 * 1024 * 1024
    assert host["PidsLimit"] == 128 and host["Binds"] == []
    backend.start(ATTEMPT, timeout_seconds=10)
    assert backend.state == "running"
    backend.remove(ATTEMPT, timeout_seconds=10)
    assert backend.state == "removed" and engine.document is None
    calls = [(m, p) for m, p, _, _ in engine.calls]
    assert calls[-2:] == [("DELETE", f"/containers/{CID}?force=true&v=false"),
                         ("GET", f"/containers/{CID}/json")]
    assert sum(m == "POST" and p.endswith("/start") for m, p in calls) == 1
    assert sum(m == "DELETE" for m, _ in calls) == 1
    before = len(engine.calls)
    with pytest.raises(SandboxError):
        backend.remove(ATTEMPT, timeout_seconds=10)
    assert len(engine.calls) == before


def test_unknown_create_cleanup_and_foreign_ownership_are_single_use():
    engine = Engine()
    engine.lost_create = True
    backend = adapter(engine)
    with pytest.raises(TimeoutError):
        backend.create(ATTEMPT, policy=SandboxPolicy(IMAGE, 1, 128), timeout_seconds=10)
    assert backend.state == "create_failed"
    with pytest.raises(SandboxError):
        backend.create(ATTEMPT, policy=SandboxPolicy(IMAGE, 1, 128), timeout_seconds=10)
    with pytest.raises(SandboxError):
        backend.remove("d" * 32, timeout_seconds=10)
    backend.remove(ATTEMPT, timeout_seconds=10)
    assert backend.state == "removed" and engine.document is None
    assert sum(m == "DELETE" for m, _, _, _ in engine.calls) == 1

    foreign = Engine()
    foreign.lost_create = True
    guarded = adapter(foreign)
    with pytest.raises(TimeoutError):
        guarded.create(ATTEMPT, policy=SandboxPolicy(IMAGE, 1, 128), timeout_seconds=10)
    foreign.document["Config"]["Labels"] = {"foreign": "owner"}
    with pytest.raises(SandboxError):
        guarded.remove(ATTEMPT, timeout_seconds=10)
    assert guarded.state == "remove_failed"
    assert not any(m == "DELETE" for m, _, _, _ in foreign.calls)
    before = len(foreign.calls)
    with pytest.raises(SandboxError):
        guarded.remove(ATTEMPT, timeout_seconds=10)
    assert len(foreign.calls) == before


def test_policy_drift_unconfirmed_delete_and_input_rejections(monkeypatch):
    engine = Engine()
    backend = adapter(engine)
    with pytest.raises((ValueError, SandboxError)):
        backend.create(ATTEMPT, policy=SandboxPolicy("mutable:latest", 1, 128), timeout_seconds=10)
    assert engine.calls == []
    backend.create(ATTEMPT, policy=SandboxPolicy(IMAGE, 1, 128), timeout_seconds=10)
    engine.document["HostConfig"]["Privileged"] = True
    with pytest.raises(SandboxError):
        backend.start(ATTEMPT, timeout_seconds=10)
    assert backend.state == "start_failed"
    assert not any(m == "POST" and p.endswith("/start") for m, p, _, _ in engine.calls)
    # Security drift must not prevent identity-verified teardown of our own container.
    backend.remove(ATTEMPT, timeout_seconds=10)
    assert backend.state == "removed"

    pending = Engine()
    unconfirmed = adapter(pending)
    unconfirmed.create(ATTEMPT, policy=SandboxPolicy(IMAGE, 1, 128), timeout_seconds=10)
    pending.delete_status = 202
    with pytest.raises(SandboxError):
        unconfirmed.remove(ATTEMPT, timeout_seconds=10)
    assert unconfirmed.state == "remove_failed" and pending.document is not None
    before = len(pending.calls)
    with pytest.raises(SandboxError):
        unconfirmed.remove(ATTEMPT, timeout_seconds=10)
    assert len(pending.calls) == before

    clock = [0.0]
    monkeypatch.setattr(lifecycle_module, "monotonic", lambda: clock[0])
    late = Engine()
    request = late.request

    def delayed(*args, **kwargs):
        reply = request(*args, **kwargs)
        clock[0] += 11
        return reply

    late.request = delayed
    expired = adapter(late)
    with pytest.raises(TimeoutError):
        expired.create(ATTEMPT, policy=SandboxPolicy(IMAGE, 1, 128), timeout_seconds=10)
    assert expired.state == "create_failed" and len(late.calls) == 1
    # Allocation happened but its late receipt was not accepted; name-based cleanup.
    late.request = request
    expired.remove(ATTEMPT, timeout_seconds=10)
    assert expired.state == "removed" and late.document is None
