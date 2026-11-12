# Vinted Cerca

Cerca un articolo su Vinted partendo dal prezzo più basso e tiene solo gli annunci che sono davvero l'oggetto cercato.

Per ogni annuncio controlla titolo e descrizione e scarta in automatico:

- **accessori**: cinturini, cover, pellicole, caricatori, scatole vuote, "per Apple Watch", "compatibile con…"
- **rotti o guasti**: rotto, guasto, non funziona, malfunzionamento, crepato, bloccato iCloud, per ricambi…
- **falsi**: replica, fake, falso, contraffatto, taroccato, 1:1, "tipo Apple Watch"…
- **modelli sbagliati**: cercando "Apple Watch Series 9" scarta Series 8, SE, Series 10.

Capisce le negazioni: "mai rotto", "nessun difetto", "non è un falso" non fanno scartare l'annuncio.
Se un difetto riguarda solo un accessorio ("cinturino rotto") l'annuncio resta, con un avviso.

Il risultato è una pagina con gli annunci validi dal più economico, più l'elenco degli scartati con il motivo, così puoi controllare che i filtri abbiano lavorato bene.

## Come si usa

1. Nel repository apri **Actions**, poi **Cerca su Vinted**, poi **Run workflow**.
2. Scrivi cosa cerchi ed eventualmente prezzo minimo, massimo e parole da escludere. Premi **Run workflow**.
3. Dopo 2–3 minuti i risultati sono su `https://TUONOME.github.io/NOMEREPO/`.
   La tabella compare anche subito nella pagina dell'esecuzione, in Actions (sezione Summary).

Consiglio: metti un **prezzo minimo** realistico (es. 80 € per un Apple Watch). Vinted mostra prima centinaia di cinturini da 5 €: lo script li scarta comunque, ma così arriva prima agli orologi.

**Parole da escludere**: utile per varianti simili. Esempio: cercando "iphone 13 pro" scrivi `max` per togliere i Pro Max.

## Se Vinted blocca GitHub

Vinted a volte rifiuta le richieste che arrivano dai server di GitHub. In quel caso l'esecuzione in Actions finisce con un errore che lo spiega. Puoi fare la stessa ricerca dal PC Windows:

1. Installa Python da python.org, spuntando "Add python.exe to PATH".
2. Su GitHub premi **Code**, poi **Download ZIP**, ed estrai la cartella.
3. Doppio clic su `cerca.bat`, scrivi la ricerca e i prezzi. I risultati si aprono nel browser.

Da riga di comando: `python vinted_cerca.py "apple watch series 9" --prezzo-min 80 --prezzo-max 300`

## Modificare i filtri

Tutte le parole sono in `filtri.json` e si possono cambiare direttamente da GitHub (apri il file, matita, salva):

- `accessori_nel_titolo` e `solo_accessorio`: cosa indica un accessorio
- `guasti` e `falsi`: cosa fa scartare un annuncio
- `negazioni`: parole che annullano un guasto ("mai rotto")
- `sinonimi`: forme equivalenti ("series" = "serie" = "s9")
- `soglia_prezzo_accessorio`: sotto questa frazione della mediana, un titolo ambiguo viene trattato come accessorio (0.4 = 40%)

Una parola che fa parte della ricerca non viene mai usata per scartare: cercando "apple watch rotto" i rotti restano.

## Limiti

Vinted non ha un'API pubblica: lo script usa quella del sito, che può cambiare. Se smette di funzionare va aggiornato `vinted_cerca.py`. I prezzi sono senza spedizione.
