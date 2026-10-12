# Daytrader

En daytrading-bot som kjører på GitHub, også når PC-en er av. Den handler på en testkonto med late-penger (10 000 kr) og følger kursene hvert minutt mens amerikanske børser er åpne.

**Nettside:** `https://<brukernavn>.github.io/<repo>/`. Den viser status, kjøp og salg med begrunnelse, og har knapper og innstillinger.

## Slik virker det
- `.github/workflows/daytrader.yml` starter boten hver time 13:07–21:07 UTC på hverdager. Hver økt sjekker kursene hvert minutt i opptil 70 minutter, og neste økt tar over.
- Alt boten vet ligger på grenen **data**: `state.json` (boten), `config.json` (innstillinger), `commands.json` (knappetrykk fra nettsiden).
- Nettsiden ligger i `docs/` og publiseres med GitHub Pages.

## Strategier
1. **Pullback:** opptrend over VWAP, kursen faller tilbake til støtte og snur opp.
2. **Feiltest:** kursen bryter under støtte, men lukker over igjen (falskt brudd).
3. **Åpningsbrudd:** kursen bryter over toppen fra de første 30 minuttene på høyt volum.

Risiko: 1 % av kontoen per handel, stop-loss under bunnen, mål på 2 × risiko, maks 3 handler og 3 % tap per dag. Alt selges før børsen stenger.

## Salg
- Boten går gjennom hvert 5-minutters lys siden kjøpet. Rører kursen stop eller mål mellom to sjekker, blir det likevel solgt på riktig nivå.
- Åpne posisjoner passes på selv om boten er slått av.
- En posisjon som ble liggende over natten selges ved første mulighet neste dag.
- Knappene **Selg** og **Selg alt** på nettsiden selger med en gang (innen ca. 1–2 minutter mens boten kjører).
- Valgfritt: flytt stop opp til kjøpskursen når kursen har steget 1 R.

## Varsler på telefonen
Legg et hemmelig ntfy-emne under **Settings → Secrets and variables → Actions → New repository secret** med navnet `NTFY_TOPIC`. Abonner på samme emne i ntfy-appen.

## Viktig
- Repoet må være **offentlig**, ellers tar boten mer gratis GitHub-tid enn det som følger med. Testkontoen og handlene blir dermed synlige for alle.
- GitHub kan starte øktene noen minutter for sent når det er travelt.
- Kursene kommer gratis fra Yahoo. Hvis Yahoo stenger for GitHub, viser nettsiden en rød melding.
