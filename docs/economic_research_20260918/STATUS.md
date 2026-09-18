# Stato verificabile dell'implementazione

## Eseguito

Il ramo `fix/v7-economic-research-20260918` implementa la proiezione contabile dei
FINAL aggregati sui fill originali, il contesto maker letto dalla sola autorità
nativa, il controllo quantità/minimo/limiti, la registrazione del flusso consumato,
un simulatore causale e strumenti di inferenza, freeze e ripristino.

La copia congelata del ledger reale contiene sei fill e cinque mercati risolti.
Dopo la correzione: cinque chiusure attribuite, zero chiusure non attribuite,
una sola posizione ancora aperta. PnL realizzato PAPER invariato: 2,390715 USD.
La differenza di arrotondamento nella riconciliazione è inferiore a 1e-14 USD.

Il manager dispone di una modalità opzionale di settlement asincrono. I crediti
irrisolti restano impegnati nell'allocazione; nessun riavvio o cambio mercato può
liberarli senza un FINAL coerente con i fill e realmente scritto nel ledger.
Il cap maker predefinito e i limiti monetari non vengono aumentati.

Il controllo ChatGPT orario in sola lettura è stato creato. Non modifica codice,
parametri, rischio, ordini o autorizzazioni.

## Non completato e non dichiarato

Questo documento non certifica un deployment. Il live verificato resta `c27afd15`.
Il ramo concorrente all-crypto modifica alcuni degli stessi file: va integrato,
non sovrascritto. Il rollout richiede verifiche sulla combinazione finale.

T1–T8 sono preregistrati e gli strumenti esistono, ma i confronti empirici
completi non sono stati conclusi. Cinque mercati risolti e due blocchi orari
non dimostrano un vantaggio economico. Il report restituisce INCONCLUSIVE.

Il modello probabilistico è implementato e testato su fixture, non addestrato e
validato su un campione reale sufficiente. Il simulatore è una ricostruzione
prudente sul flusso locale, non prova della reale coda o latenza dell'exchange.

È stata verificata e ripristinata una copia di ledger/stati, non l'intero archivio
storico dei feed. L'offload continuo di tutti i dati e la persistenza dei feed
attraverso i cambi mercato restano da completare.

## Ripresa senza ricominciare

Leggere `checkpoint.json`, stato PR #1144, SHA live, e la nota di coordinamento
sul Mac. Non cambiare parametri all'interno della baseline. Usare il modulo
`research/economic/run.py` soltanto su dataset verificati. Non convertire dati
mancanti in zero e non promuovere candidati su risultati scelti a posteriori.
