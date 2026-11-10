# ZTE MC888 SMS gateway

[English](README.md) | [Polski](README.pl.md)

An independent HTTP SMS gateway for a ZTE MC888 modem, with a queue,
deduplication and an optional incoming-message channel that is disabled by
default. Review its security and configuration before deployment.

## Documentation

- [API and first request](docs/API.en.md)
- [Architecture, setup and operations](docs/OPERATIONS.en.md)
- [OpenAPI specification (Polish)](docs/openapi.yaml)
- [Provenance and limitations](docs/PROVENANCE.en.md)

## Quick test without a modem

Python with `zoneinfo` is required:

```sh
python sandbox.py 8095
```

The sandbox listens on `127.0.0.1`, uses temporary configuration and a fake
modem, and never sends real SMS messages. Sandbox tokens are fictional.

```sh
python -m unittest -q test_sms_gateway test_sms_inbound test_otp test_otp_policy
python -m unittest -q test_api_contract
```

The API contract test also requires `PyYAML` and `jsonschema`; the gateway
tests use the standard library. Some systems need the `tzdata` package.

## Configuration and startup

1. Copy `config.example.json` to a private directory. By default `live` is
   `false`, recipients and clients are empty, and inbound SMS is disabled.
2. Define recipient aliases and local numbers, and clients with permitted
   aliases, source IP addresses and token hashes. Keep real phone numbers,
   configuration, tokens and passwords out of the repository.
3. `python sms_gateway.py --new-token` generates a token and its SHA-256 hash.
   Transfer the token securely and store only its hash in the client configuration.
   The command displays the token, so protect the terminal.
4. Set `SMSGW_CONFIG`, `SMSGW_STATE_DIR`, and, for a legacy client if needed,
   `SMSGW_TOKEN_FILE`. Protect configuration and state from other users.
   `SMSGW_BIND` defaults to `127.0.0.1` and `SMSGW_PORT` to `8095`.
5. Set `ZTE_URL` to the modem. Supply its administrator password via
   `ZTE_PASS` or a private file specified by `ZTE_ENV_FILE`. The modem has
   one shared administrator session.
6. Run `python sms_gateway.py`. Test with `live: false` first; enable real
   sending only after checking recipients, access and operator charges.

Configure the system service, TLS provider and backup policy for the target
environment. The HTTP server does not provide TLS itself: use a trusted reverse
proxy for remote access, restrict addresses and protect tokens.

## Contract and data

`POST /v1/notify` accepts a deduplication key, text and recipient aliases.
`GET /v1/status` returns status and `GET /v1/result/<key>` returns a request
result; field details are in the OpenAPI specification. Clients have separate
permissions. Daily limits, deduplication and quiet hours apply according to
configuration and priority.

`sent` indicates modem confirmation, and `sent_unconfirmed` acceptance without
confirmed status; neither is a handset delivery receipt. `unknown` means the
modem handoff result is uncertain and is not retried automatically. Check the
possibility of duplicates before sending again. Expired queued jobs are skipped.
Dry runs do not block a subsequent live request with the same key. A corrupted
state file stops processing rather than deleting the queue.

The `otp_only` profile has no numeric sending limit; clients still enforce
their own login-attempt limits. Ordinary messages may be stored in state and
queue history, so protect the directory and set retention and backup policies.
The `otp_only` profile redacts the code and does not put it in the retry queue.
Optional `otp_text_pattern` and `otp_key_pattern` restrict the format; an
invalid expression fails closed and must be configured by a trusted administrator.

The inbound channel is disabled by default. It can execute configured commands
or invoke webhooks; review sender permissions and effects before enabling it.

## Related project

[Play24 API](https://github.com/o2eric/play24-api) is an independent unofficial
Play24 client. An application may use this gateway to send its own notifications
or verification codes. The gateway does not automatically retrieve Play24
verification codes delivered to a handset.

## Release scope, license and support

An isolated-device end-to-end installation and the full origin of the modem
client have not been confirmed; see [provenance](docs/PROVENANCE.en.md).
The private parent portal and live installation configuration are not included.

The project is provided under the [MIT license](LICENSE) and is independent of
ZTE. If it helps you, you can [support o2eric on BuyCoffee](https://buycoffee.to/o2eric).
