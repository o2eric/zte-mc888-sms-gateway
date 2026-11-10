# SMS gateway API

[English](API.en.md) | [Polski](API.md)

Request and response schemas are in [openapi.yaml](openapi.yaml) (Polish).
The API requires `Authorization: Bearer <token>` and an allowed client IP.
The examples below are fictional and work only in the sandbox.

## First request

Start `python sandbox.py 8095` in another terminal, then run:

```sh
curl -X POST http://127.0.0.1:8095/v1/notify \
  -H 'Authorization: Bearer demo-token' \
  -H 'Content-Type: application/json' \
  -d '{"key":"test-1","text":"Gateway test","to":["contact1"]}'
```

The sandbox simulates the modem and does not send SMS. Reusing the same key
and recipient can be treated as a duplicate. Add `[FAIL]` to test retries or
`[UNCONFIRMED]` to simulate an unconfirmed modem result.

```sh
curl -H 'Authorization: Bearer demo-token' http://127.0.0.1:8095/v1/status
curl -H 'Authorization: Bearer demo-token' http://127.0.0.1:8095/v1/result/test-1
```

`POST /v1/notify` returns a result per recipient. HTTP 200 alone does not
mean delivery. `sent` indicates modem confirmation, not a handset receipt.
`queued`, `sent_unconfirmed` and `unknown` require separate interpretation;
check for possible duplicates before retrying an uncertain send.
