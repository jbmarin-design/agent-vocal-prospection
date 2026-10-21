# Journal des remarques vocales

Remarques sur la voix, la prononciation et le rythme, notées au fil des tests téléphoniques
**sans les corriger aussitôt** : on les traite par lot, tous les 4 ou 5 tests (décision de JB,
20/10/2026). Pour chaque point : l'appel, ce qu'on entend, ce qu'on voudrait.

Outil : des échantillons de la voix de Sarah peuvent être générés sur la VM pour choisir une
écriture à l'oreille (voir la section « Échantillons » en bas).

## À traiter au prochain lot

| Date | Appel | Constat | Souhait |
|---|---|---|---|
| — | — | — | — |

## Déjà corrigé

| Date | Constat | Correction |
|---|---|---|
| 19/10 | Lettres isolées lues à l'anglaise (« I » → « aïe ») | Nom des lettres écrit en français avant la voix (`LETTER_NAMES_FR`) |
| 20/10 | « EHPAD » épelé « E, H, P, A, D » | Écrit « épade » (`PRONUNCIATIONS_FR`) |
| 20/10 | Lettre E : « eu » entendu « u », « euh » muet | Écrite « eux » (choix à l'écoute) |
| 20/10 | « .fr » lu d'un bloc | Dit « èf èr » |
| 20/10 | « Pour l'invitation » | « Pour la confirmation » (52df178) |
| 20/10 | Nom connu redemandé | Seul l'email est demandé quand le nom est connu (52df178) |
| 20/10 | Domaine « lesbranches » dit « les, branchés » | Domaine propre à un établissement épelé (52df178) |
| 20/10 | « Hamelin » / jl.amelin@ validé | Garde-fou nom/email : RDV « à confirmer » + tâche (52df178) |
| 20/10 | Voix robotique : intonation monotone, pas d'accent ni de rythme dans la phrase (JB) | Choix à l'écoute le 21/10 (voir « Choix de la voix ») : modèle `sonic-3.6-2026-08-27`, voix Sarah, `TTS_SPEED=0.95`, écriture orale dans le prompt |
| 21/10 | Petit mot « Ok, d'accord. » = phrase close, puis 2e remerciement de Claude ~1 s après (appel 35ff905c) | « Alors… » en suspens (variante 4 choisie par JB), « merci… »/« alors » retirés en tête de réponse après le petit mot, consigne du prompt (`b8c9d55`) |

## Échantillons

Script de synthèse avec la voix et les réglages de Sarah (Cartesia, depuis le conteneur
`agent-worker`) ; les fichiers sont écrits dans `data/voix/` sur la VM. Une écriture testée et
retenue s'ajoute à `LETTER_NAMES_FR` ou `PRONUNCIATIONS_FR` dans `src/avp/agent/logic.py`.

## Choix de la voix (21/10/2026)

Deux séries d'échantillons de 3 répliques (accroche décideur, explication, relecture d'email), en
qualité téléphone (8 kHz), écoutées par JB :

1. Dix voix françaises féminines Cartesia en `sonic-3` (Sarah actuelle à 0,9 et 1,0, Sarah + émotion
   « content », Pauline, Mika, Eloise, Emmanuelle, Isabelle, Sophie, Léonie). Finalistes : **Sarah**
   (`90358bc7-3328-4b93-a942-d0447d7d4b5c`) et **Eloise** (`6c64b57a-bc65-48e4-bff4-12dbe85606cd`).
   Constat de JB : ce qui trahit l'IA n'est pas le timbre mais l'intonation monotone (pas d'accent sur
   les mots, pas de variation de rythme).
2. Sarah et Eloise en `sonic-3` puis `sonic-3.6` (modèle d'août 2026, qui fait varier rythme et
   intonation selon le sens), à 0,9 / 1,0, puis à 0,95 avec un texte écrit « à l'oral » (« Merci !
   Alors, … », points de suspension, « Est-ce que vous auriez… ? »), avec ou sans émotion « content ».
   Choix de JB : **4 et 5** (Sarah, `sonic-3.6`, 0,95, texte oral) ; retenu le 4, sans émotion
   (différence faible, pas de code en plus ; l'émotion reste possible, le plugin LiveKit l'accepte).

Mis en place :
- `.env` de la VM : `CARTESIA_MODEL=sonic-3.6-2026-08-27` (version datée, qui ne bouge plus ; l'alias
  `sonic-3.6` suivrait les mises à jour), `TTS_SPEED=0.95`, voix inchangée.
- Prompt (`prompts/base.md`, « Comment vous parlez ») : écrire comme on parle, la voix suit la
  ponctuation. Accroche décideur (`DECIDER_HOOK_LINE`, `prompts/decideur.md`) réécrite dans ce style.
- Déployé le 21/10 vers 9 h 30 : `.env` sauvegardé en `.env.bak-20261021-voix`, `AVP_VERSION=main`
  (`1a57560`, au lieu de `v0.1.0`). Synthèse en flux vérifiée par le plugin LiveKit : 1er son 0,7 s.
  Au passage : disque de la VM plein (cache de build Docker, 20 Go, inutile depuis la CI) → le worker
  ne démarrait plus ; `docker builder prune -af` (40 % utilisés ensuite).
- À écouter au prochain appel : l'accroche, les explications, la relecture d'email (les lettres
  épelées doivent rester nettes) et la latence du 1er son (sonic-3.6 à surveiller).

Complément du 21/10 (JB) : « une voix qui chante », on doit entendre le sourire au téléphone →
émotion `content` gardée finalement (`TTS_EMOTION=content`, nouveau réglage, `21945f2`), ton du prompt
« enjoué et avenant », exclamations permises (une par réplique, jamais face à un mécontentement),
ouverture « Bonjour ! Ici Sarah… », au revoir « Merci à vous, et très bonne journée ! ».

Le texte écrit compte autant que la voix : sans ponctuation orale, `sonic-3.6` reste plat.

