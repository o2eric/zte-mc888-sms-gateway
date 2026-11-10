# Architektura i eksploatacja

Klient HTTP → token i adres IP → walidacja odbiorców, polityki i deduplikacji →
modem ZTE. Stan i kolejka znajdują się w prywatnym katalogu. Opcjonalny
`sms_inbound.py` przetwarza przychodzące wiadomości i jest domyślnie wyłączony.
`zte_api.py` obsługuje komunikację z modemem.

## Przygotowanie

1. Uruchom testy i piaskownicę według [README](../README.md). Test OpenAPI wymaga
   PyYAML i jsonschema, a na niektórych systemach potrzebne jest `tzdata`.
2. Przygotuj prywatny plik na podstawie `config.example.json`. Dodaj aliasy i
   klientów z uprawnieniami; przykładowa struktura znajduje się w `sandbox.py`.
   Nie używaj przykładowych numerów i tokenów produkcyjnie.
3. Ustaw `SMSGW_CONFIG`, `SMSGW_STATE_DIR`, `ZTE_URL` i poświadczenie modemu
   (`ZTE_PASS` albo `ZTE_ENV_FILE`). Ogranicz dostęp do konfiguracji i stanu.
4. Zacznij z `live: false`. Serwer domyślnie słucha na 127.0.0.1:8095.
   Przy dostępie przez sieć użyj TLS przez reverse proxy i ogranicz dostęp.
5. Po weryfikacji konfiguracji włącz `live` i wykonaj kontrolowany test ze swoim
   odbiorcą. Potwierdzenie modemu nie jest dowodem doręczenia.

## Utrzymanie i odzyskiwanie

Chroń tokeny, hasło modemu, treść wiadomości i kopie stanu. Ustal retencję i
kopie przed wdrożeniem. Uszkodzony stan zatrzymuje pracę zamiast go usuwać;
przywracaj z zaufanej kopii po zbadaniu problemu. Deduplikacja ogranicza
duplikaty, lecz nie gwarantuje dokładnie jednokrotnej dostawy przy awarii.

MC888 ma jeden slot sesji administratora: inne narzędzia mogą przerywać sesję.
Kanał przychodzący może wywoływać skonfigurowane akcje; przed włączeniem
sprawdź nadawców, uprawnienia i skutki. Przed aktualizacją wykonaj kopię kodu
i konfiguracji oraz przetestuj stan usługi i wysyłkę próbną.
