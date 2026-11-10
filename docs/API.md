# API bramki SMS

[English](API.en.md) | [Polski](API.md)

Schematy żądań i odpowiedzi znajdują się w [openapi.yaml](openapi.yaml). API
wymaga nagłówka `Authorization: Bearer <token>` i dozwolonego adresu klienta.
Wartości poniżej są fikcyjne i działają wyłącznie w piaskownicy.

## Pierwsze żądanie

W osobnym terminalu uruchom `python sandbox.py 8095`. Następnie:

```sh
curl -X POST http://127.0.0.1:8095/v1/notify \
  -H 'Authorization: Bearer demo-token' \
  -H 'Content-Type: application/json' \
  -d '{"key":"test-1","text":"Test bramki","to":["contact1"]}'
```

Piaskownica symuluje modem i nie wysyła SMS-ów. Kolejne żądanie z tym samym
kluczem i odbiorcą może zostać uznane za duplikat. Dodaj `[FAIL]` do tekstu,
aby przetestować ponowienie, lub `[UNCONFIRMED]`, aby zasymulować brak
potwierdzenia modemu.

```sh
curl -H 'Authorization: Bearer demo-token' http://127.0.0.1:8095/v1/status
curl -H 'Authorization: Bearer demo-token' http://127.0.0.1:8095/v1/result/test-1
```

`POST /v1/notify` zwraca wynik dla każdego odbiorcy. Sam status HTTP 200 nie
oznacza doręczenia. `sent` oznacza potwierdzenie modemu, a nie raport telefonu.
`queued`, `sent_unconfirmed` i `unknown` wymagają osobnej interpretacji; nie
ponawiaj niepewnej wysyłki bez rozważenia duplikatu. Szczegóły są w specyfikacji.
