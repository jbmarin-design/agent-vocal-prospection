# Journal des remarques vocales

Remarques sur la voix, la prononciation et le rythme, notées au fil des tests téléphoniques
**sans les corriger aussitôt** : on les traite par lot, tous les 4 ou 5 tests (décision de JB,
20/10/2026). Pour chaque point : l'appel, ce qu'on entend, ce qu'on voudrait.

Outil : des échantillons de la voix de Sarah peuvent être générés sur la VM pour choisir une
écriture à l'oreille (voir la section « Échantillons » en bas).

## À traiter au prochain lot

| Date | Appel | Constat | Souhait |
|---|---|---|---|
| 20/10 | 38fbde2a | « Hamelin » épelé « h a m… » par le prospect, transcrit en bribes ; Sarah a répété « a, m, e, l, i, n » sans le H et le prospect a validé | Garde-fou : si le nom noté et l'email ne concordent pas, RDV « à confirmer » |
| 20/10 | — | Voix jugée encore un peu robotique dans l'ensemble | Comparer d'autres voix Cartesia / ElevenLabs et la vitesse (`TTS_SPEED`) |

## Déjà corrigé

| Date | Constat | Correction |
|---|---|---|
| 19/10 | Lettres isolées lues à l'anglaise (« I » → « aïe ») | Nom des lettres écrit en français avant la voix (`LETTER_NAMES_FR`) |
| 20/10 | « EHPAD » épelé « E, H, P, A, D » | Écrit « épade » (`PRONUNCIATIONS_FR`) |
| 20/10 | Lettre E : « eu » entendu « u », « euh » muet | Écrite « eux » (choix à l'écoute) |
| 20/10 | « .fr » lu d'un bloc | Dit « èf èr » |

## Échantillons

Script de synthèse avec la voix et les réglages de Sarah (Cartesia, depuis le conteneur
`agent-worker`) ; les fichiers sont écrits dans `data/voix/` sur la VM. Une écriture testée et
retenue s'ajoute à `LETTER_NAMES_FR` ou `PRONUNCIATIONS_FR` dans `src/avp/agent/logic.py`.
