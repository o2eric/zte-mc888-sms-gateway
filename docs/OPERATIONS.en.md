# Architecture and operations

[English](OPERATIONS.en.md) | [Polski](OPERATIONS.md)

HTTP client → token and IP → recipient, policy and deduplication checks → ZTE
modem. State and queue live in a private directory. Optional `sms_inbound.py`
processes incoming messages and is disabled by default. `zte_api.py` handles
modem communication.

## Setup

1. Run tests and the sandbox as described in the [README](../README.md). The
   OpenAPI test needs PyYAML and jsonschema; some systems need `tzdata`.
2. Prepare a private file based on `config.example.json`. Add aliases and
   authorized clients; `sandbox.py` shows the structure. Do not use sample
   numbers or tokens in production.
3. Set `SMSGW_CONFIG`, `SMSGW_STATE_DIR`, `ZTE_URL` and the modem credential
   (`ZTE_PASS` or `ZTE_ENV_FILE`). Restrict access to configuration and state.
4. Start with `live: false`. The server defaults to 127.0.0.1:8095. For
   network access use TLS through a reverse proxy and restrict access.
5. After checking configuration, enable `live` and perform a controlled test
   with your own recipient. Modem confirmation does not prove delivery.

## Maintenance and recovery

Protect tokens, modem credentials, message content and state backups. Define
retention and backup policies before deployment. Corrupt state stops processing
instead of being deleted; investigate and restore a trusted backup. Deduplication
reduces duplicates but cannot guarantee exactly-once delivery during failures.

MC888 has one administrator session: other tools can interrupt it. Inbound
processing can run configured actions; review senders, permissions and effects
before enabling it. Back up code and configuration before updating, and verify
service status and a test send afterward.
