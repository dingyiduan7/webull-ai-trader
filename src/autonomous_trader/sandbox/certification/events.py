"""Bounded read-only gRPC collector for a single certification process."""
from datetime import datetime, timezone
import json
import queue
import threading
import grpc
from webull.trade.events.types import EVENT_TYPE_ORDER
from ..streams import signed_order_subscription, pb, rpc, GRPC_HOST


def decode_event(event):
    # The official SDK handles application events outside the protobuf enum.
    # In particular ORDER=1024 cannot be passed to EventType.Name().
    kind = "ORDER" if event.eventType == EVENT_TYPE_ORDER else next(
        (name for name, code in pb.EventType.items() if code == event.eventType), "UNKNOWN_EVENT_TYPE")
    payload = json.loads(event.payload) if event.payload and event.contentType == "application/json" else None
    return {"event_type": kind, "event_type_code": event.eventType,
        "subscribe_type": event.subscribeType, "source_timestamp_ms": str(event.timestamp),
        "received_at": datetime.now(timezone.utc).isoformat(), "payload": payload}


class OrderEvents:
    def __init__(self, credentials):
        self.credentials = credentials
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.queue = queue.Queue(maxsize=100)
        self.call = None
        self.result = "NOT_STARTED"
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            with grpc.secure_channel(GRPC_HOST + ":443", grpc.ssl_channel_credentials(), options=(
                    ("grpc.enable_http_proxy", 0), ("grpc.enable_retries", 0), ("grpc.max_receive_message_length", 1_000_000))) as channel:
                grpc.channel_ready_future(channel).result(timeout=5)
                request, metadata = signed_order_subscription(self.credentials)
                self.call = rpc.EventServiceStub(channel).Subscribe(request, metadata=metadata, timeout=180)
                for event in self.call:
                    decoded = decode_event(event)
                    self.queue.put_nowait(decoded)
                    if event.eventType == pb.SubscribeSuccess:
                        self.ready.set()
                    if event.eventType in (pb.AuthError, pb.NumOfConnExceed, pb.SubscribeExpired):
                        self.result = decoded["event_type"]
                        break
                    if decoded["event_type"] == "UNKNOWN_EVENT_TYPE":
                        self.result = "UNKNOWN_EVENT_TYPE_RECONCILE_ONLY"
                        break
        except grpc.RpcError as exc:
            self.result = exc.code().name
        except Exception as exc:
            self.result = "COLLECTOR_ERROR_" + type(exc).__name__  # no exception text/metadata
        finally:
            self.stopped.set()

    def start(self):
        self.thread.start()
        self.ready.wait(8)
        return self.healthy()

    def healthy(self):
        return self.ready.is_set() and not self.stopped.is_set()

    def drain(self):
        rows = []
        while True:
            try:
                rows.append(self.queue.get_nowait())
            except queue.Empty:
                return rows

    def close(self):
        if self.call is not None:
            self.call.cancel()  # Subscription only, never an order cancellation.
        self.thread.join(timeout=5)
