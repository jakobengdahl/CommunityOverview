# Manual Testing Checklist

Denna lista används för manuell verifiering innan `dev` mergas till `preview`.
Uppdateras automatiskt när PRs som kräver manuell verifiering mergas till `dev`.

---

## Hur listan används

1. Kör igenom relevanta avsnitt efter att en batch features mergeats till `dev`
2. Bocka av och signera med datum i en kopia — behåll inte bockar i denna fil
3. Om ett test misslyckas: öppna ett issue och blockera merge tills det är löst

---

## Säkerhet

### CORS-konfiguration (PR #56)
**Syfte:** Verifiera att CORS-policyn inte tillåter credentials med wildcard-origin.

- [ ] Starta appen utan `CORS_ALLOWED_ORIGINS` satt (default `*`)
- [ ] Skicka en cross-origin-request med credentials från en annan origin (t.ex. via browser devtools eller curl med `Origin`-header)
- [ ] Verifiera att `Access-Control-Allow-Credentials` **saknas** i svaret
- [ ] Sätt `CORS_ALLOWED_ORIGINS=https://din-testdomän.com` och starta om
- [ ] Skicka request från den tillåtna originen → credentials-header ska vara present
- [ ] Skicka request från en **otillåten** origin → inga CORS-headers i svaret

### SSRF-skydd i webhook-leverans (PR #103)
**Syfte:** Verifiera att webhook-delivery blockerar anrop till interna/privata IP-adresser.

- [ ] Skapa en `EventSubscription`-nod med en webhook-URL till ett publikt test-endpoint (t.ex. `https://webhook.site/...`)
- [ ] Utlös en graph-mutation → verifiera att webhook levereras (kontrollera endpoint)
- [ ] Skapa en `EventSubscription` med URL `http://127.0.0.1:8000/health`
- [ ] Utlös en mutation → verifiera att leveransen loggas som `DROPPED` (inte försökt)
- [ ] Testa även med `http://169.254.169.254/` (AWS metadata-endpoint)
- [ ] **Känd begränsning:** DNS rebinding-angrepp (TOCTOU) kräver infra-nivå egress-filtrering för full mitigering

### Autentisering på execute_tool (PR #39)
**Syfte:** Verifiera att `/execute_tool` kräver auth för skrivoperationer.

- [ ] Starta appen med `AUTH_ENABLED=false` (eller inte satt)
- [ ] `POST /execute_tool {"tool_name": "add_nodes", "arguments": {...}}` utan credentials → förväntat: **403 Forbidden**
- [ ] `POST /execute_tool {"tool_name": "search_graph", "arguments": {...}}` utan credentials → förväntat: **200 OK**
- [ ] Starta om med `AUTH_ENABLED=true` och `AUTH_PASSWORD` satt
- [ ] Samma write-anrop med giltiga credentials → **200 OK**
- [ ] Verifiera att "Save View"-knappen i chat-panelen fortfarande fungerar (använder nu `api.addNodes` internt)

---

## UI / Frontendflöden

### EventSubscription och Agent-noder (PR #142)
**Syfte:** Verifiera att noder av dessa typer visas korrekt i grafen direkt efter skapande.

- [ ] Klicka "+" och välj `EventSubscription` → noden ska dyka upp i grafen **utan sidladdning**
- [ ] Klicka "+" och välj `Agent`, länka till en EventSubscription → Agent-nod och kant syns direkt
- [ ] Öppna node-type-dropdownen → `EventSubscription` och `Agent` ska finnas i listan
- [ ] Fråga chat-assistenten om EventSubscription-noder → assistenten svarar med kännedom om typen
- [ ] Öppna `/stats`-endpointen (`GET /api/stats`) med EventSubscription/Agent-noder i grafen → inget krasch, korrekt räkning

---

## Lägg till nya avsnitt här

När en PR som kräver manuell testning mergas till `dev`, lägg till ett avsnitt med:
- PR-nummer och en rad beskrivning
- Checklista med konkreta steg och förväntade utfall
