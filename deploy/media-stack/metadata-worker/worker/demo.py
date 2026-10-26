"""Deterministic, offline demonstration. Never connects to a real service."""
import tempfile
from worker import PROTOCOL, Store, Worker


class DemoCoordinator:
    def __init__(self):
        self.assigned = False
        self.received = {}
        self.simulate_lost_ack = True

    def request(self, endpoint, payload=None):
        common = {"protocol": PROTOCOL}
        if endpoint == "/capabilities":
            return {**common, "enabled": True, "role": "metadata-only", "kinds": ["series"],
                    "exclusive_assignments": True, "server_only_import": True,
                    "min_request_interval_seconds": 3}
        if endpoint == "/claim":
            task = {"job_id": "demo:series:tt0000001", "lease_id": "demo-lease-1", "generation": "demo-v1",
                    "kind": "series", "imdb": "tt0000001"}
            tasks = [] if self.assigned else [task]
            self.assigned = True
            return {**common, "tasks": tasks, "retry_after_seconds": 10}
        if endpoint == "/renew":
            return {**common, "job_id": payload["job_id"], "lease_id": payload["lease_id"], "active": True}
        if endpoint == "/result":
            result = payload["result"]
            key = result["result_id"]
            duplicate = key in self.received
            self.received[key] = result
            if self.simulate_lost_ack:
                self.simulate_lost_ack = False
                raise OSError("Simulated connection loss after server acceptance")
            return {**common, "result_id": key, "ack": "duplicate" if duplicate else "accepted"}
        raise AssertionError("Unexpected demonstration endpoint")


def run_demo():
    calls = []
    def provider(kind, imdb):
        calls.append(imdb)
        return {"id": imdb, "type": kind, "name": "Offline demonstration show", "videos": [
            {"id": imdb + ":1:1", "season": 1, "episode": 1, "name": "Demonstration episode"}]}
    print("OFFLINE DEMONSTRATION: no server, provider, media or Jellyfin access.")
    with tempfile.TemporaryDirectory() as folder:
        api = DemoCoordinator()
        store = Store(folder)
        worker = Worker({"kind": "series"}, store, api, provider)
        worker.capabilities()
        worker.step()  # claim
        worker.step()  # fetch into durable outbox
        original = store.first()[1]["result_id"]
        try:
            worker.step()  # accepted remotely, acknowledgement lost
        except OSError:
            print("Connection interruption simulated; fetched metadata remains in the local outbox.")
        store.close()
        store = Store(folder)
        worker = Worker({"kind": "series"}, store, api, provider)
        assert store.first()[1]["result_id"] == original
        worker.step()  # retry same result, no second provider fetch
        assert store.first() is None and len(calls) == 1 and len(api.received) == 1
        assert store.summary()["completed"] == 1
        store.close()
    print("PASS: restart recovery, one provider fetch, one server result, duplicate acknowledgement handled.")
    print("LAN mode remains unconfigured until the server coordinator is installed in a later phase.")
    return 0
