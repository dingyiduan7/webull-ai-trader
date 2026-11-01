"""Bounded, read-only SDK streaming probes. No order is needed or created."""
import json
import time
from datetime import datetime, timezone
from uuid import uuid4
import grpc
from google.protobuf.json_format import MessageToDict
from webull.data.data_streaming_client import DataStreamingClient
from webull.trade.events import events_pb2 as pb, events_pb2_grpc as rpc
from webull.trade.events.signature_composer import calc_signature
from webull.core.auth.algorithm import sha_hmac256_new
from webull.data.quotes.subscribe import message_pb2 as market_pb
from .http import HTTP_HOST, MQTT_HOST, GRPC_HOST
from .quiet import quiet_sdk


class ProtobufObservationDecoder:
    """Preserve SDK wire protobuf fields without lossy convenience projections."""
    def __init__(self, message_type):
        self.message_type = message_type

    def parse(self, payload):
        result = self.message_type()
        result.ParseFromString(payload)
        return result


def signed_order_subscription(credentials):
    # Official documented order subscription bit 1. SDK 3.0.0's convenience
    # client prints signed metadata and hardcodes 7, so use its proto/signer
    # directly with order-only scope and a deadline-controlled SDK stub.
    request = pb.SubscribeRequest(subscribeType=1, timestamp=int(time.time() * 1000),
                                  accounts=[credentials.account_id])
    with quiet_sdk():
        _, metadata = calc_signature(credentials.app_key, credentials.app_secret, request, sha_hmac256_new)
    return request, metadata


@quiet_sdk()
def grpc_probe(credentials, sanitizer, duration=8):
    result = {"host": GRPC_HOST, "port": 443, "tls": True, "attempts": []}
    if not credentials.account_id:
        result["blocked"] = "DESIGNATED_ACCOUNT_REQUIRED"
        return result
    # Exactly one initial connection and one controlled reconnection. No retries
    # after authentication/transport failure, and no fabricated order event.
    for attempt in range(2):
        state = {"attempt": attempt + 1, "transport_ready": False, "subscription_confirmed": False, "events": []}
        result["attempts"].append(state)
        try:
            with grpc.secure_channel(GRPC_HOST + ":443", grpc.ssl_channel_credentials(),
                    options=(("grpc.enable_http_proxy", 0), ("grpc.enable_retries", 0))) as channel:
                grpc.channel_ready_future(channel).result(timeout=5)
                state["transport_ready"] = True
                request, metadata = signed_order_subscription(credentials)
                call = rpc.EventServiceStub(channel).Subscribe(request, metadata=metadata, timeout=duration)
                try:
                    for event in call:
                        event_kind = pb.EventType.Name(event.eventType)
                        state["events"].append({"event_type": event_kind, "subscribe_type": event.subscribeType,
                            "payload": sanitizer.clean(json.loads(event.payload)) if event.contentType == "application/json" and event.payload else None})
                        if event.eventType == pb.SubscribeSuccess:
                            state["subscription_confirmed"] = True
                        if event.eventType in (pb.AuthError, pb.NumOfConnExceed, pb.SubscribeExpired):
                            state["stop_reason"] = event_kind
                            break
                        if len(state["events"]) >= 10:
                            break
                finally:
                    call.cancel()
        except grpc.FutureTimeoutError:
            state["result"] = "TRANSPORT_TIMEOUT"
        except grpc.RpcError as exc:
            state["result"] = exc.code().name  # never details/metadata/error string
        except Exception as exc:
            state["result"] = "STREAM_ERROR_DETAILS_SUPPRESSED"
            state["exception_type"] = type(exc).__name__
            if isinstance(getattr(exc, "error_code", None), int):
                state["error_code"] = exc.error_code
        if (not state["subscription_confirmed"] or state.get("stop_reason")
                or state.get("result") not in (None, "DEADLINE_EXCEEDED")):
            break
        if attempt == 0:
            time.sleep(3.2)
    return result


@quiet_sdk()
def mqtt_probe(credentials, http, sanitizer, duration=8):
    result = {"host": MQTT_HOST, "port": 1883, "tls": True, "attempts": []}
    for attempt in range(2):
        state = {"attempt": attempt + 1, "connection_confirmed": False, "subscription_confirmed": False, "messages": []}
        result["attempts"].append(state)
        client = DataStreamingClient(credentials.app_key, credentials.app_secret, "us", uuid4().hex,
            http_host=HTTP_HOST, mqtt_host=MQTT_HOST, mqtt_port=1883, tls_enable=True)
        client._api_client = http  # guard the SDK's HTTP subscription request too
        client.disable_logger()
        client.register_payload_decoder("snapshot", ProtobufObservationDecoder(market_pb.Snapshot))
        client.register_payload_decoder("quote", ProtobufObservationDecoder(market_pb.Quote))
        def connected(c, api, session_id):
            state["connection_confirmed"] = True
            c.subscribe(["AAPL"], "US_STOCK", ["QUOTE", "SNAPSHOT"])
        def subscribed(*args):
            state["subscription_confirmed"] = True
        def message(c, topic, payload):
            if len(state["messages"]) >= 10:
                return
            # The official protobuf preserves original names/decimal strings.
            # Do not synthesize nulls for unsupported SDK wrapper objects.
            data = MessageToDict(payload, preserving_proto_field_name=True) if hasattr(payload, "DESCRIPTOR") else payload
            if not isinstance(data, (dict, list)):
                state["payload_issue"] = "UNRECOGNIZED_PAYLOAD_SHAPE"
                return
            state["messages"].append({"topic": topic if topic in ("quote", "snapshot", "notice") else "other",
                                      "received_at": datetime.now(timezone.utc).isoformat(),
                                      "payload": sanitizer.clean(data)})
        client.on_connect_success = connected
        client.on_subscribe_success = subscribed
        client.on_quotes_message = message
        try:
            # Avoid connect_and_loop_forever: it initializes token caches/logs and
            # can retry indefinitely. This invokes the SDK connect/decoder path.
            client._quotes_connect(MQTT_HOST, 1883)
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                rc = client.loop(timeout=1)
                if rc:
                    state["loop_result"] = int(rc)
                    break
        except Exception as exc:
            state["result"] = "STREAM_ERROR_DETAILS_SUPPRESSED"
            state["exception_type"] = type(exc).__name__
            if isinstance(getattr(exc, "error_code", None), int):
                state["error_code"] = exc.error_code
        finally:
            client.disconnect()
            client._sock_close()
        if not state["subscription_confirmed"] or state.get("result") or state.get("loop_result"):
            break
        if attempt == 0:
            time.sleep(3.2)
    return result
