# Créer et calibrer une campagne : guide pour Claude (et pour JB)

Ce guide est la **base documentaire** à lire avant de créer ou de retoucher une campagne de
prospection. Il explique ce que fait l'agent pendant un appel, ce qu'une campagne règle et ce
qu'elle ne règle pas, comment transformer un besoin métier en campagne, et comment la valider.

Fichiers liés :

| Fichier | Rôle |
|---|---|
| [`fiche-brief.md`](fiche-brief.md) | Questions à poser à JB avant d'écrire une campagne |
| [`modele-campagne.yaml`](modele-campagne.yaml) | Squelette commenté, à copier en `campaigns/<id>.yaml` |
| [`modele-simulations.yaml`](modele-simulations.yaml) | Squelette des scénarios de test, à copier en `simulations/<id>.yaml` |
| `campaigns/ehpad.yaml` | **Campagne de référence**, calibrée et validée au téléphone (octobre 2026) |
| `simulations/ehpad.yaml` | 20 scénarios de référence (barrages, objections, refus, RDV…) |
| `docs/prompts.md` | Les couches du prompt et les commandes de contrôle |
| `docs/journal-voix.md` | Ce qui sonne bien ou mal à l'oral (à respecter dans les textes) |

Méthode de travail de JB : il dicte (transcription approximative : « axonote » = Axonaut,
« Xivo » = XiVO) ; il veut comprendre le *pourquoi*, garder la main, et avancer par lots. Poser
**toutes** les questions du brief en une fois, proposer une campagne complète, puis corriger par lot.

---

## 1. Ce que fait l'agent pendant un appel

Sarah est une **assistante virtuelle** (Claude Haiku au téléphone, voix Cartesia, transcription
Deepgram, LiveKit + XiVO). Elle appelle pour le compte de Jean-Baptiste Marin (OpteoLink) et vise
en général un **rendez-vous** avec lui.

Déroulé d'un appel :

1. **Décroché** : le XiVO joue l'annonce d'enregistrement. Sarah dit la **phrase d'ouverture
   fixe** : « Bonjour ! Ici Sarah, l'assistante virtuelle d'OpteoLink, les télécoms pro à
   Toulouse. Pourrais-je parler à {contact ou cible}, s'il vous plaît ? » (`src/avp/prompts.py`).
2. **Standard** (`prompts/accueil.md`) : passer l'accueil, ou récupérer nom, email, ligne directe
   et disponibilités du décideur, ou convenir d'un rappel.
3. **Décideur** (`prompts/decideur.md`) : accord pour deux minutes → **accroche** de la campagne
   → 2 ou 3 questions de **qualification** → **objections** → **objectif** (RDV, rappel, transfert).
   Si le décideur décroche lui-même (« C'est moi-même »), une réplique fixe enchaîne aussitôt.
4. **Fin d'appel** : au revoir fixe, raccrochage automatique.
5. **Après l'appel** (Claude Sonnet, `prompts/analyse_post_appel.md`) : résumé, score
   chaud/tiède/froid, réponses aux questions de qualification, contacts, RDV relevé → écrits dans
   **Axonaut** (opportunité déplacée d'étape, événement avec transcription, RDV + email de
   confirmation, tâches). Le prochain essai est planifié selon l'issue.

Garde-fous automatiques, **hors campagne, non négociables** : annonce IA dès la 1re phrase à
toute personne (AI Act art. 50), annonce d'enregistrement, liste d'opposition (« ne m'appelez
plus » traité sans Claude), plages horaires, durée maximale, pas de prix ni de promesse chiffrée,
pas de mensonge, un seul rebond par objection puis on conclut. Voir `prompts/base.md` § Règles.

Contraintes de l'oral qui pèsent sur **tous** les textes de campagne :

- répliques de 30 mots au plus, phrases de 20 mots, une idée et une question à la fois ;
- l'agent parle d'abord, la personne ne l'attendait pas : il faut être utile en 10 secondes ;
- la voix suit la ponctuation (modèle `sonic-3.6`) : écrire comme on parle, « En fait, … »,
  « Est-ce que vous auriez… ? », points de suspension avant l'info importante, un « ! » de bonne
  humeur de temps en temps ;
- nombres en toutes lettres (« dix-huit ans ») ; sigles du métier tels quels, sinon en clair ;
  un sigle qui se dit comme un mot (« EHPAD » → « épade ») s'ajoute à `PRONUNCIATIONS_FR`
  (`src/avp/agent/logic.py`) après écoute.

## 2. Ce qu'une campagne règle, et ce qu'elle ne règle pas

À chaque appel, le prompt est assemblé ainsi (`build_instructions`, voir `avp prompt show`) :

```
prompts/base.md            identité, façon de parler, règles non négociables     ← commun
prompts/accueil.md + decideur.md   déroulé standard puis décideur (agent unique) ← commun
section campagne           générée depuis campaigns/<id>.yaml                    ← LA CAMPAGNE
fiche prospect             nom, ville, contact connu, notes, emails déjà connus  ← import
contexte                   date, heure, tentative, créneaux de RDV calculés      ← automatique
```

La campagne règle : la cible, l'offre, l'accroche, les questions, les objections, les critères de
score, les consignes propres au secteur, les plages d'appel, le nombre d'essais, les règles de
RDV, le mapping Axonaut, les mots-clés de transcription.

**Limites actuelles (à connaître avant de calibrer une cible très différente)** : les prompts
communs ont été écrits pour la campagne EHPAD et contiennent encore des tournures qui supposent
une offre d'**audit télécom gratuit sur place** :

- `decideur.md` : « je vous appelle … au sujet des télécoms de votre établissement », « Proposer
  l'audit », « Jean-Baptiste peut passer faire le point gratuitement, sur place » ;
- `decideur.md` : « {{entreprise}} se présente … un acteur local basé à Toulouse, dix-huit ans » ;
- `base.md` § « OpteoLink en quelques mots » : arguments généraux (local, toutes les télécoms,
  interlocuteur unique, santé) valables pour toutes les campagnes ;
- la réplique fixe `DECIDER_HOOK_LINE` (`src/avp/prompts.py`) : « au sujet des télécoms de votre
  établissement ».

Pour une campagne proche (santé, mairies, cabinets : télécoms, audit ou RDV sur place), c'est
compatible. Pour une offre différente (visio, démonstration, produit précis, cible non
« établissement »), **le signaler à JB** : il faudra d'abord rendre ces passages paramétrables
(champ de campagne) plutôt que de contredire le prompt commun dans `instructions_specifiques`.
Les `instructions_specifiques` priment sur les consignes de rôle, mais un texte qui contredit le
prompt commun donne un agent hésitant.

## 3. Les champs d'une campagne, un par un

Schéma : `Campaign` dans `src/avp/models.py` (validé au chargement ; `avp check` signale les
erreurs). Chaque champ ci-dessous : ce qu'il fait pendant l'appel → comment l'écrire.

| Champ | Effet | Comment l'écrire |
|---|---|---|
| `id` | Nom du fichier, clé en base | minuscules, chiffres, tirets ; **identique au nom du fichier** |
| `nom` | Titre lu par Claude, rapports | « Secteur année — offre » |
| `actif` | Le démon appelle si `true` | `false` tant que la campagne n'est pas validée |
| `objectif` | `rdv`, `transfert` ou `qualification` | `rdv` dans presque tous les cas |
| `cible.interlocuteur` | Qui Sarah demande au standard (si aucun contact connu) | Fonction au féminin et masculin, telle qu'on la dit : « le directeur ou la directrice » |
| `cible.alternatives` | Personnes acceptables à défaut | 2 à 4 fonctions, de la plus à la moins pertinente |
| `offre.resume` | Ce que Sarah *comprend* de l'offre (jamais récité) | Le constat du métier, puis l'offre, puis la démarche concrète. Texte de fond, 10-20 lignes |
| `offre.points_forts` | Arguments à piocher | Une phrase par bénéfice **pour le client**, pas pour OpteoLink |
| `offre.preuves` | Faits citables tels quels | **Uniquement validés par JB** : ni chiffre, ni nom de client inventé |
| `accroche` | Dite après l'accord du décideur | 1 ou 2 phrases : un constat que le prospect reconnaît, puis **une question ouverte** |
| `qualification` | Guide de découverte (2-3 posées au plus) | 5 à 8 questions ; `id` en snake_case ; `but` = ce qu'on veut savoir ; `obligatoire: true` pour 2 ou 3 prioritaires |
| `objections` | Réponses conseillées | 8 à 12 objections réelles du métier ; réponse en 1-2 phrases orales qui ramènent à l'objectif |
| `scoring` | Critères chaud/tiède/froid (orientent l'échange et l'analyse) | Signaux concrets : irritant, échéance, changement d'équipe, accord |
| `mots_cles_stt` | Mots que la transcription doit reconnaître | Noms d'opérateurs, de produits, sigles, villes, « c'est moi », « moi-même » |
| `repondeur` | Messagerie : `raccrocher` ou `message` | `raccrocher` par défaut (le message n'est pas encore validé en réel) |
| `plages` | Heures d'appel (jours 1 = lundi) | Quand le décideur est joignable et pas débordé |
| `max_tentatives` / `delai_entre_tentatives_h` | Relances | 3 essais au plus (≤ 4 / 30 jours), 48 h d'écart |
| `rdv` | Durée, mode, créneaux | `mode` tel qu'il sera dit ; `plages` = créneaux pris directement ; `jours_a_confirmer` = sous réserve, JB confirme |
| `axonaut` | Pipeline et étape selon l'issue | Noms **exacts** des étapes Axonaut (casse, accents) ; clés = issues (`rdv`, `rappel`, `barrage`, `refus`, `non_qualifie`, `qualifie`, `transfert`) et scores (`chaud`, `tiede`, `froid`) |
| `instructions_specifiques` | Consignes du secteur (priment sur les rôles) | Usages et tabous du métier, ton, sujets à éviter |

## 4. Calibrer une campagne à partir d'un besoin métier

C'est le cœur du travail. Partir de la [fiche brief](fiche-brief.md) remplie avec JB, puis :

1. **Le constat** (`offre.resume`, `accroche`). Chercher ce que le prospect vit *déjà* et
   reconnaîtra en une phrase. EHPAD : « l'installation s'est construite petit à petit, les équipes
   ont changé, plus personne n'a de vue d'ensemble ». Pas de reproche (« ce n'est la faute de
   personne »), pas de jargon, pas de chiffre.
2. **La douleur → le bénéfice → la preuve** (`points_forts`, `preuves`). Pour chaque problème que
   JB sait résoudre : le bénéfice concret pour le client, et si possible un fait vérifiable.
   Toujours relier à l'objectif (le RDV) : le RDV doit apparaître comme la façon simple et sans
   risque d'y voir clair.
3. **La découverte** (`qualification`). Les questions servent à faire parler et à remplir la
   fiche Axonaut après l'appel. Commencer par une question large et facile (« qui s'occupe de… »),
   finir par le circuit de décision et l'échéance. Ne jamais demander un montant.
4. **Les objections** : celles que JB entend vraiment sur le terrain, avec sa propre réponse,
   réécrite à l'oral. Toujours inclure : « on a déjà un prestataire », « pas le temps »,
   « envoyez un mail », « pas de budget », « c'est une IA ? », « ce n'est pas moi qui décide »,
   « combien ça coûte ? ».
5. **Le score** : des signaux observables pendant l'appel, que Sonnet pourra reconnaître.
6. **Le secteur** (`instructions_specifiques`) : usages (urgences, secret médical, horaires,
   hiérarchie), sujets interdits, niveau de formalité.
7. **La logistique** : plages d'appel adaptées au métier, mode et créneaux de RDV, pipeline Axonaut.

Écrire **tout** ce qui sera dit (accroche, réponses aux objections, questions) à l'oral, au
vouvoiement, ton enjoué et avenant, en relisant à voix haute. Ce qui n'est que lu par Claude
(`resume`, `but`, `scoring`) peut être plus dense.

Leçons de la campagne EHPAD (octobre 2026) :

- OpteoLink se présente d'abord comme **acteur local** (Toulouse, Haute-Garonne, Gers),
  « les télécoms », pas « la téléphonie » ni « spécialiste » ;
- ne jamais critiquer le prestataire en place ; l'offre est « un regard extérieur, offert » ;
- une preuve vague mais vraie vaut mieux qu'un chiffre : « d'excellents retours » ;
- au standard, demander nom, email, ligne directe et disponibilités **en une phrase** ;
- l'email est toujours épelé puis confirmé (les noms propres aussi) ;
- les réponses d'objections longues sont raccourcies par Claude : les écrire courtes d'emblée.

## 5. Les scénarios de simulation

Chaque campagne a son fichier `simulations/<id>.yaml` : Claude joue le prospect, Sarah répond
avec les vrais prompts, puis des contrôles automatiques vérifient l'issue. C'est le **test de
recette** de la campagne, sans téléphone ni écriture Axonaut. Voir le
[modèle](modele-simulations.yaml) et `simulations/ehpad.yaml`.

Couvrir au minimum (10 à 20 scénarios) : décideur intéressé jusqu'au RDV ; décideur qui décroche
lui-même ; barrage qui donne les coordonnées ; barrage fermé ; refus net ; « ne m'appelez plus » ;
« c'est un robot ? » ; demande de prix ; « envoyez un mail » ; « déjà un prestataire » ;
« pas le temps, rappelez mardi » ; hors cible ; chaque objection propre au métier.

Contrôles `attendu` : `issues` acceptables, `rdv`, `optout`, `contacts_min`, `requis` et
`interdit` (expressions régulières sur les répliques de Sarah ; réutiliser les interdits de prix).

## 6. Valider et mettre en service

Sur le poste de développement (dépôt `~/GIT/agent-vocal-prospection`) :

```bash
. .venv/bin/activate
pytest -q tests/test_campaign_docs.py   # le modèle et toutes les campagnes se chargent
avp campaign show <id>                  # le YAML est valide (CAMPAIGNS_DIR=campaigns si besoin)
avp prompt show <id>                    # relire le prompt complet tel que Claude le recevra
```

Puis commit en français et push sur `main`. Sur la VM (`ssh root@10.64.0.11`, projet
`/opt/agent-vocal-prospection`) : `campaigns/` et `simulations/` sont montés dans les conteneurs,
un `sudo -u avp git pull` suffit (pas de nouvelle image).

```bash
sudo -u avp git -C /opt/agent-vocal-prospection pull
avp simulation <id> --repetitions 2      # `avp` lance la CLI dans le conteneur orchestrator
```

Lire le rapport (`data/simulations/`), corriger par lot, rejouer. Ensuite :

1. **Axonaut** : JB crée le pipeline et ses étapes avec **exactement** les noms du YAML
   (`docs/axonaut.md`), ou on aligne le YAML sur ses étapes.
2. **Appel de test** sur le portable de JB : `avp call test` (prospect fictif, pas d'écriture
   CRM), puis un prospect de test réel (`avp call prospect <id>`).
3. **Import** : `avp campaign import <id> --axonaut --dry-run`, puis sans `--dry-run`
   (ou `--csv fichier.csv`).
4. **Activation** : `actif: true`, commit, push, `git pull` sur la VM : le démon la prend en
   compte au tour suivant (20 s).

Consigner les résultats dans `docs/journal-tests.md` ; les remarques sur la voix dans
`docs/journal-voix.md`.

## 7. Liste de contrôle avant de proposer une campagne à JB

- [ ] Fiche brief complète ; les trous sont listés comme questions à JB, pas inventés
- [ ] Aucune preuve, chiffre, nom de client ou promesse non validés par JB
- [ ] Accroche = constat reconnaissable + une question ouverte, 2 phrases au plus
- [ ] Tout ce qui sera dit tient en 30 mots, à l'oral, nombres en lettres
- [ ] 5 à 8 questions de qualification, 2 ou 3 prioritaires, `id` uniques
- [ ] 8 à 12 objections dont les incontournables (§ 4)
- [ ] `instructions_specifiques` sans contradiction avec `base.md` ni avec les règles
- [ ] Compatibilité avec les tournures communes « audit / sur place / télécoms » vérifiée (§ 2)
- [ ] Plages d'appel et de RDV adaptées au métier ; `actif: false`
- [ ] Étapes Axonaut identiques à celles du pipeline de JB (ou à créer, signalé)
- [ ] `mots_cles_stt` : opérateurs, produits, sigles, villes du secteur
- [ ] `simulations/<id>.yaml` couvrant le § 5 ; simulation lancée, rapport résumé à JB
