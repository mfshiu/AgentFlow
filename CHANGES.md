# Changelog

All notable changes to AgentFlow (distribution `mas-agentflow`, import package `agentflow`) are recorded here, newest first.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versions are date-based: `YYYY.M.D`, the release date without leading zeros, optionally followed by a build suffix (as in `2025.8.22.1933`).
2026.10.13 is the first release with a git tag; earlier versions were published untagged.

**2026.10.13 contains breaking changes for users of 2025.5.24 and 2025.8.22.1933.** Read *Breaking changes* below before upgrading;
coming from 2025.5.24, also read the notes under 2025.8.22.1933. Numbers such as (#4) are issues or pull requests in
[mfshiu/AgentFlow](https://github.com/mfshiu/AgentFlow); RFC-0NN documents are in `docs/rfc/`.

## Unreleased

### Breaking changes

- `broker_type: redis` and `broker_type: ros` name reserved brokers that refuse to start: the agent logs "Broker not available" and does not activate. Before, it activated, ran `on_activate` and silently dropped every message (#42).
  Migration: use `mqtt`, or `empty` for a broker that drops everything (e.g. in tests).

### Added

- `BrokerNotAvailableError`, a `NotImplementedError`, exported from `agentflow.broker` (#42).

### Fixed

- `agentflow.logistic.logistic` can be imported; it imported a top-level `agent` module (#41).

### Packaging

- The wheel no longer ships `agentflow.broker.dds_broker` and `agentflow.broker.ros_noetic_broker`, which could not work; the drafts are in `contrib/brokers/` (#42). CI imports every module of the installed wheel.

## 2026.10.13 - 2026-10-13

Changes since 2025.8.22.1933: the reliability work of RFC-001 to RFC-013 (July-August 2026) and the fixes from the
October 2026 audit (merged pull requests #80-#109, RFC-014 and RFC-015).

### Breaking changes

#### Messaging

- Handlers run on a bounded pool of 8 daemon threads with a 1024-message queue instead of one new thread per message; when the queue is full the newest message is dropped (RFC-004). `publish_sync()` inside a handler stays safe: replies use a lane of their own (#7).
  Migration: size it with `dispatch.workers` / `dispatch.queue_capacity`; `dispatch.mode: per_message_thread` restores the old model but is deprecated.
- `Agent.publish()` returns `True` / `False` instead of `None`, and still never raises for broker errors; an invalid `qos` raises `ValueError` (#4).
  Migration: check the result where a lost message matters.
- Payloads larger than `codec.max_payload_bytes` (default 16 MiB) are dropped without decoding, and received payloads waiting for a handler may not exceed `dispatch.max_queued_bytes` (default 256 MiB) (#34).
  Migration: raise these limits if you send larger messages.
- MqttBroker refuses a publish once `max_pending_bytes` (default 64 MiB) are unsent or unacknowledged: `Agent.publish()` returns `False`, `publish_sync()` raises `MqttBackpressureError` (#35).
  Migration: raise `max_pending_bytes` in the broker config for bursty senders.
- Holon control messages, `publish_sync()` requests and automatic replies are sent at QoS 1; every message carries a `message_id`, and a redelivered id (window of 4096) is dropped before any handler runs (#4).
  Migration: none for MQTT 3.1.1 brokers; subscribe services with `qos=1` for QoS 1 end to end, and keep handlers idempotent.
- Envelopes whose `version` is missing, not an integer or outside 1-3 are dropped and counted in `decode_failure_count`, as are payloads without an AgentFlow prefix, now without a traceback per message (#15).
  Migration: none for AgentFlow senders.
- Invalid subscription filters (e.g. `a+b/c`, `a/#/b`) raise `ValueError` from `Agent.subscribe()` and `MqttBroker.subscribe()` before anything is registered, connected or not (#53).
  Migration: use `+` and `#` only as whole topic levels, `#` only last.
- `Agent.subscribe()` / `unsubscribe()` on a topic held by a running `publish_sync()` call, and `publish_sync()` on a topic owned by a `subscribe()` handler, raise `TopicWaitCollisionError` (RFC-006, RFC-007).
  Migration: keep subscription topics and reply topics apart.

#### Request/reply

- `publish_sync()` raises the broker's error at once (`MqttPublishError`, `MqttBackpressureError`, or `RuntimeError` without a broker) instead of a `TimeoutError` after the full timeout (RFC-002, RFC-012).
  Migration: catch these as well as `TimeoutError`.
- Automatic replies are sent only for topics with a handler registered by `subscribe(topic, topic_handler=...)`; a request that falls through to `on_message()` gets no reply (RFC-003).
  Migration: register an explicit handler for every request topic.
- Replies never carry `topic_return`, and a handler exception no longer mutates the incoming Parcel: the error reply is a new Parcel (RFC-003).
  Migration: none, unless a handler kept its input Parcel and relied on that mutation.
- `publish_sync()` no longer writes the reply topic into the caller's Parcel; a late reply on a generated reply topic is dropped and counted in `late_reply_count` instead of reaching `on_message()`; `timeout=None` is deprecated (#21).
  Migration: pass a finite timeout, and do not read `topic_return` back after the call.
- Requests carry a `correlation_id`, and `publish_sync()` ignores replies to other requests (RFC-014, #36). Overlapping calls that share a `topic_wait` with a responder older than 2026.10.13 now time out rather than risk taking each other's reply.
  Migration: upgrade responders first.

#### MQTT broker

- The client reconnects automatically after a connection loss (back-off 1-30 s) and restores its subscriptions; each connect attempt times out after 2 s instead of paho's 5 s (#2).
  Migration: `reconnect: false` restores the old behaviour; tune `connect_timeout`.
- The client sends a client id, `'{name}-{agent_id:.12}'` by default, instead of a zero-length one; refusals that a retry cannot fix (bad credentials, not authorised, id rejected) raise `MqttConnectRefused`, and the agent stops retrying (#17).
  Migration: update broker ACLs keyed on client ids, or set the `client_id` template.
- `MqttBroker` instances are single-shot: `start()` on an instance that is not NEW raises `RuntimeError`, and so does `stop()` while STARTING; `start(options, *, startup_timeout_s=None)` and `stop(graceful_timeout_s=5.0)` are bounded and return `bool` (RFC-010, RFC-011).
  Migration: create a new broker for each start (the Agent already does).
- `MqttBroker.publish()` raises `MqttPublishError` when the broker is not running or not connected, or when paho rejects the message, instead of returning a failed `MessageInfo` (RFC-012).
  Migration: catch `agentflow.broker.MqttPublishError` when calling the broker directly; `Agent.publish()` handles it.
- `MessageBroker.publish()` / `subscribe()` take keyword-only `qos` (and `retain` for publish). While disconnected, `MqttBroker.subscribe()` / `unsubscribe()` only update the registry replayed on the next CONNACK; after `stop()` they do nothing (RFC-005, #4).
  Migration: subclasses of the built-in brokers must accept the new keywords.

#### Lifecycle and workers

- Workers are single-shot: a second `start()` raises `RuntimeError`. `ProcessWorker.stop()` escalates to terminate and kill after 5 + 2 + 1 s and returns the exit code; `ThreadWorker.stop()` returns `False` (state `STOP_TIMEOUT`) if the thread does not exit in 5 s (RFC-008, RFC-009).
  Migration: create a new Agent to run it again.
- `start()` without an explicit `CONCURRENCY_TYPE` emits a `FutureWarning` (the default will become `'thread'`), and `agent.config` lacks that key until then; messaging and data calls on a process-mode controller, which never reach the child, emit a `RuntimeWarning` (#9).
  Migration: call `start_thread()` or `start_process()`, and do messaging inside the agent's hooks and handlers.

#### Holons

- Children re-send `register_child` on every (re)connect, and parents announce themselves with a new internal `parent_online` subject; `on_register_child` / `on_register_parent` fire once per member (#6).
  Migration: in mixed-version fleets, upgrade parents (including mid-tier agents) before children.
- `terminate` is honoured only from a registered parent, `register_parent` only on the child's own channel, and automatic replies are never published to holon control topics (#13).
  Migration: tools that stop agents by publishing on `to_child.{name}` need `holon.trust_unregistered_parents: true`; set `holon.control_secret` on every agent of a holon or on none.
- Parent messages carry the sender's `parent_name`; an agent drops its own echoed broadcasts and messages from senders not named like its parent, so name-addressed messages no longer reach grandchildren (#5, #38).
  Migration: none, unless you relied on that addressing.
- `_children` / `_parents` are replaced, not mutated, when a new member registers; a raising `on_register_child` no longer suppresses `register_parent` or `on_children_message` (#57).
  Migration: re-read the attribute instead of keeping a reference to it.

#### Security

- `BinaryParcel` (pickle) payloads are decoded with a restricted unpickler: plain data only, plus immutable types listed in `codec.pickle_allowed_globals`; protocol 0-2 payloads that contain bytes are refused (#1).
  Migration: allowlist types such as `datetime.datetime`, or send plain data; `codec.accept_pickle: false` refuses pickle entirely.
- An automatic error reply names only the exception type, e.g. `"KeyError (details are in the responder's log)"` (#68).
  Migration: `errors: {expose_details: true}` restores `str(ex)`; `publish_sync(..., raise_on_error=True)` raises `RemoteError` instead of returning the error reply.
- `agentflow.core.wrapper` was removed; it was unused, broken, and contained an unrestricted `pickle.loads` (#1).
  Migration: none.

#### Packaging

- `paho-mqtt>=2.0,<3` is a declared dependency (#45).
  Migration: replace a pinned paho-mqtt 1.x.

### Added

- `Agent.unsubscribe(topic)` and `MessageBroker.unsubscribe(topic)`, a non-abstract no-op by default (RFC-001).
- `agent_config` is optional: `Agent(name)` works, and agents created without a config no longer share one default dict (#75).
- `Agent.is_connected()` and the hooks `on_disconnected()` / `on_reconnected()`, run on a short-lived daemon thread; `BrokerNotifier._on_disconnect(planned)`, non-abstract (#2).
- `qos=` on `Agent.publish()` / `subscribe()` (#4); `publish_sync(..., raise_on_error=False)` and `RemoteError` with `topic`, `error` and `reply` (#68).
- Agent counters: `decode_failure_count` (#15), `late_reply_count` (#21), `publish_backpressure_count` (#35).
- Agent config sections, validated at construction (unknown keys and wrong types raise):
  - `codec`: `accept_pickle` (default `True`), `pickle_allowed_globals` (`[]`), `max_payload_bytes` (16 MiB) (#1, #34);
  - `errors`: `expose_details` (`False`) (#68);
  - `holon`: `control_secret` (unset; str or bytes of at least 16 bytes, signs control messages with HMAC-SHA256), `control_max_age` (300 s), `max_children` (unlimited), `trust_unregistered_parents` (`False`) (#13).
- `dispatch` config: `workers` (8), `queue_capacity` (1024), `shutdown_timeout_s` (5.0), `max_queued_bytes` (256 MiB), `mode` (`'bounded'`; `'per_message_thread'` is deprecated) (RFC-004, #34).
- MQTT broker options (the broker's config section):
  - `reconnect` (`true`), `reconnect_min_delay` / `reconnect_max_delay` (1 / 30 s), `connect_timeout` (2 s) (#2);
  - `client_id` (`'{name}-{agent_id:.12}'`), `clean_session` (`true`; `false` keeps subscriptions and QoS >= 1 messages across reconnects) (#17);
  - `max_pending_bytes` (64 MiB) (#35).
- `MqttBroker.recovery_metrics()` with `resubscribe_success_count`, `resubscribe_error_count`, `recovery_run_count`, `active_subscriptions`, `failed_subscriptions`, `connected`, `stopping`, `ever_connected`, `pending_publish_count` and `pending_publish_bytes` (RFC-005, #35, #53).
- `MqttBroker` properties `last_disconnect_was_planned`, `state`, `last_start_exception`, `last_stop_exception`, `start_generation` (RFC-005, RFC-010, RFC-011).
- Exceptions: `MqttPublishError` / `MqttPublishReason`, `MqttBackpressureError` and `PublishBackpressureError`, exported from `agentflow.broker` (RFC-012, #35); `MqttConnectRefused` with `reason_code` / `retryable` (#17); `TopicWaitCollisionError` (RFC-006); `ParcelDecodeError`, a `TypeError` (#1).
- `check_filter()` and `check_qos()` in `agentflow.broker.message_broker` (#4, #53).
- Optional envelope fields `correlation_id` (RFC-014) and `message_id` (RFC-015), written only when set and ignored by older receivers.
- `Parcel.from_payload()` keyword-only `accept_pickle` / `pickle_allowed_globals`, `parse_pickle_globals()` and `DEFAULT_PICKLE_ALLOWED_GLOBALS` (#1).
- A request the dispatcher drops is answered at once with an `"overloaded: ..."` error reply carrying its correlation id, so the requester need not wait out its timeout (#4).
- Messages with no handler yet that arrive while `on_activate()` / `on_connected()` are still running, e.g. from a resumed persistent session, are held (up to 4096, at most 10 s) and replayed once the hooks return (#17, PR #95).
- `WorkerState`; `ProcessWorker.state` / `exitcode`; `ThreadWorker.state`, `last_exception`, `thread_alive`, `thread_daemon`, `worker_thread_ident`, `requires_process_restart` (RFC-008, RFC-009, RFC-013).
- `Agent.terminate()` logs an ERROR when a wedged non-daemon worker thread will keep the interpreter from exiting (RFC-013).
- A failed process-mode start names the handlers, config values or attributes that cannot be pickled (RFC-008, #9).
- `agentflow.core.dispatcher.MessageDispatcher` with metrics (RFC-004), and `agentflow.core.holon_auth` (#13).
- Documentation: an `Agent` class docstring on thread versus process mode (#9); deployment guides in `docs/deployment/` (RFC-013); RFC-001 to RFC-015 in `docs/rfc/` and the architecture audit in `docs/audit/`.

### Fixed

- The MQTT client never reconnected after its first connection loss, leaving the agent deaf while `is_active()` stayed true; an exception in a connect callback killed paho's network thread (#2).
- Subscriptions are restored after a reconnect (RFC-005) and are sent when made before the first CONNACK (#54); an unsubscribe racing that replay now wins.
- A subscription the broker refused (SUBACK 0x80) or a failed resubscribe still counted as active; it is now logged, counted in `failed_subscriptions` and retried on the next CONNACK (#53).
- `MqttBroker.start()` and `stop()` had no time bound, and late paho callbacks after `stop()` or a failed start still reached the agent (RFC-010, RFC-011).
- `MqttBroker.publish()` ignored paho's return code (RFC-012).
- `publish_sync()` leaked a handler entry and a broker subscription on every call (RFC-001), and created a `ProcessWorker`, changing the multiprocessing start method, on an agent without a worker (#21).
- `publish_sync()` could return a late reply to an earlier call, and concurrent calls on one `topic_wait` overwrote each other's handler; they now share the topic and each gets its own reply (#36).
- Handler errors and broker self-echo could start endless reply loops (RFC-003); a burst of messages created one thread per message (RFC-004); subscribe, unsubscribe and dispatch raced on the handler registry (RFC-007).
- `ProcessWorker.stop()` / `ThreadWorker.stop()` could wait forever on a wedged child or thread (RFC-008, RFC-009); `Agent.is_active()` was always false in process mode (commit 072291d).
- The deprecated `per_message_thread` dispatcher's `stop()` always returned `True`; it now reports whether its handler threads have finished (#75).
- On Windows, a `ProcessWorker` start that failed to pickle the agent left an orphan child that broke the next spawn (#48).
- Importing `agentflow.core.agent` failed on hosts without tkinter (#47).
- Holon membership depended on start order and was lost on reconnects and parent restarts (#6); a mid-tier agent re-ran `terminate` on its own echo (#5); same-name replicas stopped each other (#38).
- A hook iterating `_children` while other children registered raised `dictionary changed size during iteration`, and a raising hook kept a child from learning its parent (#57).
- Undecodable messages were each logged with a full traceback; they are now counted and logged at a rate-limited WARNING (#15).
- Payloads were decoded on the network thread, delaying keepalives and every other message (#34).

### Security

- Remote code execution: `BinaryParcel` decoding ran `pickle.loads` on received bytes, so any client able to publish to a subscribed topic could run code in the agent (#1). `bytearray(<int>)` memory amplification is blocked too.
- Holon control plane: forged `terminate` and `register_child` messages, automatic replies redirected into control topics, and predictable reply topics, now generated with `secrets`. Opt-in HMAC-SHA256 signing via `holon.control_secret`, which is masked in config logs (#13).
- Error replies leaked exception messages (hosts, connection strings, credentials) to anyone subscribed to the reply topic (#68).
- Hostile input: oversized payloads are not decoded (#34); deeply nested JSON and huge `version` values are decode failures, and logged reasons and topics are escaped and length-capped (#15).
- Not addressed in this release: TLS (#3), and broker credentials printed in DEBUG config logs (#14).

### Packaging

- Released under the MIT License (`LICENSE.md`); earlier releases shipped an empty `LICENSE.md`.
- `install_requires` declares `paho-mqtt>=2.0,<3` (#45); 2025.8.22.1933 already needed paho-mqtt 2.x but declared no dependency.
- `python_requires` stays `>=3.11`; classifiers now name the CI-tested versions, Python 3.11 and 3.13 (#77).
- GitHub Actions CI: ruff and mypy, tests on Ubuntu and Windows with Python 3.11 and 3.13 and a 70% coverage floor, and a wheel build-and-import smoke test (#49).
- The repository root is no longer an importable package: a stray `__init__.py`, never part of the wheel, was removed (#76).

## 2025.8.22.1933 - untagged

- Untagged; set in `setup.py` by commit 65c9643 (2026-01-16). See git history. Upgrading from 2025.5.24: `on_begining` became `on_activating`, `on_began` was removed, `set_config2` no longer overwrites an existing key, and `MqttBroker` uses the paho-mqtt 2.x callback API.
- `agentflow.get_logger()`, `Logger.verbose` and `agentflow.ensure_size` (now in `agentflow.core.parcel`) were removed; modules log to `logging.getLogger(os.getenv('LOGGER_NAME'))`, the root logger when unset.

## 2025.5.24 - 2025-05-24

- Untagged (commit c685d5c); the version FlowDepot pins (`mas_agentflow==2025.5.24`). See git history.
