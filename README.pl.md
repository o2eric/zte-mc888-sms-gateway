# ZTE MC888 SMS gateway

[English](README.md) | [Polski](README.pl.md)

Samodzielna bramka HTTP do wysyłania SMS przez modem ZTE MC888, z kolejką,
deduplikacją i opcjonalnym, domyślnie wyłączonym kanałem odbioru. To kandydat
roboczy: przed wdrożeniem wymagany jest przegląd bezpieczeństwa i konfiguracji.
Nie jest częścią żadnego portalu użytkownika ani nie zawiera danych konkretnego
konta czy instalacji.

## Dokumentacja

- [Pierwszy test i API](docs/API.md)
- [Architektura, konfiguracja i eksploatacja](docs/OPERATIONS.md)
- [Specyfikacja OpenAPI](docs/openapi.yaml)
- [Pochodzenie i ograniczenia](docs/PROVENANCE.md)

## Szybki test bez modemu

Potrzebny jest Python z obsługą `zoneinfo`. Uruchom w katalogu projektu:

```sh
python sandbox.py 8095
```

Piaskownica nasłuchuje na `127.0.0.1`, tworzy tymczasową konfigurację i zastępuje
modem atrapą. Nie wysyła prawdziwych SMS-ów. Tokeny w piaskownicy są fikcyjne.

```sh
python -m unittest -q test_sms_gateway test_sms_inbound test_otp test_otp_policy
python -m unittest -q test_api_contract
```

Test kontraktu wymaga dodatkowo `PyYAML` i `jsonschema`; testy bramki używają
biblioteki standardowej. W systemie bez bazy stref czasowych może być potrzebny
pakiet `tzdata`.

## Konfiguracja i uruchomienie

1. Skopiuj `config.example.json` do prywatnego katalogu. Domyślnie `live` to
   `false`, lista odbiorców i klientów jest pusta, a odbiór SMS jest wyłączony.
2. Określ odbiorców jako aliasy, numery w lokalnej konfiguracji oraz klientów z
   dozwolonymi aliasami, adresami źródłowymi i skrótami tokenów. Nie umieszczaj
   rzeczywistych numerów, konfiguracji, tokenów ani haseł w repozytorium.
3. `python sms_gateway.py --new-token` tworzy token i jego skrót SHA-256.
   Token przekaż klientowi bezpiecznym kanałem, a w konfiguracji klienta zapisz
   tylko skrót. Polecenie wyświetla token: zadbaj o prywatność terminala.
4. Ustaw `SMSGW_CONFIG`, `SMSGW_STATE_DIR` i, jeśli potrzebny jest tryb starszego
   klienta, `SMSGW_TOKEN_FILE`. Katalog stanu i pliki konfiguracji chroń przed
   innymi użytkownikami. `SMSGW_BIND` domyślnie wynosi `127.0.0.1`;
   `SMSGW_PORT` domyślnie `8095`.
5. `ZTE_URL` wskazuje modem. Hasło administratora podaj przez `ZTE_PASS` lub
   prywatny plik wskazany przez `ZTE_ENV_FILE`. Nie wpisuj go do kodu ani do
   przykładu konfiguracji. Modem ma jeden współdzielony slot sesji administratora.
6. Uruchom `python sms_gateway.py`. Najpierw sprawdź zachowanie w trybie
   `live: false`; prawdziwą wysyłkę włącz dopiero po sprawdzeniu odbiorców,
   dostępu i kosztów swojego operatora.

Konkretną usługę systemową, dostawcę TLS i politykę kopii należy skonfigurować
w docelowym środowisku. Serwer HTTP nie zapewnia samodzielnie TLS: przy dostępie
z innych maszyn użyj zaufanego reverse proxy, ogranicz adresy i chroń tokeny.

## Kontrakt i dane

`POST /v1/notify` przyjmuje klucz deduplikacji, tekst i aliasy odbiorców.
`GET /v1/status` udostępnia stan, a `GET /v1/result/<key>` wynik żądania;
szczegóły pól i odpowiedzi opisuje `docs/openapi.yaml`. Poświadczenia klientów
mają oddzielne uprawnienia. Bramkę ograniczają m.in. limity dzienne, deduplikacja
i godziny ciszy; dokładny wynik zależy od konfiguracji i priorytetu.

Status `sent` oznacza potwierdzenie przez modem, a `sent_unconfirmed` przyjęcie
bez potwierdzonego stanu; żaden nie stanowi raportu doręczenia na telefon.
`unknown` oznacza niepewny wynik przekazania do modemu: nie jest automatycznie
ponawiany, a ponowne wysłanie wymaga sprawdzenia możliwości duplikatu.
Przeterminowane zadania kolejki są pomijane przed wysyłką, a tryb próbny
nie blokuje późniejszej rzeczywistej wysyłki tego samego klucza.
Uszkodzenie pliku stanu zatrzymuje obsługę zamiast kasować kolejkę.
Profil `otp_only` nie ma liczbowego limitu wysyłek; niezależne ograniczenia
prób logowania pozostają po stronie klienta.
Zwykłe wiadomości mogą być zapisywane w historii i kolejce, dlatego trzeba
chronić katalog stanu oraz ustalić retencję i kopie. Profil `otp_only` redaguje
treść kodu i nie odkłada jej do kolejki ponowień. Opcjonalne
`otp_text_pattern` i `otp_key_pattern` określają dopuszczalny format; niepoprawne
wyrażenie kończy się odmową. Ustawia je zaufany administrator w konfiguracji.

Kanał przychodzący jest domyślnie wyłączony. Może wykonywać skonfigurowane
komendy lub wywoływać webhooki, więc przed włączeniem wymaga osobnego przeglądu
uprawnień, nadawców i konsekwencji wykonania.

## Zakres wydania

Nie potwierdzono jeszcze instalacji E2E na odizolowanym urządzeniu ZTE ani
pełnego pochodzenia klienta modemu; zobacz [pochodzenie](docs/PROVENANCE.md).
Nie dołączono prywatnego portalu ani konfiguracji działającej instalacji.

## Powiązany projekt

[Play24 API](https://github.com/o2eric/play24-api) to niezależny, nieoficjalny
klient Play24. Aplikacja może używać bramki do wysyłania własnych powiadomień
lub kodów weryfikacyjnych. Bramka nie pobiera automatycznie kodów Play24
dostarczanych na telefon.

## Licencja i wsparcie

Kod przygotowano do udostępnienia na [licencji MIT](LICENSE). Znak ZTE należy
do odpowiedniego właściciela; projekt jest niezależny od producenta.

Jeśli projekt Ci pomaga, możesz [wesprzeć o2eric na BuyCoffee](https://buycoffee.to/o2eric).
