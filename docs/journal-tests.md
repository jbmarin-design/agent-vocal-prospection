# Journal de tests

Une ligne par test. Indiquer la version de prompt (`avp prompt show <campagne>`) dès la phase 2.

| Date | Phase | Test | Version prompt | Conditions | Résultat | Validé |
|---|---|---|---|---|---|---|
| 19/10/2026 | 2 | Simulations en texte (`avp simulation ehpad`, 20 scénarios ×2) | — | Haiku 5.5, prospect et analyse Sonnet 5.5 | 39/40 conformes (écart : longueur d'une réplique) | oui |
| 20/10/2026 | 2 | Appels téléphone JB : décroché du décideur, dictée et répétition d'email, fin d'appel | — | Haiku 5.5 | OK après corrections (réplique instantanée, lettres en français, raccrochage sur confirmation) | oui |
| 20/10/2026 | 3 | Import Axonaut « EHPAD 2026 » / « Prospect ciblé » (société de test 52300106) | — | pipeline créé par JB, étapes alignées | 1 prospect créé, téléphone et ville retenus | oui |
| 20/10/2026 | 4 | Appel réel n°1 (barrage) puis n°2 (rappel → RDV) sur la société de test | — | `avp call prospect 1` | Contact créé, opportunité Prospect contacté puis r1 (80 %), fiche enrichie, événements lisibles | oui |
| 20/10/2026 | 4 | RDV via Axonaut (sans Google OAuth) | — | API events nature 1 et 2 + send | RDV visible dans Google Agenda de JB, email reçu | oui |
| 20/10/2026 | 5.1 | Tests automatiques planificateur, créneaux, fériés, issues | — | pytest | 52/52 | oui |
| 20/10/2026 | 5.2 | Simulation DRY_RUN, campagne test-perso, 3 numéros | — | base isolée `data/phase5` | 2 appels par tour, le 3e au tour suivant ; reprogrammés J+2 sans tentative ; arrêt propre | oui |
| 20/10/2026 | 5.3 | Fin de plage horaire / jour de fermeture | — | DRY_RUN | « en pause : hors plage horaire » à l'heure de fin ; « en pause : fermeture OpteoLink » | oui |
| 20/10/2026 | 5.4 | Opposition ajoutée avant lancement | — | DRY_RUN | numéro jamais appelé, prospect exclu | oui |
| 20/10/2026 | 5.6 | LiveKit arrêté | — | DRY_RUN=false, faux numéro | dispatch en échec, appel « erreur », prospect à rappeler +15 min sans tentative, démon et worker se rétablissent seuls | oui |
| 20/10/2026 | 5.4 | Appel réel RDV complet (4c200870) | — | prospect 1, email de test | RDV jeudi 22 à 11 h 30, RDV Axonaut 130716110, email de confirmation envoyé à jbmarin@lesbranches.tech, raccrochage auto après la confirmation, prospect terminé | oui |
| 20/10/2026 | 5.4 | Appel réel non décroché (6c5094d9) | — | prospect 1 | issue non_decroche, 1 tentative consommée, rappel le 21/10 à 11 h 24 (+24 h) | oui |
| 20/10/2026 | 5.4 | Appel réel vers la messagerie (0e9066d2, appel rejeté → renvoi messagerie) | — | prospect 1 | AMD machine-vm, issue repondeur, raccrochage à 14 s sans message, rappel à +48 h | oui |
| | 5.4 | Occupé : non reproductible sur portable (rejet = messagerie) ; couvert par les tests automatiques | | | à faire depuis un fixe si besoin | |
| | 5.4 | Issues réelles : rappel daté, opposition | | appels sur le portable de JB | à faire | |
| | 5.5 | Concurrence réelle (2 appels simultanés max) | | au moins 2 numéros | à faire | |
| | 5.6 | Worker arrêté (appels orphelins), redémarrage du démon pendant un appel | | | à faire | |
